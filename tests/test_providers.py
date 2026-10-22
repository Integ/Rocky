import json
import os
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from rocky.api import create_app
from rocky.config import Settings
from rocky.credentials import CredentialError, CredentialStore
from rocky.inference import ModelSettings, model_catalog
from rocky.models import Deliverable, GoalContract, PlanInput, RunInput, RunState
from rocky.planner import OpenAIPlanner, PlannerError
from rocky.providers import PRESETS, ProviderUpdate, provider_model_catalog
from rocky.store import Store


class MemoryCredentials:
    storage = "test-memory"

    def __init__(self):
        self.values = {}

    def get(self, account):
        return self.values.get(account)

    def set(self, account, value):
        if value is None:
            self.values.pop(account, None)
        else:
            self.values[account] = value


@pytest.fixture
def provider_api(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'goals.db'}")
    credentials = MemoryCredentials()
    settings = Settings(workspace_root=tmp_path / "workspaces")
    with TestClient(
        create_app(settings, store, credentials),
        client=("127.0.0.1", 4242),
        base_url="http://127.0.0.1",
    ) as client:
        yield client, store, credentials, settings
    store.close()


def config(**updates):
    return {
        "provider": "openai-compatible",
        "base_url": "https://models.example/v1",
        "model": "vendor/model-v2",
        "reasoning_effort": None,
        **updates,
    }


def contract():
    return GoalContract(
        objective="Write a report",
        acceptance_criteria=["Actual evidence"],
        deliverables=[Deliverable(path="report.md")],
    )


def test_provider_settings_are_write_only_and_goal_snapshot_preserves_endpoint(provider_api):
    client, store, credentials, _ = provider_api
    response = client.put("/provider-settings", json=config(api_key="test-secret-value"))
    assert response.status_code == 200
    assert response.json()["has_api_key"]
    assert response.json()["credential_storage"] == "test-memory"
    assert "test-secret-value" not in response.text
    assert "test-secret-value" not in client.get("/provider-settings").text
    assert client.post("/goals", json=contract().model_dump()).status_code == 202
    first = store.list_goals()[0]
    assert first.model_settings.provider == "openai-compatible"
    assert first.model_settings.base_url == "https://models.example/v1"
    assert "test-secret-value" not in first.model_dump_json()
    client.put(
        "/provider-settings", json=config(base_url="https://other.example/api", model="other")
    )
    assert not client.get("/provider-settings").json()["has_api_key"]
    assert store.get_goal(first.goal_id).model_settings == first.model_settings
    assert len(credentials.values) == 1
    assert client.put("/provider-settings", json=config(api_key="")).json()["has_api_key"]
    assert not client.put("/provider-settings", json=config(clear_api_key=True)).json()[
        "has_api_key"
    ]


def test_provider_settings_expose_model_and_effort_choices_without_credentials(provider_api):
    client, _, _, _ = provider_api
    response = client.get("/provider-settings")
    assert response.status_code == 200
    value = response.json()
    assert value["models"] == model_catalog()
    for preset in value["providers"]:
        if preset["preset"] == "custom":
            assert preset["models"] == []
            continue
        assert len(preset["models"]) >= 2
        assert preset["model"] in {item["id"] for item in preset["models"]}
        for model in preset["models"]:
            assert model["label"]
            for effort in model["reasoning_efforts"]:
                ModelSettings(provider=preset["id"], model=model["id"], reasoning_effort=effort)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("preset", [item for item in PRESETS if item["base_url"]])
def test_active_catalog_tracks_provider_endpoint_and_preserves_custom_model(provider_api, preset):
    client, _, _, _ = provider_api
    update = config(
        provider=preset["id"], base_url=preset["base_url"] + "/", model="custom-deployment"
    )
    response = client.put("/provider-settings", json=update)
    assert response.status_code == 200
    expected = preset["models"] + [
        {"id": "custom-deployment", "label": "custom-deployment", "reasoning_efforts": []}
    ]
    assert response.json()["models"] == expected
    assert client.get("/provider-settings").json()["models"] == expected


def test_custom_endpoint_catalog_does_not_guess_third_party_capabilities(provider_api):
    client, _, _, _ = provider_api
    response = client.put("/provider-settings", json=config(model="gpt-6.1-sol"))
    assert response.status_code == 200
    assert response.json()["models"] == [
        {"id": "gpt-6.1-sol", "label": "gpt-6.1-sol", "reasoning_efforts": []}
    ]
    assert (
        client.put(
            "/provider-settings", json=config(model="gpt-6.1-sol", reasoning_effort="high")
        ).status_code
        == 422
    )


