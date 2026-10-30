"""All non-deterministic work is isolated in Temporal Activities."""

import asyncio
from datetime import timedelta

from temporalio import activity
from temporalio.exceptions import ApplicationError

from rocky.approval_policy import ApprovalPolicy
from rocky.evidence import (
    audit_review,
    collect_review_evidence,
    review_checklist,
    verify_checklist,
)
from rocky.harness import HarnessHooks
from rocky.memory import MemoryManager, StoreMemoryProvider
from rocky.models import (
    PhaseInput,
    PhaseResult,
    PlanInput,
    PlanResult,
    RunInput,
    RunState,
    ToolInput,
    ToolResult,
    VerificationResult,
    VerifyInput,
)
from rocky.planner import OpenAIPlanner, PlannerError, TransientModelError
from rocky.store import TERMINAL_STATUSES, Store
from rocky.tools import WorkspaceTools


def _model_failure(error: PlannerError) -> ApplicationError:
    """Let Temporal retry only failures that produced no model output."""
    if isinstance(error, TransientModelError):
        return ApplicationError(
            str(error),
            type="ModelTransientError",
            next_retry_delay=timedelta(seconds=error.retry_after) if error.retry_after else None,
        )
    return ApplicationError(str(error), error.usage, type="ModelDecisionError", non_retryable=True)


_HEARTBEAT_SECONDS = 15


async def _with_heartbeat(call):
    """Report liveness during a long model call so a lost worker is noticed early."""
    if not activity.in_activity():
        return await call

    async def beat() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(_HEARTBEAT_SECONDS)

    beating = asyncio.create_task(beat())
    try:
        return await call
    finally:
        beating.cancel()


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
        return await _with_heartbeat(self._plan(request))

    async def _plan(self, request: PlanInput) -> PlanResult:
        await self.check_cancelled(request.run.goal_id)
        request = request.model_copy(deep=True)
        request.memory = await asyncio.to_thread(self.memory.build, request)
        policy = await asyncio.to_thread(self.approval_policy)
        request.approvals = policy.planning_context()
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
        policy = await asyncio.to_thread(self.approval_policy)
        return await _with_heartbeat(asyncio.to_thread(self.tools.execute, request, policy))

    def approval_policy(self) -> ApprovalPolicy:
        """The owner's current rules; environment settings are the defaults."""
        default = getattr(self.tools, "default_approval_policy", ApprovalPolicy)
        return self.store.approval_policy(default())

    @activity.defn(name="rocky.verify")
    async def verify(self, request: VerifyInput) -> VerificationResult:
        return await _with_heartbeat(self._verify(request))

    async def _verify(self, request: VerifyInput) -> VerificationResult:
        await self.check_cancelled(request.run.goal_id)
        files = await asyncio.to_thread(self.tools.verify_files, request.run)
        if not files.passed:
            return files
        contents = await asyncio.to_thread(self.tools.review_text, request.run)
        # The reviewer checks deliverables against what actually happened:
        # sources read, other files changed and the recorded tool actions.
        evidence = await asyncio.to_thread(self.review_evidence, request.run)
        deliverables = await asyncio.to_thread(self.deliverable_texts, request.run)
        changed = await asyncio.to_thread(self.changed_texts, request.run, evidence)
        checklist = review_checklist(deliverables)
        # Values the application verifies itself need no citation from review.
        verified = verify_checklist(checklist, evidence, changed)
        await self.check_cancelled(request.run.goal_id)
        try:
            verdict = await self.planner.review(
                request,
                contents,
                evidence=evidence,
                checklist=[
                    item
                    for item in checklist
                    if (item["deliverable"], item["pointer"]) not in verified
                ],
                verified=[
                    {**item, "support": verified[(item["deliverable"], item["pointer"])]}
                    for item in checklist
                    if (item["deliverable"], item["pointer"]) in verified
                ],
            )
        except PlannerError as error:
            # A completed but unusable review consumed tokens; never repeat it.
            raise _model_failure(error) from None
        verdict.artifacts = files.artifacts
        # The application, not the reviewer, decides whether claims are supported.
        verdict.claims, problems = audit_review(
            verdict.claims, evidence, deliverables, checklist, verified=verified, files=changed
        )
        if problems:
            if verdict.passed:
                verdict.feedback = (
                    "Review passed, but its evidence check failed. " + verdict.feedback
                )
            verdict.passed = False
            verdict.feedback = (
                "Unsupported claims:\n- " + "\n- ".join(problems[:12]) + "\n" + verdict.feedback
            )[:4000]
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
                    cached_input_tokens=verdict.cached_input_tokens,
                )
        return verdict

    def deliverable_texts(self, run: RunInput) -> dict[str, str]:
        texts = {}
        for deliverable in run.contract.deliverables:
            described = self.tools.describe_file(run, deliverable.path, 1_000_000)
            texts[described["path"]] = described.get("content", "")
        return texts

    def changed_texts(self, run: RunInput, evidence: dict) -> dict[str, str]:
        return {
            item["path"]: self.tools.describe_file(run, item["path"], 1_000_000).get("content", "")
            for item in evidence["changed_files"]
        }

    def review_evidence(self, run: RunInput) -> dict:
        steps = self.store.all_execution_steps(run.goal_id)
        return collect_review_evidence(run, steps, self.tools)

    @activity.defn(name="rocky.persist")
    async def persist(self, state: RunState) -> None:
        await asyncio.to_thread(self.store.save_state, state)
        stop_jobs = getattr(self.tools, "stop_jobs", None)
        if state.status in TERMINAL_STATUSES and stop_jobs is not None:
            # A finished task leaves no background job running.
            try:
                await asyncio.to_thread(stop_jobs, state.goal_id)
            except Exception:
                activity.logger.warning("Could not stop a finished task's background jobs")
