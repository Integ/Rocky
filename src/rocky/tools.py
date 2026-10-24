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
import signal
import stat
import subprocess
import sys
import tempfile
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

from rocky.command_policy import command_class, matches_prefix
from rocky.config import Settings
from rocky.http_tools import HTTPToolError, request_http
from rocky.models import (
    Artifact,
    RunInput,
    ToolInput,
    ToolResult,
    VerificationResult,
    read_only_action,
)
from rocky.network import FetchError, fetch_public_text
from rocky.seatbelt import SandboxUnavailable, prepare_sandbox
from rocky.tool_contracts import (
    ActionValidationError,
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


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# Dependency, VCS and cache trees are not task output and can be very large.
_UNTRACKED_DIRECTORIES = frozenset(
    {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox"}
    | {".mypy_cache", ".pytest_cache", ".ruff_cache", ".rocky-sandbox"}
)
_SNAPSHOT_LIMIT = 5000


def _snapshot(
    directory: Path, allowed: Callable[[Path], bool]
) -> tuple[dict[str, tuple[int, int, int]], bool]:
    """Stat regular files below directory, in a stable order; report completeness."""
    entries: dict[str, tuple[int, int, int]] = {}
    for current, folders, files in os.walk(directory):
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


def _operation_fingerprint(request: ToolInput) -> str:
    # Keep the original v0.1 ToolInput serialization byte-for-byte. Inference
    # settings do not change tool semantics; including them would invalidate
    # durable receipts when old Activity inputs are decoded by newer workers.
    exclude = {"run": {"contract": {"limits"}}}
    if request.action.sandbox:
        # Adding the default must not invalidate completed pre-sandbox receipts.
        exclude["action"] = {"sandbox"}
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
        self.protected_paths = tuple(protected)
        self.protected_write_paths = tuple(
            {
                Path(__file__).resolve().parent,
                Path(sys.prefix).resolve(),
                Path(sys.base_prefix).resolve(),
            }
        )

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

    def execute(self, request: ToolInput) -> ToolResult:
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
                if unsandboxed or (
                    self.approval_mode != "trusted"
                    and requires_approval(request.action, options)
                    and not self._prefix_approved(request)
                ):
                    approval = approval_fingerprint(request)
                    if approval not in request.approved_action_fingerprints:
                        return ToolResult(
                            success=False,
                            content=(
                                "Owner approval is required to run this command outside Seatbelt with the current user's privileges. The operation has not been dispatched."
                                if unsandboxed
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
                        )
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

    def _prefix_approved(self, request: ToolInput) -> bool:
        """Owner allowlist or a task-scoped grant covers this simple command."""
        return (
            request.action.tool == "run_command"
            and request.action.sandbox
            and matches_prefix(
                request.action.content,
                [*self.auto_approved_prefixes, *request.approved_command_prefixes],
            )
        )

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

        def key(value: Path) -> str:
            text = str(value)
            return (
                unicodedata.normalize("NFD", text).casefold() if sys.platform == "darwin" else text
            )

        def inside(value: Path, parent: Path) -> bool:
            return key(value) == key(parent) or key(value).startswith(key(parent).rstrip("/") + "/")

        if inside(path, self.root) and not inside(path, self.root / goal_id):
            raise ToolError(
                "Access to other task directories and private .rocky storage is forbidden"
            )
        if any(inside(path, protected) for protected in self.protected_paths):
            raise ToolError("Access to Rocky's private control files is forbidden")
        if write and any(inside(path, protected) for protected in self.protected_write_paths):
            raise ToolError(
                "File tools cannot modify Rocky's code or Python runtime; request approved unsandboxed execution"
            )

    def _visible_path(self, goal_id: str, path: Path) -> bool:
        try:
            self._check_path(goal_id, path)
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
            environment = None
            if action.sandbox:
                prefix, environment = prepare_sandbox(
                    self.root,
                    request.run.goal_id,
                    self.root / request.run.goal_id,
                    protected_paths=self.protected_paths,
                    blocked_ports=self.control_ports,
                )
                command = [*prefix, *command]

            def visible(path: Path) -> bool:
                return self._visible_path(request.run.goal_id, path)

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
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                stdout.seek(0)
                stderr.seek(0)
                output = stdout.read(MAX_CONTENT).decode("utf-8", errors="replace")
                errors = stderr.read(MAX_CONTENT).decode("utf-8", errors="replace")
                truncated = stdout.read(1) != b"" or stderr.read(1) != b""
            description = f"Exit code: {process.returncode}"
            if timed_out:
                description += (
                    "\nCommand timed out after 30 seconds; its process group was stopped."
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
                    "Inspect partial output and filesystem effects before retrying. Break long work into bounded commands or run a background job with output redirected to a file."
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
                    if not self._visible_path(request.run.goal_id, item):
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
