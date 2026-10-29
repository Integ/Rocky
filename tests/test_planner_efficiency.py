"""Response budgets, safe retries, client reuse and prompt caching."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError
from temporalio.exceptions import ApplicationError
from test_planner import completed, fake_client, goal_request
from test_providers import MemoryCredentials, request_with_settings

from rocky.activities import Activities
from rocky.config import Settings
from rocky.inference import ModelSettings
from rocky.memory import MemoryManager
from rocky.models import Decision, VerificationResult, VerifyInput
from rocky.planner import OpenAIPlanner, PlannerError, TransientModelError

BLOCKED = {
    "kind": "blocked",
    "reason": "Need evidence",
    "action": None,
    "actions": None,
    "wait_seconds": None,
}


def provider(name, base_url="https://models.example/v1", **updates):
    return ModelSettings(
        model="vendor/model-v2",
        provider=name,
        base_url=base_url,
        credential_id="test-account",
        **updates,
    )


def http_planner(respond, *, settings=None):
    credentials = MemoryCredentials()
    credentials.set("test-account", "test-provider-key")
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    planner = OpenAIPlanner(
        settings or Settings(), defer_client=True, credential_store=credentials, http_client=http
    )
    return planner, http


async def test_anthropic_system_prompt_is_cached_and_cached_tokens_are_counted():
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": json.dumps(BLOCKED)}],
                "usage": {
                    "input_tokens": 50,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 3000,
                    "output_tokens": 20,
                },
            },
        )

    planner, http = http_planner(respond)
    async with http:
        first = await planner.plan(request_with_settings(provider("anthropic")))
        await planner.plan(request_with_settings(provider("anthropic")))
    system = seen[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "Decision" in system[0]["text"] or "kind" in system[0]["text"]
    # The cacheable prefix must be byte-identical across steps.
    assert seen[0]["system"] == seen[1]["system"]
    assert seen[0]["max_tokens"] == 16_384
    assert (first.input_tokens, first.output_tokens) == (3050, 20)


async def test_official_openai_client_is_reused_and_receives_a_prompt_cache_key(monkeypatch):
    client = fake_client(completed(Decision(kind="blocked", reason="Need evidence")))
    constructor = Mock(return_value=client)
    monkeypatch.setattr("rocky.planner.AsyncOpenAI", constructor)
    credentials = MemoryCredentials()
    credentials.set("test-account", "official-key")
    planner = OpenAIPlanner(Settings(), defer_client=True, credential_store=credentials)
    official = provider("openai", "https://api.openai.com/v1")
    for _ in range(3):
        await planner.plan(request_with_settings(official))
    constructor.assert_called_once()
    assert client.responses.parse.await_count == 3
    assert client.responses.parse.await_args.kwargs["prompt_cache_key"] == "rocky-planner"

    credentials.set("test-account", "rotated-key")
    await planner.plan(request_with_settings(official))
    assert constructor.call_count == 2

    gateway = provider("openai", "https://gateway.example/v1")
    await planner.plan(request_with_settings(gateway))
    assert "prompt_cache_key" not in client.responses.parse.await_args.kwargs
    client.close = AsyncMock()
    await planner.close()
    assert client.close.await_count == 3


async def test_default_client_sends_cache_key_only_to_the_official_endpoint():
    client = fake_client(completed(Decision(kind="blocked", reason="Need evidence")))
    client.base_url = "https://api.openai.com/v1/"
    await OpenAIPlanner(Settings(), client=client).plan(goal_request())
    assert client.responses.parse.await_args.kwargs["prompt_cache_key"] == "rocky-planner"
    client.base_url = "http://localhost:4000/v1/"
    await OpenAIPlanner(Settings(), client=client).plan(goal_request())
    assert "prompt_cache_key" not in client.responses.parse.await_args.kwargs


@pytest.mark.parametrize(
    "status,headers,retry_after",
    [
        (429, {"retry-after": "7"}, 7.0),
        (429, {"retry-after-ms": "1500"}, 1.5),
        (429, {"retry-after": "9999"}, 60.0),
        (503, {}, None),
        (529, {}, None),
    ],
)
async def test_rate_limits_and_overload_are_retryable(status, headers, retry_after):
    planner, http = http_planner(
        lambda request: httpx.Response(status, headers=headers, text="secret-provider-detail")
    )
    async with http:
        with pytest.raises(TransientModelError) as error:
            await planner.plan(request_with_settings(provider("openai-compatible")))
    assert error.value.retry_after == retry_after
    assert "secret-provider-detail" not in str(error.value)
    assert str(status) in str(error.value)


@pytest.mark.parametrize(
    "failure,transient",
    [
        (httpx.ConnectError("refused"), True),
        (httpx.ConnectTimeout("connect"), True),
        (httpx.RemoteProtocolError("disconnected"), True),
        (httpx.ReadTimeout("read"), False),
    ],
)
async def test_connection_failures_retry_but_read_timeouts_do_not(failure, transient):
    def respond(request):
        raise failure

    planner, http = http_planner(respond)
    async with http:
        with pytest.raises(PlannerError) as error:
            await planner.plan(request_with_settings(provider("ollama")))
    assert isinstance(error.value, TransientModelError) is transient
    if not transient:
        assert "sent nothing for 300 seconds" in str(error.value)


async def test_client_errors_are_not_retried():
    planner, http = http_planner(lambda request: httpx.Response(400, text="bad request"))
    async with http:
        with pytest.raises(PlannerError) as error:
            await planner.plan(request_with_settings(provider("anthropic")))
    assert not isinstance(error.value, TransientModelError)


@pytest.mark.parametrize("name", ["anthropic", "openai-compatible"])
async def test_truncated_provider_output_explains_the_response_limit(name):
    def respond(request):
        if name == "anthropic":
            return httpx.Response(
                200,
                json={"stop_reason": "max_tokens", "content": [{"type": "text", "text": "{"}]},
            )
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}
        )

    planner, http = http_planner(respond)
    async with http:
        with pytest.raises(PlannerError, match="16384-token output limit") as error:
            await planner.plan(request_with_settings(provider(name)))
    assert "append_file" in str(error.value)


async def test_openai_truncation_is_reported_from_status_and_from_cut_off_json():
    response = completed(None)
    response.status = "incomplete"
    response.incomplete_details = SimpleNamespace(reason="max_output_tokens")
    with pytest.raises(PlannerError, match="reached the 16384-token output limit"):
        await OpenAIPlanner(Settings(), client=fake_client(response)).plan(goal_request())

    try:
        Decision.model_validate_json('{"kind":"act","reason":"Write the long rep')
    except ValidationError as cut_off:
        failure = cut_off
    client = fake_client(None)
    client.responses.parse.side_effect = failure
    with pytest.raises(PlannerError, match="cut off, most likely at the 16384-token"):
        await OpenAIPlanner(Settings(), client=client).plan(goal_request())


async def test_response_budget_is_configurable_and_doubles_for_high_reasoning():
    client = fake_client(completed(Decision(kind="blocked", reason="Need evidence")))
    planner = OpenAIPlanner(Settings(plan_response_tokens=12_000), client=client)
    request = goal_request()
    await planner.plan(request)
    arguments = client.responses.parse.await_args.kwargs
    assert arguments["max_output_tokens"] == 12_000
    assert (
        json.loads(arguments["input"][1]["content"])["execution"]["response_token_limit"] == 12_000
    )
    request.run.model_settings = ModelSettings(model="gpt-6-sol", reasoning_effort="high")
    await planner.plan(request)
    assert client.responses.parse.await_args.kwargs["max_output_tokens"] == 24_000


async def test_planner_sees_batching_editing_and_auto_approved_command_prefixes():
    client = fake_client(completed(Decision(kind="blocked", reason="Need evidence")))
    request = goal_request()
    request.state.approved_command_prefixes = [["uv", "run", "pytest"]]
    await OpenAIPlanner(Settings(auto_approve_commands=["git status"]), client=client).plan(request)
    instructions, data = client.responses.parse.await_args.kwargs["input"]
    for text in ("actions", "concurrently", "append_file", "edit_file", "auto_approved_commands"):
        assert text in instructions["content"]
    execution = json.loads(data["content"])["execution"]
    assert execution["auto_approved_commands"] == ["git status", "uv run pytest"]
    assert execution["max_parallel_actions"] == 8


class FailingPlanner:
    def __init__(self, error):
        self.error = error

    async def plan(self, request):
        raise self.error

    async def review(self, request, text, **evidence):
        raise self.error


def activities(error):
    store = SimpleNamespace(
        is_cancel_requested=lambda goal_id: False, approval_policy=lambda default: default
    )
    tools = SimpleNamespace(
        verify_files=lambda run: VerificationResult(passed=True, feedback="ok"),
        review_text=lambda run: "artifact",
    )
    activities = Activities(store, tools, FailingPlanner(error), memory=MemoryManager())
    # Review evidence comes from the store and workspace; none is needed here.
    activities.review_evidence = lambda run: {}
    activities.deliverable_texts = lambda run: {}
    activities.changed_texts = lambda run, evidence: {}
    return activities


async def test_only_failures_without_model_output_are_retried_by_temporal():
    request = goal_request()
    with pytest.raises(ApplicationError) as transient:
        await activities(TransientModelError("rate limited", retry_after=7)).plan(request)
    assert transient.value.type == "ModelTransientError"
    assert not transient.value.non_retryable
    assert transient.value.next_retry_delay == timedelta(seconds=7)

    for call in ("plan", "verify"):
        target = activities(PlannerError("invalid structured result"))
        argument = request if call == "plan" else VerifyInput(run=request.run, state=request.state)
        with pytest.raises(ApplicationError) as decided:
            await getattr(target, call)(argument)
        assert decided.value.type == "ModelDecisionError"
        assert decided.value.non_retryable


@pytest.mark.parametrize("provider_name", ["anthropic", "openai-compatible", "ollama"])
@pytest.mark.parametrize("failure", ["truncated", "invalid", "refused"])
async def test_unusable_http_responses_preserve_reported_usage(provider_name, failure):
    def respond(request):
        if provider_name == "anthropic":
            return httpx.Response(
                200,
                json={
                    "stop_reason": {
                        "truncated": "max_tokens",
                        "invalid": "end_turn",
                        "refused": "refusal",
                    }[failure],
                    "content": [{"type": "text", "text": "{"}],
                    "usage": {
                        "input_tokens": 20,
                        "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 30,
                        "output_tokens": 10,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length" if failure == "truncated" else "stop",
                        "message": {"content": "{", "refusal": failure == "refused"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 50},
                },
            },
        )

    planner, http = http_planner(respond)
    async with http:
        with pytest.raises(PlannerError) as error:
            await planner.plan(request_with_settings(provider(provider_name)))
    assert error.value.usage == {
        "input_tokens": 100,
        "output_tokens": 10,
        "cached_input_tokens": 50,
    }
    # Only numeric counts, never raw provider response or credentials, cross Temporal.
    with pytest.raises(ApplicationError) as activity_error:
        await activities(error.value).plan(goal_request())
    assert activity_error.value.details == (error.value.usage,)
    assert activity_error.value.non_retryable


@pytest.mark.parametrize("failure", ["truncated", "invalid", "refused", "empty"])
async def test_unusable_responses_api_results_preserve_usage(failure):
    response = completed(None, input_tokens=100, output_tokens=10)
    response.usage.input_tokens_details = SimpleNamespace(cached_tokens=50)
    if failure == "truncated":
        response.status = "incomplete"
        response.incomplete_details = SimpleNamespace(reason="max_output_tokens")
    elif failure == "invalid":
        response.output_parsed = {"kind": "unsupported"}
    elif failure == "refused":
        response.output = [SimpleNamespace(content=[SimpleNamespace(type="refusal")])]
    with pytest.raises(PlannerError) as error:
        await OpenAIPlanner(Settings(), client=fake_client(response)).plan(goal_request())
    assert error.value.usage == {
        "input_tokens": 100,
        "output_tokens": 10,
        "cached_input_tokens": 50,
    }
