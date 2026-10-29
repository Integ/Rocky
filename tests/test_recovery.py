"""A goal the execution engine failed continues from its saved progress."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from temporalio.common import WorkflowIDReusePolicy

from rocky.api import create_app
from rocky.config import Settings
from rocky.models import Action, Deliverable, GoalContract, PendingApproval, RunInput, RunState
from rocky.service import Dispatcher
from rocky.store import Store
from rocky.workflow import _PERSIST_RETRY, GoalWorkflow

CONTRACT = GoalContract(
    objective="Write a report",
    acceptance_criteria=["Include evidence"],
    deliverables=[Deliverable(path="report.md")],
)
AUTH = {"Authorization": "Bearer owner-token"}


@pytest.fixture
def store(tmp_path):
    database = Store(f"sqlite:///{tmp_path}/recover.db")
    database.initialize()
    try:
        yield database
    finally:
        database.close()


def failed(store, goal_id="task", **changes):
    store.create_goal(goal_id, CONTRACT)
    store.mark_dispatched(goal_id)
    running = RunState(
        goal_id=goal_id,
        revision=7,
        status="running",
        steps=12,
        model_calls=13,
        input_tokens=4000,
        handoff_summary="Sources are saved in notes.md.",
        deadline_at="2026-10-29T00:00:00+00:00",
        pending_approval=PendingApproval(
            fingerprint="a" * 64,
            operation_id="step-0012",
            action=Action(tool="run_command", content="make deploy"),
            reason="Owner approval is required",
        ),
        **changes,
    )
    store.save_state(running)
    terminal = running.model_copy(update={"status": "failed", "reason": "Engine failure"})
    assert store.reconcile_terminal(goal_id, running.revision, terminal)
    return store.get_goal(goal_id)


def test_recovery_requeues_with_progress_and_a_fresh_deadline(store):
    before = failed(store)
    recovered = store.recover_goal("task")
    state = recovered.state
    assert state.status == "queued" and state.revision == before.state.revision + 1
    assert (state.steps, state.model_calls, state.input_tokens) == (12, 13, 4000)
    assert state.handoff_summary == "Sources are saved in notes.md."
    assert state.deadline_at is None
    # The undispatched request stays answerable as a declined one.
    assert state.pending_approval is None
    assert [item.fingerprint for item in state.deferred_approvals] == ["a" * 64]
    assert [goal.goal_id for goal in store.pending_goals()] == ["task"]
    assert store.recovery_requested("task")
    with pytest.raises(ValueError):
        store.recover_goal("task")


@pytest.mark.parametrize("status", ["succeeded", "cancelled", "budget_exhausted"])
def test_only_engine_failures_can_be_recovered(store, status):
    store.create_goal("task", CONTRACT)
    store.save_state(RunState(goal_id="task", revision=1, status=status))
    with pytest.raises(ValueError):
        store.recover_goal("task")
    with pytest.raises(KeyError):
        store.recover_goal("missing")


async def test_dispatcher_restarts_a_recovered_goal_from_its_projection(store):
    failed(store)
    store.recover_goal("task")
    starts = []

    async def start(name, run, **kwargs):
        starts.append((run, kwargs))

    await Dispatcher(SimpleNamespace(start_workflow=start), store, Settings()).tick()
    run, options = starts[0]
    assert options["id_reuse_policy"] == WorkflowIDReusePolicy.ALLOW_DUPLICATE
    assert run.checkpoint.state.steps == 12
    assert "inspect the current state" in run.checkpoint.feedback
    assert not store.recovery_requested("task")
    assert store.pending_goals() == []

    # The restored workflow numbers its next operation after the old ones.
    workflow = GoalWorkflow(run)
    assert workflow.state().steps == 12
    assert workflow.state().goal_id == "task"

    # Ordinary submissions still never reuse a closed execution.
    store.create_goal("fresh", CONTRACT)
    await Dispatcher(SimpleNamespace(start_workflow=start), store, Settings()).tick()
    assert starts[-1][0].checkpoint is None
    assert starts[-1][1]["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE


async def test_reconciliation_ignores_a_queued_recovery(store):
    failed(store)
    store.recover_goal("task")
    engine = SimpleNamespace(
        get_workflow_handle=lambda *_a, **_k: pytest.fail("A queued recovery is not reconciled")
    )
    assert store.reconciliation_goals() == []
    await Dispatcher(engine, store, Settings()).reconcile()


def test_recover_endpoint(store, tmp_path):
    failed(store)
    store.create_goal("done", CONTRACT)
    store.save_state(RunState(goal_id="done", revision=1, status="succeeded"))
    settings = Settings(
        auth_mode="token", api_token="owner-token", workspace_root=tmp_path / "workspaces"
    )
    with TestClient(create_app(settings, store)) as client:
        response = client.post("/goals/task/recover", headers=AUTH)
        assert response.status_code == 202
        assert response.json()["state"]["status"] == "queued"
        assert client.post("/goals/done/recover", headers=AUTH).status_code == 409
        assert client.post("/goals/missing/recover", headers=AUTH).status_code == 404


def test_persistence_retries_until_the_database_recovers():
    assert _PERSIST_RETRY.maximum_attempts == 0
    assert _PERSIST_RETRY.non_retryable_error_types == ["ValueError"]


async def test_persist_has_no_overall_deadline(monkeypatch):
    calls = []

    async def dispatch(name, value, **kwargs):
        calls.append((name, kwargs))

    monkeypatch.setattr("rocky.workflow.workflow.execute_activity", dispatch)
    monkeypatch.setattr("rocky.workflow.workflow.now", lambda: datetime(2026, 10, 29))
    goal = GoalWorkflow(RunInput(goal_id="task", contract=CONTRACT))
    await goal._persist()
    name, options = calls[0]
    assert name == "rocky.persist"
    assert "schedule_to_close_timeout" not in options
    assert options["retry_policy"] is _PERSIST_RETRY
