"""Bounded public text retrieval with a pinned, validated destination address."""

import hashlib
import http.client
import ipaddress
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlsplit

_RESOLVER = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rocky-dns")


class FetchError(ValueError):
    """A deliberately safe, user-facing fetch failure."""


@dataclass(frozen=True)
class FetchedText:
    text: str
    sha256: str


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: tuple, timeout: float, secure: bool):
        super().__init__(host, port, timeout=timeout)
        self._address = address
        self._secure = secure
        self._live_socket: socket.socket | None = None

    def connect(self) -> None:
        family, socktype, proto, _, sockaddr = self._address
        sock = socket.socket(family, socktype, proto)
        self._live_socket = sock
        try:
            sock.settimeout(self.timeout)
            # Use the validated sockaddr directly; no second hostname resolution.
            sock.connect(sockaddr)
            if self._secure:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
                self._live_socket = sock
            self.sock = sock
        except BaseException:
            sock.close()
            raise

    def abort(self) -> None:
        """A wall-clock deadline also interrupts a server trickling headers or body."""
        if self._live_socket is not None:
            try:
                self._live_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._live_socket.close()


def fetch_public_text(
    url: str,
    allowed_domains: list[str],
    *,
    max_bytes: int = 1_000_000,
    timeout: float = 15,
) -> FetchedText:
    """Fetch one allowlisted public URL. Redirects and compressed responses are rejected."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, UnicodeError) as exc:
        raise FetchError("Invalid URL") from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise FetchError("Only HTTP and HTTPS URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise FetchError("URLs containing credentials are forbidden")
    if port != (443 if parsed.scheme == "https" else 80):
        raise FetchError("Only the standard HTTP and HTTPS ports are allowed")
    if host not in allowed_domains:
        raise FetchError("The URL hostname is not in the goal's allowed_domains")
    if len(url) > 8000 or any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise FetchError("Invalid URL")

    started = time.monotonic()
    lookup = _RESOLVER.submit(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    try:
        addresses = lookup.result(timeout=timeout)
    except TimeoutError as exc:
        lookup.cancel()
        raise FetchError("Hostname resolution timed out") from exc
    except OSError as exc:
        raise FetchError("Could not resolve the URL hostname") from exc
    if not addresses:
        raise FetchError("Could not resolve the URL hostname")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise FetchError("Private, local and reserved network addresses are forbidden")
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise FetchError("URL retrieval timed out")

    connection = _PinnedHTTPConnection(
        host, port, addresses[0], remaining, parsed.scheme == "https"
    )
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    deadline = threading.Timer(remaining, connection.abort)
    deadline.daemon = True
    deadline.start()
    try:
        connection.request(
            "GET",
            path,
            headers={
                "User-Agent": "Rocky/0.1 (+goal-scoped text retrieval)",
                "Accept": "text/*, application/json, application/xml",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise FetchError("Redirects are not followed; request the destination URL explicitly")
        if response.status < 200 or response.status >= 300:
            raise FetchError(f"The server returned HTTP {response.status}")
        encoding = response.getheader("Content-Encoding", "identity").lower().strip()
        if encoding not in {"", "identity"}:
            raise FetchError("Compressed responses are not supported")
        content_type = response.headers.get_content_type()
        if not content_type.startswith("text/") and content_type not in {
            "application/json",
            "application/ld+json",
            "application/xml",
            "application/xhtml+xml",
            "application/rss+xml",
            "application/atom+xml",
        }:
            raise FetchError("Only text, JSON and XML responses are supported")
        chunks: list[bytes] = []
        size = 0
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise FetchError("URL retrieval timed out")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise FetchError("The response exceeds the maximum download size")
        body = b"".join(chunks)
        charset = response.headers.get_content_charset() or "utf-8"
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        return FetchedText(text=text, sha256=hashlib.sha256(body).hexdigest())
    except FetchError:
        raise
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise FetchError("URL retrieval failed or timed out") from exc
    finally:
        deadline.cancel()
        connection.close()
