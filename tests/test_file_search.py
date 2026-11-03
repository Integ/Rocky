"""File discovery, bounded evidence and approval/isolation for recursive searches."""

import json
import os

import pytest
from test_tool_contracts import request
from test_workflow import temporal_env as _temporal_env

from rocky import file_search
from rocky.config import Settings
from rocky.evidence import collect_review_evidence
from rocky.models import Action, read_only_action
from rocky.tool_contracts import approval_fingerprint
from rocky.tools import WorkspaceTools
from rocky.workflow import GoalWorkflow

temporal_env = _temporal_env


@pytest.fixture(params=["full", "scoped"])
def tree(tmp_path, request):
    tools = WorkspaceTools(
        Settings(workspace_root=tmp_path / "workspaces", tool_access=request.param)
    )
    root = tools.root / "tool-contract"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src/a.py").write_text(
        "before\nNeedle 中文\nafter\nneedle lowercase\n", encoding="utf-8"
    )
    (root / "main.py").write_text("literal a.*b\nregex axxxb\n", encoding="utf-8")
    (root / "notes.md").write_text("Needle notes\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules/dep.py").write_text("Needle dependency")
    (root / ".hidden").mkdir()
    (root / ".hidden/secret.py").write_text("Needle hidden")
    return tools, root


def call(tools, tool="search_files", *, path=".", operation="search", **options):
    action = Action(tool=tool, path=path, content=json.dumps(options) if options else None)
    result = tools.execute(request(action, operation_id=operation))
    assert result.success, result.content
    assert len(result.content) <= 16000
    return json.loads(result.content)


@pytest.mark.parametrize(
    "glob,expected",
    [
        ("*.py", ["main.py", "src/a.py"]),
        ("**/*.py", ["main.py", "src/a.py"]),
        ("src/*.py", ["src/a.py"]),
        ("a.py", ["src/a.py"]),
        ("SRC/*.py", []),
    ],
)
def test_find_files_returns_deterministic_root_relative_paths(tree, glob, expected):
    tools, root = tree
    data = call(tools, "find_files", glob=glob)
    assert [item["path"] for item in data["matches"]] == expected
    assert all(
        item["size_bytes"] == (root / item["path"]).stat().st_size for item in data["matches"]
    )
    assert data["complete"] and not data["truncated"]
    assert data["skipped"]["excluded"] == 2
    assert not data["next_offset"]


def test_search_returns_lines_context_and_literal_matches(tree):
    tools, _ = tree
    data = call(tools, query="Needle", glob="**/*.py", context_lines=1)
    assert data["matches"] == [
        {
            "path": "src/a.py",
            "line": 2,
            "text": "Needle 中文",
            "line_truncated": False,
            "context": [
                {"line": 1, "text": "before", "line_truncated": False},
                {"line": 3, "text": "after", "line_truncated": False},
            ],
        }
    ]
    folded = call(tools, query="NEEDLE", glob="*.py", case_sensitive=False, operation="folded")
    assert [m["line"] for m in folded["matches"]] == [2, 4]
    literal = call(tools, query="a.*b", operation="literal")
    assert [m["line"] for m in literal["matches"]] == [1]
    sub = call(tools, path="src", query="中文", operation="subroot")
    assert sub["matches"][0]["path"] == "a.py"


def test_pagination_has_no_duplicates_and_reports_exact_last_page(tree):
    tools, _ = tree
    page1 = call(tools, "find_files", glob="*.py", limit=1)
    assert page1["truncated"] and not page1["complete"] and page1["next_offset"] == 1
    page2 = call(tools, "find_files", glob="*.py", limit=1, offset=1, operation="page2")
    assert not page2["truncated"] and page2["next_offset"] is None
    assert [p["matches"][0]["path"] for p in [page1, page2]] == ["main.py", "src/a.py"]
    after = call(tools, "find_files", glob="*.py", offset=2, operation="after")
    assert not after["matches"] and after["complete"]


