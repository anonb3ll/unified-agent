"""Typed codes on MCP tool errors (task #2054).

A ``TypedToolError`` is an ordinary ``ToolError`` (same text, same ``isError``
result) that also names a machine code. ``install`` makes the MCP ``tools/call``
handler put ``{"code": ..., **data}`` in the error result's ``structuredContent``,
so a client reads the outcome instead of matching prose. Only the code and its
fixed keys (``approval_id``) go on the wire: never a payload or argument.

Codes:

* ``conflict``: a stale revision, a run or lease fence, a claim held by
  another agent, or a lost claim race. Read the object again before retrying.
* ``approval_required``: the call is parked in the approval queue as
  ``approval_id``; retry it unchanged once a human approves.
* ``permission_denied``: the caller's policy forbids the action; a newer object
  revision cannot authorize it.
* ``hosted_execution_refused``: the hub is hosted (``UA_HOSTED=1``) and will
  not execute this work on itself. Send it to a node worker.
* ``outcome_unknown``: the write began and then failed (an I/O error after the
  first byte), so it may have landed in part. Read the object before running
  it again; a blind retry can duplicate it.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolRequestParams, CallToolResult, TextContent
from pydantic import ValidationError

logger = logging.getLogger("mcp.server.mcpserver.server")


class TypedToolError(ToolError):
    """A ``ToolError`` with a stable ``code`` and a few non-secret ``data`` keys."""

    code = ""

    def __init__(self, message: str, **data: Any):
        super().__init__(message)
        self.data = data

    def wire(self) -> dict[str, Any]:
        return {"code": self.code, **{k: v for k, v in self.data.items() if v is not None}}


class ConflictError(TypedToolError):
    """The object changed or is held by another claimant (HTTP 409)."""

    code = "conflict"


class OutcomeUnknownError(TypedToolError):
    """A write failed after it began, so it may have partly happened (HTTP 409).

    Unlike a refusal, nothing says the object is unchanged: an append may have
    left part of its line. Read the object before retrying (task #2521).
    """

    code = "outcome_unknown"


class PermissionDeniedError(TypedToolError):
    """The caller is not authorized; reading a newer revision cannot admit it."""

    code = "permission_denied"


def install(server: MCPServer) -> None:
    """Replace ``tools/call`` with the SDK handler plus the typed-code branch.

    The body mirrors ``MCPServer._handle_call_tool`` (mcp 2.3.0): the same
    text, logging and ``isError`` result for every error, and a typed error's
    code added as ``structuredContent``. ``Tool.run`` re-raises a tool's own
    ``ToolError`` as ``Error executing tool <name>: ...`` from the original, so
    the code is read from ``__cause__``.
    """

    async def handle_call_tool(ctx, params: CallToolRequestParams):
        context = Context(
            request_context=ctx,
            mcp_server=server,
            input_params=params,
            subscriptions=server._subscriptions,
        )
        try:
            return await server.call_tool(params.name, params.arguments or {}, context)
        except MCPError:
            raise
        except Exception as exc:
            structured = None
            if isinstance(exc, ToolError) and not isinstance(exc, UnexpectedToolError):
                if isinstance(exc.__cause__, ValidationError):
                    fields = sorted(
                        {".".join(str(p) for p in err["loc"]) for err in exc.__cause__.errors()}
                    )
                    logger.info("Tool %r rejected arguments: %r", params.name, fields)
                else:
                    logger.info("Tool %r failed: %r", params.name, str(exc))
                if isinstance(exc.__cause__, TypedToolError):
                    structured = exc.__cause__.wire()
            else:
                logger.exception("Tool %r raised an unexpected exception", params.name)
            return CallToolResult(
                content=[TextContent(type="text", text=str(exc))],
                is_error=True,
                structured_content=structured,
            )

    server._lowlevel_server.add_request_handler(
        "tools/call", CallToolRequestParams, handle_call_tool
    )
