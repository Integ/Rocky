"""Live evaluation of Rocky on representative tasks.

Each task lives in evals/tasks/<id>. task.json holds the goal contract, the
expected outcome and independent checks; fixtures/ seeds the goal workspace;
hidden/ holds grader-only files such as held-out tests; reference/ is a
known-good result that the test suite uses to validate the checks themselves.

The grader never relies on Rocky's own verification. A run that Rocky reports
as succeeded but that fails a check is counted as a false success. Expected
answers live only in the checks, never in the contract the planner reads.
"""

import asyncio
import json
import math
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from temporalio.client import Client
from temporalio.worker import Worker

from rocky.activities import Activities
from rocky.config import Settings
from rocky.inference import ModelSettings
from rocky.models import ExecutionLimits, GoalContract
from rocky.service import Dispatcher
from rocky.store import Store
from rocky.tools import WorkspaceTools
from rocky.workflow import GoalWorkflow

TASKS_DIR = Path(__file__).resolve().parents[2] / "evals" / "tasks"
# Generous enough for real work, bounded so one runaway task cannot spend the
# default 500K-token budget. A task.json may set its own contract limits.
EVAL_LIMITS = ExecutionLimits(
    max_steps=40, max_model_calls=60, max_tokens=400_000, timeout_seconds=1200
)
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "budget_exhausted", "deadline_exceeded"})
Approvals = Literal["auto", "cancel"]


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("Check paths must be relative to the goal workspace")
    return value


def _pattern(value: str | None) -> str | None:
    if value is not None:
        re.compile(value)
    return value


class _FileCheck(_Spec):
    path: str

    relative_path = field_validator("path")(_relative)


class ExistsCheck(_FileCheck):
    type: Literal["exists"]


class MinBytesCheck(_FileCheck):
    type: Literal["min_bytes"]
    bytes: int = Field(ge=1)


class RegexCheck(_FileCheck):
    type: Literal["regex"]
    pattern: str
    negate: bool = False

    valid_pattern = field_validator("pattern")(_pattern)


class JsonCheck(_FileCheck):
    """Compare one JSON value: equality (with a numeric tolerance) or a regex."""

    type: Literal["json"]
    pointer: str
    equals: Any = None
    tolerance: float = Field(default=0.0, ge=0)
    matches: str | None = None
    negate: bool = False

    valid_matches = field_validator("matches")(_pattern)

    @model_validator(mode="after")
    def one_comparison(self) -> "JsonCheck":
        if (self.equals is None) == (self.matches is None):
            raise ValueError("A json check needs exactly one of equals or matches")
        return self


class JsonRecordsCheck(_FileCheck):
    """Exactly one record per expected key, with the expected field values."""

    type: Literal["json_records"]
    pointer: str = ""
    key: str
    expected: dict[str, dict[str, Any]] = Field(min_length=1)


class CommandCheck(_Spec):
    """Run a command in a copy of the workspace with hidden/ files added."""

    type: Literal["command"]
    argv: list[str] = Field(min_length=1)
    timeout: int = Field(default=120, ge=1, le=600)


class ReasonCheck(_Spec):
    """Match the reason Rocky gave when it stopped, e.g. what input it needs."""

    type: Literal["reason"]
    pattern: str

    valid_pattern = field_validator("pattern")(_pattern)


Check = Annotated[
    ExistsCheck
    | MinBytesCheck
    | RegexCheck
    | JsonCheck
    | JsonRecordsCheck
    | CommandCheck
    | ReasonCheck,
    Field(discriminator="type"),
]


class TaskSpec(_Spec):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    category: str = Field(min_length=1)
    description: str = Field(min_length=1)
    contract: dict[str, Any]
    expect: Literal["succeeded", "blocked"] = "succeeded"
    server: Literal["orders_api"] | None = None
    checks: list[Check] = Field(min_length=1)
    # Used only by the test suite to grade a correct blocked outcome.
    example_blocked_reason: str = ""


