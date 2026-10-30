"""Consistent backups of Rocky's databases and task workspaces, and restore.

A backup is one private .tar.gz with a manifest. SQLite files are copied with
SQLite's online backup API, so a running service yields a consistent snapshot
(a plain file copy, as Time Machine makes, can catch a database mid-write).
Workspaces are archived as they are, without re-creatable environments such as
.venv and node_modules, or browser profiles. Credentials and the API token are
never included: model keys stay in the Keychain, and a restore keeps them.

Restore needs the service stopped. It checks every file against the manifest
before changing anything, and moves the current data aside rather than
deleting it.
"""

import fcntl
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from rocky import __version__

FORMAT = 1
PREFIX = "rocky-backup-"
SUFFIX = ".tar.gz"
# Re-creatable or private bulk that would make every backup huge.
_SKIPPED_DIRECTORIES = {".venv", "venv", "node_modules", "__pycache__", ".cache"}


class BackupError(RuntimeError):
    pass


def sqlite_path(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite:///"):
        return None
    value = database_url.removeprefix("sqlite:///")
    return None if value in {"", ":memory:"} else Path(value).expanduser()


def default_backup_dir(settings) -> Path:
    if settings.backup_dir is not None:
        return Path(settings.backup_dir).expanduser()
    database = sqlite_path(settings.database_url)
    return (database.parent if database else Path("./data")) / "backups"


def _sources(settings) -> dict[str, Path]:
    """Archive names of the SQLite databases to snapshot."""
    result = {}
    database = sqlite_path(settings.database_url)
    if database is None:
        raise BackupError("Backups support Rocky's SQLite database only")
    result["rocky.db"] = database
    if settings.temporal_db is not None:
        result["temporal.db"] = Path(settings.temporal_db).expanduser()
    return result


def _snapshot(source: Path, target: Path) -> None:
    # Read-only URI: a backup never creates or changes the live database.
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30) as live:
        with sqlite3.connect(target) as copy:
            live.backup(copy)
    os.chmod(target, 0o600)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1_048_576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _skipped(relative: Path) -> bool:
    parts = relative.parts
    if any(part in _SKIPPED_DIRECTORIES for part in parts):
        return True
    # .rocky/<goal>/browser holds browser profiles: caches, rebuilt on use.
    return len(parts) >= 3 and parts[0] == ".rocky" and parts[2] == "browser"


def _add_workspaces(archive: tarfile.TarFile, root: Path, spool: Path) -> int:
    """Archive the workspace tree; return how many files changed while being read."""
    skipped = 0
    archive.add(root, arcname="workspaces", recursive=False)
    for current, directories, names in os.walk(root):
        base = Path(current)
        relative_base = base.relative_to(root)
        directories[:] = [
            name for name in sorted(directories) if not _skipped(relative_base / name)
        ]
        for name in [*directories, *sorted(names)]:
            path = base / name
            relative = relative_base / name
            if _skipped(relative):
                continue
            try:
                info = archive.gettarinfo(path, arcname=f"workspaces/{relative.as_posix()}")
                if info is None or not (info.isfile() or info.isdir() or info.issym()):
                    continue
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if info.isfile():
                    # Copy first: a file that shrinks mid-read must not leave
                    # a short member that corrupts the rest of the archive.
                    with (
                        open(path, "rb") as handle,
                        tempfile.SpooledTemporaryFile(32 * 1_048_576, dir=spool) as copy,
                    ):
                        shutil.copyfileobj(handle, copy)
                        info.size = copy.tell()
                        copy.seek(0)
                        archive.addfile(info, copy)
                else:
                    archive.addfile(info)
            except OSError:
                # A task wrote or removed the file mid-read; the next backup gets it.
                skipped += 1
    return skipped


def create_backup(settings, directory: Path | None = None, *, keep: int | None = None) -> Path:
    """Write a new backup and return its path; prune to the newest `keep`."""
    directory = Path(directory or default_backup_dir(settings)).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    lock = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("Another backup is already running") from None
        return _create_backup(settings, directory, keep)
    finally:
        os.close(lock)


