import sys
from unittest.mock import Mock

import httpx
import pytest

from rocky.cli import main
from rocky.config import Settings


def client_transport(monkeypatch, settings):
    requests = []
    real_client = httpx.Client

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    monkeypatch.setattr("rocky.cli.Settings", lambda: settings)
    monkeypatch.delenv("ROCKY_API_URL", raising=False)
    monkeypatch.setattr(
        "rocky.cli.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    return requests


def test_local_cli_reads_goals_without_token_or_authorization_header(monkeypatch, capsys):
    requests = client_transport(monkeypatch, Settings(auth_mode="local", api_token=None))
    monkeypatch.setattr(sys, "argv", ["rocky", "list"])
    main()
    assert capsys.readouterr().out.strip() == "[]"
    assert len(requests) == 1
    assert str(requests[0].url) == "http://127.0.0.1:8000/goals"
    assert "authorization" not in requests[0].headers


def test_token_mode_cli_still_requires_configured_credential(monkeypatch, capsys):
    requests = client_transport(monkeypatch, Settings(auth_mode="token", api_token=None))
    monkeypatch.setattr(sys, "argv", ["rocky", "list"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "ROCKY_API_TOKEN" in capsys.readouterr().err
    assert requests == []


def test_token_mode_cli_sends_explicit_owner_credential(monkeypatch):
    requests = client_transport(
        monkeypatch, Settings(auth_mode="token", api_token="cli-owner-test-sentinel")
    )
    monkeypatch.setattr(sys, "argv", ["rocky", "list"])
    main()
    assert requests[0].headers["authorization"] == "Bearer cli-owner-test-sentinel"


def test_api_command_disables_proxy_header_rewriting(monkeypatch):
    import uvicorn

    settings = Settings(auth_mode="local", api_token=None)
    app = object()
    runner = Mock()
    monkeypatch.setattr("rocky.cli.Settings", lambda: settings)
    monkeypatch.setattr("rocky.api.create_app", lambda value: app)
    monkeypatch.setattr(uvicorn, "run", runner)
    monkeypatch.setattr(sys, "argv", ["rocky", "api"])
    main()
    runner.assert_called_once_with(
        app, host="127.0.0.1", port=8000, proxy_headers=False, access_log=False
    )
