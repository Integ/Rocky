"""The evaluation suite: task definitions, independent grading, metrics and the runner."""

import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from test_workflow import temporal_env as _temporal_env

from rocky import evaluation
from rocky.models import Action, Decision, PlanResult, VerificationResult

temporal_env = _temporal_env
TASKS = evaluation.load_tasks()
BY_ID = {task.id: task for task in TASKS}


def workspace_for(task: evaluation.EvalTask, destination: Path, *, reference: bool) -> Path:
    if (task.root / "fixtures").is_dir():
        shutil.copytree(task.root / "fixtures", destination)
    else:
        destination.mkdir()
    if reference and (task.root / "reference").is_dir():
        shutil.copytree(task.root / "reference", destination, dirs_exist_ok=True)
    return destination


def test_suite_covers_ten_representative_task_kinds():
    assert len(TASKS) == 10
    assert len({task.spec.category for task in TASKS}) >= 8
    assert {task.spec.expect for task in TASKS} == {"succeeded", "blocked"}
    for task in TASKS:
        contract = task.contract("http://127.0.0.1:9")
        assert contract.limits == evaluation.EVAL_LIMITS
        if task.spec.server:
            assert "http://127.0.0.1:9" in contract.objective
        if task.spec.expect == "succeeded":
            # A reference solution proves each task's checks are satisfiable.
            assert all(
                (task.root / "reference" / item.path).is_file() for item in contract.deliverables
            ), task.id


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id)
def test_reference_passes_and_untouched_fixtures_fail(task, tmp_path):
    solved = workspace_for(task, tmp_path / "solved", reference=True)
    results = evaluation.grade(
        task, solved, outcome=task.spec.expect, reason=task.spec.example_blocked_reason
    )
    assert all(item["passed"] for item in results), [r for r in results if not r["passed"]]

    untouched = workspace_for(task, tmp_path / "untouched", reference=False)
    results = evaluation.grade(task, untouched, outcome="succeeded")
    assert not all(item["passed"] for item in results)


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id)
def test_expected_answers_stay_out_of_the_planner_contract(task):
    contract = task.contract("http://127.0.0.1:9")
    visible = json.dumps(
        [contract.objective, contract.acceptance_criteria, contract.model_dump()["deliverables"]],
        ensure_ascii=False,
    ).casefold()
    answers = []
    for check in task.spec.checks:
        if isinstance(check, evaluation.JsonCheck) and check.equals is not None:
            answers.append(check.equals)
        if isinstance(check, evaluation.JsonRecordsCheck):
            answers.extend(value for fields in check.expected.values() for value in fields.values())
    for answer in answers:
        if isinstance(answer, str) and answer not in {"active", "superseded"}:
            assert answer.casefold() not in visible, answer
        elif isinstance(answer, int | float):
            assert not re.search(rf"(?<![\d.]){re.escape(str(answer))}(?![\d.])", visible), answer


def test_orders_api_requires_login_and_pages_through_every_order():
    reference = json.loads((BY_ID["local-api"].root / "reference/orders-summary.json").read_text())
    with evaluation.fixture_server("orders_api") as base_url:
        with httpx.Client(base_url=base_url, trust_env=False) as client:
            assert client.get("/api/orders?page=1").status_code == 401
            denied = client.post("/login", data={"username": "analyst", "password": "wrong"})
            assert denied.status_code == 401
            assert client.post(
                "/login", json={"username": "analyst", "password": "eval-only-password"}
            ).is_success
            orders, page, pages = [], 1, 0
            while page is not None:
                body = client.get(f"/api/orders?page={page}").json()
                orders += body["orders"]
                page, pages = body["next_page"], pages + 1
            assert client.get("/api/orders?page=99").status_code == 404
    summary = evaluation.orders_summary(orders)
    assert {**summary, "pages": pages} == reference


