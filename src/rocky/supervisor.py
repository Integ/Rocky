"""Run Temporal, the API and the worker as one long-lived service.

``rocky serve`` is what launchd (or systemd) keeps alive. It owns the service
lock, a persistent owner token, child processes and an optional idle-sleep
assertion. Any child exiting stops the rest and exits non-zero so the outer
supervisor restarts everything from durable state; SIGTERM stops cleanly.
"""

import asyncio
import fcntl
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from rocky.config import Settings

# A waiting task's durable timer only fires while the Mac is awake. Blocked
# tasks wait for the owner, who wakes the Mac anyway.
ACTIVE_STATUSES = {"queued", "running", "waiting"}
_TOKEN_BYTES = 32
_STOP_GRACE_SECONDS = 10.0


class SupervisorError(RuntimeError):
    pass


def load_or_create_token(path: Path) -> str:
    """Return the owner token, creating a private file on first use."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(secrets.token_urlsafe(_TOKEN_BYTES))
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as handle:
        details = os.fstat(handle.fileno())
        if details.st_uid != os.getuid() or details.st_mode & 0o077:
            raise SupervisorError("The API token file must be owned by this user with mode 0600")
        token = handle.read().strip()
    if len(token) < 32:
        raise SupervisorError("The API token file is empty or too short")
    return token


def temporal_endpoint(address: str) -> tuple[str, int]:
    host, separator, port = address.rpartition(":")
    if not separator or not host or not port.isdigit():
        raise SupervisorError("ROCKY_TEMPORAL_ADDRESS must be host:port")
    return host.strip("[]"), int(port)


def write_status(run_dir: Path, phase: str, detail: str = "") -> None:
    """Atomically publish the phase the desktop client shows while connecting."""
    temporary = run_dir / f".status.{os.getpid()}"
    temporary.write_text(json.dumps({"phase": phase, "detail": detail, "pid": os.getpid()}))
    temporary.replace(run_dir / "status")


def _rotate(path: Path) -> None:
    if path.exists():
        path.replace(path.with_name(path.name + ".1"))


@dataclass
class Child:
    name: str
    argv: list[str]
    process: subprocess.Popen | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None


class Supervisor:
    def __init__(
        self,
        settings: Settings,
        *,
        run_dir: Path,
        token_path: Path,
        log_dir: Path | None = None,
        temporal_bin: str | None = None,
        temporal_db: Path | None = None,
        prevent_sleep: bool = False,
        startup_timeout: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.run_dir = run_dir
        self.token_path = token_path
        self.log_dir = log_dir
        self.temporal_bin = temporal_bin
        self.temporal_db = temporal_db
        self.prevent_sleep = prevent_sleep
        self.startup_timeout = startup_timeout
        self.clock = clock
        self.children: list[Child] = []
        self.ready_file = run_dir / "worker-ready"
        self._stopping = False
        self._caffeinate: subprocess.Popen | None = None
        self._token = ""

    # -- lifecycle -----------------------------------------------------------

    def request_stop(self, *_args) -> None:
        self._stopping = True

    def run(self) -> int:
        self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with ExitStack() as stack:
            lock = os.open(
                self.run_dir / "service.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            stack.callback(os.close, lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Another Rocky service already owns this data directory", file=sys.stderr)
                return 1
            stack.callback(self._stop_children)
            for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(signum, self.request_stop)
            try:
                self._token = load_or_create_token(self.token_path)
                self.ready_file.unlink(missing_ok=True)
                write_status(self.run_dir, "starting", "正在启动执行引擎")
                self._start_all()
                write_status(self.run_dir, "ready")
                return self._watch()
            except SupervisorError as exc:
                write_status(self.run_dir, "failed", str(exc))
                print(f"Rocky service stopped: {exc}", file=sys.stderr)
                return 1

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(
            {
                # The service is shared with every local program, so the API
                # always requires the owner token rather than loopback trust.
                "ROCKY_AUTH_MODE": "token",
                "ROCKY_API_TOKEN": self._token,
                "ROCKY_WORKER_READY_FILE": str(self.ready_file),
                "PYTHONUNBUFFERED": "1",
            }
        )
        return environment

    def _launch(self, child: Child, environment: dict[str, str]) -> None:
        output = None
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.log_dir / f"{child.name}.log"
            _rotate(path)
            descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            output = os.fdopen(descriptor, "wb")
        try:
            child.process = subprocess.Popen(
                child.argv,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT if output else None,
                close_fds=True,
            )
        finally:
            if output is not None:
                output.close()
        self.children.append(child)

    def _start_all(self) -> None:
        environment = self._environment()
        if self.temporal_bin:
            host, port = temporal_endpoint(self.settings.temporal_address)
            argv = [self.temporal_bin, "server", "start-dev", "--ip", host, "--port", str(port)]
            argv.append("--headless")
            if self.temporal_db is not None:
                argv += ["--db-filename", str(self.temporal_db)]
            self._launch(Child("temporal", argv), environment)
            self._wait("Temporal", self._temporal_reachable)
        python = sys.executable
        self._launch(Child("api", [python, "-m", "rocky.cli", "api"]), environment)
        self._launch(Child("worker", [python, "-m", "rocky.cli", "worker"]), environment)
        self._wait("API and worker", lambda: self.ready_file.exists() and self._api_healthy())

    def _wait(self, label: str, ready: Callable[[], bool]) -> None:
        deadline = self.clock() + self.startup_timeout
        while self.clock() < deadline:
            if self._stopping:
                raise SupervisorError("Stopped during startup")
            self._check_children()
            if ready():
                return
            time.sleep(0.25)
        raise SupervisorError(f"{label} did not become ready in time")

    def _check_children(self) -> None:
        for child in self.children:
            if not child.running:
                code = child.process.returncode if child.process else None
                raise SupervisorError(f"{child.name} exited unexpectedly (code {code})")

    def _watch(self) -> int:
        next_activity_check = 0.0
        while not self._stopping:
            try:
                self._check_children()
            except SupervisorError as exc:
                write_status(self.run_dir, "failed", str(exc))
                print(f"Rocky service restarting: {exc}", file=sys.stderr)
                return 1
            if self.prevent_sleep and self.clock() >= next_activity_check:
                self._update_sleep_assertion()
                next_activity_check = self.clock() + 15
            time.sleep(0.5)
        write_status(self.run_dir, "stopped")
        return 0

    def _stop_children(self) -> None:
        self._set_sleep_assertion(False)
        # Stop consumers before Temporal so the worker can finish heartbeats.
        for child in reversed(self.children):
            if child.running:
                child.process.terminate()
        deadline = time.monotonic() + _STOP_GRACE_SECONDS
        for child in reversed(self.children):
            if child.process is None:
                continue
            try:
                child.process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.process.kill()
                child.process.wait()
        self.ready_file.unlink(missing_ok=True)

    # -- probes --------------------------------------------------------------

    def _api_url(self) -> str:
        host = self.settings.host
        host = "127.0.0.1" if host in {"0.0.0.0", "localhost"} else host
        host = f"[{host}]" if ":" in host else host
        return f"http://{host}:{self.settings.port}"

    def _get(self, path: str, timeout: float = 2.0) -> httpx.Response:
        return httpx.get(
            self._api_url() + path,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=timeout,
            trust_env=False,
        )

    def _api_healthy(self) -> bool:
        try:
            response = self._get("/health")
            return response.status_code == 200 and response.json().get("service") == "rocky"
        except (httpx.HTTPError, ValueError):
            return False

    def _temporal_reachable(self) -> bool:
        from temporalio.client import Client

        async def probe() -> None:
            await asyncio.wait_for(
                Client.connect(
                    self.settings.temporal_address, namespace=self.settings.temporal_namespace
                ),
                timeout=2,
            )

        try:
            asyncio.run(probe())
            return True
        except Exception:
            return False

    # -- idle sleep ----------------------------------------------------------

    def _has_active_goals(self) -> bool:
        try:
            response = self._get("/goals?limit=100", timeout=5)
            goals = response.json() if response.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            return False
        return any(
            isinstance(goal, dict) and (goal.get("state") or {}).get("status") in ACTIVE_STATUSES
            for goal in goals
        )

    def _update_sleep_assertion(self) -> None:
        self._set_sleep_assertion(self._has_active_goals())

    def _set_sleep_assertion(self, active: bool) -> None:
        held = self._caffeinate is not None and self._caffeinate.poll() is None
        if active and not held and Path("/usr/bin/caffeinate").exists():
            # -i prevents idle sleep only; closing the lid still sleeps the Mac.
            # -w ties the assertion to this process if the supervisor dies.
            self._caffeinate = subprocess.Popen(
                ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif not active and held:
            self._caffeinate.terminate()
            self._caffeinate.wait()
            self._caffeinate = None


def serve(settings: Settings | None = None) -> int:
    settings = settings or Settings()
    environ = os.environ
    data = Path(settings.database_url.removeprefix("sqlite:///")).parent
    if not settings.database_url.startswith("sqlite:///"):
        data = Path("./data")
    run_dir = Path(environ.get("ROCKY_RUN_DIR") or data / "run")
    token_path = Path(environ.get("ROCKY_API_TOKEN_FILE") or data / "api-token")
    log_dir = Path(environ["ROCKY_LOG_DIR"]) if environ.get("ROCKY_LOG_DIR") else None
    temporal_db = environ.get("ROCKY_TEMPORAL_DB")
    supervisor = Supervisor(
        settings,
        run_dir=run_dir.expanduser(),
        token_path=token_path.expanduser(),
        log_dir=log_dir,
        temporal_bin=environ.get("ROCKY_TEMPORAL_BIN") or None,
        temporal_db=Path(temporal_db) if temporal_db else None,
        prevent_sleep=environ.get("ROCKY_PREVENT_SLEEP", "").lower() in {"1", "true", "yes"},
    )
    return supervisor.run()
