"""API-to-artifact tests using real activities, storage, tools and Temporal.

Only the remote model transport is replaced. No API key or model service is used.
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_workflow as workflow_tests
from fastapi.testclient import TestClient
from temporalio.worker import Worker

from rocky.activities import Activities
from rocky.api import create_app
from rocky.config import Settings
from rocky.models import (
    Action,
    Decision,
    GoalContract,
    ReviewVerdict,
    RunInput,
    RunState,
)
from rocky.planner import OpenAIPlanner
from rocky.service import Dispatcher
from rocky.store import Store
from rocky.tools import WorkspaceTools
from rocky.workflow import GoalWorkflow

temporal_env = workflow_tests.temporal_env

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]
AUTH = {"Authorization": "Bearer test-owner-token"}
REPORT = "Rocky executes durable goals.\nConclusion: execution is checked.\n"


class ScriptedResponses:
    def __init__(self, decisions: list[Decision]):
        self.decisions = list(decisions)
        self.requests: list[dict] = []
        self.review_requests: list[dict] = []

    async def parse(self, **kwargs):
        self.requests.append(kwargs)
        if kwargs["text_format"] is Decision:
            parsed = self.decisions.pop(0)
        else:
            assert kwargs["text_format"] is ReviewVerdict
            self.review_requests.append(kwargs)
            assert "Conclusion: execution is checked." in kwargs["input"][1]["content"]
            parsed = ReviewVerdict(passed=True, feedback="The actual report meets the criteria")
        return SimpleNamespace(
            status="completed",
            output=[],
            output_parsed=parsed,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


def decisions() -> list[Decision]:
    return [
        Decision(
            kind="act",
            reason="Write the required report",
            action=Action(tool="write_file", path="report.md", content=REPORT),
        ),
        Decision(kind="finish", reason="The report is ready for independent verification"),
    ]


def contract() -> dict:
    return {
        "objective": "Write a short explanation of Rocky's durable execution",
        "acceptance_criteria": ["The report explains durable goals and includes a conclusion"],
        "deliverables": [{"path": "report.md", "min_bytes": 20, "contains": ["Conclusion:"]}],
        "max_model_calls": 8,
        "max_steps": 5,
        "max_no_progress": 2,
    }


@pytest.fixture
def components(tmp_path):
    settings = Settings(
        auth_mode="token",
        database_url=f"sqlite:///{tmp_path}/goals.db",
        workspace_root=tmp_path / "workspaces",
        api_token="test-owner-token",
        task_queue="end-to-end-" + uuid4().hex,
    )
    store = Store(settings.database_url)
    tools = WorkspaceTools(settings)
    responses = ScriptedResponses(decisions())
    planner = OpenAIPlanner(settings, client=SimpleNamespace(responses=responses))
    activities = Activities(store, tools, planner)
    try:
        yield settings, store, tools, responses, activities
    finally:
        store.close()


def actual_worker(env, settings, activities):
    return Worker(
        env.client,
        task_queue=settings.task_queue,
        workflows=[GoalWorkflow],
        activities=[activities.plan, activities.execute, activities.verify, activities.persist],
    )


async def test_api_submission_runs_actual_tools_and_independent_review(temporal_env, components):
    settings, store, tools, responses, activities = components
    with TestClient(create_app(settings, store)) as api:
        pinned = {"model": "gpt-6-luna", "reasoning_effort": "low"}
        assert api.put("/model-settings", headers=AUTH, json=pinned).status_code == 200
        submitted = api.post("/goals", headers=AUTH, json=contract())
        assert submitted.status_code == 202
        goal_id = submitted.json()["goal_id"]
        assert submitted.json()["state"]["status"] == "queued"
        assert submitted.json()["model_settings"] == pinned
        # Change defaults before dispatch: all inference, including independent
        # review, must still use the configuration saved with this goal.
        changed = {"model": "gpt-6-astra", "reasoning_effort": "max"}
        assert api.put("/model-settings", headers=AUTH, json=changed).status_code == 200
        async with actual_worker(temporal_env, settings, activities):
            await Dispatcher(temporal_env.client, store, settings).tick()
            result = await temporal_env.client.get_workflow_handle(
                goal_id, result_type=RunState
            ).result()
        assert result.status == "succeeded"
        assert result.model_calls == 3
        assert (result.input_tokens, result.output_tokens) == (30, 15)
        assert len(responses.requests) == 3
        assert len(responses.review_requests) == 1
        assert all(request["model"] == pinned["model"] for request in responses.requests)
        assert all(request["reasoning"] == {"effort": "low"} for request in responses.requests)
        assert (tools.root / goal_id / "report.md").read_text() == REPORT
        run = RunInput(goal_id=goal_id, contract=GoalContract.model_validate(contract()))
        independently_checked = tools.verify_files(run)
        assert independently_checked.passed
        assert result.artifacts == independently_checked.artifacts
        status = api.get(f"/goals/{goal_id}", headers=AUTH)
        assert status.json()["state"]["status"] == "succeeded"
        assert store.pending_goals() == []
        events = api.get(f"/goals/{goal_id}/events", headers=AUTH).json()
        assert events[-1]["state"]["status"] == "succeeded"
        assert any(event["state"]["model_calls"] == 1 for event in events)


async def test_cancel_before_dispatch_never_calls_model_or_tools(
    temporal_env, components, monkeypatch
):
    settings, store, tools, responses, activities = components
    tool_calls = []

    def unexpected_tool(request):
        tool_calls.append(request)
        raise AssertionError("Cancelled goal must never invoke a tool")

    monkeypatch.setattr(tools, "execute", unexpected_tool)
    with TestClient(create_app(settings, store)) as api:
        submitted = api.post("/goals", headers=AUTH, json=contract())
        goal_id = submitted.json()["goal_id"]
        assert api.post(f"/goals/{goal_id}/cancel", headers=AUTH).status_code == 202
        assert store.is_cancel_requested(goal_id)
        async with actual_worker(temporal_env, settings, activities):
            await Dispatcher(temporal_env.client, store, settings).tick()
            result = await temporal_env.client.get_workflow_handle(
                goal_id, result_type=RunState
            ).result()
        assert result.status == "cancelled"
        assert responses.requests == []
        assert tool_calls == []
        assert not (tools.root / goal_id / "report.md").exists()
        assert store.get_goal(goal_id).state.status == "cancelled"


async def test_lost_dispatch_ack_does_not_rerun_completed_goal(
    temporal_env, components, monkeypatch
):
    settings, store, tools, responses, activities = components
    with TestClient(create_app(settings, store)) as api:
        goal_id = api.post("/goals", headers=AUTH, json=contract()).json()["goal_id"]
        dispatcher = Dispatcher(temporal_env.client, store, settings)
        mark_dispatched = store.mark_dispatched

        def lose_ack(_goal_id):
            raise OSError("Simulated dispatcher crash after a successful workflow start")

        monkeypatch.setattr(store, "mark_dispatched", lose_ack)
        async with actual_worker(temporal_env, settings, activities):
            await dispatcher.tick()
            handle = temporal_env.client.get_workflow_handle(goal_id, result_type=RunState)
            result = await handle.result()
            assert result.status == "succeeded"
            first_execution = await handle.describe()
            assert len(store.pending_goals()) == 1
            calls_before = len(responses.requests)
            events_before = len(store.events(goal_id))
            report = tools.root / goal_id / "report.md"
            # A user's later edit must survive reconciliation of the stale outbox.
            report.write_text("User edited the completed deliverable")
            monkeypatch.setattr(store, "mark_dispatched", mark_dispatched)
            await dispatcher.tick()
            after_reconciliation = await handle.describe()
        assert store.pending_goals() == []
        assert after_reconciliation.run_id == first_execution.run_id
        assert len(responses.requests) == calls_before == 3
        assert len(store.events(goal_id)) == events_before
        assert report.read_text() == "User edited the completed deliverable"


async def test_actual_file_verification_rejects_unsupported_finish_claim(temporal_env, components):
    settings, store, tools, responses, activities = components
    responses.decisions = [
        Decision(kind="finish", reason="A planner claim without a real file"),
        Decision(kind="finish", reason="The same unsupported claim"),
    ]
    with TestClient(create_app(settings, store)) as api:
        goal_id = api.post("/goals", headers=AUTH, json=contract()).json()["goal_id"]
        async with actual_worker(temporal_env, settings, activities):
            await Dispatcher(temporal_env.client, store, settings).tick()

            async def wait_for_blocked():
                while store.get_goal(goal_id).state.status != "blocked":
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_blocked(), timeout=15)
            assert len(responses.requests) == 2
            assert responses.review_requests == []
            assert not (tools.root / goal_id / "report.md").exists()
            assert store.get_goal(goal_id).state.status == "blocked"
            assert api.post(f"/goals/{goal_id}/cancel", headers=AUTH).status_code == 202
            await Dispatcher(temporal_env.client, store, settings).tick()
            result = await temporal_env.client.get_workflow_handle(
                goal_id, result_type=RunState
            ).result()
        assert result.status == "cancelled"
        assert all(event["state"]["status"] != "succeeded" for event in store.events(goal_id))
