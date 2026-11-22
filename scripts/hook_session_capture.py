#!/usr/bin/env python3
"""Claude Code SessionEnd hook -> hub passive session capture (task #813).

Reads the hook stdin JSON, POSTs {transcript_path, cwd, project} to the hub's
/api/session-capture. The hub summarizes synchronously (seconds), so the POST is
handed to a detached child and the hook returns immediately — session end is
never blocked and the child outlives the parent. Best-effort throughout: any
failure exits 0. Hub URL + bearer token are read from ~/.claude.json (the same
unified-agent MCP registration the session already uses) — no new secret.
"""

import json
import os
import sys
import urllib.request
from pathlib import Path


def _hub_endpoint() -> tuple[str, str] | None:
    try:
        cfg = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
        entry = cfg.get("mcpServers", {}).get("unified-agent", {})
        url = entry.get("url", "")
        token = entry.get("headers", {}).get("Authorization", "")
        if not url or not token:
            return None
        base = url.rsplit("/mcp", 1)[0]
        return f"{base}/api/session-capture", token
    except Exception:
        return None


def _post(url: str, token: str, payload: bytes) -> None:
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": token},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=90)  # noqa: S310 - fixed hub URL from own config
    except Exception:
        pass


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    transcript_path = data.get("transcript_path") or ""
    cwd = data.get("cwd") or ""
    if not transcript_path or not cwd:
        return 0

    endpoint = _hub_endpoint()
    if endpoint is None:
        return 0
    url, token = endpoint
    payload = json.dumps(
        {"transcript_path": transcript_path, "cwd": cwd, "project": Path(cwd).name}
    ).encode("utf-8")

    # Detach: the hub summarizes synchronously, so do the POST in a child that
    # survives this hook returning (and the parent Claude Code process exiting).
    try:
        if os.fork() != 0:
            return 0
    except OSError:
        _post(url, token, payload)  # no fork() available — fall back to inline
        return 0
    os.setsid()
    _post(url, token, payload)
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
