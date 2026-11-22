#!/usr/bin/env python3
"""Hub client for Claude Code on the web sessions (``cloud_agent1``, task #2446).

A cloud session reaches the hub's ``/mcp`` over the public Funnel with an OAuth
client-credentials grant, the path ``cloud_agent8`` already uses. The operator puts
three values in the cloud environment (docs/cloud-sessions.md, "Operator steps"):

* ``UA_HUB_MCP_URL``        the public MCP endpoint, ``https://<host>/mcp``
* ``UA_HUB_CLIENT_ID``      the client id of the hub's ``cloud_agent1`` entry
* ``UA_HUB_CLIENT_SECRET``  its secret

Subcommands:

``configured``  exit 0 when all three are set (values are never printed).
``headers``     Claude Code ``headersHelper``: exchange the credentials for a
                short-lived access token and print ``{"Authorization": ...}`` on
                stdout. Runs on every MCP connect; the token is never written
                to disk, and errors name the failing step, never a value.
``check``       mint a token, call ``profile_status`` over MCP, and require
                identity ``cloud_agent1`` authenticated by client credentials.
                Prints one line, without the token.
``status``      for the session: print the mode the setup script chose
                (``~/.unified_agent/mode``) and, in hub mode, run ``check``.

Standard library only: the setup script may run it before ``uv sync``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

EXPECTED_IDENTITY = "cloud_agent1"
ENV_URL = "UA_HUB_MCP_URL"
ENV_CLIENT_ID = "UA_HUB_CLIENT_ID"
ENV_CLIENT_SECRET = "UA_HUB_CLIENT_SECRET"
TOKEN_PATH = "/oauth/client-credentials"
MODE_FILE = Path.home() / ".unified_agent" / "mode"
TIMEOUT_S = 15
_PROTOCOL_VERSION = "2025-06-18"


class HubClientError(Exception):
    """A step failed. The message names the step and never carries a credential."""


def _http(method: str, url: str, headers: dict[str, str], body: bytes | None):
    """One HTTP exchange: ``(status, lowercased headers, body bytes)``. Tests replace it."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return (
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        reason = getattr(err, "reason", err)
        raise HubClientError(f"cannot reach the hub at {_origin(url)}: {reason}") from None


def settings(environ: dict[str, str] | None = None) -> tuple[str, str, str] | None:
    """``(mcp_url, client_id, client_secret)`` when all three are set, else ``None``."""
    env = os.environ if environ is None else environ
    values = tuple(
        env.get(name, "").strip() for name in (ENV_URL, ENV_CLIENT_ID, ENV_CLIENT_SECRET)
    )
    if not all(values):
        return None
    url = values[0]
    parsed = urllib.parse.urlsplit(url)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not parsed.netloc or not (
        parsed.scheme == "https" or (parsed.scheme == "http" and loopback)
    ):
        # A secret never travels in clear text; plain http is for a loopback test hub.
        raise HubClientError(f"{ENV_URL} must be an https URL ending in /mcp")
    return url, values[1], values[2]


def missing(environ: dict[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    return [n for n in (ENV_URL, ENV_CLIENT_ID, ENV_CLIENT_SECRET) if not env.get(n, "").strip()]


def _origin(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def access_token(url: str, client_id: str, client_secret: str) -> str:
    """Exchange the client credentials for an access token."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()
    status, _, raw = _http(
        "POST",
        _origin(url) + TOKEN_PATH,
        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        body,
    )
    if status != 200:
        error = _json_or_empty(raw).get("error") or f"HTTP {status}"
        raise HubClientError(f"token exchange refused ({error}); check the hub's client entry")
    token = _json_or_empty(raw).get("access_token")
    if not isinstance(token, str) or not token:
        raise HubClientError("token exchange returned no access_token")
    return token


def _json_or_empty(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _rpc_message(raw: bytes, content_type: str) -> dict:
    """The JSON-RPC response from a JSON or an SSE (``text/event-stream``) body."""
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in content_type:
        for line in text.splitlines():
            if line.startswith("data:"):
                message = _json_or_empty(line[5:].strip().encode())
                if "result" in message or "error" in message:
                    return message
        return {}
    return _json_or_empty(raw)


def profile_status(url: str, token: str) -> dict:
    """Call ``profile_status`` over streamable HTTP and return its payload."""
    base = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    def post(payload: dict, session: str | None) -> tuple[int, dict, bytes]:
        headers = dict(base)
        if session:
            headers["Mcp-Session-Id"] = session
        return _http("POST", url, headers, json.dumps(payload).encode())

    status, headers, raw = post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "cloud-hub-client", "version": "1"},
            },
        },
        None,
    )
    if status != 200:
        raise HubClientError(f"MCP initialize returned HTTP {status}")
    session = headers.get("mcp-session-id")
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    status, headers, raw = post(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "profile_status", "arguments": {}},
        },
        session,
    )
    if status != 200:
        raise HubClientError(f"profile_status returned HTTP {status}")
    message = _rpc_message(raw, headers.get("content-type", ""))
    result = message.get("result") or {}
    if "error" in message or result.get("isError"):
        raise HubClientError("profile_status was refused by the hub")
    payload = result.get("structuredContent")
    if not isinstance(payload, dict):
        content = result.get("content") or [{}]
        payload = _json_or_empty(str(content[0].get("text", "")).encode())
    if isinstance(payload.get("result"), dict):
        payload = payload["result"]
    return payload


def check(environ: dict[str, str] | None = None) -> dict:
    """Verify hub mode end to end; raise ``HubClientError`` on any mismatch."""
    configured = settings(environ)
    if configured is None:
        raise HubClientError("not configured: " + ", ".join(missing(environ)) + " unset")
    url, client_id, client_secret = configured
    status = profile_status(url, access_token(url, client_id, client_secret))
    identity, method = status.get("identity"), status.get("auth_method")
    if identity != EXPECTED_IDENTITY:
        raise HubClientError(
            f"hub reports identity {identity!r}, expected {EXPECTED_IDENTITY!r}; "
            "the client entry maps to the wrong label"
        )
    if method != "client_credentials":
        raise HubClientError(f"hub reports auth_method {method!r}, expected client_credentials")
    return status


def _mode_line() -> str:
    try:
        return MODE_FILE.read_text().strip() or "unknown"
    except OSError:
        return "unknown (no ~/.unified_agent/mode; the setup script did not run)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("configured", "headers", "check", "status"))
    args = parser.parse_args(argv)
    try:
        if args.command == "configured":
            return 0 if settings() is not None else 1
        if args.command == "headers":
            configured = settings()
            if configured is None:
                raise HubClientError("not configured: " + ", ".join(missing()) + " unset")
            token = access_token(*configured)
            print(json.dumps({"Authorization": "Bearer " + token}))
            return 0
        if args.command == "check":
            status = check()
            print(
                "unified-agent: hub mode verified: identity "
                f"{status['identity']}, profile {status.get('profile')}"
            )
            return 0
        mode = _mode_line()
        print(f"unified-agent mode: {mode}")
        if mode.startswith("hub"):
            status = check()
            print(
                "unified-agent: hub reachable: identity "
                f"{status['identity']}, profile {status.get('profile')}"
            )
        return 0
    except HubClientError as err:
        print(f"unified-agent: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
