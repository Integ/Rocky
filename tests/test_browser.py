"""The browse tool: validation, approval and a real headless page when a browser exists."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from rocky.browser import browser_executable, interactive, parse_browse_options
from rocky.config import Settings
from rocky.models import Action, Deliverable, GoalContract, RunInput, ToolInput, ToolResult
from rocky.tool_contracts import ActionValidationError, requires_approval, validate_action
from rocky.tools import WorkspaceTools

CONTRACT = GoalContract(
    objective="Read a page",
    acceptance_criteria=["Page read"],
    deliverables=[Deliverable(path="notes.md")],
)


def browse_input(content=None, *, url="https://example.com/", path=None, operation_id="op-1"):
    return ToolInput(
        run=RunInput(goal_id="browse", contract=CONTRACT),
        operation_id=operation_id,
        action=Action(tool="browse", url=url, path=path, content=content),
    )


def test_options_default_to_a_plain_page_read():
    options = parse_browse_options(None)
    assert options["steps"] == [] and options["session"] == "default"
    assert not interactive(options)


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        '{"script":"alert(1)"}',
        '{"steps":[{"evaluate":"fetch(1)"}]}',
        '{"steps":[{"click":""}]}',
        '{"steps":[{"fill":"#q"}]}',
        '{"steps":[{"click":"#a","fill":"#b","value":"x"}]}',
        '{"steps":[{"wait":60000}]}',
        '{"timeout":-1}',
    ],
)
def test_invalid_options_are_rejected(content):
    with pytest.raises(ActionValidationError):
        validate_action(
            Action(tool="browse", url="https://example.com", content=content), full_access=True
        )


def test_screenshots_must_be_png_and_scoped_mode_has_no_browser():
    with pytest.raises(ActionValidationError):
        validate_action(
            Action(tool="browse", url="https://example.com", path="a.txt"), full_access=True
        )
    with pytest.raises(ActionValidationError):
        validate_action(Action(tool="browse", url="https://example.com"), full_access=False)


@pytest.mark.parametrize(
    ("steps", "reviewed"),
    [
        ([], False),
        ([{"wait": "#results"}, {"scroll": True}], False),
        ([{"click": "text=Next"}], True),
        ([{"fill": "#q", "value": "rocky"}], True),
        ([{"press": "Enter"}], True),
    ],
)
def test_reading_runs_freely_but_interaction_is_reviewed(steps, reviewed):
    action = Action(tool="browse", url="https://example.com", content=json.dumps({"steps": steps}))
    options = validate_action(action, full_access=True)
    assert requires_approval(action, options) is reviewed


def test_long_urls_are_reviewed_like_a_post():
    action = Action(tool="browse", url="https://example.com/?q=" + "a" * 3000)
    assert requires_approval(action, validate_action(action, full_access=True))


@pytest.fixture
def tools(tmp_path, monkeypatch):
    tools = WorkspaceTools(Settings(workspace_root=tmp_path / "workspaces"))
    dispatched = []

    def dispatch(request, _goal_fd):
        dispatched.append(request)
        return ToolResult(success=True, content="{}")

    monkeypatch.setattr(tools, "_execute", dispatch)
    tools.dispatched = dispatched
    return tools


def test_typing_into_a_page_waits_for_the_owner(tools):
    content = json.dumps({"steps": [{"fill": "#q", "value": "secret"}, {"press": "Enter"}]})
    result = tools.execute(browse_input(content))
    assert result.error_code == "approval_required" and not tools.dispatched
    # A task-wide HTTP grant never covers browser steps.
    assert result.approval_http_scope is None
    approved = browse_input(content).model_copy(
        update={"approved_action_fingerprints": [result.approval_required]}
    )
    assert tools.execute(approved).approved_by == "owner"


def test_screenshots_outside_the_task_need_approval(tools, tmp_path):
    result = tools.execute(browse_input(path=str(tmp_path / "elsewhere" / "shot.png")))
    assert result.error_code == "approval_required" and not tools.dispatched
    assert tools.execute(browse_input(path="shot.png", operation_id="op-2")).success


def _test_browser() -> str | None:
    configured = os.environ.get("ROCKY_TEST_BROWSER")
    if configured:
        return configured
    found = browser_executable()
    if found:
        return found
    for candidate in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux64/chrome")):
        return str(candidate)
    return None


@pytest.fixture(scope="module")
def site():
    posts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, body: str):
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/result"):
                self.send(f"<title>Result</title><p>You searched {self.path.split('=')[-1]}</p>")
            else:
                self.send(
                    "<title>Search</title><h1>Rendered by script</h1>"
                    '<form action="/result"><label for="q">Query</label>'
                    '<input id="q" name="q"><button>Go</button></form>'
                    '<a href="/result?q=link">A link</a>'
                    "<script>document.querySelector('h1').innerText='Hello from JS';"
                    "fetch('/beacon',{method:'POST',body:'x'}).catch(()=>{});</script>"
                )

        def do_POST(self):
            posts.append(self.path)
            self.send("ok")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", posts
    server.shutdown()


@pytest.fixture
def real_tools(tmp_path):
    executable = _test_browser()
    if executable is None:
        pytest.skip("No Chrome or Chromium is installed")
    return WorkspaceTools(
        Settings(
            workspace_root=tmp_path / "workspaces",
            browser_path=Path(executable),
            approval_mode="trusted",
        )
    )


def test_a_page_is_rendered_and_may_not_post_while_read(real_tools, site):
    base, posts = site
    result = real_tools.execute(browse_input(url=base + "/", path="page.png"))
    assert result.success, result.content
    page = json.loads(result.content)
    assert page["title"] == "Search" and "Hello from JS" in page["text"]
    assert {"text": "A link", "url": base + "/result?q=link"} in page["links"]
    assert any(field["selector"] == "#q" for field in page["fields"])
    assert page["blocked_requests"] >= 1 and posts == []
    assert page["screenshot"] == "page.png"
    shot = real_tools.root / "browse" / "page.png"
    assert shot.read_bytes().startswith(b"\x89PNG")


def test_approved_steps_fill_and_submit_a_form(real_tools, site):
    base, _posts = site
    content = json.dumps(
        {"steps": [{"fill": "#q", "value": "rocky"}, {"press": "Enter", "selector": "#q"}]}
    )
    result = real_tools.execute(browse_input(content, url=base + "/", operation_id="op-steps"))
    page = json.loads(result.content)
    assert result.success and page["title"] == "Result" and "rocky" in page["text"]
    assert page["steps_done"] == 2


def test_control_ports_are_unreachable_from_pages(real_tools, site):
    base, _posts = site
    port = int(base.rsplit(":", 1)[1])
    real_tools.control_ports = (port,)
    result = real_tools.execute(browse_input(url=base + "/", operation_id="op-blocked"))
    assert not result.success and "control service" in result.content
