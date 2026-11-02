import io
import json
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from rocky import storage, support_bundle
from rocky.api import create_app
from rocky.config import Settings
from rocky.model_queue import ModelQueue
from rocky.models import Deliverable, GoalContract
from rocky.storage import StorageInspector
from rocky.store import Store

AUTH = {"Authorization": "Bearer support-test-token"}


@pytest.fixture
def configured(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/rocky.db",
        workspace_root=tmp_path / "workspaces",
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backups",
        run_dir=tmp_path / "run",
        credential_path=tmp_path / "credentials.json",
        auth_mode="token",
        api_token="support-test-token",
    )
    store = Store(settings.database_url)
    store.initialize()
    yield settings, store
    store.close()


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def rows(summary):
    return {item["key"]: item for item in summary["categories"]}


def report(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert set(archive.namelist()) == {"report.json", "README.txt"}
        return json.loads(archive.read("report.json")), b"\n".join(
            archive.read(name) for name in archive.namelist()
        )


def test_storage_separates_deliveries_and_counts_caches_and_sqlite_sidecars(configured, tmp_path):
    settings, _ = configured
    workspace = settings.workspace_root
    first = write(workspace / "task" / "report.md", b"r" * 18000)
    write(workspace / "task" / "node_modules" / "pkg.js", b"dependency")
    write(workspace / ".rocky" / "task" / "deliveries" / "version" / "sha", b"snapshot")
    write(workspace / ".rocky" / "task" / "browser" / "Cookies", b"cookie")
    write(settings.log_dir / "worker.log.1", b"log")
    write(settings.backup_dir / "backup.tar.gz", b"backup")
    write(tmp_path / "rocky.db-journal", b"journal")
    os.link(first, workspace / "task" / "same-inode.md")
    result = StorageInspector(settings).summary()
    values = rows(result)
    assert result["complete"] and not result["cached"]
    assert values["workspaces"]["logical_bytes"] == 18010
    assert values["workspaces"]["files"] == 2
    assert values["deliveries"]["logical_bytes"] == 8
    assert values["task_data"]["logical_bytes"] == 6
    assert values["logs"]["logical_bytes"] == 3
    assert values["backups"]["logical_bytes"] == 6
    assert values["databases"]["logical_bytes"] >= (tmp_path / "rocky.db").stat().st_size + 7
    assert result["total_bytes"] == sum(item["bytes"] for item in values.values())


def test_overlapping_roots_are_not_counted_twice(configured):
    settings, _ = configured
    settings.backup_dir = settings.workspace_root / "nested-backups"
    write(settings.backup_dir / "saved.tar.gz", b"backup")
    write(settings.workspace_root / "task" / "file", b"task")
    values = rows(StorageInspector(settings).summary())
    assert values["backups"]["logical_bytes"] == 6
    assert values["workspaces"]["logical_bytes"] == 4


def test_storage_does_not_follow_links_or_read_regular_files(configured, tmp_path, monkeypatch):
    settings, _ = configured
    external = write(tmp_path / "external" / "secret.txt", b"private" * 10000)
    own = write(settings.workspace_root / "task" / "file", b"normal")
    (own.parent / "linked-folder").symlink_to(external.parent, target_is_directory=True)
    (own.parent / "linked-file").symlink_to(external)
    original = storage.os.open

    def only_directories(path, flags, *args, **kwargs):
        assert flags & os.O_DIRECTORY
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(storage.os, "open", only_directories)
    result = StorageInspector(settings).summary()
    assert result["skipped_links"] == 2 and result["complete"]
    assert rows(result)["workspaces"]["logical_bytes"] == 6


def test_directory_swapped_for_symlink_is_not_traversed(configured, tmp_path, monkeypatch):
    settings, _ = configured
    target = settings.workspace_root / "task" / "swapped"
    target.mkdir(parents=True)
    external = write(tmp_path / "external" / "private", b"s" * 10000).parent
    original = storage.os.open

    def swap(path, flags, *args, **kwargs):
        if path == "swapped":
            target.rmdir()
            target.symlink_to(external, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(storage.os, "open", swap)
    result = StorageInspector(settings).summary()
    assert not result["complete"] and result["unreadable_entries"] == 1
    assert rows(result)["workspaces"]["bytes"] == 0


def test_scan_limits_and_unreadable_roots_are_explicit(configured, monkeypatch):
    settings, _ = configured
    for index in range(15):
        write(settings.workspace_root / "task" / str(index), b"data")
    monkeypatch.setattr(storage, "MAX_ENTRIES", 10)
    result = StorageInspector(settings).summary()
    assert result["limited"] and not result["complete"]
    assert rows(result)["workspaces"]["files"] < 15
    monkeypatch.setattr(storage, "MAX_ENTRIES", 100000)
    monkeypatch.setattr(storage, "MAX_SECONDS", 0)
    result = StorageInspector(settings).summary()
    assert result["limited"] and not result["complete"]


def test_unreadable_root_does_not_hide_other_categories(configured, monkeypatch):
    settings, _ = configured
    write(settings.backup_dir / "saved.tar.gz", b"backup")
    write(settings.log_dir / "worker.log", b"log")
    original = storage.os.open

    def denied(path, *args, **kwargs):
        if path == settings.log_dir:
            raise PermissionError("private path")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(storage.os, "open", denied)
    result = StorageInspector(settings).summary()
    assert not result["complete"] and result["unreadable_entries"] == 1
    assert rows(result)["backups"]["logical_bytes"] == 6
    assert "private path" not in json.dumps(result)


def test_cache_keeps_old_measurement_until_refresh_and_coalesces_concurrent_refreshes(
    configured, monkeypatch
):
    settings, _ = configured
    inspector = StorageInspector(settings)
    first = inspector.summary()
    write(settings.workspace_root / "task" / "new", b"new file")
    assert inspector.summary()["cached"]
    assert inspector.summary()["total_bytes"] == first["total_bytes"]
    calls = 0
    started, release = threading.Event(), threading.Event()
    scan = inspector._scan

    def slow_scan():
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(5)
        return scan()

    monkeypatch.setattr(inspector, "_scan", slow_scan)
    with ThreadPoolExecutor(2) as pool:
        first_call = pool.submit(inspector.summary, refresh=True)
        assert started.wait(5)
        second_call = pool.submit(inspector.summary, refresh=True)
        # Ensure the other request has entered summary before the scan completes.
        time.sleep(0.03)
        release.set()
        first_result, second_result = first_call.result(), second_call.result()
    assert calls == 1
    assert rows(first_result)["workspaces"]["logical_bytes"] == 8
    assert second_result["cached"]


def test_storage_and_support_export_require_auth_and_do_not_expose_content(configured, monkeypatch):
    settings, store = configured
    secret = "SENSITIVE_support_key_and_personal_content"
    store.create_goal(
        secret,
        GoalContract(
            objective=secret,
            acceptance_criteria=[secret],
            deliverables=[Deliverable(path="report.md")],
        ),
    )
    write(settings.workspace_root / secret / (secret + ".txt"), secret.encode())
    write(settings.log_dir / (secret + ".log"), secret.encode())
    write(settings.credential_path, json.dumps({"api_key": secret}).encode())
    write(settings.backup_dir / (secret + ".tar.gz"), secret.encode())
    write(
        settings.run_dir / "worker-heartbeat",
        json.dumps({"at": time.time(), "temporal": True, "error": secret}).encode(),
    )
    monkeypatch.setenv("SECRET_PROVIDER_TOKEN", secret)
    monkeypatch.delenv("ROCKY_WORKER_READY_FILE", raising=False)
    with TestClient(create_app(settings, store=store)) as client:
        for endpoint in ("/storage", "/diagnostics/export"):
            assert client.get(endpoint).status_code == 401
        before = store.health_counts()
        response = client.get("/storage?refresh=true", headers=AUTH)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert secret not in response.text
        assert client.get("/storage", headers=AUTH).json()["cached"]
        response = client.get("/diagnostics/export", headers=AUTH)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["cache-control"] == "no-store"
        value, text = report(response.content)
        assert secret.encode() not in text
        assert str(settings.workspace_root).encode() not in text
        assert b"support-test-token" not in text
        assert value["health"]["tasks"]["queued"] == 1
        assert value["health"]["worker"]["state"] == "up"
        assert value["storage"]["categories"]
        assert value["unavailable_components"] == []
        assert len(response.content) < 50000
        assert store.health_counts() == before


def test_export_remains_available_when_health_and_queue_cannot_be_read(configured, monkeypatch):
    settings, store = configured
    queue = ModelQueue(store, settings)
    queue.configure()

    def broken(*args):
        raise RuntimeError("private database path and token")

    monkeypatch.setattr(support_bundle, "service_status", broken)
    monkeypatch.setattr(queue, "summary", broken)
    result, text = report(
        support_bundle.support_bundle(settings, store, queue, StorageInspector(settings))
    )
    assert result["health"] is None and result["model_queue"] is None
    assert result["unavailable_components"] == ["health", "model_queue"]
    assert b"private database path and token" not in text


def test_export_allowlist_discards_unexpected_health_strings(configured, monkeypatch):
    settings, store = configured
    queue = ModelQueue(store, settings)
    queue.configure()
    secret = "private_unexpected_value"
    monkeypatch.setattr(
        support_bundle,
        "service_status",
        lambda *_: {
            "status": secret,
            "database": "ok",
            "worker": {"state": "up", "temporal": secret},
            "tasks": {"failed": 2, secret: 1},
            "events": secret,
            "last_backup": {"path": secret, "name": secret, "bytes": 12},
            "problems": [secret, "执行进程没有响应"],
            "unknown": secret,
        },
    )
    result, text = report(
        support_bundle.support_bundle(settings, store, queue, StorageInspector(settings))
    )
    assert secret.encode() not in text
    assert result["health"]["status"] == "unknown"
    assert result["health"]["tasks"]["failed"] == 2
    assert result["health"]["events"] is None
    assert result["health"]["problem_codes"] == ["worker_stale"]


def test_storage_export_uses_its_own_allowlist(configured, monkeypatch):
    settings, store = configured
    inspector = StorageInspector(settings)
    raw = inspector.summary()
    raw["path"] = "private-file-path"
    raw["categories"][0]["path"] = "private-file-path"
    raw["categories"].append({"key": "private-file-path", "bytes": 4})
    monkeypatch.setattr(inspector, "summary", lambda: raw)
    result, text = report(
        support_bundle.support_bundle(settings, store, ModelQueue(store, settings), inspector)
    )
    assert b"private-file-path" not in text
    assert len(result["storage"]["categories"]) == 6
