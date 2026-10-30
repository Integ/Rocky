"""Side-effect-free validation and approval identities for tool dispatch.

The public Action envelope remains compatible with persisted Temporal histories.
Its tool-specific contract is checked here before a dispatch marker or external
effect is created. Validation errors never quote request bodies or credentials.
"""

import hashlib
import json

import httpx

from rocky.http_tools import HTTPToolError, _request_options
from rocky.models import SAFE_HTTP_METHODS, Action, ToolInput

MAX_EDITS = 50


class ActionValidationError(ValueError):
    """Safe, actionable validation feedback for the planner."""


def approval_fingerprint(request: ToolInput) -> str:
    """Bind owner approval to this exact operation, goal and action.

    A grant cannot authorize a changed command, destination or a later operation
    with identical arguments. Inference settings and unrelated task metadata do
    not affect the action being reviewed.
    """
    action = request.action.model_dump(include={"tool", "path", "url", "content"})
    # Default isolation preserves identities from durable histories written
    # before sandbox was an Action field. An opt-out changes the reviewed act.
    if not request.action.sandbox:
        action["sandbox"] = False
    action.update(_command_options(request.action))
    payload = {
        "goal_id": request.run.goal_id,
        "operation_id": request.operation_id,
        "action": action,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _command_options(action: Action) -> dict:
    """Timeout and background options that change the reviewed act.

    Unset options are left out so identities recorded before they existed
    stay valid.
    """
    return {
        key: value
        for key, value in action.model_dump(include={"timeout_seconds", "background"}).items()
        if value is not None
    }


def action_identity(goal_id: str, action: Action) -> str:
    """Identify an action's content in a task, independent of its operation ID.

    An owner who approves a declined request reviewed exactly this content, so
    the task may repeat the identical action once under a new operation ID.
    """
    payload = {
        "goal_id": goal_id,
        "action": {
            **action.model_dump(include={"tool", "path", "url", "content", "sandbox"}),
            **_command_options(action),
        },
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


def parse_edits(content: str | None) -> list[tuple[str, str, bool]]:
    """Parse edit_file content into ordered (old_text, new_text, replace_all) edits."""
    if content is None:
        raise ActionValidationError(
            'edit_file requires content: a JSON object such as {"old_text":"...","new_text":"..."}.'
        )
    try:
        value = json.loads(content)
    except (ValueError, TypeError, RecursionError):
        raise ActionValidationError(
            "edit_file content must be valid JSON: "
            '{"old_text":"exact current text","new_text":"replacement"} or {"edits":[...]}.'
        ) from None
    if not isinstance(value, dict):
        raise ActionValidationError("edit_file content must be a JSON object.")
    items = value["edits"] if "edits" in value else [value]
    if "edits" in value and len(value) > 1:
        raise ActionValidationError("Use either a single edit object or only an edits list.")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_EDITS:
        raise ActionValidationError(f"edits must be a list of 1 to {MAX_EDITS} edit objects.")
    edits = []
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict) or set(item) - {"old_text", "new_text", "replace_all"}:
            raise ActionValidationError(
                f"Edit {number} must contain only old_text, new_text and optional replace_all."
            )
        old, new, replace_all = item.get("old_text"), item.get("new_text"), item.get("replace_all")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ActionValidationError(
                f"Edit {number} requires nonempty old_text and string new_text."
            )
        if replace_all is not None and not isinstance(replace_all, bool):
            raise ActionValidationError(f"Edit {number}: replace_all must be true or false.")
        edits.append((old, new, bool(replace_all)))
    return edits


def parse_read_range(content: str | None) -> tuple[int, int] | None:
    if content is None:
        return None
    message = (
        'read_file content must be {"start_line":1,"end_line":120}, '
        "using positive integer line numbers and at most 400 lines; use null to read normally."
    )

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate field")
            result[key] = value
        return result

    try:
        value = json.loads(content, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError):
        raise ActionValidationError(message) from None
    if not isinstance(value, dict) or set(value) != {"start_line", "end_line"}:
        raise ActionValidationError(message)
    start, end = value["start_line"], value["end_line"]
    if (
        type(start) is not int
        or type(end) is not int
        or not 1 <= start <= end <= 1_000_000
        or end - start >= 400
    ):
        raise ActionValidationError(message)
    return start, end


def validate_action(action: Action, *, full_access: bool) -> dict | None:
    """Validate required/unused fields; return parsed HTTP options when relevant."""
    if not action.sandbox and action.tool != "run_command":
        raise ActionValidationError(
            "sandbox=false is only supported by run_command; use sandbox=true for other tools."
        )
    if action.path is not None and (not isinstance(action.path, str) or "\x00" in action.path):
        raise ActionValidationError(
            "path must be text without NUL characters; choose a valid path."
        )
    if action.tool in {"read_file", "write_file", "append_file", "edit_file"} and not action.path:
        raise ActionValidationError(
            f"{action.tool} requires path; supply the file to read or change."
        )
    if action.tool not in {"fetch_url", "http_request"} and action.url is not None:
        raise ActionValidationError(f"{action.tool} does not accept url; set url to null.")
    if action.tool == "list_files" and action.content is not None:
        raise ActionValidationError(f"{action.tool} does not accept content; set content to null.")
    if action.tool == "read_file":
        parse_read_range(action.content)
    if action.tool == "write_file" and action.content is None:
        raise ActionValidationError(
            "write_file requires content; supply the complete text to write."
        )
    if action.tool == "append_file" and not action.content:
        raise ActionValidationError("append_file requires nonempty content to append.")
    if action.tool == "edit_file":
        parse_edits(action.content)
    if action.tool != "run_command" and (
        action.timeout_seconds is not None or action.background is not None
    ):
        raise ActionValidationError(
            f"{action.tool} does not accept timeout_seconds or background; set them to null."
        )
    if action.tool == "run_command":
        if not action.content or not action.content.strip() or "\x00" in action.content:
            raise ActionValidationError(
                "run_command requires a nonempty shell command in content without NUL characters."
            )
        if action.background and action.timeout_seconds is not None:
            raise ActionValidationError(
                "A background job has no timeout; set timeout_seconds to null when background is true."
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


# A safe-method request this long, or with a body, can carry the task's data
# out as easily as a POST, so it is reviewed like one.
MAX_UNREVIEWED_URL = 2048


def requires_approval(action: Action, options: dict | None = None) -> bool:
    """Opaque shell code and potentially mutating HTTP calls need owner approval."""
    if action.tool == "run_command":
        return True
    if action.tool not in {"http_request", "fetch_url"}:
        return False
    options = options or {}
    return (
        options.get("method", "GET").upper() not in SAFE_HTTP_METHODS
        or any(options.get(key) is not None for key in ("json", "data", "body"))
        or len(action.url or "") > MAX_UNREVIEWED_URL
    )
