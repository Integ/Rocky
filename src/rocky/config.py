"""Runtime configuration; provider credentials are kept separately from task data."""

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from rocky.command_policy import parse_prefix
from rocky.inference import DEFAULT_MODEL, ModelSettings, ReasoningEffort


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROCKY_", extra="ignore")

    database_url: str = "sqlite:///./data/rocky.db"
    workspace_root: Path = Path("./data/workspaces")
    credential_path: Path = Path("./data/credentials.json")
    # Service state written by `rocky serve`. Tools and commands can neither
    # read nor change it; unset values default to the database directory.
    api_token_file: Path | None = None
    temporal_db: Path | None = None
    run_dir: Path | None = None
    log_dir: Path | None = None
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "rocky-goals"
    model: str = DEFAULT_MODEL
    reasoning_effort: ReasoningEffort | None = None
    tool_access: Literal["full", "scoped"] = "full"
    approval_mode: Literal["required", "trusted"] = "required"
    # Simple shell commands starting with one of these word prefixes (for
    # example "ls", "git status" or "uv run pytest") run without approval.
    # Commands with shell operators, substitutions or redirects never match.
    auto_approve_commands: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Simple read-only commands (ls, cat, grep, find without -exec, ...) run in
    # Seatbelt without approval. The owner can change this in the app.
    auto_approve_read_only: bool = True
    # Unanswered approval requests are declined, never approved, after this
    # many minutes; 0 waits for the owner. Owner waits do not use the
    # task deadline. Editable in the app.
    approval_timeout_minutes: int = Field(default=30, ge=0, le=10_080)
    # Per-request response caps. Reasoning tokens count toward them.
    plan_response_tokens: int = Field(default=16_384, ge=1_024, le=128_000)
    review_response_tokens: int = Field(default=12_000, ge=1_024, le=128_000)
    auth_mode: Literal["local", "token"] = "local"
    api_token: SecretStr | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    # Tool runs and state writes share these slots; model calls have their own.
    max_concurrent_activities: int = Field(default=8, ge=1, le=64)
    max_concurrent_model_calls: int = Field(default=4, ge=1, le=32)

    @field_validator("auto_approve_commands", mode="before")
    @classmethod
    def split_command_prefixes(cls, value):
        # Accept a JSON list or comma/newline separated text from the environment.
        if isinstance(value, str):
            text = value.strip()
            value = json.loads(text) if text.startswith("[") else text.replace("\n", ",").split(",")
        return value

    @field_validator("auto_approve_commands")
    @classmethod
    def valid_command_prefixes(cls, values: list[str]) -> list[str]:
        result = []
        for value in values:
            if not value.strip():
                continue
            if parse_prefix(value) is None:
                raise ValueError(
                    "Auto-approved command prefixes must be plain words such as 'git status', "
                    "without shell operators, substitutions or wrapper commands"
                )
            result.append(value.strip())
        return result

    @model_validator(mode="after")
    def valid_inference_defaults(self) -> "Settings":
        ModelSettings(model=self.model, reasoning_effort=self.reasoning_effort)
        if self.auth_mode == "local" and self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Local access mode requires a loopback bind host")
        return self

    def auto_approved_prefixes(self) -> list[list[str]]:
        return [words for value in self.auto_approve_commands if (words := parse_prefix(value))]
