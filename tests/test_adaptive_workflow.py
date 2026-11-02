"""Exercise the production decision loop, approvals and replay with Temporal."""

import asyncio
import sys
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer
from test_workflow import Activities, act, finish, request
from test_workflow import temporal_env as _temporal_env

from rocky.config import Settings
from rocky.models import (
    Action,
    Decision,
    ExecutionLimits,
    PlanInput,
    PlanResult,
    TaskProgress,
    ToolInput,
    ToolResult,
    WakeCommand,
)
from rocky.service import goal_workers
from rocky.tools import WorkspaceTools
from rocky.workflow import GoalWorkflow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]
temporal_env = _temporal_env


@dataclass
class InteractiveLaneActivities(Activities):
    owner_id: str = ""
    busy_id: str = ""
    busy_started: asyncio.Event = field(default_factory=asyncio.Event)
    busy_gate: asyncio.Event = field(default_factory=asyncio.Event)
    owner_resumed: asyncio.Event = field(default_factory=asyncio.Event)
    owner_requests: list[tuple[bool, str]] = field(default_factory=list)

    @activity.defn(name="rocky.plan")
    async def plan(self, request: PlanInput) -> PlanResult:
        if request.run.goal_id == self.busy_id:
            self.busy_started.set()
            await self.busy_gate.wait()
            return PlanResult(decision=finish())
        self.owner_requests.append((request.interactive, activity.info().task_queue))
        count = len(self.owner_requests)
        if count == 1:
            return PlanResult(decision=Decision(kind="blocked", reason="Need an answer"))
        if count == 2:
            self.owner_resumed.set()
            return PlanResult(decision=act())
        return PlanResult(decision=finish())


async def test_owner_input_uses_reserved_lane_while_normal_model_worker_is_full(temporal_env):
    owner, busy = request(), request()
    queue = owner.goal_id
    owner.model_task_queue = busy.model_task_queue = queue + "-model"
    activities = InteractiveLaneActivities(owner_id=owner.goal_id, busy_id=busy.goal_id)
    async with goal_workers(
        temporal_env.client, queue, activities.registered(), model_slots=1, max_cached_workflows=0
    ):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, owner, id=owner.goal_id, task_queue=queue
        )
        await asyncio.wait_for(activities.blocked.wait(), 15)
        other = await temporal_env.client.start_workflow(
            GoalWorkflow.run, busy, id=busy.goal_id, task_queue=queue
        )
        try:
            await asyncio.wait_for(activities.busy_started.wait(), 15)
            await handle.signal(GoalWorkflow.wake, "Here is the missing information")
            await asyncio.wait_for(activities.owner_resumed.wait(), 15)
            assert activities.owner_requests[:2] == [
                (False, queue + "-model"),
                (True, queue + "-model-interactive"),
            ]
        finally:
            activities.busy_gate.set()
        assert (await handle.result()).status == "succeeded"
        assert (await other.result()).status == "succeeded"
        assert activities.owner_requests[-1] == (False, queue + "-model")
    replayer = Replayer(workflows=[GoalWorkflow], data_converter=pydantic_data_converter)
    await asyncio.wait_for(replayer.replay_workflow(await handle.fetch_history()), timeout=15)


def worker(env, run, activities, workflow_type=GoalWorkflow):
    return goal_workers(
        env.client,
        run.goal_id,
        activities.registered(),
        workflows=[workflow_type],
        # Worker replacement in the approval test must replay durable history.
        max_cached_workflows=0,
    )


