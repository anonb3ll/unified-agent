"""Notify Delivery Status Control Center route (task #1183).

Operator confirmation of outbound notify/iMessage delivery outcomes
(success/fail/last send) without reading agent journals. Strictly
read-only: no route here may send a notification — ``notify`` /
``batch_notify`` stay the only send path, MCP/approval-gated — and no route
ever surfaces the notification message text, only outcome metadata.
"""

from __future__ import annotations

import inspect
import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import server, storage, webui
from unified_agent.tools import notify as notify_tool
from unified_agent.webui import api as webui_api
from unified_agent.webui.routes import web_routes

TOKEN = f"tok-notifydeliv-{secrets.token_urlsafe(8)}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _fake_notify(delivered: bool, error: str | None = None):
    def _impl(message: str, timeout_s: int = 60) -> dict:
        return {"delivered": delivered, "chars": len(message), "error": error}

    return _impl


class TestNotifyDeliveryTool:
    """Ground truth for the route: the real notify_delivery_status() tool."""

    def test_notify_records_a_success_and_a_failure(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
        (tmp_path / "ws").mkdir()
        storage.init_db()

        monkeypatch.setattr(notify_tool, "_notify_impl", _fake_notify(True))
        notify_tool.notify("first message")

        monkeypatch.setattr(notify_tool, "_notify_impl", _fake_notify(False, "boom"))
        notify_tool.notify("second message")

        status = notify_tool.notify_delivery_status()
        assert status["count"] == 2
        assert status["success_count"] == 1
        assert status["failure_count"] == 1
        assert status["last_delivery"]["delivered"] is False
        assert status["last_delivery"]["error"] == "boom"
        # Never the message text.
        blob = str(status)
        assert "first message" not in blob
        assert "second message" not in blob


class TestNotifyDeliveryRoute:
    def test_reports_recent_deliveries(self, client, monkeypatch):
        monkeypatch.setattr(notify_tool, "_notify_impl", _fake_notify(True))
        notify_tool.notify("hello operator")

        res = client.get("/api/notify_delivery", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body.get("error") in (None, "")
        assert body["count"] == 1
        assert body["success_count"] == 1
        assert body["last_delivery"]["delivered"] is True

    def test_never_forwards_the_message_text(self, client, monkeypatch):
        monkeypatch.setattr(notify_tool, "_notify_impl", _fake_notify(True))
        notify_tool.notify("do not leak this exact secret sentence")

        res = client.get("/api/notify_delivery", headers=_auth())
        assert res.status_code == 200, res.text
        assert "do not leak this exact secret sentence" not in res.text

    def test_requires_auth(self, client):
        assert client.get("/api/notify_delivery").status_code == 401


class TestNotifyDeliveryUnavailable:
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
            raise OSError("delivery log unreachable")

        monkeypatch.setattr(webui_api, "call_tool", boom)
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as client:
            res = client.get("/api/notify_delivery", headers=_auth())
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["error"] == "notify delivery status unavailable"
            assert body["code"] == "notify_delivery_unavailable"
            assert body["deliveries"] == []


class TestNotifyDeliveryReadOnlyInvariant:
    def test_notify_delivery_route_is_get_only(self):
        paths = [
            (route.path, frozenset(m for m in (route.methods or set()) if m != "HEAD"))
            for route in web_routes()
            if "/notify_delivery" in route.path
        ]
        assert paths, "expected the notify_delivery read route to be registered"
        for path, methods in paths:
            assert methods == frozenset({"GET"}), f"{path} must be GET-only, got {methods}"
        assert not any("/api/action/notify_delivery" in route.path for route in web_routes())

    def test_api_helper_never_sends_a_notification(self):
        src = inspect.getsource(webui_api.notify_delivery_payload)
        body = src.split('"""', 2)[-1]
        for forbidden in ("notify(", "batch_notify(", "flush_digest("):
            assert forbidden not in body, f"notify_delivery_payload must not call {forbidden}"
