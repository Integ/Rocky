"""Goal listing cost: shared-file concurrency, one query per page, revalidated polls."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from rocky.api import create_app
from rocky.config import Settings
from rocky.inference import ModelSettings
from rocky.models import Deliverable, GoalContract, RunState
from rocky.store import Store

AUTH = {"Authorization": "Bearer listing-test-token"}
CONTRACT = GoalContract(
    objective="Write a report",
    acceptance_criteria=["Include the result"],
    deliverables=[Deliverable(path="report.md")],
)


@pytest.fixture
def store(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/listing.db")
    store.initialize()
    yield store
    store.close()


@pytest.fixture
def client(store):
    settings = Settings(auth_mode="token", api_token="listing-test-token")
    with TestClient(create_app(settings, store)) as client:
        yield client


def finish(store, goal_id, revision=1):
    store.save_state(RunState(goal_id=goal_id, revision=revision, status="succeeded"))


def test_file_database_uses_write_ahead_log(store):
    with store.engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_listing_loads_settings_and_lifecycle_in_one_query(store):
    for index in range(5):
        store.create_goal(f"goal-{index}", CONTRACT, ModelSettings(model=f"model-{index}"))
        finish(store, f"goal-{index}")
    store.set_archived("goal-1", True)
    statements = []
    event.listen(
        store.engine,
        "before_cursor_execute",
        lambda *args: statements.append(args[2]),
    )

    active = store.list_goals()
    archived = store.list_goals(archived=True)

    assert len(statements) == 2
    assert {goal.goal_id: goal.model_settings.model for goal in active} == {
        "goal-0": "model-0",
        "goal-2": "model-2",
        "goal-3": "model-3",
        "goal-4": "model-4",
    }
    assert all(not goal.archived for goal in active)
    assert [(goal.goal_id, goal.archived) for goal in archived] == [("goal-1", True)]


def test_pending_and_reconciliation_scans_return_full_views(store):
    store.create_goal("pending", CONTRACT, ModelSettings(model="pending-model"))
    store.create_goal("running", CONTRACT)
    store.mark_dispatched("running")
    store.save_state(RunState(goal_id="running", revision=1, status="running"))

    assert [goal.model_settings.model for goal in store.pending_goals()] == ["pending-model"]
    assert [(goal.goal_id, goal.model_settings) for goal in store.reconciliation_goals()] == [
        ("running", None)
    ]


def test_unchanged_list_revalidates_without_a_body(client, store):
    store.create_goal("goal", CONTRACT)
    first = client.get("/goals", headers=AUTH)
    tag = first.headers["ETag"]
    assert first.status_code == 200 and [goal["goal_id"] for goal in first.json()] == ["goal"]

    repeat = client.get("/goals", headers=AUTH | {"If-None-Match": tag})
    assert repeat.status_code == 304
    assert repeat.content == b""
    assert repeat.headers["ETag"] == tag
    # Weak and listed validators from intermediaries also match.
    weak = client.get("/goals", headers=AUTH | {"If-None-Match": f'"other", W/{tag}'})
    assert weak.status_code == 304
    # The validator never bypasses authentication.
    assert client.get("/goals", headers={"If-None-Match": tag}).status_code == 401


def test_every_visible_change_produces_a_new_list_tag(client, store):
    def tags():
        return tuple(
            client.get(f"/goals?archived={archived}", headers=AUTH).headers["ETag"]
            for archived in (False, True)
        )

    store.create_goal("other", CONTRACT)
    seen = [tags()]
    store.create_goal("goal", CONTRACT)
    seen.append(tags())
    store.save_state(RunState(goal_id="goal", revision=1, status="running"))
    seen.append(tags())
    finish(store, "goal", revision=2)
    seen.append(tags())
    store.set_archived("goal", True)
    seen.append(tags())
    store.set_archived("goal", False)
    restored = tags()
    store.delete_goal("goal")
    seen.append(tags())

    # Restoring or deleting returns an exact earlier listing, so its tag repeats.
    assert restored == seen[3]
    assert seen[-1] == seen[0]
    assert len(set(seen[:-1])) == len(seen) - 1
    # Archived and active listings are separate resources with separate tags.
    assert client.get("/goals?limit=10", headers=AUTH).headers["ETag"] != seen[-1][0]


def test_noop_command_lock_does_not_invalidate_the_list(client, store):
    store.create_goal("goal", CONTRACT)
    store.save_state(RunState(goal_id="goal", revision=1, status="running"))
    tag = client.get("/goals", headers=AUTH).headers["ETag"]
    store.enqueue_command("goal", "cancel")
    assert client.get("/goals", headers=AUTH | {"If-None-Match": tag}).status_code == 304
