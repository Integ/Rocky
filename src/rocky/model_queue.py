"""Cross-process model admission and short-lived, content-free diagnostics."""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Integer, String, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from rocky.store import TERMINAL_STATUSES, Base, GoalLifecycleRow, GoalRow, Store

WAIT_LEASE_SECONDS = 60
# A dispatched request has a hard 900-second transport limit. Do not reclaim
# its capacity early if its process disappears while the provider is working.
RUN_LEASE_SECONDS = 960


def _now():
    return datetime.now(UTC)


class ModelGateRow(Base):
    __tablename__ = "model_gate"
    key: Mapped[str] = mapped_column(String(20), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    capacity: Mapped[int] = mapped_column(Integer)
    reserved: Mapped[int] = mapped_column(Integer)


class ModelRequestRow(Base):
    __tablename__ = "model_requests"
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    goal_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    purpose: Mapped[str] = mapped_column(String(24))
    priority: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[str] = mapped_column(String(64))
    retry_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)


class ModelRequestCancelled(RuntimeError):
    pass


class ModelQueue:
    def __init__(self, store: Store, settings):
        self.store = store
        self.capacity = settings.max_concurrent_model_calls
        self.reserved = min(settings.model_interactive_reserve, self.capacity - 1)
        self._cleanup_tasks: set[asyncio.Task] = set()

    def _lock(self, session):
        changed = session.execute(
            update(ModelGateRow)
            .where(ModelGateRow.key == "models")
            .values(revision=ModelGateRow.revision + 1)
        )
        if not changed.rowcount:
            session.add(
                ModelGateRow(
                    key="models", revision=0, capacity=self.capacity, reserved=self.reserved
                )
            )
            session.flush()
        return session.get(ModelGateRow, "models")

    def configure(self):
        for attempt in range(3):
            try:
                with self.store.sessions.begin() as session:
                    gate = self._lock(session)
                    gate.capacity, gate.reserved = self.capacity, self.reserved
                return
            except IntegrityError:
                if attempt == 2:
                    raise

    @staticmethod
    def _expire(session, now):
        session.execute(
            update(ModelRequestRow)
            .where(
                ModelRequestRow.state.in_(("waiting", "running")),
                ModelRequestRow.expires_at <= now.isoformat(),
            )
            .values(state="abandoned", updated_at=now.isoformat())
        )

    def enqueue(self, purpose, goal_id=None, interactive=False, attempt=1):
        now, request_id = _now(), str(uuid4())
        for retry in range(3):
            try:
                with self.store.sessions.begin() as session:
                    self._lock(session)
                    self._expire(session, now)
                    session.execute(
                        delete(ModelRequestRow).where(
                            ModelRequestRow.state.not_in(("waiting", "running")),
                            ModelRequestRow.updated_at < (now - timedelta(days=1)).isoformat(),
                        )
                    )
                    if goal_id:
                        self._check_goal(session, goal_id)
                    session.add(
                        ModelRequestRow(
                            request_id=request_id,
                            goal_id=goal_id,
                            purpose=purpose,
                            priority="interactive" if interactive else "normal",
                            state="waiting",
                            created_at=now.isoformat(),
                            updated_at=now.isoformat(),
                            expires_at=(now + timedelta(seconds=WAIT_LEASE_SECONDS)).isoformat(),
                            attempt=attempt,
                        )
                    )
                return request_id
            except IntegrityError:
                if retry == 2:
                    raise

    @staticmethod
    def _check_goal(session, goal_id):
        # Same lock as deletion/cancellation: cancelled work cannot dispatch.
        session.execute(
            update(GoalRow).where(GoalRow.goal_id == goal_id).values(revision=GoalRow.revision)
        )
        goal = session.get(GoalRow, goal_id)
        lifecycle = session.get(GoalLifecycleRow, goal_id)
        if (
            goal is None
            or goal.cancel_requested
            or goal.state.get("status") in TERMINAL_STATUSES
            or (lifecycle and lifecycle.deleted)
        ):
            raise ModelRequestCancelled("Goal no longer accepts model work")

    def claim(self, request_id, *, now=None):
        now = now or _now()
        with self.store.sessions.begin() as session:
            gate = self._lock(session)
            self._expire(session, now)
            row = session.get(ModelRequestRow, request_id)
            if row is None or row.state != "waiting":
                raise ModelRequestCancelled("Model request is no longer queued")
            if row.goal_id:
                self._check_goal(session, row.goal_id)
            row.updated_at = now.isoformat()
            row.expires_at = (now + timedelta(seconds=WAIT_LEASE_SECONDS)).isoformat()
            running = list(
                session.scalars(select(ModelRequestRow).where(ModelRequestRow.state == "running"))
            )
            if len(running) >= gate.capacity:
                return False
            if (
                row.priority == "normal"
                and sum(r.priority == "normal" for r in running) >= gate.capacity - gate.reserved
            ):
                return False
            first = session.scalar(
                select(ModelRequestRow.request_id)
                .where(ModelRequestRow.state == "waiting")
                .order_by(
                    ModelRequestRow.priority, ModelRequestRow.created_at, ModelRequestRow.request_id
                )
                .limit(1)
            )
            if first != request_id:
                return False
            row.state = "running"
            row.started_at = row.updated_at
            row.expires_at = (now + timedelta(seconds=RUN_LEASE_SECONDS)).isoformat()
            return True

    def finish(self, request_id, state="completed", retry_after=None):
        now = _now()
        with self.store.sessions.begin() as session:
            session.execute(
                update(ModelRequestRow)
                .where(
                    ModelRequestRow.request_id == request_id,
                    ModelRequestRow.state.in_(("waiting", "running")),
                )
                .values(
                    state=state,
                    updated_at=now.isoformat(),
                    retry_at=(now + timedelta(seconds=retry_after)).isoformat()
                    if retry_after
                    else None,
                    expires_at=now.isoformat(),
                )
            )

    @asynccontextmanager
    async def slot(self, purpose, goal_id=None, interactive=False, attempt=1):
        enqueue = asyncio.create_task(
            asyncio.to_thread(self.enqueue, purpose, goal_id, interactive, attempt)
        )
        try:
            request_id = await asyncio.shield(enqueue)
        except asyncio.CancelledError:
            # Cancelling to_thread cannot stop its transaction. Keep the result
            # alive and remove the row once committed, without delaying cancellation.
            async def clean_late_enqueue():
                with suppress(Exception):
                    late_id = await enqueue
                    await asyncio.to_thread(self.finish, late_id, "cancelled")

            cleanup = asyncio.create_task(clean_late_enqueue())
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_tasks.discard)
            raise
        state, retry_after = "completed", None
        try:
            while not await asyncio.to_thread(self.claim, request_id):
                await asyncio.sleep(0.25)
            yield request_id
        except BaseException as error:
            state = (
                "cancelled"
                if isinstance(error, (asyncio.CancelledError, ModelRequestCancelled))
                else "failed"
            )
            if hasattr(error, "retry_after"):
                state = "backoff"
                retry_after = getattr(error, "retry_after", None) or min(60, 2 ** min(attempt, 6))
            raise
        finally:
            # A lost diagnostics write must never replace a completed model
            # response (or its usage) and cause a paid request to be repeated.
            with suppress(Exception):
                await asyncio.shield(asyncio.to_thread(self.finish, request_id, state, retry_after))

    def summary(self):
        now = _now().isoformat()
        with self.store.sessions() as session:
            gate = session.get(ModelGateRow, "models")
            capacity, reserved = (
                (gate.capacity, gate.reserved) if gate else (self.capacity, self.reserved)
            )
            rows = list(
                session.scalars(
                    select(ModelRequestRow).where(
                        ModelRequestRow.state.in_(("waiting", "running")),
                        ModelRequestRow.expires_at > now,
                    )
                )
            )
        return {
            "capacity": capacity,
            "reserved_interactive": reserved,
            "running": sum(r.state == "running" for r in rows),
            "waiting": sum(r.state == "waiting" for r in rows),
            "interactive_waiting": sum(
                r.state == "waiting" and r.priority == "interactive" for r in rows
            ),
        }

    def latest(self, goal_id):
        now = _now().isoformat()
        with self.store.sessions() as session:
            row = session.scalar(
                select(ModelRequestRow)
                .where(ModelRequestRow.goal_id == goal_id)
                .order_by(ModelRequestRow.created_at.desc(), ModelRequestRow.request_id.desc())
                .limit(1)
            )
            if row is None:
                return None
            return {
                key: getattr(row, key)
                for key in (
                    "request_id",
                    "purpose",
                    "priority",
                    "state",
                    "created_at",
                    "started_at",
                    "updated_at",
                    "expires_at",
                    "retry_at",
                    "attempt",
                )
            } | {"expired": row.state in {"waiting", "running"} and row.expires_at <= now}