def _create_backup(settings, directory: Path, keep: int | None) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = directory / f"{PREFIX}{stamp}{SUFFIX}"
    counter = 1
    while target.exists():
        target = directory / f"{PREFIX}{stamp}-{counter}{SUFFIX}"
        counter += 1
    workspaces = Path(settings.workspace_root).expanduser()
    with tempfile.TemporaryDirectory(dir=directory, prefix=".staging-") as staging:
        staging_path = Path(staging)
        files = {}
        for name, source in _sources(settings).items():
            if not source.exists():
                continue
            copy = staging_path / name
            _snapshot(source, copy)
            files[name] = {"sha256": _digest(copy), "bytes": copy.stat().st_size}
        if "rocky.db" not in files:
            raise BackupError("Rocky's database does not exist yet; nothing to back up")
        manifest = {
            "format": FORMAT,
            "version": __version__,
            "created_at": datetime.now(UTC).isoformat(),
            "files": files,
            "workspaces": workspaces.is_dir(),
        }
        partial = directory / f".{target.name}.partial"
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                with tarfile.open(fileobj=stream, mode="w:gz", compresslevel=6) as archive:
                    data = json.dumps(manifest, indent=2).encode()
                    info = tarfile.TarInfo("manifest.json")
                    info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
                    archive.addfile(info, io.BytesIO(data))
                    for name in files:
                        archive.add(staging_path / name, arcname=name)
                    if workspaces.is_dir():
                        _add_workspaces(archive, workspaces, staging_path)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
    if keep:
        prune(directory, keep)
    return target


def list_backups(directory: Path) -> list[dict]:
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        return []
    result = []
    paths = directory.glob(f"{PREFIX}*{SUFFIX}")
    for path in sorted(paths, key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True):
        info = path.stat()
        result.append(
            {
                "name": path.name,
                "path": str(path),
                "bytes": info.st_size,
                "created_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
            }
        )
    return result


def prune(directory: Path, keep: int) -> list[Path]:
    removed = []
    for item in list_backups(directory)[keep:]:
        path = Path(item["path"])
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = []
    for member in archive.getmembers():
        name = Path(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise BackupError(f"Backup contains an unsafe path: {member.name}")
        if member.issym():
            target = Path(member.linkname)
            if target.is_absolute() or ".." in target.parts:
                # Workspace links that pointed outside are not restored.
                continue
        elif not (member.isfile() or member.isdir()):
            continue
        members.append(member)
    return members


def restore_backup(settings, backup: Path, *, run_dir: Path | None = None) -> Path:
    """Replace the databases and workspaces with a backup's; return where the old data went."""
    backup = Path(backup).expanduser()
    if not backup.is_file():
        raise BackupError(f"{backup} is not a backup file")
    if run_dir is not None and service_running(run_dir):
        raise BackupError("Stop the Rocky service before restoring a backup")
    sources = _sources(settings)
    workspaces = Path(settings.workspace_root).expanduser()
    database = sources["rocky.db"]
    with tempfile.TemporaryDirectory(dir=database.parent, prefix=".restore-") as staging:
        staging_path = Path(staging)
        with tarfile.open(backup, "r:gz") as archive:
            try:
                manifest = json.load(archive.extractfile("manifest.json"))
            except (KeyError, ValueError, AttributeError):
                raise BackupError("The file is not a Rocky backup (no manifest)") from None
            if manifest.get("format") != FORMAT:
                raise BackupError("This backup was made by an incompatible Rocky version")
            archive.extractall(staging_path, members=_safe_members(archive), filter="data")
        for name, expected in manifest.get("files", {}).items():
            path = staging_path / name
            if not path.is_file() or _digest(path) != expected.get("sha256"):
                raise BackupError(f"{name} in the backup is damaged; nothing was changed")
        aside = database.parent / f"pre-restore-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
        aside.mkdir(mode=0o700)
        targets = {name: sources.get(name) for name in manifest.get("files", {})}
        for name, target in targets.items():
            if target is None:
                continue
            for suffix in ("", "-wal", "-shm", "-journal"):
                current = Path(f"{target}{suffix}")
                if current.exists():
                    shutil.move(current, aside / f"{name}{suffix}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(staging_path / name, target)
        if manifest.get("workspaces") and (staging_path / "workspaces").is_dir():
            if workspaces.exists():
                shutil.move(workspaces, aside / "workspaces")
            shutil.move(staging_path / "workspaces", workspaces)
            os.chmod(workspaces, 0o700)
    return aside


def service_running(run_dir: Path) -> bool:
    """Whether a `rocky serve` process holds the service lock."""
    lock = Path(run_dir) / "service.lock"
    if not lock.exists():
        return False
    descriptor = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)
    return False


def last_backup(directory: Path) -> dict | None:
    items = list_backups(directory)
    return items[0] if items else None
