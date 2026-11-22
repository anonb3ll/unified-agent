"""Resume and adopt harness sessions on a ua-node (task #1741, I11).

A harness session outlives the process that ran it. The hub keeps a durable
``agent_sessions`` row with the harness's own session id
(``external_session_id``) and a resume binding in ``meta`` (node, backend,
cwd, and for ACP the ``resume_capability`` the agent advertised at
initialize). Reopening it is a new node item that carries
``resume_session_id``:

ACP
    ``session/resume`` when the agent advertised
    ``sessionCapabilities.resume``, else ``session/load`` when it advertised
    ``loadSession``. An agent with neither cannot be resumed: the Sessions pane
    offers Start again (a fresh session) and says so.
Native CLI
    ``claude -p --resume <id>`` and ``codex exec resume <id>``, the flags the
    installed CLIs document. Used for sessions adopted from discovery.

Adopt takes over a native Claude Code or Codex session that was started
outside the hub. The node lists those sessions (metadata only, inside its
worktree roots, see ``native_sessions``); adopting one creates its
``agent_sessions`` row, idempotent on the harness and session id, attributed
to the request's verified identity, and queues a resume through the same
approval gate as any node dispatch. A session already attributed to another
label is refused: adopt never moves a session to a different identity, and a
row whose task another label holds is refused under the claim rules.

Neither path injects input into a running process. A session whose item is
still live takes follow-ups instead (``node_followups``).

The ``agent_sessions`` reads here are not tenant-fenced (task #1995). They are
keyed by a node item id or a harness session id, and nodes and the node queue
carry no tenant, so there is no caller tenant to fence on. Adopted rows are
still stamped with the hub's tenant by ``registry._insert_session``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from . import acp_matrix, native_sessions, node_queue, registry, storage, table_schemas
from .node_registry import (
    NATIVE_HARNESSES,
    NATIVE_SESSION_ID_RE,
    NodeError,
    _load,
    _row,
    list_nodes,
    validate_node_name,
)

#: agent_sessions.kind for an adopted native session, per harness.
ADOPTED_KINDS = {harness: f"{harness}_native" for harness in NATIVE_HARNESSES}
#: Resume modes as the Sessions pane names them.
MODE_LABELS = {
    "acp_resume": "Resume (ACP session/resume)",
    "acp_load": "Resume (ACP session/load)",
    "native_flag": "Resume (harness --resume)",
}
MAX_RESUMED_ITEMS = 20
OPEN_SESSION_STATUSES = ("starting", "running", "waiting")
#: A discovered session active more recently than this may still be open in
#: a terminal; adopting it would put two writers on one session file.
DEFAULT_ADOPT_QUIET_S = native_sessions.DEFAULT_ADOPT_QUIET_S


def adopt_quiet_s() -> float:
    return native_sessions.adopt_quiet_s()


def _quiet_reason(
    last_activity: Any, *, scanned_at: Any = None, now: datetime | None = None
) -> str | None:
    """Why a discovered session is too recently active to adopt, or None."""
    quiet = adopt_quiet_s()
    if quiet <= 0:
        return None
    try:
        seen = datetime.fromisoformat(str(last_activity))
    except (TypeError, ValueError):
        return "Its last activity is unknown, so it may still be open in a terminal"
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    age = ((now or datetime.now(UTC)) - seen).total_seconds()
    as_of = f" (as of the node's scan at {scanned_at})" if scanned_at else ""
    if age < quiet:
        return (
            f"Active {max(0, int(age))} s ago{as_of}, so it may still be open in a terminal; "
            f"adopt it once it has been quiet for {int(quiet)} s"
        )
    return None


def _live_reason(conn: sqlite3.Connection, row: dict[str, Any] | None, sid: str) -> str | None:
    """Why the session is still live somewhere the hub can see, or None.

    Resume and adopt never start a second writer on a live session: an open
    durable row, or a queued/claimed node item that ran under this session id.
    """
    if row is not None and row["status"] in OPEN_SESSION_STATUSES:
        return f"the session is still live (status {row['status']}); send a follow-up instead"
    hit = conn.execute(
        "SELECT id FROM node_dispatch WHERE session_id=? AND status IN ('queued', 'claimed') "
        "LIMIT 1",
        (sid,),
    ).fetchone()
    if hit is not None:
        return f"node item {hit['id']} is still running this session"
    return None


def ensure_schema(conn: sqlite3.Connection) -> None:
    table_schemas.node_dispatch_resume(conn)


def _now() -> str:
    return storage.now()


def _meta(raw: Any) -> dict[str, Any]:
    value = _load(raw) if isinstance(raw, str) else raw
    return value if isinstance(value, dict) else {}


def _binding(conn: sqlite3.Connection, item_id: str) -> dict[str, Any] | None:
    return _row(
        conn.execute("SELECT * FROM node_dispatch_resume WHERE item_id=?", (item_id,)).fetchone()
    )


def _session_row(conn: sqlite3.Connection, external_sid: str) -> dict[str, Any] | None:
    """The newest durable row for a harness session id, of any kind."""
    return _row(
        conn.execute(
            "SELECT * FROM agent_sessions WHERE external_session_id=? "
            "ORDER BY started_at DESC LIMIT 1",
            (external_sid,),
        ).fetchone()
    )


def _open_resume_item(conn: sqlite3.Connection, external_sid: str) -> dict[str, Any] | None:
    """A queued or claimed item that already reopens this session."""
    return _row(
        conn.execute(
            "SELECT d.id, d.created_by, d.status FROM node_dispatch_resume r "
            "JOIN node_dispatch d ON d.id = r.item_id "
            "WHERE r.resume_session_id=? AND d.status IN ('queued', 'claimed') "
            "ORDER BY d.created_at DESC LIMIT 1",
            (external_sid,),
        ).fetchone()
    )


def _node_features(conn: sqlite3.Connection, node: str) -> list[str]:
    row = conn.execute("SELECT facts_json FROM nodes WHERE node=?", (node,)).fetchone()
    facts = _load(row["facts_json"]) if row is not None else None
    features = facts.get("node_features") if isinstance(facts, dict) else None
    return [f for f in features if isinstance(f, str)] if isinstance(features, list) else []


# --- mirror hooks (called from node_queue) ----------------------------------------


def reopen_for_item(
    item: dict[str, Any],
    node: str,
    item_key: str,
    external_sid: str,
    resume_meta: dict[str, Any],
) -> bool:
    """Reopen the durable row when ``item_key`` was queued to resume ``external_sid``.

    Returns True when the row was rebound to this item (the caller then opens
    nothing new). Bound twice: the item's own resume binding must name the
    same session the node reports, so a node cannot reopen an arbitrary row.
    """
    try:
        conn = storage.connect()
        try:
            binding = _binding(conn, item_key)
            if binding is None or binding["resume_session_id"] != external_sid:
                return False
            row = _session_row(conn, external_sid)
            if row is None:
                return False
            meta = _meta(row.get("meta"))
            if meta.get("node") not in (None, node):
                # Another node's session that happens to share the id.
                return False
            resumed = [i for i in meta.get("resumed_items") or [] if isinstance(i, str)]
            meta.update(
                {
                    key: value
                    for key, value in resume_meta.items()
                    if value is not None or key not in meta
                }
            )
            meta.update(
                item_id=item_key,
                node=node,
                acp_session_id=external_sid,
                resumed_items=[*resumed, item_key][-MAX_RESUMED_ITEMS:],
            )
            ts = _now()
            settings = resume_meta.get("session_settings")
            model = (
                settings.get("current_model_id") if isinstance(settings, dict) else row.get("model")
            )
            conn.execute(
                "UPDATE agent_sessions SET status='running', ended_at=NULL, exit_status=NULL, "
                "last_heartbeat_at=?, meta=?, model=? WHERE id=?",
                (ts, json.dumps(meta), model, row["id"]),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def close_native_for_item(item_key: str, node: str, status: str) -> bool:
    """End the adopted row a native resume item ran, from its receipt. Best-effort."""
    try:
        conn = storage.connect()
        try:
            binding = _binding(conn, item_key)
            if binding is None:
                return False
            row = _session_row(conn, binding["resume_session_id"])
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    if row is None or not str(row["kind"]).endswith("_native"):
        return False
    meta = _meta(row.get("meta"))
    if meta.get("item_id") != item_key or meta.get("node") != node:
        return False
    # Found by the item's resume binding, not by a caller: unfenced (task #1995).
    return registry.end_session(
        row["id"],
        status="completed" if status == "finished" else "failed",
        exit_status=status,
        tenant_scoped=False,
    )


def close_ended_adoptions(*, item_id: str | None = None) -> int:
    """End open adopted rows whose bound item ended without running (#1741 review).

    An adopt item cancelled before it started, denied by policy or failed by
    lease expiry posts no receipt, so ``close_native_for_item`` never runs for
    it. Called from the same end-of-item sweep as follow-ups
    (``node_followups.sweep_ended``). The binding stays: the row remains
    resumable. Best-effort; never raises.
    """
    closed = 0
    try:
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT s.id, json_extract(s.meta, '$.item_id') AS bound, d.status "
                "FROM agent_sessions s JOIN node_dispatch d "
                "ON d.id = json_extract(s.meta, '$.item_id') "
                "WHERE s.kind LIKE '%\\_native' ESCAPE '\\' "
                "AND s.status IN ('starting', 'running', 'waiting') "
                f"AND d.status IN ({','.join('?' * len(node_queue.TERMINAL_ITEM_STATUSES))})",
                tuple(node_queue.TERMINAL_ITEM_STATUSES),
            ).fetchall()
            ts = _now()
            for row in rows:
                if item_id is not None and row["bound"] != item_id:
                    continue
                # Conditional on the row still being open and still bound to
                # that item, in the same transaction (#1741 review): a row
                # re-adopted onto a new item meanwhile is left alone.
                cur = conn.execute(
                    "UPDATE agent_sessions SET status=?, ended_at=?, last_heartbeat_at=?, "
                    "exit_status=? WHERE id=? AND status IN ('starting', 'running', 'waiting') "
                    "AND json_extract(meta, '$.item_id')=?",
                    (
                        "completed" if row["status"] == "finished" else "failed",
                        ts,
                        ts,
                        f"item_{row['status']}",
                        row["id"],
                        row["bound"],
                    ),
                )
                closed += cur.rowcount
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    except sqlite3.Error:
        # Rolled back: nothing was closed, whatever the loop counted.
        return 0
    return closed


# --- what the Sessions pane may offer ---------------------------------------------


def _option(
    item: dict[str, Any],
    *,
    features: list[str],
    meta: dict[str, Any] | None,
    busy: bool,
) -> dict[str, Any]:
    def no(reason: str) -> dict[str, Any]:
        return {"available": False, "mode": None, "label": None, "reason": reason}

    if item.get("status") not in node_queue.TERMINAL_ITEM_STATUSES:
        return no("The session is live: send a follow-up instead")
    if item.get("outcome_unknown"):
        return no("Lease expired with an unconfirmed outcome: reconcile first")
    sid = item.get("session_id")
    if not sid:
        return no("No harness session id was recorded for this run")
    if acp_matrix.RESUME_FEATURE not in features:
        return no("This ua-node predates session resume; upgrade it")
    if busy:
        return no("A resume of this session is already queued or running")
    backend = str(item.get("backend") or "")
    if acp_matrix.acp_backend_name(backend) is not None:
        capability = (meta or {}).get("resume_capability")
        if capability == "resume":
            mode = "acp_resume"
        elif capability == "load":
            mode = "acp_load"
        elif capability == "none":
            return no(
                "The agent advertised neither session/resume nor session/load; "
                "Start again opens a fresh session"
            )
        else:
            return no("The agent's resume capability was not recorded for this session")
    elif backend in node_queue.NATIVE_RESUME_BACKENDS:
        mode = "native_flag"
    else:
        return no(f"{backend} runs cannot be resumed; Start again opens a fresh session")
    return {"available": True, "mode": mode, "label": MODE_LABELS[mode], "reason": None}


def annotate_items(items: list[dict[str, Any]]) -> None:
    """Add ``resume`` to each recent item: whether Resume is real, and how."""
    if not items:
        return
    sids = sorted({str(i["session_id"]) for i in items if i.get("session_id")})
    nodes = sorted({str(i["node"]) for i in items})
    conn = storage.connect()
    try:
        metas: dict[str, dict[str, Any]] = {}
        busy: set[str] = set()
        if sids:
            marks = ",".join("?" * len(sids))
            for row in conn.execute(
                "SELECT external_session_id, meta FROM agent_sessions "
                f"WHERE external_session_id IN ({marks}) ORDER BY started_at ASC",
                sids,
            ).fetchall():
                metas[row["external_session_id"]] = _meta(row["meta"])
            for row in conn.execute(
                "SELECT r.resume_session_id FROM node_dispatch_resume r "
                "JOIN node_dispatch d ON d.id = r.item_id "
                f"WHERE r.resume_session_id IN ({marks}) "
                "AND d.status IN ('queued', 'claimed')",
                sids,
            ).fetchall():
                busy.add(row["resume_session_id"])
        features = {node: _node_features(conn, node) for node in nodes}
        resumes = {
            row["item_id"]: row["resume_session_id"]
            for row in conn.execute(
                "SELECT item_id, resume_session_id FROM node_dispatch_resume WHERE item_id IN "
                f"({','.join('?' * len(items))})",
                [i["id"] for i in items],
            ).fetchall()
        }
    finally:
        conn.close()
    for item in items:
        sid = item.get("session_id")
        item["resume_session_id"] = resumes.get(item["id"])
        item["resume"] = _option(
            item,
            features=features.get(str(item["node"]), []),
            meta=metas.get(str(sid)) if sid else None,
            busy=bool(sid) and str(sid) in busy,
        )


def observed_resume_modes() -> dict[tuple[str, str], str]:
    """``(node, backend) -> resume_capability`` from each pair's newest ACP session."""
    out: dict[tuple[str, str], str] = {}
    try:
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT meta FROM agent_sessions WHERE kind='acp' ORDER BY started_at DESC "
                "LIMIT 500"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return out
    for row in rows:
        meta = _meta(row["meta"])
        key = (meta.get("node"), meta.get("backend"))
        mode = meta.get("resume_capability")
        if all(isinstance(k, str) for k in key) and mode in ("resume", "load", "none"):
            out.setdefault(key, mode)  # newest first
    return out


# --- operator actions ---------------------------------------------------------------


def _attribution_check(
    conn: sqlite3.Connection,
    row: dict[str, Any] | None,
    actor: str,
    *,
    also: tuple[str | None, ...] = (),
    operator: bool = False,
) -> None:
    """No identity takeover: another label's session, or its claimed task, is refused.

    ``also`` names further identities the work is attributed to (the identity
    that started a hub item). ``operator`` is the verified human operator on a
    recovery channel (``routes._operator_recovery``), who may act on any
    session; nobody else may.
    """
    if row is None or operator:
        return
    if row["agent"] != actor and actor not in also:
        raise NodeError(
            f"session is attributed to {row['agent']}; only that identity may resume or adopt it",
            status=403,
        )
    if row.get("task_id") is not None:
        task = conn.execute("SELECT claimed_by FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
        holder = task["claimed_by"] if task is not None else None
        if holder and holder != actor:
            raise NodeError(
                f"task #{row['task_id']} is claimed by {holder}; claim it before resuming",
                status=403,
            )


def resume(item_id: Any, *, prompt: Any, actor: str, operator: bool = False) -> dict[str, Any]:
    """Queue a new item that reopens ``item_id``'s harness session (approval-gated)."""
    item_key = str(item_id or "").strip()
    item = node_queue.get_item(item_key)
    if item is None:
        raise NodeError(f"item {item_key!r} not found", status=404)
    receipt = item.get("receipt") or {}
    item["outcome_unknown"] = bool(
        isinstance(receipt, dict)
        and receipt.get("hub_generated")
        and receipt.get("reason") == "lease_expired_outcome_unknown"
    )
    sid = item.get("session_id")
    conn = storage.connect()
    try:
        row = _session_row(conn, sid) if sid else None
        busy = _open_resume_item(conn, sid) if sid else None
        option = _option(
            item,
            features=_node_features(conn, item["node"]),
            meta=_meta(row.get("meta")) if row else None,
            busy=busy is not None,
        )
        if not option["available"]:
            raise NodeError(f"cannot resume {item_key}: {option['reason']}", status=409)
        # Fail closed (#1741 review): only the identity the session is
        # attributed to, the identity that started this hub item (never for an
        # adopted session, which stays with its adopter), or the human operator
        # may continue it.
        native = row is not None and str(row["kind"]).endswith("_native")
        starter = None if native else item.get("created_by")
        if row is None:
            if not operator and actor != starter:
                raise NodeError(
                    f"{item_key} was started by {starter}; only that identity may resume it",
                    status=403,
                )
        else:
            _attribution_check(conn, row, actor, also=(starter,), operator=operator)
        live = _live_reason(conn, row, sid)
        if live is not None:
            raise NodeError(f"cannot resume {item_key}: {live}", status=409)
    finally:
        conn.close()
    original_settings = _meta(row.get("meta")).get("session_settings") if row else None
    original_settings = original_settings if isinstance(original_settings, dict) else {}
    original_model = original_settings.get("current_model_id") or item.get("model")
    source_tokens = set((item.get("settings_source") or "").split(";"))
    model_origin = (
        "operating_profile"
        if "model=operating_profile" in source_tokens
        else "native_default"
        if "model=native_default" in source_tokens or item.get("model") is None
        else "explicit_session"
    )
    result = node_queue.request_dispatch(
        item["node"],
        actor=actor,
        prompt=prompt,
        cwd=item["cwd"],
        backend=item["backend"],
        model=original_model,
        permission_mode=item.get("permission_mode"),
        timeout_s=item.get("timeout_s"),
        task_id=item.get("task_id"),
        hermes_profile=item.get("hermes_profile"),
        expected_profile_agent=item.get("profile_agent"),
        # The original item's planned checkout realpath (#1770/#2011), so a
        # resume cannot launch in a checkout retargeted since.
        cwd_realpath=item.get("cwd_realpath"),
        resume_session_id=sid,
        resume_model_origin=model_origin,
        resume_binding={
            # A session adopted from a terminal can be reopened there again, so
            # the node re-checks it on disk before resuming, as for adopt.
            "source": "resume_native" if native else "resume",
            "source_item_id": item_key,
            "agent_session_id": row["id"] if row else None,
        },
    )
    return {**result, "resume_session_id": sid, "mode": option["mode"]}


def _discovered(node: str, harness: str, sid: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The node's own report of this session (already bounded to its roots)."""
    info = list_nodes().get(node)
    if info is None:
        raise NodeError(f"node {node!r} is not registered", status=404)
    for row in info.get("native_sessions") or []:
        if row["harness"] == harness and row["external_session_id"] == sid:
            return info, row
    raise NodeError(
        f"{harness} session {sid!r} is not among the sessions node {node!r} discovered "
        "under its worktree roots",
        status=404,
    )


def adopt_backends(
    node: str,
    info: dict[str, Any],
    harness: str,
    modes: dict[tuple[str, str], str] | None = None,
) -> tuple[list[str], str | None]:
    """Backends this node can resume ``harness`` through, native flag first.

    ``acp:<harness>`` counts only once an ACP session on this node recorded
    that the agent advertises ``session/resume`` or ``session/load`` (#1741
    review): an unprobed agent would be refused at run time. The second value
    says why an advertised ACP backend is not offered.
    """
    advertised = [b for b in info.get("backends") or [] if isinstance(b, str)]
    out = [harness] if harness in advertised else []
    acp = f"acp:{harness}"
    if acp not in advertised:
        return out, None
    mode = (modes if modes is not None else observed_resume_modes()).get((node, acp))
    if mode in ("resume", "load"):
        return [*out, acp], None
    if mode == "none":
        return out, f"{acp} advertises neither session/resume nor session/load on this node"
    return out, f"{acp} has not recorded a resume capability on this node yet"


def adopt(
    node: Any,
    *,
    harness: Any,
    external_session_id: Any,
    prompt: Any,
    actor: str,
    backend: Any = None,
    timeout_s: Any = None,
) -> dict[str, Any]:
    """Adopt a discovered native session and queue its resume. Idempotent per session."""
    name = validate_node_name(node)
    if harness not in NATIVE_HARNESSES:
        raise NodeError(f"harness must be one of {list(NATIVE_HARNESSES)}")
    if not isinstance(external_session_id, str) or not NATIVE_SESSION_ID_RE.fullmatch(
        external_session_id
    ):
        raise NodeError("external_session_id is malformed")
    sid = external_session_id
    info, found = _discovered(name, harness, sid)
    choices, acp_reason = adopt_backends(name, info, harness)
    if backend is None:
        if not choices:
            raise NodeError(
                acp_reason or f"node {name!r} advertises neither {harness!r} nor 'acp:{harness}'",
                status=409,
            )
        chosen = choices[0]
    elif backend in choices:
        chosen = backend
    elif backend == f"acp:{harness}" and acp_reason:
        raise NodeError(f"cannot adopt over {backend}: {acp_reason}", status=409)
    else:
        raise NodeError(f"backend must be one of {choices or ['(none advertised)']}", status=409)
    if acp_matrix.RESUME_FEATURE not in (info.get("node_features") or []):
        raise NodeError(
            f"node {name!r} runs a ua-node without session resume; upgrade it", status=409
        )
    conn = storage.connect()
    try:
        row = _session_row(conn, sid)
        bound = _meta(row.get("meta")).get("node") if row is not None else None
        if bound not in (None, name):
            # Keyed on (node, harness, session id) (#1741 review): the same id
            # reported by another node is a different session.
            raise NodeError(
                f"this {harness} session id is already bound to node {bound!r}; "
                "it cannot be adopted on another node",
                status=409,
            )
        _attribution_check(conn, row, actor)
        busy = _open_resume_item(conn, sid)
        live = None if busy is not None else _live_reason(conn, row, sid)
    finally:
        conn.close()
    if live is not None:
        raise NodeError(f"cannot adopt: {live}", status=409)
    if row is not None and row["kind"] == "acp":
        # A hub-started ACP session is continued with Resume on its own item;
        # adopting it would merge native metadata into that row.
        raise NodeError(
            "this session was started by the hub over ACP; use Resume on its item",
            status=409,
        )
    if busy is None:
        quiet = _quiet_reason(found.get("last_activity"), scanned_at=found.get("scanned_at"))
        if quiet is not None:
            raise NodeError(f"cannot adopt yet: {quiet}", status=409)
    if busy is not None:
        if busy["created_by"] == actor:
            # A repeated click: the first adopt is already queued or running.
            return {
                "item_id": busy["id"],
                "status": busy["status"],
                "session_id": row["id"] if row else None,
                "adopted": False,
                "duplicate": True,
                "backend": chosen,
            }
        raise NodeError("a resume of this session is already queued or running", status=409)
    result = node_queue.request_dispatch(
        name,
        actor=actor,
        prompt=prompt,
        cwd=found["cwd"],
        backend=chosen,
        timeout_s=timeout_s,
        resume_session_id=sid,
        resume_binding={
            "source": "adopt",
            "agent_session_id": row["id"] if row else None,
        },
    )
    meta = {
        "adopted": True,
        "harness": harness,
        "node": name,
        "cwd": found["cwd"],
        "backend": chosen,
        "item_id": result["item_id"],
        "adopted_by": actor,
        "title": found.get("title"),
    }
    session_id, created = _record_adoption(actor, ADOPTED_KINDS[harness], sid, name, meta)
    if result.get("item_id"):
        _link_binding(result["item_id"], session_id)
    return {
        **result,
        "session_id": session_id,
        "adopted": created,
        "duplicate": not created,
        "backend": chosen,
        "resume_session_id": sid,
    }


def _record_adoption(
    actor: str, kind: str, sid: str, node: str, meta: dict[str, Any]
) -> tuple[str, bool]:
    """Create or reopen the adopted row with its new metadata, in one transaction.

    Idempotent on (agent, external session id). Reopening and rebinding to the
    new item happen together, so the end-of-item sweep can never see the row
    reopened while it still names the previous, finished item (#1741 review).
    """
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT id, meta FROM agent_sessions WHERE agent=? AND external_session_id=? "
                "ORDER BY started_at DESC LIMIT 1",
                (actor, sid),
            ).fetchone()
            if row is not None:
                merged = {**_meta(row["meta"]), **meta}
                conn.execute(
                    "UPDATE agent_sessions SET status='running', ended_at=NULL, "
                    "exit_status=NULL, last_heartbeat_at=?, host=?, meta=? WHERE id=?",
                    (_now(), node, json.dumps(merged), row["id"]),
                )
                session_id, created = row["id"], False
            else:
                session_id = registry._insert_session(  # noqa: SLF001 - same txn
                    conn,
                    agent=actor,
                    kind=kind,
                    task_id=None,
                    delegation_id=None,
                    worktree_path=None,
                    branch=None,
                    external_session_id=sid,
                    transcript_path=None,
                    episode_id=None,
                    pid=None,
                    model=None,
                    host=node,
                    meta=meta,
                )
                created = True
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except sqlite3.Error as exc:
        raise NodeError(f"could not record the adopted session: {exc}", status=500) from exc
    finally:
        conn.close()
    return session_id, created


def _merge_meta(session_id: str, update: dict[str, Any]) -> None:
    conn = storage.connect()
    try:
        row = conn.execute("SELECT meta FROM agent_sessions WHERE id=?", (session_id,)).fetchone()
        meta = _meta(row["meta"]) if row is not None else {}
        meta.update(update)
        conn.execute("UPDATE agent_sessions SET meta=? WHERE id=?", (json.dumps(meta), session_id))
        conn.commit()
    finally:
        conn.close()


def _link_binding(item_id: str, session_id: str) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE node_dispatch_resume SET agent_session_id=? WHERE item_id=?",
            (session_id, item_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_native_sessions(*, actor: str | None = None) -> list[dict[str, Any]]:
    """Every node's discovered native sessions, newest first, with adoption state."""
    rows: list[dict[str, Any]] = []
    modes = observed_resume_modes()
    for node, info in sorted(list_nodes().items()):
        backends = {h: adopt_backends(node, info, h, modes) for h in NATIVE_HARNESSES}
        resume_ready = acp_matrix.RESUME_FEATURE in (info.get("node_features") or [])
        for found in info.get("native_sessions") or []:
            rows.append(
                {
                    "node": node,
                    **found,
                    "backends": backends[found["harness"]][0],
                    "backend_note": backends[found["harness"]][1],
                    "resume_ready": resume_ready,
                }
            )
    if not rows:
        return []
    sids = sorted({r["external_session_id"] for r in rows})
    conn = storage.connect()
    try:
        marks = ",".join("?" * len(sids))
        owned = {
            row["external_session_id"]: _row(row)
            for row in conn.execute(
                "SELECT id, agent, kind, status, external_session_id, meta FROM agent_sessions "
                f"WHERE external_session_id IN ({marks}) ORDER BY started_at ASC",
                sids,
            ).fetchall()
        }
    finally:
        conn.close()
    for row in rows:
        held = owned.get(row["external_session_id"])
        row["adopted"] = (
            {"session_id": held["id"], "agent": held["agent"], "status": held["status"]}
            if held
            else None
        )
        bound = _meta(held.get("meta")).get("node") if held else None
        if not row["resume_ready"]:
            reason = "This ua-node predates session resume; upgrade it"
        elif bound not in (None, row["node"]):
            reason = f"This session id is bound to node {bound}"
        elif not row["backends"]:
            reason = row["backend_note"] or f"The node advertises no {row['harness']} backend"
        elif held and actor is not None and held["agent"] != actor:
            reason = f"Attributed to {held['agent']}; only that identity may adopt it"
        elif held and held["kind"] == "acp":
            reason = "Started by the hub over ACP; use Resume on its item"
        elif held and held["status"] in OPEN_SESSION_STATUSES:
            reason = f"Still live (status {held['status']})"
        else:
            reason = _quiet_reason(row.get("last_activity"), scanned_at=row.get("scanned_at"))
        row["adoptable"] = reason is None
        row["reason"] = reason
    rows.sort(key=lambda r: r.get("last_activity") or "", reverse=True)
    return rows
