"""The worker's agenda loop: starts scheduled runs and advances plans step by step."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from rocky.agenda import (
    MAX_DRAFTED_STEPS,
    MAX_PLAN_NOTES,
    MAX_PLAN_REVISIONS,
    MAX_PLAN_STEPS,
    MAX_STEP_DRAFT_FAILURES,
    AgendaStore,
    PlanStep,
    PlanView,
    _new_steps,
    _stamp,
    step_goal_id,
)
from rocky.config import Settings
from rocky.inference import ModelSettings
from rocky.models import GoalDraft, GoalView
from rocky.planner import OpenAIPlanner, PlannerError
from rocky.store import TERMINAL_STATUSES, Store
from rocky.tools import WorkspaceTools

log = logging.getLogger(__name__)
_MAX_OBJECTIVE = 8000
_ENDED_LABELS = {
    "failed": "执行失败",
    "cancelled": "已停止",
    "budget_exhausted": "预算用完",
    "deadline_exceeded": "超出时限",
}


def step_summary(goal: GoalView) -> tuple[str, list[str]]:
    """What a finished step reported, and the deliverables it left on disk."""
    state = goal.state
    text = state.handoff_summary.strip()
    if not text and state.task_progress.completed:
        text = "；".join(state.task_progress.completed)
    text = text or state.reason
    delivered = {artifact.path for artifact in state.artifacts}
    outputs = [item.path for item in goal.contract.deliverables if item.path in delivered]
    return text[:2000], outputs or [item.path for item in goal.contract.deliverables]


def step_objective(plan: PlanView, step: PlanStep, inputs: dict[str, list[str]]) -> str:
    """The step's own objective with the plan around it, within the contract limit."""
    position, total = plan.position(step), len(plan.steps)

    def compose(summary_chars: int, goal_chars: int) -> str:
        lines = [
            step.objective,
            "",
            f"—— 背景：这是一个长期目标的第 {position}/{total} 步“{step.title}”。",
            f"长期目标：{plan.objective[:goal_chars]}",
        ]
        finished = [
            (index, item)
            for index, item in enumerate(plan.steps, 1)
            if item.status in {"succeeded", "skipped"}
        ]
        if finished:
            lines.append("前面的步骤：")
        for index, item in finished:
            if item.status == "skipped":
                lines.append(f"第 {index} 步“{item.title}”：已跳过。")
                continue
            lines.append(f"第 {index} 步“{item.title}”：{item.summary[:summary_chars]}")
            if copied := inputs.get(item.step_id):
                lines.append(f"  它的产出已复制到本任务工作区：{', '.join(copied)}")
        lines.append("只完成本步骤；后面的步骤会另外执行。")
        return "\n".join(lines)

    for summary_chars, goal_chars in ((800, 2000), (300, 1000), (100, 400), (0, 200)):
        text = compose(summary_chars, goal_chars)
        if len(text) <= _MAX_OBJECTIVE:
            return text
    return text[:_MAX_OBJECTIVE]


