import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_triggers import AUTH, event, request, rules  # noqa: F401

from rocky import triggers as trigger_module
from rocky.api import create_app
from rocky.models import RunState
from rocky.triggers import TriggerDecisionRow, TriggerLimitsRequest, TriggerRuleRow, TriggerStore


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        instant = datetime(2026, 11, 2, 12, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz or UTC)

    monkeypatch.setattr(trigger_module, "datetime", Clock)
    return Clock


def finish(triggers, decision):
    triggers.store.save_state(RunState(goal_id=decision.goal_id, status="succeeded", revision=2))


def test_old_pending_suggestion_survives_hundreds_of_new_history_records(rules, clock):  # noqa: F811
    triggers, _ = rules
    triggers.create(request())
    [old] = triggers.emit(event("old-pending"), None)
    clock.instant += timedelta(hours=1)
    for index in range(125):
        [new] = triggers.emit(event(f"new-{index}"), None)
        triggers.decide(new.decision_id, "dismiss", None)
    assert old.decision_id not in {item.decision_id for item in triggers.decisions()}
    pending = triggers.decision_page(pending_only=True)
    assert pending.total == 1 and pending.items == [old]
    cursor, ids = None, []
    while True:
        page = triggers.decision_page(cursor=cursor, limit=17)
        assert page.total == 126
        ids.extend(item.decision_id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(ids) == len(set(ids)) == 126  # equal timestamp boundaries do not drop rows


def test_pending_pages_are_oldest_first_and_cursors_survive_processed_boundaries(rules, clock):  # noqa: F811
    triggers, _ = rules
    triggers.create(request())
    emitted = []
    for index in range(7):
        clock.instant += timedelta(seconds=1)
        emitted.extend(triggers.emit(event(str(index)), None))
    first = triggers.decision_page(pending_only=True, limit=3)
    assert first.items == emitted[:3] and first.total == 7
    for item in first.items:
        triggers.decide(item.decision_id, "dismiss", None)
    second = triggers.decision_page(pending_only=True, limit=3, cursor=first.next_cursor)
    assert second.items == emitted[3:6] and second.total == 4
    with pytest.raises(ValueError, match="cursor"):
        triggers.decision_page(cursor=first.next_cursor)


def test_disabled_removed_and_old_version_suggestions_do_not_count_as_pending(rules):  # noqa: F811
    triggers, _ = rules
    rule = triggers.create(request())
    [old] = triggers.emit(event(), None)
    rule = triggers.set_mode(rule.rule_id, "off", rule.revision)
    assert triggers.decision_page(pending_only=True).total == 0
    rule = triggers.set_mode(rule.rule_id, "observe", rule.revision)
    assert triggers.decision_page(pending_only=True).total == 0
    [fresh] = triggers.emit(event("new"), None)
    assert triggers.decision_page(pending_only=True).items == [fresh]
    triggers.delete(rule.rule_id, rule.revision)
    assert triggers.decision_page(pending_only=True).total == 0
    assert triggers.decision_page().total == 2
    assert triggers.decide(old.decision_id, "dismiss", None).outcome == "dismissed"


def test_quota_is_durable_idempotent_and_manual_execution_does_not_consume_it(rules, clock):  # noqa: F811
    triggers, _ = rules
    rule = triggers.create(
        request(mode="auto", daily_auto_limit=1, daily_timezone="America/Vancouver")
    )
    [first] = triggers.emit(event(), None)
    finish(triggers, first)
    [limited] = triggers.emit(event("over-limit"), None)
    assert limited.outcome == "quota_limited" and limited.goal_id is None
    assert triggers.emit(event(), None) == [first]
    assert triggers.emit(event("over-limit"), None) == [limited]
    restarted = TriggerStore(triggers.store, triggers.tools)
    assert restarted.rule_statuses()[0].auto_runs_today == 1
    owner = restarted.decide(limited.decision_id, "run", None)
    assert owner.started_by == "owner" and owner.started_at is not None
    assert owner.reason == ""
    finish(triggers, owner)
    assert restarted.rule_statuses()[0].auto_runs_today == 1
    rule = triggers.set_mode(rule.rule_id, "off", rule.revision)
    triggers.set_mode(rule.rule_id, "auto", rule.revision)
    [still_limited] = triggers.emit(event("same-day"), None)
    assert still_limited.outcome == "quota_limited"
    clock.instant += timedelta(days=1)
    assert triggers.rule_statuses()[0].auto_runs_today == 0
    [tomorrow] = triggers.emit(event("tomorrow"), None)
    assert tomorrow.outcome == "started"
    assert triggers.emit(event("same-day"), None)[0].outcome == "quota_limited"


def test_local_day_handles_dst_and_resets_at_local_midnight(rules, clock):  # noqa: F811
    triggers, _ = rules
    triggers.create(request(mode="auto", daily_auto_limit=1, daily_timezone="America/Vancouver"))
    clock.instant = datetime(2026, 3, 8, 8, 30, tzinfo=UTC)  # 00:30 before spring DST transition
    [first] = triggers.emit(event("spring"), None)
    finish(triggers, first)
    status = triggers.rule_statuses()[0]
    assert status.auto_runs_today == 1
    assert status.quota_resets_at == "2026-03-09T07:00:00+00:00"
    clock.instant = datetime(2026, 3, 9, 6, 59, tzinfo=UTC)
    assert triggers.emit(event("before-midnight"), None)[0].outcome == "quota_limited"
    clock.instant = datetime(2026, 3, 9, 7, tzinfo=UTC)
    assert triggers.emit(event("after-midnight"), None)[0].outcome == "started"


def test_legacy_automatic_starts_count_even_without_new_metadata(rules, clock):  # noqa: F811
    triggers, _ = rules
    triggers.create(request(mode="auto", daily_auto_limit=1))
    [started] = triggers.emit(event(), None)
    finish(triggers, started)
    with triggers.store.sessions.begin() as session:
        row = session.get(TriggerDecisionRow, started.decision_id)
        legacy = {key: value for key, value in row.data.items() if key != "started_at"}
        row.data = legacy
        rule = session.scalar(select(TriggerRuleRow))
        rule.data = {
            key: value
            for key, value in rule.data.items()
            if key not in {"daily_auto_limit", "daily_timezone"}
        }
    assert triggers.rule_statuses()[0].auto_runs_today == 1
    assert triggers.rules()[0].daily_auto_limit == 5


def test_concurrent_distinct_events_cannot_exceed_daily_limit(rules, monkeypatch):  # noqa: F811
    triggers, _ = rules
    triggers.create(request(mode="auto", daily_auto_limit=1))
    # Isolate the quota guard from the separate unfinished-task guard.
    monkeypatch.setattr(TriggerStore, "_busy", staticmethod(lambda *_: False))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda index: triggers.emit(event(str(index)), None)[0], range(6)))
    assert sum(item.outcome == "started" for item in results) == 1
    assert sum(item.outcome == "quota_limited" for item in results) == 5
    assert triggers.rule_statuses()[0].auto_runs_today == 1


