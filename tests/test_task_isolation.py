"""Full access retains local tools without exposing another goal or private state."""

import json
import os
import sys
from types import SimpleNamespace

import httpx
import pytest

from rocky.config import Settings
from rocky.http_tools import HTTPToolError, request_http
from rocky.models import Action, Deliverable, GoalContract, RunInput, ToolInput
from rocky.tools import WorkspaceTools


@pytest.fixture
def isolated_tasks(tmp_path):
    tools = WorkspaceTools(
        Settings(
            workspace_root=tmp_path / "workspaces", tool_access="full", approval_mode="trusted"
        )
    )
    run = RunInput(
        goal_id="task-a",
        contract=GoalContract(
            objective="Work independently on a local report",
            acceptance_criteria=["The report is complete"],
            deliverables=[Deliverable(path="report.txt")],
        ),
    )
    own = tools.root / run.goal_id
    own.mkdir()
    protected = {
        "other": tools.root / "other-task-private",
        "own_control": tools.root / ".rocky" / run.goal_id,
        "other_control": tools.root / ".rocky" / "other-task-private",
    }
    for name, directory in protected.items():
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "sensitive.txt").write_text(f"private {name} contents\n")
        (own / f"{name}-alias").symlink_to(directory, target_is_directory=True)
    external = tmp_path / "external"
    external.mkdir()
    return SimpleNamespace(tools=tools, run=run, own=own, protected=protected, external=external)


def execute(tasks, action, operation_id="isolation-operation"):
    return tasks.tools.execute(ToolInput(run=tasks.run, action=action, operation_id=operation_id))


def protected_path(tasks, protected, alias, *, filename="sensitive.txt"):
    directory = tasks.protected[protected]
    if alias == "absolute":
        path = directory
    elif alias == "traversal":
        path = os.path.join("..", directory.relative_to(tasks.tools.root))
    else:
        path = f"{protected}-alias"
    return os.path.join(path, filename) if filename else str(path)


def file_action(tool, path):
    content = None
    if tool in {"write_file", "append_file"}:
        content = "unauthorized change"
    elif tool == "edit_file":
        content = json.dumps({"old_text": "private", "new_text": "changed"})
    return Action(tool=tool, path=path, content=content)


@pytest.mark.parametrize(
    "tool", ["read_file", "write_file", "append_file", "edit_file", "list_files"]
)
@pytest.mark.parametrize("protected", ["other", "own_control", "other_control"])
@pytest.mark.parametrize("alias", ["absolute", "traversal", "symlink"])
def test_full_file_tools_reject_other_tasks_and_private_state(
    isolated_tasks, tool, protected, alias
):
    tasks = isolated_tasks
    target = tasks.protected[protected] / "sensitive.txt"
    original = target.read_text()
    path = protected_path(
        tasks, protected, alias, filename="" if tool == "list_files" else target.name
    )

    result = execute(tasks, file_action(tool, path))

    assert not result.success, result.content
    assert original not in result.content
    assert target.read_text() == original


@pytest.mark.parametrize("tool", ["write_file", "append_file"])
@pytest.mark.parametrize("relative", ["../new-task/result.txt", "../.rocky/new-task/result.txt"])
def test_full_file_tools_cannot_create_another_task_or_private_subdirectory(
    isolated_tasks, tool, relative
):
    tasks = isolated_tasks
    target = (tasks.own / relative).resolve()

    result = execute(tasks, file_action(tool, relative))

    assert not result.success, result.content
    assert not target.exists()
    assert not target.parent.exists()


