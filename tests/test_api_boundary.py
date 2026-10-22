"""The native client uses a JSON API with no browser interface."""

import pytest
from fastapi.testclient import TestClient

from rocky.api import create_app
from rocky.config import Settings
from rocky.models import Deliverable, GoalContract
from rocky.store import Store

_OWNER_TOKEN = "api-owner-token-sentinel"
_MODEL_KEY = "sk-api-test-key-sentinel-never-real"
_PRIVATE_GOAL = "Private mission objective sentinel"
_GOAL_ID = "private-api-test-goal"
_AUTH = {"Authorization": f"Bearer {_OWNER_TOKEN}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", _MODEL_KEY)
    settings = Settings(auth_mode="token", api_token=_OWNER_TOKEN)
    store = Store(f"sqlite:///{tmp_path / 'api.db'}")
    store.initialize()
    store.create_goal(
        _GOAL_ID,
        GoalContract(
            objective=_PRIVATE_GOAL,
            acceptance_criteria=["Create the private report."],
            deliverables=[Deliverable(path="private-report.md")],
        ),
    )
    try:
        with TestClient(create_app(settings, store)) as connection:
            yield connection
    finally:
        store.close()


def assert_no_private_data(content):
    for value in (_MODEL_KEY, _OWNER_TOKEN, _PRIVATE_GOAL, _GOAL_ID):
        assert value not in content


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/console-info",
        "/console-assets",
        "/console-assets/index.html",
        "/console-assets/app.js",
        "/console-assets/core.js",
        "/console-assets/visuals.js",
        "/console-assets/styles.css",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    ],
)
@pytest.mark.parametrize("auth_mode", ["local", "token"])
def test_browser_interfaces_are_unavailable_even_with_owner_auth(tmp_path, auth_mode, path):
    store = Store(f"sqlite:///{tmp_path / 'no-browser.db'}")
    settings = Settings(auth_mode=auth_mode, api_token=_OWNER_TOKEN)
    try:
        with TestClient(
            create_app(settings, store), client=("127.0.0.1", 45678), base_url="http://127.0.0.1"
        ) as client:
            response = client.get(path, headers=_AUTH)
            assert response.status_code == 404
            assert response.json() == {"detail": "Not Found"}
    finally:
        store.close()


@pytest.mark.parametrize(
    "path",
    [
        "/goals",
        f"/goals/{_GOAL_ID}",
        f"/goals/{_GOAL_ID}/events",
        "/model-settings",
        "/provider-settings",
    ],
)
def test_private_api_requires_owner_auth_without_leaking_data(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert_no_private_data(response.text)


@pytest.mark.parametrize(
    "path", ["/goals", f"/goals/{_GOAL_ID}", f"/goals/{_GOAL_ID}/events", "/model-settings"]
)
def test_authenticated_json_keeps_security_headers_and_cannot_be_cached(client, path):
    response = client.get(path, headers=_AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    )
    assert _MODEL_KEY not in response.text
    assert _OWNER_TOKEN not in response.text


def test_authenticated_request_does_not_unlock_another_request(client):
    assert client.get("/model-settings", headers=_AUTH).status_code == 200
    response = client.get("/goals")
    assert response.status_code == 401
    assert_no_private_data(response.text)


def test_machine_readable_schema_exposes_only_json_api(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert {"/health", "/goals", "/provider-settings", "/model-settings"} <= paths.keys()
    assert not any(path == "/" or path.startswith("/console") for path in paths)
