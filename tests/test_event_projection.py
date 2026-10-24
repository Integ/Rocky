"""Polling events stay compact while durable state and execution evidence remain complete."""

import json

import pytest
from sqlalchemy import select

from rocky.models import (
    Action,
    Artifact,
    Deliverable,
    ExecutionStep,
    GoalContract,
    Observation,
    PendingApproval,
    RunState,
    TaskProgress,
)
from rocky.store import EventRow, Store

CONTRACT = GoalContract(
    objective="Produce a checked report",
    acceptance_criteria=["Include source evidence"],
    deliverables=[Deliverable(path="report.md")],
)


@pytest.fixture
def store(tmp_path):
    database = Store(f"sqlite:///{tmp_path}/event-projection.db")
    database.initialize()
    database.create_goal("task", CONTRACT)
    try:
        yield database
    finally:
        database.close()


def observation(step, *, tool="read_file", content=None):
    return Observation(
        step=step,
        tool=tool,
        success=True,
        content=content if content is not None else f"evidence-{step}:" + "e" * 1900,
        progress_key=f"evidence-{step}:{tool}",
    )


def rich_state(revision=1, *, status="completed"):
    return RunState(
        goal_id="task",
        revision=revision,
        status="running",
        reason="Inspecting source evidence",
        steps=6,
        model_calls=7,
        input_tokens=12345,
        output_tokens=2345,
        observations=[observation(step) for step in range(1, 7)],
        artifacts=[Artifact(path="report.md", size=123, sha256="verified-digest")],
        wake_at="2026-10-22T12:00:00+00:00",
        updated_at="2026-10-22T11:59:01+00:00",
        cycle=6,
        phase="act",
        execution_step=ExecutionStep(
            step_id="6:act",
            cycle=6,
            phase="act",
            status=status,
            input={"action": {"tool": "write_file", "content": "i" * 16000}},
            output={"result": {"success": True, "content": "o" * 16000}},
            started_at="2026-10-22T11:59:00+00:00",
            completed_at="2026-10-22T11:59:01+00:00" if status != "started" else None,
        ),
        handoff_summary="handoff evidence: " + "h" * 3000,
        handoff_is_model=True,
        recovery_attempts=2,
        task_progress=TaskProgress(
            completed=["Read source evidence"],
            pending=["Complete the report"],
            evidence=["The source is available"],
        ),
        pending_approval=PendingApproval(
            fingerprint="pending-fingerprint",
            operation_id="pending-operation",
            action=Action(tool="run_command", content="pending command"),
            reason="Confirm this command",
            command_prefix=["git", "status"],
        ),
        deadline_at="2026-10-22T13:00:00+00:00",
        approved_command_prefixes=[["git", "status"]],
    )


def stored_events(store, goal_id="task"):
    with store.sessions() as session:
        return [
            row.state
            for row in session.scalars(
                select(EventRow).where(EventRow.goal_id == goal_id).order_by(EventRow.event_id)
            )
        ]


def assert_compact_event(projected, state):
    full = state.model_dump(mode="json")
    for key in (
        "goal_id",
        "revision",
        "status",
        "reason",
        "steps",
        "model_calls",
        "input_tokens",
        "output_tokens",
        "artifacts",
        "wake_at",
        "updated_at",
        "cycle",
        "phase",
    ):
        assert projected[key] == full[key]
    assert projected["execution_step"] == {
        key: value
        for key, value in full["execution_step"].items()
        if key not in {"input", "output"}
    }
    for key in (
        "handoff_summary",
        "handoff_is_model",
        "task_progress",
        "pending_approval",
        "approved_command_prefixes",
        "recovery_attempts",
        "deadline_at",
    ):
        assert key not in projected


def test_new_events_are_small_without_mutating_state_or_discarding_execution_evidence(store):
    state = rich_state()
    original = state.model_copy(deep=True)
    store.save_state(state)

    projected = stored_events(store)[0]
    assert_compact_event(projected, state)
    assert projected["observations"] == [state.observations[-1].model_dump(mode="json")]
    assert len(json.dumps(projected).encode()) < 3500
    assert len(json.dumps(projected).encode()) < len(state.model_dump_json().encode()) / 10
    assert store.events("task")[0]["state"] == projected
    assert state == original
    assert store.get_goal("task").state == original

    execution = state.execution_step.model_dump(mode="json")
    for records in (store.execution_events("task"), store.execution_steps("task")):
        assert records[0]["input"] == execution["input"]
        assert records[0]["output"] == execution["output"]
    message = next(item for item in store.conversation("task") if item["role"] == "tool")
    assert json.loads(message["content"]) == execution["output"]["result"]


