"""Exercise broad tools with real local files, shell processes and HTTP sessions."""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from rocky.config import Settings
from rocky.models import Action, Deliverable, GoalContract, RunInput, ToolInput
from rocky.tool_contracts import approval_fingerprint
from rocky.tools import WorkspaceTools


def legacy_run():
    # This represents a goal submitted before full access became the default.
    return RunInput(
        goal_id="legacy-full-access",
        contract=GoalContract(
            objective="Process local materials and retrieve the requested account report",
            acceptance_criteria=["The requested result is saved"],
            deliverables=[Deliverable(path="report.txt")],
            allowed_tools=["read_file"],
            allowed_domains=[],
        ),
    )


def execute(tools, action, operation_id):
    return tools.execute(ToolInput(run=legacy_run(), action=action, operation_id=operation_id))


@pytest.fixture
def tools(tmp_path):
    return WorkspaceTools(Settings(workspace_root=tmp_path / "workspaces", approval_mode="trusted"))


def test_full_access_is_the_default_and_legacy_tool_scope_does_not_block_files(tools, tmp_path):
    assert Settings().tool_access == "full"
    outside = tmp_path / "outside" / ".account" / "report.txt"
    written = execute(
        tools,
        Action(tool="write_file", path=str(outside), content="account report: complete"),
        "absolute-write",
    )
    assert written.success, written.content
    assert outside.read_text() == "account report: complete"

    read = execute(tools, Action(tool="read_file", path=str(outside)), "absolute-read")
    assert read.success, read.content
    assert read.content == "account report: complete"

    listed = execute(
        tools, Action(tool="list_files", path=str(outside.parent.parent)), "absolute-list"
    )
    assert listed.success, listed.content
    assert ".account" in listed.content
    assert "report.txt" in listed.content


