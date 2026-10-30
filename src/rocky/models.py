"""Shared API and durable workflow contracts; no I/O in this module."""

import json
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from rocky.inference import ModelSettings

Criterion = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
ToolName = Literal[
    "read_file",
    "write_file",
    "list_files",
    "fetch_url",
    "http_request",
    "run_command",
    "append_file",
    "edit_file",
]
TOOL_NAMES = get_args(ToolName)
MAX_PARALLEL_ACTIONS = 8
DEFAULT_COMMAND_SECONDS = 30
MAX_COMMAND_SECONDS = 900
SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Deliverable(Model):
    path: str = Field(min_length=1, max_length=240)
    min_bytes: int = Field(default=1, ge=1, le=1_000_000)
    contains: list[Criterion] = Field(default_factory=list, max_length=20)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or "\\" in value
            or any(ord(c) < 32 for c in value)
            or any(part.startswith(".") for part in path.parts)
        ):
            raise ValueError("Deliverable must be a visible, goal-relative file path")
        return value


class ExecutionLimits(Model):
    """Task lifetime limits; explicit API contracts may remove individual limits."""

    max_steps: int | None = Field(default=100, ge=1)
    max_model_calls: int | None = Field(default=150, ge=1)
    max_tokens: int | None = Field(default=500_000, ge=1)
    timeout_seconds: int | None = Field(default=3600, ge=1)


class AutomaticExecutionLimits(Model):
    """AI estimates are bounded; the planner adds context and review reserves."""

    max_steps: int = Field(ge=1, le=100, strict=True)
    max_model_calls: int = Field(ge=1, le=150, strict=True)
    max_tokens: int = Field(ge=1, le=5_000_000, strict=True)
    timeout_seconds: int = Field(ge=1, le=3600, strict=True)


class DeliveryRequirements(Model):
    acceptance_criteria: list[Criterion] = Field(min_length=1, max_length=20)
    deliverables: list[Deliverable] = Field(min_length=1, max_length=20)
    limits: AutomaticExecutionLimits
    # Problems the owner should resolve before submitting, in their language.
    concerns: list[Criterion] = Field(default_factory=list, max_length=8)


# Folders the owner grants one task: file tools and sandboxed commands may
# write there without per-action approval. Absolute and normalized; the
# service checks they exist and are not Rocky's own or credential folders.
GrantedFolder = Annotated[str, StringConstraints(min_length=2, max_length=1024)]
MAX_FOLDERS = 8


def _normalized_folders(values: list[str]) -> list[str]:
    result = []
    for value in values:
        if "\x00" in value or not value.startswith("/") or "/../" in value + "/":
            raise ValueError("Granted folders must be absolute paths without '..'")
        result.append(value.rstrip("/") or "/")
    return list(dict.fromkeys(result))


class GoalDraft(Model):
    objective: str = Field(min_length=1, max_length=8000)
    folders: list[GrantedFolder] = Field(default_factory=list, max_length=MAX_FOLDERS)

    @field_validator("folders")
    @classmethod
    def validate_folders(cls, values: list[str]) -> list[str]:
        return _normalized_folders(values)


class GoalContract(Model):
    objective: str = Field(min_length=1, max_length=8000)
    acceptance_criteria: list[Criterion] = Field(min_length=1, max_length=20)
    deliverables: list[Deliverable] = Field(min_length=1, max_length=20)
    allowed_tools: list[ToolName] = Field(default_factory=lambda: list(TOOL_NAMES))
    allowed_domains: list[str] = Field(default_factory=list, max_length=30)
    folders: list[GrantedFolder] = Field(default_factory=list, max_length=MAX_FOLDERS)
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)
    # Decode-only metadata retained so stored goal records remain readable.
    # Current execution uses limits exclusively and ignores these flat values.
    max_steps: int | None = Field(default=None, ge=1, json_schema_extra={"deprecated": True})
    max_model_calls: int | None = Field(default=None, ge=1, json_schema_extra={"deprecated": True})
    max_output_tokens: int | None = Field(
        default=None, ge=1, json_schema_extra={"deprecated": True}
    )
    deadline_seconds: int | None = Field(default=None, ge=1, json_schema_extra={"deprecated": True})
    max_wait_seconds: int = Field(default=3600, ge=1, le=86400)
    max_no_progress: int = Field(default=3, ge=1, le=10)

    @field_validator("folders")
    @classmethod
    def validate_folders(cls, values: list[str]) -> list[str]:
        return _normalized_folders(values)

    @field_validator("allowed_domains")
    @classmethod
    def validate_domains(cls, values: list[str]) -> list[str]:
        result = []
        for value in values:
            value = value.strip().lower().rstrip(".")
            if not value or any(c in value for c in "/:@*\\"):
                raise ValueError("Use exact hostnames without a scheme, port or wildcard")
            result.append(value.encode("idna").decode("ascii"))
        return list(dict.fromkeys(result))


class DraftedContract(GoalContract):
    """A model-drafted contract with the drafter's concerns for owner review."""

    concerns: list[Criterion] = Field(default_factory=list, max_length=8)

    def contract(self) -> GoalContract:
        return GoalContract.model_validate(self.model_dump(exclude={"concerns"}))


