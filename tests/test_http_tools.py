"""Exercise HTTP methods and durable login sessions against an actual local server."""

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from rocky.http_tools import HTTPToolError, request_http

TOKEN = "synthetic-local-session-value"


@pytest.fixture(scope="module")
def http_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, body=b"", headers=()):
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/login":
                assert self.command == "POST"
                submitted = (
                    json.loads(body)
                    if "application/json" in self.headers.get("Content-Type", "")
                    else {key: values[0] for key, values in parse_qs(body.decode()).items()}
                )
                if submitted != {"username": "tester", "password": "synthetic-password"}:
                    self.respond(401, b'{"error":"invalid login"}')
                    return
                self.respond(
                    200, b'{"login":"ok"}', [("Set-Cookie", f"session={TOKEN}; Path=/; HttpOnly")]
                )
            elif self.path == "/redirect-login":
                self.respond(
                    302,
                    headers=[
                        ("Location", "/protected"),
                        ("Set-Cookie", f"session={TOKEN}; Path=/; HttpOnly"),
                    ],
                )
            elif self.path == "/protected":
                if f"session={TOKEN}" in self.headers.get("Cookie", ""):
                    self.respond(200, b'{"private":"authorized"}')
                else:
                    self.respond(401, b'{"error":"login required"}')
            elif self.path == "/cookie-attributes":
                self.respond(
                    200,
                    b"{}",
                    [
                        (
                            "Set-Cookie",
                            f"secure_token={TOKEN}; Domain=127.0.0.1; Path=/private; Secure; HttpOnly; SameSite=Strict; Max-Age=600",
                        )
                    ],
                )
            elif self.path == "/redirect-host":
                self.respond(
                    302, headers=[("Location", f"http://localhost:{self.server.server_port}/echo")]
                )
            elif self.path == "/download":
                self.respond(200, b"\x00\xff" * 600_000)
            elif self.path == "/drip":
                self.send_response(200)
                self.send_header("Content-Length", "20")
                self.end_headers()
                try:
                    for _ in range(20):
                        self.wfile.write(b"a")
                        self.wfile.flush()
                        time.sleep(0.05)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.respond(
                    200,
                    json.dumps(
                        {
                            "method": self.command,
                            "body": body.decode(),
                            "headers": {
                                name.lower(): value for name, value in self.headers.items()
                            },
                        }
                    ).encode(),
                    [("X-Auth-Token", TOKEN), ("Set-Cookie", "ignored=synthetic; Path=/")],
                )

        do_GET = handle_request
        do_HEAD = handle_request
        do_POST = handle_request
        do_PUT = handle_request
        do_PATCH = handle_request
        do_DELETE = handle_request
        do_OPTIONS = handle_request
        do_TRACE = handle_request
        do_CONNECT = handle_request
        do_PURGE = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("body_key", ["json", "data"])
def test_login_cookie_session_survives_subsequent_client_and_worker_restart(
    http_server, tmp_path, body_key
):
    directory = tmp_path / "private-sessions"
    login = request_http(
        http_server + "/login",
        {
            "method": "POST",
            "session": "account",
            body_key: {"username": "tester", "password": "synthetic-password"},
        },
        directory,
    )
    assert login["status_code"] == 200
    assert TOKEN not in json.dumps(login)
    assert "set-cookie" not in login["headers"]
    protected = request_http(http_server + "/protected", {"session": "account"}, directory)
    assert protected["status_code"] == 200
    assert json.loads(protected["text"]) == {"private": "authorized"}
    assert TOKEN not in json.dumps(protected)
    assert (
        request_http(http_server + "/protected", {"session": "other"}, directory)["status_code"]
        == 401
    )
    files = list(directory.glob("*.json"))
    assert any(TOKEN in file.read_text() for file in files)
    assert all(file.stat().st_mode & 0o777 == 0o600 for file in directory.iterdir())
    assert directory.stat().st_mode & 0o777 == 0o700


def test_redirect_login_keeps_cookie_and_allows_disabling_redirects(http_server, tmp_path):
    result = request_http(http_server + "/redirect-login", {}, tmp_path / "sessions")
    assert result["status_code"] == 200
    assert result["url"] == http_server + "/protected"
    assert TOKEN not in json.dumps(result)
    not_followed = request_http(
        http_server + "/redirect-login", {"follow_redirects": False}, tmp_path / "sessions"
    )
    assert not_followed["status_code"] == 302


@pytest.mark.parametrize(
    "method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "PURGE"]
)
def test_arbitrary_http_methods_raw_bodies_and_custom_ports(http_server, tmp_path, method):
    result = request_http(
        http_server + "/echo",
        {"method": method, "body": "raw payload", "headers": {"X-Example": "custom"}},
        tmp_path / "sessions",
    )
    echo = json.loads(result["text"])
    assert echo["method"] == method
    assert echo["body"] == "raw payload"
    assert echo["headers"]["x-example"] == "custom"
    assert "x-auth-token" not in result["headers"]
    assert "set-cookie" not in result["headers"]


