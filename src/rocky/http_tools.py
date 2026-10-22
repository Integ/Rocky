"""Full HTTP requests with durable, private, goal-local cookie sessions."""

import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from http.cookiejar import Cookie, CookieJar
from pathlib import Path

import httpx

MAX_TEXT = 16_000
MAX_INLINE_BYTES = 1_000_000
_COOKIE_FIELDS = (
    "version",
    "name",
    "value",
    "port",
    "port_specified",
    "domain",
    "domain_specified",
    "domain_initial_dot",
    "path",
    "path_specified",
    "secure",
    "expires",
    "discard",
    "comment",
    "comment_url",
    "rfc2109",
)
_RESPONSE_HEADERS = {
    "content-type",
    "content-length",
    "content-encoding",
    "content-disposition",
    "cache-control",
    "etag",
    "last-modified",
}
_METHOD = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


class HTTPToolError(ValueError):
    """A user-facing failure which never includes credentials or request bodies."""


def _regular_private(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
        raise HTTPToolError("HTTP session storage must be private regular files")


def _load_cookies(directory: int, name: str) -> CookieJar:
    jar = CookieJar()
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return jar
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            _regular_private(stream.fileno())
            content = stream.read(4_000_001)
        if len(content) > 4_000_000:
            raise ValueError
        values = json.loads(content)
        if not isinstance(values, list):
            raise ValueError
        for value in values:
            if not isinstance(value, dict) or set(value) != {*_COOKIE_FIELDS, "rest"}:
                raise ValueError
            if not isinstance(value["rest"], dict):
                raise ValueError
            jar.set_cookie(Cookie(**value))
        jar.clear_expired_cookies()
        return jar
    except HTTPToolError:
        raise
    except (TypeError, ValueError, KeyError):
        raise HTTPToolError("HTTP cookie session is invalid and requires repair") from None


def _save_cookies(directory: int, name: str, jar: CookieJar) -> None:
    jar.clear_expired_cookies()
    values = [
        {**{field: getattr(cookie, field) for field in _COOKIE_FIELDS}, "rest": cookie._rest}
        for cookie in jar
    ]
    # Use an unrelated random name, then replace atomically within the locked directory.
    temporary = ".cookies-" + os.urandom(16).hex()
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(values, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


@contextmanager
def _session(directory: Path, session: str):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(descriptor).st_mode & 0o077:
            raise HTTPToolError("HTTP session directory must have private permissions")
        name = hashlib.sha256(session.encode()).hexdigest()
        lock = os.open(
            name + ".lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=descriptor,
        )
        try:
            _regular_private(lock)
            fcntl.flock(lock, fcntl.LOCK_EX)
            jar = _load_cookies(descriptor, name + ".json")
            try:
                yield jar
            finally:
                _save_cookies(descriptor, name + ".json", jar)
        finally:
            os.close(lock)
    finally:
        os.close(descriptor)


def _request_options(options: dict) -> tuple[str, str, float, bool, dict]:
    if not isinstance(options, dict):
        raise HTTPToolError("HTTP request options must be a JSON object")
    supported = {
        "method",
        "headers",
        "json",
        "data",
        "body",
        "session",
        "timeout",
        "follow_redirects",
        "auth",
    }
    if options.keys() - supported:
        raise HTTPToolError("Unknown HTTP request option")
    method = options.get("method", "GET")
    if not isinstance(method, str) or not _METHOD.fullmatch(method):
        raise HTTPToolError("HTTP method must be a valid method name")
    session = options.get("session", "default")
    if (
        not isinstance(session, str)
        or not session
        or len(session) > 256
        or any(ord(c) < 32 for c in session)
    ):
        raise HTTPToolError("HTTP session must be a nonempty name of at most 256 characters")
    timeout = options.get("timeout", 20)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise HTTPToolError("HTTP timeout must be a positive number of seconds")
    timeout = min(float(timeout), 30.0)
    redirects = options.get("follow_redirects", True)
    if not isinstance(redirects, bool):
        raise HTTPToolError("follow_redirects must be true or false")
    headers = options.get("headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
    ):
        raise HTTPToolError("HTTP headers must map names to string values")
    request: dict = {"headers": headers}
    bodies = [key for key in ("json", "data", "body") if key in options]
    if len(bodies) > 1:
        raise HTTPToolError("Choose one of json, data or body for the HTTP request")
    if "json" in options:
        try:
            request["content"] = json.dumps(
                options["json"], ensure_ascii=False, allow_nan=False
            ).encode()
        except (TypeError, ValueError):
            raise HTTPToolError("HTTP JSON body is invalid") from None
        request["headers"] = dict(httpx.Headers(headers))
        request["headers"].setdefault("content-type", "application/json")
    elif "data" in options:
        data = options["data"]
        if isinstance(data, dict):
            if not all(
                isinstance(value, (str, int, float, bool, list, type(None)))
                for value in data.values()
            ):
                raise HTTPToolError("HTTP form data must contain scalar or list values")
            request["data"] = data
        elif isinstance(data, (str, bytes)):
            request["content"] = data.encode() if isinstance(data, str) else data
        else:
            raise HTTPToolError("HTTP data must be a form object or raw text")
    elif "body" in options:
        body = options["body"]
        if not isinstance(body, (str, bytes)):
            raise HTTPToolError("HTTP body must be raw text")
        request["content"] = body.encode() if isinstance(body, str) else body
    if "auth" in options:
        auth = options["auth"]
        if isinstance(auth, dict) and set(auth) == {"username", "password"}:
            auth = [auth["username"], auth["password"]]
        if (
            not isinstance(auth, (list, tuple))
            or len(auth) != 2
            or not all(isinstance(value, str) for value in auth)
        ):
            raise HTTPToolError("HTTP basic auth requires a username and password")
        request["auth"] = httpx.BasicAuth(*auth)
    return method.upper(), session, timeout, redirects, request


def request_http(
    url: str,
    options: dict,
    session_directory: Path,
    *,
    download_path: Path | None = None,
) -> dict:
    """Send HTTP(S), retaining named cookies privately across worker restarts.

    HTTP error status codes are returned for the agent to inspect. Network or
    invalid-input failures raise HTTPToolError with credential-free messages.
    Inline bodies stop at 1 MB and previews stop at 16,000 characters. Downloads
    stream the complete body to an atomic file without that inline size limit.
    `sha256` and `bytes_received` cover the bytes actually retrieved.
    """
    method, session_name, timeout, redirects, request = _request_options(options)
    if (
        not isinstance(url, str)
        or len(url) > 16_384
        or any(ord(c) < 32 or ord(c) == 127 for c in url)
    ):
        raise HTTPToolError("HTTP request URL is invalid")
    try:
        parsed = httpx.URL(url)
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise HTTPToolError("HTTP requests require an http:// or https:// URL")
        with _session(Path(session_directory), session_name) as jar:
            return asyncio.run(
                _send_http(parsed, method, request, jar, timeout, redirects, download_path)
            )
    except HTTPToolError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise HTTPToolError("HTTP request timed out") from None
    except httpx.TooManyRedirects:
        raise HTTPToolError("HTTP request exceeded the redirect limit") from None
    except (httpx.HTTPError, httpx.InvalidURL):
        raise HTTPToolError("HTTP request failed; check the destination and connection") from None
    except (OSError, ValueError, TypeError, UnicodeError):
        raise HTTPToolError("HTTP request options or private session storage are invalid") from None


async def _send_http(parsed, method, request, jar, timeout, redirects, download_path):
    async def strip_redirect_credentials(outgoing):
        if (outgoing.url.scheme, outgoing.url.host, outgoing.url.port) != (
            parsed.scheme,
            parsed.host,
            parsed.port,
        ):
            for name in request["headers"]:
                if name.lower() not in {
                    "accept",
                    "accept-language",
                    "accept-encoding",
                    "user-agent",
                    "content-type",
                }:
                    outgoing.headers.pop(name, None)
            outgoing.headers.pop("Authorization", None)
            outgoing.headers.pop("Proxy-Authorization", None)

    async with asyncio.timeout(timeout):
        async with (
            httpx.AsyncClient(
                cookies=jar,
                timeout=timeout,
                follow_redirects=redirects,
                max_redirects=20,
                trust_env=False,
                event_hooks={"request": [strip_redirect_credentials]},
            ) as client,
        ):
            async with client.stream(method, parsed, **request) as response:
                size = 0
                digest = hashlib.sha256()
                preview = bytearray()
                truncated = False
                temporary: str | None = None
                output = None
                try:
                    if download_path is not None:
                        download_path = Path(download_path)
                        download_path.parent.mkdir(parents=True, exist_ok=True)
                        descriptor, temporary = tempfile.mkstemp(
                            prefix=".rocky-http-", dir=download_path.parent
                        )
                        output = os.fdopen(descriptor, "wb")
                    async for chunk in response.aiter_bytes():
                        if output is None and size + len(chunk) > MAX_INLINE_BYTES:
                            chunk = chunk[: MAX_INLINE_BYTES - size]
                            truncated = True
                        size += len(chunk)
                        digest.update(chunk)
                        if output is not None:
                            output.write(chunk)
                        if len(preview) < MAX_TEXT * 4 + 4:
                            preview.extend(chunk[: MAX_TEXT * 4 + 4 - len(preview)])
                        if truncated:
                            break
                    encoding = response.encoding or "utf-8"
                    try:
                        text = bytes(preview).decode(encoding, errors="replace")
                    except LookupError:
                        text = bytes(preview).decode("utf-8", errors="replace")
                    truncated = truncated or len(text) > MAX_TEXT or size > len(preview)
                    result = {
                        "url": str(
                            response.url.copy_with(
                                username=None, password=None, query=None, fragment=None
                            )
                        ),
                        "status_code": response.status_code,
                        "headers": {
                            key: value
                            for key, value in response.headers.items()
                            if key.lower() in _RESPONSE_HEADERS
                        },
                        "text": text[:MAX_TEXT],
                        "truncated": truncated,
                        "bytes_received": size,
                        "sha256": digest.hexdigest(),
                    }
                    if output is not None:
                        output.flush()
                        os.fsync(output.fileno())
                        output.close()
                        output = None
                        os.replace(temporary, download_path)
                        temporary = None
                        result["downloaded"] = True
                    return result
                finally:
                    if output is not None:
                        output.close()
                    if temporary is not None:
                        os.unlink(temporary)
