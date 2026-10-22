"""Parallel reads, incremental file edits and command prefix approvals."""

import json
import threading

import pytest

from rocky.command_policy import command_class, command_words, matches_prefix, parse_prefix
from rocky.config import Settings
from rocky.models import Action, Deliverable, GoalContract, RunInput, ToolInput, read_only_action
from rocky.tool_contracts import ActionValidationError, parse_edits, validate_action
from rocky.tools import WorkspaceTools


def run(**contract) -> RunInput:
    fields = {
        "objective": "Change the project files",
        "acceptance_criteria": ["The files are changed"],
        "deliverables": [Deliverable(path="notes.md")],
    }
    fields.update(contract)
    return RunInput(goal_id="efficiency-goal", contract=GoalContract(**fields))


def execute(tools, action, operation_id, **extra):
    return tools.execute(
        ToolInput(run=extra.pop("run", run()), action=action, operation_id=operation_id, **extra)
    )


@pytest.fixture
def full(tmp_path):
    return WorkspaceTools(Settings(workspace_root=tmp_path / "full", approval_mode="trusted"))


@pytest.fixture
def scoped(tmp_path):
    return WorkspaceTools(Settings(workspace_root=tmp_path / "scoped", tool_access="scoped"))


@pytest.mark.parametrize(
    "command",
    [
        "ls; rm -rf ~",
        "ls && rm x",
        "ls | sh",
        "ls > out",
        "cat < /etc/passwd",
        "echo $(id)",
        "echo `id`",
        "echo ${HOME}",
        "ls\nrm x",
        "ls &",
        "ls a#b; rm x",
        "ls *(e:'rm x':)",
        'ls "unbalanced',
        "",
    ],
)
def test_shell_syntax_never_matches_an_approved_prefix(command):
    assert command_words(command) is None
    assert command_class(command) is None
    assert not matches_prefix(command, [["ls"], ["echo"], ["cat"]])


@pytest.mark.parametrize(
    "command,prefix",
    [
        ("ls -la src", ["ls"]),
        ("pytest -q tests/test_tools.py", ["pytest"]),
        ("git status --short", ["git", "status"]),
        ("npm run build", ["npm", "run", "build"]),
        ("uv run pytest -q", ["uv", "run", "pytest"]),
        ("uv run python -m pytest -q", ["uv", "run", "python", "-m", "pytest"]),
        ("python3 scripts/check.py --fast", ["python3", "scripts/check.py"]),
        ("cat README.md", ["cat"]),
    ],
)
def test_task_scope_command_class_is_narrow_and_explicit(command, prefix):
    assert command_class(command) == prefix
    assert matches_prefix(command, [prefix])


@pytest.mark.parametrize(
    "command",
    ["python -c 'import os'", "node -e 'x'", "bash", "sudo ls", "env A=1 ls", "xargs rm"],
)
def test_inline_code_and_wrappers_are_only_approvable_exactly(command):
    assert command_class(command) is None


def test_prefix_matches_whole_words_only():
    assert matches_prefix("git status -s", [["git", "status"]])
    assert not matches_prefix("git statusx", [["git", "status"]])
    assert not matches_prefix("git stash", [["git", "status"]])
    assert not matches_prefix("/bin/ls", [["ls"]])


def test_owner_allowlist_accepts_text_or_json_and_rejects_shell_syntax(monkeypatch):
    monkeypatch.setenv("ROCKY_AUTO_APPROVE_COMMANDS", "ls, git status,uv run pytest")
    assert Settings().auto_approved_prefixes() == [
        ["ls"],
        ["git", "status"],
        ["uv", "run", "pytest"],
    ]
    monkeypatch.setenv("ROCKY_AUTO_APPROVE_COMMANDS", '["git diff", "pwd"]')
    assert Settings().auto_approve_commands == ["git diff", "pwd"]
    for invalid in ["ls; rm", "sudo ls", "echo $HOME"]:
        assert parse_prefix(invalid) is None
        monkeypatch.setenv("ROCKY_AUTO_APPROVE_COMMANDS", invalid)
        with pytest.raises(ValueError):
            Settings()


