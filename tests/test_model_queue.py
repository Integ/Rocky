import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_planner import completed, fake_client, goal_request

from rocky.api import create_app
from rocky.config import Settings
from rocky.diagnostics import goal_diagnostics
from rocky.model_queue import RUN_LEASE_SECONDS, ModelQueue, ModelRequestCancelled, ModelRequestRow
from rocky.models import Decision, ExecutionStep, RunState
from rocky.planner import OpenAIPlanner, TransientModelError
from rocky.store import Store


@pytest.fixture
def queue(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/queue.db")
    store.initialize()
    settings = Settings(max_concurrent_model_calls=2, model_interactive_reserve=1)
    admission = ModelQueue(store, settings)
    admission.configure()
    yield admission
    store.close()


def goal(queue, **state):
    request = goal_request()
    queue.store.create_goal(request.run.goal_id, request.run.contract)
    queue.store.save_state(RunState(goal_id=request.run.goal_id, revision=1, **state))
    return queue.store.get_goal(request.run.goal_id)


def test_shared_capacity_and_reserved_slot_across_independent_connections(queue):
    other_store = Store(str(queue.store.engine.url))
    try:
        # A caller's different local default cannot bypass the stored limit.
        other = ModelQueue(other_store, Settings(max_concurrent_model_calls=8))
        first = queue.enqueue("planner")
        normal = other.enqueue("contract")
        assert queue.claim(first)
        assert not other.claim(normal)
        human = other.enqueue("planner", interactive=True)
        assert other.claim(human)
        assert other.summary() == {
            "capacity": 2,
            "reserved_interactive": 1,
            "running": 2,
            "waiting": 1,
            "interactive_waiting": 0,
        }
        another = queue.enqueue("contract", interactive=True)
        assert not queue.claim(another)
        queue.finish(first)
        assert not other.claim(normal)  # older normal waits behind human work
        assert queue.claim(another)
        queue.finish(human)
        assert other.claim(normal)
    finally:
        other_store.close()


def test_admission_never_exceeds_shared_capacity_under_concurrent_claims(queue):
    queue.reserved = 0
    queue.configure()
    ids = [queue.enqueue("planner") for _ in range(8)]
    active, peak = 0, 0
    lock = threading.Lock()

    def run(request_id):
        nonlocal active, peak
        until = time.monotonic() + 5
        while not queue.claim(request_id):
            assert time.monotonic() < until
            time.sleep(0.005)
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        queue.finish(request_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run, ids))
    assert peak <= 2 and queue.summary()["running"] == 0


def test_single_slot_configuration_still_admits_normal_work(queue):
    queue.capacity, queue.reserved = 1, 0
    queue.configure()
    assert queue.claim(queue.enqueue("revision"))
    assert not queue.claim(queue.enqueue("planner", interactive=True))


def test_crashed_waiters_expire_but_dispatched_capacity_is_not_reclaimed_early(queue, monkeypatch):
    start = datetime.now(UTC)
    stale = queue.enqueue("planner")
    next_id = queue.enqueue("planner")
    assert not queue.claim(next_id)
    # A live waiting caller refreshes its lease; an abandoned earlier one expires.
    assert not queue.claim(next_id, now=start + timedelta(seconds=40))
    assert queue.claim(next_id, now=start + timedelta(seconds=70))
    with queue.store.sessions() as session:
        assert session.get(ModelRequestRow, stale).state == "abandoned"
    monkeypatch.setattr("rocky.model_queue._now", lambda: start + timedelta(seconds=70))
    newcomer = queue.enqueue("planner")
    assert not queue.claim(newcomer, now=start + timedelta(seconds=71))
    assert not queue.claim(newcomer, now=start + timedelta(seconds=120))
    # Renew this waiter just before its expiry until the old running lease ends.
    for seconds in range(170, 70 + RUN_LEASE_SECONDS, 50):
        assert not queue.claim(newcomer, now=start + timedelta(seconds=seconds))
    assert queue.claim(newcomer, now=start + timedelta(seconds=70 + RUN_LEASE_SECONDS + 1))


async def test_cancelling_a_waiter_releases_it_without_a_provider_call(queue):
    task_goal = goal(queue)
    holder = queue.enqueue("planner")
    assert queue.claim(holder)
    client = fake_client(completed(Decision(kind="blocked", reason="Need input")))
    planner = OpenAIPlanner(Settings(), client=client, model_queue=queue)
    request = goal_request()
    request.run.goal_id = request.state.goal_id = task_goal.goal_id
    waiting = asyncio.create_task(planner.plan(request))
    for _ in range(100):
        if queue.summary()["waiting"]:
            break
        await asyncio.sleep(0.01)
    assert queue.summary()["waiting"] == 1
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    client.responses.parse.assert_not_awaited()
    assert queue.latest(task_goal.goal_id)["state"] == "cancelled"
    queue.finish(holder)


