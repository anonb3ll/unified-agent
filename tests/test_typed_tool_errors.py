"""Task #2054: typed MCP tool errors carry a machine code next to the text.

A typed refusal (``conflict``, ``approval_required``) reaches an MCP client as
``isError`` with ``structuredContent = {"code": ..., "approval_id"?: ...}``. The
text is the SDK's unchanged ``Error executing tool <name>: <message>``, so a
client that only reads text keeps working. Untyped errors carry no code.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import claim_release, tool_errors
from unified_agent.tool_errors import ConflictError, PermissionDeniedError, TypedToolError
from unified_agent.tools.approvals import ApprovalPendingError

CONFLICT_TEXT = "task 5 is already claimed/locked by 'server_agent1' until 2026-10-14T12:00:00Z"
APPROVAL_TEXT = "'task_update' requires approval (global allowlist): request 742 is queued."


def _toy_server() -> MCPServer:
    server = MCPServer("task-2054")

    @server.tool()
    def conflict() -> str:
        raise ConflictError(CONFLICT_TEXT)

    @server.tool()
    def release_conflict() -> str:
        raise claim_release.ClaimReleaseConflict("task 5 has no claim to release")

    @server.tool()
    def approval() -> str:
        raise ApprovalPendingError(APPROVAL_TEXT, approval_id=742)

    @server.tool()
    def plain() -> str:
        raise ToolError("title must not be empty")

    @server.tool()
    def permission() -> str:
        raise PermissionDeniedError("steward may not update another agent's claimed task")

    @server.tool()
    def crash() -> str:
        raise RuntimeError("unexpected")

    @server.tool()
    def ok() -> str:
        return "fine"

    @server.tool()
    def needs(n: int) -> int:
        return n

    tool_errors.install(server)
    return server


def _text(result) -> str:
    return "".join(getattr(block, "text", "") or "" for block in result.content or [])


def test_codes_and_wire_shape_carry_only_code_and_approval_id():
    assert issubclass(ApprovalPendingError, TypedToolError)
    assert issubclass(claim_release.ClaimReleaseConflict, ConflictError)
    assert issubclass(ConflictError, ToolError)
    assert ConflictError("x").wire() == {"code": "conflict"}
    pending = ApprovalPendingError("y", approval_id=7)
    assert pending.wire() == {"code": "approval_required", "approval_id": 7}
    assert pending.approval_id == 7
    assert ApprovalPendingError("z").wire() == {"code": "approval_required"}
    assert PermissionDeniedError("denied").wire() == {"code": "permission_denied"}


@pytest.mark.anyio
async def test_mcp_client_sees_typed_codes_and_unchanged_text():
    async with Client(_toy_server()) as client:
        res = await client.session.call_tool("conflict", {})
        assert res.is_error
        assert res.structured_content == {"code": "conflict"}
        assert _text(res) == f"Error executing tool conflict: {CONFLICT_TEXT}"

        res = await client.session.call_tool("release_conflict", {})
        assert res.is_error and res.structured_content == {"code": "conflict"}

        res = await client.session.call_tool("approval", {})
        assert res.is_error
        assert res.structured_content == {"code": "approval_required", "approval_id": 742}
        assert _text(res) == f"Error executing tool approval: {APPROVAL_TEXT}"

        res = await client.session.call_tool("permission", {})
        assert res.is_error and res.structured_content == {"code": "permission_denied"}

        # Untyped refusals, argument errors and crashes are byte-for-byte the SDK's.
        res = await client.session.call_tool("plain", {})
        assert res.is_error and res.structured_content is None
        assert _text(res) == "Error executing tool plain: title must not be empty"
        res = await client.session.call_tool("needs", {"n": "not a number"})
        assert res.is_error and res.structured_content is None
        res = await client.session.call_tool("crash", {})
        assert res.is_error and res.structured_content is None
        assert "unexpected" not in _text(res)
        res = await client.session.call_tool("ok", {})
        assert not res.is_error and _text(res) == "fine"


@asynccontextmanager
async def _http(server: MCPServer):
    app = server.streamable_http_app()
    async with server.session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as http:
            headers = {"Accept": "application/json, text/event-stream"}
            init = await http.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "task-2054", "version": "1"},
                    },
                },
                headers=headers,
            )
            assert init.status_code == 200, init.text
            headers["mcp-session-id"] = init.headers["mcp-session-id"]
            await http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers,
            )

            async def call(name: str, req_id: int) -> dict:
                resp = await http.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": {}},
                    },
                    headers=headers,
                )
                assert resp.status_code == 200, resp.text
                body = resp.text
                if body.lstrip().startswith("event:") or "data:" in body:
                    body = "".join(
                        line[5:].strip() for line in body.splitlines() if line.startswith("data:")
                    )
                return json.loads(body)["result"]

            yield call


@pytest.mark.anyio
async def test_http_gateway_wire_carries_structured_content():
    async with _http(_toy_server()) as call:
        conflict = await call("conflict", 2)
        assert conflict["isError"] is True
        assert conflict["structuredContent"] == {"code": "conflict"}
        approval = await call("approval", 3)
        assert approval["isError"] is True
        assert approval["structuredContent"] == {"code": "approval_required", "approval_id": 742}
        assert approval["content"][0]["text"] == f"Error executing tool approval: {APPROVAL_TEXT}"
        plain = await call("plain", 4)
        assert plain["isError"] is True
        assert "structuredContent" not in plain


@pytest.mark.anyio
async def test_real_hub_returns_typed_conflict_and_approval(monkeypatch):
    """server.py installs the handler: real task_claim and approval gate refusals."""
    from unified_agent import server
    from unified_agent.tools import tasks

    task_id = tasks.task_add("typed error probe")["id"]
    held = tasks.task_claim(task_id, agent="server_agent1")
    async with Client(server.mcp) as client:
        # A fenced caller keeps the conflict; an unfenced one gets safe_wait (#2388).
        res = await client.session.call_tool(
            "task_claim",
            {"id": task_id, "agent": "Desktop_Agent2", "expected_run_id": held["current_run_id"]},
        )
        assert res.is_error, _text(res)
        assert res.structured_content == {"code": "conflict"}
        assert f"task {task_id} is already claimed/locked by 'server_agent1'" in _text(res)

        monkeypatch.setenv("UA_APPROVAL_TOOLS", "task_add")
        res = await client.session.call_tool("task_add", {"title": "gated"})
        assert res.is_error, _text(res)
        code = res.structured_content
        assert code["code"] == "approval_required"
        assert set(code) == {"code", "approval_id"}
        assert f"request {code['approval_id']} is queued" in _text(res)


def test_typed_raise_sites_keep_their_text_and_the_cli_fallback_matches(monkeypatch):
    """No message changed: the old-hub regex still classifies every typed raise."""
    from unified_agent.cli import client
    from unified_agent.tools import tasks

    task_id = tasks.task_add("text pin")["id"]
    claimed = tasks.task_claim(task_id, agent="server_agent1")
    with pytest.raises(ConflictError) as held:
        tasks.task_claim(task_id, agent="Desktop_Agent2", expected_run_id=claimed["current_run_id"])
    assert str(held.value) == (
        f"task {task_id} is already claimed/locked by 'server_agent1' "
        f"until {claimed['lock_expires_at']}"
    )
    with pytest.raises(ConflictError) as stale:
        tasks.task_update(
            task_id, append_note="x", expected_updated_at="OLD", actor="server_agent1"
        )
    assert str(stale.value).startswith(
        f"task {task_id} authority revision changed: expected updated_at 'OLD', found "
    )
    for exc, tool in ((held.value, "task_claim"), (stale.value, "task_update")):
        assert exc.wire() == {"code": "conflict"}
        assert client._CONFLICT.match(f"Error executing tool {tool}: {exc}")


def test_real_steward_policy_guard_returns_permission_denied(monkeypatch):
    from unified_agent import profiles
    from unified_agent.tools import tasks

    task_id = tasks.task_add("steward authorization probe")["id"]
    tasks.task_claim(task_id, agent="server_agent1")
    monkeypatch.setattr(profiles, "current", lambda: profiles.STEWARD)
    with pytest.raises(PermissionDeniedError) as denied:
        tasks.task_update(task_id, append_note="not authorized", actor="Desktop_Agent2")
    assert denied.value.wire() == {"code": "permission_denied"}


def test_sdk_call_tool_handler_is_the_one_we_mirror():
    """tool_errors.install copies MCPServer._handle_call_tool (mcp 2.3.0) to add the
    typed code. If an SDK upgrade changes that method, re-mirror it, then update
    this hash (#2054)."""
    import hashlib
    import inspect

    from mcp.server.mcpserver import server as sdk_server

    source = inspect.getsource(sdk_server.MCPServer._handle_call_tool)
    assert hashlib.sha256(source.encode()).hexdigest() == (
        "0c7bac402edb6448031b6b69fc6665202bdbf12c651e6ffacc56efc022d61c46"
    ), "mcp SDK _handle_call_tool changed: re-mirror tool_errors.install, then update this hash"
