"""Runs v0 projection (task #1652).

One list of runs for the desktop. A run is recorded in one of three stores:

* ``dispatch_runs`` — approved-dispatch rows (``run-<task>-<hex>`` ids);
* ``event_log`` channel ``agent.run.selfpickup`` — self-pickup claims
  (``sp-<ULID>`` ids), folded by ``run_records``;
* ``agent_sessions`` — node and harness session rows.

Each store keeps its own state vocabulary; the projection reports it verbatim
in ``status`` and adds the uniform fields the Runs pane needs: ``kind``,
``task_id``, ``agent``, ``outcome``, ``started_at``, ``ended_at``,
``lease_expires_at``, ``abandoned`` and ``links`` (platform URIs plus evidence
ledger refs).

Reads never write. A lapsed self-pickup lease reports ``abandoned`` on read —
``run_records``' abandoned-on-read rule — and the same read-time rule flags a
dispatch run whose live state holds a lapsed lease and a session whose
heartbeat went stale (what ``reap_abandoned_sessions`` would persist). The
stored rows are untouched.

Tenant scope: ``event_log``, ``agent_sessions`` and ``tasks`` carry the
nullable ``org_id`` boundary (tasks #1232, #1995); ``dispatch_runs`` has no
``org_id`` column and fences through its mandatory ``task_id`` join. A run in
another tenant is invisible: absent from the list, 404 from the detail route.

Pagination is a stable ``(started_at, run_id)`` descending cursor. Each source
contributes at most ``limit`` rows past the cursor, so a page needs no full
scan of any store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from . import config, dispatch_states, edition, object_model, run_records, storage

CHANNEL_NODE = "agent.node"
CHANNEL_DISPATCH = "agent.dispatch.transition"

KINDS = ("dispatch", "selfpickup", "session")

DEFAULT_LIMIT = 100
MAX_LIMIT = 500

_CURSOR_SEP = "|"

#: Dispatch states whose attempt has ended, for ``ended_at``/``outcome``
#: purposes: the state machine's terminal set plus ``refused`` (denied at
#: admission; outside TERMINAL_RUN_STATES because the slot was never held) and
#: ``retryable`` (the attempt stopped and its lease was cleared; a retry may
#: still reopen the run, so ``status`` reports the state verbatim rather than
#: folding it into a terminal one).
_DISPATCH_ATTEMPT_ENDED = frozenset(dispatch_states.TERMINAL_RUN_STATES | {"refused", "retryable"})

#: Same set the session reaper closes (agent_sessions CHECK constraint); kept in
#: registry as ``_SESSION_OPEN_STATUSES`` — redeclared here so the read mirror
#: cannot drift silently when the schema changes.
_SESSION_OPEN_STATUSES = ("starting", "running", "waiting")


def _parse_ts(value: str | None) -> datetime | None:
    return run_records._parse_ts(value)


def _ref(kind: str, object_id: Any) -> dict[str, Any] | None:
    """A platform object link (``{kind, id, uri}``), or None when it cannot render."""
    if object_id is None:
        return None
    try:
        object_type = object_model.by_kind(kind)
        uri = object_type.render(object_id)
    except (KeyError, TypeError, ValueError):
        return None
    return {"kind": kind, "id": str(object_id), "uri": uri}


def _evidence_links(rows: list[Any]) -> list[dict[str, Any]]:
    return [ref for ulid in rows if (ref := _ref("evidence_event", ulid)) is not None]


# --- dispatch ----------------------------------------------------------------


def _dispatch_row(row: Any, now: str) -> dict[str, Any]:
    state = row["state"]
    lease = row["lease_expires_at"]
    lapsed = state in dispatch_states.LIVE_STATES and lease is not None and lease <= now
    abandoned = state == "expired" or lapsed
    return {
        "run_id": row["run_id"],
        "kind": "dispatch",
        "task_id": row["task_id"],
        "agent": row["agent_label"],
        "status": state,
        "outcome": state if state in _DISPATCH_ATTEMPT_ENDED else None,
        "started_at": row["created_at"],
        "ended_at": row["updated_at"] if state in _DISPATCH_ATTEMPT_ENDED else None,
        "lease_expires_at": lease,
        "abandoned": abandoned,
        "abandon_reason": "expired"
        if state == "expired"
        else ("lease_expired" if lapsed else None),
        "delegation_id": row["delegation_id"],
        "evidence_ref": row["evidence_ref"],
        "failure_reason": row["failure_reason"],
        "failure_class": row["failure_class"],
        "termination_outcome": row["termination_outcome"],
        "links": {},
    }


def _dispatch_query(
    conn: Any,
    org: Any,
    *,
    agent: str | None,
    task_id: int | None,
    after: tuple[str, str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Dispatch rows past ``after`` in ``(created_at, run_id)`` descending order."""
    clauses = ["t.org_id IS ?"]
    params: list[Any] = [org]
    if agent:
        clauses.append("r.agent_label = ?")
        params.append(agent)
    if task_id is not None:
        clauses.append("r.task_id = ?")
        params.append(task_id)
    if after is not None:
        clauses.append("(r.created_at < ? OR (r.created_at = ? AND r.run_id < ?))")
        params.extend((after[0], after[0], after[1]))
    params.append(limit)
    rows = conn.execute(
        "SELECT r.* FROM dispatch_runs r JOIN tasks t ON t.id = r.task_id "
        f"WHERE {' AND '.join(clauses)} ORDER BY r.created_at DESC, r.run_id DESC LIMIT ?",
        params,
    ).fetchall()
    return rows


