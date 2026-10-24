"""SQL-backed goals, state projections, and an outbox for durable submission/signals."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    exists,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from rocky.inference import ModelSettings
from rocky.models import GoalContract, GoalView, RunState

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "budget_exhausted", "deadline_exceeded")
LEGACY_BUDGET_FIELDS = {"max_steps", "max_model_calls", "max_output_tokens", "deadline_seconds"}
EVENT_STATE_FIELDS = (
    "goal_id",
    "revision",
    "status",
    "reason",
    "steps",
    "model_calls",
    "input_tokens",
    "output_tokens",
    "artifacts",
    "wake_at",
    "updated_at",
    "cycle",
    "phase",
)
EVENT_STEP_FIELDS = ("step_id", "cycle", "phase", "status", "started_at", "completed_at")


def _event_state(data: dict, *, preserve_legacy_observations: bool = False) -> dict:
    """Project the UI timeline without duplicating phase payloads or working memory."""
    projected = {key: data[key] for key in EVENT_STATE_FIELDS if key in data}
    step = data.get("execution_step")
    projected["execution_step"] = (
        {key: step[key] for key in EVENT_STEP_FIELDS if key in step} if step else None
    )
    observations = data.get("observations", [])
    # Pre-harness state has no separate phase messages. Keep its evidence on
    # disk for conversation/recall, but serve the same compact UI projection.
    if observations and not (preserve_legacy_observations and not step):
        latest_step = observations[-1]["step"]
        # A parallel batch produces several results at the same step. The UI
        # needs every member, not only the final observation in the batch.
        observations = [item for item in observations if item["step"] == latest_step]
    projected["observations"] = observations
    return projected


class MemoryConflictError(ValueError):
    """A caller attempted to replace evidence from an obsolete memory revision."""


def equivalent_contracts(first: GoalContract | dict, second: GoalContract | dict) -> bool:
    """Ignored historical budget fields do not change an idempotent request."""
    return GoalContract.model_validate(first).model_dump(exclude=LEGACY_BUDGET_FIELDS) == (
        GoalContract.model_validate(second).model_dump(exclude=LEGACY_BUDGET_FIELDS)
    )


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


class ModelDefaultsRow(Base):
    """A separate table lets existing installations upgrade without ALTER TABLE."""

    __tablename__ = "model_defaults"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    settings: Mapped[dict] = mapped_column(JSON)


class GoalModelSettingsRow(Base):
    __tablename__ = "goal_model_settings"
    goal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    settings: Mapped[dict] = mapped_column(JSON)


class GoalLifecycleRow(Base):
    """Separate metadata upgrades existing databases without changing goal columns."""

    __tablename__ = "goal_lifecycle"
    goal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class CommandRow(Base):
    __tablename__ = "commands"
    command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    goal_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    payload: Mapped[str] = mapped_column(String(4000), default="")
    sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class EventRow(Base):
    """Compact timeline projections; full state and phase records live separately."""

    __tablename__ = "events"
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    goal_id: Mapped[str] = mapped_column(String(128), index=True)
    state: Mapped[dict] = mapped_column(JSON)


class MessageRow(Base):
    """Conversation survives the workflow's bounded observation window."""

    __tablename__ = "goal_messages"
    __table_args__ = (UniqueConstraint("goal_id", "message_id"),)
    sequence_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    goal_id: Mapped[str] = mapped_column(String(128), index=True)
    message_id: Mapped[str] = mapped_column(String(240))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(64))


class ExecutionStepRow(Base):
    """One row per phase; retries update the same row instead of duplicating it."""

    __tablename__ = "execution_steps"
    __table_args__ = (UniqueConstraint("goal_id", "step_id"),)
    sequence_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    goal_id: Mapped[str] = mapped_column(String(128), index=True)
    step_id: Mapped[str] = mapped_column(String(240))
    record: Mapped[dict] = mapped_column(JSON)


class ExecutionEventRow(Base):
    """Immutable transitions; ExecutionStepRow is only their latest projection."""

    __tablename__ = "execution_events"
    __table_args__ = (UniqueConstraint("goal_id", "revision"),)
    sequence_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    goal_id: Mapped[str] = mapped_column(String(128), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    record: Mapped[dict] = mapped_column(JSON)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class KnowledgeRow(Base):
    __tablename__ = "goal_knowledge"
    __table_args__ = (UniqueConstraint("goal_id", "key"),)
    sequence_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    goal_id: Mapped[str] = mapped_column(String(128), index=True)
    key: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(64))