@dataclass(frozen=True)
class EvalTask:
    spec: TaskSpec
    root: Path

    @property
    def id(self) -> str:
        return self.spec.id

    def contract(self, base_url: str = "") -> GoalContract:
        # URLs contain no JSON metacharacters, so textual substitution is safe.
        data = json.loads(json.dumps(self.spec.contract).replace("{base_url}", base_url))
        data.setdefault("limits", EVAL_LIMITS.model_dump())
        return GoalContract.model_validate(data)


def load_tasks(names: list[str] | None = None, directory: Path = TASKS_DIR) -> list[EvalTask]:
    tasks = []
    for path in sorted(directory.glob("*/task.json")):
        spec = TaskSpec.model_validate_json(path.read_text(encoding="utf-8"))
        if spec.id != path.parent.name:
            raise ValueError(f"{path}: id {spec.id!r} does not match its directory")
        task = EvalTask(spec, path.parent)
        task.contract("http://127.0.0.1:1")  # Validate the contract up front.
        tasks.append(task)
    if names:
        known = {task.id: task for task in tasks}
        unknown = [name for name in names if name not in known]
        if unknown:
            raise ValueError("Unknown evaluation tasks: " + ", ".join(unknown))
        tasks = [known[name] for name in dict.fromkeys(names)]
    return tasks


# --- Fixture servers ---------------------------------------------------------

ORDERS_USERNAME = "analyst"
ORDERS_PASSWORD = "eval-only-password"
ORDERS_PAGE_SIZE = 25


def orders_fixture() -> list[dict]:
    rng = random.Random(1021)
    customers = [
        "Acme Corp",
        "Blue Harbor",
        "Cobalt Labs",
        "Delta Foods",
        "Evergreen Co",
        "Fjord Media",
        "Granite AG",
        "Helix Bio",
    ]
    return [
        {
            "id": f"ORD-{1000 + index}",
            "customer": rng.choice(customers),
            "amount": round(rng.uniform(15, 900), 2),
            "created_at": f"2026-10-{1 + index % 28:02d}",
        }
        for index in range(100)
    ]


def orders_summary(orders: list[dict]) -> dict:
    totals = Counter()
    for order in orders:
        totals[order["customer"]] += order["amount"]
    return {
        "order_count": len(orders),
        "total_amount": round(sum(order["amount"] for order in orders), 2),
        "top_customer": max(totals, key=totals.get),
        "pages": math.ceil(len(orders) / ORDERS_PAGE_SIZE),
    }


def _orders_handler(orders: list[dict]) -> type[BaseHTTPRequestHandler]:
    sessions: set[str] = set()
    pages = math.ceil(len(orders) / ORDERS_PAGE_SIZE)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _send(self, status: int, body: dict, cookie: str = "") -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if cookie:
                self.send_header("Set-Cookie", f"session={cookie}; Path=/; HttpOnly")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/login":
                return self._send(404, {"error": "not found"})
            length = min(int(self.headers.get("Content-Length") or 0), 65536)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            fields: Any
            if "json" in (self.headers.get("Content-Type") or ""):
                try:
                    fields = json.loads(raw)
                except ValueError:
                    fields = {}
            else:
                fields = {key: values[0] for key, values in parse_qs(raw).items()}
            if not isinstance(fields, dict) or (
                fields.get("username"),
                fields.get("password"),
            ) != (ORDERS_USERNAME, ORDERS_PASSWORD):
                return self._send(401, {"error": "invalid credentials"})
            token = uuid4().hex
            sessions.add(token)
            self._send(200, {"ok": True}, cookie=token)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path != "/api/orders":
                return self._send(404, {"error": "not found"})
            cookie = SimpleCookie(self.headers.get("Cookie") or "").get("session")
            if cookie is None or cookie.value not in sessions:
                return self._send(401, {"error": "login required"})
            try:
                page = int(parse_qs(url.query).get("page", ["1"])[0])
            except ValueError:
                return self._send(400, {"error": "page must be an integer"})
            if not 1 <= page <= pages:
                return self._send(404, {"error": "no such page"})
            start = (page - 1) * ORDERS_PAGE_SIZE
            self._send(
                200,
                {
                    "page": page,
                    "orders": orders[start : start + ORDERS_PAGE_SIZE],
                    "next_page": page + 1 if page < pages else None,
                },
            )

    return Handler


