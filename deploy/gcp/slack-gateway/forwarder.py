#!/usr/bin/env python3
"""Stateless public Slack TLS ingress → Mac hub forwarder (task #558).

This process is intentionally *not* the Unified-Agent hub:
- No SQLite / memory.db / task store
- No task, plan, approval, or delegation dispatch
- Verifies Slack v0 HMAC + five-minute replay window, then forwards the
  original signed body and Slack headers to the Mac hub over Tailscale

TLS termination belongs to the operator (Caddy, Cloudflare Tunnel, Tailscale
Funnel, or a managed load balancer). This process binds loopback by default.

Environment (see env.example / RUNBOOK.md — never commit real secrets):

  SLACK_SIGNING_SECRET   required — Slack app signing secret
  MAC_HUB_URL            required — full hub URL ending in /api/slack/events
  LISTEN_HOST            optional — default 127.0.0.1
  LISTEN_PORT            optional — default 8787
  FORWARD_TIMEOUT_S      optional — default 25
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("ua.slack_tls_forwarder")

MAX_BODY_BYTES = 64 * 1024
SIGNATURE_MAX_AGE_S = 300
EVENTS_PATH = "/api/slack/events"
HEALTH_PATHS = frozenset({"/healthz", "/health", "/"})


class ForwarderError(ValueError):
    """Rejected Slack request or misconfigured forwarder."""


def verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    *,
    now: int | None = None,
) -> bool:
    """Mirror hub ``slack_gateway.verify_signature`` (Slack v0 + replay window)."""
    if (
        not signing_secret
        or not timestamp
        or not timestamp.isascii()
        or not timestamp.isdigit()
        or not signature
        or len(body) > MAX_BODY_BYTES
    ):
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - ts) > SIGNATURE_MAX_AGE_S:
        return False
    base = f"v0:{timestamp}:".encode("ascii", "strict") + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    return next((value for key, value in headers.items() if key.lower() == wanted), "")


def load_config(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    secret = env.get("SLACK_SIGNING_SECRET", "").strip()
    hub_url = env.get("MAC_HUB_URL", "").strip()
    if not secret:
        raise ForwarderError("SLACK_SIGNING_SECRET is required")
    if not hub_url:
        raise ForwarderError("MAC_HUB_URL is required")
    parsed = urlparse(hub_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ForwarderError("MAC_HUB_URL must be an absolute http(s) URL")
    if not parsed.path.rstrip("/").endswith("/api/slack/events"):
        raise ForwarderError("MAC_HUB_URL path must end with /api/slack/events")
    try:
        timeout = float(env.get("FORWARD_TIMEOUT_S", "25"))
    except ValueError as exc:
        raise ForwarderError("FORWARD_TIMEOUT_S must be a number") from exc
    return {
        "signing_secret": secret,
        "mac_hub_url": hub_url,
        "listen_host": env.get("LISTEN_HOST", "127.0.0.1").strip() or "127.0.0.1",
        "listen_port": int(env.get("LISTEN_PORT", "8787")),
        "forward_timeout_s": max(1.0, min(timeout, 120.0)),
    }


def forward_to_hub(
    *,
    hub_url: str,
    body: bytes,
    content_type: str,
    timestamp: str,
    signature: str,
    timeout_s: float,
    opener: Any | None = None,
) -> tuple[int, bytes, str]:
    """POST the original signed Slack request to the Mac hub. Returns status/body/ctype."""
    req = urllib.request.Request(
        hub_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type or "application/json",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
            "User-Agent": "ua-slack-tls-forwarder/1.0",
            # Explicit: this hop is not a second control plane.
            "X-UA-Slack-Forwarder": "1",
        },
    )
    open_fn = opener if opener is not None else urllib.request.urlopen
    try:
        with open_fn(req, timeout=timeout_s) as resp:
            resp_body = resp.read(MAX_BODY_BYTES + 1)
            if len(resp_body) > MAX_BODY_BYTES:
                raise ForwarderError("hub response exceeds body limit")
            ctype = resp.headers.get("Content-Type", "application/json")
            return int(resp.status), resp_body, ctype
    except urllib.error.HTTPError as exc:
        err_body = exc.read(MAX_BODY_BYTES)
        ctype = (
            exc.headers.get("Content-Type", "application/json")
            if exc.headers
            else "application/json"
        )
        return int(exc.code), err_body, ctype
    except urllib.error.URLError as exc:
        raise ForwarderError(f"hub forward failed: {type(exc.reason).__name__}") from exc
    except TimeoutError as exc:
        raise ForwarderError("hub forward timed out") from exc


def handle_slack_events(
    body: bytes,
    headers: Mapping[str, str],
    *,
    config: Mapping[str, Any],
    now: int | None = None,
    opener: Any | None = None,
) -> tuple[int, dict[str, Any] | bytes, str]:
    """Verify Slack auth then forward. Returns (status, payload_or_bytes, content_type)."""
    if len(body) > MAX_BODY_BYTES:
        return 400, {"error": "Slack request body exceeds limit"}, "application/json"
    timestamp = _header(headers, "X-Slack-Request-Timestamp")
    signature = _header(headers, "X-Slack-Signature")
    if not verify_slack_signature(
        config["signing_secret"],
        timestamp,
        body,
        signature,
        now=now,
    ):
        return 401, {"error": "invalid or replayed Slack signature"}, "application/json"
    content_type = _header(headers, "Content-Type") or "application/json"
    status, resp_body, resp_ctype = forward_to_hub(
        hub_url=config["mac_hub_url"],
        body=body,
        content_type=content_type,
        timestamp=timestamp,
        signature=signature,
        timeout_s=float(config["forward_timeout_s"]),
        opener=opener,
    )
    return status, resp_body, resp_ctype


def _json_bytes(payload: dict[str, Any], status: int = 200) -> tuple[int, bytes, str]:
    return status, json.dumps(payload).encode(), "application/json"


class SlackForwarderHandler(BaseHTTPRequestHandler):
    """Threading HTTP handler; config is attached by ``make_server``."""

    server_version = "ua-slack-tls-forwarder/1.0"
    config: dict[str, Any] = {}

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never log bodies, signatures, or secrets — path + status only.
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path in HEALTH_PATHS:
            payload = {
                "ok": True,
                "service": "ua-slack-tls-forwarder",
                "authority": "mac-hub-only",
                "events_path": EVENTS_PATH,
            }
            status, body, ctype = _json_bytes(payload)
            self._send(status, body, ctype)
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path != EVENTS_PATH:
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        length_raw = self.headers.get("Content-Length", "")
        try:
            length = int(length_raw) if length_raw else 0
        except ValueError:
            self._send(400, b'{"error":"invalid content length"}', "application/json")
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(400, b'{"error":"Slack request body exceeds limit"}', "application/json")
            return
        body = self.rfile.read(length)
        try:
            status, payload, ctype = handle_slack_events(
                body, dict(self.headers), config=self.config
            )
        except ForwarderError as exc:
            self._send(502, json.dumps({"error": str(exc)[:200]}).encode(), "application/json")
            return
        if isinstance(payload, dict):
            status, body_out, ctype = _json_bytes(payload, status=status)
            self._send(status, body_out, ctype)
            return
        self._send(status, payload, ctype)


def make_server(config: Mapping[str, Any] | None = None) -> ThreadingHTTPServer:
    cfg = dict(config) if config is not None else load_config()
    handler = type(
        "ConfiguredSlackForwarderHandler",
        (SlackForwarderHandler,),
        {"config": cfg},
    )
    server = ThreadingHTTPServer((cfg["listen_host"], int(cfg["listen_port"])), handler)
    return server


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        config = load_config()
    except ForwarderError as exc:
        log.error("configuration error: %s", exc)
        return 2
    # Redacted boot log — never print secret or full hub URL with credentials.
    hub = urlparse(config["mac_hub_url"])
    log.info(
        "starting slack TLS forwarder on %s:%s → %s://%s%s (no local DB)",
        config["listen_host"],
        config["listen_port"],
        hub.scheme,
        hub.netloc,
        hub.path,
    )
    server = make_server(config)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