class AgendaRunner:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        planner: OpenAIPlanner,
        tools: WorkspaceTools | None = None,
    ):
        self.store, self.settings, self.planner = store, settings, planner
        self.agenda = AgendaStore(store)
        self.tools = tools or WorkspaceTools(settings)

    def _model_settings(self) -> ModelSettings:
        return self.store.get_model_settings(
            ModelSettings(
                model=self.settings.model, reasoning_effort=self.settings.reasoning_effort
            )
        )

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        model_settings = await asyncio.to_thread(self._model_settings)
        started = await asyncio.to_thread(self.agenda.fire_due_schedules, model_settings, now)
        if started:
            log.info("Started %d scheduled run(s)", len(started))
        plans = await asyncio.to_thread(self.agenda.plans, ("active", "paused", "attention"))
        busy = await asyncio.to_thread(self.agenda.busy_plan_goals)
        slots = self.settings.agenda_concurrency - busy
        for plan in plans:
            try:
                if await self._advance(plan, slots > 0, now, model_settings):
                    slots -= 1
            except Exception:
                # Never log plan text or model output; other plans continue.
                log.warning("Plan step deferred; will retry")

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.warning("Agenda tick deferred; will retry")
            await asyncio.sleep(self.settings.agenda_interval_seconds)

    async def _advance(
        self, plan: PlanView, may_start: bool, now: datetime, model_settings: ModelSettings
    ) -> bool:
        """One move for one plan; True when it started a step's goal."""
        step = plan.current()
        if step is not None and step.goal_id and step.status in {"running", "failed"}:
            if await asyncio.to_thread(self._observe, plan, step):
                return False
        if plan.status != "active":
            return False
        if plan.retry_at and plan.retry_at > _stamp(now):
            return False
        if plan.review_pending:
            await self._review(plan, now, model_settings)
            return False
        if step is None:
            await asyncio.to_thread(self._update, plan, self._finish)
            return False
        if step.status == "pending" and may_start:
            return await self._start(plan, step, now, model_settings)
        return False

    def _update(self, plan: PlanView, change) -> PlanView | None:
        return self.agenda.update_plan(plan.plan_id, change, expected_revision=plan.revision)

    @staticmethod
    def _finish(view: PlanView) -> None:
        done = sum(1 for step in view.steps if step.status == "succeeded")
        view.status = "succeeded"
        view.reason = f"全部完成：{done} 步已完成" + (
            f"，{skipped} 步跳过。" if (skipped := len(view.steps) - done) else "。"
        )

    def _observe(self, plan: PlanView, step: PlanStep) -> bool:
        """Follow the step's goal; True when the plan changed."""
        goal = self.store.get_goal(step.goal_id)
        status = goal.state.status if goal else None
        position = plan.position(step)

        def change(view: PlanView) -> None:
            current = next(item for item in view.steps if item.step_id == step.step_id)
            if status == "succeeded":
                current.summary, current.outputs = step_summary(goal)
                current.status, current.finished_at = "succeeded", _stamp(datetime.now(UTC))
                # The model reviews what is left, even after the last step:
                # a finding may need another step.
                view.review_pending = view.replan and view.revisions < MAX_PLAN_REVISIONS
                if view.status == "attention":
                    view.status = "active"
                view.reason = f"第 {position} 步“{current.title}”已完成。"
            elif status is None or status in TERMINAL_STATUSES:
                current.status = "failed"
                current.finished_at = _stamp(datetime.now(UTC))
                ended = "任务已被删除" if status is None else _ENDED_LABELS.get(status, status)
                view.status = "attention" if view.status != "paused" else view.status
                view.reason = (
                    f"第 {position} 步“{current.title}”没有完成（{ended}）。可以在任务里继续或"
                    "追加要求；也可以在议程里重试或跳过这一步。"
                )
            else:
                # The owner continued the step's goal; follow it again.
                current.status, current.finished_at = "running", None
                if view.status == "attention":
                    view.status = "active"
                view.reason = f"正在执行第 {position} 步：{current.title}"

        ended = status is None or status in TERMINAL_STATUSES
        if status != "succeeded" and step.status == ("failed" if ended else "running"):
            return False
        return self._update(plan, change) is not None

    async def _review(self, plan: PlanView, now: datetime, model_settings: ModelSettings) -> None:
        kept = [step for step in plan.steps if step.status != "pending"]
        progress: dict[str, Any] = {
            "plan": {"objective": plan.objective},
            "finished": [
                {
                    "title": step.title,
                    "objective": step.objective[:1500],
                    "status": step.status,
                    "summary": step.summary,
                    "files": step.outputs,
                }
                for step in kept
            ],
            "remaining": [
                {"title": step.title, "objective": step.objective}
                for step in plan.steps
                if step.status == "pending"
            ],
            "max_remaining": max(0, min(MAX_DRAFTED_STEPS, MAX_PLAN_STEPS - len(kept))),
        }
        try:
            revision, tokens = await self.planner.revise_plan(progress, model_settings)
        except PlannerError:
            await asyncio.to_thread(self._defer, plan, now, review=True)
            return
        limit = progress["max_remaining"]

        def apply(view: PlanView) -> None:
            view.review_pending, view.draft_failures, view.retry_at = False, 0, None
            view.revisions += 1
            view.planning_tokens += tokens
            done = [step for step in view.steps if step.status != "pending"]
            pending = [step for step in view.steps if step.status == "pending"]
            if revision.complete:
                for step in pending:
                    step.status, step.summary = "skipped", "目标已达成，不再需要这一步。"
                note = revision.note or "前面的步骤已经达成目标，其余步骤不再执行。"
            else:
                drafted = revision.remaining[:limit]
                unchanged = [(s.title, s.objective) for s in pending] == [
                    (d.title.strip(), d.objective) for d in drafted
                ]
                if not unchanged:
                    pending = _new_steps(drafted)
                note = revision.note
            view.steps = done + pending
            if note:
                view.notes = [*view.notes, note][-MAX_PLAN_NOTES:]

        await asyncio.to_thread(self._update, plan, apply)

    def _defer(self, plan: PlanView, now: datetime, *, review: bool = False) -> None:
        """Back off after a failed model call; ask the owner after repeated failures."""

        def apply(view: PlanView) -> None:
            view.draft_failures += 1
            if view.draft_failures < MAX_STEP_DRAFT_FAILURES:
                minutes = 2**view.draft_failures
                view.retry_at = _stamp(now + timedelta(minutes=minutes))
                return
            view.draft_failures, view.retry_at = 0, None
            if review:
                # The plan still stands; continue without the review.
                view.review_pending = False
                view.notes = [*view.notes, "暂时无法调整计划，按原计划继续。"][-MAX_PLAN_NOTES:]
            else:
                view.status = "attention"
                view.reason = "多次未能为下一步生成任务要求，请检查模型设置后点“重试”。"

        self._update(plan, apply)

    async def _start(
        self, plan: PlanView, step: PlanStep, now: datetime, model_settings: ModelSettings
    ) -> bool:
        goal_id = step_goal_id(plan.plan_id, step)
        planned = {
            item.step_id: [f"inputs/step-{index}/{path}" for path in item.outputs]
            for index, item in enumerate(plan.steps, 1)
            if item.status == "succeeded" and item.goal_id and item.outputs
        }
        draft = GoalDraft(
            objective=step_objective(plan, step, planned), folders=plan.folders, files=plan.files
        )
        try:
            drafted = await self.planner.prepare_contract(draft, model_settings)
        except PlannerError:
            await asyncio.to_thread(self._defer, plan, now)
            return False

        def copy() -> list[str]:
            copied = []
            for index, item in enumerate(plan.steps, 1):
                if item.step_id in planned:
                    copied += self.tools.copy_goal_files(
                        item.goal_id, item.outputs, goal_id, f"inputs/step-{index}"
                    )
            return copied

        copied = await asyncio.to_thread(copy)
        started = await asyncio.to_thread(
            self.agenda.start_step,
            plan,
            step.step_id,
            drafted.contract(),
            model_settings,
            copied,
            drafted.drafting_tokens,
        )
        return started is not None
