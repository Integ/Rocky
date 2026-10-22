import json
from copy import deepcopy

import pytest

from rocky.memory import (
    MemoryManager,
    MemoryRecord,
    StoreMemoryProvider,
    bounded_memory,
    conservative_token_count,
    serialize_memory,
)
from rocky.models import Deliverable, GoalContract, Observation, PlanInput, RunInput, RunState


def request(summary=""):
    return PlanInput(
        run=RunInput(
            goal_id="memory-task",
            contract=GoalContract(
                objective="Write a report",
                acceptance_criteria=["Include evidence"],
                deliverables=[Deliverable(path="report.txt")],
            ),
        ),
        state=RunState(goal_id="memory-task", handoff_summary=summary),
    )


class Provider:
    def __init__(self, records):
        self.records = records
        self.requests = []

    def recall(self, value):
        self.requests.append(value.run.goal_id)
        return self.records


def test_recall_injects_both_categories_with_custom_providers():
    conversation = Provider(
        [
            MemoryRecord("conversation", "Latest user feedback", key="user-1", role="user"),
        ]
    )
    knowledge = Provider(
        [
            MemoryRecord("knowledge", "A previously verified fact", key="fact-1"),
        ]
    )
    memory = MemoryManager([conversation, knowledge]).build(request("Continue from verified file"))
    assert memory["handoff_summary"] == "Continue from verified file"
    assert memory["conversation"][0]["content"] == "Latest user feedback"
    assert memory["knowledge"][0]["content"] == "A previously verified fact"
    assert conversation.requests == knowledge.requests == ["memory-task"]
    assert memory["truncated"] is False


@pytest.mark.parametrize(
    "text",
    ["中文进展" * 2000, "x" * 8000, '\\"\n' * 2000, "🦊" * 3000],
    ids=["chinese", "ascii", "escaping", "emoji"],
)
def test_summary_priority_preserves_reserved_history_and_knowledge(text):
    provider = Provider(
        [
            MemoryRecord("conversation", "history must yield to summary"),
            MemoryRecord("knowledge", "knowledge must yield to summary"),
        ]
    )
    manager = MemoryManager([provider])
    memory = manager.build(request(text))
    assert manager.count(memory) <= 2000
    assert conservative_token_count(serialize_memory(memory)) <= 2000
    assert memory["handoff_summary"].startswith(text[:20])
    assert "MEMORY TRUNCATED" in memory["handoff_summary"]
    assert memory["conversation"][0]["content"] == "history must yield to summary"
    assert memory["knowledge"][0]["content"] == "knowledge must yield to summary"
    assert memory["truncated"] is True


def test_recent_history_and_high_priority_knowledge_win():
    provider = Provider(
        [
            MemoryRecord("conversation", "stale" * 1000, key="old", created_at="2025-01-01"),
            MemoryRecord("conversation", "recent", key="new", created_at="2026-01-01"),
            MemoryRecord("knowledge", "critical", key="critical", priority=10),
            MemoryRecord("knowledge", "low priority" * 1000, key="low", priority=-1),
        ]
    )
    memory = MemoryManager([provider]).build(request("handoff"))
    assert memory["knowledge"][0]["id"] == "critical"
    assert memory["conversation"][0]["id"] == "new"
    assert memory["handoff_summary"] == "handoff"
    assert memory["truncated"] is True


def test_state_fallback_keeps_latest_observation_and_does_not_mutate_input():
    value = request("already done")
    value.state.observations = [
        Observation(step=index, tool="read_file", success=True, content="x" * 16000)
        for index in range(30)
    ]
    before = value.model_dump()
    memory = MemoryManager().build(value)
    assert "Step 29" in memory["conversation"][0]["content"]
    assert memory["handoff_summary"] == "already done"
    assert value.model_dump() == before


def test_custom_counter_counts_envelope_not_sum_of_raw_contents():
    calls = []

    def counter(text):
        calls.append(text)
        # Deliberately charge labels/quotes as well as all source characters.
        return len(text) * 2

    manager = MemoryManager(token_counter=counter, budget=512)
    memory = manager.build(request("hi"), records=[MemoryRecord("knowledge", "x" * 2000)])
    assert manager.count(memory) <= 512
    assert calls[-1] == serialize_memory(memory)
    assert memory["knowledge"][0]["content"].endswith("is not evidence]")


def test_injected_memory_is_rebudgeted_and_handoff_cannot_be_displaced():
    value = request("Authoritative state handoff")
    value.memory = {
        "handoff_summary": "stale custom handoff",
        "conversation": [{"id": "history", "role": "tool", "content": "巨" * 20000}],
        "knowledge": [{"id": "fact", "content": "a" * 20000}],
        "token_budget": 100000,
    }
    before = deepcopy(value.memory)
    memory = bounded_memory(value)
    assert memory["handoff_summary"] == "Authoritative state handoff"
    assert conservative_token_count(serialize_memory(memory)) <= 2000
    assert value.memory == before


def test_store_adapter_and_duplicate_records():
    class Store:
        def memory_records(self, goal_id):
            assert goal_id == "memory-task"
            return [
                {"id": "message-1", "kind": "conversation", "content": "hello", "role": "user"},
                {"id": "fact-1", "kind": "knowledge", "content": "fact", "priority": 5},
            ]

    provider = StoreMemoryProvider(Store())
    memory = MemoryManager([provider, provider]).build(request())
    assert len(memory["conversation"]) == len(memory["knowledge"]) == 1


@pytest.mark.parametrize("budget", [0, 128, 2001])
def test_invalid_budget_rejected(budget):
    with pytest.raises(ValueError, match="between 256 and 2000"):
        MemoryManager(budget=budget)


