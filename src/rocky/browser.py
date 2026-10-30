"""Headless browser pages for sites that only work with JavaScript.

The browse tool drives the owner's installed Chrome (or Edge, Chromium,
Brave) through Playwright, with a private profile per task and session name,
never the owner's own browser profile. Opening a page and reading it is a GET
like fetch_url; while it loads, the page itself may only make safe-method
requests. Steps that click, type or select can submit forms and carry data
anywhere, so they are reviewed like a POST. Rocky's control ports are
unreachable from every page, redirect and WebSocket.
"""

import asyncio
import hashlib
import json
import math
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

from rocky.http_tools import HTTPToolError
from rocky.models import SAFE_HTTP_METHODS

MAX_STEPS = 20
MAX_TEXT = 12_000
MAX_LINKS = 40
MAX_FIELDS = 30
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 90.0
_WAIT_UNTIL = {"load", "domcontentloaded", "networkidle"}
_STEP_FIELDS = {
    "click": {"selector"},
    "fill": {"selector", "value"},
    "select": {"selector", "value"},
    "press": {"key", "selector"},
    "wait": {"selector", "ms"},
    "scroll": set(),
}
_REQUIRED = {"click": {"selector"}, "fill": {"selector", "value"}, "select": {"selector", "value"}}
_OPTIONS = {"session", "steps", "wait_until", "timeout", "full_page"}
_MAC_BROWSERS = (
    "Google Chrome.app/Contents/MacOS/Google Chrome",
    "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "Chromium.app/Contents/MacOS/Chromium",
    "Brave Browser.app/Contents/MacOS/Brave Browser",
)
# Collected in the page, so the model can name links and form fields in steps.
_PAGE_SUMMARY = """
({maxLinks, maxFields, maxText}) => {
  const clean = (value) => (value || "").replace(/\\s+/g, " ").trim().slice(0, 160);
  const selector = (element) => {
    if (element.id) return "#" + CSS.escape(element.id);
    const name = element.getAttribute("name");
    if (name) return `${element.tagName.toLowerCase()}[name="${name.replace(/"/g, '\\\\"')}"]`;
    const siblings = Array.from(element.parentElement ? element.parentElement.children : [])
      .filter((item) => item.tagName === element.tagName);
    const index = siblings.indexOf(element) + 1;
    return `${element.tagName.toLowerCase()}:nth-of-type(${index})`;
  };
  const links = [];
  for (const anchor of document.querySelectorAll("a[href]")) {
    const text = clean(anchor.innerText || anchor.getAttribute("aria-label"));
    if (!text || !/^https?:/.test(anchor.href)) continue;
    links.push({text, url: anchor.href.slice(0, 500)});
    if (links.length >= maxLinks) break;
  }
  const fields = [];
  for (const element of document.querySelectorAll("input, textarea, select, button")) {
    const type = (element.getAttribute("type") || element.tagName).toLowerCase();
    if (type === "hidden") continue;
    const label = clean(
      (element.labels && element.labels[0] && element.labels[0].innerText) ||
      element.getAttribute("aria-label") || element.getAttribute("placeholder") ||
      (element.tagName === "BUTTON" ? element.innerText : "") || element.value && type === "submit" && element.value
    );
    fields.push({selector: selector(element), type, label});
    if (fields.length >= maxFields) break;
  }
  const text = document.body ? document.body.innerText : "";
  return {text: text.slice(0, maxText), truncated: text.length > maxText, links, fields};
}
"""


class BrowserError(HTTPToolError):
    """A page or step failed; messages never include typed values."""


class BrowserUnavailable(RuntimeError):
    """No browser to drive; nothing was opened."""


