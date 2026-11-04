"""Upgrades keep existing data; older code never runs against a newer schema."""

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from rocky import store as store_module
from rocky.models import Deliverable, GoalContract
from rocky.store import SCHEMA_VERSION, SchemaTooNewError, Store

CONTRACT = GoalContract(
    objective="Write a report",
    acceptance_criteria=["Include evidence"],
    deliverables=[Deliverable(path="report.md")],
)


def open_store(path):
    store = Store(f"sqlite:///{path}")
    store.initialize()
    return store


def test_new_databases_start_at_the_current_version(tmp_path):
    store = open_store(tmp_path / "new.db")
    assert store.schema_version() == SCHEMA_VERSION
    store.initialize()
    assert store.schema_version() == SCHEMA_VERSION
    store.close()


def test_databases_from_before_versioning_upgrade_in_order(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    store = open_store(path)
    store.create_goal("kept", CONTRACT)
    with store.engine.begin() as connection:
        connection.execute(text("DROP TABLE schema_version"))
    store.close()

    applied = []

    def add_column(connection):
        applied.append(2)
        connection.execute(text("ALTER TABLE goals ADD COLUMN priority INTEGER DEFAULT 0"))

    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 2)
    monkeypatch.setitem(store_module.MIGRATIONS, 2, add_column)
    upgraded = open_store(path)
    assert applied == [2]
    assert upgraded.schema_version() == 2
    assert upgraded.get_goal("kept") is not None
    upgraded.initialize()
    assert applied == [2]
    upgraded.close()


def test_a_newer_schema_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "newer.db"
    open_store(path).close()
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 0)
    with pytest.raises(SchemaTooNewError):
        open_store(path)


def test_concurrent_first_initialization_serializes_schema_creation(tmp_path, monkeypatch):
    """Hold the first initializer inside DDL while another attempts startup.

    Before the fix the second initializer races past has_table and creates
    the tables; the held initializer then fails with 'table already exists'.
    """
    path = tmp_path / "concurrent.db"
    first, second = Store(f"sqlite:///{path}"), Store(f"sqlite:///{path}")
    # Establish WAL before testing DDL rather than racing the one-time journal
    # mode conversion; the separate process test covers unprepared databases.
    with first.engine.connect(), second.engine.connect():
        pass
    entered, release, other_started = threading.Event(), threading.Event(), threading.Event()
    create_all = store_module.Base.metadata.create_all

    def held_create_all(bind, *args, **kwargs):
        if threading.current_thread().name.startswith("first"):
            entered.set()
            assert release.wait(10)
        return create_all(bind, *args, **kwargs)

    monkeypatch.setattr(store_module.Base.metadata, "create_all", held_create_all)

    def initialize_second():
        other_started.set()
        second.initialize()

    try:
        with (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="first") as first_pool,
            ThreadPoolExecutor(max_workers=1) as second_pool,
        ):
            pending = first_pool.submit(first.initialize)
            assert entered.wait(10)
            other = second_pool.submit(initialize_second)
            assert other_started.wait(10)
            try:
                # A concurrent initializer must remain outside DDL until the
                # first commits. This wait is bounded and uses real SQLite.
                from concurrent.futures import TimeoutError

                with pytest.raises(TimeoutError):
                    other.result(timeout=0.2)
            finally:
                release.set()
            pending.result(timeout=10)
            other.result(timeout=10)
        assert first.schema_version() == second.schema_version() == SCHEMA_VERSION
        first.create_goal("kept", CONTRACT)
        assert second.get_goal("kept") is not None
    finally:
        release.set()
        first.close()
        second.close()


def test_failed_migration_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    path = tmp_path / "rollback.db"
    store = open_store(path)
    store.create_goal("kept", CONTRACT)

    def failing_migration(connection):
        connection.execute(text("ALTER TABLE goals ADD COLUMN interrupted INTEGER"))
        raise RuntimeError("interrupted migration")

    monkeypatch.setattr(store_module, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    monkeypatch.setitem(store_module.MIGRATIONS, SCHEMA_VERSION + 1, failing_migration)
    with pytest.raises(RuntimeError, match="interrupted migration"):
        store.initialize()
    assert store.schema_version() == SCHEMA_VERSION
    with store.engine.connect() as connection:
        assert "interrupted" not in [
            column["name"] for column in store_module.inspect(connection).get_columns("goals")
        ]
    assert store.get_goal("kept") is not None
    store.close()


@pytest.mark.parametrize("trial", range(5))
def test_separate_processes_initialize_an_unprepared_database(tmp_path, trial):
    source = """
import sys, time
from pathlib import Path
from rocky.store import Store
path, marker, ready = map(Path, sys.argv[1:])
store = Store(f"sqlite:///{path}")
ready.touch()
while not marker.exists():
    time.sleep(0.01)
try:
    store.initialize()
    assert store.schema_version() >= 1
finally:
    store.close()
"""
    marker = tmp_path / "start"
    children = []
    try:
        for index in range(6):
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        source,
                        str(tmp_path / "fresh.db"),
                        str(marker),
                        str(tmp_path / f"ready-{index}"),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        deadline = time.monotonic() + 15
        while len(list(tmp_path.glob("ready-*"))) != len(children):
            assert time.monotonic() < deadline, "Schema initializer failed to start"
            time.sleep(0.01)
        marker.touch()
        for child in children:
            stdout, stderr = child.communicate(timeout=35)
            assert child.returncode == 0, stdout + stderr
    finally:
        marker.touch()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
