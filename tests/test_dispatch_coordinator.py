"""Fail-closed approval-to-dispatch coordinator (task #548).

Real task/delegation/approval persistence + atomic claims; only the
process-launch boundary is stubbed. Acceptance contract:
``docs/architecture/approved-dispatch-state-machine.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, storage
from unified_agent.dispatch import coordinator
from unified_agent.dispatch.launch import RecordingLaunchBoundary
from unified_agent.tools import approvals, delegate, dispatch_report, tasks

REQUIRED_TRANSITION_FIELDS = (
    "actor",
    "task_id",
    "delegation_id",
    "agent_label",
    "run_id",
    "timestamp",
    "evidence_ref",
    "failure_reason",
)


def test_cancel_run_is_atomic_idempotent_and_identity_checked(db, monkeypatch):
    packed = _delegated()
    _approve_note(packed["task_id"])
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    monkeypatch.setattr(coordinator.supervise, "terminate_ours", lambda _evidence: "terminated")

    cancelled = coordinator.cancel_run(out["run_id"], actor="Desktop_Agent2")
    duplicate = coordinator.cancel_run(out["run_id"], actor="Desktop_Agent2")

    assert cancelled == {
        "ok": True,
        "run_id": out["run_id"],
        "state": "cancelled",
        "termination": "terminated",
        "cessation": "confirmed",
    }
    assert duplicate["reason"] == "already_terminal"
    run = coordinator.get_run(out["run_id"])
    assert run["state"] == "cancelled"
    assert run["termination_outcome"] == "confirmed"


@pytest.mark.parametrize(
    "raw_outcome",
    ["refused", "identity_mismatch"],
)
def test_cancel_run_refused_or_mismatched_termination_is_not_confirmed_cessation(
    db, monkeypatch, raw_outcome
):
    """Task #1221: a refused/identity-mismatched kill must not report a confirmed stop.

    ``terminate_ours`` is a fake here — this test never touches a real process
    — but the coordinator must treat its answer the same way it would for a
    genuine refusal: recorded cancellation, unconfirmed cessation, no signal
    ever claimed to have reached a live process beyond the fake call itself.
    """
    packed = _delegated()
    _approve_note(packed["task_id"])
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    monkeypatch.setattr(coordinator.supervise, "terminate_ours", lambda _evidence: raw_outcome)

    result = coordinator.cancel_run(out["run_id"], actor="Desktop_Agent2")

    assert result == {
        "ok": True,
        "run_id": out["run_id"],
        "state": "cancelled",
        "termination": raw_outcome,
        "cessation": "unknown",
    }
    run = coordinator.get_run(out["run_id"])
    assert run["state"] == "cancelled"
    assert run["termination_outcome"] == "unknown"

    payloads = _transition_payloads()
    cancel_payload = next(
        p for p in payloads if p.get("run_id") == out["run_id"] and p.get("state") == "cancelled"
    )
    assert cancel_payload["termination_outcome"] == "unknown"


def test_cancel_run_survives_unexpected_terminate_exception_as_unknown(db, monkeypatch):
    """An unexpected raise out of terminate_ours must not report success."""
    packed = _delegated()
    _approve_note(packed["task_id"])
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)

    def _boom(_evidence):
        raise RuntimeError("simulated supervise failure — no real signal sent")

    monkeypatch.setattr(coordinator.supervise, "terminate_ours", _boom)

    result = coordinator.cancel_run(out["run_id"], actor="Desktop_Agent2")

    assert result["ok"] is True
    assert result["cessation"] == "unknown"
    assert result["termination"] is None
    assert coordinator.get_run(out["run_id"])["termination_outcome"] == "unknown"


def test_resume_run_blocked_when_cessation_unconfirmed(db, monkeypatch):
    """Task #1221: resume must refuse to overlap a process whose stop is unconfirmed."""
    packed = _delegated()
    _approve_note(packed["task_id"])
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    run_id = out["run_id"]
    monkeypatch.setattr(coordinator.supervise, "terminate_ours", lambda _evidence: "refused")

    cancel_res = coordinator.cancel_run(run_id, actor="Desktop_Agent2")
    assert cancel_res["cessation"] == "unknown"

    resumed = coordinator.resume_run(
        run_id, actor="Desktop_Agent2", launch=RecordingLaunchBoundary()
    )

    assert resumed["ok"] is False
    assert resumed["reason"] == "cessation_unconfirmed"
    assert resumed["termination_outcome"] == "unknown"
    # No relaunch happened — the run must still be sitting cancelled, not live.
    assert coordinator.get_run(run_id)["state"] == "cancelled"


