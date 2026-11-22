"""Hub-wide agent access controls.

The three switches are independent.  Configuration is server-side only; a
caller cannot request a less restrictive mode.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from mcp.server.mcpserver.exceptions import ToolError

from . import config


class AgentAccessDenied(ToolError):
    """The hub-wide access policy refused an operation."""


#: Auth channels on which the verified operator gets the human-recovery
#: exemption from the access switches. A request naming the operator on any
#: other channel (or none) gets no exemption. The hub middleware, the governed
#: HTTP reads and the ``/api/events`` read all use this one set.
OPERATOR_RECOVERY_VIA = frozenset({"bearer", "oauth", "cookie", "workos"})


def is_operator_recovery(identity: str | None, via: str | None) -> bool:
    """True when the verified human operator calls on a human auth channel."""
    return bool(identity) and identity == config.operator_label() and via in OPERATOR_RECOVERY_VIA


# (tool, identity) of the one read an HTTP route admitted under operator recovery.
_recovery_read: ContextVar[tuple[str, str] | None] = ContextVar(
    "agent_access_recovery_read", default=None
)


@contextmanager
def operator_recovery_read(tool: str, identity: str) -> Iterator[None]:
    """Let one registered read run under the operator's human-recovery exemption.

    A route enters this only after ``is_operator_recovery`` held for the request.
    The grant names one tool and one identity: the registered wrapper honours it
    only for a read-only call of that tool by that identity, so it never covers
    a write, another tool, or another caller.
    """
    token = _recovery_read.set((tool, identity))
    try:
        yield
    finally:
        _recovery_read.reset(token)


def recovery_read_granted(tool: str, *, read_only: bool) -> bool:
    """Whether the current call is the read an ``operator_recovery_read`` admitted."""
    grant = _recovery_read.get()
    if grant is None or not read_only or grant[0] != tool:
        return False
    identity = config.verified_agent_identity()
    return identity == grant[1] and identity == config.operator_label()


class _Unknown:
    pass


UNKNOWN = _Unknown()


def _bool(name: str, default: bool) -> bool | _Unknown:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return UNKNOWN


@dataclass(frozen=True)
class AccessState:
    enabled: bool | _Unknown
    read_only: bool | _Unknown
    production_blocked: bool | _Unknown
    source: str

    @property
    def known(self) -> bool:
        return all(
            value is not UNKNOWN
            for value in (self.enabled, self.read_only, self.production_blocked)
        )


def state() -> AccessState:
    values = tuple(
        _bool(name, default)
        for name, default in (
            ("UA_AGENT_ACCESS_ENABLED", True),
            ("UA_AGENT_ACCESS_READ_ONLY", False),
            ("UA_AGENT_ACCESS_PRODUCTION_BLOCKED", False),
        )
    )
    configured = any(
        name in os.environ
        for name in (
            "UA_AGENT_ACCESS_ENABLED",
            "UA_AGENT_ACCESS_READ_ONLY",
            "UA_AGENT_ACCESS_PRODUCTION_BLOCKED",
        )
    )
    return AccessState(*values, source="configured" if configured else "legacy-default")


def status() -> dict[str, object]:
    current = state()
    return {
        "enabled": current.enabled if current.enabled is not UNKNOWN else None,
        "read_only": current.read_only if current.read_only is not UNKNOWN else None,
        "production_blocked": current.production_blocked
        if current.production_blocked is not UNKNOWN
        else None,
        "known": current.known,
        "source": current.source,
    }


def managed_secret_mode() -> bool:
    raw = os.environ.get("UA_MANAGED_SECRETS")
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    # An invalid server-side value fails closed rather than reopening arbitrary
    # execution while a managed secret store may already exist.
    return True


def reject_arbitrary_execution(operation: str) -> bool:
    return (
        managed_secret_mode()
        and operation
        not in {
            "profile_status",
            "system_doctor",
            "mcp_health_telemetry",
            "platform_manifest",
            "platform_object",
            "managed_secret_set",
            # A device-bound OAuth caller may spend only through the broker's
            # separate model/key/budget gates. This does not admit arbitrary
            # HTTP writes under managed-secret mode.
            "llm_broker_completion",
            # Human connector lifecycle actions use reviewed, fixed-provider
            # paths and remain subject to read-only and production gates.
            "connector_oauth_begin",
            "connector_oauth_disconnect",
            # Token lifecycle bookkeeping is secret-management, same class as
            # managed_secret_set above (task #1198) — agent_token_issue/rotate
            # call into managed_secret_set themselves, and agent_token_list is
            # a read of hash-only metadata.
            "agent_token_issue",
            "agent_token_rotate",
            "agent_token_list",
            "deployment_claim",
            # Approval queue review and resolution are governance chokepoints
            # rather than arbitrary execution.
            "approval_pending",
            "approval_resolve",
            "fs_read",
            "fs_list",
            "fs_search",
        }
        and not operation.startswith(("resource:", "platform_object:"))
    )


def enforce(
    operation: str,
    *,
    read_only: bool,
    production: bool = False,
    human_recovery: bool = False,
) -> None:
    current = state()
    if not current.known:
        raise AgentAccessDenied("agent access mode is unknown")
    if not current.enabled and not human_recovery:
        raise AgentAccessDenied("agent access is disabled")
    if not human_recovery and managed_secret_mode() and reject_arbitrary_execution(operation):
        raise AgentAccessDenied(
            "managed secret mode blocks arbitrary or unclassified execution; only diagnostics, platform reads, and the approved secret sink are permitted"
        )
    if current.read_only and not read_only and not human_recovery:
        raise AgentAccessDenied(f"agent access is read-only; operation {operation!r} is a write")
    if current.production_blocked and not human_recovery:
        # Production classification is trusted server metadata, never a client field.
        if production:
            raise AgentAccessDenied(f"production access is blocked for operation {operation!r}")
        runtime = os.environ.get("UA_RUNTIME_ENV", "").strip().lower()
        if runtime not in {"development", "validation", "production"}:
            raise AgentAccessDenied("production access classification is unknown")
        if runtime == "production":
            raise AgentAccessDenied(f"production access is blocked for operation {operation!r}")
