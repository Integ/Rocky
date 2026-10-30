"""Worker and durable submission/command outbox dispatcher."""

import asyncio
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path

from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHistoryEventFilterType
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from rocky.activities import Activities
from rocky.config import Settings
from rocky.models import GoalView, RunCheckpoint, RunInput, RunState, WakeCommand
from rocky.planner import OpenAIPlanner
from rocky.store import EXTENSION_REASON, FOLLOW_UP_REASON, TERMINAL_STATUSES, Store
from rocky.tools import WorkspaceTools
from rocky.workflow import MODEL_QUEUE_SUFFIX, GoalWorkflow

log = logging.getLogger(__name__)
_RECONCILIATION_INTERVAL_SECONDS = 30
_RECONCILIATION_BATCH_SIZE = 100
_RECONCILIATION_RPC_TIMEOUT = timedelta(seconds=5)

_CLOSED_REASONS = {
    WorkflowExecutionStatus.FAILED: (
        "The execution service reported a workflow failure. Inspect worker health and "
        "recover the task from its saved state before resuming."
    ),
    WorkflowExecutionStatus.CANCELED: "The execution service confirmed task cancellation.",
    WorkflowExecutionStatus.TERMINATED: (
        "The execution service terminated this task. Inspect the service and recover "
        "the task from its saved state before resuming."
    ),
    WorkflowExecutionStatus.TIMED_OUT: (
        "The execution service timed out this task. Inspect worker health and recover "
        "the task from its saved state before resuming."
    ),
}
_RECOVERY_FEEDBACK = (
    "Rocky's execution engine stopped this task unexpectedly and the owner recovered it "
    "from its saved progress. An operation that was in progress may or may not have taken "
    "effect: inspect the current state before repeating it, then continue the task."
)
_EXTENSION_FEEDBACK = (
    "The task stopped at its budget or time limit, and the owner raised the limits so it can "
    "continue. Its workspace, progress and handoff are unchanged. Pick up where it stopped, "
    "spend the new budget on the remaining work, and finish as soon as the criteria are met."
)
_FOLLOW_UP_FEEDBACK = (
    "The task had ended, and the owner added a follow-up request: it is the last 追加要求 "
    "section of the objective and the last acceptance criterion. The workspace, earlier "
    "deliverables and handoff are unchanged. Do the follow-up on top of the earlier work, "
    "update the deliverables it affects, and finish when every criterion is met."
)
_REQUEUE_FEEDBACK = {
    EXTENSION_REASON: _EXTENSION_FEEDBACK,
    FOLLOW_UP_REASON: _FOLLOW_UP_FEEDBACK,
}
_HISTORY_LIMIT_REASON = (
    "The execution service stopped this task because its workflow history reached the size "
    "limit. Its workspace and saved progress are preserved. Update the worker with history "
    "rollover support, then recover this task from its saved state."
)


