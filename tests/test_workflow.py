"""Integration tests against a real Temporal service, with fake external work.

The model and tools are Activities in these tests, not mocked Workflow APIs.
No OpenAI requests are made. Set ROCKY_SKIP_TEMPORAL_TESTS=1 only when deliberately
running an offline unit-test subset; service startup failures otherwise fail.
"""

import asyncio
import os
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from rocky.models import (
    Action,
    Decision,
    Deliverable,
    GoalContract,
    PlanInput,
    PlanResult,
    RunInput,
    RunState,
    ToolInput,
    ToolResult,
    VerificationResult,
    VerifyInput,
    WakeCommand,
)
from rocky.workflow import GoalWorkflow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def temporal_env(tmp_path_factory):
    if os.environ.get("ROCKY_SKIP_TEMPORAL_TESTS") == "1":
        pytest.skip("Temporal integration tests explicitly disabled by the operator")
    download_dir = str(tmp_path_factory.mktemp("temporal-binaries"))
    if os.environ.get("ROCKY_TEMPORAL_TEST_MODE") == "local":
        env = await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter,
            download_dest_dir=download_dir,
        )
    else:
        env = await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter,
            download_dest_dir=download_dir,
            test_server_existing_path=os.environ.get("ROCKY_TEMPORAL_TEST_SERVER"),
        )
    try:
        yield env
    finally:
        await env.shutdown()


@dataclass
class Activities:
    decisions: list[Decision] = field(default_factory=list)
    plans: list[PlanInput] = field(default_factory=list)
    tools: list[ToolInput] = field(default_factory=list)
    snapshots: list[RunState] = field(default_factory=list)
    verifications: int = 0
    verification_passed: bool = True
    tool_success: bool = True
    waiting: asyncio.Event = field(default_factory=asyncio.Event)
    wait_steps: asyncio.Queue = field(default_factory=asyncio.Queue)
    blocked: asyncio.Event = field(default_factory=asyncio.Event)
    blocked_persist_gate: asyncio.Event | None = None
    plan_started: asyncio.Event = field(default_factory=asyncio.Event)
    plan_gate: asyncio.Event | None = None
    reservation_reached: asyncio.Event = field(default_factory=asyncio.Event)
    reservation_gate: asyncio.Event | None = None
    cancel_in_plan: bool = False
    tool_failures: int = 0

    @activity.defn(name="rocky.plan")
    async def plan(self, request: PlanInput) -> PlanResult:
        self.plans.append(request)
        self.plan_started.set()
        if self.plan_gate is not None:
            await self.plan_gate.wait()
        if self.cancel_in_plan:
            raise ApplicationError(
                "Cancellation requested", type="GoalCancelled", non_retryable=True
            )
        index = len(self.plans) - 1
        decision = self.decisions[index] if index < len(self.decisions) else finish()
        return PlanResult(decision=decision, input_tokens=12, output_tokens=7)

    @activity.defn(name="rocky.execute")
    async def execute(self, request: ToolInput) -> ToolResult:
        self.tools.append(request)
        if len(self.tools) <= self.tool_failures:
            raise ApplicationError("Temporary tool failure", type="Transient")
        return ToolResult(
            success=self.tool_success,
            content="observed output" if self.tool_success else "No usable source available",
            progress_key="same-content" if self.tool_success else "",
        )

    @activity.defn(name="rocky.verify")
    async def verify(self, request: VerifyInput) -> VerificationResult:
        self.verifications += 1
        return VerificationResult(
            passed=self.verification_passed,
            feedback="Deliverables verified"
            if self.verification_passed
            else "Missing required evidence",
            input_tokens=5,
            output_tokens=3,
        )

    @activity.defn(name="rocky.persist")
    async def persist(self, state: RunState) -> None:
        self.snapshots.append(state.model_copy(deep=True))
        if state.status == "waiting":
            self.waiting.set()
            self.wait_steps.put_nowait(state.steps)
        if state.status == "blocked":
            self.blocked.set()
            if self.blocked_persist_gate is not None:
                await self.blocked_persist_gate.wait()
        if self.reservation_gate is not None and state.model_calls == 1:
            self.reservation_reached.set()
            await self.reservation_gate.wait()

    def registered(self):
        return [self.plan, self.execute, self.verify, self.persist]


def finish() -> Decision:
    return Decision(kind="finish", reason="Ready for independent verification")


def act() -> Decision:
    return Decision(kind="act", reason="Inspect current state", action=Action(tool="list_files"))


def request(**overrides) -> RunInput:
    fields = {
        "objective": "Produce a checked report",
        "acceptance_criteria": ["The report contains evidence"],
        "deliverables": [Deliverable(path="report.md")],
        "max_steps": 10,
        "max_model_calls": 10,
    }
    fields.update(overrides)
    return RunInput(goal_id=uuid4().hex, contract=GoalContract(**fields))