def approval_tools(tmp_path, **settings):
    return WorkspaceTools(
        Settings(workspace_root=tmp_path / "approval", approval_mode="required", **settings)
    )


def test_allowlisted_simple_command_runs_without_approval(tmp_path):
    tools = approval_tools(tmp_path, auto_approve_commands=["printf"])
    allowed = execute(tools, Action(tool="run_command", content="printf allowed"), "allowed")
    assert allowed.success and "allowed" in allowed.content
    chained = execute(
        tools,
        Action(tool="run_command", content="printf allowed; touch escaped.txt"),
        "chained",
    )
    assert chained.error_code == "approval_required"
    assert not (tools.root / "efficiency-goal" / "escaped.txt").exists()
    other = execute(tools, Action(tool="run_command", content="touch other.txt"), "other")
    assert other.error_code == "approval_required"


def test_pending_command_offers_its_class_and_a_task_grant_covers_similar_commands(tmp_path):
    tools = approval_tools(tmp_path)
    pending = execute(tools, Action(tool="run_command", content="printf -- first"), "first")
    assert pending.error_code == "approval_required"
    assert pending.approval_command_prefix == ["printf"]
    # Plain words may be subcommands, so they narrow the class.
    narrow = execute(tools, Action(tool="run_command", content="printf first"), "narrow")
    assert narrow.approval_command_prefix == ["printf", "first"]
    chained = execute(tools, Action(tool="run_command", content="printf a && id"), "chained")
    assert chained.approval_command_prefix == []
    mutation = execute(
        tools,
        Action(tool="http_request", url="http://127.0.0.1:9/", content='{"method":"POST"}'),
        "post",
    )
    assert mutation.error_code == "approval_required"
    assert mutation.approval_command_prefix == []

    granted = execute(
        tools,
        Action(tool="run_command", content="printf -- second"),
        "second",
        approved_command_prefixes=[["printf"]],
    )
    assert granted.success and "second" in granted.content
    still_exact = execute(
        tools,
        Action(tool="run_command", content="printf x; touch y"),
        "third",
        approved_command_prefixes=[["printf"]],
    )
    assert still_exact.error_code == "approval_required"


@pytest.mark.parametrize("mode", ["full", "scoped"])
def test_append_and_edit_change_files_incrementally(mode, full, scoped):
    tools = full if mode == "full" else scoped
    path = "notes.md"
    assert execute(tools, Action(tool="append_file", path=path, content="# Report\n"), "a1").success
    appended = execute(tools, Action(tool="append_file", path=path, content="alpha beta\n"), "a2")
    assert appended.success, appended.content
    assert appended.artifacts and appended.artifacts[0].path == path
    edited = execute(
        tools,
        Action(
            tool="edit_file",
            path=path,
            content=json.dumps(
                {
                    "edits": [
                        {"old_text": "alpha", "new_text": "gamma"},
                        {"old_text": "a", "new_text": "A", "replace_all": True},
                    ]
                }
            ),
        ),
        "e1",
    )
    assert edited.success, edited.content
    assert "2 edit(s)" in edited.content
    target = tools.root / "efficiency-goal" / path
    assert target.read_text() == "# Report\ngAmmA betA\n"