# --- self-pickup --------------------------------------------------------------


def _selfpickup_query(
    conn: Any,
    org: Any,
    *,
    agent: str | None,
    task_id: int | None,
    after: tuple[str, str] | None,
    limit: int,
    run_id: str | None = None,
) -> list[Any]:
    """Claim-event rows for open and finished self-pickup runs, newest first."""
    clauses = [
        "channel = ?",
        "org_id IS ?",
        "json_extract(payload, '$.kind') = ?",
    ]
    params: list[Any] = [run_records.CHANNEL, org, run_records.KIND_CLAIMED]
    if run_id is not None:
        clauses.append("json_extract(payload, '$.run_id') = ?")
        params.append(run_id)
    if agent:
        clauses.append("json_extract(payload, '$.agent') = ?")
        params.append(agent)
    if task_id is not None:
        clauses.append("json_extract(payload, '$.task_id') = ?")
        params.append(task_id)
    if after is not None:
        clauses.append(
            "(json_extract(payload, '$.claimed_at') < ? "
            "OR (json_extract(payload, '$.claimed_at') = ? "
            "AND json_extract(payload, '$.run_id') < ?))"
        )
        params.extend((after[0], after[0], after[1]))
    params.append(limit)
    return conn.execute(
        f"SELECT payload FROM event_log WHERE {' AND '.join(clauses)} "
        "ORDER BY json_extract(payload, '$.claimed_at') DESC, "
        "json_extract(payload, '$.run_id') DESC LIMIT ?",
        params,
    ).fetchall()


