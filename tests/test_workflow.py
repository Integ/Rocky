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
from temporalio import activity, workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

from rocky.models import (
    Action,
    Decision,
    Deliverable,
    ExecutionLimits,
    ExecutionStep,
    GoalContract,
    Observation,
    PhaseInput,
    PhaseResult,
    PlanInput,
    PlanResult,
    RunCheckpoint,
    RunInput,
    RunState,
    ToolInput,
    ToolResult,
    VerificationResult,
    VerifyInput,
    WakeCommand,
)
from rocky.service import goal_workers
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
    plan_run_ids: list[str] = field(default_factory=list)
    queues: dict[str, set[str]] = field(default_factory=dict)
    phases: list[PhaseInput] = field(default_factory=list)
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
    planning_failures: int = 0
    phase_started: asyncio.Event = field(default_factory=asyncio.Event)
    phase_gate: asyncio.Event | None = None
    failing_phases: set[str] = field(default_factory=set)
    round_persist_reached: asyncio.Event = field(default_factory=asyncio.Event)
    round_persist_gate: asyncio.Event | None = None
    round_persist_cycle: int = 1

    @activity.defn(name="rocky.phase")
    async def phase(self, request: PhaseInput) -> PhaseResult:
        self.phases.append(request)
        self.phase_started.set()
        if self.phase_gate is not None:
            await self.phase_gate.wait()
        if request.phase in self.failing_phases:
            raise ApplicationError("Extension dependency unavailable", type="Transient")
        return PhaseResult()

    @activity.defn(name="rocky.plan")
    async def plan(self, request: PlanInput) -> PlanResult:
        self.plans.append(request)
        self.plan_run_ids.append(activity.info().workflow_run_id)
        self.queues.setdefault("plan", set()).add(activity.info().task_queue)
        self.plan_started.set()
        if len(self.plans) <= self.planning_failures:
            # A persistent failure: transient provider errors are retried by
            # the Activity policy and never reach the workflow's recovery path.
            raise ApplicationError(
                "Planning dependency unavailable", type="Transient", non_retryable=True
            )
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
        self.queues.setdefault("execute", set()).add(activity.info().task_queue)
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
        self.queues.setdefault("verify", set()).add(activity.info().task_queue)
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
        if (
            self.round_persist_gate is not None
            and state.cycle == self.round_persist_cycle
            and state.execution_step is not None
            and state.execution_step.phase == "act"
            and state.execution_step.status == "completed"
        ):
            self.round_persist_reached.set()
            await self.round_persist_gate.wait()
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
        return [self.plan, self.execute, self.verify, self.persist, self.phase]


def finish() -> Decision:
    return Decision(kind="finish", reason="Ready for independent verification")


def act() -> Decision:
    return Decision(kind="act", reason="Inspect current state", action=Action(tool="list_files"))


def request(**overrides) -> RunInput:
    fields = {
        "objective": "Produce a checked report",
        "acceptance_criteria": ["The report contains evidence"],
        "deliverables": [Deliverable(path="report.md")],
    }
    fields.update(overrides)
    return RunInput(goal_id=uuid4().hex, contract=GoalContract(**fields))


def worker(env, queue: str, activities: Activities):
    return goal_workers(env.client, queue, activities.registered())


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
    # Model calls have their own queue and slots; tools stay on the goal's queue.
    model_queue = run.goal_id + "-model"
    assert activities.queues == {
        "plan": {model_queue},
        "verify": {model_queue},
        "execute": {run.goal_id},
    }


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