def test_store_provider_recalls_legacy_state_until_durable_tool_history_exists():
    class Store:
        rows = [
            {
                "id": "task",
                "kind": "conversation",
                "content": "objective " * 1000,
                "role": "user",
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": "phase:task:1:think",
                "kind": "conversation",
                "content": "read file",
                "role": "assistant",
                "created_at": "2026-01-02T00:00:00Z",
            },
        ]

        def memory_records(self, goal_id):
            return self.rows

    store = Store()
    value = request()
    value.state.observations = [
        Observation(step=3, tool="read_file", success=True, content="legacy verified evidence")
    ]
    manager = MemoryManager([StoreMemoryProvider(store)])
    first = manager.build(value)
    assert first["conversation"][0]["content"].endswith("legacy verified evidence")

    store.rows.append(
        {
            "id": "phase:task:1:act",
            "kind": "conversation",
            "role": "tool",
            "content": "new persisted tool evidence",
            "created_at": "2026-01-03T00:00:00Z",
        }
    )
    second = manager.build(value)
    assert second["conversation"][0]["content"] == "new persisted tool evidence"
    assert "legacy verified evidence" not in serialize_memory(second)


def test_large_summary_and_history_keep_multiple_recent_results_and_knowledge():
    records = [
        MemoryRecord(
            "conversation",
            f"result-{index}: " + "x" * 10000,
            key=str(index),
            created_at=f"{index:04d}",
        )
        for index in range(12)
    ]
    records.append(MemoryRecord("knowledge", "Verified repository uses uv", key="runtime"))
    manager = MemoryManager()
    memory = manager.build(request("Earlier result: " + "old evidence " * 500), records=records)
    assert manager.count(memory) <= 2000
    assert memory["handoff_summary"].startswith("Earlier result:")
    assert [item["id"] for item in memory["conversation"][:3]] == ["11", "10", "9"]
    assert memory["knowledge"][0]["content"] == "Verified repository uses uv"


def test_store_provider_uses_bounded_retrieval_instead_of_loading_full_transcript():
    class Store:
        def recall_memory(self, goal_id, *, query, limit):
            assert goal_id == "memory-task" and limit == 24
            assert "Write a report" in query and "Missing source" in query
            return [{"id": "recent", "kind": "conversation", "role": "tool", "content": "read"}]

        def memory_records(self, goal_id):
            raise AssertionError("Full transcript must remain outside working memory")

    value = request()
    value.feedback = "Missing source"
    memory = MemoryManager([StoreMemoryProvider(Store())]).build(value)
    assert memory["conversation"][0]["id"] == "recent"


def test_malformed_injected_buckets_are_ignored():
    value = request("Keep task state")
    value.memory = {"conversation": 12, "knowledge": [None, "not a record"]}
    assert bounded_memory(value)["handoff_summary"] == "Keep task state"


def test_store_working_memory_omits_duplicate_write_payload_and_preserves_error_guidance():
    class Store:
        def memory_records(self, goal_id):
            return [
                {
                    "id": "proposal",
                    "kind": "conversation",
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "kind": "act",
                            "reason": "Write verified report",
                            "action": {
                                "tool": "write_file",
                                "path": "report.md",
                                "content": "LARGE_BODY" * 10000,
                            },
                            "handoff_summary": "RESIDENT_SUMMARY",
                            "task_progress": {"completed": ["RESIDENT_PROGRESS"]},
                        }
                    ),
                    "created_at": "2026-01-01",
                },
                {
                    "id": "failure",
                    "kind": "conversation",
                    "role": "tool",
                    "content": json.dumps(
                        {
                            "success": False,
                            "content": "x" * 16000,
                            "error_code": "outcome_unknown",
                            "retry_hint": "Inspect actual state before retrying",
                            "source_url": "https://example.com/source",
                            "exit_code": 7,
                        }
                    ),
                    "created_at": "2026-01-02",
                },
            ]

    memory = MemoryManager([StoreMemoryProvider(Store())]).build(request())
    serialized = serialize_memory(memory)
    assert "LARGE_BODY" not in serialized and "RESIDENT_SUMMARY" not in serialized
    assert "RESIDENT_PROGRESS" not in serialized and "report.md" in serialized
    assert "outcome_unknown" in memory["conversation"][0]["content"]
    assert "Inspect actual state before retrying" in memory["conversation"][0]["content"]
    assert "execution record proposal" in serialized
    assert "https://example.com/source" in serialized and "exit_code" in serialized


def test_large_tool_metadata_still_exposes_recent_raw_evidence():
    class Store:
        def memory_records(self, goal_id):
            return [
                {
                    "id": f"phase:task:{index}:act",
                    "kind": "conversation",
                    "role": "tool",
                    "created_at": str(index),
                    "content": json.dumps(
                        {
                            "success": False,
                            "content": f"RAW_RESULT_{index}: " + "data" * 3000,
                            "error_code": "command_failed",
                            "retry_hint": "repair " * 300,
                            "progress_key": "redundant-hash" * 50,
                            "artifacts": [{"path": "report.md", "sha256": "x" * 64}] * 20,
                            "source_url": "https://example.com/source",
                            "exit_code": 7,
                        }
                    ),
                }
                for index in range(3)
            ] + [{"id": "fact", "kind": "knowledge", "content": "Use the verified source"}]

    memory = MemoryManager([StoreMemoryProvider(Store())]).build(request("handoff " * 900))
    assert len(memory["conversation"]) == 3
    for index, item in enumerate(memory["conversation"]):
        assert f"RAW_RESULT_{2 - index}" in item["content"]
        assert "MEMORY TRUNCATED" in item["content"]
        assert "Next: repair" in item["content"]
    assert "redundant-hash" not in serialize_memory(memory)
    assert memory["knowledge"][0]["content"] == "Use the verified source"
