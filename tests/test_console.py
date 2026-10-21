"""Console serving must not weaken the authenticated owner API."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from rocky.api import create_app
from rocky.config import Settings
from rocky.models import Deliverable, GoalContract
from rocky.store import Store

_OWNER_TOKEN = "console-owner-token-sentinel"
_MODEL_KEY = "sk-console-test-key-sentinel-never-real"
_PRIVATE_GOAL = "Private mission objective sentinel never embedded in HTML"
_GOAL_ID = "private-console-test-goal"
_AUTH = {"Authorization": f"Bearer {_OWNER_TOKEN}"}
_ASSETS = ("app.js", "core.js", "visuals.js", "styles.css")


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Replace the environment value with a test sentinel, without inspecting
    # any existing credential or initializing an OpenAI client.
    monkeypatch.setenv("OPENAI_API_KEY", _MODEL_KEY)
    settings = Settings(
        auth_mode="token",
        database_url=f"sqlite:///{tmp_path / 'console.db'}",
        api_token=SecretStr(_OWNER_TOKEN),
        model="console-configured-model",
        reasoning_effort=None,
    )
    store = Store(settings.database_url)
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


def assert_console_headers(response):
    directives = set(response.headers["content-security-policy"].split("; "))
    assert directives == {
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    }
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def assert_no_private_data(content):
    assert _MODEL_KEY not in content
    assert _OWNER_TOKEN not in content
    assert _PRIVATE_GOAL not in content
    assert _GOAL_ID not in content


def test_public_console_is_static_uncached_shell_without_credentials_or_task_data(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "Rocky" in response.text or "ROCKY" in response.text
    assert_no_private_data(response.text)
    assert_console_headers(response)


def test_console_shell_loads_same_origin_entry_assets(client):
    response = client.get("/")

    assert 'src="/console-assets/app.js"' in response.text
    assert 'href="/console-assets/styles.css"' in response.text
    assert '<script type="module"' in response.text


def test_static_index_alias_is_also_uncached(client):
    response = client.get("/console-assets/index.html")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert_no_private_data(response.text)
    assert_console_headers(response)


@pytest.mark.parametrize("filename", _ASSETS)
def test_console_assets_are_public_typed_and_have_security_headers(client, filename):
    response = client.get(f"/console-assets/{filename}")

    assert response.status_code == 200
    media_type = response.headers["content-type"].split(";")[0]
    if filename.endswith(".js"):
        assert media_type in ("application/javascript", "text/javascript")
    else:
        assert media_type == "text/css"
    assert_no_private_data(response.text)
    assert_console_headers(response)


def test_missing_console_asset_returns_404_with_security_headers(client):
    response = client.get("/console-assets/not-a-real-asset.js")

    assert response.status_code == 404
    assert_console_headers(response)


@pytest.mark.parametrize(
    "path",
    [
        "/goals",
        f"/goals/{_GOAL_ID}",
        f"/goals/{_GOAL_ID}/events",
        "/console-info",
        "/model-settings",
    ],
)
def test_public_shell_does_not_grant_api_access(client, path):
    response = client.get(path)

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert_no_private_data(response.text)


def test_authenticated_console_info_reports_configured_capabilities(client):
    response = client.get("/console-info", headers=_AUTH)

    assert response.status_code == 200
    payload = response.json()
    assert {
        key: payload[key]
        for key in ("model", "reasoning_effort", "version", "tools", "polling_seconds", "auth_mode")
    } == {
        "model": "console-configured-model",
        "reasoning_effort": None,
        "version": "0.1.0",
        "tools": ["read_file", "write_file", "list_files", "fetch_url"],
        "polling_seconds": 3,
        "auth_mode": "token",
    }
    assert any(item["id"] == "console-configured-model" for item in payload["models"])
    assert all({"id", "label", "reasoning_efforts"} <= item.keys() for item in payload["models"])
    assert response.headers["cache-control"] == "no-store"
    assert_console_headers(response)
    assert_no_private_data(response.text)


@pytest.mark.parametrize(
    "path", ["/goals", f"/goals/{_GOAL_ID}", f"/goals/{_GOAL_ID}/events", "/model-settings"]
)
def test_authenticated_task_json_is_not_browser_cached(client, path):
    response = client.get(path, headers=_AUTH)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_authenticated_console_does_not_unlock_another_request(client):
    assert client.get("/console-info", headers=_AUTH).status_code == 200
    response = client.get("/goals")
    assert response.status_code == 401
    assert_no_private_data(response.text)