@pytest.mark.parametrize("provider", ["openai", "anthropic", "ollama"])
def test_known_provider_keeps_model_choices_after_custom_endpoint_save(provider_api, provider):
    client, _, _, _ = provider_api
    preset = next(item for item in PRESETS if item["id"] == provider)
    response = client.put(
        "/provider-settings",
        json=config(provider=provider, base_url="https://proxy.example/v1", model=preset["model"]),
    )
    assert response.status_code == 200
    assert len(response.json()["models"]) > 1
    assert response.json()["models"] == preset["models"]
    assert client.get("/provider-settings").json()["models"] == preset["models"]


def test_provider_catalog_returns_fresh_capabilities_and_reuses_known_openai_support():
    selected = provider_model_catalog("openai", "https://proxy.example/v1", "gpt-6.1-sol")
    assert selected == model_catalog()
    selected[0]["reasoning_efforts"].clear()
    official = provider_model_catalog("openai", "https://api.openai.com/v1/", "gpt-6.1-sol")
    next(item for item in official if item["id"] == "gpt-6.1-sol")["reasoning_efforts"].clear()
    assert (
        provider_model_catalog("openai", "https://api.openai.com/v1", "gpt-6.1-sol")
        == model_catalog()
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "https://user:secret@models.example/v1"},
        {"base_url": "http://remote.example/v1"},
        {"base_url": "https://models.example/v1?api_key=secret"},
        {"model": "bad model"},
        {"api_key": "secret\nvalue"},
        {"api_key": "secret", "clear_api_key": True},
        {"provider": "unknown", "api_key": "secret"},
    ],
)
def test_invalid_provider_settings_do_not_echo_any_input_or_modify_defaults(provider_api, changes):
    client, _, _, _ = provider_api
    before = client.get("/model-settings").json()
    response = client.put("/provider-settings", json=config(**changes))
    assert response.status_code == 422
    assert "secret" not in response.text
    assert client.get("/model-settings").json() == before


def test_provider_fields_cannot_bypass_validated_endpoint(provider_api):
    client, _, _, _ = provider_api
    assert (
        client.put(
            "/model-settings",
            json={"model": "gpt-4.1", "provider": "openai-compatible", "base_url": "http://unsafe"},
        ).status_code
        == 422
    )


def test_private_file_credentials_are_atomic_private_and_reject_symlinks(tmp_path):
    path = tmp_path / "credentials.json"
    credentials = CredentialStore(path)
    credentials.storage = "private-file"
    credentials.set("provider-one", "secret-one")
    assert credentials.get("provider-one") == "secret-one"
    assert path.stat().st_mode & 0o777 == 0o600
    credentials.set("provider-two", "secret-two")
    credentials.set("provider-one", None)
    assert credentials.get("provider-one") is None
    assert credentials.get("provider-two") == "secret-two"
    os.chmod(path, 0o644)
    with pytest.raises(CredentialError, match="0600"):
        credentials.get("provider-two")
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(CredentialError, match="symbolic link"):
        CredentialStore(link)._load()


def request_with_settings(inference):
    return PlanInput(
        run=RunInput(goal_id="one", contract=contract(), model_settings=inference),
        state=RunState(goal_id="one"),
    )