def test_cancelled_goal_cannot_obtain_a_model_slot(queue):
    task_goal = goal(queue)
    request_id = queue.enqueue("planner", task_goal.goal_id)
    queue.store.enqueue_command(task_goal.goal_id, "cancel")
    with pytest.raises(ModelRequestCancelled):
        queue.claim(request_id)


async def test_transient_failure_releases_capacity_and_records_only_safe_retry_metadata(queue):
    task_goal = goal(queue)
    with pytest.raises(TransientModelError):
        async with queue.slot("planner", task_goal.goal_id, True, attempt=2):
            raise TransientModelError("secret response body", retry_after=7)
    recorded = queue.latest(task_goal.goal_id)
    assert recorded["state"] == "backoff" and recorded["attempt"] == 2
    assert recorded["retry_at"] is not None and "secret" not in str(recorded)
    assert queue.summary()["running"] == 0


async def test_failed_diagnostics_write_does_not_discard_a_paid_model_result(queue, monkeypatch):
    task_goal = goal(queue)

    def unavailable(*args):
        raise RuntimeError("private database failure")

    monkeypatch.setattr(queue, "finish", unavailable)
    client = fake_client(completed(Decision(kind="blocked", reason="Need input")))
    planner = OpenAIPlanner(Settings(), client=client, model_queue=queue)
    request = goal_request()
    request.run.goal_id = request.state.goal_id = task_goal.goal_id
    result = await planner.plan(request)
    assert result.input_tokens == 123
    assert client.responses.parse.await_count == 1


def test_diagnostics_report_tools_waits_and_remaining_budget_without_action_contents(queue):
    now = datetime.now(UTC).isoformat()
    task_goal = goal(
        queue,
        status="running",
        phase="act",
        steps=2,
        model_calls=3,
        input_tokens=100,
        execution_step=ExecutionStep(
            step_id="step",
            cycle=1,
            phase="act",
            started_at=now,
            input={"decision": {"action": {"tool": "run_command", "content": "PRIVATE COMMAND"}}},
        ),
    )
    result = goal_diagnostics(task_goal, queue, Settings())
    assert result["stage"] == "tool" and result["title"] == "正在执行命令"
    assert result["budget"]["steps_remaining"] == 98
    assert result["budget"]["tokens_remaining"] == 499900
    assert "PRIVATE COMMAND" not in str(result)
    blocked = task_goal.model_copy(
        update={"state": task_goal.state.model_copy(update={"status": "blocked"})}
    )
    result = goal_diagnostics(blocked, queue, Settings())
    assert result["budget"]["timer_paused"] and result["budget"]["wall_seconds_remaining"] is None


async def test_diagnostics_distinguish_admission_model_and_backoff(queue):
    task_goal = goal(queue, status="running", phase="think")
    request_id = queue.enqueue("planner", task_goal.goal_id, interactive=True, attempt=2)
    assert goal_diagnostics(task_goal, queue, Settings())["stage"] == "model_queued"
    assert queue.claim(request_id)
    assert goal_diagnostics(task_goal, queue, Settings())["stage"] == "model"
    queue.finish(request_id, "backoff", 7)
    result = goal_diagnostics(task_goal, queue, Settings())
    assert result["stage"] == "retry" and result["attempt"] == 2 and result["retry_at"]


def test_queue_and_diagnostics_api_are_authenticated_and_deletion_cleans_records(queue, tmp_path):
    task_goal = goal(queue, status="running")
    queue.enqueue("planner", task_goal.goal_id)
    queue.store.save_state(
        task_goal.state.model_copy(update={"revision": 2, "status": "succeeded"})
    )
    settings = Settings(auth_mode="token", api_token="queue-test", workspace_root=tmp_path / "work")
    headers = {"Authorization": "Bearer queue-test"}
    with TestClient(create_app(settings, queue.store)) as client:
        for path in ["/model-queue", f"/goals/{task_goal.goal_id}/diagnostics"]:
            assert client.get(path).status_code == 401
            assert client.get(path, headers=headers).status_code == 200
        assert client.get("/goals/missing/diagnostics", headers=headers).status_code == 404
        result = client.get(f"/goals/{task_goal.goal_id}/diagnostics", headers=headers).json()
        assert result["stage"] == "succeeded"  # stale live request cannot override terminal state
    queue.store.delete_goal(task_goal.goal_id)
    with queue.store.sessions() as session:
        assert (
            list(
                session.scalars(
                    select(ModelRequestRow).where(ModelRequestRow.goal_id == task_goal.goal_id)
                )
            )
            == []
        )
