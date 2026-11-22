"""MCP tools for explicit, authenticated operational-profile activation."""

from __future__ import annotations

import hashlib
import time

from mcp.server.mcpserver.exceptions import ToolError

from .. import agent_access, config, events, managed_secrets, profiles
from .approvals import check_approval_gate


def profile_activate(profile: str) -> dict:
    """Activate developer, steward, or chief-of-staff for this authenticated MCP session.

    This is intentionally session-scoped: a new MCP session starts at the
    verified identity's default (chief-of-staff for cloud_agent13 and the
    Laptop_Agent5 backup, developer for engineering identities, including cloud_agent17).
    Call only after the operator explicitly invokes the matching host command.
    """
    active = profiles.activate(profile)
    events.record_event(
        "profile.activated",
        payload={"profile": active.name, "session_id": active.session_id},
        originator=active.identity,
    )
    return {"profile": active.name, "identity": active.identity, "session_scoped": True}


def profile_reset() -> dict:
    """Reset this authenticated MCP session to its verified identity default."""
    identity = config.verified_agent_identity()
    profile = profiles.reset()
    events.record_event("profile.reset", payload={"profile": profile}, originator=identity)
    return {"profile": profile, "identity": identity, "session_scoped": True}


def profile_status() -> dict:
    """Report the profile active for this request without exposing session secrets."""
    metadata = config.current_auth_metadata() or {}
    issued_at = metadata.get("iat")
    token_age_s = None
    if isinstance(issued_at, (int, float)):
        token_age_s = max(0, round(time.time() - issued_at, 3))
    identity = config.verified_agent_identity()
    status = {
        "profile": profiles.current(),
        "identity": identity,
        "session_scoped": profiles._current_session_id() is not None,
        "auth_method": metadata.get("auth_method"),
        "scopes": metadata.get("scopes", []),
        "token_age_s": token_age_s,
        "agent_access": agent_access.status(),
    }
    grant = profiles.current_session_grant()
    if grant is not None:
        # A session credential can see its own grant (task #1744); never the value.
        status["session"] = {
            "credential_id": grant.credential_id,
            "item_id": grant.item_id,
            "task_id": grant.task_id,
            "tools": sorted(grant.tools),
            "expires_at": metadata.get("expires_at"),
        }
    seats = _team_seats(identity)
    if seats:
        status["team_seats"] = seats
    return status


def _team_seats(identity: str | None) -> list[dict]:
    """Compact team-seat view (task #1736); full detail is coordination_manage team_seat.

    Never raises: a malformed teams block must not break identity checks.
    """
    if not identity:
        return []
    try:
        from .. import team_spec

        return [
            {
                "team": seat["team"],
                "harness": seat["harness"],
                "teammates": seat["teammates"],
                "edges_out": len(seat["edges"]["outgoing"]),
                "edges_in": len(seat["edges"]["incoming"]),
            }
            for seat in team_spec.seats_for(identity)
        ]
    except Exception:  # noqa: BLE001 - advisory field only
        return []


def resolve_secret_org(org_id: str | None) -> str | None:
    """The tenant a managed-secret write lands in (task #1343).

    Under an enforced tenant boundary (``edition.tenant_org_id()``) a caller may
    only write its own tenant; naming another org is refused. Without one (the
    private single-tenant hub) ``org_id`` is an explicit operator choice and
    omitting it keeps today's root layout.
    """
    from .. import edition

    tenant = edition.tenant_org_id()
    # None means "not tenant-scoped"; any explicit string (even blank) asks for
    # a tenant and must be a valid one. Validation happens here, before the
    # approval gate, so an approved pass can never fail on a bad org_id.
    requested = None if org_id is None else managed_secrets._check_org_id(org_id)
    if tenant is not None:
        tenant = managed_secrets._check_org_id(tenant)
        if requested not in (None, tenant):
            raise ToolError("managed secret writes are limited to the caller's own org_id")
        return tenant
    return requested


# Internal consumers a managed secret may be bound to. mcp_gateway (task #1336)
# holds downstream MCP server bearers for the gateway tool.
MANAGED_SECRET_CONSUMERS = frozenset(
    {"zulip", "evidence-signing", "agent_token", "llm", "connector", "mcp_gateway"}
)


def managed_secret_payload(
    name: str,
    value: str,
    consumer: str = "zulip",
    org_id: str | None = None,
    caller_identity: str | None = None,
) -> dict:
    """Validate the sink before either gate and expose only a digest to policy."""
    import re

    identity = caller_identity or config.verified_agent_identity()
    if not identity:
        raise ToolError("managed secret writes require an authenticated identity")
    if not agent_access.managed_secret_mode():
        raise ToolError("managed secret mode is disabled")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", name):
        raise ToolError("invalid managed secret name")
    if not isinstance(value, str) or not value or len(value.encode()) > 65536:
        raise ToolError("managed secret value must be a nonempty string of at most 65536 bytes")
    if consumer not in MANAGED_SECRET_CONSUMERS:
        raise ToolError("unsupported managed secret consumer")
    payload = {
        "name": name,
        "consumer": consumer,
        "identity": identity,
        "value_sha256": hashlib.sha256(value.encode()).hexdigest(),
    }
    resolved_org = resolve_secret_org(org_id)
    if resolved_org is None:
        # Validated here, before any gate, so an approved pass never fails on it.
        managed_secrets.check_root_name(name)
    if resolved_org is not None:
        # Only present when scoped, so single-tenant approval payloads (and any
        # pending approval rows keyed on them) are byte-identical to before.
        payload["org_id"] = resolved_org
    managed_secrets.validate_write_configuration(name, org_id=resolved_org)
    return payload


def managed_secret_set(
    name: str, value: str, consumer: str = "zulip", org_id: str | None = None
) -> dict:
    """Store a provider secret; only digest metadata reaches the approval queue.

    `org_id` scopes the secret to one tenant (task #1343). Omit it on a
    single-tenant hub; under an enforced tenant boundary it defaults to, and may
    only be, the caller's own org.
    """
    agent_access.enforce("managed_secret_set", read_only=False)
    payload = managed_secret_payload(name, value, consumer, org_id)
    identity = config.verified_agent_identity()
    check_approval_gate("managed_secret_set", payload, requested_by=identity, force=True)
    return managed_secrets.set_secret(
        name, value, requested_by=identity, consumer=consumer, org_id=payload.get("org_id")
    )