class Action(Model):
    tool: ToolName
    path: str | None = None
    url: str | None = None
    content: str | None = None
    # Only run_command may opt out, after exact owner approval. Decode older
    # actions with isolation enabled; strings and integers cannot disable it.
    sandbox: bool = Field(default=True, strict=True)
    # run_command only. null keeps the 30-second default; longer work may ask
    # for up to MAX_COMMAND_SECONDS, or run as a background job.
    timeout_seconds: int | None = Field(default=None, ge=1, le=MAX_COMMAND_SECONDS, strict=True)
    # run_command only: start the command, return at once and keep its output
    # in the task's .rocky-jobs/ directory. Jobs stop when the task ends.
    background: bool | None = Field(default=None, strict=True)


def read_only_action(action: Action) -> bool:
    """Whether an action only observes state, so it may run concurrently.

    Downloads (an HTTP action with a path) write a local file and are excluded.
    Unparseable HTTP options are not read-only; validation reports them later.
    """
    if action.tool in {"read_file", "list_files"}:
        return True
    if action.tool not in {"fetch_url", "http_request"} or action.path is not None:
        return False
    try:
        options = json.loads(action.content) if action.content is not None else {}
    except (ValueError, TypeError, RecursionError):
        return False
    if not isinstance(options, dict):
        return False
    method = options.get("method", "GET")
    return isinstance(method, str) and method.upper() in SAFE_HTTP_METHODS


ProgressItem = Annotated[str, StringConstraints(min_length=1, max_length=240)]


class TaskProgress(Model):
    completed: list[ProgressItem] = Field(default_factory=list, max_length=12)
    pending: list[ProgressItem] = Field(default_factory=list, max_length=12)
    evidence: list[ProgressItem] = Field(default_factory=list, max_length=12)


MemoryKind = Literal["preference", "decision", "correction", "pitfall"]
MAX_MEMORY_PROPOSALS = 3


class MemoryProposal(Model):
    """A lasting fact the model suggests keeping; saved only when the owner agrees."""

    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    kind: MemoryKind
    content: str = Field(min_length=1, max_length=1000)
    keywords: str = Field(default="", max_length=200)


class Decision(Model):
    kind: Literal["act", "wait", "finish", "blocked"]
    reason: str = Field(max_length=2000)
    action: Action | None = None
    # Independent read-only actions dispatched concurrently in one step.
    actions: list[Action] | None = Field(default=None, max_length=MAX_PARALLEL_ACTIONS)
    wait_seconds: int | None = None
    handoff_summary: str | None = Field(default=None, max_length=4000)
    task_progress: TaskProgress | None = None
    # Only with finish or blocked; the owner reviews each before it is kept.
    memory_proposals: list[MemoryProposal] | None = Field(
        default=None, max_length=MAX_MEMORY_PROPOSALS
    )


class Artifact(Model):
    path: str
    size: int
    sha256: str


class Observation(Model):
    step: int
    tool: str
    success: bool
    content: str = Field(max_length=16000)
    progress_key: str = ""


class RunInput(Model):
    goal_id: str
    contract: GoalContract
    model_settings: ModelSettings | None = None
    checkpoint: "RunCheckpoint | None" = None
    hook_phases: list["Phase"] = Field(default_factory=list)


Status = Literal[
    "queued",
    "running",
    "waiting",
    "blocked",
    "succeeded",
    "failed",
    "cancelled",
    "budget_exhausted",
    "deadline_exceeded",
]


Phase = Literal["perceive", "think", "decide", "act", "reflect", "complete"]
PHASES: tuple[Phase, ...] = get_args(Phase)


class ExecutionStep(Model):
    step_id: str
    cycle: int
    phase: Phase
    status: Literal["started", "completed", "failed"] = "started"
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    started_at: str
    completed_at: str | None = None


class HttpScope(Model):
    """One method at one origin; the unit of a task-wide HTTP approval."""

    method: str = Field(pattern=r"^[A-Z]{1,16}$")
    # scheme://host[:port], lower case, without a path.
    origin: str = Field(min_length=1, max_length=300)


class PendingApproval(Model):
    fingerprint: str
    operation_id: str
    action: Action
    reason: str
    # Leading command words the owner may approve for the rest of the task.
    command_prefix: list[str] | None = None
    # Method and origin the owner may approve for the rest of the task.
    http_scope: HttpScope | None = None
    requested_at: str | None = None
    # When an unanswered request is declined; None waits for the owner.
    expires_at: str | None = None


