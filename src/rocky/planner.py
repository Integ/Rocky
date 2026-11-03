"""Bounded model planning and independent artifact review.

Only this adapter talks to the model. Durable execution, tool permissions and
completion checks belong to the workflow and tool layer, outside model control.
"""

import asyncio
import copy
import email.utils
import hashlib
import json
import logging
import os
import shlex
import sys
import time
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import httpx
import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from rocky.agenda import PlanDecomposition, PlanRevision
from rocky.config import Settings
from rocky.credentials import CredentialStore
from rocky.inference import ModelSettings
from rocky.memory import MemoryManager, bounded_memory, conservative_token_count, serialize_memory
from rocky.model_queue import ModelQueue, ModelRequestCancelled
from rocky.models import (
    FILE_BATCH_TOOLS,
    MAX_PARALLEL_ACTIONS,
    Decision,
    DeliveryRequirements,
    DraftedContract,
    ExecutionLimits,
    GoalDraft,
    PlanInput,
    PlanResult,
    ReviewVerdict,
    VerificationResult,
    VerifyInput,
)
from rocky.prompt_archive import PromptArchive
from rocky.prompts import PromptSnapshot, Purpose, compose, guidance_for
from rocky.providers import credential_id, normalize_base_url

_MAX_CONTEXT_CHARS = 100_000
# Conservative UTF-8 token upper bounds, including each layer's JSON labels.
# Essential task/state is kept whole; optional text is clipped before dispatch.
_TASK_TOKEN_BUDGET = 60_000
_STATE_TOKEN_BUDGET = 32_000
_FEEDBACK_TOKEN_BUDGET = 4_000
_MAX_CONTEXT_TOKENS = 100_000
# Provider requests remain finite so a single response can complete reliably.
# These are response controls, not cumulative task execution budgets. Reasoning
# tokens count toward them, so higher reasoning efforts receive twice the cap.
_PLAN_RESPONSE_TOKENS = 16_384
# One cited claim per checked value needs room for up to 90 compact claims.
_REVIEW_RESPONSE_TOKENS = 12_000
_HIGH_REASONING = frozenset({"high", "xhigh", "max"})
log = logging.getLogger(__name__)

# Responses stream, so this bounds silence between received bytes (long
# reasoning may send nothing for minutes), not the length of the answer.
MODEL_TIMEOUT_SECONDS = 300.0
# A streamed answer that keeps arriving may take this long in total. The
# workflow's per-attempt Activity timeout must stay above this value.
MODEL_RESPONSE_SECONDS = 900.0
_MODEL_TIMEOUT = httpx.Timeout(MODEL_TIMEOUT_SECONDS, connect=15.0)
_MAX_RESPONSE_BYTES = 2_000_000
# Constrained decoding rejects these keywords. Pydantic still enforces them
# after parsing, and the schema text in the instructions still states them.
_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)
_CACHE_BREAKPOINT = {"type": "ephemeral"}
_OPENAI_BASE_URL = "https://api.openai.com/v1"
_MAX_RETRY_AFTER_SECONDS = 60.0
_MAX_REVIEW_ARTIFACT_CHARS = 60_000
_REVIEW_ARTIFACT_TOKEN_BUDGET = 36_000
# Application-collected evidence takes what the contract and artifacts leave.
_REVIEW_EVIDENCE_TOKEN_BUDGET = 60_000
_TRUNCATED = "\n[CONTENT TRUNCATED; omitted content is not evidence]"


MODEL_ERROR_CODES = frozenset(
    {
        "authentication_failed",
        "model_access_denied",
        "quota_exceeded",
        "rate_limited",
        "provider_unavailable",
        "network_error",
        "model_timeout",
        "missing_api_key",
    }
)


