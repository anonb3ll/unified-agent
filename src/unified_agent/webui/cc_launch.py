"""Control Center launch attempts: idempotent launches and their lookup (#2466, #2501).

A launch starts real agent work: a hub dispatch, a dispatch-stack step, a ua-node
session, a ua-node restart. When the browser loses the answer it cannot tell
whether the work started, so the hub is the authority. A launch request may carry
an ``Idempotency-Key`` header (the client's attempt id); the hub keeps one attempt
record per tenant, verified identity and key.

* The first request records the attempt (``in_progress``, with a lease) before
  doing anything irreversible, and records each step's ids as it goes. While the
  request runs, a heartbeat renews the lease in the database (so it holds across
  hub workers), and its own worker treats the hold as live whatever the clock
  says; a lease lapses only when its holder is gone.
* Recording a step is a receipt, never the launch itself: a receipt that cannot
  be written (a locked or failing database) is logged and kept pending; every
  later write of the attempt (a save, the heartbeat, the final record) records it
  first, under one lock per attempt, and it never fails or downgrades the launch
  (task #2509). The one exception is a step that decides *what* to launch (a
  stack's task): it must be durable before anything launches, so a request that
  lost its hold answers 409 and one that could not record it answers 503.
* The same key with a different request is a typed 409 ``conflict``.
* ``GET /api/launch-attempts/{key}`` and a repeat ``POST`` read the attempt through
  one function, ``reconcile``, driven by the table below.

What proves a launch (review #1521). Evidence is positively bound to the attempt by
its operation key (derived from tenant, identity and client key): a node item whose
``launch_key`` is that key, or the dispatch run the coordinator bound to that key in
``dispatch_launch_keys``, in the same transaction that minted the run, that
**reached launch**: state ``launched``, ``running``, ``finished``, ``failed``,
``blocked`` or ``denied``, or a ``dispatch-launched:<run_id>`` transition event (a
launched run later cancelled or expired). A ``claimed`` run (the coordinator commits
it before calling the launch boundary) and a ``retryable`` run (a failed spawn) are
not launches, and no other run of the task, keyed or unkeyed, is ever evidence.

============================  ================  ===================  =====================
attempt record                evidence          GET answers          repeat POST
============================  ================  ===================  =====================
none                          —                 ``unknown``          records, runs
``in_progress``, live lease   any               ``in_progress``      409 launch_in_progress
``in_progress``, lapsed       launch proven     ``started`` (ids)    replays the answer
``in_progress``, lapsed       none / claimed /  ``failed``           continues from the
                              retryable run                          recorded steps
``parked`` (approval)         any               ``parked``           continues (gates run)
``failed``                    launch proven     ``started`` (ids)    replays the answer
``failed``                    none / claimed /  ``failed``           continues
                              retryable run
``in_progress`` lapsed, or    spawn unsettled   ``unsettled`` (run)  409 launch_unsettled
``failed``, or ``unsettled``
``unsettled``                 spawn settled     ``failed``           continues
                              failed (operator)
``started``                   task still exists ``started``          replays the answer
``started``                   task gone         ``started``          409 conflict
============================  ================  ===================  =====================

"Spawn unsettled": the bound run's spawn started (``spawn_started_at``) with no
recorded outcome, so its launcher may still be spawning (review #1521 pass 5).
``unsettled`` is not terminal: it is never pruned, so the key never gets a new
generation, and a repeat never runs. It ends when the spawn's outcome is recorded
(launched → ``started``) or when the operator's
``POST /api/dispatch-runs/{run_id}/settle-spawn`` proves the launcher and its job
gone and records the spawn ``failed`` (→ ``failed``, and a repeat continues).

A ``failed`` record whose own launch is proven is a lost receipt, not a failure
(task #2509: the run committed, then writing its id failed), so it is settled to
``started`` like a lapsed attempt. Every transition to ``started`` (a request's
own success, or a reconciled attempt) stores a replayable answer in the same
transaction: the route's answer,
or, when the launch was proven from evidence, a canonical answer built from it
(``reconciled: true``). A request's own answer is ``started`` only when its launch
is proven the same way, so a coordinator spawn failure (``launched: false`` with a
``run_id``) is ``failed``, and ``already_claimed`` by this attempt's own launched
run is ``started``. A request that loses its hold (only if its heartbeat could not
reach the database for a whole lease) still returns its own answer; it records
nothing, and the next reader reconciles from the evidence.

Below the record, the database refuses a second job on its own, on any hub
worker: ``delegate`` takes the operation key; a node item stores it under a unique
index; ``claim_and_dispatch`` binds it to the run it mints (primary key
``dispatch_launch_keys.launch_key``) and refuses a claim under a key already bound
to another run (``launch_key_bound`` for another task, ``already_claimed`` for the
same one); only a same-task run that never reached launch may be replaced by a
retry. ``claim_and_dispatch`` also never launches a claimed task twice.

Records live in the shared ``idempotency_keys`` table under tool ``cc_launch``,
read and written only through the tenant-scoped ``storage`` helpers: each tenant
has its own key space, and a record written before tenant scoping is refused,
never replayed (``AmbiguousIdempotencyReceipt``). The stored key hashes the
identity with the client key, so another identity's key is simply absent.

Retention: terminal (``started``, ``failed``) records older than ``RETENTION_S``
(30 days) are pruned, and past ``MAX_RECORDS`` the oldest terminal records go
first; both run, bounded, when a new attempt is recorded. ``in_progress``,
``parked`` and ``unsettled`` records are never pruned. A pruned key is forgotten: the same client
key later starts a new operation under a new generation (see ``operation_key``),
so nothing bound to the pruned attempt is replayed or adopted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .. import edition, storage
from ..tool_errors import ConflictError
from . import routes

logger = logging.getLogger(__name__)

TOOL = "cc_launch"
KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
KINDS = ("dispatch", "stack", "device", "restart")
_NODE_KINDS = ("device", "restart")
#: A holder's lease; its heartbeat renews it every third of this while the request runs.
LEASE_S = 60.0
RETENTION_S = 30 * 24 * 3600
MAX_RECORDS = 5000
_PRUNE_BATCH = 500
#: The only reason text a non-4xx failure records: nothing internal leaks.
INTERNAL_REASON = "the launch failed on the hub"
SETTLED_REASON = (
    "the operator settled this launch's spawn as failed; repeat it with the same key to continue"
)
INTERRUPTED_REASON = (
    "the launch was interrupted before it started; repeat it with the same key to continue"
)
_ID_FIELDS = ("approval_id", "delegation_id", "task_id", "run_id", "item_id")
#: Owner tokens of the requests this worker is running now: their holds are live
#: whatever their lease says (a starved heartbeat never hands a running launch over).
_LIVE_OWNERS: set[str] = set()
_LIVE_LOCK = threading.Lock()


UNSETTLED_REASON = (
    "the launch's spawn started but its outcome was never recorded, so it may still be "
    "running; it cannot be repeated until the operator settles its run"
)


class LaunchUnsettled(Exception):
    """The attempt's spawn started without a recorded outcome (review #1521 pass 5)."""

    def __init__(self, run_id: str | None) -> None:
        super().__init__(UNSETTLED_REASON)
        self.run_id = run_id