def test_unconfirmed_cancellation_blocks_sibling_claim_and_resume(db, monkeypatch):
    """Devin review finding 1 (PR #640): an unconfirmed cancellation must keep
    holding the task's live slot — otherwise a sibling run (a fresh claim, or
    resuming an older resumable run) can overlap a process that was never
    actually confirmed stopped.

    Mirrors the finding's own example: run A fails and is resumable; run B
    claims the same task next and is cancelled with an unconfirmed outcome
    (identity_mismatch); resuming A — or claiming a third run — must refuse
    rather than start alongside whatever B may still be doing.
    """
    packed = _delegated()
    _approve_note(packed["task_id"])
    task_id = packed["task_id"]
    delegation_id = packed["delegation_id"]

    run_a = coordinator.claim_and_dispatch(task_id=task_id, launch=RecordingLaunchBoundary())
    run_a_id = run_a["run_id"]

    # Simulate A failing and becoming resumable, with its delegation released
    # back to pending — exactly what reclaim_expired_leases does for a timed
    # out run. This mirrors the finding's "Run A fails and remains resumable."
    conn = storage.connect()
    try:
        conn.execute("UPDATE dispatch_runs SET state='retryable' WHERE run_id=?", (run_a_id,))
        conn.execute(
            "UPDATE delegations SET status='pending', last_attempt_at=NULL WHERE id=?",
            (delegation_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # Run B claims the same task next.
    run_b = coordinator.claim_and_dispatch(task_id=task_id, launch=RecordingLaunchBoundary())
    assert run_b["launched"] is True
    run_b_id = run_b["run_id"]
    assert run_b_id != run_a_id

    # B is cancelled, but identity_mismatch means cessation is unconfirmed —
    # the process it tried to stop may still be running.
    monkeypatch.setattr(coordinator.supervise, "terminate_ours", lambda _e: "identity_mismatch")
    cancel_b = coordinator.cancel_run(run_b_id, actor="Desktop_Agent2")
    assert cancel_b["cessation"] == "unknown"

    # Resuming A must not overlap B's possibly-still-running process.
    resumed_a = coordinator.resume_run(
        run_a_id, actor="Desktop_Agent2", launch=RecordingLaunchBoundary()
    )
    assert resumed_a["ok"] is False
    assert resumed_a["reason"] == "already_claimed"
    assert resumed_a["live_run_id"] == run_b_id
    assert coordinator.get_run(run_a_id)["state"] == "retryable"

    # A brand new claim on the task (a third run) must also refuse — B's
    # unconfirmed cancellation, not a stale lease, is what is blocking it.
    conn = storage.connect()
    try:
        conn.execute("UPDATE delegations SET status='pending' WHERE id=?", (delegation_id,))
        conn.commit()
    finally:
        conn.close()
    claim_c = coordinator.claim_and_dispatch(task_id=task_id, launch=RecordingLaunchBoundary())
    assert claim_c["launched"] is False
    assert claim_c["reason"] == "cessation_unconfirmed"
    assert claim_c["blocking_run_id"] == run_b_id


def test_persist_spawn_pid_yields_to_a_pending_cancellation(db):
    """Devin review finding 2: the evidence-persist write must be a
    compare-and-swap against cancellation intent, not an unconditional write.

    Once ``termination_outcome`` is no longer NULL (cancel_run's atomic
    intent commit), a spawn racing in afterward must NOT be allowed to
    silently record its pid as this run's evidence — that pid was never seen
    by cancel_run's termination attempt, so recording it would misrepresent
    what "cancelled" verified.
    """
    from unified_agent.dispatch.launch import _persist_spawn_pid

    packed = _delegated()
    _approve_note(packed["task_id"])
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"], launch=RecordingLaunchBoundary()
    )
    run_id = out["run_id"]

    # Baseline: with no cancellation in flight, the write succeeds normally.
    assert _persist_spawn_pid(run_id, 4242) is True
    assert coordinator.get_run(run_id)["evidence_ref"].startswith("pid:4242")

    # Reset evidence and simulate cancel_run's intent commit having already
    # landed (as it would have, between the row's initial read and this
    # spawn's own write).
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET evidence_ref=NULL, termination_outcome='requested' "
            "WHERE run_id=?",
            (run_id,),
        )
        conn.commit()
    finally:
        conn.close()

    assert _persist_spawn_pid(run_id, 4343) is False
    # The late pid must never have been recorded as this run's evidence.
    run = coordinator.get_run(run_id)
    assert run["evidence_ref"] is None
    assert run["termination_outcome"] == "requested"