def _tasks_by_id(conn: Any, task_ids: set[int]) -> dict[int, Any]:
    if not task_ids:
        return {}
    ids = sorted(task_ids)
    rows = conn.execute(
        "SELECT id, title, status, claimed_by, claimed_at, lock_expires_at, lane, project, "
        f"current_run_id FROM tasks WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    ).fetchall()
    return {int(r["id"]): r for r in rows}


def _reported_lease(run: dict[str, Any], task: Any, reason: str | None) -> str | None:
    """The lease a run row reports.

    Renewals move ``tasks.lock_expires_at`` without appending a claim event, so
    an open, non-abandoned run that still owns its task reports the live lease.
    Anything else — finished, abandoned, or a run the task moved on from —
    keeps the claim-event snapshot.
    """
    if task is None or run["state"] != "open" or reason is not None:
        return run.get("lock_expires_at")
    current = task["current_run_id"]
    owns = (
        current == run["run_id"] if current is not None else task["claimed_by"] == run.get("agent")
    )
    return task["lock_expires_at"] if owns else run.get("lock_expires_at")


def _selfpickup_rows(conn: Any, claims: list[Any], now: str) -> list[dict[str, Any]]:
    """Fold claim events and apply the abandoned-on-read overlay (no writes)."""
    run_ids = [p["run_id"] for p in (run_records._payload(r) for r in claims) if p.get("run_id")]
    runs = run_records._fold(
        claims,
        run_records._finishes_for(conn, run_ids),
        run_records._cancels_for(conn, run_ids),
        run_records._heartbeat_usages(conn, run_ids),
    )
    tasks = _tasks_by_id(conn, {int(r["task_id"]) for r in runs if r.get("task_id") is not None})
    out = []
    for run in runs:
        task = tasks.get(run["task_id"]) if run.get("task_id") is not None else None
        reason = run_records._abandon_reason(run, task, now) if run["state"] == "open" else None
        out.append(
            {
                "run_id": run["run_id"],
                "kind": "selfpickup",
                "task_id": run.get("task_id"),
                "agent": run.get("agent"),
                "status": "abandoned" if reason else run["state"],
                "outcome": run.get("outcome"),
                "started_at": run.get("claimed_at"),
                "ended_at": run.get("finished_at"),
                "lease_expires_at": _reported_lease(run, task, reason),
                "abandoned": bool(reason) or run.get("outcome") == "abandoned",
                "abandon_reason": reason,
                "claim_source": run.get("claim_source"),
                "to_status": run.get("to_status"),
                "cancel_requested": run.get("cancel_requested"),
                "lane": run.get("lane"),
                "project": run.get("project"),
                "links": {},
            }
        )
    return out


# --- sessions -----------------------------------------------------------------


def _session_stale(row: Any, now: datetime) -> bool:
    """The reaper's own predicate, evaluated on read: open and heartbeat stale."""
    if row["status"] not in _SESSION_OPEN_STATUSES:
        return False
    heartbeat = _parse_ts(row["last_heartbeat_at"])
    if heartbeat is None:
        return False
    cutoff = now - timedelta(minutes=config.session_abandon_minutes())
    return heartbeat < cutoff


def _session_row(row: Any, now: datetime) -> dict[str, Any]:
    status = row["status"]
    stale = _session_stale(row, now)
    return {
        "run_id": row["id"],
        "kind": "session",
        "task_id": row["task_id"],
        "agent": row["agent"],
        "status": "abandoned" if stale else status,
        "outcome": row["exit_status"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "lease_expires_at": None,
        "abandoned": status == "abandoned" or stale,
        "abandon_reason": "stale_heartbeat" if stale else None,
        "session_kind": row["kind"],
        "exit_status": row["exit_status"],
        "last_seen_at": row["last_heartbeat_at"],
        "delegation_id": row["delegation_id"],
        # node_dispatch.session_id joins to this, not to the session's ULID;
        # popped in _attach_links before the row leaves the module.
        "_external_session_id": row["external_session_id"],
        "links": {},
    }


def _session_query(
    conn: Any,
    org: Any,
    *,
    agent: str | None,
    task_id: int | None,
    after: tuple[str, str] | None,
    limit: int,
    session_id: str | None = None,
) -> list[Any]:
    clauses = ["org_id IS ?"]
    params: list[Any] = [org]
    if session_id is not None:
        clauses.append("id = ?")
        params.append(session_id)
    if agent:
        clauses.append("agent = ?")
        params.append(agent)
    if task_id is not None:
        clauses.append("task_id = ?")
        params.append(task_id)
    if after is not None:
        clauses.append("(started_at < ? OR (started_at = ? AND id < ?))")
        params.extend((after[0], after[0], after[1]))
    params.append(limit)
    return conn.execute(
        "SELECT id, agent, kind, status, started_at, ended_at, last_heartbeat_at, "
        "task_id, delegation_id, exit_status, model, host, external_session_id "
        f"FROM agent_sessions WHERE {' AND '.join(clauses)} "
        "ORDER BY started_at DESC, id DESC LIMIT ?",
        params,
    ).fetchall()


# --- evidence links --------------------------------------------------------------


def _events_by_run_id(
    conn: Any, org: Any, channel: str, run_ids: list[str]
) -> dict[str, list[str]]:
    """Event ULIDs per run id on one channel, tenant-fenced."""
    out: dict[str, list[str]] = {}
    if not run_ids:
        return out
    for i in range(0, len(run_ids), 500):
        chunk = run_ids[i : i + 500]
        rows = conn.execute(
            "SELECT ulid, json_extract(payload, '$.run_id') AS rid FROM event_log "
            f"WHERE channel = ? AND org_id IS ? "
            f"AND json_extract(payload, '$.run_id') IN ({','.join('?' * len(chunk))}) "
            "ORDER BY ulid",
            [channel, org, *chunk],
        ).fetchall()
        for row in rows:
            if row["rid"]:
                out.setdefault(row["rid"], []).append(row["ulid"])
    return out


def _attach_links(conn: Any, org: Any, rows: list[dict[str, Any]]) -> None:
    """Fill ``links`` on a page of uniform rows with bounded per-page queries."""
    for row in rows:
        task_id = row.get("task_id")
        if row["kind"] == "session":
            row["links"] = {
                "self": _ref("session", row["run_id"]),
                "task": _ref("task", task_id),
                "dispatch_run": None,
                "evidence": [],
            }
        else:
            row["links"] = {
                "self": _ref("run", row["run_id"]),
                "task": _ref("task", task_id),
                "delegation": _ref("delegation", row.get("delegation_id")),
                "evidence": [],
            }

    dispatch_ids = [r["run_id"] for r in rows if r["kind"] == "dispatch"]
    transitions = _events_by_run_id(conn, org, CHANNEL_DISPATCH, dispatch_ids)
    sp_ids = [r["run_id"] for r in rows if r["kind"] == "selfpickup"]
    sp_events = _events_by_run_id(conn, org, run_records.CHANNEL, sp_ids)

    # node_dispatch.session_id stores the external (e.g. ACP) session id, not
    # the agent_sessions ULID the run row reports as run_id.
    ext_session_ids = [
        r["_external_session_id"]
        for r in rows
        if r["kind"] == "session" and r["_external_session_id"]
    ]
    node_items: dict[str, list[Any]] = {}
    if ext_session_ids:
        for i in range(0, len(ext_session_ids), 500):
            chunk = ext_session_ids[i : i + 500]
            for row in conn.execute(
                "SELECT id, run_id, session_id FROM node_dispatch "
                f"WHERE session_id IN ({','.join('?' * len(chunk))})",
                chunk,
            ).fetchall():
                node_items.setdefault(row["session_id"], []).append(row)
    item_ids = [item["id"] for items in node_items.values() for item in items]
    node_events: dict[str, list[str]] = {}
    if item_ids:
        for i in range(0, len(item_ids), 500):
            chunk = item_ids[i : i + 500]
            for row in conn.execute(
                "SELECT ulid, json_extract(payload, '$.item_id') AS item_id FROM event_log "
                f"WHERE channel = ? AND org_id IS ? "
                f"AND json_extract(payload, '$.item_id') IN ({','.join('?' * len(chunk))}) "
                "ORDER BY ulid",
                [CHANNEL_NODE, org, *chunk],
            ).fetchall():
                if row["item_id"]:
                    node_events.setdefault(row["item_id"], []).append(row["ulid"])

    for row in rows:
        if row["kind"] == "dispatch":
            row["links"]["evidence"] = _evidence_links(transitions.get(row["run_id"], []))
        elif row["kind"] == "selfpickup":
            row["links"]["evidence"] = _evidence_links(sp_events.get(row["run_id"], []))
        else:
            bound_dispatch = None
            ulids: list[str] = []
            for item in node_items.get(row.pop("_external_session_id"), []):
                ulids.extend(node_events.get(item["id"], []))
                if bound_dispatch is None and item["run_id"]:
                    bound_dispatch = item["run_id"]
            row["links"]["dispatch_run"] = _ref("run", bound_dispatch)
            row["links"]["evidence"] = _evidence_links(ulids)


# --- public reads --------------------------------------------------------------


def _parse_cursor(raw: str | None) -> tuple[str, str] | None:
    """Decode ``<started_at>|<run_id>``; a malformed cursor raises ``ValueError``."""
    if raw is None:
        return None
    parts = raw.split(_CURSOR_SEP)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("malformed cursor")
    return parts[0], parts[1]


def _cursor_of(row: dict[str, Any]) -> str:
    return f"{row['started_at']}{_CURSOR_SEP}{row['run_id']}"


def list_runs(
    *,
    kind: str | None = None,
    agent: str | None = None,
    task_id: int | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """One page of uniform run rows, newest first.

    ``cursor`` is the previous page's ``next_cursor``: rows strictly before its
    ``(started_at, run_id)`` pair. ``kind`` narrows to one store
    (``dispatch``|``selfpickup``|``session``); ``agent`` and ``task_id`` filter
    on the row fields. ``truncated`` says another page exists.
    """
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    cap = max(1, min(int(limit), MAX_LIMIT))
    after = _parse_cursor(cursor)
    org = edition.tenant_org_id()
    now = storage.now()
    now_dt = datetime.now(UTC)

    kinds = (kind,) if kind else KINDS
    conn = storage.connect()
    try:
        rows: list[dict[str, Any]] = []
        if "dispatch" in kinds:
            for row in _dispatch_query(
                conn, org, agent=agent, task_id=task_id, after=after, limit=cap + 1
            ):
                rows.append(_dispatch_row(row, now))
        if "selfpickup" in kinds:
            claims = _selfpickup_query(
                conn, org, agent=agent, task_id=task_id, after=after, limit=cap + 1
            )
            rows.extend(_selfpickup_rows(conn, claims, now))
        if "session" in kinds:
            for row in _session_query(
                conn, org, agent=agent, task_id=task_id, after=after, limit=cap + 1
            ):
                rows.append(_session_row(row, now_dt))
        page = sorted(rows, key=lambda r: (r["started_at"] or "", r["run_id"]), reverse=True)[
            : cap + 1
        ]
        truncated = len(page) > cap
        page = page[:cap]
        _attach_links(conn, org, page)
    finally:
        conn.close()
    return {
        "runs": page,
        "count": len(page),
        "truncated": truncated,
        "next_cursor": _cursor_of(page[-1]) if truncated and page else None,
        "limit": cap,
    }


def get_run(run_id: str) -> dict[str, Any] | None:
    """One uniform run row by id, or None when no store holds it for this tenant."""
    rid = (run_id or "").strip()
    if not rid:
        return None
    org = edition.tenant_org_id()
    now = storage.now()
    now_dt = datetime.now(UTC)
    conn = storage.connect()
    try:
        row: dict[str, Any] | None = None
        if rid.startswith(run_records.RUN_ID_PREFIX):
            claims = _selfpickup_query(
                conn, org, agent=None, task_id=None, after=None, limit=1, run_id=rid
            )
            folded = _selfpickup_rows(conn, claims, now)
            row = folded[0] if folded else None
        else:
            found = conn.execute(
                "SELECT r.* FROM dispatch_runs r JOIN tasks t ON t.id = r.task_id "
                "WHERE r.run_id = ? AND t.org_id IS ?",
                (rid, org),
            ).fetchone()
            if found is not None:
                row = _dispatch_row(found, now)
            else:
                found = _session_query(
                    conn, org, agent=None, task_id=None, after=None, limit=1, session_id=rid
                )
                row = _session_row(found[0], now_dt) if found else None
        if row is None:
            return None
        _attach_links(conn, org, [row])
    finally:
        conn.close()
    return row
