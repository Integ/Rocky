"""Streamed model responses, provider-side structured output and cache-friendly envelopes."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from openai_stream import with_stream
from temporalio.testing import ActivityEnvironment
from test_planner import goal_request
from test_planner_efficiency import BLOCKED, http_planner, provider
from test_providers import request_with_settings

from rocky import activities as activities_module
from rocky import planner as planner_module
from rocky.config import Settings
from rocky.models import Decision, ReviewVerdict
from rocky.planner import OpenAIPlanner, PlannerError, TransientModelError


def sse(*events):
    """Server-sent events as providers frame them, including the named event line."""
    body = "".join(
        f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n" for event in events
    )
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def anthropic_stream(text, *, stop_reason="end_turn", cached=0):
    half = len(text) // 2
    return sse(
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 40,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": cached,
                    "output_tokens": 1,
                }
            },
        },
        # Thinking blocks carry no decision text and are ignored.
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "ping"},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": text[:half]},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": text[half:]},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": 30},
        },
        {"type": "message_stop"},
    )


def chat_stream(text, *, finish_reason="stop", usage=None):
    half = len(text) // 2
    chunks = [
        {"choices": [{"delta": {"role": "assistant", "content": text[:half]}}]},
        {"choices": [{"delta": {"content": text[half:]}, "finish_reason": finish_reason}]},
        {"choices": [], "usage": usage or {"prompt_tokens": 70, "completion_tokens": 9}},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def walk(schema):
    yield schema
    if isinstance(schema, dict):
        for value in schema.values():
            yield from walk(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from walk(value)


@pytest.mark.parametrize("schema", [Decision, ReviewVerdict])
def test_native_schema_is_closed_complete_and_free_of_unsupported_limits(schema):
    native = planner_module._native_schema(schema)
    for node in walk(native):
        if isinstance(node, dict):
            assert not set(node) & planner_module._UNSUPPORTED_SCHEMA_KEYWORDS
            if isinstance(node.get("properties"), dict):
                assert node["additionalProperties"] is False
                assert node["required"] == list(node["properties"])


def test_native_schema_keeps_properties_named_like_removed_keywords():
    strict = planner_module._strict_schema(
        {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "maxLength": {"minimum": 1}},
            "required": ["pattern"],
        }
    )
    assert strict == {
        "type": "object",
        "properties": {"pattern": {"type": "string"}, "maxLength": {}},
        "required": ["pattern", "maxLength"],
        "additionalProperties": False,
    }


async def test_anthropic_stream_is_reassembled_with_native_format_and_cached_prefix():
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return anthropic_stream(json.dumps(BLOCKED), cached=3000)

    planner, http = http_planner(respond)
    first_request = request_with_settings(provider("anthropic"))
    second_request = first_request.model_copy(deep=True)
    second_request.state.steps = 7
    second_request.feedback = "A newer observation."
    async with http:
        first = await planner.plan(first_request)
        await planner.plan(second_request)

    assert first.decision.kind == "blocked"
    assert (first.input_tokens, first.output_tokens, first.cached_input_tokens) == (3040, 30, 3000)
    body = seen[0]
    assert body["stream"] is True
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"] == planner_module._native_schema(Decision)
    stable, changing = body["messages"][0]["content"]
    assert stable["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in changing
    assert set(json.loads(stable["text"])) == {"run", "execution"}
    assert {"state", "memory", "feedback", "budget_remaining"} <= set(json.loads(changing["text"]))
    # A later step reuses the byte-identical stable part; only the tail differs.
    later = seen[1]["messages"][0]["content"]
    assert later[0] == stable
    assert later[1] != changing


async def test_anthropic_stream_stop_reasons_and_errors_never_accept_a_decision():
    responses = iter(
        [
            anthropic_stream('{"kind":"blocked","reason":"cut', stop_reason="max_tokens"),
            anthropic_stream(json.dumps(BLOCKED), stop_reason="refusal"),
            sse(
                {"type": "message_start", "message": {"usage": {}}},
                {
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "busy test-secret"},
                },
            ),
            sse(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": "{"},
                },
                {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
            ),
        ]
    )
    planner, http = http_planner(lambda request: next(responses))
    async with http:
        with pytest.raises(PlannerError, match="16384-token output limit"):
            await planner.plan(request_with_settings(provider("anthropic")))
        with pytest.raises(PlannerError, match="refused"):
            await planner.plan(request_with_settings(provider("anthropic")))
        # Overload before any output may be retried; the text is never echoed.
        with pytest.raises(TransientModelError) as early:
            await planner.plan(request_with_settings(provider("anthropic")))
        assert "test-secret" not in str(early.value)
        # Once output was produced the call was billed: it must not repeat.
        with pytest.raises(PlannerError) as late:
            await planner.plan(request_with_settings(provider("anthropic")))
        assert not isinstance(late.value, TransientModelError)


async def test_compatible_stream_uses_json_schema_and_reports_cache_hits():
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return chat_stream(
            json.dumps(BLOCKED),
            usage={
                "prompt_tokens": 70,
                "completion_tokens": 9,
                "prompt_tokens_details": {"cached_tokens": 64},
            },
        )

    planner, http = http_planner(respond)
    async with http:
        result = await planner.plan(request_with_settings(provider("openai-compatible")))
    assert (result.input_tokens, result.output_tokens, result.cached_input_tokens) == (70, 9, 64)
    body = seen[0]
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == planner_module._native_schema(
        Decision
    )
    # The schema stays in the instructions too: it states the stripped limits.
    assert '"maxLength":2000' in body["messages"][0]["content"]
    assert set(json.loads(body["messages"][1]["content"])) >= {"run", "execution", "state"}


async def test_deepseek_cache_hits_and_length_stops_are_reported():
    responses = iter(
        [
            chat_stream(
                json.dumps(BLOCKED),
                usage={"prompt_tokens": 50, "completion_tokens": 5, "prompt_cache_hit_tokens": 32},
            ),
            chat_stream('{"kind":"bloc', finish_reason="length"),
        ]
    )
    planner, http = http_planner(lambda request: next(responses))
    async with http:
        result = await planner.plan(request_with_settings(provider("openai-compatible")))
        assert result.cached_input_tokens == 32
        with pytest.raises(PlannerError, match="output limit"):
            await planner.plan(request_with_settings(provider("openai-compatible")))


@pytest.mark.parametrize("name", ["anthropic", "openai-compatible"])
async def test_rejected_native_format_falls_back_once_and_is_remembered(name):
    formats = []

    def respond(request):
        body = json.loads(request.content)
        native = "output_config" in body or body.get("response_format", {}).get("type") == (
            "json_schema"
        )
        formats.append(native)
        if native:
            return httpx.Response(400, json={"error": {"message": "unsupported test-secret"}})
        if name == "anthropic":
            return anthropic_stream(json.dumps(BLOCKED))
        assert body["response_format"] == {"type": "json_object"}
        return chat_stream(json.dumps(BLOCKED))

    planner, http = http_planner(respond)
    async with http:
        first = await planner.plan(request_with_settings(provider(name)))
        await planner.plan(request_with_settings(provider(name)))
    assert first.decision.kind == "blocked"
    assert formats == [True, False, False]


async def test_a_request_rejected_for_another_reason_keeps_native_format():
    formats = []

    def respond(request):
        body = json.loads(request.content)
        formats.append(body["response_format"]["type"])
        if len(formats) <= 2:
            return httpx.Response(400, json={"error": {"message": "context too long"}})
        return chat_stream(json.dumps(BLOCKED))

    planner, http = http_planner(respond)
    async with http:
        with pytest.raises(PlannerError):
            await planner.plan(request_with_settings(provider("openai-compatible")))
        await planner.plan(request_with_settings(provider("openai-compatible")))
    assert formats == ["json_schema", "json_object", "json_schema"]


async def test_a_steady_stream_is_bounded_by_the_total_response_limit(monkeypatch):
    monkeypatch.setattr(planner_module, "MODEL_RESPONSE_SECONDS", 0.2)

    async def trickle():
        yield b'data: {"choices":[{"delta":{"content":"{"}}]}\n\n'
        while True:
            await asyncio.sleep(0.02)
            yield b": keep-alive\n\n"

    planner, http = http_planner(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=trickle()
        )
    )
    async with http:
        with pytest.raises(PlannerError, match="did not finish within") as error:
            await planner.plan(request_with_settings(provider("openai-compatible")))
    assert not isinstance(error.value, TransientModelError)


async def test_oversized_stream_is_rejected(monkeypatch):
    monkeypatch.setattr(planner_module, "_MAX_RESPONSE_BYTES", 2_000)
    text = json.dumps({**BLOCKED, "reason": "x" * 5_000})
    planner, http = http_planner(lambda request: chat_stream(text))
    async with http:
        with pytest.raises(PlannerError, match="exceeded the accepted size"):
            await planner.plan(request_with_settings(provider("openai-compatible")))


async def test_openai_stream_reports_cached_tokens_and_incomplete_responses():
    decision = Decision(kind="blocked", reason="Need evidence")
    completed = SimpleNamespace(
        status="completed",
        output_parsed=decision,
        output=[],
        usage=SimpleNamespace(
            input_tokens=900,
            output_tokens=20,
            input_tokens_details=SimpleNamespace(cached_tokens=768),
        ),
    )
    incomplete = SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output=[],
        output_parsed=None,
    )

    async def parse(**_arguments):
        return responses.pop(0)

    responses = [completed, incomplete]
    client = SimpleNamespace(responses=with_stream(SimpleNamespace(parse=parse)))
    planner = OpenAIPlanner(Settings(), client=client)
    result = await planner.plan(goal_request())
    assert (result.input_tokens, result.cached_input_tokens) == (900, 768)
    with pytest.raises(PlannerError, match="output limit"):
        await planner.plan(goal_request())


async def test_model_activities_heartbeat_while_waiting(monkeypatch):
    monkeypatch.setattr(activities_module, "_HEARTBEAT_SECONDS", 0.01)
    beats = []
    environment = ActivityEnvironment()
    environment.on_heartbeat = lambda *details: beats.append(details)

    async def slow_model_call():
        await asyncio.sleep(0.05)
        return "decision"

    async def activity_body():
        return await activities_module._with_heartbeat(slow_model_call())

    assert await environment.run(activity_body) == "decision"
    assert len(beats) >= 2
    # Outside an Activity, as in direct unit use, nothing is reported.
    assert await activities_module._with_heartbeat(slow_model_call()) == "decision"
