"""Exercise an isolated, persistent Rocky service without live model calls.

Runs real API/worker/Temporal subprocesses with a loopback scripted model.
Each cycle kills a service child while approval is pending, verifies recovery,
injects HTTP 503s, runs a due schedule and file trigger, then backs up/restores.
Use --duration-hours 24 for repeated cycles; this does not simulate Mac sleep,
system reboot, launchd, Keychain, or real model quality.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from rocky.backup import restore_backup
from rocky.config import Settings

REPO = Path(__file__).resolve().parents[1]
REPORT = "A durable goal survives interruption.\n"
MARKER = "verified\n"
CONTRACT = {
    "objective": "Append the recovery sentence once to report.md and write marker.txt.",
    "acceptance_criteria": ["Both files contain the required fixture text exactly once"],
    "deliverables": [{"path": "report.md"}, {"path": "marker.txt"}],
    "limits": {
        "max_steps": 8,
        "max_model_calls": 12,
        "max_tokens": 50000,
        "timeout_seconds": 180,
    },
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for(probe, *, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(0.25)
    raise TimeoutError("Reliability probe did not complete before its deadline")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ModelFixture(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), ModelHandler)
        self.fail_next = 0
        self.failures = 0
        self.requests = 0
        self.errors = []


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        try:
            require(self.path == "/v1/chat/completions", "Unexpected fixture endpoint")
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.requests += 1
            if self.server.fail_next:
                self.server.fail_next -= 1
                self.server.failures += 1
                self.reply(503, {"error": {"type": "temporary_unavailable"}})
                return
            payload = json.loads(body["messages"][1]["content"])
            if "artifacts_text" in payload:
                decision = {
                    "passed": True,
                    "feedback": "Fixture files meet the deterministic contract",
                    "claims": [
                        {
                            "deliverable": name,
                            "pointer": "",
                            "value": "required fixture text",
                            "kind": "judgment",
                            "location": f"deliverable:{name}",
                            "quote": value.strip(),
                            "supported": True,
                        }
                        for name, value in (("report.md", REPORT), ("marker.txt", MARKER))
                    ],
                }
            else:
                step = payload["state"]["steps"]
                action = (
                    {"tool": "append_file", "path": "report.md", "content": REPORT}
                    if step == 1
                    else {"tool": "run_command", "content": "printf 'verified\\n' > marker.txt"}
                    if step == 2
                    else None
                )
                decision = {
                    "kind": "act" if action else "finish",
                    "reason": "Execute the fixed reliability fixture",
                    "action": action,
                }
            self.reply(
                200,
                {
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": json.dumps(decision)}}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                },
            )
        except Exception as error:
            self.server.errors.append(type(error).__name__)
            self.reply(500, {"error": {"type": "fixture_error"}})

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Service:
    def __init__(self, root, temporal):
        self.root, self.temporal = root, temporal
        self.port, self.temporal_port = free_port(), free_port()
        self.process = None
        # Every restarted child imports the same frozen source, even when the
        # developer continues editing the checkout during a long validation.
        source = root / "source"
        shutil.copytree(
            REPO / "src" / "rocky",
            source / "rocky",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        # An allowlist keeps the owner's API keys, provider configuration and
        # other Rocky instance settings out of all synthetic-test subprocesses.
        self.env = {
            key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ
        }
        self.env.update(
            {
                "PYTHONPATH": str(source),
                "ROCKY_DATABASE_URL": f"sqlite:///{root / 'rocky.db'}",
                "ROCKY_WORKSPACE_ROOT": str(root / "workspaces"),
                "ROCKY_CREDENTIAL_PATH": str(root / "unused-credentials.json"),
                "ROCKY_API_TOKEN_FILE": str(root / "api-token"),
                "ROCKY_RUN_DIR": str(root / "run"),
                "ROCKY_LOG_DIR": str(root / "logs"),
                "ROCKY_BACKUP_DIR": str(root / "backups"),
                "ROCKY_BACKUP_INTERVAL_HOURS": "0",
                "ROCKY_BACKUP_KEEP": "2",
                "ROCKY_TEMPORAL_BIN": str(temporal),
                "ROCKY_TEMPORAL_DB": str(root / "temporal.db"),
                "ROCKY_TEMPORAL_ADDRESS": f"127.0.0.1:{self.temporal_port}",
                "ROCKY_TASK_QUEUE": "reliability-fixture",
                "ROCKY_PORT": str(self.port),
                "ROCKY_AGENDA_INTERVAL_SECONDS": "1",
                "ROCKY_APPROVAL_TIMEOUT_MINUTES": "0",
            }
        )
        self.client = httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}", timeout=10, trust_env=False
        )

    def start(self):
        require(self.process is None or self.process.poll() is not None, "Service already running")
        with (self.root / "supervisor.log").open("ab") as log:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "rocky.cli", "serve"],
                cwd=self.root,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        def ready():
            require(
                self.process.poll() is None,
                "Isolated service exited during startup; inspect its logs",
            )
            token = self.root / "api-token"
            if not token.exists():
                return False
            self.client.headers["Authorization"] = "Bearer " + token.read_text().strip()
            try:
                response = self.client.get("/status")
                data = response.json()
                return (
                    response.status_code == 200
                    and data.get("worker", {}).get("temporal")
                    and (self.root / "run" / "worker-ready").exists()
                )
            except (httpx.HTTPError, ValueError):
                return False

        wait_for(ready)

    def stop(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
                raise RuntimeError("Service failed to stop gracefully") from None
        # Handles startup failures and a supervisor that exited prematurely.
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def api(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        require(response.is_success, f"{method} {path}: HTTP {response.status_code}")
        return response.json() if response.content else None

    def pending(self, goal_id):
        state = self.api("GET", f"/goals/{goal_id}")["state"]
        require(
            state["status"] not in {"failed", "cancelled", "budget_exhausted"},
            f"Fixture task ended: {state['status']}",
        )
        return state if state.get("pending_approval") else None

    def finish(self, goal_id):
        answered = set()

        def done():
            state = self.api("GET", f"/goals/{goal_id}")["state"]
            require(
                state["status"]
                not in {"failed", "cancelled", "budget_exhausted", "deadline_exceeded", "blocked"}
                or state.get("pending_approval"),
                f"Fixture task ended: {state['status']}",
            )
            pending = state.get("pending_approval")
            if pending and pending["fingerprint"] not in answered:
                self.api(
                    "POST",
                    f"/goals/{goal_id}/approve",
                    json={"fingerprint": pending["fingerprint"]},
                )
                answered.add(pending["fingerprint"])
            return state if state["status"] == "succeeded" else None

        state = wait_for(done)
        workspace = self.root / "workspaces" / goal_id
        require((workspace / "report.md").read_text() == REPORT, "Append was omitted or repeated")
        require((workspace / "marker.txt").read_text() == MARKER, "Approved command output differs")
        return state

    def crash_child(self, name):
        listing = subprocess.check_output(["ps", "-axo", "pid=,ppid=,command="], text=True)
        candidates = []
        for line in listing.splitlines():
            pid, parent, command = line.strip().split(None, 2)
            if int(parent) == self.process.pid and (
                f"rocky.cli {name}" in command
                or name == "temporal"
                and "server start-dev" in command
            ):
                candidates.append(int(pid))
        require(len(candidates) == 1, "Could not uniquely identify the isolated service child")
        os.kill(candidates[0], signal.SIGKILL)
        require(self.process.wait(timeout=25) != 0, "Child failure did not fail the supervisor")
        self.stop()

    def sample(self):
        status = self.api("GET", "/status")
        require(
            status["database"] == "ok"
            and status["worker"]["state"] == "up"
            and status["worker"]["temporal"],
            "Service degraded between cycles",
        )
        rss = 0
        listing = subprocess.check_output(["ps", "-axo", "pid=,ppid=,rss="], text=True)
        for line in listing.splitlines():
            pid, parent, resident = map(int, line.split())
            if pid == self.process.pid or parent == self.process.pid:
                rss += resident
        sample = {
            "at": datetime.now(UTC).isoformat(),
            "service_rss_kib": rss,
            "events": status["events"],
            "tasks": status["tasks"],
            "undispatched_goals": status["undispatched_goals"],
            "unsent_commands": status["unsent_commands"],
        }
        with (self.root / "samples.jsonl").open("a") as handle:
            handle.write(json.dumps(sample) + "\n")
        return sample


def run_cycle(service, fixture, cycle):
    started = time.monotonic()
    goal = service.api("POST", "/goals", json=CONTRACT)
    goal_id = goal["goal_id"]
    before = wait_for(lambda: service.pending(goal_id))
    workspace = service.root / "workspaces" / goal_id
    require((workspace / "report.md").read_text() == REPORT, "Initial append did not complete")
    require(not (workspace / "marker.txt").exists(), "Command executed before approval")
    child = ("worker", "api", "temporal")[(cycle - 1) % 3]
    service.crash_child(child)
    service.start()
    after = wait_for(lambda: service.pending(goal_id))
    for field in ("steps", "model_calls", "input_tokens", "output_tokens", "pending_approval"):
        require(before[field] == after[field], f"Restart changed pending state: {field}")
    require(not (workspace / "marker.txt").exists(), "Restart granted approval")
    failures = fixture.failures
    fixture.fail_next = 2
    service.finish(goal_id)
    require(fixture.failures >= failures + 2, "HTTP outage was not exercised")

    schedule = service.api(
        "POST",
        "/schedules",
        json={
            "name": f"Reliability once {cycle}",
            "contract": CONTRACT,
            "recurrence": {
                "kind": "once",
                "start_at": (datetime.now(UTC) + timedelta(seconds=3)).isoformat(),
            },
        },
    )
    scheduled = wait_for(lambda: service.api("GET", "/schedules")[-1].get("last_goal_id"))
    service.finish(scheduled)
    schedules = service.api("GET", "/schedules")
    require(
        next(s for s in schedules if s["schedule_id"] == schedule["schedule_id"])["runs"] == 1,
        "One-time schedule ran more than once",
    )
    service.api("DELETE", f"/schedules/{schedule['schedule_id']}")

    source = service.root / "input.txt"
    source.write_text(f"baseline-{cycle}")
    rule = service.api(
        "POST",
        "/trigger-rules",
        json={
            "name": f"Reliability file {cycle}",
            "source": "file",
            "match": str(source),
            "mode": "auto",
            "debounce_seconds": 5,
            "contract": CONTRACT,
        },
    )

    # Polling is every five seconds. Changing the file before the first scan
    # would establish the changed content as its baseline and test no event.
    def baseline_recorded():
        with sqlite3.connect(f"file:{service.root / 'rocky.db'}?mode=ro", uri=True) as connection:
            return connection.execute(
                "SELECT 1 FROM trigger_file_cursors WHERE rule_id = ?", (rule["rule_id"],)
            ).fetchone()

    wait_for(baseline_recorded)
    source.write_text(f"changed-{cycle}")

    def triggered():
        decisions = service.api("GET", "/trigger-decisions")
        return next(
            (
                d["goal_id"]
                for d in decisions
                if d["rule_id"] == rule["rule_id"] and d.get("goal_id")
            ),
            None,
        )

    service.finish(wait_for(triggered))
    time.sleep(2)
    decisions = [
        d for d in service.api("GET", "/trigger-decisions") if d["rule_id"] == rule["rule_id"]
    ]
    require(len(decisions) == 1, "File change produced duplicate decisions")
    service.api(
        "DELETE", f"/trigger-rules/{rule['rule_id']}", params={"revision": rule["revision"]}
    )

    backup = service.api("POST", "/backups")
    original = hashlib.sha256((workspace / "report.md").read_bytes()).hexdigest()
    service.stop()
    (workspace / "report.md").write_text("deliberately changed after backup")
    settings = Settings(
        database_url=service.env["ROCKY_DATABASE_URL"],
        workspace_root=service.root / "workspaces",
        temporal_db=service.root / "temporal.db",
        run_dir=service.root / "run",
    )
    aside = restore_backup(settings, Path(backup["path"]), run_dir=settings.run_dir)
    require(
        (aside / "workspaces" / goal_id / "report.md").read_text()
        == "deliberately changed after backup",
        "Restore did not preserve previous data",
    )
    # This directory contains only this runner's synthetic data. Keeping a
    # full pre-restore tree after every cycle would make a day-long run grow
    # quadratically; the byte comparison above verifies its preservation.
    shutil.rmtree(aside)
    service.start()
    require(
        service.api("GET", f"/goals/{goal_id}")["state"]["status"] == "succeeded",
        "Restore lost terminal state",
    )
    require(
        hashlib.sha256((workspace / "report.md").read_bytes()).hexdigest() == original,
        "Restore lost artifact bytes",
    )
    status = service.api("GET", "/status")
    require(
        status["undispatched_goals"]
        == status["unsent_commands"]
        == status["pending_approvals"]
        == 0,
        "Pending work remains after the cycle",
    )
    require(not fixture.errors, "The model fixture rejected an unexpected request")
    for name in ("rocky.db", "temporal.db"):
        with sqlite3.connect(f"file:{service.root / name}?mode=ro", uri=True) as connection:
            require(
                connection.execute("PRAGMA quick_check").fetchall() == [("ok",)],
                f"{name} failed integrity check",
            )
    return {
        "cycle": cycle,
        "crashed_child": child,
        "passed": True,
        "wall_seconds": round(time.monotonic() - started, 2),
        "model_503s": fixture.failures - failures,
        "events": status["events"],
        "data_bytes": sum(p.stat().st_size for p in service.root.rglob("*") if p.is_file()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    count = parser.add_mutually_exclusive_group()
    count.add_argument("--cycles", type=int, default=None)
    count.add_argument("--duration-hours", type=float)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--cycle-interval-seconds",
        type=float,
        default=300,
        help="minimum time between cycle starts for duration runs (default 300)",
    )
    parser.add_argument("--temporal-binary", type=Path, default=shutil.which("temporal"))
    args = parser.parse_args()
    if (
        (args.cycles is not None and args.cycles < 1)
        or args.cycle_interval_seconds < 0
        or (args.duration_hours is not None and args.duration_hours <= 0)
    ):
        parser.error("Cycle count and duration must be positive")
    if not args.temporal_binary or not args.temporal_binary.is_file():
        parser.error("Provide --temporal-binary pointing to a Temporal CLI")
    root = (
        args.run_dir
        or Path(tempfile.gettempdir()) / f"rocky-reliability-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    ).resolve()
    require(
        root != REPO and REPO not in root.parents, "Keep synthetic workspaces outside the checkout"
    )
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    fixture = ModelFixture()
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    service = Service(root, args.temporal_binary.resolve())
    started = time.monotonic()
    report = {
        "schema": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "live_model": False,
        "cycles": [],
        "passed": False,
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
        ).strip(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "requested_hours": args.duration_hours,
        "requested_cycles": args.cycles or (None if args.duration_hours else 1),
        "state": "running",
        "source_sha256": {
            str(path.relative_to(root / "source")): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root / "source").rglob("*"))
            if path.is_file()
        },
    }
    try:
        service.start()
        service.api(
            "PUT",
            "/provider-settings",
            json={
                "provider": "ollama",
                "base_url": f"http://127.0.0.1:{fixture.server_port}/v1",
                "model": "reliability-fixture",
            },
        )
        while (
            args.duration_hours is not None
            and time.monotonic() - started < args.duration_hours * 3600
            or args.duration_hours is None
            and len(report["cycles"]) < (args.cycles or 1)
        ):
            cycle_started = time.monotonic()
            result = run_cycle(service, fixture, len(report["cycles"]) + 1)
            result["service_rss_kib"] = service.sample()["service_rss_kib"]
            report["cycles"].append(result)
            (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(result), flush=True)
            if args.duration_hours:
                next_cycle = min(
                    cycle_started + args.cycle_interval_seconds,
                    started + args.duration_hours * 3600,
                )
                while time.monotonic() < next_cycle:
                    service.sample()
                    time.sleep(min(10, max(0, next_cycle - time.monotonic())))
        report["passed"] = True
        report["state"] = "passed"
    except Exception as error:
        report["state"] = "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        print(f"Reliability check failed: {type(error).__name__}: {error}", file=sys.stderr)
    finally:
        try:
            service.stop()
        except Exception as error:
            report["passed"], report["state"] = False, "failed"
            report["cleanup_error"] = type(error).__name__
        finally:
            service.client.close()
            fixture.shutdown()
            fixture.server_close()
            thread.join(timeout=5)
        report.update(
            {
                "wall_seconds": round(time.monotonic() - started, 2),
                "fixture_requests": fixture.requests,
                "fixture_errors": fixture.errors,
                "uncovered": [
                    "Mac sleep/wake",
                    "system reboot",
                    "launchd registration",
                    "Keychain",
                    "live model quality",
                ],
            }
        )
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Report: {root / 'report.json'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
