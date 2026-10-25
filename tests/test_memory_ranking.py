"""Curated memory search returns the most relevant facts, not merely the newest matches."""

import pytest

from rocky import store as store_module
from rocky.store import Store


@pytest.fixture
def store(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/memory.db")
    store.initialize()
    yield store
    store.close()


def keys(results):
    return [item["key"] for item in results]


def test_rare_query_terms_outrank_terms_every_memory_shares(store):
    store.put_memory("deadline", "decision", "The quarterly report deadline is Friday.")
    for index in range(4):
        # Written later, so recency alone would list these first.
        store.put_memory(f"report-{index}", "preference", f"Report formatting rule {index}.")

    results = store.memories(query="When is the report deadline?", limit=3)

    assert keys(results)[0] == "deadline"


def test_owner_keywords_weigh_more_than_incidental_content(store):
    store.put_memory("billing", "decision", "Send totals in euros.", keywords="invoices")
    # Newer, so recency alone would list it first.
    store.put_memory(
        "style", "preference", "Mention the invoices only when asked.", keywords="tone"
    )

    assert keys(store.memories(query="invoices"))[:2] == ["billing", "style"]


def test_chinese_bigrams_rank_the_matching_fact_first(store):
    store.put_memory("报销", "decision", "差旅报销需要附上发票原件。")
    store.put_memory("周报", "preference", "周报材料使用中文撰写，并附上统计表。")

    results = store.memories(query="整理这次差旅的报销材料")

    assert keys(results)[0] == "报销"


def test_owner_priority_still_orders_before_relevance(store):
    store.put_memory("pinned", "preference", "Always cite the source report.", priority=10)
    store.put_memory("relevant", "decision", "Source report totals use net revenue.")

    assert keys(store.memories(query="net revenue source report")) == ["pinned", "relevant"]


def test_terms_late_in_a_long_query_still_match(store):
    store.put_memory("sso", "decision", "SAML login uses the corporate identity provider.")
    filler = " ".join(f"word{index:02d}" for index in range(40))

    assert keys(store.memories(query=f"{filler} saml")) == ["sso"]


def test_ranking_is_bounded_to_a_candidate_set(store, monkeypatch):
    monkeypatch.setattr(store_module, "_MEMORY_CANDIDATES", 3)
    for index in range(6):
        store.put_memory(f"note-{index}", "preference", f"Release note convention {index}.")

    results = store.memories(query="release convention", limit=10)

    assert len(results) == 3


def test_retracted_and_unmatched_memories_are_not_ranked(store):
    old = store.put_memory("old", "decision", "Deploy with the legacy script.")
    store.archive_memory("old", old["revision"])
    store.put_memory("other", "preference", "Use metric units.")

    assert store.memories(query="deploy legacy script") == []
    assert keys(store.memories(query="deploy legacy", include_inactive=True)) == ["old"]
