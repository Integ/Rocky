import pytest
from pydantic import ValidationError

from rocky.config import Settings
from rocky.inference import ModelSettings, model_catalog
from rocky.models import RunInput


@pytest.mark.parametrize(
    ("model", "effort"),
    [
        ("gpt-4.1-mini", "high"),
        ("gpt-6-astra", "none"),
        ("gpt-6.1-sol", "minimal"),
        ("custom-model", "high"),
        ("gpt-6-luna", "ultra"),
    ],
)
def test_unsupported_model_effort_combinations_are_rejected(model, effort):
    with pytest.raises(ValidationError):
        ModelSettings(model=model, reasoning_effort=effort)


def test_custom_environment_model_has_no_guessed_reasoning_capabilities():
    catalog = model_catalog("custom-model")
    assert catalog[-1] == {"id": "custom-model", "label": "custom-model", "reasoning_efforts": []}
    assert Settings(model="custom-model").reasoning_effort is None
    with pytest.raises(ValidationError):
        Settings(model="custom-model", reasoning_effort="high")


def test_catalog_mutations_cannot_change_validation_or_future_responses():
    catalog = model_catalog()
    sol = next(item for item in catalog if item["id"] == "gpt-6.1-sol")
    sol["reasoning_efforts"].append("none")
    fresh = next(item for item in model_catalog() if item["id"] == "gpt-6.1-sol")
    assert "none" not in fresh["reasoning_efforts"]
    with pytest.raises(ValidationError):
        ModelSettings(model="gpt-6.1-sol", reasoning_effort="none")


def test_legacy_run_input_without_model_settings_still_decodes():
    request = RunInput.model_validate(
        {
            "goal_id": "legacy",
            "contract": {
                "objective": "Write report",
                "acceptance_criteria": ["Contains facts"],
                "deliverables": [{"path": "report.md"}],
            },
        }
    )
    assert request.model_settings is None
