"""Offline checks of the production loop; no model or Temporal server is needed."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from temporalio.exceptions import ActivityError, ApplicationError

from rocky import evaluation
from rocky.models import Decision, ExecutionLimits, PlanResult, RunInput
from rocky.workflow import GoalWorkflow


class Paused(Exception):
    pass


@pytest.mark.parametrize("reason", ["", "   \n"])
def test_blocked_decision_requires_a_useful_reason(reason):
    task = evaluation.load_tasks(["missing-info"])[0]
    run = RunInput(goal_id="missing-info", contract=task.contract())
    error = GoalWorkflow._decision_error(run, Decision(kind="blocked", reason=reason))
    assert "missing information" in error


@pytest.mark.parametrize("empty_reason,patched", [(False, True), (True, True), (True, False)])
async def test_blocking_stops_inference_without_deliverables(monkeypatch, empty_reason, patched):
    task = evaluation.load_tasks(["missing-info"])[0]
    run = RunInput(goal_id="missing-info", contract=task.contract())
    if not patched:
        run.contract.limits.max_model_calls = 2
    goal = GoalWorkflow(run)
    snapshots, calls = [], []
    reason = "" if empty_reason else task.spec.example_blocked_reason

    async def dispatch(name, value, **kwargs):
        if name == "rocky.persist":
            snapshots.append(value.model_copy(deep=True))
        elif name == "rocky.plan":
            calls.append(value)
            # Bound a regression in the test itself instead of spinning forever.
            assert len(calls) <= 2 * run.contract.max_no_progress
            return PlanResult(decision=Decision(kind="blocked", reason=reason))
        else:
            pytest.fail(f"Blocking must not execute tools or review: {name}")

    async def wait_condition(predicate, **kwargs):
        assert not predicate()
        raise Paused

    monkeypatch.setattr("rocky.workflow.workflow.now", lambda: datetime(2026, 10, 27, tzinfo=UTC))
    monkeypatch.setattr("rocky.workflow.workflow.execute_activity", dispatch)
    monkeypatch.setattr("rocky.workflow.workflow.wait_condition", wait_condition)
    monkeypatch.setattr("rocky.workflow.workflow.patched", lambda _: patched)
    monkeypatch.setattr(
        "rocky.workflow.workflow.info", lambda: SimpleNamespace(task_queue="rocky-goals")
    )
    monkeypatch.setattr(goal, "_should_continue_as_new", lambda: False)
    if not patched:
        # A history without the patch marker must preserve its original branch.
        result = await goal.run(run)
        assert result.status == "budget_exhausted" and len(calls) == 2
        assert not any(state.status == "blocked" for state in snapshots)
        return
    with pytest.raises(Paused):
        await goal.run(run)
    assert snapshots[-1].status == "blocked"
    assert snapshots[-1].pending_approval is None
    if empty_reason:
        assert len(calls) == 2 * run.contract.max_no_progress
        assert "missing information" in snapshots[-1].reason
    else:
        assert len(calls) == 1
        assert snapshots[-1].reason == reason


@pytest.mark.parametrize("review", [False, True])
async def test_failed_model_usage_exhausts_budget_before_another_call(monkeypatch, review):
    task = evaluation.load_tasks(["missing-info"])[0]
    contract = task.contract().model_copy(update={"limits": ExecutionLimits(max_tokens=100)})
    run = RunInput(goal_id="usage", contract=contract)
    goal = GoalWorkflow(run)
    calls = []
    error = ActivityError(
        "Model failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test",
        activity_type="rocky.verify" if review else "rocky.plan",
        activity_id="test",
        retry_state=None,
    )
    error.__cause__ = ApplicationError(
        "Unusable response",
        {
            "input_tokens": 100,
            "output_tokens": 10,
            "cached_input_tokens": 50,
        },
        type="ModelDecisionError",
        non_retryable=True,
    )

    async def dispatch(name, value, **kwargs):
        if name == "rocky.persist":
            return
        calls.append(name)
        if name == "rocky.plan" and review:
            return PlanResult(decision=Decision(kind="finish", reason="Ready to check"))
        raise error

    monkeypatch.setattr("rocky.workflow.workflow.now", lambda: datetime(2026, 10, 27, tzinfo=UTC))
    monkeypatch.setattr("rocky.workflow.workflow.execute_activity", dispatch)
    monkeypatch.setattr(goal, "_should_continue_as_new", lambda: False)
    monkeypatch.setattr(goal, "_model_queue", lambda: None)
    result = await goal.run(run)
    assert result.status == "budget_exhausted"
    assert calls == (["rocky.plan", "rocky.verify"] if review else ["rocky.plan"])
    assert (result.input_tokens, result.output_tokens, result.cached_input_tokens) == (100, 10, 50)


def test_model_error_without_usage_remains_compatible():
    task = evaluation.load_tasks(["missing-info"])[0]
    goal = GoalWorkflow(RunInput(goal_id="usage", contract=task.contract()))
    error = ActivityError(
        "Model failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test",
        activity_type="rocky.plan",
        activity_id="test",
        retry_state=None,
    )
    error.__cause__ = ApplicationError("Old failure", type="ModelDecisionError")
    goal._record_failed_model_usage(error)
    assert goal.state().input_tokens == goal.state().output_tokens == 0
