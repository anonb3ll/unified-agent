"""Shared allowlist for the environment handed to spawned agent CLIs.

Two spawn paths start agent harnesses as children of a hub-side process:
dispatch (``dispatch/child_env.py``) and the active executor
(``executor/child_env.py``). Both used to start from ``dict(os.environ)``, which
carries ``UA_AGENT_TOKENS`` (every agent's bearer), ``UA_MANAGED_SECRET_KEYS``
(the Fernet key for managed secrets), provider API keys and anything else the
service environment holds. This module is the one place that decides which
names are ordinary process plumbing a child may inherit and which are secrets
it must never see. Each spawn path adds only its own binding variables on top.
"""

from __future__ import annotations

import re

from . import config

# Exact names always eligible for passthrough when present in the parent.
SAFE_EXACT = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "TERM",
        "COLORTERM",
        "TZ",
        "DISPLAY",
        "XDG_RUNTIME_DIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        # Single-agent hub bearer used by Cursor/Claude MCP env pass-through.
        # Never confuse with UA_AGENT_TOKENS (the full multi-agent map). Spawn
        # paths drop the inherited value and inject the bound agent's own.
        "UA_AGENT_BEARER_TOKEN",
        "UA_AGENT_LABEL",
        "UA_WORKSPACE",
        "UA_HOME",
        "UA_VAULT_PATH",
        # Claude Code may need its own config home when not using $HOME default.
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_PATH",
    }
)

# Names that are never handed to a child, whatever an allowlist or an operator
# passthrough says. These are hub authority, not harness configuration.
HARD_DENY = frozenset(
    {
        "UA_AGENT_TOKENS",
        "UA_MANAGED_SECRET_KEYS",
        "UA_EXECUTOR_TOKEN",
        "UA_HTTP_BEARER",
        "UA_ENFORCE_AUTH",
        "UA_APPROVAL_TOOLS",
    }
)

# Never passed by the generic rules even if they match a safe prefix.
DENY_EXACT = HARD_DENY | frozenset(
    {
        "DATABASE_URL",
        "SSH_AUTH_SOCK",  # do not let the child talk to the parent's agent
    }
)

DENY_SUBSTRING_RE = re.compile(
    r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY|BEARER|CREDENTIAL"
    r"|(?:^|_)KEYS?(?:$|_))"
)

# Explicit exceptions to the substring deny (already vetted above).
DENY_SUBSTRING_ALLOW = frozenset(
    {
        "UA_AGENT_BEARER_TOKEN",  # single-agent MCP auth — intentional
        "UA_DISPATCH_RUN_ID",
        "UA_DISPATCH_TASK_ID",
        "UA_DISPATCH_AGENT",
        "UA_DISPATCH_PROFILE",
        "TMPDIR",
        "TEMP",
        "TERM",
        "COLORTERM",
    }
)


def resolve_agent_bearer_token(agent_label: str) -> str | None:
    """Return the hub bearer for ``agent_label`` from ``UA_AGENT_TOKENS``."""
    if not agent_label:
        return None
    tokens = config.agent_tokens()
    for tok, lbl in tokens.items():
        if lbl == agent_label:
            return tok
    # A token configured under a case variant is the same identity (#2315 review).
    wanted = config.identity_fold(agent_label)
    for tok, lbl in tokens.items():
        if config.identity_fold(lbl) == wanted:
            return tok
    return None


def looks_secret(name: str) -> bool:
    """True when ``name`` reads like a credential (``*_TOKEN``, ``*_KEY``, ...)."""
    return bool(DENY_SUBSTRING_RE.search(name))


def is_denied(name: str) -> bool:
    if name in DENY_EXACT:
        return True
    if name in DENY_SUBSTRING_ALLOW:
        return False
    if name in SAFE_EXACT:
        return False
    return looks_secret(name)


def is_allowlisted(
    name: str,
    *,
    safe_exact: frozenset[str] = SAFE_EXACT,
    safe_prefixes: tuple[str, ...] = (),
) -> bool:
    if is_denied(name):
        return False
    if name in safe_exact:
        return True
    return any(name.startswith(prefix) for prefix in safe_prefixes)