def test_skip_binary_large_links_and_special_files_without_hanging(tree):
    tools, root = tree
    (root / "binary").write_bytes(b"Needle\x00binary")
    (root / "invalid").write_bytes(b"Needle\xff")
    (root / "large").write_bytes(b"x" * (file_search.MAX_FILE_BYTES + 1))
    (root / "alias").symlink_to(root / "src", target_is_directory=True)
    os.link(root / "notes.md", root / "hardlink")
    os.mkfifo(root / "fifo")
    data = call(tools, query="Needle")
    assert not data["complete"] and not data["truncated"]
    assert data["skipped"]["binary"] == 2 and data["skipped"]["oversized"] == 1
    assert data["skipped"]["links"] == 3 and data["skipped"]["special"] == 1
    assert [m["path"] for m in data["matches"]] == ["src/a.py"]


@pytest.mark.parametrize(
    "setting,value,reason",
    [
        ("MAX_ENTRIES", 1, "entry_limit"),
        ("MAX_SCAN_BYTES", 2, "byte_limit"),
        ("MAX_SECONDS", 0, "time_limit"),
    ],
)
def test_scan_limits_are_explicit_and_never_claim_complete(
    tree, monkeypatch, setting, value, reason
):
    tools, _ = tree
    monkeypatch.setattr(file_search, setting, value)
    data = call(tools, query="not present")
    assert data["truncated"] and not data["complete"] and data["stop_reason"] == reason
    assert data["next_offset"] is None


def test_output_stays_valid_json_and_lines_are_marked_when_clipped(tree):
    tools, root = tree
    (root / "long.txt").write_text(("hit" + "x" * 500 + "\n") * 100)
    data = call(tools, query="hit", glob="*.txt", limit=200, context_lines=3)
    assert data["stop_reason"] == "output_limit" and data["next_offset"] == len(data["matches"])
    assert data["matches"][0]["line_truncated"] and len(data["matches"][0]["text"]) == 400


@pytest.mark.parametrize(
    "tool,content",
    [
        ("search_files", None),
        ("search_files", "{}"),
        ("find_files", "[]"),
        ("search_files", '{"query":"a","query":"b"}'),
        ("find_files", '{"unknown":"private-marker"}'),
        ("search_files", '{"query":"x","context_lines":true}'),
        ("search_files", '{"query":"x","case_sensitive":"false"}'),
        ("find_files", '{"limit":true}'),
        ("find_files", '{"offset":-1}'),
        ("find_files", '{"glob":"../*"}'),
        ("find_files", '{"glob":"/etc/*"}'),
        ("find_files", '{"glob":null}'),
        ("find_files", '{"limit":201}'),
        ("find_files", '{"include_hidden":NaN}'),
        ("search_files", '{"query":"x\\ny"}'),
    ],
)
def test_invalid_arguments_never_dispatch_or_echo_supplied_values(
    tmp_path, monkeypatch, tool, content
):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path))
    monkeypatch.setattr(tools, "_execute", lambda *_args: pytest.fail("Invalid search dispatched"))
    result = tools.execute(request(Action(tool=tool, path=".", content=content)))
    assert result.error_code == "invalid_arguments" and result.retry_hint
    assert "private-marker" not in result.content
    assert not list((tools.root / ".rocky/tool-contract").glob("*.json"))