def test_head_returns_status_without_body(http_server, tmp_path):
    result = request_http(http_server + "/echo", {"method": "HEAD"}, tmp_path / "sessions")
    assert result["status_code"] == 200
    assert result["text"] == ""
    assert result["bytes_received"] == 0


def test_connect_returns_tunnel_status(http_server, tmp_path):
    result = request_http(http_server + "/echo", {"method": "CONNECT"}, tmp_path / "sessions")
    assert result["status_code"] == 200
    assert result["text"] == ""


def test_secure_cookie_attributes_survive_private_disk_roundtrip(http_server, tmp_path):
    directory = tmp_path / "sessions"
    request_http(http_server + "/cookie-attributes", {}, directory)
    cookie = json.loads(next(directory.glob("*.json")).read_text())[0]
    assert cookie["path"] == "/private"
    assert cookie["domain"] == ".127.0.0.1"
    assert cookie["domain_specified"] is True
    assert cookie["secure"] is True
    assert cookie["discard"] is False
    assert cookie["expires"] > time.time()
    assert "HttpOnly" in cookie["rest"]
    assert cookie["rest"]["SameSite"] == "Strict"
    echoed = request_http(http_server + "/private/echo", {}, directory)
    assert "secure_token" not in json.loads(echoed["text"])["headers"].get("cookie", "")


def test_redirect_to_another_host_does_not_forward_request_credentials(http_server, tmp_path):
    result = request_http(
        http_server + "/redirect-host",
        {
            "headers": {
                "Authorization": "Bearer synthetic-secret",
                "X-API-Key": "synthetic-secret",
                "X-Custom-Secret": "synthetic-secret",
                "Cookie": "manual=synthetic-secret",
            }
        },
        tmp_path / "sessions",
    )
    headers = json.loads(result["text"])["headers"]
    for name in ["authorization", "x-api-key", "x-custom-secret", "cookie"]:
        assert name not in headers


def test_basic_auth_and_json_null_are_sent(http_server, tmp_path):
    result = request_http(
        http_server + "/echo",
        {"method": "POST", "json": None, "auth": ["username", "password"]},
        tmp_path / "sessions",
    )
    echo = json.loads(result["text"])
    assert echo["body"] == "null"
    assert echo["headers"]["authorization"] == "Basic dXNlcm5hbWU6cGFzc3dvcmQ="
    assert echo["headers"]["content-type"] == "application/json"


def test_download_streams_past_inline_limit_atomically(http_server, tmp_path):
    destination = tmp_path / "downloads" / "binary.dat"
    result = request_http(
        http_server + "/download", {}, tmp_path / "sessions", download_path=destination
    )
    expected = b"\x00\xff" * 600_000
    assert destination.read_bytes() == expected
    assert result["bytes_received"] == len(expected)
    assert result["sha256"] == hashlib.sha256(expected).hexdigest()
    assert result["downloaded"] is True
    assert result["truncated"] is True
    assert len(result["text"]) == 16_000
    assert not list(destination.parent.glob(".rocky-http-*"))


def test_inline_response_is_bounded(http_server, tmp_path):
    result = request_http(http_server + "/download", {}, tmp_path / "sessions")
    assert result["bytes_received"] == 1_000_000
    assert result["truncated"] is True
    assert len(result["text"]) == 16_000


def test_total_timeout_interrupts_dripping_response_and_preserves_destination(
    http_server, tmp_path
):
    destination = tmp_path / "report.txt"
    destination.write_text("original")
    started = time.monotonic()
    with pytest.raises(HTTPToolError, match="timed out"):
        request_http(
            http_server + "/drip",
            {"timeout": 0.15},
            tmp_path / "sessions",
            download_path=destination,
        )
    assert time.monotonic() - started < 0.75
    assert destination.read_text() == "original"
    assert not list(tmp_path.glob(".rocky-http-*"))


@pytest.mark.parametrize(
    "url,options",
    [
        ("file:///etc/passwd", {}),
        ("https://example.com", {"method": "GET\r\nAuthorization: secret"}),
        ("https://example.com", {"timeout": 0}),
        ("https://example.com", {"timeout": float("nan")}),
        ("https://example.com", {"json": {}, "body": "secret"}),
        ("https://example.com", {"headers": {"Authorization": ["secret"]}}),
        ("https://example.com", {"auth": "secret"}),
        ("https://example.com", {"session": ""}),
    ],
)
def test_invalid_request_options_fail_without_echoing_secrets(tmp_path, url, options):
    with pytest.raises(HTTPToolError) as error:
        request_http(url, options, tmp_path / "sessions")
    assert "secret" not in str(error.value)


def test_session_file_rejects_unsafe_permissions(http_server, tmp_path):
    directory = tmp_path / "sessions"
    request_http(http_server + "/echo", {}, directory)
    next(directory.glob("*.json")).chmod(0o644)
    with pytest.raises(HTTPToolError, match="private regular"):
        request_http(http_server + "/protected", {}, directory)
