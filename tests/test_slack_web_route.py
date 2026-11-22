from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from unified_agent import server, storage, webui
from unified_agent.slack_gateway import MAX_BODY_BYTES
from unified_agent.webui import routes


def test_slack_route_uses_signature_instead_of_hub_bearer_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:unused")
    monkeypatch.setenv("UA_SLACK_ENABLED", "1")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "signing")
    monkeypatch.setenv("UA_SLACK_USERS", "U_OPERATOR")
    monkeypatch.setenv("UA_SLACK_CHANNEL_MAP", "C_REQUESTS=requests")
    storage.init_db()
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-route",
            "event": {"user": "U_OPERATOR", "channel": "C_REQUESTS", "text": "/contact"},
        }
    ).encode()
    timestamp = str(int(time.time()))
    digest = hmac.new(
        b"signing", b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        response = client.post(
            "/api/slack/events",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Request-Timestamp": timestamp,
                "X-Slack-Signature": f"v0={digest}",
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["dispatched"] is True


def test_slack_route_rejects_oversized_content_length_before_body_read(monkeypatch):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/slack/events",
            "headers": [(b"content-length", str(MAX_BODY_BYTES + 1).encode())],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 80),
            "scheme": "http",
            "http_version": "1.1",
            "root_path": "",
            "app": None,
        },
        receive=lambda: pytest.fail("request body must not be read"),
    )

    response = asyncio.run(routes.slack_events(request))
    assert response.status_code == 400
    assert b"exceeds limit" in response.body


def test_slack_route_offloads_gateway_to_worker_thread(monkeypatch):
    import threading

    called_from = []

    def fake_handler(body, *, headers):
        called_from.append(threading.current_thread())
        return {"dispatched": True}

    monkeypatch.setattr(
        "unified_agent.webui.routes.slack_gateway.handle_http_request", fake_handler
    )
    body = b"{}"
    messages = iter([{"type": "http.request", "body": body, "more_body": False}])

    async def receive():
        return next(messages)

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/slack/events",
            "headers": [(b"content-length", str(len(body)).encode())],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 80),
            "scheme": "http",
            "http_version": "1.1",
            "root_path": "",
            "app": None,
        },
        receive=receive,
    )
    response = asyncio.run(routes.slack_events(request))
    assert response.status_code == 200
    assert called_from and called_from[0] is not threading.current_thread()
