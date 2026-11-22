"""Readers for the platform object model (task #1072).

Where the readers live matters as much as what they do. The three resources that
shipped with task #823 keep their readers inside ``mcp_live_state``, which is the
MCP *adapter* — so those readers are reachable only through MCP. This module is
the other arrangement: a reader belongs to the platform, and an adapter (MCP
today, HTTP/JSON in #1071, the SDK, the Control Center) calls it. Nothing here
imports MCP.

This is the half of the object model that was declared but not served. The
objects here are the ones a buyer's auditor or compliance officer has to be able
to *open*: the one-shot approval gate, an entry of the tamper-evident evidence
chain, a plan, a delegation, an agent, a session, and a memory note. Until
task #1072, all of them were reachable only by asking an agent to call a tool.

Two rules are inherited from the object model:

1. **The declaration validates.** Instance ids are checked against the
   ``id_pattern`` the object type already declares, via
   :meth:`object_model.ObjectType.render`. There is no second id grammar to keep
   in sync, and a bad id becomes the same ``ResourceNotFoundError`` a missing row
   produces — a reader never leaks whether an id was malformed or absent.
2. **The gate is the declared tool's.** Visibility is already derived from
   ``profile_tool`` by the adapter; the reader enforces the same gate so a caller
   that reaches it by another route (a future HTTP surface, an in-process
   caller) is held to the same permission.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError

from . import edition, object_model, profiles, storage

#: Kept byte-identical to ``mcp_live_state._PROFILE_DENIED`` — a test asserts the
#: two match, so the wording cannot drift between the legacy and platform paths.
PROFILE_DENIED_MESSAGE = "resource is not exposed by the active {profile} profile"


def deny(tool_name: str) -> None:
    """Raise when the active profile denies the tool that gates this object.

    Mirrors ``mcp_live_state._deny``. Deliberately duplicated rather than
    imported: importing the adapter from the platform layer would invert the
    dependency this module exists to establish, and the parity is pinned by test.
    """
    if not profiles.tool_is_allowed(tool_name):
        raise ResourceError(PROFILE_DENIED_MESSAGE.format(profile=profiles.current()))


def _uri_for(kind: str, object_id: Any, label: str) -> str:
    """Render the instance URI, or raise not-found for an id the model rejects."""
    try:
        return object_model.by_kind(kind).render(object_id)
    except ValueError as exc:
        raise ResourceNotFoundError(f"unknown {label}: {object_id!r}") from exc


def read_approval(approval_id: str) -> dict[str, Any]:
    """One approval row: the one-shot human gate and how it was resolved.

    Same profile permission as ``approval_pending``. A missing, non-numeric, or
    out-of-range id is not-found.

    Returns ``{uri, approval}`` where ``approval`` is the stored row: ``tool``,
    ``payload``, ``status``, ``requested_by``, ``resolved_by``, ``reason``,
    ``created_at``, ``updated_at``, ``resolved_at``, and ``run_id`` (the
    caller's current run at request time, or null; display linkage only).

    Note the payload is **decoded to a dict**, because this reuses the domain's
    own accessor ``tools.approvals.get_approval``, which documents exactly that
    ("payload decoded") and is what the elicitation adapter already renders from.
    That is deliberately different from :func:`read_evidence_event`, which returns
    its payload column verbatim: an approval is a live governance object with an
    established representation, whereas an evidence entry is an audit artifact
    whose bytes must survive the read untouched.
    """
    deny("approval_pending")
    uri = _uri_for("approval", approval_id, "approval")
    try:
        numeric_id = int(str(approval_id))
    except (TypeError, ValueError) as exc:  # pragma: no cover - render already rejects
        raise ResourceNotFoundError(f"unknown approval: {approval_id!r}") from exc

    from .tools.approvals import get_approval

    row = get_approval(numeric_id)
    if row is None:
        raise ResourceNotFoundError(f"unknown approval: {numeric_id}")
    return {"uri": uri, "approval": row}


def read_approvals(approval_ids) -> dict[str, dict[str, Any]]:
    """Many approval rows in one read: ``{id: {uri, approval}}`` (task #1786).

    The batch form of :func:`read_approval` for list views. The same gate runs
    once (it does not depend on the id), each id is validated by the same
    declared ``id_pattern``, and the rows come from
    ``tools.approvals.get_approvals``, which decodes exactly as ``get_approval``
    does but on one connection. An id :func:`read_approval` would report as
    not-found (malformed, out of range, or absent) is simply missing from the
    result, keyed by the id string the caller passed.
    """
    deny("approval_pending")
    wanted: dict[int, list[tuple[str, str]]] = {}
    for approval_id in approval_ids:
        key = str(approval_id)
        try:
            uri = _uri_for("approval", approval_id, "approval")
            numeric_id = int(key)
        except (ResourceNotFoundError, TypeError, ValueError):
            continue
        wanted.setdefault(numeric_id, []).append((key, uri))
    if not wanted:
        return {}

    from .tools.approvals import get_approvals

    rows = get_approvals(wanted)
    out: dict[str, dict[str, Any]] = {}
    for numeric_id, keys in wanted.items():
        row = rows.get(numeric_id)
        if row is None:
            continue
        for key, uri in keys:
            # A fresh dict per key, as one read_approval call per id would give.
            out[key] = {"uri": uri, "approval": dict(row)}
    return out


def read_evidence_event(event_ulid: str) -> dict[str, Any]:
    """One entry of the tamper-evident evidence chain, by ULID.

    Same profile permission as ``event_log_query``.

    Returns ``{uri, evidence_event}`` with the stored columns plus
    ``payload_parsed``: the ``payload`` column stays exactly as written (an audit
    record is not rewritten on read), and the decoded form is offered alongside
    it for consumers that want the structure. ``payload_parsed`` is ``None`` when
    the column does not hold JSON. ``prev_hash`` and ``entry_hash`` are the chain
    links — a verifier needs both to check an entry against its neighbours.
    """
    deny("event_log_query")
    uri = _uri_for("evidence_event", event_ulid, "evidence event")
    ulid = str(event_ulid)

    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM event_log WHERE ulid=?", (ulid,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ResourceNotFoundError(f"unknown evidence event: {ulid}")

    record = {key: row[key] for key in row.keys()}
    try:
        record["payload_parsed"] = json.loads(record.get("payload") or "null")
    except (TypeError, ValueError):
        record["payload_parsed"] = None
    return {"uri": uri, "evidence_event": record}


def read_plan(plan_id: str) -> dict[str, Any]:
    """One plan row: goal, scope, status, and escalation state.

    Same profile permission as ``plan_get``. A missing id is not-found.

    Returns ``{uri, plan}`` where ``plan`` carries the stored columns plus the
    same derived ``needs_human`` flag ``plan_get`` exposes (a bool alias of
    ``escalated``), so a client reads a plan exactly as the domain represents
    it.
    """
    deny("plan_get")
    uri = _uri_for("plan", plan_id, "plan")
    plan_id_text = str(plan_id)

    conn = storage.connect()
    try:
        # Another tenant's plan is not-found, like an unknown id (#2042).
        row = conn.execute(
            "SELECT * FROM plans WHERE id=? AND org_id IS ?",
            (plan_id_text, edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ResourceNotFoundError(f"unknown plan: {plan_id_text}")
    record = {key: row[key] for key in row.keys()}
    record["needs_human"] = bool(record.get("escalated"))
    return {"uri": uri, "plan": record}


def read_delegation(delegation_id: str) -> dict[str, Any]:
    """One delegation row: an agent-to-agent handoff and its result.

    Same profile permission as ``delegate_list``. A missing, non-numeric, or
    out-of-range id is not-found.

    Returns ``{uri, delegation}`` where ``delegation`` is the stored row:
    ``from_agent``, ``to_agent``, ``task_id``, ``task_spec``, ``deadline``,
    ``callback``, ``status``, ``memory_id``, ``callback_result``,
    ``attempts``, ``last_attempt_at``, ``created_at``, ``updated_at``,
    ``completed_at``.
    """
    deny("delegate_list")
    uri = _uri_for("delegation", delegation_id, "delegation")
    try:
        numeric_id = int(str(delegation_id))
    except (TypeError, ValueError) as exc:  # pragma: no cover - render already rejects
        raise ResourceNotFoundError(f"unknown delegation: {delegation_id!r}") from exc

    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM delegations WHERE id=?", (numeric_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ResourceNotFoundError(f"unknown delegation: {numeric_id}")
    return {"uri": uri, "delegation": {key: row[key] for key in row.keys()}}


def read_agent(agent_label: str) -> dict[str, Any]:
    """One registered agent and its derived liveness and display persona.

    Same profile permission as ``agent_list``. Status is computed at read time
    from ``last_seen`` by the registry — never a stored column — and ``meta``
    is the parsed JSON object. Personas are purely presentational display metadata
    (display_name, avatar, color/colour, bio, model_routing_preference).
    A missing label is not-found.

    Returns ``{uri, agent}`` with the registry's single-agent shape.
    """
    deny("agent_list")
    uri = _uri_for("agent", agent_label, "agent")

    from .registry import get_agent

    agent = get_agent(str(agent_label))
    if agent is None:
        raise ResourceNotFoundError(f"unknown agent: {agent_label!r}")
    return {"uri": uri, "agent": agent}


def read_persona(agent_label: str) -> dict[str, Any]:
    """Display persona for an agent label.

    Same profile permission as ``agent_list``. Purely presentational display
    metadata (avatar, color, display_name, bio, model_routing_preference).
    """
    deny("agent_list")
    from . import personas

    return {
        "label": str(agent_label),
        "persona": personas.resolve_effective_persona(str(agent_label)),
    }


def read_session(session_id: str) -> dict[str, Any]:
    """One agent session and its outcome.

    Same profile permission as ``session_list``. Duration and heartbeat age are
    computed at read time, and the joined task's title/status ride along
    exactly as ``session_list`` shows them. A missing id is not-found.

    Returns ``{uri, session}`` with the registry's session shape (the same
    fields ``list_sessions`` emits).
    """
    deny("session_list")
    uri = _uri_for("session", session_id, "session")

    from .registry import get_session

    session = get_session(str(session_id))
    if session is None:
        raise ResourceNotFoundError(f"unknown session: {session_id!r}")
    return {"uri": uri, "session": session}


def read_memory_note(memory_id: str) -> dict[str, Any]:
    """One shared-memory note.

    Same profile permission as ``memory_get``. Returns whatever is stored: the
    ``tags`` column is a JSON-encoded string and stays a string here — the
    reader never rewrites an object on read (contrast ``memory_get``, which
    decodes tags for the calling agent). A missing id is not-found.

    Returns ``{uri, memory_note}`` with the stored row.
    """
    deny("memory_get")
    uri = _uri_for("memory_note", memory_id, "memory note")
    note_id = str(memory_id)

    conn = storage.connect()
    try:
        from . import edition

        # Tenant-scoped (task #1762): another org's note reads as not-found.
        row = conn.execute(
            "SELECT * FROM memory WHERE id=? AND org_id IS ?",
            (note_id, edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ResourceNotFoundError(f"unknown memory note: {note_id}")
    return {"uri": uri, "memory_note": {key: row[key] for key in row.keys()}}


def read_agent_token(token_id: str) -> dict[str, Any]:
    """One agent-bearer-token lifecycle row, by row id (task #1198).

    Same profile permission as ``agent_token_list``. Returns the stored
    metadata row verbatim: ``token_hash`` is the SHA-256 digest
    ``managed_secret_set`` computed at issuance, never a value that can be
    turned back into the raw token. There is no column in ``agent_tokens``
    capable of holding a plaintext value, so this reader cannot leak one by
    construction — same guarantee as the writers in
    ``tools/token_lifecycle.py``. A missing id is not-found.

    Returns ``{uri, agent_token}``.
    """
    deny("agent_token_list")
    uri = _uri_for("agent_token", token_id, "agent token")
    try:
        numeric_id = int(str(token_id))
    except (TypeError, ValueError) as exc:  # pragma: no cover - render already rejects
        raise ResourceNotFoundError(f"unknown agent token: {token_id!r}") from exc

    conn = storage.connect()
    try:
        from . import edition

        # Tenant-scoped (task #1343): another org's row reads as not-found.
        row = conn.execute(
            "SELECT * FROM agent_tokens WHERE id=? AND org_id IS ?",
            (numeric_id, edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ResourceNotFoundError(f"unknown agent token: {numeric_id}")
    return {"uri": uri, "agent_token": {key: row[key] for key in row.keys()}}


def read_work_product(work_product_id: str) -> dict[str, Any]:
    """One work-product row: submit -> named-reviewer approval -> release (task #1278).

    Same profile permission as ``work_product_submit`` (the tool is pruned
    from the default developer profile, per Epic B; steward and
    chief-of-staff keep it, matching the agent-token pattern). A missing,
    non-numeric, or out-of-range id is not-found.

    Returns ``{uri, work_product}`` where ``work_product`` is the stored row
    with ``tags`` decoded to a dict, reusing the domain's own accessor
    (``tools.work_product.get_work_product``) rather than a second row shape.
    """
    deny("work_product_submit")
    uri = _uri_for("work_product", work_product_id, "work product")
    try:
        numeric_id = int(str(work_product_id))
    except (TypeError, ValueError) as exc:  # pragma: no cover - render already rejects
        raise ResourceNotFoundError(f"unknown work product: {work_product_id!r}") from exc

    from .tools.work_product import get_work_product

    record = get_work_product(numeric_id)
    if record is None:
        raise ResourceNotFoundError(f"unknown work product: {numeric_id}")
    return {"uri": uri, "work_product": record}


def read_org_unit(org_unit_id: str) -> dict[str, Any]:
    """Read one checked-in office charter under the task-list profile gate."""
    deny("task_list")
    uri = _uri_for("org_unit", org_unit_id, "org unit")
    from . import org_registry

    office = next((row for row in org_registry.load_registry() if row["id"] == org_unit_id), None)
    if office is None:
        raise ResourceNotFoundError(f"unknown org unit: {org_unit_id!r}")
    return {"uri": uri, "org_unit": office}


def read_goal(goal_id: str) -> dict[str, Any]:
    """One goal plus its why-ancestry. Same profile gate as ``task_list``."""
    deny("task_list")
    uri = _uri_for("goal", goal_id, "goal")
    try:
        numeric_id = int(str(goal_id))
    except (TypeError, ValueError) as exc:  # pragma: no cover - render already rejects
        raise ResourceNotFoundError(f"unknown goal: {goal_id!r}") from exc
    from .goals import get_goal

    detail = get_goal(numeric_id)
    if detail is None:
        raise ResourceNotFoundError(f"unknown goal: {numeric_id}")
    return {"uri": uri, **detail}


def read_comment(comment_id: str) -> dict[str, Any]:
    """One append-only task comment. Same profile gate as ``task_list``."""
    deny("task_list")
    uri = _uri_for("comment", comment_id, "comment")
    try:
        numeric_id = int(str(comment_id))
    except (TypeError, ValueError) as exc:  # pragma: no cover - render already rejects
        raise ResourceNotFoundError(f"unknown comment: {comment_id!r}") from exc
    from .task_comments import get_comment

    detail = get_comment(numeric_id)
    if detail is None:
        raise ResourceNotFoundError(f"unknown comment: {numeric_id}")
    grant = profiles.current_session_grant()
    if grant is not None and (
        grant.task_id is None or detail["comment"].get("task_id") != grant.task_id
    ):
        # A session credential reads comments on its own bound task only
        # (task #1744), the same scope as its task_comment tool.
        raise ResourceError(PROFILE_DENIED_MESSAGE.format(profile=profiles.current()))
    return {"uri": uri, **detail}


def read_org_proposal(proposal_id: str) -> dict[str, Any]:
    """One proposed organizational change. Same profile gate as task_list."""
    deny("task_list")
    uri = _uri_for("org_proposal", proposal_id, "org proposal")
    from . import self_organization

    record = self_organization.get_org_proposal(proposal_id)
    if record is None:
        raise ResourceNotFoundError(f"unknown org proposal: {proposal_id!r}")
    return {"uri": uri, "org_proposal": record}
