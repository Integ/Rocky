"""Service status for the owner: what is running, what is stuck, what is at risk.

/health stays a bare liveness probe. /status adds readiness: the database, the
worker's heartbeat and its view of Temporal, task and outbox counts, pending
approvals, free disk space and the newest backup. /metrics returns the same
numbers in Prometheus text format. Neither includes task content.
"""

import json
import os
import shutil
import time
from pathlib import Path

from rocky import __version__
from rocky.backup import default_backup_dir, last_backup, sqlite_path

HEARTBEAT_FILE = "worker-heartbeat"
HEARTBEAT_SECONDS = 15
# Missing three beats means the worker is gone or wedged.
STALE_HEARTBEAT_SECONDS = HEARTBEAT_SECONDS * 3 + 5
LOW_DISK_BYTES = 2_000_000_000
_STARTED = time.time()


def heartbeat_path(settings) -> Path | None:
    ready = os.environ.get("ROCKY_WORKER_READY_FILE")
    if ready:
        return Path(ready).with_name(HEARTBEAT_FILE)
    if settings.run_dir is not None:
        return Path(settings.run_dir).expanduser() / HEARTBEAT_FILE
    return None


def _worker(settings, now: float) -> dict:
    path = heartbeat_path(settings)
    if path is None:
        return {"state": "unknown", "temporal": None, "age_seconds": None}
    try:
        beat = json.loads(path.read_text())
        age = max(0.0, now - float(beat["at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "down", "temporal": None, "age_seconds": None}
    return {
        "state": "up" if age <= STALE_HEARTBEAT_SECONDS else "stale",
        "temporal": bool(beat.get("temporal")),
        "age_seconds": round(age, 1),
    }


def service_status(settings, store) -> dict:
    now = time.time()
    problems: list[str] = []
    try:
        counts = store.health_counts()
        approvals = len(store.approvals())
        database = "ok"
    except Exception:
        counts, approvals, database = {}, None, "error"
        problems.append("数据库无法读取")
    worker = _worker(settings, now)
    if worker["state"] == "down":
        problems.append("执行进程没有运行")
    elif worker["state"] == "stale":
        problems.append("执行进程没有响应")
    elif worker["temporal"] is False:
        problems.append("执行引擎（Temporal）连不上")
    data_dir = sqlite_path(settings.database_url)
    data_dir = data_dir.parent if data_dir else Path(settings.workspace_root).expanduser()
    try:
        disk_free = shutil.disk_usage(data_dir).free
    except OSError:
        disk_free = None
    if disk_free is not None and disk_free < LOW_DISK_BYTES:
        problems.append("磁盘剩余空间不足 2 GB")
    backup = last_backup(default_backup_dir(settings))
    backup_age = None
    if backup is not None:
        backup_age = now - Path(backup["path"]).stat().st_mtime
    if settings.backup_interval_hours:
        overdue = settings.backup_interval_hours * 3600 * 2
        if backup is None or backup_age > overdue:
            problems.append("最近没有成功的备份")
    if counts.get("undispatched_goals", 0) or counts.get("unsent_commands", 0) > 20:
        if worker["state"] != "up":
            problems.append("有任务或指令在排队等待执行进程")
    return {
        "status": "ok" if not problems else "degraded",
        "version": __version__,
        "uptime_seconds": round(now - _STARTED),
        "database": database,
        "worker": worker,
        "tasks": counts.get("goals", {}),
        "undispatched_goals": counts.get("undispatched_goals"),
        "unsent_commands": counts.get("unsent_commands"),
        "events": counts.get("events"),
        "pending_approvals": approvals,
        "disk_free_bytes": disk_free,
        "last_backup": backup,
        "last_backup_age_seconds": round(backup_age) if backup_age is not None else None,
        "problems": problems,
    }


def prometheus(status: dict) -> str:
    lines = []

    def metric(name: str, kind: str, help_text: str, samples: list[tuple[dict, float]]):
        lines.append(f"# HELP rocky_{name} {help_text}")
        lines.append(f"# TYPE rocky_{name} {kind}")
        for labels, value in samples:
            label = ",".join(f'{key}="{value}"' for key, value in labels.items())
            lines.append(f"rocky_{name}{{{label}}} {value}" if label else f"rocky_{name} {value}")

    def gauge(name: str, help_text: str, value) -> None:
        if value is not None:
            metric(name, "gauge", help_text, [({}, float(value))])

    gauge("up", "1 when Rocky reports no problems", status["status"] == "ok")
    gauge("database_up", "1 when the database answers", status["database"] == "ok")
    gauge("worker_up", "1 while the worker heartbeat is fresh", status["worker"]["state"] == "up")
    if status["worker"]["temporal"] is not None:
        gauge("temporal_up", "1 when the worker reaches Temporal", status["worker"]["temporal"])
    gauge(
        "worker_heartbeat_age_seconds",
        "Age of the worker heartbeat",
        status["worker"]["age_seconds"],
    )
    metric(
        "tasks",
        "gauge",
        "Tasks by status",
        [({"status": name}, float(count)) for name, count in sorted(status["tasks"].items())],
    )
    gauge("undispatched_tasks", "Accepted tasks not yet started", status["undispatched_goals"])
    gauge("unsent_commands", "Owner commands not yet delivered", status["unsent_commands"])
    gauge("events", "Rows in the task event table", status["events"])
    gauge(
        "pending_approvals", "Approval requests waiting for the owner", status["pending_approvals"]
    )
    gauge("disk_free_bytes", "Free space on the data volume", status["disk_free_bytes"])
    gauge(
        "last_backup_age_seconds",
        "Seconds since the newest backup",
        status["last_backup_age_seconds"],
    )
    gauge("uptime_seconds", "Seconds since the API started", status["uptime_seconds"])
    return "\n".join(lines) + "\n"
