"""scripts/cloud_hub_client.py: the Claude Code on the web hub client (task #2446).

The HTTP leg is replaced by a fake hub, so no socket opens. The real hub path
(client credentials -> MCP ``profile_status`` as ``cloud_agent1``) is covered by
tests/test_mcp_oauth.py and tests/test_cloud_Agent17_chief_of_staff.py.
"""

from __future__ import annotations

import importlib.util
import json
import secrets
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "cloud_hub_client.py"
_spec = importlib.util.spec_from_file_location("cloud_hub_client", _PATH)
client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(client)

SECRET = secrets.token_urlsafe(24)
TOKEN = secrets.token_urlsafe(24)
CONFIG = {
    client.ENV_URL: "https://hub.example.test/mcp",
    client.ENV_CLIENT_ID: "cloud-agent1-test",
    client.ENV_CLIENT_SECRET: SECRET,
}


class FakeHub:
    """Answers the token endpoint and the three MCP posts the client makes."""

    def __init__(self, *, identity="cloud_agent1", auth_method="client_credentials", sse=True):
        self.identity = identity
        self.auth_method = auth_method
        self.sse = sse
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if url.endswith(client.TOKEN_PATH):
            form = dict(p.split("=", 1) for p in body.decode().split("&"))
            if form.get("client_secret") != SECRET:
                return 401, {}, json.dumps({"error": "invalid_client"}).encode()
            return 200, {}, json.dumps({"access_token": TOKEN}).encode()
        assert headers["Authorization"] == "Bearer " + TOKEN
        message = json.loads(body)
        if message.get("method") == "initialize":
            return 200, {"mcp-session-id": "sid-1"}, b"{}"
        if message.get("method") == "notifications/initialized":
            return 202, {}, b""
        assert headers["Mcp-Session-Id"] == "sid-1"
        status = {"identity": self.identity, "profile": "developer"}
        status["auth_method"] = self.auth_method
        rpc = {"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": status}}
        if self.sse:
            text = "event: message\ndata: " + json.dumps(rpc) + "\n\n"
            return 200, {"content-type": "text/event-stream"}, text.encode()
        return 200, {"content-type": "application/json"}, json.dumps(rpc).encode()


@pytest.fixture
def hub(monkeypatch):
    fake = FakeHub()
    monkeypatch.setattr(client, "_http", fake)
    for name, value in CONFIG.items():
        monkeypatch.setenv(name, value)
    return fake


def test_settings_need_all_three_values_and_https():
    assert client.settings({}) is None
    partial = {k: v for k, v in CONFIG.items() if k != client.ENV_CLIENT_SECRET}
    assert client.settings(partial) is None
    assert client.missing(partial) == [client.ENV_CLIENT_SECRET]
    assert client.settings(CONFIG) == tuple(CONFIG.values())
    with pytest.raises(client.HubClientError, match="https"):
        client.settings({**CONFIG, client.ENV_URL: "http://hub.example.test/mcp"})
    # Plain http only for a loopback test hub.
    assert client.settings({**CONFIG, client.ENV_URL: "http://127.0.0.1:8765/mcp"})


@pytest.mark.parametrize("sse", [True, False])
def test_check_verifies_cloud_Agent1_over_mcp(hub, sse):
    hub.sse = sse
    status = client.check()
    assert status["identity"] == "cloud_agent1"
    token_call = hub.calls[0]
    assert token_call[1] == "https://hub.example.test/oauth/client-credentials"
    assert [json.loads(c[3]).get("method") for c in hub.calls[1:]] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]


@pytest.mark.parametrize(
    ("identity", "auth_method", "expected"),
    [
        ("server_agent8", "client_credentials", "wrong label"),
        ("cloud_agent1", None, "auth_method"),
    ],
)
def test_check_refuses_another_identity_or_auth_leg(hub, identity, auth_method, expected):
    hub.identity, hub.auth_method = identity, auth_method
    with pytest.raises(client.HubClientError, match=expected):
        client.check()


def test_headers_prints_only_the_authorization_header(hub, capsys):
    assert client.main(["headers"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == {"Authorization": "Bearer " + TOKEN}
    assert SECRET not in out.out + out.err


def test_failures_name_the_step_and_never_the_secret(hub, monkeypatch, capsys):
    monkeypatch.setenv(client.ENV_CLIENT_SECRET, "wrong-" + SECRET)
    assert client.main(["headers"]) == 1
    assert client.main(["check"]) == 1
    err = capsys.readouterr().err
    assert "token exchange refused (invalid_client)" in err
    assert SECRET not in err


def test_unconfigured_session_reports_fixture_mode(monkeypatch, tmp_path, capsys):
    for name in CONFIG:
        monkeypatch.delenv(name, raising=False)
    mode = tmp_path / "mode"
    mode.write_text("fixture: UA_HUB_MCP_URL unset\n")
    monkeypatch.setattr(client, "MODE_FILE", mode)
    assert client.main(["configured"]) == 1
    assert client.main(["status"]) == 0
    assert capsys.readouterr().out == "unified-agent mode: fixture: UA_HUB_MCP_URL unset\n"
    assert client.main(["check"]) == 1
    assert "not configured" in capsys.readouterr().err


def test_hub_mode_status_runs_the_check(hub, monkeypatch, tmp_path, capsys):
    mode = tmp_path / "mode"
    mode.write_text("hub: https://hub.example.test/mcp as cloud_agent1\n")
    monkeypatch.setattr(client, "MODE_FILE", mode)
    assert client.main(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("unified-agent mode: hub")
    assert out[1] == "unified-agent: hub reachable: identity cloud_agent1, profile developer"