@contextmanager
def fixture_server(name: str | None) -> Iterator[str]:
    """Serve a task's local dependency on a loopback port; yield its base URL."""
    if name is None:
        yield ""
        return
    server = ThreadingHTTPServer(("127.0.0.1", 0), _orders_handler(orders_fixture()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


# --- Grading -----------------------------------------------------------------


def _normalized(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.strip().casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _same(actual: Any, expected: Any, tolerance: float = 0.0) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual is expected
    if isinstance(expected, int | float):
        return isinstance(actual, int | float) and abs(actual - expected) <= tolerance + 1e-9
    if isinstance(expected, str):
        return isinstance(actual, str) and _normalized(actual) == _normalized(expected)
    return actual == expected


def _pointer(document: Any, pointer: str) -> Any:
    for token in pointer.split("/")[1:] if pointer else []:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(document, list):
            document = document[int(token)]
        elif isinstance(document, dict):
            document = document[token]
        else:
            raise KeyError(token)
    return document


def _file(workspace: Path, path: str) -> Path | None:
    target = workspace / path
    return target if target.is_file() else None


def _json_file(workspace: Path, path: str) -> Any:
    target = _file(workspace, path)
    if target is None:
        raise FileNotFoundError(f"{path} is missing")
    return json.loads(target.read_text(encoding="utf-8"))


def _check_exists(task: EvalTask, workspace: Path, check: ExistsCheck, reason: str):
    return _file(workspace, check.path) is not None, ""


def _check_min_bytes(task: EvalTask, workspace: Path, check: MinBytesCheck, reason: str):
    target = _file(workspace, check.path)
    size = target.stat().st_size if target else 0
    return size >= check.bytes, f"{size} bytes"


def _check_regex(task: EvalTask, workspace: Path, check: RegexCheck, reason: str):
    target = _file(workspace, check.path)
    if target is None:
        return False, f"{check.path} is missing"
    found = re.search(check.pattern, target.read_text(encoding="utf-8", errors="replace"))
    return bool(found) != check.negate, ""


def _check_json(task: EvalTask, workspace: Path, check: JsonCheck, reason: str):
    try:
        value = _pointer(_json_file(workspace, check.path), check.pointer)
    except (LookupError, ValueError):
        return False, f"{check.pointer} is missing"
    if check.matches is not None:
        passed = isinstance(value, str | int | float) and bool(re.search(check.matches, str(value)))
    else:
        passed = _same(value, check.equals, check.tolerance)
    return passed != check.negate, f"actual: {json.dumps(value, ensure_ascii=False)[:200]}"


def _check_records(task: EvalTask, workspace: Path, check: JsonRecordsCheck, reason: str):
    try:
        records = _pointer(_json_file(workspace, check.path), check.pointer)
    except (LookupError, ValueError):
        return False, f"{check.pointer or 'document'} is missing"
    if not isinstance(records, list):
        return False, f"{check.pointer or 'document'} is not a list"
    by_key: dict[str, list[dict]] = {}
    for record in records:
        if isinstance(record, dict) and check.key in record:
            by_key.setdefault(_normalized(str(record[check.key])), []).append(record)
    problems = []
    for identity, fields in check.expected.items():
        found = by_key.pop(_normalized(identity), [])
        if len(found) != 1:
            problems.append(f"{identity}: {len(found)} records")
            continue
        problems.extend(
            f"{identity}.{field}={found[0].get(field)!r}, expected {value!r}"
            for field, value in fields.items()
            if not _same(found[0].get(field), value)
        )
    if by_key:
        problems.append("unexpected: " + ", ".join(sorted(by_key)))
    return not problems, "; ".join(problems)[:500]


def _check_command(task: EvalTask, workspace: Path, check: CommandCheck, reason: str):
    with tempfile.TemporaryDirectory(prefix="rocky-eval-check-") as scratch:
        target = Path(scratch) / "workspace"
        shutil.copytree(workspace, target, symlinks=True)
        if (task.root / "hidden").is_dir():
            # Held-out files replace any same-named file the agent wrote.
            shutil.copytree(task.root / "hidden", target, dirs_exist_ok=True)
        argv = [sys.executable if part == "{python}" else part for part in check.argv]
        try:
            completed = subprocess.run(
                argv,
                cwd=target,
                capture_output=True,
                text=True,
                timeout=check.timeout,
                stdin=subprocess.DEVNULL,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            return False, f"timed out after {check.timeout} seconds"
    detail = f"exit {completed.returncode}"
    if completed.returncode:
        detail += "\n" + (completed.stdout + completed.stderr)[-1500:]
    return completed.returncode == 0, detail


def _check_reason(task: EvalTask, workspace: Path, check: ReasonCheck, reason: str):
    return bool(re.search(check.pattern, reason)), ""


_CHECKS: dict[str, Callable] = {
    "exists": _check_exists,
    "min_bytes": _check_min_bytes,
    "regex": _check_regex,
    "json": _check_json,
    "json_records": _check_records,
    "command": _check_command,
    "reason": _check_reason,
}


def _label(check) -> str:
    match check:
        case ExistsCheck():
            return f"{check.path} exists"
        case MinBytesCheck():
            return f"{check.path} has at least {check.bytes} bytes"
        case RegexCheck():
            return (
                f"{check.path} {'does not match' if check.negate else 'matches'} /{check.pattern}/"
            )
        case JsonCheck():
            test = f"== {check.equals!r}" if check.matches is None else f"~ /{check.matches}/"
            return f"{check.path} {check.pointer} {'not ' if check.negate else ''}{test}"
        case JsonRecordsCheck():
            return f"{check.path} {check.pointer or '/'} has the expected {check.key} records"
        case CommandCheck():
            return "command: " + " ".join(check.argv)
        case ReasonCheck():
            return f"stop reason matches /{check.pattern}/"
    raise TypeError(check)


def grade(task: EvalTask, workspace: Path, *, outcome: str, reason: str = "") -> list[dict]:
    """Independent checks of the actual workspace and outcome."""
    results = [
        {
            "check": f"outcome is {task.spec.expect}",
            "passed": outcome == task.spec.expect,
            "detail": f"outcome: {outcome}",
        }
    ]
    for check in task.spec.checks:
        try:
            passed, detail = _CHECKS[check.type](task, workspace, check, reason)
        except Exception as error:
            passed, detail = False, f"{type(error).__name__}: {error}"[:500]
        results.append({"check": _label(check), "passed": passed, "detail": detail})
    return results


# --- Metrics -----------------------------------------------------------------


def _seconds(step: dict) -> float | None:
    try:
        started = datetime.fromisoformat(step["started_at"])
        return (datetime.fromisoformat(step["completed_at"]) - started).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def step_metrics(steps: list[dict]) -> dict:
    """Summarize the execution record: tool use, repeats, recoveries and time."""
    phase_seconds: Counter = Counter()
    tools: Counter = Counter()
    seen: set[str] = set()
    counts = Counter()
    for step in steps:
        phase = step.get("phase")
        inputs = step.get("input") or {}
        output = step.get("output") or {}
        if (seconds := _seconds(step)) is not None:
            phase_seconds[phase] += seconds
        if phase == "think":
            counts["planning_errors"] += step.get("status") == "failed"
            counts["recoveries"] += str(inputs.get("feedback", "")).startswith("Recovery required")
        elif phase == "act":
            decision = inputs.get("decision") or {}
            if decision.get("kind") == "wait":
                counts["waits"] += 1
            if decision.get("kind") != "act":
                continue
            if isinstance(output.get("results"), list):
                outcomes = [(item.get("action"), item.get("result")) for item in output["results"]]
            elif isinstance(decision.get("actions"), list):
                outcomes = [(action, None) for action in decision["actions"]]
            else:
                outcomes = [(decision.get("action"), output.get("result"))]
            for action, result in outcomes:
                if not isinstance(action, dict):
                    continue
                counts["tool_calls"] += 1
                tools[str(action.get("tool"))] += 1
                identity = json.dumps(
                    {key: action.get(key) for key in ("tool", "path", "url", "content")},
                    sort_keys=True,
                )
                counts["duplicate_actions"] += identity in seen
                seen.add(identity)
                counts["tool_failures"] += (
                    not result.get("success")
                    if isinstance(result, dict)
                    else step.get("status") == "failed"
                )
        elif phase == "complete":
            counts["verifications"] += 1
            verification = output.get("verification")
            counts["verification_failures"] += not (
                isinstance(verification, dict) and verification.get("passed")
            )
    return {
        **{
            name: counts[name]
            for name in (
                "tool_calls",
                "tool_failures",
                "duplicate_actions",
                "waits",
                "verifications",
                "verification_failures",
                "planning_errors",
                "recoveries",
            )
        },
        "tools": dict(sorted(tools.items())),
        "phase_seconds": {name: round(value, 2) for name, value in sorted(phase_seconds.items())},
    }


# --- Runner ------------------------------------------------------------------


@dataclass
class _Outcome:
    outcome: str
    reason: str
    approvals: int


async def _command(store: Store, goal_id: str, kind: str, payload: str = "") -> None:
    try:
        await asyncio.to_thread(store.enqueue_command, goal_id, kind, payload)
    except ValueError:
        pass  # The task moved on or ended; the next poll observes it.


async def _drive(
    goal_id: str,
    store: Store,
    dispatcher: Dispatcher,
    approvals: Approvals,
    *,
    limit_seconds: float,
    poll_seconds: float,
) -> _Outcome:
    """Act as the owner: answer approvals per policy, stop at requests for input."""
    deadline = time.monotonic() + limit_seconds
    answered: set[str] = set()
    count = 0
    stop: tuple[str, str] | None = None
    stop_deadline = math.inf
    while True:
        await dispatcher.tick()
        state = (await asyncio.to_thread(store.get_goal, goal_id)).state
        if state.status in TERMINAL:
            outcome, reason = stop or (state.status, state.reason)
            return _Outcome(outcome, reason, count)
        if stop is None:
            pending = state.pending_approval
            if pending is not None:
                if pending.fingerprint not in answered:
                    answered.add(pending.fingerprint)
                    count += 1
                    if approvals == "auto":
                        await _command(store, goal_id, "approve", pending.fingerprint)
                    else:
                        stop = (
                            "approval_required",
                            f"Approval requested for {pending.action.tool}",
                        )
            elif state.status == "blocked":
                stop = ("blocked", state.reason)
            elif time.monotonic() > deadline:
                stop = ("runner_timeout", "The runner stopped a task that outlived its limits.")
            if stop is not None:
                stop_deadline = time.monotonic() + 120
                await _command(store, goal_id, "cancel")
        elif time.monotonic() > stop_deadline:
            raise TimeoutError("The task did not stop after cancellation")
        await asyncio.sleep(poll_seconds)


async def _run_task(
    task: EvalTask,
    attempt: int,
    *,
    settings: Settings,
    store: Store,
    dispatcher: Dispatcher,
    approvals: Approvals,
    model_settings: ModelSettings | None,
    poll_seconds: float,
) -> dict:
    goal_id = uuid4().hex
    workspace = settings.workspace_root / goal_id
    if (task.root / "fixtures").is_dir():
        shutil.copytree(task.root / "fixtures", workspace)
    else:
        workspace.mkdir(parents=True)
    with fixture_server(task.spec.server) as base_url:
        contract = task.contract(base_url)
        started = time.monotonic()
        await asyncio.to_thread(store.create_goal, goal_id, contract, model_settings)
        result = await _drive(
            goal_id,
            store,
            dispatcher,
            approvals,
            limit_seconds=(contract.limits.timeout_seconds or 3600) + 60,
            poll_seconds=poll_seconds,
        )
        wall_seconds = time.monotonic() - started
    state = (await asyncio.to_thread(store.get_goal, goal_id)).state
    steps = await asyncio.to_thread(store.all_execution_steps, goal_id)
    checks = await asyncio.to_thread(
        grade, task, workspace, outcome=result.outcome, reason=result.reason
    )
    passed = all(item["passed"] for item in checks)
    return {
        "task": task.id,
        "category": task.spec.category,
        "attempt": attempt,
        "goal_id": goal_id,
        "expected": task.spec.expect,
        "outcome": result.outcome,
        "status": state.status,
        "passed": passed,
        # Rocky's own verifier accepted work that the independent checks reject.
        "false_success": state.status == "succeeded" and not passed,
        "reason": result.reason[:500],
        "steps": state.steps,
        "model_calls": state.model_calls,
        "input_tokens": state.input_tokens,
        "output_tokens": state.output_tokens,
        "cached_input_tokens": state.cached_input_tokens,
        "tokens": state.input_tokens + state.output_tokens,
        "wall_seconds": round(wall_seconds, 2),
        "approvals": result.approvals,
        **step_metrics(steps),
        "checks": checks,
    }


async def run_suite(
    tasks: list[EvalTask],
    *,
    client: Client,
    planner,
    root: Path,
    approvals: Approvals,
    repeat: int = 1,
    model_settings: ModelSettings | None = None,
    poll_seconds: float = 0.5,
    on_result: Callable[[dict], None] | None = None,
) -> list[dict]:
    """Run each task through the real workflow, tools and planner, one at a time.

    Tasks run sequentially so timings are not distorted by each other or by
    provider rate limits. Workspaces and the SQLite record stay under root.
    """
    settings = Settings(
        database_url=f"sqlite:///{root / 'rocky.db'}",
        workspace_root=root / "workspaces",
        credential_path=root / "credentials.json",
        task_queue="rocky-eval-" + uuid4().hex,
        tool_access="full",
        approval_mode="required",
    )
    store = Store(settings.database_url)
    store.initialize()
    activities = Activities(store, WorkspaceTools(settings), planner)
    dispatcher = Dispatcher(client, store, settings)
    results = []
    try:
        async with Worker(
            client,
            task_queue=settings.task_queue,
            workflows=[GoalWorkflow],
            activities=[
                activities.plan,
                activities.execute,
                activities.verify,
                activities.persist,
                activities.phase,
            ],
            max_concurrent_activities=settings.max_concurrent_activities,
        ):
            for task in tasks:
                for attempt in range(1, repeat + 1):
                    try:
                        record = await _run_task(
                            task,
                            attempt,
                            settings=settings,
                            store=store,
                            dispatcher=dispatcher,
                            approvals=approvals,
                            model_settings=model_settings,
                            poll_seconds=poll_seconds,
                        )
                    except Exception as error:
                        # Exception text can echo provider data; keep the type only.
                        record = {
                            "task": task.id,
                            "category": task.spec.category,
                            "attempt": attempt,
                            "expected": task.spec.expect,
                            "outcome": "error",
                            "passed": False,
                            "false_success": False,
                            "error": type(error).__name__,
                        }
                    results.append(record)
                    if on_result is not None:
                        on_result(record)
    finally:
        store.close()
    return results


# --- Reports -----------------------------------------------------------------

METRICS = (
    "steps",
    "model_calls",
    "tokens",
    "cached_input_tokens",
    "wall_seconds",
    "approvals",
    "tool_calls",
    "duplicate_actions",
    "recoveries",
)


def _aggregate(runs: list[dict]) -> dict:
    passed = sum(1 for run in runs if run["passed"])
    summary: dict[str, Any] = {
        "runs": len(runs),
        "passed": passed,
        "success_rate": round(passed / len(runs), 3) if runs else None,
        "false_successes": sum(1 for run in runs if run.get("false_success")),
        "errors": sum(1 for run in runs if run.get("outcome") == "error"),
    }
    measured = [run for run in runs if "steps" in run]
    for name in METRICS:
        # Records from older versions may lack metrics added since.
        values = [run[name] for run in measured if name in run]
        summary[name] = round(statistics.fmean(values), 2) if values else None
    return summary


def summarize(runs: list[dict]) -> dict:
    by_task: dict[str, list[dict]] = {}
    by_category: dict[str, list[dict]] = {}
    for run in runs:
        by_task.setdefault(run["task"], []).append(run)
        by_category.setdefault(run["category"], []).append(run)
    return {
        "overall": _aggregate(runs),
        # Efficiency only means something for work that was actually done.
        "passed_runs": _aggregate([run for run in runs if run["passed"]]),
        "by_category": {name: _aggregate(items) for name, items in sorted(by_category.items())},
        "by_task": {name: _aggregate(items) for name, items in by_task.items()},
    }


def build_report(runs: list[dict], metadata: dict) -> dict:
    return {"schema": 1, **metadata, "summary": summarize(runs), "runs": runs}


def _number(value: Any) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def format_report(report: dict) -> str:
    lines = [
        "| Task | Result | Outcome | Steps | Calls | Tokens | Seconds | Approvals | Repeated actions |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for run in report["runs"]:
        name = run["task"] + (f" #{run['attempt']}" if run.get("attempt", 1) > 1 else "")
        result = (
            "PASS" if run["passed"] else ("FALSE SUCCESS" if run.get("false_success") else "FAIL")
        )
        lines.append(
            f"| {name} | {result} | {run['outcome']} | "
            + " | ".join(
                _number(run.get(key))
                for key in ("steps", "model_calls", "tokens", "wall_seconds", "approvals")
            )
            + f" | {_number(run.get('duplicate_actions'))} |"
        )
    overall = report["summary"]["overall"]
    efficient = report["summary"]["passed_runs"]
    lines += [
        "",
        f"Passed {overall['passed']}/{overall['runs']} "
        f"(false successes: {overall['false_successes']}, errors: {overall['errors']}).",
        "Mean per passed run: "
        + ", ".join(f"{name} {_number(efficient[name])}" for name in METRICS),
    ]
    return "\n".join(lines)


def _change(before: Any, after: Any) -> str:
    if before is None or after is None:
        return f"{_number(before)} → {_number(after)}"
    text = f"{_number(before)} → {_number(after)}"
    if before:
        text += f" ({(after - before) / before:+.0%})"
    return text


def _model_identity(report: dict) -> tuple:
    model = report.get("model") or {}
    return tuple(model.get(key) for key in ("provider", "base_url", "model", "reasoning_effort"))


def compare_reports(baseline: dict, current: dict) -> str:
    """Per-task change in success rate and mean cost between two reports."""
    columns = ("success_rate", "steps", "tokens", "wall_seconds", "approvals")
    lines = []
    if _model_identity(baseline) != _model_identity(current):
        lines += [
            "Warning: the reports used different models; differences are not comparable.",
            "",
        ]
    lines += [
        "| Task | " + " | ".join(columns) + " |",
        "|---|" + "---|" * len(columns),
    ]
    before_tasks = baseline["summary"]["by_task"]
    after_tasks = current["summary"]["by_task"]
    rows = [
        (name, before_tasks[name], after_tasks[name])
        for name in after_tasks
        if name in before_tasks
    ]
    rows.append(("overall", baseline["summary"]["overall"], current["summary"]["overall"]))
    for name, before, after in rows:
        lines.append(
            f"| {name} | "
            + " | ".join(_change(before.get(column), after.get(column)) for column in columns)
            + " |"
        )
    return "\n".join(lines)


def baseline_summary(report: dict) -> dict:
    """What a committed baseline keeps: run metadata and the summary, without
    per-run records, check output or workspace paths."""
    return {key: value for key, value in report.items() if key != "runs"}


def find_regressions(
    baseline: dict,
    current: dict,
    *,
    max_success_drop: float = 0.15,
    max_token_increase: float = 0.25,
    require_pass: tuple[str, ...] = (),
    max_task_tokens: dict[str, int] | None = None,
) -> list[str]:
    """Reasons the current report is worse than the baseline; empty when it is not.

    Success may fall by up to max_success_drop (absolute) and mean tokens per
    passed run may grow by up to max_token_increase (relative) before failing,
    because single runs of a live model vary. Any rise in the false-success
    rate fails: Rocky claiming unverified work is never noise to tolerate.
    Optional task guards require every repetition to pass or cap mean tokens
    across all measured runs, including failures. They are absolute checks,
    so an already failing baseline cannot normalize a persistent failure.
    """
    problems = []
    if _model_identity(baseline) != _model_identity(current):
        problems.append("The baseline was recorded with a different model or provider.")
    before_tasks = set(baseline["summary"]["by_task"])
    after_tasks = set(current["summary"]["by_task"])
    if before_tasks != after_tasks:
        problems.append(
            "The task sets differ: "
            + ", ".join(
                f"{label} {', '.join(sorted(names))}"
                for label, names in (
                    ("missing", before_tasks - after_tasks),
                    ("new", after_tasks - before_tasks),
                )
                if names
            )
            + "."
        )
    before, after = baseline["summary"]["overall"], current["summary"]["overall"]
    if after.get("errors"):
        problems.append(f"{after['errors']} runs ended in a runner error.")
    if before.get("success_rate") is not None and after.get("success_rate") is not None:
        drop = before["success_rate"] - after["success_rate"]
        if drop > max_success_drop + 1e-9:
            problems.append(
                f"Success rate fell from {_number(before['success_rate'])} "
                f"to {_number(after['success_rate'])}."
            )

    def false_rate(summary: dict) -> float:
        return summary.get("false_successes", 0) / summary["runs"] if summary.get("runs") else 0.0

    if false_rate(after) > false_rate(before) + 1e-9:
        problems.append(
            f"False successes rose from {before.get('false_successes', 0)}/{before.get('runs', 0)} "
            f"to {after.get('false_successes', 0)}/{after.get('runs', 0)}."
        )
    before_tokens = baseline["summary"]["passed_runs"].get("tokens")
    after_tokens = current["summary"]["passed_runs"].get("tokens")
    if before_tokens and after_tokens is not None:
        growth = (after_tokens - before_tokens) / before_tokens
        if growth > max_token_increase + 1e-9:
            problems.append(
                f"Mean tokens per passed run rose {growth:+.0%} "
                f"({_number(before_tokens)} → {_number(after_tokens)})."
            )
    tasks = current["summary"]["by_task"]
    for name in dict.fromkeys(require_pass):
        task = tasks.get(name, {})
        if not task.get("runs") or task.get("passed") != task["runs"]:
            problems.append(f"Required task {name} did not pass every run (or was not run).")
    for name, limit in (max_task_tokens or {}).items():
        tokens = tasks.get(name, {}).get("tokens")
        if tokens is None or tokens > limit:
            problems.append(
                f"Task {name} mean tokens {_number(tokens)} exceeds limit {limit} "
                "or has no measurement."
            )
    return problems
