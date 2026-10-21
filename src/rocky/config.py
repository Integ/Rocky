"""Runtime configuration. Secrets are read from the process environment only."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROCKY_", extra="ignore")

    database_url: str = "sqlite:///./data/rocky.db"
    workspace_root: Path = Path("./data/workspaces")
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "rocky-goals"
    model: str = "gpt-4.1-mini"
    api_token: SecretStr | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    max_concurrent_activities: int = 4
