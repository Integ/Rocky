"""Leaving task isolation requires an exact owner grant under every policy."""

import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rocky import tools as tools_module
from rocky.api import create_app
from rocky.config import Settings
from rocky.models import (
    TOOL_NAMES,
    Action,
    Deliverable,
    GoalContract,
    PendingApproval,
    RunInput,
    RunState,
    ToolInput,
    ToolResult,
    WakeCommand,
)
from rocky.store import Store
from rocky.tool_contracts import approval_fingerprint
from rocky.tools import WorkspaceTools, _operation_fingerprint
from rocky.workflow import GoalWorkflow


def command_request(*, sandbox=True):
    return ToolInput(
        run=RunInput(
            goal_id="sandbox-approval",
            contract=GoalContract(
                objective="Run the authorized browser task",
                acceptance_criteria=["The requested result is verified"],
                deliverables=[Deliverable(path="report.txt")],
            ),
        ),
        operation_id="browser-operation",
        action=Action(tool="run_command", content="python browser.py", sandbox=sandbox),
    )


def test_legacy_actions_default_to_sandbox_and_schema_exposes_boolean():
    assert Action.model_validate({"tool": "run_command", "content": "pwd"}).sandbox is True
    assert Action.model_json_schema()["properties"]["sandbox"]["type"] == "boolean"


@pytest.mark.parametrize("value", [None, "false", "true", 0, 1, [], {}])
def test_sandbox_accepts_only_actual_boolean_values(value):
    with pytest.raises(ValidationError):
        Action(tool="run_command", content="pwd", sandbox=value)


@pytest.mark.parametrize("tool", [tool for tool in TOOL_NAMES if tool != "run_command"])
def test_only_commands_may_request_unsandboxed_execution(tmp_path, monkeypatch, tool):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path, approval_mode="trusted"))
    request = command_request(sandbox=False).model_copy(
        update={"action": Action(tool=tool, sandbox=False)}
    )

    def must_not_dispatch(*_args):
        pytest.fail("Invalid sandbox opt-outs cannot dispatch a tool")

    monkeypatch.setattr(tools, "_execute", must_not_dispatch)
    result = tools.execute(request)
    assert result.error_code == "invalid_arguments"
    assert "only supported by run_command" in result.content
    assert not list((tmp_path / ".rocky" / request.run.goal_id).glob("*.json"))


def test_default_sandbox_keeps_legacy_approval_fingerprint():
    request = command_request()
    legacy_payload = {
        "goal_id": request.run.goal_id,
        "operation_id": request.operation_id,
        "action": {
            "tool": "run_command",
            "path": None,
            "url": None,
            "content": "python browser.py",
        },
    }
    expected = hashlib.sha256(
        json.dumps(
            legacy_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    assert approval_fingerprint(request) == expected


def test_sandbox_choice_changes_approval_and_operation_fingerprints():
    assert approval_fingerprint(command_request()) != approval_fingerprint(
        command_request(sandbox=False)
    )
    assert _operation_fingerprint(command_request()) != _operation_fingerprint(
        command_request(sandbox=False)
    )


@pytest.mark.parametrize("approval_mode", ["required", "trusted"])
@pytest.mark.parametrize("prefix_source", ["none", "settings", "task"])
def test_unsandboxed_command_needs_exact_approval_even_when_prefix_is_granted(
    tmp_path, monkeypatch, approval_mode, prefix_source
):
    tools = WorkspaceTools(
        Settings(
            workspace_root=tmp_path,
            approval_mode=approval_mode,
            auto_approve_commands=["python"] if prefix_source == "settings" else [],
        )
    )
    request = command_request(sandbox=False)
    if prefix_source == "task":
        request.approved_command_prefixes = [["python"]]
    dispatched = []

    def dispatch(value, _goal_fd):
        dispatched.append(value)
        return ToolResult(success=True, content="browser completed")

    monkeypatch.setattr(tools, "_execute", dispatch)
    denial = tools.execute(request)
    assert denial.error_code == "approval_required"
    assert denial.approval_required == approval_fingerprint(request)
    assert denial.approval_command_prefix == []
    assert not tools._prefix_approved(request)
    assert dispatched == []
    assert not list((tmp_path / ".rocky" / request.run.goal_id).glob("*.json"))

    # A previous sandboxed grant cannot authorize dropping the sandbox.
    request.approved_action_fingerprints = [approval_fingerprint(command_request())]
    assert tools.execute(request).error_code == "approval_required"
    assert dispatched == []

    request.approved_action_fingerprints = [denial.approval_required]
    assert tools.execute(request).success
    assert len(dispatched) == 1
    # Receipts remain idempotent after the one-shot grant is consumed.
    request.approved_action_fingerprints = []
    assert tools.execute(request).success
    assert len(dispatched) == 1


@pytest.mark.parametrize("field", ["operation_id", "goal_id", "content", "path"])
def test_unsandboxed_grant_cannot_authorize_changed_action(tmp_path, monkeypatch, field):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path, approval_mode="trusted"))
    request = command_request(sandbox=False)
    request.approved_action_fingerprints = [approval_fingerprint(request)]
    if field == "operation_id":
        request.operation_id = "different-operation"
    elif field == "goal_id":
        request.run.goal_id = "other-task"
    else:
        setattr(request.action, field, "other-command" if field == "content" else "different")

    def must_not_dispatch(*_args):
        pytest.fail("A changed unsandboxed action requires new approval")

    monkeypatch.setattr(tools, "_execute", must_not_dispatch)
    assert tools.execute(request).error_code == "approval_required"


