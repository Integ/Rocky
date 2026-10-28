"""Deterministic orchestration for Rocky's long-running goals.

All model, filesystem and database work happens in Activities. A Workflow only
records decisions, waits on durable timers and chooses which Activity runs next.
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from rocky.models import (
        MAX_PARALLEL_ACTIONS,
        Action,
        Decision,
        ExecutionStep,
        Observation,
        PendingApproval,
        Phase,
        PhaseInput,
        PhaseResult,
        PlanInput,
        PlanResult,
        RunCheckpoint,
        RunInput,
        RunState,
        Status,
        ToolInput,
        ToolResult,
        VerificationResult,
        VerifyInput,
        WakeCommand,
        read_only_action,
    )

# A streamed model response may take up to the planner's 900-second total limit.
# Heartbeats detect a lost worker sooner than that. Rate limits, overload and
# connection failures produce no model output, so the Activity retries them;
# ModelDecisionError marks responses never to repeat.
_MODEL_ATTEMPT_SECONDS = 930
_MODEL_HEARTBEAT = timedelta(seconds=60)
_MODEL_ATTEMPTS = 3
_MODEL_TOTAL_SECONDS = _MODEL_ATTEMPT_SECONDS * _MODEL_ATTEMPTS + 120
_MODEL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=_MODEL_ATTEMPTS,
    non_retryable_error_types=["ModelDecisionError", "GoalCancelled"],
)
_REPORTED_MODEL_ERRORS = {"ModelDecisionError", "ModelTransientError"}
_BLOCKED_REASON_ERROR = (
    "A blocked decision requires the concrete missing information or dependency."
)


@workflow.defn(name="RockyGoal")
class GoalWorkflow:
    """One durable execution owns the state and usage counters of one goal."""

    @workflow.init
    def __init__(self, run: RunInput) -> None:
        self._state = RunState(goal_id="pending")
        self._cancel_requested = False
        self._wake_generation = 0
        self._consumed_wake_generation = 0
        self._wake_feedback = ""
        self._no_progress = 0
        self._seen_progress: set[str] = set()
        self._refreshed_progress: set[str] = set()
        self._last_progress = ""
        # Fingerprints of the batch the planner saw last, like _last_progress.
        self._last_batch: set[str] = set()
        self._seen_wake_commands: set[str] = set()
        self._run_start_cycle = 0
        self._limits = run.contract.limits
        self._approved_action = ""
        # Restore before signal handlers can run in a continued execution. A
        # duplicated wake delivered at startup must see its prior command ID.
        if checkpoint := run.checkpoint:
            if checkpoint.state.goal_id != run.goal_id:
                raise ApplicationError("Checkpoint belongs to another goal", non_retryable=True)
            self._state = checkpoint.state.model_copy(deep=True)
            self._no_progress = checkpoint.no_progress
            self._seen_progress = set(checkpoint.seen_progress)
            self._refreshed_progress = set(checkpoint.refreshed_progress)
            self._last_progress = checkpoint.last_progress
            self._last_batch = set(checkpoint.last_batch_progress)
            self._wake_generation = checkpoint.wake_generation
            self._consumed_wake_generation = checkpoint.consumed_wake_generation
            self._wake_feedback = checkpoint.wake_feedback
            self._seen_wake_commands = set(checkpoint.seen_wake_commands)
            self._cancel_requested = checkpoint.cancel_requested

    def _should_continue_as_new(self) -> bool:
        info = workflow.info()
        return (
            self._state.cycle - self._run_start_cycle >= 8
            or info.get_current_history_size() >= 8_000_000
            or info.get_current_history_length() >= 1500
            or info.is_continue_as_new_suggested()
        )

    def _checkpoint(self, feedback: str) -> RunCheckpoint:
        return RunCheckpoint(
            state=self._state.model_copy(deep=True),
            feedback=feedback[:4000],
            no_progress=self._no_progress,
            seen_progress=sorted(self._seen_progress)[-256:],
            refreshed_progress=sorted(self._refreshed_progress)[-256:],
            last_progress=self._last_progress,
            last_batch_progress=sorted(self._last_batch),
            wake_generation=self._wake_generation,
            consumed_wake_generation=self._consumed_wake_generation,
            wake_feedback=self._wake_feedback,
            seen_wake_commands=sorted(self._seen_wake_commands),
            cancel_requested=self._cancel_requested,
        )

    def _check_activity_cancellation(self, error: ActivityError) -> bool:
        cause = error.cause
        if isinstance(cause, ApplicationError) and cause.type == "GoalCancelled":
            self._cancel_requested = True
        return self._cancel_requested

    def _record_failed_model_usage(self, error: ActivityError) -> None:
        cause = error.cause
        if not isinstance(cause, ApplicationError) or cause.type != "ModelDecisionError":
            return
        if not cause.details or not isinstance(cause.details[0], dict):
            return  # Older histories and failures with no reported usage.
        for name in ("input_tokens", "output_tokens", "cached_input_tokens"):
            value = cause.details[0].get(name)
            if type(value) is int and value >= 0:
                setattr(self._state, name, getattr(self._state, name) + value)

    def _guard_dispatch(self) -> None:
        if self._cancel_requested:
            raise _StopExecution("cancelled", "Cancelled by the user.")
        if self._state.deadline_at:
            if workflow.now() >= datetime.fromisoformat(self._state.deadline_at):
                raise _StopExecution(
                    "deadline_exceeded", "The task's wall-clock limit has elapsed."
                )

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

    @workflow.signal(name="approval_command")
    def approval_command(self, command: WakeCommand) -> None:
        # This signal is dispatched only from the authenticated owner outbox.
        # Ordinary wake text and model/tool output can never authorize a tool.
        # "task:<fingerprint>" also approves the pending command's prefix for
        # later simple commands in this task; the tool layer matches it.
        pending = self._state.pending_approval
        scope, _, fingerprint = command.message.rpartition(":")
        if pending is None or fingerprint != pending.fingerprint or scope not in {"", "task"}:
            return
        if scope == "task":
            if not pending.action.sandbox or not pending.command_prefix:
                return
            if pending.command_prefix not in self._state.approved_command_prefixes:
                self._state.approved_command_prefixes.append(list(pending.command_prefix))
        self._approved_action = pending.fingerprint

    def _append_observation(self, observation: Observation) -> None:
        # Full results have their own SQL phase/message record. Repeated
        # snapshots need only a short working set, not the entire transcript.
        content = observation.content
        if len(content) > 2000:
            content = content[:1900] + "\n[Excerpt; full result is in the execution record]"
        observation = observation.model_copy(update={"content": content})
        self._state.observations = [*self._state.observations[-5:], observation]

    def _take_wake_feedback(self) -> str | None:
        if self._wake_generation == self._consumed_wake_generation:
            return None
        self._consumed_wake_generation = self._wake_generation
        feedback = self._wake_feedback or "External wake received; reconsider the goal."
        self._wake_feedback = ""
        return feedback

    async def _await_external_input(self, reason: str) -> str:
        """Keep blocked goals alive without inference until input or cancellation.

        Capture the last *consumed* generation before persisting the blocked
        snapshot. This also notices a wake received during planning or persistence.
        """
        generation = self._consumed_wake_generation
        self._state.status = "blocked"
        self._state.reason = reason[:4000]
        self._state.wake_at = self._state.deadline_at
        await self._persist()
        self._guard_dispatch()
        try:
            await workflow.wait_condition(
                lambda: self._cancel_requested or self._wake_generation != generation,
                timeout=datetime.fromisoformat(self._state.deadline_at) - workflow.now()
                if self._state.deadline_at
                else None,
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
        if self._state.execution_step:
            step = self._state.execution_step
            if step.status == "started":
                step.status = "failed"
                step.completed_at = workflow.now().isoformat()
                step.output = {"error": reason[:4000]}
        self._state.status = status
        self._state.reason = reason[:4000]
        self._state.wake_at = None
        self._state.pending_approval = None
        await self._persist()
        return self._state.model_copy(deep=True)

    def _timeout(self, maximum_seconds: float) -> timedelta:
        # Each operation is bounded by both its own timeout and the task deadline.
        if self._state.deadline_at:
            remaining = (
                datetime.fromisoformat(self._state.deadline_at) - workflow.now()
            ).total_seconds()
            return timedelta(seconds=max(0.001, min(remaining, maximum_seconds)))
        return timedelta(seconds=maximum_seconds)

    async def _plan(self, run: RunInput, feedback: str) -> PlanResult:
        # Record before dispatch, including failed/unknown-result attempts.
        self._state.model_calls += 1
        self._state.steps += 1
        await self._persist()
        self._guard_dispatch()
        return await workflow.execute_activity(
            "rocky.plan",
            PlanInput(run=run, state=self._state.model_copy(deep=True), feedback=feedback),
            result_type=PlanResult,
            start_to_close_timeout=self._timeout(_MODEL_ATTEMPT_SECONDS),
            schedule_to_close_timeout=self._timeout(_MODEL_TOTAL_SECONDS),
            heartbeat_timeout=_MODEL_HEARTBEAT,
            retry_policy=_MODEL_RETRY,
        )

    async def _verify(self, run: RunInput) -> VerificationResult:
        self._state.model_calls += 1
        await self._persist()
        self._guard_dispatch()
        return await workflow.execute_activity(
            "rocky.verify",
            VerifyInput(run=run, state=self._state.model_copy(deep=True)),
            result_type=VerificationResult,
            start_to_close_timeout=self._timeout(_MODEL_ATTEMPT_SECONDS),
            schedule_to_close_timeout=self._timeout(_MODEL_TOTAL_SECONDS),
            heartbeat_timeout=_MODEL_HEARTBEAT,
            retry_policy=_MODEL_RETRY,
        )

    async def _execute(
        self, run: RunInput, action: Action | None, operation_id: str | None = None
    ) -> ToolResult:
        return await workflow.execute_activity(
            "rocky.execute",
            ToolInput(
                run=run,
                operation_id=operation_id or f"step-{self._state.steps:04d}",
                action=action,
                approved_action_fingerprints=[self._approved_action]
                if self._approved_action
                else [],
                approved_command_prefixes=[
                    list(prefix) for prefix in self._state.approved_command_prefixes
                ],
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

    async def _start_phase(self, run: RunInput, phase: Phase, cycle: "_Cycle") -> None:
        self._state.phase = phase
        self._state.execution_step = ExecutionStep(
            step_id=f"{run.goal_id}:{self._state.cycle}:{phase}",
            cycle=self._state.cycle,
            phase=phase,
            started_at=workflow.now().isoformat(),
            input={
                "feedback": cycle.feedback[:4000],
                "decision": cycle.decision.model_dump(mode="json") if cycle.decision else None,
            },
        )
        await self._persist()
        self._guard_dispatch()
        if phase not in run.hook_phases:
            return
        hook_state = self._state.model_copy(deep=True)
        hook_state.execution_step.input = {}
        hook_state.execution_step.output = {}
        result = await workflow.execute_activity(
            "rocky.phase",
            PhaseInput(
                run=run,
                state=hook_state,
                phase=phase,
                feedback=cycle.feedback,
                decision=cycle.decision,
                tool_result=cycle.result,
                verification=cycle.verification,
            ),
            result_type=PhaseResult,
            start_to_close_timeout=self._timeout(30),
            schedule_to_close_timeout=self._timeout(30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        self._guard_dispatch()
        if result.feedback is not None:
            cycle.feedback = result.feedback
        if result.handoff_summary is not None:
            self._state.handoff_summary = result.handoff_summary
            self._state.handoff_is_model = True
            cycle.custom_summary = True
        if result.decision is not None:
            if phase != "decide":
                cycle.error = "Only the decide phase may replace a decision."
            else:
                cycle.decision = result.decision
                cycle.error = ""

    async def _end_phase(self, output: dict, *, failed: bool = False) -> None:
        step = self._state.execution_step
        assert step is not None
        step.status = "failed" if failed else "completed"
        step.output = output
        step.completed_at = workflow.now().isoformat()
        await self._persist()

    @staticmethod
    def _decision_error(run: RunInput, decision: Decision | None) -> str:
        if decision is None:
            return "Planning produced no usable decision."
        if decision.kind == "act":
            if decision.actions is not None:
                if decision.action is not None or decision.wait_seconds is not None:
                    return "Set either action or actions, never both, and no wait duration."
                if not 1 <= len(decision.actions) <= MAX_PARALLEL_ACTIONS:
                    return f"actions must contain 1 to {MAX_PARALLEL_ACTIONS} read-only actions."
                if not all(read_only_action(item) for item in decision.actions):
                    return (
                        "actions may contain only read_file, list_files, fetch_url and "
                        "GET/HEAD/OPTIONS http_request actions with a null path. Propose "
                        "writes, commands and mutating requests as a single action."
                    )
            elif decision.action is None or decision.wait_seconds is not None:
                return "An act decision requires one concrete action and no wait duration."
        elif decision.kind == "wait":
            if (
                decision.action is not None
                or decision.actions is not None
                or decision.wait_seconds is None
                or not 1 <= decision.wait_seconds <= run.contract.max_wait_seconds
            ):
                return "Provide a positive wait within the contract's maximum wait duration."
        elif (
            decision.action is not None
            or decision.actions is not None
            or decision.wait_seconds is not None
        ):
            return "Only act may contain an action, and only wait may contain a wait duration."
        if decision.kind == "blocked" and not decision.reason.strip():
            return _BLOCKED_REASON_ERROR
        return ""

    async def _harness_wait(self, decision: Decision) -> str:
        generation = self._consumed_wake_generation
        if feedback := self._take_wake_feedback():
            self._no_progress = 0
            self._state.recovery_attempts = 0
            return feedback
        assert decision.wait_seconds is not None
        wake_at = workflow.now() + timedelta(seconds=decision.wait_seconds)
        if self._state.deadline_at:
            wake_at = min(wake_at, datetime.fromisoformat(self._state.deadline_at))
        self._state.status = "waiting"
        self._state.wake_at = wake_at.isoformat()
        await self._persist()
        self._guard_dispatch()
        seconds = (wake_at - workflow.now()).total_seconds()
        if seconds > 0:
            try:
                await workflow.wait_condition(
                    lambda: self._cancel_requested or self._wake_generation != generation,
                    timeout=timedelta(seconds=seconds),
                )
            except TimeoutError:
                pass
        self._guard_dispatch()
        self._state.status = "running"
        self._state.wake_at = None
        if feedback := self._take_wake_feedback():
            self._no_progress = 0
            self._state.recovery_attempts = 0
            return feedback
        return "The durable wait ended; check current conditions."

    def _check_limits(self, *, planning: bool = False, model_call: bool = False) -> None:
        self._guard_dispatch()
        limits = self._limits
        if planning and limits.max_steps is not None and self._state.steps >= limits.max_steps:
            raise _StopExecution(
                "budget_exhausted", "The task's planning-step limit has been reached."
            )
        if (
            model_call
            and limits.max_model_calls is not None
            and self._state.model_calls >= limits.max_model_calls
        ):
            raise _StopExecution(
                "budget_exhausted", "The task's model-call limit has been reached."
            )
        used = self._state.input_tokens + self._state.output_tokens
        if limits.max_tokens is not None and (
            used > limits.max_tokens or ((planning or model_call) and used >= limits.max_tokens)
        ):
            raise _StopExecution(
                "budget_exhausted", "The task's cumulative token limit has been reached."
            )

    async def _await_approval(self, decision: Decision, result: ToolResult) -> None:
        assert decision.action is not None
        self._state.pending_approval = PendingApproval(
            fingerprint=result.approval_required,
            operation_id=f"step-{self._state.steps:04d}",
            action=decision.action,
            reason=result.content,
            command_prefix=result.approval_command_prefix or None,
        )
        self._state.status = "blocked"
        self._state.reason = result.content
        self._state.wake_at = self._state.deadline_at
        await self._persist()
        self._guard_dispatch()
        try:
            await workflow.wait_condition(
                lambda: self._cancel_requested or self._approved_action == result.approval_required,
                timeout=self._timeout(86400 * 365) if self._state.deadline_at else None,
            )
        except TimeoutError:
            pass
        self._guard_dispatch()
        self._state.status = "running"
        self._state.pending_approval = None
        self._state.wake_at = None

    async def _harness_action(self, run: RunInput, cycle: "_Cycle") -> dict:
        decision = cycle.decision
        if cycle.error or decision is None:
            return {"skipped": True, "error": cycle.error}
        if decision.kind == "wait":
            cycle.feedback = await self._harness_wait(decision)
            return {"wait_ended": True, "feedback": cycle.feedback}
        if decision.kind == "blocked":
            return {"blocked": True, "reason": decision.reason}
        if decision.kind == "finish":
            self._check_limits(model_call=True)
            cycle.verification = await self._verify(run)
            self._guard_dispatch()
            verification = cycle.verification
            self._state.input_tokens += max(0, verification.input_tokens)
            self._state.output_tokens += max(0, verification.output_tokens)
            self._state.cached_input_tokens += max(0, verification.cached_input_tokens)
            self._state.artifacts = verification.artifacts
            cycle.feedback = verification.feedback[:4000]
            if not verification.passed:
                self._append_observation(
                    Observation(
                        step=self._state.steps, tool="verify", success=False, content=cycle.feedback
                    )
                )
            return {"verification": verification.model_dump(mode="json")}
        if decision.actions is not None:
            return await self._harness_batch(run, cycle, decision.actions)
        cycle.result = await self._execute(run, decision.action)
        if cycle.result.approval_required:
            await self._await_approval(decision, cycle.result)
            cycle.result = await self._execute(run, decision.action)
            self._approved_action = ""
            if cycle.result.approval_required:
                cycle.error = (
                    "Approval no longer matches the operation. Request a new action for review."
                )
        self._guard_dispatch()
        result = cycle.result
        assert decision.action is not None
        content = result.content
        if not result.success:
            content = "\n".join(
                part for part in (result.error_code, result.retry_hint, result.content) if part
            )[:16000]
        self._append_observation(
            Observation(
                step=self._state.steps,
                tool=decision.action.tool,
                success=result.success,
                content=content,
                progress_key=result.progress_key,
            )
        )
        artifacts = {item.path: item for item in self._state.artifacts}
        artifacts.update({item.path: item for item in result.artifacts})
        self._state.artifacts = list(artifacts.values())[-20:]
        cycle.feedback = "" if result.success else content[:4000]
        return {"result": result.model_dump(mode="json")}

    async def _harness_batch(self, run: RunInput, cycle: "_Cycle", actions: list[Action]) -> dict:
        """Run independent read-only actions concurrently as one step.

        Each action has its own operation identifier, receipt and observation.
        Read-only actions never wait for approval; an unexpected approval
        request becomes a failed result instead of a paused batch.
        """
        outcomes = await asyncio.gather(
            *(
                self._execute(run, action, f"step-{self._state.steps:04d}-{index}")
                for index, action in enumerate(actions, 1)
            ),
            return_exceptions=True,
        )
        self._guard_dispatch()
        for action, outcome in zip(actions, outcomes, strict=True):
            if isinstance(outcome, ActivityError):
                if self._check_activity_cancellation(outcome):
                    raise _StopExecution("cancelled", "Cancelled by the user.")
                outcome = ToolResult(
                    success=False,
                    content="The action could not complete after bounded retries.",
                    error_code="tool_unavailable",
                    retry_hint="Inspect its actual state before issuing it again.",
                )
            elif isinstance(outcome, BaseException):
                raise outcome
            elif outcome.approval_required:
                outcome = ToolResult(
                    success=False,
                    content="This action requires owner approval and was not dispatched.",
                    error_code="approval_required",
                    retry_hint="Propose it as a single action so the owner can review it.",
                )
            cycle.results.append((action, outcome))
            content = outcome.content
            if not outcome.success:
                content = "\n".join(
                    part
                    for part in (outcome.error_code, outcome.retry_hint, outcome.content)
                    if part
                )[:16000]
            self._append_observation(
                Observation(
                    step=self._state.steps,
                    tool=action.tool,
                    success=outcome.success,
                    content=content,
                    progress_key=outcome.progress_key,
                )
            )
        failures = [
            f"{action.tool} {action.path or action.url or ''}: "
            + "\n".join(
                part
                for part in (result.error_code, result.retry_hint, result.content[:500])
                if part
            )
            for action, result in cycle.results
            if not result.success
        ]
        cycle.feedback = "\n".join(failures)[:4000]
        return {
            "results": [
                {"action": action.model_dump(mode="json"), "result": result.model_dump(mode="json")}
                for action, result in cycle.results
            ]
        }

    def _refreshes_evidence(self, tool: str, success: bool, fingerprint: str) -> bool:
        """Re-reading evidence that left the planner's recent window is not a stall.

        Planning memory holds a bounded window of results, so a long task may
        need an unchanged file again. Each observation may be refreshed once,
        never right after the planner saw it (alone or in a parallel batch). A
        refresh is still not new evidence: it neither counts as a stall nor
        resets the counter.
        """
        if (
            not success
            or tool not in _OBSERVATION_TOOLS
            or fingerprint == self._last_progress
            or fingerprint in self._last_batch
            or fingerprint in self._refreshed_progress
        ):
            return False
        self._refreshed_progress.add(fingerprint)
        return True

    def _reflect_cycle(self, run: RunInput, cycle: "_Cycle") -> dict:
        if cycle.error:
            self._no_progress += 1
            cycle.feedback = cycle.error
            self._append_observation(
                Observation(
                    step=self._state.steps, tool="harness", success=False, content=cycle.error
                )
            )
        elif cycle.result is not None and cycle.decision is not None:
            # The target and parameters matter: reading two different empty
            # files is new evidence even when their result hashes are identical.
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "action": cycle.decision.action.model_dump(mode="json"),
                        "result": cycle.result.progress_key or cycle.result.content,
                        "success": cycle.result.success,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if cycle.result.success and fingerprint not in self._seen_progress:
                self._seen_progress.add(fingerprint)
                self._no_progress = 0
                self._state.recovery_attempts = 0
            elif not self._refreshes_evidence(
                cycle.decision.action.tool, cycle.result.success, fingerprint
            ):
                self._no_progress += 1
            self._last_progress = fingerprint
            self._last_batch = set()
        elif cycle.results:
            # A batch progresses when any action returned new evidence. When
            # none did, re-reading results that left the window is a refresh.
            fingerprints = {
                hashlib.sha256(
                    json.dumps(
                        {
                            "action": action.model_dump(mode="json"),
                            "result": result.progress_key or result.content,
                            "success": True,
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest(): action.tool
                for action, result in cycle.results
                if result.success
            }
            new = set(fingerprints) - self._seen_progress
            if new:
                self._seen_progress.update(new)
                self._no_progress = 0
                self._state.recovery_attempts = 0
            elif not any(
                [
                    self._refreshes_evidence(tool, True, fingerprint)
                    for fingerprint, tool in sorted(fingerprints.items())
                ]
            ):
                self._no_progress += 1
            self._last_progress = ""
            self._last_batch = set(fingerprints)
        elif cycle.verification is not None and not cycle.verification.passed:
            self._no_progress += 1
        # Waiting on a dependency is not progress and cannot erase a recovery
        # attempt. It also does not count as a failed action.
        if self._no_progress >= run.contract.max_no_progress:
            if self._state.recovery_attempts == 0:
                self._state.recovery_attempts = 1
                self._no_progress = 0
                cycle.feedback = (
                    "Recovery required: recent attempts did not advance the objective. "
                    "Use the handoff summary and recorded failures to choose a different "
                    "strategy or inspect missing evidence. Do not repeat the same operation "
                    "with the same inputs. If an external dependency prevents progress, "
                    "state exactly what is needed. Last outcome: " + cycle.feedback
                )[:4000]
            else:
                cycle.blocked_reason = (
                    "Recovery did not produce new progress. "
                    + (cycle.feedback or "The same action returned already observed evidence.")
                    + " Provide new information or resolve the dependency, then send a message to resume."
                )[:4000]
        if not cycle.custom_summary:
            recent = self._state.observations[-4:]
            decision = cycle.decision
            action = decision.action if decision else None
            summary = [
                f"Cycle {self._state.cycle}; objective remains unverified.",
                f"Last decision: {decision.kind}: {decision.reason}"
                if decision
                else "Planning failed.",
            ]
            if action:
                summary.append(
                    f"Attempted {action.tool}; path={action.path!r}; url={action.url!r}."
                )
            elif decision and decision.actions:
                summary.append(
                    f"Attempted {len(decision.actions)} parallel reads: "
                    + "; ".join(
                        f"{item.tool} {item.path or item.url or ''}" for item in decision.actions
                    )[:1000]
                )
            for item in recent:
                summary.append(
                    f"Step {item.step} {item.tool} {'succeeded' if item.success else 'failed'}: "
                    + item.content[:320]
                )
            summary.append(
                "Next: " + (cycle.feedback or "Advance the remaining criteria using this evidence.")
            )
            self._state.handoff_summary = "\n".join(summary)[:8000]
        return {
            "handoff_summary": self._state.handoff_summary,
            "no_progress": self._no_progress,
            "recovery_attempts": self._state.recovery_attempts,
            "feedback": cycle.feedback,
        }

    async def _run_adaptive(self, run: RunInput, feedback: str) -> RunState:
        """A small decision loop. Phases label actual work, never dictate a plan."""
        if self._state.deadline_at is None and self._limits.timeout_seconds is not None:
            self._state.deadline_at = (
                workflow.now() + timedelta(seconds=self._limits.timeout_seconds)
            ).isoformat()
        while True:
            self._check_limits(planning=True, model_call=True)
            if self._should_continue_as_new():
                workflow.continue_as_new(
                    run.model_copy(update={"checkpoint": self._checkpoint(feedback)})
                )
            self._state.cycle += 1
            self._state.status = "running"
            self._state.wake_at = None
            cycle = _Cycle(feedback=feedback)
            if wake_feedback := self._take_wake_feedback():
                cycle.feedback = wake_feedback
                self._no_progress = self._state.recovery_attempts = 0
            # Optional observers are explicit configuration. Empty hooks incur
            # no Activity dispatch and no ceremonial phase transitions.
            try:
                if "perceive" in run.hook_phases:
                    await self._start_phase(run, "perceive", cycle)
                    await self._end_phase({"feedback": cycle.feedback})
                await self._start_phase(run, "think", cycle)
                planned = await self._plan(run, cycle.feedback)
                self._state.input_tokens += max(0, planned.input_tokens)
                self._state.output_tokens += max(0, planned.output_tokens)
                self._state.cached_input_tokens += max(0, planned.cached_input_tokens)
                self._guard_dispatch()
                cycle.decision = planned.decision
                if "decide" in run.hook_phases:
                    await self._end_phase({"decision": planned.decision.model_dump(mode="json")})
                    await self._start_phase(run, "decide", cycle)
                cycle.error = self._decision_error(run, cycle.decision)
                # Existing histories may contain accepted empty reasons. Keep
                # their recorded branches when replaying; validate new decisions.
                if cycle.error == _BLOCKED_REASON_ERROR and not workflow.patched(
                    "require-blocked-reason-v1"
                ):
                    cycle.error = ""
                if not cycle.error:
                    decision = cycle.decision
                    if decision.task_progress is not None:
                        self._state.task_progress = decision.task_progress.model_copy(deep=True)
                    if decision.handoff_summary is not None:
                        self._state.handoff_summary = decision.handoff_summary
                        self._state.handoff_is_model = True
                await self._end_phase(
                    {"decision": cycle.decision.model_dump(mode="json"), "error": cycle.error},
                    failed=bool(cycle.error),
                )
            except ActivityError as error:
                if self._check_activity_cancellation(error):
                    raise _StopExecution("cancelled", "Cancelled by the user.") from None
                self._record_failed_model_usage(error)
                cause = error.cause
                cycle.error = (
                    cause.message[:4000]
                    if isinstance(cause, ApplicationError) and cause.type in _REPORTED_MODEL_ERRORS
                    else "Planning could not complete. Check provider availability or return a valid decision on the next attempt."
                )
                await self._end_phase({"error": cycle.error}, failed=True)
            self._check_limits()
            if not cycle.error:
                phase = "complete" if cycle.decision.kind == "finish" else "act"
                try:
                    await self._start_phase(run, phase, cycle)
                    output = await self._harness_action(run, cycle)
                except ActivityError as error:
                    if self._check_activity_cancellation(error):
                        raise _StopExecution("cancelled", "Cancelled by the user.") from None
                    self._record_failed_model_usage(error)
                    cycle.error = "The action could not complete. Inspect its actual state before issuing another operation; change strategy if the dependency remains unavailable."
                    output = {"error": cycle.error}
                # Model-maintained summaries accumulate conclusions across the
                # task. Do not replace them with the last four observations.
                cycle.custom_summary = self._state.handoff_is_model
                reflection = self._reflect_cycle(run, cycle)
                output.update(reflection)
                self._state.reason = (cycle.error or cycle.feedback or cycle.decision.reason)[:4000]
                await self._end_phase(
                    output,
                    failed=bool(
                        cycle.error
                        or (cycle.result and not cycle.result.success)
                        or (cycle.results and not any(item.success for _, item in cycle.results))
                        or (cycle.verification and not cycle.verification.passed)
                    ),
                )
            else:
                cycle.custom_summary = self._state.handoff_is_model
                self._reflect_cycle(run, cycle)
                await self._persist()
            if "reflect" in run.hook_phases:
                try:
                    await self._start_phase(run, "reflect", cycle)
                    await self._end_phase({"handoff_summary": self._state.handoff_summary})
                except ActivityError as error:
                    if self._check_activity_cancellation(error):
                        raise _StopExecution("cancelled", "Cancelled by the user.") from None
                    cycle.error = "The reflection observer failed. Inspect the extension dependency before retrying."
                    self._reflect_cycle(run, cycle)
                    await self._end_phase({"error": cycle.error}, failed=True)
            self._check_limits()
            if cycle.verification and cycle.verification.passed and not cycle.error:
                return await self._complete("succeeded", cycle.verification.feedback)
            if cycle.decision and cycle.decision.kind == "blocked" and not cycle.error:
                cycle.blocked_reason = cycle.decision.reason
            if cycle.blocked_reason:
                feedback = await self._await_external_input(cycle.blocked_reason)
                self._state.recovery_attempts = 0
            else:
                feedback = cycle.feedback

    @workflow.run
    async def run(self, run: RunInput) -> RunState:
        checkpoint = run.checkpoint
        if checkpoint is not None:
            # Continuation state belongs only to the Workflow input, never to
            # the model/tool/phase Activity payloads in the new run.
            run = run.model_copy(update={"checkpoint": None})
        else:
            self._state = RunState(goal_id=run.goal_id, status="running")
        self._run_start_cycle = self._state.cycle
        feedback = checkpoint.feedback if checkpoint else ""
        try:
            await self._persist()
            return await self._run_adaptive(run, feedback)
        except _StopExecution as stop:
            return await self._complete(stop.status, stop.reason)
        except asyncio.CancelledError:
            # Native Temporal cancellation is also reflected in the projection.
            return await self._complete("cancelled", "Execution cancelled.")


# Read-only observations whose repetition may refresh rolled-out evidence.
_OBSERVATION_TOOLS = frozenset({"read_file", "list_files", "fetch_url"})


class _StopExecution(Exception):
    def __init__(self, status: Status, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass
class _Cycle:
    feedback: str = ""
    decision: Decision | None = None
    result: ToolResult | None = None
    # Results of a parallel read-only batch, in action order.
    results: list[tuple[Action, ToolResult]] = field(default_factory=list)
    verification: VerificationResult | None = None
    error: str = ""
    blocked_reason: str = ""
    custom_summary: bool = False
