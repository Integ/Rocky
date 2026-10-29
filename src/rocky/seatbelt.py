"""Per-goal macOS command confinement.

The policy permits ordinary tool and system reads and network access, but only
the current goal can be written. Other goal trees, Rocky's control files and
configured control ports are inaccessible. Seatbelt is inherited across
fork/exec, including detached children. Ordinary external reads and network
access remain available; this is not a general secrets or network boundary.
"""

import os
import re
import sys
from contextlib import ExitStack
from pathlib import Path

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
RUNTIME_DIRECTORY = ".rocky-sandbox"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_PRIVATE_ENV_SUFFIXES = (
    "_API_KEY",
    "_API_TOKEN",
    "_ACCESS_TOKEN",
    "_AUTH_TOKEN",
    "_SECRET_KEY",
    "_PRIVATE_KEY",
)
_PRIVATE_ENV_NAMES = {
    "API_KEY",
    "API_TOKEN",
    "ACCESS_TOKEN",
    "AUTH_TOKEN",
    "SECRET_KEY",
    "PRIVATE_KEY",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "OPENAI_ADMIN_KEY",
}
_WORKER_ENV_NAMES = {
    "UV_PROJECT_ENVIRONMENT",
    "UV_WORKING_DIRECTORY",
    "UV_PROJECT",
    "VIRTUAL_ENV",
    "PYTHONHOME",
    "PYTHONPATH",
}


class SandboxUnavailable(RuntimeError):
    """The requested confinement cannot be established; never run unconfined."""


def _quote(value: Path | str) -> str:
    """Quote a literal SBPL string, without interpreting paths as regexes."""
    value = str(value)
    # SBPL is Scheme, not JSON: reject control characters instead of depending
    # on JSON's unsupported \u escapes or a newline being accepted by SBPL.
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Sandbox paths must not contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _runtime_directories(goal: Path) -> dict[str, Path]:
    """Create private caches without following goal-controlled directory links."""
    paths = {name: goal / RUNTIME_DIRECTORY / name for name in ("tmp", "cache", "data", "config")}
    with ExitStack() as stack:
        descriptor = os.open(goal, _DIRECTORY_FLAGS)
        stack.callback(os.close, descriptor)
        for name in (RUNTIME_DIRECTORY,):
            try:
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            stack.callback(os.close, descriptor)
        for name in paths:
            try:
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(child)
    return paths


def _profile(
    root: Path,
    goal: Path,
    *,
    protected_paths: tuple[Path, ...] = (),
    blocked_ports: tuple[int, ...] = (),
) -> str:
    # The root-wide rule covers directories created after compilation too.
    # Existing peer symlinks are denied by canonical target as well, since
    # Seatbelt evaluates resolved vnodes rather than the spelling of a symlink.
    protected = {root / ".rocky", (root / ".rocky").resolve()}
    for path in protected_paths:
        path = Path(path).expanduser()
        if not path.is_absolute():
            raise ValueError("Protected sandbox paths must be absolute")
        protected.update((path, path.resolve()))
    for child in root.iterdir():
        if child != goal:
            canonical = child.resolve()
            if not canonical.is_relative_to(root):
                protected.add(canonical)
    blocked = "\n".join(
        f"(deny file-read* file-write* (subpath {_quote(path)}))" for path in sorted(protected)
    )
    # SBPL's localhost matcher misses IPv4-mapped IPv6 loopback. Denying the
    # configured destination ports on every interface closes that route, plus
    # connections through the machine's LAN address or a remote control host.
    # Other external services using these same ports are deliberately denied.
    for port in blocked_ports:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Blocked sandbox ports must be integers from 1 to 65535")
    blocked_network = "\n".join(
        f'(deny network-outbound (remote ip "*:{port}"))\n(deny network-bind (local ip "*:{port}"))'
        for port in sorted(set(blocked_ports))
    )
    return f"""(version 1)
(deny default)
(allow process-exec process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read)
(allow network*)
{blocked_network}
(allow system-socket (require-all (socket-domain AF_SYSTEM) (socket-protocol 2)))
(allow mach-lookup
  (global-name "com.apple.system.opendirectoryd.libinfo")
  (global-name "com.apple.system.opendirectoryd.membership")
  (global-name "com.apple.bsd.dirhelper")
  (global-name "com.apple.SecurityServer")
  (global-name "com.apple.networkd")
  (global-name "com.apple.ocspd")
  (global-name "com.apple.trustd.agent")
  (global-name "com.apple.TrustEvaluationAgent")
  (global-name "com.apple.SystemConfiguration.DNSConfiguration")
  (global-name "com.apple.SystemConfiguration.configd"))
(allow file-read*)
(deny file-read* (require-all
  (subpath {_quote(root)})
  (require-not (subpath {_quote(goal)}))))
; Traversal and realpath need the workspace's own metadata, not its contents.
(allow file-read-metadata (literal {_quote(root)}))
(allow file-write* (subpath {_quote(goal)}))
(allow file-write-data (require-all
  (literal "/dev/null") (vnode-type CHARACTER-DEVICE)))
{blocked}
"""


