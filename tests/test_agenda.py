from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from rocky.agenda import (
    AgendaStore,
    PlanDecomposition,
    PlanRequest,
    PlanRevision,
    PlanStepDraft,
    Recurrence,
    ScheduleRequest,
)
from rocky.agenda_runner import AgendaRunner, step_objective
from rocky.api import create_app
from rocky.config import Settings
from rocky.models import Artifact, DraftedContract, GoalContract, RunState
from rocky.planner import OpenAIPlanner, PlannerError
from rocky.store import Store

AUTH = {"Authorization": "Bearer owner-test-token"}
NOW = datetime(2026, 10, 30, 1, 0, tzinfo=UTC)  # 09:00 in Shanghai, a Friday


def contract(objective="写一份日报") -> GoalContract:
    return GoalContract(
        objective=objective,
        acceptance_criteria=["日报完整"],
        deliverables=[{"path": "report.md"}],
    )


@pytest.fixture
def store(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/agenda.db")
    store.initialize()
    yield store
    store.close()


# Recurrence


def shanghai(**fields) -> Recurrence:
    return Recurrence(timezone="Asia/Shanghai", **fields)


def test_daily_and_weekly_runs_use_the_local_wall_clock():
    daily = shanghai(kind="daily", time="08:30")
    # 09:00 local has passed 08:30, so the next run is tomorrow 08:30 local.
    assert daily.next_after(NOW, NOW) == datetime(2026, 10, 31, 0, 30, tzinfo=UTC)
    later = shanghai(kind="daily", time="21:15")
    assert later.next_after(NOW, NOW) == datetime(2026, 10, 30, 13, 15, tzinfo=UTC)
    # Friday now; Monday and Wednesday only.
    weekly = shanghai(kind="weekly", time="09:00", weekdays=[3, 1, 1])
    assert weekly.weekdays == [1, 3]
    assert weekly.next_after(NOW, NOW) == datetime(2026, 11, 2, 1, 0, tzinfo=UTC)


def test_daily_run_skipped_by_daylight_saving_moves_an_hour_later():
    recurrence = Recurrence(kind="daily", time="02:30", timezone="America/New_York")
    after = datetime(2026, 3, 8, 5, 0, tzinfo=UTC)  # 00:00 EST on the change day
    # 02:30 does not exist that day; it runs at 03:30 EDT = 07:30 UTC.
    assert recurrence.next_after(after, after) == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)


def test_interval_counts_from_its_anchor_and_once_runs_a_single_time():
    interval = Recurrence(kind="interval", every_minutes=60)
    anchor = NOW - timedelta(minutes=150)
    assert interval.next_after(NOW, anchor) == NOW + timedelta(minutes=30)
    once = Recurrence(kind="once", start_at=NOW + timedelta(hours=1))
    assert once.next_after(NOW, NOW) == NOW + timedelta(hours=1)
    assert once.next_after(NOW + timedelta(hours=2), NOW) is None


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "once"},
        {"kind": "interval"},
        {"kind": "interval", "every_minutes": 1},
        {"kind": "daily"},
        {"kind": "daily", "time": "25:00"},
        {"kind": "weekly", "time": "09:00"},
        {"kind": "weekly", "time": "09:00", "weekdays": [8]},
        {"kind": "daily", "time": "09:00", "timezone": "Mars/Base"},
        {"kind": "once", "start_at": "2026-10-30T09:00:00"},
    ],
)
def test_incomplete_recurrences_are_rejected(fields):
    with pytest.raises(ValueError):
        Recurrence(**fields)


# Schedules


def daily_schedule(agenda: AgendaStore, **fields):
    request = ScheduleRequest(
        name="每日简报",
        contract=contract(),
        recurrence=shanghai(kind="daily", time="08:30"),
        **fields,
    )
    return agenda.create_schedule(request)


