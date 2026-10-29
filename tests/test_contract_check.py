from rocky.contract_check import budget_warnings, contract_warnings
from rocky.models import GoalContract


def contract(**limits):
    return GoalContract(
        objective="整理材料",
        acceptance_criteria=["逐项回答"],
        deliverables=[{"path": "answers.md"}],
        limits=limits,
    )


def test_budget_that_fits_its_call_limit_has_no_warnings():
    assert (
        budget_warnings(
            contract(max_steps=12, max_model_calls=20, max_tokens=704_000, timeout_seconds=900)
        )
        == []
    )


def test_each_short_limit_is_reported_with_a_suggestion():
    warnings = budget_warnings(
        contract(max_steps=40, max_model_calls=40, max_tokens=400_000, timeout_seconds=1200)
    )
    assert [(item.code, item.current, item.suggested) for item in warnings] == [
        ("model_calls_low", 40, 48),
        ("token_budget_low", 400_000, 40 * 32_000 + 64_000),
        ("time_limit_low", 1200, 40 * 45),
    ]


def test_disabled_limits_are_not_second_guessed():
    assert (
        budget_warnings(
            contract(max_steps=None, max_model_calls=None, max_tokens=1, timeout_seconds=1)
        )
        == []
    )
    # Without a call limit, the step limit plus review calls bounds the run.
    warnings = budget_warnings(
        contract(max_steps=10, max_model_calls=None, max_tokens=None, timeout_seconds=60)
    )
    assert [(item.code, item.suggested) for item in warnings] == [("time_limit_low", 18 * 45)]


def test_concerns_come_first_and_keep_the_drafters_words():
    warnings = contract_warnings(
        contract(max_steps=12, max_model_calls=20, max_tokens=704_000, timeout_seconds=900),
        ["需要已批准的 Q3 目标"],
    )
    assert [(item.code, item.message) for item in warnings] == [("concern", "需要已批准的 Q3 目标")]