def test_failed_checks_explain_what_is_wrong(tmp_path):
    spec = evaluation.TaskSpec.model_validate(
        {
            "id": "probe",
            "category": "test",
            "description": "Exercise each check's failure detail.",
            "contract": {},
            "checks": [
                {"type": "json", "path": "out.json", "pointer": "/missing", "equals": 1},
                {"type": "json", "path": "out.json", "pointer": "/total", "equals": 10},
                {
                    "type": "json_records",
                    "path": "out.json",
                    "pointer": "/items",
                    "key": "id",
                    "expected": {"A": {"owner": "Tomás"}, "B": {"owner": "Lena"}},
                },
                {"type": "regex", "path": "absent.md", "pattern": "x", "negate": True},
                {"type": "min_bytes", "path": "out.json", "bytes": 10_000},
                {
                    "type": "command",
                    "argv": ["{python}", "-c", "print('boom'); raise SystemExit(3)"],
                },
            ],
        }
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "out.json").write_text(
        json.dumps(
            {
                "total": 9.5,
                "items": [
                    {"id": "a", "owner": "TOMAS"},
                    {"id": "B", "owner": "Samir"},
                    {"id": "C", "owner": "Noor"},
                ],
            }
        )
    )
    task = evaluation.EvalTask(spec, tmp_path)
    results = evaluation.grade(task, workspace, outcome="succeeded")
    details = {item["check"]: item for item in results}
    assert results[0]["passed"]
    assert [item["passed"] for item in results[1:]] == [False] * 6
    assert details["out.json /missing == 1"]["detail"] == "/missing is missing"
    assert details["out.json /total == 10"]["detail"] == "actual: 9.5"
    records = details["out.json /items has the expected id records"]["detail"]
    # Case and accents are not substantive differences; owners and extras are.
    assert "A." not in records and "B.owner='Samir'" in records and "unexpected: c" in records
    assert "absent.md is missing" in details["absent.md does not match /x/"]["detail"]
    command = next(item for item in results if item["check"].startswith("command"))
    assert command["detail"].startswith("exit 3") and "boom" in command["detail"]


def test_load_tasks_rejects_unknown_names_and_mismatched_ids(tmp_path):
    with pytest.raises(ValueError, match="Unknown evaluation tasks: nope"):
        evaluation.load_tasks(["csv-summary", "nope"])
    assert [task.id for task in evaluation.load_tasks(["fix-bug", "csv-summary"])] == [
        "fix-bug",
        "csv-summary",
    ]
    spec = json.loads((BY_ID["csv-summary"].root / "task.json").read_text())
    (tmp_path / "renamed").mkdir()
    (tmp_path / "renamed/task.json").write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="does not match its directory"):
        evaluation.load_tasks(directory=tmp_path)


def step(phase, status="completed", *, decision=None, output=None, feedback="", seconds=1.0):
    return {
        "phase": phase,
        "status": status,
        "input": {"feedback": feedback, "decision": decision},
        "output": output or {},
        "started_at": "2026-10-21T00:00:00+00:00",
        "completed_at": f"2026-10-21T00:00:{seconds:06.3f}+00:00",
    }


def act(tool, path, success=True):
    return step(
        "act",
        "completed" if success else "failed",
        decision={"kind": "act", "action": {"tool": tool, "path": path}},
        output={"result": {"success": success}},
        seconds=2.0,
    )


def test_step_metrics_count_tools_repeats_recoveries_and_time():
    steps = [
        step("think", seconds=3.0),
        act("read_file", "a.txt"),
        step("think"),
        act("read_file", "a.txt"),
        step("think", feedback="Recovery required: change strategy"),
        act("run_command", None, success=False),
        step("think", "failed"),
        step("act", decision={"kind": "wait", "action": None}),
        step("think"),
        step("complete", output={"verification": {"passed": False}}),
        step("think"),
        step("complete", output={"verification": {"passed": True}}),
    ]
    metrics = evaluation.step_metrics(steps)
    assert metrics["tool_calls"] == 3 and metrics["tools"] == {"read_file": 2, "run_command": 1}
    assert metrics["duplicate_actions"] == 1 and metrics["tool_failures"] == 1
    assert metrics["waits"] == 1 and metrics["recoveries"] == 1 and metrics["planning_errors"] == 1
    assert metrics["verifications"] == 2 and metrics["verification_failures"] == 1
    assert metrics["phase_seconds"] == {"act": 7.0, "complete": 2.0, "think": 8.0}


