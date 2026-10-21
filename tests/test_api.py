import pytest
from fastapi.testclient import TestClient

from rocky.api import create_app
from rocky.config import Settings
from rocky.models import RunState
from rocky.store import Store


@pytest.fixture
def api(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/api.db")
    settings = Settings(api_token="owner-test-token", model="gpt-4.1-mini", reasoning_effort=None)
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


def test_owner_auth_and_fail_closed_without_configuration(api):
    client, _ = api
    assert client.get("/health").status_code == 200
    assert client.get("/goals").status_code == 401
    assert client.get("/goals", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/goals", headers=[(b"authorization", b"Bearer \xff")]).status_code == 401
    store = Store("sqlite://")
    with TestClient(create_app(Settings(api_token=None), store)) as unconfigured:
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
