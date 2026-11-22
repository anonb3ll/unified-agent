"""Render the one hub entry a harness needs behind the MCP gateway (task #1336).

Print only. Nothing here reads a credential, opens a connection or touches a
harness config file; an operator pastes the output, and registration stays the
red-tier step it always was. The formats follow docs/harnesses.md. Tokens appear
as a placeholder or an environment variable name, never a value.
"""

from __future__ import annotations

import json
import os

from ..hub_client import PlatformCliError, _platform_url
from .result import CliError

SERVER_NAME = "unified-agent"
TOKEN_ENV = "UA_AGENT_BEARER_TOKEN"
HARNESSES = ("claude", "codex", "cursor", "antigravity", "opencode", "hermes")
_NOTE = (
    "Printed only; no harness configuration was read or changed. This is the "
    "single entry: with the hub's mcp_gateway on, remove per-server entries for "
    "each fronted server only after a live call through the hub."
)


def _json(entry: dict) -> str:
    return json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2) + "\n"


def _claude(url: str) -> tuple[str, str]:
    entry = {
        "type": "http",
        "url": url,
        "headers": {"Authorization": "Bearer <this-agent-token>"},
    }
    return "~/.claude.json (top-level mcpServers)", _json(entry)


def _codex(url: str) -> tuple[str, str]:
    text = f"[mcp_servers.{SERVER_NAME}]\nurl = {json.dumps(url)}\n"
    return "~/.codex/config.toml", text + f'bearer_token_env_var = "{TOKEN_ENV}"\n'


def _cursor(url: str) -> tuple[str, str]:
    entry = {"url": url, "headers": {"Authorization": "Bearer ${env:UA_CURSOR_HUB_TOKEN}"}}
    return "~/.cursor/mcp.json", _json(entry)


def _antigravity(url: str) -> tuple[str, str]:
    return "~/.gemini/config/mcp_config.json", _json({"serverUrl": url})


def _opencode(url: str) -> tuple[str, str]:
    entry = {
        "type": "remote",
        "url": url,
        "headers": {"Authorization": "Bearer {env:" + TOKEN_ENV + "}"},
    }
    return "~/.config/opencode/opencode.json (mcp key)", json.dumps(
        {"mcp": {SERVER_NAME: entry}}, indent=2
    ) + "\n"


def _hermes(url: str) -> tuple[str, str]:
    text = f"mcp_servers:\n  {SERVER_NAME}:\n    url: {json.dumps(url)}\n"
    return "~/.hermes/config.yaml", text


_RENDERERS = {
    "claude": _claude,
    "codex": _codex,
    "cursor": _cursor,
    "antigravity": _antigravity,
    "opencode": _opencode,
    "hermes": _hermes,
}


def render_entries(hub: str | None, harness: str | None = None) -> dict:
    """The entry for one harness, or for each, with no side effect of any kind."""
    url = (hub or os.environ.get("UA_HTTP_HUB_URL", "")).strip()
    if not url:
        raise CliError("configuration", "Set --hub or UA_HTTP_HUB_URL to the hub /mcp URL.", 2)
    try:
        _platform_url(url, "/mcp")
    except PlatformCliError:
        raise CliError("validation", "The hub URL must be http(s) and end in /mcp.", 2) from None
    names = HARNESSES if harness is None else (harness,)
    entries = []
    for name in names:
        path, config = _RENDERERS[name](url)
        entries.append({"harness": name, "path": path, "config": config})
    return {"hub": url, "server": SERVER_NAME, "entries": entries, "note": _NOTE}


def text(data: dict) -> str:
    """Plain output: each snippet as it would be pasted."""
    parts = [f"# {data['note']}"]
    for entry in data["entries"]:
        parts.append(f"# {entry['harness']}: {entry['path']}\n{entry['config'].rstrip()}")
    return "\n\n".join(parts)
