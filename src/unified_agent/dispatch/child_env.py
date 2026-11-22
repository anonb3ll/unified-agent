"""Explicit child-environment allowlist for unattended dispatch spawns.

``CliProcessSpawner`` previously did ``env = dict(os.environ)`` and then
merged the dispatch request extras. That inherited the hub service environment
wholesale — including ``UA_AGENT_TOKENS`` (every agent's bearer), provider API
keys, and anything else landed in ``service.env``.

A dispatched Claude/Codex/agy child must receive only what it needs to run as
that persona: OS path/locale basics, its own hub bearer (when the MCP config
uses ``${env:UA_AGENT_BEARER_TOKEN}``), and the ``UA_DISPATCH_*`` binding
vars. Everything else stays in the parent.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from .. import env_allowlist

# The allowlist itself is shared with the executor (task #1910) and lives in
# ``unified_agent.env_allowlist``. These aliases keep existing importers working.
_SAFE_EXACT = env_allowlist.SAFE_EXACT
_DENY_EXACT = env_allowlist.DENY_EXACT
_DENY_SUBSTRING_RE = env_allowlist.DENY_SUBSTRING_RE
_DENY_SUBSTRING_ALLOW = env_allowlist.DENY_SUBSTRING_ALLOW
_is_denied = env_allowlist.is_denied
resolve_agent_bearer_token = env_allowlist.resolve_agent_bearer_token

# Prefixes allowed after the deny-list check. Dispatch binding vars always win
# via ``extras``; the prefix keeps any additional UA_DISPATCH_* annotations.
_SAFE_PREFIXES = (
    "UA_DISPATCH_",
    "LC_",
)


def _is_allowlisted(name: str) -> bool:
    return env_allowlist.is_allowlisted(name, safe_prefixes=_SAFE_PREFIXES)


def build_child_env(
    parent: Mapping[str, str] | None = None,
    *,
    extras: Mapping[str, Any] | None = None,
    agent_label: str | None = None,
) -> dict[str, str]:
    """Build a scrubbed environment for a dispatched CLI child.

    ``extras`` (typically the launch request's ``UA_DISPATCH_*`` map) is applied
    last and still filtered through the deny-list so a buggy caller cannot
    smuggle ``UA_AGENT_TOKENS`` via the request.

    When ``agent_label`` is set, the child receives only that label's bearer
    from ``UA_AGENT_TOKENS`` — never the parent process's ``UA_AGENT_BEARER_TOKEN``.
    """
    source = os.environ if parent is None else parent
    bound_bearer = resolve_agent_bearer_token(agent_label) if agent_label else None
    out: dict[str, str] = {}
    for key, value in source.items():
        if value is None:
            continue
        # Never inherit the parent hub bearer — inject only from bound_bearer.
        if key == "UA_AGENT_BEARER_TOKEN":
            continue
        if not _is_allowlisted(key):
            continue
        out[key] = str(value)

    for key, value in (extras or {}).items():
        if value is None:
            continue
        name = str(key)
        if name == "UA_AGENT_BEARER_TOKEN":
            continue
        if _is_denied(name):
            continue
        # Extras may introduce UA_DISPATCH_* and UA_AGENT_LABEL even when they
        # were absent from the parent; still refuse anything matching deny.
        if not (_is_allowlisted(name) or name.startswith("UA_DISPATCH_")):
            continue
        out[name] = str(value)

    if bound_bearer:
        out["UA_AGENT_BEARER_TOKEN"] = bound_bearer
    return out