def test_due_schedule_starts_one_goal_and_missed_runs_collapse(store):
    agenda = AgendaStore(store)
    schedule = daily_schedule(agenda)
    due = datetime.fromisoformat(schedule.next_run_at)
    assert agenda.fire_due_schedules(None, due - timedelta(seconds=1)) == []
    # The Mac slept through three runs; it wakes two days later.
    late = due + timedelta(days=2, hours=1)
    [goal_id] = agenda.fire_due_schedules(None, late)
    goal = store.get_goal(goal_id)
    assert goal.state.status == "queued"
    assert "定时任务“每日简报”" in goal.contract.objective
    current = agenda.schedule(schedule.schedule_id)
    assert current.runs == 1 and current.last_goal_id == goal_id
    assert datetime.fromisoformat(current.next_run_at) == due + timedelta(days=3)
    # Firing the same time again is a no-op.
    assert agenda.fire_due_schedules(None, late) == []
    assert len(store.list_goals()) == 1


def test_run_is_skipped_while_the_previous_run_is_unfinished(store):
    agenda = AgendaStore(store)
    schedule = daily_schedule(agenda)
    first = datetime.fromisoformat(schedule.next_run_at)
    [goal_id] = agenda.fire_due_schedules(None, first)
    assert agenda.fire_due_schedules(None, first + timedelta(days=1)) == []
    current = agenda.schedule(schedule.schedule_id)
    assert current.skipped == 1 and current.runs == 1
    store.save_state(RunState(goal_id=goal_id, revision=3, status="succeeded"))
    assert len(agenda.fire_due_schedules(None, first + timedelta(days=2))) == 1


def test_one_time_schedule_runs_once_and_must_be_in_the_future(store):
    agenda = AgendaStore(store)
    soon = datetime.now(UTC) + timedelta(minutes=5)
    request = ScheduleRequest(
        name="一次", contract=contract(), recurrence=Recurrence(kind="once", start_at=soon)
    )
    schedule = agenda.create_schedule(request)
    assert len(agenda.fire_due_schedules(None, soon + timedelta(seconds=1))) == 1
    assert agenda.schedule(schedule.schedule_id).next_run_at is None
    past = request.model_copy(
        update={"recurrence": Recurrence(kind="once", start_at=soon - timedelta(hours=1))}
    )
    with pytest.raises(ValueError, match="already passed"):
        agenda.create_schedule(past)


def test_paused_schedule_does_not_run(store):
    agenda = AgendaStore(store)
    schedule = daily_schedule(agenda, enabled=False)
    assert schedule.next_run_at is None
    assert agenda.fire_due_schedules(None, NOW + timedelta(days=5)) == []
    resumed = agenda.set_schedule_enabled(schedule.schedule_id, True)
    assert resumed.next_run_at is not None


# Plans


class FakePlanner:
    def __init__(self):
        self.drafts = []
        self.reviews = []
        self.revision = PlanRevision(complete=False)
        self.fail_drafts = 0

    async def prepare_contract(self, draft, model_settings):
        if self.fail_drafts:
            self.fail_drafts -= 1
            raise PlannerError("model unavailable")
        self.drafts.append(draft)
        return DraftedContract(
            objective=draft.objective,
            folders=draft.folders,
            files=draft.files,
            acceptance_criteria=["完成本步"],
            deliverables=[{"path": "notes.md"}],
            drafting_tokens=10,
        )

    async def revise_plan(self, progress, model_settings):
        self.reviews.append(progress)
        if self.revision.complete or not self.revision.remaining:
            remaining = [] if self.revision.complete else progress["remaining"]
            revision = self.revision.model_copy(
                update={"remaining": [PlanStepDraft(**item) for item in remaining]}
            )
        else:
            revision = self.revision
        return revision, 5


@pytest.fixture
def runner(store, tmp_path):
    settings = Settings(workspace_root=tmp_path / "workspaces", agenda_concurrency=1)
    settings.workspace_root.mkdir()
    return AgendaRunner(store, settings, FakePlanner())


def two_step_plan(agenda: AgendaStore, **fields):
    return agenda.create_plan(
        PlanRequest(
            objective="调研并写出智能门锁选购报告",
            steps=[
                PlanStepDraft(title="调研", objective="收集主流门锁的资料"),
                PlanStepDraft(title="成文", objective="根据资料写选购报告"),
            ],
            **fields,
        )
    )


def finish(runner: AgendaRunner, goal_id: str, status="succeeded", text="资料已整理"):
    workspace = runner.settings.workspace_root / goal_id
    workspace.mkdir(exist_ok=True)
    (workspace / "notes.md").write_text(text)
    artifacts = [Artifact(path="notes.md", size=len(text.encode()), sha256="0" * 64)]
    state = RunState(
        goal_id=goal_id, revision=50, status=status, handoff_summary=text, artifacts=artifacts
    )
    runner.store.save_state(state)


