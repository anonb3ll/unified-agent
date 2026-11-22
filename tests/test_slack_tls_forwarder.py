"""Unit tests for the stateless Slack TLS forwarder scaffold (task #558)."""

from __future__ import annotations

import hashlib
import hmac
import io
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

FORWARDER_PATH = (
    Path(__file__).resolve().parents[1] / "deploy" / "gcp" / "slack-gateway" / "forwarder.py"
)


def _load_forwarder():
    import importlib.util

    spec = importlib.util.spec_from_file_location("ua_slack_tls_forwarder", FORWARDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fwd = _load_forwarder()


def _signed(
    body: bytes, *, timestamp: str = "1700000000", secret: str = "signing"
) -> dict[str, str]:
    digest = hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": f"v0={digest}",
    }


def test_verify_accepts_valid_and_rejects_replay():
    body = b'{"type":"event_callback"}'
    headers = _signed(body)
    assert fwd.verify_slack_signature(
        "signing",
        headers["X-Slack-Request-Timestamp"],
        body,
        headers["X-Slack-Signature"],
        now=1700000000,
    )
    assert not fwd.verify_slack_signature(
        "signing",
        headers["X-Slack-Request-Timestamp"],
        body,
        headers["X-Slack-Signature"],
        now=1700000401,
    )
    assert not fwd.verify_slack_signature(
        "wrong",
        headers["X-Slack-Request-Timestamp"],
        body,
        headers["X-Slack-Signature"],
        now=1700000000,
    )


def test_load_config_requires_secret_and_hub_path():
    with pytest.raises(fwd.ForwarderError, match="SLACK_SIGNING_SECRET"):
        fwd.load_config({"MAC_HUB_URL": "http://hub.example/api/slack/events"})
    with pytest.raises(fwd.ForwarderError, match="MAC_HUB_URL"):
        fwd.load_config({"SLACK_SIGNING_SECRET": "x"})
    with pytest.raises(fwd.ForwarderError, match="/api/slack/events"):
        fwd.load_config(
            {
                "SLACK_SIGNING_SECRET": "x",
                "MAC_HUB_URL": "http://hub.example/mcp",
            }
        )
    cfg = fwd.load_config(
        {
            "SLACK_SIGNING_SECRET": "x",
            "MAC_HUB_URL": "http://mac.tailnet:8765/api/slack/events",
            "LISTEN_PORT": "9001",
        }
    )
    assert cfg["listen_port"] == 9001
    assert cfg["mac_hub_url"].endswith("/api/slack/events")


def test_handle_rejects_bad_signature_without_forwarding():
    calls: list[object] = []

    def opener(req, timeout=None):  # noqa: ARG001
        calls.append(req)
        raise AssertionError("must not forward")

    body = b'{"type":"url_verification","challenge":"c"}'
    headers = _signed(body, secret="signing")
    headers["X-Slack-Signature"] = "v0=" + ("0" * 64)
    status, payload, _ctype = fwd.handle_slack_events(
        body,
        headers,
        config={
            "signing_secret": "signing",
            "mac_hub_url": "http://mac.example/api/slack/events",
            "forward_timeout_s": 5,
        },
        now=1700000000,
        opener=opener,
    )
    assert status == 401
    assert payload == {"error": "invalid or replayed Slack signature"}
    assert calls == []


def test_handle_forwards_original_body_and_slack_headers():
    captured: dict[str, object] = {}

    class _Resp:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self, _n: int = -1) -> bytes:
            return b'{"challenge":"abc"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["timeout"] = timeout
        return _Resp()

    body = b'{"type":"url_verification","challenge":"abc"}'
    headers = _signed(body)
    status, resp_body, ctype = fwd.handle_slack_events(
        body,
        headers,
        config={
            "signing_secret": "signing",
            "mac_hub_url": "http://mac.example/api/slack/events",
            "forward_timeout_s": 7.5,
        },
        now=1700000000,
        opener=opener,
    )
    assert status == 200
    assert resp_body == b'{"challenge":"abc"}'
    assert "json" in ctype
    assert captured["url"] == "http://mac.example/api/slack/events"
    assert captured["data"] == body
    assert captured["headers"]["x-slack-request-timestamp"] == "1700000000"
    assert captured["headers"]["x-slack-signature"] == headers["X-Slack-Signature"]
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["timeout"] == 7.5


def test_forward_surfaces_hub_http_error_status():
    def opener(req, timeout=None):  # noqa: ARG001
        raise HTTPError(
            req.full_url,
            401,
            "unauthorized",
            hdrs={"Content-Type": "application/json"},
            fp=io.BytesIO(b'{"error":"invalid or replayed Slack signature"}'),
        )

    body = b'{"ok":true}'
    headers = _signed(body)
    status, resp_body, _ctype = fwd.handle_slack_events(
        body,
        headers,
        config={
            "signing_secret": "signing",
            "mac_hub_url": "http://mac.example/api/slack/events",
            "forward_timeout_s": 5,
        },
        now=1700000000,
        opener=opener,
    )
    assert status == 401
    assert b"signature" in resp_body


def test_forward_maps_url_errors_to_forwarder_error():
    def opener(req, timeout=None):  # noqa: ARG001
        raise URLError("connection refused")

    body = b'{"ok":true}'
    headers = _signed(body)
    with pytest.raises(fwd.ForwarderError, match="hub forward failed"):
        fwd.forward_to_hub(
            hub_url="http://mac.example/api/slack/events",
            body=body,
            content_type="application/json",
            timestamp=headers["X-Slack-Request-Timestamp"],
            signature=headers["X-Slack-Signature"],
            timeout_s=5,
            opener=opener,
        )


def test_forwarder_does_not_import_hub_storage():
    source = FORWARDER_PATH.read_text()
    assert "import unified_agent" not in source
    assert "from unified_agent" not in source
    assert "import sqlite3" not in source
    assert fwd.EVENTS_PATH == "/api/slack/events"
