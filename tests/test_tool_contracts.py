"""Tool validation and owner approval must run before any dispatch side effect."""

import json

import pytest

from rocky.config import Settings
from rocky.http_tools import HTTPToolError
from rocky.models import Action, Deliverable, GoalContract, RunInput, ToolInput
from rocky.tool_contracts import approval_fingerprint
from rocky.tools import WorkspaceTools


def request(action, *, operation_id="operation-1", approved=()):
    return ToolInput(
        run=RunInput(
            goal_id="tool-contract",
            contract=GoalContract(
                objective="Inspect and prepare a report",
                acceptance_criteria=["The result is verified"],
                deliverables=[Deliverable(path="report.txt")],
            ),
        ),
        operation_id=operation_id,
        action=action,
        approved_action_fingerprints=list(approved),
    )


@pytest.mark.parametrize(
    "action",
    [
        Action(tool="read_file"),
        Action(tool="read_file", path="report.txt", content="ignored"),
        Action(tool="write_file", path="report.txt"),
        Action(tool="list_files", url="https://example.com"),
        Action(tool="run_command", content="\x00"),
        Action(tool="fetch_url", url="file:///etc/passwd"),
        Action(tool="http_request", url="https://example.com", content="[]"),
        Action(tool="http_request", url="https://example.com", content=""),
        Action(
            tool="http_request",
            url="https://example.com",
            content='{"method":"GET","method":"POST"}',
        ),
        Action(tool="http_request", url="https://example.com", content='{"timeout":NaN}'),
        Action(
            tool="http_request",
            url="https://example.com",
            content='{"method":"POST","unknown":"sensitive-secret"}',
        ),
        Action(
            tool="http_request",
            url="https://example.com",
            content='{"headers":{"Authorization":["sensitive-secret"]}}',
        ),
    ],
)
def test_invalid_action_returns_repair_feedback_without_dispatch(tmp_path, monkeypatch, action):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path))

    def dispatch(*_args):
        pytest.fail("Invalid requests cannot dispatch a tool")

    monkeypatch.setattr(tools, "_execute", dispatch)
    result = tools.execute(request(action))
    assert not result.success
    assert result.error_code == "invalid_arguments"
    assert result.retry_hint
    assert "sensitive-secret" not in result.content
    assert not list((tmp_path / ".rocky" / "tool-contract").glob("*.json"))


@pytest.mark.parametrize("tool", ["run_command", "http_request", "fetch_url"])
def test_mutating_calls_require_exact_owner_approval_before_dispatch(tmp_path, monkeypatch, tool):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path))
    action = (
        Action(tool="run_command", content="printf prepared > report.txt")
        if tool == "run_command"
        else Action(tool=tool, url="https://example.com/report", content='{"method":"POST"}')
    )
    dispatched = []

    def dispatch(value, _goal_fd):
        dispatched.append(value)
        from rocky.models import ToolResult

        return ToolResult(success=True, content="prepared")

    monkeypatch.setattr(tools, "_execute", dispatch)
    original = request(action)
    denial = tools.execute(original)
    assert denial.error_code == "approval_required"
    assert denial.approval_required == approval_fingerprint(original)
    assert dispatched == []
    assert not list((tmp_path / ".rocky" / "tool-contract").glob("*.json"))

    granted = original.model_copy(
        update={"approved_action_fingerprints": [denial.approval_required]}
    )
    assert tools.execute(granted).success
    assert len(dispatched) == 1
    # A completed receipt remains replayable without requiring another approval.
    assert tools.execute(original).success
    assert len(dispatched) == 1

    replay_elsewhere = granted.model_copy(update={"operation_id": "another-operation"})
    assert tools.execute(replay_elsewhere).error_code == "approval_required"
    changed = granted.model_copy(
        update={
            "operation_id": "changed",
            "action": Action(tool="run_command", content="printf changed"),
        }
    )
    assert tools.execute(changed).error_code == "approval_required"
    assert len(dispatched) == 1


def test_approval_cannot_be_reused_after_action_arguments_change(tmp_path, monkeypatch):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path))
    original = request(Action(tool="run_command", content="printf approved"))
    approved = approval_fingerprint(original)
    changed = original.model_copy(
        update={
            "action": Action(tool="run_command", content="printf changed"),
            "approved_action_fingerprints": [approved],
        }
    )
    assert tools.execute(changed).error_code == "approval_required"
    assert not list((tmp_path / ".rocky" / "tool-contract").glob("*.json"))


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_read_only_http_calls_need_no_approval(tmp_path, monkeypatch, method):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path))

    def read_http(*_args, **_kwargs):
        return {"status_code": 200, "text": "evidence"}

    monkeypatch.setattr("rocky.tools.request_http", read_http)
    result = tools.execute(
        request(
            Action(
                tool="http_request",
                url="https://example.com",
                content=json.dumps({"method": method}),
            )
        )
    )
    assert result.success
    assert not result.approval_required


def test_missing_path_reports_actionable_error(tmp_path):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path))
    result = tools.execute(request(Action(tool="read_file", path="missing.txt")))
    # Full-access read_file provides its own file-type error without attempting
    # an unsafe open; the retry hint still tells the planner what to inspect.
    assert not result.success
    assert result.retry_hint


def test_shell_failure_retains_exit_evidence_and_repair_hint(tmp_path):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path, approval_mode="trusted"))
    result = tools.execute(
        request(Action(tool="run_command", content="printf problem >&2; exit 7"))
    )
    assert not result.success
    assert result.error_code == "command_failed"
    assert "Exit code: 7" in result.content
    assert "problem" in result.content
    assert "stderr" in result.retry_hint


def test_mutating_http_timeout_reports_unknown_outcome_and_is_not_retried(tmp_path, monkeypatch):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path, approval_mode="trusted"))
    calls = []

    def interrupted(*_args, **_kwargs):
        calls.append(True)
        raise HTTPToolError("HTTP request timed out")

    monkeypatch.setattr("rocky.tools.request_http", interrupted)
    value = request(
        Action(tool="http_request", url="https://example.com", content='{"method":"POST"}')
    )
    result = tools.execute(value)
    assert result.error_code == "outcome_unknown"
    assert "Inspect actual external state" in result.retry_hint
    assert tools.execute(value) == result
    assert calls == [True]


def test_new_lifetime_policy_does_not_invalidate_an_existing_tool_receipt(tmp_path):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path))
    value = request(Action(tool="write_file", path="report.txt", content="original"))
    result = tools.execute(value)
    assert result.success
    value.run.contract.limits.max_steps = 2
    (tmp_path / "tool-contract" / "report.txt").write_text("newer")
    assert tools.execute(value) == result
    assert (tmp_path / "tool-contract" / "report.txt").read_text() == "newer"
