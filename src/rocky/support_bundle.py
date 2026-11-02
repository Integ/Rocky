"""An allowlisted support report, never raw logs with best-effort redaction."""

import io
import json
import math
import platform
import sys
import zipfile
from datetime import UTC, datetime

from rocky import __version__
from rocky.monitoring import service_status
from rocky.storage import CATEGORIES
from rocky.store import SCHEMA_VERSION

_STATUSES = (
    "queued",
    "running",
    "waiting",
    "blocked",
    "succeeded",
    "cancelled",
    "failed",
    "budget_exhausted",
    "deadline_exceeded",
)
_PROBLEMS = {
    "数据库无法读取": "database_unavailable",
    "执行进程没有运行": "worker_down",
    "执行进程没有响应": "worker_stale",
    "执行引擎（Temporal）连不上": "temporal_unreachable",
    "磁盘剩余空间不足 2 GB": "disk_low",
    "最近没有成功的备份": "backup_overdue",
    "有任务或指令在排队等待执行进程": "outbox_waiting",
}


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _enum(value, allowed):
    return value if value in allowed else "unknown"


def _storage(raw):
    # Keep this independent of the owner-facing API: future UI fields must not
    # silently expand what is shared in exported reports.
    return {
        **{
            key: _number(raw.get(key))
            for key in (
                "total_bytes",
                "logical_bytes",
                "unreadable_entries",
                "skipped_links",
                "scan_seconds",
            )
        },
        **{key: raw.get(key) is True for key in ("complete", "limited", "cached")},
        "categories": [
            {
                "key": item["key"],
                **{key: _number(item.get(key)) for key in ("bytes", "logical_bytes", "files")},
            }
            for item in raw.get("categories", [])
            if item.get("key") in CATEGORIES
        ],
    }


def _health(raw):
    worker = raw.get("worker") or {}
    backup = raw.get("last_backup") or {}
    return {
        "status": _enum(raw.get("status"), ("ok", "degraded")),
        "database": _enum(raw.get("database"), ("ok", "error")),
        "worker": {
            "state": _enum(worker.get("state"), ("up", "down", "stale")),
            "temporal": worker.get("temporal") if type(worker.get("temporal")) is bool else None,
            "age_seconds": _number(worker.get("age_seconds")),
        },
        "tasks": {key: _number((raw.get("tasks") or {}).get(key)) for key in _STATUSES},
        **{
            key: _number(raw.get(key))
            for key in (
                "uptime_seconds",
                "undispatched_goals",
                "unsent_commands",
                "events",
                "pending_approvals",
                "disk_free_bytes",
                "last_backup_age_seconds",
            )
        },
        "last_backup_bytes": _number(backup.get("bytes")),
        "problem_codes": [
            _PROBLEMS[value] for value in raw.get("problems", []) if value in _PROBLEMS
        ],
    }


def support_bundle(settings, store, queue, storage) -> bytes:
    """Export fixed enums and aggregates without copying raw logs or task content."""
    unavailable = []
    try:
        health = _health(service_status(settings, store))
    except Exception:
        health = None
        unavailable.append("health")
    try:
        raw = queue.summary()
        admission = {
            key: _number(raw.get(key))
            for key in (
                "capacity",
                "reserved_interactive",
                "running",
                "waiting",
                "interactive_waiting",
            )
        }
    except Exception:
        admission = None
        unavailable.append("model_queue")
    try:
        usage = _storage(storage.summary())
    except Exception:
        usage = None
        unavailable.append("storage")
    system = _enum(platform.system(), ("Darwin", "Linux", "Windows"))
    report = {
        "format": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "environment": {
            "system": system,
            "machine": _enum(platform.machine(), ("arm64", "aarch64", "x86_64", "AMD64")),
            "python": ".".join(map(str, sys.version_info[:3])),
        },
        "configuration": {
            "tool_access": _enum(settings.tool_access, ("full", "scoped")),
            "approval_mode": _enum(settings.approval_mode, ("required", "trusted")),
            "auth_mode": _enum(settings.auth_mode, ("local", "token")),
            **{
                key: _number(getattr(settings, key))
                for key in (
                    "backup_interval_hours",
                    "backup_keep",
                    "log_max_bytes",
                    "agenda_concurrency",
                    "max_concurrent_activities",
                    "max_concurrent_model_calls",
                    "model_interactive_reserve",
                )
            },
        },
        "health": health,
        "model_queue": admission,
        "storage": usage,
        "unavailable_components": unavailable,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in {
            "report.json": json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
            "README.txt": "Rocky 本机脱敏诊断包\n\nreport.json 包含应用与 Python 版本、系统类型、运行状态、任务数量、模型排队数量、空间统计和非敏感运行选项。\n不包含任务内容、交付文件、文件名或路径、原始日志、数据库副本、模型地址和配置、API 令牌、密钥、Cookie 或环境变量。\n空间占用为估算，可能使用最近 60 秒的缓存；受限或无法读取时会明确标记。unavailable_components 表示本次未能收集的项目，不包含原始异常。\n\n这不是备份，不能用于恢复。文件仅保存到你选择的位置，Rocky 不会自动上传；请自行决定是否分享。\n",
        }.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, content)
    return output.getvalue()
