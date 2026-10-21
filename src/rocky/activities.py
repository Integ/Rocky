"""All non-deterministic work is isolated in Temporal Activities."""

import asyncio

from temporalio import activity
from temporalio.exceptions import ApplicationError

from rocky.models import (
    PlanInput,
    PlanResult,
    RunState,
    ToolInput,
    ToolResult,
    VerificationResult,
    VerifyInput,
)
from rocky.planner import OpenAIPlanner
from rocky.store import Store
from rocky.tools import WorkspaceTools


class Activities:
    def __init__(self, store: Store, tools: WorkspaceTools, planner: OpenAIPlanner):
        self.store, self.tools, self.planner = store, tools, planner

    async def check_cancelled(self, goal_id: str) -> None:
        if await asyncio.to_thread(self.store.is_cancel_requested, goal_id):
            raise ApplicationError("Goal cancelled", type="GoalCancelled", non_retryable=True)

    @activity.defn(name="rocky.plan")
    async def plan(self, request: PlanInput) -> PlanResult:
        await self.check_cancelled(request.run.goal_id)
        return await self.planner.plan(request)

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
        verdict = await self.planner.review(request, contents)
        verdict.artifacts = files.artifacts
        return verdict

    @activity.defn(name="rocky.persist")
    async def persist(self, state: RunState) -> None:
        await asyncio.to_thread(self.store.save_state, state)
