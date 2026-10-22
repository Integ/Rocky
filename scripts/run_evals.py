"""Run Rocky's evaluation suite against a live model; see evals/README.md.

run      execute tasks and write report.json (live model calls; costs tokens)
compare  show per-task changes between two report.json files
list     list the available tasks
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


class KeyFromEnvironment:
    """Planner credential lookup backed by one environment value; never persisted."""

    def __init__(self, key: str | None) -> None:
        self._key = key

    def get(self, account: str) -> str | None:
        return self._key


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
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
        print("\n" + evaluation.compare_reports(_load(args.baseline), report))
    return 0


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
    compare = commands.add_parser("compare", help="compare two report.json files")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("current", type=Path)
    commands.add_parser("list", help="list tasks")
    args = parser.parse_args()

    if args.command == "list":
        for task in evaluation.load_tasks():
            print(f"{task.id:20} {task.spec.category:14} {task.spec.description}")
        return 0
    if args.command == "compare":
        print(evaluation.compare_reports(_load(args.baseline), _load(args.current)))
        return 0
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
