"""Red-loop for task #487: three application isError results vs Hermes breaker.

Set HERMES_AGENT_ROOT to your hermes-agent checkout, then run:

    HERMES_HOME=/tmp/hermes-repro-487 \\
    "${HERMES_AGENT_ROOT}/venv/bin/python" \\
      scripts/repro_hermes_mcp_breaker.py

Expected before the client fix: the fourth (valid) call is short-circuited as
"MCP server 'unified-agent' is unreachable after 3 consecutive failures".
Expected after: the fourth call hits the session and returns ok.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from unittest.mock import MagicMock

HERMES_ROOT = os.environ.get("HERMES_AGENT_ROOT", os.path.expanduser("~/.hermes/hermes-agent"))
sys.path.insert(0, HERMES_ROOT)

os.environ.setdefault("HERMES_HOME", tempfile.mkdtemp(prefix="hermes-repro-487-"))


class _NullLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def main() -> int:
    from tools import mcp_tool
    from tools.mcp_tool import _make_tool_handler

    calls = {"n": 0}

    async def call_tool(*_a, **_kw):
        calls["n"] += 1
        result = MagicMock()
        result.structured_content = None
        block = MagicMock()
        if calls["n"] <= 3:
            result.is_error = True
            block.text = (
                "delegation does not meet the Delegation Spec Standard: "
                "missing required section(s): objective, definition of done, "
                "tdd, boundaries"
            )
            result.content = [block]
            return result
        result.is_error = False
        block.text = "ok"
        result.content = [block]
        return result

    server = MagicMock()
    server.name = "unified-agent"
    server.session = MagicMock()
    server.session.call_tool = call_tool
    server._rpc_lock = _NullLock()
    server._is_recycled_stdio.return_value = False
    server._reconnect_event = None

    mcp_tool._servers["unified-agent"] = server
    mcp_tool._server_error_counts.pop("unified-agent", None)
    mcp_tool._server_breaker_opened_at.pop("unified-agent", None)
    mcp_tool._ensure_mcp_loop()

    handler = _make_tool_handler("unified-agent", "delegate", 10.0)
    rows = []
    for i in range(4):
        parsed = json.loads(handler({"task_spec": "incomplete"} if i < 3 else {}))
        rows.append(
            {
                "call": i + 1,
                "session_calls": calls["n"],
                "keys": sorted(parsed.keys()),
                "error_prefix": str(parsed.get("error", parsed.get("result", "")))[:160],
            }
        )

    print(json.dumps({"threshold": mcp_tool._CIRCUIT_BREAKER_THRESHOLD, "rows": rows}, indent=2))
    fourth = rows[3]
    unreachable = "unreachable" in fourth["error_prefix"].lower()
    if unreachable or fourth["session_calls"] < 4:
        print(
            "RED: application isError poisoned the Hermes MCP circuit breaker; "
            "the valid fourth call did not reach the session.",
            file=sys.stderr,
        )
        return 1
    print("GREEN: three application errors did not hide a later valid call.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
