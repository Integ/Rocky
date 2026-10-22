"""Closed Temporal executions cannot leave an apparently running SQL task."""

import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest
from temporalio.client import WorkflowExecutionStatus, WorkflowHistoryEventFilterType

from rocky.config import Settings
from rocky.models import Artifact, Deliverable, ExecutionStep, GoalContract, RunState
from rocky.service import Dispatcher
from rocky.store import Store

CONTRACT = GoalContract(
    objective="Write a report",
    acceptance_criteria=["Include evidence"],
    deliverables=[Deliverable(path="report.md")],
)


@pytest.fixture
def store(tmp_path):
    database = Store(f"sqlite:///{tmp_path}/reconcile.db")
    database.initialize()
    try:
        yield database
    finally:
        database.close()


def active(store, goal_id="task", revision=5):
    store.create_goal(goal_id, CONTRACT)
    store.mark_dispatched(goal_id)
    state = RunState(
        goal_id=goal_id,
        revision=revision,
        status="running",
        steps=4,
        cycle=3,
        phase="act",
        wake_at="2026-10-22T00:00:00+00:00",
        handoff_summary="The source is saved in the workspace.",
        execution_step=ExecutionStep(
            step_id=f"{goal_id}:3:act",
            cycle=3,
            phase="act",
            started_at="2026-10-21T00:00:00+00:00",
        ),
    )
    store.save_state(state)
    return state


class Engine:
    def __init__(self, status, *, result=None, termination_reason="Operator terminated"):
        self.status, self.outcome, self.termination_reason = status, result, termination_reason
        self.handles, self.descriptions, self.histories = [], [], []
        self.before_confirmation = None
        self.confirmation_run_id = "closed-run"

    def get_workflow_handle(self, goal_id, **options):
        self.handles.append((goal_id, options))

        async def describe(**kwargs):
            self.descriptions.append(goal_id)
            if self.descriptions.count(goal_id) > 1:
                if self.before_confirmation:
                    self.before_confirmation()
                return SimpleNamespace(status=self.status, run_id=self.confirmation_run_id)
            return SimpleNamespace(status=self.status, run_id="closed-run")

        async def result(**kwargs):
            assert kwargs["follow_runs"] is False
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

        async def fetch_history(**kwargs):
            self.histories.append(kwargs)
            return SimpleNamespace(
                events=[
                    SimpleNamespace(
                        workflow_execution_terminated_event_attributes=SimpleNamespace(
                            reason=self.termination_reason
                        )
                    )
                ]
            )

        return SimpleNamespace(describe=describe, result=result, fetch_history=fetch_history)


@pytest.mark.parametrize(
    ("engine_status", "projected_status"),
    [
        (WorkflowExecutionStatus.FAILED, "failed"),
        (WorkflowExecutionStatus.CANCELED, "cancelled"),
        (WorkflowExecutionStatus.TIMED_OUT, "failed"),
        (WorkflowExecutionStatus.TERMINATED, "failed"),
    ],
)
async def test_closed_engine_outcomes_are_projected_and_late_activities_are_fenced(
    store, engine_status, projected_status
):
    old = active(store)
    engine = Engine(engine_status)
    dispatcher = Dispatcher(engine, store, Settings())
    await dispatcher.reconcile()
    state = store.get_goal("task").state
    assert state.status == projected_status and state.revision == old.revision + 1
    assert state.wake_at is None
    assert state.execution_step.status == "failed"
    assert state.handoff_summary == old.handoff_summary
    assert store.execution_steps("task")[0]["status"] == "failed"
    assert store.reconciliation_goals() == []
    store.save_state(old.model_copy(update={"revision": 999, "status": "running"}))
    assert store.get_goal("task").state == state
    await dispatcher.reconcile()
    assert len(engine.descriptions) == 2
    assert len(store.events("task")) == 2


async def test_history_limit_termination_is_actionable_without_copying_engine_details(
    store, caplog
):
    active(store)
    secret = "sensitive-engine-details-should-never-be-persisted"
    engine = Engine(
        WorkflowExecutionStatus.TERMINATED,
        termination_reason="Workflow history size exceeds limit. " + secret,
    )
    await Dispatcher(engine, store, Settings()).reconcile()
    state = store.get_goal("task").state
    assert state.status == "failed"
    assert "history" in state.reason and "rollover" in state.reason and "recover" in state.reason
    assert secret not in state.model_dump_json() and secret not in caplog.text
    assert engine.histories[0]["event_filter_type"] == WorkflowHistoryEventFilterType.CLOSE_EVENT
    assert engine.handles[1][1] == {"run_id": "closed-run", "result_type": RunState}


