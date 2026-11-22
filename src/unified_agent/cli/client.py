"""Bounded, authenticated read client over the existing hub transport."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from time import monotonic
from typing import Any

import httpx

from .. import config, device_login
from ..hub_client import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MCP_PROTOCOL_VERSION,
    HubClient,
    PlatformCliError,
    _platform_url,
)
from .result import CliError, strict_json

# A hub with #2054 names these write outcomes in the error's structuredContent
# (tool_errors.TypedToolError); _write_refusal reads that first. An older hub
# returns ToolError text only, so match its fixed templates, anchored at the MCP
# prefix so text a caller supplied later in a message cannot forge one
# (tools/tasks.py task_claim/task_update, tools/approvals.py check_approval_gate).
# Anything else stays tool_error.
# ponytail: text fallback for pre-#2054 hubs; delete _PREFIX, _CONFLICT and
# _APPROVAL one release after #2054 deploys to the Mac hub.
_PREFIX = r"Error executing tool [\w.-]+: "
_CONFLICT = re.compile(
    _PREFIX + r"(?:task \d+ authority revision changed"
    r"|task \d+ is already claimed/locked by "
    r"|task \d+ is claimed by [^\n]+ with an active lease; "
    r"|task \d+'s current run is "
    r"|task \d+'s lease expired for run "
    r"|lost race to claim task \d+)"
)
_PERMISSION = re.compile(
    _PREFIX
    + r"task \d+ is claimed by [^\n]+ — steward may not update another agent's claimed task; "
)
_APPROVAL = re.compile(
    _PREFIX + r"(?:task \d+ is quarantined \(untrusted\); clearing that needs a verified "
    r"human identity or a human-approved one-shot pass\. )?"
    r"'[\w.-]+' requires approval \(global allowlist\): request (\d+) is queued\."
)


def _approval_required(approval_id: int | None) -> CliError:
    return CliError(
        "approval_required",
        (
            "The hub queued this call for human approval; retry it unchanged once approved."
            if approval_id is not None
            else "The hub requires human approval but returned no approval ID; "
            "check the operator approval surface."
        ),
        9,
        outcome="approval_required",
        approval_id=approval_id,
    )


def _conflict() -> CliError:
    return CliError(
        "conflict",
        "The object changed or is held by another claimant; read it again before retrying.",
        8,
        outcome="conflict",
    )


def _permission_denied() -> CliError:
    return CliError("permission_denied", "Hub denied this action for your identity.", 4)


def _write_refusal(result: dict) -> CliError:
    typed = result.get("structuredContent")
    code = typed.get("code") if isinstance(typed, dict) else None
    approval_id = typed.get("approval_id") if isinstance(typed, dict) else None
    if code == "approval_required" and (
        approval_id is None or type(approval_id) is int and approval_id > 0
    ):
        return _approval_required(approval_id)
    if code == "conflict":
        return _conflict()
    if code == "permission_denied":
        return _permission_denied()
    blocks = result.get("content")
    first = blocks[0] if isinstance(blocks, list) and blocks else None
    text = first.get("text") if isinstance(first, dict) else None
    text = text if isinstance(text, str) else ""
    if _PERMISSION.match(text):
        return _permission_denied()
    approval = _APPROVAL.match(text)
    if approval:
        return _approval_required(int(approval.group(1)))
    if _CONFLICT.match(text):
        return _conflict()
    return CliError("tool_error", "Hub refused the write; check permissions and arguments.", 1)


def _validate_bearer(token: str | None) -> None:
    if not token or len(token) > 16 * 1024 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise CliError("authentication", "Could not resolve a valid bearer credential.", 3)


class ReadClient(HubClient):
    """Reuse credential/header semantics, with no direct execution or local store.

    A credential is resolved before constructing HubClient, including for loopback,
    so its legacy hub-local managed-secret lookup can never open a database here.
    This client does not use the executor's fallback or retry a tools/call.
    """

    def __init__(
        self,
        hub: str | None,
        identity: str | None,
        timeout: float = 10,
        token_file: str | None = None,
    ):
        hub = (hub or os.environ.get("UA_HTTP_HUB_URL", "")).strip()
        if not hub:
            raise CliError("configuration", "Set --hub or UA_HTTP_HUB_URL to the hub /mcp URL.", 2)
        try:
            _platform_url(hub, "/mcp")
        except PlatformCliError as exc:
            raise CliError("configuration", str(exc), 2) from None
        label = identity or os.environ.get("UA_CLI_IDENTITY") or os.environ.get("UA_EXECUTOR_AGENT")
        self.expected_identity = label
        # Explicit file selection supports the same private bearer file a gateway
        # headers helper uses, without copying a credential into argv or settings.
        credential_file = token_file or os.environ.get("UA_CLI_TOKEN_FILE")
        token = None
        if credential_file:
            try:
                path = Path(credential_file).expanduser()
                if not path.is_file():
                    raise OSError("not a regular file")
                with path.open("rb") as stream:
                    raw = stream.read(16 * 1024 + 1)
                token = raw.decode("utf-8").strip()
                if len(raw) > 16 * 1024:
                    raise ValueError("invalid bearer")
                _validate_bearer(token)
            except (OSError, UnicodeError, ValueError):
                raise CliError(
                    "authentication",
                    "Could not read a valid bearer from the selected token file.",
                    3,
                ) from None
        # Otherwise use the same sources/precedence as HubClient; no managed store.
        if token is None:
            token = os.environ.get("UA_EXECUTOR_TOKEN") or os.environ.get("UA_AGENT_BEARER_TOKEN")
        explicit = bool(token)
        matched = None
        from_device = False
        if not token:
            token = device_login.stored_access_token(hub=hub, agent=label)
            from_device = bool(token)
        if not explicit and label:
            matched = next(
                (
                    tok
                    for tok, agent in config.agent_tokens(include_managed=False).items()
                    if agent == label
                ),
                None,
            )
            token = token or matched
        if not token:
            raise CliError(
                "authentication",
                "No credential for this hub. Use unified-agent login, a bearer environment "
                "variable, or --identity to select a configured token label.",
                3,
            )
        _validate_bearer(token)
        super().__init__(
            agent_label=label or "ua-cli", hub_url=hub, bearer_token=token, allow_fallback=False
        )
        # The constructor receives a credential to fence off managed-store access.
        # Restore the shared client's per-request refresh for saved device sign-in.
        if not explicit:
            # An omitted selector lets the saved sign-in supply its bound identity.
            self.agent_label = label
            self._constructed_with_explicit = False
            self._static_bearer = matched
            self._bearer_from_device_login = from_device
        self.timeout = timeout
        self.http = httpx.Client(timeout=timeout, follow_redirects=False)
        self._rpc_id = 0

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = super()._headers(extra)
        _validate_bearer(self.bearer_token)
        return headers

    def close(self) -> None:
        try:
            if self._session_id:
                # End only the transport session, never the selected domain session/run.
                self.http.delete(self.hub_url, headers=self._headers(), timeout=1)
        except (httpx.HTTPError, CliError):
            pass
        finally:
            self.http.close()

    def reconnect(self) -> None:
        """Drop the transport session so the next call opens a new one.

        Used by bounded waits and event follows after a lost connection. It
        ends no domain session, run or claim, and resolves the credential
        again on the next request.
        """
        try:
            self.http.close()
        except httpx.HTTPError:
            pass
        self._session_id = None
        self.http = httpx.Client(timeout=self.timeout, follow_redirects=False)

    def _post(self, body: dict, timeout: float = 10.0) -> tuple[int, dict[str, str], str]:
        encoded = json.dumps(body, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise CliError("validation", "Request exceeds the 1 MiB client limit.", 2)
        deadline = monotonic() + self.timeout
        try:
            with self.http.stream(
                "POST", self.hub_url, headers=self._headers(), content=encoded, timeout=self.timeout
            ) as response:
                status = response.status_code
                if status == 401:
                    raise CliError(
                        "authentication", "Hub rejected the credential; sign in again.", 3
                    )
                if status == 403:
                    raise CliError(
                        "permission_denied", "Hub denied this action for your identity.", 4
                    )
                if not 200 <= status < 300:
                    raise CliError("hub_rejected", f"Hub rejected the request (HTTP {status}).", 1)
                headers = {k.lower(): v for k, v in response.headers.items()}
                # Yield each decoded network chunk, so heartbeats cannot keep a read
                # alive by continually resetting HTTPX's idle timeout. A pending
                # final receive also remains bounded by that idle timeout.
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if monotonic() >= deadline:
                        raise CliError(
                            "timeout", "Hub read exceeded its time budget; retry the read.", 7
                        )
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise CliError(
                            "invalid_response",
                            f"Hub response exceeds {MAX_RESPONSE_BYTES} bytes.",
                            1,
                        )
                if monotonic() >= deadline:
                    raise CliError(
                        "timeout", "Hub read exceeded its time budget; retry the read.", 7
                    )
        except httpx.TimeoutException:
            raise CliError(
                "timeout", "Hub read timed out; retry the read when reachable.", 7
            ) from None
        except httpx.HTTPError:
            raise CliError(
                "transport", "Could not reach the hub; check the URL and connection.", 6
            ) from None
        try:
            return status, headers, raw.decode("utf-8")
        except UnicodeDecodeError:
            raise CliError("invalid_response", "Hub returned invalid UTF-8.", 1) from None

    def _decode(self, headers: dict[str, str], text: str, request_id: int) -> dict:
        try:
            # Select the matching response: a stream may contain notifications first,
            # and SSE data fields can span several lines.
            if "text/event-stream" in headers.get("content-type", ""):
                candidates = []
                for event in text.replace("\r\n", "\n").split("\n\n"):
                    fields = [
                        line[5:].lstrip(" ")
                        for line in event.splitlines()
                        if line.startswith("data:")
                    ]
                    if fields:
                        candidates.append(strict_json("\n".join(fields)))
            else:
                candidates = [strict_json(text)]
            message = next(
                (
                    item
                    for item in candidates
                    if isinstance(item, dict) and item.get("id") == request_id
                ),
                None,
            )
        except (ValueError, RecursionError):
            raise CliError("invalid_response", "Hub returned invalid JSON/RPC data.", 1) from None
        if not message or message.get("jsonrpc") != "2.0":
            raise CliError("invalid_response", "Hub returned no matching JSON-RPC response.", 1)
        if "error" in message:
            error = message["error"]
            code = error.get("code") if isinstance(error, dict) else None
            if code == -32602:
                raise CliError(
                    "validation", "Hub rejected the arguments; inspect the live schema.", 2
                )
            if code == -32601:
                raise CliError("unavailable", "This action is unavailable on the hub.", 5)
            raise CliError(
                "hub_rejected", "Hub rejected the read; check permissions and inputs.", 1
            )
        if not isinstance(message.get("result"), dict):
            raise CliError("invalid_response", "Hub returned an invalid RPC result.", 1)
        return message["result"]

    def _rpc(self, method: str, params: dict | None = None, *, initialize: bool = False) -> dict:
        if not initialize:
            self._ensure_session()
        self._rpc_id += 1
        request_id = self._rpc_id
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        _, headers, text = self._post(body)
        result = self._decode(headers, text, request_id)
        if initialize:
            session = headers.get("mcp-session-id", "")
            if (
                not session
                or len(session) > 512
                or any(ord(c) < 33 or ord(c) > 126 for c in session)
            ):
                raise CliError("invalid_response", "Hub returned no valid transport session.", 1)
            self._session_id = session
        return result

    def _ensure_session(self) -> None:
        if self._session_id:
            return
        self._rpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "unified-agent-cli", "version": "1"},
            },
            initialize=True,
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tool(self, name: str, arguments: dict, *, write: bool = False) -> Any:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError") and write:
            raise _write_refusal(result)
        if result.get("isError"):
            # Tool exceptions are often plain text, without a stable typed code.
            # Do not invent approval/conflict outcomes or print raw error bodies.
            raise CliError(
                "tool_error", "Hub refused the read; check permissions and arguments.", 1
            )
        if isinstance(result.get("structuredContent"), dict):
            data = result["structuredContent"]
        else:
            data = self._content(result.get("content"))
        # Some gates return a typed pending row instead of raising.
        if (
            write
            and isinstance(data, dict)
            and (data.get("gated") is True or data.get("status") == "pending-approval")
        ):
            approval_id = data.get("approval_id")
            if approval_id is not None and (type(approval_id) is not int or approval_id <= 0):
                raise CliError(
                    "invalid_response",
                    "Hub returned an invalid approval ID for this pending write.",
                )
            raise _approval_required(approval_id)
        return data

    @staticmethod
    def _content(blocks: Any, *, resource: bool = False) -> Any:
        if not isinstance(blocks, list):
            raise CliError("invalid_response", "Hub returned no readable content.", 1)
        for block in blocks:
            if isinstance(block, dict) and (resource or block.get("type") == "text"):
                try:
                    return strict_json(block["text"])
                except (KeyError, TypeError, ValueError, RecursionError):
                    raise CliError(
                        "invalid_response", "Hub returned non-JSON domain content.", 1
                    ) from None
        raise CliError("invalid_response", "Hub returned no JSON content.", 1)

    def platform(
        self, method: str, path: str, body: dict | None = None, *, query: str = ""
    ) -> tuple[int, Any]:
        """One bounded request to a same-origin hub HTTP route (task #2208).

        For operator actions whose MCP twin the human operator's read-only
        profile cannot call. The same credential as ``tool``; no MCP transport
        session, so the route resolves the identity's own profile. Returns the
        status and the strict-JSON body (``None`` when it is not JSON).
        """
        try:
            url = _platform_url(self.hub_url, path, query)
        except PlatformCliError as exc:
            raise CliError("configuration", str(exc), 2) from None
        headers = self._headers()
        headers.pop("Mcp-Session-Id", None)
        content = None
        if body is not None:
            content = json.dumps(body, allow_nan=False).encode("utf-8")
            if len(content) > MAX_REQUEST_BYTES:
                raise CliError("validation", "Request exceeds the 1 MiB client limit.", 2)
        deadline = monotonic() + self.timeout
        raw = bytearray()
        try:
            with self.http.stream(
                method, url, headers=headers, content=content, timeout=self.timeout
            ) as response:
                status = response.status_code
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise CliError(
                            "invalid_response",
                            f"Hub response exceeds {MAX_RESPONSE_BYTES} bytes.",
                            1,
                        )
                    if monotonic() >= deadline:
                        raise CliError("timeout", "Hub exceeded its time budget.", 7)
        except httpx.TimeoutException:
            raise CliError("timeout", "Hub timed out; retry when reachable.", 7) from None
        except httpx.HTTPError:
            raise CliError(
                "transport", "Could not reach the hub; check the URL and connection.", 6
            ) from None
        try:
            data = strict_json(bytes(raw)) if raw else None
        except (ValueError, RecursionError):
            data = None
        return status, data

    def whoami(self) -> dict:
        data = self.tool("profile_status", {})
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("identity"), str)
            or not data["identity"]
        ):
            raise CliError("authentication", "Hub did not confirm an authenticated identity.", 3)
        if self.expected_identity and data["identity"] != self.expected_identity:
            raise CliError(
                "identity_mismatch",
                "Credential identity differs from --identity; select the correct credential.",
                3,
            )
        return data

    def tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        seen = set()
        # Bounded even if a faulty server rotates cursors forever.
        for _ in range(100):
            data = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            page = data.get("tools")
            if not isinstance(page, list) or any(
                not isinstance(t, dict)
                or not isinstance(t.get("name"), str)
                or not isinstance(t.get("inputSchema"), dict)
                for t in page
            ):
                raise CliError("invalid_response", "Hub returned an invalid tool catalog.", 1)
            tools.extend(page)
            cursor = data.get("nextCursor")
            if cursor is None:
                names = [t["name"] for t in tools]
                if len(set(names)) != len(names):
                    raise CliError("invalid_response", "Hub returned duplicate tool names.", 1)
                return tools
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise CliError("invalid_response", "Hub returned an invalid catalog cursor.", 1)
            seen.add(cursor)
        raise CliError("invalid_response", "Hub catalog exceeds the 100-page client limit.", 1)

    def resource(self, uri: str) -> Any:
        result = self._rpc("resources/read", {"uri": uri})
        data = self._content(result.get("contents"), resource=True)
        if not isinstance(data, dict):
            raise CliError("invalid_response", "Hub returned a non-object domain resource.", 1)
        return data
