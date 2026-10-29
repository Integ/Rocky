"""Tiered approval: read-only commands, owner rules, batch answers and timeouts."""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rocky.api import create_app
from rocky.approval_policy import ApprovalPolicy, ApprovalRule, ApprovalSettings
from rocky.command_policy import read_only_command
from rocky.config import Settings
from rocky.models import (
    Action,
    Decision,
    Deliverable,
    GoalContract,
    HttpScope,
    PendingApproval,
    PlanResult,
    RunCheckpoint,
    RunInput,
    RunState,
    ToolInput,
    ToolResult,
    WakeCommand,
)
from rocky.store import Store
from rocky.tool_contracts import action_identity, approval_fingerprint
from rocky.tools import WorkspaceTools
from rocky.workflow import GoalWorkflow

CONTRACT = GoalContract(
    objective="Produce a report",
    acceptance_criteria=["Contains verified facts"],
    deliverables=[Deliverable(path="report.md")],
)
AUTH = {"Authorization": "Bearer tier-test-token"}


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "cat README.md",
        "grep -rn 'TODO*' src",
        "rg --max-count=3 approval src",
        "find . -name '*.py' -type f",
        "head -n 20 notes.txt",
        "tail -n 5 log.txt",
        "sort -k2 data.csv",
        "wc -l a.txt b.txt",
        "du -sh .",
        "jq .name package.json",
    ],
)
def test_read_only_catalog_accepts_plain_reads(command):
    assert read_only_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "ls; rm -rf data",
        "cat a > b",
        "cat $HOME/.ssh/id_rsa",
        "ls `whoami`",
        "ls *",
        "cat ~/notes",
        "find . -name x -exec rm {} ;",
        "find . -delete",
        "find . -fprint out.txt",
        "rg --pre ./decode src",
        "rg --pre=./decode src",
        "sort -o out.txt data.csv",
        "sort -uo out.txt data.csv",
        "sort --outp=out.txt data.csv",
        "tail -f log.txt",
        "tail --follow log.txt",
        "tree -o listing.txt",
        "git status",
        "git diff",
        "sed -i s/a/b/ file",
        "awk '{system(\"id\")}' file",
        "./ls",
        "/tmp/ls",
        "python -c 'print(1)'",
        "env ls",
        "",
    ],
)
def test_read_only_catalog_rejects_writes_execution_and_expansion(command):
    assert not read_only_command(command)


def command_input(content, *, sandbox=True, goal_id="tiers", operation_id="step-0001"):
    return ToolInput(
        run=RunInput(goal_id=goal_id, contract=CONTRACT),
        operation_id=operation_id,
        action=Action(tool="run_command", content=content, sandbox=sandbox),
    )


def http_input(url, method="POST", operation_id="step-0001"):
    return ToolInput(
        run=RunInput(goal_id="tiers", contract=CONTRACT),
        operation_id=operation_id,
        action=Action(tool="http_request", url=url, content=json.dumps({"method": method})),
    )


@pytest.fixture
def tools(tmp_path, monkeypatch):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path / "workspaces"))
    dispatched = []

    def dispatch(request, _goal_fd):
        dispatched.append(request)
        return ToolResult(success=True, content="done")

    monkeypatch.setattr(tools, "_execute", dispatch)
    tools.dispatched = dispatched
    return tools


def test_read_only_commands_run_in_the_sandbox_without_approval(tools):
    result = tools.execute(command_input("ls -la"))
    assert result.success and result.approved_by == "read_only"
    assert len(tools.dispatched) == 1


def test_read_only_tier_never_covers_unsandboxed_or_disabled_runs(tools):
    unsandboxed = tools.execute(command_input("ls -la", sandbox=False))
    assert unsandboxed.error_code == "approval_required"
    disabled = ApprovalPolicy(settings=ApprovalSettings(read_only_commands=False))
    gated = tools.execute(command_input("ls -la", operation_id="step-0002"), disabled)
    assert gated.error_code == "approval_required"
    assert gated.approval_timeout_seconds == 30 * 60
    assert gated.approval_command_prefix == ["ls"]
    assert tools.dispatched == []


def test_read_only_program_must_not_resolve_inside_a_workspace(tools, monkeypatch):
    planted = tools.root / "tiers"
    planted.mkdir(parents=True)
    fake = planted / "cat"
    fake.write_text("#!/bin/sh\necho planted\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{planted}{os.pathsep}{os.environ['PATH']}")
    assert tools.execute(command_input("cat notes.txt")).error_code == "approval_required"
    assert tools.dispatched == []