def run_record(task, passed, *, status="succeeded", steps=10, tokens=1000, approvals=0):
    return {
        "task": task,
        "category": "code",
        "attempt": 1,
        "outcome": status,
        "status": status,
        "passed": passed,
        "false_success": status == "succeeded" and not passed,
        "steps": steps,
        "model_calls": steps + 1,
        "tokens": tokens,
        "wall_seconds": steps * 2.0,
        "approvals": approvals,
        "tool_calls": steps - 1,
        "duplicate_actions": 0,
        "recoveries": 0,
    }


def test_reports_summarize_flag_false_successes_and_compare_runs():
    baseline = evaluation.build_report(
        [
            run_record("fix-bug", True, steps=10, tokens=2000, approvals=4),
            run_record("fix-bug", False, steps=20, tokens=4000),
            {
                "task": "docs-qa",
                "category": "research",
                "attempt": 1,
                "outcome": "error",
                "passed": False,
                "false_success": False,
                "error": "TimeoutError",
            },
        ],
        {"git_commit": "abc"},
    )
    overall = baseline["summary"]["overall"]
    assert (overall["runs"], overall["passed"], overall["success_rate"]) == (3, 1, 0.333)
    assert overall["false_successes"] == 1 and overall["errors"] == 1
    assert overall["steps"] == 15 and baseline["summary"]["passed_runs"]["steps"] == 10
    assert baseline["summary"]["by_task"]["docs-qa"]["steps"] is None
    table = evaluation.format_report(baseline)
    assert "FALSE SUCCESS" in table and "| docs-qa | FAIL | error |" in table

    current = evaluation.build_report(
        [run_record("fix-bug", True, steps=6, tokens=1500, approvals=1)] * 2, {}
    )
    comparison = evaluation.compare_reports(baseline, current)
    assert "| fix-bug | 0.5 → 1 (+100%) | 15 → 6 (-60%) | 3000 → 1500 (-50%)" in comparison
    assert comparison.splitlines()[-1].startswith("| overall |")


# --- The runner against the real workflow, tools and a scripted planner -----


@dataclass
class ScriptedPlanner:
    """Stands in for the model: scripted decisions, and a reviewer that cites the
    last command for every required value, as an honest review of computed output would."""

    decisions: list
    plans: list = field(default_factory=list)
    reviews: list = field(default_factory=list)

    async def plan(self, request) -> PlanResult:
        self.plans.append(request)
        item = self.decisions[min(len(self.plans), len(self.decisions)) - 1]
        decision = item(request) if callable(item) else item
        return PlanResult(decision=decision, input_tokens=100, output_tokens=20)

    async def review(
        self, request, artifacts_text, evidence=None, checklist=None, verified=None
    ) -> VerificationResult:
        self.reviews.append((evidence, checklist, verified))
        commands = [item for item in evidence["actions"] if item["tool"] == "run_command"]
        # Without a command, cite whatever wrote the files: the audit decides.
        writers = commands or [item for item in evidence["actions"] if item.get("changed")]
        step = writers[-1] if writers else {"step": 0, "target": ""}
        claims = [
            {
                "deliverable": item["deliverable"],
                "pointer": item["pointer"],
                "value": str(item["value"]),
                "kind": "computed",
                "location": f"step:{step['step']}",
                "quote": step["target"][:24],
                "supported": True,
            }
            for item in checklist or []
        ]
        return VerificationResult(
            passed=True, feedback="Matches", claims=claims, input_tokens=50, output_tokens=10
        )


