"""Task #487: application ToolError must not poison the MCP session.

Desktop_Agent5 reported that three delegation-spec rejections were followed by
"MCP server unreachable after 3 consecutive failures". That message is minted
by hermes-agent's MCP client circuit breaker, not by this hub. These tests lock
the server contract: a completed tools/call that returns isError (or a JSON-RPC
invalid-params error) leaves the session usable for a later valid call,
including an unrelated tool.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.server import mcp
from unified_agent.tools.delegate import delegate
from unified_agent.tools.delegation_spec import enforce_delegation_spec

INCOMPLETE_SPEC = "do the public-repo orchestration without a spec"


@pytest.fixture(autouse=True)
def _enforce_delegation_spec(monkeypatch):
    """Host env may have UA_DELEGATION_SPEC_ENFORCE=0; this file needs the live gate."""
    monkeypatch.setenv("UA_DELEGATION_SPEC_ENFORCE", "1")


@asynccontextmanager
async def _running_session_manager(server: MCPServer) -> AsyncGenerator[None, None]:
    async with server.session_manager.run():
        yield


def _asgi_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8765",
    )


async def _init_http_session(client: httpx.AsyncClient) -> str:
    init = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "task-487", "version": "1.0"},
            },
        },
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert init.status_code == 200, init.text
    session_id = init.headers.get("mcp-session-id")
    assert session_id
    notified = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={
            "mcp-session-id": session_id,
            "Accept": "application/json, text/event-stream",
        },
    )
    assert notified.status_code == 202, notified.text
    return session_id


async def _call_tool_http(
    client: httpx.AsyncClient, session_id: str, req_id: int, name: str, arguments: dict
) -> httpx.Response:
    return await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={
            "mcp-session-id": session_id,
            "Accept": "application/json, text/event-stream",
        },
    )


@pytest.mark.anyio
async def test_three_toolerrors_then_valid_call_same_session():
    """The observed sequence: three application failures, then a valid call."""
    async with Client(mcp) as client:
        for _ in range(3):
            err = await client.session.call_tool("fs_read", {"path": "../../etc/passwd"})
            assert err.is_error
        listed = await client.session.call_tool("task_list", {"status": "open", "limit": 5})
        assert not listed.is_error
        payload = json.loads(listed.content[0].text)
        assert "tasks" in payload


@pytest.mark.anyio
async def test_delegation_spec_rejection_does_not_block_unrelated_tool():
    async with Client(mcp) as client:
        err = await client.session.call_tool(
            "delegate",
            {
                "task_spec": INCOMPLETE_SPEC,
                "from_agent": "Desktop_Agent5",
                "to_agent": "Desktop_Agent2",
            },
        )
        assert err.is_error
        text = "".join(getattr(block, "text", "") or "" for block in err.content or [])
        assert "Delegation Spec Standard" in text
        listed = await client.session.call_tool("task_list", {"status": "open", "limit": 5})
        assert not listed.is_error


def test_enforce_delegation_spec_still_raises_toolerror():
    """Do not weaken the spec gate to soothe a host circuit breaker."""
    with pytest.raises(ToolError, match="Delegation Spec Standard"):
        enforce_delegation_spec(INCOMPLETE_SPEC, "Desktop_Agent2")
    with pytest.raises(ToolError, match="Delegation Spec Standard"):
        delegate(
            task_spec=INCOMPLETE_SPEC,
            from_agent="Desktop_Agent5",
            to_agent="Desktop_Agent2",
        )


@pytest.mark.asyncio
async def test_http_session_survives_three_application_errors_then_valid_call():
    server = MCPServer("task-487-isolation")

    @server.tool()
    def reject(reason: str) -> str:
        raise ToolError(reason)

    @server.tool()
    def echo(message: str) -> str:
        return f"echo: {message}"

    app = server.streamable_http_app()
    async with _running_session_manager(server):
        async with _asgi_client(app) as client:
            session_id = await _init_http_session(client)
            for i in range(3):
                resp = await _call_tool_http(
                    client, session_id, i + 2, "reject", {"reason": "missing spec"}
                )
                assert resp.status_code == 200, resp.text
                assert "missing spec" in resp.text
                assert resp.headers.get("mcp-session-id", session_id) == session_id
            ok = await _call_tool_http(client, session_id, 5, "echo", {"message": "still-alive"})
            assert ok.status_code == 200, ok.text
            assert "echo: still-alive" in ok.text
            assert ok.headers.get("mcp-session-id", session_id) == session_id


@pytest.mark.asyncio
async def test_http_session_survives_invalid_params_then_valid_call():
    """JSON-RPC schema rejection is also an application error, not a dead hub."""
    server = MCPServer("task-487-invalid-params")

    @server.tool()
    def echo(message: str) -> str:
        return f"echo: {message}"

    app = server.streamable_http_app()
    async with _running_session_manager(server):
        async with _asgi_client(app) as client:
            session_id = await _init_http_session(client)
            for i in range(3):
                resp = await _call_tool_http(client, session_id, i + 2, "echo", {})
                assert resp.status_code == 200, resp.text
                body = resp.text.lower()
                assert "iserror" in body.replace("_", "")
                assert "field required" in body or "required" in body
            ok = await _call_tool_http(
                client, session_id, 5, "echo", {"message": "after-invalid-params"}
            )
            assert ok.status_code == 200, ok.text
            assert "echo: after-invalid-params" in ok.text
