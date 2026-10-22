"""Persistent defaults and immutable per-goal inference snapshots."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from rocky.api import create_app
from rocky.config import Settings
from rocky.inference import ModelSettings
from rocky.models import GoalContract, RunState
from rocky.store import Store

_AUTH = {"Authorization": "Bearer model-settings-owner-test"}
_FALLBACK = {"model": "gpt-4.1-mini", "reasoning_effort": None}
_REASONING = {"model": "gpt-6.1-sol", "reasoning_effort": "high"}


def contract():
    return GoalContract.model_validate(
        {
            "objective": "Write a reproducible report.",
            "acceptance_criteria": ["Explain the evidence."],
            "deliverables": [{"path": "report.md"}],
        }
    )


def api_settings(**kwargs):
    return Settings(
        auth_mode="token",
        api_token="model-settings-owner-test",
        model=kwargs.pop("model", "gpt-4.1-mini"),
        reasoning_effort=kwargs.pop("reasoning_effort", None),
        **kwargs,
    )


@pytest.fixture
def api(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'model-settings.db'}")
    try:
        with TestClient(create_app(api_settings(), store)) as client:
            yield client, store
    finally:
        store.close()


def test_defaults_require_owner_auth_and_fall_back_to_validated_process_configuration(api):
    client, _ = api
    assert client.get("/model-settings").status_code == 401
    assert client.put("/model-settings", json=_REASONING).status_code == 401
    assert (
        client.get("/model-settings", headers={"Authorization": "Bearer wrong"}).status_code == 401
    )

    response = client.get("/model-settings", headers=_AUTH)
    assert response.status_code == 200
    assert response.json() == _FALLBACK
    assert response.headers["cache-control"] == "no-store"


def test_configured_custom_model_is_selectable_without_guessed_reasoning_controls(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'custom.db'}")
    configured = {"model": "custom-eval-deployment", "reasoning_effort": None}
    try:
        with TestClient(create_app(api_settings(model=configured["model"]), store)) as client:
            assert client.get("/model-settings", headers=_AUTH).json() == configured
            assert client.put("/model-settings", json=_FALLBACK, headers=_AUTH).status_code == 200
            assert (
                client.put("/model-settings", json=configured, headers=_AUTH).json() == configured
            )
            invalid = configured | {"reasoning_effort": "high"}
            assert client.put("/model-settings", json=invalid, headers=_AUTH).status_code == 422
            assert client.get("/model-settings", headers=_AUTH).json() == configured
    finally:
        store.close()


@pytest.mark.parametrize(
    "value",
    [
        {"model": "gpt-4.1-mini", "reasoning_effort": "high"},
        {"model": "gpt-6.1-sol", "reasoning_effort": "none"},
        {"model": "gpt-6-astra", "reasoning_effort": "minimal"},
        {"model": "gpt-6-luna", "reasoning_effort": "ultra"},
        {"model": "not-in-the-configured-catalog", "reasoning_effort": None},
        {"model": "not-in-the-configured-catalog", "reasoning_effort": "high"},
        {"model": "gpt-4.1-mini", "api_key": "must-not-be-accepted"},
        {"model": "bad model name", "reasoning_effort": None},
    ],
)
def test_invalid_models_combinations_and_extra_fields_never_change_saved_defaults(api, value):
    client, _ = api
    assert client.put("/model-settings", json=_REASONING, headers=_AUTH).status_code == 200
    response = client.put("/model-settings", json=value, headers=_AUTH)
    assert response.status_code == 422
    assert client.get("/model-settings", headers=_AUTH).json() == _REASONING


@pytest.mark.parametrize(
    "value",
    [
        _FALLBACK,
        _REASONING,
        {"model": "gpt-6.1-sol", "reasoning_effort": "max"},
        {"model": "gpt-6-luna", "reasoning_effort": "none"},
    ],
)
def test_valid_model_settings_are_returned_and_reflected_in_provider_settings(api, value):
    client, _ = api
    response = client.put("/model-settings", json=value, headers=_AUTH)
    assert response.status_code == 200
    assert response.json() == value
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/model-settings", headers=_AUTH).json() == value
    info = client.get("/provider-settings", headers=_AUTH).json()
    assert info["model"] == value["model"]
    assert info["reasoning_effort"] == value["reasoning_effort"]
    catalog_entry = next(item for item in info["models"] if item["id"] == value["model"])
    assert isinstance(catalog_entry["label"], str)
    if value["reasoning_effort"] is not None:
        assert value["reasoning_effort"] in catalog_entry["reasoning_efforts"]


def test_saved_defaults_survive_app_and_store_restart_without_using_new_env_fallback(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'reopened.db'}"
    first = Store(database_url)
    with TestClient(create_app(api_settings(), first)) as client:
        assert client.put("/model-settings", json=_REASONING, headers=_AUTH).status_code == 200
    first.close()

    reopened = Store(database_url)
    try:
        with TestClient(create_app(api_settings(model="gpt-4.1"), reopened)) as client:
            assert client.get("/model-settings", headers=_AUTH).json() == _REASONING
            assert (
                client.get("/provider-settings", headers=_AUTH).json()["model"]
                == _REASONING["model"]
            )
    finally:
        reopened.close()


def test_goal_submission_snapshots_defaults_and_idempotency_preserves_original_snapshot(api):
    client, store = api
    payload = contract().model_dump(mode="json")
    first_headers = _AUTH | {"Idempotency-Key": "first-goal"}
    first = client.post("/goals", json=payload, headers=first_headers)
    assert first.status_code == 202
    assert first.json()["model_settings"] == _FALLBACK
    assert first.json()["contract"] == payload

    client.put("/model-settings", json=_REASONING, headers=_AUTH)
    second = client.post("/goals", json=payload, headers=_AUTH)
    assert second.status_code == 202
    assert second.json()["model_settings"] == _REASONING
    retry = client.post("/goals", json=payload, headers=first_headers)
    assert retry.status_code == 202
    assert retry.json() == first.json()
    assert (
        client.post(
            "/goals", json=payload | {"objective": "A different objective"}, headers=first_headers
        ).status_code
        == 409
    )

    first_id, second_id = first.json()["goal_id"], second.json()["goal_id"]
    assert store.get_goal(first_id).model_settings == ModelSettings(**_FALLBACK)
    assert store.get_goal(second_id).model_settings == ModelSettings(**_REASONING)
    for goals in (store.list_goals(), store.pending_goals()):
        snapshots = {goal.goal_id: goal.model_settings for goal in goals}
        assert snapshots[first_id] == ModelSettings(**_FALLBACK)
        assert snapshots[second_id] == ModelSettings(**_REASONING)


def test_idempotent_retry_of_legacy_goal_does_not_assign_it_a_new_snapshot(api):
    client, store = api
    key = "legacy-goal"
    goal_id = str(uuid5(NAMESPACE_URL, f"rocky:{key}"))
    original = store.create_goal(goal_id, contract())
    assert original.model_settings is None
    client.put("/model-settings", json=_REASONING, headers=_AUTH)
    response = client.post(
        "/goals",
        json=contract().model_dump(mode="json"),
        headers=_AUTH | {"Idempotency-Key": key},
    )
    assert response.status_code == 202
    assert response.json()["model_settings"] is None
    assert store.get_goal(goal_id).model_settings is None


def test_existing_goal_table_upgrades_with_new_tables_without_alter_or_backfill(tmp_path):
    database_path = tmp_path / "legacy.db"
    old_contract = contract()
    old_state = RunState(goal_id="old-goal").model_dump(mode="json")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """CREATE TABLE goals (
                goal_id VARCHAR(128) PRIMARY KEY,
                contract JSON NOT NULL,
                state JSON NOT NULL,
                created_at VARCHAR(64) NOT NULL,
                dispatched BOOLEAN NOT NULL,
                cancel_requested BOOLEAN NOT NULL,
                revision INTEGER NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO goals VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "old-goal",
                old_contract.model_dump_json(),
                json.dumps(old_state),
                "2026-01-01",
                0,
                0,
                0,
            ),
        )

    database_url = f"sqlite:///{database_path}"
    store = Store(database_url)

    def goal_columns():
        return [
            {**column, "type": str(column["type"])}
            for column in inspect(store.engine).get_columns("goals")
        ]

    before = goal_columns()
    store.initialize()
    assert goal_columns() == before
    assert store.get_goal("old-goal").model_settings is None
    assert store.pending_goals()[0].model_settings is None
    assert store.get_model_settings(ModelSettings(**_FALLBACK)) == ModelSettings(**_FALLBACK)
    store.set_model_settings(ModelSettings(**_REASONING))
    store.create_goal("new-goal", contract(), ModelSettings(**_REASONING))
    store.close()

    reopened = Store(database_url)
    try:
        reopened.initialize()
        assert reopened.get_goal("old-goal").model_settings is None
        assert reopened.get_goal("new-goal").model_settings == ModelSettings(**_REASONING)
        assert reopened.get_model_settings(ModelSettings(**_FALLBACK)) == ModelSettings(
            **_REASONING
        )
    finally:
        reopened.close()


def test_concurrent_first_default_updates_are_atomic_and_leave_one_valid_setting(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'concurrent-defaults.db'}")
    store.initialize()
    choices = [ModelSettings(**_FALLBACK), ModelSettings(**_REASONING)] * 3
    start = Barrier(len(choices))

    def save(choice):
        start.wait(timeout=10)
        return store.set_model_settings(choice)

    try:
        with ThreadPoolExecutor(max_workers=len(choices)) as executor:
            futures = [executor.submit(save, choice) for choice in choices]
            assert [future.result(timeout=15) for future in futures] == choices
        assert store.get_model_settings(ModelSettings(**_FALLBACK)) in choices
    finally:
        store.close()
