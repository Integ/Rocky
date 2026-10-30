"""The model suggests lasting memories; only the owner saves them."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rocky.activities import Activities
from rocky.api import create_app
from rocky.config import Settings
from rocky.models import (
    Decision,
    Deliverable,
    GoalContract,
    MemoryProposal,
    PlanInput,
    PlanResult,
    RunInput,
    RunState,
)
from rocky.store import Store

CONTRACT = GoalContract(
    objective="Write a report",
    acceptance_criteria=["Include evidence"],
    deliverables=[Deliverable(path="report.md")],
)
AUTH = {"Authorization": "Bearer owner-token"}
STYLE = MemoryProposal(
    key="report-style",
    kind="preference",
    content="Adele wants reports in Chinese.",
    keywords="报告",
)


@pytest.fixture
def store(tmp_path):
    database = Store(f"sqlite:///{tmp_path}/proposals.db")
    database.initialize()
    database.create_goal("task", CONTRACT)
    try:
        yield database
    finally:
        database.close()


def test_proposals_are_bounded_and_well_formed():
    with pytest.raises(ValidationError):
        MemoryProposal(key="Has Spaces", kind="preference", content="x")
    with pytest.raises(ValidationError):
        Decision(kind="finish", reason="done", memory_proposals=[STYLE] * 4)


def test_saving_a_proposal_writes_memory_and_dismissing_does_not(store):
    pitfall = MemoryProposal(key="pdf-tool", kind="pitfall", content="pdftotext is missing.")
    store.propose_memories("task", [STYLE, pitfall])
    # A retried planning call records nothing twice.
    store.propose_memories("task", [STYLE])
    pending = store.memory_proposals("task")
    assert [item["key"] for item in pending] == ["report-style", "pdf-tool"]
    assert store.memories() == []

    saved = store.resolve_memory_proposal("task:report-style", save=True, content="用中文写报告。")
    assert saved["status"] == "saved"
    memory = store.memories()[0]
    assert (memory["key"], memory["content"], memory["source"]) == (
        "report-style",
        "用中文写报告。",
        "goal:task",
    )
    store.resolve_memory_proposal("task:pdf-tool", save=False)
    assert [item["key"] for item in store.memories()] == ["report-style"]
    assert store.memory_proposals() == []
    with pytest.raises(ValueError):
        store.resolve_memory_proposal("task:pdf-tool", save=True)
    # A dismissed suggestion is not offered again by the same goal.
    store.propose_memories("task", [pitfall])
    assert store.memory_proposals() == []


def test_a_proposal_can_replace_an_existing_memory(store):
    store.put_memory("report-style", "preference", "English reports.")
    store.propose_memories("task", [STYLE])
    [pending] = store.memory_proposals()
    assert pending["replaces"] == "English reports."
    store.resolve_memory_proposal(pending["proposal_id"], save=True)
    assert store.memories()[0]["content"] == STYLE.content
    # An identical memory is not suggested at all.
    store.create_goal("other", CONTRACT)
    store.propose_memories("other", [STYLE])
    assert store.memory_proposals() == []


def test_deleting_a_goal_drops_its_suggestions(store):
    store.propose_memories("task", [STYLE])
    store.save_state(RunState(goal_id="task", revision=1, status="succeeded"))
    store.delete_goal("task")
    assert store.memory_proposals() == []


@pytest.mark.parametrize("kind", ["finish", "act"])
def test_planning_records_proposals_only_when_the_task_stops(store, kind):
    decision = Decision(kind=kind, reason="r", memory_proposals=[STYLE])

    class Planner:
        async def plan(self, request):
            return PlanResult(decision=decision)

    activities = Activities(store, SimpleNamespace(), Planner())
    request = PlanInput(
        run=RunInput(goal_id="task", contract=CONTRACT), state=RunState(goal_id="task")
    )
    asyncio.run(activities._plan(request))
    assert len(store.memory_proposals()) == (1 if kind == "finish" else 0)


def test_proposal_endpoints(store, tmp_path):
    store.propose_memories("task", [STYLE])
    settings = Settings(
        auth_mode="token", api_token="owner-token", workspace_root=tmp_path / "workspaces"
    )
    with TestClient(create_app(settings, store)) as client:
        assert client.get("/memory-proposals").status_code == 401
        listed = client.get("/memory-proposals?goal_id=task", headers=AUTH).json()
        assert [item["key"] for item in listed] == ["report-style"]
        saved = client.post("/memory-proposals/task:report-style/save", headers=AUTH)
        assert saved.status_code == 200 and saved.json()["status"] == "saved"
        again = client.post("/memory-proposals/task:report-style/dismiss", headers=AUTH)
        assert again.status_code == 409
        assert client.post("/memory-proposals/x/save", headers=AUTH).status_code == 404
        assert client.get("/memories", headers=AUTH).json()[0]["key"] == "report-style"
