"""Swarm Event Bus (task #157): append-only ULID event log + local Unix-socket pub/sub.

Every component in this cluster used to learn about state by polling: the
executor re-queries ``delegate_list`` every 5s, the TUI re-runs six queries per
keypress, ``monitor`` reconstructs activity after the fact. Nothing could be
*told* that something happened. This module is the missing primitive.

Two halves, with deliberately different failure postures:

* **The log** (``record_event`` → the ``event_log`` table) is durable and
  reports its failures. Task #156's policy engine must be able to fail CLOSED on
  "no episode record", which is impossible if a lost append is indistinguishable
  from a successful one. This is why ``record_event`` raises where
  ``storage.record_agent_event`` swallows.
* **The socket** (``EventBusServer`` / ``publish`` / ``subscribe``) is
  best-effort fan-out. A dead, stale, or missing ``events.sock`` must never take
  down the caller — the bus buys latency, not correctness.

Consumers that reconnect resume from a ULID cursor (``subscribe(since=...)``),
which is the property that makes the bus worth trusting: without it every
consumer still has to poll defensively for what it might have slept through.

Deliberate deviation from the 2026-07-25 synthesis capture, which proposed that
publishers stream over the socket and the server batch-append to SQLite to avoid
WAL write-lock contention: that makes the bus server a single point of *loss*
for events #156 must fail closed on, and the contention it avoids is unmeasured
at this cluster's event rates (single digits per second, ``busy_timeout=5000``
already configured). Publishers append durably, then notify best-effort.
Batching can be added behind this same ``record_event`` signature if contention
is ever measured.

The socket is same-user IPC (mode 0600). It is NOT an authentication boundary
and must not be used as one — anything needing a real boundary goes through the
hub's bearer-token path.
"""

import asyncio
import bisect
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import stat
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from . import config, edition, storage, table_schemas

logger = logging.getLogger("unified_agent.events")

# --- ULID ---------------------------------------------------------------------

# Crockford base32: no I, L, O, U — so a ULID read aloud or transcribed from a
# log line does not turn into a different id.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_INDEX = {ch: i for i, ch in enumerate(_CROCKFORD)}
ULID_LEN = 26
_TIME_LEN = 10  # 48 bits of milliseconds
_RAND_LEN = 16  # 80 bits of randomness
_MAX_RAND = (1 << 80) - 1

_ulid_lock = threading.Lock()
_last_ts_ms = -1
_last_rand = 0

# Channels named in the roadmap capture. The namespace is open — publishing to a
# new channel needs no change here — but these are the ones consumers can rely on
# existing.
CHANNEL_TASK_UPDATED = "agent.task.updated"
CHANNEL_APPROVAL_REQUESTED = "agent.approval.requested"
CHANNEL_HEALTH_ALERT = "agent.health.alert"
CHANNEL_PROJECT_BUILT = "agent.project.built"
# FirstMate-style COS wake (task #1028): completion / block / needs-decision only.
CHANNEL_SUPERVISOR_WAKE = "agent.supervisor.wake"

# storage.record_agent_event's `kind` → channel. Anything not listed maps to
# ``agent.<kind>.updated``, so a new kind is publishable without touching this.
_KIND_CHANNELS = {
    "task": CHANNEL_TASK_UPDATED,
    "approval": CHANNEL_APPROVAL_REQUESTED,
    "executor": CHANNEL_HEALTH_ALERT,
    "journal": "agent.journal.appended",
}

# How many rows a reconnecting subscriber may replay in one go. A cursor far
# enough in the past to exceed this gets the most recent window; unbounded
# replay would let one stale consumer pull the whole log into memory.
REPLAY_LIMIT = 1000

GENESIS_HASH = "GENESIS"


def compute_entry_hash(
    prev_hash: str | None,
    ulid: str,
    channel: str,
    payload_str: str,
    originator: str | None,
    episode_id: str | None,
    causal_parent_id: str | None,
    created_at: str,
    dedupe_key: str | None = None,
) -> str:
    """Compute SHA-256 hash for an event_log entry chained to prev_hash."""
    fields = {
        "causal_parent_id": causal_parent_id,
        "channel": channel,
        "created_at": created_at,
        "dedupe_key": dedupe_key,
        "episode_id": episode_id,
        "originator": originator,
        "payload": payload_str,
        "prev_hash": GENESIS_HASH if prev_hash is None else prev_hash,
        "ulid": ulid,
    }
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _encode(value: int, length: int) -> str:
    out = [""] * length
    for i in range(length - 1, -1, -1):
        out[i] = _CROCKFORD[value & 0x1F]
        value >>= 5
    return "".join(out)


def _decode(text: str) -> int | None:
    value = 0
    for ch in text:
        idx = _CROCKFORD_INDEX.get(ch)
        if idx is None:
            return None
        value = (value << 5) | idx
    return value


def _ulid_parts(ulid: Any) -> tuple[int, int] | None:
    """``(ms, random)`` of a canonical ULID, or None for anything else."""
    if not isinstance(ulid, str) or len(ulid) != ULID_LEN:
        return None
    ts = _decode(ulid[:_TIME_LEN])
    rand = _decode(ulid[_TIME_LEN:])
    if ts is None or rand is None or ts >= 1 << 48:
        return None
    return ts, rand


def is_ulid(value: Any) -> bool:
    """True for a canonical 26-character Crockford ULID (as ``new_ulid`` mints)."""
    return _ulid_parts(value) is not None