def test_workflow_rejects_prefix_grant_for_unsandboxed_pending_action():
    request = command_request(sandbox=False)
    workflow = GoalWorkflow(request.run)
    fingerprint = approval_fingerprint(request)
    # Even a malformed/stale reusable prefix cannot expand this one-shot grant.
    workflow._state.pending_approval = PendingApproval(
        fingerprint=fingerprint,
        operation_id=request.operation_id,
        action=request.action,
        reason="Run browser outside the sandbox",
        command_prefix=["python"],
    )
    workflow.approval_command(WakeCommand(command_id="task-grant", message="task:" + fingerprint))
    assert workflow._state.approved_command_prefixes == []
    assert workflow._approved_action == ""
    workflow.approval_command(WakeCommand(command_id="one-shot", message=fingerprint))
    assert workflow._approved_action == fingerprint
    assert workflow._state.approved_command_prefixes == []


def test_owner_api_rejects_task_scope_for_unsandboxed_action_even_with_stale_prefix(tmp_path):
    request = command_request(sandbox=False)
    store = Store(f"sqlite:///{tmp_path}/approvals.db")
    settings = Settings(auth_mode="token", api_token="sandbox-owner")
    headers = {"Authorization": "Bearer sandbox-owner"}
    fingerprint = approval_fingerprint(request)
    try:
        with TestClient(create_app(settings, store)) as client:
            store.create_goal(request.run.goal_id, request.run.contract)
            store.save_state(
                RunState(
                    goal_id=request.run.goal_id,
                    revision=1,
                    status="blocked",
                    pending_approval=PendingApproval(
                        fingerprint=fingerprint,
                        operation_id=request.operation_id,
                        action=request.action,
                        reason="Run browser outside Seatbelt",
                        command_prefix=["python"],
                    ),
                )
            )
            endpoint = f"/goals/{request.run.goal_id}/approve"
            assert client.post(endpoint, json={"fingerprint": fingerprint}).status_code == 401
            assert (
                client.post(
                    endpoint,
                    headers=headers,
                    json={"fingerprint": fingerprint, "scope": "task"},
                ).status_code
                == 409
            )
            assert store.pending_commands() == []
            assert (
                client.post(
                    endpoint, headers=headers, json={"fingerprint": fingerprint}
                ).status_code
                == 202
            )
            assert store.pending_commands()[0]["payload"] == fingerprint
    finally:
        store.close()


