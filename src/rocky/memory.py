"""Pluggable, bounded recall for task history and knowledge.

The default counter charges one token per UTF-8 byte. This deliberately
conservative upper bound works across providers without adding a tokenizer
runtime. A provider's tokenizer can be injected when a tighter count is needed.
The budget covers the *serialized* memory object, including labels and metadata.
Handoff, recent raw records and retrieved knowledge have independent reservations;
a large summary cannot evict the most recent failure or all recalled knowledge.
The newest tool result gets most of the recent-record share: the next decision
depends on it, while older results should already be reflected in the handoff.
"""

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from rocky.models import PlanInput

# Large enough that a typical file read or command output stays visible in the
# next planning call. The planner lowers it when essential layers need the room.
MEMORY_TOKEN_BUDGET = 24_000
MAX_MEMORY_TOKEN_BUDGET = 64_000
# Shares apply to the available serialized budget, after the envelope overhead.
# Inactive layers release their share; unused space flows to the next layer.
_LAYER_SHARES = {"handoff_summary": 4, "conversation": 4, "knowledge": 2}
_RECENT_RECORDS = 3
# Fraction of the remaining conversation budget the newest tool result may use.
_LATEST_RESULT_SHARE = (3, 4)
_TRUNCATED = "\n[MEMORY TRUNCATED; omitted text is not evidence]"
MemoryKind = Literal["conversation", "knowledge"]
TokenCounter = Callable[[str], int]


def conservative_token_count(text: str) -> int:
    """Count an upper bound rather than assuming English characters per token."""
    return len(text.encode("utf-8"))


