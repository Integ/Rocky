from rocky.models import Deliverable, GoalContract


def contract(**legacy_fields):
    return GoalContract(
        objective="Produce a verified report",
        acceptance_criteria=["The report contains the requested evidence"],
        deliverables=[Deliverable(path="report.md")],
        **legacy_fields,
    )


def test_tasks_have_explicit_default_limits_and_legacy_fields_are_unused():
    data = contract().model_dump(mode="json")
    for field in ("max_steps", "max_model_calls", "max_output_tokens", "deadline_seconds"):
        assert data[field] is None
    assert data["limits"] == {
        "max_steps": 100,
        "max_model_calls": 150,
        "max_tokens": 500000,
        "timeout_seconds": 3600,
    }


def test_legacy_execution_limits_remain_decodable_as_historical_metadata():
    legacy = {
        "max_steps": 1,
        "max_model_calls": 1,
        "max_output_tokens": 256,
        "deadline_seconds": 10,
    }
    data = contract(**legacy).model_dump(mode="json")
    assert {key: data[key] for key in legacy} == legacy