def test_timeout_setting_is_reported_with_each_request(tools):
    never = ApprovalPolicy(settings=ApprovalSettings(timeout_minutes=0))
    result = tools.execute(command_input("uv run pytest"), never)
    assert result.error_code == "approval_required"
    assert result.approval_timeout_seconds == 0


def test_owner_rules_cover_command_prefixes_and_http_origins(tools):
    policy = ApprovalPolicy(
        rules=[
            ApprovalRule(rule_id="tests", kind="command", command="uv run pytest"),
            ApprovalRule(
                rule_id="api",
                kind="http",
                methods=["post", "put"],
                origin="https://API.example.com:443",
                path_prefix="/v1/",
            ),
        ]
    )
    assert tools.execute(command_input("uv run pytest -q"), policy).approved_by == "rule:tests"
    allowed = tools.execute(
        http_input("https://api.example.com/v1/items", "POST", operation_id="step-0002"), policy
    )
    assert allowed.approved_by == "rule:api"
    for operation, request in enumerate(
        [
            command_input("uv run pytest -q; curl evil.test"),
            command_input("uv run pytest", sandbox=False),
            http_input("https://api.example.com/v1/items", "DELETE"),
            http_input("https://api.example.com/v2/items"),
            http_input("https://api.example.com/v1/../admin"),
            http_input("https://api.example.com/v1/%2e%2e/admin"),
            http_input("http://api.example.com/v1/items"),
            http_input("https://api.example.com.evil.test/v1/items"),
        ],
        start=3,
    ):
        request.operation_id = f"step-{operation:04d}"
        assert tools.execute(request, policy).error_code == "approval_required", request
    assert len(tools.dispatched) == 2


@pytest.mark.parametrize(
    "origin",
    ["https://*.example.com", "https://example.com/path", "https://user:pw@example.com", "ftp://x"],
)
def test_http_rules_need_an_exact_origin(origin):
    with pytest.raises(ValidationError):
        ApprovalRule(kind="http", methods=["POST"], origin=origin)


def test_http_rules_ignore_read_methods_and_need_a_mutating_one():
    with pytest.raises(ValidationError):
        ApprovalRule(kind="http", methods=["GET", "HEAD"], origin="https://example.com")
    with pytest.raises(ValidationError):
        ApprovalRule(kind="command", command="sudo rm")


def test_task_http_scope_and_deferred_preapproval(tools):
    request = http_input("https://hooks.example.com/notify")
    denied = tools.execute(request)
    assert denied.approval_http_scope == HttpScope(
        method="POST", origin="https://hooks.example.com"
    )
    request.approved_http_scopes = [denied.approval_http_scope]
    assert tools.execute(request).approved_by == "task"

    later = command_input("curl -X POST https://example.com", operation_id="step-0009")
    assert tools.execute(later).error_code == "approval_required"
    later.preapproved_actions = [action_identity("tiers", later.action)]
    result = tools.execute(later)
    assert result.success
    assert result.approved_by == "deferred:" + action_identity("tiers", later.action)


