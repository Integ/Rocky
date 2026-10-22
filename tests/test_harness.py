import asyncio
import threading
from types import SimpleNamespace

import pytest
from temporalio.exceptions import ApplicationError

from rocky.activities import Activities
from rocky.harness import HarnessHooks
from rocky.models import (
    Artifact,
    Decision,
    Deliverable,
    GoalContract,
    PhaseInput,
    PhaseResult,
    RunInput,
    RunState,
    VerificationResult,
    VerifyInput,
)


def request(phase="decide"):
    return PhaseInput(
        run=RunInput(
            goal_id="hooks",
            contract=GoalContract(
                objective="Write a report",
                acceptance_criteria=["Has evidence"],
                deliverables=[Deliverable(path="report.md")],
            ),
        ),
        state=RunState(goal_id="hooks"),
        phase=phase,
    )


async def test_sync_and_async_handlers_compose_without_blocking_activity_loop():
    hooks = HarnessHooks()
    loop_thread = threading.get_ident()
    called = []

    def sync(context):
        assert threading.get_ident() != loop_thread
        called.append("sync")
        return PhaseResult(feedback="Inspect the source", handoff_summary="Source is pending")

    async def async_handler(context):
        await asyncio.sleep(0)
        assert context.feedback == "Inspect the source"
        assert context.state.handoff_summary == "Source is pending"
        called.append("async")
        return PhaseResult(decision=Decision(kind="blocked", reason="Source is unavailable"))

    hooks.register("decide", sync)
    hooks.register("decide", async_handler)
    original = request()
    result = await hooks.run(original)
    assert called == ["sync", "async"]
    assert result.decision.kind == "blocked"
    assert result.feedback == "Inspect the source"
    assert original.feedback == ""
    assert original.state.handoff_summary == ""


async def test_hooks_cannot_replace_decision_outside_decide_phase():
    hooks = HarnessHooks()
    hooks.register(
        "act",
        lambda context: PhaseResult(
            decision=Decision(kind="finish", reason="Premature completion")
        ),
    )
    with pytest.raises(ValueError, match="Only the decide phase"):
        await hooks.run(request("act"))


async def test_empty_phase_is_noop_and_unknown_phase_is_rejected():
    hooks = HarnessHooks()
    assert await hooks.run(request()) == PhaseResult()
    with pytest.raises(ValueError, match="Unknown execution phase"):
        hooks.register("unknown", lambda context: None)


async def test_activity_sanitizes_hook_errors_before_persistence():
    hooks = HarnessHooks()

    def fail(context):
        raise RuntimeError("private request body and credential")

    hooks.register("decide", fail)
    activities = Activities(
        SimpleNamespace(is_cancel_requested=lambda goal_id: False),
        SimpleNamespace(),
        SimpleNamespace(),
        hooks=hooks,
    )
    with pytest.raises(ApplicationError, match="Phase hook failed") as caught:
        await activities.phase(request())
    assert caught.value.type == "HarnessHookFailed"
    assert caught.value.non_retryable
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("changed", [False, True])
async def test_completion_rechecks_actual_artifact_bytes_after_model_review(changed):
    original = Artifact(path="report.md", size=10, sha256="before")
    current = original.model_copy(update={"sha256": "after"}) if changed else original
    reads = iter([original, current])

    async def review(value, contents):
        assert contents == "actual report excerpt"
        return VerificationResult(
            passed=True, feedback="Criteria met", input_tokens=4, output_tokens=2
        )

    activities = Activities(
        SimpleNamespace(is_cancel_requested=lambda goal_id: False),
        SimpleNamespace(
            verify_files=lambda run: VerificationResult(
                passed=True, feedback="File exists", artifacts=[next(reads)]
            ),
            review_text=lambda run: "actual report excerpt",
        ),
        SimpleNamespace(review=review),
    )
    context = request()
    result = await activities.verify(VerifyInput(run=context.run, state=context.state))
    assert result.passed is (not changed)
    assert result.artifacts == [current]
    assert (result.input_tokens, result.output_tokens) == (4, 2)
    if changed:
        assert "changed during verification" in result.feedback