async def test_plan_runs_steps_in_order_and_hands_files_forward(runner):
    agenda = runner.agenda
    plan = two_step_plan(agenda)
    await runner.tick()
    plan = agenda.plan(plan.plan_id)
    first, second = plan.steps
    assert first.status == "running" and second.status == "pending"
    assert store_goal(runner, first.goal_id).state.status == "queued"
    assert "第 1/2 步“调研”" in runner.planner.drafts[0].objective
    await runner.tick()  # still running: nothing else starts
    assert len(runner.planner.drafts) == 1

    finish(runner, first.goal_id, text="三款门锁的参数")
    await runner.tick()
    plan = agenda.plan(plan.plan_id)
    assert plan.steps[0].status == "succeeded"
    assert plan.steps[0].outputs == ["notes.md"]
    assert plan.review_pending
    await runner.tick()  # review keeps the remaining step
    plan = agenda.plan(plan.plan_id)
    assert not plan.review_pending and plan.revisions == 1
    assert plan.steps[1].step_id == second.step_id
    review = runner.planner.reviews[0]
    assert review["finished"][0]["summary"] == "三款门锁的参数"
    assert review["remaining"] == [{"title": "成文", "objective": "根据资料写选购报告"}]

    await runner.tick()
    plan = agenda.plan(plan.plan_id)
    goal_id = plan.steps[1].goal_id
    copied = runner.settings.workspace_root / goal_id / "inputs" / "step-1" / "notes.md"
    assert copied.read_text() == "三款门锁的参数"
    objective = runner.planner.drafts[1].objective
    assert "inputs/step-1/notes.md" in objective and "三款门锁的参数" in objective

    runner.planner.revision = PlanRevision(complete=True)
    finish(runner, goal_id)
    for _ in range(3):
        await runner.tick()
    plan = agenda.plan(plan.plan_id)
    assert plan.status == "succeeded"
    assert plan.reason.startswith("全部完成：2 步")
    assert plan.planning_tokens == 10 * 2 + 5 * 2


def store_goal(runner, goal_id):
    return runner.store.get_goal(goal_id)


async def test_review_can_replace_remaining_steps_or_end_the_plan_early(runner):
    agenda = runner.agenda
    plan = two_step_plan(agenda)
    runner.planner.revision = PlanRevision(
        complete=False,
        remaining=[
            PlanStepDraft(title="对比", objective="对比三款门锁"),
            PlanStepDraft(title="成文", objective="写报告"),
        ],
        note="资料显示需要先做对比",
    )
    await runner.tick()
    finish(runner, agenda.plan(plan.plan_id).steps[0].goal_id)
    await runner.tick()
    await runner.tick()
    plan = agenda.plan(plan.plan_id)
    assert [step.title for step in plan.steps] == ["调研", "对比", "成文"]
    assert plan.notes == ["资料显示需要先做对比"]

    other = two_step_plan(agenda)
    runner.planner.revision = PlanRevision(complete=True, note="一步就够了")
    agenda.cancel_plan(plan.plan_id)
    await runner.tick()
    finish(runner, agenda.plan(other.plan_id).steps[0].goal_id)
    for _ in range(3):
        await runner.tick()
    other = agenda.plan(other.plan_id)
    assert other.status == "succeeded"
    assert other.steps[1].status == "skipped"
    assert other.reason.endswith("1 步跳过。")


async def test_failed_step_asks_the_owner_and_can_be_retried_or_skipped(runner):
    agenda = runner.agenda
    plan = two_step_plan(agenda, replan=False)
    await runner.tick()
    first_goal = agenda.plan(plan.plan_id).steps[0].goal_id
    finish(runner, first_goal, status="budget_exhausted")
    await runner.tick()
    plan = agenda.plan(plan.plan_id)
    assert plan.status == "attention" and plan.steps[0].status == "failed"
    assert "预算用完" in plan.reason
    await runner.tick()  # waits for the owner
    assert len(runner.planner.drafts) == 1

    plan = agenda.owner_action(plan.plan_id, "retry")
    await runner.tick()
    plan = agenda.plan(plan.plan_id)
    assert plan.status == "active"
    assert plan.steps[0].attempts == 1
    assert plan.steps[0].goal_id not in {None, first_goal}

    finish(runner, plan.steps[0].goal_id, status="failed")
    await runner.tick()
    plan = agenda.owner_action(plan.plan_id, "skip")
    assert plan.steps[0].status == "skipped"
    await runner.tick()
    assert agenda.plan(plan.plan_id).steps[1].status == "running"