class LaunchBusy(Exception):
    """Another request of this attempt holds its lease."""


def validate_key(raw: str) -> str:
    if not KEY_RE.fullmatch(raw):
        raise ValueError(
            "Idempotency-Key must be 1-128 characters of letters, digits, '.', '_', ':' or "
            "'-', starting with a letter or digit"
        )
    return raw


def _record_key(identity: str, key: str) -> str:
    """The caller key ``storage`` scopes by tenant: identity and client key, hashed."""
    return "cc-launch:" + hashlib.sha256(f"{identity}\0{key}".encode()).hexdigest()


def operation_key(identity: str, key: str, generation: str | None = None) -> str:
    """The key the launched operation is bound to: tenant, identity, client key, generation.

    ``generation`` is a random value each attempt record gets when it is created
    (review #1521 pass 4): once a record is pruned, a new launch under the same
    client key is a new operation, and nothing bound to the pruned one (its
    delegation, dispatch run or queue item) is ever adopted. A record written
    before generations has none, and keeps the key it was launched under.
    """
    tenant = edition.tenant_org_id() or ""
    parts = [tenant, identity, key] + ([generation] if generation else [])
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()
    return f"cc-launch-op:{digest}"


def request_digest(kind: str, request: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"kind": kind, "request": request}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _expired(record: dict[str, Any], now: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(record.get("lease_until"))) <= now
    except ValueError:
        return True


