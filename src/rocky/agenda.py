"""Long-running agenda: goals decomposed into sequential steps, and scheduled runs.

A plan breaks a large objective into steps. Each step runs as an ordinary goal
with its own drafted contract, one after another; the deliverables of finished
steps are copied into the next step's workspace. After each step the model may
revise the remaining steps. A schedule submits a fixed contract once, at an
interval, or daily/weekly at a local time.

Both live in the goal database. Every change advances a row revision under a
compare-and-set update, so the owner's API calls and the worker's agenda loop
never overwrite each other, and a step's goal is created in the same
transaction that records it.
"""

import os
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from sqlalchemy import JSON, Boolean, Integer, String, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from rocky.inference import ModelSettings
from rocky.models import (
    MAX_FILES,
    MAX_FOLDERS,
    Criterion,
    GoalContract,
    GoalView,
    GrantedFolder,
    Model,
    _normalized_folders,
)
from rocky.store import TERMINAL_STATUSES, Base, GoalLifecycleRow, GoalRow, Store

MAX_PLAN_STEPS = 20
MAX_DRAFTED_STEPS = 12
# Model revisions of one plan's remaining steps; later steps run as planned.
MAX_PLAN_REVISIONS = 10
MAX_PLAN_NOTES = 20
# Consecutive failures to draft a step's contract before the owner is asked.
MAX_STEP_DRAFT_FAILURES = 3
MAX_RECENT_RUNS = 10
MIN_INTERVAL_MINUTES = 5

PlanStatus = Literal["active", "paused", "attention", "succeeded", "cancelled"]
StepStatus = Literal["pending", "running", "succeeded", "skipped", "failed"]
PLAN_ENDED = ("succeeded", "cancelled")


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime) -> str:
    """UTC ISO text; equal formats keep stored times comparable as strings."""
    return value.astimezone(UTC).isoformat(timespec="seconds")


# MARK: Plans


class PlanStepDraft(Model):
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=4000)


class PlanDecomposition(Model):
    """A model's proposed breakdown of an objective, for owner review."""

    steps: list[PlanStepDraft] = Field(min_length=1, max_length=MAX_DRAFTED_STEPS)
    # Problems the owner should resolve before starting, in their language.
    concerns: list[Criterion] = Field(default_factory=list, max_length=8)


class PlanRevision(Model):
    """The model's review of a plan after one of its steps finished."""

    # The objective is achieved; remaining steps are dropped.
    complete: bool
    # Replaces every step that has not started yet.
    remaining: list[PlanStepDraft] = Field(default_factory=list, max_length=MAX_DRAFTED_STEPS)
    # One sentence for the owner on what changed and why, in their language.
    note: str = Field(default="", max_length=1000)


class PlanDraftReview(Model):
    steps: list[PlanStepDraft]
    concerns: list[str] = Field(default_factory=list)
    # Tokens the decomposition used; not charged to any task.
    drafting_tokens: int | None = None


class PlanRequest(Model):
    objective: str = Field(min_length=1, max_length=4000)
    steps: list[PlanStepDraft] = Field(min_length=1, max_length=MAX_PLAN_STEPS)
    folders: list[GrantedFolder] = Field(default_factory=list, max_length=MAX_FOLDERS)
    files: list[GrantedFolder] = Field(default_factory=list, max_length=MAX_FILES)
    # Let the model adjust the remaining steps after each finished step.
    replan: bool = True
    # Higher runs first when several plans wait for the same slot.
    priority: int = Field(default=0, ge=-100, le=100)

    @field_validator("folders", "files")
    @classmethod
    def validate_folders(cls, values: list[str]) -> list[str]:
        return _normalized_folders(values)


class PlanStep(Model):
    step_id: str
    title: str
    objective: str
    status: StepStatus = "pending"
    # The goal of the latest attempt.
    goal_id: str | None = None
    attempts: int = 0
    # What the step's goal reported, for later steps and the owner.
    summary: str = Field(default="", max_length=2000)
    # Workspace files the step's goal delivered.
    outputs: list[str] = Field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None


