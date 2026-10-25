from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from rocky.api import create_app
from rocky.config import Settings
from rocky.inference import ModelSettings
from rocky.models import GoalContract, RunState
from rocky.planner import OpenAIPlanner, PlannerError
from rocky.store import Store


@pytest.fixture
def api(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/api.db")
    settings = Settings(
        auth_mode="token", api_token="owner-test-token", model="gpt-4.1-mini", reasoning_effort=None
    )
    with TestClient(create_app(settings, store)) as client:
        yield client, store
    store.close()


def contract():
    return {
        "objective": "Write a short report",
        "acceptance_criteria": ["Explain durable execution"],
        "deliverables": [{"path": "report.md", "contains": ["Rocky"]}],
    }


AUTH = {"Authorization": "Bearer owner-test-token"}


def test_prepare_contract_requires_auth_and_uses_saved_model_without_creating_goal(
    api, monkeypatch
):
    client, store = api
    saved_model = store.set_model_settings(
        ModelSettings(
            model="configured-model",
            provider="openai-compatible",
            base_url="https://models.example/v1",
            credential_id="saved-credential-reference",
        )
    )
    generated = GoalContract(**contract())
    prepare = AsyncMock(return_value=generated)
    monkeypatch.setattr(OpenAIPlanner, "prepare_contract", prepare)
    payload = {"objective": generated.objective}
    assert client.post("/goal-contract", json=payload).status_code == 401
    prepare.assert_not_awaited()
    response = client.post("/goal-contract", json=payload, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["deliverables"] == generated.model_dump()["deliverables"]
    draft, model = prepare.await_args.args
    assert draft.objective == generated.objective
    assert model == saved_model
    assert store.pending_goals() == []
    assert store.list_goals() == []


def test_prepare_contract_failure_is_sanitized_and_does_not_submit(api, monkeypatch):
    client, store = api
    monkeypatch.setattr(
        OpenAIPlanner,
        "prepare_contract",
        AsyncMock(side_effect=PlannerError("sensitive provider response")),
    )
    response = client.post("/goal-contract", json={"objective": "整理材料"}, headers=AUTH)
    assert response.status_code == 502
    assert "sensitive" not in response.text
    assert store.pending_goals() == []


def test_automatic_preparation_rejects_manual_budget_override_before_model_call(api, monkeypatch):
    client, _ = api
    prepare = AsyncMock()
    monkeypatch.setattr(OpenAIPlanner, "prepare_contract", prepare)
    response = client.post(
        "/goal-contract", json={"objective": "整理材料", "limits": {"max_steps": 0}}, headers=AUTH
    )
    assert response.status_code == 422
    prepare.assert_not_awaited()


def test_shutdown_closes_owned_database_when_planner_close_fails(tmp_path, monkeypatch):
    closed = []
    monkeypatch.setattr(OpenAIPlanner, "close", AsyncMock(side_effect=PlannerError("close failed")))
    monkeypatch.setattr(Store, "close", lambda self: closed.append(self))
    settings = Settings(
        auth_mode="token",
        api_token="owner-test-token",
        database_url=f"sqlite:///{tmp_path}/owned.db",
    )
    with pytest.raises(PlannerError):
        with TestClient(create_app(settings)):
            pass
    assert len(closed) == 1


def test_owner_auth_and_fail_closed_without_configuration(api):
    client, _ = api
    assert client.get("/health").status_code == 200
    assert client.get("/goals").status_code == 401
    assert client.get("/goals", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/goals", headers=[(b"authorization", b"Bearer \xff")]).status_code == 401
    store = Store("sqlite://")
    with TestClient(create_app(Settings(auth_mode="token", api_token=None), store)) as unconfigured:
        assert unconfigured.get("/goals", headers=AUTH).status_code == 503
    store.close()


def test_submit_commits_outbox_without_temporal_and_idempotent_retry(api):
    client, store = api
    headers = AUTH | {"Idempotency-Key": "stable-owner-request"}
    first = client.post("/goals", json=contract(), headers=headers)
    assert first.status_code == 202
    second = client.post("/goals", json=contract(), headers=headers)
    assert second.json()["goal_id"] == first.json()["goal_id"]
    assert first.json()["model_settings"] == {"model": "gpt-4.1-mini", "reasoning_effort": None}
    assert len(store.pending_goals()) == 1
    changed = contract() | {"objective": "Different objective"}
    assert client.post("/goals", json=changed, headers=headers).status_code == 409
    assert len(client.get("/goals", headers=AUTH).json()) == 1


def test_cancellation_and_wake_are_durable_and_terminal_goals_reject_commands(api):
    client, store = api
    goal = client.post("/goals", json=contract(), headers=AUTH).json()
    goal_id = goal["goal_id"]
    assert (
        client.post(
            f"/goals/{goal_id}/wake", json={"message": "New evidence"}, headers=AUTH
        ).status_code
        == 202
    )
    assert client.post(f"/goals/{goal_id}/cancel", headers=AUTH).status_code == 202
    assert store.is_cancel_requested(goal_id)
    assert [item["kind"] for item in store.pending_commands()] == ["wake", "cancel"]
    state = RunState(
        goal_id=goal_id, revision=1, status="cancelled", updated_at="9999-01-01T00:00:00+00:00"
    )
    store.save_state(state)
    assert client.post(f"/goals/{goal_id}/cancel", headers=AUTH).status_code == 409
    assert (
        client.get(f"/goals/{goal_id}/events", headers=AUTH).json()[0]["state"]["status"]
        == "cancelled"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"deliverables": []},
        {"deliverables": [{"path": "../escape.txt"}]},
        {"deliverables": [{"path": "/outside.txt"}]},
        {"max_model_calls": 0},
        {"allowed_domains": ["https://example.com"]},
        {"allowed_domains": ["*.example.com"]},
        {"acceptance_criteria": ["x" * 2001]},
    ],
)
def test_invalid_contracts_rejected_before_queuing(api, changes):
    client, store = api
    response = client.post("/goals", json=contract() | changes, headers=AUTH)
    assert response.status_code == 422
    assert store.pending_goals() == []


def test_missing_goals_and_invalid_pagination(api):
    client, _ = api
    assert client.get("/goals/missing", headers=AUTH).status_code == 404
    assert client.get("/goals?limit=1000", headers=AUTH).status_code == 422


def test_budget_retry_contract_above_automatic_ceilings_is_accepted_once(api):
    client, _ = api
    retried = contract() | {
        "limits": {
            "max_steps": 200,
            "max_model_calls": 300,
            "max_tokens": 9_728_000,
            "timeout_seconds": 7200,
        }
    }
    headers = AUTH | {"Idempotency-Key": "budget-retry:original-goal"}
    first = client.post("/goals", json=retried, headers=headers)
    second = client.post("/goals", json=retried, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["goal_id"] == second.json()["goal_id"]
    assert first.json()["contract"]["limits"] == retried["limits"]
