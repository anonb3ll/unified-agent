"""The lease reaper writes only after the owner probe, and only sometimes (#2395).

Ground truth, against a real SQLite store:
- a stale lease whose owner is active is not released;
- a stale lease whose owner is unknown is not released, and the warning is recorded;
- a stale lease whose owner is terminal or absent goes back to open only when
  the recorded run id still matches, and the release does not start another run;
- a closed task is never selected, so it keeps who closed it;
- a mismatched run id is refused and the claim stays;
- a lease that has not lapsed is not a candidate and is not written.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import config, lease_reaper, owner_liveness, run_records, storage
from unified_agent.lease_reaper import KEPT, REFUSED, RELEASED, ReapResult, reap_stale_claim_leases
from unified_agent.owner_liveness import ABSENT, ACTIVE, TERMINAL, UNKNOWN
from unified_agent.tools.tasks import task_add, task_claim, task_update

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
OWNER = "Desktop_Agent2"
RUN = "run-1"


def _reap(**kwargs) -> tuple[ReapResult, ...]:
    conn = storage.connect()
    try:
        results = reap_stale_claim_leases(conn, now=NOW, **kwargs)
        conn.commit()
        return results
    finally:
        conn.close()


def _seen(at: datetime) -> None:
    stamp = NOW.isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agents (label, kind, known, first_seen, last_seen, last_seen_via, "
            "created_at, updated_at) VALUES (?, 'agent', 1, ?, ?, 'heartbeat', ?, ?)",
            (OWNER, stamp, at.isoformat(), stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()


def _lease(
    *,
    status: str = "doing",
    lease: datetime | None,
    run_id: str | None = RUN,
    owner: str | None = OWNER,
    cancel_requested: int = 0,
) -> int:
    task = task_add("owned work")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status=?, claimed_by=?, lock_expires_at=?, current_run_id=?, "
            "current_run_agent=?, cancel_requested=?, next_owner=?, updated_at=? WHERE id=?",
            (
                status,
                owner,
                None if lease is None else lease.isoformat(),
                run_id,
                owner if run_id else None,
                cancel_requested,
                owner,
                NOW.isoformat(),
                task["id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return int(task["id"])


def _row(task_id: int) -> tuple:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT status, claimed_by, claimed_at, lock_expires_at, current_run_id, "
            "current_run_agent, updated_at, next_owner, ownership_generation, notes "
            "FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return tuple(row)


def _counts() -> tuple[int, int, int]:
    """Tasks, self-pickup claims, and retryable dispatch runs."""
    conn = storage.connect()
    try:
        tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        claims = conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE json_extract(payload, '$.kind')=?",
            ("selfpickup.claimed",),
        ).fetchone()[0]
        retryable = conn.execute(
            "SELECT COUNT(*) FROM dispatch_runs WHERE state='retryable'"
        ).fetchone()[0]
    finally:
        conn.close()
    return (tasks, claims, retryable)


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_a_closed_task_is_not_selected_and_keeps_who_closed_it(status):
    _seen(NOW)
    task_id = _lease(status=status, lease=NOW - timedelta(hours=1))
    before = _row(task_id)
    assert _reap() == ()
    assert _row(task_id) == before
    assert before[1] == OWNER


def test_a_stale_lease_with_an_active_owner_is_not_released():
    _seen(NOW)
    task_id = _lease(lease=NOW - timedelta(minutes=5))
    before = _row(task_id)
    assert _reap() == (
        ReapResult(task_id=task_id, owner=OWNER, outcome=ACTIVE, action=KEPT, warning=None),
    )
    assert _row(task_id) == before


def test_a_stale_lease_with_an_unknown_owner_is_kept_and_warned(caplog):
    _seen(NOW - timedelta(days=2))
    task_id = _lease(lease=NOW - timedelta(minutes=5))
    before = _row(task_id)
    with caplog.at_level(logging.WARNING, logger="unified_agent.owner_liveness"):
        results = _reap()
    assert [(r.task_id, r.outcome, r.action) for r in results] == [(task_id, UNKNOWN, KEPT)]
    assert "owner kept" in results[0].warning
    assert "candidate" in results[0].warning
    assert any("owner kept" in rec.message for rec in caplog.records)
    assert _row(task_id) == before


def test_a_recorded_end_returns_the_task_to_open_even_with_the_owner_online():
    _seen(NOW)
    task_id = _lease(lease=NOW - timedelta(hours=1), cancel_requested=1)
    generation = _row(task_id)[8]
    before_counts = _counts()
    results = _reap()
    assert results == (
        ReapResult(task_id=task_id, owner=OWNER, outcome=TERMINAL, action=RELEASED, warning=None),
    )
    assert results[0].title == "owned work"
    after = _row(task_id)
    assert (after[0], after[1], after[2], after[3]) == ("open", None, None, None)
    assert after[7] is None and after[8] == generation + 1
    assert _counts() == before_counts
    assert _reap() == ()
    assert _row(task_id) == after


def test_an_absent_owner_releases():
    task_id = _lease(lease=NOW - timedelta(minutes=5))
    assert _reap() == (
        ReapResult(task_id=task_id, owner=OWNER, outcome=ABSENT, action=RELEASED, warning=None),
    )
    assert _row(task_id)[:2] == ("open", None)


def test_a_lapsed_lease_with_no_claimant_is_released():
    task_id = _lease(lease=NOW - timedelta(minutes=5), run_id=None, owner=None)
    assert [(r.task_id, r.action) for r in _reap()] == [(task_id, RELEASED)]
    assert _row(task_id)[:2] == ("open", None)


def test_a_mismatched_run_id_is_refused_and_the_claim_stays(monkeypatch):
    task_id = _lease(lease=NOW - timedelta(hours=1), cancel_requested=1)
    before = _row(task_id)
    real = owner_liveness.probe_owner

    def replaced(conn, owner, *, task_id=None, now=None):
        probed = real(conn, owner, task_id=task_id, now=now)
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", ("run-replaced", task_id))
        return probed

    monkeypatch.setattr(owner_liveness, "probe_owner", replaced)
    results = _reap()
    assert [(r.outcome, r.action) for r in results] == [(TERMINAL, REFUSED)]
    after = _row(task_id)
    assert after[:4] == before[:4]
    assert after[1] == OWNER
    assert after[4] == "run-replaced"


def test_a_live_or_missing_lease_is_not_a_candidate():
    future = _lease(lease=NOW + timedelta(hours=1), run_id="run-future")
    open_ended = _lease(lease=None, run_id="run-open")
    future_before, open_before = _row(future), _row(open_ended)
    assert _reap() == ()
    assert _row(future) == future_before
    assert _row(open_ended) == open_before


def test_skip_and_notes_hooks_control_the_release():
    skipped = _lease(lease=NOW - timedelta(minutes=5))
    declined = _lease(lease=NOW - timedelta(minutes=5))
    marked = _lease(lease=NOW - timedelta(minutes=5))

    def notes_for(candidate):
        return None if candidate.task_id == declined else "released-marker"

    results = _reap(skip_task_ids=frozenset({skipped}), notes_for=notes_for)
    assert [r.task_id for r in results] == [marked]
    assert _row(skipped)[0] == _row(declined)[0] == "doing"
    assert _row(marked)[0] == "open" and _row(marked)[9] == "released-marker"


def _claimed_with_open_run(*, cancel_requested: int = 0) -> int:
    """A real claim (so a real open self-pickup run), lapsed, with no trace of the owner."""
    task_id = int(task_add("accepted work")["id"])
    claim = task_claim(task_id, agent=OWNER)
    assert claim["current_run_id"]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET lock_expires_at=?, cancel_requested=? WHERE id=?",
            ((NOW - timedelta(minutes=5)).isoformat(), cancel_requested, task_id),
        )
        conn.execute("DELETE FROM agents WHERE label=?", (OWNER,))
        conn.commit()
    finally:
        conn.close()
    return task_id


def _witnesses(task_id: int) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload, originator, dedupe_key FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.task_id')=?",
            (lease_reaper.CHANNEL_RUN_STALLED, task_id),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def test_an_open_run_left_by_a_gone_owner_is_held_as_interrupted_not_reopened():
    task_id = _claimed_with_open_run()
    run_id = _row(task_id)[4]
    before_counts = _counts()
    results = _reap()
    assert [(r.outcome, r.action, r.interrupted_run, r.to_status) for r in results] == [
        (ABSENT, RELEASED, run_id, "blocked")
    ]
    after = _row(task_id)
    assert (after[0], after[1], after[3]) == ("blocked", None, None)
    assert f"[interrupted: run={run_id} owner={OWNER} probe=absent" in after[9]
    witness = _witnesses(task_id)
    assert len(witness) == 1
    assert witness[0]["originator"] == "lease-reaper"
    assert witness[0]["dedupe_key"] == f"run-interrupted:{run_id}"
    assert '"kind": "run.interrupted"' in witness[0]["payload"]
    assert _counts() == before_counts
    assert _reap() == ()
    assert len(_witnesses(task_id)) == 1


def test_a_requested_cancel_is_a_recorded_stop_and_reopens_without_a_witness():
    task_id = _claimed_with_open_run(cancel_requested=1)
    results = _reap()
    assert [(r.action, r.interrupted_run, r.to_status) for r in results] == [
        (RELEASED, None, "open")
    ]
    assert _row(task_id)[0] == "open"
    assert _witnesses(task_id) == []


def test_a_rolled_back_release_leaves_no_witness():
    task_id = _claimed_with_open_run()
    conn = storage.connect()
    try:
        results = reap_stale_claim_leases(conn, now=NOW)
        assert results[0].interrupted_run
        conn.rollback()
    finally:
        conn.close()
    assert _row(task_id)[0] == "doing"
    assert _witnesses(task_id) == []
    assert run_records.open_run_on(storage.connect(), task_id) is not None


def _delegation(task_id: int, status: str) -> None:
    """One delegation of ``task_id`` to its owner, in ``status`` (replacing any earlier one)."""
    stamp = NOW.isoformat()
    conn = storage.connect()
    try:
        conn.execute("DELETE FROM delegations WHERE task_id=?", (task_id,))
        conn.execute(
            "INSERT INTO delegations (from_agent, to_agent, task_id, task_spec, callback, status, "
            "created_at, updated_at) VALUES ('Desktop_Agent1', ?, ?, 'retry', 'none', ?, ?, ?)",
            (OWNER, task_id, status, stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()


def _hold(task_id: int) -> str | None:
    conn = storage.connect()
    try:
        return lease_reaper.interrupted_hold(conn, task_id)
    finally:
        conn.close()


def test_ended_sessions_hold_interrupted_work_even_with_a_delegation_pending():
    """#2482: a delegation still queued does not stop the hold, and the hold outlasts it."""
    task_id = _claimed_with_open_run()
    run_id = _row(task_id)[4]
    _seen(NOW - timedelta(days=2))
    stamp = (NOW - timedelta(hours=1)).isoformat()
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET claimed_at=? WHERE id=?", (stamp, task_id))
        conn.execute(
            "INSERT INTO agent_sessions (id, agent, kind, status, started_at, "
            "last_heartbeat_at, ended_at) VALUES ('s0', ?, 'interactive', 'abandoned', ?, ?, ?)",
            (OWNER, stamp, stamp, (NOW - timedelta(minutes=10)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    _delegation(task_id, "pending")
    assert [(r.outcome, r.action, r.interrupted_run, r.to_status) for r in _reap()] == [
        (TERMINAL, RELEASED, run_id, "blocked")
    ]
    assert _row(task_id)[:2] == ("blocked", None)
    held = _hold(task_id)
    assert held and run_id in held
    # The queued delegation changes nothing about the hold, pending or gone.
    _delegation(task_id, "cancelled")
    assert _hold(task_id) == held


def test_the_interrupted_hold_lasts_until_an_explicit_claim_or_status_change():
    task_id = _claimed_with_open_run()
    run_id = _row(task_id)[4]
    assert _hold(task_id) is None
    _reap()
    held = _hold(task_id)
    assert f"task {task_id} is held as interrupted work" in held and run_id in held
    conn = storage.connect()
    try:
        assert lease_reaper.interrupted_holds(conn, [task_id, task_id + 1]) == {task_id: held}
    finally:
        conn.close()

    task_update(task_id, status="open", worktree=False)
    assert _hold(task_id) is None
    task_update(task_id, status="blocked", worktree=False)
    assert _hold(task_id) == held  # put back unresumed, it is the same interrupted work

    resumed = task_claim(task_id, agent="Desktop_Agent1")
    assert resumed["status"] == "doing" and resumed["current_run_id"] != run_id
    assert _hold(task_id) is None
    # Blocked again after a resume is a new decision: the old marker is history.
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        task_update(task_id, status="blocked", release_claim=True, worktree=False)
    finally:
        config.agent_identity_var.reset(token)
    assert _row(task_id)[:2] == ("blocked", None) and "[interrupted: run=" in _row(task_id)[9]
    assert _hold(task_id) is None
