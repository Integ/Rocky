import hashlib
import io
import json
import socket

import pytest
from pydantic import BaseModel

from rocky.config import Settings
from rocky.inference import ModelSettings
from rocky.models import Action, Deliverable, GoalContract, RunInput, ToolInput
from rocky.network import FetchError, fetch_public_text
from rocky.tools import WorkspaceTools


@pytest.fixture
def tools(tmp_path):
    return WorkspaceTools(Settings(workspace_root=tmp_path / "workspaces", tool_access="scoped"))


def run(**updates):
    return RunInput(
        goal_id="test-goal",
        contract=GoalContract(
            objective="Write a report",
            acceptance_criteria=["Report contains the conclusion"],
            deliverables=[Deliverable(path="report.txt", min_bytes=5, contains=["conclusion"])],
            **updates,
        ),
    )


def execute(tools, action, operation_id="operation-1", goal=None):
    return tools.execute(ToolInput(run=goal or run(), operation_id=operation_id, action=action))


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "/etc/passwd",
        "a/../../secret",
        "a//b",
        ".rocky/lock",
        "a/.env",
        "C:\\secret",
        "a\nfile",
    ],
)
def test_scope_rejects_unsafe_paths(tools, path):
    result = execute(tools, Action(tool="write_file", path=path, content="unsafe"))
    assert not result.success
    assert not (tools.root.parent / "secret").exists()


def test_invalid_goal_cannot_escape_workspace(tools):
    goal = run()
    goal.goal_id = "../elsewhere"
    result = execute(tools, Action(tool="list_files", path="."), goal=goal)
    assert not result.success


def test_read_and_write_reject_symlinks(tools, tmp_path):
    goal_dir = tools.root / "test-goal"
    goal_dir.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("private content")
    (goal_dir / "link.txt").symlink_to(secret)
    assert not execute(tools, Action(tool="read_file", path="link.txt")).success
    assert not execute(
        tools, Action(tool="write_file", path="link.txt", content="replaced"), "write-link"
    ).success
    assert secret.read_text() == "private content"


def test_parent_symlink_rejected(tools, tmp_path):
    goal_dir = tools.root / "test-goal"
    goal_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (goal_dir / "folder").symlink_to(outside, target_is_directory=True)
    assert not execute(
        tools, Action(tool="write_file", path="folder/result.txt", content="unsafe")
    ).success
    assert not (outside / "result.txt").exists()


def test_goal_symlink_rejected(tools, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tools.root / "test-goal").symlink_to(outside, target_is_directory=True)
    assert not execute(
        tools, Action(tool="write_file", path="result.txt", content="unsafe")
    ).success
    assert not (outside / "result.txt").exists()


def test_tool_permission_is_enforced(tools):
    result = execute(
        tools,
        Action(tool="write_file", path="report.txt", content="hello"),
        goal=run(allowed_tools=["read_file"]),
    )
    assert not result.success
    assert "authorized" in result.content


def test_receipts_survive_new_tool_instance_and_do_not_repeat_writes(tools):
    action = Action(tool="write_file", path="report.txt", content="first conclusion")
    first = execute(tools, action)
    assert first.success
    # A later operation changes the file. Retrying the earlier operation must
    # return its receipt, rather than overwrite the newer file with old content.
    assert execute(
        tools, Action(tool="write_file", path="report.txt", content="new conclusion"), "operation-2"
    ).success
    recreated = WorkspaceTools(Settings(workspace_root=tools.root))
    assert execute(recreated, action) == first
    assert (tools.root / "test-goal/report.txt").read_text() == "new conclusion"
    assert not (tools.root / "test-goal/.rocky").exists()


def test_operation_identifier_cannot_be_reused_with_changed_payload(tools):
    assert execute(tools, Action(tool="write_file", path="report.txt", content="first")).success
    result = execute(tools, Action(tool="write_file", path="report.txt", content="second"))
    assert not result.success
    assert "cannot be reused" in result.content
    assert (tools.root / "test-goal/report.txt").read_text() == "first"


