import fcntl
import json
import os
import stat
import sys
import threading
from pathlib import Path

import httpx
import pytest

from rocky.config import Settings
from rocky.supervisor import (
    Child,
    Supervisor,
    SupervisorError,
    load_or_create_token,
    temporal_endpoint,
)


def test_token_is_created_private_and_reused(tmp_path):
    path = tmp_path / "data" / "api-token"
    first = load_or_create_token(path)
    assert len(first) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_token(path) == first


def test_token_file_readable_by_others_is_rejected(tmp_path):
    path = tmp_path / "api-token"
    path.write_text("x" * 40)
    path.chmod(0o644)
    with pytest.raises(SupervisorError, match="0600"):
        load_or_create_token(path)


def test_token_symlink_is_rejected(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("x" * 40)
    target.chmod(0o600)
    (tmp_path / "api-token").symlink_to(target)
    with pytest.raises(OSError):
        load_or_create_token(tmp_path / "api-token")


@pytest.mark.parametrize(
    ("address", "expected"),
    [("127.0.0.1:18743", ("127.0.0.1", 18743)), ("[::1]:7233", ("::1", 7233))],
)
def test_temporal_endpoint(address, expected):
    assert temporal_endpoint(address) == expected


def test_temporal_endpoint_requires_port():
    with pytest.raises(SupervisorError):
        temporal_endpoint("localhost")


class ScriptedSupervisor(Supervisor):
    """Runs plain Python children instead of Temporal, the API and the worker."""

    def __init__(self, tmp_path: Path, scripts: dict[str, str]):
        super().__init__(
            Settings(),
            run_dir=tmp_path / "run",
            token_path=tmp_path / "api-token",
            log_dir=tmp_path / "logs",
        )
        self.scripts = scripts

    def _start_all(self) -> None:
        environment = self._environment()
        for name, script in self.scripts.items():
            self._launch(Child(name, [sys.executable, "-c", script]), environment)


def status(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "run" / "status").read_text())


def test_child_exit_stops_the_others_and_reports_failure(tmp_path):
    supervisor = ScriptedSupervisor(
        tmp_path,
        {"api": "import time; time.sleep(60)", "worker": "import time; time.sleep(0.3)"},
    )
    assert supervisor.run() == 1
    assert status(tmp_path)["phase"] == "failed"
    assert "worker exited" in status(tmp_path)["detail"]
    assert all(not child.running for child in supervisor.children)


def test_stop_request_terminates_children_and_exits_cleanly(tmp_path):
    supervisor = ScriptedSupervisor(
        tmp_path,
        {
            "api": "import time; time.sleep(60)",
            "worker": "import os; print(os.environ['ROCKY_AUTH_MODE'], flush=True); "
            "import time; time.sleep(60)",
        },
    )
    threading.Timer(0.5, supervisor.request_stop).start()
    assert supervisor.run() == 0
    assert status(tmp_path)["phase"] == "stopped"
    assert all(not child.running for child in supervisor.children)
    assert (tmp_path / "logs" / "worker.log").read_text().strip() == "token"


def test_children_receive_the_persistent_token(tmp_path):
    supervisor = ScriptedSupervisor(
        tmp_path,
        {"worker": "import os, sys; sys.exit(0 if os.environ['ROCKY_API_TOKEN'] else 3)"},
    )
    supervisor.run()
    token = (tmp_path / "api-token").read_text().strip()
    assert supervisor.children[0].process.returncode == 0
    assert len(token) >= 32


def test_second_service_on_the_same_data_directory_refuses_to_start(tmp_path):
    (tmp_path / "run").mkdir()
    descriptor = os.open(tmp_path / "run" / "service.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        supervisor = ScriptedSupervisor(tmp_path, {"api": "pass"})
        assert supervisor.run() == 1
        assert supervisor.children == []
    finally:
        os.close(descriptor)


def test_active_goals_detection(tmp_path, monkeypatch):
    supervisor = ScriptedSupervisor(tmp_path, {})
    goals = [{"state": {"status": "blocked"}}, {"state": {"status": "running"}}]
    monkeypatch.setattr(
        supervisor, "_get", lambda path, timeout=2.0: httpx.Response(200, json=goals)
    )
    assert supervisor._has_active_goals()
    goals[1]["state"]["status"] = "succeeded"
    assert not supervisor._has_active_goals()


def test_unreachable_api_does_not_hold_the_mac_awake(tmp_path, monkeypatch):
    supervisor = ScriptedSupervisor(tmp_path, {})

    def fail(path, timeout=2.0):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(supervisor, "_get", fail)
    assert not supervisor._has_active_goals()