def new_ulid(ts_ms: int | None = None, *, after: str | None = None) -> str:
    """A lexicographically sortable, monotonic ULID.

    Monotonicity within a millisecond is not a nicety: a burst of events arrives
    at exactly that resolution, and a random suffix would let two same-ms events
    sort in the wrong order — which silently drops or repeats rows for every
    consumer resuming from a cursor. Same millisecond as the last id ⇒ increment
    the random component instead of redrawing it (the standard ULID monotonic
    rule).

    That guarantee is per process. ``after`` extends it across writers: the id
    returned sorts strictly after that ULID too (task #1640). An event_log
    append passes the log's current maximum, read under the write lock, so ULID
    order equals commit order no matter how many processes share the file —
    see ``mint_ulid_on_connection``.
    """
    global _last_ts_ms, _last_rand
    ts = _now_ms() if ts_ms is None else ts_ms
    floor = _ulid_parts(after) if after is not None else None
    with _ulid_lock:
        if ts == _last_ts_ms:
            rand = (_last_rand + 1) & _MAX_RAND
        elif ts < _last_ts_ms:
            # Clock went backwards (NTP step). Keep issuing ids that sort after
            # what we already wrote rather than colliding with history.
            rand = (_last_rand + 1) & _MAX_RAND
            ts = _last_ts_ms
        else:
            rand = secrets.randbits(80)
        if floor is not None and (ts, rand) <= floor:
            ts, rand = floor
            if rand == _MAX_RAND:
                ts, rand = ts + 1, 0
            else:
                rand += 1
        _last_ts_ms, _last_rand = ts, rand
    return _encode(ts, _TIME_LEN) + _encode(rand, _RAND_LEN)


def mint_ulid_on_connection(conn: Any) -> str:
    """A ULID for a row about to be appended on ``conn`` (task #1640).

    The caller must already hold the SQLite write lock (``BEGIN IMMEDIATE``, or
    a write earlier in the same transaction). Minting before the lock let a row
    commit after one carrying a newer ULID, and a ``since=`` consumer whose
    cursor had passed it skipped that row forever. Minted here, the id sorts
    after everything already committed, from any process.
    """
    row = conn.execute(
        "SELECT ulid FROM event_log WHERE length(ulid)=? ORDER BY ulid DESC LIMIT 1",
        (ULID_LEN,),
    ).fetchone()
    return new_ulid(after=row["ulid"] if row else None)


# --- channel matching ----------------------------------------------------------


def channel_matches(pattern: str, channel: str) -> bool:
    """``*`` matches everything; a trailing ``*`` matches a prefix; else exact."""
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return channel.startswith(pattern[:-1])
    return pattern == channel


def merge_channel_prefix(
    channels: list[str] | None, channel_prefix: str | None
) -> list[str] | None:
    """Union ``channels`` with the prefix form a client sends as ``channel_prefix``.

    A trailing ``*`` in ``channels`` is already a prefix. ``channel_prefix`` is
    that same filter as one string: ``agent.approval`` matches ``agent.approval``
    and ``agent.approval.requested``. A value that already ends in ``*`` is kept.
    Blank is ignored. ``None`` means no channel filter, the same as omitting both.
    """
    selected = [item.strip() for item in (channels or []) if isinstance(item, str) and item.strip()]
    prefix = channel_prefix.strip() if isinstance(channel_prefix, str) else ""
    if prefix:
        pattern = prefix if prefix.endswith("*") else f"{prefix}*"
        if pattern not in selected:
            selected.append(pattern)
    return selected or None


def _like(pattern: str) -> str:
    """`channel_matches` as a SQL LIKE operand (used with ESCAPE '\\')."""
    if pattern == "*":
        return "%"
    literal = pattern[:-1] if pattern.endswith("*") else pattern
    escaped = literal.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%" if pattern.endswith("*") else escaped


# --- the durable log -----------------------------------------------------------


def record_event(
    channel: str,
    payload: dict[str, Any] | None = None,
    originator: str | None = None,
    episode_id: str | None = None,
    causal_parent_id: str | None = None,
    notify: bool = True,
    dedupe_key: str | None = None,
    precondition: Callable[[Any], None] | None = None,
) -> str:
    """Append one event to ``event_log`` and return its ULID.

    Raises on a failed append — deliberately, and unlike
    ``storage.record_agent_event``. A caller that must fail closed on a missing
    record (task #156) cannot do so if this returns an id for a row that was
    never written.

    ``dedupe_key`` makes an external message safe to replay: the first durable
    row wins and later attempts return its ULID without publishing another event.
    ``precondition``, when supplied by an in-process writer, runs under the same
    SQLite write transaction immediately before the append.
    The socket fan-out afterwards is best-effort and never raises: the event is
    already durable, and a dead bus is a latency problem, not a data problem.
    """
    return record_event_once(
        channel,
        payload=payload,
        originator=originator,
        episode_id=episode_id,
        causal_parent_id=causal_parent_id,
        notify=notify,
        dedupe_key=dedupe_key,
        precondition=precondition,
    )[0]


def record_event_once(
    channel: str,
    payload: dict[str, Any] | None = None,
    originator: str | None = None,
    episode_id: str | None = None,
    causal_parent_id: str | None = None,
    notify: bool = True,
    dedupe_key: str | None = None,
    precondition: Callable[[Any], None] | None = None,
) -> tuple[str, bool]:
    """Append one event and report whether this call won the dedupe race.

    The boolean is important for external ingress adapters: a duplicate must not
    dispatch its mutation a second time, including when two retries arrive at
    the same time. The insert transaction is the authoritative idempotency gate.
    """
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        event = record_event_once_on_connection(
            conn,
            channel,
            payload=payload,
            originator=originator,
            episode_id=episode_id,
            causal_parent_id=causal_parent_id,
            dedupe_key=dedupe_key,
            precondition=precondition,
        )
        conn.commit()
    finally:
        conn.close()

    if notify and event[1] and event[2] is not None:
        publish(event[2])
    return event[0], event[1]


