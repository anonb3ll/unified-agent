"""Agent registry and session tracking — who is alive, and what are they running.

Before this module an "agent" was a bare label string denormalized across
`agent_events.agent`, `tasks.claimed_by`, `memory.author` and friends, and the only
liveness signal was `monitor.check_anomalies()`'s `silent_agent` rule at two-day
granularity. Pi runs existed solely as JSONL transcripts under `.worktrees/sessions/`
that nothing indexed. This module turns both into queryable state.

Two design rules run through everything here:

1. **Heartbeats never raise.** `touch_agent` sits on hot paths (every authenticated
   hub request, every `record_agent_event`). It follows the same contract as
   `storage.record_agent_event`: bookkeeping layered onto real work must never break
   that work. Every failure is swallowed.

2. **Status is derived, never stored.** A `status` column would be written by the
   process whose death you are trying to detect, so it reads "online" forever exactly
   when it matters. `derive_status()` computes it from `last_seen` at read time.

Sessions carry a tenant (task #1995). ``agent_sessions.org_id`` is stamped from
``edition.tenant_org_id()`` at insert, and every caller-facing session read and
write is fenced on ``org_id IS <tenant>``. Another tenant's session reads exactly
like a missing one. On an untenanted hub the tenant is ``None``, every row is
NULL and ``IS NULL`` matches them all, so nothing changes there.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from . import config, edition, storage

# Per-label monotonic clock of the last write, so a burst of hub requests from one
# agent collapses to one row update per config.heartbeat_min_interval_s(). Process
# local on purpose: it is a write-rate damper, not a correctness mechanism, and the
# hub, executor and TUI each damping independently is fine.
_last_touch: dict[str, float] = {}
_touch_lock = threading.Lock()

_SESSION_OPEN_STATUSES = ("starting", "running", "waiting")


def _hostname() -> str | None:
    try:
        return socket.gethostname()
    except OSError:
        return None


def reset_throttle() -> None:
    """Clear the per-label throttle. For tests that assert write behaviour."""
    with _touch_lock:
        _last_touch.clear()


def _should_touch(label: str, min_interval: float) -> bool:
    """True if `label` is due for a write. Records the attempt when it returns True."""
    if min_interval <= 0:
        return True
    now_mono = time.monotonic()
    with _touch_lock:
        prev = _last_touch.get(label)
        if prev is not None and (now_mono - prev) < min_interval:
            return False
        _last_touch[label] = now_mono
        return True


def touch_agent(
    label: str | None,
    *,
    via: str,
    kind: str | None = None,
    pid: int | None = None,
    host: str | None = None,
    version: str | None = None,
    force: bool = False,
) -> bool:
    """Record that `label` was alive just now. Best-effort; never raises.

    Returns True if a row was written, False if throttled or if the write failed.
    Callers use the return value for tests and metrics only — no control flow in
    the codebase should depend on a heartbeat having landed.

    `via` names the observation channel (hub_request, agent_event, executor_poll,
    heartbeat_tool, backfill) so the UI can show *how* we know an agent is alive;
    "we saw an authenticated hub request" is a far stronger claim than "a row
    mentioning this label was written".
    """
    if not label or not label.strip():
        return False
    label = label.strip()

    if not force and not _should_touch(label, config.heartbeat_min_interval_s()):
        return False

    try:
        known = 1 if config.is_known_agent_label(label) else 0
        ts = storage.now()
        conn = storage.connect()
        try:
            # Single statement, no surrounding transaction: this runs on hot paths
            # against a SQLite file shared by the hub, executor, cron and TUI.
            # COALESCE keeps first_seen at its original value and lets an optional
            # field (kind/pid/host/version) stay put when this caller doesn't know it.
            conn.execute(
                """
                INSERT INTO agents
                  (label, kind, host, known, first_seen, last_seen, last_seen_via,
                   pid, version, created_at, updated_at)
                VALUES (?, COALESCE(?, 'unknown'), ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(label) DO UPDATE SET
                  last_seen = excluded.last_seen,
                  last_seen_via = excluded.last_seen_via,
                  known = excluded.known,
                  first_seen = COALESCE(agents.first_seen, excluded.first_seen),
                  kind = CASE WHEN excluded.kind = 'unknown' THEN agents.kind
                              ELSE excluded.kind END,
                  host = COALESCE(excluded.host, agents.host),
                  pid = COALESCE(excluded.pid, agents.pid),
                  version = COALESCE(excluded.version, agents.version),
                  updated_at = excluded.updated_at
                """,
                (label, kind, host, known, ts, ts, via, pid, version, ts, ts),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        # Same posture as storage.record_agent_event: a missed heartbeat is a
        # cosmetic loss; an exception here would break a real tool call.
        return False


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def derive_status(last_seen: str | None, *, now: datetime | None = None) -> str:
    """Map a last_seen timestamp onto online | idle | stale | offline | unknown.

    Thresholds come from config.agent_status_thresholds(). A NULL last_seen is
    'unknown' rather than 'offline': a configured agent we have never observed is a
    different fact from one that went quiet, and collapsing them hides config drift.
    """
    parsed = _parse_ts(last_seen)
    if parsed is None:
        return "unknown"
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    age = (reference - parsed).total_seconds()
    online_s, idle_s, stale_s = config.agent_status_thresholds()
    if age < 0:
        # Clock skew across machines sharing memory.db. Treat the future as now
        # rather than reporting a nonsense negative age.
        return "online"
    if age <= online_s:
        return "online"
    if age <= idle_s:
        return "idle"
    if age <= stale_s:
        return "stale"
    return "offline"


# The "seen acting" test (task #1337), shared by queue hygiene's live-claimant
# renewal and the liveness sweep (task #1749). Stamps that are not the agent
# acting (doctor sweeps, backfills) never count. Everything else (hub requests,
# tool calls, sessions, and the agent's own task/journal events) does, as long
# as it happened after the act being judged: a claim or a run start stamps
# last_seen itself, so a dead agent's only activity is at that instant and
# never qualifies.
LIVE_AGENT_MINUTES = 60
_NOT_THE_AGENT = frozenset({"system_doctor", "backfill"})
_ACTIVITY_AFTER = timedelta(seconds=60)


def live_agent_window() -> timedelta:
    """``UA_HYGIENE_LIVE_CLAIMANT_MINUTES`` (default 60): how recent "seen" must be.

    A value that is not a positive number falls back to the default.
    """
    try:
        minutes = float(os.environ.get("UA_HYGIENE_LIVE_CLAIMANT_MINUTES", "") or 0)
    except ValueError:
        minutes = 0
    return timedelta(minutes=minutes if minutes > 0 else LIVE_AGENT_MINUTES)


def seen_acting(
    last_seen: str | None,
    last_seen_via: str | None,
    *,
    since: datetime,
    after: datetime | None = None,
) -> bool:
    """True when the agent itself was seen at or after ``since``.

    ``after`` is the instant of the act being judged (a claim, a run start);
    when given, only activity at least a minute later counts.
    """
    if last_seen_via in _NOT_THE_AGENT:
        return False
    seen = _parse_ts(last_seen)
    if seen is None or seen < since:
        return False
    return after is None or seen >= after + _ACTIVITY_AFTER


def _row_to_agent(row: sqlite3.Row, now: datetime) -> dict[str, Any]:
    from . import personas

    agent = {
        "label": row["label"],
        "kind": row["kind"],
        "host": row["host"],
        "known": bool(row["known"]),
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "last_seen_via": row["last_seen_via"],
        "pid": row["pid"],
        "version": row["version"],
        "status": derive_status(row["last_seen"], now=now),
        "meta": _load_json(row["meta"]),
    }
    return personas.enrich_agent(agent)


def _load_json(raw: Any) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def list_agents(include_configured: bool = True) -> list[dict[str, Any]]:
    """Every observed agent, plus configured-but-never-seen labels as 'unknown'.

    Including the never-seen labels is what makes a misconfiguration visible: a label
    in UA_AGENT_LABELS that has produced no activity is exactly the shape of a broken
    deployment, and omitting it would render that as a clean screen.
    """
    from . import personas

    now = datetime.now(UTC)
    agents: list[dict[str, Any]] = []
    seen_labels: set[str] = set()

    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM agents ORDER BY (last_seen IS NULL), last_seen DESC, label"
        ).fetchall()
        open_counts = {
            r["agent"]: r["c"]
            for r in conn.execute(
                "SELECT agent, COUNT(*) AS c FROM agent_sessions "
                f"WHERE status IN ({','.join('?' * len(_SESSION_OPEN_STATUSES))}) "
                "AND org_id IS ? GROUP BY agent",
                (*_SESSION_OPEN_STATUSES, edition.tenant_org_id()),
            ).fetchall()
        }
        task_counts = {
            r["claimed_by"]: r["c"]
            for r in conn.execute(
                "SELECT claimed_by, COUNT(*) AS c FROM tasks "
                "WHERE claimed_by IS NOT NULL AND status = 'doing' GROUP BY claimed_by"
            ).fetchall()
        }
    finally:
        conn.close()

    for row in rows:
        agent = _row_to_agent(row, now)
        agent["open_sessions"] = open_counts.get(row["label"], 0)
        agent["active_tasks"] = task_counts.get(row["label"], 0)
        agents.append(agent)
        seen_labels.add(row["label"])

    if include_configured:
        for label in sorted(config.agent_labels() - seen_labels):
            unseen = {
                "label": label,
                "kind": "unknown",
                "host": None,
                "known": True,
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
            agents.append(personas.enrich_agent(unseen))
    return agents


def get_agent(label: str) -> dict[str, Any] | None:
    now = datetime.now(UTC)
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM agents WHERE label = ?", (label,)).fetchone()
    finally:
        conn.close()
    return _row_to_agent(row, now) if row else None


# --- sessions -------------------------------------------------------------------


def _insert_session(
    conn: sqlite3.Connection,
    *,
    agent: str,
    kind: str,
    task_id: int | None,
    delegation_id: int | None,
    worktree_path: str | None,
    branch: str | None,
    external_session_id: str | None,
    transcript_path: str | None,
    episode_id: str | None,
    pid: int | None,
    model: str | None,
    host: str | None,
    meta: dict | None,
) -> str:
    """Insert one ``agent_sessions`` row on the caller's connection; never commits.

    The row is stamped with the caller's tenant (``edition.tenant_org_id()``,
    task #1995), NULL on an untenanted hub.
    """
    from .events import new_ulid

    session_id = new_ulid()
    ts = storage.now()
    conn.execute(
        """
        INSERT INTO agent_sessions
          (id, agent, kind, status, started_at, last_heartbeat_at, task_id,
           delegation_id, worktree_path, branch, external_session_id,
           transcript_path, host, pid, episode_id, model, meta, org_id)
        VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            agent,
            kind,
            ts,
            ts,
            task_id,
            delegation_id,
            worktree_path,
            branch,
            external_session_id,
            transcript_path,
            host or _hostname(),
            pid if pid is not None else os.getpid(),
            episode_id,
            model,
            json.dumps(meta or {}),
            edition.tenant_org_id(),
        ),
    )
    return session_id