def test_launch_boundary_self_terminates_spawn_racing_a_cancellation(db):
    """Devin review finding 2: a spawn that loses the race to a concurrent
    cancellation must terminate the child it just started itself, and must
    never be reported back as a successful, confirmed launch.

    ``RacingCancelSpawner`` simulates cancel_run's intent commit landing in
    the gap between the spawner being invoked and its result being read —
    the exact window the finding describes (claimed, evidence_ref still
    NULL, cancellation lands underneath the launch).
    """
    from unified_agent.dispatch.launch import RoutedLaunchBoundary

    class RacingCancelSpawner:
        def spawn(self, request):
            run_id = request["run_id"]
            conn = storage.connect()
            try:
                conn.execute(
                    "UPDATE dispatch_runs SET termination_outcome='requested', updated_at=? "
                    "WHERE run_id=?",
                    (storage.now(), run_id),
                )
                conn.commit()
            finally:
                conn.close()
            # A real pid handed back by the spawner — never actually a live
            # process in this test, but the launch boundary must not assume
            # that; it discovers the race via the guarded write, not by
            # checking liveness itself.
            return {"ok": True, "stub": False, "spawned": True, "pid": 4242}

    packed = _delegated(to_agent="Desktop_Agent1")
    _approve_note(packed["task_id"])

    launch = RoutedLaunchBoundary(spawner=RacingCancelSpawner())
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)

    assert out["launched"] is False
    assert out["reason"] == "cancelled_during_launch"
    run = coordinator.get_run(out["run_id"])
    assert run["state"] == "cancelled"
    # pid 4242 was never a real process here, so terminate() reports
    # already_gone — confirmed. The point under test is that this reflects
    # the launch boundary's OWN kill of the pid it just spawned, not a stale
    # or absent evidence_ref that a concurrent cancel_run would otherwise
    # have wrongly trusted as proof of a confirmed stop.
    assert run["termination_outcome"] == "confirmed"
    assert run["evidence_ref"].startswith("pid:4242")


def test_reconcile_pending_cancellations_finishes_a_crash_left_intent(db, monkeypatch):
    """Devin review finding 3: a crash between persisting cancellation intent
    and finishing termination must leave a reconcilable row, not a silent
    'nothing happened here'. reconcile_pending_cancellations (invoked by
    reclaim_expired_leases) must pick it back up and finalize it.
    """
    packed = _delegated()
    _approve_note(packed["task_id"])
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"], launch=RecordingLaunchBoundary()
    )
    run_id = out["run_id"]

    # Simulate the coordinator crashing right after cancel_run's intent
    # commit, before it could call terminate_ours or finalize.
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET termination_outcome='requested', updated_at=? WHERE run_id=?",
            (storage.now(), run_id),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(coordinator.supervise, "terminate_ours", lambda _e: "terminated")
    result = coordinator.reconcile_pending_cancellations(actor="dispatch-coordinator")

    assert result["resolved_count"] == 1
    assert result["resolved"][0]["run_id"] == run_id
    assert result["resolved"][0]["cessation"] == "confirmed"
    run = coordinator.get_run(run_id)
    assert run["state"] == "cancelled"
    assert run["termination_outcome"] == "confirmed"