def test_limits_changes_are_revision_fenced_and_preserve_usage(rules, clock):  # noqa: F811
    triggers, _ = rules
    rule = triggers.create(request(mode="auto"))
    [first] = triggers.emit(event(), None)
    finish(triggers, first)
    changed = triggers.set_limits(rule.rule_id, rule.revision, 1, "UTC")
    assert changed.revision == rule.revision + 1
    assert triggers.rule_statuses()[0].auto_runs_today == 1
    assert triggers.emit(event("new"), None)[0].outcome == "quota_limited"
    with pytest.raises(ValueError, match="changed"):
        triggers.set_limits(rule.rule_id, rule.revision, 10, "UTC")
    with pytest.raises(ValueError):
        TriggerLimitsRequest(
            revision=changed.revision, daily_auto_limit=1, daily_timezone="not-a-zone"
        )


def test_observation_does_not_spend_quota_and_failed_start_rolls_back(rules, monkeypatch):  # noqa: F811
    triggers, _ = rules
    rule = triggers.create(request(daily_auto_limit=1))
    [observation] = triggers.emit(event(), None)
    triggers.decide(observation.decision_id, "run", None)
    assert triggers.rule_statuses()[0].auto_runs_today == 0
    finish(triggers, triggers.decide(observation.decision_id, "run", None))
    triggers.set_mode(rule.rule_id, "auto", rule.revision)

    def fail(*args, **kwargs):
        raise RuntimeError("simulated write failure")

    with monkeypatch.context() as patch:
        patch.setattr(triggers.store, "insert_goal", fail)
        with pytest.raises(RuntimeError):
            triggers.emit(event("rollback"), None)
    assert triggers.rule_statuses()[0].auto_runs_today == 0
    assert triggers.emit(event("rollback"), None)[0].outcome == "started"


def test_page_and_limit_api_validate_auth_cursors_and_stale_edits(rules):  # noqa: F811
    triggers, settings = rules
    rule = triggers.create(request())
    for index in range(3):
        triggers.emit(event(str(index)), None)
    with TestClient(create_app(settings, triggers.store)) as client:
        assert client.get("/trigger-decisions/page").status_code == 401
        page = client.get("/trigger-decisions/page?pending_only=true&limit=1", headers=AUTH).json()
        assert page["total"] == 3 and len(page["items"]) == 1 and page["next_cursor"]
        for cursor in [
            "bad",
            page["next_cursor"],
            base64.urlsafe_b64encode(
                json.dumps({"at": "no-date", "id": "f" * 64, "pending": False}).encode()
            ).decode(),
        ]:
            assert (
                client.get(
                    "/trigger-decisions/page", params={"cursor": cursor}, headers=AUTH
                ).status_code
                == 422
            )
        assert client.get("/trigger-decisions/page?limit=101", headers=AUTH).status_code == 422
        endpoint = f"/trigger-rules/{rule.rule_id}/limits"
        body = {"revision": 1, "daily_auto_limit": 2, "daily_timezone": "America/Vancouver"}
        assert client.post(endpoint, json=body).status_code == 401
        assert (
            client.post(
                endpoint, json=body | {"daily_timezone": "invalid"}, headers=AUTH
            ).status_code
            == 422
        )
        assert client.post(endpoint, json=body, headers=AUTH).status_code == 200
        assert client.post(endpoint, json=body, headers=AUTH).status_code == 409
        assert (
            client.get("/trigger-decisions/page?pending_only=true", headers=AUTH).json()["total"]
            == 0
        )
        [status] = client.get("/trigger-rules", headers=AUTH).json()
        assert status["daily_auto_limit"] == 2 and status["auto_runs_today"] == 0