def test_legacy_receipt_accepts_model_settings_without_repeating_the_operation(tools, monkeypatch):
    # These schemas reproduce the original persisted Activity input, before
    # RunInput had inference settings. Do not derive the fixture hash from the
    # new fingerprint helper: that would hide a serialization incompatibility.
    class LegacyRunInput(BaseModel):
        goal_id: str
        contract: GoalContract

    class LegacyToolInput(BaseModel):
        run: LegacyRunInput
        operation_id: str
        action: Action

    original_run = run()
    action = Action(tool="write_file", path="report.txt", content="原始 conclusion\n")
    operation_id = "legacy-operation"
    first = execute(tools, action, operation_id, original_run)
    assert first.success
    legacy_input = LegacyToolInput(
        run=LegacyRunInput(goal_id=original_run.goal_id, contract=original_run.contract),
        operation_id=operation_id,
        action=action,
    )
    receipt_path = (
        tools.root
        / ".rocky"
        / original_run.goal_id
        / (hashlib.sha256(operation_id.encode()).hexdigest() + ".json")
    )
    receipt = json.loads(receipt_path.read_text())
    legacy_fingerprint = hashlib.sha256(
        legacy_input.model_dump_json(exclude={"run": {"contract": {"limits"}}}).encode()
    ).hexdigest()
    assert receipt["fingerprint"] == legacy_fingerprint
    # Materialize the old receipt explicitly, then change the visible file with
    # another operation. A retry must return the old receipt, not repeat a write.
    receipt["fingerprint"] = legacy_fingerprint
    receipt_path.write_text(json.dumps(receipt))
    assert execute(
        tools,
        Action(tool="write_file", path="report.txt", content="newer conclusion"),
        "newer-operation",
    ).success
    upgraded_run = original_run.model_copy(
        update={"model_settings": ModelSettings(model="gpt-6-sol", reasoning_effort="high")}
    )
    recreated = WorkspaceTools(Settings(workspace_root=tools.root))

    def must_not_repeat(*args, **kwargs):
        raise AssertionError("A legacy receipt must bypass tool execution")

    monkeypatch.setattr(recreated, "_execute", must_not_repeat)
    assert execute(recreated, action, operation_id, upgraded_run) == first
    assert (tools.root / "test-goal/report.txt").read_text() == "newer conclusion"


def test_changing_inference_settings_does_not_change_tool_idempotency(tools, monkeypatch):
    configured_run = run().model_copy(
        update={"model_settings": ModelSettings(model="gpt-6-sol", reasoning_effort="high")}
    )
    action = Action(tool="write_file", path="report.txt", content="A conclusion")
    first = execute(tools, action, goal=configured_run)
    assert first.success
    changed_run = configured_run.model_copy(
        update={"model_settings": ModelSettings(model="gpt-6-luna", reasoning_effort="low")}
    )

    def must_not_repeat(*args, **kwargs):
        raise AssertionError("Inference settings must not repeat an existing tool operation")

    monkeypatch.setattr(tools, "_execute", must_not_repeat)
    assert execute(tools, action, goal=changed_run) == first


def test_model_settings_compatibility_does_not_weaken_contract_conflict_detection(tools):
    action = Action(tool="write_file", path="report.txt", content="A conclusion")
    assert execute(tools, action).success
    changed_run = run(max_steps=21).model_copy(
        update={"model_settings": ModelSettings(model="gpt-6-sol", reasoning_effort="high")}
    )
    result = execute(tools, action, goal=changed_run)
    assert not result.success
    assert "cannot be reused" in result.content
    assert (tools.root / "test-goal/report.txt").read_text() == "A conclusion"


def test_model_settings_compatibility_does_not_weaken_action_conflict_detection(tools):
    assert execute(tools, Action(tool="write_file", path="report.txt", content="first")).success
    configured_run = run().model_copy(
        update={"model_settings": ModelSettings(model="gpt-6-sol", reasoning_effort="high")}
    )
    result = execute(
        tools,
        Action(tool="write_file", path="report.txt", content="different"),
        goal=configured_run,
    )
    assert not result.success
    assert "cannot be reused" in result.content
    assert (tools.root / "test-goal/report.txt").read_text() == "first"


def test_artifact_verification_and_hashes(tools):
    assert not tools.verify_files(run()).passed
    assert execute(tools, Action(tool="write_file", path="report.txt", content="hello")).success
    assert not tools.verify_files(run()).passed
    assert execute(
        tools, Action(tool="write_file", path="report.txt", content="A conclusion"), "operation-2"
    ).success
    verification = tools.verify_files(run())
    assert verification.passed
    assert verification.artifacts[0].sha256 == hashlib.sha256(b"A conclusion").hexdigest()
    assert "A conclusion" in tools.review_text(run())