def test_searches_use_the_existing_personal_read_gate_and_skip_nested_credentials(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    (home / "project").mkdir(parents=True)
    (home / "project/notes.txt").write_text("Needle project")
    (home / ".ssh").mkdir()
    (home / ".ssh/key").write_text("Needle PRIVATE KEY")
    monkeypatch.setenv("HOME", str(home))
    tools = WorkspaceTools(Settings(workspace_root=tmp_path / "workspaces"))
    action = Action(
        tool="search_files", path=str(home), content='{"query":"Needle","include_hidden":true}'
    )
    req = request(action)
    pending = tools.execute(req)
    assert pending.error_code == "approval_required" and "PRIVATE KEY" not in pending.content
    req.approved_action_fingerprints = [approval_fingerprint(req)]
    result = tools.execute(req)
    assert result.success and result.approved_by == "owner"
    data = json.loads(result.content)
    assert [m["path"] for m in data["matches"]] == ["project/notes.txt"]
    assert data["skipped"]["protected"] and "PRIVATE KEY" not in result.content
    # A direct credential-root query is a separate exact approval.
    credential = request(
        action.model_copy(update={"path": str(home / ".ssh")}), operation_id="credential"
    )
    assert tools.execute(credential).error_code == "approval_required"


def test_granted_project_is_searchable_without_approval_and_hidden_is_explicit(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    (project / ".config").write_text("Needle project")
    monkeypatch.setenv("HOME", str(home))
    tools = WorkspaceTools(Settings(workspace_root=tmp_path / "workspaces"))
    req = request(
        Action(
            tool="search_files",
            path=str(project),
            content='{"query":"Needle","include_hidden":true}',
        )
    )
    req.run.contract.folders = [str(project)]
    result = tools.execute(req)
    assert result.success and json.loads(result.content)["matches"][0]["path"] == ".config"
    scoped = WorkspaceTools(Settings(workspace_root=tmp_path / "scoped", tool_access="scoped"))
    assert scoped.execute(req).error_code == "invalid_arguments"


def test_recursive_search_never_enters_other_task_or_private_state(tmp_path):
    tools = WorkspaceTools(
        Settings(workspace_root=tmp_path / "workspaces", approval_mode="trusted")
    )
    own = tools.root / "tool-contract"
    own.mkdir()
    other = tools.root / "other-task"
    other.mkdir()
    (other / "secret").write_text("PRIVATE OTHER TASK")
    (own / "alias").symlink_to(other, target_is_directory=True)
    for tool in ["find_files", "search_files"]:
        action = Action(
            tool=tool,
            path=str(other),
            content='{"query":"PRIVATE"}' if tool == "search_files" else None,
        )
        denied = tools.execute(request(action, operation_id=tool + "denied"))
        assert not denied.success and "PRIVATE OTHER TASK" not in denied.content
        data = call(
            tools, tool, operation=tool, **({"query": "PRIVATE"} if tool == "search_files" else {})
        )
        assert not data["matches"] and data["skipped"]["protected"] == 1


def test_search_output_is_reviewable_evidence_but_does_not_mark_a_file_fully_read(tree):
    tools, _ = tree
    action = Action(tool="search_files", path=".", content='{"query":"Needle","glob":"*.py"}')
    req = request(action)
    result = tools.execute(req)
    steps = [
        {
            "cycle": 1,
            "phase": "act",
            "input": {"decision": {"action": action.model_dump()}},
            "output": {"result": result.model_dump()},
        }
    ]
    evidence = collect_review_evidence(req.run, steps, tools)
    assert "Needle 中文" in evidence["actions"][0]["output"]
    assert evidence["actions"][0]["output_format"].startswith("decoded search lines")
    assert all(s["read_by_task"] != "complete" for s in evidence["sources"])
    assert read_only_action(action)
    from rocky.models import Decision

    assert not GoalWorkflow._decision_error(
        req.run,
        Decision(
            kind="act", reason="Search", actions=[action, Action(tool="find_files", path=".")]
        ),
    )


def test_last_supported_offset_does_not_offer_an_invalid_next_page(tree):
    tools, root = tree
    (root / "many.txt").write_text("hit\n" * 10003)
    result = call(tools, query="hit", glob="many.txt", offset=10000, limit=1)
    assert result["truncated"] and result["stop_reason"] == "offset_limit"
    assert result["next_offset"] is None
    assert result["matches"][0]["line"] == 10001


def test_depth_limit_and_unicode_casefold_are_honest(tree, monkeypatch):
    tools, root = tree
    (root / "case.txt").write_text("Straße 中文\n", encoding="utf-8")
    folded = call(tools, query="STRASSE", case_sensitive=False)
    assert folded["matches"][0]["text"] == "Straße 中文"
    monkeypatch.setattr(file_search, "MAX_DEPTH", 0)
    data = call(tools, query="Needle", operation="shallow")
    assert data["skipped"]["depth"] and not data["complete"]


def test_search_receipt_is_stable_and_new_operation_observes_changed_files(tree):
    tools, root = tree
    before = call(tools, query="Needle", glob="notes.md")
    (root / "notes.md").write_text("changed\n")
    replay = call(tools, query="Needle", glob="notes.md")
    assert replay == before
    fresh = call(tools, query="Needle", glob="notes.md", operation="fresh")
    assert not fresh["matches"] and fresh["complete"]


def test_scoped_search_still_requires_tool_in_contract(tmp_path):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path, tool_access="scoped"))
    req = request(Action(tool="find_files", path="."))
    req.run.contract.allowed_tools = ["read_file"]
    assert not tools.execute(req).success


