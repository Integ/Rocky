"""Shared API and durable workflow contracts; no I/O in this module."""

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Criterion = Annotated[str, StringConstraints(min_length=1, max_length=2000)]


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


class GoalContract(Model):
    objective: str = Field(min_length=1, max_length=8000)
    acceptance_criteria: list[Criterion] = Field(min_length=1, max_length=20)
    deliverables: list[Deliverable] = Field(min_length=1, max_length=20)
    allowed_tools: list[Literal["read_file", "write_file", "list_files", "fetch_url"]] = Field(
        default_factory=lambda: ["read_file", "write_file", "list_files"]
    )
    allowed_domains: list[str] = Field(default_factory=list, max_length=30)
    max_steps: int = Field(default=20, ge=1, le=200)
    max_model_calls: int = Field(default=30, ge=1, le=300)
    max_output_tokens: int = Field(default=4096, ge=256, le=16000)
    deadline_seconds: int = Field(default=86400, ge=10, le=2_592_000)
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
    tool: Literal["read_file", "write_file", "list_files", "fetch_url"]
    path: str | None = None
    url: str | None = None
    content: str | None = None


class Decision(Model):
    kind: Literal["act", "wait", "finish", "blocked"]
    reason: str = Field(max_length=2000)
    action: Action | None = None
    wait_seconds: int | None = None


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


class PlanInput(Model):
    run: RunInput
    state: RunState
    feedback: str = ""


class PlanResult(Model):
    decision: Decision
    input_tokens: int = 0
    output_tokens: int = 0


class ToolInput(Model):
    run: RunInput
    operation_id: str
    action: Action


class ToolResult(Model):
    success: bool
    content: str = Field(max_length=16000)
    progress_key: str = ""
    artifacts: list[Artifact] = Field(default_factory=list)


class VerifyInput(Model):
    run: RunInput
    state: RunState


class VerificationResult(Model):
    passed: bool
    feedback: str
    artifacts: list[Artifact] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class ReviewVerdict(Model):
    passed: bool
    feedback: str = Field(max_length=4000)


class GoalView(Model):
    goal_id: str
    contract: GoalContract
    state: RunState
    created_at: str


class WakeCommand(Model):
    command_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)
