"""Allowlisted environment for executor backend children (task #1910).

Every native executor backend (codex, agy, claude, cursor, copilot, hermes,
devin) used to start its child from ``dict(os.environ)`` and remove three bearer
variables. Everything else the daemon held reached the harness: provider API
keys, ``UA_MANAGED_SECRET_KEYS`` (the managed-secret Fernet key), Slack and
GitHub tokens. The child now gets only:

* the shared process plumbing in ``unified_agent.env_allowlist`` (``PATH``,
  ``HOME``, locale, ``TERM``, ``XDG_*``, ``UA_WORKSPACE``/``UA_HOME``/...);
* the hub URL variables, which are not secrets;
* the backend's own config locations from ``BACKEND_ENV`` (``CODEX_HOME``,
  the ``*-with-hub`` wrappers' token-file paths, ...);
* the OpenRouter route for backends that can use it (``BACKEND_PROVIDER_ENV``);
* names the operator lists in ``UA_EXECUTOR_ENV_PASSTHROUGH``. A credential-
  looking name must be scoped to one backend (``codex:OPENAI_API_KEY``), and
  ``env_allowlist.DENY_EXACT`` is never passed;
* ``UA_AGENT_LABEL`` and a ``UA_AGENT_BEARER_TOKEN`` bound to the executing
  agent (registry first, then the same-label ``UA_EXECUTOR_TOKEN``), never the
  daemon's ambient bearer.

The Pi backend has its own, narrower allowlist in ``tools/pi_runner.py``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from .. import config, env_allowlist

logger = logging.getLogger("unified_agent.executor")

# Hub location variables: addresses, not credentials.
_EXECUTOR_SAFE_EXACT = env_allowlist.SAFE_EXACT | frozenset(
    {
        "UA_HTTP_HUB_URL",
        "UA_HUB_URL",
    }
)
_EXECUTOR_SAFE_PREFIXES = ("LC_", "XDG_")

# Per-backend config locations. Several hold the *path* of a token file, which
# trips the credential-name pattern; listing them here is the vetting.
BACKEND_ENV: dict[str, frozenset[str]] = {
    "codex": frozenset({"CODEX_HOME"}),
    "agy": frozenset(),
    "claude": frozenset({"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_PATH"}),
    # scripts/agent-with-hub
    "cursor": frozenset({"UA_CURSOR_TOKEN_FILE", "CURSOR_AGENT_LINK", "GBRAIN_TOKEN_FILE"}),
    # scripts/copilot-with-hub
    "copilot": frozenset(
        {
            "UA_COPILOT_TOKEN_FILE",
            "UA_COPILOT_TOKEN_SOURCE",
            "GBRAIN_TOKEN_FILE",
            "COPILOT_BIN",
            "COPILOT_HOME",
        }
    ),
    "hermes": frozenset({"HERMES_HOME"}),
    # scripts/devin-with-hub
    "devin": frozenset({"UA_DEVIN_TOKEN_FILE"}),
}

# Provider credentials a backend may use without an explicit passthrough entry.
# Executors route through OpenRouter: codex reads the key
# through a model_providers entry with env_key, and hermes reads it natively.
# No other backend can use it, so it does not reach them.
_OPENROUTER_ENV = frozenset({"OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "UA_OPENROUTER_MODEL"})
BACKEND_PROVIDER_ENV: dict[str, frozenset[str]] = {
    "codex": _OPENROUTER_ENV,
    "hermes": _OPENROUTER_ENV,
}

_BOUND_ONLY = frozenset({"UA_AGENT_BEARER_TOKEN", "UA_AGENT_LABEL"})


def _passthrough_names(backend: str | None) -> set[str]:
    names: set[str] = set()
    for scope, name in config.executor_env_passthrough():
        if name in env_allowlist.DENY_EXACT or name in _BOUND_ONLY:
            logger.warning("UA_EXECUTOR_ENV_PASSTHROUGH: %s is never passed to a child", name)
            continue
        if scope == "*":
            if env_allowlist.looks_secret(name):
                logger.warning(
                    "UA_EXECUTOR_ENV_PASSTHROUGH: %s looks like a credential; scope it "
                    "to one backend (backend:%s) to pass it",
                    name,
                    name,
                )
                continue
            names.add(name)
        elif backend is not None and scope == backend:
            names.add(name)
    return names


def _bound_bearer(parent: Mapping[str, str], agent_label: str) -> str | None:
    bearer = env_allowlist.resolve_agent_bearer_token(agent_label)
    if bearer:
        return bearer
    executor_label = (parent.get("UA_EXECUTOR_AGENT") or "").strip()
    if executor_label and executor_label == agent_label:
        return parent.get("UA_EXECUTOR_TOKEN") or None
    return None


def build_executor_child_env(
    backend: str | None,
    agent_label: str,
    *,
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the allowlisted environment for one executor backend child.

    ``backend`` selects the per-backend extras in ``BACKEND_ENV`` and the
    backend-scoped passthrough entries; ``None`` gets the shared base only.
    """
    source = os.environ if parent is None else parent
    extra = (
        BACKEND_ENV.get(backend or "", frozenset())
        | BACKEND_PROVIDER_ENV.get(backend or "", frozenset())
        | _passthrough_names(backend)
    ) - (env_allowlist.DENY_EXACT | _BOUND_ONLY)
    out: dict[str, str] = {}
    for key, value in source.items():
        if value is None or key in _BOUND_ONLY:
            continue
        if key in extra or env_allowlist.is_allowlisted(
            key, safe_exact=_EXECUTOR_SAFE_EXACT, safe_prefixes=_EXECUTOR_SAFE_PREFIXES
        ):
            out[key] = str(value)

    out["UA_AGENT_LABEL"] = agent_label
    bearer = _bound_bearer(source, agent_label)
    if bearer:
        out["UA_AGENT_BEARER_TOKEN"] = bearer
    return out
