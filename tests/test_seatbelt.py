"""Policy generation checks and real macOS kernel confinement regressions."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from rocky import seatbelt


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspaces"
    goal = root / "current"
    goal.mkdir(parents=True)
    (root / "other").mkdir()
    (root / ".rocky").mkdir()
    return root, goal


def test_prepare_fails_closed_without_seatbelt(workspace, monkeypatch):
    root, goal = workspace
    monkeypatch.setattr(seatbelt.sys, "platform", "linux")
    with pytest.raises(seatbelt.SandboxUnavailable, match="Seatbelt requires macOS"):
        seatbelt.prepare_sandbox(root, "current", goal)
    assert not (goal / seatbelt.RUNTIME_DIRECTORY).exists()


def test_prepare_uses_goal_local_caches_and_preserves_home(workspace, monkeypatch):
    root, goal = workspace
    monkeypatch.setattr(seatbelt.sys, "platform", "darwin")
    monkeypatch.setattr(seatbelt.os, "access", lambda *_: True)
    monkeypatch.setenv("HOME", "/ordinary/home")
    monkeypatch.setenv("UV_CACHE_DIR", "/shared/cache")
    private_variables = (
        "ROCKY_API_TOKEN",
        "ROCKY_APPROVAL_MODE",
        "TEMPORAL_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "GEMINI_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "HF_TOKEN",
    )
    for name in private_variables:
        monkeypatch.setenv(name, "private")
    worker_variables = (
        "UV_PROJECT_ENVIRONMENT",
        "UV_WORKING_DIRECTORY",
        "UV_PROJECT",
        "VIRTUAL_ENV",
        "PYTHONHOME",
        "PYTHONPATH",
    )
    for name in worker_variables:
        monkeypatch.setenv(name, "/parent/environment")
    prefix, env = seatbelt.prepare_sandbox(root, "current", goal)
    assert prefix[:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert "(deny default)" in prefix[2]
    assert env["HOME"] == "/ordinary/home"
    assert os.environ["UV_CACHE_DIR"] == "/shared/cache"
    assert env["PATH"] == os.environ["PATH"]
    for name in private_variables:
        assert name not in env
        assert os.environ[name] == "private"
    for name in worker_variables:
        assert name not in env
        assert os.environ[name] == "/parent/environment"
    for name in ("TMPDIR", "TMPPREFIX", "XDG_CACHE_HOME", "UV_CACHE_DIR", "PYTHONPYCACHEPREFIX"):
        assert Path(env[name]).is_relative_to(goal)
    assert Path(env["TMPDIR"]).is_dir()


@pytest.mark.parametrize("relative", [".rocky-sandbox", ".rocky-sandbox/tmp"])
def test_prepare_refuses_runtime_directory_symlinks(workspace, tmp_path, monkeypatch, relative):
    root, goal = workspace
    monkeypatch.setattr(seatbelt.sys, "platform", "darwin")
    monkeypatch.setattr(seatbelt.os, "access", lambda *_: True)
    destination = tmp_path / "outside"
    destination.mkdir()
    link = goal / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(OSError):
        seatbelt.prepare_sandbox(root, "current", goal)
    assert list(destination.iterdir()) == []


def test_profile_quotes_literal_paths_and_canonical_protected_targets(workspace, tmp_path):
    root, goal = workspace
    external = tmp_path / 'external " ) (allow default) \\ dir'
    external.mkdir()
    (root / "alias").symlink_to(external, target_is_directory=True)
    profile = seatbelt._profile(root, goal)
    assert f"(subpath {seatbelt._quote(external)})" in profile
    assert '\\"' in seatbelt._quote(external)
    assert "\\\\" in seatbelt._quote(external)
    with pytest.raises(ValueError, match="control characters"):
        seatbelt._quote("/tmp/bad\n(allow default)")


@pytest.mark.parametrize("port", [0, -1, 65536, True, '8000") (allow default)'])
def test_profile_refuses_invalid_control_ports(workspace, port):
    root, goal = workspace
    with pytest.raises(ValueError, match="ports must be integers"):
        seatbelt._profile(root, goal, blocked_ports=(port,))


def test_profile_refuses_relative_protected_paths(workspace):
    root, goal = workspace
    with pytest.raises(ValueError, match="paths must be absolute"):
        seatbelt._profile(root, goal, protected_paths=(Path("relative-credentials.json"),))


@pytest.fixture
def confined(workspace):
    if sys.platform != "darwin" or not seatbelt.SANDBOX_EXEC.exists():
        pytest.skip("Real Seatbelt enforcement requires macOS")
    probe = subprocess.run(
        [str(seatbelt.SANDBOX_EXEC), "-p", "(version 1) (allow default)", "/usr/bin/true"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode and "sandbox_apply: Operation not permitted" in probe.stderr:
        pytest.skip("The host's enclosing sandbox forbids applying a nested Seatbelt policy")
    assert probe.returncode == 0, probe.stderr
    root, goal = workspace

    def prepare(**kwargs):
        prefix, env = seatbelt.prepare_sandbox(root, "current", goal, **kwargs)

        def run(*arguments):
            return subprocess.run(
                [*prefix, *map(str, arguments)],
                cwd=goal,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

        return run, env

    return root, goal, prepare


def test_commands_cannot_read_or_write_peers_control_or_outside(confined, tmp_path):
    root, goal, prepare = confined
    peer = root / "other" / "secret.txt"
    control = root / ".rocky" / "secret.txt"
    peer.write_text("peer")
    control.write_text("control")
    (goal / "peer-link").symlink_to(peer)
    (goal / "control-link").symlink_to(control)
    alias = tmp_path / "external-peer-alias"
    alias.symlink_to(peer)
    run, _ = prepare()
    # The profile has already been generated when a second task appears.
    future = root / "future"
    future.mkdir()
    future_file = future / "secret.txt"
    future_file.write_text("future")
    for path in (peer, control, goal / "peer-link", goal / "control-link", alias, future_file):
        result = run(
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; p=Path(sys.argv[1]); "
            "\nfor mode in ('r','w'):"
            "\n try: p.open(mode).close()"
            "\n except PermissionError: pass"
            "\n else: raise SystemExit('unexpected access: '+mode)",
            path,
        )
        assert result.returncode == 0, result.stderr + result.stdout
    outside = tmp_path / "outside-write.txt"
    result = run(sys.executable, "-c", "import sys; open(sys.argv[1], 'w')", outside)
    assert result.returncode != 0
    assert not outside.exists()
    assert peer.read_text() == "peer"
    assert control.read_text() == "control"
    assert future_file.read_text() == "future"


def test_peer_symlink_target_and_sbpl_metacharacters_remain_confined(confined, tmp_path):
    root, _, prepare = confined
    external = tmp_path / 'peer " ) (allow default) \\'
    external.mkdir()
    secret = external / "secret.txt"
    secret.write_text("secret")
    (root / "peer-alias").symlink_to(external, target_is_directory=True)
    run, _ = prepare()
    result = run(sys.executable, "-c", "import sys; open(sys.argv[1]).read()", secret)
    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr


def test_protected_state_files_are_unreadable_through_aliases(confined, tmp_path):
    _, goal, prepare = confined
    state = tmp_path / "rocky.db"
    credentials = goal / "credentials.json"
    state.write_text("control state")
    credentials.write_text("private credential")
    alias = goal / "state-alias"
    alias.symlink_to(state)
    run, _ = prepare(protected_paths=(state, credentials))
    for path in (state, credentials, alias):
        result = run(sys.executable, "-c", "import sys; open(sys.argv[1]).read()", path)
        assert result.returncode != 0
        assert "Operation not permitted" in result.stderr
    result = run(sys.executable, "-c", "import sys; open(sys.argv[1], 'w')", credentials)
    assert result.returncode != 0
    assert credentials.read_text() == "private credential"


def test_control_ports_are_blocked_for_ipv4_ipv6_and_mapped_loopback(confined):
    _, _, prepare = confined
    with socket.socket() as protected, socket.socket() as ordinary:
        protected.bind(("127.0.0.1", 0))
        ordinary.bind(("127.0.0.1", 0))
        protected.listen()
        ordinary.listen()
        blocked_port = protected.getsockname()[1]
        run, _ = prepare(blocked_ports=(blocked_port,))
        code = (
            "import socket,sys; "
            "s=socket.socket(int(sys.argv[1]), socket.SOCK_STREAM); s.settimeout(2); "
            "s.connect((sys.argv[2], int(sys.argv[3])))"
        )
        for family, address in (
            (socket.AF_INET, "127.0.0.1"),
            (socket.AF_INET, "127.0.0.2"),
            (socket.AF_INET6, "::1"),
            (socket.AF_INET6, "::ffff:127.0.0.1"),
        ):
            result = run(sys.executable, "-c", code, str(int(family)), address, str(blocked_port))
            assert result.returncode != 0
            assert "Operation not permitted" in result.stderr, result.stderr
        result = run(
            sys.executable,
            "-c",
            code,
            str(int(socket.AF_INET)),
            "127.0.0.1",
            str(ordinary.getsockname()[1]),
        )
        assert result.returncode == 0, result.stderr


def test_control_ports_cannot_be_bound_while_ordinary_listeners_work(confined):
    _, _, prepare = confined
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        blocked_port = reservation.getsockname()[1]
    run, _ = prepare(blocked_ports=(blocked_port,))
    code = (
        "import socket,sys; s=socket.socket(int(sys.argv[1]), socket.SOCK_STREAM); "
        "s.bind((sys.argv[2], int(sys.argv[3]))); s.listen(); print('listener works')"
    )
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        result = run(sys.executable, "-c", code, str(int(family)), address, str(blocked_port))
        assert result.returncode != 0
        assert "Operation not permitted" in result.stderr, result.stderr
    result = run(sys.executable, "-c", code, str(int(socket.AF_INET)), "127.0.0.1", "0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "listener works"


def test_python_git_and_uv_work_with_local_cache_and_temp(confined, tmp_path, monkeypatch):
    _, goal, prepare = confined
    external_env = tmp_path / "worker-venv"
    external_env.mkdir()
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(external_env))
    monkeypatch.setenv("UV_WORKING_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("VIRTUAL_ENV", str(external_env))
    run, env = prepare()
    result = run(
        "/bin/zsh",
        "-c",
        "cat <<'ROCKY_HEREDOC'\nheredoc works\nROCKY_HEREDOC\n",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "heredoc works"
    result = run(
        sys.executable,
        "-c",
        "import json, pathlib, tempfile; pathlib.Path('result.txt').write_text('ok'); "
        "f=tempfile.NamedTemporaryFile(); print(json.dumps(f.name))",
    )
    assert result.returncode == 0, result.stderr
    assert (goal / "result.txt").read_text() == "ok"
    assert Path(json.loads(result.stdout)).is_relative_to(Path(env["TMPDIR"]))
    for arguments in (("git", "init", "."), ("git", "status", "--porcelain")):
        result = run(*arguments)
        assert result.returncode == 0, result.stderr
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    result = run(uv, "venv", "--python", sys.executable, "--no-python-downloads", ".test-venv")
    assert result.returncode == 0, result.stderr
    result = run(goal / ".test-venv" / "bin" / "python", "-c", "print('venv works')")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "venv works"
    result = run(
        uv,
        "run",
        "--no-project",
        "--python",
        sys.executable,
        "--no-python-downloads",
        "python",
        "-c",
        "print('uv run works')",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "uv run works"
    (goal / "pyproject.toml").write_text(
        '[project]\nname = "isolated-task"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
    )
    result = run(
        uv,
        "run",
        "--python",
        sys.executable,
        "--no-python-downloads",
        "python",
        "-c",
        "import sys; print(sys.prefix)",
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == goal / ".venv"
    assert (goal / ".venv" / "pyvenv.cfg").exists()
    assert list(external_env.iterdir()) == []


def test_detached_descendant_inherits_confinement(confined):
    root, goal, prepare = confined
    peer = root / "other" / "background.txt"
    outcome = goal / "background-result.txt"
    child_code = (
        "import sys,time; from pathlib import Path; time.sleep(.1); "
        "\ntry: Path(sys.argv[1]).write_text('escaped')"
        "\nexcept PermissionError: Path(sys.argv[2]).write_text('denied')"
    )
    parent_code = (
        "import subprocess,sys; subprocess.Popen([sys.executable,'-c',sys.argv[1],"
        "sys.argv[2],sys.argv[3]], start_new_session=True, stdin=subprocess.DEVNULL,"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
    )
    run, _ = prepare()
    result = run(sys.executable, "-c", parent_code, child_code, peer, outcome)
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 5
    while not outcome.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert outcome.read_text() == "denied"
    assert not peer.exists()
