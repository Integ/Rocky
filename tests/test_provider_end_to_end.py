"""Third-party transport through real Temporal, activities, storage and artifacts.

Only the remote HTTP model service is scripted. Credentials are invented test
values in a private temporary file; no user key or model service is contacted.
"""

import json
from uuid import uuid4

import httpx
import pytest
import test_workflow as workflow_tests
from fastapi.testclient import TestClient
from temporalio.worker import Worker

from rocky.activities import Activities
from rocky.api import create_app
from rocky.config import Settings
from rocky.credentials import CredentialStore
from rocky.models import RunState
from rocky.planner import OpenAIPlanner
from rocky.service import Dispatcher
from rocky.store import Store
from rocky.tools import WorkspaceTools
from rocky.workflow import GoalWorkflow

temporal_env = workflow_tests.temporal_env
pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]
AUTH = {"Authorization": "Bearer provider-e2e-owner"}
REPORT = (
    "# Rocky\nA durable goal survives interruption.\nConclusion: actual artifacts are checked.\n"
)


async def test_compatible_provider_survives_default_switch_and_produces_reviewed_artifact(
    temporal_env, tmp_path
):
    settings = Settings(
        auth_mode="token",
        api_token="provider-e2e-owner",
        database_url=f"sqlite:///{tmp_path / 'goals.db'}",
        workspace_root=tmp_path / "workspaces",
        task_queue="provider-end-to-end-" + uuid4().hex,
    )
    store = Store(settings.database_url)
    credentials = CredentialStore(tmp_path / "test-credentials.json")
    credentials.storage = "private-file"
    requests = []

    def model_service(request):
        payload = json.loads(request.content)
        requests.append((request, payload))
        # Both planning and the independent reviewer must use the original
        # endpoint, model and endpoint-scoped credential after defaults change.
        assert str(request.url) == "https://original-model.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer invented-original-test-key"
        assert payload["model"] == "original-vendor/model-v1"
        assert payload["response_format"] == {"type": "json_object"}
        assert "invented-original-test-key" not in request.content.decode()
        if len(requests) == 1:
            decision = {
                "kind": "act",
                "reason": "Write the required explanation",
                "action": {
                    "tool": "write_file",
                    "path": "report.md",
                    "url": None,
                    "content": REPORT,
                },
                "wait_seconds": None,
            }
        elif len(requests) == 2:
            decision = {
                "kind": "finish",
                "reason": "The actual report is ready for independent verification",
                "action": None,
                "wait_seconds": None,
            }
        else:
            assert len(requests) == 3
            assert "independent artifact reviewer" in payload["messages"][0]["content"]
            evidence = json.loads(payload["messages"][1]["content"])
            assert REPORT in evidence["artifacts_text"]
            assert "required_claims" not in evidence  # report.md is not structured data.
            decision = {
                "claims": [
                    {
                        "deliverable": "report.md",
                        "pointer": "",
                        "value": "the report explains interruption recovery",
                        "kind": "judgment",
                        "location": "deliverable:report.md",
                        "quote": "A durable goal survives interruption.",
                        "supported": True,
                    }
                ],
                "passed": True,
                "feedback": "The supplied report meets every criterion",
            }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(decision)}}
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(model_service)) as http:
            planner = OpenAIPlanner(
                settings, defer_client=True, credential_store=credentials, http_client=http
            )
            tools = WorkspaceTools(settings)
            activities = Activities(store, tools, planner)
            with TestClient(create_app(settings, store, credentials)) as api:
                original = {
                    "provider": "openai-compatible",
                    "base_url": "https://original-model.example/v1",
                    "model": "original-vendor/model-v1",
                    "api_key": "invented-original-test-key",
                }
                assert api.put("/provider-settings", json=original, headers=AUTH).status_code == 200
                submitted = api.post(
                    "/goals",
                    headers=AUTH,
                    json={
                        "objective": "Explain Rocky's durable execution in report.md",
                        "acceptance_criteria": [
                            "Explain interruption recovery and conclude with artifact verification"
                        ],
                        "deliverables": [
                            {"path": "report.md", "min_bytes": 20, "contains": ["Conclusion:"]}
                        ],
                        "max_model_calls": 8,
                        "max_steps": 5,
                    },
                )
                assert submitted.status_code == 202
                goal_id = submitted.json()["goal_id"]
                pinned = submitted.json()["model_settings"]
                assert "invented-original-test-key" not in submitted.text
                changed = original | {
                    "base_url": "https://replacement-model.example/v1",
                    "model": "replacement-model",
                    "api_key": "invented-replacement-test-key",
                }
                assert api.put("/provider-settings", json=changed, headers=AUTH).status_code == 200
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
                    await Dispatcher(temporal_env.client, store, settings).tick()
                    result = await temporal_env.client.get_workflow_handle(
                        goal_id, result_type=RunState
                    ).result()
                assert result.status == "succeeded"
                assert result.model_calls == 3
                assert (result.input_tokens, result.output_tokens) == (21, 9)
                assert len(requests) == 3
                assert store.get_goal(goal_id).model_settings.model_dump(mode="json") == pinned
                assert (tools.root / goal_id / "report.md").read_text() == REPORT
                assert store.pending_goals() == []
                assert store.events(goal_id)[-1]["state"]["status"] == "succeeded"
                preview = api.get(
                    f"/goals/{goal_id}/artifacts/content",
                    params={"path": "report.md"},
                    headers=AUTH,
                )
                assert preview.json() == {
                    "path": "report.md",
                    "content": REPORT,
                    "truncated": False,
                }
                download = api.get(
                    f"/goals/{goal_id}/artifacts/download",
                    params={"path": "report.md"},
                    headers=AUTH,
                )
                assert download.content == REPORT.encode()
                assert "attachment;" in download.headers["content-disposition"]
                assert (
                    "invented-original-test-key"
                    not in api.get(f"/goals/{goal_id}/events", headers=AUTH).text
                )
    finally:
        store.close()