async def test_disabled_timeout_allows_durable_wait(temporal_env):
    activities = Activities(
        decisions=[
            Decision(kind="wait", reason="Wait with no task timeout", wait_seconds=100),
            finish(),
        ]
    )
    run = request(limits=ExecutionLimits(timeout_seconds=None))
    async with worker(temporal_env, run.goal_id, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "succeeded"
    assert result.model_calls == 3


async def test_repeated_identical_results_stop_without_infinite_replanning(temporal_env):
    activities = Activities(decisions=[act()] * 5)
    run = request(max_no_progress=2)
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        blocked = await handle.query(GoalWorkflow.state)
        assert blocked.status == "blocked"
        assert blocked.model_calls == 5
        await handle.signal(GoalWorkflow.cancel)
        result = await handle.result()
    assert result.status == "cancelled"
    assert len(activities.tools) == 5
    assert result.model_calls == 5


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
    assert not first.phases and not replacement.phases
    steps = {
        state.execution_step.step_id: state.execution_step
        for state in first.snapshots + replacement.snapshots
        if state.execution_step is not None
    }
    assert [item.phase for item in steps.values()] == ["think", "act", "think", "complete"]


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


async def test_signal_during_usage_persistence_prevents_model_dispatch(temporal_env):
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
    activities = Activities(decisions=[finish()] * 4, verification_passed=False)
    run = request(max_no_progress=2)
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        blocked = await handle.query(GoalWorkflow.state)
        assert blocked.status == "blocked"
        assert blocked.model_calls == 8
        await handle.signal(GoalWorkflow.cancel)
        result = await handle.result()
    assert result.status == "cancelled"
    assert activities.verifications == 4
    assert result.model_calls == 8
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
    activities = Activities(decisions=[act()] * 5 + [finish()])
    run = request(max_no_progress=2)
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        assert len(activities.plans) == 5
        await handle.signal(GoalWorkflow.wake, "The report is now complete; verify it")
        result = await handle.result()
    assert result.status == "succeeded"
    assert result.model_calls == 7
    assert activities.plans[-1].feedback == "The report is now complete; verify it"


async def test_blocked_goal_has_no_deadline_and_can_resume_later(temporal_env):
    activities = Activities(
        decisions=[Decision(kind="blocked", reason="Missing information"), finish()]
    )
    run = request(limits=ExecutionLimits(timeout_seconds=None))
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        await temporal_env.sleep(20)
        state = await handle.query(GoalWorkflow.state)
        assert state.status == "blocked"
        assert state.wake_at is None
        assert state.model_calls == 1
        await handle.signal(GoalWorkflow.wake, "The missing information is now available")
        result = await handle.result()
    assert result.status == "succeeded"
    assert result.model_calls == 3


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


async def test_execution_records_actual_work_and_replays_completed_rounds(temporal_env):
    activities = Activities(decisions=[act(), finish()])
    run = request()
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        result = await handle.result()
    assert result.status == "succeeded"
    assert not activities.phases
    steps = {
        state.execution_step.step_id: state.execution_step
        for state in activities.snapshots
        if state.execution_step is not None
    }
    assert [item.phase for item in steps.values()] == ["think", "act", "think", "complete"]
    assert all(item.status == "completed" for item in steps.values())
    assert all(item.completed_at is not None for item in steps.values())
    assert result.handoff_summary
    history = await handle.fetch_history()
    assert not any(event.HasField("marker_recorded_event_attributes") for event in history.events)
    replayer = Replayer(workflows=[GoalWorkflow], data_converter=pydantic_data_converter)
    await asyncio.wait_for(replayer.replay_workflow(history), timeout=15)


async def test_failed_planning_records_failure_and_recovers(temporal_env):
    activities = Activities(planning_failures=1)
    run = request()
    async with worker(temporal_env, run.goal_id, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "succeeded"
    assert not activities.phases
    assert "Planning" in activities.plans[1].feedback
    assert activities.plans[1].state.handoff_summary
    assert not activities.tools
    assert activities.verifications == 1


async def test_automatic_recovery_can_finish_without_external_wake(temporal_env):
    activities = Activities(decisions=[act()] * 3 + [finish()])
    run = request(max_no_progress=2)
    async with worker(temporal_env, run.goal_id, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "succeeded"
    assert not activities.blocked.is_set()
    assert activities.plans[3].feedback
    assert activities.plans[3].state.handoff_summary
    assert len(activities.tools) == 3
    assert activities.verifications == 1


async def test_changed_action_is_progress_even_when_tool_content_is_unchanged(temporal_env):
    changed = Decision(
        kind="act",
        reason="Inspect a different source after repeated results",
        action=Action(tool="read_file", path="different.txt"),
    )
    activities = Activities(decisions=[act(), act(), changed, finish()])
    run = request(max_no_progress=2)
    async with worker(temporal_env, run.goal_id, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "succeeded"
    assert not activities.blocked.is_set()
    assert not activities.plans[3].feedback
    assert len(activities.tools) == 3


async def test_cancel_during_phase_hook_prevents_model_dispatch(temporal_env):
    gate = asyncio.Event()
    activities = Activities(phase_gate=gate)
    run = request()
    run.hook_phases = ["think"]
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.phase_started.wait(), timeout=15)
        await handle.signal(GoalWorkflow.cancel)
        gate.set()
        result = await handle.result()
    assert result.status == "cancelled"
    assert not activities.plans
    assert not activities.tools


async def test_complete_hook_failure_recovers_then_blocks_without_endless_reviews(temporal_env):
    activities = Activities(decisions=[finish()] * 4, failing_phases={"complete"})
    run = request(max_no_progress=2)
    run.hook_phases = ["complete"]
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        state = await handle.query(GoalWorkflow.state)
        assert state.status == "blocked"
        assert state.recovery_attempts == 1
        assert activities.verifications == 0
        assert state.model_calls == 4
        assert "Recovery required" in activities.plans[2].feedback
        assert "Recovery did not produce new progress" in state.reason
        await handle.signal(GoalWorkflow.cancel)
        result = await handle.result()
    assert result.status == "cancelled"
    assert [item.phase for item in activities.phases] == ["complete"] * 4
    assert not any(snapshot.status == "succeeded" for snapshot in activities.snapshots)


async def test_wait_wake_gives_stalled_task_a_fresh_recovery_allowance(temporal_env):
    activities = Activities(
        decisions=[
            *[act()] * 3,
            Decision(kind="wait", reason="Await changed external conditions", wait_seconds=3600),
            act(),
            act(),
            finish(),
        ]
    )
    run = request(max_no_progress=2)
    async with worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        assert await asyncio.wait_for(activities.wait_steps.get(), timeout=15) == 4
        waiting = await handle.query(GoalWorkflow.state)
        assert waiting.recovery_attempts == 1
        await handle.signal(GoalWorkflow.wake, "External conditions changed; inspect again")
        result = await handle.result()
    assert result.status == "succeeded"
    assert not activities.blocked.is_set()
    assert activities.plans[4].feedback == "External conditions changed; inspect again"
    assert activities.plans[4].state.recovery_attempts == 0
    assert activities.plans[6].state.recovery_attempts == 1
    assert "Recovery required" in activities.plans[6].feedback
    assert activities.verifications == 1


async def test_exhausted_tool_retries_record_failure_and_recover(temporal_env):
    activities = Activities(decisions=[act(), finish()], tool_failures=3)
    run = request()
    async with worker(temporal_env, run.goal_id, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "succeeded"
    assert not activities.phases
    assert len(activities.tools) == 3
    assert {item.operation_id for item in activities.tools} == {"step-0001"}
    assert "The action could not complete" in activities.plans[1].feedback
    assert activities.plans[1].state.handoff_summary
    assert activities.plans[1].state.observations[-1].success is False
    assert activities.verifications == 1


@workflow.defn(name="RockyGoal")
class FrequentContinuationWorkflow(GoalWorkflow):
    """Exercise production checkpointing with short, real Temporal histories."""

    def _should_continue_as_new(self) -> bool:
        return self._state.cycle - self._run_start_cycle >= 2

    @workflow.run
    async def run(self, run: RunInput) -> RunState:
        return await super().run(run)


def continuation_worker(env, queue: str, activities: Activities):
    return goal_workers(
        env.client,
        queue,
        activities.registered(),
        workflows=[FrequentContinuationWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )


def distinct_action(index: int) -> Decision:
    return Decision(
        kind="act",
        reason=f"Inspect source {index}",
        action=Action(tool="read_file", path=f"source-{index}.txt"),
    )


async def test_continue_as_new_preserves_cumulative_state_and_replays_each_run(temporal_env):
    activities = Activities(decisions=[*[distinct_action(index) for index in range(3)], finish()])
    run = request()
    async with continuation_worker(temporal_env, run.goal_id, activities):
        result = await temporal_env.client.execute_workflow(
            FrequentContinuationWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "succeeded"
    assert (result.steps, result.model_calls, result.cycle) == (4, 5, 4)
    assert (result.input_tokens, result.output_tokens) == (53, 31)
    assert len(result.observations) == 3
    assert result.handoff_summary
    assert activities.plans[2].state.handoff_summary
    assert activities.plans[2].state.steps == 3
    assert activities.plans[2].state.model_calls == 3
    assert activities.plans[2].run.checkpoint is None
    assert len(set(activities.plan_run_ids)) == 2
    assert [item.operation_id for item in activities.tools] == [
        "step-0001",
        "step-0002",
        "step-0003",
    ]
    revisions = [item.revision for item in activities.snapshots]
    assert revisions == sorted(set(revisions))
    assert not activities.phases
    step_ids = {
        item.execution_step.step_id
        for item in activities.snapshots
        if item.execution_step is not None
    }
    assert len(step_ids) == 8
    replayer = Replayer(
        workflows=[FrequentContinuationWorkflow],
        data_converter=pydantic_data_converter,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    for run_id in set(activities.plan_run_ids):
        history = await temporal_env.client.get_workflow_handle(
            run.goal_id, run_id=run_id
        ).fetch_history()
        await asyncio.wait_for(replayer.replay_workflow(history), timeout=15)


async def test_continue_as_new_preserves_stall_detection_and_recovery_allowance(temporal_env):
    activities = Activities(decisions=[act()] * 5)
    run = request(max_no_progress=2)
    async with continuation_worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            FrequentContinuationWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.blocked.wait(), timeout=15)
        state = await handle.query(GoalWorkflow.state)
        assert state.steps == 5
        assert state.model_calls == 5
        assert state.recovery_attempts == 1
        assert len(set(activities.plan_run_ids)) == 3
        assert "Recovery required" in activities.plans[3].feedback
        await handle.signal(GoalWorkflow.cancel)
        result = await handle.result()
    assert result.status == "cancelled"
    assert len(activities.tools) == 5


async def test_wake_command_deduplication_survives_continue_as_new(temporal_env):
    activities = Activities(
        decisions=[
            distinct_action(1),
            Decision(kind="wait", reason="Await first event", wait_seconds=3600),
            Decision(kind="wait", reason="Await a distinct second event", wait_seconds=3600),
            finish(),
        ]
    )
    run = request()
    first = WakeCommand(command_id="first-event", message="First event is available")
    async with continuation_worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            FrequentContinuationWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        assert await asyncio.wait_for(activities.wait_steps.get(), timeout=15) == 2
        await handle.signal(GoalWorkflow.wake_command, first)
        assert await asyncio.wait_for(activities.wait_steps.get(), timeout=15) == 3
        assert len(set(activities.plan_run_ids)) == 2
        await handle.signal(GoalWorkflow.wake_command, first)
        state = await handle.query(GoalWorkflow.state)
        assert state.status == "waiting"
        assert state.model_calls == 3
        await handle.signal(
            GoalWorkflow.wake_command,
            WakeCommand(command_id="second-event", message="Distinct second event is available"),
        )
        result = await handle.result()
    assert result.status == "succeeded"
    assert activities.plans[3].feedback == "Distinct second event is available"


async def test_pending_wake_at_rollover_reaches_the_next_run(temporal_env):
    gate = asyncio.Event()
    activities = Activities(
        decisions=[distinct_action(1), distinct_action(2), finish()],
        round_persist_gate=gate,
        round_persist_cycle=2,
    )
    run = request()
    async with continuation_worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            FrequentContinuationWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.round_persist_reached.wait(), timeout=15)
        await handle.signal(
            GoalWorkflow.wake_command,
            WakeCommand(command_id="rollover-event", message="New evidence arrived at rollover"),
        )
        gate.set()
        result = await handle.result()
    assert result.status == "succeeded"
    assert len(set(activities.plan_run_ids)) == 2
    assert activities.plans[2].feedback == "New evidence arrived at rollover"
    assert result.steps == 3


async def test_cancel_at_rollover_prevents_dispatch_in_another_run(temporal_env):
    gate = asyncio.Event()
    activities = Activities(
        decisions=[distinct_action(1), distinct_action(2), finish()],
        round_persist_gate=gate,
        round_persist_cycle=2,
    )
    run = request()
    async with continuation_worker(temporal_env, run.goal_id, activities):
        handle = await temporal_env.client.start_workflow(
            FrequentContinuationWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        await asyncio.wait_for(activities.round_persist_reached.wait(), timeout=15)
        await handle.signal(GoalWorkflow.cancel)
        gate.set()
        result = await handle.result()
    assert result.status == "cancelled"
    assert len(set(activities.plan_run_ids)) == 1
    assert len(activities.plans) == len(activities.tools) == 2
    assert activities.verifications == 0


async def test_saved_round_checkpoint_preserves_state_without_repeating_the_tool(
    temporal_env,
):
    run = request()
    previous_result = ToolResult(
        success=True, content="Saved external result", progress_key="saved"
    )
    state = RunState(
        goal_id=run.goal_id,
        status="running",
        revision=535,
        steps=54,
        model_calls=54,
        cycle=39,
        phase="act",
        input_tokens=1000,
        output_tokens=100,
        handoff_summary="Saved external result was observed; verify the deliverable next",
        observations=[
            Observation(
                step=54,
                tool="list_files",
                success=True,
                content=previous_result.content,
                progress_key=previous_result.progress_key,
            )
        ],
        execution_step=ExecutionStep(
            step_id=f"{run.goal_id}:39:act",
            cycle=39,
            phase="act",
            status="completed",
            started_at="2026-10-21T20:00:00+00:00",
            completed_at="2026-10-21T20:00:01+00:00",
            output={"result": previous_result.model_dump(mode="json")},
        ),
    )
    run.checkpoint = RunCheckpoint(
        state=state,
        feedback="Saved round completed before engine termination",
    )
    activities = Activities(decisions=[finish()])
    async with worker(temporal_env, run.goal_id, activities):
        result = await temporal_env.client.execute_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
    assert result.status == "succeeded"
    assert not activities.tools
    assert not activities.phases
    assert (result.steps, result.model_calls, result.cycle) == (55, 56, 40)
    assert (result.input_tokens, result.output_tokens) == (1017, 110)
    assert result.revision > 535
    assert len(result.observations) == 1
    assert activities.plans[0].state.recovery_attempts == 0
    assert "Saved external result" in activities.plans[0].state.handoff_summary
    assert activities.plans[0].run.checkpoint is None