def serialize_memory(memory: dict) -> str:
    return json.dumps(memory, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class MemoryRecord:
    kind: MemoryKind
    content: str
    key: str = ""
    role: str = ""
    priority: int = 0
    created_at: str = ""


class MemoryProvider(Protocol):
    def recall(self, request: PlanInput) -> Iterable[MemoryRecord]: ...


class StateMemoryProvider:
    """Fallback for standalone planners and histories created before persistence."""

    def recall(self, request: PlanInput) -> Iterable[MemoryRecord]:
        for index, observation in enumerate(request.state.observations):
            yield MemoryRecord(
                kind="conversation",
                key=f"observation-{observation.step}-{index}",
                role="tool",
                content=(
                    f"Step {observation.step}, {observation.tool}, "
                    f"success={observation.success}: {observation.content}"
                ),
                created_at=f"{index:012d}",
            )


class StoreMemoryProvider:
    """Adapt the SQL store without coupling memory selection to its schema."""

    def __init__(self, store) -> None:
        self.store = store

    def recall(self, request: PlanInput) -> Iterable[MemoryRecord]:
        if hasattr(self.store, "recall_memory"):
            progress = getattr(request.state, "task_progress", None)
            query = "\n".join(
                [
                    request.run.contract.objective[:2000],
                    request.feedback[:1000],
                    "\n".join(progress.pending)[:1000] if progress is not None else "",
                ]
            )[:4000]
            rows = list(self.store.recall_memory(request.run.goal_id, query=query, limit=24))
        else:
            rows = list(self.store.memory_records(request.run.goal_id))
        # A matching saved decision tells us exactly which earlier results the
        # current handoff could have consumed. Never assume an arbitrary or
        # stale summary covers newer evidence, and keep the newest batch raw.
        ordered = sorted(enumerate(rows), key=lambda item: (item[1].get("created_at", ""), item[0]))
        covered = set()
        summary = request.state.handoff_summary
        if summary:
            for position, (_, row) in enumerate(ordered):
                if row.get("role") != "assistant":
                    continue
                try:
                    decision = json.loads(row["content"])
                except (ValueError, TypeError):
                    continue
                if isinstance(decision, dict) and decision.get("handoff_summary") == summary:
                    covered = {index for index, _ in ordered[:position]}
        latest = next((row for _, row in reversed(ordered) if row.get("role") == "tool"), None)
        latest_batch = self._batch_key(latest) if latest else ""
        for index, row in enumerate(rows):
            content = self._working_content(row)
            if summary and self._batch_key(row) != latest_batch:
                content = self._earlier_excerpt(row, content, covered=index in covered)
            yield MemoryRecord(
                kind=row["kind"],
                content=content,
                key=str(row.get("id", "")),
                role=row.get("role", ""),
                priority=row.get("priority", 0),
                created_at=row.get("created_at", ""),
            )
        # Older workflows persisted observations in their state/event snapshots,
        # before the message table existed. Keep that evidence on the first
        # upgraded planning call. Once phase tool messages exist, they supply
        # current history without duplicating the same results from RunState.
        if not any(row.get("role") == "tool" for row in rows):
            for record in StateMemoryProvider().recall(request):
                # The task itself is already present in run.contract; its copy
                # in the message store must not displace prior tool evidence.
                yield replace(record, priority=1)

    @staticmethod
    def _batch_key(row: dict) -> str:
        key = str(row.get("id", ""))
        # Parallel act messages append an integer to their shared phase ID.
        prefix, separator, suffix = key.rpartition(":act")
        if separator and (not suffix or (suffix.startswith(":") and suffix[1:].isdigit())):
            return prefix + separator
        if key.startswith("legacy-observation:"):
            return key.rsplit(":", 1)[0]
        return key

    @staticmethod
    def _earlier_excerpt(row: dict, original: str, *, covered: bool) -> str:
        if row.get("kind") != "conversation" or row.get("role") != "tool":
            return original
        try:
            payload = json.loads(row["content"])
        except (ValueError, TypeError):
            return original
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return original  # Failures and repair advice remain available verbatim.
        body = payload.get("content")
        if not isinstance(body, str) or len(body.encode("utf-8")) <= 1024:
            return original
        metadata = {
            key: payload[key]
            for key in ("success", "action", "source_url", "status_code", "exit_code")
            if key in payload
        }
        raw = body.encode("utf-8")
        excerpt = raw[:640].decode("utf-8", errors="ignore")
        excerpt += _TRUNCATED + "\n" + raw[-256:].decode("utf-8", errors="ignore")
        return (
            serialize_memory(metadata)
            + (
                "\nEarlier result covered by handoff; "
                if covered
                else "\nEarlier result excerpt; omitted details may require a targeted reread; "
            )
            + "full execution record "
            + str(row.get("id", ""))
            + ":\n"
            + excerpt
        )

    @staticmethod
    def _working_content(row: dict) -> str:
        """Keep durable payloads intact while avoiding redundant working copies."""
        content = row["content"]
        if row.get("kind") != "conversation" or row.get("role") not in {"assistant", "tool"}:
            return content
        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            return content
        if not isinstance(payload, dict):
            return content
        if row.get("role") == "assistant" and "kind" in payload and "reason" in payload:
            action = payload.get("action")
            actions = payload.get("actions")
            summary = {
                "kind": payload["kind"],
                "reason": payload["reason"],
                "action": {key: action.get(key) for key in ("tool", "path", "url")}
                if isinstance(action, dict)
                else None,
            }
            if isinstance(actions, list):
                summary["actions"] = [
                    {key: item.get(key) for key in ("tool", "path", "url")}
                    for item in actions
                    if isinstance(item, dict)
                ]
            summary["full_payload"] = "See execution record " + str(row.get("id", ""))
            return serialize_memory(summary)
        if row.get("role") == "tool" and "content" in payload and "success" in payload:
            # Put repair guidance before a possibly large response body, so it
            # survives clipping. Full results stay available in the SQL log.
            metadata = {"success": payload["success"]}
            metadata.update(
                error_code=payload.get("error_code", ""),
                retry_hint=payload.get("retry_hint", ""),
            )
            metadata.update(
                (key, value)
                for key, value in payload.items()
                # Hashes/exports already live in resident state. The message ID
                # locates the full audit record. Keep URL/status/exit metadata.
                if key
                not in {
                    *metadata,
                    "content",
                    "progress_key",
                    "artifacts",
                    "approval_required",
                    "approval_command_prefix",
                }
            )
            body = payload["content"]
            if not isinstance(body, str):
                return serialize_memory({**metadata, "content": body})
            # Raw text after the metadata line: the envelope escapes it once,
            # instead of twice for a JSON string nested in a JSON string.
            normalized = serialize_memory(metadata) + "\nContent:\n" + body
            if len(body) > 240:
                # Reserve bytes for both raw evidence and repair advice before
                # potentially huge metadata. Outer clipping marks omissions.
                raw = body.encode("utf-8")[:40].decode("utf-8", errors="ignore")
                hint = (
                    str(payload.get("retry_hint", ""))
                    .encode("utf-8")[:40]
                    .decode("utf-8", errors="ignore")
                )
                prefix = "Raw: " + raw + ("\nNext: " + hint if hint else "") + "\n"
                return prefix + normalized
            return normalized
        return content


class MemoryManager:
    def __init__(
        self,
        providers: Sequence[MemoryProvider] | None = None,
        *,
        token_counter: TokenCounter = conservative_token_count,
        budget: int = MEMORY_TOKEN_BUDGET,
    ) -> None:
        if not 256 <= budget <= MAX_MEMORY_TOKEN_BUDGET:
            raise ValueError(
                f"Memory budget must be between 256 and {MAX_MEMORY_TOKEN_BUDGET} tokens"
            )
        self.providers = tuple(providers) if providers is not None else (StateMemoryProvider(),)
        self.token_counter = token_counter
        self.budget = budget

    def with_budget(self, budget: int) -> "MemoryManager":
        """The same providers and counter under a different serialized budget."""
        return MemoryManager(self.providers, token_counter=self.token_counter, budget=budget)

    def count(self, memory: dict) -> int:
        count = self.token_counter(serialize_memory(memory))
        if not isinstance(count, int) or count < 0:
            raise ValueError("Token counter must return a nonnegative integer")
        return count

    def build(
        self,
        request: PlanInput,
        *,
        records: Iterable[MemoryRecord] | None = None,
        handoff_summary: str | None = None,
    ) -> dict:
        memory = {
            "handoff_summary": "",
            "conversation": [],
            "knowledge": [],
            "token_budget": self.budget,
            "truncated": False,
        }
        if self.count(memory) > self.budget:
            raise ValueError("Token counter exceeds the memory budget for an empty envelope")
        summary = (
            getattr(request.state, "handoff_summary", "")
            if handoff_summary is None
            else handoff_summary
        )
        available = (
            list(records)
            if records is not None
            else [record for provider in self.providers for record in provider.recall(request)]
        )
        # Rank inside each layer. Even pinned knowledge cannot displace recent
        # raw errors, and a long history cannot displace the handoff summary.
        available.sort(key=lambda item: (item.priority, item.created_at), reverse=True)
        memory["truncated"] = any(_TRUNCATED in record.content for record in available)
        grouped: dict[str, list[MemoryRecord]] = {"conversation": [], "knowledge": []}
        seen = set()
        for record in available:
            if record.kind not in grouped or not record.content:
                continue
            identity = (record.kind, record.key or record.content)
            if identity in seen:
                continue
            seen.add(identity)
            grouped[record.kind].append(record)

        active = [
            name
            for name in _LAYER_SHARES
            if (summary if name == "handoff_summary" else grouped[name])
        ]
        available_budget = self.budget - self.count(memory)
        total_shares = sum(_LAYER_SHARES[name] for name in active)
        reservations = {
            name: available_budget * _LAYER_SHARES[name] // total_shares for name in active
        }
        if active:
            reservations[active[-1]] += available_budget - sum(reservations.values())
        unused = 0
        for name in active:
            before = self.count(memory)
            limit = min(self.budget, before + reservations[name] + unused)
            if name == "handoff_summary":
                memory[name] = summary
                if self.count(memory) > limit:
                    memory["truncated"] = True
                    self._fit(
                        memory,
                        lambda value, name=name: memory.__setitem__(name, value),
                        summary,
                        limit,
                    )
            else:
                rows = grouped[name]
                latest = (
                    next((i for i, record in enumerate(rows) if record.role == "tool"), None)
                    if name == "conversation"
                    else None
                )
                for index, record in enumerate(rows):
                    # Reserve room for several recent records, rather than
                    # spending the entire rolling window on one huge tool body.
                    # The newest result may take most of it, but never all.
                    slots = min(_RECENT_RECORDS - index, len(rows) - index)
                    item_limit = limit
                    if index == latest and len(rows) - index > 1:
                        # Older records keep up to the remaining share, but
                        # no more than they would actually use.
                        numerator, denominator = _LATEST_RESULT_SHARE
                        share = (limit - self.count(memory)) * (denominator - numerator)
                        older = sum(
                            self.token_counter(serialize_memory(_item(row, name))) + 1
                            for row in rows[index + 1 :]
                        )
                        item_limit = limit - min(share // denominator, older)
                    elif slots > 1:
                        item_limit = self.count(memory) + (limit - self.count(memory)) // slots
                    item = _item(record, name)
                    bucket = memory[name]
                    bucket.append(item)
                    if self.count(memory) <= item_limit:
                        continue
                    memory["truncated"] = True
                    content = self._fit(
                        memory,
                        lambda value, item=item: item.__setitem__("content", value),
                        record.content,
                        item_limit,
                    )
                    if not content:
                        bucket.pop()
            unused = max(0, limit - self.count(memory))

        # Custom tokenizers may merge differently at string boundaries. Count
        # the final envelope as sent, never rely on sums of individual strings.
        if self.count(memory) > self.budget:
            raise ValueError("Memory could not fit within its serialized token budget")
        return memory

    def _fit(
        self, memory: dict, assign: Callable[[str], None], content: str, limit: int | None = None
    ) -> str:
        limit = self.budget if limit is None else min(limit, self.budget)
        assign("")
        if self.count(memory) > limit:
            return ""
        low, high = 0, len(content)
        best = ""
        while low <= high:
            midpoint = (low + high) // 2
            candidate = content[:midpoint] + _TRUNCATED
            assign(candidate)
            if self.count(memory) <= limit:
                best = candidate
                low = midpoint + 1
            else:
                high = midpoint - 1
        assign(best)
        return best


def _item(record: MemoryRecord, layer: str) -> dict:
    item = {"id": record.key[:128], "content": record.content}
    if layer == "conversation":
        item["role"] = record.role[:24]
    return item


def bounded_memory(request: PlanInput, *, manager: MemoryManager | None = None) -> dict:
    """Rebudget injected data at the model boundary; use state when absent."""
    supplied = getattr(request, "memory", None)
    manager = manager or MemoryManager()
    if not supplied:
        return manager.build(request)
    records = []
    for kind in ("conversation", "knowledge"):
        rows = supplied.get(kind, [])
        if not isinstance(rows, (list, tuple)):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("content"), str):
                continue
            records.append(
                MemoryRecord(
                    kind=kind,
                    content=row["content"],
                    key=str(row.get("id", index)),
                    role=str(row.get("role", "")),
                    # Preserve the already ranked order from custom providers.
                    priority=-index,
                )
            )
    summary = getattr(request.state, "handoff_summary", "") or supplied.get("handoff_summary", "")
    return manager.build(request, records=records, handoff_summary=str(summary))