def record_event_once_on_connection(
    conn: Any,
    channel: str,
    payload: dict[str, Any] | None = None,
    originator: str | None = None,
    episode_id: str | None = None,
    causal_parent_id: str | None = None,
    dedupe_key: str | None = None,
    precondition: Callable[[Any], None] | None = None,
) -> tuple[str, bool, dict[str, Any] | None]:
    """Insert an event using a caller-owned transaction without committing.

    The returned publish payload is for post-commit socket fan-out. Callers that
    own a larger SQLite mutation can therefore make the durable ledger append
    part of that mutation's transaction while keeping notifications best-effort.

    The caller's transaction must already hold the write lock (``BEGIN
    IMMEDIATE`` or an earlier write): the ULID is minted here, against the
    log's committed maximum, so it agrees with commit order (task #1640).
    """
    if not conn.in_transaction:
        raise RuntimeError("record_event_once_on_connection requires an active caller transaction")
    if episode_id is None:
        episode_id = config.current_episode_id()
    if causal_parent_id is None:
        causal_parent_id = config.current_causal_parent_id()

    body = json.dumps(payload if payload is not None else {})
    ts = storage.now()
    if dedupe_key is not None:
        if not isinstance(dedupe_key, str) or not dedupe_key.strip():
            raise ValueError("dedupe_key must be a non-empty string when provided")
        dedupe_key = dedupe_key.strip()
    if precondition is not None:
        precondition(conn)

    ulid = mint_ulid_on_connection(conn)
    if edition.is_feature_available("audit_hash_chain"):
        last_row = conn.execute(
            "SELECT entry_hash FROM event_log WHERE entry_hash IS NOT NULL ORDER BY ROWID DESC LIMIT 1"
        ).fetchone()
        prev_hash = last_row["entry_hash"] if last_row and last_row["entry_hash"] else GENESIS_HASH
        entry_hash = compute_entry_hash(
            prev_hash,
            ulid,
            channel,
            body,
            originator,
            episode_id,
            causal_parent_id,
            ts,
            dedupe_key,
        )
    else:
        prev_hash = None
        entry_hash = None

    cur = conn.execute(
        "INSERT INTO event_log "
        "(ulid, channel, payload, originator, episode_id, causal_parent_id, created_at, dedupe_key, prev_hash, entry_hash, org_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(dedupe_key) DO NOTHING",
        (
            ulid,
            channel,
            body,
            originator,
            episode_id,
            causal_parent_id,
            ts,
            dedupe_key,
            prev_hash,
            entry_hash,
            edition.tenant_org_id(),
        ),
    )
    if cur.rowcount == 0:
        row = conn.execute(
            "SELECT ulid FROM event_log WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        if row is None:
            raise RuntimeError("event dedupe conflict did not resolve to an existing row")
        return row["ulid"], False, None
    publish_payload = {
        "seq": cur.lastrowid,
        "ulid": ulid,
        "channel": channel,
        "payload": payload if payload is not None else {},
        "originator": originator,
        "episode_id": episode_id,
        "causal_parent_id": causal_parent_id,
        "created_at": ts,
        "prev_hash": prev_hash,
        "entry_hash": entry_hash,
    }
    return ulid, True, publish_payload


def _row_to_event(row: Any) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {"raw": row["payload"]}
    d = {
        "seq": row["seq"],
        "ulid": row["ulid"],
        "dedupe_key": row["dedupe_key"],
        "channel": row["channel"],
        "payload": payload,
        "originator": row["originator"],
        "episode_id": row["episode_id"],
        "causal_parent_id": row["causal_parent_id"],
        "created_at": row["created_at"],
    }
    if "prev_hash" in row.keys():
        d["prev_hash"] = row["prev_hash"]
    if "entry_hash" in row.keys():
        d["entry_hash"] = row["entry_hash"]
    return d


def query_events(
    channels: list[str] | None = None,
    since: str | None = None,
    limit: int = 100,
    newest_first: bool = False,
    after_seq: int | None = None,
    item_id: str | None = None,
) -> list[dict[str, Any]]:
    """Events matching `channels`, after the `since` ULID or `after_seq` cursor.

    ``item_id`` keeps only events whose payload's top-level ``item_id`` equals
    it, so one node dispatch item's events are read whatever else the fleet
    logged since (task #1992).

    Every row carries ``seq``, its ROWID: the order rows were committed in,
    which is the order the hash chain links them in. ``after_seq`` is the
    gap-free cursor (task #1640). Since that task every append mints its ULID
    under the write lock, so new rows also agree on ULID order — but rows a
    pre-#1640 writer committed late (behind a newer ULID) are still in the log,
    and a ``since=`` consumer whose cursor passed one never sees it. ``since``
    stays for existing clients; new consumers should resume by ``seq``.

    With ``after_seq`` rows come in ascending ROWID order; otherwise in
    ascending ULID order (oldest first). Pass ``newest_first=True`` for a
    newest-window read. Both cursors are exclusive; passing both is an error.
    """
    if since and after_seq is not None:
        raise ValueError("pass either since (ULID) or after_seq, not both")
    where: list[str] = []
    params: list[Any] = []
    if channels:
        where.append("(" + " OR ".join("channel LIKE ? ESCAPE '\\'" for _ in channels) + ")")
        params.extend(_like(p) for p in channels)
    if item_id:
        # CASE is evaluated lazily, so a malformed payload never reaches json_extract.
        where.append(
            "(CASE WHEN json_valid(payload) THEN json_extract(payload, '$.item_id') END) = ?"
        )
        params.append(str(item_id))
    if after_seq is not None:
        where.append("ROWID > ?")
        params.append(int(after_seq))
        order_col = "ROWID"
    else:
        order_col = "ulid"
        if since:
            where.append("ulid > ?")
            params.append(since)
    sql = (
        "SELECT ROWID AS seq, ulid, channel, payload, originator, episode_id, "
        "causal_parent_id, created_at, dedupe_key, prev_hash, entry_hash "
        "FROM event_log"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    order = "DESC" if newest_first else "ASC"
    sql += f" ORDER BY {order_col} {order} LIMIT ?"
    params.append(max(1, int(limit)))

    conn = storage.connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_row_to_event(r) for r in rows]


LEDGER_ROWS_MAX = 1000


def query_ledger_rows(after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
    """Raw ``event_log`` rows in commit (ROWID) order, for external verifiers.

    ``payload_raw`` is the stored payload string, byte for byte, never parsed
    and re-serialized: ``compute_entry_hash`` hashes that exact string, so a
    verifier that re-encoded a parsed payload could disagree with the chain on
    key order or whitespace alone. Unhashed (core-edition) rows are included
    with ``entry_hash`` None, as ``verify_event_chain`` sees them.
    """
    limit = max(1, min(int(limit), LEDGER_ROWS_MAX))
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT ROWID AS seq, ulid, channel, payload, originator, episode_id, "
            "causal_parent_id, created_at, dedupe_key, prev_hash, entry_hash "
            "FROM event_log WHERE ROWID > ? ORDER BY ROWID ASC LIMIT ?",
            (max(0, int(after_seq)), limit),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "seq": r["seq"],
            "ulid": r["ulid"],
            "channel": r["channel"],
            "payload_raw": r["payload"],
            "originator": r["originator"],
            "episode_id": r["episode_id"],
            "causal_parent_id": r["causal_parent_id"],
            "created_at": r["created_at"],
            "dedupe_key": r["dedupe_key"],
            "prev_hash": r["prev_hash"],
            "entry_hash": r["entry_hash"],
        }
        for r in rows
    ]


