"""Completed task lifecycle, persistence, and protection against delayed retries."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, current_thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from rocky.api import create_app
from rocky.config import Settings
from rocky.inference import ModelSettings
from rocky.models import Deliverable, GoalContract, RunState
from rocky.service import Dispatcher
from rocky.store import (
    CommandRow,
    EventRow,
    GoalLifecycleRow,
    GoalModelSettingsRow,
    GoalRow,
    Store,
)

AUTH = {"Authorization": "Bearer lifecycle-test-token"}
CONTRACT = GoalContract(
    objective="Write a report",
    acceptance_criteria=["Include the result"],
    deliverables=[Deliverable(path="report.md")],
)


@pytest.fixture
def api(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/lifecycle.db")
    settings = Settings(
        auth_mode="token",
        api_token="lifecycle-test-token",
        workspace_root=tmp_path / "workspaces",
    )
    with TestClient(create_app(settings, store)) as client:
        yield client, store
    store.close()


def test_deleting_a_task_removes_its_workspace_and_receipts(api, tmp_path):
    client, store = api
    goal_id = submit(client, "files")
    workspace = tmp_path / "workspaces" / goal_id
    receipts = tmp_path / "workspaces" / ".rocky" / goal_id
    (workspace / "notes").mkdir(parents=True)
    (workspace / "notes" / "report.md").write_text("report")
    receipts.mkdir(parents=True)
    (receipts / "receipt.json").write_text("{}")
    outside = tmp_path / "owner-project"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    finish(store, goal_id)

    assert client.delete(f"/goals/{goal_id}", headers=AUTH).status_code == 200

    assert not workspace.exists() and not receipts.exists()
    assert (outside / "keep.txt").read_text() == "keep"


def submit(client, key="lifecycle"):
    response = client.post(
        "/goals",
        json=CONTRACT.model_dump(mode="json"),
        headers=AUTH | {"Idempotency-Key": key},
    )
    assert response.status_code == 202
    return response.json()["goal_id"]


def finish(store, goal_id, status="succeeded"):
    state = RunState(goal_id=goal_id, revision=1, status=status, reason="Finished")
    store.save_state(state)
    return state


@pytest.mark.parametrize(
    "status", ["succeeded", "failed", "cancelled", "budget_exhausted", "deadline_exceeded"]
)
def test_completed_tasks_can_be_archived_inspected_and_restored(api, status):
    client, store = api
    goal_id = submit(client)
    store.enqueue_command(goal_id, "wake", "Saved user feedback")
    finish(store, goal_id, status)
    before = client.get(f"/goals/{goal_id}/events", headers=AUTH).json()

    archived = client.post(f"/goals/{goal_id}/archive", headers=AUTH)
    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert archived.json()["state"]["status"] == status
    assert client.post(f"/goals/{goal_id}/archive", headers=AUTH).status_code == 200
    assert client.get("/goals", headers=AUTH).json() == []
    archive = client.get("/goals?archived=true", headers=AUTH).json()
    assert [item["goal_id"] for item in archive] == [goal_id]
    assert client.get(f"/goals/{goal_id}", headers=AUTH).json()["archived"] is True
    assert client.get(f"/goals/{goal_id}/events", headers=AUTH).json() == before
    assert (
        client.get(f"/goals/{goal_id}/messages", headers=AUTH).json()[0]["message"]
        == "Saved user feedback"
    )
    assert store.pending_goals() == []

    restored = client.post(f"/goals/{goal_id}/restore", headers=AUTH)
    assert restored.status_code == 200
    assert restored.json()["archived"] is False
    assert [item["goal_id"] for item in client.get("/goals", headers=AUTH).json()] == [goal_id]
    assert client.get("/goals?archived=true", headers=AUTH).json() == []


@pytest.mark.parametrize("status", ["queued", "running", "waiting", "blocked"])
def test_unfinished_tasks_reject_archive_restore_and_delete(api, status):
    client, store = api
    goal_id = submit(client)
    if status != "queued":
        store.save_state(RunState(goal_id=goal_id, revision=1, status=status))
    for action in ("archive", "restore"):
        response = client.post(f"/goals/{goal_id}/{action}", headers=AUTH)
        assert response.status_code == 409
    assert client.delete(f"/goals/{goal_id}", headers=AUTH).status_code == 409
    assert client.get(f"/goals/{goal_id}", headers=AUTH).json()["state"]["status"] == status
    assert len(client.get("/goals", headers=AUTH).json()) == 1


@pytest.mark.parametrize("archive_first", [False, True])
def test_deletion_removes_task_details_and_prevents_idempotent_resurrection(api, archive_first):
    client, store = api
    goal_id = submit(client)
    store.enqueue_command(goal_id, "wake", "Delete this saved feedback")
    finish(store, goal_id)
    if archive_first:
        client.post(f"/goals/{goal_id}/archive", headers=AUTH)

    response = client.delete(f"/goals/{goal_id}", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"goal_id": goal_id, "deleted": True}
    assert client.get("/goals", headers=AUTH).json() == []
    assert client.get("/goals?archived=true", headers=AUTH).json() == []
    assert store.pending_goals() == []
    assert store.pending_commands() == []
    assert store.events(goal_id) == []
    assert store.goal_messages(goal_id) == []
    assert store.is_cancel_requested(goal_id)
    for suffix in ("", "/events", "/messages"):
        assert client.get(f"/goals/{goal_id}{suffix}", headers=AUTH).status_code == 404
    for action in ("archive", "restore", "cancel"):
        assert client.post(f"/goals/{goal_id}/{action}", headers=AUTH).status_code == 404
    assert client.delete(f"/goals/{goal_id}", headers=AUTH).status_code == 404

    repeated = client.post(
        "/goals",
        json=CONTRACT.model_dump(mode="json"),
        headers=AUTH | {"Idempotency-Key": "lifecycle"},
    )
    assert repeated.status_code == 409
    # A timed-out final persistence Activity can finish after the user deletes.
    store.save_state(RunState(goal_id=goal_id, revision=100, status="succeeded"))
    assert store.get_goal(goal_id) is None
    assert store.events(goal_id) == []
    with store.sessions() as session:
        tombstone = session.get(GoalRow, goal_id)
        assert tombstone.contract == {} and tombstone.state == {}
        assert session.get(GoalLifecycleRow, goal_id).deleted is True
        for table in (EventRow, CommandRow, GoalModelSettingsRow):
            assert list(session.scalars(select(table).where(table.goal_id == goal_id))) == []


def test_lifecycle_endpoints_require_owner_and_report_missing_goals(api):
    client, _ = api
    for action in ("archive", "restore"):
        assert client.post(f"/goals/missing/{action}").status_code == 401
        assert client.post(f"/goals/missing/{action}", headers=AUTH).status_code == 404
    assert client.delete("/goals/missing").status_code == 401
    assert client.delete("/goals/missing", headers=AUTH).status_code == 404


def test_archive_metadata_and_deleted_identity_survive_store_restart(tmp_path):
    url = f"sqlite:///{tmp_path}/restart.db"
    first = Store(url)
    first.initialize()
    for goal_id in ("archive", "delete"):
        first.create_goal(goal_id, CONTRACT, ModelSettings(model="gpt-4.1-mini"))
        finish(first, goal_id)
    first.set_archived("archive", True)
    first.delete_goal("delete")
    first.close()

    second = Store(url)
    second.initialize()
    assert second.list_goals() == []
    assert [goal.goal_id for goal in second.list_goals(archived=True)] == ["archive"]
    assert second.get_goal("archive").model_settings == ModelSettings(model="gpt-4.1-mini")
    assert second.get_goal("delete") is None
    with pytest.raises(ValueError, match="deleted"):
        second.create_goal("delete", CONTRACT)
    second.close()


def test_existing_database_gains_lifecycle_metadata_without_losing_goals(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/upgrade.db")
    store.initialize()
    store.create_goal("existing", CONTRACT)
    finish(store, "existing")
    # The previous application version has the original goal schema only.
    GoalLifecycleRow.__table__.drop(store.engine)
    store.initialize()
    assert store.get_goal("existing").archived is False
    assert store.list_goals()[0].goal_id == "existing"
    assert store.set_archived("existing", True).archived is True
    store.close()


def test_legacy_budget_fields_do_not_change_idempotent_submission(api):
    client, store = api
    legacy = CONTRACT.model_dump(mode="json") | {
        "max_steps": 20,
        "max_model_calls": 30,
        "max_output_tokens": 4096,
        "deadline_seconds": 86400,
    }
    headers = AUTH | {"Idempotency-Key": "upgraded-task"}
    first = client.post("/goals", json=legacy, headers=headers)
    assert first.status_code == 202
    retry = client.post("/goals", json=CONTRACT.model_dump(mode="json"), headers=headers)
    assert retry.status_code == 202
    assert retry.json()["goal_id"] == first.json()["goal_id"]
    assert len(store.pending_goals()) == 1


async def test_dispatcher_drops_a_command_snapshot_for_a_deleted_task(api, monkeypatch):
    client, store = api
    goal_id = submit(client)
    store.mark_dispatched(goal_id)
    store.enqueue_command(goal_id, "wake", "Saved before completion")
    stale_commands = store.pending_commands()
    finish(store, goal_id)
    store.delete_goal(goal_id)
    monkeypatch.setattr(store, "pending_commands", lambda: stale_commands)

    def unexpected_signal(_):
        raise AssertionError("Deleted task must not receive a signal")

    await Dispatcher(
        SimpleNamespace(get_workflow_handle=unexpected_signal), store, Settings()
    ).tick()
    assert store.get_goal(goal_id) is None


def test_delayed_enqueue_cannot_insert_command_after_deletion(api, monkeypatch):
    client, store = api
    goal_id = submit(client)
    real_sessions = store.sessions
    held, release = Event(), Event()

    class PausingSessions:
        @contextmanager
        def begin(self):
            with real_sessions.begin() as session:
                original_get, original_execute = session.get, session.execute
                already_held = False

                def pause():
                    nonlocal already_held
                    if current_thread().name.startswith("late-command") and not already_held:
                        already_held = True
                        held.set()
                        if not release.wait(timeout=10):
                            raise TimeoutError("Delayed command was not released")

                def get(*args, **kwargs):
                    result = original_get(*args, **kwargs)
                    pause()
                    return result

                def execute(*args, **kwargs):
                    pause()
                    return original_execute(*args, **kwargs)

                session.get, session.execute = get, execute
                yield session

        def __call__(self):
            return real_sessions()

    monkeypatch.setattr(store, "sessions", PausingSessions())
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="late-command") as executor:
        delayed = executor.submit(store.enqueue_command, goal_id, "wake", "Late feedback")
        try:
            assert held.wait(timeout=10)
            finish(store, goal_id)
            store.delete_goal(goal_id)
        finally:
            release.set()
        with pytest.raises(KeyError):
            delayed.result(timeout=10)
    assert store.pending_commands() == []
    assert store.goal_messages(goal_id) == []


def test_delayed_persistence_cannot_recreate_deleted_task_data(api, monkeypatch):
    client, store = api
    goal_id = submit(client)
    finish(store, goal_id)
    real_sessions = store.sessions
    held, release = Event(), Event()

    class PausingSessions:
        @contextmanager
        def begin(self):
            with real_sessions.begin() as session:
                original_execute = session.execute

                def execute(*args, **kwargs):
                    if current_thread().name.startswith("late-persist"):
                        held.set()
                        if not release.wait(timeout=10):
                            raise TimeoutError("Delayed persistence was not released")
                    return original_execute(*args, **kwargs)

                session.execute = execute
                yield session

        def __call__(self):
            return real_sessions()

    monkeypatch.setattr(store, "sessions", PausingSessions())
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="late-persist") as executor:
        delayed = executor.submit(
            store.save_state, RunState(goal_id=goal_id, revision=2, status="running")
        )
        try:
            assert held.wait(timeout=10)
            store.delete_goal(goal_id)
        finally:
            release.set()
        delayed.result(timeout=10)
    assert store.get_goal(goal_id) is None
    assert store.events(goal_id) == []