def parse_browse_options(content: str | None) -> dict:
    """Validate browse options; raise ValueError with planner-facing advice."""
    if content is None:
        return {"session": "default", "steps": [], "wait_until": "load", "timeout": DEFAULT_TIMEOUT}
    try:
        value = json.loads(content)
    except (ValueError, TypeError, RecursionError):
        raise ValueError(
            'browse content must be a JSON object such as {"steps":[{"click":"text=Next"}]}'
        ) from None
    if not isinstance(value, dict) or value.keys() - _OPTIONS:
        raise ValueError(
            "browse content may contain only session, steps, wait_until, timeout and full_page"
        )
    session = value.get("session", "default")
    if not isinstance(session, str) or not session or len(session) > 256:
        raise ValueError("browse session must be a nonempty name of at most 256 characters")
    if any(ord(character) < 32 for character in session):
        raise ValueError("browse session must not contain control characters")
    wait_until = value.get("wait_until", "load")
    if wait_until not in _WAIT_UNTIL:
        raise ValueError("wait_until must be load, domcontentloaded or networkidle")
    timeout = value.get("timeout", DEFAULT_TIMEOUT)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("browse timeout must be a positive number of seconds")
    full_page = value.get("full_page", False)
    if not isinstance(full_page, bool):
        raise ValueError("full_page must be true or false")
    steps = value.get("steps", [])
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise ValueError(f"steps must be a list of at most {MAX_STEPS} steps")
    parsed = []
    for number, step in enumerate(steps, 1):
        parsed.append(_parse_step(number, step))
    return {
        "session": session,
        "steps": parsed,
        "wait_until": wait_until,
        "timeout": min(float(timeout), MAX_TIMEOUT),
        "full_page": full_page,
    }


def _parse_step(number: int, step) -> dict:
    """A step is {"click": "<selector>"}, or {"fill": "<selector>", "value": "..."}, etc."""
    message = (
        f'Step {number} must be one of: {{"click":"<selector>"}}, '
        '{"fill":"<selector>","value":"text"}, {"select":"<selector>","value":"option"}, '
        '{"press":"Enter"} (optionally with "selector"), {"wait":"<selector>"} or '
        '{"wait":1000} in milliseconds, {"scroll":true}'
    )
    if not isinstance(step, dict):
        raise ValueError(message)
    kinds = [kind for kind in _STEP_FIELDS if kind in step]
    if len(kinds) != 1:
        raise ValueError(message)
    kind = kinds[0]
    target = step[kind]
    extra = step.keys() - {kind, "value", "selector"}
    if extra:
        raise ValueError(message)
    result: dict = {"kind": kind}
    if kind in {"click", "fill", "select"}:
        if not isinstance(target, str) or not target.strip() or "selector" in step:
            raise ValueError(message)
        result["selector"] = target
    if kind in {"fill", "select"}:
        if not isinstance(step.get("value"), str):
            raise ValueError(message)
        result["value"] = step["value"]
    elif "value" in step:
        raise ValueError(message)
    if kind == "press":
        if not isinstance(target, str) or not target or len(target) > 40:
            raise ValueError(message)
        result["key"] = target
        if "selector" in step:
            if not isinstance(step["selector"], str) or not step["selector"].strip():
                raise ValueError(message)
            result["selector"] = step["selector"]
    if kind == "wait":
        if "selector" in step:
            raise ValueError(message)
        if isinstance(target, str) and target.strip():
            result["selector"] = target
        elif type(target) is int and 0 < target <= 10_000:
            result["ms"] = target
        else:
            raise ValueError(message)
    if kind == "scroll" and (target is not True or "selector" in step):
        raise ValueError(message)
    return result


def interactive(options: dict) -> bool:
    """Steps act on the page: a click or keypress may submit what was typed."""
    return any(step["kind"] not in {"wait", "scroll"} for step in options.get("steps", []))


