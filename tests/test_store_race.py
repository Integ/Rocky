"""Real SQLite regressions for overlapping persistence Activities and retries."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event, current_thread

import pytest

from rocky.models import Deliverable, GoalContract, RunState
from rocky.store import Store


@pytest.fixture
def store(tmp_path):
    database = Store(f"sqlite:///{tmp_path / 'state-race.db'}")
    database.initialize()
    database.create_goal(
        "goal-race",
        GoalContract(
            objective="Write the final report.",
            acceptance_criteria=["The report is complete."],
            deliverables=[Deliverable(path="report.md")],
        ),
    )
    try:
        yield database
    finally:
        database.close()


def snapshot(revision: int, *, terminal: bool = False) -> RunState:
    return RunState(
        goal_id="goal-race",
        revision=revision,
        status="succeeded" if terminal else "running",
        reason=f"Snapshot {revision}",
        steps=revision,
        # Identical wall-clock timestamps are intentional: durable ordering
        # comes from revisions even when Workflow time does not advance.
        updated_at="2099-01-01T00:00:00+00:00",
    )


def event_revisions(store: Store) -> list[int]:
    return [event["state"]["revision"] for event in store.events("goal-race")]


def test_late_snapshot_and_duplicate_revision_do_not_regress_state_or_append_events(store):
    store.save_state(snapshot(1))
    terminal = snapshot(3, terminal=True)
    store.save_state(terminal)

    # A timed-out Activity can finish after the retry and later snapshots.
    store.save_state(snapshot(2))
    store.save_state(terminal.model_copy(deep=True))
    # Even a different payload cannot reuse a persisted revision.
    store.save_state(snapshot(3))

    assert store.get_goal("goal-race").state == terminal
    assert event_revisions(store) == [1, 3]


def test_revision_is_authoritative_when_wall_clock_order_differs(store):
    earlier = snapshot(1)
    earlier.updated_at = "2099-12-31T23:59:59+00:00"
    terminal = snapshot(2, terminal=True)
    terminal.updated_at = "2000-01-01T00:00:00+00:00"
    store.save_state(earlier)
    store.save_state(terminal)

    assert store.get_goal("goal-race").state == terminal
    assert event_revisions(store) == [1, 2]


def test_stale_transaction_cannot_overwrite_concurrently_committed_terminal_state(
    store, monkeypatch
):
    """Hold an older persistence operation across a newer transaction's commit.

    Instrument the session boundary to reproduce both a stale SELECT snapshot
    and a delayed UPDATE. This would deterministically expose a read/check/write
    implementation even when ordinary thread scheduling avoids the race.
    """
    real_sessions = store.sessions
    stale_held = Event()
    release_stale = Event()

    class PausingSessions:
        @contextmanager
        def begin(self):
            with real_sessions.begin() as session:
                original_get = session.get
                original_execute = session.execute
                already_held = False

                def hold_stale():
                    nonlocal already_held
                    if current_thread().name.startswith("older-persist") and not already_held:
                        already_held = True
                        stale_held.set()
                        if not release_stale.wait(timeout=10):
                            raise TimeoutError(
                                "The test did not release the stale persistence call"
                            )

                def get(*args, **kwargs):
                    result = original_get(*args, **kwargs)
                    hold_stale()
                    return result

                def execute(*args, **kwargs):
                    hold_stale()
                    return original_execute(*args, **kwargs)

                session.get = get
                session.execute = execute
                yield session

        def __call__(self):
            return real_sessions()

    monkeypatch.setattr(store, "sessions", PausingSessions())
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="older-persist") as executor:
        stale = executor.submit(store.save_state, snapshot(1))
        try:
            assert stale_held.wait(timeout=10), "The stale transaction never started"
            terminal = snapshot(2, terminal=True)
            store.save_state(terminal)
        finally:
            release_stale.set()
        stale.result(timeout=10)

    assert store.get_goal("goal-race").state == terminal
    assert event_revisions(store) == [2]


def test_concurrent_sqlite_persists_keep_projection_and_events_monotonic(store):
    revisions = [7, 2, 10, 1, 12, 5, 9, 3, 11, 4, 8, 6]
    highest_revision = max(revisions)
    start = Barrier(len(revisions) + 1)

    def persist(revision):
        start.wait(timeout=10)
        store.save_state(snapshot(revision, terminal=revision == highest_revision))

    with ThreadPoolExecutor(max_workers=len(revisions)) as executor:
        futures = [executor.submit(persist, revision) for revision in revisions]
        start.wait(timeout=10)
        for future in futures:
            future.result(timeout=20)

    state = store.get_goal("goal-race").state
    recorded = event_revisions(store)
    assert state == snapshot(highest_revision, terminal=True)
    assert recorded == sorted(set(recorded))
    assert recorded[-1] == highest_revision
    assert all(1 <= revision <= highest_revision for revision in recorded)
    assert store.events("goal-race")[-1]["state"] == state.model_dump(mode="json")
