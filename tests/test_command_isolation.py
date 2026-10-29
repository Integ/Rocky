"""WorkspaceTools dispatch must preserve the per-goal kernel boundary."""

import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from rocky import seatbelt
from rocky.config import Settings
from rocky.models import Action, Deliverable, GoalContract, RunInput, ToolInput
from rocky.tools import WorkspaceTools


@pytest.fixture
def command_tasks(tmp_path):
    settings = Settings(
        workspace_root=tmp_path / "workspaces",
        tool_access="full",
        approval_mode="trusted",
        credential_path=tmp_path / "credentials.json",
        database_url=f"sqlite:///{tmp_path / 'rocky.db'}",
        port=49111,
        temporal_address="localhost:49112",
    )
    tools = WorkspaceTools(settings)
    run = RunInput(
        goal_id="current",
        contract=GoalContract(
            objective="Complete an isolated local task",
            acceptance_criteria=["The result is saved"],
            deliverables=[Deliverable(path="report.txt")],
        ),
    )
    goal = tools.root / run.goal_id
    goal.mkdir()
    protected = {}
    for name in ("other", ".rocky"):
        directory = tools.root / name
        directory.mkdir()
        target = directory / "secret.txt"
        target.write_text(f"private {name} contents")
        protected[name] = target
    return SimpleNamespace(tools=tools, run=run, goal=goal, protected=protected)


def execute_command(tasks, command, operation_id="command"):
    action = Action(tool="run_command", content=command)
    assert action.sandbox is True
    return tasks.tools.execute(ToolInput(run=tasks.run, action=action, operation_id=operation_id))


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt execution requires macOS")
@pytest.mark.parametrize("protected", ["other", ".rocky"])
def test_default_trusted_command_cannot_write_other_goal_or_private_storage(
    command_tasks, protected
):
    tasks = command_tasks
    target = tasks.protected[protected]
    original = target.read_text()
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('unauthorized')",
            str(target),
        ]
    )

    result = execute_command(tasks, command)

    assert not result.success, result.content
    assert result.error_code == "command_failed"
    assert "PermissionError" in result.content
    assert target.read_text() == original


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt execution requires macOS")
@pytest.mark.parametrize("protected", ["other", ".rocky"])
@pytest.mark.parametrize("location", ["goal_file", "goal_directory", "external"])
def test_default_command_cannot_read_protected_contents_through_aliases(
    command_tasks, protected, location
):
    tasks = command_tasks
    target = tasks.protected[protected]
    original = target.read_text()
    if location == "goal_directory":
        alias = tasks.goal / "directory-alias"
        alias.symlink_to(target.parent, target_is_directory=True)
        alias = alias / target.name
    else:
        directory = tasks.goal if location == "goal_file" else tasks.tools.root.parent
        alias = directory / "secret-alias"
        alias.symlink_to(target)
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())",
            str(alias),
        ]
    )

    result = execute_command(tasks, command)

    assert not result.success, result.content
    assert result.error_code == "command_failed"
    assert "PermissionError" in result.content
    assert original not in result.content


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt execution requires macOS")
def test_default_command_runs_python_with_goal_local_files_and_temporary_directory(command_tasks):
    tasks = command_tasks
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "import pathlib,tempfile; pathlib.Path('report.txt').write_text('python works'); "
            "f=tempfile.NamedTemporaryFile(); print('temporary file: ' + f.name)",
        ]
    )

    result = execute_command(tasks, command)

    assert result.success, result.content
    assert (tasks.goal / "report.txt").read_text() == "python works"
    assert (
        "temporary file: " + str(tasks.goal / seatbelt.RUNTIME_DIRECTORY / "tmp") in result.content
    )
    assert result.changed_paths == ["report.txt"]


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt execution requires macOS")
def test_default_command_runs_git_inside_current_goal(command_tasks):
    tasks = command_tasks

    initialized = execute_command(tasks, "git init .", "git-init")
    status = execute_command(tasks, "git status --porcelain", "git-status")

    assert initialized.success, initialized.content
    assert status.success, status.content
    assert (tasks.goal / ".git" / "HEAD").is_file()


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt execution requires macOS")
def test_default_command_runs_uv_without_shared_cache_writes(command_tasks):
    tasks = command_tasks
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    command = shlex.join(
        [uv, "venv", "--python", sys.executable, "--no-python-downloads", ".test-venv"]
    )

    created = execute_command(tasks, command, "uv-create")
    invoked = execute_command(
        tasks,
        shlex.join([str(tasks.goal / ".test-venv" / "bin" / "python"), "-c", "print('uv works')"]),
        "uv-python",
    )

    assert created.success, created.content
    assert invoked.success, invoked.content
    assert "uv works" in invoked.content