def start_session(
    agent: str,
    kind: str,
    *,
    task_id: int | None = None,
    delegation_id: int | None = None,
    worktree_path: str | None = None,
    branch: str | None = None,
    external_session_id: str | None = None,
    transcript_path: str | None = None,
    episode_id: str | None = None,
    pid: int | None = None,
    model: str | None = None,
    meta: dict | None = None,
) -> str | None:
    """Open a session row and return its id. Best-effort; returns None on failure.

    Callers must treat None as "no session recorded" and carry on — the executor
    must still run the task if the bookkeeping row could not be written.
    Always inserts a new row; ``register_session`` is the idempotent entry point.
    """
    try:
        conn = storage.connect()
        try:
            session_id = _insert_session(
                conn,
                agent=agent,
                kind=kind,
                task_id=task_id,
                delegation_id=delegation_id,
                worktree_path=worktree_path,
                branch=branch,
                external_session_id=external_session_id,
                transcript_path=transcript_path,
                episode_id=episode_id,
                pid=pid,
                model=model,
                host=None,
                meta=meta,
            )
            conn.commit()
        finally:
            conn.close()
        touch_agent(agent, via="session_start", pid=pid, force=True)
        return session_id
    except Exception:
        return None


#: ``exit_status`` prefix of a session the human operator ended (task #2480).
OPERATOR_END_EXIT_PREFIX = "operator_end"