def browser_executable(configured: Path | None = None) -> str | None:
    """The browser to drive, or None to use Playwright's own Chromium."""
    if configured is not None:
        return str(configured.expanduser())
    for base in (Path("/Applications"), Path.home() / "Applications"):
        for relative in _MAC_BROWSERS:
            candidate = base / relative
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def _profile(directory: Path, session: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise BrowserError("Browser profile storage must be a private directory")
    profile = directory / hashlib.sha256(session.encode()).hexdigest()[:32]
    profile.mkdir(exist_ok=True, mode=0o700)
    if profile.is_symlink() or os.lstat(profile).st_mode & 0o077:
        raise BrowserError("Browser profile storage must be a private directory")
    return profile


def _port(url: str) -> int | None:
    try:
        parts = urlsplit(url)
        return parts.port or {"http": 80, "https": 443, "ws": 80, "wss": 443}.get(parts.scheme)
    except ValueError:
        return None


def browse(
    url: str,
    options: dict,
    profile_directory: Path,
    *,
    blocked_ports: tuple[int, ...] = (),
    executable: str | None = None,
    screenshot: bool = False,
) -> tuple[dict, bytes | None]:
    """Open url, run the steps and return a page summary and optional PNG."""
    if _port(url) in blocked_ports:
        raise BrowserError("Browser access to Rocky's control service ports is forbidden")
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        raise BrowserUnavailable(
            "The browse tool needs the playwright package; reinstall Rocky's dependencies"
        ) from None
    profile = _profile(profile_directory, options["session"])
    return asyncio.run(
        _browse(url, options, profile, blocked_ports, executable, screenshot=screenshot)
    )


async def _browse(url, options, profile, blocked_ports, executable, *, screenshot):
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    from playwright.async_api import async_playwright

    timeout_ms = options["timeout"] * 1000
    allow_mutations = interactive(options)
    blocked: list[str] = []

    async def guard(route, request):
        if _port(request.url) in blocked_ports:
            blocked.append("control port")
            await route.abort("accessdenied")
        elif not allow_mutations and request.method.upper() not in SAFE_HTTP_METHODS:
            # Opening a page is a read; the page may not post on the task's behalf.
            blocked.append(request.method.upper())
            await route.abort("accessdenied")
        else:
            await route.continue_()

    async def guard_socket(socket):
        if _port(socket.url) in blocked_ports:
            blocked.append("control port")
            await socket.close()
        else:
            socket.connect_to_server()

    launch = {
        "headless": True,
        "accept_downloads": False,
        "service_workers": "block",
        "viewport": {"width": 1280, "height": 900},
    }
    if executable:
        launch["executable_path"] = executable
    async with async_playwright() as playwright:
        try:
            context = await playwright.chromium.launch_persistent_context(str(profile), **launch)
        except PlaywrightError:
            raise BrowserUnavailable(
                "No browser could be started. Install Google Chrome, set ROCKY_BROWSER_PATH, "
                "or run `rocky browser-install` once"
            ) from None
        try:
            context.set_default_timeout(timeout_ms)
            await context.route("**/*", guard)
            if hasattr(context, "route_web_socket"):
                await context.route_web_socket("**/*", guard_socket)
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                response = await page.goto(url, wait_until=options["wait_until"])
            except PlaywrightTimeout:
                raise BrowserError("The page did not finish loading in time") from None
            except PlaywrightError:
                raise BrowserError("The page could not be opened; check the URL") from None
            done = 0
            for number, step in enumerate(options["steps"], 1):
                try:
                    await _run_step(page, step)
                except PlaywrightTimeout:
                    raise BrowserError(
                        f"Step {number} ({step['kind']}) timed out; check its selector. "
                        f"Steps before it ran."
                    ) from None
                except PlaywrightError:
                    raise BrowserError(
                        f"Step {number} ({step['kind']}) failed; check its selector. "
                        f"Steps before it ran."
                    ) from None
                done += 1
            if options["steps"]:
                try:
                    await page.wait_for_load_state("load", timeout=min(timeout_ms, 10_000))
                except PlaywrightError:
                    pass
            try:
                summary = await page.evaluate(
                    _PAGE_SUMMARY,
                    {"maxLinks": MAX_LINKS, "maxFields": MAX_FIELDS, "maxText": MAX_TEXT},
                )
                title = await page.title()
                image = (
                    await page.screenshot(full_page=options.get("full_page", False))
                    if screenshot
                    else None
                )
            except PlaywrightError:
                raise BrowserError("The page changed while it was being read; try again") from None
        finally:
            try:
                await context.close()
            except PlaywrightError:
                pass
    result = {
        "url": page.url,
        "status_code": response.status if response is not None else None,
        "title": title[:300],
        **summary,
        "session": options["session"],
        "steps_done": done,
        "blocked_requests": len(blocked),
    }
    return result, image


async def _run_step(page, step: dict) -> None:
    kind = step["kind"]
    if kind == "click":
        await page.click(step["selector"])
    elif kind == "fill":
        await page.fill(step["selector"], step["value"])
    elif kind == "select":
        await page.select_option(step["selector"], step["value"])
    elif kind == "press":
        if "selector" in step:
            await page.press(step["selector"], step["key"])
        else:
            await page.keyboard.press(step["key"])
    elif kind == "wait":
        if "selector" in step:
            await page.wait_for_selector(step["selector"])
        else:
            await page.wait_for_timeout(step["ms"])
    elif kind == "scroll":
        await page.mouse.wheel(0, 2000)
        await page.wait_for_timeout(300)
