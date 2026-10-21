"""Runtime configuration. Secrets are read from the process environment only."""

from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from rocky.inference import ModelSettings, ReasoningEffort


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROCKY_", extra="ignore")

    database_url: str = "sqlite:///./data/rocky.db"
    workspace_root: Path = Path("./data/workspaces")
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "rocky-goals"
    model: str = "gpt-4.1-mini"
    reasoning_effort: ReasoningEffort | None = None
    auth_mode: Literal["local", "token"] = "local"
    api_token: SecretStr | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    max_concurrent_activities: int = 4

    @model_validator(mode="after")
    def valid_inference_defaults(self) -> "Settings":
        ModelSettings(model=self.model, reasoning_effort=self.reasoning_effort)
        if self.auth_mode == "local" and self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Local access mode requires a loopback bind host")
        return self