def worker_free_environment() -> dict[str, str]:
    """The worker's environment without Rocky's control token or its Python setup.

    Unsandboxed commands get this too: the owner approved the command, not
    access to Rocky's API token or the worker's virtual environment.
    """
    return {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(("ROCKY_", "TEMPORAL_"))
        and name.upper() not in _WORKER_ENV_NAMES
    }


def prepare_sandbox(
    root: Path,
    goal_id: str,
    directory: Path,
    *,
    protected_paths: tuple[Path, ...] = (),
    blocked_ports: tuple[int, ...] = (),
) -> tuple[list[str], dict[str, str]]:
    """Return the command prefix and environment for a trusted goal directory.

    ``directory`` is the goal root, not an arbitrary command working directory.
    The caller separately validates cwd. No unsupported-platform fallback or
    profile file exists for a command to replace before sandbox-exec reads it.
    """
    if sys.platform != "darwin" or not os.access(SANDBOX_EXEC, os.X_OK):
        raise SandboxUnavailable(
            "Seatbelt requires macOS and /usr/bin/sandbox-exec; "
            "request approved unsandboxed execution if confinement is unavailable."
        )
    if not _IDENTIFIER.fullmatch(goal_id):
        raise ValueError("Invalid goal identifier")
    root = root.expanduser().resolve(strict=True)
    goal = root / goal_id
    if goal.is_symlink() or directory.resolve(strict=True) != goal:
        raise ValueError("Sandbox directory must be the current goal's real directory")
    # Validate path quoting before making any goal-local directories.
    profile = _profile(root, goal, protected_paths=protected_paths, blocked_ports=blocked_ports)
    paths = _runtime_directories(goal)
    # The worker's control credentials must not become command capabilities.
    # Keep the toolchain environment (including PATH/HOME) and the parent
    # environment intact while dropping Rocky/Temporal and common API secrets.
    env = {
        name: value
        for name, value in worker_free_environment().items()
        if name.upper() not in _PRIVATE_ENV_NAMES
        and not name.upper().endswith(_PRIVATE_ENV_SUFFIXES)
    }
    env.update(
        {
            "TMPDIR": str(paths["tmp"]) + os.sep,
            # zsh here-documents use TMPPREFIX rather than TMPDIR.
            "TMPPREFIX": str(paths["tmp"] / "zsh"),
            "TMP": str(paths["tmp"]),
            "TEMP": str(paths["tmp"]),
            "XDG_CACHE_HOME": str(paths["cache"]),
            "XDG_DATA_HOME": str(paths["data"]),
            "XDG_CONFIG_HOME": str(paths["config"]),
            "UV_CACHE_DIR": str(paths["cache"] / "uv"),
            "UV_PYTHON_INSTALL_DIR": str(paths["data"] / "uv" / "python"),
            "UV_TOOL_DIR": str(paths["data"] / "uv" / "tools"),
            "UV_TOOL_BIN_DIR": str(paths["data"] / "uv" / "bin"),
            "PIP_CACHE_DIR": str(paths["cache"] / "pip"),
            "PYTHONPYCACHEPREFIX": str(paths["cache"] / "python"),
            "npm_config_cache": str(paths["cache"] / "npm"),
        }
    )
    return [str(SANDBOX_EXEC), "-p", profile], env
