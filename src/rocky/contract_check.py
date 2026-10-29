"""Pre-submission checks of a goal contract's budget; no I/O in this module.

The model that drafts a contract also lists content concerns (criteria that
cannot be verified, inputs Rocky cannot obtain). These checks cover what code
can decide reliably: whether the limits leave room for the work they allow.
Warnings are advice for the owner; they never block a submission.
"""

from typing import Literal

from rocky.models import ExecutionLimits, GoalContract, Model

# Every planning call resends the contract, state and memory; see
# OpenAIPlanner._automatic_limits, which applies the same reserves.
TOKENS_PER_CALL = 32_000
SETUP_TOKENS = 64_000
# Calls beyond the step limit for independent review and repairs.
REVIEW_CALLS = 8
# Typical active seconds per model call, excluding owner waits.
SECONDS_PER_CALL = 45


class ContractWarning(Model):
    code: Literal["concern", "model_calls_low", "token_budget_low", "time_limit_low"]
    # Model-written text for a concern, in the owner's language; empty otherwise.
    message: str = ""
    current: int | None = None
    suggested: int | None = None


def _calls(limits: ExecutionLimits) -> int | None:
    if limits.max_model_calls is not None:
        return limits.max_model_calls
    return None if limits.max_steps is None else limits.max_steps + REVIEW_CALLS


def budget_warnings(contract: GoalContract) -> list[ContractWarning]:
    limits = contract.limits
    warnings = []
    if (
        limits.max_steps is not None
        and limits.max_model_calls is not None
        and limits.max_model_calls < limits.max_steps + REVIEW_CALLS
    ):
        warnings.append(
            ContractWarning(
                code="model_calls_low",
                current=limits.max_model_calls,
                suggested=limits.max_steps + REVIEW_CALLS,
            )
        )
    calls = _calls(limits)
    if calls is not None and limits.max_tokens is not None:
        needed = calls * TOKENS_PER_CALL + SETUP_TOKENS
        if limits.max_tokens < needed:
            warnings.append(
                ContractWarning(
                    code="token_budget_low", current=limits.max_tokens, suggested=needed
                )
            )
    if calls is not None and limits.timeout_seconds is not None:
        needed = calls * SECONDS_PER_CALL
        if limits.timeout_seconds < needed:
            warnings.append(
                ContractWarning(
                    code="time_limit_low", current=limits.timeout_seconds, suggested=needed
                )
            )
    return warnings


def contract_warnings(contract: GoalContract, concerns: list[str] = ()) -> list[ContractWarning]:
    return [
        *(ContractWarning(code="concern", message=text) for text in concerns),
        *budget_warnings(contract),
    ]
