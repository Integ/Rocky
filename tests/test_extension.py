"""A goal that ran out of budget or time continues with larger limits."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from temporalio.common import WorkflowIDReusePolicy

from rocky.api import create_app
from rocky.config import Settings
from rocky.models import Deliverable, ExecutionLimits, GoalContract, RunState
from rocky.service import Dispatcher
from rocky.store import Store
from rocky.workflow import GoalWorkflow

CONTRACT = GoalContract(
    objective="Write a report",
    acceptance_criteria=["Include evidence"],
    deliverables=[Deliverable(path="report.md")],
    limits=ExecutionLimits(
        max_steps=12, max_model_calls=20, max_tokens=50_000, timeout_seconds=600
    ),
)
AUTH = {"Authorization": "Bearer owner-token"}
LARGER = ExecutionLimits(max_steps=24, max_model_calls=40, max_tokens=100_000, timeout_seconds=1200)


@pytest.fixture
def store(tmp_path):
    database = Store(f"sqlite:///{tmp_path}/extend.db")
    database.initialize()
    try:
        yield database
    finally:
        database.close()


def exhausted(store, status="budget_exhausted", goal_id="task"):
    store.create_goal(goal_id, CONTRACT)
    store.mark_dispatched(goal_id)
    running = RunState(
        goal_id=goal_id,
        revision=5,
        status="running",
        steps=12,
        model_calls=14,
        input_tokens=30_000,
        output_tokens=2_000,
        handoff_summary="Draft is in report.md; section 3 remains.",
        deadline_at="2026-10-30T00:00:00+00:00",
    )
    store.save_state(running)
    terminal = running.model_copy(update={"status": status, "reason": "limit reached"})
    assert store.reconcile_terminal(goal_id, running.revision, terminal)
    return store.get_goal(goal_id)


@pytest.mark.parametrize("status", ["budget_exhausted", "deadline_exceeded"])
def test_extension_keeps_progress_and_raises_the_limits(store, status):
    before = exhausted(store, status)
    extended = store.extend_goal("task", LARGER)
    assert extended.contract.limits == LARGER
    assert extended.contract.objective == before.contract.objective
    state = extended.state
    assert state.status == "queued" and state.deadline_at is None
    assert (state.steps, state.model_calls) == (12, 14)
    assert state.handoff_summary.startswith("Draft is in report.md")
    assert store.recovery_requested("task")
    # Stored, so the next execution and later reads use the new limits.
    assert store.get_goal("task").contract.limits == LARGER


def test_limits_must_leave_room_beyond_what_was_used(store):
    exhausted(store)
    too_small = LARGER.model_copy(update={"max_steps": 12})
    with pytest.raises(ValueError, match="no room"):
        store.extend_goal("task", too_small)
    assert store.get_goal("task").state.status == "budget_exhausted"
    unlimited = ExecutionLimits(
        max_steps=None, max_model_calls=None, max_tokens=None, timeout_seconds=None
    )
    assert store.extend_goal("task", unlimited).state.status == "queued"


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "running"])
def test_only_budget_or_time_ends_can_be_extended(store, status):
    store.create_goal("task", CONTRACT)
    store.save_state(RunState(goal_id="task", revision=1, status=status))
    with pytest.raises(ValueError):
        store.extend_goal("task", LARGER)
    with pytest.raises(KeyError):
        store.extend_goal("missing", LARGER)


async def test_dispatcher_continues_an_extended_goal_with_its_new_limits(store):
    exhausted(store)
    store.extend_goal("task", LARGER)
    starts = []

    async def start(name, run, **kwargs):
        starts.append((run, kwargs))

    await Dispatcher(SimpleNamespace(start_workflow=start), store, Settings()).tick()
    run, options = starts[0]
    assert options["id_reuse_policy"] == WorkflowIDReusePolicy.ALLOW_DUPLICATE
    assert run.contract.limits == LARGER
    assert run.checkpoint.state.steps == 12
    assert "raised the limits" in run.checkpoint.feedback
    workflow = GoalWorkflow(run)
    assert workflow._limits == LARGER


def test_extend_endpoint(store, tmp_path):
    exhausted(store)
    settings = Settings(
        auth_mode="token", api_token="owner-token", workspace_root=tmp_path / "workspaces"
    )
    body = LARGER.model_dump()
    with TestClient(create_app(settings, store)) as client:
        response = client.post("/goals/task/extend", headers=AUTH, json=body)
        assert response.status_code == 202
        assert response.json()["state"]["status"] == "queued"
        assert client.post("/goals/task/extend", headers=AUTH, json=body).status_code == 409
        assert client.post("/goals/missing/extend", headers=AUTH, json=body).status_code == 404
