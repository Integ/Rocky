"""Version-bound owner review, independent of execution and tool approvals."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field
from sqlalchemy import JSON, Integer, String, Text, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

from rocky.artifact_archive import archive_identity
from rocky.models import Artifact, GoalContract, Model, RunState
from rocky.store import Base, ExecutionStepRow, GoalLifecycleRow, GoalRow, Store


class Delivery(Model):
    goal_id: str
    revision: int
    completed_at: str
    summary: str
    contract: GoalContract
    artifacts: list[Artifact]
    archive_id: str | None = None
    verification_feedback: str = ""
    review_status: Literal["pending", "accepted", "changes_requested"] = "pending"
    feedback: str = ""
    reviewed_at: str | None = None
    continued_revision: int | None = None


class DeliveryPage(Model):
    items: list[Delivery]
    total: int
    next_before_revision: int | None = None


class DeliveryReviewRequest(Model):
    action: Literal["accept", "request_changes"]
    feedback: str = Field(default="", max_length=2000)


class DeliveryRow(Base):
    __tablename__ = "deliveries"
    goal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    review_status: Mapped[str] = mapped_column(String(24), default="pending")
    feedback: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    continued_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DeliveryStore:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _view(row):
        return Delivery(
            **row.data,
            review_status=row.review_status,
            feedback=row.feedback,
            reviewed_at=row.reviewed_at,
            continued_revision=row.continued_revision,
        )

    @staticmethod
    def record(session, goal, state: RunState, archive_id=None):
        if state.status != "succeeded":
            return None
        identity = archive_id or state.delivery_archive
        if identity and identity != archive_identity(state.artifacts):
            raise ValueError("Delivery archive does not match the verified artifact set")
        row = session.get(DeliveryRow, (goal.goal_id, state.revision))
        if row is None:
            # The full review remains in its original immutable execution step.
            records = session.scalars(
                select(ExecutionStepRow.record)
                .where(
                    ExecutionStepRow.goal_id == goal.goal_id,
                    ExecutionStepRow.record["phase"].as_string() == "complete",
                )
                .order_by(ExecutionStepRow.sequence_id.desc())
                .limit(1)
            ).all()
            verdict = (records[0].get("output", {}).get("verification") or {}) if records else {}
            payload = Delivery(
                goal_id=goal.goal_id,
                revision=state.revision,
                completed_at=state.updated_at,
                summary=state.reason,
                contract=GoalContract.model_validate(goal.contract),
                artifacts=state.artifacts,
                archive_id=identity,
                verification_feedback=verdict.get("feedback", "")[:4000],
            )
            row = DeliveryRow(
                goal_id=goal.goal_id,
                revision=state.revision,
                data=payload.model_dump(
                    mode="json",
                    exclude={"review_status", "feedback", "reviewed_at", "continued_revision"},
                ),
                review_status="pending",
                feedback="",
            )
            session.add(row)
            session.flush()
        elif row.data.get("archive_id") is None and identity:
            row.data = {**row.data, "archive_id": identity}
        return row

    @staticmethod
    def changes(row, feedback):
        if row.review_status == "pending":
            row.review_status = "changes_requested"
            row.feedback = feedback
            row.reviewed_at = datetime.now(UTC).isoformat()

    def page(self, goal_id, before_revision=None, limit=20):
        goal = self.store.get_goal(goal_id)
        if goal is None:
            raise KeyError(goal_id)
        with self.store.sessions() as session:
            query = select(DeliveryRow).where(DeliveryRow.goal_id == goal_id)
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            # Older completed goals have no fabricated historical snapshots.
            virtual = None
            if (
                goal.state.status == "succeeded"
                and session.get(DeliveryRow, (goal_id, goal.state.revision)) is None
            ):
                virtual = Delivery(
                    goal_id=goal_id,
                    revision=goal.state.revision,
                    completed_at=goal.state.updated_at,
                    summary=goal.state.reason,
                    contract=goal.contract,
                    artifacts=goal.state.artifacts,
                )
                total += 1
            if before_revision is not None:
                query = query.where(DeliveryRow.revision < before_revision)
            rows = list(
                session.scalars(query.order_by(DeliveryRow.revision.desc()).limit(limit + 1))
            )
            values = [self._view(row) for row in rows]
            if virtual and (before_revision is None or virtual.revision < before_revision):
                values.insert(0, virtual)
            return DeliveryPage(
                items=values[:limit],
                total=total,
                next_before_revision=values[limit - 1].revision if len(values) > limit else None,
            )

    def get(self, goal_id, revision):
        goal = self.store.get_goal(goal_id)
        if goal is None:
            raise KeyError(goal_id)
        with self.store.sessions() as session:
            row = session.get(DeliveryRow, (goal_id, revision))
            if row:
                return self._view(row)
            if goal.state.status == "succeeded" and goal.state.revision == revision:
                return Delivery(
                    goal_id=goal_id,
                    revision=revision,
                    completed_at=goal.state.updated_at,
                    summary=goal.state.reason,
                    contract=goal.contract,
                    artifacts=goal.state.artifacts,
                )
            raise KeyError(revision)

    def review(self, goal_id, revision, action, feedback="", archive_id=None):
        if action not in {"accept", "request_changes"}:
            raise ValueError("Unknown review action")
        feedback = feedback.strip()
        if action == "request_changes" and not feedback:
            raise ValueError("Describe the requested changes")
        if action == "accept" and feedback:
            raise ValueError("Acceptance does not include a change request")
        expected = "accepted" if action == "accept" else "changes_requested"
        with self.store.sessions.begin() as session:
            session.execute(
                update(GoalRow).where(GoalRow.goal_id == goal_id).values(revision=GoalRow.revision)
            )
            goal = session.get(GoalRow, goal_id)
            lifecycle = session.get(GoalLifecycleRow, goal_id)
            if goal is None or (lifecycle and lifecycle.deleted):
                raise KeyError(goal_id)
            row = session.get(DeliveryRow, (goal_id, revision))
            if row and row.review_status != "pending":
                if row.review_status != expected or row.feedback != feedback:
                    raise ValueError("This delivery already has a different review decision")
                return {"delivery": self._view(row), "goal": self.store._view(goal, session)}
            if goal.revision != revision or goal.state.get("status") != "succeeded":
                raise ValueError("A newer delivery or execution exists; refresh before reviewing")
            state = RunState.model_validate(goal.state)
            row = self.record(session, goal, state, archive_id)
            if action == "accept":
                if state.artifacts and not row.data.get("archive_id"):
                    raise ValueError(
                        "Delivery files could not be archived unchanged; request changes or verify them again"
                    )
                row.review_status, row.reviewed_at = "accepted", datetime.now(UTC).isoformat()
            else:
                self.store.follow_up_goal(
                    goal_id, feedback, session=session, expected_revision=revision
                )
            session.flush()
            return {"delivery": self._view(row), "goal": self.store._view(goal, session)}
