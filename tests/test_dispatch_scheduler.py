"""Durable scheduler / Kanban poller for approved dispatch (task #550).

Real task/delegation/event/dispatch_runs persistence + atomic claims; wall
clock and process launch are stubbed. Acceptance contract:
``docs/architecture/approved-dispatch-state-machine.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from org_test_helpers import installation_org as installation_org

from unified_agent import events, queue_hygiene, storage
from unified_agent.dispatch import coordinator, scheduler
from unified_agent.dispatch.launch import RecordingLaunchBoundary
from unified_agent.tools import approvals, delegate, doctor, tasks


@pytest.fixture
def db(tmp_path, monkeypatch, installation_org):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    # Scheduler must stay off unless a test opts in.
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    storage.init_db()
    return tmp_path


def _spec(title: str = "Dispatch scheduler fixture") -> str:
    return (
        f"### Objective\n{title}\n"
        "### Scope\n- src/unified_agent/dispatch/scheduler.py\n"
        "- tests/test_dispatch_scheduler.py\n"
        "### Definition of Done\n"
        "- scheduler tick claims only approved ready work once\n"
        "- restart/duplicate ticks never double-launch; leases reclaim\n"
        "- Gates: pytest tests/test_dispatch_scheduler.py\n"
        "### TDD Constraints\n"
        "- Stub: wall-clock and subprocess launch only\n"
        "- Must be real: queue/event persistence and atomic claims\n"
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
    return {
        "delegation_id": result["delegation_id"],
        "task_id": result["task_id"],
    }


def _approve(task_id: int, *, ts: str = "2026-08-23T00:00:00Z") -> None:
    tasks.task_update(task_id, append_note=f"[plan: approved @ {ts}]")


def _transitions() -> list[dict]:
    rows = events.query_events(channels=["agent.dispatch.transition"], limit=200)
    return [r["payload"] for r in rows]


def test_disabled_tick_is_noop(db):
    packed = _delegated()
    _approve(packed["task_id"])
    launch = RecordingLaunchBoundary()

    out = scheduler.scheduler_tick(launch=launch)
    assert out["status"] == "disabled"
    assert out["launched"] == []
    assert launch.calls == []
    assert coordinator.list_runs(task_id=packed["task_id"]) == []


def test_force_tick_claims_approved_ready_once(db, monkeypatch):
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "0")
    packed = _delegated()
    _approve(packed["task_id"])
    launch = RecordingLaunchBoundary()
    clock = {"t": 1_700_000_000.0}

    first = scheduler.scheduler_tick(
        force=True,
        launch=launch,
        clock=lambda: clock["t"],
        batch_limit=5,
    )
    clock["t"] += 1.0
    second = scheduler.scheduler_tick(
        force=True,
        launch=launch,
        clock=lambda: clock["t"],
        batch_limit=5,
    )

    assert first["status"] == "ok"
    assert first["forced"] is True
    assert len(first["launched"]) == 1
    run_id = first["launched"][0]["run_id"]
    assert first["launched"][0]["task_id"] == packed["task_id"]

    assert second["status"] == "ok"
    assert second["launched"] == []
    # Already-claimed work fails evaluate_ready, so it never enters the claim loop.
    assert second["skipped"] == []
    assert len(launch.calls) == 1

    run = coordinator.get_run(run_id)
    assert run is not None
    assert run["state"] == "launched"

    notes = tasks.task_list(status="all")["tasks"]
    task = next(t for t in notes if t["id"] == packed["task_id"])
    assert "[dispatch: launched" in (task.get("notes") or "")


def test_enabled_tick_finds_only_approved_ready(db, monkeypatch):
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    approved = _delegated()
    _approve(approved["task_id"])
    unapproved = _delegated()
    launch = RecordingLaunchBoundary()

    ready = scheduler.list_ready_candidates(limit=20)
    assert {r["task_id"] for r in ready} == {approved["task_id"]}

    out = scheduler.scheduler_tick(launch=launch)
    assert out["status"] == "ok"
    assert len(out["launched"]) == 1
    assert out["launched"][0]["task_id"] == approved["task_id"]
    assert coordinator.list_runs(task_id=unapproved["task_id"]) == []


def test_ready_candidates_offer_epic_office_members_before_bench_without_retargeting(
    db, monkeypatch
):
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent2:server_agent4:Desktop_Agent5")
    bench = _delegated(to_agent="server_agent4")
    office = _delegated(to_agent="Desktop_Agent2")
    _approve(bench["task_id"])
    _approve(office["task_id"])
    evaluate_ready = coordinator.evaluate_ready

    def with_platform_epic(**kwargs):
        verdict = evaluate_ready(**kwargs)
        if verdict.get("task"):
            verdict["task"]["parent_id"] = 101
        return verdict

    monkeypatch.setattr(coordinator, "evaluate_ready", with_platform_epic)

    ready = scheduler.list_ready_candidates(limit=20)

    assert [row["task_id"] for row in ready] == [office["task_id"], bench["task_id"]]
    assert {row["task_id"]: row["agent_label"] for row in ready} == {
        office["task_id"]: "Desktop_Agent2",
        bench["task_id"]: "server_agent4",
    }


def test_nested_epic_descendant_keeps_office_priority(db, monkeypatch):
    from unified_agent import org_registry

    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent2:server_agent4:Desktop_Agent5")
    epic = tasks.task_add("Owned epic")["id"]
    child = tasks.task_add("Epic child", parent_id=epic)["id"]
    monkeypatch.setattr(
        org_registry,
        "load_registry",
        lambda: [
            {
                "id": "platform",
                "name": "Platform",
                "members": ["Desktop_Agent2"],
                "owned_epics": [epic],
            },
            {"id": "bench", "name": "Bench", "members": ["server_agent4"], "owned_epics": []},
        ],
    )
    bench = _delegated(to_agent="server_agent4")
    office = _delegated(to_agent="Desktop_Agent2")
    for packed in (bench, office):
        _approve(packed["task_id"])
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET parent_id=? WHERE id=?", (child, office["task_id"]))
        conn.execute("UPDATE tasks SET parent_id=? WHERE id=?", (child, bench["task_id"]))
        conn.commit()
    finally:
        conn.close()

    ready = scheduler.list_ready_candidates(limit=2)
    assert [row["task_id"] for row in ready] == [office["task_id"], bench["task_id"]]


def test_ready_candidates_scan_past_initial_limit_for_office_member(db, monkeypatch):
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent2:server_agent4:Desktop_Agent5")
    bench_tasks = [_delegated(to_agent="server_agent4") for _ in range(5)]
    office = _delegated(to_agent="Desktop_Agent2")
    for packed in [*bench_tasks, office]:
        _approve(packed["task_id"])
    evaluate_ready = coordinator.evaluate_ready

    def with_platform_epic(**kwargs):
        verdict = evaluate_ready(**kwargs)
        if verdict.get("task"):
            verdict["task"]["parent_id"] = 101
        return verdict

    monkeypatch.setattr(coordinator, "evaluate_ready", with_platform_epic)

    ready = scheduler.list_ready_candidates(limit=1)

    assert ready[0]["task_id"] == office["task_id"]
    assert ready[0]["agent_label"] == "Desktop_Agent2"


def test_restart_reclaim_timeout_then_single_relaunch(db, monkeypatch):
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    packed = _delegated()
    _approve(packed["task_id"])
    launch = RecordingLaunchBoundary()

    first = scheduler.scheduler_tick(launch=launch, lease_ttl_s=60.0)
    assert len(first["launched"]) == 1
    run_id = first["launched"][0]["run_id"]

    # Crash mid-run: leave launched with an expired lease (timeout path).
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET state='launched', lease_expires_at=?, updated_at=? "
            "WHERE run_id=?",
            (past, storage.now(), run_id),
        )
        conn.commit()
    finally:
        conn.close()

    second = scheduler.scheduler_tick(launch=launch, lease_ttl_s=60.0)
    assert second["reclaimed_count"] == 1
    assert second["reclaimed"][0]["failure_reason"] == "timeout"
    assert coordinator.get_run(run_id)["state"] == "retryable"
    assert len(second["launched"]) == 1
    assert second["launched"][0]["run_id"] != run_id
    assert len(launch.calls) == 2

    third = scheduler.scheduler_tick(launch=launch)
    assert third["launched"] == []
    assert len(launch.calls) == 2

    payloads = _transitions()
    assert any(p.get("run_id") == run_id and p.get("failure_reason") == "timeout" for p in payloads)


def test_launch_failure_retry_then_dead_letter(db, monkeypatch):
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    packed = _delegated()
    _approve(packed["task_id"])
    launch = RecordingLaunchBoundary(fail_with="spawn_unavailable")

    for _ in range(3):
        out = scheduler.scheduler_tick(
            launch=launch,
            max_attempts=3,
            lease_ttl_s=60.0,
        )
        assert out["launched"] == []
        assert any(s.get("reason") == "launch_failed" for s in out["skipped"])

    # Fourth tick dead-letters (attempts already at ceiling).
    final = scheduler.scheduler_tick(launch=launch, max_attempts=3)
    assert final["dead_lettered_count"] >= 1
    assert final["launched"] == []

    conn = storage.connect()
    try:
        drow = conn.execute(
            "SELECT status, attempts FROM delegations WHERE id=?",
            (packed["delegation_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert drow["status"] == "failed"
    assert int(drow["attempts"]) >= 3

    task = next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == packed["task_id"])
    assert "blocked" in (task.get("notes") or "")
    assert any(p.get("state") == "blocked" for p in _transitions())


def test_record_success_and_block_outcomes(db, monkeypatch):
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    packed = _delegated()
    _approve(packed["task_id"])
    launch = RecordingLaunchBoundary()

    tick = scheduler.scheduler_tick(launch=launch)
    run_id = tick["launched"][0]["run_id"]

    finished = scheduler.record_run_outcome(
        run_id,
        "finished",
        evidence_ref="https://example.test/pr/1",
    )
    assert finished["ok"] is True
    run = coordinator.get_run(run_id)
    assert run["state"] == "finished"
    assert run["evidence_ref"] == "https://example.test/pr/1"

    conn = storage.connect()
    try:
        drow = conn.execute(
            "SELECT status FROM delegations WHERE id=?",
            (packed["delegation_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert drow["status"] == "completed"

    task = next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == packed["task_id"])
    assert task["status"] == "done"
    assert task["notes"].count("[follow-up: pending") == 1
    assert "checks=tests,ci,review,live-health" in task["notes"]
    assert "evidence=https://example.test/pr/1" in task["notes"]

    # Duplicate finish refuses; no second launch possible.
    again = scheduler.record_run_outcome(run_id, "finished")
    assert again["ok"] is False
    assert scheduler.scheduler_tick(launch=launch)["launched"] == []
    assert len(launch.calls) == 1
    task = next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == packed["task_id"])
    assert task["notes"].count("[follow-up: pending") == 1


def test_doctor_reports_dispatch_scheduler_retired(db, monkeypatch):
    """Explicit Linux absence retires the timer even with stale gates."""
    from unified_agent import monitor

    # Retirement intent alone cannot prove absence; use the shared probe contract.
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(monitor, "_service_probe", lambda *_: {"LoadState": "not-found"})
    monkeypatch.delenv("UA_DISPATCH_SCHEDULER_ENABLED", raising=False)
    check = doctor._dispatch_scheduler_check()
    assert check["capability"] == "dispatch_scheduler"
    assert check["status"] == "DORMANT"
    assert check["gate"] == "UA_DISPATCH_SCHEDULER_ENABLED"
    assert "retired by #1435" in check["detail"]

    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    leftover = doctor._dispatch_scheduler_check()
    assert leftover["status"] == "DORMANT"
    assert "retired by #1435" in leftover["detail"]
    assert "mode=retired" in leftover["detail"]


def test_superseded_run_outcome_refused(db, monkeypatch):
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    packed = _delegated()
    _approve(packed["task_id"])
    launch = RecordingLaunchBoundary()

    first = scheduler.scheduler_tick(launch=launch, lease_ttl_s=60.0)
    old_run = first["launched"][0]["run_id"]

    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET state='launched', lease_expires_at=?, updated_at=? "
            "WHERE run_id=?",
            (past, storage.now(), old_run),
        )
        conn.commit()
    finally:
        conn.close()

    second = scheduler.scheduler_tick(launch=launch, lease_ttl_s=60.0)
    assert len(second["launched"]) == 1
    new_run = second["launched"][0]["run_id"]
    assert new_run != old_run

    late = scheduler.record_run_outcome(old_run, "finished")
    assert late["ok"] is False
    assert late["reason"] == "superseded_run"

    run = coordinator.get_run(new_run)
    assert run["state"] == "launched"


def test_observability_reports_historical_retry_count(db, monkeypatch):
    """Older runs keep the retry_count from their claimed transition."""
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    packed = _delegated()
    _approve(packed["task_id"])
    launch = RecordingLaunchBoundary()

    first = scheduler.scheduler_tick(launch=launch, lease_ttl_s=60.0)
    old_run = first["launched"][0]["run_id"]

    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET state='launched', lease_expires_at=?, updated_at=? "
            "WHERE run_id=?",
            (past, storage.now(), old_run),
        )
        conn.commit()
    finally:
        conn.close()

    second = scheduler.scheduler_tick(launch=launch, lease_ttl_s=60.0)
    assert len(second["launched"]) == 1

    old_obs = coordinator.get_run_observability(old_run)
    new_obs = coordinator.get_run_observability(second["launched"][0]["run_id"])
    assert old_obs is not None and new_obs is not None
    assert old_obs["retry_count"] == 1
    assert new_obs["retry_count"] == 2


def test_dead_letter_skips_executor_only_delegations(db, monkeypatch):
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    packed = _delegated()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE delegations SET attempts=3 WHERE id=?",
            (packed["delegation_id"],),
        )
        conn.commit()
    finally:
        conn.close()

    dead = scheduler.dead_letter_exhausted(max_attempts=3)
    assert not any(b["delegation_id"] == packed["delegation_id"] for b in dead["blocked"])

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT status FROM delegations WHERE id=?",
            (packed["delegation_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending"


def test_finished_outcome_marks_child_task_done(db, monkeypatch):
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    packed = _delegated()
    _approve(packed["task_id"])
    launch = RecordingLaunchBoundary()

    tick = scheduler.scheduler_tick(launch=launch)
    run_id = tick["launched"][0]["run_id"]

    finished = scheduler.record_run_outcome(
        run_id,
        "finished",
        evidence_ref="https://example.test/pr/99",
    )
    assert finished["ok"] is True

    task = next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == packed["task_id"])
    assert task["status"] == "done"
    assert task.get("evidence_ref") == "https://example.test/pr/99"


def test_approval_row_launch_failure_retries_on_next_tick(db, monkeypatch):
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
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

    fail_launch = RecordingLaunchBoundary(fail_with="spawn_unavailable")
    first = scheduler.scheduler_tick(launch=fail_launch, lease_ttl_s=60.0)
    assert first["launched"] == []

    second = scheduler.scheduler_tick(launch=RecordingLaunchBoundary(), lease_ttl_s=60.0)
    assert len(second["launched"]) == 1


def test_real_spawn_flag_selects_cli_spawner(monkeypatch):
    """Codex PR #292. Timer ticks must opt into CliProcessSpawner via UA_DISPATCH_REAL_SPAWN."""
    from unified_agent import config
    from unified_agent.dispatch.launch import (
        CliProcessSpawner,
        StubProcessSpawner,
        scheduler_launch_boundary,
    )

    monkeypatch.delenv("UA_DISPATCH_REAL_SPAWN", raising=False)
    assert config.dispatch_real_spawn() is False
    assert isinstance(scheduler_launch_boundary().spawner, StubProcessSpawner)

    monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
    assert config.dispatch_real_spawn() is True
    assert isinstance(scheduler_launch_boundary().spawner, CliProcessSpawner)