class Dispatcher:
    def __init__(self, client: Client, store: Store, settings: Settings):
        self.client, self.store, self.settings = client, store, settings
        self._reconciliation_cursor = ""
        self._reconciliation_lock = asyncio.Lock()

    async def reconcile(self) -> None:
        """Reconcile one bounded batch without restarting a closed execution."""
        if self._reconciliation_lock.locked():
            return
        async with self._reconciliation_lock:
            goals = await asyncio.to_thread(
                self.store.reconciliation_goals,
                self._reconciliation_cursor,
                _RECONCILIATION_BATCH_SIZE,
            )
            if not goals and self._reconciliation_cursor:
                self._reconciliation_cursor = ""
                goals = await asyncio.to_thread(
                    self.store.reconciliation_goals, "", _RECONCILIATION_BATCH_SIZE
                )
            self._reconciliation_cursor = (
                goals[-1].goal_id if len(goals) == _RECONCILIATION_BATCH_SIZE else ""
            )
            concurrency = asyncio.Semaphore(4)

            async def inspect(goal: GoalView) -> None:
                async with concurrency:
                    try:
                        await self._reconcile_goal(goal)
                    except Exception:
                        # Transport errors and raw engine failure payloads can
                        # contain request data. Leave the projection untouched.
                        log.warning("Task reconciliation deferred; other tasks will continue")

            await asyncio.gather(*(inspect(goal) for goal in goals))

    async def _reconcile_goal(self, goal: GoalView) -> None:
        current = self.client.get_workflow_handle(goal.goal_id)
        description = await current.describe(rpc_timeout=_RECONCILIATION_RPC_TIMEOUT)
        status = description.status
        if status not in (*_CLOSED_REASONS, WorkflowExecutionStatus.COMPLETED):
            return
        # Pin all outcome reads to the described run. A continued or recovered
        # execution of the same workflow ID must not inherit an old outcome.
        closed = self.client.get_workflow_handle(
            goal.goal_id, run_id=description.run_id, result_type=RunState
        )
        state = goal.state.model_copy(deep=True)
        if status == WorkflowExecutionStatus.COMPLETED:
            outcome = await closed.result(
                follow_runs=False, rpc_timeout=_RECONCILIATION_RPC_TIMEOUT
            )
            try:
                outcome = RunState.model_validate(outcome)
            except (TypeError, ValueError):
                outcome = None
            if (
                outcome is not None
                and outcome.goal_id == goal.goal_id
                and outcome.status in TERMINAL_STATUSES
            ):
                state = outcome.model_copy(deep=True)
                state.reason = (
                    "Task completion confirmed by the execution service."
                    if state.status == "succeeded"
                    else "The execution service confirmed the task's terminal outcome."
                )
            else:
                state.status = "failed"
                state.reason = (
                    "The execution finished without a valid terminal task result. "
                    "Inspect the worker and recover the task from its saved state."
                )
        else:
            state.status = "cancelled" if status == WorkflowExecutionStatus.CANCELED else "failed"
            state.reason = _CLOSED_REASONS[status]
            if status == WorkflowExecutionStatus.TERMINATED:
                history = await closed.fetch_history(
                    event_filter_type=WorkflowHistoryEventFilterType.CLOSE_EVENT,
                    rpc_timeout=_RECONCILIATION_RPC_TIMEOUT,
                )
                for event in history.events:
                    reason = event.workflow_execution_terminated_event_attributes.reason
                    # Only recognize a fixed diagnostic. Never persist engine
                    # termination reasons, details or failure stacks verbatim.
                    if "workflow history size exceeds limit" in reason.lower():
                        state.reason = _HISTORY_LIMIT_REASON
                        break
        latest = await current.describe(rpc_timeout=_RECONCILIATION_RPC_TIMEOUT)
        if latest.run_id != description.run_id or latest.status != status:
            return
        await asyncio.to_thread(
            self.store.reconcile_terminal, goal.goal_id, goal.state.revision, state
        )

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception:
                log.warning("Task reconciliation deferred; will retry")
            await asyncio.sleep(_RECONCILIATION_INTERVAL_SECONDS)

    async def tick(self) -> None:
        for goal in await asyncio.to_thread(self.store.pending_goals):
            try:
                recovery = await asyncio.to_thread(self.store.recovery_requested, goal.goal_id)
                try:
                    await self.client.start_workflow(
                        "RockyGoal",
                        RunInput(
                            goal_id=goal.goal_id,
                            contract=goal.contract,
                            model_settings=goal.model_settings,
                            # A recovered goal continues its counters, step
                            # numbers and progress from the saved projection.
                            checkpoint=RunCheckpoint(
                                state=goal.state,
                                feedback=_REQUEUE_FEEDBACK.get(
                                    goal.state.reason, _RECOVERY_FEEDBACK
                                ),
                            )
                            if recovery
                            else None,
                        ),
                        id=goal.goal_id,
                        task_queue=self.settings.task_queue,
                        # Only an owner recovery may start a closed goal again.
                        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE
                        if recovery
                        else WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )
                except WorkflowAlreadyStartedError:
                    pass
                await asyncio.to_thread(self.store.mark_dispatched, goal.goal_id)
            except Exception:
                log.warning("Goal dispatch deferred; other goals will continue")
        for item in await asyncio.to_thread(self.store.pending_commands):
            try:
                goal = await asyncio.to_thread(self.store.get_goal, item["goal_id"])
                if goal is None or goal.state.status in TERMINAL_STATUSES:
                    await asyncio.to_thread(self.store.mark_command_sent, item["command_id"])
                    continue
                handle = self.client.get_workflow_handle(item["goal_id"])
                if item["kind"] == "cancel":
                    await handle.signal("cancel")
                else:
                    await handle.signal(
                        "approval_command" if item["kind"] == "approve" else "wake_command",
                        WakeCommand(
                            command_id=item["command_id"],
                            message=item["payload"],
                        ),
                    )
                await asyncio.to_thread(self.store.mark_command_sent, item["command_id"])
            except Exception:
                log.warning("Command dispatch deferred; other commands will continue")

    async def run(self) -> None:
        reconciliation = asyncio.create_task(self._reconcile_loop())
        try:
            while True:
                try:
                    await self.tick()
                except Exception:
                    # Never log provider responses, credentials, or goal contents.
                    log.warning("Outbox dispatch deferred; will retry")
                await asyncio.sleep(1)
        finally:
            reconciliation.cancel()
            with suppress(asyncio.CancelledError):
                await reconciliation


@asynccontextmanager
async def goal_workers(
    client: Client,
    task_queue: str,
    activities: list,
    *,
    workflows: list | None = None,
    tool_slots: int | None = None,
    model_slots: int | None = None,
    **options,
):
    """Run goal workflows with tools on one queue and model calls on another.

    Both workers register every activity, so activities scheduled on the
    workflow queue by histories recorded before the split still run.
    """
    limits = {"max_concurrent_activities": tool_slots} if tool_slots else {}
    model_limits = {"max_concurrent_activities": model_slots} if model_slots else {}
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            Worker(
                client,
                task_queue=task_queue,
                workflows=workflows or [GoalWorkflow],
                activities=activities,
                **limits,
                **options,
            )
        )
        await stack.enter_async_context(
            Worker(
                client,
                task_queue=task_queue + MODEL_QUEUE_SUFFIX,
                activities=activities,
                **model_limits,
            )
        )
        yield


async def run_worker(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    store = Store(settings.database_url)
    store.initialize()
    # Settings must remain accessible on first launch before a key is saved.
    planner = OpenAIPlanner(settings, defer_client=True)
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
    )
    activities = Activities(store, WorkspaceTools(settings), planner)
    dispatcher = asyncio.create_task(Dispatcher(client, store, settings).run())
    ready_path = (
        Path(os.environ["ROCKY_WORKER_READY_FILE"])
        if os.environ.get("ROCKY_WORKER_READY_FILE")
        else None
    )
    try:
        async with goal_workers(
            client,
            settings.task_queue,
            activities.registered(),
            tool_slots=settings.max_concurrent_activities,
            model_slots=settings.max_concurrent_model_calls,
        ):
            if ready_path:
                descriptor = os.open(
                    ready_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                os.close(descriptor)
            await asyncio.Event().wait()
    finally:
        if ready_path:
            ready_path.unlink(missing_ok=True)
        dispatcher.cancel()
        with suppress(asyncio.CancelledError):
            await dispatcher
        store.close()
        await planner.close()