@pytest.mark.parametrize("mode", ["full", "scoped"])
def test_failed_edits_leave_the_file_unchanged_with_repair_guidance(mode, full, scoped):
    tools = full if mode == "full" else scoped
    target = tools.root / "efficiency-goal" / "notes.md"
    execute(tools, Action(tool="write_file", path="notes.md", content="one two one\n"), "w")
    for operation, edit, expected in [
        ("missing", {"old_text": "three", "new_text": "3"}, "was not found"),
        ("ambiguous", {"old_text": "one", "new_text": "1"}, "occurs 2 times"),
        (
            "partial",
            {"edits": [{"old_text": "two", "new_text": "2"}, {"old_text": "nine", "new_text": ""}]},
            "Edit 2",
        ),
    ]:
        result = execute(
            tools,
            Action(tool="edit_file", path="notes.md", content=json.dumps(edit)),
            operation,
        )
        assert not result.success and expected in result.content
        assert target.read_text() == "one two one\n"
    created = execute(
        tools,
        Action(tool="edit_file", path="absent.md", content='{"old_text":"a","new_text":"b"}'),
        "absent",
    )
    assert not created.success and "write_file" in created.content


def test_full_access_edit_preserves_line_endings_and_permissions(full):
    target = full.root / "efficiency-goal" / "script.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"echo one\r\necho two\r\n")
    target.chmod(0o755)
    result = execute(
        full,
        Action(tool="edit_file", path=str(target), content='{"old_text":"two","new_text":"2"}'),
        "crlf",
    )
    assert result.success, result.content
    assert target.read_bytes() == b"echo one\r\necho 2\r\n"
    assert target.stat().st_mode & 0o777 == 0o755


def test_interrupted_append_is_never_repeated_automatically(full):
    action = Action(tool="append_file", path="notes.md", content="once\n")
    request = ToolInput(run=run(), action=action, operation_id="append-once")
    assert full.execute(request).success
    # A retried Activity replays its receipt instead of appending again.
    assert full.execute(request).success
    assert (full.root / "efficiency-goal" / "notes.md").read_text() == "once\n"


def test_edit_and_append_arguments_are_validated_before_dispatch():
    for content in [None, "not json", "[]", '{"old_text":""}', '{"edits":[]}', '{"x":1}']:
        with pytest.raises(ActionValidationError):
            validate_action(Action(tool="edit_file", path="a", content=content), full_access=True)
    with pytest.raises(ActionValidationError):
        validate_action(Action(tool="append_file", path="a", content=""), full_access=True)
    assert parse_edits('{"old_text":"a","new_text":"b"}') == [("a", "b", False)]


@pytest.mark.parametrize(
    "action,expected",
    [
        (Action(tool="read_file", path="a"), True),
        (Action(tool="list_files", path="."), True),
        (Action(tool="fetch_url", url="https://example.com"), True),
        (Action(tool="http_request", url="https://e.com", content='{"method":"head"}'), True),
        (Action(tool="http_request", url="https://e.com", content='{"method":"POST"}'), False),
        (Action(tool="http_request", url="https://e.com", path="download.bin"), False),
        (Action(tool="http_request", url="https://e.com", content="{bad"), False),
        (Action(tool="write_file", path="a", content="x"), False),
        (Action(tool="append_file", path="a", content="x"), False),
        (Action(tool="run_command", content="ls"), False),
    ],
)
def test_only_observing_actions_are_batchable(action, expected):
    assert read_only_action(action) is expected


def test_read_only_operations_share_the_goal_lock(full):
    execute(full, Action(tool="write_file", path="notes.md", content="shared"), "seed")
    results = []
    with full._goal("efficiency-goal", shared=True):
        # Another read proceeds while a read holds the lock; a mutation waits.
        reader = threading.Thread(
            target=lambda: results.append(
                execute(full, Action(tool="read_file", path="notes.md"), "concurrent-read")
            )
        )
        writer = threading.Thread(
            target=lambda: results.append(
                execute(full, Action(tool="write_file", path="w.md", content="x"), "write")
            )
        )
        reader.start()
        reader.join(timeout=10)
        assert not reader.is_alive()
        writer.start()
        writer.join(timeout=0.3)
        assert writer.is_alive()
    writer.join(timeout=10)
    assert [item.success for item in results] == [True, True]
    assert results[0].content == "shared"