def worker(env, queue: str, activities: Activities) -> Worker:
    return Worker(
        env.client,
        task_queue=queue,
        workflows=[GoalWorkflow],
        activities=activities.registered(),
    )


async def test_goal_requires_independent_verification(temporal_env):
    activities = Activities(decisions=[act(), finish()])
    run = request()
    async with worker(temporal_env, run.goal_id, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "succeeded"
    assert result.steps == 2
    assert result.model_calls == 3
    assert (result.input_tokens, result.output_tokens) == (29, 17)
    assert len(activities.tools) == activities.verifications == 1
    assert activities.snapshots[-1].status == "succeeded"


async def test_verification_is_included_in_model_call_budget(temporal_env):
    activities = Activities(decisions=[finish()])
    run = request(max_model_calls=1)
    async with worker(temporal_env, run.goal_id, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "budget_exhausted"
    assert result.model_calls == 1
    assert activities.verifications == 0


async def test_wait_is_durable_and_can_be_woken(temporal_env):
    activities = Activities(
        decisions=[
            Decision(kind="wait", reason="Await an external event", wait_seconds=3600),
            finish(),
        ]
    )
    run = request()
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.waiting.wait(), timeout=15)
        waiting_state = await handle.query(GoalWorkflow.state)
        assert waiting_state.status == "waiting"
        assert waiting_state.model_calls == 1
        assert waiting_state.wake_at is not None
        await handle.signal(GoalWorkflow.wake, "The requested file is available")
        result = await handle.result()
    assert result.status == "succeeded"
    assert "requested file" in activities.plans[1].feedback


async def test_cancel_while_waiting_stops_further_work(temporal_env):
    activities = Activities(
        decisions=[Decision(kind="wait", reason="Await an event", wait_seconds=3600)]
    )
    run = request()
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.waiting.wait(), timeout=15)
        await handle.signal(GoalWorkflow.cancel)
        result = await handle.result()
    assert result.status == "cancelled"
    assert len(activities.plans) == 1
    assert not activities.tools
    assert activities.verifications == 0


async def test_wall_clock_deadline_includes_waits(temporal_env):
    activities = Activities(
        decisions=[
            Decision(kind="wait", reason="Wait longer than the goal deadline", wait_seconds=100)
        ]
    )
    run = request(deadline_seconds=10)
    async with worker(temporal_env, run.goal_id, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "deadline_exceeded"
    assert result.model_calls == 1


async def test_repeated_identical_results_stop_without_infinite_replanning(temporal_env):
    activities = Activities(decisions=[act(), act(), act(), act()])
    run = request(max_no_progress=2)
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        blocked = await handle.query(GoalWorkflow.state)
        assert blocked.status == "blocked"
        assert blocked.model_calls == 3
        await handle.signal(GoalWorkflow.cancel)
        result = await handle.result()
    assert result.status == "cancelled"
    assert len(activities.tools) == 3
    assert result.model_calls == 3


async def test_worker_replacement_resumes_existing_wait(temporal_env):
    first = Activities(
        decisions=[Decision(kind="wait", reason="Wait through a worker restart", wait_seconds=3600)]
    )
    run = request()
    async with worker(temporal_env, run.goal_id, first):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(first.waiting.wait(), timeout=15)
    replacement = Activities(decisions=[finish()])
    async with worker(temporal_env, run.goal_id, replacement):
        await handle.signal(GoalWorkflow.wake, "Worker replaced; continue the same goal")
        result = await handle.result()
    assert result.status == "succeeded"
    assert result.model_calls == 3
    assert len(first.plans) == len(replacement.plans) == 1
    assert replacement.plans[0].state.steps == 2


async def test_activity_cancellation_fence_is_terminal(temporal_env):
    activities = Activities(cancel_in_plan=True)
    run = request()
    async with worker(temporal_env, run.goal_id, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "cancelled"
    assert len(activities.plans) == 1
    assert not activities.tools


async def test_signal_during_model_call_prevents_following_tool(temporal_env):
    gate = asyncio.Event()
    activities = Activities(decisions=[act()], plan_gate=gate)
    run = request()
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.plan_started.wait(), timeout=15)
        await handle.signal(GoalWorkflow.cancel)
        gate.set()
        result = await handle.result()
    assert result.status == "cancelled"
    assert not activities.tools


async def test_signal_during_budget_persistence_prevents_model_dispatch(temporal_env):
    gate = asyncio.Event()
    activities = Activities(reservation_gate=gate)
    run = request()
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.reservation_reached.wait(), timeout=15)
        await handle.signal(GoalWorkflow.cancel)
        gate.set()
        result = await handle.result()
    assert result.status == "cancelled"
    assert result.model_calls == 1  # conservative reservation, no actual model call
    assert not activities.plans


