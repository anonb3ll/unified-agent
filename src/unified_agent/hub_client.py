"""Authenticated HTTP client for the unified-agent hub (MCP and platform JSON reads).

Moved out of ``executor.daemon`` and ``platform_cli`` (task #1724) so services
such as ``planning_loop`` can read from the canonical hub without importing the
executor runtime or a CLI adapter. ``HubClient`` here is the transport and
credential resolution; ``executor.daemon.HubClient`` subclasses it to add the
executor's in-process tool fallback. ``executor.daemon`` and ``platform_cli``
re-export the moved names, so old imports keep working.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import config

# Same logger the executor's HubClient always used, so its warnings keep
# landing in the executor's log channel.
logger = logging.getLogger("unified_agent.executor")

# Protocol version sent on the MCP initialize handshake.
MCP_PROTOCOL_VERSION = "2025-06-18"


class HubClient:
    """HTTP client for executing MCP tools over the unified-agent HTTP hub."""

    def __init__(
        self,
        agent_label: str | None = None,
        hub_url: str | None = None,
        bearer_token: str | None = None,
        allow_fallback: bool = True,
    ):
        self.agent_label = (agent_label or config.executor_agent()).strip()
        self.hub_url = config.http_hub_url(self.agent_label, hub_url)
        # Explicit remote hubs always own run admission, even when the older
        # general-tool fallback switch is enabled. The only standalone opt-out
        # is an explicitly enabled direct mode against a loopback hub; it offers
        # no remote admission/revocation guarantee. Merely finding a local DB
        # never establishes that mode.
        configured_hub = bool(hub_url or os.environ.get("UA_HTTP_HUB_URL", "").strip())
        loopback_hub = urlsplit(self.hub_url).hostname in {"localhost", "127.0.0.1", "::1"}
        standalone_direct = os.environ.get("UA_EXECUTOR_ALLOW_DIRECT") == "1" and loopback_hub
        self.authoritative_run_checks = (
            configured_hub or config.is_primary_client(self.agent_label)
        ) and not standalone_direct

        def _match(include_managed: bool) -> str | None:
            for tok, lbl in config.agent_tokens(include_managed=include_managed).items():
                if lbl == self.agent_label:
                    return tok
            return None

        # UA_AGENT_TOKENS from the environment; no database access.
        matched_token = _match(include_managed=False)

        # No fabricated credential. This used to fall back to f"token-{label}",
        # which the hub cannot possibly know, so every request 401'd — and the
        # 401 was then swallowed into the in-process fallback below. A missing
        # token is a misconfiguration and has to look like one.
        explicit = (
            bearer_token
            or os.environ.get("UA_EXECUTOR_TOKEN")
            or os.environ.get("UA_AGENT_BEARER_TOKEN")
        )
        # Device sign-in (`unified-agent login`, task #1320) outranks a static
        # UA_AGENT_TOKENS match so a migrated device stops using the shared bearer.
        from . import device_login

        self._constructed_with_explicit = bool(bearer_token)
        self._static_bearer = explicit or matched_token
        device_token = (
            None
            if explicit
            else device_login.stored_access_token(hub=self.hub_url, agent=self.agent_label)
        )
        self._bearer_from_device_login = bool(device_token)
        if loopback_hub and not (explicit or device_token or matched_token):
            # Last resort, for a client of a loopback hub only: managed secrets
            # in the local live store (#904), e.g. an executor on the hub host
            # provisioned by agent_token_issue. That store is the hub's own only
            # when the hub is local; a remote client never opens a local
            # memory.db, and one with a credential never needs to.
            matched_token = _match(include_managed=True)
            self._static_bearer = matched_token
        self.bearer_token = explicit or device_token or matched_token
        # Primary clients must not silently execute shared-state tools against
        # their own process when the configured hub is wrong or unreachable.
        # Tests and deliberately standalone runs can still opt in explicitly.
        self.allow_fallback = (
            allow_fallback
            and not self.authoritative_run_checks
            and (
                not config.is_primary_client(self.agent_label)
                or os.environ.get("UA_EXECUTOR_ALLOW_DIRECT") == "1"
            )
        )
        self._session_id: str | None = None
        # A saved sign-in whose access token is expired while its issuer is
        # briefly unreachable is not "no credential": _headers re-resolves it on
        # every request, so startup must not fail (PR #750 review 4).
        if not self.bearer_token and device_login.has_saved_signin(
            hub=self.hub_url, agent=self.agent_label
        ):
            logger.warning(
                "saved device sign-in for %r could not refresh at startup; retrying per request",
                self.agent_label,
            )
        elif not self.bearer_token:
            msg = (
                f"no bearer token resolved for agent {self.agent_label!r}: set UA_EXECUTOR_TOKEN, "
                "UA_AGENT_BEARER_TOKEN, or a matching label in UA_AGENT_TOKENS"
            )
            if not allow_fallback:
                raise RuntimeError(msg)
            logger.warning("%s — hub calls will be refused; in-process fallback only", msg)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        # Always re-resolve a saved device sign-in when no explicit constructor/
        # env token was configured: recovers after a temporary issuer outage at
        # startup, and falls back to the static bearer when refresh fails after
        # expiry instead of stranding an unusable access token.
        explicit_env = self._constructed_with_explicit or bool(
            os.environ.get("UA_EXECUTOR_TOKEN") or os.environ.get("UA_AGENT_BEARER_TOKEN")
        )
        if not explicit_env:
            from . import device_login

            refreshed = device_login.stored_access_token(hub=self.hub_url, agent=self.agent_label)
            if refreshed:
                self.bearer_token = refreshed
                self._bearer_from_device_login = True
            else:
                # Refresh failed or no saved login — prefer static over a stale
                # device access token left in self.bearer_token.
                if self._bearer_from_device_login or not self.bearer_token:
                    self.bearer_token = self._static_bearer
                self._bearer_from_device_login = False
        headers = {
            "Content-Type": "application/json",
            # MCP streamable-HTTP requires this; without it the hub answers a
            # bare 406 that reads like a transport failure. This client shipped
            # without it, so every hub call it ever made failed — the 401s from
            # the missing token (PR #82) hid that there was a second defect
            # underneath.
            "Accept": "application/json, text/event-stream",
        }
        if self.bearer_token:
            # Never send "Bearer None": with no credential the hub's 401 is the
            # honest answer, not a malformed header (PR #760 review).
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        headers.update(extra or {})
        return headers

    def _post(self, body: dict, timeout: float = 10.0) -> tuple[int, dict[str, str], str]:
        req = urllib.request.Request(
            self.hub_url,
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, headers, resp.read().decode("utf-8")

    @staticmethod
    def _decode_rpc(headers: dict[str, str], text: str) -> dict:
        """The JSON-RPC message from a plain-JSON or SSE-framed hub response.

        The hub answers tools/call with `text/event-stream`
        (``event: message\\r\\ndata: {...}``). The previous implementation ran
        ``json.loads`` straight over the body, which cannot parse that framing.
        """
        if "text/event-stream" in (headers.get("content-type") or ""):
            for line in text.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            raise RuntimeError("hub returned an SSE response with no data frame")
        return json.loads(text)

    @staticmethod
    def _unwrap_tool_result(result: dict) -> dict:
        """MCP wraps a tool's return value in content blocks; callers want the dict.

        Returning the envelope would fail silently rather than loudly:
        ``find_pending_work()`` does ``.get("delegations", [])``, which on
        ``{"content": [...]}`` yields ``[]`` — indistinguishable from "no work
        to do" — forever.
        """
        if result.get("isError"):
            raise RuntimeError(f"hub reported a tool error: {result}")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for block in result.get("content") or []:
            if block.get("type") == "text":
                try:
                    parsed = json.loads(block.get("text") or "")
                except (TypeError, ValueError):
                    return {"text": block.get("text")}
                return parsed if isinstance(parsed, dict) else {"result": parsed}
        return result

    def _ensure_session(self) -> None:
        """Perform the MCP initialize handshake and keep the session id.

        Streamable-HTTP rejects tools/call before this with
        ``400 Bad Request: Missing session ID``. The old client never
        handshook at all.
        """
        if self._session_id:
            return
        _status, headers, _text = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "unified-agent-executor", "version": "1"},
                },
            }
        )
        session_id = headers.get("mcp-session-id")
        if not session_id:
            raise RuntimeError("hub returned no Mcp-Session-Id on initialize")
        self._session_id = session_id
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _call_over_hub(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._ensure_session()
        _status, headers, text = self._post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
        )
        message = self._decode_rpc(headers, text)
        if "error" in message:
            raise RuntimeError(f"HTTP Hub RPC Error: {message['error']}")
        return self._unwrap_tool_result(message.get("result") or {})

    def ping(self) -> None:
        """Prove this client can complete a real hub round-trip. Raises if not.

        Read-only (``tools/list``) and deliberately the SAME code path
        ``call_tool`` uses, so a health check cannot pass while the daemon's
        own requests fail — which is exactly what happened when system_doctor
        built its own request with headers the daemon lacked.
        """
        self._ensure_session()
        _status, headers, text = self._post({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        message = self._decode_rpc(headers, text)
        if "error" in message:
            raise RuntimeError(f"hub rejected tools/list: {message['error']}")

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call an MCP tool on the HTTP hub over streamable-HTTP."""
        try:
            return self._call_over_hub(tool_name, arguments)
        except urllib.error.HTTPError as e:
            # An auth rejection is NOT a transport failure and must never
            # degrade into local execution. Falling back here bypasses the very
            # boundary the token exists to enforce — the hub's auth check, the
            # approval gate, and per-agent workspace scoping all live on the hub
            # side, and none of them apply to an in-process call. This path ran
            # unnoticed for days at DEBUG level while the daemon 401'd every 5s
            # and did its work outside the boundary anyway.
            if e.code in (401, 403):
                logger.error(
                    "Hub rejected the executor's credentials (HTTP %s) for %s at %s. "
                    "Refusing to fall back to in-process dispatch: that would run the tool "
                    "outside the hub's auth, approval, and workspace boundaries. Fix the "
                    "token (UA_EXECUTOR_TOKEN / UA_AGENT_BEARER_TOKEN / UA_AGENT_TOKENS).",
                    e.code,
                    tool_name,
                    self.hub_url,
                )
                raise RuntimeError(
                    f"hub auth rejected for agent {self.agent_label!r} (HTTP {e.code}) "
                    f"calling {tool_name}"
                ) from e
            if e.code == 404 and self._session_id:
                # The hub expires idle sessions; re-handshake once rather than
                # falling back, which would leave the boundary for a recoverable
                # condition.
                logger.info("hub session expired; re-initializing")
                self._session_id = None
                try:
                    return self._call_over_hub(tool_name, arguments)
                except Exception as retry_exc:  # noqa: BLE001 - handled below
                    return self._fallback_or_raise(tool_name, arguments, retry_exc)
            return self._fallback_or_raise(tool_name, arguments, e)
        except (urllib.error.URLError, OSError, RuntimeError) as e:
            return self._fallback_or_raise(tool_name, arguments, e)

    def _fallback_or_raise(
        self, tool_name: str, arguments: dict[str, Any], exc: Exception
    ) -> dict[str, Any]:
        """Transport-level failure: optionally dispatch in-process instead."""
        if not self.allow_fallback:
            raise RuntimeError(
                f"Failed to communicate with HTTP hub at {self.hub_url}: {exc}"
            ) from exc
        # Standalone / test convenience only: the hub is unreachable, not refusing.
        logger.warning(
            "HTTP hub unreachable (%s); falling back to in-process dispatch of %s",
            exc,
            tool_name,
        )
        return self._direct_tool_fallback(tool_name, arguments)

    def _direct_tool_fallback(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """No in-process tool surface below the runtime layer: fail closed.

        ``executor.daemon.HubClient`` overrides this with the executor's
        in-process dispatch. Construct with ``allow_fallback=False`` (as the
        CLI and planning reads do) and this is never reached.
        """
        raise RuntimeError(
            f"no in-process fallback for {tool_name!r} in this client; "
            f"the HTTP hub at {self.hub_url} must answer"
        )


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
_MCP_PATH = "/mcp"


class PlatformCliError(ValueError):
    """A safe, actionable CLI configuration or platform-read failure."""


def _platform_url(hub_url: str, path: str, query: str = "") -> str:
    """Build a same-origin platform URL from a validated canonical MCP URL."""
    if any(ord(char) < 32 or ord(char) == 127 for char in hub_url):
        raise PlatformCliError("UA_HTTP_HUB_URL must not contain control characters")
    try:
        parsed = urlsplit(hub_url)
    except ValueError as exc:
        raise PlatformCliError("UA_HTTP_HUB_URL is malformed") from exc
    try:
        port = parsed.port
    except ValueError as exc:
        raise PlatformCliError("canonical hub URL has an invalid port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != _MCP_PATH
    ):
        raise PlatformCliError("UA_HTTP_HUB_URL must be an http(s) URL ending exactly in /mcp")
    netloc = parsed.hostname
    if ":" in netloc:
        netloc = f"[{netloc}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, path, query, ""))


def _read_response(response: httpx.Response) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes(chunk_size=64 * 1024):
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise PlatformCliError("platform response exceeds the 2 MiB CLI limit")
    return bytes(body)


def _post_json(client: HubClient, path: str, payload: dict) -> dict:
    """Authenticated read-only projection request with bounded transport."""
    url = _platform_url(client.hub_url, path)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise PlatformCliError("next-work manifest exceeds the 1 MiB request limit")
    try:
        with httpx.Client(timeout=10.0, follow_redirects=False) as http:
            with http.stream("POST", url, headers=client._headers(), content=body) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise PlatformCliError(
                        f"canonical hub rejected projection read (HTTP {response.status_code})"
                    )
                data = _read_response(response)
    except PlatformCliError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise PlatformCliError(f"could not reach canonical hub ({type(exc).__name__})") from exc
    try:
        result = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformCliError("canonical hub returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise PlatformCliError("canonical hub returned a non-object JSON payload")
    return result
