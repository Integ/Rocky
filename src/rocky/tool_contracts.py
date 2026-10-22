"""Side-effect-free validation and approval identities for tool dispatch.

The public Action envelope remains compatible with persisted Temporal histories.
Its tool-specific contract is checked here before a dispatch marker or external
effect is created. Validation errors never quote request bodies or credentials.
"""

import hashlib
import json

import httpx

from rocky.http_tools import HTTPToolError, _request_options
from rocky.models import Action, ToolInput


class ActionValidationError(ValueError):
    """Safe, actionable validation feedback for the planner."""


def approval_fingerprint(request: ToolInput) -> str:
    """Bind owner approval to this exact operation, goal and action.

    A grant cannot authorize a changed command, destination or a later operation
    with identical arguments. Inference settings and unrelated task metadata do
    not affect the action being reviewed.
    """
    payload = {
        "goal_id": request.run.goal_id,
        "operation_id": request.operation_id,
        "action": request.action.model_dump(include={"tool", "path", "url", "content"}),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _json_object(content: str | None) -> dict:
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate field")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("Non-finite JSON number")

    try:
        value = (
            json.loads(content, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
            if content is not None
            else {}
        )
    except (ValueError, TypeError, RecursionError):
        raise ActionValidationError(
            "HTTP content must be a JSON object; correct its JSON syntax, remove duplicate "
            'fields and use finite numbers. Example: {"method":"GET"}.'
        ) from None
    if not isinstance(value, dict):
        raise ActionValidationError(
            "HTTP content must be a JSON object, not an array or scalar. "
            "Use {} for a default GET request."
        )
    return value


def validate_action(action: Action, *, full_access: bool) -> dict | None:
    """Validate required/unused fields; return parsed HTTP options when relevant."""
    if action.path is not None and (not isinstance(action.path, str) or "\x00" in action.path):
        raise ActionValidationError(
            "path must be text without NUL characters; choose a valid path."
        )
    if action.tool in {"read_file", "write_file"} and not action.path:
        raise ActionValidationError(
            f"{action.tool} requires path; supply the file to read or write."
        )
    if action.tool not in {"fetch_url", "http_request"} and action.url is not None:
        raise ActionValidationError(f"{action.tool} does not accept url; set url to null.")
    if action.tool in {"read_file", "list_files"} and action.content is not None:
        raise ActionValidationError(f"{action.tool} does not accept content; set content to null.")
    if action.tool == "write_file" and action.content is None:
        raise ActionValidationError(
            "write_file requires content; supply the complete text to write."
        )
    if action.tool == "run_command":
        if not action.content or not action.content.strip() or "\x00" in action.content:
            raise ActionValidationError(
                "run_command requires a nonempty shell command in content without NUL characters."
            )
    if action.tool not in {"fetch_url", "http_request"}:
        return None
    if (
        not action.url
        or len(action.url) > 16_384
        or any(ord(c) < 32 or ord(c) == 127 for c in action.url)
    ):
        raise ActionValidationError(
            "Supply a valid http:// or https:// URL without control characters."
        )
    try:
        parsed = httpx.URL(action.url)
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ValueError
    except (ValueError, httpx.InvalidURL):
        raise ActionValidationError(
            "Supply an absolute http:// or https:// URL with a hostname."
        ) from None
    if not full_access and (action.content is not None or action.path is not None):
        raise ActionValidationError(
            "Scoped fetch_url accepts only url; set path and content to null."
        )
    options = _json_object(action.content)
    try:
        _request_options(options)
    except HTTPToolError as exc:
        raise ActionValidationError(
            f"{exc}. Correct the HTTP options: use method, headers, one of json/data/body, "
            "session, timeout, follow_redirects or auth; omit unused fields."
        ) from None
    except (TypeError, ValueError, UnicodeError):
        raise ActionValidationError(
            "HTTP options contain invalid header or body values. Use valid HTTP header names "
            "and string values without control characters, and a JSON-serializable body."
        ) from None
    return options


def requires_approval(action: Action, options: dict | None = None) -> bool:
    """Opaque shell code and potentially mutating HTTP calls need owner approval."""
    return action.tool == "run_command" or (
        action.tool in {"http_request", "fetch_url"}
        and (options or {}).get("method", "GET").upper() not in {"GET", "HEAD", "OPTIONS"}
    )
