"""File batches reduce planning rounds without widening tool permissions."""

import asyncio
import json
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Replayer
from test_adaptive_workflow import ApprovalActivities, worker
from test_planner import completed, fake_client, goal_request
from test_workflow import Activities, finish, request
from test_workflow import temporal_env as _temporal_env

from rocky.config import Settings
from rocky.models import Action, Artifact, Decision, ToolInput, ToolResult, workspace_file_batch
from rocky.planner import OpenAIPlanner
from rocky.tools import WorkspaceTools
from rocky.workflow import GoalWorkflow

temporal_env = _temporal_env


def batch(*paths):
    return Decision(
        kind="act",
        reason="Create independent files",
        actions=[Action(tool="write_file", path=path, content=path) for path in paths],
    )


@pytest.mark.parametrize(
    "paths",
    [
        ["a", "./a"],
        ["a", "a/b"],
        ["a/b", "a"],
        ["a", "A"],
        ["é.txt", "e\u0301.txt"],
        ["../a"],
        ["/tmp/a"],
        ["."],
        ["a\\b"],
        ["a\x00b"],
    ],
)
def test_rejects_unsafe_or_overlapping_paths(paths):
    assert not workspace_file_batch(batch(*paths).actions)


@pytest.mark.parametrize(
    "changes",
    [
        {"tool": "run_command"},
        {"tool": "read_file"},
        {"url": "https://example.com"},
        {"sandbox": False},
        {"background": False},
        {"timeout_seconds": 2},
        {"content": None},
    ],
)
def test_rejects_mixed_tools_and_command_options(changes):
    actions = batch("a", "b").actions
    actions[1] = actions[1].model_copy(update=changes)
    assert not workspace_file_batch(actions)


def test_workspace_file_batches_require_version_capability():
    decision = batch("a", "b")
    assert workspace_file_batch(decision.actions)
    assert GoalWorkflow._decision_error(request(), decision)
    assert not GoalWorkflow._decision_error(request(), decision, file_batches=True)
    assert not workspace_file_batch([])
    assert not workspace_file_batch(
        batch(*map(str, range(8))).actions + [Action(tool="write_file", path="9", content="x")]
    )


@pytest.mark.parametrize("enabled", [False, True])
async def test_planner_advertises_only_workflow_supported_file_batches(enabled):
    client = fake_client(completed(finish()))
    plan = goal_request()
    plan.file_batches = enabled
    await OpenAIPlanner(Settings(), client=client).plan(plan)
    payload = json.loads(client.responses.parse.await_args.kwargs["input"][1]["content"])
    assert payload["execution"]["file_batch_tools"] == (
        ["write_file", "edit_file", "append_file"] if enabled else []
    )


integration = pytest.mark.integration
session = pytest.mark.asyncio(loop_scope="session")