def register_session(
    agent: str,
    kind: str,
    *,
    task_id: int | None = None,
    worktree_path: str | None = None,
    branch: str | None = None,
    external_session_id: str | None = None,
    transcript_path: str | None = None,
    host: str | None = None,
    meta: dict | None = None,
) -> tuple[str, bool] | None:
    """Register a session idempotently; return ``(session_id, created)`` or None.

    With an ``external_session_id`` the pair ``(agent, external_session_id)``
    names one logical session: a second call finds the existing row, refreshes
    its heartbeat, reopens it if it had been reaped or ended, fills in any task,
    branch, worktree, host or transcript it now knows, and returns the same
    ``session_id`` with ``created=False``. Without an external id every call
    opens a new row, as ``start_session`` does. The lookup and the insert share
    one write transaction, so two racing registrations cannot both insert.

    ``host`` is where the agent runs. For a new session, when omitted, it falls back
    to the host the label last heartbeated from, then to this process's hostname.
    A refresh keeps the session's existing host unless one is passed.

    The ``(agent, external_session_id)`` lookup is fenced on the caller's tenant
    (task #1995): another tenant's row with the same pair is never reopened.
    A row the human operator ended (``operator_ended_by`` set, tasks #2480 and
    #2502) is never reopened either: the registration opens a new session.
    """
    ext = (external_session_id or "").strip() or None
    try:
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = None
                if ext is not None:
                    existing = conn.execute(
                        "SELECT id FROM agent_sessions WHERE agent=? AND external_session_id=? "
                        "AND org_id IS ? AND operator_ended_by IS NULL "
                        "ORDER BY started_at DESC LIMIT 1",
                        (agent, ext, edition.tenant_org_id()),
                    ).fetchone()
                if existing is not None:
                    session_id = existing["id"]
                    conn.execute(
                        "UPDATE agent_sessions SET status='running', ended_at=NULL, "
                        "exit_status=NULL, last_heartbeat_at=?, "
                        "task_id=COALESCE(?, task_id), worktree_path=COALESCE(?, worktree_path), "
                        "branch=COALESCE(?, branch), transcript_path=COALESCE(?, transcript_path), "
                        "host=COALESCE(?, host) WHERE id=?",
                        (
                            storage.now(),
                            task_id,
                            worktree_path,
                            branch,
                            transcript_path,
                            host,
                            session_id,
                        ),
                    )
                    created = False
                else:
                    if host is None:
                        row = conn.execute(
                            "SELECT host FROM agents WHERE label=?", (agent,)
                        ).fetchone()
                        host = row["host"] if row is not None else None
                    session_id = _insert_session(
                        conn,
                        agent=agent,
                        kind=kind,
                        task_id=task_id,
                        delegation_id=None,
                        worktree_path=worktree_path,
                        branch=branch,
                        external_session_id=ext,
                        transcript_path=transcript_path,
                        episode_id=None,
                        pid=None,
                        model=None,
                        host=host,
                        meta=meta,
                    )
                    created = True
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()
        touch_agent(agent, via="session_start", force=True)
        return session_id, created
    except Exception:
        return None


