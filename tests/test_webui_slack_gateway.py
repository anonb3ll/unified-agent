"""Slack Gateway status Control Center route (task #1185).

Status-only surface: LIVE / DORMANT / MISCONFIGURED for the Slack HTTP
ingress adapter (``slack_gateway.handle_http_request``), plus recent ingress
activity read from the existing evidence-event log. No route here may send a
Slack message, dispatch a coordination command, or surface a secret value —
only presence booleans for the signing secret and bot token.
"""

from __future__ import annotations

import inspect
import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import events, server, storage, webui
from unified_agent.webui import api as webui_api
from unified_agent.webui.routes import web_routes

TOKEN = f"tok-slackgw-{secrets.token_urlsafe(8)}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.delenv("UA_SLACK_ENABLED", raising=False)
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("UA_SLACK_USERS", raising=False)
    monkeypatch.delenv("UA_SLACK_CHANNEL_IDS", raising=False)
    monkeypatch.delenv("UA_SLACK_CHANNEL_MAP", raising=False)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class TestSlackGatewayStatus:
    def test_dormant_when_gateway_disabled(self, client):
        res = client.get("/api/slack_gateway", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "DORMANT"
        assert body["enabled"] is False
        assert any("UA_SLACK_ENABLED" in reason for reason in body["reasons"])

    def test_misconfigured_when_enabled_without_secret(self, client, monkeypatch):
        monkeypatch.setenv("UA_SLACK_ENABLED", "1")
        monkeypatch.setenv("UA_SLACK_USERS", "U1")
        monkeypatch.setenv("UA_SLACK_CHANNEL_IDS", "C1")
        res = client.get("/api/slack_gateway", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "MISCONFIGURED"
        assert any("SLACK_SIGNING_SECRET" in reason for reason in body["reasons"])

    def test_live_when_fully_configured(self, client, monkeypatch):
        monkeypatch.setenv("UA_SLACK_ENABLED", "1")
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "signing-secret-value")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "bot-token-value")
        monkeypatch.setenv("UA_SLACK_USERS", "U1,U2")
        monkeypatch.setenv("UA_SLACK_CHANNEL_IDS", "C1,C2")
        res = client.get("/api/slack_gateway", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "LIVE"
        assert body["reasons"] == []
        assert body["channel_count"] == 2
        assert body["user_allowlist_count"] == 2

    def test_never_forwards_a_secret_value(self, client, monkeypatch):
        monkeypatch.setenv("UA_SLACK_ENABLED", "1")
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "super-secret-signing-value")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "fake-super-secret-bot-token-value")
        monkeypatch.setenv("UA_SLACK_USERS", "U1")
        monkeypatch.setenv("UA_SLACK_CHANNEL_IDS", "C1")
        res = client.get("/api/slack_gateway", headers=_auth())
        assert res.status_code == 200, res.text
        blob = str(res.json())
        assert "super-secret-signing-value" not in blob
        assert "fake-super-secret-bot-token-value" not in blob
        # Only presence booleans, never the raw value.
        assert res.json()["signing_secret_present"] is True
        assert res.json()["bot_token_present"] is True

    def test_recent_ingress_activity_surfaced(self, client):
        events.record_event(
            "slack.inbound",
            payload={"source": "slack", "event_id": "ev1", "command": "task"},
            originator="U_TEST",
            notify=False,
        )
        events.record_event(
            "slack.dispatch.failed",
            payload={"event_id": "ev1", "error_type": "ValueError"},
            originator="U_TEST",
            notify=False,
        )
        res = client.get("/api/slack_gateway", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        channels = {row["channel"] for row in body["recent_events"]}
        assert "slack.inbound" in channels
        assert "slack.dispatch.failed" in channels
        assert body["recent_error_count"] >= 1

    def test_requires_auth(self, client):
        assert client.get("/api/slack_gateway").status_code == 401


class TestSlackGatewayUnavailable:
    def test_named_error_when_projection_unavailable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("UA_WORKSPACE", str(ws))
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        storage.init_db()

        def boom(**_kwargs):
            raise OSError("event log unreachable")

        monkeypatch.setattr(webui_api, "call_tool", boom)
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as client:
            res = client.get("/api/slack_gateway", headers=_auth())
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["error"] == "slack gateway status unavailable"
            assert body["code"] == "slack_gateway_unavailable"


class TestSlackGatewayReadOnlyInvariant:
    def test_slack_gateway_route_is_get_only(self):
        paths = [
            (route.path, frozenset(m for m in (route.methods or set()) if m != "HEAD"))
            for route in web_routes()
            if "/slack_gateway" in route.path
        ]
        assert paths, "expected the slack_gateway read route to be registered"
        for path, methods in paths:
            assert methods == frozenset({"GET"}), f"{path} must be GET-only, got {methods}"
        assert not any("/api/action/slack_gateway" in route.path for route in web_routes())

    def test_api_helper_never_sends_or_dispatches(self):
        src = inspect.getsource(webui_api.slack_gateway_payload)
        body = src.split('"""', 2)[-1]  # drop the docstring, which documents what it avoids
        for forbidden in (
            "handle_http_request(",
            "slack_gateway_module.dispatch(",
            "schedule_reply_to_inbound",
        ):
            assert forbidden not in body, f"slack_gateway_payload must not call {forbidden}"
