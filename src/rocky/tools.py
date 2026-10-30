"""Local execution tools with durable receipts and optional legacy workspace scope.

The workspace and receipt volume must be shared by workers on the same host.
Advisory locks coordinate retries; descriptor-based storage protects receipts
and artifact exports independently of the selected execution access policy.
"""

import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

from rocky.approval_policy import (
    ApprovalPolicy,
    ApprovalSettings,
    automatic_approval,
    http_scope,
)
from rocky.command_policy import command_class, command_words
from rocky.config import Settings
from rocky.http_tools import HTTPToolError, request_http
from rocky.models import (
    DEFAULT_COMMAND_SECONDS,
    MAX_COMMAND_SECONDS,
    Artifact,
    RunInput,
    ToolInput,
    ToolResult,
    VerificationResult,
    read_only_action,
)
from rocky.network import FetchError, fetch_public_text
from rocky.seatbelt import SandboxUnavailable, prepare_sandbox, worker_free_environment
from rocky.tool_contracts import (
    ActionValidationError,
    action_identity,
    approval_fingerprint,
    parse_edits,
    parse_read_range,
    requires_approval,
    validate_action,
)

MAX_BYTES = 1_000_000
MAX_CONTENT = 16000
# Full-access edits load the file into memory; larger files need run_command.
MAX_EDIT_BYTES = 20_000_000
# Interrupted non-idempotent effects must be inspected, never silently repeated.
_DISPATCH_MARKED = {"http_request", "run_command", "append_file", "edit_file"}
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_FILE_WRITE_TOOLS = frozenset({"write_file", "append_file", "edit_file"})
# Credential stores below the owner's home. Full-access file tools need exact
# owner approval to read them, and Seatbelt commands cannot read them at all:
# a command that needs one (git over SSH, gh, aws) must request sandbox=false.
SENSITIVE_HOME_PATHS = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".gem/credentials",
    ".docker/config.json",
    ".config/gh",
    ".config/gcloud",
    "Library/Keychains",
    "Library/Cookies",
    "Library/Safari",
    "Library/Mail",
    "Library/Messages",
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft Edge",
    "Library/Application Support/Arc",
    "Library/Application Support/Firefox",
)


class ToolError(ValueError):
    """Safe failure text that may be returned to the planner."""


def _failure(message: str, *, code: str = "tool_error", hint: str = "") -> ToolResult:
    return ToolResult(success=False, content=_clip(message), error_code=code, retry_hint=hint)


def _path_failure(exc: OSError) -> ToolResult:
    if isinstance(exc, FileNotFoundError):
        return _failure(
            "The requested path or command working directory does not exist.",
            code="path_not_found",
            hint="Use list_files to inspect the parent directory, then correct path or create it.",
        )
    if isinstance(exc, PermissionError):
        return _failure(
            "The operating system denied access to the requested path or process.",
            code="permission_denied",
            hint="Choose an accessible path or ask the owner to resolve the OS permission.",
        )
    return _failure(
        "Workspace path is missing, unsafe or inaccessible.",
        code="path_unavailable",
        hint="Inspect the parent directory and correct the path or file type before retrying.",
    )


def _clip(value: str) -> str:
    suffix = "\n[truncated]"
    return value if len(value) <= MAX_CONTENT else value[: MAX_CONTENT - len(suffix)] + suffix


@lru_cache(maxsize=256)
def _path_key(text: str) -> str:
    """Compare paths the way the macOS filesystem does: case and Unicode insensitive."""
    return unicodedata.normalize("NFD", text).casefold() if sys.platform == "darwin" else text