def touch_session(
    session_id: str | None, conn: sqlite3.Connection | None = None, *, revive: bool = True
) -> bool:
    """Record that a session is alive now; revive one the reaper marked abandoned.

    Called from ``session_start`` and from mailbox reads that pass a
    ``session_id``. A session the agent ended itself (``completed`` or
    ``failed``), or one the human operator ended (``operator_ended_by`` set,
    tasks #2480 and #2502, even after a later end rewrote its status), stays ended. Pass ``conn`` to reuse the caller's connection (the
    update is committed on it). Another tenant's session is not touched (task
    #1995). Best-effort; never raises.

    ``revive=False`` (task #2547) only refreshes a session that is still open:
    reading an ended session's stranded mail must not resurrect it. Session
    registration still revives, because only the live harness registers.
    """
    if not session_id:
        return False
    open_states = (
        "('starting','running','waiting','abandoned')"
        if revive
        else "('starting','running','waiting')"
    )
    try:
        own = conn is None
        db = storage.connect() if conn is None else conn
        try:
            cur = db.execute(
                "UPDATE agent_sessions SET last_heartbeat_at=?, "
                "status=CASE WHEN status='abandoned' THEN 'running' ELSE status END, "
                "ended_at=CASE WHEN status='abandoned' THEN NULL ELSE ended_at END, "
                "exit_status=CASE WHEN status='abandoned' THEN NULL ELSE exit_status END "
                f"WHERE id=? AND status IN {open_states} "
                "AND operator_ended_by IS NULL AND org_id IS ?",
                (storage.now(), session_id, edition.tenant_org_id()),
            )
            db.commit()
            return cur.rowcount > 0
        finally:
            if own:
                db.close()
    except Exception:
        return False


