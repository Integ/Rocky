"""Durable harness traces and memory use the same transaction as goal state."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, current_thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from rocky.activities import Activities
from rocky.api import create_app
from rocky.config import Settings
from rocky.memory import conservative_token_count, serialize_memory
from rocky.models import (
    PHASES,
    Decision,
    Deliverable,
    ExecutionStep,
    GoalContract,
    Observation,
    PlanInput,
    PlanResult,
    RunInput,
    RunState,
)
from rocky.store import (
    CommandRow,
    ExecutionEventRow,
    ExecutionStepRow,
    KnowledgeRow,
    MessageRow,
    Store,
)

CONTRACT = GoalContract(
    objective="Produce a checked report",
    acceptance_criteria=["Include source evidence"],
    deliverables=[Deliverable(path="report.md")],
)
AUTH = {"Authorization": "Bearer harness-test-token"}


def phase(cycle=1, phase="think", status="completed", **output):
    return ExecutionStep(
        step_id=f"{cycle}:{phase}",
        cycle=cycle,
        phase=phase,
        status=status,
        output=output,
        started_at="2026-10-21T10:00:00+00:00",
        completed_at="2026-10-21T10:00:01+00:00" if status != "started" else None,
    )


def snapshot(revision, execution_step, status="running"):
    return RunState(
        goal_id="task-1",
        revision=revision,
        status=status,
        cycle=execution_step.cycle,
        phase=execution_step.phase,
        execution_step=execution_step,
        handoff_summary="Read the source; next produce the report.",
    )


def test_trace_messages_and_knowledge_survive_restart_without_duplicate_retries(tmp_path):
    url = f"sqlite:///{tmp_path}/harness.db"
    store = Store(url)
    store.initialize()
    store.create_goal("task-1", CONTRACT)
    store.create_goal("task-1", CONTRACT)
    command_id = store.enqueue_command("task-1", "wake", "Use the new evidence")
    store.put_knowledge("task-1", "An established fact", key="fact", priority=5)
    store.save_state(snapshot(1, phase(status="started")))
    completed = snapshot(2, phase(decision={"kind": "act", "reason": "Inspect evidence"}))
    store.save_state(completed)
    store.save_state(completed)
    store.save_state(snapshot(1, phase(cycle=99, decision={"kind": "blocked"})))
    store.save_state(snapshot(3, phase(phase="act", result={"success": True, "content": "Source"})))
    store.close()

    reopened = Store(url)
    try:
        reopened.initialize()
        steps = reopened.execution_steps("task-1")
        assert [item["phase"] for item in steps] == ["think", "act"]
        assert all(item["status"] == "completed" for item in steps)
        assert steps[0]["output"]["decision"]["kind"] == "act"
        assert reopened.execution_steps("task-1", after=steps[0]["sequence_id"]) == steps[1:]
        assert reopened.execution_steps("task-1", limit=1) == steps[:1]
        messages = reopened.conversation("task-1")
        assert [item["role"] for item in messages] == ["user", "user", "assistant", "tool"]
        assert json.loads(messages[0]["content"])["objective"] == CONTRACT.objective
        assert messages[1]["id"] == f"wake:{command_id}"
        assert messages[1]["content"] == "Use the new evidence"
        assert reopened.get_goal("task-1").state.handoff_summary.startswith("Read the source")
        records = reopened.memory_records("task-1")
        assert records[:-1] == messages
        assert records[-1]["kind"] == "knowledge" and records[-1]["id"] == "fact"
        assert reopened.goal_messages("task-1") == [
            {"command_id": command_id, "message": "Use the new evidence", "sent": False}
        ]
    finally:
        reopened.close()


def test_knowledge_updates_are_scoped_and_deleted_task_traces_cannot_reappear(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/delete.db")
    store.initialize()
    try:
        store.create_goal("task-1", CONTRACT)
        store.create_goal("task-2", CONTRACT)
        store.put_knowledge("task-1", "Old", key="shared")
        store.put_knowledge("task-1", "Updated", key="shared", priority=9)
        store.put_knowledge("task-2", "Other task", key="shared")
        assert len(store.knowledge("task-1")) == 1
        assert store.knowledge("task-1")[0]["content"] == "Updated"
        store.save_state(snapshot(1, phase(decision={"kind": "finish"}), status="succeeded"))
        store.delete_goal("task-1")
        store.save_state(snapshot(100, phase(cycle=99, decision={"kind": "act"})))
        assert store.memory_records("task-1") == []
        assert store.execution_steps("task-1") == []
        assert store.knowledge("task-2")[0]["content"] == "Other task"
        with pytest.raises(KeyError):
            store.put_knowledge("task-1", "Do not resurrect")
        with store.sessions() as session:
            for table in (MessageRow, ExecutionStepRow, ExecutionEventRow, KnowledgeRow):
                assert not list(session.scalars(select(table).where(table.goal_id == "task-1")))
    finally:
        store.close()


def test_execution_events_preserve_started_input_and_final_output_with_duration(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/event-log.db")
    store.initialize()
    store.create_goal("task-1", CONTRACT)
    try:
        started = phase(phase="act", status="started")
        started.input = {"action": {"tool": "fetch_url", "url": "https://example.test"}}
        store.save_state(snapshot(1, started))
        completed = started.model_copy(deep=True)
        completed.status = "completed"
        completed.completed_at = "2026-10-21T10:00:01.250+00:00"
        completed.output = {"result": {"success": True, "content": "Actual evidence"}}
        store.save_state(snapshot(2, completed))
        store.save_state(snapshot(2, completed))  # Lost response retry.
        store.save_state(snapshot(3, completed))  # Unchanged phase projection.
        store.save_state(snapshot(1, started))  # Delayed stale writer.
        events = store.execution_events("task-1")
        assert [item["status"] for item in events] == ["started", "completed"]
        assert [item["revision"] for item in events] == [1, 2]
        assert events[0]["output"] == {}
        assert events[0]["input"] == started.input
        assert events[0]["duration_ms"] is None
        assert events[1]["duration_ms"] == 1250
        assert events[1]["output"] == completed.output
        assert store.execution_steps("task-1")[0]["status"] == "completed"
        assert store.execution_events("task-1", after=events[0]["sequence_id"]) == events[1:]
    finally:
        store.close()


def test_curated_memory_corrections_retract_old_evidence_and_reject_stale_updates(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/memory.db")
    store.initialize()
    try:
        original = store.put_memory(
            "research-citations", "preference", "Prefer original sources", keywords="research"
        )
        assert (
            store.put_memory(
                "research-citations", "preference", "Prefer original sources", keywords="research"
            )
            == original
        )
        with pytest.raises(ValueError, match="current revision"):
            store.put_memory("research-citations", "correction", "Use primary and dated sources")
        corrected = store.put_memory(
            "research-citations",
            "correction",
            "Use primary and dated sources",
            keywords="research",
            source="Owner clarified the research policy",
            expected_revision=original["revision"],
        )
        assert corrected["revision"] == 2
        with pytest.raises(ValueError, match="current revision"):
            store.put_memory(
                "research-citations", "preference", "Obsolete advice", expected_revision=1
            )
        assert store.memories(query="research")[0]["content"] == corrected["content"]
        assert len(store.memories(query="research")) == 1
        assert store.memories(query="gardening") == []
        with pytest.raises(ValueError, match="current revision"):
            store.archive_memory("research-citations", expected_revision=1)
        retracted = store.archive_memory("research-citations", expected_revision=2)
        assert retracted["revision"] == 3
        assert store.memories(query="research") == []
        assert store.archive_memory("research-citations", expected_revision=2) == retracted
        assert store.memories(include_inactive=True) == [retracted]
        with pytest.raises(ValueError):
            store.put_memory("raw-log", "transcript", "Do not promote a log to memory")
    finally:
        store.close()


def test_recall_bounds_recent_messages_and_retrieves_only_relevant_curated_memory(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/bounded-recall.db")
    store.initialize()
    store.create_goal("task-1", CONTRACT)
    try:
        store.enqueue_command("task-1", "wake", "Keep citations next to claims")
        for cycle in range(1, 31):
            store.save_state(
                snapshot(
                    cycle,
                    phase(cycle=cycle, phase="act", result={"content": f"Evidence {cycle}"}),
                )
            )
        store.put_memory("research", "preference", "Cite primary sources", keywords="research")
        store.put_memory("food", "preference", "Prefer vegetarian", keywords="food")
        records = store.recall_memory("task-1", query="research", limit=4)
        tools = [item for item in records if item["role"] == "tool"]
        assert len(tools) == 4
        assert all(f"Evidence {cycle}" in str(tools) for cycle in range(27, 31))
        assert any(item["content"] == "Keep citations next to claims" for item in records)
        assert [item["id"] for item in records if item["kind"] == "knowledge"] == [
            "memory:research"
        ]
        assert not any(item["kind"] == "knowledge" for item in store.recall_memory("task-1"))
        assert len(store.conversation("task-1")) == 32  # Full audit remains readable.
        assert store.recall_memory("missing", query="research") == []
    finally:
        store.close()


def test_phase_failure_is_retained_and_legacy_feedback_still_loads(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/legacy.db")
    store.initialize()
    try:
        store.create_goal("task-1", CONTRACT)
        store.save_state(snapshot(1, phase(status="failed", error="Planning unavailable")))
        assert store.execution_steps("task-1")[0]["status"] == "failed"
        assert store.conversation("task-1")[-1]["role"] == "system"
        with store.sessions.begin() as session:
            session.execute(delete(MessageRow).where(MessageRow.goal_id == "task-1"))
            session.add(
                CommandRow(
                    command_id="legacy-wake", goal_id="task-1", kind="wake", payload="Earlier input"
                )
            )
        messages = store.conversation("task-1")
        assert messages[0]["id"] == "task"
        assert messages[1]["content"] == "Earlier input"
        assert messages[1]["created_at"] == ""
    finally:
        store.close()


def test_harness_api_reads_records_and_enforces_access_and_deleted_identity(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/api.db")
    settings = Settings(auth_mode="token", api_token="harness-test-token")
    try:
        with TestClient(create_app(settings, store)) as client:
            store.create_goal("task-1", CONTRACT)
            store.save_state(snapshot(1, phase(decision={"kind": "act"})))
            for suffix in ("conversation", "steps", "knowledge"):
                assert client.get(f"/goals/task-1/{suffix}").status_code == 401
                assert client.get(f"/goals/missing/{suffix}", headers=AUTH).status_code == 404
            records = client.get("/goals/task-1/conversation", headers=AUTH).json()
            assert len(records) == 2
            steps = client.get("/goals/task-1/steps?limit=1", headers=AUTH).json()
            assert steps[0]["phase"] == "think"
            assert client.get("/goals/task-1/steps?after=-1", headers=AUTH).status_code == 422
            response = client.put(
                "/goals/task-1/knowledge",
                headers=AUTH,
                json={"content": "Remember the source URL", "key": "source", "priority": 3},
            )
            assert response.status_code == 200 and response.json()["id"] == "source"
            assert client.get("/goals/task-1/knowledge", headers=AUTH).json()[0]["priority"] == 3
            assert (
                client.put(
                    "/goals/task-1/knowledge", headers=AUTH, json={"content": "   "}
                ).status_code
                == 422
            )
            store.save_state(snapshot(2, phase(phase="complete"), status="succeeded"))
            store.delete_goal("task-1")
            for suffix in ("conversation", "steps", "knowledge"):
                assert client.get(f"/goals/task-1/{suffix}", headers=AUTH).status_code == 404
            assert (
                client.put(
                    "/goals/task-1/knowledge", headers=AUTH, json={"content": "Again"}
                ).status_code
                == 404
            )
    finally:
        store.close()


async def test_real_activities_recall_durable_phase_history_after_state_window_is_empty(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/activities.db")
    store.initialize()
    store.create_goal("task-1", CONTRACT)
    requests = []

    async def plan(request):
        requests.append(request)
        return PlanResult(decision=Decision(kind="finish", reason="Ready"))

    activities = Activities(store, SimpleNamespace(), SimpleNamespace(plan=plan))
    try:
        revision = 0
        for cycle in range(1, 26):
            for name in PHASES:
                output = {}
                if name == "think":
                    output = {"decision": {"kind": "act", "reason": f"Inspect source {cycle}"}}
                elif name == "act":
                    output = {
                        "result": {"success": True, "content": f"Evidence from cycle {cycle}"}
                    }
                elif name == "complete":
                    output = {"passed": False, "verification": None}
                record = phase(cycle=cycle, phase=name, status="started")
                revision += 1
                state = snapshot(revision, record)
                await activities.persist(state)
                record.status = "completed"
                record.output = output
                record.completed_at = f"2026-10-21T10:{cycle:02d}:01+00:00"
                revision += 1
                state = snapshot(revision, record)
                await activities.persist(state)
        steps = store.execution_steps("task-1", limit=1000)
        assert len(steps) == 25 * len(PHASES)
        assert [item["phase"] for item in steps[:6]] == list(PHASES)
        assert all(item["status"] == "completed" for item in steps)
        messages = store.conversation("task-1")
        assert len(messages) == 1 + 25 * 2
        assert all(item["content"] != "null" for item in messages)
        assert "Evidence from cycle 1" in messages[2]["content"]
        store.put_knowledge("task-1", "Use the verified source", key="source", priority=20)
        # A fresh request has no in-memory observation text; the default
        # Activities adapter must recover messages from SQL.
        request = PlanInput(
            run=RunInput(goal_id="task-1", contract=CONTRACT),
            state=RunState(
                goal_id="task-1", handoff_summary="Continue using the recorded evidence."
            ),
        )
        await activities.plan(request)
        memory = requests[0].memory
        assert memory["handoff_summary"] == request.state.handoff_summary
        assert "Evidence from cycle 25" in serialize_memory(memory)
        assert memory["knowledge"][0]["content"] == "Use the verified source"
        assert conservative_token_count(serialize_memory(memory)) <= 2000
        assert request.memory == {}  # The caller's workflow snapshot is untouched.
    finally:
        store.close()


async def test_schema_upgrade_preserves_legacy_evidence_outside_latest_state(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/upgrade.db")
    store.initialize()
    store.create_goal("task-1", CONTRACT)
    command_id = store.enqueue_command("task-1", "wake", "Earlier task clarification")
    store.save_state(
        RunState(
            goal_id="task-1",
            revision=1,
            status="running",
            updated_at="2026-10-21T10:00:01+00:00",
            observations=[
                Observation(step=1, tool="read_file", success=True, content="Original evidence")
            ],
        )
    )
    store.save_state(
        RunState(
            goal_id="task-1",
            revision=2,
            status="running",
            updated_at="2026-10-21T10:00:02+00:00",
            observations=[],
        )
    )
    # The previous schema has goals/commands/events, without harness tables.
    for table in (MessageRow, ExecutionStepRow, KnowledgeRow):
        table.__table__.drop(store.engine)
    store.initialize()
    requests = []

    async def plan(request):
        requests.append(request)
        return PlanResult(decision=Decision(kind="finish", reason="Ready"))

    try:
        assert store.get_goal("task-1").state.observations == []
        messages = store.conversation("task-1")
        assert [item["id"] for item in messages] == [
            "task",
            f"wake:{command_id}",
            "legacy-observation:1:read_file",
        ]
        # Entering the harness should not hide prior event-only evidence.
        store.save_state(snapshot(3, phase(phase="perceive", status="started")))
        activities = Activities(store, SimpleNamespace(), SimpleNamespace(plan=plan))
        await activities.plan(
            PlanInput(
                run=RunInput(goal_id="task-1", contract=CONTRACT),
                state=RunState(goal_id="task-1"),
            )
        )
        memory = requests[0].memory
        assert "Original evidence" in serialize_memory(memory)
        ids = [item["id"] for item in memory["conversation"]]
        assert ids.count("legacy-observation:1:read_file") == 1
        assert "Earlier task clarification" in serialize_memory(memory)
    finally:
        store.close()


def test_delayed_knowledge_write_cannot_recreate_deleted_task_memory(tmp_path, monkeypatch):
    store = Store(f"sqlite:///{tmp_path}/delete-race.db")
    store.initialize()
    store.create_goal("task-1", CONTRACT)
    store.save_state(snapshot(1, phase(phase="complete"), status="succeeded"))
    real_sessions = store.sessions
    held, release = Event(), Event()

    class PausingSessions:
        @contextmanager
        def begin(self):
            with real_sessions.begin() as session:
                original_execute = session.execute

                def execute(*args, **kwargs):
                    if current_thread().name.startswith("late-knowledge"):
                        held.set()
                        if not release.wait(timeout=10):
                            raise TimeoutError("Delayed knowledge was not released")
                    return original_execute(*args, **kwargs)

                session.execute = execute
                yield session

        def __call__(self):
            return real_sessions()

    monkeypatch.setattr(store, "sessions", PausingSessions())
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="late-knowledge") as executor:
            delayed = executor.submit(store.put_knowledge, "task-1", "Late fact", "late")
            try:
                assert held.wait(timeout=10)
                store.delete_goal("task-1")
            finally:
                release.set()
            with pytest.raises(KeyError):
                delayed.result(timeout=10)
        assert store.memory_records("task-1") == []
        assert store.execution_steps("task-1") == []
    finally:
        store.close()


def test_parallel_batch_results_become_separate_recallable_tool_messages(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/batch.db")
    store.initialize()
    store.create_goal("task-1", CONTRACT)
    results = [
        {
            "action": {"tool": "read_file", "path": f"src/{name}.py", "url": None, "content": None},
            "result": {"success": True, "content": f"{name} source", "progress_key": name},
        }
        for name in ("alpha", "beta")
    ]
    try:
        store.save_state(snapshot(1, phase(phase="act", results=results)))
        store.save_state(snapshot(2, phase(phase="act", results=results)))
        tools = [item for item in store.conversation("task-1") if item["role"] == "tool"]
        assert [item["id"] for item in tools] == ["phase:1:act:1", "phase:1:act:2"]
        payloads = [json.loads(item["content"]) for item in tools]
        assert [item["action"]["path"] for item in payloads] == ["src/alpha.py", "src/beta.py"]
        assert "content" not in payloads[0]["action"]
        memory = Activities(store, SimpleNamespace(), SimpleNamespace()).memory.build(
            PlanInput(
                run=RunInput(goal_id="task-1", contract=CONTRACT), state=RunState(goal_id="task-1")
            )
        )
        recalled = " ".join(item["content"] for item in memory["conversation"])
        assert "src/alpha.py" in recalled and "beta source" in recalled
    finally:
        store.close()