def test_reclaim_never_promotes_a_pending_cancellation_to_retryable(db, monkeypatch):
    """Devin review finding 3: the ordinary lease-expiry path in
    reclaim_expired_leases must never silently turn a stuck
    termination_outcome='requested' row into 'retryable' — that would drop
    the unconfirmed-cessation guard entirely and make the run look freely
    resumable again.
    """
    packed = _delegated()
    _approve_note(packed["task_id"])
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"], launch=RecordingLaunchBoundary()
    )
    run_id = out["run_id"]

    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET termination_outcome='requested', "
            "lease_expires_at='2000-01-01T00:00:00Z' WHERE run_id=?",
            (run_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # Termination cannot be confirmed on this reconcile pass either.
    monkeypatch.setattr(coordinator.supervise, "terminate_ours", lambda _e: "refused")
    coordinator.reclaim_expired_leases()

    run = coordinator.get_run(run_id)
    assert run["state"] == "cancelled"
    assert run["termination_outcome"] == "unknown"
    assert run["state"] != "retryable"


def test_operator_retry_and_confirm_cancellation(db, monkeypatch):
    """Devin review finding 1/3: an unconfirmed cancellation must not be a
    state nobody can clear — retry_cancellation re-attempts the real
    termination, and confirm_cancellation is the manual override for when the
    target was verified gone by other means.
    """
    packed = _delegated()
    _approve_note(packed["task_id"])
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"], launch=RecordingLaunchBoundary()
    )
    run_id = out["run_id"]

    monkeypatch.setattr(coordinator.supervise, "terminate_ours", lambda _e: "refused")
    cancelled = coordinator.cancel_run(run_id, actor="Desktop_Agent2")
    assert cancelled["cessation"] == "unknown"

    # Recovery is operator authority, not an actor string supplied by a caller.
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Person")

    # retry_cancellation re-attempts — still refused, still unconfirmed.
    retried = coordinator.retry_cancellation(run_id, actor="Desktop_Person")
    assert retried["cessation"] == "unknown"
    assert coordinator.get_run(run_id)["termination_outcome"] == "unknown"

    # Now the retry succeeds (process finally exited on its own).
    monkeypatch.setattr(coordinator.supervise, "terminate_ours", lambda _e: "already_gone")
    retried_ok = coordinator.retry_cancellation(run_id, actor="Desktop_Person")
    assert retried_ok["cessation"] == "confirmed"
    run = coordinator.get_run(run_id)
    assert run["termination_outcome"] == "confirmed"
    assert run["lease_expires_at"] is None
    cancellation_events = [
        p
        for p in _transition_payloads()
        if p.get("run_id") == run_id and p.get("state") == "cancelled"
    ]
    assert [p["termination_outcome"] for p in cancellation_events] == ["unknown", "confirmed"]

    # Once confirmed, resuming is no longer blocked by the cessation gate.
    resumed = coordinator.resume_run(
        run_id, actor="Desktop_Agent2", launch=RecordingLaunchBoundary()
    )
    assert resumed["ok"] is True


def test_operator_confirm_cancellation_override(db, monkeypatch):
    packed = _delegated()
    _approve_note(packed["task_id"])
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"], launch=RecordingLaunchBoundary()
    )
    run_id = out["run_id"]

    monkeypatch.setattr(coordinator.supervise, "terminate_ours", lambda _e: "identity_mismatch")
    coordinator.cancel_run(run_id, actor="Desktop_Agent2")
    assert coordinator.get_run(run_id)["termination_outcome"] == "unknown"

    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent2")
    unauthorized = coordinator.confirm_cancellation(run_id, actor="Operator")
    assert unauthorized == {
        "ok": False,
        "reason": "operator_authorization_required",
        "run_id": run_id,
    }

    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Person")
    confirmed = coordinator.confirm_cancellation(
        run_id, actor="Desktop_Person", note="verified dead via host reboot"
    )
    assert confirmed["ok"] is True
    assert confirmed["cessation"] == "confirmed"
    run = coordinator.get_run(run_id)
    assert run["termination_outcome"] == "confirmed"
    assert run["lease_expires_at"] is None


def test_dispatch_cancellation_recovery_tools_are_operator_only(db, monkeypatch):
    packed = _delegated()
    _approve_note(packed["task_id"])
    out = coordinator.claim_and_dispatch(
        task_id=packed["task_id"], launch=RecordingLaunchBoundary()
    )
    run_id = out["run_id"]
    monkeypatch.setattr(coordinator.supervise, "terminate_ours", lambda _e: "refused")
    coordinator.cancel_run(run_id, actor="Desktop_Agent2")

    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent2")
    with pytest.raises(ToolError, match="operator profile"):
        dispatch_report.dispatch_retry_cancellation(run_id=run_id)
    with pytest.raises(ToolError, match="operator profile"):
        dispatch_report.dispatch_confirm_cancellation(run_id=run_id)

    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Person")
    monkeypatch.setattr(coordinator.supervise, "terminate_ours", lambda _e: "already_gone")
    retried = dispatch_report.dispatch_retry_cancellation(run_id=run_id)
    assert retried["cessation"] == "confirmed"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    storage.init_db()
    return tmp_path