@integration
@session
async def test_real_file_batch_keeps_successes_and_artifacts_when_one_edit_fails(
    temporal_env, tmp_path
):
    run = request()
    settings = Settings(workspace_root=tmp_path)
    decision = batch("first.txt", "missing.txt", "third.txt", "fourth.txt")
    decision.actions[1] = Action(
        tool="edit_file", path="missing.txt", content='{"old_text":"old","new_text":"new"}'
    )
    acts = ApprovalActivities(
        decisions=[decision, finish()], workspace_tools=WorkspaceTools(settings)
    )
    async with worker(temporal_env, run, acts):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        result = await handle.result()
    assert (
        result.status == "succeeded"
    )  # Fake reviewer; real tools above, no completion claim from it.
    assert len(acts.plans) == 2 and result.model_calls == 3
    assert all(plan.file_batches for plan in acts.plans)
    assert [item.operation_id for item in acts.tools] == [f"step-0001-{i}" for i in range(1, 5)]
    assert len(acts.plans[1].state.artifacts) == 3
    assert "missing.txt" in acts.plans[1].feedback
    for path in ["first.txt", "third.txt", "fourth.txt"]:
        assert (tmp_path / run.goal_id / path).read_text() == path
    snapshots = [s for s in acts.snapshots if s.execution_step and s.execution_step.phase == "act"]
    assert any(
        s.execution_step.status == "started"
        and len(s.execution_step.output.get("results", [])) == 1
        for s in snapshots
    )
    # Production operation receipts make replay of the completed append/write idempotent.
    original = acts.tools[0]
    assert acts.workspace_tools.execute(original).success
    assert (tmp_path / run.goal_id / "first.txt").read_text() == "first.txt"
    await Replayer(
        workflows=[GoalWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(await handle.fetch_history())


@dataclass
class GatedFiles(Activities):
    first_started: asyncio.Event = field(default_factory=asyncio.Event)
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    first_result: ToolResult = field(
        default_factory=lambda: ToolResult(
            success=True,
            content="saved",
            artifacts=[Artifact(path="first.txt", size=1, sha256="abc")],
        )
    )

    @activity.defn(name="rocky.execute")
    async def execute(self, value: ToolInput) -> ToolResult:
        self.tools.append(value)
        self.first_started.set()
        await self.gate.wait()
        return self.first_result


@integration
@session
@pytest.mark.parametrize("interrupt", ["owner", "cancel", "approval", "unknown"])
async def test_file_batch_stops_remaining_writes_and_keeps_completed_progress(
    temporal_env, interrupt
):
    run = request()
    acts = GatedFiles(decisions=[batch("first.txt", "second.txt"), finish()])
    if interrupt == "approval":
        acts.first_result = ToolResult(
            success=False, content="needs approval", approval_required="file-approval"
        )
    elif interrupt == "unknown":
        acts.first_result = ToolResult(
            success=False,
            content="unknown",
            error_code="outcome_unknown",
            retry_hint="Inspect first.txt",
        )
    async with worker(temporal_env, run, acts):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(acts.first_started.wait(), 15)
        if interrupt == "owner":
            await handle.signal(GoalWorkflow.wake, "Change the remaining work")
        elif interrupt == "cancel":
            await handle.signal(GoalWorkflow.cancel)
        acts.gate.set()
        result = await asyncio.wait_for(handle.result(), 15)
    assert len(acts.tools) == 1
    if interrupt == "cancel":
        assert result.status == "cancelled"
        assert result.artifacts[0].path == "first.txt"
        assert len(result.execution_step.output["results"]) == 1
    else:
        assert result.status == "succeeded"
        assert any("batch_stopped" in item.content for item in acts.plans[1].state.observations)
        if interrupt == "approval":
            assert "approval_required" in acts.plans[1].feedback
            assert not result.pending_approval
    await Replayer(
        workflows=[GoalWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(await handle.fetch_history())


@dataclass
class PauseSecondFile(ApprovalActivities):
    second_started: asyncio.Event = field(default_factory=asyncio.Event)
    gate: asyncio.Event = field(default_factory=asyncio.Event)

    @activity.defn(name="rocky.execute")
    async def execute(self, value: ToolInput) -> ToolResult:
        if value.action.path == "second.txt":
            self.second_started.set()
            await self.gate.wait()
        return await super().execute(value)


@integration
@session
async def test_worker_replacement_resumes_remaining_file_without_repeating_append(
    temporal_env, tmp_path
):
    run = request()
    settings = Settings(workspace_root=tmp_path)
    decision = batch("first.txt", "second.txt")
    decision.actions[0].tool = "append_file"
    first = PauseSecondFile(decisions=[decision], workspace_tools=WorkspaceTools(settings))
    async with worker(temporal_env, run, first):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(first.second_started.wait(), 15)
        state = await handle.query(GoalWorkflow.state)
        assert len(state.execution_step.output["results"]) == 1
    replacement = ApprovalActivities(decisions=[finish()], workspace_tools=WorkspaceTools(settings))
    async with worker(temporal_env, run, replacement):
        result = await asyncio.wait_for(handle.result(), 20)
    assert result.status == "succeeded"
    assert [item.action.path for item in replacement.tools] == ["second.txt"]
    assert (tmp_path / run.goal_id / "first.txt").read_text() == "first.txt"
    assert (tmp_path / run.goal_id / "second.txt").read_text() == "second.txt"
    assert replacement.plans[0].state.artifacts[0].path == "first.txt"
    await Replayer(
        workflows=[GoalWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(await handle.fetch_history())


@integration
@session
async def test_relative_symlink_cannot_bypass_existing_file_approval(temporal_env, tmp_path):
    run = request()
    workspace = tmp_path / "workspaces"
    folder = workspace / run.goal_id
    folder.mkdir(parents=True)
    external = tmp_path / "external.txt"
    external.write_text("owner content")
    (folder / "alias.txt").symlink_to(external)
    acts = ApprovalActivities(
        decisions=[batch("alias.txt", "later.txt"), finish()],
        workspace_tools=WorkspaceTools(Settings(workspace_root=workspace)),
    )
    async with worker(temporal_env, run, acts):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "succeeded"
    assert len(acts.tools) == 1
    assert "approval_required" in acts.plans[1].feedback
    assert external.read_text() == "owner content"
    assert not (folder / "later.txt").exists()
