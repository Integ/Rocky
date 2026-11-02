import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from rocky.activities import Activities
from rocky.api import create_app
from rocky.config import Settings
from rocky.deliveries import DeliveryRow, DeliveryStore
from rocky.models import (
    Artifact,
    ExecutionStep,
    GoalContract,
    RunInput,
    RunState,
    VerificationResult,
    VerifyInput,
)
from rocky.store import Store
from rocky.tools import ToolError, WorkspaceTools

AUTH = {"Authorization": "Bearer delivery-test"}


@pytest.fixture
def context(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/deliveries.db")
    store.initialize()
    settings = Settings(
        workspace_root=tmp_path / "work", auth_mode="token", api_token="delivery-test"
    )
    tools = WorkspaceTools(settings)
    contract = GoalContract(
        objective="Write a report",
        acceptance_criteria=["Deliver a checked report"],
        deliverables=[{"path": "report.md"}],
    )
    store.create_goal("task", contract)
    (tools.root / "task").mkdir()
    yield store, settings, tools, DeliveryStore(store)
    store.close()


def complete(context, text="first edition", revision=10, frozen=True):
    store, _, tools, _ = context
    data = text.encode()
    (tools.root / "task/report.md").write_bytes(data)
    artifact = Artifact(path="report.md", size=len(data), sha256=hashlib.sha256(data).hexdigest())
    identity = tools.freeze_artifacts("task", [artifact]) if frozen else None
    now = datetime.now(UTC).isoformat()
    state = RunState(
        goal_id="task",
        revision=revision,
        status="succeeded",
        reason=text,
        updated_at=now,
        artifacts=[artifact],
        delivery_archive=identity,
        execution_step=ExecutionStep(
            step_id=f"{revision}:complete",
            cycle=revision,
            phase="complete",
            status="completed",
            started_at=now,
            completed_at=now,
            output={"verification": {"passed": True, "feedback": "Checked against the source"}},
        ),
    )
    store.save_state(state)
    return state


def test_success_records_one_immutable_delivery_and_acceptance_updates_list_tag(context):
    store, _, _, deliveries = context
    state = complete(context)
    store.save_state(state)
    [first] = deliveries.page("task").items
    assert first.verification_feedback == "Checked against the source"
    assert first.review_status == "pending" and first.archive_id
    tag = store.list_goals_tag()
    result = deliveries.review("task", 10, "accept")
    assert result["goal"].delivery_review == "accepted"
    assert result["goal"].state.revision == 10
    assert store.list_goals_tag() != tag
    assert deliveries.review("task", 10, "accept")["delivery"] == result["delivery"]
    assert deliveries.page("task").total == 1
    with pytest.raises(ValueError, match="different"):
        deliveries.review("task", 10, "request_changes", "rewrite")


@pytest.mark.parametrize("legacy", [False, True])
def test_pending_delivery_stays_listed_after_it_falls_out_of_recent_history(context, legacy):
    store, _, _, deliveries = context
    complete(context)
    if legacy:
        with store.sessions.begin() as session:
            session.delete(session.get(DeliveryRow, ("task", 10)))
    store.create_goal("recent", store.get_goal("task").contract)
    assert {goal.goal_id for goal in store.list_goals(limit=1)} == {"task", "recent"}
    tag = store.list_goals_tag(limit=1)
    deliveries.review("task", 10, "accept")
    assert {goal.goal_id for goal in store.list_goals(limit=1)} == {"recent"}
    assert store.list_goals_tag(limit=1) != tag


def test_request_changes_is_atomic_idempotent_and_keeps_each_artifact_version(context):
    store, _, tools, deliveries = context
    first = complete(context)
    receipt = deliveries.review("task", 10, "request_changes", "Expand the explanation")
    assert receipt["goal"].state.status == "queued"
    assert receipt["delivery"].continued_revision == 11
    assert receipt["delivery"].review_status == "changes_requested"
    assert len(receipt["goal"].contract.acceptance_criteria) == 2
    assert (
        deliveries.review("task", 10, "request_changes", "Expand the explanation")[
            "goal"
        ].state.revision
        == 11
    )
    assert store.get_goal("task").state.revision == 11
    with pytest.raises(ValueError):
        deliveries.review("task", 10, "request_changes", "a different request")
    second = complete(context, "second edition", 20)
    assert (
        tools.read_frozen_artifact("task", first.delivery_archive, first.artifacts[0])
        == b"first edition"
    )
    assert (
        tools.read_frozen_artifact("task", second.delivery_archive, second.artifacts[0])
        == b"second edition"
    )
    page = deliveries.page("task", limit=1)
    assert page.total == 2 and page.items[0].revision == 20 and page.next_before_revision == 20
    assert deliveries.page("task", before_revision=20).items[0].revision == 10
    assert store.get_goal("task").delivery_review == "pending"


def test_ordinary_follow_up_preserves_pending_or_accepted_review(context):
    store, _, _, deliveries = context
    complete(context)
    deliveries.review("task", 10, "accept")
    store.follow_up_goal("task", "Add a table")
    assert deliveries.get("task", 10).review_status == "accepted"
    complete(context, "updated", 20)
    store.follow_up_goal("task", "Improve the wording")
    assert deliveries.get("task", 20).feedback == "Improve the wording"
    assert deliveries.get("task", 20).review_status == "changes_requested"


def test_old_pending_review_cannot_accept_a_newer_result(context):
    store, _, _, deliveries = context
    complete(context)
    store.follow_up_goal("task", "Revise")
    complete(context, "second", 20)
    with pytest.raises(ValueError):
        deliveries.review("task", 10, "accept")
    assert store.get_goal("task").delivery_review == "pending"


def test_failed_change_request_rolls_back_review_and_execution(context):
    store, _, _, deliveries = context
    complete(context)
    with store.sessions.begin() as session:
        from rocky.store import GoalRow

        row = session.get(GoalRow, "task")
        row.contract = {**row.contract, "acceptance_criteria": ["Check"] * 20}
    with pytest.raises(ValueError, match="no room"):
        deliveries.review("task", 10, "request_changes", "One more request")
    assert deliveries.get("task", 10).review_status == "pending"
    assert store.get_goal("task").state.status == "succeeded"


def test_concurrent_duplicate_changes_create_one_continuation(context):
    store, _, _, deliveries = context
    complete(context)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(
            pool.map(
                lambda _: deliveries.review("task", 10, "request_changes", "Rewrite the ending"),
                range(4),
            )
        )
    assert {r["goal"].state.revision for r in receipts} == {11}
    assert len(store.get_goal("task").contract.acceptance_criteria) == 2


def test_archives_reject_changed_bytes_and_symlinks(context, tmp_path):
    _, _, tools, _ = context
    state = complete(context)
    (tools.root / "task/report.md").write_text("changed")
    with pytest.raises(ToolError, match="changed"):
        tools.freeze_artifacts("task", state.artifacts)
    frozen = (
        tools.root / ".rocky/task/deliveries" / state.delivery_archive / state.artifacts[0].sha256
    )
    secret = tmp_path / "outside.txt"
    secret.write_text("outside")
    frozen.unlink()
    frozen.symlink_to(secret)
    with pytest.raises((OSError, ToolError)):
        tools.read_frozen_artifact("task", state.delivery_archive, state.artifacts[0])
    assert secret.read_text() == "outside"


async def test_verification_freezes_exact_bytes_and_preserves_usage_on_archive_failure(
    context, monkeypatch
):
    store, _, tools, _ = context
    contract = store.get_goal("task").contract
    (tools.root / "task/report.md").write_text("verified report")
    planner = SimpleNamespace(
        review=AsyncMock(
            return_value=VerificationResult(passed=True, feedback="checked", input_tokens=123)
        )
    )
    monkeypatch.setattr("rocky.activities.audit_review", lambda *args, **kwargs: ([], []))
    request = VerifyInput(
        run=RunInput(goal_id="task", contract=contract), state=RunState(goal_id="task")
    )
    activities = Activities(store, tools, planner)
    result = await activities._verify(request)
    assert result.passed and result.delivery_archive
    assert (
        tools.read_frozen_artifact("task", result.delivery_archive, result.artifacts[0])
        == b"verified report"
    )

    def changed(*args):
        raise ToolError("Artifact changed")

    monkeypatch.setattr(tools, "freeze_artifacts", changed)
    result = await activities._verify(request)
    assert not result.passed and result.input_tokens == 123


def test_api_reviews_are_authenticated_revision_bound_and_snapshots_survive_workspace_edits(
    context,
):
    store, settings, tools, _ = context
    first = complete(context)
    with TestClient(create_app(settings, store)) as client:
        endpoint = "/goals/task/deliveries/10/review"
        assert client.get("/goals/task/deliveries").status_code == 401
        assert client.post(endpoint, json={"action": "accept"}).status_code == 401
        tag = client.get("/goals", headers=AUTH).headers["etag"]
        accepted = client.post(endpoint, json={"action": "accept"}, headers=AUTH)
        assert accepted.status_code == 200
        assert client.get("/goals", headers=AUTH | {"If-None-Match": tag}).status_code == 200
        (tools.root / "task/report.md").write_text("external edit")
        assert (
            client.get("/goals/task/artifacts/content?path=report.md", headers=AUTH).json()[
                "content"
            ]
            == "first edition"
        )
        path = "/goals/task/deliveries/10/artifacts/download?path=report.md"
        assert client.get(path, headers=AUTH).content == b"first edition"
        assert (
            client.get(
                "/goals/task/deliveries/10/artifacts/content?path=../secret", headers=AUTH
            ).status_code
            == 404
        )
        assert client.get("/goals/missing/deliveries", headers=AUTH).status_code == 404
        frozen = (
            tools.root
            / ".rocky/task/deliveries"
            / first.delivery_archive
            / first.artifacts[0].sha256
        )
        frozen.write_text("corrupt")
        assert client.get(path, headers=AUTH).status_code == 410


def test_legacy_completion_does_not_invent_archives_and_can_capture_unchanged_files_on_review(
    context,
):
    store, settings, _, _ = context
    complete(context, frozen=False)
    with store.sessions.begin() as session:
        session.delete(session.get(DeliveryRow, ("task", 10)))
    with TestClient(create_app(settings, store)) as client:
        page = client.get("/goals/task/deliveries", headers=AUTH).json()
        assert page["items"][0]["archive_id"] is None
        result = client.post(
            "/goals/task/deliveries/10/review", json={"action": "accept"}, headers=AUTH
        )
        assert result.status_code == 200 and result.json()["delivery"]["archive_id"]


def test_unavailable_legacy_files_block_acceptance_but_allow_explicit_changes(context):
    store, settings, tools, deliveries = context
    complete(context, frozen=False)
    (tools.root / "task/report.md").write_text("different bytes")
    with TestClient(create_app(settings, store)) as client:
        endpoint = "/goals/task/deliveries/10/review"
        assert client.post(endpoint, json={"action": "accept"}, headers=AUTH).status_code == 410

        assert (
            client.post(
                endpoint,
                json={"action": "request_changes", "feedback": "Update the report"},
                headers=AUTH,
            ).status_code
            == 200
        )
        assert (
            client.post(
                endpoint,
                json={"action": "request_changes", "feedback": "Update the report"},
                headers=AUTH,
            ).status_code
            == 200
        )
    assert deliveries.get("task", 10).archive_id is None
    assert store.get_goal("task").state.status == "queued"


def test_deleting_goal_removes_delivery_records_and_archive_files(context):
    store, settings, tools, _ = context
    complete(context)
    with TestClient(create_app(settings, store)) as client:
        assert client.delete("/goals/task", headers=AUTH).status_code == 200
        assert client.get("/goals/task/deliveries", headers=AUTH).status_code == 404
    with store.sessions() as session:
        assert list(session.scalars(select(DeliveryRow))) == []
    assert not (tools.root / ".rocky/task").exists()


def test_corrupt_pending_archive_cannot_be_accepted(context):
    store, settings, tools, _ = context
    state = complete(context)
    frozen = (
        tools.root / ".rocky/task/deliveries" / state.delivery_archive / state.artifacts[0].sha256
    )
    frozen.write_text("changed archive")
    with TestClient(create_app(settings, store)) as client:
        response = client.post(
            "/goals/task/deliveries/10/review", json={"action": "accept"}, headers=AUTH
        )
        assert response.status_code == 410
    assert store.get_goal("task").delivery_review == "pending"
