"""Public provider configuration; credentials are write-only and stored separately."""

import hashlib
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from rocky.inference import DEFAULT_MODEL, ModelID, ModelSettings, ReasoningEffort, model_catalog

Provider = Literal["openai", "openai-compatible", "anthropic", "ollama"]


def _model_choices(*models: tuple[str, str]) -> list[dict]:
    # An empty list means Rocky does not expose effort controls for this adapter.
    return [{"id": model, "label": label, "reasoning_efforts": []} for model, label in models]


PRESETS = [
    {
        "id": "openai",
        "preset": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": DEFAULT_MODEL,
        "requires_api_key": True,
        "models": model_catalog(),
    },
    {
        "id": "openai-compatible",
        "preset": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "requires_api_key": True,
        # https://api-docs.deepseek.com/quick_start/pricing/
        "models": _model_choices(
            ("deepseek-chat", "DeepSeek Chat"),
            ("deepseek-flash", "DeepSeek Flash"),
            ("deepseek-v4-pro", "DeepSeek V4 Pro"),
        ),
    },
    {
        "id": "openai-compatible",
        "preset": "openrouter",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "anthropic/claude-sonnet-5.5",
        "requires_api_key": True,
        # https://openrouter.ai/anthropic/claude-sonnet-5.5
        # https://openrouter.ai/openai/gpt-6.1-sol
        "models": _model_choices(
            ("anthropic/claude-sonnet-5.5", "Claude Sonnet 5.5"),
            ("openai/gpt-6.1-sol", "GPT-6.1 Sol"),
        ),
    },
    {
        "id": "anthropic",
        "preset": "anthropic",
        "label": "Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-5-5",
        "requires_api_key": True,
        # https://platform.claude.com/docs/en/about-claude/models/overview
        "models": _model_choices(
            ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
            ("claude-opus-5-5", "Claude Opus 5.5"),
            ("claude-haiku-5-5", "Claude Haiku 5.5"),
        ),
    },
    {
        "id": "ollama",
        "preset": "ollama",
        "label": "Ollama（本机）",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen3:8b",
        "requires_api_key": False,
        # https://ollama.com/library/qwen3
        "models": _model_choices(
            ("qwen3:8b", "Qwen3 8B"),
            ("qwen3:4b", "Qwen3 4B"),
            ("qwen3:14b", "Qwen3 14B"),
            ("qwen3:32b", "Qwen3 32B"),
        ),
    },
    {
        "id": "openai-compatible",
        "preset": "custom",
        "label": "自定义 OpenAI 兼容服务",
        "base_url": "",
        "model": "",
        "requires_api_key": True,
        "models": [],
    },
]


def normalize_base_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.scheme not in {"http", "https"}
        or any(ord(c) < 33 or c == "\\" for c in value.strip())
    ):
        raise ValueError("Use a valid model API base URL without credentials, query or fragment")
    if parts.scheme != "https" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Remote model APIs require HTTPS; HTTP is allowed only on loopback")
    # Also validates malformed ports without allowing urllib to repair the URL.
    _ = parts.port
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def provider_model_catalog(provider: str, base_url: str, current_model: str) -> list[dict]:
    """Return provider suggestions without losing an existing custom model.

    These are suggested model IDs, not a claim about the account's access or
    which Ollama models have been downloaded. Unknown adapters/models never
    inherit reasoning capabilities from a similarly named OpenAI model. Known
    providers retain their suggestions when configured through a custom URL;
    compatible services require an exact endpoint match to identify the vendor.
    """
    endpoint = normalize_base_url(base_url)
    preset = next(
        (
            item
            for item in PRESETS
            if item["id"] == provider
            and item["base_url"]
            and normalize_base_url(item["base_url"]) == endpoint
        ),
        None,
    )
    if preset is None and provider != "openai-compatible":
        preset = next((item for item in PRESETS if item["id"] == provider), None)
    choices = preset["models"] if preset else []
    result = [{**item, "reasoning_efforts": list(item["reasoning_efforts"])} for item in choices]
    if current_model not in {item["id"] for item in result}:
        known = (
            next((item for item in model_catalog() if item["id"] == current_model), None)
            if provider == "openai"
            else None
        )
        result.append(
            known or {"id": current_model, "label": current_model, "reasoning_efforts": []}
        )
    return result


def credential_id(provider: str, base_url: str) -> str:
    return hashlib.sha256(f"{provider}\n{base_url}".encode()).hexdigest()


class ProviderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Provider
    base_url: str = Field(min_length=1, max_length=2048)
    model: ModelID
    reasoning_effort: ReasoningEffort | None = None
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value):
        return normalize_base_url(value)

    @field_validator("api_key")
    @classmethod
    def valid_api_key(cls, value):
        if value is not None and any(
            ord(c) < 32 or ord(c) == 127 for c in value.get_secret_value()
        ):
            raise ValueError("API key contains invalid characters")
        return value

    @model_validator(mode="after")
    def valid_model_settings(self):
        ModelSettings(
            model=self.model, reasoning_effort=self.reasoning_effort, provider=self.provider
        )
        if self.clear_api_key and self.api_key and self.api_key.get_secret_value():
            raise ValueError("Choose either replace or clear the saved key")
        return self

    def snapshot(self) -> ModelSettings:
        return ModelSettings(
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            provider=self.provider,
            base_url=self.base_url,
            credential_id=credential_id(self.provider, self.base_url),
        )
