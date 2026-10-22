"""Explicit, bounded live-model smoke test; not part of the default suite."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from rocky.activities import Activities
from rocky.config import Settings
from rocky.models import GoalContract, RunState
from rocky.planner import OpenAIPlanner
from rocky.service import Dispatcher
from rocky.store import Store
from rocky.tools import WorkspaceTools
from rocky.workflow import GoalWorkflow


async def run() -> dict:
    contract = GoalContract.model_validate_json(
        (Path(__file__).resolve().parent.parent / "examples/hello.json").read_text()
    )
    with TemporaryDirectory(prefix="rocky-live-") as directory:
        settings = Settings(
            database_url=f"sqlite:///{directory}/rocky.db",
            workspace_root=Path(directory) / "files",
            task_queue="rocky-live-" + uuid4().hex,
        )
        store = Store(settings.database_url)
        store.initialize()
        planner = OpenAIPlanner(settings)
        tools = WorkspaceTools(settings)
        activities = Activities(store, tools, planner)
        try:
            async with await WorkflowEnvironment.start_time_skipping(
                data_converter=pydantic_data_converter,
                download_dest_dir=directory,
                test_server_existing_path=os.environ.get("ROCKY_TEMPORAL_TEST_SERVER"),
            ) as env:
                goal_id = uuid4().hex
                store.create_goal(goal_id, contract)
                async with Worker(
                    env.client,
                    task_queue=settings.task_queue,
                    workflows=[GoalWorkflow],
                    activities=[
                        activities.plan,
                        activities.execute,
                        activities.verify,
                        activities.persist,
                        activities.phase,
                    ],
                ):
                    await Dispatcher(env.client, store, settings).tick()
                    result = await asyncio.wait_for(
                        env.client.get_workflow_handle(goal_id, result_type=RunState).result(),
                        timeout=240,
                    )
                return {
                    "status": result.status,
                    "model_calls": result.model_calls,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "verified_artifacts": [item.path for item in result.artifacts],
                }
        finally:
            await planner.close()
            store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    try:
        outcome = asyncio.run(run())
    except Exception as error:
        print(json.dumps({"status": "error", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(outcome))
    raise SystemExit(0 if outcome["status"] == "succeeded" else 1)