def most_recent_prior_session(
    task_id: int, *, exclude_session_id: str | None = None
) -> dict[str, Any] | None:
    """Most recent session opened against ``task_id`` before ``exclude_session_id``.

    Used by ``session_start``'s recall path (task #1121 phase 2) to find the
    session a resuming agent should recall a packet from — any status, not
    just completed, since an abandoned or crashed session still carries real
    prior context worth surfacing. Returns ``None`` when no prior session
    exists for the task, or on any storage error (best-effort, never raises).
    Only the caller's tenant's sessions count (task #1995).
    """
    try:
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT id, agent, status, started_at, ended_at FROM agent_sessions "
                "WHERE task_id = ? AND id != ? AND org_id IS ? ORDER BY started_at DESC LIMIT 1",
                (task_id, exclude_session_id or "", edition.tenant_org_id()),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def heartbeat_session(session_id: str | None) -> bool:
    """Push a session's last_heartbeat_at forward so the reaper leaves it alone.

    Not tenant-fenced (task #1995): its only caller is the node lease renewal,
    which heartbeats mirror rows it found by node item id. That path is the
    node control plane, which carries no tenant, not a caller-facing view.
    Caller-driven heartbeats go through ``touch_session``, which is fenced.
    """
    if not session_id:
        return False
    try:
        conn = storage.connect()
        try:
            cur = conn.execute(
                "UPDATE agent_sessions SET last_heartbeat_at = ? "
                f"WHERE id = ? AND status IN ({','.join('?' * len(_SESSION_OPEN_STATUSES))})",
                (storage.now(), session_id, *_SESSION_OPEN_STATUSES),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:
        return False


def end_session(
    session_id: str | None,
    *,
    status: str = "completed",
    exit_status: str | None = None,
    summary: str | None = None,
    tenant_scoped: bool = True,
) -> bool:
    """Close a session. Best-effort; never raises.

    Fenced on the caller's tenant (task #1995): another tenant's session is
    left unchanged and reads as missing (False). ``tenant_scoped=False`` is for
    the node control plane only (ACP turn ends, native resume receipts): it
    closes a row it found by node item or harness session id, and node
    receipts carry no tenant. The executor keeps the fence: it ends the row it
    opened itself, in the same process, under the same tenant.
    """
    if not session_id:
        return False
    if status not in ("completed", "failed", "abandoned"):
        status = "completed"
    try:
        ts = storage.now()
        conn = storage.connect()
        try:
            fence, fence_params = (
                (" AND org_id IS ?", (edition.tenant_org_id(),)) if tenant_scoped else ("", ())
            )
            cur = conn.execute(
                "UPDATE agent_sessions SET status = ?, ended_at = ?, last_heartbeat_at = ?,"
                " exit_status = COALESCE(?, exit_status), summary = COALESCE(?, summary)"
                f" WHERE id = ?{fence}",
                (status, ts, ts, exit_status, summary, session_id, *fence_params),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:
        return False


def reap_abandoned_sessions() -> int:
    """Mark open sessions whose heartbeat went stale as 'abandoned'. Returns the count.

    Without this a killed executor leaves a session reading 'running' forever, which
    is worse than no session record at all — it asserts something false.

    Not tenant-fenced (task #1995): it is hub housekeeping keyed on heartbeat
    age, it returns only a count, and a stale session is abandoned whoever
    owns it.
    """
    cutoff_minutes = config.session_abandon_minutes()
    try:
        conn = storage.connect()
        try:
            cutoff = datetime.now(UTC).timestamp() - cutoff_minutes * 60.0
            cutoff_ts = datetime.fromtimestamp(cutoff, UTC).isoformat(timespec="seconds")
            cur = conn.execute(
                "UPDATE agent_sessions SET status = 'abandoned', ended_at = ?,"
                " exit_status = COALESCE(exit_status, 'no_heartbeat')"
                f" WHERE status IN ({','.join('?' * len(_SESSION_OPEN_STATUSES))})"
                " AND last_heartbeat_at < ?",
                (storage.now(), *_SESSION_OPEN_STATUSES, cutoff_ts),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    except Exception:
        return 0


def list_sessions(
    agent: str | None = None,
    status: str | None = None,
    since: str | None = None,
    limit: int = 100,
    reap: bool = True,
    live_only: bool = False,
) -> list[dict[str, Any]]:
    """Sessions newest-first, joined to their task title.

    Reaps stale sessions first by default so a read never reports a dead run as live.
    ``live_only`` keeps open sessions seen within ``UA_SESSION_LIVE_MINUTES`` and
    applies that in SQL, before ``limit``, so stale newer sessions cannot push a
    live older one out of the page. Only the caller's tenant's sessions are
    listed (task #1995).
    """
    if reap:
        reap_abandoned_sessions()

    limit = max(1, min(int(limit), 500))
    now = datetime.now(UTC)
    clauses: list[str] = ["s.org_id IS ?"]
    params: list[Any] = [edition.tenant_org_id()]
    if agent:
        clauses.append("s.agent = ?")
        params.append(agent)
    if status == "open":
        clauses.append(f"s.status IN ({','.join('?' * len(_SESSION_OPEN_STATUSES))})")
        params.extend(_SESSION_OPEN_STATUSES)
    elif status:
        clauses.append("s.status = ?")
        params.append(status)
    if live_only:
        # Heartbeats are stored in whole seconds and compared as text. Round the one
        # cutoff _is_live also uses UP to a whole second, so a row passes here exactly
        # when it will read live: truncating would admit a row up to a second too old.
        cutoff = _live_cutoff(now)
        if cutoff.microsecond:
            cutoff = cutoff.replace(microsecond=0) + timedelta(seconds=1)
        clauses.append(f"s.status IN ({','.join('?' * len(_SESSION_OPEN_STATUSES))})")
        params.extend(_SESSION_OPEN_STATUSES)
        clauses.append("s.last_heartbeat_at >= ?")
        params.append(cutoff.isoformat(timespec="seconds"))
    if since:
        clauses.append("s.started_at >= ?")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}"

    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT s.*, t.title AS task_title, t.status AS task_status "
            f"FROM agent_sessions s LEFT JOIN tasks t ON t.id = s.task_id AND t.org_id IS s.org_id {where} "
            "ORDER BY s.started_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()

    return [_session_result(row, now) for row in rows]


def _live_cutoff(now: datetime) -> datetime:
    """The oldest heartbeat that still counts as live at ``now``."""
    return now - timedelta(seconds=config.session_live_minutes() * 60.0)


def _is_live(status: str, heartbeat: datetime | None, now: datetime) -> bool:
    """Open and seen within ``UA_SESSION_LIVE_MINUTES``; a stale session is not live."""
    if status not in _SESSION_OPEN_STATUSES or heartbeat is None:
        return False
    return heartbeat >= _live_cutoff(now)


def _session_result(row: sqlite3.Row, now: datetime) -> dict[str, Any]:
    """Keep list and id-based session readers on the same response shape."""
    started = _parse_ts(row["started_at"])
    ended = _parse_ts(row["ended_at"])
    heartbeat = _parse_ts(row["last_heartbeat_at"])
    return {
        "id": row["id"],
        "agent": row["agent"],
        "kind": row["kind"],
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "last_heartbeat_at": row["last_heartbeat_at"],
        # The liveness signal under its messaging name (task #1860): one timestamp,
        # refreshed by session_start, mailbox reads with a session_id and the
        # executor heartbeat, so the reaper and "live" can never disagree.
        "last_seen_at": row["last_heartbeat_at"],
        "live": _is_live(row["status"], heartbeat, now),
        # ``now`` is taken before the read, so a row written during it can be newer:
        # clamp rather than report a negative age.
        "duration_s": max(0.0, ((ended or now) - started).total_seconds()) if started else None,
        "heartbeat_age_s": (
            max(0.0, ((ended or now) - heartbeat).total_seconds()) if heartbeat else None
        ),
        "task_id": row["task_id"],
        "task_title": row["task_title"],
        "task_status": row["task_status"],
        "delegation_id": row["delegation_id"],
        "worktree_path": row["worktree_path"],
        "branch": row["branch"],
        "external_session_id": row["external_session_id"],
        "transcript_path": row["transcript_path"],
        "host": row["host"],
        "pid": row["pid"],
        "exit_status": row["exit_status"],
        "summary": row["summary"],
        "model": row["model"] if "model" in row.keys() else None,
        "meta": _load_json(row["meta"]),
    }


def list_live_sessions(agent: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Live sessions as message targets: open, seen within ``UA_SESSION_LIVE_MINUTES``.

    Compact rows for a sender picking a ``to_session``: ``session_id``, ``agent``,
    ``kind``, ``host``, ``branch``, ``last_seen_at`` and the task the session is
    working. ``task_id`` comes from the session itself (``task_source`` is
    ``session``); when the session names none and its agent holds exactly one
    ``doing`` claim, that claim is shown instead (``task_source`` is ``claim``);
    otherwise the task is ``None``. No filesystem paths or transcripts leave here.
    Newest first.
    """
    rows = list_sessions(agent=agent, limit=limit, live_only=True)
    claims: dict[str, list[sqlite3.Row]] = {}
    if any(s["task_id"] is None for s in rows):
        conn = storage.connect()
        try:
            for r in conn.execute(
                "SELECT id, title, claimed_by FROM tasks "
                "WHERE status='doing' AND claimed_by IS NOT NULL AND org_id IS ?",
                (edition.tenant_org_id(),),
            ).fetchall():
                claims.setdefault(r["claimed_by"], []).append(r)
        finally:
            conn.close()
    out: list[dict[str, Any]] = []
    for s in rows:
        task_id, task_title, source = s["task_id"], s["task_title"], "session"
        if task_id is None:
            sole = claims.get(s["agent"], [])
            if len(sole) == 1:
                task_id, task_title, source = sole[0]["id"], sole[0]["title"], "claim"
            else:
                source = None
        out.append(
            {
                "session_id": s["id"],
                "agent": s["agent"],
                "kind": s["kind"],
                "host": s["host"],
                "branch": s["branch"],
                "task_id": task_id,
                "task_title": task_title,
                "task_source": source if task_id is not None else None,
                "started_at": s["started_at"],
                "last_seen_at": s["last_seen_at"],
                "heartbeat_age_s": s["heartbeat_age_s"],
            }
        )
    return out


def get_session(session_id: str) -> dict[str, Any] | None:
    """Read one session by id, including history older than the list page.

    Another tenant's session reads as missing (task #1995).
    """
    reap_abandoned_sessions()
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT s.*, t.title AS task_title, t.status AS task_status "
            "FROM agent_sessions s LEFT JOIN tasks t ON t.id = s.task_id AND t.org_id IS s.org_id "
            "WHERE s.id = ? AND s.org_id IS ?",
            (session_id, edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    return _session_result(row, datetime.now(UTC)) if row is not None else None


def unregistered_labels(since_days: float = 7.0) -> list[str]:
    """Labels seen recently that are absent from config.agent_labels().

    Surfaces config drift as an observation instead of a theory. Feeds the
    `agent_label_drift` doctor check.
    """
    cutoff = datetime.fromtimestamp(
        datetime.now(UTC).timestamp() - since_days * 86400.0, UTC
    ).isoformat(timespec="seconds")
    try:
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT label FROM agents WHERE known = 0 AND last_seen >= ? ORDER BY label",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [r["label"] for r in rows]
