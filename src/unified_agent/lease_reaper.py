"""The one place a lapsed claim is released (#2395).

A stale ``lock_expires_at`` on a ``doing`` task is a candidate, nothing more.
:func:`owner_liveness.probe_owner` is the read that decides:

* ``active`` keeps the owner;
* ``unknown`` keeps the owner and reports the probe's warning;
* ``terminal`` (a recorded end of the owner's run) or ``absent`` (no trace of the
  owner anywhere) releases the claim, in one update fenced on the claimant, the
  recorded run id and the expiry that were read. A mismatch matches no row and
  the claim stays.

Released work goes one of two ways:

* **open** when nothing accepted is left unresolved: the owner's run already
  has a recorded end, a cancel was requested, or no run was open;
* **interrupted** when the owner's self-pickup run is still open with no finish
  and no cancel request: the work was accepted and nobody recorded how it
  ended. The task goes to ``blocked`` (not ``open``), so no executor or
  dispatcher picks it up again on its own, with ``waiting_on`` and a notes
  marker saying so, and the same transaction appends one ``run.interrupted``
  event on ``agent.run.stalled`` (deduplicated per run), the channel the
  supervisor's stall reconciler drains. Nothing here replays work.

That ``blocked``, unowned state is a hold (task #2482), and
:func:`interrupted_hold` is its one reader. It lasts while the task stays
``blocked`` and unclaimed and the interrupted run is still its newest run.
Queued work never ends it: ``task_holds.claim_block``, the check every
admission path already makes, refuses a held task, and a delegation of one is
neither listed as claimable nor claimable. Three explicit acts end it: a
``task_claim`` by an agent with no delegation queued for the task (it opens a
newer run, so the work is resumed by someone who looked), a status change to
``open`` (queued work may run again), or closing the task.

A closed task is never selected: ``claimed_by`` on a done or cancelled task
records who closed it, and that is history rather than a claim.

Both sweeps that used to release a lapsed lease on their own, queue hygiene and
the doctor's stale-lock reclaim, call this module, so ``claimed_by`` has one
lease-expiry writer. The caller owns the transaction (this module takes the
writer lock when there is a candidate, and never commits). After its commit the
caller records the abandoned run and publishes each result's ``publish``
payload, as it did before.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import events, owner_liveness, run_records, storage, task_liveness

KEPT = "kept"
RELEASED = "released"
REFUSED = "refused"

CHANNEL_RUN_STALLED = "agent.run.stalled"
KIND_RUN_INTERRUPTED = "run.interrupted"
ORIGINATOR = "lease-reaper"
INTERRUPTED_MARKER = "[interrupted: run="
_MARKER = re.compile(re.escape(INTERRUPTED_MARKER) + r"(\S+) owner=(\S+) ")
_CHUNK = 500

_CANDIDATE_SQL = (
    "SELECT id, title, claimed_by, claimed_at, lock_expires_at, current_run_id, "
    "cancel_requested, notes FROM tasks "
    "WHERE status='doing' AND lock_expires_at IS NOT NULL ORDER BY id"
)


@dataclass(frozen=True)
class Candidate:
    """A ``doing`` row whose lease has lapsed. ``owner`` is empty for an unowned row."""

    task_id: int
    title: str
    owner: str
    claimed_at: str | None
    lock_expires_at: str
    run_id: str | None
    notes: str | None
    cancel_requested: bool = False


@dataclass(frozen=True)
class ReapResult:
    """What the reaper did with one stale lease. ``action`` is kept, released or refused.

    ``interrupted_run`` names the open run a release left interrupted (the task
    is then ``blocked``); ``publish`` is that witness event's post-commit
    payload, or ``None``.
    """

    task_id: int
    owner: str
    outcome: str
    action: str
    warning: str | None
    title: str = field(default="", compare=False)
    expired_at: str = field(default="", compare=False)
    interrupted_run: str | None = field(default=None, compare=False)
    publish: dict[str, Any] | None = field(default=None, compare=False)

    @property
    def to_status(self) -> str:
        """Where a release left the task."""
        return "blocked" if self.interrupted_run else "open"


def _reference(now: datetime | None) -> datetime:
    reference = now or datetime.now(UTC)
    return reference if reference.tzinfo is not None else reference.replace(tzinfo=UTC)


def stale_candidates(conn: sqlite3.Connection, now: datetime) -> tuple[Candidate, ...]:
    """Lapsed leases on ``doing`` tasks. A missing or future expiry is not selected."""
    found: list[Candidate] = []
    for row in conn.execute(_CANDIDATE_SQL):
        lease = task_liveness.parse_ts(row["lock_expires_at"])
        if lease is None or lease > now:
            continue
        found.append(
            Candidate(
                task_id=int(row["id"]),
                title=str(row["title"] or ""),
                owner=str(row["claimed_by"] or "").strip(),
                claimed_at=row["claimed_at"],
                lock_expires_at=str(row["lock_expires_at"]),
                run_id=row["current_run_id"],
                notes=row["notes"],
                cancel_requested=bool(row["cancel_requested"]),
            )
        )
    return tuple(found)


def reap_stale_claim_leases(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    skip_task_ids: frozenset[int] = frozenset(),
    notes_for: Callable[[Candidate], str | None] | None = None,
) -> tuple[ReapResult, ...]:
    """Probe each lapsed lease and release only a terminal or absent owner.

    ``now`` is the clock, shared with the probe so a fixed instant is the lease
    boundary too. ``skip_task_ids`` are left alone unprobed. ``notes_for`` returns
    the notes a release should write, or ``None`` to leave that candidate alone;
    without it the notes stay as they are. The caller commits or rolls back.
    """
    reference = _reference(now)
    if not stale_candidates(conn, reference):
        return ()
    # Re-read under the writer lock so the fence below compares against what is
    # true now; the common nothing-lapsed pass never takes the lock.
    storage.begin_task_write(conn)
    results: list[ReapResult] = []
    for candidate in stale_candidates(conn, reference):
        if candidate.task_id in skip_task_ids:
            continue
        notes = notes_for(candidate) if notes_for is not None else candidate.notes
        if notes_for is not None and notes is None:
            continue
        results.append(_reap_one(conn, candidate, reference, notes))
    return tuple(results)


def _reap_one(
    conn: sqlite3.Connection, candidate: Candidate, now: datetime, notes: str | None
) -> ReapResult:
    if not candidate.owner:
        # A lease with no claimant has nobody to protect.
        return _release(conn, candidate, owner_liveness.ABSENT, notes)
    probe = owner_liveness.probe_owner(conn, candidate.owner, task_id=candidate.task_id, now=now)
    outcome = probe.outcome
    if outcome in (owner_liveness.ACTIVE, owner_liveness.UNKNOWN):
        return _result(candidate, outcome, KEPT, probe.warning)
    if outcome in (owner_liveness.TERMINAL, owner_liveness.ABSENT):
        return _release(conn, candidate, outcome, notes)
    raise RuntimeError(f"unknown owner probe outcome {outcome!r}")


def _result(
    candidate: Candidate,
    outcome: str,
    action: str,
    warning: str | None,
    *,
    interrupted_run: str | None = None,
    publish: dict[str, Any] | None = None,
) -> ReapResult:
    return ReapResult(
        task_id=candidate.task_id,
        owner=candidate.owner,
        outcome=outcome,
        action=action,
        warning=warning,
        title=candidate.title,
        expired_at=candidate.lock_expires_at,
        interrupted_run=interrupted_run,
        publish=publish,
    )


def _interrupted_run(conn: sqlite3.Connection, candidate: Candidate) -> str | None:
    """The owner's still-open self-pickup run, when nothing recorded how it ended.

    A requested cancel is a recorded stop, so it is not interruption.
    """
    if not candidate.owner or candidate.cancel_requested:
        return None
    run = run_records.open_run_on(conn, candidate.task_id)
    if run is None or run.get("agent") != candidate.owner or not run.get("run_id"):
        return None
    return str(run["run_id"])


def _release(
    conn: sqlite3.Connection, candidate: Candidate, outcome: str, notes: str | None
) -> ReapResult:
    """Release the claim if what was read still holds. Never opens or replays a run."""
    interrupted = _interrupted_run(conn, candidate)
    waiting_on = None
    if interrupted:
        stamp = storage.now()
        marker = (
            f"{INTERRUPTED_MARKER}{interrupted} owner={candidate.owner} probe={outcome} @ {stamp}]"
        )
        notes = f"{(notes or '').rstrip()}\n{marker}" if notes else marker
        waiting_on = (
            f"interrupted run {interrupted}: its owner {candidate.owner} probed {outcome} "
            "with no recorded finish. Claim to resume it, reopen it to let queued work "
            "run, or close it."
        )[:500]
    updated = conn.execute(
        "UPDATE tasks SET status=?, claimed_by=NULL, claimed_at=NULL, "
        "lock_expires_at=NULL, cancel_requested=0, notes=?, updated_at=?, next_owner=NULL, "
        "waiting_on=COALESCE(?, waiting_on), ownership_generation=ownership_generation+1 "
        "WHERE id=? AND status='doing' AND claimed_by IS ? AND current_run_id IS ? "
        "AND lock_expires_at=?",
        (
            "blocked" if interrupted else "open",
            notes,
            storage.next_task_revision(conn, candidate.task_id),
            waiting_on,
            candidate.task_id,
            candidate.owner or None,
            candidate.run_id,
            candidate.lock_expires_at,
        ),
    ).rowcount
    if updated != 1:
        return _result(candidate, outcome, REFUSED, None)
    if not interrupted:
        return _result(candidate, outcome, RELEASED, None)
    _, _, publish = events.record_event_once_on_connection(
        conn,
        CHANNEL_RUN_STALLED,
        payload={
            "kind": KIND_RUN_INTERRUPTED,
            "task_id": candidate.task_id,
            "run_id": interrupted,
            "owner": candidate.owner,
            "probe": outcome,
            "ledger": "self_pickup",
            "to_status": "blocked",
            "lease_expired_at": candidate.lock_expires_at,
        },
        originator=ORIGINATOR,
        dedupe_key=f"run-interrupted:{interrupted}",
    )
    return _result(candidate, outcome, RELEASED, None, interrupted_run=interrupted, publish=publish)


def interrupted_holds(conn: sqlite3.Connection, task_ids: Iterable[Any]) -> dict[int, str]:
    """The tasks among ``task_ids`` held as interrupted work, each with the reason.

    Reads the state :func:`_release` leaves and nothing else: the task is
    ``blocked`` and unclaimed, and the run its newest ``[interrupted: run=…]``
    marker names is still the task's newest run. A later claim opens a newer
    run, so a task resumed and then blocked again for another reason is not
    held by an old marker. A task with no run on record at all (its events were
    pruned) stays held: nothing shows it was resumed.
    """
    ids = sorted({int(t) for t in task_ids if t is not None})
    held: dict[int, str] = {}
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        rows = conn.execute(
            "SELECT id, notes FROM tasks WHERE status='blocked' "
            "AND (claimed_by IS NULL OR TRIM(claimed_by)='') AND instr(notes, ?) > 0 "
            f"AND id IN ({','.join('?' * len(chunk))})",
            (INTERRUPTED_MARKER, *chunk),
        ).fetchall()
        for row in rows:
            markers = _MARKER.findall(row["notes"])
            if not markers:
                continue
            run_id, owner = markers[-1]
            task_id = int(row["id"])
            end = run_records.latest_run_end(conn, task_id)
            newest = end or run_records.open_run_on(conn, task_id) or {}
            if newest.get("run_id") not in (None, run_id):
                continue
            held[task_id] = (
                f"task {task_id} is held as interrupted work: run {run_id} of {owner} has no "
                "recorded finish, so nothing queued may pick it up. Claim it to resume it (an "
                "agent with no delegation queued for it), reopen it to let queued work run "
                "(task_update status=open), or close it"
            )
    return held


def interrupted_hold(conn: sqlite3.Connection, task_id: Any) -> str | None:
    """Why ``task_id`` takes no queued work right now, or ``None``. See the module docstring."""
    return interrupted_holds(conn, [task_id]).get(int(task_id))


def publish_after_commit(results: tuple[ReapResult, ...]) -> None:
    """Fan out the witness events of committed releases. Best effort, never raises."""
    for result in results:
        if result.publish is not None:
            events.publish(result.publish)
