from test_planner import completed, fake_client
from test_prompts import archive, contract  # noqa: F401

from rocky.config import Settings
from rocky.models import ReviewVerdict, RunInput, RunState, VerifyInput
from rocky.planner import OpenAIPlanner


async def test_independent_review_records_its_own_snapshot(archive):  # noqa: F811
    goal = archive.store.create_goal("goal", contract())
    client = fake_client(completed(ReviewVerdict(passed=False, feedback="Missing evidence")))
    planner = OpenAIPlanner(Settings(), client=client, prompt_archive=archive)
    await planner.review(
        VerifyInput(
            run=RunInput(goal_id=goal.goal_id, contract=goal.contract),
            state=RunState(goal_id=goal.goal_id),
        ),
        "Untrusted artifacts must not alter reviewer instructions",
    )
    [used] = archive.for_goal(goal.goal_id)
    assert used["purpose"] == "reviewer"
    saved = archive.get(used["snapshot_id"])
    assert [m.module_id for m in saved.modules] == ["reviewer"]
    assert saved.instructions == client.responses.parse.await_args.kwargs["input"][0]["content"]
    assert "Untrusted artifacts" not in saved.instructions