def test_scheduler_tick_resolves_real_spawn_boundary_lazily(db, monkeypatch):
    """PR #292. UA_DISPATCH_REAL_SPAWN is read at tick time, not construction."""
    from unified_agent.dispatch.launch import RecordingLaunchBoundary

    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    packed = _delegated()
    _approve(packed["task_id"])

    resolved: list[str] = []
    real_boundary = scheduler.scheduler_launch_boundary

    def tracking_boundary():
        boundary = real_boundary()
        resolved.append(type(boundary.spawner).__name__)
        return RecordingLaunchBoundary()

    monkeypatch.setattr(scheduler, "scheduler_launch_boundary", tracking_boundary)

    sched = scheduler.DispatchScheduler()

    monkeypatch.delenv("UA_DISPATCH_REAL_SPAWN", raising=False)
    first = sched.tick()
    assert first["status"] == "ok"
    assert len(first["launched"]) == 1
    assert resolved == ["StubProcessSpawner"]

    packed2 = _delegated()
    _approve(packed2["task_id"])
    resolved.clear()
    monkeypatch.setenv("UA_DISPATCH_REAL_SPAWN", "1")
    second = sched.tick()
    assert second["status"] == "ok"
    assert len(second["launched"]) == 1
    assert resolved == ["CliProcessSpawner"]


