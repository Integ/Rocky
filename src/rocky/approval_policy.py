"""Owner approval rules that let gated actions run without pausing the task.

Each gated action is checked against tiers in order: the read-only command
catalog, the owner's persistent rules (and the environment allowlist), then
the task's own grants. Nothing here approves an unsandboxed command; that
always needs the owner's exact approval. Unanswered requests are declined
after a timeout, never approved.
"""

import shlex
from typing import Literal

import httpx
from pydantic import Field, field_validator, model_validator

from rocky.command_policy import matches_prefix, parse_prefix, read_only_command
from rocky.models import SAFE_HTTP_METHODS, HttpScope, Model, ToolInput

MAX_RULES = 200


class ApprovalSettings(Model):
    """Owner choices editable at runtime; environment settings supply defaults."""

    read_only_commands: bool = True
    # 0 waits for the owner indefinitely; owner waits pause the task deadline.
    timeout_minutes: int = Field(default=30, ge=0, le=10_080)


def http_origin(url: str) -> str | None:
    """scheme://host[:port] of an absolute HTTP URL, with default ports omitted."""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        return None
    host = parsed.host.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    default = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{host}" + (f":{port}" if port and port != default else "")


def _url_path(url: str) -> str | None:
    """The request path, or None when dot segments could escape a prefix."""
    try:
        raw = httpx.URL(url).raw_path.decode("ascii").partition("?")[0]
    except (httpx.InvalidURL, TypeError, ValueError, UnicodeError):
        return None
    segments = raw.split("/")
    if any(segment.lower() in {".", "..", "%2e", "%2e%2e", ".%2e", "%2e."} for segment in segments):
        return None
    if "%2f" in raw.lower() or "\\" in raw:
        return None
    return raw or "/"


class ApprovalRule(Model):
    """A persistent owner grant: a command prefix, or methods at one origin."""

    rule_id: str = Field(default="", max_length=64)
    kind: Literal["command", "http"]
    # command: leading words such as "uv run pytest".
    command: str | None = Field(default=None, max_length=1000)
    # http: methods at an exact origin, optionally under a path prefix.
    methods: list[str] = Field(default_factory=list, max_length=10)
    origin: str | None = Field(default=None, max_length=300)
    path_prefix: str = Field(default="/", max_length=1000)
    note: str = Field(default="", max_length=500)
    created_at: str = ""

    @field_validator("methods")
    @classmethod
    def upper_methods(cls, values: list[str]) -> list[str]:
        result = []
        for value in values:
            value = value.strip().upper()
            if not value.isascii() or not value.isalpha() or len(value) > 16:
                raise ValueError("HTTP methods are words such as POST or DELETE")
            if value not in SAFE_HTTP_METHODS:
                result.append(value)
        return list(dict.fromkeys(result))

    @model_validator(mode="after")
    def complete(self) -> "ApprovalRule":
        if self.kind == "command":
            if self.command is None or parse_prefix(self.command) is None:
                raise ValueError(
                    "A command rule needs plain leading words such as 'uv run pytest', "
                    "without shell operators, substitutions or wrapper commands"
                )
            self.command = shlex.join(parse_prefix(self.command))
            self.methods, self.origin, self.path_prefix = [], None, "/"
        else:
            if not self.methods:
                raise ValueError("An HTTP rule needs at least one method other than GET")
            text = (self.origin or "").strip()
            origin = http_origin(text)
            # The origin must be stated exactly: no path, query, wildcard or credentials.
            try:
                parsed = httpx.URL(text)
                exact = (
                    parsed.raw_path in {b"", b"/"}
                    and not parsed.userinfo
                    and not parsed.fragment
                    and "*" not in text
                )
            except (httpx.InvalidURL, TypeError, ValueError):
                exact = False
            if origin is None or not exact:
                raise ValueError(
                    "An HTTP rule needs an exact origin such as https://api.example.com, "
                    "without a path, wildcard or credentials"
                )
            self.origin = origin
            if not self.path_prefix.startswith("/") or _url_path(origin + self.path_prefix) is None:
                raise ValueError("The path prefix must be an absolute path such as /api/")
            self.command = None
        return self

    def matches(self, request: ToolInput, options: dict | None) -> bool:
        action = request.action
        if self.kind == "command":
            return (
                action.tool == "run_command"
                and action.sandbox
                and matches_prefix(action.content, [parse_prefix(self.command or "") or []])
            )
        if action.tool not in {"http_request", "fetch_url"} or not action.url:
            return False
        method = str((options or {}).get("method", "GET")).upper()
        path = _url_path(action.url)
        return (
            method in self.methods
            and http_origin(action.url) == self.origin
            and path is not None
            and path.startswith(self.path_prefix)
        )

    def describe(self) -> str:
        if self.kind == "command":
            return f"command {self.command}"
        return f"{'/'.join(self.methods)} {self.origin}{self.path_prefix}"


class ApprovalPolicy(Model):
    settings: ApprovalSettings = Field(default_factory=ApprovalSettings)
    rules: list[ApprovalRule] = Field(default_factory=list, max_length=MAX_RULES)
    # Prefixes from ROCKY_AUTO_APPROVE_COMMANDS.
    allowlist: list[list[str]] = Field(default_factory=list)

    def planning_context(self) -> dict:
        """What the planner may rely on running without a pause."""
        return {
            "read_only_commands": self.settings.read_only_commands,
            "owner_rules": [rule.describe() for rule in self.rules][:50],
            "approval_timeout_minutes": self.settings.timeout_minutes,
        }


def http_scope(request: ToolInput, options: dict | None) -> HttpScope | None:
    """The method and origin a task-wide approval of this request would cover."""
    action = request.action
    if action.tool not in {"http_request", "fetch_url"} or not action.url:
        return None
    origin = http_origin(action.url)
    method = str((options or {}).get("method", "GET")).upper()
    if origin is None or not method.isascii() or not method.isalpha() or len(method) > 16:
        return None
    return HttpScope(method=method, origin=origin)


def automatic_approval(request: ToolInput, options: dict | None, policy: ApprovalPolicy) -> str:
    """Name the tier that lets this sandboxed or HTTP action run, or return ''."""
    action = request.action
    if action.tool == "run_command" and not action.sandbox:
        return ""
    if action.tool == "run_command":
        if policy.settings.read_only_commands and read_only_command(action.content):
            return "read_only"
        if matches_prefix(action.content, policy.allowlist):
            return "allowlist"
    for rule in policy.rules:
        if rule.matches(request, options):
            return f"rule:{rule.rule_id}"
    if action.tool == "run_command" and matches_prefix(
        action.content, request.approved_command_prefixes
    ):
        return "task"
    scope = http_scope(request, options)
    if scope is not None and scope in request.approved_http_scopes:
        return "task"
    return ""
