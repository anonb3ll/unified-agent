#!/usr/bin/env python3
"""Probe a throwaway failover hub. Prints counts, never the bearer token."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx


def _token_from_file(path: Path) -> str:
    raw = path.read_text(encoding="utf-8").strip()
    if ":" not in raw:
        raise SystemExit("token file must be label:token")
    _label, token = raw.split(":", 1)
    token = token.strip()
    if len(token) < 16:
        raise SystemExit("token too short")
    return token


def _jsonrpc_result(response: httpx.Response) -> dict:
    text = response.text
    if text.startswith("event:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[5:].strip()
                break
    payload = json.loads(text)
    if "error" in payload:
        raise SystemExit(f"jsonrpc error: {payload['error']}")
    return payload.get("result") or {}


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: failover_probe.py MCP_URL TOKEN_FILE")
    url = sys.argv[1]
    token = _token_from_file(Path(sys.argv[2]))
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30.0) as client:
        init = client.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "failover-rehearsal", "version": "0"},
                },
            },
        )
        init.raise_for_status()
        session = init.headers.get("mcp-session-id")
        if not session:
            raise SystemExit("missing mcp-session-id")
        headers["mcp-session-id"] = session
        notified = client.post(
            url,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        if notified.status_code not in {200, 202}:
            raise SystemExit(f"initialized notification status={notified.status_code}")
        called = client.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "task_list",
                    "arguments": {"status": "open", "fields": "compact", "limit": 3},
                },
            },
        )
        called.raise_for_status()
        result = _jsonrpc_result(called)
        structured = result.get("structuredContent") or result
        content = result.get("content") or []
        if isinstance(structured, dict) and "tasks" in structured:
            tasks = structured["tasks"]
        else:
            text = ""
            for block in content:
                if block.get("type") == "text":
                    text = block.get("text") or ""
                    break
            parsed = json.loads(text) if text else {}
            tasks = parsed.get("tasks") if isinstance(parsed, dict) else parsed
        count = len(tasks) if isinstance(tasks, list) else "unknown"
        print(f"mcp_initialize=ok session=yes task_list_rows={count}")
        if not tasks:
            raise SystemExit("task_list returned no rows")


if __name__ == "__main__":
    main()