async def test_owner_continuing_a_failed_step_goal_resumes_the_plan(runner):
    agenda = runner.agenda
    plan = two_step_plan(agenda, replan=False)
    await runner.tick()
    goal_id = agenda.plan(plan.plan_id).steps[0].goal_id
    finish(runner, goal_id, status="budget_exhausted")
    await runner.tick()
    runner.store.follow_up_goal(goal_id, "再补充一款")
    await runner.tick()
    plan = agenda.plan(plan.plan_id)
    assert plan.status == "active" and plan.steps[0].status == "running"
    assert plan.steps[0].goal_id == goal_id


async def test_repeated_drafting_failures_back_off_then_need_the_owner(runner):
    agenda = runner.agenda
    plan = two_step_plan(agenda)
    runner.planner.fail_drafts = 3
    now = datetime.now(UTC)
    await runner.tick(now)
    plan = agenda.plan(plan.plan_id)
    assert plan.draft_failures == 1 and plan.retry_at is not None
    await runner.tick(now + timedelta(seconds=30))  # still backing off
    assert runner.planner.fail_drafts == 2
    await runner.tick(now + timedelta(minutes=3))
    await runner.tick(now + timedelta(minutes=10))
    plan = agenda.plan(plan.plan_id)
    assert plan.status == "attention" and plan.steps[0].status == "pending"
    agenda.owner_action(plan.plan_id, "retry")
    await runner.tick()
    assert agenda.plan(plan.plan_id).steps[0].status == "running"


async def test_plans_share_the_agenda_slot_by_priority(runner):
    agenda = runner.agenda
    low = two_step_plan(agenda)
    high = two_step_plan(agenda, priority=5)
    await runner.tick()
    assert agenda.plan(high.plan_id).steps[0].status == "running"
    assert agenda.plan(low.plan_id).steps[0].status == "pending"
    agenda.owner_action(high.plan_id, "pause")
    await runner.tick()
    # The paused plan's running step still holds the slot.
    assert agenda.plan(low.plan_id).steps[0].status == "pending"


async def test_cancelling_a_plan_stops_its_running_step(runner):
    agenda = runner.agenda
    plan = two_step_plan(agenda)
    await runner.tick()
    goal_id = agenda.plan(plan.plan_id).steps[0].goal_id
    agenda.cancel_plan(plan.plan_id)
    assert runner.store.is_cancel_requested(goal_id)
    with pytest.raises(ValueError):
        agenda.owner_action(plan.plan_id, "resume")
    agenda.delete_plan(plan.plan_id)
    assert agenda.plan(plan.plan_id) is None
    assert runner.store.get_goal(goal_id) is not None


def test_step_objective_stays_within_the_contract_limit(store):
    agenda = AgendaStore(store)
    drafts = [PlanStepDraft(title=f"步骤{i}", objective="很长" * 1900) for i in range(20)]
    plan = agenda.create_plan(PlanRequest(objective="目标" * 1900, steps=drafts))
    for step in plan.steps[:19]:
        step.status, step.summary = "succeeded", "总结" * 900
    text = step_objective(plan, plan.steps[19], {})
    assert len(text) <= 8000 and text.startswith("很长")


# API