def test_five_persistence_points_do_not_repeat_full_round_payloads(store):
    for revision in range(1, 6):
        state = rich_state(revision)
        state.execution_step.status = "completed" if revision in {3, 5} else "started"
        if revision <= 3:
            state.phase = state.execution_step.phase = "think"
            state.execution_step.step_id = "6:think"
        store.save_state(state)

    rows = stored_events(store)
    assert len(rows) == 5
    # Six ~2 KB observations, a 3 KB handoff, and 16 KB phase input/output must
    # not accumulate into the old 80–100 KB per-round snapshot stream.
    assert sum(len(json.dumps(row).encode()) for row in rows) < 18000
    assert [item["state"]["revision"] for item in store.events("task")] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    "observations", [[], [observation(5), observation(6), observation(6, tool="fetch_url")]]
)
def test_event_keeps_the_entire_latest_parallel_observation_batch(store, observations):
    state = rich_state()
    state.observations = observations
    store.save_state(state)

    expected = [item.model_dump(mode="json") for item in observations if item.step == 6]
    assert stored_events(store)[0]["observations"] == expected
    assert store.events("task")[0]["state"]["observations"] == expected


def test_terminal_reconciliation_uses_compact_events_and_retains_failure_evidence(store):
    started = rich_state(status="started")
    store.mark_dispatched("task")
    store.save_state(started)
    terminal = started.model_copy(deep=True)
    terminal.status = "failed"
    terminal.reason = "The execution engine closed unexpectedly"
    original = terminal.model_copy(deep=True)

    assert store.reconcile_terminal("task", started.revision, terminal)
    persisted = store.get_goal("task").state
    assert terminal == original
    assert persisted.handoff_summary == started.handoff_summary
    assert persisted.observations == started.observations
    assert persisted.execution_step.input == started.execution_step.input
    assert persisted.execution_step.output == {"error": terminal.reason}
    projected = stored_events(store)[-1]
    assert_compact_event(projected, persisted)
    assert projected["execution_step"]["status"] == "failed"
    assert projected["observations"] == [started.observations[-1].model_dump(mode="json")]
    assert store.events("task")[-1]["state"] == projected
    assert len(json.dumps(projected).encode()) < 3500

    assert store.execution_events("task")[-1]["output"] == {"error": terminal.reason}
    assert store.execution_steps("task")[0]["output"] == {"error": terminal.reason}
    message = next(item for item in store.conversation("task") if item["role"] == "system")
    assert json.loads(message["content"]) == {"error": terminal.reason}
    assert not store.reconcile_terminal("task", started.revision, terminal)
    assert len(store.events("task")) == 2


def test_events_project_existing_full_snapshots_without_rewriting_durable_rows(store):
    legacy = rich_state().model_dump(mode="json")
    with store.sessions.begin() as session:
        session.add(EventRow(goal_id="task", state=legacy))

    projected = store.events("task")[0]["state"]
    assert_compact_event(projected, rich_state())
    assert projected["observations"] == [legacy["observations"][-1]]
    assert len(json.dumps(projected).encode()) < 3500
    assert stored_events(store) == [legacy]


def test_events_cursor_and_page_limit_are_scoped_to_the_selected_goal(store):
    store.create_goal("other", CONTRACT)
    for revision in range(1, 5):
        store.save_state(RunState(goal_id="other", revision=revision, status="running"))
        store.save_state(rich_state(revision))

    all_events = store.events("task")
    first = store.events("task", limit=2)
    second = store.events("task", after=first[-1]["event_id"], limit=2)
    assert first + second == all_events
    assert [event["state"]["revision"] for event in all_events] == [1, 2, 3, 4]
    assert all(event["state"]["goal_id"] == "task" for event in first + second)
    assert all_events[0]["event_id"] > 1
    assert store.events("task", after=second[-1]["event_id"]) == []
    assert store.events("missing", after=0) == []


def test_legacy_observation_history_survives_later_empty_state_but_polling_is_compact(store):
    legacy = rich_state()
    legacy.execution_step = None
    store.save_state(legacy)
    store.save_state(RunState(goal_id="task", revision=2, status="running"))

    raw = stored_events(store)
    assert raw[0]["observations"] == [item.model_dump(mode="json") for item in legacy.observations]
    assert "handoff_summary" not in raw[0]
    polled = store.events("task")
    assert polled[0]["state"]["observations"] == [legacy.observations[-1].model_dump(mode="json")]
    assert polled[1]["state"]["observations"] == []
    expected_ids = {f"legacy-observation:{item.step}:{item.tool}" for item in legacy.observations}
    for records in (store.conversation("task"), store.memory_records("task")):
        historical = [item for item in records if item["id"] in expected_ids]
        assert {item["id"] for item in historical} == expected_ids
        assert [json.loads(item["content"])["content"] for item in historical] == [
            item.content for item in legacy.observations
        ]
