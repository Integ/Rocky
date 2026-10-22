"""Model capabilities and serializable, per-goal inference settings.

This catalog describes API capabilities, not the current account's access.
Keep it in sync with the official model pages and reasoning guide.
Retain support for persisted model/effort pairs when updating this catalog:
old Temporal histories must remain decodable after an upgrade.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_serializer, model_validator

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
ModelID = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*$")
]

_THINKING = ("low", "medium", "high", "xhigh", "max")
_CATALOG = (
    ("gpt-4.1-mini", "GPT-4.1 mini", ()),
    ("gpt-4.1", "GPT-4.1", ()),
    ("gpt-6-luna", "GPT-6 Luna", ("none", *_THINKING)),
    ("gpt-6-sol", "GPT-6 Sol", ("none", *_THINKING)),
    ("gpt-6.1-sol", "GPT-6.1 Sol", _THINKING),
    ("gpt-6-astra", "GPT-6 Astra", _THINKING),
)


def model_catalog(configured_model: str = "gpt-4.1-mini") -> list[dict]:
    """Return fresh objects, including a legacy/custom deployment's model.

    An unknown configured model remains usable without an effort parameter;
    we do not guess that a custom model supports reasoning controls.
    """
    result = [
        {"id": model, "label": label, "reasoning_efforts": list(efforts)}
        for model, label, efforts in _CATALOG
    ]
    if configured_model not in {item["id"] for item in result}:
        result.append({"id": configured_model, "label": configured_model, "reasoning_efforts": []})
    return result


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: ModelID
    reasoning_effort: ReasoningEffort | None = None
    provider: Literal["openai", "openai-compatible", "anthropic", "ollama"] | None = None
    base_url: str | None = None
    credential_id: str | None = None

    @model_serializer(mode="wrap")
    def serialize_settings(self, handler):
        data = handler(self)
        # Preserve the wire format of existing histories and API clients.
        return {
            key: value
            for key, value in data.items()
            if value is not None or key in {"model", "reasoning_effort"}
        }

    @model_validator(mode="after")
    def supported_reasoning(self) -> "ModelSettings":
        if self.reasoning_effort is not None:
            if self.provider not in (None, "openai"):
                raise ValueError(
                    "Reasoning controls are only available for the built-in OpenAI catalog"
                )
            supported = next((efforts for model, _, efforts in _CATALOG if model == self.model), ())
            if self.reasoning_effort not in supported:
                raise ValueError("This model does not support the selected reasoning effort")
        return self