class PlannerError(RuntimeError):
    """A safe error suitable for persisted workflow status and user display."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code if isinstance(code, str) and code in MODEL_ERROR_CODES else None
        self.usage: dict[str, int] = {}

    def with_usage(self, usage: dict[str, int]) -> "PlannerError":
        self.usage = usage
        return self


def _known_usage(usage: Any, provider: str = "responses") -> dict[str, int]:
    """Keep only reported numeric counts, including for unusable responses.

    No estimate is invented when a connection/SDK failure exposes no usage.
    """

    def get(name, default=0):
        return (
            usage.get(name, default) if isinstance(usage, dict) else getattr(usage, name, default)
        )

    try:
        if provider == "anthropic":
            cached = int(get("cache_read_input_tokens") or 0)
            incoming = (
                cached
                + int(get("input_tokens") or 0)
                + int(get("cache_creation_input_tokens") or 0)
            )
            outgoing = int(get("output_tokens") or 0)
        elif provider == "responses":
            incoming, outgoing = int(get("input_tokens") or 0), int(get("output_tokens") or 0)
            cached = _cached_tokens(get("input_tokens_details", None))
        else:
            incoming, outgoing = int(get("prompt_tokens") or 0), int(get("completion_tokens") or 0)
            cached = _cached_tokens(get("prompt_tokens_details", None)) or int(
                get("prompt_cache_hit_tokens") or 0
            )
        return {
            "input_tokens": max(0, incoming),
            "output_tokens": max(0, outgoing),
            "cached_input_tokens": min(max(0, cached), max(0, incoming)),
        }
    except (TypeError, ValueError, OverflowError):
        return {}


class TransientModelError(PlannerError):
    """The provider rejected or dropped the request before producing a response.

    Rate limits, overload responses and failed connections consume no output,
    so the Activity may safely be retried, after retry_after when supplied.
    """

    def __init__(
        self, message: str, retry_after: float | None = None, *, code: str | None = None
    ) -> None:
        super().__init__(message, code=code)
        self.retry_after = retry_after


def _truncated_output(limit: int, *, likely: bool = False) -> PlannerError:
    cause = "its structured output was cut off, most likely at" if likely else "it reached"
    return PlannerError(
        f"Model response did not complete: {cause} the {limit}-token output limit, "
        "which includes reasoning. No decision was accepted. Keep reasoning brief and "
        "action content shorter: write long files in parts with append_file and change "
        "existing files with edit_file instead of rewriting them."
    )


def _retry_after(headers: Any) -> float | None:
    if headers is None:
        return None
    try:
        if (milliseconds := headers.get("retry-after-ms")) is not None:
            seconds = float(milliseconds) / 1000
        elif (value := headers.get("retry-after")) is None:
            return None
        elif value.strip().replace(".", "", 1).isdigit():
            seconds = float(value)
        else:
            seconds = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
    except (TypeError, ValueError, OverflowError):
        return None
    return min(max(0.0, seconds), _MAX_RETRY_AFTER_SECONDS)


def _provider_failure(error: Exception) -> PlannerError | None:
    """Expose fixed categories, never upstream messages, URLs or credentials."""
    if isinstance(error, openai.APIStatusError | httpx.HTTPStatusError):
        response = error.response
        status = response.status_code
    else:
        return None
    if status == 401:
        return PlannerError(
            "The model provider rejected the API key; update it in Model Settings.",
            code="authentication_failed",
        )
    if status in {403, 404}:
        return PlannerError(
            "The model or endpoint is unavailable to this credential; check Model Settings.",
            code="model_access_denied",
        )
    if status == 429:
        code = response.extensions.get("rocky_error_code")
        try:
            body = response.json()
            detail = body.get("error", {}) if isinstance(body, dict) else {}
            if isinstance(detail, dict):
                code = detail.get("code") or detail.get("type") or code
        except (ValueError, httpx.ResponseNotRead):
            pass
        if isinstance(code, str) and code in {"insufficient_quota", "billing_hard_limit_reached"}:
            return PlannerError(
                "The model provider's quota or billing limit is exhausted; check the account limits.",
                code="quota_exceeded",
            )
    return None


def _transient_failure(error: Exception) -> TransientModelError | None:
    """Classify provider failures that are safe to retry; never echo their text."""
    if isinstance(error, openai.APITimeoutError | httpx.TimeoutException) and not isinstance(
        error, httpx.ConnectTimeout | httpx.PoolTimeout
    ):
        # The provider may have processed and billed a request that timed out.
        return None
    if isinstance(
        error,
        openai.APIConnectionError
        | httpx.NetworkError
        | httpx.RemoteProtocolError
        | httpx.ConnectTimeout
        | httpx.PoolTimeout,
    ):
        return TransientModelError(
            "The model provider could not be reached. Automatic retries did not succeed; "
            "check the network connection and provider endpoint.",
            code="network_error",
        )
    if isinstance(error, openai.APIStatusError):
        status, headers = error.status_code, error.response.headers
    elif isinstance(error, httpx.HTTPStatusError):
        status, headers = error.response.status_code, error.response.headers
    else:
        return None
    if status == 429:
        return TransientModelError(
            "The model provider rate-limited the request (HTTP 429). Automatic retries did "
            "not succeed; check the account's rate limits or try again later.",
            _retry_after(headers),
            code="rate_limited",
        )
    if status in {408, 409} or status >= 500:
        return TransientModelError(
            f"The model provider was temporarily unavailable (HTTP {status}). Automatic "
            "retries did not succeed; try again later.",
            _retry_after(headers),
            code="provider_unavailable",
        )
    return None


def _bounded_text(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    return value[: maximum - len(_TRUNCATED)] + _TRUNCATED


def _json_context(payload: dict[str, Any]) -> str:
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(content) > _MAX_CONTEXT_CHARS or conservative_token_count(content) > _MAX_CONTEXT_TOKENS:
        raise PlannerError("Goal context is too large for a bounded model request.")
    return content


def _context_layer(value: Any, budget: int, name: str) -> Any:
    if conservative_token_count(serialize_memory(value)) > budget:
        raise PlannerError(
            f"Goal context is too large in {name}; reduce its size before retrying. "
            f"The layer has a {budget}-token conservative serialized budget."
        )
    return value


def _bounded_serialized_text(value: str, budget: int) -> str:
    if conservative_token_count(serialize_memory(value)) <= budget:
        return value
    # Count the JSON representation too: escaping and multibyte text cost space.
    low, high, best = 0, len(value), ""
    while low <= high:
        midpoint = (low + high) // 2
        candidate = value[:midpoint] + _TRUNCATED
        if conservative_token_count(serialize_memory(candidate)) <= budget:
            best, low = candidate, midpoint + 1
        else:
            high = midpoint - 1
    return best


def _fair_shares(sizes: list[int], total: int) -> list[int]:
    """Max-min fair allocation: small items keep everything, large ones share the rest."""
    shares = [0] * len(sizes)
    left = max(0, total)
    order = sorted(range(len(sizes)), key=sizes.__getitem__)
    for rank, index in enumerate(order):
        shares[index] = min(sizes[index], left // (len(order) - rank))
        left -= shares[index]
    return shares


def _fit_evidence(evidence: dict[str, Any], budget: int) -> dict[str, Any]:
    """Fit review evidence into a serialized budget without dropping file metadata.

    Action outputs are shortened, then the oldest actions dropped, within a
    quarter of the budget. File text then gets max-min fair shares of the rest.
    """
    evidence = copy.deepcopy(evidence)

    def size(value: Any) -> int:
        return conservative_token_count(serialize_memory(value))

    # Keep whole, recent owner replies with their question binding. Never
    # turn a partial reply into apparent evidence of what the owner said.
    messages = evidence.get("owner_messages", [])
    while messages and size(messages) > budget // 4:
        messages.pop(0)
        evidence["owner_messages_omitted"] = evidence.get("owner_messages_omitted", 0) + 1
    questions = evidence.get("questions", [])
    while questions and size(questions) > budget // 8:
        questions.pop(0)
        evidence["questions_omitted"] = evidence.get("questions_omitted", 0) + 1
    actions = evidence.get("actions", [])
    for limit in (600, 200):
        if size(actions) <= budget // 4:
            break
        for action in actions:
            if "output" in action:
                action["output"] = _bounded_serialized_text(action["output"], limit)
    while actions and size(actions) > budget // 4:
        actions.pop(0)
        evidence["earlier_actions_omitted"] = evidence.get("earlier_actions_omitted", 0) + 1
    sources = evidence.get("sources", [])
    changed = evidence.get("changed_files", [])
    slots = []
    for item in [*changed, *sources]:
        field = "diff" if "diff" in item else "content" if "content" in item else None
        if field:
            slots.append((item, field, item[field]))
            # Reserve the marker's bytes now; untruncated items drop it later.
            item[field], item["truncated"] = "", True
    # If even the metadata does not fit, sources go before changed files.
    while size(evidence) > budget and (sources or changed):
        dropped = (sources or changed).pop()
        slots = [slot for slot in slots if slot[0] is not dropped]
        evidence["files_omitted"] = evidence.get("files_omitted", 0) + 1
    # Each emptied field still costs its two quote bytes in the current size.
    room = budget - size(evidence) + 2 * len(slots)
    shares = _fair_shares([size(text) for _, _, text in slots], room)
    for (item, field, text), share in zip(slots, shares, strict=True):
        item[field] = _bounded_serialized_text(text, share)
        if item[field] == text:
            del item["truncated"]
    if size(evidence) > budget:
        return {"omitted": "Evidence did not fit the review request."}
    return evidence


def _state_context(request: PlanInput) -> dict[str, Any]:
    state = request.state
    progress = state.task_progress.model_dump(mode="json")
    artifacts = [item.model_dump(mode="json") for item in state.artifacts[-20:]]
    context = {
        "goal_id": state.goal_id,
        "status": state.status,
        "reason": _bounded_serialized_text(state.reason, 2_000),
        "steps": state.steps,
        "model_calls": state.model_calls,
        "input_tokens": state.input_tokens,
        "output_tokens": state.output_tokens,
        # History text appears only inside the bounded memory envelope.
        "observation_count": len(state.observations),
        "artifacts": artifacts,
        "earlier_artifacts_omitted": max(0, len(state.artifacts) - 20),
        "task_progress": progress,
        "context_truncated": state.reason != _bounded_serialized_text(state.reason, 2_000),
    }
    # Progress is optional working state, not the owner's acceptance contract.
    # Clip individual notes visibly so a valid multilingual state cannot make
    # the next request fail before the model gets an opportunity to compact it.
    for item_budget, path_budget in ((256, 1024), (128, 512)):
        for kind, items in progress.items():
            compact = [_bounded_serialized_text(item, item_budget) for item in items]
            context["context_truncated"] |= compact != items
            progress[kind] = compact
        for artifact in artifacts:
            original = artifact["path"]
            artifact["path"] = _bounded_serialized_text(original, path_budget)
            if artifact["path"] != original:
                artifact["path_truncated"] = True
                context["context_truncated"] = True
        if conservative_token_count(serialize_memory(context)) <= _STATE_TOKEN_BUDGET:
            break
    return context


def _compact_schema(schema: Any) -> Any:
    """Remove redundant annotations, preserving constraints and property names."""
    if isinstance(schema, list):
        return [_compact_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    named_maps = {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}
    return {
        key: {name: _compact_schema(value) for name, value in item.items()}
        if key in named_maps and isinstance(item, dict)
        else item
        if key in {"enum", "const", "dependentRequired"}
        else _compact_schema(item)
        for key, item in schema.items()
        if key not in {"title", "default", "examples"}
    }


@lru_cache(maxsize=16)
def _schema_text(schema: type[BaseModel]) -> str:
    """Schemas are fixed per class; serialize each once instead of per request."""
    return json.dumps(_compact_schema(schema.model_json_schema()), separators=(",", ":"))


def _strict_schema(schema: Any) -> Any:
    """Closed objects with every property present, minus unsupported keywords."""
    if isinstance(schema, list):
        return [_strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    named_maps = {"properties", "$defs", "definitions"}
    result = {
        key: {name: _strict_schema(value) for name, value in item.items()}
        if key in named_maps and isinstance(item, dict)
        else item
        if key in {"enum", "const", "required"}
        else _strict_schema(item)
        for key, item in schema.items()
        if key not in _UNSUPPORTED_SCHEMA_KEYWORDS
    }
    if isinstance(result.get("properties"), dict):
        # Optional fields are nullable in Rocky's schemas, and the instructions
        # already require explicit nulls, so requiring every field loses nothing.
        result["additionalProperties"] = False
        result["required"] = list(result["properties"])
    return result


@lru_cache(maxsize=16)
def _native_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """The schema for provider-side constrained decoding. Callers must not mutate it."""
    return _strict_schema(_compact_schema(schema.model_json_schema()))


def _envelope_parts(payload: dict[str, Any], stable: tuple[str, ...]) -> list[str]:
    """Split the envelope after the fields that stay identical on every step.

    Prompt caches match request prefixes. The stable part ends at a cache
    breakpoint, so only the changing state, memory and budget are reprocessed.
    """
    head = {key: payload[key] for key in stable if key in payload}
    tail = {key: value for key, value in payload.items() if key not in stable}
    return [_json_context(part) for part in (head, tail) if part]


async def _sse_payloads(response: httpx.Response):
    """Yield each JSON data payload of a server-sent event stream."""
    received = 0
    data: list[str] = []

    def payload() -> Any:
        text = "\n".join(data)
        data.clear()
        if text.strip() == "[DONE]":
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise PlannerError("Model stream was malformed; no decision was accepted.") from None

    async for line in response.aiter_lines():
        received += len(line) + 1
        if received > _MAX_RESPONSE_BYTES:
            raise PlannerError("Model response exceeded the accepted size.")
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif not line and data:
            if (item := payload()) is None:
                return
            yield item
        # event:, id:, retry: and comments carry nothing the JSON lacks.
    if data and (item := payload()) is not None:
        yield item


def _stream_failure(error: Any, produced: bool) -> PlannerError:
    """An error event mid-stream. Never echo provider text into task state."""
    kind = error.get("type", "") if isinstance(error, dict) else ""
    if not produced and kind in {"overloaded_error", "rate_limit_error", "api_error"}:
        return TransientModelError(
            "The model provider was temporarily unavailable. Automatic retries did not "
            "succeed; try again later."
        )
    return PlannerError("Model response did not complete; no decision was accepted.")


async def _anthropic_message(response: httpx.Response) -> dict[str, Any]:
    """Rebuild the non-streaming Messages body from its event stream."""
    message: dict[str, Any] = {"content": [], "usage": {}}
    texts: dict[int, list[str]] = {}
    async for event in _sse_payloads(response):
        kind = event.get("type")
        if kind == "message_start":
            message["usage"].update(event.get("message", {}).get("usage") or {})
        elif kind == "content_block_start":
            if event.get("content_block", {}).get("type") == "text":
                texts[event.get("index", 0)] = [event["content_block"].get("text", "")]
        elif kind == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta" and event.get("index", 0) in texts:
                texts[event.get("index", 0)].append(delta.get("text", ""))
        elif kind == "message_delta":
            message["stop_reason"] = event.get("delta", {}).get("stop_reason")
            # Final usage counts are cumulative; null fields keep earlier values.
            message["usage"].update(
                {key: value for key, value in (event.get("usage") or {}).items() if value}
            )
        elif kind == "error":
            raise _stream_failure(event.get("error"), bool(texts))
    message["content"] = [
        {"type": "text", "text": "".join(parts)} for _, parts in sorted(texts.items())
    ]
    return message


async def _chat_completion(response: httpx.Response) -> dict[str, Any]:
    """Rebuild a non-streaming chat completion body from its chunks."""
    content: list[str] = []
    refusal: list[str] = []
    finish_reason = None
    usage: dict[str, Any] = {}
    async for chunk in _sse_payloads(response):
        if "error" in chunk:
            raise _stream_failure(chunk["error"], bool(content))
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content.append(delta.get("content") or "")
            refusal.append(delta.get("refusal") or "")
            finish_reason = choice.get("finish_reason") or finish_reason
        usage = chunk.get("usage") or usage
    message = {"content": "".join(content), "refusal": "".join(refusal) or None}
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


async def _completion_body(response: httpx.Response, provider: str) -> dict[str, Any]:
    """Accept a stream, or a whole JSON body from a gateway that ignores stream=true."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        if provider == "anthropic":
            return await _anthropic_message(response)
        return await _chat_completion(response)
    content = b""
    async for chunk in response.aiter_bytes():
        content += chunk
        if len(content) > _MAX_RESPONSE_BYTES:
            raise PlannerError("Model response exceeded the accepted size.")
    try:
        return json.loads(content)
    except ValueError:
        raise PlannerError("Model returned an unreadable response.") from None