def test_parent_search_skips_rocky_control_and_custom_credential_paths(tmp_path):
    tools = WorkspaceTools(
        Settings(
            workspace_root=tmp_path / "workspaces",
            credential_path=tmp_path / "credentials.json",
            database_url=f"sqlite:///{tmp_path}/control.db",
            approval_mode="trusted",
        )
    )
    (tmp_path / "credentials.json").write_text("PRIVATE marker")
    (tmp_path / "control.db").write_text("PRIVATE marker")
    (tmp_path / "public.txt").write_text("public text")
    result = call(tools, query="PRIVATE", path=str(tmp_path), include_hidden=True)
    assert not result["matches"] and result["skipped"]["protected"] >= 3


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="session")
async def test_production_loop_batches_new_searches_and_replays(temporal_env, tmp_path):
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.worker import Replayer
    from test_adaptive_workflow import ApprovalActivities, worker
    from test_workflow import finish

    from rocky.models import Decision

    tools = WorkspaceTools(Settings(workspace_root=tmp_path))
    (tmp_path / "tool-contract").mkdir()
    (tmp_path / "tool-contract/source.txt").write_text("needle evidence\n")
    run = request(Action(tool="find_files", path=".")).run
    actions = [
        Action(tool="find_files", path=".", content='{"glob":"*.txt"}'),
        Action(tool="search_files", path=".", content='{"query":"needle"}'),
    ]
    acts = ApprovalActivities(
        decisions=[Decision(kind="act", reason="Find evidence", actions=actions), finish()],
        workspace_tools=tools,
    )
    async with worker(temporal_env, run, acts):
        handle = await temporal_env.client.start_workflow(
            GoalWorkflow.run, run, id=run.goal_id, task_queue=run.goal_id
        )
        result = await handle.result()
    assert result.status == "succeeded" and result.model_calls == 3
    assert {item.operation_id for item in acts.tools} == {"step-0001-1", "step-0001-2"}
    observations = acts.plans[1].state.observations
    assert [item.tool for item in observations] == ["find_files", "search_files"]
    assert all(
        json.loads(item.content)["matches"][0]["path"] == "source.txt" for item in observations
    )
    await Replayer(
        workflows=[GoalWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(await handle.fetch_history())


@pytest.mark.parametrize(
    "separator", ["\x0b", "\x0c", "\r", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\r\n"]
)
def test_search_locations_match_targeted_reads_for_non_lf_separators(tree, separator):
    tools, root = tree
    text = f"before{separator}inside\nneedle\r\nafter\r"
    (root / "separators.txt").write_bytes(text.encode("utf-8"))
    data = call(tools, query="needle", glob="separators.txt", context_lines=1)
    [match] = data["matches"]
    expected_line = text.partition("needle")[0].count("\n") + 1
    assert match["line"] == expected_line
    assert match["text"] == "needle"
    action = Action(
        tool="read_file",
        path="separators.txt",
        content=json.dumps({"start_line": match["line"], "end_line": match["line"]}),
    )
    actual = tools.execute(request(action, operation_id="read-matched-line"))
    assert actual.success and actual.content.endswith("needle\r\n")
    assert match["context"][-1]["text"] == "after\r"


@pytest.mark.parametrize("name", ["venv", "node_modules", "__pycache__"])
def test_dependency_names_only_exclude_directories_not_regular_files(tree, name):
    tools, root = tree
    # The fixture already has a dependency directory, so use a separate scope.
    scope = root / "plain-files"
    scope.mkdir()
    (scope / name).write_text("needle regular file\n")
    found = call(tools, "find_files", path="plain-files", glob=name)
    assert [m["path"] for m in found["matches"]] == [name]
    searched = call(tools, path="plain-files", query="needle", operation="content")
    assert searched["matches"][0]["text"] == "needle regular file"
    assert searched["complete"] and not searched["skipped"]["excluded"]
