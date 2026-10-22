"""All non-deterministic work is isolated in Temporal Activities."""

import asyncio
from datetime import timedelta

from temporalio import activity
from temporalio.exceptions import ApplicationError

from rocky.harness import HarnessHooks
from rocky.memory import MemoryManager, StoreMemoryProvider
from rocky.models import (
    PhaseInput,
    PhaseResult,
    PlanInput,
    PlanResult,
    RunState,
    ToolInput,
    ToolResult,
    VerificationResult,
    VerifyInput,
)
from rocky.planner import OpenAIPlanner, PlannerError, TransientModelError
from rocky.store import Store
from rocky.tools import WorkspaceTools


def _model_failure(error: PlannerError) -> ApplicationError:
    """Let Temporal retry only failures that produced no model output."""
    if isinstance(error, TransientModelError):
        return ApplicationError(
            str(error),
            type="ModelTransientError",
            next_retry_delay=timedelta(seconds=error.retry_after) if error.retry_after else None,
        )
    return ApplicationError(str(error), type="ModelDecisionError", non_retryable=True)


class Activities:
    def __init__(
        self,
        store: Store,
        tools: WorkspaceTools,
        planner: OpenAIPlanner,
        *,
        hooks: HarnessHooks | None = None,
        memory: MemoryManager | None = None,
    ):
        self.store, self.tools, self.planner = store, tools, planner
        self.hooks = hooks or HarnessHooks()
        self.memory = memory or MemoryManager([StoreMemoryProvider(store)])

    async def check_cancelled(self, goal_id: str) -> None:
        if await asyncio.to_thread(self.store.is_cancel_requested, goal_id):
            raise ApplicationError("Goal cancelled", type="GoalCancelled", non_retryable=True)

    @activity.defn(name="rocky.plan")
    async def plan(self, request: PlanInput) -> PlanResult:
        await self.check_cancelled(request.run.goal_id)
        request = request.model_copy(deep=True)
        request.memory = await asyncio.to_thread(self.memory.build, request)
        await self.check_cancelled(request.run.goal_id)
        try:
            return await self.planner.plan(request)
        except PlannerError as error:
            raise _model_failure(error) from None

    @activity.defn(name="rocky.phase")
    async def phase(self, request: PhaseInput) -> PhaseResult:
        await self.check_cancelled(request.run.goal_id)
        try:
            return await self.hooks.run(request)
        except Exception:
            # Extension exceptions can contain request bodies or credentials.
            # Sanitize before Temporal serializes the Activity failure itself.
            raise ApplicationError(
                "Phase hook failed", type="HarnessHookFailed", non_retryable=True
            ) from None

    @activity.defn(name="rocky.execute")
    async def execute(self, request: ToolInput) -> ToolResult:
        await self.check_cancelled(request.run.goal_id)
        return await asyncio.to_thread(self.tools.execute, request)

    @activity.defn(name="rocky.verify")
    async def verify(self, request: VerifyInput) -> VerificationResult:
        await self.check_cancelled(request.run.goal_id)
        files = await asyncio.to_thread(self.tools.verify_files, request.run)
        if not files.passed:
            return files
        contents = await asyncio.to_thread(self.tools.review_text, request.run)
        await self.check_cancelled(request.run.goal_id)
        try:
            verdict = await self.planner.review(request, contents)
        except PlannerError as error:
            # A completed but unusable review consumed tokens; never repeat it.
            raise _model_failure(error) from None
        verdict.artifacts = files.artifacts
        if verdict.passed:
            # Background jobs or another process may change an artifact while
            # the model reviews it. Completion must refer to the checked bytes.
            current = await asyncio.to_thread(self.tools.verify_files, request.run)
            await self.check_cancelled(request.run.goal_id)
            if not current.passed or current.artifacts != files.artifacts:
                return VerificationResult(
                    passed=False,
                    feedback="Artifacts changed during verification. Inspect the current files, stop concurrent writers and request verification again.",
                    artifacts=current.artifacts,
                    input_tokens=verdict.input_tokens,
                    output_tokens=verdict.output_tokens,
                )
        return verdict

    @activity.defn(name="rocky.persist")
    async def persist(self, state: RunState) -> None:
        await asyncio.to_thread(self.store.save_state, state)