class PlanView(Model):
    plan_id: str
    objective: str
    folders: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    status: PlanStatus = "active"
    reason: str = ""
    priority: int = 0
    replan: bool = True
    steps: list[PlanStep]
    notes: list[str] = Field(default_factory=list)
    revisions: int = 0
    # A step finished and the model has not reviewed the remaining steps yet.
    review_pending: bool = False
    # Drafting and review calls; each step's own usage is on its goal.
    planning_tokens: int = 0
    draft_failures: int = 0
    retry_at: str | None = None
    created_at: str
    updated_at: str
    revision: int = 0

    def current(self) -> PlanStep | None:
        """The step that runs now or next; None when every step is done."""
        return next(
            (step for step in self.steps if step.status in {"pending", "running", "failed"}),
            None,
        )

    def position(self, step: PlanStep) -> int:
        return next(index for index, item in enumerate(self.steps, 1) if item is step)


class PlanRow(Base):
    __tablename__ = "plans"
    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict] = mapped_column(JSON)


def _new_steps(drafts: list[PlanStepDraft]) -> list[PlanStep]:
    return [
        PlanStep(step_id=uuid4().hex[:12], title=item.title.strip(), objective=item.objective)
        for item in drafts
    ]


def step_goal_id(plan_id: str, step: PlanStep) -> str:
    """Deterministic, so a retried start finds the goal it already created."""
    return str(uuid5(NAMESPACE_URL, f"rocky:plan:{plan_id}:{step.step_id}:{step.attempts}"))


# MARK: Schedules


class Recurrence(Model):
    """When a schedule runs, in the owner's local time zone.

    once: at start_at. interval: every every_minutes from start_at (default:
    creation). daily: at time. weekly: at time on weekdays (1 = Monday).
    """

    kind: Literal["once", "interval", "daily", "weekly"]
    start_at: datetime | None = None
    every_minutes: int | None = Field(default=None, ge=MIN_INTERVAL_MINUTES, le=31 * 24 * 60)
    time: str | None = Field(default=None, pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    # IANA name such as Asia/Shanghai; empty uses the Mac's time zone.
    timezone: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def complete(self) -> "Recurrence":
        if self.start_at is not None and self.start_at.tzinfo is None:
            raise ValueError("start_at needs a UTC offset")
        if self.kind == "once" and self.start_at is None:
            raise ValueError("A one-time schedule needs start_at")
        if self.kind == "interval" and self.every_minutes is None:
            raise ValueError("An interval schedule needs every_minutes")
        if self.kind in {"daily", "weekly"} and self.time is None:
            raise ValueError("Daily and weekly schedules need a time such as 09:00")
        if self.kind == "weekly":
            if not self.weekdays or any(day < 1 or day > 7 for day in self.weekdays):
                raise ValueError("A weekly schedule needs weekdays from 1 (Monday) to 7")
            self.weekdays = sorted(set(self.weekdays))
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("Unknown time zone") from None
        return self

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone) if self.timezone else local_zone()

    def next_after(self, after: datetime, anchor: datetime) -> datetime | None:
        """The first run strictly after `after`; anchor is when the schedule began."""
        if self.kind == "once":
            return self.start_at if self.start_at > after else None
        if self.kind == "interval":
            start = self.start_at or anchor
            if start > after:
                return start
            step = timedelta(minutes=self.every_minutes)
            return start + step * ((after - start) // step + 1)
        zone = self.zone()
        hour, minute = (int(part) for part in self.time.split(":"))
        day = after.astimezone(zone).date()
        for offset in range(8):
            candidate_day = day + timedelta(days=offset)
            if self.kind == "weekly" and candidate_day.isoweekday() not in self.weekdays:
                continue
            candidate = _local(candidate_day, time(hour, minute), zone)
            if candidate > after:
                return candidate
        return None


def _local(day: date, at: time, zone: ZoneInfo) -> datetime:
    """A wall-clock time; one skipped by a DST change moves forward an hour."""
    value = datetime.combine(day, at, tzinfo=zone)
    round_trip = value.astimezone(UTC).astimezone(zone)
    if round_trip.replace(tzinfo=None) != value.replace(tzinfo=None):
        value = datetime.combine(day, at, tzinfo=zone) + timedelta(hours=1)
    return value.astimezone(UTC)


def local_zone() -> ZoneInfo:
    """The Mac's time zone: TZ, else the /etc/localtime link, else UTC."""
    name = os.environ.get("TZ", "").lstrip(":")
    if not name:
        try:
            target = str(Path("/etc/localtime").resolve())
            if "zoneinfo/" in target:
                name = target.split("zoneinfo/", 1)[1]
        except OSError:
            pass
    try:
        return ZoneInfo(name) if name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


class ScheduleRequest(Model):
    name: str = Field(min_length=1, max_length=120)
    contract: GoalContract
    recurrence: Recurrence
    enabled: bool = True

    @model_validator(mode="after")
    def room_for_run_note(self) -> "ScheduleRequest":
        # Each run adds a line naming the schedule and its time.
        if len(self.contract.objective) > 7600:
            raise ValueError("A scheduled objective may have at most 7600 characters")
        return self


class ScheduleView(Model):
    schedule_id: str
    name: str
    contract: GoalContract
    recurrence: Recurrence
    enabled: bool = True
    # When the current rhythm began; intervals without start_at count from it.
    anchor_at: str = ""
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_goal_id: str | None = None
    recent_goal_ids: list[str] = Field(default_factory=list)
    runs: int = 0
    # Runs not started because the previous run was still going.
    skipped: int = 0
    last_skipped_at: str | None = None
    created_at: str
    updated_at: str
    revision: int = 0


class ScheduleRow(Base):
    __tablename__ = "schedules"
    schedule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # UTC ISO text, comparable as a string; null once nothing is left to run.
    next_run_at: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict] = mapped_column(JSON)


