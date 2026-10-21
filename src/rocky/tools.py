"""Goal-scoped tools with durable receipts and race-resistant filesystem access.

The workspace and receipt volume must be shared by workers on the same host.
Advisory locks coordinate retries; file descriptors and O_NOFOLLOW enforce scope.
"""

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

from rocky.config import Settings
from rocky.models import Artifact, RunInput, ToolInput, ToolResult, VerificationResult
from rocky.network import FetchError, fetch_public_text

MAX_BYTES = 1_000_000
MAX_CONTENT = 16000
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class ToolError(ValueError):
    """Safe failure text that may be returned to the planner."""


def _clip(value: str) -> str:
    suffix = "\n[truncated]"
    return value if len(value) <= MAX_CONTENT else value[: MAX_CONTENT - len(suffix)] + suffix


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _operation_fingerprint(request: ToolInput) -> str:
    # Keep the original v0.1 ToolInput serialization byte-for-byte. Inference
    # settings do not change tool semantics; including them would invalidate
    # durable receipts when old Activity inputs are decoded by newer workers.
    payload = request.model_dump_json(
        include={"run": {"goal_id", "contract"}, "operation_id": True, "action": True}
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


def _read_regular(parent: int, name: str, *, max_bytes: int = MAX_BYTES) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ToolError("Only regular files without hard links can be read")
        if info.st_size > max_bytes:
            raise ToolError("File exceeds the maximum size")
        content = handle.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ToolError("File exceeds the maximum size")
        return content


def _atomic_write(parent: int, name: str, content: bytes) -> None:
    try:
        existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
            raise ToolError("The destination must be a regular file without links")
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
        # The configured base is trusted; goal-relative traversal is never resolved.
        self.root = settings.workspace_root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def _goal(self, goal_id: str, *, create: bool = True) -> Iterator[tuple[int, int]]:
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
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield goal_fd, receipt_fd

    def execute(self, request: ToolInput) -> ToolResult:
        try:
            if not request.operation_id or len(request.operation_id) > 256:
                raise ToolError("Invalid operation identifier")
            if request.action.tool not in request.run.contract.allowed_tools:
                raise ToolError("The tool is not authorized by this goal")
            fingerprint = _operation_fingerprint(request)
            receipt_name = _digest(request.operation_id.encode()) + ".json"
            with self._goal(request.run.goal_id) as (goal_fd, receipts_fd):
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
                    return ToolResult.model_validate(stored["result"])
                try:
                    result = self._execute(request, goal_fd)
                except (ToolError, FetchError) as exc:
                    result = ToolResult(success=False, content=str(exc))
                except OSError:
                    result = ToolResult(
                        success=False, content="Workspace path is missing, unsafe or inaccessible"
                    )
                _atomic_write(
                    receipts_fd,
                    receipt_name,
                    json.dumps(
                        {"fingerprint": fingerprint, "result": result.model_dump()}
                    ).encode(),
                )
                return result
        except (ToolError, FetchError) as exc:
            return ToolResult(success=False, content=str(exc))
        except (OSError, ValueError, KeyError, TypeError):
            return ToolResult(success=False, content="Operation storage is unavailable or invalid")

    def _execute(self, request: ToolInput, goal_fd: int) -> ToolResult:
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
        with _parent_directory(goal_fd, parts, create=action.tool == "write_file") as parent:
            if action.tool == "read_file":
                content = _read_regular(parent, parts[-1])
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ToolError("The file is not UTF-8 text") from exc
                return ToolResult(
                    success=True, content=_clip(text), progress_key="read:" + _digest(content)
                )
            if action.content is None:
                raise ToolError("write_file requires text content")
            content = action.content.encode("utf-8")
            if len(content) > MAX_BYTES:
                raise ToolError("File exceeds the maximum size")
            _atomic_write(parent, parts[-1], content)
            artifact = Artifact(path="/".join(parts), size=len(content), sha256=_digest(content))
            return ToolResult(
                success=True,
                content=f"Wrote {artifact.path} ({artifact.size} bytes)",
                progress_key="write:" + artifact.path + ":" + artifact.sha256,
                artifacts=[artifact],
            )

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
                        _read_regular(parent, parts[-1]).decode("utf-8", errors="replace")
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
                    text = _read_regular(parent, parts[-1]).decode("utf-8", errors="replace")
                block = f"\n--- artifact {json.dumps(deliverable.path)} ---\n{text}\n"
                remaining = maximum - len(marker) - size
                if len(block) > remaining:
                    blocks.append(block[: max(0, remaining)])
                    return "".join(blocks) + marker
                blocks.append(block)
                size += len(block)
        return "".join(blocks)

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
                            content = _read_regular(parent, parts[-1])
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