@pytest.mark.parametrize("tool", ["fetch_url", "http_request"])
@pytest.mark.parametrize("protected", ["other", "own_control", "other_control"])
@pytest.mark.parametrize("alias", ["absolute", "traversal", "symlink"])
def test_download_destination_is_rejected_before_request_or_parent_creation(
    isolated_tasks, monkeypatch, tool, protected, alias
):
    tasks = isolated_tasks
    calls = []

    def unexpected_request(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("A denied download must not reach the network")

    monkeypatch.setattr("rocky.tools.request_http", unexpected_request)
    destination = protected_path(tasks, protected, alias, filename="new/nested/download.txt")

    result = execute(tasks, Action(tool=tool, url="https://example.com/report", path=destination))

    assert not result.success, result.content
    assert calls == []
    assert not (tasks.protected[protected] / "new").exists()


@pytest.mark.parametrize("tool", ["read_file", "write_file", "append_file", "edit_file"])
@pytest.mark.parametrize("location", ["own", "external"])
def test_hardlink_aliases_cannot_read_or_modify_protected_files(isolated_tasks, tool, location):
    tasks = isolated_tasks
    protected = tasks.protected["other"] / "sensitive.txt"
    original = protected.read_text()
    alias = getattr(tasks, location) / "hardlink.txt"
    alias.hardlink_to(protected)

    result = execute(tasks, file_action(tool, str(alias)))

    assert not result.success, result.content
    assert original not in result.content
    assert protected.read_text() == original


@pytest.mark.parametrize("location", ["own", "external"])
def test_full_file_tools_keep_own_hidden_files_and_external_paths_available(
    isolated_tasks, location
):
    tasks = isolated_tasks
    # An external .rocky directory is ordinary user data. Only Rocky's own
    # workspace_root/.rocky directory holds protected operation state.
    directory = tasks.own / ".settings" if location == "own" else tasks.external / ".rocky"
    target = directory / "report.txt"
    path = str(target) if location == "external" else ".settings/report.txt"

    written = execute(tasks, Action(tool="write_file", path=path, content="first"), "write")
    assert written.success, written.content
    appended = execute(tasks, Action(tool="append_file", path=path, content=" second"), "append")
    assert appended.success, appended.content
    edited = execute(
        tasks,
        Action(tool="edit_file", path=path, content='{"old_text":"first","new_text":"final"}'),
        "edit",
    )
    assert edited.success, edited.content
    read = execute(tasks, Action(tool="read_file", path=path), "read")
    assert read.success, read.content
    assert read.content == "final second"
    assert target.read_text() == "final second"
    listed = execute(tasks, Action(tool="list_files", path=str(directory.parent)), "list")
    assert listed.success, listed.content
    assert directory.name in listed.content
    assert "report.txt" in listed.content


def test_full_file_tools_keep_symlinks_to_allowed_external_paths_available(isolated_tasks):
    tasks = isolated_tasks
    target = tasks.external / "report.txt"
    target.write_text("first")
    (tasks.own / "external-folder").symlink_to(tasks.external, target_is_directory=True)
    (tasks.own / "external-file").symlink_to(target)

    read = execute(tasks, Action(tool="read_file", path="external-file"), "read")
    assert read.success, read.content
    assert read.content == "first"
    written = execute(
        tasks,
        Action(tool="write_file", path="external-folder/report.txt", content="second"),
        "write",
    )
    assert written.success, written.content
    assert target.read_text() == "second"


def test_listing_an_external_ancestor_prunes_other_tasks_and_private_state(isolated_tasks):
    tasks = isolated_tasks
    (tasks.own / "report.txt").write_text("own output")
    (tasks.external / "visible.txt").write_text("ordinary external file")
    (tasks.external / "other-link").symlink_to(tasks.protected["other"], target_is_directory=True)
    (tasks.external / "receipt-link").symlink_to(
        tasks.protected["own_control"], target_is_directory=True
    )

    result = execute(tasks, Action(tool="list_files", path=str(tasks.tools.root.parent)))

    assert result.success, result.content
    assert "visible.txt" in result.content
    assert "other-task-private" not in result.content
    assert ".rocky" not in result.content
    assert "sensitive.txt" not in result.content
    assert "other-link" not in result.content
    assert "receipt-link" not in result.content


def test_listing_resolves_only_symlink_entries(isolated_tasks, monkeypatch):
    tasks = isolated_tasks
    for number in range(50):
        (tasks.own / f"note-{number}.txt").write_text("plain")
    (tasks.own / "notes-link").symlink_to(tasks.own / "note-0.txt")
    resolved = []
    original = type(tasks.own).resolve

    def counting(self, *args, **kwargs):
        resolved.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(tasks.own), "resolve", counting)
    result = execute(tasks, Action(tool="list_files", path=None))

    assert result.success, result.content
    assert "note-49.txt" in result.content and "notes-link" in result.content
    # Plain entries skip realpath; the symlink entries (including the fixture's
    # aliases, which stay hidden) are still checked through their target.
    assert not any(name.startswith("note-") for name in resolved)
    assert "notes-link" in resolved and "other-alias" in resolved
    assert "other-alias" not in result.content


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt execution requires macOS")
def test_command_snapshot_resolves_only_symlink_entries(isolated_tasks, monkeypatch):
    tasks = isolated_tasks
    for number in range(50):
        (tasks.own / f"file-{number}.txt").write_text("plain")
    resolved = []
    original = type(tasks.own).resolve

    def counting(self, *args, **kwargs):
        resolved.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(tasks.own), "resolve", counting)
    result = execute(tasks, Action(tool="run_command", content="echo ok > out.txt"))

    assert result.success, result.content
    assert (tasks.own / "out.txt").read_text() == "ok\n"
    assert not any(name.startswith("file-") for name in resolved)


