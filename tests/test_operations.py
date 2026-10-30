"""Backups, restore, service status, metrics, log trimming and token rotation."""

import json
import os
import sqlite3
import tarfile
import time

import pytest
from fastapi.testclient import TestClient

from rocky.api import create_app
from rocky.backup import BackupError, create_backup, list_backups, prune, restore_backup
from rocky.config import Settings
from rocky.monitoring import HEARTBEAT_FILE, prometheus, service_status
from rocky.store import Store
from rocky.supervisor import Child, Supervisor, load_or_create_token, rotate_token

AUTH = {"Authorization": "Bearer operations-test-token"}


@pytest.fixture
def settings(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    workspaces = data / "workspaces"
    (workspaces / "goal-1" / "node_modules" / "pkg").mkdir(parents=True)
    (workspaces / "goal-1" / "report.md").write_text("first version")
    (workspaces / "goal-1" / "node_modules" / "pkg" / "index.js").write_text("x")
    (workspaces / ".rocky" / "goal-1" / "browser" / "profile").mkdir(parents=True)
    (workspaces / ".rocky" / "goal-1" / "browser" / "profile" / "Cookies").write_text("c")
    (workspaces / ".rocky" / "goal-1" / "receipts").mkdir(parents=True)
    (workspaces / ".rocky" / "goal-1" / "receipts" / "r.json").write_text("{}")
    temporal = data / "temporal.db"
    with sqlite3.connect(temporal) as connection:
        connection.execute("create table history(value text)")
        connection.execute("insert into history values ('one')")
    value = Settings(
        database_url=f"sqlite:///{data / 'rocky.db'}",
        workspace_root=workspaces,
        temporal_db=temporal,
        run_dir=data / "run",
        backup_dir=tmp_path / "backups",
        auth_mode="token",
        api_token="operations-test-token",
    )
    Store(value.database_url).initialize()
    return value


def test_backup_snapshots_databases_and_skips_rebuildable_files(settings):
    path = create_backup(settings)
    assert path.stat().st_mode & 0o077 == 0
    with tarfile.open(path) as archive:
        names = set(archive.getnames())
        manifest = json.load(archive.extractfile("manifest.json"))
    assert {"rocky.db", "temporal.db", "workspaces/goal-1/report.md"} <= names
    assert "workspaces/.rocky/goal-1/receipts/r.json" in names
    assert not any("node_modules" in name or "/browser" in name for name in names)
    assert set(manifest["files"]) == {"rocky.db", "temporal.db"}


def test_restore_replaces_data_and_keeps_the_old_copy(settings):
    backup = create_backup(settings)
    report = settings.workspace_root / "goal-1" / "report.md"
    report.write_text("changed later")
    with sqlite3.connect(settings.temporal_db) as connection:
        connection.execute("insert into history values ('two')")
    aside = restore_backup(settings, backup, run_dir=settings.run_dir)
    assert report.read_text() == "first version"
    with sqlite3.connect(settings.temporal_db) as connection:
        assert connection.execute("select count(*) from history").fetchone() == (1,)
    assert (aside / "workspaces" / "goal-1" / "report.md").read_text() == "changed later"
    assert (aside / "temporal.db").exists()


def test_restore_refuses_a_running_service_and_a_damaged_backup(settings, tmp_path):
    import fcntl

    backup = create_backup(settings)
    settings.run_dir.mkdir()
    lock = os.open(settings.run_dir / "service.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        with pytest.raises(BackupError, match="Stop the Rocky service"):
            restore_backup(settings, backup, run_dir=settings.run_dir)
    finally:
        os.close(lock)
    damaged = tmp_path / "damaged.tar.gz"
    with tarfile.open(backup) as source, tarfile.open(damaged, "w:gz") as target:
        for member in source.getmembers():
            data = source.extractfile(member)
            if member.name == "rocky.db":
                content = b"not a database"
                member.size = len(content)
                import io

                data = io.BytesIO(content)
            target.addfile(member, data)
    report = settings.workspace_root / "goal-1" / "report.md"
    report.write_text("current")
    with pytest.raises(BackupError, match="damaged"):
        restore_backup(settings, damaged, run_dir=settings.run_dir)
    assert report.read_text() == "current"


def test_old_backups_are_pruned(settings):
    for _ in range(3):
        create_backup(settings)
    assert len(list_backups(settings.backup_dir)) == 3
    prune(settings.backup_dir, 1)
    assert len(list_backups(settings.backup_dir)) == 1
    create_backup(settings, keep=1)
    assert len(list_backups(settings.backup_dir)) == 1


def test_status_reports_worker_heartbeat_and_problems(settings, monkeypatch):
    monkeypatch.delenv("ROCKY_WORKER_READY_FILE", raising=False)
    store = Store(settings.database_url)
    status = service_status(settings, store)
    assert status["status"] == "degraded" and status["worker"]["state"] == "down"
    assert "最近没有成功的备份" in status["problems"]
    settings.run_dir.mkdir()
    (settings.run_dir / HEARTBEAT_FILE).write_text(
        json.dumps({"at": time.time(), "temporal": True})
    )
    create_backup(settings)
    status = service_status(settings, store)
    assert status["worker"]["state"] == "up" and status["worker"]["temporal"] is True
    assert status["status"] == "ok", status["problems"]
    text = prometheus(status)
    assert "rocky_up 1.0" in text and "rocky_worker_up 1.0" in text
    (settings.run_dir / HEARTBEAT_FILE).write_text(json.dumps({"at": time.time() - 600}))
    assert service_status(settings, store)["worker"]["state"] == "stale"


def test_status_metrics_and_backups_need_the_token(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/status").status_code == 401
        assert client.get("/metrics").status_code == 401
        assert client.get("/status", headers=AUTH).json()["database"] == "ok"
        assert "rocky_database_up 1.0" in client.get("/metrics", headers=AUTH).text
        created = client.post("/backups", headers=AUTH)
        assert created.status_code == 201
        listed = client.get("/backups", headers=AUTH).json()
        assert listed["backups"][0]["name"] == created.json()["name"]


def test_tools_cannot_reach_backups(settings):
    from rocky.tools import WorkspaceTools

    tools = WorkspaceTools(settings.model_copy(update={"tool_access": "full"}))
    assert any(
        settings.backup_dir.resolve() == path or settings.backup_dir.resolve().is_relative_to(path)
        for path in tools.protected_paths
    )


def test_logs_are_trimmed_without_stopping_children(settings, tmp_path):
    logs = tmp_path / "logs"
    supervisor = Supervisor(
        settings.model_copy(update={"log_max_bytes": 100_000}),
        run_dir=tmp_path / "run",
        token_path=tmp_path / "token",
        log_dir=logs,
    )
    child = Child("api", ["/bin/sh", "-c", "sleep 5"])
    supervisor._launch(child, dict(os.environ))
    try:
        log = logs / "api.log"
        log.write_bytes(b"x" * 200_000)
        supervisor._trim_logs()
        assert log.stat().st_size == 0
        assert (logs / "api.log.1").stat().st_size == 200_000
    finally:
        child.process.kill()
        child.process.wait()


def test_scheduled_backup_runs_once_per_interval(settings, tmp_path):
    supervisor = Supervisor(settings, run_dir=tmp_path / "run", token_path=tmp_path / "token")
    supervisor._schedule_backup()
    supervisor._backup.join(30)
    assert len(list_backups(settings.backup_dir)) == 1
    first = supervisor._backup
    supervisor._schedule_backup()
    assert supervisor._backup is first


def test_token_rotation_replaces_the_private_token(tmp_path):
    path = tmp_path / "data" / "api-token"
    old = load_or_create_token(path)
    rotate_token(path)
    new = load_or_create_token(path)
    assert new != old and len(new) >= 32 and path.stat().st_mode & 0o077 == 0


async def test_worker_heartbeat_records_temporal_reachability(tmp_path, monkeypatch):
    import asyncio

    from rocky import service

    class Health:
        async def check_health(self):
            return True

    class FakeClient:
        service_client = Health()

    path = tmp_path / HEARTBEAT_FILE
    task = asyncio.create_task(service._heartbeat(FakeClient(), path))
    for _ in range(50):
        if path.exists():
            break
        await asyncio.sleep(0.02)
    task.cancel()
    beat = json.loads(path.read_text())
    assert beat["temporal"] is True and time.time() - beat["at"] < 5