def _cached_tokens(details: Any) -> int:
    value = details.get("cached_tokens") if isinstance(details, dict) else None
    if value is None:
        value = getattr(details, "cached_tokens", 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _budget_remaining(request: PlanInput) -> dict[str, int | None]:
    """What the task may still spend; None means that limit is disabled."""
    limits, state = request.run.contract.limits, request.state

    def left(limit: int | None, used: int) -> int | None:
        return None if limit is None else max(0, limit - used)

    seconds = None
    if state.deadline_at:
        deadline = datetime.fromisoformat(state.deadline_at)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        seconds = max(0, int((deadline - datetime.now(UTC)).total_seconds()))
    return {
        "steps": left(limits.max_steps, state.steps),
        "model_calls": left(limits.max_model_calls, state.model_calls),
        "tokens": left(limits.max_tokens, state.input_tokens + state.output_tokens),
        "seconds": seconds,
    }


def _planning_run_context(request: PlanInput, *, scoped: bool) -> dict[str, Any]:
    fields = {
        "objective",
        "acceptance_criteria",
        "deliverables",
        "limits",
        "folders",
        "files",
        "max_wait_seconds",
        "max_no_progress",
    }
    if scoped:
        fields.update({"allowed_tools", "allowed_domains"})
    return {
        "goal_id": request.run.goal_id,
        "contract": request.run.contract.model_dump(mode="json", include=fields),
    }


def _invalid_result(error: ValidationError) -> PlannerError:
    # Validation values/messages can contain credentials or injected text. Only
    # known schema field names and static error codes cross the model boundary.
    fields = {
        "kind",
        "reason",
        "action",
        "actions",
        "wait_seconds",
        "tool",
        "path",
        "url",
        "content",
        "handoff_summary",
        "task_progress",
        "memory_proposals",
        "completed",
        "pending",
        "evidence",
        "passed",
        "feedback",
    }
    issues = []
    for issue in error.errors(include_input=False, include_context=False)[:5]:
        location = (
            ".".join(
                str(part) if isinstance(part, int) else part if part in fields else "field"
                for part in issue["loc"]
            )
            or "result"
        )
        code = issue["type"]
        if not code.replace("_", "").isalnum():
            code = "schema_error"
        issues.append(f"{location}: {code}")
    return PlannerError(
        "Model returned an invalid structured result. Correct these schema fields: "
        + "; ".join(issues)
        + ". Return the complete requested schema with null for unused fields."
    )


def _parsed_result[Parsed: BaseModel](
    response: Any, schema: type[Parsed], max_output_tokens: int
) -> tuple[Parsed, int, int, int]:
    reported = _known_usage(getattr(response, "usage", None))
    if getattr(response, "status", None) != "completed":
        details = getattr(response, "incomplete_details", None)
        if getattr(details, "reason", None) == "max_output_tokens":
            raise _truncated_output(max_output_tokens).with_usage(reported)
        raise PlannerError("Model response did not complete; no decision was accepted.").with_usage(
            reported
        )
    for item in getattr(response, "output", None) or []:
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", None) == "refusal":
                raise PlannerError(
                    "Model refused the request; no decision was accepted."
                ).with_usage(reported)
    parsed = getattr(response, "output_parsed", None)
    if parsed is None:
        raise PlannerError(
            "Model returned no structured result; no decision was accepted."
        ).with_usage(reported)
    try:
        result = schema.model_validate(parsed)
        usage = getattr(response, "usage", None)
        input_tokens = max(0, int(getattr(usage, "input_tokens", 0) or 0))
        output_tokens = max(0, int(getattr(usage, "output_tokens", 0) or 0))
        cached = _cached_tokens(getattr(usage, "input_tokens_details", None))
    except ValidationError as error:
        raise _invalid_result(error).with_usage(reported) from None
    except (TypeError, ValueError, OverflowError):
        raise PlannerError("Model returned an invalid structured result.").with_usage(
            reported
        ) from None
    return result, input_tokens, output_tokens, min(cached, input_tokens)


async def _streamed_response(client: Any, arguments: dict[str, Any]) -> Any:
    """Stream a Responses request; return its completed, incomplete or failed response."""
    async with client.responses.stream(**arguments) as stream:
        final = None
        async for event in stream:
            if event.type in {"response.completed", "response.incomplete", "response.failed"}:
                final = event
        if final is None:
            raise PlannerError("Model response did not complete; no decision was accepted.")
        if final.type == "response.completed":
            return await stream.get_final_response()
        return final.response


def consumed_owner_messages(request: PlanInput, payload: dict) -> list[str]:
    """Confirm only whole messages actually included in the bounded request."""
    visible = {item["id"]: item["content"] for item in payload["memory"].get("conversation", [])}
    return [
        item["id"]
        for item in request.memory.get("conversation", [])
        if item.get("role") == "user"
        and str(item.get("id", "")).startswith("wake:")
        and "[MEMORY TRUNCATED" not in item["content"]
        and (
            visible.get(item["id"]) == item["content"]
            or (item["content"] == request.feedback == payload["feedback"])
        )
    ][:128]


class OpenAIPlanner:
    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        *,
        defer_client: bool = False,
        credential_store: CredentialStore | None = None,
        http_client: httpx.AsyncClient | None = None,
        memory_manager: MemoryManager | None = None,
        prompt_archive: PromptArchive | None = None,
        model_queue: ModelQueue | None = None,
    ) -> None:
        self.settings = settings
        self.memory_manager = memory_manager or MemoryManager()
        self.prompt_archive = prompt_archive
        self.model_queue = model_queue
        self._owns_client = client is None
        self.credentials = credential_store or CredentialStore(settings.credential_path)
        self._http_client = http_client
        self._owns_http_client = http_client is None
        # Reused across steps so each request skips connection and TLS setup.
        # Keyed by endpoint and a key digest: a replaced key gets a new client.
        self._provider_clients: dict[tuple[str, str], AsyncOpenAI] = {}
        # Endpoints and models that rejected provider-side structured output.
        # Their requests rely on the schema in the instructions instead.
        self._prompted_schema: set[tuple[str, str, str]] = set()
        # The SDK reads OPENAI_API_KEY itself. Never copy it into settings,
        # persisted state, prompts, logs or custom exception messages.
        try:
            self.client = (
                client
                if client is not None
                else None
                if defer_client
                else AsyncOpenAI(max_retries=0, timeout=_MODEL_TIMEOUT)
            )
        except Exception:
            raise PlannerError(
                "Model client could not initialize; check its configuration."
            ) from None

    async def close(self) -> None:
        clients = list(self._provider_clients.values())
        self._provider_clients.clear()
        if self._owns_client and self.client is not None:
            clients.append(self.client)
        failed = False
        for client in clients:
            try:
                await client.close()
            except Exception:
                failed = True
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
        if failed:
            raise PlannerError("Model client could not close cleanly.")

    def _openai_client(self, base_url: str, key: str) -> AsyncOpenAI:
        identity = (base_url, hashlib.sha256(key.encode()).hexdigest())
        if (client := self._provider_clients.get(identity)) is None:
            client = AsyncOpenAI(
                api_key=key, base_url=base_url, max_retries=0, timeout=_MODEL_TIMEOUT
            )
            self._provider_clients[identity] = client
        return client

    def _inference(self, model_settings: ModelSettings | None) -> ModelSettings:
        return model_settings or ModelSettings(
            model=self.settings.model, reasoning_effort=self.settings.reasoning_effort
        )

    def _response_tokens(self, setting: str, default: int, inference: ModelSettings) -> int:
        tokens = getattr(self.settings, setting, None) or default
        return tokens * 2 if inference.reasoning_effort in _HIGH_REASONING else tokens

    @staticmethod
    def _responses_arguments(
        inference: ModelSettings,
        instructions: str,
        context: str,
        schema: type[BaseModel],
        max_output_tokens: int,
        cache_key: str | None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "model": inference.model,
            "input": [
                {"role": "developer", "content": instructions},
                {"role": "user", "content": context},
            ],
            "text_format": schema,
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        if inference.reasoning_effort is not None:
            arguments["reasoning"] = {"effort": inference.reasoning_effort}
        if cache_key is not None:
            # The instructions and schema form an identical prefix on every
            # step; a stable key routes requests to the same prompt cache.
            arguments["prompt_cache_key"] = cache_key
        return arguments

    async def _provider_request[Parsed: BaseModel](
        self,
        inference: ModelSettings,
        instructions: str,
        payload: dict[str, Any],
        stable: tuple[str, ...],
        schema: type[Parsed],
        max_output_tokens: int,
        cache_key: str,
    ) -> tuple[Parsed, int, int, int]:
        provider = inference.provider or "openai"
        base_url = normalize_base_url(inference.base_url or _OPENAI_BASE_URL)
        account = inference.credential_id or credential_id(provider, base_url)
        key = self.credentials.get(account)
        # Environment fallback is restricted to the official OpenAI endpoint.
        # A custom endpoint never inherits a key intended for another service.
        if not key and provider == "openai" and base_url == _OPENAI_BASE_URL:
            key = os.environ.get("OPENAI_API_KEY")
        if not key and provider != "ollama":
            raise PlannerError(
                "No API key is configured for this provider; open Model Settings.",
                code="missing_api_key",
            )
        if provider == "openai":
            response = await _streamed_response(
                self._openai_client(base_url, key),
                self._responses_arguments(
                    inference,
                    instructions,
                    _json_context(payload),
                    schema,
                    max_output_tokens,
                    # Compatible gateways may reject parameters they do not know.
                    cache_key if base_url == _OPENAI_BASE_URL else None,
                ),
            )
            return _parsed_result(response, schema, max_output_tokens)
        endpoint = (provider, base_url, inference.model)
        native = endpoint not in self._prompted_schema
        try:
            return await self._http_request(
                provider,
                base_url,
                key,
                inference,
                instructions,
                payload,
                stable,
                schema,
                max_output_tokens,
                native=native,
            )
        except httpx.HTTPStatusError as error:
            if not native or error.response.status_code not in {400, 422}:
                raise
        # A rejected request produced no output. Retry once without the
        # provider-side format; only its success shows the format was the cause.
        result = await self._http_request(
            provider,
            base_url,
            key,
            inference,
            instructions,
            payload,
            stable,
            schema,
            max_output_tokens,
            native=False,
        )
        self._prompted_schema.add(endpoint)
        return result

    async def _http_request[Parsed: BaseModel](
        self,
        provider: str,
        base_url: str,
        key: str | None,
        inference: ModelSettings,
        instructions: str,
        payload: dict[str, Any],
        stable: tuple[str, ...],
        schema: type[Parsed],
        max_output_tokens: int,
        *,
        native: bool,
    ) -> tuple[Parsed, int, int, int]:
        # The schema text stays in both modes: it states the length and range
        # limits that constrained decoding drops, and keeps the cached prefix.
        system = (
            instructions
            + "\nReturn exactly one JSON object, without Markdown, matching this JSON schema:\n"
            + _schema_text(schema)
        )
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=_MODEL_TIMEOUT, trust_env=False, follow_redirects=False
            )
        if provider == "anthropic":
            parts = _envelope_parts(payload, stable)
            # The system prompt and the stable envelope part are identical on
            # every step. Each ends at a breakpoint the provider caches.
            content = [{"type": "text", "text": text} for text in parts]
            if len(content) > 1:
                content[0]["cache_control"] = _CACHE_BREAKPOINT
            body = {
                "model": inference.model,
                "system": [{"type": "text", "text": system, "cache_control": _CACHE_BREAKPOINT}],
                "messages": [{"role": "user", "content": content}],
                "max_tokens": max_output_tokens,
                "stream": True,
            }
            if native:
                body["output_config"] = {
                    "format": {"type": "json_schema", "schema": _native_schema(schema)}
                }
            request = self._http_client.stream(
                "POST",
                base_url + "/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json=body,
            )
        else:
            body = {
                "model": inference.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": _json_context(payload)},
                ],
                "max_tokens": max_output_tokens,
                "stream": True,
                "stream_options": {"include_usage": True},
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema.__name__,
                        "schema": _native_schema(schema),
                        "strict": True,
                    },
                }
                if native
                else {"type": "json_object"},
            }
            request = self._http_client.stream(
                "POST",
                base_url + "/chat/completions",
                headers={"Authorization": f"Bearer {key or 'ollama'}"},
                json=body,
            )
        async with request as response:
            if response.status_code == 429:
                # Streaming errors have no cached body. Read only a bounded
                # error envelope to distinguish exhausted quota from pacing.
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk[: 16_385 - len(body)])
                    if len(body) > 16_384:
                        break
                try:
                    error = json.loads(body).get("error", {}) if len(body) <= 16_384 else {}
                    if isinstance(error, dict):
                        response.extensions["rocky_error_code"] = error.get("code") or error.get(
                            "type"
                        )
                except (ValueError, AttributeError):
                    pass
            response.raise_for_status()
            result = await _completion_body(response, provider)
        usage = result.get("usage") or {}
        reported = _known_usage(usage, provider)
        if provider == "anthropic":
            if result.get("stop_reason") == "max_tokens":
                raise _truncated_output(max_output_tokens).with_usage(reported)
            if result.get("stop_reason") == "refusal":
                raise PlannerError(
                    "Model refused the request; no decision was accepted."
                ).with_usage(reported)
            if result.get("stop_reason") != "end_turn":
                raise PlannerError(
                    "Model response did not complete; no decision was accepted."
                ).with_usage(reported)
            content = "".join(
                item.get("text", "")
                for item in result.get("content", [])
                if item.get("type") == "text"
            )
            cached = int(usage.get("cache_read_input_tokens") or 0)
            # Cached prefix tokens are reported apart from input_tokens. Count
            # all of them so the task's token limit keeps its meaning.
            input_tokens = cached + sum(
                int(usage.get(name) or 0)
                for name in ("input_tokens", "cache_creation_input_tokens")
            )
            output_tokens = usage.get("output_tokens", 0)
        else:
            choice = result["choices"][0]
            if choice.get("finish_reason") == "length":
                raise _truncated_output(max_output_tokens).with_usage(reported)
            if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
                raise PlannerError(
                    "Model response did not complete; no decision was accepted."
                ).with_usage(reported)
            content = choice["message"]["content"]
            input_tokens, output_tokens = (
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
            )
            # DeepSeek reports cache hits under its own field name.
            cached = _cached_tokens(usage.get("prompt_tokens_details")) or int(
                usage.get("prompt_cache_hit_tokens") or 0
            )
        content = content.strip()
        if content.startswith("```json\n") and content.endswith("\n```"):
            content = content[8:-4]
        try:
            parsed = schema.model_validate_json(content)
        except ValidationError as error:
            raise _invalid_result(error).with_usage(reported) from None
        input_tokens = max(0, int(input_tokens))
        return parsed, input_tokens, max(0, int(output_tokens)), min(max(0, cached), input_tokens)

    async def _request(self, *, interactive=False, **arguments):
        if self.model_queue is None:
            return await self._request_raw(**arguments)
        from temporalio import activity

        schema = arguments["schema"]
        purpose = {
            Decision: "planner",
            ReviewVerdict: "reviewer",
            DeliveryRequirements: "contract",
            PlanDecomposition: "decomposition",
            PlanRevision: "revision",
        }[schema]
        goal_id = arguments["payload"].get("run", {}).get("goal_id")
        attempt = activity.info().attempt if activity.in_activity() else 1
        try:
            async with self.model_queue.slot(purpose, goal_id, interactive, attempt):
                return await self._request_raw(**arguments)
        except (PlannerError, ModelRequestCancelled):
            raise
        except Exception:
            raise TransientModelError("Model admission is temporarily unavailable.") from None

    async def _request_raw[Parsed: BaseModel](
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
        schema: type[Parsed],
        max_output_tokens: int,
        inference: ModelSettings,
        cache_key: str,
        stable: tuple[str, ...] = ("run",),
    ) -> tuple[Parsed, int, int, int]:
        """Returns the result and input, output and cache-read input tokens."""
        context = _json_context(payload)
        try:
            async with asyncio.timeout(MODEL_RESPONSE_SECONDS):
                if inference.provider is not None or self.client is None:
                    result = await self._provider_request(
                        inference,
                        instructions,
                        payload,
                        stable,
                        schema,
                        max_output_tokens,
                        cache_key,
                    )
                    log.info(
                        "Model usage: %d input (%d cached), %d output tokens",
                        result[1],
                        result[3],
                        result[2],
                    )
                    return result
                official = str(getattr(self.client, "base_url", "")).rstrip("/") == _OPENAI_BASE_URL
                response = await _streamed_response(
                    self.client,
                    self._responses_arguments(
                        inference,
                        instructions,
                        context,
                        schema,
                        max_output_tokens,
                        cache_key if official else None,
                    ),
                )
        except PlannerError:
            raise
        except ValidationError as error:
            # The SDK validates structured output itself. Invalid JSON from a
            # schema-constrained response almost always means it was cut off.
            issues = error.errors(include_input=False, include_context=False)
            if any(issue["type"] == "json_invalid" for issue in issues):
                raise _truncated_output(max_output_tokens, likely=True) from None
            raise _invalid_result(error) from None
        except Exception as error:
            # Provider/validation errors can echo request data or headers. Keep
            # only fixed messages, including in Temporal's serialized error.
            if failure := _provider_failure(error):
                raise failure from None
            if transient := _transient_failure(error):
                raise transient from None
            if isinstance(error, TimeoutError):
                raise PlannerError(
                    f"Model response did not finish within {MODEL_RESPONSE_SECONDS:.0f} "
                    "seconds; no decision was accepted. Keep reasoning and action content "
                    "shorter.",
                    code="model_timeout",
                ) from None
            if isinstance(error, openai.APITimeoutError | httpx.TimeoutException):
                raise PlannerError(
                    f"Model sent nothing for {MODEL_TIMEOUT_SECONDS:.0f} seconds; no "
                    "decision was accepted. Keep reasoning and action content shorter.",
                    code="model_timeout",
                ) from None
            raise PlannerError(
                "Model request failed; check service availability and configuration."
            ) from None
        result = _parsed_result(response, schema, max_output_tokens)
        log.info(
            "Model usage: %d input (%d cached), %d output tokens", result[1], result[3], result[2]
        )
        return result

    @staticmethod
    def _automatic_limits(requirements: DeliveryRequirements) -> ExecutionLimits:
        estimated = requirements.limits
        # Every planning call resends instructions, the contract and accumulated
        # evidence. A short answer is not a cheap *run*. Reserve review/repair
        # calls and 32k cumulative input/output tokens per call, plus setup.
        calls = max(estimated.max_model_calls, estimated.max_steps + 8)
        return ExecutionLimits(
            max_steps=estimated.max_steps,
            max_model_calls=calls,
            max_tokens=max(estimated.max_tokens, calls * 32_000 + 64_000),
            timeout_seconds=max(estimated.timeout_seconds, min(3600, calls * 45)),
        )

    async def prepare_contract(
        self, draft: GoalDraft, model_settings: ModelSettings, *, interactive: bool = True
    ) -> DraftedContract:
        prompt = prompt_preview("contract", self.settings.tool_access)
        await self._record_prompt(prompt)
        requirements, input_tokens, output_tokens, _ = await self._request(
            interactive=interactive,
            instructions=prompt.instructions,
            payload={"objective": draft.objective}
            | ({"granted_folders": draft.folders} if draft.folders else {})
            | ({"attached_files": draft.files} if draft.files else {}),
            schema=DeliveryRequirements,
            max_output_tokens=self._response_tokens(
                "plan_response_tokens", _PLAN_RESPONSE_TOKENS, model_settings
            ),
            inference=model_settings,
            cache_key="rocky-delivery-requirements-v6",
        )
        # This call precedes the goal, so no task budget records its cost.
        log.info(
            "Prepared goal contract with %d input and %d output tokens",
            input_tokens,
            output_tokens,
        )
        return DraftedContract(
            objective=draft.objective,
            folders=draft.folders,
            files=draft.files,
            acceptance_criteria=requirements.acceptance_criteria,
            deliverables=requirements.deliverables,
            limits=self._automatic_limits(requirements),
            concerns=requirements.concerns,
            drafting_tokens=input_tokens + output_tokens,
        )

    async def decompose_goal(
        self, draft: GoalDraft, model_settings: ModelSettings
    ) -> tuple[PlanDecomposition, int]:
        """Break a large objective into sequential steps; returns the plan and tokens used."""
        prompt = prompt_preview("decomposition", self.settings.tool_access)
        await self._record_prompt(prompt)
        result, input_tokens, output_tokens, _ = await self._request(
            interactive=True,
            instructions=prompt.instructions,
            payload={"objective": draft.objective}
            | ({"granted_folders": draft.folders} if draft.folders else {})
            | ({"attached_files": draft.files} if draft.files else {}),
            schema=PlanDecomposition,
            max_output_tokens=self._response_tokens(
                "plan_response_tokens", _PLAN_RESPONSE_TOKENS, model_settings
            ),
            inference=model_settings,
            cache_key="rocky-plan-decomposition-v1",
        )
        log.info(
            "Decomposed a goal with %d input and %d output tokens", input_tokens, output_tokens
        )
        return result, input_tokens + output_tokens

    async def revise_plan(
        self, progress: dict[str, Any], model_settings: ModelSettings
    ) -> tuple[PlanRevision, int]:
        """Review a plan after a step finished; returns the revision and tokens used."""
        prompt = prompt_preview("revision", self.settings.tool_access)
        await self._record_prompt(prompt)
        result, input_tokens, output_tokens, _ = await self._request(
            instructions=prompt.instructions,
            payload=progress,
            schema=PlanRevision,
            max_output_tokens=self._response_tokens(
                "plan_response_tokens", _PLAN_RESPONSE_TOKENS, model_settings
            ),
            inference=model_settings,
            cache_key="rocky-plan-revision-v1",
        )
        log.info("Revised a plan with %d input and %d output tokens", input_tokens, output_tokens)
        return result, input_tokens + output_tokens

    async def plan(self, request: PlanInput) -> PlanResult:
        prompt = prompt_preview(
            "planner", self.settings.tool_access, guidance_for(request.run.contract)
        )
        inference = self._inference(request.run.model_settings)
        response_tokens = self._response_tokens(
            "plan_response_tokens", _PLAN_RESPONSE_TOKENS, inference
        )
        configured = getattr(self.settings, "auto_approved_prefixes", None)
        prefixes = [
            *(configured() if callable(configured) else []),
            *request.state.approved_command_prefixes,
        ]
        # Fields in order of how long they stay identical: the contract and
        # execution policy for the whole task, then per-step state and memory.
        # Prompt caches reuse the longest unchanged prefix.
        payload = {
            "run": _context_layer(
                _planning_run_context(request, scoped=self.settings.tool_access != "full"),
                _TASK_TOKEN_BUDGET,
                "task contract",
            ),
            "execution": {
                "tool_access": self.settings.tool_access,
                "approval_mode": getattr(self.settings, "approval_mode", "required"),
                "auto_approved_commands": list(
                    dict.fromkeys(shlex.join(prefix) for prefix in prefixes)
                )[:50],
                "approval_rules": request.approvals,
                "max_parallel_actions": MAX_PARALLEL_ACTIONS,
                "file_batch_tools": list(FILE_BATCH_TOOLS) if request.file_batches else [],
                "response_token_limit": response_tokens,
                "python_executable": sys.executable,
            },
            "state": _context_layer(_state_context(request), _STATE_TOKEN_BUDGET, "task state"),
            "memory": {},
            "feedback": _bounded_serialized_text(request.feedback, _FEEDBACK_TOKEN_BUDGET),
            "budget_remaining": _budget_remaining(request),
        }
        # Memory receives whatever the essential layers leave of the request
        # bound, so a large contract shrinks recall instead of failing the call.
        room = (
            _MAX_CONTEXT_TOKENS
            - conservative_token_count(serialize_memory(payload))
            + len(serialize_memory({}))
        )
        if room < 256:
            raise PlannerError("Goal context is too large for a bounded model request.")
        # Whole owner feedback already present in the envelope needs one copy.
        # If feedback was clipped, retain the independently bounded history.
        payload["memory"] = bounded_memory(
            request,
            manager=self.memory_manager.with_budget(min(self.memory_manager.budget, room)),
            omit_user_content=request.feedback if payload["feedback"] == request.feedback else "",
        )
        await self._record_prompt(prompt, request.run.goal_id)
        decision, input_tokens, output_tokens, cached = await self._request(
            instructions=prompt.instructions,
            payload=payload,
            schema=Decision,
            max_output_tokens=response_tokens,
            inference=inference,
            cache_key="rocky-planner",
            stable=("run", "execution"),
            interactive=request.interactive,
        )
        return PlanResult(
            decision=decision,
            consumed_message_ids=consumed_owner_messages(request, payload),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
        )

    async def review(
        self,
        request: VerifyInput,
        artifacts_text: str,
        evidence: dict | None = None,
        checklist: list[dict] | None = None,
        verified: list[dict] | None = None,
    ) -> VerificationResult:
        visible_artifacts = _bounded_serialized_text(
            _bounded_text(artifacts_text, _MAX_REVIEW_ARTIFACT_CHARS),
            _REVIEW_ARTIFACT_TOKEN_BUDGET,
        )
        inference = self._inference(request.run.model_settings)
        payload = {
            "run": _context_layer(
                request.run.model_copy(update={"checkpoint": None}).model_dump(mode="json"),
                _TASK_TOKEN_BUDGET,
                "task contract",
            ),
            "artifacts_text": visible_artifacts,
            "artifacts_text_truncated": visible_artifacts != artifacts_text,
        }
        if checklist:
            payload["required_claims"] = checklist
        if verified:
            payload["checked_by_application"] = verified
        if evidence is not None:
            payload["evidence"] = {}
            room = (
                _MAX_CONTEXT_TOKENS
                - conservative_token_count(serialize_memory(payload))
                + len(serialize_memory({}))
            )
            payload["evidence"] = _fit_evidence(evidence, min(room, _REVIEW_EVIDENCE_TOKEN_BUDGET))
        prompt = prompt_preview("reviewer", self.settings.tool_access)
        await self._record_prompt(prompt, request.run.goal_id)
        verdict, input_tokens, output_tokens, cached = await self._request(
            instructions=prompt.instructions,
            payload=payload,
            schema=ReviewVerdict,
            max_output_tokens=self._response_tokens(
                "review_response_tokens", _REVIEW_RESPONSE_TOKENS, inference
            ),
            inference=inference,
            cache_key="rocky-reviewer",
        )
        return VerificationResult(
            passed=verdict.passed,
            feedback=verdict.feedback,
            claims=[claim.model_dump() for claim in verdict.claims],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
        )

    async def _record_prompt(self, prompt: PromptSnapshot, goal_id: str | None = None) -> None:
        if self.prompt_archive is not None:
            try:
                await asyncio.to_thread(self.prompt_archive.record, prompt, goal_id)
            except Exception:
                # Fail before dispatch, so an unrecorded request never consumes tokens.
                raise TransientModelError(
                    "Could not save the model instruction snapshot."
                ) from None


def prompt_preview(purpose: Purpose, tool_access: str = "full", guidance=None) -> PromptSnapshot:
    schemas = {
        "planner": Decision,
        "reviewer": ReviewVerdict,
        "contract": DeliveryRequirements,
        "decomposition": PlanDecomposition,
        "revision": PlanRevision,
    }
    return compose(purpose, schemas[purpose], tool_access=tool_access, guidance=guidance)
