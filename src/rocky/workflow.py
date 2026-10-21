"""Deterministic, bounded orchestration for Rocky's long-running goals.

All model, filesystem and database work happens in Activities. A Workflow only
records decisions, waits on durable timers and chooses which Activity runs next.
"""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from rocky.models import (
        Decision,
        Observation,
        PlanInput,
        PlanResult,
        RunInput,
        RunState,
        Status,
        ToolInput,
        ToolResult,
        VerificationResult,
        VerifyInput,
        WakeCommand,
    )


@workflow.defn(name="RockyGoal")
class GoalWorkflow:
    """One durable execution owns the state and budget of one goal."""

    def __init__(self) -> None:
        self._state = RunState(goal_id="pending")
        self._cancel_requested = False
        self._wake_generation = 0
        self._consumed_wake_generation = 0
        self._wake_feedback = ""
        self._deadline = None
        self._no_progress = 0
        self._seen_progress: set[str] = set()
        self._seen_wake_commands: set[str] = set()

    def _check_activity_cancellation(self, error: ActivityError) -> bool:
        cause = error.cause
        if isinstance(cause, ApplicationError) and cause.type == "GoalCancelled":
            self._cancel_requested = True
        return self._cancel_requested

    def _guard_dispatch(self) -> None:
        if self._cancel_requested:
            raise _StopExecution("cancelled", "Cancelled by the user.")
        if self._remaining() <= 0:
            raise _StopExecution("deadline_exceeded", "The goal's wall-clock deadline has elapsed.")

    @workflow.query(name="state")
    def state(self) -> RunState:
        return self._state.model_copy(deep=True)

    @workflow.signal(name="cancel")
    def cancel(self) -> None:
        self._cancel_requested = True

    @workflow.signal(name="wake")
    def wake(self, feedback: str) -> None:
        self._wake_generation += 1
        self._wake_feedback = feedback[:4000]

    @workflow.signal(name="wake_command")
    def wake_command(self, command: WakeCommand) -> None:
        if command.command_id in self._seen_wake_commands:
            return
        self._seen_wake_commands.add(command.command_id)
        self.wake(command.message)

    def _remaining(self) -> float:
        assert self._deadline is not None
        return (self._deadline - workflow.now()).total_seconds()

    def _stop_reason(self, run: RunInput) -> tuple[Status, str] | None:
        if self._cancel_requested:
            return "cancelled", "Cancelled by the user."
        if self._remaining() <= 0:
            return "deadline_exceeded", "The goal's wall-clock deadline has elapsed."
        if self._state.steps >= run.contract.max_steps:
            return "budget_exhausted", "The planning-step budget has been exhausted."
        if self._state.model_calls >= run.contract.max_model_calls:
            return "budget_exhausted", "The model-call budget has been exhausted."
        if self._no_progress >= run.contract.max_no_progress:
            return "blocked", "Repeated attempts produced no new progress."
        return None

    def _append_observation(self, observation: Observation) -> None:
        self._state.observations = [*self._state.observations[-19:], observation]

    def _take_wake_feedback(self) -> str | None:
        if self._wake_generation == self._consumed_wake_generation:
            return None
        self._consumed_wake_generation = self._wake_generation
        feedback = self._wake_feedback or "External wake received; reconsider the goal."
        self._wake_feedback = ""
        return feedback

    async def _await_external_input(self, reason: str) -> str:
        """Keep blocked goals alive without inference until input or deadline.

        Capture the last *consumed* generation before persisting the blocked
        snapshot. This also notices a wake received during planning or persistence.
        """
        generation = self._consumed_wake_generation
        self._state.status = "blocked"
        self._state.reason = reason[:4000]
        self._state.wake_at = self._deadline.isoformat()
        await self._persist()
        self._guard_dispatch()
        try:
            await workflow.wait_condition(
                lambda: self._cancel_requested or self._wake_generation != generation,
                timeout=timedelta(seconds=self._remaining()),
            )
        except TimeoutError:
            pass
        self._guard_dispatch()
        self._no_progress = 0
        self._state.status = "running"
        self._state.wake_at = None
        return self._take_wake_feedback() or "External input received; reconsider the goal."

    async def _persist(self) -> None:
        self._state.revision += 1
        self._state.updated_at = workflow.now().isoformat()
        await workflow.execute_activity(
            "rocky.persist",
            self._state.model_copy(deep=True),
            start_to_close_timeout=timedelta(seconds=15),
            schedule_to_close_timeout=timedelta(seconds=45),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

    async def _complete(self, status: Status, reason: str) -> RunState:
        self._state.status = status
        self._state.reason = reason[:4000]
        self._state.wake_at = None
        await self._persist()
        return self._state.model_copy(deep=True)

    def _timeout(self, maximum_seconds: float) -> timedelta:
        return timedelta(seconds=max(0.001, min(self._remaining(), maximum_seconds)))

    async def _plan(self, run: RunInput, feedback: str) -> PlanResult:
        # Reserve before dispatch. Even failed/unknown-result calls consume budget.
        self._state.model_calls += 1
        self._state.steps += 1
        await self._persist()
        self._guard_dispatch()
        return await workflow.execute_activity(
            "rocky.plan",
            PlanInput(run=run, state=self._state.model_copy(deep=True), feedback=feedback),
            result_type=PlanResult,
            start_to_close_timeout=self._timeout(90),
            schedule_to_close_timeout=self._timeout(90),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    async def _verify(self, run: RunInput) -> VerificationResult:
        self._state.model_calls += 1
        await self._persist()
        self._guard_dispatch()
        return await workflow.execute_activity(
            "rocky.verify",
            VerifyInput(run=run, state=self._state.model_copy(deep=True)),
            result_type=VerificationResult,
            start_to_close_timeout=self._timeout(90),
            schedule_to_close_timeout=self._timeout(90),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

    async def _execute(self, run: RunInput, decision: Decision) -> ToolResult:
        return await workflow.execute_activity(
            "rocky.execute",
            ToolInput(
                run=run,
                operation_id=f"step-{self._state.steps:04d}",
                action=decision.action,
            ),
            result_type=ToolResult,
            start_to_close_timeout=self._timeout(45),
            schedule_to_close_timeout=self._timeout(150),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=5),
                maximum_attempts=3,
            ),
        )

    @workflow.run
    async def run(self, run: RunInput) -> RunState:
        self._state = RunState(goal_id=run.goal_id, status="running")
        self._deadline = workflow.now() + timedelta(seconds=run.contract.deadline_seconds)
        feedback = ""
        try:
            await self._persist()
            while True:
                if stop := self._stop_reason(run):
                    if stop[0] == "blocked":
                        feedback = await self._await_external_input(stop[1])
                        continue
                    return await self._complete(*stop)
                if wake_feedback := self._take_wake_feedback():
                    feedback = wake_feedback
                self._state.status = "running"
                self._state.wake_at = None
                try:
                    result = await self._plan(run, feedback)
                except ActivityError as error:
                    if self._check_activity_cancellation(error):
                        return await self._complete("cancelled", "Cancelled by the user.")
                    # Provider errors must not expose request headers or secrets.
                    self._no_progress += 1
                    feedback = "Planning failed. Reconsider once the dependency is available."
                    self._state.reason = feedback
                    await self._persist()
                    continue

                self._state.input_tokens += max(0, result.input_tokens)
                self._state.output_tokens += max(0, result.output_tokens)
                decision = result.decision
                self._state.reason = decision.reason
                # Signals can arrive while the model Activity is in flight.
                if self._cancel_requested:
                    return await self._complete("cancelled", "Cancelled by the user.")
                if self._remaining() <= 0:
                    return await self._complete(
                        "deadline_exceeded", "The goal's wall-clock deadline has elapsed."
                    )

                if decision.kind == "blocked":
                    # Explicit input can change the plan, but never replenish a
                    # spent model/step budget or extend the original deadline.
                    stop = self._stop_reason(run)
                    if stop is not None and stop[0] != "blocked":
                        return await self._complete(*stop)
                    feedback = await self._await_external_input(decision.reason)
                    continue

                if decision.kind == "wait":
                    seconds = decision.wait_seconds
                    if seconds is None or seconds < 1 or seconds > run.contract.max_wait_seconds:
                        self._no_progress += 1
                        feedback = (
                            "Provide a positive wait within the contract's maximum wait duration."
                        )
                        await self._persist()
                        continue
                    generation = self._consumed_wake_generation
                    if wake_feedback := self._take_wake_feedback():
                        feedback = wake_feedback
                        await self._persist()
                        continue
                    wait_seconds = min(float(seconds), self._remaining())
                    self._state.status = "waiting"
                    wake_at = workflow.now() + timedelta(seconds=wait_seconds)
                    self._state.wake_at = wake_at.isoformat()
                    await self._persist()
                    # Projection persistence must not extend an absolute wake or
                    # the goal deadline if the database was temporarily slow.
                    wait_seconds = (wake_at - workflow.now()).total_seconds()
                    if wait_seconds <= 0:
                        feedback = "The durable wait ended; check current conditions."
                        continue
                    try:
                        await workflow.wait_condition(
                            lambda generation=generation: (
                                self._cancel_requested or self._wake_generation != generation
                            ),
                            timeout=timedelta(seconds=wait_seconds),
                        )
                    except TimeoutError:
                        pass
                    feedback = (
                        self._take_wake_feedback()
                        or "The durable wait ended; check current conditions."
                    )
                    continue

                if decision.kind == "finish":
                    if self._state.model_calls >= run.contract.max_model_calls:
                        return await self._complete(
                            "budget_exhausted",
                            "No model-call budget remains for independent verification.",
                        )
                    try:
                        verification = await self._verify(run)
                    except ActivityError as error:
                        if self._check_activity_cancellation(error):
                            return await self._complete("cancelled", "Cancelled by the user.")
                        self._no_progress += 1
                        feedback = "Verification could not complete; the goal is not verified."
                        await self._persist()
                        continue
                    self._state.input_tokens += max(0, verification.input_tokens)
                    self._state.output_tokens += max(0, verification.output_tokens)
                    self._state.artifacts = verification.artifacts
                    if self._cancel_requested:
                        return await self._complete("cancelled", "Cancelled by the user.")
                    if self._remaining() <= 0:
                        return await self._complete(
                            "deadline_exceeded", "The goal's wall-clock deadline has elapsed."
                        )
                    if verification.passed:
                        return await self._complete("succeeded", verification.feedback)
                    self._no_progress += 1
                    feedback = verification.feedback[:4000]
                    self._append_observation(
                        Observation(
                            step=self._state.steps,
                            tool="verify",
                            success=False,
                            content=feedback,
                        )
                    )
                    await self._persist()
                    continue

                if decision.action is None:
                    self._no_progress += 1
                    feedback = "An act decision requires a concrete action."
                    await self._persist()
                    continue
                try:
                    tool_result = await self._execute(run, decision)
                except ActivityError as error:
                    if self._check_activity_cancellation(error):
                        return await self._complete("cancelled", "Cancelled by the user.")
                    tool_result = ToolResult(
                        success=False,
                        content="The tool failed after bounded retries; verify its state before trying again.",
                    )
                self._append_observation(
                    Observation(
                        step=self._state.steps,
                        tool=decision.action.tool,
                        success=tool_result.success,
                        content=tool_result.content,
                        progress_key=tool_result.progress_key,
                    )
                )
                by_path = {artifact.path: artifact for artifact in self._state.artifacts}
                by_path.update({artifact.path: artifact for artifact in tool_result.artifacts})
                self._state.artifacts = list(by_path.values())[-20:]
                progress_key = f"{decision.action.tool}:{tool_result.progress_key}"
                if (
                    tool_result.success
                    and tool_result.progress_key
                    and progress_key not in self._seen_progress
                ):
                    self._no_progress = 0
                    self._seen_progress.add(progress_key)
                else:
                    self._no_progress += 1
                feedback = "" if tool_result.success else tool_result.content[:4000]
                await self._persist()
        except _StopExecution as stop:
            return await self._complete(stop.status, stop.reason)
        except asyncio.CancelledError:
            # Native Temporal cancellation is also reflected in the projection.
            return await self._complete("cancelled", "Execution cancelled.")


class _StopExecution(Exception):
    def __init__(self, status: Status, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
