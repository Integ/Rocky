"""Bounded, metadata-only storage accounting; never follows task symlinks."""

import copy
import os
import stat
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from rocky.backup import default_backup_dir, sqlite_path

CATEGORIES = ("workspaces", "deliveries", "task_data", "databases", "backups", "logs")
CACHE_SECONDS = 60
MAX_ENTRIES = 100_000
MAX_SECONDS = 3
MAX_DEPTH = 64


class StorageInspector:
    def __init__(self, settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._cached = None
        self._cached_at = 0.0

    def summary(self, *, refresh=False):
        requested = time.monotonic()
        with self._lock:
            # Coalesce simultaneous refreshes into one scan as well as caching polls.
            if self._cached is not None and (
                self._cached_at >= requested
                or (not refresh and requested - self._cached_at < CACHE_SECONDS)
            ):
                return {**copy.deepcopy(self._cached), "cached": True}
            result = self._scan()
            self._cached, self._cached_at = result, time.monotonic()
            return {**copy.deepcopy(result), "cached": False}

    def _scan(self):
        start = time.monotonic()
        categories = {
            key: {"key": key, "bytes": 0, "logical_bytes": 0, "files": 0} for key in CATEGORIES
        }
        seen_files, seen_directories = set(), set()
        entries, unreadable, skipped_links, limited = 0, 0, 0, False

        def capacity():
            nonlocal limited
            if entries >= MAX_ENTRIES or time.monotonic() - start >= MAX_SECONDS:
                limited = True
                return False
            return True

        def account(info, category):
            identity = (info.st_dev, info.st_ino)
            if identity not in seen_files:
                seen_files.add(identity)
                row = categories[category]
                row["files"] += 1
                row["logical_bytes"] += info.st_size
                row["bytes"] += getattr(info, "st_blocks", (info.st_size + 511) // 512) * 512

        def workspace_category(relative):
            if relative and relative[0] == ".rocky":
                return (
                    "deliveries"
                    if len(relative) >= 3 and relative[2] == "deliveries"
                    else "task_data"
                )
            return "workspaces"

        def walk(descriptor, category, relative=(), depth=0):
            nonlocal entries, unreadable, skipped_links, limited
            if depth >= MAX_DEPTH:
                limited = True
                return
            info = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino)
            if identity in seen_directories:
                return
            seen_directories.add(identity)
            with os.scandir(descriptor) as children:
                for child in children:
                    if not capacity():
                        break
                    entries += 1
                    parts = (*relative, child.name)
                    try:
                        info = child.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            skipped_links += 1
                        elif stat.S_ISREG(info.st_mode):
                            account(
                                info,
                                workspace_category(parts)
                                if category == "workspace_tree"
                                else category,
                            )
                        elif stat.S_ISDIR(info.st_mode):
                            nested = os.open(
                                child.name,
                                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor,
                            )
                            try:
                                walk(nested, category, parts, depth + 1)
                            finally:
                                os.close(nested)
                    except FileNotFoundError:
                        # A moving tree is an estimate, not a consistent backup.
                        unreadable += 1
                    except OSError:
                        unreadable += 1

        # More specific roots run first; inode deduplication prevents overlap and
        # hard links from inflating the total. SQLite sidecars are included.
        databases = [sqlite_path(self.settings.database_url), self.settings.temporal_db]
        for database in databases:
            if database is None:
                continue
            for suffix in ("", "-wal", "-shm", "-journal"):
                if not capacity():
                    break
                entries += 1
                try:
                    info = Path(str(Path(database).expanduser()) + suffix).lstat()
                    if stat.S_ISREG(info.st_mode):
                        account(info, "databases")
                    elif stat.S_ISLNK(info.st_mode):
                        skipped_links += 1
                except FileNotFoundError:
                    pass
                except OSError:
                    unreadable += 1
        roots = [(default_backup_dir(self.settings), "backups")]
        if self.settings.log_dir is not None:
            roots.append((self.settings.log_dir, "logs"))
        roots.append((self.settings.workspace_root, "workspace_tree"))
        roots.sort(key=lambda item: len(Path(item[0]).expanduser().absolute().parts), reverse=True)
        for root, category in roots:
            if not capacity():
                break
            try:
                descriptor = os.open(
                    Path(root).expanduser(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
                try:
                    walk(descriptor, category)
                finally:
                    os.close(descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                unreadable += 1
        values = list(categories.values())
        return {
            "scanned_at": datetime.now(UTC).isoformat(),
            "total_bytes": sum(item["bytes"] for item in values),
            "logical_bytes": sum(item["logical_bytes"] for item in values),
            "categories": values,
            "complete": not limited and unreadable == 0,
            "limited": limited,
            "unreadable_entries": unreadable,
            "skipped_links": skipped_links,
            "scan_seconds": round(time.monotonic() - start, 3),
        }