class ScheduleBusyError(ValueError):
    """The schedule's previous run has not ended."""


def _schedule_note(view: ScheduleView, at: datetime) -> str:
    local = at.astimezone(view.recurrence.zone()).strftime("%Y-%m-%d %H:%M")
    return f"\n\n（这是定时任务“{view.name}”在 {local} 的一次运行。）"


# MARK: Storage


class AgendaStore:
    """Plans and schedules in the goal database, next to the goals they create."""

    def __init__(self, store: Store):
        self.store = store

    # Plans

    def create_plan(self, request: PlanRequest) -> PlanView:
        now = _stamp(_now())
        view = PlanView(
            plan_id=uuid4().hex,
            objective=request.objective,
            folders=request.folders,
            files=request.files,
            priority=request.priority,
            replan=request.replan,
            steps=_new_steps(request.steps),
            created_at=now,
            updated_at=now,
            revision=1,
        )
        with self.store.sessions.begin() as session:
            session.add(
                PlanRow(
                    plan_id=view.plan_id,
                    status=view.status,
                    priority=view.priority,
                    created_at=now,
                    revision=1,
                    data=view.model_dump(mode="json"),
                )
            )
        return view

    def plan(self, plan_id: str) -> PlanView | None:
        with self.store.sessions() as session:
            row = session.get(PlanRow, plan_id)
            return PlanView.model_validate(row.data) if row else None

    def plans(self, statuses: tuple[str, ...] | None = None) -> list[PlanView]:
        """Plans by priority, then age; ended ones after the rest, newest first."""
        with self.store.sessions() as session:
            query = select(PlanRow.data)
            if statuses is not None:
                query = query.where(PlanRow.status.in_(statuses))
            rows = session.scalars(query.order_by(PlanRow.priority.desc(), PlanRow.created_at))
            views = [PlanView.model_validate(data) for data in rows]
        ended = [view for view in views if view.status in PLAN_ENDED]
        ended.sort(key=lambda view: view.updated_at, reverse=True)
        return [view for view in views if view.status not in PLAN_ENDED] + ended

    def update_plan(
        self,
        plan_id: str,
        change: Callable[[PlanView], None],
        *,
        expected_revision: int | None = None,
        session: Session | None = None,
    ) -> PlanView | None:
        """Apply change under a revision check; None when the plan moved on.

        Raises KeyError for an unknown plan and lets ValueError from change
        through, leaving the plan as it was.
        """
        if session is None:
            with self.store.sessions.begin() as session:
                return self.update_plan(
                    plan_id, change, expected_revision=expected_revision, session=session
                )
        row = session.get(PlanRow, plan_id)
        if row is None:
            raise KeyError(plan_id)
        if expected_revision is not None and row.revision != expected_revision:
            return None
        view = PlanView.model_validate(row.data)
        change(view)
        view.revision = row.revision + 1
        view.updated_at = _stamp(_now())
        changed = session.execute(
            update(PlanRow)
            .where(PlanRow.plan_id == plan_id, PlanRow.revision == row.revision)
            .values(
                status=view.status,
                priority=view.priority,
                revision=view.revision,
                data=view.model_dump(mode="json"),
            )
        )
        if not changed.rowcount:
            return None
        return view

    def start_step(
        self,
        plan: PlanView,
        step_id: str,
        contract: GoalContract,
        model_settings: ModelSettings | None,
        outputs: list[str],
        drafting_tokens: int = 0,
    ) -> PlanView | None:
        """Create the step's goal and mark the step running, in one transaction."""
        with self.store.sessions.begin() as session:

            def start(view: PlanView) -> None:
                step = next(item for item in view.steps if item.step_id == step_id)
                if step.status != "pending" or view.status != "active":
                    raise ValueError("The step is no longer waiting to start")
                step.goal_id = step_goal_id(view.plan_id, step)
                self.store.insert_goal(session, step.goal_id, contract, model_settings)
                step.status, step.started_at = "running", _stamp(_now())
                step.finished_at, step.summary, step.outputs = None, "", []
                view.draft_failures, view.retry_at = 0, None
                view.planning_tokens += drafting_tokens
                view.reason = f"正在执行第 {view.position(step)} 步：{step.title}"
                if outputs:
                    view.reason += f"（已带上前面步骤的 {len(outputs)} 个文件）"

            try:
                return self.update_plan(
                    plan.plan_id, start, expected_revision=plan.revision, session=session
                )
            except ValueError:
                return None

    def delete_plan(self, plan_id: str) -> None:
        """Remove a plan record; its goals stay in the task list."""
        with self.store.sessions.begin() as session:
            row = session.get(PlanRow, plan_id)
            if row is None:
                raise KeyError(plan_id)
            view = PlanView.model_validate(row.data)
            if view.status not in PLAN_ENDED and self._running_goal(view, session):
                raise ValueError("Stop the plan before deleting it")
            session.delete(row)

    @staticmethod
    def goal_status(session: Session, goal_id: str) -> str | None:
        """A goal's status read in the caller's transaction; None once deleted."""
        row = session.get(GoalRow, goal_id)
        lifecycle = session.get(GoalLifecycleRow, goal_id)
        if row is None or (lifecycle and lifecycle.deleted):
            return None
        return row.state["status"]

    def _running_goal(self, view: PlanView, session: Session | None = None) -> str | None:
        step = view.current()
        if step is None or step.status != "running" or step.goal_id is None:
            return None
        if session is None:
            with self.store.sessions() as session:
                return self._running_goal(view, session)
        status = self.goal_status(session, step.goal_id)
        return step.goal_id if status and status not in TERMINAL_STATUSES else None

    def cancel_plan(self, plan_id: str) -> PlanView:
        def cancel(view: PlanView) -> None:
            if view.status in PLAN_ENDED:
                raise ValueError("The plan has already ended")
            view.status, view.reason = "cancelled", "已由你停止。"
            view.review_pending = False

        view = self.update_plan(plan_id, cancel)
        if view is None:
            raise ValueError("The plan changed; refresh and try again")
        if goal_id := self._running_goal(view):
            try:
                self.store.enqueue_command(goal_id, "cancel")
            except (KeyError, ValueError):
                pass
        return view

    def owner_action(self, plan_id: str, action: str) -> PlanView:
        """pause, resume, retry (the current step) or skip (the current step)."""

        def apply(view: PlanView) -> None:
            if view.status in PLAN_ENDED:
                raise ValueError("The plan has already ended")
            step = view.current()
            if action == "pause":
                if view.status != "active":
                    raise ValueError("Only an active plan can be paused")
                view.status = "paused"
                view.reason = "已暂停；正在执行的步骤会继续完成，之后不再开始新步骤。"
            elif action == "resume":
                if view.status != "paused":
                    raise ValueError("Only a paused plan can be resumed")
                view.status, view.reason = "active", "已继续。"
            elif action == "retry":
                if step is None or step.status == "running":
                    raise ValueError("No step is waiting for a retry")
                if step.status == "failed":
                    step.attempts += 1
                    step.status, step.goal_id, step.summary = "pending", None, ""
                view.status, view.draft_failures, view.retry_at = "active", 0, None
                view.reason = f"将重新执行第 {view.position(step)} 步：{step.title}"
            elif action == "skip":
                if step is None:
                    raise ValueError("No step is left to skip")
                if step.status == "running":
                    raise ValueError("Stop the running step's task before skipping it")
                step.status, step.finished_at = "skipped", _stamp(_now())
                view.status, view.draft_failures, view.retry_at = "active", 0, None
                view.reason = f"已跳过第 {view.position(step)} 步：{step.title}"
            else:
                raise ValueError("Unsupported plan action")

        view = self.update_plan(plan_id, apply)
        if view is None:
            raise ValueError("The plan changed; refresh and try again")
        return view

    def replace_remaining(self, plan_id: str, drafts: list[PlanStepDraft]) -> PlanView:
        """The owner rewrites the steps that have not started."""

        def apply(view: PlanView) -> None:
            if view.status in PLAN_ENDED:
                raise ValueError("The plan has already ended")
            kept = [step for step in view.steps if step.status != "pending"]
            if len(kept) + len(drafts) > MAX_PLAN_STEPS:
                raise ValueError(f"A plan has at most {MAX_PLAN_STEPS} steps")
            view.steps = kept + _new_steps(drafts)

        view = self.update_plan(plan_id, apply)
        if view is None:
            raise ValueError("The plan changed; refresh and try again")
        return view

    # Schedules

    def create_schedule(self, request: ScheduleRequest) -> ScheduleView:
        now = _now()
        view = ScheduleView(
            schedule_id=uuid4().hex,
            name=request.name,
            contract=request.contract,
            recurrence=request.recurrence,
            enabled=request.enabled,
            created_at=_stamp(now),
            updated_at=_stamp(now),
            revision=1,
        )
        self._plan_next(view, now)
        with self.store.sessions.begin() as session:
            session.add(
                ScheduleRow(
                    schedule_id=view.schedule_id,
                    enabled=view.enabled,
                    next_run_at=view.next_run_at,
                    created_at=view.created_at,
                    revision=1,
                    data=view.model_dump(mode="json"),
                )
            )
        return view

    @staticmethod
    def _plan_next(view: ScheduleView, now: datetime) -> None:
        """Start the schedule's rhythm afresh from now."""
        recurrence = view.recurrence
        view.anchor_at = _stamp(now)
        if not view.enabled:
            view.next_run_at = None
        elif recurrence.kind == "once":
            # A minute's grace, so a run set for "now" still happens.
            if recurrence.start_at < now - timedelta(minutes=1):
                raise ValueError("The one-time schedule's time has already passed")
            view.next_run_at = _stamp(recurrence.start_at)
        else:
            view.next_run_at = _stamp(recurrence.next_after(now, now))

    def schedule(self, schedule_id: str) -> ScheduleView | None:
        with self.store.sessions() as session:
            row = session.get(ScheduleRow, schedule_id)
            return ScheduleView.model_validate(row.data) if row else None

    def schedules(self) -> list[ScheduleView]:
        with self.store.sessions() as session:
            rows = session.scalars(select(ScheduleRow.data).order_by(ScheduleRow.created_at))
            return [ScheduleView.model_validate(data) for data in rows]

    def _update_schedule(
        self, session: Session, schedule_id: str, change: Callable[[ScheduleView], None]
    ) -> ScheduleView:
        row = session.get(ScheduleRow, schedule_id)
        if row is None:
            raise KeyError(schedule_id)
        view = ScheduleView.model_validate(row.data)
        change(view)
        view.revision = row.revision + 1
        view.updated_at = _stamp(_now())
        changed = session.execute(
            update(ScheduleRow)
            .where(ScheduleRow.schedule_id == schedule_id, ScheduleRow.revision == row.revision)
            .values(
                enabled=view.enabled,
                next_run_at=view.next_run_at,
                revision=view.revision,
                data=view.model_dump(mode="json"),
            )
        )
        if not changed.rowcount:
            raise ValueError("The schedule changed; refresh and try again")
        return view

    def update_schedule(self, schedule_id: str, request: ScheduleRequest) -> ScheduleView:
        now = _now()

        def apply(view: ScheduleView) -> None:
            timing = view.recurrence != request.recurrence or (request.enabled and not view.enabled)
            view.name, view.contract = request.name, request.contract
            view.recurrence, view.enabled = request.recurrence, request.enabled
            if timing or not view.enabled:
                # A changed rhythm starts from now; an unchanged one keeps its next run.
                self._plan_next(view, now)

        with self.store.sessions.begin() as session:
            return self._update_schedule(session, schedule_id, apply)

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> ScheduleView:
        current = self.schedule(schedule_id)
        if current is None:
            raise KeyError(schedule_id)
        request = ScheduleRequest(
            name=current.name,
            contract=current.contract,
            recurrence=current.recurrence,
            enabled=enabled,
        )
        return self.update_schedule(schedule_id, request)

    def delete_schedule(self, schedule_id: str) -> None:
        """Remove a schedule; goals it already started are kept."""
        with self.store.sessions.begin() as session:
            row = session.get(ScheduleRow, schedule_id)
            if row is None:
                raise KeyError(schedule_id)
            session.delete(row)

    def _start_run(
        self,
        session: Session,
        view: ScheduleView,
        at: datetime,
        goal_id: str,
        model_settings: ModelSettings | None,
    ) -> GoalView:
        if view.last_goal_id:
            previous = self.goal_status(session, view.last_goal_id)
            if previous is not None and previous not in TERMINAL_STATUSES:
                raise ScheduleBusyError("The previous run has not ended yet")
        contract = view.contract.model_copy(deep=True)
        contract.objective += _schedule_note(view, at)
        goal = self.store.insert_goal(session, goal_id, contract, model_settings)
        view.last_run_at, view.last_goal_id, view.runs = _stamp(at), goal_id, view.runs + 1
        view.recent_goal_ids = [goal_id, *view.recent_goal_ids][:MAX_RECENT_RUNS]
        return goal

    def run_schedule_now(self, schedule_id: str, model_settings: ModelSettings | None) -> GoalView:
        """Start one run at once; the regular rhythm is unchanged."""
        result: list[GoalView] = []

        def apply(view: ScheduleView) -> None:
            result.append(self._start_run(session, view, _now(), str(uuid4()), model_settings))
            if view.recurrence.kind == "once":
                view.next_run_at = None

        with self.store.sessions.begin() as session:
            self._update_schedule(session, schedule_id, apply)
        return result[0]

    def fire_due_schedules(
        self, model_settings: ModelSettings | None, now: datetime | None = None
    ) -> list[str]:
        """Start every run that is due; returns the new goal IDs.

        Missed runs (the Mac was asleep or off) collapse into one. A run whose
        previous run is still going is skipped and counted, not queued.
        """
        now = now or _now()
        with self.store.sessions() as session:
            due = session.scalars(
                select(ScheduleRow.schedule_id).where(
                    ScheduleRow.enabled.is_(True),
                    ScheduleRow.next_run_at.is_not(None),
                    ScheduleRow.next_run_at <= _stamp(now),
                )
            ).all()
        started: list[str] = []
        for schedule_id in due:
            try:
                if goal_id := self._fire(schedule_id, now, model_settings):
                    started.append(goal_id)
            except (KeyError, ValueError):
                # Deleted or changed by the owner meanwhile; the transaction
                # rolled back, and the next tick sees the new timing.
                continue
        return started

    def _fire(
        self, schedule_id: str, now: datetime, model_settings: ModelSettings | None
    ) -> str | None:
        started: list[str] = []
        with self.store.sessions.begin() as session:

            def fire(view: ScheduleView) -> None:
                if not view.enabled or not view.next_run_at or view.next_run_at > _stamp(now):
                    return
                planned = datetime.fromisoformat(view.next_run_at)
                goal_id = str(
                    uuid5(NAMESPACE_URL, f"rocky:schedule:{schedule_id}:{view.next_run_at}")
                )
                try:
                    self._start_run(session, view, planned, goal_id, model_settings)
                    started.append(goal_id)
                except ScheduleBusyError:
                    view.skipped += 1
                    view.last_skipped_at = _stamp(planned)
                anchor = datetime.fromisoformat(view.anchor_at or view.created_at)
                following = (
                    None
                    if view.recurrence.kind == "once"
                    else view.recurrence.next_after(max(now, planned), anchor)
                )
                view.next_run_at = _stamp(following) if following else None

            self._update_schedule(session, schedule_id, fire)
        return started[0] if started else None

    def busy_plan_goals(self) -> int:
        """Plan steps whose goal is still running; they share the agenda's slots."""
        views = self.plans(("active", "paused", "attention"))
        with self.store.sessions() as session:
            return sum(1 for view in views if self._running_goal(view, session))