class MemoryRow(Base):
    """Explicitly curated reusable knowledge, never an automatic transcript copy."""

    __tablename__ = "memories"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    keywords: Mapped[str] = mapped_column(String(2000), default="")
    source: Mapped[str] = mapped_column(String(2000), default="")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[str] = mapped_column(String(64))


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
    def _view(row: GoalRow, session: Session) -> GoalView:
        snapshot = session.get(GoalModelSettingsRow, row.goal_id)
        lifecycle = session.get(GoalLifecycleRow, row.goal_id)
        return GoalView(
            goal_id=row.goal_id,
            contract=row.contract,
            state=row.state,
            created_at=row.created_at,
            model_settings=ModelSettings.model_validate(snapshot.settings) if snapshot else None,
            archived=bool(lifecycle and lifecycle.archived),
        )

    def get_model_settings(self, fallback: ModelSettings) -> ModelSettings:
        with self.sessions() as session:
            row = session.get(ModelDefaultsRow, "default")
            return (
                ModelSettings.model_validate(row.settings)
                if row
                else fallback.model_copy(deep=True)
            )

    def set_model_settings(self, value: ModelSettings) -> ModelSettings:
        data = value.model_dump(mode="json")
        with self.sessions.begin() as session:
            statement = (
                update(ModelDefaultsRow)
                .where(ModelDefaultsRow.key == "default")
                .values(settings=data)
            )
            changed = session.execute(statement)
            if not changed.rowcount:
                try:
                    # Concurrent first writes may both observe no default row.
                    # A savepoint keeps an insert conflict from aborting the
                    # enclosing PostgreSQL transaction before its retry UPDATE.
                    with session.begin_nested():
                        session.add(ModelDefaultsRow(key="default", settings=data))
                        session.flush()
                except IntegrityError:
                    session.execute(statement)
        return value.model_copy(deep=True)

    def create_goal(
        self,
        goal_id: str,
        contract: GoalContract,
        model_settings: ModelSettings | None = None,
    ) -> GoalView:
        with self.sessions.begin() as session:
            existing = session.get(GoalRow, goal_id)
            if existing:
                lifecycle = session.get(GoalLifecycleRow, goal_id)
                if lifecycle and lifecycle.deleted:
                    raise ValueError("Idempotency key belongs to a deleted goal")
                if not equivalent_contracts(existing.contract, contract):
                    raise ValueError("Idempotency key is already associated with a different goal")
                return self._view(existing, session)
            now = datetime.now(UTC).isoformat()
            state = RunState(goal_id=goal_id, updated_at=now)
            row = GoalRow(
                goal_id=goal_id,
                contract=contract.model_dump(mode="json"),
                state=state.model_dump(mode="json"),
                created_at=now,
            )
            session.add(row)
            session.add(
                MessageRow(
                    goal_id=goal_id,
                    message_id="task",
                    role="user",
                    content=self._task_message(contract),
                    priority=0,
                    created_at=now,
                )
            )
            if model_settings is not None:
                session.add(
                    GoalModelSettingsRow(
                        goal_id=goal_id, settings=model_settings.model_dump(mode="json")
                    )
                )
            session.flush()
            return self._view(row, session)

    def get_goal(self, goal_id: str) -> GoalView | None:
        with self.sessions() as session:
            row = session.get(GoalRow, goal_id)
            lifecycle = session.get(GoalLifecycleRow, goal_id)
            if lifecycle and lifecycle.deleted:
                return None
            return self._view(row, session) if row else None

    def list_goals(self, limit: int = 50, *, archived: bool = False) -> list[GoalView]:
        with self.sessions() as session:
            hidden = exists().where(
                GoalLifecycleRow.goal_id == GoalRow.goal_id,
                GoalLifecycleRow.deleted.is_(True),
            )
            in_archive = exists().where(
                GoalLifecycleRow.goal_id == GoalRow.goal_id,
                GoalLifecycleRow.archived.is_(True),
            )
            return [
                self._view(row, session)
                for row in session.scalars(
                    select(GoalRow)
                    .where(~hidden, in_archive if archived else ~in_archive)
                    .order_by(GoalRow.created_at.desc())
                    .limit(limit)
                )
            ]

    def pending_goals(self) -> list[GoalView]:
        with self.sessions() as session:
            return [
                self._view(row, session)
                for row in session.scalars(
                    select(GoalRow)
                    .where(
                        GoalRow.dispatched.is_(False),
                        GoalRow.state["status"].as_string().not_in(TERMINAL_STATUSES),
                    )
                    .limit(100)
                )
            ]

    def mark_dispatched(self, goal_id: str) -> None:
        with self.sessions.begin() as session:
            row = session.get(GoalRow, goal_id)
            if row:
                row.dispatched = True

    def reconciliation_goals(self, after: str = "", limit: int = 100) -> list[GoalView]:
        """Bounded cursor scan of projections that may outlive their engine run."""
        with self.sessions() as session:
            return [
                self._view(row, session)
                for row in session.scalars(
                    select(GoalRow)
                    .where(
                        GoalRow.goal_id > after,
                        GoalRow.dispatched.is_(True),
                        GoalRow.state["status"].as_string().not_in(TERMINAL_STATUSES),
                        ~exists().where(
                            GoalLifecycleRow.goal_id == GoalRow.goal_id,
                            GoalLifecycleRow.deleted.is_(True),
                        ),
                    )
                    .order_by(GoalRow.goal_id)
                    .limit(limit)
                )
            ]

    def reconcile_terminal(self, goal_id: str, expected_revision: int, state: RunState) -> bool:
        """Fence a closed engine outcome against newer work and deletion.

        Reconciliation never revives a task. Operator recovery must explicitly
        advance the projection before starting another execution of that task.
        """
        if state.goal_id != goal_id or state.status not in TERMINAL_STATUSES:
            raise ValueError("Reconciliation requires a matching terminal task state")
        state = state.model_copy(deep=True)
        state.revision = max(expected_revision + 1, state.revision)
        state.updated_at = datetime.now(UTC).isoformat()
        state.wake_at = None
        if state.execution_step and state.execution_step.status == "started":
            state.execution_step.status = "failed"
            state.execution_step.completed_at = state.updated_at
            state.execution_step.output = {"error": state.reason}
        data = state.model_dump(mode="json")
        with self.sessions.begin() as session:
            changed = session.execute(
                update(GoalRow)
                .where(
                    GoalRow.goal_id == goal_id,
                    GoalRow.revision == expected_revision,
                    GoalRow.dispatched.is_(True),
                    GoalRow.state["status"].as_string().not_in(TERMINAL_STATUSES),
                    ~exists().where(
                        GoalLifecycleRow.goal_id == GoalRow.goal_id,
                        GoalLifecycleRow.deleted.is_(True),
                    ),
                )
                .values(state=data, revision=state.revision)
            )
            if not changed.rowcount:
                return False
            session.add(
                EventRow(
                    goal_id=goal_id,
                    state=_event_state(data, preserve_legacy_observations=True),
                )
            )
            if state.execution_step:
                self._save_execution_step(session, goal_id, data["execution_step"], state.revision)
            return True

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
                    GoalRow.state["status"].as_string().not_in(TERMINAL_STATUSES),
                    ~exists().where(
                        GoalLifecycleRow.goal_id == GoalRow.goal_id,
                        GoalLifecycleRow.deleted.is_(True),
                    ),
                )
                .values(state=data, revision=state.revision)
            )
            if changed.rowcount:
                session.add(
                    EventRow(
                        goal_id=state.goal_id,
                        state=_event_state(data, preserve_legacy_observations=True),
                    )
                )
                if record := data.get("execution_step"):
                    self._save_execution_step(session, state.goal_id, record, state.revision)
            elif session.get(GoalRow, state.goal_id) is None:
                raise ValueError("Goal does not exist")

    @staticmethod
    def _task_message(contract: GoalContract) -> str:
        return json.dumps(
            {
                "objective": contract.objective,
                "acceptance_criteria": contract.acceptance_criteria,
                "deliverables": [item.model_dump(mode="json") for item in contract.deliverables],
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _save_execution_step(session: Session, goal_id: str, record: dict, revision: int) -> None:
        row = session.scalar(
            select(ExecutionStepRow).where(
                ExecutionStepRow.goal_id == goal_id,
                ExecutionStepRow.step_id == record["step_id"],
            )
        )
        if row is None or row.record != record:
            duration_ms = None
            if record.get("completed_at"):
                try:
                    elapsed = datetime.fromisoformat(
                        record["completed_at"]
                    ) - datetime.fromisoformat(record["started_at"])
                    duration_ms = max(0, int(elapsed.total_seconds() * 1000))
                except (ValueError, TypeError):
                    pass
            session.add(
                ExecutionEventRow(
                    goal_id=goal_id, revision=revision, record=record, duration_ms=duration_ms
                )
            )
        if row is None:
            session.add(ExecutionStepRow(goal_id=goal_id, step_id=record["step_id"], record=record))
        else:
            row.record = record
        if record["status"] not in {"completed", "failed"}:
            return
        output = record.get("output", {})
        if record["phase"] == "act" and isinstance(output.get("results"), list):
            # A parallel batch keeps one tool message per action, so recall can
            # rank and clip each result and still say which action produced it.
            for index, item in enumerate(output["results"], 1):
                action = item.get("action") or {}
                Store._add_message(
                    session,
                    goal_id,
                    f"phase:{record['step_id']}:{index}",
                    "tool",
                    {
                        **item.get("result", {}),
                        "action": {key: action.get(key) for key in ("tool", "path", "url")},
                    },
                    record.get("completed_at") or record["started_at"],
                )
            return
        role, content = None, None
        if record["phase"] == "think" and output.get("decision") is not None:
            role, content = "assistant", output["decision"]
        elif record["phase"] == "act" and output.get("result") is not None:
            role, content = "tool", output["result"]
        elif record["phase"] == "complete" and output.get("verification") is not None:
            role, content = "assistant", output["verification"]
        elif record["status"] == "failed" and output:
            role, content = "system", output
        if role is None:
            return
        Store._add_message(
            session,
            goal_id,
            f"phase:{record['step_id']}",
            role,
            content,
            record.get("completed_at") or record["started_at"],
        )

    @staticmethod
    def _add_message(
        session: Session, goal_id: str, message_id: str, role: str, content, created_at: str
    ) -> None:
        previous = session.scalar(
            select(MessageRow).where(
                MessageRow.goal_id == goal_id, MessageRow.message_id == message_id
            )
        )
        if previous is None:
            session.add(
                MessageRow(
                    goal_id=goal_id,
                    message_id=message_id,
                    role=role,
                    content=content
                    if isinstance(content, str)
                    else json.dumps(content, ensure_ascii=False),
                    priority=0,
                    created_at=created_at,
                )
            )

    @staticmethod
    def _lock_completed(session: Session, goal_id: str) -> GoalRow:
        # A no-op UPDATE locks on both SQLite and PostgreSQL. Validation and
        # lifecycle changes then share the same lock as persistence Activities.
        changed = session.execute(
            update(GoalRow)
            .where(
                GoalRow.goal_id == goal_id,
                GoalRow.state["status"].as_string().in_(TERMINAL_STATUSES),
            )
            .values(revision=GoalRow.revision)
        )
        if not changed.rowcount:
            lifecycle = session.get(GoalLifecycleRow, goal_id)
            if session.get(GoalRow, goal_id) is None or (lifecycle and lifecycle.deleted):
                raise KeyError(goal_id)
            raise ValueError("Only completed goals can be archived or deleted")
        return session.get(GoalRow, goal_id)

    def set_archived(self, goal_id: str, archived: bool) -> GoalView:
        with self.sessions.begin() as session:
            row = self._lock_completed(session, goal_id)
            lifecycle = session.get(GoalLifecycleRow, goal_id)
            if lifecycle is None:
                lifecycle = GoalLifecycleRow(goal_id=goal_id, archived=archived, deleted=False)
                session.add(lifecycle)
            else:
                lifecycle.archived = archived
            session.flush()
            return self._view(row, session)

    def delete_goal(self, goal_id: str) -> None:
        with self.sessions.begin() as session:
            row = self._lock_completed(session, goal_id)
            lifecycle = session.get(GoalLifecycleRow, goal_id)
            if lifecycle is None:
                session.add(GoalLifecycleRow(goal_id=goal_id, archived=False, deleted=True))
            else:
                lifecycle.archived = False
                lifecycle.deleted = True
            # Keep only the unique goal identity as a tombstone. An HTTP retry
            # with its old idempotency key must not resurrect a deleted task.
            row.contract = {}
            row.state = {}
            row.created_at = ""
            row.dispatched = True
            row.cancel_requested = True
            for table in (
                EventRow,
                CommandRow,
                GoalModelSettingsRow,
                MessageRow,
                ExecutionStepRow,
                ExecutionEventRow,
                KnowledgeRow,
            ):
                session.execute(delete(table).where(table.goal_id == goal_id))

    def enqueue_command(self, goal_id: str, kind: str, payload: str = "") -> str:
        if kind not in ("cancel", "wake", "approve"):
            raise ValueError("Unsupported command")
        with self.sessions.begin() as session:
            # SELECT FOR UPDATE is ignored by SQLite. Acquire the same write
            # lock used by persistence/deletion before validating the snapshot.
            session.execute(
                update(GoalRow).where(GoalRow.goal_id == goal_id).values(revision=GoalRow.revision)
            )
            row = session.get(GoalRow, goal_id)
            lifecycle = session.get(GoalLifecycleRow, goal_id)
            if row is None or (lifecycle and lifecycle.deleted):
                raise KeyError(goal_id)
            if row.state["status"] in TERMINAL_STATUSES:
                raise ValueError("Goal has already ended")
            if kind == "approve":
                pending = row.state.get("pending_approval") or {}
                scope, _, fingerprint = payload.rpartition(":")
                if (
                    not pending
                    or fingerprint != pending.get("fingerprint")
                    or scope not in {"", "task"}
                ):
                    raise ValueError("Approval must match the task's current pending action")
                if scope == "task" and (
                    not pending.get("command_prefix")
                    or not pending.get("action", {}).get("sandbox", True)
                ):
                    raise ValueError(
                        "This action can only be approved once; it has no reusable command prefix"
                    )
            if kind == "cancel":
                row.cancel_requested = True
            command_id = str(uuid4())
            session.add(
                CommandRow(command_id=command_id, goal_id=goal_id, kind=kind, payload=payload)
            )
            if kind == "wake":
                session.add(
                    MessageRow(
                        goal_id=goal_id,
                        message_id=f"wake:{command_id}",
                        role="user",
                        content=payload,
                        priority=10,
                        created_at=datetime.now(UTC).isoformat(),
                    )
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

    def goal_messages(self, goal_id: str) -> list[dict]:
        """Saved user feedback, in stable ID order (not a claimed chronology)."""
        with self.sessions() as session:
            return [
                {"command_id": row.command_id, "message": row.payload, "sent": row.sent}
                for row in session.scalars(
                    select(CommandRow)
                    .where(CommandRow.goal_id == goal_id, CommandRow.kind == "wake")
                    .order_by(CommandRow.command_id)
                )
            ]

    def conversation(self, goal_id: str) -> list[dict]:
        """All task messages, with a fallback for pre-harness task histories."""
        with self.sessions() as session:
            goal = session.get(GoalRow, goal_id)
            lifecycle = session.get(GoalLifecycleRow, goal_id)
            if goal is None or (lifecycle and lifecycle.deleted):
                return []
            records = [
                {
                    "id": row.message_id,
                    "kind": "conversation",
                    "role": row.role,
                    "content": row.content,
                    "priority": row.priority,
                    "created_at": row.created_at,
                }
                for row in session.scalars(
                    select(MessageRow)
                    .where(MessageRow.goal_id == goal_id)
                    .order_by(MessageRow.sequence_id)
                )
            ]
            message_ids = {item["id"] for item in records}
            if "task" not in message_ids:
                records.insert(
                    0,
                    {
                        "id": "task",
                        "kind": "conversation",
                        "role": "user",
                        "content": self._task_message(GoalContract.model_validate(goal.contract)),
                        "priority": 0,
                        "created_at": goal.created_at,
                    },
                )
            for command in session.scalars(
                select(CommandRow)
                .where(CommandRow.goal_id == goal_id, CommandRow.kind == "wake")
                .order_by(CommandRow.command_id)
            ):
                message_id = f"wake:{command.command_id}"
                if message_id not in message_ids:
                    records.append(
                        {
                            "id": message_id,
                            "kind": "conversation",
                            "role": "user",
                            "content": command.payload,
                            "priority": 10,
                            # Old commands have no timestamp; do not invent chronology.
                            "created_at": "",
                        }
                    )
            # Pre-harness executions stored observations only in state events.
            # Recall their full history, including evidence that has since fallen
            # out of the workflow's 20-observation window. Harness phase messages
            # already retain this data and must not be duplicated here.
            legacy_snapshots = [
                event.state
                for event in session.scalars(
                    select(EventRow)
                    .where(
                        EventRow.goal_id == goal_id,
                        EventRow.state["execution_step"].as_string().is_(None),
                    )
                    .order_by(EventRow.event_id)
                )
                if not event.state.get("execution_step")
            ]
            if not goal.state.get("execution_step"):
                legacy_snapshots.append(goal.state)
            for snapshot in legacy_snapshots:
                for observation in snapshot.get("observations", []):
                    message_id = f"legacy-observation:{observation['step']}:{observation['tool']}"
                    if message_id in message_ids:
                        continue
                    message_ids.add(message_id)
                    records.append(
                        {
                            "id": message_id,
                            "kind": "conversation",
                            "role": "tool",
                            "content": json.dumps(observation, ensure_ascii=False),
                            "priority": 0,
                            "created_at": snapshot.get("updated_at", ""),
                        }
                    )
            return records

    def execution_steps(self, goal_id: str, after: int = 0, limit: int = 100) -> list[dict]:
        with self.sessions() as session:
            return [
                {"sequence_id": row.sequence_id, **row.record}
                for row in session.scalars(
                    select(ExecutionStepRow)
                    .where(
                        ExecutionStepRow.goal_id == goal_id,
                        ExecutionStepRow.sequence_id > after,
                    )
                    .order_by(ExecutionStepRow.sequence_id)
                    .limit(limit)
                )
            ]

    def all_execution_steps(self, goal_id: str) -> list[dict]:
        steps: list[dict] = []
        while batch := self.execution_steps(
            goal_id, after=steps[-1]["sequence_id"] if steps else 0
        ):
            steps.extend(batch)
        return steps

    def execution_events(self, goal_id: str, after: int = 0, limit: int = 100) -> list[dict]:
        """Read immutable step transitions in persisted order for audit/replay."""
        with self.sessions() as session:
            return [
                {
                    "sequence_id": row.sequence_id,
                    "revision": row.revision,
                    "duration_ms": row.duration_ms,
                    **row.record,
                }
                for row in session.scalars(
                    select(ExecutionEventRow)
                    .where(
                        ExecutionEventRow.goal_id == goal_id,
                        ExecutionEventRow.sequence_id > after,
                    )
                    .order_by(ExecutionEventRow.sequence_id)
                    .limit(max(1, min(limit, 1000)))
                )
            ]

    def knowledge(self, goal_id: str) -> list[dict]:
        with self.sessions() as session:
            return [
                {
                    "id": row.key,
                    "kind": "knowledge",
                    "role": "knowledge",
                    "content": row.content,
                    "priority": row.priority,
                    "created_at": row.created_at,
                }
                for row in session.scalars(
                    select(KnowledgeRow)
                    .where(KnowledgeRow.goal_id == goal_id)
                    .order_by(KnowledgeRow.sequence_id)
                )
            ]

    def put_knowledge(
        self, goal_id: str, content: str, key: str | None = None, priority: int = 0
    ) -> dict:
        key = key if key is not None else str(uuid4())
        if not content.strip() or len(content) > 16000:
            raise ValueError("Knowledge content must contain 1 to 16000 characters")
        if not key or len(key) > 128 or not -100 <= priority <= 100:
            raise ValueError("Invalid knowledge key or priority")
        with self.sessions.begin() as session:
            session.execute(
                update(GoalRow).where(GoalRow.goal_id == goal_id).values(revision=GoalRow.revision)
            )
            goal = session.get(GoalRow, goal_id)
            lifecycle = session.get(GoalLifecycleRow, goal_id)
            if goal is None or (lifecycle and lifecycle.deleted):
                raise KeyError(goal_id)
            row = session.scalar(
                select(KnowledgeRow).where(KnowledgeRow.goal_id == goal_id, KnowledgeRow.key == key)
            )
            now = datetime.now(UTC).isoformat()
            if row is None:
                session.add(
                    KnowledgeRow(
                        goal_id=goal_id, key=key, content=content, priority=priority, created_at=now
                    )
                )
            else:
                row.content, row.priority, row.created_at = content, priority, now
            return {
                "id": key,
                "kind": "knowledge",
                "role": "knowledge",
                "content": content,
                "priority": priority,
                "created_at": now,
            }

    def memory_records(self, goal_id: str) -> list[dict]:
        return [*self.conversation(goal_id), *self.knowledge(goal_id)]

    def recall_memory(self, goal_id: str, query: str = "", limit: int = 24) -> list[dict]:
        """Bound recall work at the database boundary, not after loading a transcript.

        Full history remains available through conversation/events. Older task
        histories receive a bounded event fallback until they produce tool messages.
        """
        limit = max(1, min(limit, 100))
        with self.sessions() as session:
            goal = session.get(GoalRow, goal_id)
            lifecycle = session.get(GoalLifecycleRow, goal_id)
            if goal is None or (lifecycle and lifecycle.deleted):
                return []
            messages = list(
                session.scalars(
                    select(MessageRow)
                    .where(MessageRow.goal_id == goal_id, MessageRow.message_id != "task")
                    .order_by(MessageRow.sequence_id.desc())
                    .limit(limit)
                )
            )
            records = [
                {
                    "id": row.message_id,
                    "kind": "conversation",
                    "role": row.role,
                    "content": row.content,
                    "priority": row.priority,
                    "created_at": row.created_at,
                }
                for row in reversed(messages)
            ]
            ids = {item["id"] for item in records}
            # A clarification may precede many tool calls. Keep the latest real
            # owner messages resident even after the raw tool window rolls over.
            owner_messages = list(
                session.scalars(
                    select(MessageRow)
                    .where(
                        MessageRow.goal_id == goal_id,
                        MessageRow.role == "user",
                        MessageRow.message_id != "task",
                    )
                    .order_by(MessageRow.sequence_id.desc())
                    .limit(3)
                )
            )
            for row in owner_messages:
                if row.message_id not in ids:
                    ids.add(row.message_id)
                    records.append(
                        {
                            "id": row.message_id,
                            "kind": "conversation",
                            "role": "user",
                            "content": row.content,
                            "priority": row.priority,
                            "created_at": row.created_at,
                        }
                    )
            if not owner_messages:
                # Legacy commands have no timestamps. Their ID gives stable
                # fallback ordering, without claiming to know their chronology.
                for command in session.scalars(
                    select(CommandRow)
                    .where(CommandRow.goal_id == goal_id, CommandRow.kind == "wake")
                    .order_by(CommandRow.command_id.desc())
                    .limit(3)
                ):
                    records.append(
                        {
                            "id": f"wake:{command.command_id}",
                            "kind": "conversation",
                            "role": "user",
                            "content": command.payload,
                            "priority": 10,
                            "created_at": "",
                        }
                    )
            if not any(row.role == "tool" for row in messages):
                snapshots = list(
                    session.scalars(
                        select(EventRow)
                        .where(
                            EventRow.goal_id == goal_id,
                            EventRow.state["execution_step"].as_string().is_(None),
                        )
                        .order_by(EventRow.event_id.desc())
                        .limit(limit)
                    )
                )
                for state in [goal.state, *(row.state for row in snapshots)]:
                    for observation in reversed(state.get("observations", [])[-limit:]):
                        identity = f"legacy-observation:{observation['step']}:{observation['tool']}"
                        if identity in ids:
                            continue
                        ids.add(identity)
                        records.append(
                            {
                                "id": identity,
                                "kind": "conversation",
                                "role": "tool",
                                "content": json.dumps(observation, ensure_ascii=False),
                                "priority": 0,
                                "created_at": state.get("updated_at", ""),
                            }
                        )
                        if sum(item["role"] == "tool" for item in records) >= limit:
                            break
                    if sum(item["role"] == "tool" for item in records) >= limit:
                        break
            for row in session.scalars(
                select(KnowledgeRow)
                .where(KnowledgeRow.goal_id == goal_id)
                .order_by(KnowledgeRow.priority.desc(), KnowledgeRow.created_at.desc())
                .limit(min(limit, 12))
            ):
                records.append(
                    {
                        "id": row.key,
                        "kind": "knowledge",
                        "role": "knowledge",
                        "content": row.content,
                        "priority": row.priority,
                        "created_at": row.created_at,
                    }
                )
        if query:
            records.extend(
                {
                    "id": f"memory:{row['key']}",
                    "kind": "knowledge",
                    "role": "knowledge",
                    "content": row["content"],
                    "priority": row["priority"],
                    "created_at": row["updated_at"],
                }
                for row in self.memories(query=query, limit=6)
            )
        return records

    @staticmethod
    def _memory_view(row: MemoryRow) -> dict:
        return {
            "key": row.key,
            "kind": row.kind,
            "content": row.content,
            "keywords": row.keywords,
            "source": row.source,
            "priority": row.priority,
            "revision": row.revision,
            "active": row.active,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _memory_terms(query: str) -> list[str]:
        terms = re.findall(r"[a-z0-9_-]{3,}|[\u3400-\u9fff]{2,}", query.lower()[:8000])
        # Adjacent Chinese words need no spaces; bigrams also find explicit tags
        # inside a longer objective without introducing a tokenizer dependency.
        terms = [
            part
            for term in terms
            for part in (
                [term] if term.isascii() else [term[i : i + 2] for i in range(len(term) - 1)]
            )
        ]
        return list(dict.fromkeys(terms))[:24]

    def memories(
        self, query: str = "", limit: int = 20, *, include_inactive: bool = False
    ) -> list[dict]:
        """List for owner inspection; a nonempty query retrieves only matching facts."""
        statement = select(MemoryRow)
        if not include_inactive:
            statement = statement.where(MemoryRow.active.is_(True))
        if query:
            terms = self._memory_terms(query)
            if not terms:
                return []
            statement = statement.where(
                or_(
                    *[
                        or_(
                            func.lower(MemoryRow.key).contains(term, autoescape=True),
                            func.lower(MemoryRow.keywords).contains(term, autoescape=True),
                            func.lower(MemoryRow.content).contains(term, autoescape=True),
                        )
                        for term in terms
                    ]
                )
            )
        with self.sessions() as session:
            return [
                self._memory_view(row)
                for row in session.scalars(
                    statement.order_by(
                        MemoryRow.priority.desc(), MemoryRow.updated_at.desc()
                    ).limit(max(1, min(limit, 100)))
                )
            ]

    def put_memory(
        self,
        key: str,
        kind: str,
        content: str,
        *,
        keywords: str = "",
        source: str = "",
        priority: int = 0,
        expected_revision: int | None = None,
    ) -> dict:
        """Explicit stable-key curation; corrections require the current revision.

        Matching retries are harmless, including a retry whose previous commit
        succeeded but response was lost. Stale evidence cannot overwrite a newer
        correction. Callers never promote full transcripts or summaries implicitly.
        """
        if not key.strip() or len(key) > 128:
            raise ValueError("Memory key must contain 1 to 128 characters")
        if kind not in {"preference", "decision", "correction", "pitfall"}:
            raise ValueError("Memory must be a preference, decision, correction or pitfall")
        if not content.strip() or len(content) > 8000:
            raise ValueError("Memory content must contain 1 to 8000 characters")
        if len(keywords) > 2000 or len(source) > 2000 or not -100 <= priority <= 100:
            raise ValueError("Memory metadata exceeds its bounds")
        desired = dict(
            kind=kind, content=content, keywords=keywords, source=source, priority=priority
        )
        with self.sessions.begin() as session:
            session.execute(
                update(MemoryRow).where(MemoryRow.key == key).values(revision=MemoryRow.revision)
            )
            row = session.get(MemoryRow, key)
            if row is not None:
                if row.active and all(
                    getattr(row, name) == value for name, value in desired.items()
                ):
                    return self._memory_view(row)
                if expected_revision != row.revision:
                    raise MemoryConflictError(
                        "Memory changed; read its current revision before correcting it"
                    )
                for name, value in desired.items():
                    setattr(row, name, value)
                row.revision += 1
                row.active = True
                row.updated_at = datetime.now(UTC).isoformat()
            else:
                if expected_revision not in (None, 0):
                    raise MemoryConflictError("Memory does not exist at the expected revision")
                row = MemoryRow(
                    key=key,
                    **desired,
                    revision=1,
                    active=True,
                    updated_at=datetime.now(UTC).isoformat(),
                )
                try:
                    # Two first writes of the same stable key may race on
                    # PostgreSQL. A savepoint lets a matching retry return the
                    # original memory without aborting the outer transaction.
                    with session.begin_nested():
                        session.add(row)
                        session.flush()
                except IntegrityError:
                    row = session.get(MemoryRow, key)
                    if (
                        row is None
                        or not row.active
                        or any(getattr(row, name) != value for name, value in desired.items())
                    ):
                        raise MemoryConflictError(
                            "Memory changed; read its current revision before correcting it"
                        ) from None
            session.flush()
            return self._memory_view(row)

    def archive_memory(self, key: str, expected_revision: int) -> dict:
        """Retract obsolete evidence so future recall cannot present it as current."""
        with self.sessions.begin() as session:
            session.execute(
                update(MemoryRow).where(MemoryRow.key == key).values(revision=MemoryRow.revision)
            )
            row = session.get(MemoryRow, key)
            if row is None:
                raise KeyError(key)
            if not row.active:
                return self._memory_view(row)
            if row.revision != expected_revision:
                raise MemoryConflictError(
                    "Memory changed; read its current revision before retracting it"
                )
            row.active = False
            row.revision += 1
            row.updated_at = datetime.now(UTC).isoformat()
            return self._memory_view(row)

    def mark_command_sent(self, command_id: str) -> None:
        with self.sessions.begin() as session:
            row = session.get(CommandRow, command_id)
            if row:
                row.sent = True

    def events(self, goal_id: str, after: int = 0, limit: int = 100) -> list[dict]:
        with self.sessions() as session:
            return [
                # Also compact historical full snapshots without rewriting
                # evidence that legacy conversation/recall may still need.
                dict(event_id=row.event_id, state=_event_state(row.state))
                for row in session.scalars(
                    select(EventRow)
                    .where(EventRow.goal_id == goal_id, EventRow.event_id > after)
                    .order_by(EventRow.event_id)
                    .limit(limit)
                )
            ]