def test_review_context_is_bounded(tools):
    content = "conclusion " * 10000
    assert execute(tools, Action(tool="write_file", path="report.txt", content=content)).success
    review = tools.review_text(run())
    assert len(review) <= 24000
    assert "truncated" in review


def test_listing_omits_control_files_and_links(tools, tmp_path):
    assert execute(
        tools, Action(tool="write_file", path="nested/report.txt", content="conclusion")
    ).success
    (tools.root / "test-goal" / "link").symlink_to(tmp_path)
    result = execute(tools, Action(tool="list_files", path="."), "listing")
    assert result.success
    assert "nested/report.txt" in result.content
    assert ".rocky" not in result.content
    assert "link" not in result.content


def test_network_tool_requires_both_permission_and_domain(tools):
    result = execute(
        tools,
        Action(tool="fetch_url", url="https://example.com"),
        goal=run(allowed_tools=["read_file"]),
    )
    assert not result.success
    result = execute(
        tools,
        Action(tool="fetch_url", url="https://example.com"),
        goal=run(allowed_tools=["fetch_url"]),
    )
    assert not result.success
    assert "allowed_domains" in result.content


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.0.0.2", "169.254.169.254", "::1", "::ffff:127.0.0.1", "100.64.0.1"]
)
def test_fetch_rejects_private_dns_answers(monkeypatch, ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(family, socket.SOCK_STREAM, 6, "", (ip, 443))]
    )
    with pytest.raises(FetchError, match="forbidden"):
        fetch_public_text("https://example.com/", ["example.com"])


def test_mixed_public_and_private_dns_answers_rejected(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(FetchError, match="forbidden"):
        fetch_public_text("https://example.com/", ["example.com"])


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:pass@example.com/",
        "https://example.com:444/",
        "https://sub.example.com/",
        "https://example.com/\r\nHost: evil",
    ],
)
def test_unsafe_urls_rejected_without_network(monkeypatch, url):
    def unexpected(*args, **kwargs):
        raise AssertionError("should not resolve an invalid URL")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected)
    with pytest.raises(FetchError):
        fetch_public_text(url, ["example.com"])


def fake_public_server(monkeypatch, response):
    """Exercise the real HTTP client with an in-memory socket, never the network."""
    observed = {"lookups": 0, "sent": b""}

    def resolve(*args, **kwargs):
        observed["lookups"] += 1
        if observed["lookups"] > 1:
            # A rebinding hostname would resolve locally on a second lookup.
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

    class Socket:
        def settimeout(self, value):
            pass

        def connect(self, address):
            observed["connected"] = address

        def sendall(self, content):
            observed["sent"] += content

        def makefile(self, *args):
            return io.BytesIO(response)

        def close(self):
            pass

        def shutdown(self, how):
            pass

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", lambda *args: Socket())
    return observed


def test_fetch_pins_public_ip_without_resolving_again(monkeypatch):
    observed = fake_public_server(
        monkeypatch,
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 5\r\n\r\nhello",
    )
    result = fetch_public_text("http://example.com/report", ["example.com"])
    assert result.text == "hello"
    assert result.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert observed["lookups"] == 1
    assert observed["connected"] == ("93.184.216.34", 80)
    assert b"Host: example.com\r\n" in observed["sent"]


def test_fetch_does_not_follow_redirects(monkeypatch):
    observed = fake_public_server(
        monkeypatch,
        b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/secret\r\nContent-Length: 0\r\n\r\n",
    )
    with pytest.raises(FetchError, match="Redirects"):
        fetch_public_text("http://example.com/report", ["example.com"])
    assert observed["lookups"] == 1


def test_fetch_rejects_oversized_body(monkeypatch):
    fake_public_server(
        monkeypatch,
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 5\r\n\r\nhello",
    )
    with pytest.raises(FetchError, match="maximum download"):
        fetch_public_text("http://example.com/report", ["example.com"], max_bytes=4)


def test_fetch_rejects_compressed_bodies(monkeypatch):
    fake_public_server(
        monkeypatch,
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Encoding: gzip\r\nContent-Length: 5\r\n\r\nhello",
    )
    with pytest.raises(FetchError, match="Compressed"):
        fetch_public_text("http://example.com/report", ["example.com"])