def _spec(title: str = "Dispatch coordinator fixture") -> str:
    return (
        f"### Objective\n{title}\n"
        "### Scope\n- src/unified_agent/dispatch/\n"
        "- tests/test_dispatch_coordinator.py\n"
        "### Definition of Done\n"
        "- approved delegation claims once and mints one run_id\n"
        "- denied/expired/cancelled/unapproved/malformed/unknown-agent refuse\n"
        "- Gates: pytest tests/test_dispatch_coordinator.py\n"
        "### TDD Constraints\n"
        "- Stub: process launch boundary only\n"
        "- Must be real: task/delegation/approval persistence and atomic claims\n"
        "### Boundaries\n"
        "- Must not enable Kanban/cron autonomy or edit Red-tier config\n"
        "- Must not weaken approval fail-closed rules\n"
    )


def _delegated(*, to_agent: str = "Desktop_Agent1") -> dict:
    result = delegate.delegate(
        task_spec=_spec(),
        from_agent="Desktop_Agent5",
        to_agent=to_agent,
        callback="none",
    )
    task = next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == result["task_id"])
    return {
        "delegation_id": result["delegation_id"],
        "task_id": result["task_id"],
        "task": task,
    }


def _approve_note(task_id: int, *, ts: str = "2026-08-23T00:00:00Z") -> None:
    tasks.task_update(task_id, append_note=f"[plan: approved @ {ts}]")


def _transition_payloads() -> list[dict]:
    rows = events.query_events(channels=["agent.dispatch.transition"], limit=100)
    return [r["payload"] for r in rows]


def test_approved_claims_once_and_mints_one_run_id(db):
    packed = _delegated()
    _approve_note(packed["task_id"])
    launch = RecordingLaunchBoundary()

    first = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        actor="Operator",
        launch=launch,
    )
    second = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        actor="dispatch-coordinator",
        launch=launch,
    )

    assert first["launched"] is True
    assert first["run_id"]
    assert first["reason"] == "ok"
    assert second["launched"] is False
    assert second["reason"] == "already_claimed"
    assert second["run_id"] == first["run_id"]
    assert len(launch.calls) == 1
    conn = storage.connect()
    try:
        generation = conn.execute(
            "SELECT claim_generation FROM delegations WHERE id=?", (packed["delegation_id"],)
        ).fetchone()["claim_generation"]
    finally:
        conn.close()
    assert generation == 1
    assert launch.calls[0]["run_id"] == first["run_id"]

    run = coordinator.get_run(first["run_id"])
    assert run is not None
    assert run["task_id"] == packed["task_id"]
    assert run["delegation_id"] == packed["delegation_id"]
    assert run["agent_label"] == "Desktop_Agent1"
    assert run["state"] == "launched"

    payloads = _transition_payloads()
    ok = next(
        p for p in payloads if p.get("run_id") == first["run_id"] and p.get("state") == "launched"
    )
    for name in REQUIRED_TRANSITION_FIELDS:
        assert name in ok
    assert ok["actor"] == "Operator"
    assert ok["failure_reason"] is None


def test_denied_never_launches(db):
    packed = _delegated()
    tasks.task_update(
        packed["task_id"],
        append_note="[plan: denied @ 2026-08-23T00:00:00Z]",
    )
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is False
    assert out["reason"] == "denied"
    assert out["run_id"] is None
    assert launch.calls == []
    refused = _transition_payloads()[-1]
    assert refused["failure_reason"] == "denied"
    assert refused["run_id"] is None


def test_unapproved_missing_marker_refused(db):
    packed = _delegated()
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is False
    assert out["reason"] in {"missing_approval_marker", "unapproved"}
    assert launch.calls == []


