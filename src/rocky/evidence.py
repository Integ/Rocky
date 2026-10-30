"""Evidence for the independent reviewer, collected by the application.

The planner's own claims are not evidence. This assembles what actually
happened from the execution record and the files on disk:

- sources: files the task read, or found in its workspace and left unchanged,
  with size, line count and whether the task read each one completely;
- changed_files: files created or modified besides the deliverables, as a diff
  against the content the task first read when one exists;
- deliverables: which recorded actions wrote each deliverable, in order;
- actions: the chronological tool actions with output excerpts and changes.

The planner fits the whole envelope into the review request. The reviewer
returns one claim per figure or fact with a cited location and a verbatim
quote; audit_review then checks every claim against this evidence, so a pass
does not rest on the reviewer's judgment of figures alone.
"""

import difflib
import json
import re

from rocky.models import RunInput
from rocky.tools import WorkspaceTools

# Per-file and per-output bounds before model-boundary fitting.
_FILE_LIMIT = 60_000
# Outputs and command text are kept long enough to audit quoted values.
_OUTPUT_LIMIT = 4_000
_COMMAND_LIMIT = 2_000
_MAX_FILES = 30
_MAX_ACTIONS = 80
_CLIPPED = "\n[truncated]"
_OUTPUT_TOOLS = {"run_command", "fetch_url", "http_request", "list_files", "browse"}


def _excerpt(value, limit: int) -> str:
    text = value if isinstance(value, str) else str(value or "")
    return text if len(text) <= limit else text[:limit] + "\n[excerpt]"


def _diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{path} (as first read)",
            tofile=f"{path} (final)",
        )
    )


def _record_action(
    run: RunInput,
    tools: WorkspaceTools,
    step: dict,
    action: dict,
    result: dict,
    actions: list[dict],
    reads: dict[str, str],
    originals: dict[str, str],
    writers: dict[str, list[str]],
) -> None:
    tool = action.get("tool")
    label = f"step {step.get('cycle')} {tool}"
    target = action.get("content") if tool == "run_command" else action.get("path")
    entry = {
        "step": step.get("cycle"),
        "tool": tool,
        "target": _excerpt(target or action.get("url") or "", _COMMAND_LIMIT),
        "success": bool(result.get("success")),
    }
    if tool in _OUTPUT_TOOLS or not entry["success"]:
        entry["output"] = _excerpt(result.get("content"), _OUTPUT_LIMIT)
    if tool == "read_file" and entry["success"] and action.get("path"):
        path = tools.display_path(run.goal_id, action["path"])
        content = str(result.get("content") or "")
        complete = not content.endswith(_CLIPPED)
        entry["read"] = "complete" if complete else "partial: only the start was returned"
        if reads.get(path) != "complete":
            reads[path] = "complete" if complete else "partial"
        # The content before the task changed it, for a reviewable diff.
        if complete and path not in writers:
            originals.setdefault(path, content)
    changed = [str(item) for item in result.get("changed_paths") or []]
    if changed:
        entry["changed"] = changed[:50]
    if result.get("changes_truncated"):
        entry["changes_truncated"] = True
    for path in changed:
        writers.setdefault(path, []).append(label)
    actions.append(entry)


