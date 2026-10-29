"""Upgrades keep existing data; older code never runs against a newer schema."""

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