async def test_completed_result_preserves_verified_artifacts_and_final_usage(store):
    active(store)
    outcome = RunState(
        goal_id="task",
        revision=8,
        status="succeeded",
        reason="Raw private response",
        steps=5,
        model_calls=6,
        input_tokens=123,
        output_tokens=45,
        artifacts=[Artifact(path="report.md", size=25, sha256="verified-digest")],
    )
    engine = Engine(WorkflowExecutionStatus.COMPLETED, result=outcome)
    await Dispatcher(engine, store, Settings()).reconcile()
    state = store.get_goal("task").state
    assert state.status == "succeeded" and state.revision == 8
    assert state.artifacts == outcome.artifacts
    assert (state.steps, state.model_calls, state.input_tokens, state.output_tokens) == (
        5,
        6,
        123,
        45,
    )
    assert "Raw private response" not in state.reason
    assert engine.histories == []


@pytest.mark.parametrize("result", [None, {}, RunState(goal_id="other", status="succeeded")])
async def test_invalid_completed_result_never_claims_success(store, result):
    active(store)
    await Dispatcher(
        Engine(WorkflowExecutionStatus.COMPLETED, result=result), store, Settings()
    ).reconcile()
    state = store.get_goal("task").state
    assert state.status == "failed" and "valid terminal task result" in state.reason


@pytest.mark.parametrize(
    "status", [WorkflowExecutionStatus.RUNNING, WorkflowExecutionStatus.CONTINUED_AS_NEW, None]
)
async def test_running_or_unrecognized_engine_state_is_not_changed_or_restarted(store, status):
    before = active(store)
    await Dispatcher(Engine(status), store, Settings()).reconcile()
    assert store.get_goal("task").state == before


async def test_transport_failure_never_exposes_error_or_changes_projection(store, caplog):
    before = active(store)
    secret = "credential-bearing-engine-response"
    await Dispatcher(
        Engine(WorkflowExecutionStatus.COMPLETED, result=RuntimeError(secret)), store, Settings()
    ).reconcile()
    assert store.get_goal("task").state == before
    assert secret not in caplog.text


async def test_newer_projection_or_recovered_run_wins_over_a_stale_engine_observation(store):
    old = active(store)
    newer = old.model_copy(update={"revision": old.revision + 1, "reason": "New activity progress"})
    engine = Engine(WorkflowExecutionStatus.FAILED)
    engine.before_confirmation = lambda: store.save_state(newer)
    await Dispatcher(engine, store, Settings()).reconcile()
    assert store.get_goal("task").state == newer

    recovered_engine = Engine(WorkflowExecutionStatus.FAILED)
    recovered_engine.confirmation_run_id = "recovered-run"
    await Dispatcher(recovered_engine, store, Settings()).reconcile()
    assert store.get_goal("task").state == newer


def test_terminal_compare_and_set_rejects_duplicates_deletion_and_invalid_targets(store):
    before = active(store)
    terminal = before.model_copy(update={"status": "failed", "reason": "Execution closed"})
    assert store.reconcile_terminal("task", before.revision, terminal)
    assert not store.reconcile_terminal("task", before.revision, terminal)
    store.delete_goal("task")
    assert not store.reconcile_terminal("task", before.revision + 1, terminal)
    assert store.get_goal("task") is None and store.events("task") == []
    assert store.execution_steps("task") == [] and store.memory_records("task") == []
    with pytest.raises(ValueError):
        store.reconcile_terminal("different", before.revision, terminal)


async def test_reconciliation_scans_bounded_batches_without_starving_later_tasks(store):
    for index in range(102):
        active(store, goal_id=f"task-{index:03d}")
    engine = Engine(WorkflowExecutionStatus.RUNNING)
    dispatcher = Dispatcher(engine, store, Settings())
    await dispatcher.reconcile()
    assert len(engine.descriptions) == 100
    await dispatcher.reconcile()
    assert len(engine.descriptions) == 102
    assert engine.descriptions[-2:] == ["task-100", "task-101"]


async def test_slow_reconciliation_does_not_block_dispatch_and_is_throttled(store, monkeypatch):
    active(store)
    started, release, second_tick = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0
    dispatcher = Dispatcher(Engine(WorkflowExecutionStatus.RUNNING), store, Settings())

    async def slow_reconcile(goal):
        started.set()
        await release.wait()

    async def tick():
        nonlocal calls
        calls += 1
        if calls >= 2:
            second_tick.set()

    monkeypatch.setattr(dispatcher, "_reconcile_goal", slow_reconcile)
    monkeypatch.setattr(dispatcher, "tick", tick)
    task = asyncio.create_task(dispatcher.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(second_tick.wait(), timeout=2)
        assert calls >= 2
        release.set()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