def collect_review_evidence(run: RunInput, steps: list[dict], tools: WorkspaceTools) -> dict:
    deliverables = {
        tools.display_path(run.goal_id, item.path) for item in run.contract.deliverables
    }
    actions: list[dict] = []
    reads: dict[str, str] = {}
    originals: dict[str, str] = {}
    writers: dict[str, list[str]] = {}
    for step in steps:
        if step.get("phase") != "act":
            continue
        decision = (step.get("input") or {}).get("decision") or {}
        output = step.get("output") or {}
        # A parallel read batch records each action next to its own result.
        pairs = (
            [
                (item.get("action"), item.get("result"))
                for item in output["results"]
                if isinstance(item, dict)
            ]
            if isinstance(output.get("results"), list)
            else [(decision.get("action"), output.get("result"))]
        )
        for action, result in pairs:
            if isinstance(action, dict) and isinstance(result, dict):
                _record_action(run, tools, step, action, result, actions, reads, originals, writers)

    sources = []
    seen = set(writers) | deliverables
    for path in [*reads, *tools.workspace_files(run)]:
        if path in seen or len(sources) >= _MAX_FILES:
            continue
        seen.add(path)
        item = tools.describe_file(run, path, _FILE_LIMIT)
        item["read_by_task"] = reads.get(path, "not read with read_file")
        sources.append(item)

    changed_files = []
    for path, by in writers.items():
        if path in deliverables or len(changed_files) >= _MAX_FILES:
            continue
        item = tools.describe_file(run, path, _FILE_LIMIT)
        item["changed_by"] = by
        if path in originals and "content" in item and not item.get("excerpt"):
            item["diff"] = _diff(path, originals[path], item.pop("content")) or "(unchanged)"
        changed_files.append(item)

    return {
        "sources": sources,
        "changed_files": changed_files,
        "deliverables": {
            path: writers.get(path, ["not written by any recorded tool action"])
            for path in sorted(deliverables)
        },
        "actions": actions[-_MAX_ACTIONS:],
        "earlier_actions_omitted": max(0, len(actions) - _MAX_ACTIONS),
    }


# --- Structured review: required values and the claim audit -----------------

_NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_SOLE_NUMBER = re.compile(r"^\s*[$€£]?\s*-?(\d+(?:,\d{3})*(?:\.\d+)?)\s*%?\s*$")
_PREFIX = re.compile(
    r"^\s*(source|file|step|deliverable|artifact|output|action)s?\s*[:#]?\s*(.+?)\s*$",
    re.IGNORECASE,
)
_FOLD = str.maketrans(
    {"—": "-", "–": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "*": None, "`": None}
)
_CHECKLIST_LIMIT = 60
_SHORT_TEXT = 40


def _normal(text) -> str:
    return " ".join(str(text).translate(_FOLD).casefold().split())


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    match = _SOLE_NUMBER.match(str(value))
    return float(match.group(1).replace(",", "")) if match else None


def _numbers(text) -> list[float]:
    return [float(item.replace(",", "")) for item in _NUMBER.findall(str(text))]


def _has_number(number: float, text) -> bool:
    # Two-decimal rounding of a computed value still matches its source.
    return any(abs(abs(number) - candidate) <= 0.006 for candidate in _numbers(text))


def _key_tokens(value: str) -> list[str]:
    """Names, codes, dates and numbers that a paraphrase must keep verbatim."""
    return [token for token in re.findall(r"[\w./:-]+", value) if _identifier(token)]


def _value_in(value, text) -> bool:
    """The value, or for a paraphrase every key token of it, appears in text."""
    number = _number(value)
    if number is not None:
        return _has_number(number, text)
    if _normal(value) and _normal(value) in _normal(text):
        return True
    tokens = _key_tokens(str(value))
    haystack = _normal(text)
    return bool(tokens) and all(_normal(token) in haystack for token in tokens)


def _unescape(quote: str) -> str:
    # Reviewers read evidence inside JSON, so quotes may keep its escapes.
    return quote.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')


def _quote_in(quote: str, text: str) -> bool:
    """Every line of the quote, split further at ellipses, appears in order.

    Quoting code or prose, reviewers skip lines; each line they keep must exist.
    """
    haystack, position = _normal(text), 0
    fragments = [_normal(part) for part in re.split(r"\.\.\.|…|\n", _unescape(quote))]
    if not any(fragments):
        return False
    for fragment in filter(None, fragments):
        found = haystack.find(fragment, position)
        if found < 0:
            return False
        position = found + len(fragment)
    return True


def _identifier(value: str) -> bool:
    """Names, codes, IDs and dates: values a source would state verbatim."""
    return bool(re.search(r"[A-Z0-9]", value))


