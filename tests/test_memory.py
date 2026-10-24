import json
from copy import deepcopy

import pytest

from rocky.memory import (
    MAX_MEMORY_TOKEN_BUDGET,
    MEMORY_TOKEN_BUDGET,
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
    # A tight budget: summaries are capped at 8000 characters, below the default.
    manager = MemoryManager([provider], budget=4000)
    memory = manager.build(request(text))
    assert manager.count(memory) <= 4000
    assert conservative_token_count(serialize_memory(memory)) <= 4000
    assert memory["handoff_summary"].startswith(text[:20])
    assert "MEMORY TRUNCATED" in memory["handoff_summary"]
    assert memory["conversation"][0]["content"] == "history must yield to summary"
    assert memory["knowledge"][0]["content"] == "knowledge must yield to summary"
    assert memory["truncated"] is True


def test_recent_history_and_high_priority_knowledge_win():
    provider = Provider(
        [
            MemoryRecord("conversation", "stale" * 5000, key="old", created_at="2025-01-01"),
            MemoryRecord("conversation", "recent", key="new", created_at="2026-01-01"),
            MemoryRecord("knowledge", "critical", key="critical", priority=10),
            MemoryRecord("knowledge", "low priority" * 5000, key="low", priority=-1),
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
    assert conservative_token_count(serialize_memory(memory)) <= MEMORY_TOKEN_BUDGET
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


@pytest.mark.parametrize("budget", [0, 128, MAX_MEMORY_TOKEN_BUDGET + 1])
def test_invalid_budget_rejected(budget):
    with pytest.raises(ValueError, match=f"between 256 and {MAX_MEMORY_TOKEN_BUDGET}"):
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
    assert manager.count(memory) <= MEMORY_TOKEN_BUDGET
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


def _store_rows(results):
    """Alternating decision/tool rows, oldest first, as the SQL store records them."""
    rows = []
    for index, body in enumerate(results):
        rows.append(
            {
                "id": f"phase:task:{index}:think",
                "kind": "conversation",
                "role": "assistant",
                "content": json.dumps(
                    {
                        "kind": "act",
                        "reason": "Read the next module",
                        "action": {"tool": "read_file", "path": f"module_{index}.py"},
                    }
                ),
                "created_at": f"{2 * index:04d}",
            }
        )
        rows.append(
            {
                "id": f"phase:task:{index}:act",
                "kind": "conversation",
                "role": "tool",
                "content": json.dumps(
                    {"success": True, "content": body, "error_code": "", "retry_hint": ""},
                    ensure_ascii=False,
                ),
                "created_at": f"{2 * index + 1:04d}",
            }
        )
    return rows


class RowStore:
    def __init__(self, rows):
        self.rows = rows

    def memory_records(self, goal_id):
        return self.rows


def test_latest_tool_result_stays_whole_while_older_results_shrink_first():
    code = "\n".join(
        f'def handler_{index}(request):\n    return {{"id": {index}, "status": "ok"}}\n'
        for index in range(220)
    )
    results = [code.replace("handler", f"v{index}_handler") for index in range(6)]
    manager = MemoryManager([StoreMemoryProvider(RowStore(_store_rows(results)))])
    memory = manager.build(request("Modules 0-3 reviewed; handlers return status ok."))
    assert manager.count(memory) <= MEMORY_TOKEN_BUDGET
    latest, decision, *older = memory["conversation"]
    assert latest["role"] == "tool" and decision["role"] == "assistant"
    # Raw text, not a JSON string nested in another JSON string.
    assert results[-1] in latest["content"]
    assert "MEMORY TRUNCATED" not in latest["content"]
    previous = next(item for item in older if item["role"] == "tool")
    assert previous["id"] == "phase:task:4:act"
    assert "MEMORY TRUNCATED" in previous["content"]
    assert memory["handoff_summary"].startswith("Modules 0-3 reviewed")


def test_multibyte_file_read_fits_when_older_records_need_little_room():
    # A 19.5 KB Chinese file used to leave the planner about 600 bytes of it.
    text = "\n".join(
        f"第{index}行：这是一个需要被模型完整看到的文件内容，包含关键数据 value={index * 7}"
        for index in range(200)
    )
    manager = MemoryManager([StoreMemoryProvider(RowStore(_store_rows([text])))])
    memory = manager.build(request("开始汇总数据"))
    latest = memory["conversation"][0]
    assert text in latest["content"]
    assert "value=1393" in latest["content"]
    assert memory["truncated"] is False


def test_tool_metadata_precedes_unescaped_raw_body():
    body = 'print("ready")\n' * 40
    row = {
        "id": "phase:task:1:act",
        "kind": "conversation",
        "role": "tool",
        "content": json.dumps(
            {
                "success": False,
                "content": body,
                "error_code": "command_failed",
                "retry_hint": "Install the dependency",
                "exit_code": 1,
            }
        ),
    }
    content = StoreMemoryProvider._working_content(row)
    header, raw = content.split("\nContent:\n", 1)
    assert raw == body
    assert "command_failed" in header and '"exit_code":1' in header


def test_earlier_successes_are_excerpted_after_handoff_without_filling_the_budget():
    body = "SOURCE_HEAD\n" + "source line\n" * 1300 + "SOURCE_TAIL"
    latest = "LATEST_EVIDENCE " + "new finding " * 250
    rows = _store_rows([body] * 6 + [latest])
    summary = "Modules 0-5 inspected; prepare the report using source evidence."
    decision = json.loads(rows[-2]["content"])
    decision["handoff_summary"] = summary
    rows[-2]["content"] = json.dumps(decision)
    before = deepcopy(rows)
    manager = MemoryManager([StoreMemoryProvider(RowStore(rows))])
    memory = manager.build(request(summary))
    assert manager.count(memory) < 15000
    assert latest in memory["conversation"][0]["content"]
    old = next(item for item in memory["conversation"] if item["id"] == "phase:task:0:act")
    assert "SOURCE_HEAD" in old["content"] and "SOURCE_TAIL" in old["content"]
    assert "MEMORY TRUNCATED" in old["content"] and "covered by handoff" in old["content"]
    assert body not in serialize_memory(memory)
    assert "module_0.py" in serialize_memory(memory)
    assert memory["truncated"] is True
    assert rows == before


def test_newest_parallel_batch_and_error_guidance_survive_earlier_result_compaction():
    rows = _store_rows(["old body " * 1500])
    rows += [
        {
            "id": "failure",
            "kind": "conversation",
            "role": "tool",
            "created_at": "0002",
            "content": json.dumps(
                {
                    "success": False,
                    "content": "diagnostic " * 200,
                    "error_code": "outcome_unknown",
                    "retry_hint": "Inspect actual state before retrying",
                }
            ),
        },
        {
            "id": "correction",
            "kind": "conversation",
            "role": "user",
            "priority": 10,
            "created_at": "0003",
            "content": "Use Chinese and cite the original sources.",
        },
        *[
            {
                "id": f"phase:task:2:act:{index}",
                "kind": "conversation",
                "role": "tool",
                "created_at": f"000{index + 3}",
                "content": json.dumps(
                    {"success": True, "content": f"BATCH_{index}_" + "evidence " * 300}
                ),
            }
            for index in (1, 2)
        ],
    ]
    memory = MemoryManager([StoreMemoryProvider(RowStore(rows))]).build(request("Working notes"))
    serialized = serialize_memory(memory)
    for index in (1, 2):
        item = next(
            item for item in memory["conversation"] if item["id"] == f"phase:task:2:act:{index}"
        )
        assert f"BATCH_{index}_" + "evidence " * 300 in item["content"]
        assert "MEMORY TRUNCATED" not in item["content"]
    assert "Inspect actual state before retrying" in serialized and "outcome_unknown" in serialized
    assert "Use Chinese and cite the original sources." in serialized


def test_uncovered_old_excerpts_do_not_claim_summary_coverage():
    rows = _store_rows(["earlier source " * 200, "latest source"])
    memory = MemoryManager([StoreMemoryProvider(RowStore(rows))]).build(
        request("Unmatched summary")
    )
    older = next(item for item in memory["conversation"] if item["id"] == "phase:task:0:act")
    assert "targeted reread" in older["content"]
    assert "covered by handoff" not in older["content"]
    uncompressed = MemoryManager([StoreMemoryProvider(RowStore(rows))]).build(request())
    assert "earlier source " * 200 in serialize_memory(uncompressed)