@pytest.mark.parametrize("protected", ["other", "own_control", "other_control"])
@pytest.mark.parametrize("alias", ["absolute", "traversal", "symlink"])
def test_reviewer_file_descriptions_do_not_expose_protected_contents(
    isolated_tasks, protected, alias
):
    tasks = isolated_tasks
    path = protected_path(tasks, protected, alias)

    details = tasks.tools.describe_file(tasks.run, path, limit=10000)

    assert details.get("missing") is True
    assert "content" not in details
    assert "bytes" not in details


def test_reviewer_file_descriptions_do_not_follow_hardlink_aliases(isolated_tasks):
    tasks = isolated_tasks
    alias = tasks.external / "hardlink.txt"
    alias.hardlink_to(tasks.protected["other"] / "sensitive.txt")

    details = tasks.tools.describe_file(tasks.run, str(alias), limit=10000)

    assert details.get("missing") is True
    assert "content" not in details
    assert "bytes" not in details


@pytest.mark.parametrize("tool", ["read_file", "write_file"])
def test_parent_symlink_swap_after_resolution_cannot_cross_task_boundary(
    isolated_tasks, monkeypatch, tool
):
    tasks = isolated_tasks
    original_parent = tasks.own / "mutable-parent"
    original_parent.mkdir()
    (original_parent / "sensitive.txt").write_text("own file")
    protected = tasks.protected["other"] / "sensitive.txt"
    original = protected.read_text()
    original_path = tasks.tools._path
    swapped = False

    def swap_parent_after_resolution(*args, **kwargs):
        nonlocal swapped
        resolved = original_path(*args, **kwargs)
        if not swapped and resolved.parent == original_parent:
            swapped = True
            original_parent.rename(tasks.own / "previous-parent")
            original_parent.symlink_to(tasks.protected["other"], target_is_directory=True)
        return resolved

    monkeypatch.setattr(tasks.tools, "_path", swap_parent_after_resolution)

    result = execute(tasks, file_action(tool, "mutable-parent/sensitive.txt"))

    assert swapped
    assert not result.success, result.content
    assert original not in result.content
    assert protected.read_text() == original


def test_http_download_stays_in_open_directory_after_parent_symlink_swap(
    isolated_tasks, monkeypatch
):
    tasks = isolated_tasks
    parent = tasks.own / "download-parent"
    parent.mkdir()
    previous = tasks.own / "previous-download-parent"
    protected = tasks.protected["other"] / "sensitive.txt"
    original = protected.read_text()
    async_client = httpx.AsyncClient

    def respond(_request):
        parent.rename(previous)
        parent.symlink_to(tasks.protected["other"], target_is_directory=True)
        return httpx.Response(200, content=b"downloaded report")

    def mock_client(**kwargs):
        return async_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("rocky.http_tools.httpx.AsyncClient", mock_client)
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        result = request_http(
            "https://example.com/report",
            {},
            tasks.external / "sessions",
            download_path=parent / "sensitive.txt",
            download_directory_fd=descriptor,
        )
    finally:
        os.close(descriptor)

    assert result["downloaded"] is True
    assert (previous / "sensitive.txt").read_text() == "downloaded report"
    assert protected.read_text() == original
    assert sorted(path.name for path in previous.iterdir()) == ["sensitive.txt"]


