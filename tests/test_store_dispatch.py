from types import SimpleNamespace

from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from rocky.config import Settings
from rocky.inference import ModelSettings
from rocky.models import Deliverable, GoalContract, RunState
from rocky.service import Dispatcher
from rocky.store import Store


def make_goal(store):
    return store.create_goal(
        "goal-1",
        GoalContract(
            objective="Write report",
            acceptance_criteria=["Contains facts"],
            deliverables=[Deliverable(path="report.txt")],
        ),
    )


def test_goals_commands_and_snapshots_survive_store_restart(tmp_path):
    url = f"sqlite:///{tmp_path}/persistent.db"
    first = Store(url)
    first.initialize()
    make_goal(first)
    command_id = first.enqueue_command("goal-1", "cancel")
    state = RunState(
        goal_id="goal-1",
        revision=1,
        status="waiting",
        steps=2,
        updated_at="9998-01-01T00:00:00+00:00",
    )
    first.save_state(state)
    first.save_state(state)
    first.close()
    second = Store(url)
    assert second.get_goal("goal-1").state.status == "waiting"
    assert second.is_cancel_requested("goal-1")
    assert second.pending_commands()[0]["command_id"] == command_id
    assert len(second.events("goal-1")) == 1
    assert len(second.pending_goals()) == 1
    second.close()


async def test_dispatch_reconciles_start_succeeded_but_ack_was_lost(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/outbox.db")
    store.initialize()
    make_goal(store)
    store.enqueue_command("goal-1", "wake", "Continue with new evidence")
    starts, signals = [], []

    async def start(*args, **kwargs):
        starts.append(kwargs)
        # Simulates workflow already created before the dispatcher crashed.
        raise WorkflowAlreadyStartedError("goal-1", "RockyGoal")

    async def signal(*args):
        signals.append(args)

    fake_client = SimpleNamespace(
        start_workflow=start, get_workflow_handle=lambda _: SimpleNamespace(signal=signal)
    )
    await Dispatcher(fake_client, store, Settings()).tick()
    assert starts[0]["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert starts[0]["id"] == "goal-1"
    assert signals[0][0] == "wake_command"
    assert signals[0][1].message == "Continue with new evidence"
    assert store.pending_goals() == []
    assert store.pending_commands() == []
    store.close()


async def test_dispatch_uses_frozen_goal_model_settings_instead_of_current_defaults(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/models.db")
    store.initialize()
    snapshot = ModelSettings(model="gpt-6-luna", reasoning_effort="low")
    store.create_goal("pinned", make_goal(store).contract, model_settings=snapshot)
    store.mark_dispatched("goal-1")
    store.set_model_settings(ModelSettings(model="gpt-6-astra", reasoning_effort="max"))
    dispatched = []

    async def start(name, run, **kwargs):
        dispatched.append(run)

    await Dispatcher(SimpleNamespace(start_workflow=start), store, Settings()).tick()
    assert len(dispatched) == 1
    assert dispatched[0].model_settings == snapshot
    store.close()
