"""Verification tests for the 8 findings in task #1221.

Each test is designed to prove that the pre-fix behavior exhibits the bug,
and the post-fix behavior resolves it fail-closed.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import config, storage
from unified_agent.dispatch import coordinator, launch, scheduler, supervise


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    yield tmp_path
    storage._connection = None


def _delegated_task(task_id: int = 1001, delegation_id: int = 2001) -> dict:
    conn = storage.connect()
    try:
        now = storage.now()
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, notes, created_at, updated_at) "
            "VALUES (?, 'test task', 'doing', 'med', '[plan: approved @ 2026-01-01T00:00:00Z]', ?, ?)",
            (task_id, now, now),
        )
        conn.execute(
            "INSERT INTO delegations (id, from_agent, to_agent, task_id, task_spec, callback, "
            "status, created_at, updated_at) VALUES (?, 'Desktop_Person', 'Desktop_Agent2', ?, 'spec', 'notify', "
            "'in_flight', ?, ?)",
            (delegation_id, task_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"task_id": task_id, "delegation_id": delegation_id}


def _insert_run(
    run_id: str,
    task_id: int,
    delegation_id: int,
    state: str = "launched",
    termination_outcome: str | None = None,
    failure_reason: str | None = None,
    evidence_ref: str | None = "pid:12345:2026-01-01",
    lease_expires_at: str | None = None,
) -> dict:
    conn = storage.connect()
    try:
        now = storage.now()
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, delegation_id, agent_label, state, "
            "actor, lease_owner, lease_expires_at, failure_reason, evidence_ref, "
            "termination_outcome, created_at, updated_at) "
            "VALUES (?, ?, ?, 'Desktop_Agent2', ?, 'coordinator', 'coordinator', ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                task_id,
                delegation_id,
                state,
                lease_expires_at,
                failure_reason,
                evidence_ref,
                termination_outcome,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return coordinator.get_run(run_id)


# --- Finding 1: reclaim can erase cancellation intent ---
def test_finding_1_reclaim_cannot_erase_cancellation_intent(db, monkeypatch):
    """Finding 1 (RED): reclaim must not overwrite state='retryable' if cancellation
    intent was recorded (termination_outcome is non-NULL).
    """
    data = _delegated_task(1001, 2001)
    past = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    _insert_run(
        "run-f1",
        data["task_id"],
        data["delegation_id"],
        state="running",
        lease_expires_at=past,
    )

    # Simulate a concurrent cancel marking intent right after reclaim selected the row
    orig_pid = coordinator._pid_from_evidence

    def concurrent_cancel_during_reclaim(evidence):
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE dispatch_runs SET termination_outcome='requested' WHERE run_id='run-f1'"
            )
            conn.commit()
        finally:
            conn.close()
        return orig_pid(evidence)

    monkeypatch.setattr(coordinator, "_pid_from_evidence", concurrent_cancel_during_reclaim)
    monkeypatch.setattr(supervise, "pid_is_ours", lambda _e: False)

    # Now reclaim runs its lease expiry loop
    coordinator.reclaim_expired_leases()

    # In pre-fix code: reclaim overwrote state='retryable' despite termination_outcome='requested'!
    run = coordinator.get_run("run-f1")
    assert run["state"] != "retryable", (
        "Reclaim overwrote cancellation intent with state='retryable'"
    )


# --- Finding 2: late spawn keeps false confirmation ---
def test_finding_2_late_spawn_revokes_false_confirmation(db):
    """Finding 2 (RED): late spawn whose kill is unconfirmed must downgrade
    premature 'confirmed' cessation to 'unknown'.
    """
    data = _delegated_task(1002, 2002)
    # cancel_run completed against empty evidence, setting confirmed
    _insert_run(
        "run-f2",
        data["task_id"],
        data["delegation_id"],
        state="cancelled",
        termination_outcome="confirmed",
        evidence_ref=None,
    )

    # Simulate finalize_cancellation called from launch with unconfirmed cessation
    res = coordinator.finalize_cancellation(
        coordinator.get_run("run-f2"),
        actor="dispatch-launch",
        reason="cancelled_during_launch",
        termination="kill_unconfirmed",
        cessation="unknown",
    )
    assert res["ok"] is True

    run = coordinator.get_run("run-f2")
    assert run["termination_outcome"] == "unknown", (
        f"Row retained false confirmed outcome: {run['termination_outcome']}"
    )


# --- Finding 3: unconfirmed kills release live slots ---
def test_finding_3_unconfirmed_kill_retains_live_slot(db, monkeypatch):
    """Finding 3 (RED): a kill_unconfirmed overrun becomes blocked but must
    retain its live-slot reservation until verified.
    """
    monkeypatch.setenv("UA_DISPATCH_RUN_MAX_S", "10")
    data = _delegated_task(1003, 2003)
    run_id = "run-f3"
    past = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    lease_past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    _insert_run(
        run_id,
        data["task_id"],
        data["delegation_id"],
        state="running",
        evidence_ref="pid:99999:start",
        lease_expires_at=lease_past,
    )
    # Backdate created_at to trigger runtime ceiling overrun
    conn = storage.connect()
    try:
        conn.execute("UPDATE dispatch_runs SET created_at=? WHERE run_id=?", (past, run_id))
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(supervise, "pid_is_ours", lambda _evidence: True)
    monkeypatch.setattr(coordinator, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(supervise, "terminate_ours", lambda _evidence: "kill_unconfirmed")

    coordinator.reclaim_expired_leases()

    run = coordinator.get_run(run_id)
    assert run["state"] == "blocked"
    assert run["failure_reason"] == "runtime_ceiling_kill_unconfirmed"

    # Live slot must still be occupied by this run!
    conn = storage.connect()
    try:
        live = coordinator._live_run_for_task(conn, data["task_id"])
        assert live is not None, "Live slot was released despite unconfirmed kill!"
        assert live["run_id"] == run_id

        # Concurrency ceiling count must include it
        assert launch.live_run_count() >= 1

        # Inserting another live run for this task must fail on partial unique index
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dispatch_runs (run_id, task_id, delegation_id, agent_label, state, "
                "actor, lease_owner, created_at, updated_at) "
                "VALUES ('run-sibling', ?, ?, 'Desktop_Agent2', 'claimed', 'coord', 'coord', ?, ?)",
                (data["task_id"], data["delegation_id"], storage.now(), storage.now()),
            )
    finally:
        conn.close()


# --- Finding 4: cancellation exits become failed runs ---
def test_finding_4_cancellation_intent_locks_out_failed_outcome(db):
    """Finding 4 (RED): record_run_outcome must not write 'failed' when
    termination_outcome is already 'requested'.
    """
    data = _delegated_task(1004, 2004)
    _insert_run(
        "run-f4",
        data["task_id"],
        data["delegation_id"],
        state="running",
        termination_outcome="requested",
    )

    out = scheduler.record_run_outcome("run-f4", "failed", failure_reason="exit_status_143")
    assert out["ok"] is False, (
        "record_run_outcome allowed writing state='failed' during cancellation"
    )

    run = coordinator.get_run("run-f4")
    assert run["state"] == "running"
    assert run["termination_outcome"] == "requested"


# --- Finding 5: legacy cancelled rows with NULL termination_outcome ---
def test_finding_5_legacy_cancelled_row_with_null_termination_updated_by_confirm(db, monkeypatch):
    """Finding 5 (BUG): confirm_cancellation must be able to update legacy
    cancelled rows with NULL termination_outcome to 'confirmed'.
    """
    data = _delegated_task(1005, 2005)
    _insert_run(
        "run-f5",
        data["task_id"],
        data["delegation_id"],
        state="cancelled",
        termination_outcome=None,
    )

    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Person")
    res = coordinator.confirm_cancellation("run-f5", actor="Desktop_Person", note="manual override")
    assert res["ok"] is True

    run = coordinator.get_run("run-f5")
    assert run["termination_outcome"] == "confirmed", (
        f"Legacy row was not updated to confirmed: {run['termination_outcome']}"
    )

    # Now the live slot must be freed
    conn = storage.connect()
    try:
        live = coordinator._live_run_for_task(conn, data["task_id"])
        assert live is None, "Live slot was not freed after confirmation!"
    finally:
        conn.close()


# --- Finding 7: migration failure aborts startup but omits conflicting IDs ---
def test_finding_7_migration_failure_includes_conflicting_task_and_run_ids(tmp_path, monkeypatch):
    """Finding 7 (BUG): when installing unique index fails, error must name
    the conflicting task and run IDs.
    """
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()

    conn = storage.connect()
    try:
        now = storage.now()
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at) "
            "VALUES (999, 'conflicted task', 'doing', 'med', ?, ?)",
            (now, now),
        )
        conn.execute("DROP INDEX IF EXISTS idx_dispatch_runs_live_task")
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, created_at, updated_at) "
            "VALUES ('run-conflict-1', 999, 'Desktop_Agent2', 'running', 'actor', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, created_at, updated_at) "
            "VALUES ('run-conflict-2', 999, 'Desktop_Agent2', 'running', 'actor', ?, ?)",
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()

    storage._connection = None
    with pytest.raises(sqlite3.IntegrityError) as exc_info:
        storage.init_db()

    msg = str(exc_info.value)
    assert "999" in msg, f"Task ID 999 missing from migration error message: {msg}"
    assert "run-conflict-1" in msg or "run-conflict-2" in msg, (
        f"Run IDs missing from migration error message: {msg}"
    )


# --- Finding 8: live index dropped and rebuilt on every startup ---
def test_finding_8_live_index_not_dropped_and_rebuilt_when_already_installed(tmp_path, monkeypatch):
    """Finding 8 (PERF/SAFETY): init_db must not drop and recreate the live index
    if it already exists with the expected schema.
    """
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()

    # Track execute calls during second init_db using trace callback
    executed_statements = []
    real_connect = storage.connect

    def tracing_connect(**kwargs):
        c = real_connect(**kwargs)
        c.set_trace_callback(executed_statements.append)
        return c

    monkeypatch.setattr(storage, "connect", tracing_connect)
    storage._connection = None
    storage.init_db()

    drop_live_index = [
        s for s in executed_statements if "DROP INDEX" in s and "idx_dispatch_runs_live_task" in s
    ]
    assert not drop_live_index, "idx_dispatch_runs_live_task was dropped on second init_db!"


# --- Finding 6: predicate quadruplication ---
def test_finding_6_unified_live_slot_predicate_consistency(db):
    """Finding 6 (ANALYSIS): the live-slot predicate is defined once in storage.py
    and used consistently across coordinator, scheduler, launch, and migration.
    """
    assert hasattr(storage, "DISPATCH_RUN_LIVE_SLOT_PREDICATE")
    pred = storage.DISPATCH_RUN_LIVE_SLOT_PREDICATE
    assert "runtime_ceiling_" in pred

    # Verify coordinator._live_run_for_task, launch.live_run_count,
    # and scheduler live checks all include unconfirmed ceiling timeouts.
    data = _delegated_task(1006, 2006)
    _insert_run(
        "run-f6-unconfirmed-timeout",
        data["task_id"],
        data["delegation_id"],
        state="blocked",
        failure_reason="runtime_ceiling_kill_unconfirmed",
        termination_outcome="unknown",
    )

    conn = storage.connect()
    try:
        # 1. coordinator._live_run_for_task recognizes it as occupying live slot
        live_coord = coordinator._live_run_for_task(conn, data["task_id"])
        assert live_coord is not None
        assert live_coord["run_id"] == "run-f6-unconfirmed-timeout"

        # 2. launch.live_run_count includes it in process concurrency ceiling
        assert launch.live_run_count() == 1

        # 3. index enforces exclusivity (cannot insert another live run for same task)
        now = storage.now()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, created_at, updated_at) "
                "VALUES ('run-f6-dupe', ?, 'Desktop_Agent2', 'running', 'actor', ?, ?)",
                (data["task_id"], now, now),
            )
    finally:
        conn.close()
