from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rocky.api import create_app
from rocky.config import Settings
from rocky.models import (
    Action,
    Deliverable,
    ExecutionStep,
    GoalContract,
    PendingApproval,
    RunState,
)
from rocky.service import Dispatcher
from rocky.store import Store, equivalent_contracts

AUTH = {"Authorization": "Bearer control-test-token"}
FINGERPRINT = "a" * 64
CONTRACT = GoalContract(
    objective="Produce a report",
    acceptance_criteria=["Contains verified facts"],
    deliverables=[Deliverable(path="report.md")],
)


@pytest.fixture
def control(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/control.db")
    settings = Settings(auth_mode="token", api_token="control-test-token")
    with TestClient(create_app(settings, store)) as client:
        store.create_goal("task", CONTRACT)
        yield client, store
    store.close()


def pending_state(revision=1):
    return RunState(
        goal_id="task",
        revision=revision,
        status="blocked",
        pending_approval=PendingApproval(
            fingerprint=FINGERPRINT,
            operation_id="step-1",
            action=Action(
                tool="http_request", url="https://example.test", content='{"method":"POST"}'
            ),
            reason="Posting changes external state",
        ),
    )


def test_curated_memory_api_authentication_revision_conflicts_and_retraction(control):
    client, _ = control
    body = {"kind": "preference", "content": "Use primary sources", "keywords": "research"}
    assert client.get("/memories").status_code == 401
    assert client.put("/memories/citations", json=body).status_code == 401
    assert client.delete("/memories/citations?expected_revision=1").status_code == 401
    original = client.put("/memories/citations", headers=AUTH, json=body)
    assert original.status_code == 200 and original.json()["revision"] == 1
    assert client.put("/memories/citations", headers=AUTH, json=body).json() == original.json()
    correction = {**body, "kind": "correction", "content": "Use dated primary sources"}
    assert client.put("/memories/citations", headers=AUTH, json=correction).status_code == 409
    updated = client.put(
        "/memories/citations", headers=AUTH, json={**correction, "expected_revision": 1}
    )
    assert updated.status_code == 200 and updated.json()["revision"] == 2
    assert client.get("/memories?query=research", headers=AUTH).json() == [updated.json()]
    assert client.get("/memories?query=gardening", headers=AUTH).json() == []
    assert client.delete("/memories/citations?expected_revision=1", headers=AUTH).status_code == 409
    assert (
        client.delete("/memories/citations?expected_revision=2", headers=AUTH).json()["active"]
        is False
    )
    assert client.get("/memories?query=research", headers=AUTH).json() == []
    assert len(client.get("/memories?include_inactive=true", headers=AUTH).json()) == 1
    assert client.delete("/memories/missing?expected_revision=1", headers=AUTH).status_code == 404
    assert (
        client.put("/memories/bad", headers=AUTH, json={**body, "content": " "}).status_code == 422
    )
    assert (
        client.put("/memories/bad", headers=AUTH, json={**body, "kind": "transcript"}).status_code
        == 422
    )


def test_approval_api_only_accepts_current_pending_fingerprint(control):
    client, store = control
    assert client.post("/goals/task/approve", json={"fingerprint": FINGERPRINT}).status_code == 401
    assert (
        client.post(
            "/goals/task/approve", headers=AUTH, json={"fingerprint": FINGERPRINT}
        ).status_code
        == 409
    )
    store.save_state(pending_state())
    assert (
        client.post("/goals/task/approve", headers=AUTH, json={"fingerprint": "b" * 64}).status_code
        == 409
    )
    assert (
        client.post(
            "/goals/task/approve", headers=AUTH, json={"fingerprint": "invalid"}
        ).status_code
        == 422
    )
    accepted = client.post("/goals/task/approve", headers=AUTH, json={"fingerprint": FINGERPRINT})
    assert accepted.status_code == 202
    assert store.pending_commands() == [
        {
            "command_id": accepted.json()["command_id"],
            "goal_id": "task",
            "kind": "approve",
            "payload": FINGERPRINT,
        }
    ]
    # Plain wake text never creates an approval record.
    client.post("/goals/task/wake", headers=AUTH, json={"message": "approve " + FINGERPRINT})
    assert [item["kind"] for item in store.pending_commands()] == ["approve", "wake"]


def test_task_scoped_approval_requires_a_reusable_command_prefix(control):
    client, store = control
    store.save_state(pending_state())
    task_scope = {"fingerprint": FINGERPRINT, "scope": "task"}
    # An HTTP mutation has no command class; it can only be approved once.
    assert client.post("/goals/task/approve", headers=AUTH, json=task_scope).status_code == 409
    assert (
        client.post(
            "/goals/task/approve", headers=AUTH, json={**task_scope, "scope": "forever"}
        ).status_code
        == 422
    )
    command = pending_state(revision=2)
    command.pending_approval.action = Action(tool="run_command", content="uv run pytest -q")
    command.pending_approval.command_prefix = ["uv", "run", "pytest"]
    store.save_state(command)
    accepted = client.post("/goals/task/approve", headers=AUTH, json=task_scope)
    assert accepted.status_code == 202
    assert store.pending_commands()[-1]["payload"] == "task:" + FINGERPRINT
    with pytest.raises(ValueError):
        store.enqueue_command("task", "approve", "other:" + FINGERPRINT)


def test_execution_event_api_exposes_immutable_transitions_with_bounded_cursor(control):
    client, store = control
    record = ExecutionStep(
        step_id="1:act",
        cycle=1,
        phase="act",
        started_at="2026-10-21T10:00:00+00:00",
        input={"operation_id": "step-1"},
    )
    store.save_state(RunState(goal_id="task", revision=1, status="running", execution_step=record))
    record = record.model_copy(
        update={
            "status": "completed",
            "completed_at": "2026-10-21T10:00:02+00:00",
            "output": {"success": True},
        }
    )
    store.save_state(RunState(goal_id="task", revision=2, status="running", execution_step=record))
    assert client.get("/goals/task/execution-events").status_code == 401
    first = client.get("/goals/task/execution-events?limit=1", headers=AUTH).json()
    assert len(first) == 1 and first[0]["status"] == "started"
    rest = client.get(
        f"/goals/task/execution-events?after={first[0]['sequence_id']}", headers=AUTH
    ).json()
    assert len(rest) == 1 and rest[0]["duration_ms"] == 2000
    assert client.get("/goals/task/execution-events?limit=1001", headers=AUTH).status_code == 422
    assert client.get("/goals/missing/execution-events", headers=AUTH).status_code == 404


async def test_approval_outbox_retries_same_command_without_becoming_a_wake(control):
    _, store = control
    store.save_state(pending_state())
    store.mark_dispatched("task")
    command_id = store.enqueue_command("task", "approve", FINGERPRINT)
    delivered = []

    async def signal(name, command):
        delivered.append((name, command))
        if len(delivered) == 1:
            raise OSError("Response lost after signal delivery")

    dispatcher = Dispatcher(
        SimpleNamespace(get_workflow_handle=lambda _: SimpleNamespace(signal=signal)),
        store,
        Settings(),
    )
    await dispatcher.tick()
    assert len(store.pending_commands()) == 1
    await dispatcher.tick()
    assert store.pending_commands() == []
    assert [item[0] for item in delivered] == ["approval_command", "approval_command"]
    assert all(
        item[1].command_id == command_id and item[1].message == FINGERPRINT for item in delivered
    )


def test_new_execution_limits_change_the_idempotent_task_contract():
    changed = CONTRACT.model_copy(deep=True)
    changed.limits.max_steps = CONTRACT.limits.max_steps + 1
    assert not equivalent_contracts(CONTRACT, changed)
    assert equivalent_contracts(CONTRACT, CONTRACT.model_copy(update={"max_steps": 1}))