def test_unknown_agent_refused(db, monkeypatch):
    """Roster check is fail-closed even if a stale delegation row exists."""
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1:Desktop_Agent5")
    packed = _delegated()
    _approve_note(packed["task_id"])
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE delegations SET to_agent=? WHERE id=?",
            ("Desktop_Nobody", packed["delegation_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(
        delegation_id=packed["delegation_id"],
        launch=launch,
    )
    assert out == {"launched": False, "reason": "unknown_agent", "run_id": None}
    assert launch.calls == []


def test_cancelled_task_refused(db):
    packed = _delegated()
    _approve_note(packed["task_id"])
    tasks.task_update(packed["task_id"], status="cancelled")
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is False
    assert out["reason"] == "cancelled"
    assert launch.calls == []


def test_expired_approval_refused(db):
    packed = _delegated()
    tasks.task_update(
        packed["task_id"],
        append_note="[plan: expired @ 2026-08-23T00:00:00Z]",
    )
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is False
    assert out["reason"] == "expired"
    assert launch.calls == []


def test_malformed_approval_marker_refused(db):
    packed = _delegated()
    tasks.task_update(packed["task_id"], append_note="[plan: approved]")  # missing @ ts
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is False
    assert out["reason"] == "malformed_approval"
    assert launch.calls == []


def test_malformed_approval_timestamp_refused(db):
    packed = _delegated()
    tasks.task_update(packed["task_id"], append_note="[plan: approved @ banana]")
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is False
    assert out["reason"] == "malformed_approval"
    assert launch.calls == []


def test_later_approval_overrides_earlier_denial(db):
    packed = _delegated()
    tasks.task_update(
        packed["task_id"],
        append_note="[plan: denied @ 2026-08-22T00:00:00Z]\n[plan: approved @ 2026-08-23T00:00:00Z]",
    )
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is True
    assert out["run_id"]
    assert len(launch.calls) == 1


def test_cancelled_parent_refuses_child(db):
    parent = tasks.task_add("parent", notes="parent task", priority="med")
    packed = _delegated()
    tasks.task_update(packed["task_id"], parent_id=parent["id"])
    _approve_note(packed["task_id"])
    tasks.task_update(parent["id"], status="cancelled")
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is False
    assert out["reason"] == "cancelled"
    assert launch.calls == []


def test_cancelled_root_refuses_nested_descendant(db):
    root = tasks.task_add("root", notes="project root", priority="med")
    middle = tasks.task_add("middle", parent_id=root["id"])
    packed = _delegated()
    tasks.task_update(packed["task_id"], parent_id=middle["id"])
    _approve_note(packed["task_id"])
    tasks.task_update(root["id"], status="cancelled")

    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)

    assert out["launched"] is False
    assert out["reason"] == "cancelled"
    assert launch.calls == []


def test_locked_revalidation_catches_newly_cancelled_root(db, monkeypatch):
    root = tasks.task_add("root", notes="project root", priority="med")
    middle = tasks.task_add("middle", parent_id=root["id"])
    packed = _delegated()
    tasks.task_update(packed["task_id"], parent_id=middle["id"])
    _approve_note(packed["task_id"])
    evaluate_ready = coordinator.evaluate_ready

    def evaluate_then_cancel(**kwargs):
        verdict = evaluate_ready(**kwargs)
        tasks.task_update(root["id"], status="cancelled")
        return verdict

    monkeypatch.setattr(coordinator, "evaluate_ready", evaluate_then_cancel)
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)

    assert out["launched"] is False
    assert out["reason"] == "cancelled"
    assert launch.calls == []


