r"""Agent-authored session handoff notes with a commit point (task #2397).

A session can already be resumed from a derived packet
(``lifecycle.packet.build_session_packet``), but nothing the agent itself knew
at the end of its context survived: what it finished, what comes next, what is
still open. This module stores that note, written by the agent that owns the
session, and makes its delivery safe.

State machine (enforced here and again by SQLite triggers)::

    draft --seal--> sealed --failover--> assigned --> delivered
      |               |          \_______________________^
      +--discard--> discarded
                      +--(a newer note seals)--> superseded

``seal`` is the commit point. Before it, a draft may be edited in place
(``update_draft``) or discarded, and ``recover`` never returns it. After it,
the note is immutable and only moves forward. Nothing returns to ``draft``.

Three facts about a handed-over note are kept apart, because they are not the
same event and only the last two mean it arrived:

* **assigned**: ``failover`` chose a receiver and recorded it (``assigned_to``).
  Nothing says the receiver has the note. The default delivery only records,
  and a transport may answer ``delivered: false``; either leaves the note
  ``assigned``, which is still pending and may be failed over again.
* **delivered**: the transport accepted the note (``deliver`` returned
  ``delivered: true``), or the receiver acknowledged it. Terminal for failover.
* **acknowledged**: the receiver called ``acknowledge`` (``acknowledged_at``).

A pending note (sealed or assigned) becomes ``superseded`` when a newer note
for the same session seals.

Limits, per session: one pending note at a time (a draft, or a sealed or
assigned note not yet delivered; a new note while one is pending must say
``supersede=True``), a 5-minute cooldown between proposals, and at most 5
proposals in any rolling hour. Edits to a draft are not proposals.

``failover`` hands a pending note to another eligible agent, for example when
the owner's provider quota is exhausted. The chooser and the delivery are
injectable; the default chooser picks a live member of the owner's office from
the org registry, and the default delivery only records (hub messaging is a
different lane). One ``agent.lifecycle.handoff`` event is written in the same
transaction as each state change, so the existing session packet already shows
it as ``prior_handoff``.

Context epochs count compactions (or other context resets) per session, so
packet assembly can tell what was written before the current context began.
``label_packet_sources`` labels packet sections by trust tier for that
assembly to call.

Everything is tenant-fenced on ``org_id`` (``edition.tenant_org_id()``): a
session or note in another tenant reads as not found. Plain functions only;
no MCP tool is registered here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import edition, events, storage
from ..telemetry.redaction import redact_sensitive_content
from .schema import CHANNEL_LIFECYCLE_PREFIX, LifecycleKind

STATES = ("draft", "sealed", "assigned", "delivered", "superseded", "discarded")
#: Committed and not yet delivered: what ``failover`` may hand over and a new seal supersedes.
PENDING_STATES = ("sealed", "assigned")
LIVE_SESSION_STATUSES = ("starting", "running", "waiting")

COOLDOWN = timedelta(minutes=5)
HOURLY_LIMIT = 5
ITEMS_MAX = 20
ITEM_MAX_CHARS = 500
REASON_MAX_CHARS = 64

HANDOFF_CHANNEL = f"{CHANNEL_LIFECYCLE_PREFIX}.{LifecycleKind.HANDOFF.value}"

Clock = Callable[[], datetime]
Picker = Callable[[dict[str, Any]], str | None]
Deliverer = Callable[[dict[str, Any], str], dict[str, Any] | None]

_DDL = """
CREATE TABLE IF NOT EXISTS session_handoff_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  task_id INTEGER,
  done TEXT NOT NULL,
  next_steps TEXT NOT NULL,
  open_questions TEXT NOT NULL DEFAULT '[]',
  state TEXT NOT NULL DEFAULT 'draft'
    CHECK(state IN ('draft','sealed','assigned','delivered','superseded','discarded')),
  epoch INTEGER NOT NULL DEFAULT 0,
  supersedes INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  sealed_at TEXT,
  assigned_to TEXT,
  assigned_at TEXT,
  delivered_to TEXT,
  delivered_at TEXT,
  acknowledged_at TEXT,
  failover_reason TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  org_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_handoff_notes_org_session
  ON session_handoff_notes(org_id, session_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_handoff_notes_one_draft
  ON session_handoff_notes(session_id) WHERE state = 'draft';
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_handoff_notes_one_pending
  ON session_handoff_notes(session_id) WHERE state IN ('sealed', 'assigned');
CREATE TRIGGER IF NOT EXISTS session_handoff_notes_forward_only
BEFORE UPDATE OF state ON session_handoff_notes
WHEN NOT (
  OLD.state = NEW.state
  OR (OLD.state = 'draft' AND NEW.state IN ('sealed', 'discarded'))
  OR (OLD.state = 'sealed' AND NEW.state IN ('assigned', 'delivered', 'superseded'))
  OR (OLD.state = 'assigned' AND NEW.state IN ('delivered', 'superseded'))
)
BEGIN
  SELECT RAISE(ABORT, 'session handoff notes are forward-only');
END;
CREATE TRIGGER IF NOT EXISTS session_handoff_notes_sealed_immutable
BEFORE UPDATE OF session_id, agent, task_id, done, next_steps, open_questions, epoch
ON session_handoff_notes
WHEN OLD.state != 'draft'
BEGIN
  SELECT RAISE(ABORT, 'session handoff notes are forward-only: a sealed note is immutable');
END;
CREATE TABLE IF NOT EXISTS context_epochs (
  session_id TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  org_id TEXT,
  PRIMARY KEY (session_id, epoch)
);
CREATE INDEX IF NOT EXISTS idx_context_epochs_org_session
  ON context_epochs(org_id, session_id, epoch);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the handoff and epoch tables. Idempotent; called from ``storage._migrate()``.

    Self-contained tables with no column edit to an existing table, so not a
    ``SCHEMA_VERSION`` bump.
    """
    conn.executescript(_DDL)


# --- helpers --------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _stamp(clock: Clock | None) -> tuple[datetime, str]:
    moment = (clock or _utcnow)()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    return moment, moment.isoformat(timespec="seconds")


def _parse(stamp: str) -> datetime:
    moment = datetime.fromisoformat(stamp)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


@contextmanager
def _write() -> Iterator[sqlite3.Connection]:
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _items(value: Any, *, field: str, required: bool) -> list[str]:
    if value is None and not required:
        return []
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ToolError(f"{field} must be a list of strings")
    out = []
    for item in value:
        if not isinstance(item, str):
            raise ToolError(f"{field} must be a list of strings")
        text = item.strip()
        if text:
            out.append(redact_sensitive_content(text, max_len=ITEM_MAX_CHARS))
        if len(out) >= ITEMS_MAX:
            break
    if required and not out:
        raise ToolError(f"{field} needs at least one non-empty item")
    return out


def _reason(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError("reason must be a non-empty string")
    return value.strip()[:REASON_MAX_CHARS]


def _load_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, agent, task_id FROM agent_sessions WHERE id=? AND org_id IS ?",
        (session_id, edition.tenant_org_id()),
    ).fetchone()
    if row is None:
        raise ToolError(f"session {session_id} not found")
    return row


def _owned_session(conn: sqlite3.Connection, session_id: str, agent: str) -> sqlite3.Row:
    row = _load_session(conn, session_id)
    if row["agent"] != agent:
        raise ToolError(
            f"session {session_id} is owned by {row['agent']}; only its owner writes its handoff"
        )
    return row


def _require_task(conn: sqlite3.Connection, task_id: Any) -> int:
    """The task must exist in the caller's tenant; another tenant's reads as not found."""
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        raise ToolError(f"task_id must be an integer, not {task_id!r}") from None
    row = conn.execute(
        "SELECT 1 FROM tasks WHERE id=? AND org_id IS ?", (tid, edition.tenant_org_id())
    ).fetchone()
    if row is None:
        raise ToolError(f"task {tid} not found: a handoff note can only reference a task here")
    return tid


def _row_to_note(row: sqlite3.Row) -> dict[str, Any]:
    note = {key: row[key] for key in row.keys() if key != "org_id"}
    for key in ("done", "next_steps", "open_questions"):
        note[key] = json.loads(note[key] or "[]")
    return note


def _load_note(conn: sqlite3.Connection, note_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM session_handoff_notes WHERE id=? AND org_id IS ?",
        (int(note_id), edition.tenant_org_id()),
    ).fetchone()
    if row is None:
        raise ToolError(f"handoff note {note_id} not found")
    return _row_to_note(row)


def _owned_note(conn: sqlite3.Connection, note_id: int, agent: str) -> dict[str, Any]:
    note = _load_note(conn, note_id)
    if note["agent"] != agent:
        raise ToolError(f"handoff note {note_id} is owned by {note['agent']}")
    return note


def _require_draft(note: dict[str, Any], action: str) -> None:
    if note["state"] != "draft":
        raise ToolError(
            f"handoff note {note['id']} is {note['state']}: only a draft can be {action}; "
            "a sealed note only moves forward (assigned, delivered or superseded)"
        )


def _epoch(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT epoch, reason, started_at FROM context_epochs "
        "WHERE session_id=? AND org_id IS ? ORDER BY epoch DESC LIMIT 1",
        (session_id, edition.tenant_org_id()),
    ).fetchone()


def _check_limits(conn: sqlite3.Connection, session_id: str, moment: datetime) -> None:
    rows = conn.execute(
        "SELECT created_at FROM session_handoff_notes WHERE session_id=? AND org_id IS ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (session_id, edition.tenant_org_id(), HOURLY_LIMIT),
    ).fetchall()
    stamps = [_parse(row["created_at"]) for row in rows]
    if stamps and moment - stamps[0] < COOLDOWN:
        wait = int((COOLDOWN - (moment - stamps[0])).total_seconds()) + 1
        raise ToolError(
            f"handoff cooldown: session {session_id} proposed a note less than 5 minutes ago; "
            f"retry in {wait}s, or edit the existing draft with update_draft"
        )
    recent = [stamp for stamp in stamps if moment - stamp < timedelta(hours=1)]
    if len(recent) >= HOURLY_LIMIT:
        raise ToolError(
            f"handoff limit: session {session_id} reached 5 per hour; "
            "edit the existing draft instead of proposing a new note"
        )


# --- the note -------------------------------------------------------------------


def propose(
    session_id: str,
    *,
    agent: str,
    done: Sequence[str],
    next_steps: Sequence[str],
    open_questions: Sequence[str] | None = None,
    task_id: int | None = None,
    supersede: bool = False,
    now: Clock | None = None,
) -> dict[str, Any]:
    """Persist a draft handoff note for a session the caller owns.

    ``done`` and ``next_steps`` are required and must each carry at least one
    non-empty item. Items are redacted and bounded (``ITEMS_MAX`` items of
    ``ITEM_MAX_CHARS``). ``task_id`` defaults to the session's task.
    """
    done_items = _items(done, field="done", required=True)
    next_items = _items(next_steps, field="next_steps", required=True)
    questions = _items(open_questions, field="open_questions", required=False)
    moment, stamp = _stamp(now)
    tenant = edition.tenant_org_id()
    with _write() as conn:
        session = _owned_session(conn, session_id, agent)
        pending = conn.execute(
            "SELECT id, state FROM session_handoff_notes WHERE session_id=? AND org_id IS ? "
            "AND state IN ('draft','sealed','assigned') ORDER BY state = 'draft' DESC LIMIT 1",
            (session_id, tenant),
        ).fetchone()
        supersedes = None
        if pending is not None and pending["state"] == "draft":
            raise ToolError(
                f"session {session_id} already has a pending draft (note {pending['id']}): "
                "edit it with update_draft, seal it, or discard it"
            )
        if pending is not None:
            if not supersede:
                raise ToolError(
                    f"session {session_id} has {pending['state']} note {pending['id']} pending "
                    "delivery; "
                    "pass supersede=True to write a note that replaces it once sealed"
                )
            supersedes = pending["id"]
        _check_limits(conn, session_id, moment)
        note_task = task_id if task_id is not None else session["task_id"]
        if note_task is not None:
            note_task = _require_task(conn, note_task)
        epoch_row = _epoch(conn, session_id)
        cursor = conn.execute(
            "INSERT INTO session_handoff_notes (session_id, agent, task_id, done, next_steps, "
            "open_questions, state, epoch, supersedes, created_at, updated_at, org_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)",
            (
                session_id,
                agent,
                note_task,
                json.dumps(done_items),
                json.dumps(next_items),
                json.dumps(questions),
                epoch_row["epoch"] if epoch_row else 0,
                supersedes,
                stamp,
                stamp,
                tenant,
            ),
        )
        return _load_note(conn, cursor.lastrowid)


def update_draft(
    note_id: int,
    *,
    agent: str,
    done: Sequence[str] | None = None,
    next_steps: Sequence[str] | None = None,
    open_questions: Sequence[str] | None = None,
    now: Clock | None = None,
) -> dict[str, Any]:
    """Replace fields of a draft in place. Not a new proposal, so not rate-limited."""
    _, stamp = _stamp(now)
    with _write() as conn:
        note = _owned_note(conn, note_id, agent)
        _require_draft(note, "edited")
        fields = {
            "done": _items(done, field="done", required=True) if done is not None else None,
            "next_steps": (
                _items(next_steps, field="next_steps", required=True)
                if next_steps is not None
                else None
            ),
            "open_questions": (
                _items(open_questions, field="open_questions", required=False)
                if open_questions is not None
                else None
            ),
        }
        for column, value in fields.items():
            if value is not None:
                conn.execute(
                    f"UPDATE session_handoff_notes SET {column}=? WHERE id=?",
                    (json.dumps(value), note["id"]),
                )
        conn.execute(
            "UPDATE session_handoff_notes SET updated_at=? WHERE id=?", (stamp, note["id"])
        )
        return _load_note(conn, note["id"])


def discard(note_id: int, *, agent: str, now: Clock | None = None) -> dict[str, Any]:
    """Drop a draft. A sealed note cannot be discarded."""
    _, stamp = _stamp(now)
    with _write() as conn:
        note = _owned_note(conn, note_id, agent)
        _require_draft(note, "discarded")
        conn.execute(
            "UPDATE session_handoff_notes SET state='discarded', updated_at=? WHERE id=?",
            (stamp, note["id"]),
        )
        return _load_note(conn, note["id"])


def seal(note_id: int, *, agent: str, now: Clock | None = None) -> dict[str, Any]:
    """The commit point: freeze the draft and make it what ``recover`` returns.

    A sealed or assigned note already pending for the same session becomes
    ``superseded`` in the same transaction.
    """
    _, stamp = _stamp(now)
    with _write() as conn:
        note = _owned_note(conn, note_id, agent)
        _require_draft(note, "sealed")
        conn.execute(
            "UPDATE session_handoff_notes SET state='superseded', updated_at=? "
            "WHERE session_id=? AND org_id IS ? AND state IN ('sealed','assigned')",
            (stamp, note["session_id"], edition.tenant_org_id()),
        )
        conn.execute(
            "UPDATE session_handoff_notes SET state='sealed', sealed_at=?, updated_at=? WHERE id=?",
            (stamp, stamp, note["id"]),
        )
        return _load_note(conn, note["id"])


def get_note(note_id: int) -> dict[str, Any] | None:
    """One note in the caller's tenant, any state, or ``None``."""
    conn = storage.connect()
    try:
        return _load_note(conn, note_id)
    except ToolError:
        return None
    finally:
        conn.close()


def recover(session_id: str) -> dict[str, Any] | None:
    """The latest committed note to resume from (sealed, assigned or delivered), never a draft."""
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT * FROM session_handoff_notes WHERE session_id=? AND org_id IS ? "
            "AND state IN ('sealed','assigned','delivered') "
            "ORDER BY sealed_at DESC, id DESC LIMIT 1",
            (session_id, edition.tenant_org_id()),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_note(row) if row is not None else None


# --- failover -------------------------------------------------------------------


def _is_live(conn: sqlite3.Connection, agent: str) -> bool:
    placeholders = ",".join("?" for _ in LIVE_SESSION_STATUSES)
    row = conn.execute(
        f"SELECT 1 FROM agent_sessions WHERE agent=? AND org_id IS ? "
        f"AND status IN ({placeholders}) LIMIT 1",
        (agent, edition.tenant_org_id(), *LIVE_SESSION_STATUSES),
    ).fetchone()
    return row is not None


def office_peers(agent: str) -> list[str]:
    """Members of every office the agent belongs to, leads first, charter order."""
    from .. import org_registry

    try:
        offices = org_registry.load_registry()
    except (OSError, ValueError):
        return []
    peers: list[str] = []
    for office in offices:
        if agent not in office["members"]:
            continue
        for label in [office["lead"], *office["members"]]:
            if label != agent and label not in peers:
                peers.append(label)
    return peers


def default_pick(note: dict[str, Any]) -> str | None:
    """First office peer of the note's author with a live session, or ``None``."""
    peers = office_peers(note["agent"])
    if not peers:
        return None
    conn = storage.connect()
    try:
        return next((label for label in peers if _is_live(conn, label)), None)
    finally:
        conn.close()


def record_only(note: dict[str, Any], to_agent: str) -> dict[str, Any]:
    """Default delivery: nothing is sent, so the note is assigned, not delivered."""
    del note, to_agent
    return {"delivered": False, "mode": "record_only"}


def failover(
    note_id: int,
    *,
    reason: str = "quota",
    pick: Picker | None = None,
    deliver: Deliverer | None = None,
    now: Clock | None = None,
) -> dict[str, Any]:
    """Hand a pending note to another eligible agent and record how far it got.

    ``pick(note)`` names the receiver (default ``default_pick``); it may not be
    the note's author. The assignment is claimed first, by a compare-and-set
    committed with its ``assigned`` event; only a call that wins it runs
    ``deliver(note, to_agent)``, so two overlapping failovers never send one
    note twice. If ``deliver`` raises, the note stays ``assigned`` (pending and
    visible) and the call raises, so failover may be retried.

    What ``deliver`` returns decides the new state. Only ``delivered: True``
    means the transport accepted the note: the note becomes ``delivered``.
    Anything else (the default ``record_only``, or ``delivered: False``)
    records the choice and no more: the note becomes ``assigned`` to that
    agent and stays pending, so a later call may hand it to the same agent
    again or to another one. A ``delivered`` note is never failed over again.

    Each state change and its ``agent.lifecycle.handoff`` event commit
    together. Delivery is at-least-once: a crash between ``deliver`` and the
    promotion leaves the note ``assigned``, so a retry may deliver it again.
    """
    why = _reason(reason)
    conn = storage.connect()
    try:
        note = _load_note(conn, note_id)
    finally:
        conn.close()
    if note["state"] == "draft":
        raise ToolError(f"handoff note {note_id} is a draft: seal it before failover")
    if note["state"] == "delivered":
        raise ToolError(f"handoff note {note_id} was already delivered to {note['delivered_to']}")
    if note["state"] not in PENDING_STATES:
        raise ToolError(
            f"handoff note {note_id} is {note['state']}: fail over the session's latest "
            "pending note (recover) instead"
        )
    to_agent = (pick or default_pick)(note)
    if not isinstance(to_agent, str) or not to_agent.strip():
        raise ToolError(
            f"no eligible agent for handoff note {note_id}: no other live member of "
            f"{note['agent']}'s office; pass pick= to name one"
        )
    to_agent = to_agent.strip()
    if to_agent == note["agent"]:
        raise ToolError(f"cannot fail handoff note {note_id} over to its original agent")
    payload = {
        "kind": LifecycleKind.HANDOFF.value,
        "session_id": note["session_id"],
        "task_id": note["task_id"],
        "note_id": note["id"],
        "from_agent": note["agent"],
        "to_agent": to_agent,
        "reason": why,
        "epoch": note["epoch"],
    }
    # 1. Claim: the compare-and-set commits before anything is sent. A caller
    # that loses it sends nothing. ``attempts`` is the version the CAS checks,
    # so a reassignment to the same agent in the same second still conflicts.
    _, stamp = _stamp(now)
    with _write() as conn:
        changed = conn.execute(
            "UPDATE session_handoff_notes SET state='assigned', assigned_to=?, assigned_at=?, "
            "failover_reason=?, attempts=attempts+1, updated_at=? "
            "WHERE id=? AND state=? AND attempts=?",
            (to_agent, stamp, why, stamp, note["id"], note["state"], note["attempts"]),
        ).rowcount
        if changed != 1:
            raise ToolError(f"handoff note {note_id} changed state during failover; re-read it")
        claimed = _load_note(conn, note["id"])
        # One event per assignee: a second assignment is a new fact, a repeated
        # one for the same agent is not.
        event_id, _, publish = events.record_event_once_on_connection(
            conn,
            HANDOFF_CHANNEL,
            payload={**payload, "state": "assigned"},
            originator=note["agent"],
            episode_id=note["session_id"],
            dedupe_key=f"session_handoff:{note['id']}:assigned:{to_agent}",
        )
    if publish is not None:
        events.publish(publish)
    # 2. Send, only after the claim won. A failure leaves the note assigned:
    # visible as pending, with its event, and failover may run again.
    try:
        delivery = (deliver or record_only)(claimed, to_agent) or {}
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller, note stays assigned
        raise ToolError(
            f"delivery to {to_agent} failed: {type(exc).__name__}; handoff note {note_id} "
            "stays assigned and pending, so failover may be retried"
        ) from exc
    state, delivered_to = "assigned", None
    if delivery.get("delivered") is True:
        # 3. Promote, only if nothing moved the note since this call's claim.
        _, stamp = _stamp(now)
        with _write() as conn:
            promoted = conn.execute(
                "UPDATE session_handoff_notes SET state='delivered', delivered_to=?, "
                "delivered_at=?, updated_at=? "
                "WHERE id=? AND state='assigned' AND attempts=?",
                (to_agent, stamp, stamp, note["id"], claimed["attempts"]),
            ).rowcount
            if promoted == 1:
                state, delivered_to = "delivered", to_agent
                event_id, _, publish = events.record_event_once_on_connection(
                    conn,
                    HANDOFF_CHANNEL,
                    payload={**payload, "state": "delivered"},
                    originator=note["agent"],
                    episode_id=note["session_id"],
                    dedupe_key=f"session_handoff:{note['id']}:delivered",
                )
            else:
                # Acknowledged, reassigned or superseded meanwhile: report what is stored.
                current = _load_note(conn, note["id"])
                state, delivered_to = current["state"], current["delivered_to"]
                publish = None
        if publish is not None:
            events.publish(publish)
    return {
        "note_id": note["id"],
        "session_id": note["session_id"],
        "state": state,
        "assigned_to": to_agent,
        "delivered_to": delivered_to,
        "reason": why,
        "delivery": delivery,
        "event_id": event_id,
    }


def acknowledge(note_id: int, *, agent: str, now: Clock | None = None) -> dict[str, Any]:
    """The receiver says it has the note. Only the agent it was handed to may.

    Sets ``acknowledged_at`` once. An ``assigned`` note becomes ``delivered``
    by this call, since the receiver holding it is the delivery. Repeating the
    call returns the note unchanged.
    """
    _, stamp = _stamp(now)
    with _write() as conn:
        note = _load_note(conn, note_id)
        receiver = note["delivered_to"] or note["assigned_to"]
        if note["state"] not in ("assigned", "delivered") or receiver != agent:
            raise ToolError(
                f"handoff note {note_id} is not handed to {agent}: only the agent a note "
                "was assigned or delivered to acknowledges it"
            )
        if note["acknowledged_at"]:
            return note
        conn.execute(
            "UPDATE session_handoff_notes SET state='delivered', delivered_to=?, "
            "delivered_at=COALESCE(delivered_at, ?), acknowledged_at=?, updated_at=? WHERE id=?",
            (agent, stamp, stamp, stamp, note["id"]),
        )
        _, _, publish = events.record_event_once_on_connection(
            conn,
            HANDOFF_CHANNEL,
            payload={
                "kind": LifecycleKind.HANDOFF.value,
                "session_id": note["session_id"],
                "task_id": note["task_id"],
                "note_id": note["id"],
                "from_agent": note["agent"],
                "to_agent": agent,
                "epoch": note["epoch"],
                "state": "acknowledged",
            },
            originator=agent,
            episode_id=note["session_id"],
            dedupe_key=f"session_handoff:{note['id']}:acknowledged",
        )
        acknowledged = _load_note(conn, note["id"])
    if publish is not None:
        events.publish(publish)
    return acknowledged


def failover_session(
    session_id: str,
    *,
    reason: str = "quota",
    pick: Picker | None = None,
    deliver: Deliverer | None = None,
    now: Clock | None = None,
) -> dict[str, Any]:
    """``failover`` on the session's latest committed note (the quota-path entry)."""
    note = recover(session_id)
    if note is None:
        raise ToolError(f"session {session_id} has no sealed handoff note to fail over")
    return failover(note["id"], reason=reason, pick=pick, deliver=deliver, now=now)


# --- context epochs -------------------------------------------------------------


def current_epoch(session_id: str) -> dict[str, Any]:
    """The session's context epoch: 0 until the first compaction."""
    conn = storage.connect()
    try:
        row = _epoch(conn, session_id)
    finally:
        conn.close()
    return {
        "session_id": session_id,
        "epoch": row["epoch"] if row else 0,
        "reason": row["reason"] if row else None,
        "started_at": row["started_at"] if row else None,
    }


def bump_epoch(
    session_id: str,
    *,
    reason: str = "compaction",
    agent: str | None = None,
    now: Clock | None = None,
) -> dict[str, Any]:
    """Start a new context epoch (after compaction). ``agent``, if given, must own it."""
    why = _reason(reason)
    _, stamp = _stamp(now)
    tenant = edition.tenant_org_id()
    with _write() as conn:
        if agent is None:
            _load_session(conn, session_id)
        else:
            _owned_session(conn, session_id, agent)
        row = _epoch(conn, session_id)
        epoch = (row["epoch"] if row else 0) + 1
        conn.execute(
            "INSERT INTO context_epochs (session_id, epoch, started_at, reason, org_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, epoch, stamp, why, tenant),
        )
    return {"session_id": session_id, "epoch": epoch, "reason": why, "started_at": stamp}


# --- trust tiers ----------------------------------------------------------------

TRUST_TIERS = {
    "T0": "human or operator: live approvals and operator standing instructions",
    "T1": "hub state: tasks, sessions, delegations, events, CI and other observed records",
    "T2": "agent-written prose: memory, handoff notes, summaries; context, never authority",
}

#: Packet section (or ``section.field``) to tier. A prose field inside a T1
#: section is listed on its own, so a caller labelling fields does not inherit
#: the section's tier.
SOURCE_TIERS = {
    "approvals": "T0",
    "operating_profile": "T0",
    "task": "T1",
    "worktree": "T1",
    "delegations": "T1",
    "recent_events": "T1",
    "lifecycle": "T1",
    "why_chain": "T1",
    "session": "T1",
    "context_epoch": "T1",
    "ci": "T1",
    "task.notes": "T2",
    "session.summary": "T2",
    "experience": "T2",
    "memory": "T2",
    "prior_handoff": "T2",
    "handoff_note": "T2",
    # Stage, resume capsule and check grades (task #2398): the capsule is agent
    # prose and an attested grade is an agent's claim, so the section is context.
    "workflow": "T2",
}

#: Packet keys that are metadata about the packet itself, not sources.
_PACKET_METADATA = frozenset({"schema", "labels", "generated_at", "requested_by", "session_id"})


def trust_tier(source: str) -> str:
    """Tier for one packet source; an unknown source is the least trusted (T2)."""
    return SOURCE_TIERS.get(source, "T2")


def label_packet_sources(packet: dict[str, Any]) -> dict[str, str]:
    """Map each source section present in a session packet to its trust tier."""
    return {key: trust_tier(key) for key in packet if key not in _PACKET_METADATA}