def test_interactive_only_delegation_is_never_auto_launched(db, monkeypatch):
    packed = _delegated()
    _approve(packed["task_id"])
    ready_before = scheduler.list_ready_candidates(limit=20)
    candidate = next(row for row in ready_before if row["task_id"] == packed["task_id"])
    tasks.task_update(packed["task_id"], lane="interactive-only")

    assert scheduler.list_ready_candidates(limit=20) == []

    # The candidate can become stale between selection and the atomic claim.
    monkeypatch.setattr(scheduler, "list_ready_candidates", lambda **_: [candidate])
    launch = RecordingLaunchBoundary()
    out = scheduler.scheduler_tick(force=True, launch=launch)
    assert out["launched"] == []
    assert out["skipped"][0]["reason"] == "lane_mismatch"
    assert launch.calls == []


def _launched_run(monkeypatch) -> tuple[dict, str]:
    monkeypatch.setenv("UA_DISPATCH_SCHEDULER_ENABLED", "1")
    packed = _delegated()
    _approve(packed["task_id"])
    tick = scheduler.scheduler_tick(launch=RecordingLaunchBoundary())
    return packed, tick["launched"][0]["run_id"]


def _task_row(task_id: int) -> dict:
    return next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == task_id)


def _crash_on_done(monkeypatch) -> None:
    """Simulate a crash after the delegation commit, before the task closes."""
    real = scheduler.tasks_tool.task_update

    def crashing(task_id, *args, **kwargs):
        if kwargs.get("status") == "done":
            raise RuntimeError("process died between commits")
        return real(task_id, *args, **kwargs)

    monkeypatch.setattr(scheduler.tasks_tool, "task_update", crashing)


