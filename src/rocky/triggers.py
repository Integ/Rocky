"""Owner-defined event rules. Events are evidence, never new task authority."""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from sqlalchemy import JSON, Integer, String, and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from rocky.inference import ModelSettings
from rocky.models import GoalContract, Model
from rocky.store import TERMINAL_STATUSES, Base, MessageRow, Store
from rocky.tools import ToolError, WorkspaceTools

Mode = Literal["off", "observe", "auto"]
log = logging.getLogger(__name__)


def stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(UTC).isoformat()


class TriggerRuleRequest(Model):
    name: str = Field(min_length=1, max_length=100)
    source: Literal["file", "event"]
    match: str = Field(min_length=1, max_length=1000)
    mode: Mode = "observe"
    contract: GoalContract
    debounce_seconds: int = Field(default=30, ge=5, le=86400)
    daily_auto_limit: int = Field(default=5, ge=1, le=100, strict=True)
    daily_timezone: str = Field(default="UTC", min_length=1, max_length=100)

    @field_validator("daily_timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use a valid IANA timezone") from None
        return value

    @field_validator("name", "match")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("A name and event name or file path are required")
        return value.strip()


class TriggerRule(TriggerRuleRequest):
    rule_id: str
    revision: int = 1
    created_at: str
    last_goal_id: str | None = None
    problem: str = ""


class TriggerEvent(Model):
    event_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2000)

    @field_validator("event_id", "name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Event ID and name cannot be blank")
        return value.strip()


class TriggerDecision(Model):
    decision_id: str
    rule_id: str
    rule_name: str
    rule_revision: int
    event: TriggerEvent
    mode: Mode
    outcome: Literal[
        "observed", "started", "skipped_busy", "dismissed", "unavailable", "quota_limited"
    ]
    received_at: str
    goal_id: str | None = None
    reason: str = ""
    started_by: Literal["auto", "owner"] | None = None
    started_at: str | None = None


class TriggerRuleStatus(TriggerRule):
    auto_runs_today: int
    quota_resets_at: str


class TriggerLimitsRequest(Model):
    revision: int = Field(ge=1)
    daily_auto_limit: int = Field(ge=1, le=100, strict=True)
    daily_timezone: str = Field(min_length=1, max_length=100)

    @field_validator("daily_timezone")
    @classmethod
    def valid_timezone(cls, value):
        return TriggerRuleRequest.valid_timezone(value)


class TriggerDecisionPage(Model):
    items: list[TriggerDecision]
    total: int
    next_cursor: str | None = None


class _DecisionCursor(Model):
    at: str = Field(max_length=64)
    id: str = Field(pattern=r"^[a-f0-9]{64}$")
    pending: bool

    @field_validator("at")
    @classmethod
    def timestamp(cls, value):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("Cursor timestamps require a timezone")
        return value


_PENDING = ("observed", "skipped_busy", "unavailable", "quota_limited")


class TriggerRuleRow(Base):
    __tablename__ = "trigger_rules"
    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)


class TriggerEnvelopeRow(Base):
    __tablename__ = "trigger_envelopes"
    event_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class TriggerDecisionRow(Base):
    __tablename__ = "trigger_decisions"
    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(64), index=True)
    rule_id: Mapped[str] = mapped_column(String(64), index=True)
    received_at: Mapped[str] = mapped_column(String(64), index=True)
    data: Mapped[dict] = mapped_column(JSON)


