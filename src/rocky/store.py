"""SQL-backed goals, state projections, and an outbox for durable submission/signals."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import JSON, Boolean, Integer, String, create_engine, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from rocky.models import GoalContract, GoalView, RunState


class Base(DeclarativeBase):
    pass


class GoalRow(Base):
    __tablename__ = "goals"
    goal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    contract: Mapped[dict] = mapped_column(JSON)
    state: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(64))
    dispatched: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class CommandRow(Base):
    __tablename__ = "commands"
    command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    goal_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    payload: Mapped[str] = mapped_column(String(4000), default="")
    sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class EventRow(Base):
    __tablename__ = "events"
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    goal_id: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[dict] = mapped_column(JSON)


class Store:
    def __init__(self, database_url: str):
        url = make_url(database_url)
        kwargs = {}
        if url.get_backend_name() == "sqlite":
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if url.database in (None, "", ":memory:"):
                kwargs["poolclass"] = StaticPool
            else:
                Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(database_url, **kwargs)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _view(row: GoalRow) -> GoalView:
        return GoalView(
            goal_id=row.goal_id, contract=row.contract, state=row.state, created_at=row.created_at
        )

    def create_goal(self, goal_id: str, contract: GoalContract) -> GoalView:
        with self.sessions.begin() as session:
            existing = session.get(GoalRow, goal_id)
            if existing:
                if existing.contract != contract.model_dump(mode="json"):
                    raise ValueError("Idempotency key is already associated with a different goal")
                return self._view(existing)
            now = datetime.now(UTC).isoformat()
            state = RunState(goal_id=goal_id, updated_at=now)
            row = GoalRow(
                goal_id=goal_id,
                contract=contract.model_dump(mode="json"),
                state=state.model_dump(mode="json"),
                created_at=now,
            )
            session.add(row)
            session.flush()
            return self._view(row)

    def get_goal(self, goal_id: str) -> GoalView | None:
        with self.sessions() as session:
            row = session.get(GoalRow, goal_id)
            return self._view(row) if row else None

    def list_goals(self, limit: int = 50) -> list[GoalView]:
        with self.sessions() as session:
            return [
                self._view(row)
                for row in session.scalars(
                    select(GoalRow).order_by(GoalRow.created_at.desc()).limit(limit)
                )
            ]

    def pending_goals(self) -> list[GoalView]:
        with self.sessions() as session:
            return [
                self._view(row)
                for row in session.scalars(
                    select(GoalRow).where(GoalRow.dispatched.is_(False)).limit(100)
                )
            ]

    def mark_dispatched(self, goal_id: str) -> None:
        with self.sessions.begin() as session:
            row = session.get(GoalRow, goal_id)
            if row:
                row.dispatched = True

    def save_state(self, state: RunState) -> None:
        with self.sessions.begin() as session:
            data = state.model_dump(mode="json")
            # Atomic compare-and-set works on PostgreSQL AND SQLite. A delayed
            # Activity attempt cannot overwrite a newer projection or event.
            changed = session.execute(
                update(GoalRow)
                .where(
                    GoalRow.goal_id == state.goal_id,
                    GoalRow.revision < state.revision,
                )
                .values(state=data, revision=state.revision)
            )
            if changed.rowcount:
                session.add(EventRow(goal_id=state.goal_id, state=data))
            elif session.get(GoalRow, state.goal_id) is None:
                raise ValueError("Goal does not exist")

    def enqueue_command(self, goal_id: str, kind: str, payload: str = "") -> str:
        if kind not in ("cancel", "wake"):
            raise ValueError("Unsupported command")
        with self.sessions.begin() as session:
            row = session.get(GoalRow, goal_id, with_for_update=True)
            if row is None:
                raise KeyError(goal_id)
            if row.state["status"] in (
                "succeeded",
                "failed",
                "cancelled",
                "budget_exhausted",
                "deadline_exceeded",
            ):
                raise ValueError("Goal has already ended")
            if kind == "cancel":
                row.cancel_requested = True
            command_id = str(uuid4())
            session.add(
                CommandRow(command_id=command_id, goal_id=goal_id, kind=kind, payload=payload)
            )
            return command_id

    def is_cancel_requested(self, goal_id: str) -> bool:
        with self.sessions() as session:
            row = session.get(GoalRow, goal_id)
            return row is None or row.cancel_requested

    def pending_commands(self) -> list[dict]:
        with self.sessions() as session:
            return [
                dict(
                    command_id=row.command_id,
                    goal_id=row.goal_id,
                    kind=row.kind,
                    payload=row.payload,
                )
                for row in session.scalars(
                    select(CommandRow).where(CommandRow.sent.is_(False)).limit(100)
                )
            ]

    def mark_command_sent(self, command_id: str) -> None:
        with self.sessions.begin() as session:
            row = session.get(CommandRow, command_id)
            if row:
                row.sent = True

    def events(self, goal_id: str, after: int = 0, limit: int = 100) -> list[dict]:
        with self.sessions() as session:
            return [
                dict(event_id=row.event_id, state=json.loads(json.dumps(row.state)))
                for row in session.scalars(
                    select(EventRow)
                    .where(EventRow.goal_id == goal_id, EventRow.event_id > after)
                    .order_by(EventRow.event_id)
                    .limit(limit)
                )
            ]
