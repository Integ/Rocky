"""Independent review sees what actually happened, not only the deliverables."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from rocky.config import Settings
from rocky.evidence import (
    audit_review,
    collect_review_evidence,
    review_checklist,
    verify_checklist,
)
from rocky.memory import serialize_memory
from rocky.models import (
    Action,
    Deliverable,
    GoalContract,
    ReviewVerdict,
    RunInput,
    RunState,
    ToolInput,
    VerifyInput,
)
from rocky.planner import OpenAIPlanner, _fit_evidence
from rocky.tools import WorkspaceTools

RUN = RunInput(
    goal_id="evidence-goal",
    contract=GoalContract(
        objective="Summarise the sales data",
        acceptance_criteria=["Figures come from sales.csv"],
        deliverables=[Deliverable(path="summary.json")],
    ),
)


@pytest.fixture
def tools(tmp_path):
    return WorkspaceTools(Settings(workspace_root=tmp_path / "workspaces", approval_mode="trusted"))


def workspace(tools):
    path = tools.root / RUN.goal_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def execute(tools, action, operation_id):
    return tools.execute(ToolInput(run=RUN, action=action, operation_id=operation_id))


def test_commands_report_the_files_they_create_or_modify(tools, tmp_path):
    root = workspace(tools)
    (root / "existing.txt").write_text("old")
    (root / "untouched.txt").write_text("same")
    result = execute(
        tools,
        Action(
            tool="run_command",
            content="mkdir -p data .git && printf new > new.txt && printf changed > existing.txt"
            " && printf out > data/out.txt && printf ignored > .git/HEAD",
        ),
        "command",
    )
    assert result.success, result.content
    assert sorted(result.changed_paths) == ["data/out.txt", "existing.txt", "new.txt"]
    assert result.changes_truncated is False

    outside = tmp_path / "project"
    outside.mkdir()
    elsewhere = execute(
        tools, Action(tool="run_command", content="printf x > a.txt", path=str(outside)), "outside"
    )
    assert elsewhere.changed_paths == [str((outside / "a.txt").resolve())]


def test_file_writes_report_goal_relative_or_absolute_paths(tools, tmp_path):
    workspace(tools)
    inside = execute(tools, Action(tool="write_file", path="notes/a.md", content="a"), "inside")
    assert inside.changed_paths == ["notes/a.md"]
    target = tmp_path / "elsewhere" / "b.md"
    outside = execute(tools, Action(tool="write_file", path=str(target), content="b"), "outside")
    assert outside.changed_paths == [str(target.resolve())]


def act(cycle, tool, *, path=None, content=None, result="", changed=(), success=True):
    return {
        "phase": "act",
        "cycle": cycle,
        "input": {
            "decision": {
                "kind": "act",
                "action": {"tool": tool, "path": path, "url": None, "content": content},
            }
        },
        "output": {
            "result": {"success": success, "content": result, "changed_paths": list(changed)}
        },
    }


def test_evidence_attributes_outputs_and_flags_partial_and_unread_sources(tools):
    root = workspace(tools)
    (root / "sales.csv").write_text("region,units\nNorth,3\nSouth,4\n")
    (root / "app.log").write_text("line\n" * 5000)
    (root / "pricing.py").write_text("def total(x):\n    return x * 2\n")
    (root / "summary.json").write_text('{"rows": 2}')
    steps = [
        {"phase": "think", "cycle": 1, "input": {}, "output": {}},
        act(1, "read_file", path="app.log", result="line\n" * 3000 + "\n[truncated]"),
        act(2, "read_file", path="pricing.py", result="def total(x):\n    return x\n"),
        act(
            3,
            "run_command",
            content="python summarise.py",
            result="Exit code: 0\nstdout:\n",
            changed=["summary.json"],
        ),
        act(4, "write_file", path="summary.json", content='{"rows": 2}', changed=["summary.json"]),
        act(5, "write_file", path="pricing.py", content="...", changed=["pricing.py"]),
        act(6, "run_command", content="false", result="Exit code: 1", success=False),
    ]
    evidence = collect_review_evidence(RUN, steps, tools)

    # The deliverable was computed, then overwritten by hand: both are recorded.
    assert evidence["deliverables"] == {"summary.json": ["step 3 run_command", "step 4 write_file"]}
    sources = {item["path"]: item for item in evidence["sources"]}
    assert set(sources) == {"app.log", "sales.csv"}
    assert sources["app.log"]["read_by_task"] == "partial"
    assert (sources["app.log"]["bytes"], sources["app.log"]["lines"]) == (25000, 5000)
    assert sources["sales.csv"]["read_by_task"] == "not read with read_file"
    assert sources["sales.csv"]["lines"] == 3 and "North,3" in sources["sales.csv"]["content"]

    [changed] = evidence["changed_files"]
    assert changed["path"] == "pricing.py" and changed["changed_by"] == ["step 5 write_file"]
    assert "-    return x\n" in changed["diff"] and "+    return x * 2\n" in changed["diff"]

    actions = {item["step"]: item for item in evidence["actions"]}
    assert actions[1]["read"].startswith("partial") and actions[2]["read"] == "complete"
    assert actions[3]["changed"] == ["summary.json"] and "Exit code: 0" in actions[3]["output"]
    assert "output" not in actions[4] and actions[6]["output"] == "Exit code: 1"


def fake_client():
    response = SimpleNamespace(
        status="completed",
        output_parsed=ReviewVerdict(passed=False, feedback="pricing.py still doubles"),
        output=[],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    return SimpleNamespace(responses=SimpleNamespace(parse=AsyncMock(return_value=response)))


def large_evidence():
    return {
        "sources": [
            {
                "path": "small.csv",
                "bytes": 20,
                "lines": 2,
                "read_by_task": "complete",
                "content": "a,b\n1,2\n",
            },
            *[
                {
                    "path": f"big-{index}.log",
                    "bytes": 90_000,
                    "lines": 9000,
                    "read_by_task": "partial",
                    "content": "日志" * 30_000,
                }
                for index in range(2)
            ],
        ],
        "changed_files": [
            {
                "path": "src/app.py",
                "bytes": 80_000,
                "lines": 2000,
                "changed_by": ["step 4"],
                "diff": "+changed\n" * 10_000,
            }
        ],
        "deliverables": {"report.md": ["step 5 write_file"]},
        "actions": [
            {
                "step": index,
                "tool": "run_command",
                "target": "cat big.log",
                "success": True,
                "output": "x" * 1500,
            }
            for index in range(80)
        ],
        "earlier_actions_omitted": 0,
    }


async def test_review_payload_carries_bounded_evidence_with_fair_shares():
    client = fake_client()
    planner = OpenAIPlanner(Settings(), client=client)
    request = VerifyInput(run=RUN, state=RunState(goal_id=RUN.goal_id))
    evidence = large_evidence()
    result = await planner.review(request, "report text", evidence=evidence)
    assert result.feedback == "pricing.py still doubles"

    instructions, data = client.responses.parse.await_args.kwargs["input"]
    assert len(data["content"].encode()) <= 100_000
    payload = json.loads(data["content"])["evidence"]
    sources = {item["path"]: item for item in payload["sources"]}
    # Small files survive whole; large ones share the rest and are marked.
    assert (
        sources["small.csv"]["content"] == "a,b\n1,2\n" and "truncated" not in sources["small.csv"]
    )
    for name in ("big-0.log", "big-1.log"):
        assert sources[name]["truncated"] and sources[name]["lines"] == 9000
        assert len(sources[name]["content"]) > 1000
    assert payload["changed_files"][0]["truncated"] and payload["changed_files"][0]["diff"]
    assert payload["deliverables"] == {"report.md": ["step 5 write_file"]}
    assert payload["actions"] and payload["actions"][-1]["step"] == 79
    assert all(item["output"] for item in payload["actions"])  # Shortened, not erased.
    assert evidence == large_evidence()  # The caller's evidence is not modified.
    assert "traceable to" in instructions["content"]
    assert "source content or to an observed tool output" in instructions["content"]


async def test_review_without_evidence_keeps_the_artifact_only_payload():
    client = fake_client()
    request = VerifyInput(run=RUN, state=RunState(goal_id=RUN.goal_id))
    await OpenAIPlanner(Settings(), client=client).review(request, "report text")
    payload = json.loads(client.responses.parse.await_args.kwargs["input"][1]["content"])
    assert "evidence" not in payload


def test_extreme_pressure_drops_sources_before_changed_files():
    evidence = large_evidence()
    evidence["sources"] *= 10
    fitted = _fit_evidence(evidence, 3_000)
    assert len(serialize_memory(fitted).encode()) <= 3_000
    assert fitted["files_omitted"] > 0
    assert fitted["changed_files"] and fitted["changed_files"][0]["path"] == "src/app.py"
    assert fitted["earlier_actions_omitted"] > 0
    assert _fit_evidence(evidence, 50) == {"omitted": "Evidence did not fit the review request."}


AUDIT_EVIDENCE = {
    "sources": [
        {
            "path": "notes.md",
            "content": "**Decision DEC-01** — adopt PostgreSQL 17. Owner: Priya.\n"
            "DEC-12 replaces DEC-06: reports run at 04:30 UTC. Lena takes over ownership from Samir.\n"
            "Lena: the dashboard is slow.\n",
        }
    ],
    "changed_files": [],
    "deliverables": {
        "summary.json": ["step 2 run_command"],
        "decisions.json": ["step 3 write_file"],
    },
    "actions": [
        {
            "step": 1,
            "tool": "run_command",
            "target": "grep -c ERROR app.log",
            "output": "stdout:\n23\n",
        },
        {
            "step": 2,
            "tool": "run_command",
            "target": "python3 summarise.py",
            "output": "Exit code: 0",
        },
        {"step": 3, "tool": "write_file", "target": "decisions.json"},
        {"step": 4, "tool": "run_command", "target": "echo 52335.99", "output": "52335.99"},
    ],
}
AUDIT_DELIVERABLES = {
    "summary.json": json.dumps({"total": 7, "errors": 23, "revenue": 52335.99}),
    "decisions.json": json.dumps(
        {
            "decisions": [
                {"id": "DEC-01", "owner": "Lena", "status": "active"},
                {"id": "DEC-12", "owner": "Lena", "status": "active"},
            ]
        }
    ),
}


def claim(pointer, kind, location, quote, *, deliverable=None, supported=True, value=""):
    return {
        "deliverable": deliverable
        or ("summary.json" if pointer.count("/") == 1 else "decisions.json"),
        "pointer": pointer,
        "value": value,
        "kind": kind,
        "location": location,
        "quote": quote,
        "supported": supported,
    }


@pytest.mark.parametrize(
    "item,problem",
    [
        # The command that wrote the deliverable computed it.
        (claim("/total", "computed", "step:2", "python3 summarise"), ""),
        # A count shown in the output of the command that computed it.
        (claim("/errors", "computed", "Step 1", "grep -c ERROR"), ""),
        (claim("/revenue", "computed", "step:4", "echo"), "states this value literally"),
        (claim("/errors", "computed", "step:3", "decisions.json"), "must come from a command"),
        (claim("/errors", "judgment", "source:notes.md", "Owner: Priya"), "a number must be"),
        # A name taken from the wrong line: it does not name the record.
        (
            claim("/decisions/0/owner", "quoted", "source:notes.md", "Lena takes over ownership"),
            "tie this value to its record (DEC-01)",
        ),
        # A quotable name cannot be waved through as a judgment.
        (
            claim("/decisions/0/owner", "judgment", "source:notes.md", "Decision DEC-01"),
            "appears in the evidence; quote the text",
        ),
        (
            claim(
                "/decisions/1/owner",
                "quoted",
                "source: notes.md",
                "DEC-12 replaces DEC-06 ... Lena takes over",
            ),
            "",
        ),
        # Markdown emphasis and dash variants do not break a faithful quote.
        (
            claim("/decisions/0/status", "judgment", "source:notes.md", "Decision DEC-01 - adopt"),
            "",
        ),
        (
            claim("/total", "quoted", "source:notes.md", "Owner: Marco"),
            "does not appear at the cited location",
        ),
        (claim("/total", "quoted", "source:missing.md", "x"), "is not in the evidence"),
        (
            claim("/total", "computed", "step:2", "python3", supported=False),
            "no supporting evidence",
        ),
        (
            claim("/total", "quoted", "deliverable:summary.json", '"total": 7'),
            "needs evidence other than the deliverable itself",
        ),
    ],
)
def test_claim_audit_rules(item, problem):
    checklist = review_checklist(AUDIT_DELIVERABLES)
    audited, _ = audit_review([item], AUDIT_EVIDENCE, AUDIT_DELIVERABLES, checklist)
    assert audited[0]["problem"] == "" if not problem else problem in audited[0]["problem"]


def test_audit_requires_every_listed_value_and_at_least_one_claim():
    checklist = review_checklist(AUDIT_DELIVERABLES)
    assert {item["pointer"] for item in checklist} == {
        "/total",
        "/errors",
        "/revenue",
        # Plain lowercase labels such as statuses are judged, not cited.
        *(f"/decisions/{index}/{key}" for index in (0, 1) for key in ("id", "owner")),
    }
    owner = next(item for item in checklist if item["pointer"] == "/decisions/0/owner")
    assert owner["context"] == ["DEC-01"]
    _, problems = audit_review(
        [claim("/total", "computed", "step:2", "python3")],
        AUDIT_EVIDENCE,
        AUDIT_DELIVERABLES,
        checklist,
    )
    assert len(problems) == len(checklist) - 1
    assert any(
        "summary.json /errors = 23: the review did not check this value" in p for p in problems
    )
    _, problems = audit_review([], AUDIT_EVIDENCE, {"report.md": "text"}, [])
    assert problems == ["The review listed no checked claims; a pass needs cited evidence."]
    judged = claim("", "judgment", "deliverable:report.md", "text", deliverable="report.md")
    assert audit_review([judged], AUDIT_EVIDENCE, {"report.md": "text"}, [])[1] == []


async def test_review_request_lists_required_values_and_the_audit_rules():
    client = fake_client()
    request = VerifyInput(run=RUN, state=RunState(goal_id=RUN.goal_id))
    checklist = review_checklist(AUDIT_DELIVERABLES)
    result = await OpenAIPlanner(Settings(), client=client).review(
        request, "report text", evidence={"sources": [], "actions": []}, checklist=checklist
    )
    assert result.claims == []
    instructions, data = client.responses.parse.await_args.kwargs["input"]
    assert json.loads(data["content"])["required_claims"] == checklist
    assert "The application audits every claim" in instructions["content"]
    assert "never a number" in instructions["content"]


def test_rocky_verifies_values_it_can_trace_and_nothing_else():
    evidence = {
        "sources": [
            {
                "path": "docs/sso.md",
                "content": "SCIM provisioning requires the **Enterprise** plan.\n",
            },
            {"path": "docs/billing.md", "content": "Payment terms are net 30.\n"},
            {
                "path": "notes.md",
                "content": "DEC-01 adopt PostgreSQL 17. Owner: Priya.\nLena: hello.\n",
            },
        ],
        "changed_files": [],
        "deliverables": {
            "summary.json": ["step 2 run_command"],
            "answers.json": ["step 3 write_file"],
        },
        "actions": [
            {
                "step": 1,
                "tool": "run_command",
                "target": "wc -l data.csv",
                "output": "181 data.csv",
            },
            {"step": 2, "tool": "run_command", "target": "python3 sum.py", "output": ""},
            {"step": 3, "tool": "write_file", "target": "answers.json"},
        ],
    }
    deliverables = {
        "summary.json": json.dumps({"total": 52335.99, "rows": 180}),
        "answers.json": json.dumps(
            {
                "answers": {"q1": "Enterprise", "q2": "net 30", "lines": 181, "small": 7},
                "sources": {"q1": "docs/sso.md", "q2": "docs/sso.md"},
                "decisions": [
                    {"id": "DEC-01", "owner": "Priya", "status": "active"},
                    {"id": "DEC-01", "owner": "Lena", "status": "active"},
                ],
            }
        ),
    }
    checklist = review_checklist(deliverables)
    verified = verify_checklist(checklist, evidence, {})
    support = {pointer: why for (_, pointer), why in verified.items()}
    # Written by the command that computed them, and not stated in its text.
    assert support["/total"] == support["/rows"] == "computed by step 2 run_command"
    # Stated in a source or an output line.
    assert support["/answers/q1"].startswith("stated in source:docs/sso.md")
    assert support["/answers/lines"].startswith("stated in step:1")
    # Small numbers occur everywhere: they need a cited claim.
    assert "/answers/small" not in support
    # A citation holds when the cited file states its paired answer...
    assert support["/sources/q1"] == 'cited source:docs/sso.md states "Enterprise"'
    # ...and not when it does not: sso.md says nothing about net 30.
    assert "/sources/q2" not in support
    # A record field is verified only on a line that names its record.
    assert support["/decisions/0/owner"].startswith("stated in source:notes.md: DEC-01")
    assert "/decisions/1/owner" not in support
    assert not any(pointer.endswith("/status") for pointer in support)

    # A command that states a value literally did not compute it.
    literal = {
        **evidence,
        "actions": [{"step": 2, "tool": "run_command", "target": "echo 52335.99 > x"}],
    }
    assert ("summary.json", "/total") not in verify_checklist(checklist, literal, {})

    # Verified values need no claim; marking one unsupported disputes it.
    remaining = [
        item for item in checklist if (item["deliverable"], item["pointer"]) not in verified
    ]
    assert {item["pointer"] for item in remaining} == {
        "/answers/small",
        "/sources/q2",
        # Nothing ties DEC-01 to Lena, so neither field of that record holds.
        "/decisions/1/id",
        "/decisions/1/owner",
    }
    dispute = claim("/total", "computed", "step:2", "python3", supported=False)
    _, problems = audit_review([dispute], evidence, deliverables, checklist, verified=verified)
    assert any("disputes a value the application found" in problem for problem in problems)
    assert sum("did not check" in problem for problem in problems) == 4


def test_quotes_may_keep_json_escapes_and_cite_locations_loosely():
    evidence = {
        "sources": [
            {"path": "docs/rate-limits.md", "content": 'Limit: "600" per minute\nTeam plan'}
        ],
        "changed_files": [],
        "deliverables": {},
        "actions": [],
    }
    deliverables = {"report.md": "Team: 600"}
    for location in (
        "rate-limits.md",
        "file:docs/rate-limits.md",
        "artifact:report.md and source:docs/rate-limits.md",
    ):
        item = claim(
            "",
            "quoted",
            location,
            'Limit: \\"600\\" per minute\\nTeam plan',
            deliverable="report.md",
            value="600",
        )
        assert audit_review([item], evidence, deliverables, [])[1] == [], location


def test_quotes_may_skip_lines_but_record_values_stay_on_their_line():
    code = "def f(x):\n    # explain\n    y = x * 2\n    return y\n"
    notes = "DEC-01 adopt PostgreSQL 17. Owner: Priya.\nLena: the dashboard is slow.\n"
    evidence = {
        "sources": [{"path": "notes.md", "content": notes}],
        "changed_files": [],
        "deliverables": {},
        "actions": [],
    }
    deliverables = {
        "f.py": code,
        "d.json": json.dumps({"decisions": [{"id": "DEC-01", "owner": "Lena"}]}),
    }
    skipped = claim(
        "", "quoted", "file:f.py", "def f(x):\n    y = x * 2\n    return y", deliverable="f.py"
    )
    assert audit_review([skipped], evidence, deliverables, [])[1] == []
    # Two real lines spliced together cannot tie Lena to DEC-01.
    checklist = review_checklist(deliverables)
    spliced = claim(
        "/decisions/0/owner",
        "quoted",
        "source:notes.md",
        "DEC-01 adopt PostgreSQL 17. Owner: Priya.\nLena: the dashboard is slow.",
        deliverable="d.json",
    )
    audited, _ = audit_review([spliced], evidence, deliverables, checklist)
    assert "does not tie this value to its record (DEC-01)" in audited[0]["problem"]


def test_citations_hold_by_quoting_the_cited_file_or_its_paired_answer():
    evidence = {
        "sources": [
            {"path": "docs/billing.md", "content": "Invoices must be paid within 30 days.\n"},
            {"path": "docs/sso.md", "content": "SSO sessions last 12 hours.\n"},
        ],
        "changed_files": [],
        "deliverables": {"answers.json": ["step 1 write_file"]},
        "actions": [],
    }
    deliverables = {
        "answers.json": json.dumps(
            {
                "answers": {"q4": 30, "q5": 30},
                "sources": {"q4": "docs/billing.md", "q5": "docs/sso.md"},
            }
        )
    }
    checklist = review_checklist(deliverables)
    verified = verify_checklist(checklist, evidence, {})
    # A small paired answer still verifies the file that states it, not another.
    assert ("answers.json", "/sources/q4") in verified
    assert ("answers.json", "/sources/q5") not in verified
    citation = claim(
        "/sources/q5",
        "quoted",
        "source:docs/sso.md",
        "SSO sessions last",
        deliverable="answers.json",
    )
    audited, _ = audit_review([citation], evidence, deliverables, checklist, verified=verified)
    # Quoting the cited file itself is the reviewer's support for a citation.
    assert audited[0]["problem"] == ""
