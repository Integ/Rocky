"""Run Rocky's evaluation suite against a live model; see evals/README.md.

run       execute tasks and write report.json (live model calls; costs tokens)
compare   show per-task changes between two report.json files
baseline  reduce a report.json to the summary worth committing as a baseline
list      list the available tasks
"""

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from rocky import evaluation
from rocky.config import Settings
from rocky.inference import ModelSettings
from rocky.planner import OpenAIPlanner

_KEY_VARIABLES = ("OPENAI_API_KEY", "ROCKY_EVAL_API_KEY")


def _withhold_keys() -> None:
    """Remove API keys from this process's environment once the planner holds them.

    Approved commands, including unsandboxed ones that skip the sandbox's
    environment filter, inherit this environment. No model-proposed command
    may see the key, for example on a CI runner with --approvals auto.
    """
    for name in _KEY_VARIABLES:
        os.environ.pop(name, None)


class KeyFromEnvironment:
    """Planner credential lookup backed by one environment value; never persisted."""

    def __init__(self, key: str | None) -> None:
        self._key = key

    def get(self, account: str) -> str | None:
        return self._key


def _git_commit() -> str:
    try:
        return subprocess.run(
            # "-dirty" marks a run of uncommitted changes, not of that commit.
            ["git", "describe", "--always", "--dirty"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parent,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _model(args) -> tuple[ModelSettings, str | None]:
    if args.provider is None:
        if not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("Set OPENAI_API_KEY, or choose --provider with ROCKY_EVAL_API_KEY.")
        model = args.model or os.environ.get("ROCKY_MODEL") or "gpt-4.1-mini"
        return ModelSettings(model=model, reasoning_effort=args.reasoning_effort), None
    if not args.model:
        raise SystemExit("--model is required with --provider.")
    key = os.environ.get("ROCKY_EVAL_API_KEY") or (
        os.environ.get("OPENAI_API_KEY") if args.provider == "openai" else None
    )
    if not key and args.provider != "ollama":
        raise SystemExit("Set ROCKY_EVAL_API_KEY to the provider's API key.")
    return (
        ModelSettings(
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            provider=args.provider,
            base_url=args.base_url,
            credential_id="evaluation",
        ),
        key,
    )


async def _run(args) -> int:
    tasks = evaluation.load_tasks(args.task)
    model_settings, key = _model(args)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # Outside the repository: shell commands in a workspace must not see this checkout.
    run_dir = (args.run_dir or Path(tempfile.gettempdir()) / "rocky-evals" / stamp).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    log = run_dir / "runs.jsonl"
    total = len(tasks) * args.repeat
    print(f"Running {total} task runs; workspaces and records in {run_dir}", file=sys.stderr)

    def progress(record: dict) -> None:
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        done = sum(1 for _ in log.open(encoding="utf-8"))
        print(
            f"[{done}/{total}] {record['task']} {'PASS' if record['passed'] else 'FAIL'} "
            f"outcome={record['outcome']} steps={record.get('steps', '—')} "
            f"tokens={record.get('tokens', '—')} seconds={record.get('wall_seconds', '—')} "
            f"approvals={record.get('approvals', '—')}",
            file=sys.stderr,
        )

    planner = OpenAIPlanner(
        Settings(model=model_settings.model, tool_access="full", approval_mode="required"),
        credential_store=KeyFromEnvironment(key),
        defer_client=model_settings.provider is not None,
    )
    _withhold_keys()
    try:
        async with await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter,
            dev_server_existing_path=args.temporal_binary,
            dev_server_log_level="error",
        ) as env:
            # Durable waits must take real time: this measures actual efficiency.
            with env.auto_time_skipping_disabled():
                runs = await evaluation.run_suite(
                    tasks,
                    client=env.client,
                    planner=planner,
                    root=run_dir,
                    approvals=args.approvals,
                    repeat=args.repeat,
                    model_settings=model_settings,
                    on_result=progress,
                )
    finally:
        await planner.close()
    report = evaluation.build_report(
        runs,
        {
            "created_at": datetime.now(UTC).isoformat(),
            "git_commit": _git_commit(),
            "model": model_settings.model_dump(mode="json", exclude={"credential_id"}),
            "approvals": args.approvals,
            "repeat": args.repeat,
            "tasks": [task.id for task in tasks],
            "python": platform.python_version(),
        },
    )
    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(evaluation.format_report(report))
    print(f"\nReport: {report_path}")
    if args.baseline:
        return _compare(_load(args.baseline), report, args)
    return 0


def _compare(baseline: dict, current: dict, args) -> int:
    print("\n" + evaluation.compare_reports(baseline, current))
    if not args.fail_on_regression:
        return 0
    problems = evaluation.find_regressions(
        baseline,
        current,
        max_success_drop=args.max_success_drop,
        max_token_increase=args.max_token_increase,
        require_pass=tuple(args.require_pass),
        max_task_tokens=dict(args.max_task_tokens),
    )
    if not problems:
        print("\nNo regression against the baseline.")
        return 0
    print("\nRegressions against the baseline:\n" + "\n".join(f"- {item}" for item in problems))
    return 1


def _add_gate(parser) -> None:
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="exit 1 when the run is worse than the baseline (see --max-* thresholds)",
    )
    parser.add_argument(
        "--max-success-drop",
        type=float,
        default=0.15,
        help="tolerated absolute fall in overall success rate (default 0.15)",
    )
    parser.add_argument(
        "--max-token-increase",
        type=float,
        default=0.25,
        help="tolerated relative rise in mean tokens per passed run (default 0.25)",
    )
    parser.add_argument(
        "--require-pass",
        action="append",
        default=[],
        metavar="TASK",
        help="require this task to pass every run, even if it failed in the baseline (repeatable)",
    )
    parser.add_argument(
        "--max-task-tokens",
        action="append",
        default=[],
        type=_task_token_limit,
        metavar="TASK=TOKENS",
        help="cap this task's mean tokens across passed and failed runs (repeatable)",
    )


