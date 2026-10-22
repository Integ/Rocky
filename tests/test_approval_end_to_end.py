"""Exact-action approval through API/outbox, real tools and durable execution."""

import asyncio
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
    PlanResult,
    RunInput,
    RunState,
    ToolInput,
    VerificationResult,
    WakeCommand,
)
from rocky.service import Dispatcher
from rocky.store import Store
from rocky.tools import WorkspaceTools
from rocky.workflow import GoalWorkflow

temporal_env = workflow_tests.temporal_env
pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]
AUTH = {"Authorization": "Bearer approval-owner-token"}


class ApprovalPlanner:
    def __init__(self, command):
        self.command = command
        self.plans = []
        self.reviews = []

    async def plan(self, request):
        self.plans.append(request)
        return PlanResult(
            decision=(
                Decision(kind="act", reason="Generate the report", action=self.command)
                if len(self.plans) == 1
                else Decision(kind="finish", reason="Check the actual report")
            ),
            input_tokens=10,
            output_tokens=5,
        )

    async def review(self, request, text, evidence=None, checklist=None, verified=None):
        self.reviews.append(text)
        assert "verified report" in text
        path = request.run.contract.deliverables[0].path
        return VerificationResult(
            passed=True,
            feedback="The actual report meets its criteria",
            claims=[
                {
                    "deliverable": path,
                    "pointer": "",
                    "value": "the report states it was verified",
                    "kind": "judgment",
                    "location": f"deliverable:{path}",
                    "quote": "verified report",
                    "supported": True,
                }
            ],
            input_tokens=8,
            output_tokens=4,
        )


async def test_approved_shell_runs_once_and_persists_actual_results(temporal_env, tmp_path):
    settings = Settings(
        auth_mode="token",
        api_token="approval-owner-token",
        database_url=f"sqlite:///{tmp_path}/approval.db",
        workspace_root=tmp_path / "workspaces",
        task_queue="approval-end-to-end-" + uuid4().hex,
        approval_mode="required",
    )
    store = Store(settings.database_url)
    tools = WorkspaceTools(settings)
    command = Action(
        tool="run_command",
        content="printf 'once\\n' >> invocations.txt; printf 'verified report\\n' > report.md; cat invocations.txt",
    )
    planner = ApprovalPlanner(command)
    activities = Activities(store, tools, planner)
    try:
        with TestClient(create_app(settings, store)) as api:
            contract = GoalContract.model_validate(
                {
                    "objective": "Generate and verify the report",
                    "acceptance_criteria": ["The report contains verified report"],
                    "deliverables": [{"path": "report.md", "contains": ["verified report"]}],
                }
            )
            submitted = api.post("/goals", headers=AUTH, json=contract.model_dump(mode="json"))
            assert submitted.status_code == 202
            goal_id = submitted.json()["goal_id"]
            async with Worker(
                temporal_env.client,
                task_queue=settings.task_queue,
                workflows=[GoalWorkflow],
                activities=[
                    activities.plan,
                    activities.execute,
                    activities.verify,
                    activities.persist,
                    activities.phase,
                ],
            ):
                dispatcher = Dispatcher(temporal_env.client, store, settings)
                await dispatcher.tick()
                handle = temporal_env.client.get_workflow_handle(goal_id, result_type=RunState)

                async def persisted_pending():
                    while True:
                        state = api.get(f"/goals/{goal_id}", headers=AUTH).json()["state"]
                        if state.get("pending_approval"):
                            return state["pending_approval"]
                        await asyncio.sleep(0.01)

                pending = await asyncio.wait_for(persisted_pending(), timeout=15)
                counter = tools.root / goal_id / "invocations.txt"
                assert not counter.exists()
                assert len(planner.plans) == 1
                assert pending["action"]["content"] == command.content
                assert (
                    api.post(
                        f"/goals/{goal_id}/approve", json={"fingerprint": pending["fingerprint"]}
                    ).status_code
                    == 401
                )
                assert (
                    api.post(
                        f"/goals/{goal_id}/approve", headers=AUTH, json={"fingerprint": "0" * 64}
                    ).status_code
                    == 409
                )
                accepted = api.post(
                    f"/goals/{goal_id}/approve",
                    headers=AUTH,
                    json={"fingerprint": pending["fingerprint"]},
                )
                assert accepted.status_code == 202
                await dispatcher.tick()
                # Duplicate delivery is safe even when the first signal was
                # applied and its acknowledgement was lost.
                await handle.signal(
                    "approval_command",
                    WakeCommand(
                        command_id=accepted.json()["command_id"], message=pending["fingerprint"]
                    ),
                )
                result = await handle.result()
            assert result.status == "succeeded"
            assert result.pending_approval is None
            assert counter.read_text() == "once\n"
            assert len(planner.plans) == 2 and len(planner.reviews) == 1
            tool_messages = [item for item in store.conversation(goal_id) if item["role"] == "tool"]
            assert len(tool_messages) == 1 and "once" in tool_messages[0]["content"]
            # A retried Activity can read the immutable receipt after restart,
            # including after its consumed approval is no longer in memory.
            retried = tools.execute(
                ToolInput(
                    run=RunInput(goal_id=goal_id, contract=contract),
                    operation_id=pending["operation_id"],
                    action=command,
                )
            )
            assert retried.success and counter.read_text() == "once\n"
            events = store.execution_events(goal_id)
            assert [item["phase"] for item in events if item["status"] == "completed"] == [
                "think",
                "act",
                "think",
                "complete",
            ]
            assert events[-1]["output"]["verification"]["passed"]
            assert all(
                item["duration_ms"] is not None for item in events if item["status"] != "started"
            )
            assert store.get_goal(goal_id).state.status == "succeeded"
            assert store.recall_memory(goal_id)[-1]["role"] == "assistant"
    finally:
        store.close()
