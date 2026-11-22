"""MCP tools over the agent registry and session tracker.

These exist alongside the web UI rather than only inside it: the TUI, remote agents and
cron scripts all want to ask "who is alive?" and to declare their own sessions, and
making that reachable only through an HTTP page would have put the answer behind the
one client that cannot be scripted.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, edition, personas, registry, storage


def agent_heartbeat(
    agent: str | None = None,
    kind: str | None = None,
    version: str | None = None,
    host: str | None = None,
    doctor: dict | None = None,
) -> dict:
    """Declare that an agent is alive right now.

    Most agents never need to call this — a hub request or any `record_agent_event`
    call site already produces a heartbeat. It exists for agents whose work is bursty
    (a Claude Code session idles for ten minutes between tool calls) and as the hook
    point for SessionStart/Stop.

    Attribution follows the usual rule: over HTTP the verified identity from the bearer
    token wins, and `agent` is only consulted when there is nothing verified to use.
    Optional `host` records that machine's last-seen. It becomes a Devices row
    when this call includes `doctor` or the machine is a registered ua-node;
    a hostname-only heartbeat is kept for that later snapshot and is not a
    host row by itself. Agent labels are not hosts.
    Optional `doctor` (a system_doctor payload) stores a snapshot for that host —
    missing snapshots stay missing; this never invents unit status.
    Returns {agent, recorded, host_recorded, status, known}.
    """
    label = config.verified_agent_identity() or agent
    if not label:
        raise ToolError("no agent label: pass agent= or call over an authenticated hub connection")
    recorded = registry.touch_agent(
        label, via="heartbeat_tool", kind=kind, version=version, host=host, force=True
    )
    host_recorded = False
    if host:
        from .. import host_registry

        if isinstance(doctor, dict) and doctor.get("checks") is not None:
            host_recorded = (
                host_registry.record_doctor_snapshot(
                    host, doctor, via="heartbeat_tool", version=version
                )
                is not None
            )
        else:
            host_recorded = host_registry.touch_host_seen(
                host, via="heartbeat_tool", version=version
            )
    current = registry.get_agent(label)
    return {
        "agent": label,
        "recorded": recorded,
        "host_recorded": host_recorded,
        "status": current["status"] if current else "unknown",
        "known": bool(current["known"]) if current else config.is_known_agent_label(label),
    }


def _listed_nodes() -> dict[str, dict]:
    """Registered ua-nodes. Empty when the node table is absent."""
    try:
        from .. import node_registry

        nodes = node_registry.list_nodes()
    except Exception:
        return {}
    return nodes if isinstance(nodes, dict) else {}


def _node_bound_agents(nodes: dict[str, dict] | None = None) -> dict[str, str]:
    """Agent label → node name."""
    if nodes is None:
        nodes = _listed_nodes()
    bound: dict[str, str] = {}
    for name, info in nodes.items():
        agent = info.get("agent")
        if agent:
            bound[agent] = name
    return bound


def _apply_node_liveness(
    agent: dict,
    nodes: dict[str, dict],
    node_agents: dict[str, str],
) -> dict:
    """Fill a missing agent heartbeat from the ua-node that is bound to it.

    Does not write an ``agents`` row. An agent that has its own ``last_seen``
    keeps that timestamp.
    """
    node_name = node_agents.get(agent["label"])
    if not node_name or agent.get("last_seen"):
        return agent
    beat = (nodes.get(node_name) or {}).get("last_heartbeat")
    if not beat:
        return agent
    updated = dict(agent)
    updated["host"] = agent.get("host") or node_name
    updated["last_seen"] = beat
    updated["last_seen_via"] = "ua_node"
    # The node liveness rule, so a node-only agent never reads online while
    # routing calls its node offline (task #1997). Status comes from the beat
    # that counts for liveness (task #2196); last_seen keeps the stored one.
    from ..node_registry import node_status

    updated["status"] = node_status(nodes.get(node_name) or {})
    if updated.get("kind") in (None, "unknown"):
        updated["kind"] = "ua-node"
    return updated


def _merge_node_agents(
    agents: list[dict],
    node_agents: dict[str, str],
    nodes: dict[str, dict],
) -> list[dict]:
    """Add node-bound agents that have no registry row yet. Does not write one.

    Liveness for a node-only agent, including a configured label that has never
    heartbeated, comes from ``node_registry.list_nodes()``.
    """
    seen = {agent["label"] for agent in agents}
    merged = [_apply_node_liveness(agent, nodes, node_agents) for agent in agents]
    for label, node_name in sorted(node_agents.items()):
        if label in seen:
            continue
        current = registry.get_agent(label)
        if current is None:
            current = {
                "label": label,
                "kind": "ua-node",
                "host": node_name,
                "known": config.is_fleet_roster_label(label),
                "first_seen": None,
                "last_seen": None,
                "last_seen_via": None,
                "pid": None,
                "version": None,
                "status": "unknown",
                "meta": {},
                "open_sessions": 0,
                "active_tasks": 0,
            }
        current = personas.enrich_agent(current)
        merged.append(_apply_node_liveness(current, nodes, node_agents))
    return merged


def select_fleet_agents(include_configured: bool = True) -> list[dict]:
    """Registry rows that count as fleet agents. Does not rewrite history."""
    nodes = _listed_nodes()
    node_agents = _node_bound_agents(nodes)
    agents = [
        agent
        for agent in registry.list_agents(include_configured=include_configured)
        if config.is_fleet_roster_label(agent["label"]) or agent["label"] in node_agents
    ]
    return _merge_node_agents(agents, node_agents, nodes)


def agent_list(include_configured: bool = True) -> dict:
    """Fleet agents, with derived liveness.

    A row requires a roster label (``config.is_fleet_roster_label``: the closed
    ``UA_AGENT_LABELS`` set, or a ``FLEET_AGENT_LABELS`` catalog identity when
    that set is unset) or a registered ua-node. Attribution strings stay in the
    ``agents`` table and are excluded from ``agents`` and ``counts``. This does
    not rewrite historical events.

    `status` is computed from last_seen at read time (online/idle/stale/offline), never
    stored — a stored status would keep reading "online" for a process that died.
    Set include_configured=False to hide labels from UA_AGENT_LABELS that have never
    been seen.

    Returns {agents: [...], counts: {status: n}, unregistered: [...]}.
    """
    agents = select_fleet_agents(include_configured=include_configured)
    counts: dict[str, int] = {}
    for agent in agents:
        counts[agent["status"]] = counts.get(agent["status"], 0) + 1
    return {
        "agents": agents,
        "counts": counts,
        # Labels active in the last week that nobody put in UA_AGENT_LABELS. Config
        # drift you can see beats config drift you assume isn't there. This list is
        # the drift signal, not a fleet count.
        "unregistered": registry.unregistered_labels(),
    }


_PEER_SESSION_CAP = 10


def agent_context() -> dict:
    """Return the verified caller's identity and the current peer contract.

    This is the compact startup-grounding surface for a single hub: identity and
    profile come from authenticated request context, peers come from the hub's
    registry, and the rules are stable protocol names rather than prompt text.
    It exposes no bearer tokens or peer filesystem paths.

    Each peer lists its ``live_sessions`` (task #1860): open sessions seen within
    ``session_live_minutes``, newest first, each with ``session_id``, ``host``,
    ``branch``, the task it is working when that is known (``task_source`` says
    whether the session or the agent's sole claim supplied it) and
    ``last_seen_at``. Pass a ``session_id`` as ``message_send(to_session=...)``
    to reach that session alone.
    """
    from .. import profiles

    identity = config.verified_agent_identity()
    nodes = _listed_nodes()
    node_agents = _node_bound_agents(nodes)
    # Peers are not fleet counts. On an open roster a well-formed custom
    # identity stays discoverable; attribution prose and humans do not.
    peers = _merge_node_agents(
        [
            agent
            for agent in registry.list_agents(include_configured=True)
            if config.is_peer_agent_label(agent["label"]) or agent["label"] in node_agents
        ],
        node_agents,
        nodes,
    )
    try:
        workspace = str(config.workspace_root())
    except Exception:
        # Identity has an explicit UA_AGENT_WORKSPACES DENY entry — report that
        # plainly rather than letting this read-only status call fail outright.
        workspace = "denied"
    # Live sessions, so a sender can pass one as message_send(to_session=...).
    live_by_agent: dict[str, list[dict]] = {}
    for session in registry.list_live_sessions():
        live_by_agent.setdefault(session["agent"], []).append(session)
    return {
        "identity": identity,
        "profile": profiles.current(),
        "workspace": workspace,
        "session_live_minutes": config.session_live_minutes(),
        "peers": [
            {
                "label": peer["label"],
                "kind": peer["kind"],
                "host": peer["host"],
                "status": peer["status"],
                "known": peer["known"],
                "live_sessions": live_by_agent.get(peer["label"], [])[:_PEER_SESSION_CAP],
            }
            for peer in peers
        ],
        "rules": [
            "identity_is_authenticated_and_session_scoped",
            "task_claim_is_atomic_and_claimed_by_is_authoritative",
            "note_markers_are_descriptive_not_authorization",
            "development_workspaces_are_separate_from_shared_control_state",
            "remote_cutover_requires_human_approval",
        ],
    }


def session_list(
    agent: str | None = None,
    status: str | None = None,
    limit: int = 50,
    live_only: bool = False,
) -> dict:
    """Agent sessions, newest first, joined to the task each was working.

    `status` accepts a literal status or the alias "open" (starting|running|waiting).
    Stale sessions are reaped to 'abandoned' before the read, so a killed process never
    reads back as still running.

    Each row carries `last_seen_at` and `live` (task #1860): open and seen within
    UA_SESSION_LIVE_MINUTES (default 30). `live_only=True` returns only those
    (it implies status "open"), the list to pick a `message_send(to_session=...)`
    target from.

    Returns {sessions: [...], open: n, live: n, total: n}.
    """
    sessions = registry.list_sessions(
        agent=agent, status="open" if live_only else status, limit=limit, live_only=live_only
    )
    open_count = sum(1 for s in sessions if s["status"] in ("starting", "running", "waiting"))
    return {
        "sessions": sessions,
        "open": open_count,
        "live": sum(1 for s in sessions if s["live"]),
        "total": len(sessions),
    }


def session_start(
    kind: str,
    agent: str | None = None,
    task_id: int | None = None,
    worktree_path: str | None = None,
    branch: str | None = None,
    external_session_id: str | None = None,
    transcript_path: str | None = None,
    meta: dict[str, Any] | None = None,
    include_packet: bool = False,
    recall_prior_packet: bool = False,
    host: str | None = None,
) -> dict:
    """Open a session row and return its id.

    Idempotent on ``(agent, external_session_id)`` (task #1860): registering the
    same external id again returns the same stable ``session_id`` with
    ``created=False``, refreshes ``last_seen_at``, reopens a row the reaper had
    marked abandoned, and fills in any task, branch, worktree or host it now
    knows. Call it again as a heartbeat. Without an ``external_session_id`` every
    call opens a new row. A verified caller can register only under its own
    label: naming another ``agent`` is refused. ``host`` is where the agent runs
    (defaults to the host the label last heartbeated from).

    Call this when an agent begins a discrete unit of work it wants to be visible
    while running — not for every tool call. Pair with session_end; anything left open
    without a heartbeat is reaped as 'abandoned' after
    UA_SESSION_ABANDON_MINUTES (default 30).

    ``include_packet=True`` attaches a bounded resume packet derived from existing
    task/delegation/approval/event/memory state, scoped to the session just opened.
    The packet is historical context, never a transcript and never instruction
    authority.

    ``recall_prior_packet=True`` is the resume path (task #1121 phase 2): when
    ``task_id`` is given, it looks up the most recent *other* session already
    recorded against that task and attaches that prior session's packet under
    ``recalled_packet`` (plus ``recalled_from_session_id``), so a resuming agent
    can recall what a previous session on the same task did without re-reading a
    transcript. ``recalled_packet`` is ``None`` when no prior session exists for
    the task. Independent of ``include_packet``, which is about the new session's
    own packet, not a prior one's.
    """
    verified = config.verified_agent_identity()
    named = (agent or "").strip() or None
    if verified and named and named != verified:
        raise ToolError(
            f"a verified caller registers sessions only under its own label: "
            f"agent {named!r} is not {verified!r}"
        )
    label = verified or named
    if not label:
        raise ToolError("no agent label: pass agent= or call over an authenticated hub connection")
    if task_id is not None and not _task_visible(int(task_id)):
        # Another tenant's task reads as missing: no session anchored to it and
        # no prior-session recall through it (task #1985).
        raise ToolError(f"no task with id {int(task_id)}")
    registered = registry.register_session(
        label,
        kind,
        task_id=task_id,
        worktree_path=worktree_path,
        branch=branch,
        external_session_id=external_session_id,
        transcript_path=transcript_path,
        host=host,
        meta=meta,
    )
    if registered is None:
        raise ToolError("could not open session row")
    session_id, created = registered
    if created:
        from ..lifecycle.capture import record_lifecycle_observation

        record_lifecycle_observation(
            kind="session_start",
            session_id=session_id,
            task_id=task_id,
            agent=label,
            payload={
                "kind": kind,
                "branch": branch,
                "worktree_present": bool(worktree_path),
            },
            idempotency_key=f"session_start:{session_id}",
        )
    row = registry.get_session(session_id) or {}
    result: dict[str, Any] = {
        "session_id": session_id,
        "agent": label,
        "kind": row.get("kind", kind),
        "created": created,
        "host": row.get("host"),
        "last_seen_at": row.get("last_seen_at"),
    }
    if include_packet:
        from ..lifecycle.packet import build_session_packet

        result["packet"] = build_session_packet(task_id=task_id, session_id=session_id, agent=label)
    if recall_prior_packet and task_id is not None:
        prior = registry.most_recent_prior_session(task_id, exclude_session_id=session_id)
        if prior is None:
            result["recalled_packet"] = None
            result["recalled_from_session_id"] = None
        else:
            from ..lifecycle.packet import build_session_packet

            result["recalled_packet"] = build_session_packet(
                task_id=task_id, session_id=prior["id"], agent=label
            )
            result["recalled_from_session_id"] = prior["id"]
    return result


def _task_visible(task_id: int) -> bool:
    """False only for a tenanted caller and a task outside its tenant (or none).

    An untenanted hub keeps its old behaviour; a tenanted caller cannot tell a
    missing task from another tenant's.
    """
    from .. import edition

    if edition.tenant_org_id() is None:
        return True
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE id=? AND org_id IS ?", (task_id, edition.tenant_org_id())
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def session_end(
    session_id: str,
    status: str = "completed",
    exit_status: str | None = None,
    summary: str | None = None,
) -> dict:
    """Close a session. `status` is completed | failed | abandoned.

    The result's `owed` reports what the session leaves behind: replies it took
    on, next moves stored against its agent, and claims with no action path once
    the lease stops. Report only (task #2389): it never refuses the close.

    Another tenant's session is refused exactly like a missing one and is left
    unchanged (task #1995).
    """
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT id, agent, task_id FROM agent_sessions WHERE id=? AND org_id IS ?",
            (session_id, edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ToolError(f"no such session: {session_id}")

    verified = config.verified_agent_identity()
    if verified is not None and row["agent"] != verified:
        raise ToolError(
            f"session {session_id} is owned by {row['agent']!r}; verified caller is {verified!r}"
        )

    closed = registry.end_session(
        session_id, status=status, exit_status=exit_status, summary=summary
    )
    if not closed:
        raise ToolError(f"no such session: {session_id}")
    from ..lifecycle.capture import record_lifecycle_observation

    record_lifecycle_observation(
        kind="session_end",
        session_id=session_id,
        task_id=row["task_id"],
        agent=verified or row["agent"],
        payload={"status": status, "exit_status": exit_status, "summary": summary},
        idempotency_key=f"session_end:{session_id}:{status}",
    )
    from .learning import maybe_propose_completed_session

    learning = maybe_propose_completed_session(session_id, status)
    result: dict[str, Any] = {"session_id": session_id, "status": status, "closed": True}
    from .. import session_owed

    owed = session_owed.report_session(session_id)
    if owed is not None:
        result["owed"] = owed
    if learning is not None:
        proposal = {
            "path": learning.get("path"),
            "written": learning.get("written"),
            "applied": False,
        }
        if learning.get("skipped"):
            proposal["skipped"] = learning["skipped"]
        result["learning_proposal"] = proposal

    from ..session_friction import maybe_nudge_session_learning

    nudge = maybe_nudge_session_learning(session_id, status)
    if nudge is not None:
        result["friction_nudge"] = nudge
    return result


def session_operator_end(
    session_id: str,
    operator: str,
    reason: str,
    status: str = "abandoned",
) -> dict:
    """End another identity's session as the verified human operator (task #2480).

    The platform writer behind ``session.operator_end``; the Control Center calls it
    through the governed write adapter (``webui.api._registered_platform_write``),
    which runs the object profile, agent-access, policy and approval gates first.
    ``session_end`` keeps its ownership rule: this writer never borrows or clears
    the verified identity. It requires that identity to be ``UA_OPERATOR_LABEL`` and
    ``operator`` to name it, ends the row under the caller's tenant fence, and
    appends a ``session.operator_ended`` evidence event attributed to the operator
    with the session's owner and the reason. An already-ended session is a conflict.
    """
    from ..tool_errors import ConflictError

    verified = config.verified_agent_identity()
    if not verified or verified != config.operator_label() or operator != verified:
        raise ToolError("only the verified human operator may end another identity's session")
    if config.actor_kind_for_identity(verified) != "human":
        raise ToolError("only the verified human operator may end another identity's session")
    note = reason.strip() if isinstance(reason, str) else ""
    if not note or len(note) > 2000:
        raise ToolError("reason must be a non-empty string of at most 2000 characters")
    if status not in ("completed", "failed", "abandoned"):
        raise ToolError("status must be completed, failed or abandoned")
    from .. import events

    # One write transaction for the tenant-fenced read, the conditional end and
    # the ledger append (review #1516): an append failure rolls the end back, and
    # of two concurrent ends exactly one wins; the other reads the ended row.
    publish_payload = None
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, agent, task_id, status, ended_at FROM agent_sessions "
            "WHERE id=? AND org_id IS ?",
            (session_id, edition.tenant_org_id()),
        ).fetchone()
        if row is None:
            raise ToolError(f"no such session: {session_id}")
        if row["ended_at"]:
            raise ConflictError(f"session {session_id} already ended ({row['status']})")
        ts = storage.now()
        cur = conn.execute(
            "UPDATE agent_sessions SET status=?, ended_at=?, last_heartbeat_at=?, "
            "exit_status=?, summary=?, operator_ended_by=? "
            "WHERE id=? AND org_id IS ? AND ended_at IS NULL",
            (
                status,
                ts,
                ts,
                f"{registry.OPERATOR_END_EXIT_PREFIX}:{verified}",
                f"ended by operator {verified}: {note}",
                verified,
                session_id,
                edition.tenant_org_id(),
            ),
        )
        if cur.rowcount != 1:
            raise ConflictError(f"session {session_id} already ended")
        ulid, inserted, publish_payload = events.record_event_once_on_connection(
            conn,
            "session.operator_ended",
            payload={
                "session_id": session_id,
                "owner": row["agent"],
                "task_id": row["task_id"],
                "status": status,
                "reason": note,
                "operator": verified,
                "ended_at": ts,
            },
            originator=verified,
            dedupe_key=f"session.operator_ended:{session_id}:{ts}",
        )
        if not inserted:
            raise ConflictError(f"session {session_id} was already ended by the operator")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    if publish_payload is not None:
        events.publish(publish_payload)
    from ..lifecycle.capture import record_lifecycle_observation

    record_lifecycle_observation(
        kind="session_end",
        session_id=session_id,
        task_id=row["task_id"],
        agent=row["agent"],
        payload={"status": status, "exit_status": "operator_end", "summary": note},
        idempotency_key=f"session_end:{session_id}:{status}",
    )
    return {
        "session_id": session_id,
        "owner": row["agent"],
        "status": status,
        "ended_by": verified,
        "closed": True,
        "event_ulid": ulid,
        "outstanding": _outstanding_for(row["agent"]),
    }


def _outstanding_for(owner: str) -> dict:
    """What the ended session's owner still holds. Ending a session releases none of it."""
    conn = storage.connect()
    try:
        claimed = conn.execute(
            "SELECT id FROM tasks WHERE claimed_by=? AND status='doing' AND org_id IS ? "
            "ORDER BY id",
            (owner, edition.tenant_org_id()),
        ).fetchall()
        delegations = conn.execute(
            "SELECT d.id FROM delegations d JOIN tasks t ON t.id=d.task_id "
            "WHERE d.to_agent=? AND d.status IN ('pending','in_flight') AND t.org_id IS ? "
            "ORDER BY d.id",
            (owner, edition.tenant_org_id()),
        ).fetchall()
    finally:
        conn.close()
    return {
        "claimed_task_ids": [int(r["id"]) for r in claimed],
        "open_delegation_ids": [int(r["id"]) for r in delegations],
        "note": "ending a session does not cancel runs, release claims or cancel delegations",
    }