def test_finished_run_whose_task_close_crashed_self_heals(db, monkeypatch):
    """Task #1916 (audit O3): the delegation commit and the task-done write are
    separate; a crash between them left a finished run on an open task that
    nothing ever closed. The reconciler replays the close."""
    packed, run_id = _launched_run(monkeypatch)
    with monkeypatch.context() as m:
        _crash_on_done(m)
        with pytest.raises(RuntimeError):
            scheduler.record_run_outcome(run_id, "finished", evidence_ref="https://e.test/pr/7")
    assert coordinator.get_run(run_id)["state"] == "finished"
    assert _task_row(packed["task_id"])["status"] != "done"

    healed = scheduler.reconcile_finished_runs()
    assert [h["run_id"] for h in healed["healed"]] == [run_id]
    task = _task_row(packed["task_id"])
    assert task["status"] == "done"
    assert task.get("evidence_ref") == "https://e.test/pr/7"
    assert f"run={run_id}" in task["notes"]

    # Idempotent: a second pass finds nothing to do.
    assert scheduler.reconcile_finished_runs()["healed"] == []


@pytest.mark.parametrize("status", ["open", "doing"])
@pytest.mark.parametrize("defer", [None, "bounded", "failure"])
def test_hygiene_preserves_finished_run_recovery_before_queue_changes(
    db, monkeypatch, status, defer
):
    packed, run_id = _launched_run(monkeypatch)
    with monkeypatch.context() as m:
        _crash_on_done(m)
        with pytest.raises(RuntimeError):
            scheduler.record_run_outcome(run_id, "finished")
    now = datetime.now(UTC)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status=?, priority='high', created_at=?, updated_at=?, "
            "claimed_by=?, claimed_at=?, lock_expires_at=? WHERE id=?",
            (
                status,
                (now - timedelta(days=30)).isoformat(),
                (now - timedelta(minutes=2)).isoformat(),
                "Desktop_Agent1" if status == "doing" else None,
                (now - timedelta(hours=2)).isoformat() if status == "doing" else None,
                (now - timedelta(hours=1)).isoformat() if status == "doing" else None,
                packed["task_id"],
            ),
        )
        conn.execute(
            "UPDATE dispatch_runs SET updated_at=? WHERE run_id=?",
            ((now - timedelta(minutes=1)).isoformat(), run_id),
        )
        conn.commit()
    finally:
        conn.close()
    before = _task_row(packed["task_id"])
    with monkeypatch.context() as m:
        if defer:
            # A bounded pass or transient per-row error leaves this recovery
            # for the next pass; hygiene must not erase its eligibility.
            def postponed(**_):
                if defer == "failure":
                    raise RuntimeError("transient completion write failure")
                return {"healed": [], "errors": []}

            m.setattr(scheduler, "reconcile_finished_runs", postponed)
        result = queue_hygiene.run_hygiene_pass(send_notifications=False)
    assert packed["task_id"] not in [r["task_id"] for r in result["demoted"] + result["released"]]
    if defer:
        assert _task_row(packed["task_id"])["updated_at"] == before["updated_at"]
        result = queue_hygiene.run_hygiene_pass(send_notifications=False)
    assert result["dispatch_reconciled"] == [{"run_id": run_id, "task_id": packed["task_id"]}]
    assert _task_row(packed["task_id"])["status"] == "done"


