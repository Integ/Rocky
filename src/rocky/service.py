"""Worker and durable submission/command outbox dispatcher."""

import asyncio
import logging
from contextlib import suppress

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from rocky.activities import Activities
from rocky.config import Settings
from rocky.models import RunInput, WakeCommand
from rocky.planner import OpenAIPlanner
from rocky.store import Store
from rocky.tools import WorkspaceTools
from rocky.workflow import GoalWorkflow

log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, client: Client, store: Store, settings: Settings):
        self.client, self.store, self.settings = client, store, settings

    async def tick(self) -> None:
        for goal in await asyncio.to_thread(self.store.pending_goals):
            try:
                try:
                    await self.client.start_workflow(
                        "RockyGoal",
                        RunInput(goal_id=goal.goal_id, contract=goal.contract),
                        id=goal.goal_id,
                        task_queue=self.settings.task_queue,
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )
                except WorkflowAlreadyStartedError:
                    pass
                await asyncio.to_thread(self.store.mark_dispatched, goal.goal_id)
            except Exception:
                log.warning("Goal dispatch deferred; other goals will continue")
        for item in await asyncio.to_thread(self.store.pending_commands):
            try:
                goal = await asyncio.to_thread(self.store.get_goal, item["goal_id"])
                if goal and goal.state.status in (
                    "succeeded",
                    "failed",
                    "cancelled",
                    "budget_exhausted",
                    "deadline_exceeded",
                ):
                    await asyncio.to_thread(self.store.mark_command_sent, item["command_id"])
                    continue
                handle = self.client.get_workflow_handle(item["goal_id"])
                if item["kind"] == "cancel":
                    await handle.signal("cancel")
                else:
                    await handle.signal(
                        "wake_command",
                        WakeCommand(
                            command_id=item["command_id"],
                            message=item["payload"],
                        ),
                    )
                await asyncio.to_thread(self.store.mark_command_sent, item["command_id"])
            except Exception:
                log.warning("Command dispatch deferred; other commands will continue")

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                # Never log provider responses, credentials, or goal contents.
                log.warning("Outbox dispatch deferred; will retry")
            await asyncio.sleep(1)


async def run_worker(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    store = Store(settings.database_url)
    store.initialize()
    planner = OpenAIPlanner(settings)
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=pydantic_data_converter,
    )
    activities = Activities(store, WorkspaceTools(settings), planner)
    dispatcher = asyncio.create_task(Dispatcher(client, store, settings).run())
    try:
        async with Worker(
            client,
            task_queue=settings.task_queue,
            workflows=[GoalWorkflow],
            activities=[activities.plan, activities.execute, activities.verify, activities.persist],
            max_concurrent_activities=settings.max_concurrent_activities,
        ):
            await asyncio.Event().wait()
    finally:
        dispatcher.cancel()
        with suppress(asyncio.CancelledError):
            await dispatcher
        store.close()
        await planner.close()