def test_failed_http_download_removes_temporary_file_by_directory_descriptor(
    isolated_tasks, monkeypatch
):
    tasks = isolated_tasks
    parent = tasks.own / "download-parent"
    parent.mkdir()
    async_client = httpx.AsyncClient

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial report"
            raise httpx.ReadError("interrupted fixture stream")

    def respond(_request):
        return httpx.Response(200, stream=InterruptedStream())

    def mock_client(**kwargs):
        return async_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("rocky.http_tools.httpx.AsyncClient", mock_client)
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(HTTPToolError, match="HTTP request failed"):
            request_http(
                "https://example.com/report",
                {},
                tasks.external / "sessions",
                download_path=parent / "report.txt",
                download_directory_fd=descriptor,
            )
    finally:
        os.close(descriptor)

    assert list(parent.iterdir()) == []


def test_http_download_temporary_name_collision_preserves_existing_file(
    isolated_tasks, monkeypatch
):
    tasks = isolated_tasks
    temporary = tasks.own / ".rocky-http-collision"
    temporary.write_text("existing user file")
    async_client = httpx.AsyncClient

    def mock_client(**kwargs):
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"report"))
        return async_client(transport=transport, **kwargs)

    monkeypatch.setattr("rocky.http_tools.httpx.AsyncClient", mock_client)
    monkeypatch.setattr("rocky.http_tools.uuid.uuid4", lambda: SimpleNamespace(hex="collision"))
    descriptor = os.open(tasks.own, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(HTTPToolError):
            request_http(
                "https://example.com/report",
                {},
                tasks.external / "sessions",
                download_path=tasks.own / "report.txt",
                download_directory_fd=descriptor,
            )
    finally:
        os.close(descriptor)

    assert temporary.read_text() == "existing user file"
    assert not (tasks.own / "report.txt").exists()


def test_http_download_closes_descriptor_when_stream_creation_fails(isolated_tasks, monkeypatch):
    tasks = isolated_tasks
    parent = tasks.own / "download-parent"
    parent.mkdir()
    async_client = httpx.AsyncClient
    fdopen = os.fdopen
    download_descriptors = []

    def mock_client(**kwargs):
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"report"))
        return async_client(transport=transport, **kwargs)

    def interrupted_fdopen(descriptor, mode, *args, **kwargs):
        if mode == "wb":
            download_descriptors.append(descriptor)
            raise ValueError("cannot create fixture stream")
        return fdopen(descriptor, mode, *args, **kwargs)

    monkeypatch.setattr("rocky.http_tools.httpx.AsyncClient", mock_client)
    monkeypatch.setattr("rocky.http_tools.os.fdopen", interrupted_fdopen)
    directory_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(HTTPToolError):
            request_http(
                "https://example.com/report",
                {},
                tasks.external / "sessions",
                download_path=parent / "report.txt",
                download_directory_fd=directory_descriptor,
            )
    finally:
        os.close(directory_descriptor)

    assert len(download_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(download_descriptors[0])
    assert list(parent.iterdir()) == []


@pytest.fixture
def control_state_tasks(isolated_tasks):
    tasks = isolated_tasks
    directory = tasks.external / "control-state"
    directory.mkdir()
    credential = directory / "credentials.json"
    database = directory / "rocky.sqlite"
    targets = {"credential": credential, "database": database}
    targets.update(
        (suffix[1:], directory / (database.name + suffix))
        for suffix in ("-wal", "-shm", "-journal")
    )
    for name, target in targets.items():
        target.write_text(f"private {name} state")
    tasks.tools = WorkspaceTools(
        Settings(
            workspace_root=tasks.tools.root,
            tool_access="full",
            approval_mode="trusted",
            credential_path=credential,
            database_url=f"sqlite:///{database}",
        )
    )
    tasks.state_targets = targets
    return tasks


@pytest.mark.skipif(sys.platform != "darwin", reason="Casefold aliases require macOS")
@pytest.mark.parametrize("tool", ["read_file", "write_file", "append_file", "edit_file"])
@pytest.mark.parametrize("protected", ["credential", "database", "wal", "shm", "journal"])
def test_casefold_control_state_aliases_remain_protected(control_state_tasks, tool, protected):
    tasks = control_state_tasks
    target = tasks.state_targets[protected]
    alias = target.with_name(target.name.upper())
    if not alias.exists() or not alias.samefile(target):
        pytest.skip("The test volume has case-sensitive filenames")
    original = target.read_text()

    result = execute(tasks, file_action(tool, str(alias)))

    assert not result.success, result.content
    assert original not in result.content
    assert target.read_text() == original