async def test_tool_retries_reuse_the_same_operation_id(temporal_env):
    activities = Activities(decisions=[act(), finish()], tool_failures=2)
    run = request()
    async with worker(temporal_env, run.goal_id, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "succeeded"
    assert len(activities.tools) == 3
    assert {tool.operation_id for tool in activities.tools} == {"step-0001"}
    assert result.model_calls == 3


async def test_failed_verification_replans_and_never_claims_success(temporal_env):
    activities = Activities(decisions=[finish(), finish()], verification_passed=False)
    run = request(max_no_progress=2)
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        blocked = await handle.query(GoalWorkflow.state)
        assert blocked.status == "blocked"
        assert blocked.model_calls == 4
        await handle.signal(GoalWorkflow.cancel)
        result = await handle.result()
    assert result.status == "cancelled"
    assert activities.verifications == 2
    assert result.model_calls == 4
    assert activities.plans[1].feedback == "Missing required evidence"


async def test_explicit_blocked_goal_sleeps_then_wake_resumes_same_execution(temporal_env):
    activities = Activities(
        decisions=[
            Decision(kind="blocked", reason="A source must be supplied by the user"),
            act(),
            finish(),
        ]
    )
    run = request()
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        state = await handle.query(GoalWorkflow.state)
        assert state.status == "blocked"
        assert state.model_calls == len(activities.plans) == 1
        assert not activities.tools
        await handle.signal(GoalWorkflow.wake, "Use the newly supplied source")
        result = await handle.result()
    assert result.status == "succeeded"
    assert result.model_calls == 4
    assert activities.plans[1].feedback == "Use the newly supplied source"


async def test_no_progress_block_can_be_resolved_by_explicit_input(temporal_env):
    activities = Activities(decisions=[act(), act(), act(), finish()])
    run = request(max_no_progress=2)
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        assert len(activities.plans) == 3
        await handle.signal(GoalWorkflow.wake, "The report is now complete; verify it")
        result = await handle.result()
    assert result.status == "succeeded"
    assert result.model_calls == 5
    assert activities.plans[-1].feedback == "The report is now complete; verify it"


async def test_blocked_goal_keeps_original_wall_clock_deadline(temporal_env):
    activities = Activities(decisions=[Decision(kind="blocked", reason="Missing information")])
    run = request(deadline_seconds=10)
    async with worker(temporal_env, run.goal_id, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "deadline_exceeded"
    assert len(activities.plans) == result.model_calls == 1


async def test_wake_during_blocked_snapshot_is_not_lost(temporal_env):
    gate = asyncio.Event()
    activities = Activities(
        decisions=[Decision(kind="blocked", reason="Missing information"), finish()],
        blocked_persist_gate=gate,
    )
    run = request()
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        await handle.signal(GoalWorkflow.wake, "Input received before persistence finished")
        gate.set()
        result = await handle.result()
    assert result.status == "succeeded"
    assert activities.plans[1].feedback == "Input received before persistence finished"


async def test_blocked_decision_cannot_extend_an_exhausted_budget(temporal_env):
    activities = Activities(decisions=[Decision(kind="blocked", reason="Need more time")])
    run = request(max_model_calls=1)
    async with worker(temporal_env, run.goal_id, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "budget_exhausted"
    assert not activities.blocked.is_set()


async def test_duplicate_wake_command_does_not_release_the_next_wait(temporal_env):
    activities = Activities(
        decisions=[
            Decision(kind="wait", reason="Wait for the first event", wait_seconds=3600),
            Decision(kind="wait", reason="Wait for a second distinct event", wait_seconds=3600),
            finish(),
        ]
    )
    run = request()
    command = WakeCommand(command_id="event-1", message="First event received")
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        assert await asyncio.wait_for(activities.wait_steps.get(), timeout=15) == 1
        await handle.signal(GoalWorkflow.wake_command, command)
        assert await asyncio.wait_for(activities.wait_steps.get(), timeout=15) == 2
        await handle.signal(GoalWorkflow.wake_command, command)
        state = await handle.query(GoalWorkflow.state)
        assert state.status == "waiting"
        assert state.model_calls == 2
        assert len(activities.plans) == 2
        await handle.signal(
            GoalWorkflow.wake_command,
            WakeCommand(command_id="event-2", message="Second distinct event received"),
        )
        result = await handle.result()
    assert result.status == "succeeded"
    assert result.model_calls == 4
    assert activities.plans[2].feedback == "Second distinct event received"
