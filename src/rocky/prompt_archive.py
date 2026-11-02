"""Deduplicated instruction snapshots, separate from task inputs and credentials."""

from datetime import UTC, datetime

from sqlalchemy import JSON, String, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from rocky.prompts import PromptSnapshot
from rocky.store import Base, GoalLifecycleRow, GoalRow, Store


class PromptSnapshotRow(Base):
    __tablename__ = "prompt_snapshots"
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON)


class PromptUseRow(Base):
    __tablename__ = "prompt_uses"
    goal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    first_used_at: Mapped[str] = mapped_column(String(64))
    last_used_at: Mapped[str] = mapped_column(String(64))


class PromptArchive:
    def __init__(self, store: Store):
        self.store = store

    def record(self, snapshot: PromptSnapshot, goal_id: str | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        # Content IDs and composite use IDs make concurrent workers idempotent.
        for attempt in range(3):
            try:
                with self.store.sessions.begin() as session:
                    if goal_id is not None:
                        # Share deletion's write lock: deleted goals cannot regain metadata.
                        session.execute(
                            update(GoalRow)
                            .where(GoalRow.goal_id == goal_id)
                            .values(revision=GoalRow.revision)
                        )
                        goal = session.get(GoalRow, goal_id)
                        lifecycle = session.get(GoalLifecycleRow, goal_id)
                        if goal is None or (lifecycle is not None and lifecycle.deleted):
                            raise KeyError(goal_id)
                    existing = session.get(PromptSnapshotRow, snapshot.snapshot_id)
                    if existing is not None and existing.snapshot != snapshot.model_dump(
                        mode="json"
                    ):
                        raise ValueError("Instruction snapshot identity conflict")
                    if existing is None:
                        session.add(
                            PromptSnapshotRow(
                                snapshot_id=snapshot.snapshot_id,
                                snapshot=snapshot.model_dump(mode="json"),
                            )
                        )
                    if goal_id is not None:
                        key = (goal_id, snapshot.snapshot_id)
                        used = session.get(PromptUseRow, key)
                        if used is None:
                            session.add(
                                PromptUseRow(
                                    goal_id=goal_id,
                                    snapshot_id=snapshot.snapshot_id,
                                    first_used_at=now,
                                    last_used_at=now,
                                )
                            )
                        else:
                            used.last_used_at = max(used.last_used_at, now)
                return
            except IntegrityError:
                if attempt == 2:
                    raise

    def get(self, snapshot_id: str) -> PromptSnapshot:
        with self.store.sessions() as session:
            row = session.get(PromptSnapshotRow, snapshot_id)
            if row is None:
                raise KeyError(snapshot_id)
            return PromptSnapshot.model_validate(row.snapshot)

    def for_goal(self, goal_id: str) -> list[dict]:
        with self.store.sessions() as session:
            rows = session.execute(
                select(PromptUseRow, PromptSnapshotRow)
                .join(PromptSnapshotRow, PromptUseRow.snapshot_id == PromptSnapshotRow.snapshot_id)
                .where(PromptUseRow.goal_id == goal_id)
                .order_by(PromptUseRow.first_used_at, PromptUseRow.snapshot_id)
            ).all()
            return [
                {
                    "snapshot_id": used.snapshot_id,
                    "purpose": row.snapshot["purpose"],
                    "modules": [
                        {k: v for k, v in item.items() if k != "text"}
                        for item in row.snapshot["modules"]
                    ],
                    "first_used_at": used.first_used_at,
                    "last_used_at": used.last_used_at,
                }
                for used, row in rows
            ]