async def test_default_loop_records_actual_work_without_six_phase_ceremony(temporal_env):
    activities = Activities(decisions=[act(), finish()])
    run = request()
    async with worker(temporal_env, run, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "succeeded"
    assert activities.phases == []
    steps = {
        state.execution_step.step_id: state.execution_step
        for state in activities.snapshots
        if state.execution_step is not None
    }
    assert [item.phase for item in steps.values()] == ["think", "act", "think", "complete"]
    assert all(item.status == "completed" and item.completed_at for item in steps.values())
    assert len(activities.tools) == activities.verifications == 1
    assert result.model_calls == 3


async def test_only_explicitly_registered_checkpoints_dispatch_hooks(temporal_env):
    activities = Activities(decisions=[act(), finish()])
    run = request()
    run.hook_phases = ["think", "reflect"]
    async with worker(temporal_env, run, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "succeeded"
    assert [item.phase for item in activities.phases] == ["think", "reflect"] * 2


@pytest.mark.parametrize("phase", ["perceive", "think", "decide", "act", "reflect", "complete"])
async def test_observer_failures_become_visible_recovery_and_blocking(temporal_env, phase):
    activities = Activities(
        decisions=([act()] * 2 if phase == "act" else [finish()] * 2),
        failing_phases={phase},
    )
    run = request(max_no_progress=1)
    run.hook_phases = [phase]
    async with worker(temporal_env, run, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        state = await handle.query(GoalWorkflow.state)
        assert state.status == "blocked"
        assert state.recovery_attempts == 1
        assert "Recovery" in state.reason
        assert not any(item.status == "succeeded" for item in activities.snapshots)
        await handle.signal(GoalWorkflow.cancel)
        result = await asyncio.wait_for(handle.result(), timeout=15)
    assert result.status == "cancelled"
    if phase in {"perceive", "think"}:
        assert not activities.plans


async def test_verification_usage_is_checked_before_claiming_success(temporal_env):
    activities = Activities(decisions=[finish()])
    # Planning consumes 19 tokens, so review is allowed. Its additional eight
    # tokens take the task over the cap and must be reflected in the result.
    run = request(limits=ExecutionLimits(max_tokens=20))
    async with worker(temporal_env, run, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "budget_exhausted"
    assert activities.verifications == 1
    assert result.input_tokens + result.output_tokens == 27


async def test_fallback_handoff_refreshes_until_a_model_summary_is_supplied(temporal_env):
    activities = Activities(
        decisions=[
            Decision(
                kind="act",
                reason="Read the first source",
                action=Action(tool="read_file", path="first.txt"),
            ),
            Decision(
                kind="act",
                reason="Read the second source",
                action=Action(tool="read_file", path="second.txt"),
            ),
            finish(),
        ]
    )
    run = request()
    async with worker(temporal_env, run, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "succeeded"
    assert "first.txt" in activities.plans[1].state.handoff_summary
    assert "second.txt" in activities.plans[2].state.handoff_summary
    assert not activities.plans[2].state.handoff_is_model


@pytest.mark.parametrize(
    "limits,decisions,status,steps,calls,tools,verifications",
    [
        (ExecutionLimits(max_steps=1), [act(), finish()], "budget_exhausted", 1, 1, 1, 0),
        (ExecutionLimits(max_model_calls=1), [finish()], "budget_exhausted", 1, 1, 0, 0),
        (ExecutionLimits(max_tokens=10), [act()], "budget_exhausted", 1, 1, 0, 0),
        (
            ExecutionLimits(timeout_seconds=1),
            [Decision(kind="wait", reason="Wait for a remote result", wait_seconds=100)],
            "deadline_exceeded",
            1,
            1,
            0,
            0,
        ),
    ],
)
async def test_owner_limits_stop_before_more_work(
    temporal_env, limits, decisions, status, steps, calls, tools, verifications
):
    activities = Activities(decisions=decisions)
    run = request(limits=limits)
    async with worker(temporal_env, run, activities):
        result = await asyncio.wait_for(
            temporal_env.client.execute_workflow(
                GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
            ),
            timeout=20,
        )
    assert result.status == status
    assert (result.steps, result.model_calls) == (steps, calls)
    assert len(activities.tools) == tools
    assert activities.verifications == verifications
    assert activities.snapshots[-1].status == status
    assert result.reason


@dataclass
class ApprovalActivities(Activities):
    workspace_tools: WorkspaceTools | None = None

    @activity.defn(name="rocky.execute")
    async def execute(self, value: ToolInput) -> ToolResult:
        self.tools.append(value)
        assert self.workspace_tools is not None
        return await asyncio.to_thread(self.workspace_tools.execute, value)


@dataclass
class RepairActivities(Activities):
    @activity.defn(name="rocky.execute")
    async def execute(self, value: ToolInput) -> ToolResult:
        self.tools.append(value)
        return ToolResult(
            success=False,
            content="Command exited without producing the report.",
            error_code="missing_dependency",
            retry_hint="Install the missing fixture dependency before retrying.",
        )


async def test_tool_error_code_and_repair_hint_reach_the_next_model_turn(temporal_env):
    activities = RepairActivities(decisions=[act(), finish()])
    run = request()
    async with worker(temporal_env, run, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "succeeded"
    feedback = activities.plans[1].feedback
    observation = activities.plans[1].state.observations[-1].content
    for text in (feedback, observation):
        assert "missing_dependency" in text
        assert "Install the missing fixture dependency" in text


async def test_waiting_for_approval_pauses_the_task_timeout(temporal_env, tmp_path):
    settings = Settings(workspace_root=tmp_path / "approval-timeout")
    run = request(limits=ExecutionLimits(timeout_seconds=5))
    activities = ApprovalActivities(
        decisions=[
            Decision(
                kind="act",
                reason="Prepare result",
                action=Action(tool="run_command", content="printf prepared > mutation.txt"),
            )
        ],
        workspace_tools=WorkspaceTools(settings),
    )
    async with worker(temporal_env, run, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        # Far past the 5-second limit, but the clock stops while the owner decides.
        await temporal_env.sleep(120)
        state = await handle.query(GoalWorkflow.state)
        assert state.status == "blocked" and state.pending_approval is not None
        assert state.wake_at == state.pending_approval.expires_at
        await handle.signal(
            GoalWorkflow.approval_command,
            WakeCommand(command_id="deny-1", message="deny:" + state.pending_approval.fingerprint),
        )
        result = await asyncio.wait_for(handle.result(), timeout=20)
    assert result.status == "succeeded"
    assert len(activities.tools) == 1
    assert activities.verifications == 1
    assert not (settings.workspace_root / run.goal_id / "mutation.txt").exists()


async def test_blocked_question_pauses_the_task_timeout_without_replanning(temporal_env):
    activities = Activities(decisions=[Decision(kind="blocked", reason="Missing source")])
    run = request(limits=ExecutionLimits(timeout_seconds=5))
    async with worker(temporal_env, run, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        await temporal_env.sleep(120)
        state = await handle.query(GoalWorkflow.state)
        assert state.status == "blocked" and state.wake_at is None
        assert len(activities.plans) == state.model_calls == 1
        await handle.signal(GoalWorkflow.wake, "The source is attached")
        result = await asyncio.wait_for(handle.result(), timeout=20)
    assert result.status == "succeeded"
    assert not activities.tools
    assert activities.plans[1].feedback == "The source is attached"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt execution requires macOS")
async def test_exact_owner_approval_survives_worker_replacement_and_wake_cannot_grant_it(
    temporal_env, tmp_path
):
    settings = Settings(workspace_root=tmp_path / "workspaces")
    run = request()
    action = Action(tool="run_command", content="printf prepared > mutation.txt")
    first = ApprovalActivities(
        decisions=[Decision(kind="act", reason="Prepare the requested result", action=action)] * 2,
        workspace_tools=WorkspaceTools(settings),
    )
    destination = settings.workspace_root / run.goal_id / "mutation.txt"
    async with worker(temporal_env, run, first):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(first.blocked.wait(), timeout=15)
        state = await handle.query(GoalWorkflow.state)
        assert state.pending_approval is not None
        fingerprint = state.pending_approval.fingerprint
        assert state.pending_approval.action == action
        assert state.pending_approval.operation_id == first.tools[0].operation_id
        assert not destination.exists()
        first.plan_gate = asyncio.Event()
        first.plan_started.clear()
        await handle.signal(GoalWorkflow.wake, fingerprint)
        await asyncio.wait_for(first.plan_started.wait(), timeout=15)
        unchanged = await handle.query(GoalWorkflow.state)
        assert unchanged.pending_approval is None
        assert unchanged.model_calls == 2
        assert len(first.tools) == 1
        assert not destination.exists()
        first.plan_gate.set()

        async def renewed_request():
            while True:
                current = await handle.query(GoalWorkflow.state)
                if current.pending_approval and current.steps == 2:
                    return current.pending_approval
                await asyncio.sleep(0.01)

        fingerprint = (await asyncio.wait_for(renewed_request(), timeout=15)).fingerprint

    replacement = ApprovalActivities(decisions=[finish()], workspace_tools=WorkspaceTools(settings))
    async with worker(temporal_env, run, replacement):
        await handle.signal(
            GoalWorkflow.approval_command,
            WakeCommand(command_id="wrong", message="0" * 64),
        )
        unchanged = await handle.query(GoalWorkflow.state)
        assert unchanged.pending_approval.fingerprint == fingerprint
        assert not replacement.plans
        assert not replacement.tools
        assert not destination.exists()
        await handle.signal(
            GoalWorkflow.approval_command,
            WakeCommand(command_id="approve-exact-operation", message=fingerprint),
        )
        result = await asyncio.wait_for(handle.result(), timeout=20)
    assert result.status == "succeeded"
    assert result.pending_approval is None
    assert destination.read_text() == "prepared"
    assert len(replacement.tools) == 1
    assert replacement.tools[0].operation_id == first.tools[-1].operation_id
    assert replacement.tools[0].action == action
    assert replacement.tools[0].approved_action_fingerprints == [fingerprint]
    assert len(first.plans) == 2 and len(replacement.plans) == 1
    replayer = Replayer(workflows=[GoalWorkflow], data_converter=pydantic_data_converter)
    await asyncio.wait_for(replayer.replay_workflow(await handle.fetch_history()), timeout=15)


async def test_model_handoff_and_progress_survive_actions_and_continue_as_new(temporal_env):
    progress = TaskProgress(
        completed=["Located primary sources"],
        pending=["Compare evidence and verify the final report"],
        evidence=["sources/primary.txt contains the retrieved evidence"],
    )
    summary = "Primary sources located; use their dates to resolve contradictions."
    decisions = [
        Decision(
            kind="act",
            reason="Inspect the next independent source",
            action=Action(tool="read_file", path=f"sources/source-{index}.txt"),
            handoff_summary=summary if index == 0 else None,
            task_progress=progress if index == 0 else None,
        )
        for index in range(8)
    ]
    activities = Activities(decisions=[*decisions, finish()])
    run = request()
    async with worker(temporal_env, run, activities):
        result = await asyncio.wait_for(
            temporal_env.client.execute_workflow(
                GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
            ),
            timeout=30,
        )
    assert result.status == "succeeded"
    assert result.handoff_summary == summary
    assert result.task_progress == progress
    assert len(set(activities.plan_run_ids)) == 2
    assert activities.plan_run_ids[7] != activities.plan_run_ids[8]
    for planned in activities.plans[1:]:
        assert planned.state.handoff_summary == summary
        assert planned.state.task_progress == progress
    assert activities.plans[-1].run.checkpoint is None
    assert len(result.observations) <= 6
    assert result.steps == 9
    assert result.model_calls == 10


@dataclass
class BatchActivities(Activities):
    """Each read waits until the whole batch is in flight: serial dispatch deadlocks."""

    batch_size: int = 0
    in_flight: int = 0
    all_started: asyncio.Event = field(default_factory=asyncio.Event)

    @activity.defn(name="rocky.execute")
    async def execute(self, value: ToolInput) -> ToolResult:
        self.tools.append(value)
        self.in_flight += 1
        if self.in_flight >= self.batch_size:
            self.all_started.set()
        await asyncio.wait_for(self.all_started.wait(), timeout=10)
        return ToolResult(
            success=True, content=f"contents of {value.action.path}", progress_key=value.action.path
        )


async def test_independent_reads_run_concurrently_in_one_step(temporal_env):
    reads = [Action(tool="read_file", path=f"src/module_{index}.py") for index in range(3)]
    batch = [*reads, Action(tool="list_files", path="tests")]
    activities = BatchActivities(
        decisions=[Decision(kind="act", reason="Inspect the project", actions=batch), finish()],
        batch_size=len(batch),
    )
    run = request()
    async with worker(temporal_env, run, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        result = await asyncio.wait_for(handle.result(), timeout=20)
    assert result.status == "succeeded"
    # Activities start in any order; each keeps its own operation identifier.
    dispatched = {item.operation_id: item.action for item in activities.tools}
    assert dispatched == {f"step-0001-{index}": action for index, action in enumerate(batch, 1)}
    # One planning call gathered four observations.
    assert (result.steps, result.model_calls) == (2, 3)
    observed = activities.plans[1].state.observations
    assert [item.tool for item in observed] == ["read_file"] * 3 + ["list_files"]
    assert observed[0].content == "contents of src/module_0.py"
    assert "4 parallel reads" in activities.plans[1].state.handoff_summary
    replayer = Replayer(workflows=[GoalWorkflow], data_converter=pydantic_data_converter)
    await asyncio.wait_for(replayer.replay_workflow(await handle.fetch_history()), timeout=15)


@pytest.mark.parametrize(
    "decision",
    [
        Decision(
            kind="act",
            reason="Read and write together",
            actions=[
                Action(tool="read_file", path="a.txt"),
                Action(tool="write_file", path="b.txt", content="x"),
            ],
        ),
        Decision(
            kind="act",
            reason="Both forms",
            action=Action(tool="read_file", path="a.txt"),
            actions=[Action(tool="read_file", path="b.txt")],
        ),
        Decision(
            kind="act",
            reason="Batch a mutation",
            actions=[Action(tool="http_request", url="https://e.com", content='{"method":"PUT"}')],
        ),
    ],
)
async def test_batches_with_mutations_are_rejected_before_any_dispatch(temporal_env, decision):
    activities = Activities(decisions=[decision, finish()])
    run = request()
    async with worker(temporal_env, run, activities):
        result = await asyncio.wait_for(
            temporal_env.client.execute_workflow(
                GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
            ),
            timeout=20,
        )
    assert result.status == "succeeded"
    assert not activities.tools
    assert "action" in activities.plans[1].feedback


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt execution requires macOS")
async def test_task_scoped_approval_covers_later_similar_commands_only(temporal_env, tmp_path):
    settings = Settings(workspace_root=tmp_path / "task-approval")
    run = request()
    decisions = [
        Decision(
            kind="act",
            reason="Create the first file",
            action=Action(tool="run_command", content="touch first.txt"),
        ),
        Decision(
            kind="act",
            reason="Create the second file",
            action=Action(tool="run_command", content="touch second.txt"),
        ),
        Decision(
            kind="act",
            reason="Chain commands",
            action=Action(tool="run_command", content="touch third.txt; touch fourth.txt"),
        ),
    ]
    activities = ApprovalActivities(decisions=decisions, workspace_tools=WorkspaceTools(settings))
    activities.decisions.insert(0, decisions[0])
    workspace = settings.workspace_root / run.goal_id
    async with worker(temporal_env, run, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        state = await handle.query(GoalWorkflow.state)
        pending = state.pending_approval
        assert pending.command_prefix == ["touch"]
        # A wake message or an unscoped mismatch cannot grant the prefix.
        activities.plan_gate = asyncio.Event()
        activities.plan_started.clear()
        await handle.signal(GoalWorkflow.wake, "task:" + pending.fingerprint)
        await handle.signal(
            GoalWorkflow.approval_command, WakeCommand(command_id="bad", message="all:" + "0" * 64)
        )
        assert (await handle.query(GoalWorkflow.state)).approved_command_prefixes == []
        await asyncio.wait_for(activities.plan_started.wait(), timeout=15)
        activities.plan_gate.set()

        async def renewed_request():
            while True:
                current = await handle.query(GoalWorkflow.state)
                if current.pending_approval and current.steps == 2:
                    return current.pending_approval
                await asyncio.sleep(0.01)

        pending = await asyncio.wait_for(renewed_request(), timeout=15)
        await handle.signal(
            GoalWorkflow.approval_command,
            WakeCommand(command_id="task-grant", message="task:" + pending.fingerprint),
        )

        async def chained_pending():
            while True:
                current = await handle.query(GoalWorkflow.state)
                if current.pending_approval and current.steps == 4:
                    return current
                await asyncio.sleep(0.05)

        chained = await asyncio.wait_for(chained_pending(), timeout=15)
        assert chained.approved_command_prefixes == [["touch"]]
        assert chained.pending_approval.command_prefix is None
        await handle.signal(GoalWorkflow.cancel)
        result = await asyncio.wait_for(handle.result(), timeout=15)
    assert result.status == "cancelled"
    assert (workspace / "first.txt").exists() and (workspace / "second.txt").exists()
    assert not (workspace / "third.txt").exists()
    # Only the first command and the chained command paused for review.
    paused = {
        state.pending_approval.operation_id
        for state in activities.snapshots
        if state.pending_approval
    }
    assert paused == {"step-0001", "step-0002", "step-0004"}
    replayer = Replayer(workflows=[GoalWorkflow], data_converter=pydantic_data_converter)
    await asyncio.wait_for(replayer.replay_workflow(await handle.fetch_history()), timeout=15)


@dataclass
class RateLimitedActivities(Activities):
    attempts: int = 0
    failure_type: str = "ModelTransientError"
    failure_usage: dict = field(default_factory=dict)

    @activity.defn(name="rocky.plan")
    async def plan(self, value: PlanInput) -> PlanResult:
        self.attempts += 1
        if self.attempts == 1:
            raise ApplicationError("rate limited", self.failure_usage, type=self.failure_type)
        self.plans.append(value)
        return PlanResult(decision=finish(), input_tokens=12, output_tokens=7)


async def test_rate_limited_planning_is_retried_without_spending_a_step(temporal_env):
    activities = RateLimitedActivities()
    run = request()
    async with worker(temporal_env, run, activities):
        result = await asyncio.wait_for(
            temporal_env.client.execute_workflow(
                GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
            ),
            timeout=30,
        )
    assert result.status == "succeeded"
    assert activities.attempts == 2
    assert (result.steps, result.model_calls) == (1, 2)


async def test_unusable_model_responses_are_never_repeated(temporal_env):
    activities = RateLimitedActivities(failure_type="ModelDecisionError")
    run = request()
    async with worker(temporal_env, run, activities):
        result = await asyncio.wait_for(
            temporal_env.client.execute_workflow(
                GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
            ),
            timeout=30,
        )
    assert result.status == "succeeded"
    # The failed response became feedback for a new planning step.
    assert activities.attempts == 2
    assert result.steps == 2
    assert activities.plans[0].feedback == "rate limited"


async def test_reported_failed_response_usage_survives_temporal_serialization(temporal_env):
    activities = RateLimitedActivities(
        failure_type="ModelDecisionError",
        failure_usage={"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 50},
    )
    run = request(limits=ExecutionLimits(max_tokens=100))
    async with worker(temporal_env, run, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert activities.attempts == 1 and activities.verifications == 0
    assert result.status == "budget_exhausted"
    assert (result.input_tokens, result.output_tokens, result.cached_input_tokens) == (100, 10, 50)


async def test_empty_blocked_reason_enters_bounded_recovery(temporal_env):
    activities = Activities(decisions=[Decision(kind="blocked", reason="")] * 2)
    run = request(max_no_progress=1)
    async with worker(temporal_env, run, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        state = await handle.query(GoalWorkflow.state)
        assert state.status == "blocked" and "missing information" in state.reason
        assert state.model_calls == 2 and not activities.tools
        await handle.signal(GoalWorkflow.cancel)
        assert (await handle.result()).status == "cancelled"
    replayer = Replayer(workflows=[GoalWorkflow], data_converter=pydantic_data_converter)
    await asyncio.wait_for(replayer.replay_workflow(await handle.fetch_history()), timeout=15)


@dataclass
class ReadActivities(Activities):
    """Each path has stable content, so reading it again returns identical evidence."""

    @activity.defn(name="rocky.execute")
    async def execute(self, value: ToolInput) -> ToolResult:
        self.tools.append(value)
        return ToolResult(
            success=True,
            content=f"contents of {value.action.path}",
            progress_key=f"read:{value.action.path}",
        )


def read(path: str) -> Decision:
    return Decision(kind="act", reason=f"Read {path}", action=Action(tool="read_file", path=path))


def recovered(activities: Activities) -> list[int]:
    return [
        index for index, plan in enumerate(activities.plans) if "Recovery required" in plan.feedback
    ]


async def test_rereading_evidence_that_left_the_recent_window_is_not_a_stall(temporal_env):
    write = Decision(
        kind="act",
        reason="Write the combined report",
        action=Action(tool="write_file", path="report.md", content="# Report"),
    )
    activities = ReadActivities(
        decisions=[read("a.txt"), read("b.txt"), read("a.txt"), read("b.txt"), write, finish()]
    )
    # Two stalls would enter recovery: the earlier rule counted both re-reads.
    run = request(max_no_progress=2)
    async with worker(temporal_env, run, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        result = await asyncio.wait_for(handle.result(), timeout=20)
    assert result.status == "succeeded"
    assert len(activities.plans) == 6
    assert recovered(activities) == []
    assert all(plan.state.recovery_attempts == 0 for plan in activities.plans)
    replayer = Replayer(workflows=[GoalWorkflow], data_converter=pydantic_data_converter)
    await asyncio.wait_for(replayer.replay_workflow(await handle.fetch_history()), timeout=15)


async def test_immediate_and_repeated_rereads_still_trigger_recovery(temporal_env):
    activities = ReadActivities(
        decisions=[
            read("a.txt"),
            read("b.txt"),
            read("a.txt"),  # refresh: neutral
            read("b.txt"),  # refresh: neutral
            read("b.txt"),  # the planner just saw this result: stall 1
            read("a.txt"),  # second refresh of the same evidence: stall 2
            finish(),
        ]
    )
    run = request(max_no_progress=2)
    async with worker(temporal_env, run, activities):
        result = await asyncio.wait_for(
            temporal_env.client.execute_workflow(
                GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
            ),
            timeout=20,
        )
    assert result.status == "succeeded"
    assert recovered(activities) == [6]
    assert activities.plans[6].state.recovery_attempts == 1


async def test_refresh_allowance_survives_continue_as_new(temporal_env):
    sources = [read(f"s{index}.txt") for index in range(6)]
    activities = ReadActivities(
        decisions=[
            *sources,
            read("s0.txt"),  # cycle 7: refresh
            read("s6.txt"),  # cycle 8: new evidence; the next cycle starts a new run
            read("s6.txt"),  # immediate repeat across the run boundary: stall 1
            read("s0.txt"),  # already refreshed in the previous run: stall 2
            finish(),
        ]
    )
    run = request(max_no_progress=2)
    async with worker(temporal_env, run, activities):
        result = await asyncio.wait_for(
            temporal_env.client.execute_workflow(
                GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
            ),
            timeout=30,
        )
    assert result.status == "succeeded"
    assert activities.plan_run_ids[7] != activities.plan_run_ids[8]
    assert recovered(activities) == [10]


async def test_batched_rereads_follow_the_refresh_rule(temporal_env):
    again = Decision(
        kind="act",
        reason="Re-read both sources",
        actions=[Action(tool="read_file", path="a.txt"), Action(tool="read_file", path="b.txt")],
    )
    # The first batch refreshes a.txt (b.txt was just seen); repeating the same
    # batch right away re-reads what the planner just saw and is a stall.
    activities = ReadActivities(decisions=[read("a.txt"), read("b.txt"), again, again, finish()])
    run = request(max_no_progress=1)
    async with worker(temporal_env, run, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        result = await asyncio.wait_for(handle.result(), timeout=20)
    assert result.status == "succeeded"
    assert recovered(activities) == [4]
    replayer = Replayer(workflows=[GoalWorkflow], data_converter=pydantic_data_converter)
    await asyncio.wait_for(replayer.replay_workflow(await handle.fetch_history()), timeout=15)


async def test_structured_question_survives_worker_replacement_and_replay(temporal_env):
    from rocky.models import OwnerQuestion

    activities = Activities(
        decisions=[
            Decision(
                kind="blocked",
                reason="Need the reporting year",
                question=OwnerQuestion(text="Which year?", options=["2025", "2026"]),
            ),
            finish(),
        ]
    )
    run = request()
    async with worker(temporal_env, run, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        state = await handle.query(GoalWorkflow.state)
        assert state.pending_question.text == "Which year?"
        question_id = state.pending_question.question_id
    async with worker(temporal_env, run, activities):
        state = await handle.query(GoalWorkflow.state)
        assert state.pending_question.question_id == question_id
        await handle.signal(
            GoalWorkflow.wake_command, WakeCommand(command_id="answer-1", message="2026")
        )
        result = await asyncio.wait_for(handle.result(), timeout=20)
    assert result.status == "succeeded" and result.pending_question is None
    assert activities.plans[-1].feedback == "2026"
    replayer = Replayer(workflows=[GoalWorkflow], data_converter=pydantic_data_converter)
    await asyncio.wait_for(replayer.replay_workflow(await handle.fetch_history()), timeout=15)


async def test_owner_message_during_model_call_replans_before_dispatch(temporal_env):
    activities = Activities(decisions=[act(), finish()], plan_gate=asyncio.Event())
    run = request()
    async with worker(temporal_env, run, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.plan_started.wait(), timeout=15)
        await handle.signal(
            GoalWorkflow.wake_command,
            WakeCommand(command_id="steering-1", message="Use the new figures"),
        )
        activities.plan_gate.set()
        result = await asyncio.wait_for(handle.result(), timeout=20)
    assert result.status == "succeeded" and not activities.tools
    assert activities.plans[-1].feedback == "Use the new figures"
    replayer = Replayer(workflows=[GoalWorkflow], data_converter=pydantic_data_converter)
    await asyncio.wait_for(replayer.replay_workflow(await handle.fetch_history()), timeout=15)