def _lease_until(now: datetime) -> str:
    return _iso(now + timedelta(seconds=LEASE_S))


def _held_live(record: dict[str, Any], now: datetime) -> bool:
    """A holder is running: its lease is current, or it runs in this worker now."""
    with _LIVE_LOCK:
        running_here = record.get("owner") in _LIVE_OWNERS
    return running_here or not _expired(record, now)


# --- the store ----------------------------------------------------------------------


def _read(conn, identity: str, key: str) -> dict[str, Any] | None:
    row = storage.read_idempotency_row(conn, _record_key(identity, key))
    if row is None:
        return None
    if row["tool"] != TOOL:
        raise ConflictError("Idempotency-Key is bound to another operation; use a new key")
    try:
        record = json.loads(row["result_json"])
    except ValueError:
        return None
    # Defence in depth: the hashed key already names the identity.
    return record if record.get("identity") == identity else None


def _write(conn, identity: str, key: str, record: dict[str, Any]) -> None:
    stored = storage.idempotency_storage_key(_record_key(identity, key))
    conn.execute(
        "INSERT INTO idempotency_keys (key, tool, result_json, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET result_json=excluded.result_json WHERE tool=excluded.tool",
        (stored, TOOL, json.dumps(record, sort_keys=True), record["created_at"]),
    )


def _prune(conn, now: datetime) -> None:
    """Drop terminal records past retention, then the oldest past ``MAX_RECORDS``."""
    terminal = "json_extract(result_json, '$.state') IN ('started', 'failed')"
    conn.execute(
        "DELETE FROM idempotency_keys WHERE rowid IN (SELECT rowid FROM idempotency_keys "
        f"WHERE tool=? AND created_at < ? AND {terminal} LIMIT ?)",
        (TOOL, _iso(now - timedelta(seconds=RETENTION_S)), _PRUNE_BATCH),
    )
    count = conn.execute("SELECT COUNT(*) FROM idempotency_keys WHERE tool=?", (TOOL,)).fetchone()
    excess = int(count[0]) - MAX_RECORDS
    if excess > 0:
        conn.execute(
            "DELETE FROM idempotency_keys WHERE rowid IN (SELECT rowid FROM idempotency_keys "
            f"WHERE tool=? AND {terminal} ORDER BY created_at ASC LIMIT ?)",
            (TOOL, min(excess, _PRUNE_BATCH)),
        )