def tail_cursor(channels: list[str] | None = None) -> tuple[str | None, int]:
    """``(newest matching ULID, newest seq)`` read from ONE snapshot.

    A live consumer starting "now" needs both, and two separate reads let a
    commit land between them: its seq becomes the exclusive start, so the
    event is never streamed. Both SELECTs here run in one read transaction.
    """
    where: list[str] = []
    params: list[Any] = []
    if channels:
        where.append("(" + " OR ".join("channel LIKE ? ESCAPE '\\'" for _ in channels) + ")")
        params.extend(_like(p) for p in channels)
    ulid_sql = "SELECT ulid FROM event_log"
    if where:
        ulid_sql += " WHERE " + " AND ".join(where)
    ulid_sql += " ORDER BY ulid DESC LIMIT 1"

    conn = storage.connect()
    try:
        conn.execute("BEGIN")
        seq_row = conn.execute("SELECT MAX(ROWID) AS seq FROM event_log").fetchone()
        ulid_row = conn.execute(ulid_sql, params).fetchone()
        conn.commit()
    finally:
        conn.close()
    return (ulid_row["ulid"] if ulid_row else None), int(seq_row["seq"] or 0)


def seq_for_ulid_cursor(since: str) -> int:
    """Translate a legacy ULID cursor into a ``seq`` cursor that never skips.

    For rows written since task #1640, ULID order is commit order and this is
    exactly the cursor row's seq. Rows written before it may be out of order,
    and a ``since`` client may have stopped (``limit``) before a row with a
    higher ULID that committed *earlier* than its cursor row. So the start is
    the lower of: the cursor row's seq, and the seq just before the first
    committed row that sorts after it. A missing cursor row (pruned, or never
    existed) uses the second alone; with neither, the tail.

    Delivery is therefore at-least-once over pre-#1640 reordered history: a
    row committed after that start but sorting at or before ``since`` is sent,
    even if the client happened to see it. Never skipping is the point;
    consumers dedupe by ``ulid``.
    """
    conn = storage.connect()
    try:
        conn.execute("BEGIN")
        own = conn.execute("SELECT ROWID AS seq FROM event_log WHERE ulid=?", (since,)).fetchone()
        after = conn.execute(
            "SELECT MIN(ROWID) AS seq FROM event_log WHERE ulid > ?", (since,)
        ).fetchone()
        tail = conn.execute("SELECT MAX(ROWID) AS seq FROM event_log").fetchone()
        conn.commit()
    finally:
        conn.close()
    candidates = []
    if own is not None:
        candidates.append(int(own["seq"]))
    if after is not None and after["seq"] is not None:
        candidates.append(int(after["seq"]) - 1)
    return min(candidates) if candidates else int(tail["seq"] or 0)