def test_executor_task_lock_prevents_orphan_reclaim(db):
    from unified_agent.tools import delegate as delegate_tool

    packed = _delegated()
    claimed = delegate_tool.delegate_claim(packed["delegation_id"], "Desktop_Agent1")
    assert claimed["claimed"] is True

    future = (datetime.now(UTC) + timedelta(seconds=600)).isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET claimed_by=?, lock_expires_at=?, status='doing', updated_at=? "
            "WHERE id=?",
            ("Desktop_Agent1", future, storage.now(), packed["task_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    reclaimed = coordinator.reclaim_expired_leases()
    orphan_hits = [
        r
        for r in reclaimed.get("reclaimed", [])
        if r.get("delegation_id") == packed["delegation_id"]
    ]
    assert orphan_hits == []

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT status FROM delegations WHERE id=?",
            (packed["delegation_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "in_flight"


def test_approval_row_authorizes_without_note_marker(db):
    packed = _delegated()
    payload = coordinator.dispatch_approval_payload(
        task_id=packed["task_id"],
        delegation_id=packed["delegation_id"],
    )
    with pytest.raises(approvals.ApprovalPendingError):
        approvals.check_approval_gate(
            coordinator.DISPATCH_APPROVAL_TOOL,
            payload,
            requested_by="Desktop_Agent5",
            force=True,
        )
    pending = approvals.approval_pending(tool=coordinator.DISPATCH_APPROVAL_TOOL)["pending"]
    assert pending
    approvals.approval_resolve(pending[0]["id"], "approve", resolved_by="Operator")

    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert out["launched"] is True
    assert out["run_id"]
    assert len(launch.calls) == 1


def test_stale_lease_reclaim_allows_single_relaunch(db):
    packed = _delegated()
    _approve_note(packed["task_id"])
    launch = RecordingLaunchBoundary()

    first = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        launch=launch,
        lease_ttl_s=60.0,
    )
    assert first["launched"] is True
    run_id = first["run_id"]

    # Simulate crash mid-flight: leave run claimed with an expired lease.
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET state='claimed', lease_expires_at=?, updated_at=? "
            "WHERE run_id=?",
            (past, storage.now(), run_id),
        )
        conn.commit()
    finally:
        conn.close()

    reclaimed = coordinator.reclaim_expired_leases()
    assert reclaimed["reclaimed_count"] == 1
    run = coordinator.get_run(run_id)
    assert run["state"] == "retryable"

    second = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert second["launched"] is True
    assert second["run_id"] != run_id
    assert len(launch.calls) == 2

    # Live second claim still blocks duplicates.
    third = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert third["launched"] is False
    assert third["reason"] == "already_claimed"


def test_reclaim_promotes_claimed_run_with_live_child(db, monkeypatch):
    packed = _delegated()
    _approve_note(packed["task_id"])
    launch = RecordingLaunchBoundary()
    first = coordinator.claim_and_dispatch(
        task_id=packed["task_id"],
        launch=launch,
        lease_ttl_s=60.0,
    )
    run_id = first["run_id"]
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET state='claimed', evidence_ref=?, "
            "lease_expires_at=?, updated_at=? WHERE run_id=?",
            ("pid:4242", past, storage.now(), run_id),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(coordinator, "_pid_alive", lambda pid: pid == 4242)
    reclaimed = coordinator.reclaim_expired_leases()
    assert reclaimed["reclaimed_count"] == 0
    run = coordinator.get_run(run_id)
    assert run["state"] == "launched"
    ready = coordinator.evaluate_ready(task_id=packed["task_id"])
    assert ready["ready"] is False
    assert ready["reason"] == "already_claimed"
    second = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert second["launched"] is False
    assert second["reason"] == "already_claimed"
    assert second["run_id"] == run_id
    assert len(launch.calls) == 1


def test_duplicate_poll_does_not_double_launch_under_contention(db):
    packed = _delegated()
    _approve_note(packed["task_id"])
    launch = RecordingLaunchBoundary()

    # First claim wins inside the coordinator; a second concurrent-style call
    # must observe the unique live-run index rather than spawning again.
    a = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    b = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    assert a["launched"] is True
    assert b["launched"] is False
    assert len(launch.calls) == 1


def test_marker_embedded_in_task_spec_does_not_authorize(db):
    """Caller-controlled spec text must not satisfy the approval predicate."""
    marker = "[plan: approved @ 2026-08-24T00:00:00Z]"
    result = delegate.delegate(
        task_spec=_spec() + f"\n{marker}",
        from_agent="Desktop_Agent5",
        to_agent="Desktop_Agent1",
        callback="none",
    )
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=result["task_id"], launch=launch)
    assert out["launched"] is False
    assert out["reason"] in {"missing_approval_marker", "unapproved"}
    assert launch.calls == []


def test_approval_row_survives_launch_failure_for_retry(db):
    packed = _delegated()
    payload = coordinator.dispatch_approval_payload(
        task_id=packed["task_id"],
        delegation_id=packed["delegation_id"],
    )
    with pytest.raises(approvals.ApprovalPendingError):
        approvals.check_approval_gate(
            coordinator.DISPATCH_APPROVAL_TOOL,
            payload,
            requested_by="Desktop_Agent5",
            force=True,
        )
    pending = approvals.approval_pending(tool=coordinator.DISPATCH_APPROVAL_TOOL)["pending"]
    approvals.approval_resolve(pending[0]["id"], "approve", resolved_by="Operator")

    failing = RecordingLaunchBoundary(fail_with="spawn_unavailable")
    first = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=failing)
    assert first["launched"] is False
    assert first["reason"] == "launch_failed"

    second = coordinator.claim_and_dispatch(
        task_id=packed["task_id"], launch=RecordingLaunchBoundary()
    )
    assert second["launched"] is True
    assert second["run_id"] != first["run_id"]