def finish() -> Decision:
    return Decision(kind="finish", reason="Deliverables are complete")


def write_reference(task_id: str, path: str) -> Decision:
    content = (BY_ID[task_id].root / "reference" / path).read_text()
    return Decision(
        kind="act",
        reason=f"Write {path}",
        action=Action(tool="write_file", path=path, content=content),
    )


def inspect_command() -> Decision:
    return Decision(
        kind="act",
        reason="Inspect the data",
        action=Action(tool="run_command", content="head -3 sales.csv"),
    )


SUMMARY_SCRIPT = """python3 - <<'PY'
import csv, json
rows = list(csv.DictReader(open('sales.csv'))){limit}
revenue = lambda row: int(row['units']) * float(row['unit_price'])
regions, units = {{}}, {{}}
for row in rows:
    regions[row['region']] = regions.get(row['region'], 0) + revenue(row)
    units[row['product']] = units.get(row['product'], 0) + int(row['units'])
summary = {{
    'total_revenue': round(sum(map(revenue, rows)), 2),
    'revenue_by_region': {{name: round(value, 2) for name, value in regions.items()}},
    'top_product_by_units': max(units, key=units.get),
    'row_count': len(rows),
}}
json.dump(summary, open('summary.json', 'w'))
print(json.dumps(summary))
PY"""


def compute_summary(limit: str = "") -> Decision:
    return Decision(
        kind="act",
        reason="Compute the summary from every row",
        action=Action(tool="run_command", content=SUMMARY_SCRIPT.format(limit=limit)),
    )