def _held_update(attempt: Attempt, change: Callable[[Any, dict[str, Any]], None]) -> bool:
    """Apply ``change`` to the record while ``attempt`` still holds it; False otherwise.

    One writer per attempt at a time (the request thread and its heartbeat share
    ``attempt.lock``). Every write first merges the attempt's pending step
    receipts (those an earlier write could not record), so a later save or a
    heartbeat never drops them (review #1521 pass 3, item 2).
    """
    with attempt.lock, closing(storage.connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _read(conn, attempt.identity, attempt.key)
            if current is None or current.get("owner") != attempt.token:
                conn.rollback()
                return False
            pending = dict(attempt.pending)
            _merge_steps(current, pending)
            change(conn, current)
            _write(conn, attempt.identity, attempt.key, current)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        for name, value in pending.items():
            if attempt.pending.get(name) == value:
                del attempt.pending[name]
        attempt.record = current
    return True


def _merge_steps(record: dict[str, Any], changes: dict[str, Any]) -> None:
    record.setdefault("steps", {}).update(changes)
    for name in _ID_FIELDS:
        if changes.get(name) is not None:
            record.setdefault("ids", {})[name] = changes[name]


class ReceiptUnavailable(Exception):
    """A step that must be durable before the launch goes on could not be recorded."""


@dataclass
class Attempt:
    """One request's hold on an attempt record. ``steps`` survive a repeat.

    ``pending`` holds step receipts not yet written; every later write of this
    attempt (a save, the heartbeat, ``finish``) records them first.
    """

    identity: str
    key: str
    kind: str
    token: str
    record: dict[str, Any] = field(default_factory=dict)
    pending: dict[str, Any] = field(default_factory=dict)
    lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)

    @property
    def op_key(self) -> str:
        return operation_key(self.identity, self.key, self.record.get("generation"))

    @property
    def steps(self) -> dict[str, Any]:
        return self.record.setdefault("steps", {})

    def _save(self, changes: dict[str, Any]) -> str:
        """``"ok"``, ``"lost"`` (another request holds the attempt) or ``"error"``."""
        with self.lock:
            self.pending.update(changes)
            self.steps.update(changes)

        def change(_conn, current: dict[str, Any]) -> None:
            now = _now()
            current["lease_until"] = _lease_until(now)
            current["updated_at"] = _iso(now)

        try:
            if _held_update(self, change):
                return "ok"
        except sqlite3.Error:
            logger.warning("launch attempt %s: step receipt not written", self.kind, exc_info=True)
            return "error"
        logger.warning("launch attempt %s: hold lost; step not recorded", self.kind)
        with self.lock:
            self.steps.update(changes)
        return "lost"

    def save(self, **changes: Any) -> bool:
        """Record step results (ids, flags) and renew the lease, as the holder only.

        A lost hold, or a database error, records nothing and returns False; it
        never fails the request, whose work may already be done (review #1521,
        item 1; task #2509). The receipts stay pending: the next write records them.
        """
        return self._save(changes) == "ok"

    async def asave(self, **changes: Any) -> bool:
        return await anyio.to_thread.run_sync(lambda: self.save(**changes))

    def require(self, **changes: Any) -> None:
        """Record a step that must be durable before anything irreversible follows.

        Raises ``LaunchBusy`` when another request holds the attempt and
        ``ReceiptUnavailable`` when the database could not record it: either way
        the caller launches nothing (review #1521 pass 3, item 1).
        """
        outcome = self._save(changes)
        if outcome == "lost":
            raise LaunchBusy("this launch is already in progress")
        if outcome == "error":
            raise ReceiptUnavailable("the launch could not be recorded; retry with the same key")

    async def arequire(self, **changes: Any) -> None:
        await anyio.to_thread.run_sync(lambda: self.require(**changes))

    def renew(self) -> bool:
        """Push the lease forward (the heartbeat), as the holder only."""

        def change(_conn, current: dict[str, Any]) -> None:
            current["lease_until"] = _lease_until(_now())

        return _held_update(self, change)


# --- what proves a launch -----------------------------------------------------------


def _bound_run(conn, op_key: str) -> dict[str, Any] | None:
    """The run bound to this attempt's operation key that reached launch, or None.

    The coordinator binds the key to the run in the claim's own transaction
    (``dispatch_launch_keys``), so this is positive proof: no other run of the
    task, keyed or not, ever counts (review #1521 pass 3, item 3).
    """
    from ..dispatch import coordinator

    bound = coordinator.launch_key_run(conn, op_key)
    return bound if bound is not None and bound["launched"] else None


def _evidence(conn, record: dict[str, Any], identity: str, key: str) -> dict[str, Any] | None:
    """The proven launch of an attempt, as ``{ids..., "answer": (status, body)}``, or None."""
    ids = dict(record.get("ids") or {})
    op_key = operation_key(identity, key, record.get("generation"))
    if record.get("kind") in _NODE_KINDS:
        from .. import node_queue

        item = node_queue.item_for_launch_key(op_key)
        if item is None:
            return None
        ids["item_id"] = item["item_id"]
        return {"ids": ids, "answer": (201, {**item, "reconciled": True})}
    bound = _bound_run(conn, op_key)
    if bound is None:
        return None
    ids = _bound_ids(ids, bound)
    return {"ids": ids, "answer": _dispatch_answer(record.get("kind"), ids)}


def _bound_ids(ids: dict[str, Any], bound: dict[str, Any]) -> dict[str, Any]:
    """The attempt's ids, with the task, delegation and run of its bound run."""
    out = {**ids, "run_id": bound["run_id"], "task_id": bound["task_id"]}
    if bound.get("delegation_id") is not None:
        out["delegation_id"] = bound["delegation_id"]
    return out