def _task_token_limit(value: str) -> tuple[str, int]:
    name, separator, number = value.partition("=")
    try:
        limit = int(number)
    except ValueError:
        limit = 0
    if not name.strip() or not separator or limit <= 0:
        raise argparse.ArgumentTypeError("Expected TASK=TOKENS with a positive integer limit.")
    return name, limit


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run tasks against a live model")
    run.add_argument(
        "--approvals",
        choices=["auto", "cancel"],
        required=True,
        help="auto: approve every shell command and mutating HTTP request the model proposes, "
        "running them as the current user; cancel: stop a task at its first approval request",
    )
    run.add_argument("--task", action="append", help="run only this task (repeatable)")
    run.add_argument("--repeat", type=int, default=1, help="runs per task, to measure variance")
    run.add_argument("--model", help="model ID (default: ROCKY_MODEL or gpt-4.1-mini)")
    run.add_argument(
        "--provider",
        choices=["openai", "openai-compatible", "anthropic", "ollama"],
        help="provider API; the key is read from ROCKY_EVAL_API_KEY",
    )
    run.add_argument("--base-url", help="provider API base URL")
    run.add_argument("--reasoning-effort", help="reasoning effort for supported OpenAI models")
    run.add_argument("--run-dir", type=Path, help="where to keep workspaces and the report")
    run.add_argument("--baseline", type=Path, help="an earlier report.json to compare against")
    run.add_argument(
        "--temporal-binary",
        default=os.environ.get("ROCKY_TEMPORAL_BINARY") or shutil.which("temporal"),
        help="Temporal CLI to run the local dev server (downloaded when absent)",
    )
    _add_gate(run)
    compare = commands.add_parser("compare", help="compare two report.json files")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("current", type=Path)
    _add_gate(compare)
    baseline = commands.add_parser("baseline", help="write a committable baseline summary")
    baseline.add_argument("report", type=Path)
    baseline.add_argument("--output", "-o", type=Path, required=True)
    commands.add_parser("list", help="list tasks")
    args = parser.parse_args()
    if getattr(args, "require_pass", []) or getattr(args, "max_task_tokens", []):
        if not args.fail_on_regression:
            parser.error("task guards require --fail-on-regression")

    if args.command == "list":
        for task in evaluation.load_tasks():
            print(f"{task.id:20} {task.spec.category:14} {task.spec.description}")
        return 0
    if args.command == "compare":
        return _compare(_load(args.baseline), _load(args.current), args)
    if args.command == "baseline":
        summary = evaluation.baseline_summary(_load(args.report))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
        print(f"Baseline: {args.output}")
        return 0
    if args.fail_on_regression and not args.baseline:
        parser.error("--fail-on-regression requires --baseline")
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