@pytest.fixture
def control(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/tiers.db")
    settings = Settings(auth_mode="token", api_token="tier-test-token")
    with TestClient(create_app(settings, store)) as client:
        store.create_goal("task", CONTRACT)
        store.create_goal("other", CONTRACT)
        yield client, store
    store.close()


def pending(fingerprint, action, **fields):
    return PendingApproval(
        fingerprint=fingerprint, operation_id="step-1", action=action, reason="review", **fields
    )


def test_always_approval_saves_a_rule_and_policy_is_editable(control):
    client, store = control
    command = Action(tool="run_command", content="uv run pytest -q")
    store.save_state(
        RunState(
            goal_id="task",
            revision=1,
            status="blocked",
            pending_approval=pending("a" * 64, command, command_prefix=["uv", "run", "pytest"]),
        )
    )
    answer = client.post(
        "/goals/task/approve", headers=AUTH, json={"fingerprint": "a" * 64, "scope": "always"}
    )
    assert answer.status_code == 202
    assert store.pending_commands()[-1]["payload"] == "always:" + "a" * 64
    policy = client.get("/approval-policy", headers=AUTH).json()
    assert [rule["command"] for rule in policy["rules"]] == ["uv run pytest"]
    assert policy["settings"] == {"read_only_commands": True, "timeout_minutes": 30}

    http = client.post(
        "/approval-rules",
        headers=AUTH,
        json={"kind": "http", "methods": ["POST"], "origin": "https://api.example.com"},
    )
    assert http.status_code == 201
    rule_id = http.json()["rule_id"]
    assert rule_id and http.json()["created_at"]
    # The same rule is not stored twice.
    again = client.post(
        "/approval-rules",
        headers=AUTH,
        json={"kind": "http", "methods": ["post"], "origin": "https://api.example.com/"},
    )
    assert again.json()["rule_id"] == rule_id
    assert (
        client.post(
            "/approval-rules", headers=AUTH, json={"kind": "command", "command": "ls | sh"}
        ).status_code
        == 422
    )
    updated = client.put(
        "/approval-policy", headers=AUTH, json={"read_only_commands": False, "timeout_minutes": 0}
    )
    assert updated.json()["settings"] == {"read_only_commands": False, "timeout_minutes": 0}
    policy = store.approval_policy(ApprovalPolicy())
    assert not policy.settings.read_only_commands and len(policy.rules) == 2
    assert client.delete(f"/approval-rules/{rule_id}", headers=AUTH).status_code == 204
    assert client.delete(f"/approval-rules/{rule_id}", headers=AUTH).status_code == 404
    assert client.get("/approval-policy").status_code == 401


def test_batch_answers_pending_and_declined_requests_across_tasks(control):
    client, store = control
    post = Action(tool="http_request", url="https://api.example.com/x", content='{"method":"POST"}')
    store.save_state(
        RunState(
            goal_id="task",
            revision=1,
            status="blocked",
            pending_approval=pending(
                "a" * 64,
                post,
                http_scope=HttpScope(method="POST", origin="https://api.example.com"),
            ),
        )
    )
    declined = Action(tool="run_command", content="make deploy")
    store.save_state(
        RunState(
            goal_id="other",
            revision=1,
            status="running",
            deferred_approvals=[pending("b" * 64, declined)],
        )
    )
    listed = client.get("/approvals", headers=AUTH).json()
    assert {(item["goal_id"], item["status"]) for item in listed} == {
        ("task", "pending"),
        ("other", "deferred"),
    }
    answer = client.post(
        "/approvals/batch",
        headers=AUTH,
        json={
            "items": [
                {"goal_id": "task", "fingerprint": "a" * 64, "scope": "task"},
                {"goal_id": "other", "fingerprint": "b" * 64, "scope": "deferred"},
                {"goal_id": "other", "fingerprint": "c" * 64, "scope": "deferred"},
                {"goal_id": "missing", "fingerprint": "a" * 64},
            ]
        },
    )
    statuses = [item["status"] for item in answer.json()["results"]]
    assert statuses == ["queued", "queued", "error", "error"]
    assert [item["payload"] for item in store.pending_commands()] == [
        "task:" + "a" * 64,
        "deferred:" + "b" * 64,
    ]
    # A pending request can be declined, but a declined one cannot be "denied".
    assert (
        client.post(
            "/goals/task/approve", headers=AUTH, json={"fingerprint": "a" * 64, "scope": "deny"}
        ).status_code
        == 202
    )
    assert (
        client.post(
            "/goals/other/approve", headers=AUTH, json={"fingerprint": "b" * 64, "scope": "deny"}
        ).status_code
        == 409
    )


class Paused(Exception):
    pass


def offline_goal(monkeypatch, decisions, results, *, waits, state=None):
    """Run the workflow loop with scripted Activities and approval waits."""
    run = RunInput(
        goal_id="tiers",
        contract=CONTRACT,
        checkpoint=RunCheckpoint(state=state) if state else None,
    )
    goal = GoalWorkflow(run)
    clock = [datetime(2026, 10, 29, tzinfo=UTC)]
    executed, snapshots = [], []

    async def dispatch(name, value, **kwargs):
        if name == "rocky.persist":
            snapshots.append(value.model_copy(deep=True))
        elif name == "rocky.plan":
            if not decisions:
                raise Paused
            return PlanResult(decision=decisions.pop(0))
        elif name == "rocky.execute":
            executed.append(value)
            return results.pop(0)
        else:
            pytest.fail(name)

    async def wait_condition(predicate, timeout=None):
        if not waits:
            raise Paused
        answer = waits.pop(0)
        if answer is None:
            clock[0] += timeout
            raise TimeoutError
        answer(goal)
        assert predicate()

    monkeypatch.setattr("rocky.workflow.workflow.now", lambda: clock[0])
    monkeypatch.setattr("rocky.workflow.workflow.execute_activity", dispatch)
    monkeypatch.setattr("rocky.workflow.workflow.wait_condition", wait_condition)
    monkeypatch.setattr("rocky.workflow.workflow.patched", lambda _: True)
    monkeypatch.setattr(goal, "_should_continue_as_new", lambda: False)
    return run, goal, executed, snapshots


def act(content):
    return Decision(kind="act", reason="Run it", action=Action(tool="run_command", content=content))


def gated(fingerprint, timeout=1800):
    return ToolResult(
        success=False,
        content="Owner approval is required",
        error_code="approval_required",
        approval_required=fingerprint,
        approval_timeout_seconds=timeout,
    )


async def test_unanswered_request_is_declined_and_kept_for_later(monkeypatch):
    run, goal, executed, snapshots = offline_goal(
        monkeypatch,
        [act("make deploy"), act("make check")],
        [gated("a" * 64), ToolResult(success=True, content="checked")],
        waits=[None],
    )
    with pytest.raises(Paused):
        await goal.run(run)
    waiting = next(state for state in snapshots if state.pending_approval)
    assert waiting.status == "blocked"
    assert (
        waiting.pending_approval.expires_at
        == (datetime(2026, 10, 29, tzinfo=UTC) + timedelta(minutes=30)).isoformat()
    )
    state = goal.state()
    assert state.pending_approval is None
    assert [item.fingerprint for item in state.deferred_approvals] == ["a" * 64]
    assert state.approval_timeouts == 1
    timed_out = [item for item in state.observations if "approval_timeout" in item.content]
    assert timed_out and "not dispatched" in timed_out[0].content
    # The declined action was never dispatched a second time.
    assert [item.action.content for item in executed] == ["make deploy", "make check"]

    # Approving it later lets the task repeat that exact action once.
    goal.approval_command(WakeCommand(command_id="c1", message="deferred:" + "a" * 64))
    state = goal.state()
    assert state.deferred_approvals == []
    assert state.preapproved_actions == [action_identity("tiers", act("make deploy").action)]
    assert "make deploy" in goal._take_wake_feedback()


async def test_owner_denial_declines_without_deferring(monkeypatch):
    def deny(goal):
        goal.approval_command(WakeCommand(command_id="c1", message="deny:" + "a" * 64))

    run, goal, executed, _ = offline_goal(
        monkeypatch, [act("make deploy")], [gated("a" * 64)], waits=[deny]
    )
    with pytest.raises(Paused):
        await goal.run(run)
    state = goal.state()
    assert state.deferred_approvals == [] and state.approval_timeouts == 0
    assert any("approval_denied" in item.content for item in state.observations)
    assert len(executed) == 1


async def test_repeated_timeouts_wait_for_owner_input(monkeypatch):
    run, goal, executed, snapshots = offline_goal(
        monkeypatch,
        [act("make a"), act("make b"), act("make c")],
        [gated("a" * 64, 600), gated("b" * 64, 600), gated("c" * 64, 600)],
        waits=[None, None, None],
    )
    with pytest.raises(Paused):
        await goal.run(run)
    blocked = snapshots[-1]
    assert blocked.status == "blocked" and blocked.pending_approval is None
    assert "3 approval requests in a row went unanswered" in blocked.reason
    assert len(blocked.deferred_approvals) == 3


async def test_task_approval_of_http_request_grants_its_origin(monkeypatch):
    scope = HttpScope(method="POST", origin="https://hooks.example.com")
    post = Action(
        tool="http_request", url="https://hooks.example.com/a", content='{"method":"POST"}'
    )

    def approve(goal):
        goal.approval_command(WakeCommand(command_id="c1", message="task:" + "a" * 64))

    run, goal, executed, _ = offline_goal(
        monkeypatch,
        [Decision(kind="act", reason="Notify", action=post)],
        [
            ToolResult(
                success=False,
                content="Owner approval is required",
                error_code="approval_required",
                approval_required="a" * 64,
                approval_http_scope=scope,
            ),
            ToolResult(success=True, content="sent", approved_by="owner"),
        ],
        waits=[approve],
    )
    with pytest.raises(Paused):
        await goal.run(run)
    assert goal.state().approved_http_scopes == [scope]
    assert executed[-1].approved_http_scopes == [scope]
    assert executed[-1].approved_action_fingerprints == ["a" * 64]


def test_approval_fingerprint_still_binds_the_operation():
    first = command_input("make deploy")
    second = command_input("make deploy", operation_id="step-0002")
    assert approval_fingerprint(first) != approval_fingerprint(second)
    assert action_identity("tiers", first.action) == action_identity("tiers", second.action)


async def test_a_deferred_approval_is_used_once(monkeypatch):
    identity = action_identity("tiers", act("make deploy").action)
    run, goal, executed, _ = offline_goal(
        monkeypatch,
        [act("make deploy")],
        [ToolResult(success=True, content="deployed", approved_by="deferred:" + identity)],
        waits=[],
        state=RunState(goal_id="tiers", status="running", preapproved_actions=[identity, "other"]),
    )
    with pytest.raises(Paused):
        await goal.run(run)
    assert executed[0].preapproved_actions == [identity, "other"]
    assert goal.state().preapproved_actions == ["other"]