def _checkable(value) -> bool:
    """Numbers, names, codes, dates and paths. Plain lowercase labels such as
    statuses are classifications: review judges them, they need no citation."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int | float):
        return True
    text = value.strip() if isinstance(value, str) else ""
    return 0 < len(text) <= _SHORT_TEXT and (_identifier(text) or "/" in text)


def review_checklist(deliverables: dict[str, str]) -> list[dict]:
    """Every number and short string in JSON deliverables, which review must check.

    A value in an object inside an array is a record field; its context lists
    identifier-like sibling values, one of which the supporting quote must name.
    """
    items: list[dict] = []

    def visit(path: str, pointer: str, value, record: dict | None, key: str) -> None:
        if isinstance(value, dict):
            for name, child in value.items():
                escaped = str(name).replace("~", "~0").replace("/", "~1")
                visit(
                    path,
                    f"{pointer}/{escaped}",
                    child,
                    value if record == "element" else None,
                    name,
                )
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(path, f"{pointer}/{index}", child, "element", str(index))
        elif _checkable(value):
            item = {"deliverable": path, "pointer": pointer, "value": value}
            if isinstance(record, dict):
                item["context"] = [
                    str(other)
                    for name, other in record.items()
                    if name != key and _checkable(other) and _identifier(str(other))
                ][:3]
            items.append(item)

    for path, text in deliverables.items():
        if path.lower().endswith(".json"):
            try:
                visit(path, "", json.loads(text), None, "")
            except ValueError:
                continue
    return items[:_CHECKLIST_LIMIT]


def _resolve(location: str, index: dict[str, str]) -> list[str]:
    """Map a cited location, written loosely, to evidence keys.

    Reviewers write source:x, file:x, a bare path, artifact:x or several
    locations at once; deliverables also resolve when cited as files.
    """
    keys = []
    for part in re.split(r"\s*(?:,|;|\band\b)\s*", location or ""):
        match = _PREFIX.match(part)
        kind, rest = (match.group(1).lower(), match.group(2)) if match else ("", part.strip())
        if kind in {"step", "output", "action"} or (not kind and rest.isdigit()):
            number = re.search(r"\d+", rest)
            key = f"step:{number.group()}" if number else ""
            if key in index:
                keys.append(key)
            continue
        path = rest.removeprefix("./")
        candidates = [f"{prefix}:{path}" for prefix in ("deliverable", "source", "file")]
        found = [key for key in candidates if key in index]
        if not found:
            # A path cited relative to a subdirectory, if it names one file.
            suffix = [
                key for key in index if not key.startswith("step:") and key.endswith("/" + path)
            ]
            found = suffix[:1] if len(suffix) == 1 else []
        keys.extend(found[:1])
    return keys


def _evidence_index(
    evidence: dict, deliverables: dict[str, str], files: dict[str, str]
) -> dict[str, str]:
    index: dict[str, str] = {}
    for item in evidence.get("sources", []):
        index[f"source:{item['path']}"] = item.get("content", "")
    for item in evidence.get("changed_files", []):
        # Quotes may come from the diff or from the file as it is now.
        index[f"file:{item['path']}"] = (
            item.get("diff", "") + "\n" + files.get(item["path"], item.get("content", ""))
        )
    for action in evidence.get("actions", []):
        key = f"step:{action.get('step')}"
        index[key] = (
            index.get(key, "") + f"\n{action.get('target', '')}\n{action.get('output', '')}"
        )
    for path, text in deliverables.items():
        index[f"deliverable:{path}"] = text
    return index


def _statements(evidence: dict, files: dict[str, str]) -> list[tuple[str, str]]:
    """Lines of evidence that state facts: file content and action outputs, not commands."""
    lines = []
    for item in evidence.get("sources", []):
        lines += [(f"source:{item['path']}", line) for line in item.get("content", "").splitlines()]
    for item in evidence.get("changed_files", []):
        text = files.get(item["path"], item.get("content", ""))
        lines += [(f"file:{item['path']}", line) for line in text.splitlines()]
    for action in evidence.get("actions", []):
        lines += [
            (f"step:{action.get('step')}", line)
            for line in str(action.get("output", "")).splitlines()
        ]
    return [(location, line) for location, line in lines if line.strip()]


def _distinctive(value) -> bool:
    """Small numbers and very short text appear everywhere; they prove nothing alone."""
    number = _number(value)
    if number is not None:
        return len(str(value).replace(".", "").lstrip("-0")) >= 3
    return len(str(value).strip()) >= 3


def verify_checklist(checklist: list[dict], evidence: dict, files: dict[str, str]) -> dict:
    """Values the application can verify itself, keyed by (deliverable, pointer).

    A value is computed when the deliverable was last written by a command that
    does not state the value literally. A distinctive value is stated when one
    line of a source or an action output contains it, together with an
    identifier of its record for record fields. Everything else needs a claim.
    """
    actions = {action.get("step"): action for action in evidence.get("actions", [])}
    statements = _statements(evidence, files)
    support: dict[tuple[str, str], str] = {}
    for item in checklist:
        key, value = (item["deliverable"], item["pointer"]), item["value"]
        context = item.get("context", [])
        last = (evidence.get("deliverables", {}).get(item["deliverable"]) or [""])[-1].split()
        if len(last) == 3 and last[2] == "run_command" and last[1].isdigit():
            command = actions.get(int(last[1]), {}).get("target", "")
            if not (_distinctive(value) and _value_in(value, command)):
                support[key] = f"computed by step {last[1]} run_command"
                continue
        if not _distinctive(value):
            continue
        for location, line in statements:
            if _value_in(value, line) and (
                not context or any(_normal(term) in _normal(line) for term in context)
            ):
                support[key] = f"stated in {location}: {line.strip()[:200]}"
                break
    # A cited file is supported when it states the answer the citation is paired
    # with: a record sibling, or the value under the same key in a parallel
    # object (answers/q1 with sources/q1).
    files_index = {
        f"source:{item['path']}": item.get("content", "") for item in evidence.get("sources", [])
    }
    files_index |= {f"file:{path}": text for path, text in files.items()}
    by_key: dict[str, list[dict]] = {}
    for item in checklist:
        by_key.setdefault(item["pointer"].rsplit("/", 1)[-1], []).append(item)
    for item in checklist:
        key, value = (item["deliverable"], item["pointer"]), item["value"]
        if key in support or not isinstance(value, str) or "/" not in value and "." not in value:
            continue
        paired = [
            other["value"]
            for other in by_key[item["pointer"].rsplit("/", 1)[-1]]
            if other is not item
        ] + item.get("context", [])
        for location in _resolve(value, files_index):
            lines = files_index[location].splitlines()
            # Unlike a free-standing value, a paired answer may be small: the
            # question is only whether the cited file states it.
            stated = next(
                (other for other in paired if any(_value_in(other, line) for line in lines)),
                None,
            )
            if stated is not None:
                support[key] = f"cited {location} states {json.dumps(stated, ensure_ascii=False)}"
                break
    return support


def _audit_claim(claim: dict, item: dict | None, index, corpus, actions, writers) -> str:
    if not claim.get("supported"):
        return "the review found no supporting evidence"
    keys = _resolve(claim.get("location", ""), index)
    if not keys:
        return f"the cited location {claim.get('location')!r} is not in the evidence"
    if item is not None:
        # A deliverable cannot be the evidence for the values it reports.
        keys = [key for key in keys if not key.startswith("deliverable:")]
        if not keys:
            return "a reported value needs evidence other than the deliverable itself"
    kind, quote = claim.get("kind"), claim.get("quote", "")
    keys = [key for key in keys if _quote_in(quote, index[key])]
    if not keys:
        return "the quoted text does not appear at the cited location"
    value = item["value"] if item is not None else claim.get("value", "")
    if item is not None and isinstance(value, str) and ("/" in value or "." in value):
        # A citation is supported by quoting the very file it cites.
        files = {key: text for key, text in index.items() if key.startswith(("source:", "file:"))}
        if set(_resolve(value, files)) & set(keys):
            return ""
    if kind == "computed":
        commands = [
            actions[key] for key in keys if actions.get(key, {}).get("tool") == "run_command"
        ]
        if not commands:
            return "computed values must come from a command the task ran; compute it and keep the output"
        action = commands[0]
        if _distinctive(value) and _value_in(value, action.get("target", "")):
            return "the command states this value literally instead of computing it"
        wrote = writers.get(claim.get("deliverable"), [])[-1:] == [
            f"step {action.get('step')} run_command"
        ]
        if not wrote and not _value_in(value, action.get("output", "")):
            return f"the output of step {action.get('step')} does not show this value"
        return ""
    if item is None:
        # Claims about the artifact itself: the quote must exist; numbers in it too.
        if any(not _has_number(number, quote) for number in _numbers(value)) and kind == "quoted":
            return "the quoted text does not contain the numbers in this value"
        return ""
    if kind == "judgment":
        if _number(value) is not None:
            return "a number must be quoted from evidence or computed by a command, not judged"
        if _identifier(str(value)) and _normal(value) in corpus:
            return "this value appears in the evidence; quote the text that states it"
        if any(not _has_number(number, quote) for number in _numbers(value)):
            return "the quoted text does not contain the numbers in this value"
    elif not _value_in(value, quote):
        return "the quoted text does not contain this value"
    context = item.get("context", [])
    lines = _unescape(quote).splitlines() or [quote]
    # Lines may be quoted from different places: one line must hold both.
    tied = any(
        any(_normal(term) in _normal(line) for term in context)
        and (kind == "judgment" or _value_in(value, line))
        for line in lines
    )
    if context and not tied:
        return f"the quoted text does not tie this value to its record ({context[0]})"
    return ""


def audit_review(
    claims: list[dict],
    evidence: dict,
    deliverables: dict[str, str],
    checklist: list[dict],
    *,
    verified: dict | None = None,
    files: dict[str, str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Check claims against the collected evidence; return audited claims and problems.

    Values in verified need no claim, but a claim marking one unsupported is a
    dispute and still fails the review. Every other listed value needs a claim
    that survives the audit.
    """
    verified, files = verified or {}, files or {}
    index = _evidence_index(evidence, deliverables, files)
    corpus = _normal("\n".join(line for _, line in _statements(evidence, files)))
    actions = {f"step:{action.get('step')}": action for action in evidence.get("actions", [])}
    writers = evidence.get("deliverables", {})
    required = {(item["deliverable"], item["pointer"]): item for item in checklist}
    audited, problems, covered = [], [], set()
    for claim in claims:
        key = (claim.get("deliverable"), claim.get("pointer"))
        item = required.get(key) if claim.get("pointer") else None
        if item is not None and key in verified and claim.get("supported"):
            audited.append({**claim, "problem": "", "verified_by": verified[key]})
            covered.add(key)
            continue
        problem = _audit_claim(claim, item, index, corpus, actions, writers)
        if item is not None and key in verified:
            problem = "the review disputes a value the application found in the evidence"
        audited.append({**claim, "problem": problem})
        if item is not None and not problem:
            covered.add(key)
        if problem:
            value = item["value"] if item is not None else claim.get("value", "")
            label = f"{claim.get('deliverable')} {claim.get('pointer') or ''}".strip()
            problems.append(f"{label} = {json.dumps(value, ensure_ascii=False)}: {problem}.")
    for key, item in required.items():
        if key not in covered and key not in verified:
            if not any((c.get("deliverable"), c.get("pointer")) == key for c in claims):
                value = json.dumps(item["value"], ensure_ascii=False)
                problems.append(
                    f"{key[0]} {key[1]} = {value}: the review did not check this value."
                )
    if not claims and not required:
        problems.append("The review listed no checked claims; a pass needs cited evidence.")
    return audited, problems
