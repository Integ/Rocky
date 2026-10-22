"""Activity-side observers for optional durable execution checkpoints.

Handlers run in registration order, before the built-in work for their phase.
They may do I/O here, never inside the deterministic Workflow. Use the request's
execution_step.step_id to make external effects idempotent across worker retries.
"""

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable

from rocky.models import PHASES, Phase, PhaseInput, PhaseResult

PhaseHandler = Callable[[PhaseInput], PhaseResult | None | Awaitable[PhaseResult | None]]


class HarnessHooks:
    def __init__(self) -> None:
        self._handlers: dict[Phase, list[PhaseHandler]] = defaultdict(list)

    def register(self, phase: Phase, handler: PhaseHandler) -> None:
        if phase not in PHASES:
            raise ValueError("Unknown execution phase")
        self._handlers[phase].append(handler)

    @property
    def phases(self) -> list[Phase]:
        """Pass these to RunInput.hook_phases when enabling custom observers."""
        return [phase for phase in PHASES if self._handlers[phase]]

    async def run(self, request: PhaseInput) -> PhaseResult:
        combined = PhaseResult()
        context = request.model_copy(deep=True)
        for handler in self._handlers[request.phase]:
            result = (
                handler(context)
                if inspect.iscoroutinefunction(handler)
                else await asyncio.to_thread(handler, context)
            )
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                continue
            result = PhaseResult.model_validate(result)
            if result.decision is not None:
                if request.phase != "decide":
                    raise ValueError("Only the decide phase may replace a decision")
                combined.decision = result.decision
                context.decision = result.decision
            if result.feedback is not None:
                combined.feedback = result.feedback
                context.feedback = result.feedback
            if result.handoff_summary is not None:
                combined.handoff_summary = result.handoff_summary
                context.state.handoff_summary = result.handoff_summary
        return combined
