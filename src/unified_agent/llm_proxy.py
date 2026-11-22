"""Device-local OpenAI chat proxy that refreshes the hub OAuth token per call.

Only loopback clients with a separate local bearer can use it. The local bearer
is not a provider credential; provider keys remain in the hub secret store.

Usage accounting (task #1526): this process runs on the device, not the hub,
and never opens the hub's storage — it only relays bytes to and from
``{hub}/api/llm/v1/chat/completions``, which *is* ``llm_broker.chat_completions``.
The hub already records one hub-observed row per completed call inside
``llm_broker._record_result``. Recording anything here too would double-count
the same call, so this module deliberately does not import ``usage_ledger`` or
``storage`` at all — see ``tests/test_llm_proxy.py::test_module_never_touches_the_usage_ledger``.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import secrets
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import device_login

_MAX_BODY = 256 * 1024
_MAX_RESPONSE = 4 * 1024 * 1024 + 4096  # broker may append [DONE] or a generic error frame
_UPLOAD_TIMEOUT = 15


def config_dir() -> Path:
    return Path(os.environ.get("UA_HOME") or "~/.unified_agent").expanduser() / "llm-proxy"


def _endpoint_dir(hub: str, port: int) -> Path:
    endpoint = f"{device_login.hub_origin(hub)}:{port}"
    return config_dir() / hashlib.sha256(endpoint.encode()).hexdigest()


def _saved_key(hub: str, port: int) -> dict | None:
    """Move a matching legacy proxy bearer without breaking installed snippets."""
    target = _endpoint_dir(hub, port) / "local-key.json"
    saved = device_login._read_saved(target)
    if saved:
        return saved
    legacy = device_login._read_saved(config_dir() / "local-key.json")
    if (
        not legacy
        or legacy.get("hub") != device_login.hub_origin(hub)
        or legacy.get("port") != port
    ):
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    device_login._write_private(target, legacy)
    return legacy


def validate_config(harness: str, *, hub: str, model: str, port: int) -> None:
    if harness not in {"openai-compatible", "opencode"}:
        raise ValueError("unsupported harness")
    if not model or any(c in model for c in "\r\n\0"):
        raise ValueError("--model is required")
    if not device_login.transport_is_protected(hub):
        raise ValueError("hub URL must use HTTPS or a protected tailnet transport")
    if not 1 <= port <= 65535:
        raise ValueError("invalid proxy port")


def configure(harness: str, *, hub: str, model: str, port: int = 9127) -> Path:
    """Emit a private harness snippet without editing a running harness config."""
    validate_config(harness, hub=hub, model=model, port=port)
    directory = _endpoint_dir(hub, port)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    existing = _saved_key(hub, port) or {}
    matching = existing.get("hub") == device_login.hub_origin(hub) and existing.get("port") == port
    local_key = existing.get("key") if matching and isinstance(existing.get("key"), str) else None
    if not local_key:
        local_key = secrets.token_urlsafe(32)
        device_login._write_private(
            directory / "local-key.json",
            {"key": local_key, "hub": device_login.hub_origin(hub), "port": port},
        )
    base = f"http://127.0.0.1:{port}/v1"
    if harness == "opencode":
        payload = {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                "unified-agent": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Unified Agent Hub",
                    "options": {"baseURL": base, "apiKey": local_key},
                    "models": {model: {"name": model}},
                }
            },
        }
        target = directory / "opencode.provider.json"
        device_login._write_private(target, payload)
    else:
        target = directory / "openai-compatible.json"
        device_login._write_private(
            target,
            {"base_url": base, "api_key": local_key, "model": model},
        )
    return target


def _handler(hub: str, token_path: Path, local_key: str):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            if self.headers.get("Origin") is not None:
                self.send_error(403)
                return
            if self.headers.get("Host", "").lower() not in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }:
                self.send_error(403)
                return
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth, f"Bearer {local_key}"):
                self.send_error(401)
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0 or length > _MAX_BODY:
                self.send_error(413)
                return
            self.connection.settimeout(_UPLOAD_TIMEOUT)
            try:
                body = self.rfile.read(length)
            except OSError:
                self.send_error(408, "request body timed out")
                self.close_connection = True
                return
            if len(body) != length:
                self.send_error(400, "incomplete request body")
                self.close_connection = True
                return
            token = device_login.stored_access_token(token_path, hub=hub)
            if not token:
                self.send_error(401, "device sign-in expired; run unified-agent login")
                return
            request = urllib.request.Request(
                f"{hub}/api/llm/v1/chat/completions",
                data=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                upstream = device_login.open_no_redirect(request, timeout=120)
            except urllib.error.HTTPError as exc:
                upstream = exc
            except (urllib.error.URLError, OSError, ValueError):
                self.send_error(502, "hub unavailable")
                return
            with upstream:
                self.send_response(upstream.status)
                content_type = upstream.headers.get("Content-Type", "application/json")
                self.send_header("Content-Type", content_type)
                audit_state = upstream.headers.get("X-UA-Broker-Audit")
                if audit_state:
                    self.send_header("X-UA-Broker-Audit", audit_state)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                remaining = _MAX_RESPONSE
                read_chunk = (
                    getattr(upstream, "read1", upstream.read)
                    if content_type.startswith("text/event-stream")
                    else upstream.read
                )
                try:
                    while remaining > 0:
                        chunk = read_chunk(min(16384, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                if remaining == 0 and upstream.read(1):
                    self.close_connection = True
                    if content_type.startswith("text/event-stream"):
                        try:
                            self.wfile.write(
                                b'data: {"error":{"message":"proxy response limit exceeded","type":"broker_error"}}\n\n'
                            )
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            pass

        def log_message(self, format, *args):  # noqa: A002
            # Never log request paths, token headers, or model prompt content.
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unified-agent llm-proxy")
    parser.add_argument("--hub", default=os.environ.get("UA_HUB_URL", ""))
    parser.add_argument("--port", type=int, default=9127)
    parser.add_argument("--token-file", type=Path, default=None)
    args = parser.parse_args(argv)
    if not args.hub or not device_login.transport_is_protected(args.hub):
        parser.error("--hub must use HTTPS or a protected tailnet transport")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be 1..65535")
    saved = _saved_key(args.hub, args.port)
    if (
        not saved
        or saved.get("hub") != device_login.hub_origin(args.hub)
        or saved.get("port") != args.port
    ):
        print("error: run unified-agent login --configure first", file=sys.stderr)
        return 1
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        _handler(
            args.hub.rstrip("/"), args.token_file or device_login.default_token_path(), saved["key"]
        ),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