def _inside(path: Path, parent: Path) -> bool:
    target, base = _path_key(str(path)), _path_key(str(parent))
    return target == base or target.startswith(base.rstrip("/") + "/")


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# Dependency, VCS and cache trees are not task output and can be very large.
_UNTRACKED_DIRECTORIES = frozenset(
    {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox"}
    | {".mypy_cache", ".pytest_cache", ".ruff_cache", ".rocky-sandbox"}
)
_SNAPSHOT_LIMIT = 5000
# Two snapshots and a 30-second command must fit the 45-second Activity attempt,
# even when the working directory is a large tree outside the workspace.
_SNAPSHOT_SECONDS = 5.0


def _snapshot(
    directory: Path, allowed: Callable[[Path], bool]
) -> tuple[dict[str, tuple[int, int, int]], bool]:
    """Stat regular files below directory, in a stable order; report completeness."""
    entries: dict[str, tuple[int, int, int]] = {}
    deadline = time.monotonic() + _SNAPSHOT_SECONDS
    for current, folders, files in os.walk(directory):
        if time.monotonic() > deadline:
            return entries, False
        folders[:] = sorted(
            name
            for name in folders
            if name not in _UNTRACKED_DIRECTORIES and allowed(Path(current, name))
        )
        for name in sorted(files):
            path = os.path.join(current, name)
            if not allowed(Path(path)):
                continue
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                entries[path] = (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                if len(entries) >= _SNAPSHOT_LIMIT:
                    return entries, False
    return entries, True


def _changed(before: dict, after: dict) -> list[str]:
    return [path for path, signature in after.items() if before.get(path) != signature]


# Background jobs per task, and how long one may run.
MAX_JOBS = 4
MAX_JOB_SECONDS = 6 * 3600


def _kill_group(pgid: int, signum: int = signal.SIGKILL) -> None:
    if pgid <= 1:
        # 0 and 1 would signal Rocky's own group or every process.
        return
    try:
        os.killpg(pgid, signum)
    except OSError:
        pass


def _operation_fingerprint(request: ToolInput) -> str:
    # Keep the original v0.1 ToolInput serialization byte-for-byte. Inference
    # settings do not change tool semantics; including them would invalidate
    # durable receipts when old Activity inputs are decoded by newer workers.
    exclude = {"run": {"contract": {"limits"}}}
    # Adding a default must not invalidate receipts written before the field.
    unset = {
        name for name in ("timeout_seconds", "background") if getattr(request.action, name) is None
    }
    if request.action.sandbox:
        unset.add("sandbox")
    if unset:
        exclude["action"] = unset
    payload = request.model_dump_json(
        include={"run": {"goal_id", "contract"}, "operation_id": True, "action": True},
        exclude=exclude,
    )
    return _digest(payload.encode())


def _parts(path: str | None, *, allow_root: bool = False) -> list[str]:
    if allow_root and path in {None, "", "."}:
        return []
    if (
        not path
        or len(path) > 240
        or path.startswith("/")
        or any(c in path for c in "\\\x00:")
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
    ):
        raise ToolError("Use a relative workspace path of at most 240 characters")
    parts = path.split("/")
    if any(not p or p.startswith(".") for p in parts):
        raise ToolError("Traversal, empty path segments and hidden files are forbidden")
    return parts


def _open_directory(parent: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)


@contextmanager
def _parent_directory(goal_fd: int, parts: list[str], *, create: bool) -> Iterator[int]:
    with ExitStack() as stack:
        current = goal_fd
        for part in parts[:-1]:
            current = _open_directory(current, part, create=create)
            stack.callback(os.close, current)
        yield current


def _read_regular(parent: int, name: str, *, max_bytes: int | None = MAX_BYTES) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ToolError("Only regular files without hard links can be read")
        if max_bytes is not None and info.st_size > max_bytes:
            raise ToolError("File exceeds the maximum size")
        content = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
        if max_bytes is not None and len(content) > max_bytes:
            raise ToolError("File exceeds the maximum size")
        return content


def _read_lines(handle, bounds: tuple[int, int], *, errors: str = "strict") -> str:
    start, end = bounds
    selected = []
    scanned = 0
    last = 0
    for number in range(1, end + 1):
        line = handle.readline(MAX_EDIT_BYTES - scanned + 1)
        scanned += len(line)
        if scanned > MAX_EDIT_BYTES:
            raise ToolError(
                "Line range exceeds the bounded scan size; use a targeted search command."
            )
        if not line:
            break
        if number >= start:
            selected.append(line)
            last = number
    if not selected:
        raise ToolError("start_line is beyond the end of the file; choose an earlier range.")
    try:
        text = b"".join(selected).decode("utf-8", errors=errors)
    except UnicodeDecodeError:
        raise ToolError("The selected lines are not UTF-8 text") from None
    return _clip(f"[Lines {start}-{last}; other lines are not included]\n" + text)


def _apply_edits(text: str, edits: list[tuple[str, str, bool]]) -> str:
    """Apply exact replacements in order; any failed edit leaves the file unchanged."""
    for number, (old, new, replace_all) in enumerate(edits, 1):
        count = text.count(old)
        if count == 0:
            raise ToolError(
                f"Edit {number}: old_text was not found. Read the file again and copy the exact current text, including whitespace."
            )
        if count > 1 and not replace_all:
            raise ToolError(
                f"Edit {number}: old_text occurs {count} times. Include more surrounding text to make it unique, or set replace_all to true."
            )
        text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    return text


def _atomic_write(parent: int, name: str, content: bytes, *, preserve_mode: bool = False) -> None:
    mode = 0o600
    try:
        existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
            raise ToolError("The destination must be a regular file without links")
        if preserve_mode:
            mode = stat.S_IMODE(existing.st_mode)
    except FileNotFoundError:
        pass
    temporary = ".rocky-tmp-" + uuid.uuid4().hex
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


class WorkspaceTools:
    def __init__(self, settings: Settings):
        self.full_access = settings.tool_access == "full"
        self.approval_mode = getattr(settings, "approval_mode", "required")
        prefixes = getattr(settings, "auto_approved_prefixes", None)
        self.auto_approved_prefixes = prefixes() if callable(prefixes) else []
        self.approval_settings = ApprovalSettings(
            read_only_commands=getattr(settings, "auto_approve_read_only", True),
            timeout_minutes=getattr(settings, "approval_timeout_minutes", 30),
        )
        # The configured base holds default working directories and private receipts.
        self.root = settings.workspace_root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The control plane cannot be used to approve a task's own opt-out.
        temporal_address = settings.temporal_address.removeprefix("http://").removeprefix(
            "https://"
        )
        temporal = urlsplit("//" + temporal_address)
        self.control_ports = tuple(sorted({settings.port, temporal.port or 7233}))
        protected = [settings.credential_path.expanduser().resolve()]
        database = make_url(settings.database_url)
        if database.get_backend_name() == "sqlite" and database.database not in {
            None,
            "",
            ":memory:",
        }:
            filename = Path(database.database).expanduser().resolve()
            protected.extend(
                Path(str(filename) + suffix) for suffix in ("", "-wal", "-shm", "-journal")
            )
            # `rocky serve` keeps its token and status next to the database.
            protected += [filename.parent / "api-token", filename.parent / "run"]
        for name in ("api_token_file", "temporal_db", "run_dir", "log_dir"):
            if (path := getattr(settings, name, None)) is not None:
                path = Path(path).expanduser().resolve()
                protected.append(path)
                if name == "temporal_db":
                    protected += [
                        Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal")
                    ]
        self.protected_paths = tuple(dict.fromkeys(protected))
        home = Path.home()
        # Background jobs started by this worker, by process group.
        self._processes: dict[int, subprocess.Popen] = {}
        self.sensitive_paths = tuple(
            dict.fromkeys((home / path).resolve() for path in SENSITIVE_HOME_PATHS)
        )
        self.protected_write_paths = tuple(
            {
                Path(__file__).resolve().parent,
                Path(sys.prefix).resolve(),
                Path(sys.base_prefix).resolve(),
            }
        )

    def _jobs_dir(self, goal_id: str) -> Path:
        # Kept in the private control directory: a task can read its job logs
        # but cannot edit the process IDs Rocky later signals.
        return self.root / ".rocky" / goal_id / "jobs"

    def _job_records(self, goal_id: str) -> list[dict]:
        directory = self._jobs_dir(goal_id)
        records = []
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and isinstance(record.get("pgid"), int):
                records.append(record)
        return records

    def _job_running(self, record: dict) -> bool:
        process = self._processes.get(record["pgid"])
        if process is not None:
            # Reap jobs this worker started, so they do not linger as zombies.
            if process.poll() is None:
                return True
            del self._processes[record["pgid"]]
            return False
        try:
            # The wrapper shell leads the group until the command ends.
            return os.getpgid(record["pgid"]) == record["pgid"]
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def stop_jobs(self, goal_id: str, *, older_than: float | None = None) -> int:
        """Stop a task's background jobs; returns how many were running."""
        if not _IDENTIFIER.fullmatch(goal_id):
            raise ToolError("Invalid goal identifier")
        stopped = 0
        now = time.time()
        for record in self._job_records(goal_id):
            if older_than is not None and now - record.get("started", now) < older_than:
                continue
            if self._job_running(record):
                _kill_group(record["pgid"], signal.SIGTERM)
                stopped += 1
        if stopped:
            time.sleep(0.5)
            for record in self._job_records(goal_id):
                if older_than is None or now - record.get("started", now) >= older_than:
                    _kill_group(record["pgid"])
        return stopped

    def _start_job(
        self, request: ToolInput, directory: Path, prefix: list[str], environment: dict
    ) -> ToolResult:
        goal_id = request.run.goal_id
        self.stop_jobs(goal_id, older_than=MAX_JOB_SECONDS)
        running = [record for record in self._job_records(goal_id) if self._job_running(record)]
        if len(running) >= MAX_JOBS:
            raise ToolError(
                f"This task already has {MAX_JOBS} background jobs running; wait for one to finish "
                "(its .exit file appears) before starting another."
            )
        job_id = _digest(request.operation_id.encode())[:12]
        output = self.root / goal_id / ".rocky-jobs"
        output.mkdir(mode=0o700, exist_ok=True)
        log_path, exit_path = output / f"{job_id}.log", output / f"{job_id}.exit"
        exit_path.unlink(missing_ok=True)
        # The wrapper records the exit status where the task can read it.
        # A subshell, so `exit` in the command still reaches the status line.
        script = '(eval "$1"); status=$?; printf "%s\\n" "$status" > "$2"'
        argv = [*prefix, script, "rocky-job", request.action.content, str(exit_path)]
        with open(log_path, "wb") as log:
            process = subprocess.Popen(
                argv,
                cwd=directory,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        self._processes[process.pid] = process
        jobs = self._jobs_dir(goal_id)
        jobs.mkdir(parents=True, mode=0o700, exist_ok=True)
        (jobs / f"{job_id}.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "pgid": process.pid,
                    "command": request.action.content[:2000],
                    "started": time.time(),
                }
            )
        )
        log_name = self.display_path(goal_id, log_path)
        exit_name = self.display_path(goal_id, exit_path)
        return ToolResult(
            success=True,
            content=(
                f"Started background job {job_id}. Output goes to {log_name}; when the command "
                f"ends its exit status is written to {exit_name}. Read those files to follow it. "
                f"Jobs are stopped when the task ends or after {MAX_JOB_SECONDS // 3600} hours."
            ),
            progress_key=f"job:{job_id}",
        )

    def delete_goal_files(self, goal_id: str) -> None:
        """Remove a deleted goal's workspace, receipts and cookie sessions."""
        if not _IDENTIFIER.fullmatch(goal_id):
            raise ToolError("Invalid goal identifier")
        self.stop_jobs(goal_id)
        for path in (self.root / goal_id, self.root / ".rocky" / goal_id):
            # Never follow a link: remove the link itself, not what it points to.
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)

    def read_artifact(self, goal_id: str, path: str) -> str:
        """Read a declared UI artifact inside its goal's export directory."""
        return self.read_artifact_bytes(goal_id, path).decode("utf-8")

    def read_artifact_bytes(self, goal_id: str, path: str) -> bytes:
        parts = _parts(path)
        with self._goal(goal_id, create=False) as (goal_fd, _):
            with _parent_directory(goal_fd, parts, create=False) as parent:
                return _read_regular(
                    parent, parts[-1], max_bytes=None if self.full_access else MAX_BYTES
                )

    @contextmanager
    def _goal(
        self, goal_id: str, *, create: bool = True, shared: bool = False
    ) -> Iterator[tuple[int, int]]:
        """Open a goal under its operation lock.

        Read-only operations share the lock so a parallel batch actually runs
        concurrently; each writes only its own receipt atomically. Mutations
        keep the exclusive lock and therefore never overlap any other operation.
        """
        if not _IDENTIFIER.fullmatch(goal_id):
            raise ToolError("Invalid goal identifier")
        with ExitStack() as stack:
            root_fd = os.open(self.root, _DIRECTORY_FLAGS)
            stack.callback(os.close, root_fd)
            goal_fd = _open_directory(root_fd, goal_id, create=create)
            stack.callback(os.close, goal_fd)
            control_fd = _open_directory(root_fd, ".rocky", create=True)
            stack.callback(os.close, control_fd)
            receipt_fd = _open_directory(control_fd, goal_id, create=True)
            stack.callback(os.close, receipt_fd)
            lock_fd = os.open(
                "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=receipt_fd
            )
            stack.callback(os.close, lock_fd)
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise ToolError("Invalid operation lock")
            fcntl.flock(lock_fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            yield goal_fd, receipt_fd

    def default_approval_policy(self) -> ApprovalPolicy:
        return ApprovalPolicy(
            settings=self.approval_settings.model_copy(),
            allowlist=[list(prefix) for prefix in self.auto_approved_prefixes],
        )

    def execute(self, request: ToolInput, policy: ApprovalPolicy | None = None) -> ToolResult:
        try:
            if not request.operation_id or len(request.operation_id) > 256:
                raise ToolError("Invalid operation identifier")
            if not self.full_access and (
                request.action.tool not in request.run.contract.allowed_tools
                or request.action.tool in {"http_request", "run_command"}
            ):
                raise ToolError("The tool is not authorized by this goal")
            fingerprint = _operation_fingerprint(request)
            receipt_name = _digest(request.operation_id.encode()) + ".json"
            with self._goal(request.run.goal_id, shared=read_only_action(request.action)) as (
                goal_fd,
                receipts_fd,
            ):
                try:
                    stored = json.loads(_read_regular(receipts_fd, receipt_name, max_bytes=200000))
                except FileNotFoundError:
                    stored = None
                except (ValueError, TypeError) as exc:
                    raise ToolError(
                        "The stored operation receipt requires operator repair"
                    ) from exc
                if stored is not None:
                    if stored.get("fingerprint") != fingerprint:
                        raise ToolError(
                            "An operation identifier cannot be reused for another request"
                        )
                    if stored.get("status") == "started":
                        return _failure(
                            "The previous attempt may have taken effect before it was interrupted. Inspect the external state before issuing a new operation; this operation will not be repeated automatically.",
                            code="outcome_unknown",
                            hint="Inspect actual external state with a read operation. Do not assume failure or resubmit a mutation until its outcome is known.",
                        )
                    return ToolResult.model_validate(stored["result"])
                # Validation and approval happen after receipt lookup (completed
                # legacy calls remain replayable), but before any dispatch marker.
                # A rejected request can be corrected safely without an unknown
                # outcome or a permanently cached denial.
                options = validate_action(request.action, full_access=self.full_access)
                unsandboxed = request.action.tool == "run_command" and not request.action.sandbox
                file_access = self._file_access(request)
                # Like an unsandboxed command, these reach the owner's files
                # outside the task, so only an exact approval lets them run.
                exact = unsandboxed or (bool(file_access) and self.approval_mode != "trusted")
                approved_by = ""
                if exact or (
                    self.approval_mode != "trusted" and requires_approval(request.action, options)
                ):
                    policy = policy or self.default_approval_policy()
                    approval = approval_fingerprint(request)
                    identity = action_identity(request.run.goal_id, request.action)
                    if approval in request.approved_action_fingerprints:
                        approved_by = "owner"
                    elif identity in request.preapproved_actions:
                        approved_by = "deferred:" + identity
                    elif not exact:
                        approved_by = automatic_approval(request, options, policy)
                        if approved_by == "read_only" and not self._system_program(
                            request.action.content
                        ):
                            approved_by = ""
                    if not approved_by:
                        return ToolResult(
                            success=False,
                            content=(
                                "Owner approval is required to run this command outside Seatbelt with the current user's privileges. The operation has not been dispatched."
                                if unsandboxed
                                else f"Owner approval is required to {file_access}. The operation has not been dispatched."
                                if exact
                                else "Owner approval is required before running this shell command or potentially mutating HTTP request. The operation has not been dispatched."
                            ),
                            error_code="approval_required",
                            retry_hint="Request owner approval for this exact action; keep its operation identifier and arguments unchanged after approval.",
                            approval_required=approval,
                            approval_command_prefix=(
                                command_class(request.action.content) or []
                                if request.action.tool == "run_command" and not unsandboxed
                                else []
                            ),
                            approval_http_scope=None if exact else http_scope(request, options),
                            approval_timeout_seconds=policy.settings.timeout_minutes * 60,
                        )
                elif file_access or requires_approval(request.action, options):
                    approved_by = "trusted"
                if request.action.tool in _DISPATCH_MARKED or (
                    self.full_access and request.action.tool == "fetch_url"
                ):
                    # External side effects cannot be committed atomically with a
                    # receipt. Mark dispatch durably so a retry after a crash
                    # cannot silently submit the same POST or command twice.
                    _atomic_write(
                        receipts_fd,
                        receipt_name,
                        json.dumps({"fingerprint": fingerprint, "status": "started"}).encode(),
                    )
                try:
                    result = self._execute(request, goal_fd)
                except SandboxUnavailable as exc:
                    result = _failure(
                        str(exc),
                        code="sandbox_unavailable",
                        hint="Run the worker on macOS with Seatbelt available, or request exact owner approval for run_command with sandbox=false.",
                    )
                except HTTPToolError as exc:
                    uncertain = requires_approval(request.action, options)
                    result = _failure(
                        str(exc),
                        code="outcome_unknown" if uncertain else "http_request_failed",
                        hint=(
                            "The request may have reached the server. Inspect actual external state with a read operation before issuing another mutation."
                            if uncertain
                            else "Check the destination, connection and HTTP options before making a new read request."
                        ),
                    )
                except (ToolError, FetchError) as exc:
                    result = _failure(
                        str(exc),
                        hint="Inspect the reported constraint, correct the action parameters and choose a new operation identifier.",
                    )
                except OSError as exc:
                    result = _path_failure(exc)
                result.approved_by = approved_by
                _atomic_write(
                    receipts_fd,
                    receipt_name,
                    json.dumps(
                        {"fingerprint": fingerprint, "result": result.model_dump()}
                    ).encode(),
                )
                return result
        except ActionValidationError as exc:
            return _failure(
                str(exc),
                code="invalid_arguments",
                hint="Correct the action using this error before retrying; no operation was dispatched.",
            )
        except (ToolError, FetchError) as exc:
            return _failure(str(exc), hint="Correct the reported constraint before retrying.")
        except (OSError, ValueError, KeyError, TypeError):
            return _failure(
                "Operation storage is unavailable or invalid",
                code="operation_storage_unavailable",
                hint="Ask the owner to inspect the workspace receipt storage; do not repeat an external effect with a different identifier.",
            )

    def _file_access(self, request: ToolInput) -> str:
        """Describe a full-access file operation that needs the owner, or return ''.

        Writes outside the task directory could change startup files, other
        projects or Rocky itself; credential stores could be read and sent on
        with an unreviewed GET. Neither runs inside Seatbelt.
        """
        action = request.action
        if not self.full_access or not action.path:
            return ""
        goal_id = request.run.goal_id
        if action.tool in _FILE_WRITE_TOOLS or action.tool in {"fetch_url", "http_request"}:
            target = self._resolve_path(goal_id, action.path, write=True)
            if not _inside(target, self.root / goal_id):
                return f"write {target} outside this task's directory"
        elif action.tool in {"read_file", "list_files"}:
            target = self._resolve_path(goal_id, action.path)
            if any(_inside(target, path) for path in self.sensitive_paths):
                return f"read the credential store {target}"
        return ""

    def _system_program(self, command: str | None) -> bool:
        """The read-only program resolves outside every task workspace.

        A PATH entry such as "." could otherwise run a file the task wrote
        under the name of a catalog program.
        """
        words = command_words(command)
        if not words:
            return False
        # Relative entries resolve against the command's working directory.
        entries = os.environ.get("PATH", "").split(os.pathsep)
        if any(
            not entry or not os.path.isabs(entry) or Path(entry).resolve().is_relative_to(self.root)
            for entry in entries
        ):
            return False
        found = shutil.which(words[0])
        return bool(found) and not Path(found).resolve().is_relative_to(self.root)

    def _execute(self, request: ToolInput, goal_fd: int) -> ToolResult:
        if self.full_access:
            return self._execute_full(request)
        action = request.action
        if action.tool == "fetch_url":
            if not action.url:
                raise ToolError("fetch_url requires a URL")
            fetched = fetch_public_text(action.url, request.run.contract.allowed_domains)
            return ToolResult(
                success=True,
                content=_clip(fetched.text),
                progress_key="fetch:" + fetched.sha256,
            )
        parts = _parts(action.path, allow_root=action.tool == "list_files")
        if action.tool == "list_files":
            with ExitStack() as stack:
                current = goal_fd
                for part in parts:
                    current = _open_directory(current, part, create=False)
                    stack.callback(os.close, current)
                entries = self._list(current, "/".join(parts))
            text = "\n".join(entries) or "(empty workspace)"
            return ToolResult(
                success=True, content=_clip(text), progress_key="list:" + _digest(text.encode())
            )
        create = action.tool in {"write_file", "append_file"}
        with _parent_directory(goal_fd, parts, create=create) as parent:
            if action.tool == "read_file":
                content = _read_regular(parent, parts[-1])
                if bounds := parse_read_range(action.content):
                    text = _read_lines(io.BytesIO(content), bounds)
                    return ToolResult(
                        success=True, content=text, progress_key="read:" + _digest(text.encode())
                    )
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ToolError("The file is not UTF-8 text") from exc
                return ToolResult(
                    success=True, content=_clip(text), progress_key="read:" + _digest(content)
                )
            if action.content is None:
                raise ToolError(f"{action.tool} requires text content")
            if action.tool == "write_file":
                content = action.content.encode("utf-8")
                summary = "Wrote {path} ({size} bytes)"
            else:
                try:
                    existing = _read_regular(parent, parts[-1]).decode("utf-8")
                except FileNotFoundError:
                    if action.tool == "edit_file":
                        raise ToolError(
                            "edit_file requires an existing file; use write_file to create it"
                        ) from None
                    existing = ""
                except UnicodeDecodeError as exc:
                    raise ToolError("The file is not UTF-8 text") from exc
                if action.tool == "append_file":
                    content = (existing + action.content).encode("utf-8")
                    summary = "Appended to {path} ({size} bytes total)"
                else:
                    edits = parse_edits(action.content)
                    content = _apply_edits(existing, edits).encode("utf-8")
                    summary = f"Applied {len(edits)} edit(s) to {{path}} ({{size}} bytes)"
            if len(content) > MAX_BYTES:
                raise ToolError("File exceeds the maximum size")
            _atomic_write(parent, parts[-1], content)
            artifact = Artifact(path="/".join(parts), size=len(content), sha256=_digest(content))
            return ToolResult(
                success=True,
                content=summary.format(path=artifact.path, size=artifact.size),
                progress_key=f"{action.tool.split('_')[0]}:{artifact.path}:{artifact.sha256}",
                artifacts=[artifact],
                changed_paths=[artifact.path],
            )

    def _path(self, request: ToolInput, path: str | None, *, default_root: bool = False) -> Path:
        writes = request.action.tool in {
            "write_file",
            "append_file",
            "edit_file",
            "fetch_url",
            "http_request",
        }
        return self._resolve_path(
            request.run.goal_id, path, default_root=default_root, write=writes
        )

    def _resolve_path(
        self, goal_id: str, path: str | None, *, default_root: bool = False, write: bool = False
    ) -> Path:
        if not path and not default_root:
            raise ToolError("This tool requires a path")
        value = Path(path or ".").expanduser()
        if not value.is_absolute():
            value = self.root / goal_id / value
        try:
            self._check_path(goal_id, Path(os.path.abspath(value)), write=write)
            resolved = value.resolve()
            self._check_path(goal_id, resolved, write=write)
            return resolved
        except RuntimeError:
            raise ToolError("The path contains a symbolic link loop") from None

    def _check_path(self, goal_id: str, path: Path, *, write: bool = False) -> None:
        """Full access never includes another goal or the private control tree."""

        def inside(parent: Path) -> bool:
            return _inside(path, parent)

        if inside(self.root) and not inside(self.root / goal_id):
            raise ToolError(
                "Access to other task directories and private .rocky storage is forbidden"
            )
        if any(inside(protected) for protected in self.protected_paths):
            raise ToolError("Access to Rocky's private control files is forbidden")
        if write and any(inside(protected) for protected in self.protected_write_paths):
            raise ToolError(
                "File tools cannot modify Rocky's code or Python runtime; request approved unsandboxed execution"
            )

    def _visible_path(self, goal_id: str, path: Path, *, parent_resolved: bool = False) -> bool:
        """parent_resolved: path is an entry of a real directory below a resolved root."""
        try:
            self._check_path(goal_id, path)
            # Walkers never descend symlinked directories, so only the entry itself
            # can redirect elsewhere; one lstat then replaces a full realpath.
            if not parent_resolved or path.is_symlink():
                self._check_path(goal_id, path.resolve())
            return True
        except (OSError, RuntimeError, ToolError):
            return False

    def _check_parent(self, goal_id: str, descriptor: int, name: str) -> None:
        # Inode comparison also catches case/Unicode aliases of the workspace
        # root on macOS. Never follow a newly swapped parent symlink.
        root = self.root.stat()
        parent = os.fstat(descriptor)
        if (root.st_dev, root.st_ino) == (parent.st_dev, parent.st_ino) and name != goal_id:
            raise ToolError(
                "Access to other task directories and private .rocky storage is forbidden"
            )

    @contextmanager
    def _full_parent(self, goal_id: str, path: Path, *, create: bool = False) -> Iterator[int]:
        with ExitStack() as stack:
            current = os.open(path.anchor, _DIRECTORY_FLAGS)
            stack.callback(os.close, current)
            for part in path.parts[1:-1]:
                self._check_parent(goal_id, current, part)
                current = _open_directory(current, part, create=create)
                stack.callback(os.close, current)
            self._check_parent(goal_id, current, path.name)
            yield current

    @contextmanager
    def _full_file(self, goal_id: str, path: Path, *, append: bool = False) -> Iterator:
        with self._full_parent(goal_id, path, create=append) as parent:
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT if append else os.O_RDONLY
            descriptor = os.open(
                path.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent
            )
            with os.fdopen(descriptor, "ab" if append else "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ToolError("Only regular files without hard links can be accessed")
                yield handle

    def display_path(self, goal_id: str, path: str | Path) -> str:
        """Goal-relative inside the workspace, otherwise absolute, for change records."""
        value = Path(path).expanduser()
        if not self.full_access:
            return "/".join(_parts(str(path)))
        if not value.is_absolute():
            value = self.root / goal_id / value
        try:
            value = value.resolve()
        except (OSError, RuntimeError):
            pass
        try:
            return value.relative_to(self.root / goal_id).as_posix()
        except ValueError:
            return str(value)

    def _artifact(self, request: ToolInput, path: Path) -> list[Artifact]:
        try:
            relative = path.relative_to(self.root / request.run.goal_id)
        except ValueError:
            return []
        if any(part.startswith(".") for part in relative.parts):
            return []
        digest = hashlib.sha256()
        with self._full_file(request.run.goal_id, path) as handle:
            size = os.fstat(handle.fileno()).st_size
            for chunk in iter(lambda: handle.read(1_048_576), b""):
                digest.update(chunk)
        return [Artifact(path=relative.as_posix(), size=size, sha256=digest.hexdigest())]

    def _execute_full(self, request: ToolInput) -> ToolResult:
        action = request.action
        if action.tool in {"fetch_url", "http_request"}:
            if not action.url:
                raise ToolError(f"{action.tool} requires a URL")
            try:
                options = json.loads(action.content) if action.content else {}
            except (ValueError, TypeError):
                raise ToolError(
                    "HTTP content must be a JSON object with method, headers, json/data/body and session options"
                ) from None
            if not isinstance(options, dict):
                raise ToolError("HTTP content must be a JSON object")
            destination = self._path(request, action.path) if action.path else None
            with ExitStack() as stack:
                download_options = {}
                if destination:
                    parent = stack.enter_context(
                        self._full_parent(request.run.goal_id, destination, create=True)
                    )
                    try:
                        info = os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise ToolError("The destination must be a regular file without links")
                    except FileNotFoundError:
                        pass
                    download_options["download_directory_fd"] = parent
                response = request_http(
                    action.url,
                    options,
                    self.root / ".rocky" / request.run.goal_id / "sessions",
                    download_path=destination,
                    blocked_ports=self.control_ports,
                    **download_options,
                )
            content = json.dumps(response, ensure_ascii=False)
            return ToolResult(
                success=200 <= response["status_code"] < 400,
                content=_clip(content),
                progress_key="http:" + _digest(content.encode()),
                artifacts=self._artifact(request, destination) if destination else [],
                changed_paths=[self.display_path(request.run.goal_id, destination)]
                if destination
                else [],
                error_code="" if 200 <= response["status_code"] < 400 else "http_status",
                retry_hint=""
                if 200 <= response["status_code"] < 400
                else "Inspect status_code and response text. Correct authentication, URL or request body; verify mutations before resubmitting.",
            )
        if action.tool == "run_command":
            if not action.content or not action.content.strip():
                raise ToolError("run_command requires a shell command in content")
            directory = self._path(request, action.path, default_root=True)
            if not directory.is_dir():
                raise ToolError("The command working directory does not exist")
            shell = "/bin/zsh" if Path("/bin/zsh").exists() else "/bin/sh"
            command = [shell, "-c", action.content]
            environment = worker_free_environment()
            if action.sandbox:
                prefix, environment = prepare_sandbox(
                    self.root,
                    request.run.goal_id,
                    self.root / request.run.goal_id,
                    protected_paths=self.protected_paths + self.sensitive_paths,
                    blocked_ports=self.control_ports,
                )
                command = [*prefix, *command]

            if action.background:
                return self._start_job(request, directory, command[:-1], environment)
            limit = action.timeout_seconds or DEFAULT_COMMAND_SECONDS

            def visible(path: Path) -> bool:
                return self._visible_path(request.run.goal_id, path, parent_resolved=True)

            before, complete_before = _snapshot(directory, visible)
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.Popen(
                    command,
                    cwd=directory,
                    env=environment,
                    stdout=stdout,
                    stderr=stderr,
                    stdin=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
                timed_out = False
                try:
                    process.wait(timeout=limit)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_group(process.pid)
                    process.wait()
                else:
                    # Children left behind (`cmd &`) would outlive the task
                    # unmanaged; long work belongs in a background job.
                    _kill_group(process.pid)
                stdout.seek(0)
                stderr.seek(0)
                output = stdout.read(MAX_CONTENT).decode("utf-8", errors="replace")
                errors = stderr.read(MAX_CONTENT).decode("utf-8", errors="replace")
                truncated = stdout.read(1) != b"" or stderr.read(1) != b""
            description = f"Exit code: {process.returncode}"
            if timed_out:
                description += (
                    f"\nCommand timed out after {limit} seconds; its process group was stopped."
                )
            after, complete_after = _snapshot(directory, visible)
            changed = [
                self.display_path(request.run.goal_id, path) for path in _changed(before, after)
            ]
            content = f"{description}\nstdout:\n{output}\nstderr:\n{errors}"
            if truncated:
                content += "\n[truncated; redirect large output to a file]"
            return ToolResult(
                success=not timed_out and process.returncode == 0,
                content=_clip(content),
                changed_paths=changed[:200],
                changes_truncated=len(changed) > 200 or not (complete_before and complete_after),
                progress_key="command:"
                + _digest((str(directory) + action.content + content).encode()),
                error_code="command_timeout"
                if timed_out
                else ("command_failed" if process.returncode else ""),
                retry_hint=(
                    f"Inspect partial output and filesystem effects before retrying. Ask for a longer timeout_seconds (up to {MAX_COMMAND_SECONDS}) or run it with background=true and check its log later."
                    if timed_out
                    else (
                        "Inspect stderr and exit code, correct the command or dependencies, then retry with a new operation identifier."
                        if process.returncode
                        else ""
                    )
                ),
            )
        path = self._path(request, action.path, default_root=action.tool == "list_files")
        if action.tool == "list_files":
            entries: list[str] = []

            def visit(descriptor: int, directory: Path) -> None:
                for name in sorted(os.listdir(descriptor)):
                    if len(entries) >= 300:
                        return
                    item = directory / name
                    # Task runtime caches are implementation details, not sources.
                    # They remain readable when explicitly requested.
                    if name == ".rocky-sandbox":
                        continue
                    if not self._visible_path(request.run.goal_id, item, parent_resolved=True):
                        continue
                    try:
                        self._check_parent(request.run.goal_id, descriptor, name)
                        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                            continue
                        suffix = "/" if stat.S_ISDIR(info.st_mode) else ""
                        if stat.S_ISLNK(info.st_mode):
                            suffix = " -> " + os.readlink(name, dir_fd=descriptor)
                        entries.append(item.relative_to(path).as_posix() + suffix)
                        if stat.S_ISDIR(info.st_mode):
                            child = _open_directory(descriptor, name, create=False)
                            try:
                                visit(child, item)
                            finally:
                                os.close(child)
                    except (OSError, ToolError):
                        continue

            with self._full_parent(request.run.goal_id, path) as parent:
                descriptor = os.open(path.name or path.anchor, _DIRECTORY_FLAGS, dir_fd=parent)
                try:
                    # A differently spelled root must not expose sibling names.
                    self._check_parent(request.run.goal_id, descriptor, "")
                    visit(descriptor, path)
                finally:
                    os.close(descriptor)
            if len(entries) >= 300:
                entries.append("[listing truncated; use run_command for a complete listing]")
            content = "\n".join(entries) or "(empty directory)"
            return ToolResult(
                success=True,
                content=_clip(content),
                progress_key="list:" + _digest(content.encode()),
            )
        if action.tool == "read_file":
            with self._full_file(request.run.goal_id, path) as handle:
                if bounds := parse_read_range(action.content):
                    content = _read_lines(handle, bounds, errors="replace")
                    return ToolResult(
                        success=True,
                        content=content,
                        progress_key="read:" + _digest(content.encode()),
                    )
                content = handle.read(MAX_CONTENT * 4 + 1).decode("utf-8", errors="replace")
            return ToolResult(
                success=True,
                content=_clip(content),
                progress_key="read:" + _digest(content.encode()),
            )
        if action.tool == "write_file":
            if action.content is None:
                raise ToolError("write_file requires text content")
            with self._full_parent(request.run.goal_id, path, create=True) as parent:
                _atomic_write(parent, path.name, action.content.encode("utf-8"), preserve_mode=True)
            return ToolResult(
                success=True,
                content=f"Wrote {path}",
                progress_key="write:" + str(path) + ":" + _digest(action.content.encode()),
                artifacts=self._artifact(request, path),
                changed_paths=[self.display_path(request.run.goal_id, path)],
            )
        if action.tool == "append_file":
            if not action.content:
                raise ToolError("append_file requires nonempty text content")
            with self._full_file(request.run.goal_id, path, append=True) as handle:
                handle.write(action.content.encode("utf-8"))
                handle.flush()
                size = os.fstat(handle.fileno()).st_size
            return ToolResult(
                success=True,
                content=f"Appended {len(action.content)} characters to {path} ({size} bytes total)",
                progress_key=f"append:{path}:{size}:{_digest(action.content.encode())}",
                artifacts=self._artifact(request, path),
                changed_paths=[self.display_path(request.run.goal_id, path)],
            )
        if action.tool == "edit_file":
            try:
                with self._full_parent(request.run.goal_id, path) as parent:
                    text = _read_regular(parent, path.name, max_bytes=MAX_EDIT_BYTES).decode(
                        "utf-8"
                    )
                    edits = parse_edits(action.content)
                    updated = _apply_edits(text, edits)
                    _atomic_write(parent, path.name, updated.encode("utf-8"), preserve_mode=True)
            except UnicodeDecodeError:
                raise ToolError("The file is not UTF-8 text") from None
            except FileNotFoundError:
                raise ToolError(
                    "edit_file requires an existing regular file; use write_file to create it"
                ) from None
            return ToolResult(
                success=True,
                content=f"Applied {len(edits)} edit(s) to {path} ({len(updated.encode())} bytes)",
                progress_key=f"edit:{path}:{_digest(updated.encode())}",
                artifacts=self._artifact(request, path),
                changed_paths=[self.display_path(request.run.goal_id, path)],
            )
        raise ToolError("Unknown tool")

    def _list(self, directory_fd: int, prefix: str) -> list[str]:
        entries: list[str] = []

        def visit(descriptor: int, base: str) -> None:
            for name in sorted(os.listdir(descriptor)):
                if name.startswith(".") or len(entries) >= 300:
                    continue
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                path = f"{base}/{name}" if base else name
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    entries.append(f"{path}\t{info.st_size} bytes")
                elif stat.S_ISDIR(info.st_mode):
                    entries.append(path + "/")
                    child = _open_directory(descriptor, name, create=False)
                    try:
                        visit(child, path)
                    finally:
                        os.close(child)

        visit(directory_fd, prefix)
        if len(entries) >= 300:
            entries.append("[listing limited to 300 entries]")
        return entries

    def read_artifacts(self, run: RunInput) -> dict[str, str]:
        """Read deliverables for an independent reviewer; caller bounds the model context."""
        result = {}
        with self._goal(run.goal_id, create=False) as (goal_fd, _):
            for deliverable in run.contract.deliverables:
                parts = _parts(deliverable.path)
                with _parent_directory(goal_fd, parts, create=False) as parent:
                    result[deliverable.path] = _clip(
                        _read_regular(
                            parent, parts[-1], max_bytes=None if self.full_access else MAX_BYTES
                        ).decode("utf-8", errors="replace")
                    )
        return result

    def review_text(self, run: RunInput) -> str:
        """Return at most 24000 characters of declared artifacts for semantic review."""
        maximum = 24000
        marker = "\n[Artifact text truncated; review covers excerpts only.]"
        blocks = []
        size = 0
        with self._goal(run.goal_id, create=False) as (goal_fd, _):
            for deliverable in run.contract.deliverables:
                parts = _parts(deliverable.path)
                with _parent_directory(goal_fd, parts, create=False) as parent:
                    text = _read_regular(
                        parent, parts[-1], max_bytes=None if self.full_access else MAX_BYTES
                    ).decode("utf-8", errors="replace")
                block = f"\n--- artifact {json.dumps(deliverable.path)} ---\n{text}\n"
                remaining = maximum - len(marker) - size
                if len(block) > remaining:
                    blocks.append(block[: max(0, remaining)])
                    return "".join(blocks) + marker
                blocks.append(block)
                size += len(block)
        return "".join(blocks)

    def workspace_files(self, run: RunInput, limit: int = 200) -> list[str]:
        """Visible regular files in the goal workspace, goal-relative and sorted."""
        root = self.root / run.goal_id
        files: list[str] = []
        for current, folders, names in os.walk(root):
            folders[:] = sorted(
                name
                for name in folders
                if not name.startswith(".") and name not in _UNTRACKED_DIRECTORIES
            )
            for name in sorted(names):
                path = Path(current, name)
                if name.startswith(".") or not stat.S_ISREG(path.lstat().st_mode):
                    continue
                files.append(path.relative_to(root).as_posix())
                if len(files) >= limit:
                    return files
        return files

    def describe_file(self, run: RunInput, path: str, limit: int) -> dict:
        """Size, line count and a text excerpt of a file, for independent review."""
        display = self.display_path(run.goal_id, path)
        try:
            if self.full_access:
                target = self._resolve_path(run.goal_id, path)
                with self._full_file(run.goal_id, target) as handle:
                    # Line counts stay exact for any plausible input; giant files
                    # report their size and an excerpt without a full scan.
                    content = handle.read(MAX_BYTES * 16 + 1)
                    size = os.fstat(handle.fileno()).st_size
            else:
                parts = _parts(display)
                with self._goal(run.goal_id, create=False) as (goal_fd, _):
                    with _parent_directory(goal_fd, parts, create=False) as parent:
                        content = _read_regular(parent, parts[-1])
                size = len(content)
        except (OSError, ToolError):
            return {"path": display, "missing": True}
        details: dict = {"path": display, "bytes": size}
        if len(content) == size:
            details["lines"] = content.count(b"\n") + (not content.endswith(b"\n") and size > 0)
        if b"\0" in content[:8192]:
            return {**details, "binary": True}
        text = content[:limit].decode("utf-8", errors="replace")
        return {**details, "content": text, "excerpt": size > limit}

    def verify_files(self, run: RunInput) -> VerificationResult:
        artifacts: list[Artifact] = []
        checks: list[str] = []
        passed = True
        try:
            with self._goal(run.goal_id, create=False) as (goal_fd, _):
                for deliverable in run.contract.deliverables:
                    try:
                        parts = _parts(deliverable.path)
                        with _parent_directory(goal_fd, parts, create=False) as parent:
                            content = _read_regular(
                                parent, parts[-1], max_bytes=None if self.full_access else MAX_BYTES
                            )
                        if len(content) < deliverable.min_bytes:
                            raise ToolError("file is smaller than min_bytes")
                        text = content.decode("utf-8", errors="replace")
                        if any(required not in text for required in deliverable.contains):
                            raise ToolError("file does not contain all required text")
                        artifacts.append(
                            Artifact(
                                path=deliverable.path, size=len(content), sha256=_digest(content)
                            )
                        )
                        checks.append(f"{deliverable.path}: passed")
                    except (ToolError, OSError) as exc:
                        passed = False
                        reason = (
                            str(exc)
                            if isinstance(exc, ToolError)
                            else "missing, unsafe or inaccessible file"
                        )
                        checks.append(f"{deliverable.path}: {reason}")
        except (ToolError, OSError):
            return VerificationResult(
                passed=False, feedback="Goal workspace is missing or inaccessible"
            )
        return VerificationResult(passed=passed, feedback="\n".join(checks), artifacts=artifacts)