def test_reconciler_never_recloses_a_task_reopened_after_its_run(db, monkeypatch):
    packed, run_id = _launched_run(monkeypatch)
    assert scheduler.record_run_outcome(run_id, "finished")["ok"] is True
    tasks.task_update(packed["task_id"], status="open")

    assert scheduler.reconcile_finished_runs()["healed"] == []
    assert _task_row(packed["task_id"])["status"] == "open"


def test_reconciler_skips_a_task_touched_after_the_run_finished(db, monkeypatch):
    """No outcome note (the crash window) but the task moved on afterwards:
    someone reopened or re-planned it, so the old run must not close it."""
    packed, run_id = _launched_run(monkeypatch)
    with monkeypatch.context() as m:
        _crash_on_done(m)
        with pytest.raises(RuntimeError):
            scheduler.record_run_outcome(run_id, "finished")
    later = (datetime.now(UTC) + timedelta(minutes=5)).isoformat(timespec="seconds")
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (later, packed["task_id"]))
        conn.commit()
    finally:
        conn.close()

    assert scheduler.reconcile_finished_runs()["healed"] == []
    assert _task_row(packed["task_id"])["status"] != "done"


def test_superseded_check_and_transition_share_one_transaction(db, monkeypatch):
    """Task #1916 (audit O3): the superseding-run read and the terminal write
    must run on one connection inside one write transaction, so no run can be
    launched between the check and the write."""
    packed, run_id = _launched_run(monkeypatch)
    seen: list[bool] = []
    real = coordinator.transition_run

    def spy(conn, *args, **kwargs):
        seen.append(conn.in_transaction)
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(coordinator, "transition_run", spy)
    assert scheduler.record_run_outcome(run_id, "finished")["ok"] is True
    assert seen == [True]