def test_resume_run_from_cancelled_state_succeeds_and_relaunches(db, monkeypatch):
    packed = _delegated()
    _approve_note(packed["task_id"])
    launch1 = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch1)
    conn = storage.connect()
    try:
        first_generation = conn.execute(
            "SELECT claim_generation FROM delegations WHERE id=?", (packed["delegation_id"],)
        ).fetchone()["claim_generation"]
    finally:
        conn.close()
    run_id = out["run_id"]
    monkeypatch.setattr(coordinator.supervise, "terminate_ours", lambda _evidence: "terminated")
    cancel_res = coordinator.cancel_run(run_id, actor="Desktop_Agent2")
    assert cancel_res["ok"] is True
    assert coordinator.get_run(run_id)["state"] == "cancelled"

    launch2 = RecordingLaunchBoundary()
    resumed = coordinator.resume_run(run_id, actor="Desktop_Agent2", launch=launch2)
    assert resumed["ok"] is True
    assert resumed["run_id"] == run_id
    assert resumed["state"] == "launched"
    assert len(launch2.calls) == 1
    assert coordinator.get_run(run_id)["state"] == "launched"
    conn = storage.connect()
    try:
        resumed_generation = conn.execute(
            "SELECT claim_generation FROM delegations WHERE id=?", (packed["delegation_id"],)
        ).fetchone()["claim_generation"]
    finally:
        conn.close()
    assert resumed_generation == first_generation + 1

    payloads = _transition_payloads()
    assert any(p.get("state") == "resumed" and p.get("run_id") == run_id for p in payloads)


def test_resume_run_fails_closed_when_terminal(db):
    packed = _delegated()
    _approve_note(packed["task_id"])
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    run_id = out["run_id"]

    conn = storage.connect()
    try:
        conn.execute("UPDATE dispatch_runs SET state='finished' WHERE run_id=?", (run_id,))
        conn.commit()
    finally:
        conn.close()

    res = coordinator.resume_run(run_id, actor="Desktop_Agent2")
    assert res["ok"] is False
    assert res["reason"] == "terminal_finished"


def test_resume_run_fails_closed_when_task_cancelled(db, monkeypatch):
    packed = _delegated()
    _approve_note(packed["task_id"])
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    run_id = out["run_id"]
    monkeypatch.setattr(coordinator.supervise, "terminate_ours", lambda _evidence: "terminated")
    coordinator.cancel_run(run_id, actor="Desktop_Agent2")

    tasks.task_update(packed["task_id"], status="cancelled")

    res = coordinator.resume_run(run_id, actor="Desktop_Agent2")
    assert res["ok"] is False
    assert res["reason"] == "task_terminal"


def test_resume_run_fails_closed_when_already_active(db):
    packed = _delegated()
    _approve_note(packed["task_id"])
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    run_id = out["run_id"]

    res = coordinator.resume_run(run_id, actor="Desktop_Agent2")
    assert res["ok"] is False
    assert res["reason"] == "already_active"


def test_dispatch_resume_tool_authenticated_and_identity_checked(db, monkeypatch):
    packed = _delegated(to_agent="Desktop_Agent1")
    _approve_note(packed["task_id"])
    launch = RecordingLaunchBoundary()
    out = coordinator.claim_and_dispatch(task_id=packed["task_id"], launch=launch)
    run_id = out["run_id"]
    monkeypatch.setattr(coordinator.supervise, "terminate_ours", lambda _evidence: "terminated")
    coordinator.cancel_run(run_id, actor="Desktop_Agent1")

    # Unauthenticated
    monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
    with pytest.raises(ToolError, match="requires an authenticated hub identity"):
        dispatch_report.dispatch_resume(run_id=run_id)

    # Identity mismatch
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent5")
    with pytest.raises(ToolError, match="identity mismatch"):
        dispatch_report.dispatch_resume(run_id=run_id)

    # Matching identity succeeds
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Agent1")
    res = dispatch_report.dispatch_resume(run_id=run_id)
    assert res["ok"] is True
    assert res["state"] == "launched"


@pytest.mark.parametrize("stored_lane", ["operator-only", config.LEGACY_OPERATOR_LANE])
@pytest.mark.parametrize("expected_lane", ["operator-only", config.LEGACY_OPERATOR_LANE])
def test_operator_lane_alias_readiness_keeps_explicit_lane_scope(db, stored_lane, expected_lane):
    packed = _delegated()
    _approve_note(packed["task_id"])
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET lane=? WHERE id=?", (stored_lane, packed["task_id"]))
        conn.commit()
    finally:
        conn.close()
    assert (
        coordinator.evaluate_ready(task_id=packed["task_id"], expected_lane=expected_lane)["ready"]
        is True
    )
    wrong_scope = coordinator.evaluate_ready(
        task_id=packed["task_id"], expected_lane="dispatch-ready"
    )
    assert wrong_scope["ready"] is False
    assert wrong_scope["reason"] == "lane_mismatch"