def _dispatch_answer(kind: Any, ids: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """The canonical answer of a launch proven from its run (``reconciled: true``)."""
    if kind == "stack":
        body = {
            "next_task_id": ids.get("task_id"),
            "dispatch": {"launched": True, "reason": "ok", "run_id": ids["run_id"]},
            "reconciled": True,
        }
        return 202, body
    return 202, {
        "status": "running",
        "task_id": ids.get("task_id"),
        "delegation_id": ids.get("delegation_id"),
        "run_id": ids["run_id"],
        "session_id": ids["run_id"],
        "reconciled": True,
    }


def _mark_started(record: dict[str, Any], ids: dict[str, Any], answer: tuple) -> None:
    status, body = answer
    record.update(
        state="started",
        ids=ids,
        owner=None,
        reason=None,
        response={"status_code": status, "body": body},
        updated_at=_iso(_now()),
    )


def _unsettled_run(conn, record: dict[str, Any], identity: str, key: str) -> dict | None:
    """The attempt's bound run whose spawn started without a recorded outcome, or None."""
    if record.get("kind") in _NODE_KINDS:
        return None
    from ..dispatch import coordinator

    op_key = operation_key(identity, key, record.get("generation"))
    bound = coordinator.launch_key_run(conn, op_key)
    return bound if bound is not None and bound["spawn_unsettled"] else None


def _mark_unsettled(record: dict[str, Any], bound: dict[str, Any]) -> None:
    record.update(
        state="unsettled",
        ids=_bound_ids(record.get("ids") or {}, bound),
        owner=None,
        reason=UNSETTLED_REASON,
        updated_at=_iso(_now()),
    )


def reconcile(conn, record: dict[str, Any], identity: str, key: str) -> bool:
    """Settle an attempt from its evidence (the module table).

    Runs inside the caller's ``BEGIN IMMEDIATE``; shared by ``begin`` (a repeat
    POST) and ``lookup`` (GET). Returns whether the record changed (the caller
    writes it). A lapsed ``in_progress`` attempt becomes ``started`` (launch
    proven), ``unsettled`` (its spawn started without a recorded outcome) or
    ``failed``; a ``failed`` one becomes ``started`` (a lost receipt, task #2509)
    or ``unsettled``; an ``unsettled`` one becomes ``started`` once its spawn
    is recorded, or ``failed`` once the operator settled it (review #1521 pass
    5). A live hold, and every other state, is left as it is.
    """
    state = record.get("state")
    if state == "in_progress" and _held_live(record, _now()):
        return False
    if state not in ("in_progress", "failed", "unsettled"):
        return False
    proven = _evidence(conn, record, identity, key)
    if proven is not None:
        _mark_started(record, proven["ids"], proven["answer"])
        return True
    unsettled = _unsettled_run(conn, record, identity, key)
    if unsettled is not None:
        if state == "unsettled":
            return False
        _mark_unsettled(record, unsettled)
        return True
    if state == "failed":
        return False
    reason = INTERRUPTED_REASON if state == "in_progress" else SETTLED_REASON
    record.update(state="failed", owner=None, reason=reason, updated_at=_iso(_now()))
    return True


def _recorded_task_gone(conn, record: dict[str, Any]) -> bool:
    task_id = (record.get("ids") or {}).get("task_id")
    if record.get("kind") in _NODE_KINDS or task_id is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM tasks WHERE id=? AND org_id IS ?", (task_id, edition.tenant_org_id())
    ).fetchone()
    return row is None


# --- the request side ---------------------------------------------------------------