def test_reconciler_respects_later_edit_within_same_second(db, monkeypatch):
    packed, rid = _launched_run(monkeypatch)
    with monkeypatch.context() as m:
        _crash_on_done(m)
        with pytest.raises(RuntimeError):
            scheduler.record_run_outcome(rid, "finished")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE dispatch_runs SET updated_at='2026-10-07T01:00:00.100000+00:00' WHERE run_id=?",
            (rid,),
        )
        conn.execute(
            "UPDATE tasks SET updated_at='2026-10-07T01:00:00.200000+00:00' WHERE id=?",
            (packed["task_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    assert scheduler.reconcile_finished_runs()["healed"] == []
    assert _task_row(packed["task_id"])["status"] != "done"


def test_reconciler_skips_ineligible_rows_before_limit(db, monkeypatch):
    first, first_rid = _launched_run(monkeypatch)
    second, second_rid = _launched_run(monkeypatch)
    with monkeypatch.context() as m:
        _crash_on_done(m)
        for rid in [first_rid, second_rid]:
            with pytest.raises(RuntimeError):
                scheduler.record_run_outcome(rid, "finished")
    tasks.task_update(second["task_id"], append_note="Human replanned this task")
    assert scheduler.reconcile_finished_runs(limit=1)["healed"] == [
        {"run_id": first_rid, "task_id": first["task_id"]}
    ]


def test_reconciler_close_fences_new_dispatch_inside_task_transaction(db, monkeypatch):
    packed, rid = _launched_run(monkeypatch)
    with monkeypatch.context() as m:
        _crash_on_done(m)
        with pytest.raises(RuntimeError):
            scheduler.record_run_outcome(rid, "finished")
    real = scheduler.tasks_tool.task_update

    def concurrent_launch(task_id, *args, **kwargs):
        if kwargs.get("status") == "done":
            conn = storage.connect()
            try:
                # Same task version: the dispatch fence itself must catch this.
                row = coordinator.get_run(rid)
                conn.execute(
                    "INSERT INTO dispatch_runs (run_id,task_id,state,agent_label,actor,created_at,updated_at) VALUES ('newer-live',?,'running',?,?,?,?)",
                    (task_id, row["agent_label"], row["actor"], storage.now(), storage.now()),
                )
                conn.commit()
            finally:
                conn.close()
        return real(task_id, *args, **kwargs)

    monkeypatch.setattr(scheduler.tasks_tool, "task_update", concurrent_launch)
    assert scheduler.reconcile_finished_runs()["errors"]
    assert _task_row(packed["task_id"])["status"] != "done"


def test_reconciler_repairs_legacy_done_task_with_missing_notes(db, monkeypatch):
    packed, rid = _launched_run(monkeypatch)
    with monkeypatch.context() as m:
        _crash_on_done(m)
        with pytest.raises(RuntimeError):
            scheduler.record_run_outcome(rid, "finished")
    tasks.task_update(packed["task_id"], status="done", actor="dispatch-scheduler")
    assert scheduler.reconcile_finished_runs()["healed"] == [
        {"run_id": rid, "task_id": packed["task_id"]}
    ]
    notes = _task_row(packed["task_id"])["notes"]
    assert scheduler._dispatch_outcome_noted(notes, state="finished", run_id=rid)
    assert f"[follow-up: pending run={rid}" in notes