def query_recent_events(
    channels: list[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Newest ``limit`` events for ``channels``, returned in chronological order."""
    rows = query_events(channels=channels, limit=limit, newest_first=True)
    return list(reversed(rows))


def event_log_liveness(channels: list[str] | None = None) -> dict[str, int | str | None]:
    """Return total collected events and the newest timestamp for a channel source."""
    where: list[str] = []
    params: list[Any] = []
    if channels:
        where.append("(" + " OR ".join("channel LIKE ? ESCAPE '\\'" for _ in channels) + ")")
        params.extend(_like(p) for p in channels)
    sql = "SELECT COUNT(*) AS event_count, MAX(created_at) AS last_event_at FROM event_log"
    if where:
        sql += " WHERE " + " AND ".join(where)

    conn = storage.connect()
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return {"source_event_count": int(row["event_count"]), "last_event_at": row["last_event_at"]}


def latest_ulid(channels: list[str] | None = None) -> str | None:
    """The newest ULID in the log, or None when it is empty.

    `query_events` is oldest-first with a limit, so it cannot answer "where does the
    log end?" — a live consumer needs that to start at *now* instead of replaying
    history it never asked for.
    """
    where: list[str] = []
    params: list[Any] = []
    if channels:
        where.append("(" + " OR ".join("channel LIKE ? ESCAPE '\\'" for _ in channels) + ")")
        params.extend(_like(p) for p in channels)
    sql = "SELECT ulid FROM event_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ulid DESC LIMIT 1"

    conn = storage.connect()
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return row["ulid"] if row else None


@contextlib.contextmanager
def ledger_maintenance(conn: Any, reason: str = "prune") -> Iterator[None]:
    """Lift the event_log append-only triggers for the body of this block (#1908).

    ``event_log`` refuses UPDATE and DELETE unless ``event_log_maintenance``
    holds a row (``table_schemas.event_log_append_only``). This inserts that
    row on ``conn``'s write transaction (opening ``BEGIN IMMEDIATE`` if none is
    active) and deletes it again when the block exits, so the bypass is never
    committed: other connections cannot see an uncommitted row, and a rollback
    discards it as well. The caller commits or rolls back afterwards.

    Production code uses it only in :func:`prune_events`. Tests that simulate
    tampering use it to get past the triggers on purpose.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    table = table_schemas.EVENT_LOG_MAINTENANCE_TABLE
    conn.execute(
        f"INSERT INTO {table} (reason, started_at) VALUES (?, ?)",
        (reason, storage.now()),
    )
    try:
        yield
    except BaseException:
        # The caller's rollback discards the row; a failing cleanup must not
        # mask the original error.
        with contextlib.suppress(Exception):
            conn.execute(f"DELETE FROM {table}")
        raise
    conn.execute(f"DELETE FROM {table}")


#: Channels whose rows are durable proofs that later decisions still read, so
#: retention pruning never removes them: approval consumption nonces (dispatch
#: recovery, invocation recovery) and the task invocation receipt/commit events
#: that authenticate replay of a stored receipt (#1973 review), and the
#: conversation handoffs that authorize a task's completion notices (#2019).
#: Kept in sync with ``tools.approvals`` and ``messaging.handoff`` by test.
PRUNE_PROTECTED_CHANNELS = frozenset(
    {
        "agent.approval.consumed",
        "agent.synthetic_workflow",
        "agent.synthetic_workflow.completed",
        "agent.task.invocation_receipt",
        "agent.task.invocation_committed",
        "agent.conversation.handoff",
    }
)


def prune_events(older_than_days: float | None = None) -> int:
    """Delete events older than the retention window; returns rows removed.

    Never called automatically. An append-only ledger that silently trims itself
    is a ledger a downstream consumer cannot reason about, so pruning is an
    explicit operator action (or a future cron) rather than a side effect of
    startup. ``0`` days retention means "keep forever" and prunes nothing.

    Re-rooting the chain (below) intentionally does NOT try to make the pruned
    prefix cryptographically unforgeable in place — see the design note there.
    What this function does instead, to honor the "a ledger a downstream
    consumer cannot reason about" principle from this docstring: it records the
    prune itself as a normal, hashed ``agent.audit.pruned`` event once the chain
    is valid again, so the gap left by deletion is visible in the log a
    consumer already reads, rather than vanishing without a trace (the same
    "external witness" idea ``audit/reanchor.py`` uses for daily chain heads,
    applied to the one operation that is allowed to shorten the chain).

    The newest row is never pruned, however old (task #1640). ``event_log``
    has no AUTOINCREMENT, so SQLite hands out ``MAX(ROWID) + 1``: deleting the
    row that holds the maximum would let the next append reuse a ``seq`` an
    ``after_seq`` consumer has already passed, and emptying the table would
    restart ``seq`` at 1.

    Known design gap, not fixed here: re-rooting rewrites every surviving
    ``prev_hash``/``entry_hash``, so a chain head anchored externally before a
    prune (``audit/reanchor.py`` daily heads, or any verifier's saved head) no
    longer appears in the log afterwards. Such an anchor can only be checked
    against the ``agent.audit.pruned`` event, not re-derived from the rows.

    ``event_log`` refuses UPDATE and DELETE by trigger (task #1908). The delete
    and re-root run inside :func:`ledger_maintenance`, which lifts the
    triggers only for this write transaction; the audit event is appended
    after the bypass is closed.
    """
    from datetime import UTC, datetime, timedelta

    days = config.event_log_retention_days() if older_than_days is None else older_than_days
    if days <= 0:
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Refuse to rewrite an already damaged ledger, including damage in
            # the prefix about to be removed. Otherwise an unhashed gap can
            # become a permitted legacy prefix after re-rooting.
            verification = verify_event_chain(conn)
            if verification["status"] == "tampered":
                rowid = verification["tampered_at"]["rowid"]
                raise ValueError(
                    f"tampered event_log row {rowid}: verify and repair before pruning"
                )
            protected = sorted(PRUNE_PROTECTED_CHANNELS)
            marks = ",".join("?" for _ in protected)
            prunable = (
                f"created_at < ? AND channel NOT IN ({marks}) "
                "AND ROWID < (SELECT MAX(ROWID) FROM event_log) "
                "AND NOT EXISTS (SELECT 1 FROM event_log proof "
                "WHERE proof.channel='agent.synthetic_workflow' "
                "AND json_extract(proof.payload,'$.gateway_event_id')=event_log.ulid)"
            )
            # Protected proofs can survive between pruned rows, so the surviving
            # chain may have holes, not only a missing prefix. Remember exactly
            # where rows were removed so the link check below tolerates only those.
            pruned_rowids = [
                row[0]
                for row in conn.execute(
                    f"SELECT ROWID FROM event_log WHERE {prunable} ORDER BY ROWID",
                    (cutoff, *protected),
                ).fetchall()
            ]
            with ledger_maintenance(conn, "prune"):
                cur = conn.execute(f"DELETE FROM event_log WHERE {prunable}", (cutoff, *protected))
                removed = cur.rowcount

                if removed:
                    # A pruned self-pickup claim leaves its heartbeat usage with no
                    # run to belong to (Devin review on PR #940).
                    from . import run_records

                    run_records.drop_orphan_heartbeats(conn)

                    rows = conn.execute(
                        "SELECT ROWID, ulid, channel, payload, originator, episode_id, "
                        "causal_parent_id, created_at, dedupe_key, prev_hash, entry_hash "
                        "FROM event_log WHERE entry_hash IS NOT NULL ORDER BY ROWID ASC"
                    ).fetchall()
                    new_root_ulid = rows[0]["ulid"] if rows else None

                    # Verify stored links before rewriting the surviving suffix. A
                    # break is allowed only where this prune removed rows.
                    previous_hash = None
                    previous_rowid = None
                    for row in rows:
                        gap = previous_rowid is not None and bisect.bisect_right(
                            pruned_rowids, previous_rowid
                        ) < bisect.bisect_left(pruned_rowids, row["ROWID"])
                        if (
                            previous_hash is not None
                            and not gap
                            and row["prev_hash"] != previous_hash
                        ):
                            raise ValueError(
                                f"tampered event_log row {row['ROWID']}: prev_hash link"
                            )
                        expected_hash = compute_entry_hash(
                            row["prev_hash"],
                            row["ulid"],
                            row["channel"],
                            row["payload"],
                            row["originator"],
                            row["episode_id"],
                            row["causal_parent_id"],
                            row["created_at"],
                            row["dedupe_key"],
                        )
                        if expected_hash != row["entry_hash"]:
                            raise ValueError(f"tampered event_log row {row['ROWID']}: entry_hash")
                        previous_hash = row["entry_hash"]
                        previous_rowid = row["ROWID"]

                    # Re-root and hash the whole suffix while the write lock is held.
                    prev_hash = GENESIS_HASH
                    for row in rows:
                        entry_hash = compute_entry_hash(
                            prev_hash,
                            row["ulid"],
                            row["channel"],
                            row["payload"],
                            row["originator"],
                            row["episode_id"],
                            row["causal_parent_id"],
                            row["created_at"],
                            row["dedupe_key"],
                        )
                        conn.execute(
                            "UPDATE event_log SET prev_hash=?, entry_hash=? WHERE ROWID=?",
                            (prev_hash, entry_hash, row["ROWID"]),
                        )
                        prev_hash = entry_hash

            if removed:
                record_event_once_on_connection(
                    conn,
                    "agent.audit.pruned",
                    payload={
                        "rows_pruned": removed,
                        "cutoff": cutoff,
                        "retention_days": days,
                        "new_root_ulid": new_root_ulid,
                    },
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    return removed


#: Cap on the offending rows listed in a verify report; the count is exact.
UNHASHED_ROWS_REPORT_LIMIT = 100


def verify_event_chain(conn: Any = None) -> dict[str, Any]:
    """Verify the integrity of the event_log hash chain (task #746).

    Recomputes hashes for every entry with an entry_hash, checking both:
    1. prev_hash matches the preceding row's entry_hash (or GENESIS for the root).
    2. entry_hash matches the sha256 of the entry's canonical representation.

    Unhashed rows (``entry_hash`` NULL) before the first hashed row predate the
    chain (a legacy store, or a hub that ran ``UA_EDITION=core`` before the
    chain started). They are allowed and counted in ``unhashed_skipped``.

    An unhashed row *after* the chain started is not allowed (task #1908): it is
    an append that went around the ledger, by direct SQL or by a writer running
    without the chain. The hashed rows around it can still link, so the report
    is ``tampered`` with reason ``unhashed_row_after_chain_start``, the count in
    ``unhashed_after_chain_start`` and the first
    :data:`UNHASHED_ROWS_REPORT_LIMIT` offenders in ``unhashed_rows``. A
    prev_hash or entry_hash mismatch is reported first when both occur.

    Returns:
        {"status": "valid"|"tampered"|"empty", "verified": bool, "total_verified": int, ...}
    """
    close_conn = False
    if conn is None:
        conn = storage.connect()
        close_conn = True

    try:
        rows = conn.execute(
            "SELECT ROWID, ulid, channel, payload, originator, episode_id, causal_parent_id, "
            "created_at, dedupe_key, prev_hash, entry_hash "
            "FROM event_log "
            "ORDER BY ROWID ASC"
        ).fetchall()

        if not rows:
            return {
                "status": "empty",
                "verified": True,
                "total_verified": 0,
                "unhashed_skipped": 0,
                "unhashed_after_chain_start": 0,
                "head_hash": None,
                "head_ulid": None,
            }

        expected_prev = GENESIS_HASH
        chain_started = False
        verified_count = 0
        unhashed_skipped = 0
        unhashed_after = 0
        unhashed_rows: list[dict[str, Any]] = []
        last_hashed_row = None

        def _tampered(tampered_at: dict[str, Any]) -> dict[str, Any]:
            report = {
                "status": "tampered",
                "verified": False,
                "total_verified": verified_count,
                "unhashed_skipped": unhashed_skipped,
                "unhashed_after_chain_start": unhashed_after,
                "tampered_at": tampered_at,
            }
            if unhashed_rows:
                report["unhashed_rows"] = unhashed_rows
            return report

        # Count all gaps up front: a primary hash failure below returns early.
        first_hashed = next(
            (i for i, row in enumerate(rows) if row["entry_hash"] is not None), len(rows)
        )
        gaps = [row for row in rows[first_hashed:] if row["entry_hash"] is None]
        unhashed_after = len(gaps)
        unhashed_rows = [
            {"rowid": row["ROWID"], "ulid": row["ulid"], "channel": row["channel"]}
            for row in gaps[:UNHASHED_ROWS_REPORT_LIMIT]
        ]
        for row in rows:
            entry_hash = row["entry_hash"]
            if entry_hash is None:
                if not chain_started:
                    # Predates the chain: legacy rows, or a core-edition hub
                    # before audit_hash_chain was on. Allowed and counted.
                    unhashed_skipped += 1
                    continue
                # Inside the chain, a row without a hash bypassed the ledger.
                # Keep walking so a hash mismatch further on still wins.
                continue

            chain_started = True
            actual_prev = row["prev_hash"]
            if actual_prev != expected_prev:
                return _tampered(
                    {
                        "rowid": row["ROWID"],
                        "ulid": row["ulid"],
                        "channel": row["channel"],
                        "reason": "prev_hash_mismatch",
                        "expected_prev_hash": expected_prev,
                        "actual_prev_hash": actual_prev,
                    }
                )

            expected_hash = compute_entry_hash(
                actual_prev,
                row["ulid"],
                row["channel"],
                row["payload"],
                row["originator"],
                row["episode_id"],
                row["causal_parent_id"],
                row["created_at"],
                row["dedupe_key"],
            )

            if expected_hash != entry_hash:
                return _tampered(
                    {
                        "rowid": row["ROWID"],
                        "ulid": row["ulid"],
                        "channel": row["channel"],
                        "reason": "hash_mismatch",
                        "expected_hash": expected_hash,
                        "actual_hash": entry_hash,
                    }
                )

            expected_prev = entry_hash
            verified_count += 1
            last_hashed_row = row

        if not chain_started:
            return {
                "status": "empty",
                "verified": True,
                "total_verified": 0,
                "unhashed_skipped": unhashed_skipped,
                "unhashed_after_chain_start": 0,
                "head_hash": None,
                "head_ulid": None,
            }

        if unhashed_after:
            first = unhashed_rows[0]
            return _tampered(
                {
                    **first,
                    "reason": "unhashed_row_after_chain_start",
                    "unhashed_count": unhashed_after,
                }
            )

        return {
            "status": "valid",
            "verified": True,
            "total_verified": verified_count,
            "unhashed_skipped": unhashed_skipped,
            "unhashed_after_chain_start": 0,
            "head_hash": last_hashed_row["entry_hash"],
            "head_ulid": last_hashed_row["ulid"],
        }
    finally:
        if close_conn:
            conn.close()


# --- the chokepoint hook -------------------------------------------------------


def approval_notification_tool(tool: str) -> str | None:
    """A bounded tool identifier, never arbitrary approval text or payload."""
    if len(tool) <= 128 and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*(?:[.:][A-Za-z_][A-Za-z0-9_]*)*", tool
    ):
        return tool
    return None


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def bounded_run_id(value: object) -> str | None:
    """A run id safe to store and notify, or None.

    Shape only. Callers still decide whether the id is the caller's current
    run; this refuses blank, oversized, or punctuation-bearing text so a
    notification cannot carry an arbitrary string.
    """
    if isinstance(value, str) and _RUN_ID_RE.fullmatch(value):
        return value
    return None


def mirror_agent_event(
    kind: str,
    ref_id: str | None = None,
    agent: str | None = None,
    summary: str | None = None,
    *,
    approval_tool: str | None = None,
    run_id: str | None = None,
) -> str | None:
    """Mirror a ``storage.record_agent_event`` call onto the bus.

    Called from that function so the ~18 existing call sites (task claim and
    update, approval request, delegation, memory save, plan, workflow, journal)
    become publishers without any of them being edited. Returns None when the
    bus is disabled.
    """
    if not config.event_bus_enabled():
        return None
    channel = _KIND_CHANNELS.get(kind, f"agent.{kind}.updated")
    payload = {"kind": kind, "ref_id": ref_id, "summary": summary}
    if kind == "approval" and approval_tool is not None:
        # Only the request producer opts into this additive shape. Resolution
        # and legacy mirrors keep their original fields. A caller-supplied
        # agent label cannot impersonate the requester in notification data.
        tool = approval_notification_tool(approval_tool)
        agent = config.verified_agent_identity()
        payload.update(
            tool=tool,
            requested_by=agent,
            summary=f"queued: {tool}" if tool else "queued",
            # Display linkage beside the #1615 fields. Null when the request
            # had no unambiguous run. Never read back as authority.
            run_id=bounded_run_id(run_id),
        )
    return record_event(channel=channel, payload=payload, originator=agent)


# --- the socket: publishing ----------------------------------------------------


def socket_path() -> Path:
    return config.event_socket_path()


def publish(event: dict[str, Any], socket_path: Path | str | None = None) -> bool:
    """Best-effort fan-out of one already-durable event. Never raises.

    Returns True if the bus accepted the bytes, False for every failure mode —
    no server listening (ENOENT), a stale socket file left by a killed process
    (ECONNREFUSED), a regular file at the path (ENOTSOCK), or the bus disabled.
    All four are ordinary states, not errors: the event is already in the log
    and a reconnecting subscriber will replay it.
    """
    if not config.event_bus_enabled():
        return False
    path = str(socket_path if socket_path is not None else config.event_socket_path())
    frame = json.dumps({"op": "publish", "event": event}).encode("utf-8") + b"\n"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(1.0)
        sock.connect(path)
        sock.sendall(frame)
        return True
    except OSError as e:
        logger.debug("event bus publish skipped (%s): %s", path, e)
        return False
    finally:
        sock.close()


# --- the socket: subscribing ---------------------------------------------------


class Subscription:
    """A live subscriber connection. Use as a context manager."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""

    def next_event(self, timeout: float = 5.0) -> dict[str, Any] | None:
        """The next event, or None if `timeout` elapses or the server hangs up."""
        deadline = time.monotonic() + timeout
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line, self._buf = self._buf[:nl], self._buf[nl + 1 :]
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("op") == "event":
                    return msg.get("event")
                continue

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                self._sock.settimeout(remaining)
                chunk = self._sock.recv(65536)
            except TimeoutError:
                return None
            except OSError:
                return None
            if not chunk:
                return None
            self._buf += chunk

    def close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()

    def abort(self) -> None:
        """Drop the connection without an orderly shutdown (what a crash does)."""
        self._sock.close()

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def subscribe(
    channels: list[str],
    since: str | None = None,
    socket_path: Path | str | None = None,
    connect_timeout: float = 5.0,
) -> Subscription:
    """Open a subscription to `channels`, optionally replaying after `since`.

    Unlike ``publish``, this raises when it cannot connect: a consumer that
    believes it is subscribed but is not would sit silent forever, which is the
    failure this whole layer exists to remove.
    """
    path = str(socket_path if socket_path is not None else config.event_socket_path())
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(connect_timeout)
    sock.connect(path)
    frame = json.dumps({"op": "subscribe", "channels": channels, "since": since})
    sock.sendall(frame.encode("utf-8") + b"\n")
    return Subscription(sock)


# --- the socket: the server ----------------------------------------------------


class _Subscriber:
    __slots__ = ("patterns", "queue")

    def __init__(self, patterns: list[str], queue: "asyncio.Queue[dict[str, Any]]"):
        self.patterns = patterns
        self.queue = queue


class EventBusServer:
    """Unix-socket relay: accepts publishes, fans them out, replays from the log.

    Deliberately NOT the write path — see the module docstring. Runs its own
    asyncio loop on a dedicated thread so it can be embedded in the hub, the
    executor, or a test without entangling with MCPServer's loop.
    """

    def __init__(self, socket_path: Path | str | None = None):
        self.socket_path = Path(
            socket_path if socket_path is not None else config.event_socket_path()
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: asyncio.AbstractServer | None = None
        self._subscribers: set[_Subscriber] = set()
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    # -- lifecycle --

    def _clear_stale_socket(self) -> None:
        """Remove a socket left by a dead predecessor — and ONLY a socket.

        ``cleanup_stale_worktrees`` shipped the opposite bug (it deleted
        whatever it could not identify), so this refuses to unlink anything that
        is not a socket, and refuses to unlink a socket something is still
        accepting on.
        """
        if not self.socket_path.exists():
            return
        if not stat.S_ISSOCK(os.stat(self.socket_path).st_mode):
            raise OSError(f"{self.socket_path} exists and is not a socket; refusing to remove it")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.5)
            probe.connect(str(self.socket_path))
        except OSError:
            self.socket_path.unlink()  # nobody is accepting: a dead predecessor
            return
        finally:
            probe.close()
        raise OSError(f"another event bus is already listening on {self.socket_path}")

    def _bind(self) -> socket.socket:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._clear_stale_socket()
        # umask rather than a post-bind chmod: there must be no window in which
        # the socket is group/world-accessible.
        old_umask = os.umask(0o177)
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(self.socket_path))
            sock.listen(128)
        finally:
            os.umask(old_umask)
        sock.setblocking(False)
        return sock

    def start(self, timeout: float = 10.0) -> None:
        """Bind and serve on a background thread; returns once accepting."""
        if self._thread is not None:
            return
        listener = self._bind()  # bind on THIS thread so errors surface to the caller
        self._ready.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._run, args=(listener,), name="ua-event-bus", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("event bus server did not start within timeout")
        if self._start_error is not None:
            raise self._start_error

    def _run(self, listener: socket.socket) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._server = loop.run_until_complete(
                asyncio.start_unix_server(self._handle, sock=listener)
            )
            self._ready.set()
            loop.run_forever()
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
            self._start_error = e
            self._ready.set()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _shutdown(self) -> None:
        """Close the listener and finish every in-flight handler ON the loop.

        Stopping the loop out from under live subscriber tasks leaves their
        writers to be closed by the garbage collector against an already-closed
        loop, which surfaces as an unraisable ``RuntimeError: Event loop is
        closed`` during teardown. Draining here keeps shutdown quiet and, more
        importantly, deterministic.
        """
        # Order matters: Server.wait_closed() waits for every handler task to
        # finish, and a subscriber handler is parked on `await queue.get()`
        # forever by design. Closing and awaiting first therefore deadlocks
        # until the caller's timeout — cancel the handlers, then wait.
        if self._server is not None:
            self._server.close()
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()

    def stop(self, timeout: float = 5.0) -> None:
        loop, thread = self._loop, self._thread
        if loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout)
            except Exception as e:  # noqa: BLE001 - shutdown is best-effort
                logger.debug("event bus shutdown drain failed: %s", e)
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=timeout)
        self._loop = None
        self._thread = None
        self._server = None
        self._subscribers.clear()
        try:
            if self.socket_path.exists() and stat.S_ISSOCK(os.stat(self.socket_path).st_mode):
                self.socket_path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "EventBusServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- protocol --

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            msg = json.loads(line)
        except (ValueError, OSError):
            self._close_writer(writer)
            return

        op = msg.get("op")
        if op == "publish":
            self._fanout(msg.get("event") or {})
            self._close_writer(writer)
        elif op == "subscribe":
            await self._serve_subscriber(msg, writer)
        else:
            self._close_writer(writer)

    def _fanout(self, event: dict[str, Any]) -> None:
        channel = event.get("channel") or ""
        for sub in list(self._subscribers):
            if any(channel_matches(p, channel) for p in sub.patterns):
                sub.queue.put_nowait(event)

    async def _serve_subscriber(self, msg: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        patterns = msg.get("channels") or ["*"]
        since = msg.get("since")

        # Register BEFORE replaying: an event recorded between the replay query
        # and registration would otherwise fall in the gap the cursor exists to
        # close. Overlap is then removed by the `<= last` check below, which is
        # exact because ULIDs are monotonic.
        sub = _Subscriber(patterns, asyncio.Queue())
        self._subscribers.add(sub)
        last = since
        try:
            if since is not None:
                loop = asyncio.get_running_loop()
                rows = await loop.run_in_executor(
                    None, lambda: query_events(patterns, since, REPLAY_LIMIT)
                )
                for row in rows:
                    self._write_event(writer, row)
                    last = row["ulid"]
                await writer.drain()

            while True:
                event = await sub.queue.get()
                if last is not None and (event.get("ulid") or "") <= last:
                    continue
                last = event.get("ulid") or last
                self._write_event(writer, event)
                await writer.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass  # the subscriber went away; that is its business, not the bus's
        finally:
            self._subscribers.discard(sub)
            self._close_writer(writer)

    @staticmethod
    def _write_event(writer: asyncio.StreamWriter, event: dict[str, Any]) -> None:
        writer.write(json.dumps({"op": "event", "event": event}).encode("utf-8") + b"\n")

    @staticmethod
    def _close_writer(writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
        except OSError:
            pass


def main() -> None:
    """Run the event bus in the foreground (``unified-agent-eventbus``)."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    storage.init_db()
    server = EventBusServer()
    server.start()
    logger.info("event bus listening on %s", server.socket_path)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("event bus stopped by user")
    finally:
        server.stop()


if __name__ == "__main__":
    main()