class FileCursorRow(Base):
    __tablename__ = "trigger_file_cursors"
    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class TriggerStore:
    def __init__(self, store: Store, tools: WorkspaceTools):
        self.store, self.tools = store, tools

    def validate(self, rule: TriggerRuleRequest) -> TriggerRuleRequest:
        contract = rule.contract
        if rule.source == "file":
            path = str(Path(rule.match).expanduser().resolve())
            contract = GoalContract.model_validate(
                {**contract.model_dump(), "files": list(dict.fromkeys([*contract.files, path]))}
            )
            rule = rule.model_copy(update={"match": path, "contract": contract})
        elif len(rule.match) > 200:
            raise ValueError("An event name has at most 200 characters")
        self.tools.granted_folders(contract)
        self.tools.attached_files(contract)
        return rule

    def create(self, request: TriggerRuleRequest) -> TriggerRule:
        request = self.validate(request)
        rule = TriggerRule(**request.model_dump(), rule_id=uuid4().hex, created_at=stamp())
        with self.store.sessions.begin() as session:
            if session.scalar(select(func.count()).select_from(TriggerRuleRow)) >= 100:
                raise ValueError("At most 100 event rules are supported")
            session.add(
                TriggerRuleRow(rule_id=rule.rule_id, revision=1, data=rule.model_dump(mode="json"))
            )
        return rule

    def rules(self) -> list[TriggerRule]:
        with self.store.sessions() as session:
            return [
                TriggerRule.model_validate(row.data)
                for row in session.scalars(
                    select(TriggerRuleRow).order_by(TriggerRuleRow.rule_id).limit(100)
                )
            ]

    @staticmethod
    def _lock(session, rule_id):
        session.execute(
            update(TriggerRuleRow)
            .where(TriggerRuleRow.rule_id == rule_id)
            .values(revision=TriggerRuleRow.revision)
        )
        row = session.get(TriggerRuleRow, rule_id, populate_existing=True)
        if row is None:
            raise KeyError(rule_id)
        return row

    def set_mode(self, rule_id: str, mode: Mode, revision: int) -> TriggerRule:
        with self.store.sessions.begin() as session:
            row = self._lock(session, rule_id)
            if row.revision != revision:
                raise ValueError("The rule changed; refresh before changing its mode")
            rule = TriggerRule.model_validate(row.data)
            if mode != "off":
                self.validate(rule)
            rule.mode, rule.revision = mode, rule.revision + 1
            if mode == "off":
                rule.problem = ""
            row.revision, row.data = rule.revision, rule.model_dump(mode="json")
            cursor = session.get(FileCursorRow, rule_id)
            if cursor:
                session.delete(cursor)
            return rule

    def decisions(self, limit: int = 100) -> list[TriggerDecision]:
        with self.store.sessions() as session:
            return [
                TriggerDecision.model_validate(row.data)
                for row in session.scalars(
                    select(TriggerDecisionRow)
                    .order_by(TriggerDecisionRow.received_at.desc(), TriggerDecisionRow.decision_id)
                    .limit(limit)
                )
            ]

    def decision_page(self, *, pending_only=False, limit=25, cursor=None) -> TriggerDecisionPage:
        if not 1 <= limit <= 100:
            raise ValueError("Page limit must be between 1 and 100")
        boundary = None
        if cursor:
            try:
                if len(cursor) > 2048:
                    raise ValueError()
                boundary = _DecisionCursor.model_validate_json(
                    base64.b64decode(
                        cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
                    )
                )
                if boundary.pending != pending_only:
                    raise ValueError()
            except (ValueError, binascii.Error):
                raise ValueError("Invalid event page cursor") from None
        query = select(TriggerDecisionRow)
        if pending_only:
            query = query.join(
                TriggerRuleRow, TriggerRuleRow.rule_id == TriggerDecisionRow.rule_id
            ).where(
                TriggerDecisionRow.data["outcome"].as_string().in_(_PENDING),
                TriggerDecisionRow.data["rule_revision"].as_integer() == TriggerRuleRow.revision,
                TriggerRuleRow.data["mode"].as_string() != "off",
            )
        count = select(func.count()).select_from(query.subquery())
        if boundary:
            at, key = TriggerDecisionRow.received_at, TriggerDecisionRow.decision_id
            query = query.where(
                or_(at > boundary.at, and_(at == boundary.at, key > boundary.id))
                if pending_only
                else or_(at < boundary.at, and_(at == boundary.at, key < boundary.id))
            )
        order = (TriggerDecisionRow.received_at, TriggerDecisionRow.decision_id)
        query = query.order_by(*(order if pending_only else (item.desc() for item in order)))
        with self.store.sessions() as session:
            total = session.scalar(count)
            rows = list(session.scalars(query.limit(limit + 1)))
            items = [TriggerDecision.model_validate(row.data) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            payload = _DecisionCursor(
                at=last.received_at, id=last.decision_id, pending=pending_only
            )
            next_cursor = (
                base64.urlsafe_b64encode(payload.model_dump_json().encode()).decode().rstrip("=")
            )
        return TriggerDecisionPage(items=items, total=total, next_cursor=next_cursor)

    @staticmethod
    def _daily_usage(session, rule, now=None):
        local = (now or datetime.now(UTC)).astimezone(ZoneInfo(rule.daily_timezone))
        start = datetime.combine(local.date(), time.min, tzinfo=local.tzinfo)
        end = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=local.tzinfo)
        # Legacy automatic starts happened on receipt, and had no started_at.
        started = func.coalesce(
            TriggerDecisionRow.data["started_at"].as_string(), TriggerDecisionRow.received_at
        )
        used = session.scalar(
            select(func.count())
            .select_from(TriggerDecisionRow)
            .where(
                TriggerDecisionRow.rule_id == rule.rule_id,
                TriggerDecisionRow.data["outcome"].as_string() == "started",
                TriggerDecisionRow.data["started_by"].as_string() == "auto",
                started >= stamp(start),
                started < stamp(end),
            )
        )
        return used, stamp(end)

    def rule_statuses(self, now=None):
        with self.store.sessions() as session:
            result = []
            for row in session.scalars(
                select(TriggerRuleRow).order_by(TriggerRuleRow.rule_id).limit(100)
            ):
                rule = TriggerRule.model_validate(row.data)
                used, reset = self._daily_usage(session, rule, now)
                result.append(
                    TriggerRuleStatus(
                        **rule.model_dump(), auto_runs_today=used, quota_resets_at=reset
                    )
                )
            return result

    def set_limits(self, rule_id, revision, daily_auto_limit, daily_timezone):
        with self.store.sessions.begin() as session:
            row = self._lock(session, rule_id)
            if row.revision != revision:
                raise ValueError("The rule changed; refresh before changing its limits")
            rule = TriggerRule.model_validate(
                {**row.data, "daily_auto_limit": daily_auto_limit, "daily_timezone": daily_timezone}
            )
            rule.revision += 1
            row.revision, row.data = rule.revision, rule.model_dump(mode="json")
            return rule

    def delete(self, rule_id: str, revision: int) -> None:
        with self.store.sessions.begin() as session:
            row = self._lock(session, rule_id)
            if row.revision != revision:
                raise ValueError("The rule changed; refresh before removing it")
            cursor = session.get(FileCursorRow, rule_id)
            if cursor:
                session.delete(cursor)
            session.delete(row)

    def emit(self, event: TriggerEvent, model: ModelSettings | None) -> list[TriggerDecision]:
        key = hashlib.sha256(("external:" + event.event_id).encode()).hexdigest()
        try:
            with self.store.sessions.begin() as session:
                existing = session.get(TriggerEnvelopeRow, key)
                if existing:
                    return self._existing(session, key, event)
                session.add(TriggerEnvelopeRow(event_key=key, payload=event.model_dump()))
                session.flush()
                ids = session.scalars(
                    select(TriggerRuleRow.rule_id).order_by(TriggerRuleRow.rule_id)
                ).all()
                result = []
                for rule_id in ids:
                    row = self._lock(session, rule_id)
                    rule = TriggerRule.model_validate(row.data)
                    if rule.source == "event" and rule.mode != "off" and rule.match == event.name:
                        result.append(self._receive(session, row, rule, key, event, model))
                return result
        except IntegrityError:
            # Concurrent redelivery is a read, never a second task.
            with self.store.sessions() as session:
                return self._existing(session, key, event)

    @staticmethod
    def _existing(session, key, event):
        stored = session.get(TriggerEnvelopeRow, key)
        if stored is None or stored.payload != event.model_dump():
            raise ValueError("An event ID cannot be reused for different content")
        return [
            TriggerDecision.model_validate(data)
            for data in session.scalars(
                select(TriggerDecisionRow.data).where(TriggerDecisionRow.event_key == key)
            )
        ]

    def _receive(self, session, row, rule, key, event, model, now=None):
        decision_id = hashlib.sha256((rule.rule_id + ":" + key).encode()).hexdigest()
        existing = session.get(TriggerDecisionRow, decision_id)
        if existing:
            return TriggerDecision.model_validate(existing.data)
        decision = TriggerDecision(
            decision_id=decision_id,
            rule_id=rule.rule_id,
            rule_name=rule.name,
            rule_revision=rule.revision,
            event=event,
            mode=rule.mode,
            outcome="observed",
            received_at=stamp(now),
        )
        if rule.mode == "auto":
            if self._busy(session, rule):
                decision.outcome = "skipped_busy"
            elif self._daily_usage(session, rule, now)[0] >= rule.daily_auto_limit:
                decision.outcome = "quota_limited"
                decision.reason = f"今日自动启动已达 {rule.daily_auto_limit} 次上限（{rule.daily_timezone}），本次仅记录建议，可手动执行。"
            else:
                try:
                    self._start(session, row, rule, decision, model, now=now)
                except ToolError:
                    decision.outcome = "unavailable"
                    decision.reason = "规则中的文件不可访问，请检查后重试。"
        session.add(
            TriggerDecisionRow(
                decision_id=decision_id,
                event_key=key,
                rule_id=rule.rule_id,
                received_at=decision.received_at,
                data=decision.model_dump(mode="json"),
            )
        )
        return decision

    @staticmethod
    def _busy(session, rule):
        from rocky.agenda import AgendaStore

        status = AgendaStore.goal_status(session, rule.last_goal_id) if rule.last_goal_id else None
        return status is not None and status not in TERMINAL_STATUSES

    def _start(self, session, row, rule, decision, model, by="auto", now=None):
        self.validate(rule)
        goal_id = str(uuid5(NAMESPACE_URL, "rocky:trigger:" + decision.decision_id))
        # Only the owner's fixed contract grants authority. Event text is tool
        # evidence in memory, never concatenated into the authorized objective.
        self.store.insert_goal(session, goal_id, rule.contract, model)
        session.add(
            MessageRow(
                goal_id=goal_id,
                message_id="trigger:" + decision.decision_id,
                role="tool",
                content=json.dumps(
                    {"untrusted_event": decision.event.model_dump()}, ensure_ascii=False
                ),
                priority=10,
                created_at=decision.received_at,
            )
        )
        decision.outcome, decision.goal_id = "started", goal_id
        decision.reason = ""
        decision.started_by = by
        decision.started_at = stamp(now)
        rule.last_goal_id = goal_id
        row.data = rule.model_dump(mode="json")

    def decide(
        self, decision_id: str, action: Literal["run", "dismiss"], model: ModelSettings | None
    ):
        with self.store.sessions.begin() as session:
            session.execute(
                update(TriggerDecisionRow)
                .where(TriggerDecisionRow.decision_id == decision_id)
                .values(data=TriggerDecisionRow.data)
            )
            entry = session.get(TriggerDecisionRow, decision_id)
            if entry is None:
                raise KeyError(decision_id)
            decision = TriggerDecision.model_validate(entry.data)
            if decision.outcome in {"started", "dismissed"}:
                return decision
            if action == "dismiss":
                decision.outcome = "dismissed"
            else:
                row = self._lock(session, entry.rule_id)
                rule = TriggerRule.model_validate(row.data)
                if rule.mode == "off" or rule.revision != decision.rule_revision:
                    raise ValueError("This suggestion used an old or disabled rule")
                if self._busy(session, rule):
                    raise ValueError("The rule's previous task has not ended")
                self._start(session, row, rule, decision, model, by="owner")
            entry.data = decision.model_dump(mode="json")
            return decision

    def poll_files(self, model: ModelSettings | None, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        for rule in self.rules():
            if rule.source != "file" or rule.mode == "off":
                continue
            try:
                self.validate(rule)
                info = Path(rule.match).stat()
                signature = f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_size}"
                with self.store.sessions.begin() as session:
                    row = self._lock(session, rule.rule_id)
                    current = TriggerRule.model_validate(row.data)
                    if current.revision != rule.revision or current.mode == "off":
                        continue
                    if current.problem:
                        current.problem = ""
                        row.data = current.model_dump(mode="json")
                    cursor = session.get(FileCursorRow, rule.rule_id)
                    if cursor is None:
                        session.add(
                            FileCursorRow(rule_id=rule.rule_id, data={"fingerprint": signature})
                        )
                    elif cursor.data["fingerprint"] == signature:
                        cursor.data = {"fingerprint": signature}
                    elif cursor.data.get("candidate") != signature:
                        cursor.data = {**cursor.data, "candidate": signature, "since": stamp(now)}
                    elif (
                        now - datetime.fromisoformat(cursor.data["since"])
                    ).total_seconds() >= rule.debounce_seconds:
                        key = hashlib.sha256(
                            (rule.rule_id + str(rule.revision) + signature).encode()
                        ).hexdigest()
                        event = TriggerEvent(
                            event_id=key,
                            name="file.changed",
                            summary="监听文件已变化：" + rule.match,
                        )
                        self._receive(session, row, current, key, event, model, now)
                        cursor.data = {"fingerprint": signature}
            except KeyError:
                continue
            except (OSError, ToolError, ValueError):
                with self.store.sessions.begin() as session:
                    try:
                        row = self._lock(session, rule.rule_id)
                    except KeyError:
                        continue
                    current = TriggerRule.model_validate(row.data)
                    if current.revision == rule.revision:
                        current.problem = "无法检查监听文件，请确认文件仍存在且可访问。"
                        row.data = current.model_dump(mode="json")

    async def run(self, defaults: ModelSettings) -> None:
        try:
            while True:
                try:
                    model = await asyncio.to_thread(self.store.get_model_settings, defaults)
                    await asyncio.to_thread(self.poll_files, model)
                except Exception:
                    log.warning("Event source check deferred")
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