def test_full_access_follows_file_and_directory_symlinks(tools, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    original = outside / ".settings"
    original.write_text("before")
    goal_dir = tools.root / legacy_run().goal_id
    goal_dir.mkdir(parents=True)
    (goal_dir / "linked-folder").symlink_to(outside, target_is_directory=True)
    (goal_dir / "linked-file").symlink_to(original)

    read = execute(tools, Action(tool="read_file", path="linked-file"), "symlink-read")
    assert read.success, read.content
    assert read.content == "before"
    changed = execute(
        tools,
        Action(tool="write_file", path="linked-folder/.settings", content="after"),
        "symlink-write",
    )
    assert changed.success, changed.content
    assert original.read_text() == "after"


def test_full_access_reports_a_symlink_loop_without_aborting_the_activity(tools, tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    result = execute(tools, Action(tool="read_file", path=str(loop)), "symlink-loop")
    assert not result.success
    assert result.content


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt execution requires macOS")
def test_full_access_shell_uses_goal_directory_and_receipts_prevent_duplicate_work(tools):
    action = Action(
        tool="run_command",
        content="printf 'once\\n' >> invocations.txt; printf '%s\\n' \"$((19 + 23))\"",
    )
    first = execute(tools, action, "shell-once")
    assert first.success, first.content
    assert "42" in first.content
    invocations = tools.root / legacy_run().goal_id / "invocations.txt"
    assert invocations.read_text() == "once\n"

    recreated = WorkspaceTools(Settings(workspace_root=tools.root, approval_mode="trusted"))
    assert execute(recreated, action, "shell-once") == first
    assert invocations.read_text() == "once\n"
    conflict = execute(
        recreated,
        Action(tool="run_command", content="printf 'twice\\n' >> invocations.txt"),
        "shell-once",
    )
    assert not conflict.success
    assert invocations.read_text() == "once\n"


def test_external_working_directory_write_requires_approved_sandbox_opt_out(tools, tmp_path):
    cwd = tmp_path / "project"
    cwd.mkdir()
    action = Action(tool="run_command", path=str(cwd), content="printf 'prepared' > .build-result")
    confined = execute(tools, action, "shell-external-cwd")
    assert not confined.success
    assert not (cwd / ".build-result").exists()
    request = ToolInput(
        run=legacy_run(),
        operation_id="shell-external-cwd-approved",
        action=action.model_copy(update={"sandbox": False}),
    )
    pending = tools.execute(request)
    assert pending.error_code == "approval_required"
    request.approved_action_fingerprints = [approval_fingerprint(request)]
    result = tools.execute(request)
    assert result.success, result.content
    assert (cwd / ".build-result").read_text() == "prepared"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt execution requires macOS")
def test_distinct_silent_shell_commands_have_distinct_progress_keys(tools):
    first = execute(
        tools,
        Action(tool="run_command", content="printf 'first' > first.txt"),
        "silent-first",
    )
    second = execute(
        tools,
        Action(tool="run_command", content="printf 'second' > second.txt"),
        "silent-second",
    )
    assert first.success, first.content
    assert second.success, second.content
    assert first.content == second.content
    assert first.progress_key
    assert second.progress_key != first.progress_key
    assert (tools.root / legacy_run().goal_id / "first.txt").read_text() == "first"
    assert (tools.root / legacy_run().goal_id / "second.txt").read_text() == "second"


@pytest.fixture
def account_server():
    state = {"logins": 0, "report_reads": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self, status, text, *, cookie=None):
            body = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            if self.path != "/login" or payload != {"user": "fixture-user"}:
                self.respond(400, "invalid login fixture")
                return
            state["logins"] += 1
            self.respond(200, "logged in", cookie="fixture_session=authenticated; Path=/; HttpOnly")

        def do_GET(self):
            if self.path == "/public":
                self.respond(200, "local public report")
            elif self.path == "/report" and "fixture_session=authenticated" in self.headers.get(
                "Cookie", ""
            ):
                state["report_reads"] += 1
                self.respond(200, "authenticated account report")
            else:
                self.respond(401, "authentication required")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_full_access_fetch_can_reach_local_services_without_legacy_domain_allowlist(
    tools, account_server
):
    base_url, _ = account_server
    result = execute(tools, Action(tool="fetch_url", url=base_url + "/public"), "fetch-local")
    assert result.success, result.content
    assert "local public report" in result.content


def test_full_access_http_login_session_survives_tool_restart_and_post_retry(tools, account_server):
    base_url, state = account_server
    login = Action(
        tool="http_request",
        url=base_url + "/login",
        content=json.dumps(
            {"method": "POST", "json": {"user": "fixture-user"}, "session": "account"}
        ),
    )
    first = execute(tools, login, "login-once")
    assert first.success, first.content
    assert state["logins"] == 1
    assert "fixture_session=authenticated" not in first.content

    recreated = WorkspaceTools(Settings(workspace_root=tools.root, approval_mode="trusted"))
    assert execute(recreated, login, "login-once") == first
    assert state["logins"] == 1
    report = execute(
        recreated,
        Action(
            tool="http_request",
            url=base_url + "/report",
            content=json.dumps({"session": "account"}),
        ),
        "read-account-report",
    )
    assert report.success, report.content
    assert "authenticated account report" in report.content
    assert state["report_reads"] == 1

    other_session = execute(
        recreated,
        Action(
            tool="http_request",
            url=base_url + "/report",
            content=json.dumps({"session": "other-account"}),
        ),
        "other-account-report",
    )
    assert "authenticated account report" not in other_session.content
    assert state["report_reads"] == 1


@pytest.mark.parametrize(
    "tool",
    [
        pytest.param(
            "run_command",
            marks=pytest.mark.skipif(
                sys.platform != "darwin", reason="Seatbelt execution requires macOS"
            ),
        ),
        "http_request",
    ],
)
def test_interrupted_external_operation_is_not_repeated_automatically(
    tools, account_server, monkeypatch, tool
):
    base_url, state = account_server
    action = (
        Action(tool="run_command", content="printf 'once\\n' >> interrupted.txt")
        if tool == "run_command"
        else Action(
            tool="http_request",
            url=base_url + "/login",
            content=json.dumps({"method": "POST", "json": {"user": "fixture-user"}}),
        )
    )
    original_execute = tools._execute

    def interrupt_after_effect(*args):
        result = original_execute(*args)
        assert result.success, result.content
        raise RuntimeError("synthetic interruption before final receipt")

    monkeypatch.setattr(tools, "_execute", interrupt_after_effect)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        execute(tools, action, "interrupted-external-operation")

    recreated = WorkspaceTools(Settings(workspace_root=tools.root, approval_mode="trusted"))
    retry = execute(recreated, action, "interrupted-external-operation")
    assert not retry.success
    assert "interrupted" in retry.content
    assert "will not be repeated automatically" in retry.content
    if tool == "run_command":
        assert (tools.root / legacy_run().goal_id / "interrupted.txt").read_text() == "once\n"
        assert state["logins"] == 0
    else:
        assert state["logins"] == 1
