"""Command time limits, leftover processes and background jobs."""

import json
import os
import time

import pytest

from rocky import tools as tools_module
from rocky.config import Settings
from rocky.models import Action, Deliverable, GoalContract, RunInput, ToolInput
from rocky.tool_contracts import ActionValidationError, approval_fingerprint, validate_action
from rocky.tools import WorkspaceTools, _operation_fingerprint

RUN = RunInput(
    goal_id="jobs-goal",
    contract=GoalContract(
        objective="Build and test the project",
        acceptance_criteria=["The build log is saved"],
        deliverables=[Deliverable(path="build.log")],
    ),
)


@pytest.fixture
def tools(tmp_path, monkeypatch):
    # Seatbelt is macOS-only; these tests cover process handling around it.
    monkeypatch.setattr(
        tools_module,
        "prepare_sandbox",
        lambda *args, **kwargs: ([], tools_module.worker_free_environment()),
    )
    return WorkspaceTools(Settings(workspace_root=tmp_path / "workspaces", approval_mode="trusted"))


def run(tools, operation_id, content, **options):
    action = Action(tool="run_command", content=content, **options)
    return tools.execute(ToolInput(run=RUN, action=action, operation_id=operation_id))


def wait_for(path, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.05)
    raise AssertionError(f"{path} did not appear")


def test_timeout_seconds_extends_and_limits_a_command(tools):
    slow = run(tools, "slow", "sleep 2; echo done", timeout_seconds=1)
    assert not slow.success
    assert slow.error_code == "command_timeout"
    assert "after 1 seconds" in slow.content
    assert "background=true" in slow.retry_hint
    patient = run(tools, "patient", "sleep 1; echo done", timeout_seconds=5)
    assert patient.success, patient.content
    assert "done" in patient.content


def test_processes_left_by_a_finished_command_are_stopped(tools):
    marker = tools.root / RUN.goal_id / "leftover.txt"
    result = run(tools, "leftover", "(sleep 1; echo late > leftover.txt) &")
    assert result.success, result.content
    time.sleep(1.5)
    assert not marker.exists()


def test_background_job_reports_its_output_and_exit_status(tools):
    result = run(tools, "job", "echo started; sleep 0.2; echo finished; exit 3", background=True)
    assert result.success, result.content
    job_id = result.progress_key.removeprefix("job:")
    jobs = tools.root / RUN.goal_id / ".rocky-jobs"
    assert f".rocky-jobs/{job_id}.log" in result.content
    assert wait_for(jobs / f"{job_id}.exit") == "3"
    assert (jobs / f"{job_id}.log").read_text() == "started\nfinished\n"
    # The record of the process lives outside the task's writable workspace.
    record = json.loads(
        (tools.root / ".rocky" / RUN.goal_id / "jobs" / f"{job_id}.json").read_text()
    )
    assert record["command"].startswith("echo started")


def test_stopping_jobs_ends_running_background_work(tools):
    result = run(tools, "server", "sleep 30; echo never > never.txt", background=True)
    assert result.success, result.content
    job_id = result.progress_key.removeprefix("job:")
    record = json.loads(
        (tools.root / ".rocky" / RUN.goal_id / "jobs" / f"{job_id}.json").read_text()
    )
    os.getpgid(record["pgid"])
    assert tools.stop_jobs(RUN.goal_id) == 1
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and tools._job_running(record):
        time.sleep(0.05)
    assert not tools._job_running(record)
    assert tools.stop_jobs(RUN.goal_id) == 0


def test_job_count_is_bounded(tools, monkeypatch):
    monkeypatch.setattr(tools_module, "MAX_JOBS", 1)
    assert run(tools, "first", "sleep 30", background=True).success
    second = run(tools, "second", "sleep 30", background=True)
    assert not second.success
    assert "already has 1 background jobs" in second.content
    tools.stop_jobs(RUN.goal_id)


def test_deleting_a_task_stops_its_jobs(tools):
    result = run(tools, "orphan", "sleep 30", background=True)
    job_id = result.progress_key.removeprefix("job:")
    record = json.loads(
        (tools.root / ".rocky" / RUN.goal_id / "jobs" / f"{job_id}.json").read_text()
    )
    tools.delete_goal_files(RUN.goal_id)
    time.sleep(0.6)
    assert not tools._job_running(record)


def test_command_options_validate_and_bind_approval():
    with pytest.raises(ActionValidationError, match="does not accept timeout_seconds"):
        validate_action(
            Action(tool="read_file", path="a.txt", timeout_seconds=60), full_access=True
        )
    with pytest.raises(ActionValidationError, match="no timeout"):
        validate_action(
            Action(tool="run_command", content="make", background=True, timeout_seconds=60),
            full_access=True,
        )
    with pytest.raises(ValueError):
        Action(tool="run_command", content="make", timeout_seconds=901)

    def request(**options):
        return ToolInput(
            run=RUN, action=Action(tool="run_command", content="make", **options), operation_id="op"
        )

    plain = request()
    # Unset options leave identities recorded before they existed unchanged.
    assert "timeout_seconds" not in plain.action.model_dump(exclude_none=True)
    assert approval_fingerprint(plain) != approval_fingerprint(request(timeout_seconds=600))
    assert approval_fingerprint(plain) != approval_fingerprint(request(background=True))
    assert _operation_fingerprint(plain) != _operation_fingerprint(request(background=True))
    assert _operation_fingerprint(plain) == _operation_fingerprint(request(background=None))


def test_persisting_a_finished_task_stops_its_jobs():
    import asyncio
    from types import SimpleNamespace

    from rocky.activities import Activities
    from rocky.models import RunState

    stopped = []
    store = SimpleNamespace(save_state=lambda state: None)
    tools = SimpleNamespace(stop_jobs=stopped.append)
    activities = Activities(store, tools, planner=None)
    asyncio.run(activities.persist(RunState(goal_id="g", status="running")))
    asyncio.run(activities.persist(RunState(goal_id="g", status="cancelled")))
    assert stopped == ["g"]