async def run(temporal_env, tmp_path, task_id, planner, approvals="auto") -> dict:
    [record] = await evaluation.run_suite(
        [BY_ID[task_id]],
        client=temporal_env.client,
        planner=planner,
        root=tmp_path,
        approvals=approvals,
        poll_seconds=0.1,
    )
    return record


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt execution requires macOS")
async def test_runner_approves_counts_and_grades_independently(temporal_env, tmp_path):
    planner = ScriptedPlanner([compute_summary(), finish()])
    record = await run(temporal_env, tmp_path, "csv-summary", planner)
    assert record["passed"] and record["outcome"] == record["status"] == "succeeded"
    assert record["approvals"] == 1 and record["false_success"] is False
    assert (record["steps"], record["model_calls"], record["tokens"]) == (2, 3, 300)
    assert record["tools"] == {"run_command": 1}
    assert record["tool_failures"] == record["duplicate_actions"] == 0
    assert record["verifications"] == 1 and record["wall_seconds"] > 0
    # The approved command ran for real; the next planning call saw its output
    # and the file it wrote.
    latest = planner.plans[1].memory["conversation"][0]
    assert latest["role"] == "tool" and '"row_count": 180' in latest["content"]
    assert '"changed_paths":["summary.json"]' in latest["content"]
    # The reviewer received the application's evidence and the values to check.
    [(evidence, checklist, verified)] = planner.reviews
    assert evidence["deliverables"] == {"summary.json": ["step 1 run_command"]}
    [source] = evidence["sources"]
    assert (source["path"], source["lines"]) == ("sales.csv", 181)
    # Rocky verified every value itself: the command that wrote them computed them.
    assert checklist == []
    assert {item["pointer"] for item in verified} >= {"/row_count", "/revenue_by_region/North"}
    assert {item["support"] for item in verified} == {"computed by step 1 run_command"}


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_hand_written_figures_fail_rockys_claim_audit(temporal_env, tmp_path):
    planner = ScriptedPlanner([write_reference("csv-summary", "summary.json"), finish()])
    record = await run(temporal_env, tmp_path, "csv-summary", planner)
    # Correct numbers, but nothing the task ran produced them: no success.
    assert record["status"] != "succeeded" and record["false_success"] is False
    assert record["verification_failures"] >= 1
    feedback = planner.plans[2].feedback
    assert "Unsupported claims" in feedback and "summary.json /total_revenue" in feedback
    assert "computed values must come from a command the task ran" in feedback


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt execution requires macOS")
async def test_runner_flags_a_success_the_checks_reject(temporal_env, tmp_path):
    # A real but buggy computation: provenance holds, so only the grader can tell.
    planner = ScriptedPlanner([compute_summary("[:10]"), finish()])
    record = await run(temporal_env, tmp_path, "csv-summary", planner)
    assert record["status"] == "succeeded" and not record["passed"]
    assert record["false_success"] is True
    assert any(not item["passed"] and "/row_count" in item["check"] for item in record["checks"])


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_cancel_policy_stops_at_the_first_approval_request(temporal_env, tmp_path):
    planner = ScriptedPlanner([inspect_command(), finish()])
    record = await run(temporal_env, tmp_path, "csv-summary", planner, approvals="cancel")
    assert record["outcome"] == "approval_required" and record["status"] == "cancelled"
    assert record["approvals"] == 1 and not record["passed"]
    assert len(planner.plans) == 1


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_request_for_missing_information_is_the_expected_outcome(temporal_env, tmp_path):
    reason = BY_ID["missing-info"].spec.example_blocked_reason
    planner = ScriptedPlanner([Decision(kind="blocked", reason=reason)])
    record = await run(temporal_env, tmp_path, "missing-info", planner)
    assert record["outcome"] == "blocked" and record["status"] == "cancelled"
    assert record["passed"] and record["reason"] == reason
    assert record["steps"] == 1 and record["tool_calls"] == 0


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt execution requires macOS")
async def test_local_api_task_logs_in_and_pages_through_rocky_http_tools(temporal_env, tmp_path):
    def base_url(request) -> str:
        return re.search(r"http://127\.0\.0\.1:\d+", request.run.contract.objective).group()

    def login(request) -> Decision:
        options = {
            "method": "POST",
            "data": {"username": "analyst", "password": "eval-only-password"},
        }
        return Decision(
            kind="act",
            reason="Log in",
            action=Action(
                tool="http_request", url=base_url(request) + "/login", content=json.dumps(options)
            ),
        )

    def page(number):
        return lambda request: Decision(
            kind="act",
            reason=f"Save page {number}",
            action=Action(
                tool="fetch_url",
                url=f"{base_url(request)}/api/orders?page={number}",
                path=f"pages/{number}.json",
            ),
        )

    summarise = Decision(
        kind="act",
        reason="Summarise every saved page",
        action=Action(
            tool="run_command",
            content="""python3 - <<'PY'
import glob, json
files = sorted(glob.glob('pages/*.json'))
orders = [order for name in files for order in json.load(open(name))['orders']]
totals = {}
for order in orders:
    totals[order['customer']] = totals.get(order['customer'], 0) + order['amount']
summary = {
    'order_count': len(orders),
    'total_amount': round(sum(order['amount'] for order in orders), 2),
    'top_customer': max(totals, key=totals.get),
    'pages': len(files),
}
json.dump(summary, open('orders-summary.json', 'w'))
print(json.dumps(summary))
PY""",
        ),
    )
    planner = ScriptedPlanner(
        [login, *[page(number) for number in range(1, 5)], summarise, finish()]
    )
    record = await run(temporal_env, tmp_path, "local-api", planner)
    assert record["passed"] and record["approvals"] == 2
    # Every page was served to the logged-in session: no 401 tool failures.
    assert record["tools"] == {"fetch_url": 4, "http_request": 1, "run_command": 1}
    assert record["tool_failures"] == 0
    [(evidence, _, _)] = planner.reviews
    downloads = [item for item in evidence["actions"] if item["tool"] == "fetch_url"]
    assert [item["changed"] for item in downloads] == [[f"pages/{n}.json"] for n in range(1, 5)]