@pytest.mark.parametrize("provider", ["openai-compatible", "anthropic", "ollama"])
async def test_third_party_adapter_uses_frozen_endpoint_model_and_validates_json(provider):
    credentials = MemoryCredentials()
    inference = ProviderUpdate(**config(provider=provider)).snapshot()
    credentials.set(inference.credential_id, "test-provider-key")
    seen = []

    def respond(request):
        seen.append(request)
        verdict = {
            "kind": "blocked",
            "reason": "Need source evidence",
            "action": None,
            "wait_seconds": None,
        }
        if provider == "anthropic":
            return httpx.Response(
                200,
                json={
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": json.dumps(verdict)}],
                    "usage": {"input_tokens": 12, "output_tokens": 8},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(verdict)}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        planner = OpenAIPlanner(
            Settings(), defer_client=True, credential_store=credentials, http_client=http
        )
        result = await planner.plan(request_with_settings(inference))
        assert result.decision.kind == "blocked"
        assert (result.input_tokens, result.output_tokens) == (12, 8)
        assert str(seen[0].url) == inference.base_url + (
            "/messages" if provider == "anthropic" else "/chat/completions"
        )
        payload = json.loads(seen[0].content)
        assert payload["model"] == "vendor/model-v2"
        assert "test-provider-key" not in seen[0].content.decode()
        if provider == "anthropic":
            assert seen[0].headers["x-api-key"] == "test-provider-key"
        else:
            assert seen[0].headers["authorization"] == "Bearer test-provider-key"


async def test_ollama_without_key_and_invalid_or_truncated_output_never_accepts_decision():
    inference = ProviderUpdate(
        **config(provider="ollama", base_url="http://127.0.0.1:11434/v1")
    ).snapshot()
    for choice in [
        {"finish_reason": "length", "message": {"content": '{"kind":"finish","reason":"done"}'}},
        {"finish_reason": "stop", "message": {"content": "not JSON test-secret"}},
    ]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request, choice=choice: httpx.Response(200, json={"choices": [choice]})
            )
        ) as http:
            planner = OpenAIPlanner(
                Settings(),
                defer_client=True,
                credential_store=MemoryCredentials(),
                http_client=http,
            )
            with pytest.raises(PlannerError) as error:
                await planner.plan(request_with_settings(inference))
            assert "test-secret" not in str(error.value)


async def test_custom_endpoint_never_inherits_official_openai_environment_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-custom-provider")
    inference = ProviderUpdate(**config()).snapshot()
    transport = Mock(side_effect=AssertionError("Must not send a request"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        planner = OpenAIPlanner(
            Settings(), defer_client=True, credential_store=MemoryCredentials(), http_client=http
        )
        with pytest.raises(PlannerError):
            await planner.plan(request_with_settings(inference))
        transport.assert_not_called()


def test_worker_planner_can_start_before_key_is_configured(monkeypatch):
    constructor = Mock(side_effect=AssertionError("Must not initialize provider eagerly"))
    monkeypatch.setattr("rocky.planner.AsyncOpenAI", constructor)
    OpenAIPlanner(Settings(), defer_client=True)
    constructor.assert_not_called()


def test_declared_artifact_content_and_unsafe_files(provider_api, tmp_path):
    client, store, _, settings = provider_api
    store.create_goal("artifact-test", contract())
    goal_root = settings.workspace_root / "artifact-test"
    goal_root.mkdir(parents=True)
    report = goal_root / "report.md"
    report.write_text("# Report\nActual evidence")
    url = "/goals/artifact-test/artifacts/content"
    response = client.get(url, params={"path": "report.md"})
    assert response.status_code == 200
    assert response.json() == {
        "path": "report.md",
        "content": "# Report\nActual evidence",
        "truncated": False,
    }
    assert client.get(url, params={"path": "../report.md"}).status_code == 404
    assert client.get(url, params={"path": "private.txt"}).status_code == 404
    report.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("private content")
    report.symlink_to(outside)
    assert client.get(url, params={"path": "report.md"}).status_code == 404
    report.unlink()
    os.link(outside, report)
    assert client.get(url, params={"path": "report.md"}).status_code == 404


def test_artifact_download_is_complete_and_supports_binary(provider_api):
    client, store, _, settings = provider_api
    store.create_goal("download-test", contract())
    root = settings.workspace_root / "download-test"
    root.mkdir(parents=True)
    content = b"\xff\x00" + b"x" * 150_000
    (root / "report.md").write_bytes(content)
    response = client.get("/goals/download-test/artifacts/download", params={"path": "report.md"})
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["cache-control"] == "no-store"
    assert "attachment;" in response.headers["content-disposition"]
    assert (
        client.get(
            "/goals/download-test/artifacts/download", params={"path": "../private"}
        ).status_code
        == 404
    )


def test_saved_feedback_is_readable_after_reopening_app(provider_api):
    client, store, _, _ = provider_api
    store.create_goal("feedback-test", contract())
    assert (
        client.post(
            "/goals/feedback-test/wake", json={"message": "Use the newly supplied evidence"}
        ).status_code
        == 202
    )
    messages = client.get("/goals/feedback-test/messages").json()
    assert len(messages) == 1
    assert messages[0]["message"] == "Use the newly supplied evidence"
    assert messages[0]["sent"] is False
    assert client.get("/goals/missing/messages").status_code == 404