@pytest.mark.parametrize("tool", ["read_file", "write_file"])
@pytest.mark.parametrize(
    "filename", ["credentials.json", "rocky.db", "rocky.db-wal", "rocky.db-shm", "rocky.db-journal"]
)
@pytest.mark.parametrize("alias", [False, True])
def test_control_files_are_protected_outside_workspace_root(tmp_path, tool, filename, alias):
    controls = tmp_path / "private-control"
    controls.mkdir()
    target = controls / filename
    target.write_text("private task and credential state")
    tools = WorkspaceTools(
        Settings(
            workspace_root=tmp_path / "workspaces",
            credential_path=controls / "credentials.json",
            database_url=f"sqlite:///{controls / 'rocky.db'}",
        )
    )
    request = command_request()
    own = tools.root / request.run.goal_id
    own.mkdir()
    path = target
    if alias:
        path = own / "alias"
        path.symlink_to(target)
    request.action = Action(
        tool=tool, path=str(path), content="overwrite" if tool == "write_file" else None
    )
    result = tools.execute(request)
    assert not result.success
    assert "private task and credential state" not in result.content
    assert target.read_text() == "private task and credential state"


@pytest.mark.parametrize("tool", ["fetch_url", "http_request"])
@pytest.mark.parametrize("port", [18741, 18743])
def test_http_tools_cannot_access_configured_control_services(tmp_path, monkeypatch, tool, port):
    tools = WorkspaceTools(
        Settings(
            workspace_root=tmp_path / "workspaces",
            port=18741,
            temporal_address="127.0.0.1:18743",
        )
    )
    request = command_request()
    request.action = Action(tool=tool, url=f"http://127.0.0.1:{port}/goals")

    def must_not_open_session(*_args):
        pytest.fail("Control-service requests must be rejected before storage or dispatch")

    monkeypatch.setattr("rocky.http_tools._session", must_not_open_session)
    result = tools.execute(request)
    assert not result.success
    assert "control service ports" in result.content


def test_live_worker_code_and_python_runtime_roots_are_write_protected(tmp_path):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path / "workspaces"))
    assert {
        Path(tools_module.__file__).resolve().parent,
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
    } <= set(tools.protected_write_paths)


@pytest.mark.parametrize(
    "tool", ["write_file", "append_file", "edit_file", "fetch_url", "http_request"]
)
@pytest.mark.parametrize("location", ["code", "runtime"])
@pytest.mark.parametrize("alias", [False, True])
def test_file_mutations_cannot_change_worker_code_or_runtime(
    tmp_path, monkeypatch, tool, location, alias
):
    # Use temporary stand-ins so a regression cannot modify the running worker.
    protected = tmp_path / location
    protected.mkdir()
    target = protected / "module.py"
    original = "original worker code"
    target.write_text(original)
    tools = WorkspaceTools(
        Settings(workspace_root=tmp_path / "workspaces", approval_mode="trusted")
    )
    monkeypatch.setattr(tools, "protected_write_paths", (protected,))
    request = command_request()
    path = target
    if alias:
        own = tools.root / request.run.goal_id
        own.mkdir()
        path = own / "module-alias.py"
        path.symlink_to(target)
    if tool in {"fetch_url", "http_request"}:
        request.action = Action(tool=tool, path=str(path), url="https://example.test/module")
    else:
        request.action = Action(
            tool=tool,
            path=str(path),
            content=json.dumps({"old_text": "original", "new_text": "changed"})
            if tool == "edit_file"
            else "changed worker code",
        )

    def must_not_download(*_args, **_kwargs):
        pytest.fail("Downloads cannot dispatch to a protected worker-code destination")

    monkeypatch.setattr("rocky.tools.request_http", must_not_download)
    result = tools.execute(request)
    assert not result.success
    assert "cannot modify Rocky's code or Python runtime" in result.content
    assert target.read_text() == original


@pytest.mark.parametrize("tool", ["read_file", "list_files"])
@pytest.mark.parametrize("location", ["code", "runtime"])
def test_worker_code_and_runtime_remain_readable(tmp_path, monkeypatch, tool, location):
    protected = tmp_path / location
    protected.mkdir()
    target = protected / "module.py"
    target.write_text("inspectable worker code")
    tools = WorkspaceTools(Settings(workspace_root=tmp_path / "workspaces"))
    monkeypatch.setattr(tools, "protected_write_paths", (protected,))
    request = command_request()
    request.action = Action(tool=tool, path=str(target if tool == "read_file" else protected))
    result = tools.execute(request)
    assert result.success, result.content
    expected = "inspectable worker code" if tool == "read_file" else "module.py"
    assert expected in result.content
