"""Token-free local access keeps a peer, Host and browser-origin boundary."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rocky.api import create_app
from rocky.config import Settings
from rocky.store import Store

_BASE = "http://127.0.0.1:8000"
_GOAL = {
    "objective": "Write a local report.",
    "acceptance_criteria": ["Explain the evidence."],
    "deliverables": [{"path": "report.md"}],
}
_MODEL = {"model": "gpt-6-luna", "reasoning_effort": "low"}


@pytest.fixture
def app_store(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'local.db'}")
    settings = Settings(auth_mode="local", host="127.0.0.1", api_token=None)
    try:
        yield create_app(settings, store), store
    finally:
        store.close()


@pytest.fixture
def local_client(app_store):
    app, _ = app_store
    with TestClient(app, client=("127.0.0.1", 45678), base_url=_BASE) as client:
        yield client


def assert_denied(response):
    assert response.status_code == 403
    assert response.json() == {"detail": "Local access denied"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "access-control-allow-origin" not in response.headers


def test_loopback_api_works_without_any_bearer_token(local_client, app_store):
    _, store = app_store
    assert local_client.get("/health").json() == {"status": "ok", "service": "rocky"}
    assert local_client.get("/goals").json() == []
    assert local_client.put("/model-settings", json=_MODEL).json() == _MODEL
    assert local_client.get("/model-settings").json() == _MODEL
    created = local_client.post("/goals", json=_GOAL)
    assert created.status_code == 202
    assert created.json()["model_settings"] == _MODEL
    goal_id = created.json()["goal_id"]
    assert local_client.get(f"/goals/{goal_id}").status_code == 200
    assert local_client.get(f"/goals/{goal_id}/events").json() == []
    assert (
        local_client.post(f"/goals/{goal_id}/wake", json={"message": "Continue"}).status_code == 202
    )
    assert local_client.post(f"/goals/{goal_id}/cancel").status_code == 202
    assert store.is_cancel_requested(goal_id)
    assert (
        local_client.get("/goals", headers={"Authorization": "Bearer irrelevant"}).status_code
        == 200
    )


@pytest.mark.parametrize(
    ("base_url", "peer", "origin"),
    [
        (_BASE, "127.0.0.1", _BASE),
        ("http://localhost:8000", "127.0.0.1", "http://localhost:8000"),
        ("http://127.0.0.1", "127.0.0.1", "http://127.0.0.1:80"),
        ("https://localhost", "127.0.0.1", "https://LOCALHOST:443"),
        ("http://[::1]:8000", "::1", "http://[::1]:8000"),
        ("http://[0:0:0:0:0:0:0:1]:8000", "::1", "http://[::1]:8000"),
        ("http://[::ffff:127.0.0.1]:8000", "::ffff:127.0.0.1", "http://[::ffff:127.0.0.1]:8000"),
    ],
)
def test_matching_origin_loopback_and_canonical_default_ports_are_allowed(
    app_store, base_url, peer, origin
):
    app, _ = app_store
    with TestClient(app, client=(peer, 45678), base_url=base_url) as client:
        response = client.get(
            "/model-settings", headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"}
        )
        assert response.status_code == 200
        assert response.json()["model"]


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "",
        "http://evil.example",
        "http://localhost:8000",
        "https://127.0.0.1:8000",
        "http://127.0.0.1:9000",
        "http://127.0.0.1:8000/",
        "http://127.0.0.1:8000?",
        "http://127.0.0.1:8000#",
        "http://user@127.0.0.1:8000",
        "http://127.0.0.1:bad",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:8000\\",
        "http://127.0.0.1:8000\r\n",
        "http://127.0.0.1:\t8000",
        "http://[::1%lo0]:8000",
    ],
)
def test_cross_origin_opaque_or_malformed_origins_are_rejected_before_writes(
    local_client, app_store, origin
):
    _, store = app_store
    response = local_client.post("/goals", json=_GOAL, headers={"Origin": origin})
    assert_denied(response)
    assert store.pending_goals() == []


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "localhost.evil.example",
        "127.0.0.1.evil.example",
        "localhost.",
        "2130706433",
        "0x7f000001",
        "127.1",
        "",
        "::1",
        "[::1",
        "[::1]evil",
        "[::1]:",
        "[::1%lo0]:8000",
        "localhost:",
        "localhost:0",
        "localhost:65536",
        "localhost:8000/",
        "user@localhost:8000",
        "localhost,127.0.0.1",
        "localhost:8000?",
        "localhost:8000#",
        "localhost\\evil",
        "localhost :8000",
        "local\thost:8000",
        "localhost\r\n:8000",
    ],
)
def test_dns_rebinding_and_malformed_host_authorities_cannot_access_api(local_client, host):
    assert_denied(local_client.get("/goals", headers={"Host": host}))


def test_non_ascii_host_is_rejected_as_fixed_json_not_a_server_error(local_client):
    response = local_client.get("/goals", headers=[(b"host", b"\xff")])
    assert_denied(response)


@pytest.mark.parametrize("missing", ["host", "client"])
def test_missing_asgi_connection_information_cannot_fall_back_to_server_name(app_store, missing):
    app, _ = app_store

    async def missing_connection(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            if missing == "host":
                # TestClient repairs a missing HTTP Host on send. Strip it at
                # the ASGI boundary to exercise the actual missing-header case.
                scope["headers"] = [
                    (key, value) for key, value in scope["headers"] if key.lower() != b"host"
                ]
            else:
                scope["client"] = None
        await app(scope, receive, send)

    with TestClient(missing_connection, client=("127.0.0.1", 45678), base_url=_BASE) as client:
        assert_denied(client.get("/model-settings"))


@pytest.mark.parametrize(
    "headers",
    [
        [("Host", "127.0.0.1:8000"), ("Host", "evil.example")],
        [("Host", "127.0.0.1:8000"), ("Host", "127.0.0.1:8000")],
        [("Origin", _BASE), ("Origin", _BASE)],
        [("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "cross-site")],
    ],
)
def test_duplicate_security_headers_are_rejected(local_client, headers):
    assert_denied(local_client.get("/model-settings", headers=headers))


@pytest.mark.parametrize("peer", ["203.0.113.7", "10.0.0.4", "::2", "localhost", "testclient"])
@pytest.mark.parametrize(
    "path", ["/health", "/goals", "/model-settings", "/provider-settings", "/openapi.json"]
)
def test_remote_or_non_ip_peer_cannot_access_any_route_even_with_spoofed_proxy_headers(
    app_store, peer, path
):
    app, _ = app_store
    with TestClient(app, client=(peer, 45678), base_url=_BASE) as client:
        response = client.get(
            path,
            headers={
                "X-Forwarded-For": "127.0.0.1",
                "Forwarded": "for=127.0.0.1;host=localhost;proto=http",
                "X-Forwarded-Host": "localhost",
                "Authorization": "Bearer irrelevant",
            },
        )
        assert_denied(response)


def test_local_peer_ignores_proxy_headers_instead_of_treating_them_as_authority(local_client):
    response = local_client.get(
        "/model-settings",
        headers={
            "Origin": _BASE,
            "X-Forwarded-For": "203.0.113.7",
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Proto": "https",
            "Forwarded": "for=203.0.113.7;host=evil.example;proto=https",
        },
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/health", None),
        ("GET", "/goals", None),
        ("POST", "/goals", _GOAL),
        ("PUT", "/model-settings", _MODEL),
    ],
)
def test_cross_site_reads_and_writes_are_rejected_even_without_origin(
    local_client, app_store, method, path, body
):
    _, store = app_store
    response = local_client.request(
        method, path, json=body, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert_denied(response)
    assert store.pending_goals() == []


@pytest.mark.parametrize("site", ["", "invalid-site", "same-origin,cross-site"])
def test_unknown_fetch_site_is_rejected(local_client, site):
    assert_denied(local_client.get("/goals", headers={"Sec-Fetch-Site": site}))


@pytest.mark.parametrize("site", ["same-origin", "same-site", "none"])
def test_known_non_cross_site_values_are_allowed(local_client, site):
    assert local_client.get("/goals", headers={"Sec-Fetch-Site": site}).status_code == 200


_NAVIGATION = {
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-User": "?1",
}


@pytest.mark.parametrize(
    "path", ["/", "/goals", "/model-settings", "/provider-settings", "/health", "/openapi.json"]
)
def test_user_activated_navigation_never_bypasses_cross_site_protection(local_client, path):
    assert_denied(local_client.get(path, headers=_NAVIGATION))


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "::", "192.168.1.2", "example.com", "127.0.0.2", "localhost."]
)
def test_local_mode_cannot_bind_to_a_nonapproved_host(host):
    with pytest.raises(ValidationError, match="loopback bind host"):
        Settings(auth_mode="local", host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_local_mode_accepts_explicit_loopback_bind_hosts(host):
    assert Settings(auth_mode="local", host=host, api_token=None).auth_mode == "local"


def test_local_is_the_default_without_requiring_an_api_token(monkeypatch):
    monkeypatch.delenv("ROCKY_AUTH_MODE", raising=False)
    settings = Settings(host="127.0.0.1", api_token=None)
    assert settings.auth_mode == "local"


def test_local_openapi_has_no_bearer_requirement(local_client):
    schema = local_client.get("/openapi.json").json()
    assert not schema.get("components", {}).get("securitySchemes")
    assert "security" not in schema["paths"]["/goals"]["get"]
    assert "security" not in schema["paths"]["/goals"]["post"]


def test_token_mode_allows_remote_binding_and_keeps_bearer_auth_fail_closed(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'token.db'}")
    settings = Settings(auth_mode="token", host="0.0.0.0", api_token="owner-test-token")
    try:
        with TestClient(
            create_app(settings, store),
            client=("203.0.113.7", 45678),
            base_url="http://service.example:8000",
        ) as client:
            assert client.get("/model-settings").status_code == 401
            response = client.get(
                "/model-settings", headers={"Authorization": "Bearer owner-test-token"}
            )
            assert response.status_code == 200
            assert response.json()["model"]
            assert "access-control-allow-origin" not in response.headers
            schema = client.get("/openapi.json").json()
            assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
            assert schema["paths"]["/goals"]["get"]["security"] == [{"HTTPBearer": []}]
        with TestClient(
            create_app(Settings(auth_mode="token", host="0.0.0.0", api_token=None), store)
        ) as client:
            assert client.get("/model-settings").status_code == 503
    finally:
        store.close()
