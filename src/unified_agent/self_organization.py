"""Self-organization proposals with human/policy gate (task #1431).

Core invariants:
1. PROPOSING IS ALWAYS ON BY DEFAULT. Agents may propose organizational changes
   (roles, lane and routing structure, delegation shape, and new routines).
2. APPLYING IS NEVER AUTOMATIC. Proposing lands in a review queue where a human
   or policy approval gate resolves it. An agent cannot self-apply changes.
3. TOGGLEABLE VIA CONTROL_FLAGS. The feature can be switched off via the
   existing `control_flags` table (key='self_organization'). When toggled off,
   proposing is suppressed.
4. SCOPE-AS-MEMBERSHIP (task #704). Agents can only propose changes for offices
   they are members of (org_registry.offices_for_identity). Scope escape is denied.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, events, org_registry, storage
from .tools import approvals

CONTROL_FLAG_KEY = "self_organization"
APPLY_TOOL = "org_proposal_apply"

PROPOSED = "proposed"
REVIEWED = "reviewed"
APPLIED = "applied"
REJECTED = "rejected"

LIFECYCLE_STATES = (PROPOSED, REVIEWED, APPLIED, REJECTED)
TERMINAL_STATES = (APPLIED, REJECTED)
VALID_PROPOSAL_TYPES = frozenset({"role", "lane", "routing", "delegation", "routine", "other"})


class SelfOrganizationDisabledError(ToolError):
    """Raised when an agent attempts to propose changes while self-organization is toggled off."""


class ScopeEscapeError(ToolError):
    """Raised when an agent attempts to propose changes outside its member offices."""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Ensure the org_proposals schema exists."""
    from .storage import _ORG_PROPOSALS_SCHEMA

    conn.executescript(_ORG_PROPOSALS_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(org_proposals)").fetchall()}
    if "review_notes" not in cols:
        conn.execute("ALTER TABLE org_proposals ADD COLUMN review_notes TEXT")


def _same_identity(left: str | None, right: str | None) -> bool:
    """True when two labels are the same identity, including human aliases."""
    if not left or not right:
        return False
    if left == right:
        return True
    for canonical, aliases in config.identity_alias_allowlist().items():
        group = {canonical, *aliases}
        if left in group and right in group:
            return True
    return False


def _current_agent_label() -> str | None:
    """Return the active agent label from environment if present."""
    return os.environ.get("UA_AGENT_LABEL") or os.environ.get("AGENT_LABEL")


def is_self_organization_enabled(conn: sqlite3.Connection | None = None) -> bool:
    """Check if self-organization proposing is enabled.

    Default is True (always on by default) unless explicitly set to a falsey
    value in the control_flags table.
    """
    close_conn = False
    if conn is None:
        conn = storage.connect()
        close_conn = True
    try:
        row = conn.execute(
            "SELECT value FROM control_flags WHERE key = ?",
            (CONTROL_FLAG_KEY,),
        ).fetchone()
        if row is None:
            return True
        val = str(row["value"]).strip().lower()
        return val not in {"false", "0", "off", "disabled", "no"}
    finally:
        if close_conn:
            conn.close()


def set_self_organization_toggle(
    enabled: bool,
    *,
    reason: str | None = None,
    set_by: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Toggle self-organization proposing on or off in control_flags."""
    actor = set_by or config.verified_agent_identity() or "operator"
    now = storage.now()
    val_str = "true" if enabled else "false"

    close_conn = False
    if conn is None:
        conn = storage.connect()
        close_conn = True
    try:
        conn.execute(
            """
            INSERT INTO control_flags (key, value, reason, set_by, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              reason = excluded.reason,
              set_by = excluded.set_by,
              updated_at = excluded.updated_at
            """,
            (CONTROL_FLAG_KEY, val_str, reason, actor, now),
        )
        conn.commit()
    finally:
        if close_conn:
            conn.close()

    return {
        "key": CONTROL_FLAG_KEY,
        "enabled": enabled,
        "reason": reason,
        "set_by": actor,
        "updated_at": now,
    }


def validate_proposal_scope(author: str, scope: str) -> None:
    """Validate that scope is an existing office and author has membership.

    Scope-as-membership (task #704): an agent may not propose changes outside
    its own member offices. Founder/human identities have global authority.
    """
    offices = org_registry.load_registry()
    matched = org_registry.find_office(scope, offices)
    if matched is None:
        raise ToolError(f"unknown org unit / office scope: {scope!r}")

    office_id = matched["id"]

    # Humans have global authority across all offices
    if (
        config.actor_kind_for_identity(author) == "human"
        or author == config.operator_label()
        or author in config.operator_aliases()
    ):
        return

    member_offices = org_registry.offices_for_identity(author, offices)
    if office_id not in member_offices:
        raise ScopeEscapeError(
            f"scope escape denied: agent {author!r} is not a member of office {office_id!r} "
            f"(member of: {member_offices})"
        )


def _proposal_id() -> str:
    return f"orgprop-{secrets.token_hex(8)}"


def _public_proposal(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    record = dict(row)
    raw_changes = record.get("proposed_changes")
    if isinstance(raw_changes, str):
        try:
            record["proposed_changes"] = json.loads(raw_changes)
        except json.JSONDecodeError:
            pass
    return record


def propose_org_change(
    title: str,
    proposal_type: str,
    scope: str,
    rationale: str,
    proposed_changes: Any,
    author: str | None = None,
) -> dict[str, Any]:
    """Agent entry point to propose organizational changes.

    Proposing is always on by default, but suppressed when toggled off via control_flags.
    Parked immediately in the approval queue; applying requires human approval.
    """
    if not is_self_organization_enabled():
        raise SelfOrganizationDisabledError(
            "self-organization proposals are currently disabled via control_flags"
        )

    verified_identity = config.verified_agent_identity()
    if verified_identity:
        if (
            author
            and author != verified_identity
            and config.actor_kind_for_identity(verified_identity) != "human"
        ):
            raise ToolError(
                f"caller {verified_identity!r} cannot submit proposals as author {author!r}"
            )
        actor = author or verified_identity
    else:
        actor = (author or "").strip()
        if not actor:
            raise ToolError("author is required when no verified identity is present")

    clean_title = (title or "").strip()
    if not clean_title:
        raise ToolError("proposal title must not be empty")

    clean_rationale = (rationale or "").strip()
    if not clean_rationale:
        raise ToolError("proposal rationale must not be empty")

    clean_type = (proposal_type or "").strip().lower()
    if clean_type not in VALID_PROPOSAL_TYPES:
        raise ToolError(
            f"invalid proposal_type {proposal_type!r}; must be one of {sorted(VALID_PROPOSAL_TYPES)}"
        )

    # Validate scope-as-membership
    validate_proposal_scope(actor, scope)
    office_id = org_registry.find_office(scope)["id"]

    # Format proposed changes
    if isinstance(proposed_changes, (dict, list)):
        changes_obj = proposed_changes
        changes_json = json.dumps(proposed_changes, sort_keys=True)
    elif isinstance(proposed_changes, str):
        try:
            changes_obj = json.loads(proposed_changes)
        except json.JSONDecodeError:
            changes_obj = {"raw": proposed_changes}
        changes_json = json.dumps(changes_obj, sort_keys=True)
    else:
        raise ToolError("proposed_changes must be an object, list, or JSON string")

    pid = _proposal_id()
    now = storage.now()
    org_id = edition.tenant_org_id()

    # Park a request in the global approval queue
    canonical_payload = {
        "proposal_id": pid,
        "title": clean_title,
        "proposal_type": clean_type,
        "scope": office_id,
        "author": actor,
        "rationale": clean_rationale,
        "proposed_changes": changes_obj,
    }

    gate = approvals.check_approval_gate(
        APPLY_TOOL,
        canonical_payload,
        requested_by=actor,
        force=True,
        request_only=True,
    )
    approval_id = gate.get("approval_id")

    conn = storage.connect()
    try:
        ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO org_proposals (
              id, title, proposal_type, author, scope, rationale, proposed_changes,
              status, approval_id, created_at, updated_at, org_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?, ?, ?)
            """,
            (
                pid,
                clean_title,
                clean_type,
                actor,
                office_id,
                clean_rationale,
                changes_json,
                approval_id,
                now,
                now,
                org_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        events.record_event_once(
            "org_proposal_created",
            payload={
                "proposal_id": pid,
                "title": clean_title,
                "proposal_type": clean_type,
                "scope": office_id,
                "author": actor,
                "approval_id": approval_id,
            },
            notify=False,
        )
    except Exception:
        pass

    return {
        "id": pid,
        "proposal_id": pid,
        "title": clean_title,
        "proposal_type": clean_type,
        "author": actor,
        "scope": office_id,
        "rationale": clean_rationale,
        "proposed_changes": changes_obj,
        "status": PROPOSED,
        "approval_id": approval_id,
        "created_at": now,
        "updated_at": now,
        "org_id": org_id,
    }


def get_org_proposal(proposal_id: str, org_id: str | None = None) -> dict[str, Any] | None:
    """Retrieve one org proposal by id."""
    clean_id = (proposal_id or "").strip()
    if not clean_id:
        return None

    tenant = org_id if org_id is not None else edition.tenant_org_id()
    conn = storage.connect()
    try:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM org_proposals WHERE id = ? AND org_id IS ?",
            (clean_id, tenant),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None
    return _public_proposal(row)


def list_org_proposals(
    status: str | None = None,
    scope: str | None = None,
    author: str | None = None,
    limit: int = 50,
    org_id: str | None = None,
) -> dict[str, Any]:
    """List org proposals with optional filters."""
    capped = max(1, min(int(limit), 200))
    query = "SELECT * FROM org_proposals"
    clauses: list[str] = []
    params: list[Any] = []

    tenant = org_id if org_id is not None else edition.tenant_org_id()
    clauses.append("org_id IS ?")
    params.append(tenant)

    if status:
        clauses.append("status = ?")
        params.append(status.strip().lower())
    if scope:
        clauses.append("scope = ?")
        params.append(scope.strip().lower())
    if author:
        clauses.append("author = ?")
        params.append(author.strip())

    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(capped)

    conn = storage.connect()
    try:
        ensure_schema(conn)
        rows = conn.execute(query, params).fetchall()
        enabled = is_self_organization_enabled(conn)
    finally:
        conn.close()

    return {
        "proposals": [_public_proposal(r) for r in rows],
        "count": len(rows),
        "self_organization_enabled": enabled,
    }


def review_org_proposal(
    proposal_id: str,
    reviewed_by: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Mark a proposal as reviewed. Must be currently in 'proposed' state."""
    clean_id = (proposal_id or "").strip()
    proposal = get_org_proposal(clean_id)
    if proposal is None:
        raise ToolError(f"no org proposal with id {clean_id!r}")

    if proposal["status"] != PROPOSED:
        raise ToolError(
            f"proposal {clean_id!r} is in state {proposal['status']!r}; cannot mark as reviewed"
        )

    actor = reviewed_by or config.verified_agent_identity() or _current_agent_label() or "reviewer"
    now = storage.now()
    conn = storage.connect()
    try:
        cursor = conn.execute(
            """
            UPDATE org_proposals
            SET status = 'reviewed', reviewed_at = ?, reviewed_by = ?, review_notes = ?, updated_at = ?
            WHERE id = ? AND status = 'proposed'
            """,
            (now, actor, notes, now, clean_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            current = get_org_proposal(clean_id)
            if not current:
                raise ToolError(f"no org proposal with id {clean_id!r}")
            raise ToolError(
                f"cannot review proposal {clean_id!r} because its status is {current['status']!r}"
            )
    finally:
        conn.close()

    try:
        events.record_event_once(
            "org_proposal_reviewed",
            payload={"proposal_id": clean_id, "reviewed_by": actor, "notes": notes},
            notify=False,
        )
    except Exception:
        pass

    return {
        "ok": True,
        "proposal_id": clean_id,
        "status": REVIEWED,
        "reviewed_by": actor,
        "reviewed_at": now,
    }


def apply_org_proposal(
    proposal_id: str,
    applied_by: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Apply an approved org proposal.

    APPLYING IS NEVER AUTOMATIC. Agents cannot self-apply. A human approval gate
    must have approved this proposal before it can be applied.
    """
    clean_id = (proposal_id or "").strip()
    proposal = get_org_proposal(clean_id)
    if proposal is None:
        raise ToolError(f"no org proposal with id {clean_id!r}")

    current_status = proposal["status"]
    if current_status == APPLIED:
        raise ToolError(f"proposal {clean_id!r} has already been applied")
    if current_status == REJECTED:
        raise ToolError(f"proposal {clean_id!r} was rejected and cannot be applied")

    verified_identity = config.verified_agent_identity()
    actor = applied_by or verified_identity or _current_agent_label()
    if not actor:
        raise ToolError("applied_by is required when no verified identity is present")

    if verified_identity and config.actor_kind_for_identity(verified_identity) != "human":
        if applied_by and applied_by != verified_identity:
            raise ToolError(f"caller {verified_identity!r} cannot apply proposal as {applied_by!r}")

    # Separation of duties: proposing agent cannot apply their own proposal (including aliases)
    if _same_identity(proposal["author"], actor):
        raise ToolError(
            f"author {proposal['author']!r} cannot self-apply proposal {clean_id!r} as {actor!r} (separation of duties)"
        )

    # If the proposal has an associated approval row, check its status and human provenance
    approval_id = proposal.get("approval_id")
    resolved_human: str | None = None
    if approval_id is not None:
        app_row = approvals.get_approval(int(approval_id))
        if app_row is None:
            raise ToolError(f"no approval request found for proposal {clean_id!r}")
        app_status = app_row.get("status")
        if app_status == "denied":
            raise ToolError(
                f"approval for proposal {clean_id!r} was denied: {app_row.get('reason') or 'no reason given'}"
            )
        if app_status != "approved":
            raise ToolError(
                f"proposal {clean_id!r} is {app_status!r}, not 'approved'; "
                "a human must resolve it with approval_resolve first"
            )
        resolved_by = app_row.get("resolved_by")
        if not resolved_by or config.actor_kind_for_identity(resolved_by) != "human":
            raise ToolError(
                f"approval was resolved by {resolved_by!r}, which is not a human; human gate required"
            )
        resolved_human = resolved_by
    else:
        # No approval_id recorded; actor attempting to apply must be a verified human
        if config.actor_kind_for_identity(actor) != "human":
            raise ToolError("only human operators may apply org proposals without an approval pass")
        if verified_identity and config.actor_kind_for_identity(verified_identity) != "human":
            raise ToolError(
                f"authenticated caller {verified_identity!r} is not human; human gate required"
            )
        resolved_human = actor

    # Consume the one-shot approval gate
    canonical_payload = {
        "proposal_id": proposal["id"],
        "title": proposal["title"],
        "proposal_type": proposal["proposal_type"],
        "scope": proposal["scope"],
        "author": proposal["author"],
        "rationale": proposal["rationale"],
        "proposed_changes": proposal["proposed_changes"],
    }

    # Consume the one-shot approval gate. The pass names the proposal's author,
    # and separation of duties guarantees the applier is a different person, so
    # this is the explicit on-behalf-of spend (#2492): without it the gate would
    # look for a pass belonging to the applier and park a second request forever.
    approvals.check_approval_gate(
        APPLY_TOOL,
        canonical_payload,
        requested_by=proposal["author"],
        force=True,
        request_only=False,
        on_behalf_of=proposal["author"],
    )

    now = storage.now()
    final_applied_by = resolved_human or actor
    conn = storage.connect()
    try:
        cursor = conn.execute(
            """
            UPDATE org_proposals
            SET status = 'applied', applied_at = ?, applied_by = ?, resolution_reason = ?, updated_at = ?
            WHERE id = ? AND status IN ('proposed', 'reviewed')
            """,
            (now, final_applied_by, reason, now, clean_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            current = get_org_proposal(clean_id)
            if not current:
                raise ToolError(f"no org proposal with id {clean_id!r}")
            raise ToolError(
                f"cannot apply proposal {clean_id!r} because its status is already {current['status']!r}"
            )
    finally:
        conn.close()

    try:
        events.record_event_once(
            "org_proposal_applied",
            payload={
                "proposal_id": clean_id,
                "applied_by": final_applied_by,
                "approval_id": approval_id,
            },
            notify=False,
        )
    except Exception:
        pass

    return {
        "ok": True,
        "proposal_id": clean_id,
        "status": APPLIED,
        "applied_by": final_applied_by,
        "applied_at": now,
    }


def reject_org_proposal(
    proposal_id: str,
    rejected_by: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Reject an org proposal."""
    clean_id = (proposal_id or "").strip()
    proposal = get_org_proposal(clean_id)
    if proposal is None:
        raise ToolError(f"no org proposal with id {clean_id!r}")

    if proposal["status"] in TERMINAL_STATES:
        raise ToolError(
            f"proposal {clean_id!r} is already in terminal state {proposal['status']!r}"
        )

    actor = rejected_by or config.verified_agent_identity() or _current_agent_label() or "reviewer"
    now = storage.now()

    # If there is an approval row, resolve it (deny if pending, revoke if approved)
    approval_id = proposal.get("approval_id")
    if approval_id is not None:
        try:
            app_row = approvals.get_approval(int(approval_id))
            if app_row:
                st = app_row.get("status")
                if st == "pending":
                    approvals.approval_resolve(
                        int(approval_id),
                        "deny",
                        reason=reason or "proposal rejected",
                        resolved_by=actor,
                    )
                elif st == "approved":
                    approvals.approval_resolve(
                        int(approval_id),
                        "revoke",
                        reason=reason or "proposal rejected",
                        resolved_by=actor,
                    )
        except Exception:
            pass

    conn = storage.connect()
    try:
        cursor = conn.execute(
            """
            UPDATE org_proposals
            SET status = 'rejected', rejected_at = ?, rejected_by = ?, resolution_reason = ?, updated_at = ?
            WHERE id = ? AND status IN ('proposed', 'reviewed')
            """,
            (now, actor, reason, now, clean_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            current = get_org_proposal(clean_id)
            if not current:
                raise ToolError(f"no org proposal with id {clean_id!r}")
            raise ToolError(
                f"cannot reject proposal {clean_id!r} because its status is already {current['status']!r}"
            )
    finally:
        conn.close()

    try:
        events.record_event_once(
            "org_proposal_rejected",
            payload={"proposal_id": clean_id, "rejected_by": actor, "reason": reason},
            notify=False,
        )
    except Exception:
        pass

    return {
        "ok": True,
        "proposal_id": clean_id,
        "status": REJECTED,
        "rejected_by": actor,
        "rejected_at": now,
        "reason": reason,
    }


def org_proposal(action: str, params: dict | None = None) -> dict[str, Any]:
    """MCP entry point for self-organization proposals.

    Actions:
      - propose: propose organizational change (roles, lanes, routing, delegation, routines)
      - list: list proposals with optional status, scope, or author filters
      - get: retrieve one proposal by proposal_id
      - review: mark a proposal as reviewed
      - apply: apply an approved proposal (strictly requires human approval gate)
      - reject: reject a proposal
      - toggle: read or set self-organization toggle in control_flags
    """
    if not isinstance(action, str):
        raise ToolError("action must be a string")
    act = action.strip().lower()
    body = params or {}
    if not isinstance(body, dict):
        raise ToolError("params must be an object")

    if act == "propose":
        verified_identity = config.verified_agent_identity()
        given_author = body.get("author")
        if verified_identity:
            if (
                given_author
                and given_author != verified_identity
                and config.actor_kind_for_identity(verified_identity) != "human"
            ):
                raise ToolError(
                    f"caller {verified_identity!r} cannot submit proposals as author {given_author!r}"
                )
            author = given_author or verified_identity
        else:
            author = given_author or _current_agent_label()

        return propose_org_change(
            title=str(body.get("title") or ""),
            proposal_type=str(body.get("proposal_type") or ""),
            scope=str(body.get("scope") or ""),
            rationale=str(body.get("rationale") or ""),
            proposed_changes=body.get("proposed_changes"),
            author=author,
        )
    if act == "list":
        return list_org_proposals(
            status=body.get("status"),
            scope=body.get("scope"),
            author=body.get("author"),
            limit=int(body.get("limit") or 50),
        )
    if act == "get":
        pid = str(body.get("proposal_id") or "").strip()
        found = get_org_proposal(pid)
        if found is None:
            raise ToolError(f"unknown org proposal: {pid}")
        return {"proposal": found}
    if act == "review":
        return review_org_proposal(
            proposal_id=str(body.get("proposal_id") or ""),
            reviewed_by=body.get("reviewed_by"),
            notes=body.get("notes"),
        )
    if act == "apply":
        verified_identity = config.verified_agent_identity()
        given_applied_by = body.get("applied_by")
        if verified_identity:
            if (
                given_applied_by
                and given_applied_by != verified_identity
                and config.actor_kind_for_identity(verified_identity) != "human"
            ):
                raise ToolError(
                    f"caller {verified_identity!r} cannot apply proposals as {given_applied_by!r}"
                )
            applied_by = given_applied_by or verified_identity
        else:
            applied_by = given_applied_by or _current_agent_label()

        return apply_org_proposal(
            proposal_id=str(body.get("proposal_id") or ""),
            applied_by=applied_by,
            reason=body.get("reason"),
        )
    if act == "reject":
        return reject_org_proposal(
            proposal_id=str(body.get("proposal_id") or ""),
            rejected_by=body.get("rejected_by"),
            reason=body.get("reason"),
        )
    if act == "toggle":
        if "enabled" in body:
            # Only human operators may toggle the self-organization control flag
            verified_identity = config.verified_agent_identity()
            active_label = _current_agent_label()
            if verified_identity:
                if config.actor_kind_for_identity(verified_identity) != "human":
                    raise ToolError(
                        f"caller {verified_identity!r} is not a human operator; toggling self-organization requires human authorization"
                    )
                caller = verified_identity
            elif active_label and config.actor_kind_for_identity(active_label) != "human":
                raise ToolError(
                    f"agent {active_label!r} cannot toggle self-organization; human authorization required"
                )
            else:
                caller = body.get("set_by")
                if not caller or config.actor_kind_for_identity(caller) != "human":
                    raise ToolError(
                        f"caller {caller!r} is not a human operator; toggling self-organization requires human authorization"
                    )
            raw_enabled = body["enabled"]
            if isinstance(raw_enabled, str):
                cleaned = raw_enabled.strip().lower()
                if cleaned in ("false", "0", "off", "no", "disabled"):
                    enabled = False
                elif cleaned in ("true", "1", "on", "yes", "enabled"):
                    enabled = True
                else:
                    raise ToolError(f"invalid boolean value for 'enabled': {raw_enabled!r}")
            elif isinstance(raw_enabled, bool):
                enabled = raw_enabled
            elif isinstance(raw_enabled, (int, float)):
                enabled = bool(raw_enabled)
            else:
                raise ToolError(f"invalid boolean value for 'enabled': {raw_enabled!r}")

            return set_self_organization_toggle(
                enabled=enabled,
                reason=body.get("reason"),
                set_by=caller,
            )
        return {
            "key": CONTROL_FLAG_KEY,
            "enabled": is_self_organization_enabled(),
        }

    raise ToolError(
        f"action must be one of: propose, list, get, review, apply, reject, toggle (got {action!r})"
    )