@pytest.mark.parametrize("available", [False, True])
def test_unavailable_seatbelt_fails_closed_before_process_dispatch(
    command_tasks, monkeypatch, available
):
    tasks = command_tasks
    monkeypatch.setattr(seatbelt.sys, "platform", "linux" if available else "darwin")
    monkeypatch.setattr(seatbelt.os, "access", lambda *_args: available)
    dispatched = []

    def unexpected_process(*args, **kwargs):
        dispatched.append((args, kwargs))
        raise AssertionError("A confined command must never fall back to an unconfined process")

    monkeypatch.setattr("rocky.tools.subprocess.Popen", unexpected_process)

    result = execute_command(tasks, "printf escaped > escaped.txt")

    assert not result.success
    assert result.error_code == "sandbox_unavailable"
    assert "sandbox=false" in result.retry_hint
    assert dispatched == []
    assert not (tasks.goal / "escaped.txt").exists()


def test_default_dispatch_passes_profile_argv_environment_and_closes_control_descriptors(
    command_tasks, monkeypatch
):
    tasks = command_tasks
    monkeypatch.setattr(seatbelt.sys, "platform", "darwin")
    monkeypatch.setattr(seatbelt.os, "access", lambda *_args: True)
    captured = []

    def fake_process(arguments, **kwargs):
        captured.append((arguments, kwargs))
        kwargs["stdout"].write(b"stub command output\n")
        return SimpleNamespace(returncode=0, wait=lambda timeout=None: 0)

    monkeypatch.setattr("rocky.tools.subprocess.Popen", fake_process)
    command = "printf '%s' '$(touch should-not-run)'"

    result = execute_command(tasks, command)

    assert result.success, result.content
    assert len(captured) == 1
    arguments, options = captured[0]
    assert arguments[:2] == ["/usr/bin/sandbox-exec", "-p"]
    profile = arguments[2]
    assert "(deny default)" in profile
    assert f"(subpath {seatbelt._quote(tasks.tools.root)})" in profile
    assert f"(allow file-write* (subpath {seatbelt._quote(tasks.goal)}))" in profile
    for target in (*tasks.tools.protected_paths, *tasks.tools.sensitive_paths):
        assert f"(deny file-read* file-write* (subpath {seatbelt._quote(target)}))" in profile
    for port in tasks.tools.control_ports:
        assert f'(deny network-outbound (remote ip "*:{port}"))' in profile
    shell = "/bin/zsh" if Path("/bin/zsh").exists() else "/bin/sh"
    assert arguments[3:] == [shell, "-c", command]
    assert "shell" not in options
    assert "executable" not in options
    assert options["close_fds"] is True
    assert options["start_new_session"] is True
    assert options["stdin"] == subprocess.DEVNULL
    assert options["cwd"] == tasks.goal
    assert Path(options["env"]["UV_CACHE_DIR"]).is_relative_to(tasks.goal)
    assert Path(options["env"]["TMPDIR"]).is_relative_to(tasks.goal)
    assert "stub command output" in result.content
    assert not (tasks.goal / "should-not-run").exists()
