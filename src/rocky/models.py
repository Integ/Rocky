"""Shared API and durable workflow contracts; no I/O in this module."""

from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from rocky.inference import ModelSettings

Criterion = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
ToolName = Literal[
    "read_file", "write_file", "list_files", "fetch_url", "http_request", "run_command"
]
TOOL_NAMES = get_args(ToolName)


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
    """Owner-controlled lifetime limits; None explicitly removes a limit."""

    max_steps: int | None = Field(default=100, ge=1)
    max_model_calls: int | None = Field(default=150, ge=1)
    max_tokens: int | None = Field(default=500_000, ge=1)
    timeout_seconds: int | None = Field(default=3600, ge=1)


class GoalContract(Model):
    objective: str = Field(min_length=1, max_length=8000)
    acceptance_criteria: list[Criterion] = Field(min_length=1, max_length=20)
    deliverables: list[Deliverable] = Field(min_length=1, max_length=20)
    allowed_tools: list[ToolName] = Field(default_factory=lambda: list(TOOL_NAMES))
    allowed_domains: list[str] = Field(default_factory=list, max_length=30)
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)
    # Retained for decoding old clients and histories. Execution ignores these
    # historical values: every task has unlimited steps, calls and runtime.
    max_steps: int | None = Field(default=None, ge=1, json_schema_extra={"deprecated": True})
    max_model_calls: int | None = Field(default=None, ge=1, json_schema_extra={"deprecated": True})
    max_output_tokens: int | None = Field(
        default=None, ge=1, json_schema_extra={"deprecated": True}
    )
    deadline_seconds: int | None = Field(default=None, ge=1, json_schema_extra={"deprecated": True})
    max_wait_seconds: int = Field(default=3600, ge=1, le=86400)
    max_no_progress: int = Field(default=3, ge=1, le=10)

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


class Action(Model):
    tool: ToolName
    path: str | None = None
    url: str | None = None
    content: str | None = None


ProgressItem = Annotated[str, StringConstraints(min_length=1, max_length=240)]


class TaskProgress(Model):
    completed: list[ProgressItem] = Field(default_factory=list, max_length=12)
    pending: list[ProgressItem] = Field(default_factory=list, max_length=12)
    evidence: list[ProgressItem] = Field(default_factory=list, max_length=12)


class Decision(Model):
    kind: Literal["act", "wait", "finish", "blocked"]
    reason: str = Field(max_length=2000)
    action: Action | None = None
    wait_seconds: int | None = None
    handoff_summary: str | None = Field(default=None, max_length=4000)
    task_progress: TaskProgress | None = None


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


class PendingApproval(Model):
    fingerprint: str
    operation_id: str
    action: Action
    reason: str


class RunState(Model):
    goal_id: str
    revision: int = 0
    status: Status = "queued"
    reason: str = ""
    steps: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
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


class PlanInput(Model):
    run: RunInput
    state: RunState
    feedback: str = ""
    memory: dict[str, Any] = Field(default_factory=dict)


class PlanResult(Model):
    decision: Decision
    input_tokens: int = 0
    output_tokens: int = 0


class ToolInput(Model):
    run: RunInput
    operation_id: str
    action: Action
    approved_action_fingerprints: list[str] = Field(default_factory=list)


class ToolResult(Model):
    success: bool
    content: str = Field(max_length=16000)
    progress_key: str = ""
    artifacts: list[Artifact] = Field(default_factory=list)
    error_code: str = ""
    retry_hint: str = ""
    approval_required: str = ""


class VerifyInput(Model):
    run: RunInput
    state: RunState


class VerificationResult(Model):
    passed: bool
    feedback: str
    artifacts: list[Artifact] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


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


class ReviewVerdict(Model):
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
    """Bounded continuation state; full transcripts remain in SQL storage."""

    state: RunState
    feedback: str = ""
    no_progress: int = 0
    seen_progress: list[str] = Field(default_factory=list)
    wake_generation: int = 0
    consumed_wake_generation: int = 0
    wake_feedback: str = ""
    seen_wake_commands: list[str] = Field(default_factory=list)
    cancel_requested: bool = False
    # Operator recovery may finish the remaining phases of a saved cycle.
    resume_phase: Phase | None = None
    decision: Decision | None = None
    tool_result: ToolResult | None = None
    verification: VerificationResult | None = None
    adaptive: bool = False


RunInput.model_rebuild()