def begin(identity: str, key: str, kind: str, digest: str) -> Attempt | dict[str, Any]:
    """Hold the attempt for this request, or return its recorded answer.

    Reconciles first (the module table), so a lapsed attempt whose launch is proven
    replays instead of running again. Returns an ``Attempt`` the caller runs (a new
    attempt, or a parked, failed or interrupted one it continues), or the stored
    ``{"status_code", "body"}`` of a started launch. Raises ``ConflictError`` for a
    key used with another request or a started launch whose task is gone,
    ``LaunchBusy`` while a live request holds it, and
    ``storage.AmbiguousIdempotencyReceipt`` for a record written before tenant
    scoping.
    """
    now = _now()
    with closing(storage.connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            record = _read(conn, identity, key)
            if record is None:
                _prune(conn, now)
                record = {
                    "identity": identity,
                    "kind": kind,
                    "digest": digest,
                    "generation": secrets.token_hex(16),
                    "steps": {},
                    "ids": {},
                    "created_at": _iso(now),
                }
            elif record.get("kind") != kind or record.get("digest") != digest:
                raise ConflictError(
                    "Idempotency-Key was already used for a different launch; "
                    "use a new key for a new launch"
                )
            elif reconcile(conn, record, identity, key):
                _write(conn, identity, key, record)
            if record.get("state") == "started":
                if _recorded_task_gone(conn, record):
                    raise ConflictError(
                        "this launch's task no longer exists; start a new launch with a new key"
                    )
                conn.commit()
                return record["response"]
            if record.get("state") == "unsettled":
                conn.commit()  # keep what reconcile recorded
                raise LaunchUnsettled((record.get("ids") or {}).get("run_id"))
            if record.get("state") == "in_progress" and record.get("owner"):
                raise LaunchBusy("this launch is already in progress")
            record.update(
                state="in_progress",
                owner=secrets.token_hex(16),
                lease_until=_lease_until(now),
                updated_at=_iso(now),
                reason=None,
            )
            _write(conn, identity, key, record)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return Attempt(identity, key, kind, record["owner"], record)


def _run_id_of(kind: str, body: dict[str, Any]) -> str | None:
    if kind == "stack":
        dispatch = body.get("dispatch") if isinstance(body.get("dispatch"), dict) else {}
        run_id = dispatch.get("run_id")
    else:
        run_id = body.get("run_id")
    return str(run_id) if run_id else None


def _not_launched_reason(kind: str, body: dict[str, Any]) -> str:
    source = body.get("dispatch") if kind == "stack" else body
    source = source if isinstance(source, dict) else {}
    return str(source.get("error") or source.get("reason") or "agent_did_not_start")[:300]


def finish(attempt: Attempt, response: Response | None) -> Response | None:
    """Record how the request ended, as the holder only; return the answer to send.

    2xx with a parked approval → ``parked``; a device launch's 2xx → ``started``.
    A dispatch or stack request whose key-bound run reached launch (the module
    table) → ``started``, whatever the route answered: with the route's answer when
    it reported that very run launched, else the canonical one (an
    ``already_claimed`` answer, or an error raised after the run committed, task
    #2509). Otherwise a dispatch or stack 2xx → ``failed`` with the start's reason;
    a 4xx → ``failed`` with the route's error text; anything else, including an
    exception (``response`` None) → ``failed`` with ``INTERNAL_REASON``. Pending
    step receipts are recorded first. A lost hold records nothing and changes no
    answer; the caller treats a database error here the same way.
    """
    body: Any = None
    if response is not None:
        try:
            body = json.loads(bytes(response.body))
        except ValueError:
            body = None
    body_dict = body if isinstance(body, dict) else {}
    status = response.status_code if response is not None else 500
    kind = attempt.kind
    outcome: dict[str, Any] = {}

    def change(conn, current: dict[str, Any]) -> None:
        now = _iso(_now())
        ok = 200 <= status < 300
        if ok and body_dict.get("code") == "approval_required":
            if body_dict.get("approval_id") is not None:
                current.setdefault("ids", {})["approval_id"] = body_dict["approval_id"]
            current.update(state="parked", reason=None, owner=None, updated_at=now)
            return
        if ok and kind in _NODE_KINDS:
            ids = current.setdefault("ids", {})
            if body_dict.get("item_id"):
                ids["item_id"] = body_dict["item_id"]
            _mark_started(current, dict(ids), (status, body))
            return
        bound = None if kind in _NODE_KINDS else _bound_run(conn, attempt.op_key)
        if bound is not None:
            ids = _bound_ids(current.get("ids") or {}, bound)
            launched = ok and (
                body_dict.get("dispatch", {}).get("launched") is True
                if kind == "stack"
                else body_dict.get("status") == "running"
            )
            if launched and _run_id_of(kind, body_dict) == bound["run_id"]:
                _mark_started(current, ids, (status, body))
            else:
                outcome["answer"] = _dispatch_answer(kind, ids)
                _mark_started(current, ids, outcome["answer"])
        elif kind not in _NODE_KINDS and (
            unsettled := _unsettled_run(conn, current, attempt.identity, attempt.key)
        ):
            _mark_unsettled(current, unsettled)  # its spawn may still be running
        elif ok:
            reason = _not_launched_reason(kind, body_dict)
            current.update(state="failed", reason=reason, owner=None, updated_at=now)
        elif 400 <= status < 500:
            reason = str(body_dict.get("error") or "the launch was refused")[:300]
            current.update(state="failed", reason=reason, owner=None, updated_at=now)
        else:
            current.update(state="failed", reason=INTERNAL_REASON, owner=None, updated_at=now)

    if not _held_update(attempt, change):
        return response
    if "answer" in outcome:
        answer_status, answer_body = outcome["answer"]
        return routes._json(answer_body, status=answer_status)
    return response


def lookup(identity: str, key: str) -> dict[str, Any]:
    """The attempt as ``GET /api/launch-attempts/{key}`` reports it (reconciling first)."""
    with closing(storage.connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            record = _read(conn, identity, key)
            if record is not None and reconcile(conn, record, identity, key):
                _write(conn, identity, key, record)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    if record is None:
        return {"key": key, "state": "unknown"}
    ids = record.get("ids") or {}
    out: dict[str, Any] = {
        "key": key,
        "state": record.get("state"),
        "kind": record.get("kind"),
        **{name: ids.get(name) for name in _ID_FIELDS},
        "session_id": None,
        "reason": record.get("reason"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }
    if out["item_id"]:
        out["session_id"] = _registry_session(str(out["item_id"]))
    return out


def _registry_session(item_id: str) -> str | None:
    from .. import node_queue

    item = node_queue.get_item(item_id)
    if item is None:
        return None
    rows = [{"id": item["id"], "session_id": item.get("session_id")}]
    node_queue.annotate_registry_sessions(rows)
    return rows[0]["agent_session_id"]


# --- the route side -----------------------------------------------------------------


def _unsettled(run_id: str | None) -> Response:
    return routes._json(
        {"error": UNSETTLED_REASON, "code": "launch_unsettled", "run_id": run_id}, status=409
    )


async def settle_spawn(request: Request) -> Response:
    """POST /api/dispatch-runs/{run_id}/settle-spawn — the operator's recovery (pass 5).

    Records an unsettled keyed spawn as ``failed`` only once the coordinator has
    proven its launcher and its job gone (``settle_unsettled_spawn``); otherwise
    409 with the reason. The launch attempt then reads ``failed`` and a repeat
    with its key continues it.
    """
    guard = routes._guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return routes._json({"error": "authenticated identity required"}, status=401)
    if not routes._operator_recovery(request):
        return routes._json({"error": "settling a spawn is an operator action"}, status=403)
    from ..dispatch import coordinator

    run_id = request.path_params["run_id"]
    result = await anyio.to_thread.run_sync(
        lambda: coordinator.settle_unsettled_spawn(run_id, actor=identity)
    )
    if not result["settled"]:
        return routes._json({**result, "code": "spawn_not_settled"}, status=409)
    return routes._json(result)


settle_spawn.identity_args = ()  # type: ignore[attr-defined]


def _busy() -> Response:
    return routes._json(
        {
            "error": "this launch is already in progress; look it up or retry shortly",
            "code": "launch_in_progress",
        },
        status=409,
    )


def _heartbeat(attempt: Attempt, stop: threading.Event) -> None:
    """Renew the lease every third of ``LEASE_S`` until ``stop`` (or the hold is lost)."""
    while not stop.wait(LEASE_S / 3):
        try:
            if not attempt.renew():
                return
        except sqlite3.Error:  # a busy database: try again next beat
            logger.warning("launch attempt heartbeat could not renew; retrying")


async def keyed_launch(
    request: Request,
    identity: str,
    kind: str,
    normalized: dict[str, Any] | Callable[[], dict[str, Any]],
    run: Callable[[Attempt | None], Awaitable[Response]],
) -> Response:
    """Run one launch under its ``Idempotency-Key``, or exactly as before without one.

    Call it after the route's own guards (scope, CSRF, identity) and body
    validation, so a recorded answer is only ever returned to a caller who could
    make the request now. ``normalized`` (the validated request the key is bound
    to) may be a callable; it is only evaluated when a key was sent. While ``run``
    works, a heartbeat thread keeps the attempt's lease in the database.
    """
    raw = request.headers.get("idempotency-key")
    if raw is None:
        return await run(None)
    try:
        key = validate_key(raw)
    except ValueError as exc:
        return routes._json({"error": str(exc)}, status=400)
    digest = request_digest(kind, normalized() if callable(normalized) else normalized)
    try:
        held = await anyio.to_thread.run_sync(lambda: begin(identity, key, kind, digest))
    except LaunchBusy:
        return _busy()
    except LaunchUnsettled as exc:
        return _unsettled(exc.run_id)
    except (ConflictError, storage.AmbiguousIdempotencyReceipt) as exc:
        return routes._json({"error": str(exc)[:300], "code": "conflict"}, status=409)
    if isinstance(held, dict):
        return routes._json(held["body"], status=held["status_code"])
    stop = threading.Event()
    beat = threading.Thread(
        target=_heartbeat, args=(held, stop), name="cc-launch-heartbeat", daemon=True
    )
    with _LIVE_LOCK:
        _LIVE_OWNERS.add(held.token)
    beat.start()
    try:
        try:
            response = await run(held)
        except LaunchBusy:
            # This request lost its hold before launching: the holder answers.
            stop.set()
            return _busy()
        except ReceiptUnavailable as exc:
            stop.set()
            with anyio.CancelScope(shield=True):
                await _finish_quietly(held, None)
            return routes._json({"error": str(exc), "code": "launch_unavailable"}, status=503)
        except Exception:
            stop.set()
            with anyio.CancelScope(shield=True):
                answer = await _finish_quietly(held, None)
            if answer is not None:  # the launch is proven: answer it (task #2509)
                return answer
            raise
        except BaseException:
            # Cancelled: record nothing; the lease lapses and a reader reconciles.
            stop.set()
            raise
        stop.set()
        with anyio.CancelScope(shield=True):
            answer = await _finish_quietly(held, response)
        return answer if answer is not None else response
    finally:
        with _LIVE_LOCK:
            _LIVE_OWNERS.discard(held.token)


async def _finish_quietly(attempt: Attempt, response: Response | None) -> Response | None:
    """``finish``, except a database error leaves the answer to the evidence (#2509).

    The record stays ``in_progress`` until its lease lapses; the next reader then
    settles it from the evidence of the steps recorded so far.
    """
    try:
        return await anyio.to_thread.run_sync(lambda: finish(attempt, response))
    except sqlite3.Error:
        logger.warning("launch attempt %s: final receipt not written", attempt.kind, exc_info=True)
        return response


async def launch_attempt(request: Request) -> Response:
    """GET /api/launch-attempts/{key} — the caller's launch attempt in its tenant."""
    guard = routes._guard(request)
    if guard is not None:
        return guard
    identity = routes._writer_identity(request)
    if identity is None:
        return routes._json({"error": "authenticated identity required"}, status=401)
    try:
        key = validate_key(request.path_params["key"])
    except ValueError as exc:
        return routes._json({"error": str(exc)}, status=400)
    try:
        payload = await anyio.to_thread.run_sync(lambda: lookup(identity, key))
    except (ConflictError, storage.AmbiguousIdempotencyReceipt) as exc:
        return routes._json({"error": str(exc)[:300], "code": "conflict"}, status=409)
    return routes._json(payload)


launch_attempt.identity_args = ()  # type: ignore[attr-defined]


ROUTES: list[Route] = [
    Route("/api/launch-attempts/{key}", launch_attempt, methods=["GET"]),
    Route("/api/dispatch-runs/{run_id}/settle-spawn", settle_spawn, methods=["POST"]),
]
