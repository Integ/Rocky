"""Questions are scoped, and delivery is never mistaken for model consumption."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from rocky.api import create_app
from rocky.config import Settings
from rocky.models import (
    Decision,
    Deliverable,
    ExecutionStep,
    GoalContract,
    OwnerQuestion,
    PendingQuestion,
    PlanInput,
    PlanResult,
    RunInput,
    RunState,
    VerificationResult,
)
from rocky.planner import consumed_owner_messages
from rocky.store import Store
from rocky.workflow import GoalWorkflow


@pytest.fixture
def store(tmp_path):
    value = Store(f"sqlite:///{tmp_path}/inputs.db")
    value.initialize()
    value.create_goal("task", contract())
    yield value
    value.close()


def contract():
    return GoalContract(
        objective="Write a report",
        acceptance_criteria=["Use the supplied figures"],
        deliverables=[Deliverable(path="report.md")],
    )


def blocked(store):
    state = RunState(
        goal_id="task",
        status="blocked",
        revision=1,
        pending_question=PendingQuestion(
            question_id="question-1",
            text="Which year?",
            options=["2025", "2026"],
            requested_at="2026-11-01",
        ),
    )
    store.save_state(state)
    return state


def test_answer_auth_scope_idempotency_and_no_approval(store):
    blocked(store)
    settings = Settings(auth_mode="token", api_token="test-owner")
    auth = {"Authorization": "Bearer test-owner"}
    with TestClient(create_app(settings, store)) as client:
        url = "/goals/task/answer"
        answer = {"question_id": "question-1", "answer": "2026"}
        assert client.post(url, json=answer).status_code == 401
        assert client.post(url, json=answer | {"answer": "  "}, headers=auth).status_code == 422
        assert (
            client.post(url, json=answer | {"question_id": "old"}, headers=auth).status_code == 409
        )
        response = client.post(url, json=answer, headers=auth)
        assert response.status_code == 202
        assert client.post(url, json=answer, headers=auth).json() == response.json()
        assert client.post(url, json=answer | {"answer": "2025"}, headers=auth).status_code == 409
    commands = store.pending_commands()
    assert len(commands) == 1 and commands[0]["kind"] == "wake"
    assert store.get_goal("task").state.pending_approval is None


def test_receipts_are_monotonic_revision_fenced_and_do_not_confirm_other_tasks(store):
    command = store.enqueue_command("task", "wake", "Use 2026")
    assert store.goal_messages("task")[0]["delivery_status"] == "saved"
    store.mark_command_sent(command)
    assert store.goal_messages("task")[0]["delivery_status"] == "delivered"
    store.create_goal("other", contract())
    foreign = store.enqueue_command("other", "wake", "Private")
    state = RunState(
        goal_id="task",
        revision=2,
        status="running",
        execution_step=ExecutionStep(
            step_id="cycle-1:think",
            cycle=1,
            phase="think",
            status="completed",
            started_at="2026-11-01T00:00:00Z",
            completed_at="2026-11-01T00:00:01Z",
            output={"consumed_message_ids": [f"wake:{command}", f"wake:{foreign}"]},
        ),
    )
    store.save_state(state)
    message = store.goal_messages("task")[0]
    assert message["delivery_status"] == "consumed"
    assert message["consumed_at"] == "2026-11-01T00:00:01Z"
    store.save_state(state)
    store.mark_command_sent(command)
    assert store.goal_messages("task")[0] == message
    assert store.goal_messages("other")[0]["delivery_status"] == "saved"
    # A delayed completed phase cannot confirm a newer input.
    late = store.enqueue_command("task", "wake", "Actually 2025")
    state.execution_step.output["consumed_message_ids"].append(f"wake:{late}")
    store.save_state(state)
    assert (
        next(row for row in store.goal_messages("task") if row["command_id"] == late)[
            "delivery_status"
        ]
        == "saved"
    )


def test_only_complete_model_visible_messages_are_confirmed():
    request = PlanInput(
        run=RunInput(goal_id="task", contract=contract()),
        state=RunState(goal_id="task"),
        feedback="Latest",
    )
    records = [
        {"id": "wake:full", "role": "user", "content": "Full"},
        {"id": "wake:partial", "role": "user", "content": "Long message"},
        {"id": "wake:omitted", "role": "user", "content": "Omitted"},
        {"id": "wake:feedback", "role": "user", "content": "Latest"},
        {"id": "wake:clipped", "role": "user", "content": "Text[MEMORY TRUNCATED]"},
    ]
    request.memory = {"conversation": records}
    visible = [records[0], records[1] | {"content": "Long"}, records[-1]]
    payload = {"memory": {"conversation": visible}, "feedback": "Latest"}
    assert consumed_owner_messages(request, payload) == ["wake:full", "wake:feedback"]
    payload["feedback"] = "Late"
    assert consumed_owner_messages(request, payload) == ["wake:full"]


def test_question_is_only_allowed_with_blocked_decisions():
    run = RunInput(goal_id="task", contract=contract())
    question = OwnerQuestion(text="Which year?", options=["2026"])
    assert "Only a blocked" in GoalWorkflow._decision_error(
        run, Decision(kind="finish", reason="Done", question=question)
    )
    assert (
        GoalWorkflow._decision_error(
            run, Decision(kind="blocked", reason="Need the year", question=question)
        )
        == ""
    )


@pytest.mark.parametrize("arrival", ["planning", "verification"])
async def test_new_owner_message_prevents_stale_completion(monkeypatch, arrival):
    run = RunInput(goal_id="task", contract=contract(), model_task_queue="offline-model")
    goal = GoalWorkflow(run)
    planned, verified = [], []

    async def dispatch(name, value, **kwargs):
        if name == "rocky.persist":
            return
        if name == "rocky.plan":
            planned.append(value)
            if arrival == "planning" and len(planned) == 1:
                goal.wake("Include the new figures")
            return PlanResult(decision=Decision(kind="finish", reason="Delivered"))
        if name == "rocky.verify":
            verified.append(value)
            if arrival == "verification" and len(verified) == 1:
                goal.wake("Include the new figures")
            return VerificationResult(passed=True, feedback="Verified")
        pytest.fail(name)

    monkeypatch.setattr("rocky.workflow.workflow.now", lambda: datetime(2026, 11, 1, tzinfo=UTC))
    monkeypatch.setattr("rocky.workflow.workflow.execute_activity", dispatch)
    monkeypatch.setattr("rocky.workflow.workflow.patched", lambda _: True)
    monkeypatch.setattr(goal, "_should_continue_as_new", lambda: False)
    result = await goal.run(run)
    assert result.status == "succeeded" and len(planned) == 2
    assert planned[-1].feedback == "Include the new figures"
    assert len(verified) == (1 if arrival == "planning" else 2)