@pytest.fixture
def api(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/api.db")
    settings = Settings(
        auth_mode="token", api_token="owner-test-token", model="gpt-4.1-mini", reasoning_effort=None
    )
    with TestClient(create_app(settings, store)) as client:
        yield client, store
    store.close()


def test_plan_api_drafts_creates_and_controls_plans(api, monkeypatch):
    client, store = api
    decomposition = PlanDecomposition(
        steps=[PlanStepDraft(title="调研", objective="收集资料")], concerns=["缺少预算"]
    )
    monkeypatch.setattr(
        OpenAIPlanner, "decompose_goal", AsyncMock(return_value=(decomposition, 42))
    )
    assert client.post("/plans/draft", json={"objective": "x"}).status_code == 401
    response = client.post("/plans/draft", json={"objective": "调研门锁"}, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "steps": [{"title": "调研", "objective": "收集资料"}],
        "concerns": ["缺少预算"],
        "drafting_tokens": 42,
    }
    assert store.list_goals() == []

    body = {"objective": "调研门锁", "steps": response.json()["steps"]}
    created = client.post("/plans", json=body, headers=AUTH)
    assert created.status_code == 201
    plan_id = created.json()["plan_id"]
    assert [item["plan_id"] for item in client.get("/plans", headers=AUTH).json()] == [plan_id]
    paused = client.post(f"/plans/{plan_id}/pause", headers=AUTH)
    assert paused.json()["status"] == "paused"
    assert client.post(f"/plans/{plan_id}/pause", headers=AUTH).status_code == 409
    assert client.post(f"/plans/{plan_id}/explode", headers=AUTH).status_code == 422
    steps = {"steps": [{"title": "成文", "objective": "写报告"}]}
    replaced = client.put(f"/plans/{plan_id}/steps", json=steps, headers=AUTH)
    assert [step["title"] for step in replaced.json()["steps"]] == ["成文"]
    assert client.post(f"/plans/{plan_id}/cancel", headers=AUTH).json()["status"] == "cancelled"
    assert client.delete(f"/plans/{plan_id}", headers=AUTH).status_code == 204
    assert client.get(f"/plans/{plan_id}", headers=AUTH).status_code == 404


def test_plan_draft_failure_is_sanitized(api, monkeypatch):
    client, _ = api
    failure = AsyncMock(side_effect=PlannerError("secret details"))
    monkeypatch.setattr(OpenAIPlanner, "decompose_goal", failure)
    response = client.post("/plans/draft", json={"objective": "调研"}, headers=AUTH)
    assert response.status_code == 502
    assert "secret" not in response.text


def test_schedule_api_creates_runs_pauses_and_deletes(api):
    client, store = api
    body = {
        "name": "每日简报",
        "contract": contract().model_dump(mode="json"),
        "recurrence": {"kind": "daily", "time": "08:30", "timezone": "Asia/Shanghai"},
    }
    created = client.post("/schedules", json=body, headers=AUTH)
    assert created.status_code == 201
    schedule_id = created.json()["schedule_id"]
    assert created.json()["next_run_at"]
    run = client.post(f"/schedules/{schedule_id}/run", headers=AUTH)
    assert run.status_code == 202
    assert store.get_goal(run.json()["goal_id"]).state.status == "queued"
    # The first run is still going.
    assert client.post(f"/schedules/{schedule_id}/run", headers=AUTH).status_code == 409
    paused = client.post(f"/schedules/{schedule_id}/pause", headers=AUTH).json()
    assert not paused["enabled"] and paused["next_run_at"] is None
    resumed = client.post(f"/schedules/{schedule_id}/resume", headers=AUTH).json()
    assert resumed["enabled"] and resumed["next_run_at"]
    body["recurrence"] = {"kind": "interval", "every_minutes": 30}
    updated = client.put(f"/schedules/{schedule_id}", json=body, headers=AUTH).json()
    assert updated["recurrence"]["kind"] == "interval" and updated["runs"] == 1
    assert client.delete(f"/schedules/{schedule_id}", headers=AUTH).status_code == 204
    assert client.get("/schedules", headers=AUTH).json() == []
    # Started tasks are kept.
    assert len(store.list_goals()) == 1


def test_schedule_api_rejects_a_past_one_time_run(api):
    client, _ = api
    body = {
        "name": "过去",
        "contract": contract().model_dump(mode="json"),
        "recurrence": {"kind": "once", "start_at": "2020-01-01T00:00:00+00:00"},
    }
    assert client.post("/schedules", json=body, headers=AUTH).status_code == 422


def test_schedule_api_accepts_the_desktop_encoding(api):
    client, _ = api
    soon = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "name": "提醒",
        "contract": contract().model_dump(mode="json", exclude_none=True),
        "recurrence": {
            "kind": "once",
            "start_at": soon,
            "weekdays": [],
            "timezone": "Asia/Shanghai",
        },
        "enabled": True,
    }
    response = client.post("/schedules", json=body, headers=AUTH)
    assert response.status_code == 201
    assert response.json()["next_run_at"].startswith(soon[:16])