class RunState(Model):
    goal_id: str
    revision: int = 0
    status: Status = "queued"
    reason: str = ""
    steps: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Part of input_tokens served from the provider's prompt cache.
    cached_input_tokens: int = 0
    observations: list[Observation] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    wake_at: str | None = None
    updated_at: str = ""
    cycle: int = 0
    phase: Phase | None = None
    execution_step: ExecutionStep | None = None
    handoff_summary: str = Field(default="", max_length=8000)
    handoff_is_model: bool = False
    recovery_attempts: int = 0
    task_progress: TaskProgress = Field(default_factory=TaskProgress)
    pending_approval: PendingApproval | None = None
    deadline_at: str | None = None
    approved_command_prefixes: list[list[str]] = Field(default_factory=list)
    approved_http_scopes: list[HttpScope] = Field(default_factory=list)
    # Requests declined because the owner did not answer in time. The owner may
    # still approve one later; the task can then repeat that exact action once.
    deferred_approvals: list[PendingApproval] = Field(default_factory=list, max_length=20)
    # Identities (see action_identity) of deferred actions the owner approved.
    preapproved_actions: list[str] = Field(default_factory=list, max_length=20)
    # Consecutive approval requests that expired without an answer.
    approval_timeouts: int = 0


class PlanInput(Model):
    run: RunInput
    state: RunState
    feedback: str = ""
    memory: dict[str, Any] = Field(default_factory=dict)
    # Owner approval rules in effect, added by the planning Activity.
    approvals: dict[str, Any] = Field(default_factory=dict)


class PlanResult(Model):
    decision: Decision
    input_tokens: int = 0
    output_tokens: int = 0
    # Part of input_tokens served from the provider's prompt cache.
    cached_input_tokens: int = 0


class ToolInput(Model):
    run: RunInput
    operation_id: str
    action: Action
    approved_action_fingerprints: list[str] = Field(default_factory=list)
    # Owner grants from task-scoped approvals; only owner signals add entries.
    approved_command_prefixes: list[list[str]] = Field(default_factory=list)
    approved_http_scopes: list[HttpScope] = Field(default_factory=list)
    preapproved_actions: list[str] = Field(default_factory=list)


class ToolResult(Model):
    success: bool
    content: str = Field(max_length=16000)
    progress_key: str = ""
    artifacts: list[Artifact] = Field(default_factory=list)
    error_code: str = ""
    retry_hint: str = ""
    approval_required: str = ""
    approval_command_prefix: list[str] = Field(default_factory=list)
    approval_http_scope: HttpScope | None = None
    # Seconds to wait for the owner before declining; 0 waits indefinitely.
    approval_timeout_seconds: int = 0
    # What let a gated action run without a pause, for the execution record:
    # read_only, allowlist, rule:<id>, task, deferred:<identity> or trusted.
    approved_by: str = ""
    # Files the action created or modified: goal-relative inside the workspace,
    # otherwise absolute. Commands report changes under their working directory.
    changed_paths: list[str] = Field(default_factory=list, max_length=200)
    changes_truncated: bool = False


class VerifyInput(Model):
    run: RunInput
    state: RunState


class VerificationResult(Model):
    passed: bool
    feedback: str
    artifacts: list[Artifact] = Field(default_factory=list)
    # Reviewer claims with the application's audit result for each.
    claims: list[dict[str, Any]] = Field(default_factory=list, max_length=90)
    input_tokens: int = 0
    output_tokens: int = 0
    # Part of input_tokens served from the provider's prompt cache.
    cached_input_tokens: int = 0


class PhaseInput(Model):
    run: RunInput
    state: RunState
    phase: Phase
    feedback: str = ""
    decision: Decision | None = None
    tool_result: ToolResult | None = None
    verification: VerificationResult | None = None


class PhaseResult(Model):
    feedback: str | None = Field(default=None, max_length=4000)
    handoff_summary: str | None = Field(default=None, max_length=8000)
    decision: Decision | None = None


class ClaimCheck(Model):
    """One figure or fact from a deliverable and the evidence the reviewer cites for it."""

    deliverable: str = Field(max_length=240)
    # JSON Pointer of a value from required_claims; empty for other claims.
    pointer: str = Field(max_length=300)
    value: str = Field(max_length=300)
    kind: Literal["quoted", "computed", "judgment"]
    # source:<path>, file:<path>, step:<number> or deliverable:<path>.
    location: str = Field(max_length=300)
    quote: str = Field(max_length=600)
    supported: bool


class ReviewVerdict(Model):
    # Claims come first so the verdict follows the per-claim check.
    claims: list[ClaimCheck] = Field(default_factory=list, max_length=90)
    passed: bool
    feedback: str = Field(max_length=4000)


class GoalView(Model):
    goal_id: str
    contract: GoalContract
    state: RunState
    created_at: str
    model_settings: ModelSettings | None = None
    archived: bool = False


class WakeCommand(Model):
    command_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)


class RunCheckpoint(Model):
    """Continuation state captured between rounds; full transcripts remain in SQL."""

    state: RunState
    feedback: str = ""
    no_progress: int = 0
    seen_progress: list[str] = Field(default_factory=list)
    refreshed_progress: list[str] = Field(default_factory=list)
    last_progress: str = ""
    last_batch_progress: list[str] = Field(default_factory=list)
    wake_generation: int = 0
    consumed_wake_generation: int = 0
    wake_feedback: str = ""
    seen_wake_commands: list[str] = Field(default_factory=list)
    cancel_requested: bool = False


RunInput.model_rebuild()
