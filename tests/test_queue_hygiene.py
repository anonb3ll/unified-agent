"""Queue hygiene automation tests (task #729 / Lane A Increment A2).

Ground truth:
- stale claim (lock_expires_at passed) → released to open, cleared claim
  columns, ONE notification; a re-run releases nothing and notifies nothing.
- open >14d → priority demoted exactly one step (high→med→low; low stays),
  once; re-run is a no-op.
- done >30d → archived_at stamped, once; re-run is a no-op.
- dry_run performs no writes.
- healthy state → pass is a no-op end to end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import queue_hygiene, storage
from unified_agent.tools.tasks import task_add, task_claim, task_update

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(queue_hygiene, "_clock", lambda: FIXED_NOW)


@pytest.fixture(autouse=True)
def _silent_notify(monkeypatch):
    """Capture best-effort notifications without a real bridge."""
    sent: list[str] = []

    def fake_notify(message: str) -> dict:
        sent.append(message)
        return {"delivered": True}

    monkeypatch.setattr("unified_agent.tools.notify.notify", fake_notify, raising=False)
    # The pass imports the seam lazily inside _notify_best_effort; patch the
    # function module attribute it resolves through.
    monkeypatch.setattr(
        queue_hygiene, "_notify_best_effort", lambda message: (sent.append(message), True)[1]
    )
    yield sent


def _get_task(task_id: int):
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()


def _set(task_id: int, **fields: str) -> None:
    conn = storage.connect()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))
        conn.commit()
    finally:
        conn.close()


# ── stale claims ─────────────────────────────────────────────────────


class TestStaleClaimRelease:
    def test_expired_lock_released_with_one_notification(self, _silent_notify):
        t = task_add("Stale claim victim")
        task_claim(t["id"], agent="Desktop_Agent1", timeout_seconds=3600)
        _set(
            t["id"],
            lock_expires_at=(FIXED_NOW - timedelta(hours=1)).isoformat(),
            cancel_requested=1,
        )

        res = queue_hygiene.run_hygiene_pass()
        assert [r["task_id"] for r in res["released"]] == [t["id"]]
        assert res["notified"] is True
        assert len(_silent_notify) == 1
        assert f"#{t['id']}" in _silent_notify[0]

        row = _get_task(t["id"])
        assert row["status"] == "open"
        assert row["claimed_by"] is None
        assert row["claimed_at"] is None
        assert row["lock_expires_at"] is None
        assert "[hygiene: stale-claim @" in row["notes"]

    def test_expired_lock_with_a_quiet_known_owner_is_kept_and_listed(self, _silent_notify):
        t = task_add("Quiet owner")
        task_claim(t["id"], agent="server_agent3", timeout_seconds=3600)
        _set(t["id"], lock_expires_at=(FIXED_NOW - timedelta(hours=1)).isoformat())
        _upsert_agent("server_agent3", last_seen=(FIXED_NOW - timedelta(days=3)).isoformat())

        res = queue_hygiene.run_hygiene_pass()
        assert res["released"] == []
        assert [k["task_id"] for k in res["leases_kept"]] == [t["id"]]
        assert "owner kept" in res["leases_kept"][0]["warning"]
        assert _silent_notify == []
        row = _get_task(t["id"])
        assert (row["status"], row["claimed_by"]) == ("doing", "server_agent3")
        assert "[hygiene: stale-claim @" not in (row["notes"] or "")

    def test_interrupted_work_is_held_blocked_and_named_in_the_notification(self, _silent_notify):
        from unified_agent import run_records, storage

        t = task_add("Accepted then orphaned")
        claim = task_claim(t["id"], agent="Gone_Agent", timeout_seconds=3600)
        _set(t["id"], lock_expires_at=(FIXED_NOW - timedelta(hours=1)).isoformat())
        conn = storage.connect()
        try:
            conn.execute("DELETE FROM agents WHERE label='Gone_Agent'")
            conn.commit()
        finally:
            conn.close()

        res = queue_hygiene.run_hygiene_pass()
        assert [(r["task_id"], r["to_status"]) for r in res["released"]] == [(t["id"], "blocked")]
        assert res["released"][0]["interrupted_run"] == claim["current_run_id"]
        assert "interrupted work held as blocked" in _silent_notify[0]
        assert _get_task(t["id"])["status"] == "blocked"
        end = run_records.latest_run_end(storage.connect(), t["id"])
        assert (end["outcome"], end["to_status"]) == ("abandoned", "blocked")

    def test_live_lock_is_untouched(self, _silent_notify):
        t = task_add("Live claim")
        task_claim(t["id"], agent="Desktop_Agent1", timeout_seconds=3600)

        res = queue_hygiene.run_hygiene_pass()
        assert res["released"] == []
        row = _get_task(t["id"])
        assert row["status"] == "doing"
        assert row["claimed_by"] == "Desktop_Agent1"

    def test_second_run_is_a_full_noop(self, _silent_notify):
        t = task_add("Runs once")
        task_claim(t["id"], agent="Desktop_Agent1", timeout_seconds=3600)
        _set(
            t["id"],
            lock_expires_at=(FIXED_NOW - timedelta(hours=1)).isoformat(),
            cancel_requested=1,
        )

        first = queue_hygiene.run_hygiene_pass()
        assert len(first["released"]) == 1
        assert len(_silent_notify) == 1

        second = queue_hygiene.run_hygiene_pass()
        assert second["released"] == []
        assert second["demoted"] == []
        assert second["archived"] == []
        assert second["notified"] is False
        assert len(_silent_notify) == 1  # still exactly one notification

        row = _get_task(t["id"])
        assert row["status"] == "open"
        assert row["notes"].count("[hygiene: stale-claim @") == 1

    def test_reclaimed_and_abandoned_again_is_released_again(self, _silent_notify):
        """Task #1916: the stale-claim marker is per claim, not once per task."""
        t = task_add("Abandoned twice")
        task_claim(t["id"], agent="Desktop_Agent1", timeout_seconds=3600)
        _set(
            t["id"],
            lock_expires_at=(FIXED_NOW - timedelta(hours=1)).isoformat(),
            cancel_requested=1,
        )
        first = queue_hygiene.run_hygiene_pass()
        assert [r["task_id"] for r in first["released"]] == [t["id"]]

        # A second agent claims the released task, then abandons it too. Its
        # claim is a new generation (new claimed_at), so the first release's
        # marker must not shield it.
        task_claim(t["id"], agent="Desktop_Agent2", timeout_seconds=3600)
        _set(
            t["id"],
            claimed_at="2099-01-01T00:00:00+00:00",
            lock_expires_at=(FIXED_NOW - timedelta(minutes=5)).isoformat(),
            cancel_requested=1,
        )
        second = queue_hygiene.run_hygiene_pass()
        assert [r["task_id"] for r in second["released"]] == [t["id"]]
        assert second["released"][0]["claimed_by"] == "Desktop_Agent2"

        row = _get_task(t["id"])
        assert row["status"] == "open"
        assert row["claimed_by"] is None
        assert row["notes"].count("[hygiene: stale-claim @") == 2

        # And the second release is itself idempotent.
        third = queue_hygiene.run_hygiene_pass()
        assert third["released"] == []

    def test_marker_for_the_current_claim_still_skips(self, _silent_notify):
        """A marker naming the live claim generation keeps the pass idempotent."""
        t = task_add("Marker for current claim")
        task_claim(t["id"], agent="Desktop_Agent1", timeout_seconds=3600)
        claimed_at = _get_task(t["id"])["claimed_at"]
        _set(
            t["id"],
            lock_expires_at=(FIXED_NOW - timedelta(hours=1)).isoformat(),
            notes=f"[hygiene: stale-claim @ 2026-08-31T00:00:00+00:00Z claim={claimed_at}]",
        )
        res = queue_hygiene.run_hygiene_pass()
        assert res["released"] == []
        assert _get_task(t["id"])["status"] == "doing"

    def test_two_fresh_claims_in_one_second_are_released_independently(self, monkeypatch):
        fixed_second = storage.now()
        monkeypatch.setattr(storage, "now", lambda: fixed_second)
        t = task_add("Two abandoned claims in one second")
        first_claim = task_claim(t["id"], agent="Desktop_Agent1", timeout_seconds=3600)
        _set(
            t["id"],
            lock_expires_at=(FIXED_NOW - timedelta(hours=1)).isoformat(),
            cancel_requested=1,
        )
        assert len(queue_hygiene.run_hygiene_pass()["released"]) == 1

        second_claim = task_claim(t["id"], agent="Desktop_Agent2", timeout_seconds=3600)
        assert first_claim["claimed_at"] != second_claim["claimed_at"]
        _set(
            t["id"],
            lock_expires_at=(FIXED_NOW - timedelta(hours=1)).isoformat(),
            cancel_requested=1,
        )
        assert len(queue_hygiene.run_hygiene_pass()["released"]) == 1
        assert _get_task(t["id"])["notes"].count("[hygiene: stale-claim @") == 2
        assert queue_hygiene.run_hygiene_pass()["released"] == []

    def test_live_renewal_retains_claim_identity(self):
        t = task_add("Stable renewal identity")
        claimed = task_claim(t["id"], agent="Desktop_Agent1", timeout_seconds=3600)
        renewed = task_claim(t["id"], agent="Desktop_Agent1", timeout_seconds=7200)
        assert renewed["claimed_at"] == claimed["claimed_at"]
        assert renewed["current_run_id"] == claimed["current_run_id"]
        assert renewed["lock_expires_at"] > claimed["lock_expires_at"]

    def test_task_without_lock_never_released(self, _silent_notify):
        t = task_add("No lock set")
        task_update(t["id"], status="doing", worktree=False)

        res = queue_hygiene.run_hygiene_pass()
        assert res["released"] == []
        assert _get_task(t["id"])["status"] == "doing"


# ── aging open demotion ──────────────────────────────────────────────


class TestAgingOpenDemotion:
    def _aged(self, title: str, priority: str, *, days: float = 20, seconds: int = 0) -> int:
        t = task_add(title, priority=priority)
        _set(
            t["id"],
            created_at=(FIXED_NOW - timedelta(days=days, seconds=seconds)).isoformat(),
        )
        return t["id"]

    def test_aged_open_demoted_one_step(self, _silent_notify):
        tid = self._aged("Aged high", "high")
        res = queue_hygiene.run_hygiene_pass()
        assert [r["task_id"] for r in res["demoted"]] == [tid]
        assert res["demoted"][0]["new_priority"] == "med"
        assert _get_task(tid)["priority"] == "med"
        assert "[hygiene: aging-demote @" in _get_task(tid)["notes"]

    def test_med_demotes_to_low(self, _silent_notify):
        tid = self._aged("Aged med", "med")
        res = queue_hygiene.run_hygiene_pass()
        assert res["demoted"][0]["new_priority"] == "low"
        assert _get_task(tid)["priority"] == "low"

    def test_low_never_demoted_and_never_marked(self, _silent_notify):
        tid = self._aged("Aged low", "low")
        res = queue_hygiene.run_hygiene_pass()
        assert res["demoted"] == []
        assert _get_task(tid)["priority"] == "low"
        assert "[hygiene: aging-demote @" not in (_get_task(tid)["notes"] or "")

    def test_young_open_untouched(self, _silent_notify):
        t = task_add("Young open", priority="high")
        res = queue_hygiene.run_hygiene_pass()
        assert res["demoted"] == []
        assert _get_task(t["id"])["priority"] == "high"

    def test_boundary_exactly_14d_not_demoted_but_beyond_is(self, _silent_notify):
        # Spec: open >14d demotes — strictly greater, exactly 14d stays.
        exact = self._aged("Exactly 14d", "high", days=14)
        beyond = self._aged("Beyond 14d", "high", days=14, seconds=1)
        res = queue_hygiene.run_hygiene_pass()
        assert [r["task_id"] for r in res["demoted"]] == [beyond]
        assert _get_task(exact)["priority"] == "high"

    def test_demote_does_not_retrigger(self, _silent_notify):
        tid = self._aged("Demote once", "high")
        first = queue_hygiene.run_hygiene_pass()
        assert first["demoted"][0]["new_priority"] == "med"

        second = queue_hygiene.run_hygiene_pass()
        assert second["demoted"] == []
        assert _get_task(tid)["priority"] == "med"
        assert _get_task(tid)["notes"].count("[hygiene: aging-demote @") == 1
        assert len(_silent_notify) == 1

    def test_doing_task_is_not_demoted(self, _silent_notify):
        t = task_add("Aged doing", priority="high")
        task_update(t["id"], status="doing", worktree=False)
        _set(t["id"], created_at=(FIXED_NOW - timedelta(days=30)).isoformat())

        res = queue_hygiene.run_hygiene_pass()
        assert res["demoted"] == []
        assert _get_task(t["id"])["priority"] == "high"


# ── auto-archive ─────────────────────────────────────────────────────


class TestAutoArchive:
    def _old_done(self, title: str, *, days: float = 45) -> int:
        t = task_add(title)
        task_update(t["id"], status="done", actor="test-agent")
        _set(
            t["id"],
            completed_at=(FIXED_NOW - timedelta(days=days)).isoformat(),
        )
        return t["id"]

    def test_old_done_gets_archived_at(self, _silent_notify):
        tid = self._old_done("Old done")
        res = queue_hygiene.run_hygiene_pass()
        assert [r["task_id"] for r in res["archived"]] == [tid]
        assert _get_task(tid)["archived_at"] is not None

    def test_recent_done_untouched(self, _silent_notify):
        tid = self._old_done("Recent done", days=2)
        res = queue_hygiene.run_hygiene_pass()
        assert res["archived"] == []
        assert _get_task(tid)["archived_at"] is None

    def test_archive_is_idempotent(self, _silent_notify):
        tid = self._old_done("Archive once")
        first = queue_hygiene.run_hygiene_pass()
        assert len(first["archived"]) == 1
        stamp = _get_task(tid)["archived_at"]

        second = queue_hygiene.run_hygiene_pass()
        assert second["archived"] == []
        assert _get_task(tid)["archived_at"] == stamp
        assert len(_silent_notify) == 1

    def test_done_without_completed_at_not_archived(self, _silent_notify):
        t = task_add("Done no completion stamp")
        task_update(t["id"], status="done", actor="test-agent")
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE tasks SET completed_at=NULL, updated_at=? WHERE id=?",
                ((FIXED_NOW - timedelta(days=60)).isoformat(), t["id"]),
            )
            conn.commit()
        finally:
            conn.close()

        res = queue_hygiene.run_hygiene_pass()
        assert res["archived"] == []
        assert _get_task(t["id"])["archived_at"] is None


# ── pass-level behavior ──────────────────────────────────────────────


class TestPassLevel:
    def test_healthy_queue_is_a_noop(self, _silent_notify):
        task_add("Healthy open")
        res = queue_hygiene.run_hygiene_pass()
        assert res == {
            "renewed": [],
            "released": [],
            "leases_kept": [],
            "demoted": [],
            "archived": [],
            "closed_on_merge": [],
            "would_close_on_merge": [],
            "close_on_merge_error": None,
            "dispatch_reconciled": [],
            "dispatch_reconcile_error": None,
            "dispatch_spawn_unsettled": [],
            "workflow_plans": {"checked": 0, "closed": [], "left_alone": 0, "unverifiable": []},
            "session_owed_reports": 0,
            "dead_letters": {"mode": "notify", "dead_letters": [], "notices": 0, "errors": []},
            "stranded_mail": {"mode": "notify", "stranded": [], "notices": 0, "errors": []},
            "command_notices": {"sent": [], "errors": []},
            "approval_receipts": {"scanned": 0},
            "work_proposals": {"recovered_forward": [], "rolled_back": []},
            "design_panels": {"revealed": []},
            "friction": {
                "ingested": {"sessions": 0, "inserted": 0, "already_ingested": 0, "backlog": 0},
                "closed": [],
                "reopened": [],
                "pending": [],
            },
            "liveness": {
                "deployments": [],
                "mode": "report",
                "dry_run": False,
                "checked": 0,
                "monitors": [],
                "none": [],
                "reported": [],
                "blocked": [],
                "retries_exhausted": [],
                "wait_chains": [],
                "errors": [],
            },
            "dispatch_artifacts": {"retention_days": 7.0, "expired": [], "dry_run": False},
            "completion_returns": {
                "delivered": [],
                "failed": [],
                "abandoned": [],
                "expired": [],
                "superseded": [],
                "waiting": [],
                "skipped": [],
            },
            "ready_notices": {"enabled": True, "sent": [], "skipped": [], "failed": []},
            "notified": False,
            "dry_run": False,
        }
        assert _silent_notify == []

    def test_completion_return_drain_respects_send_notifications(self, _silent_notify, monkeypatch):
        """Task #1751: a completion return is a chat message, so a pass told
        not to notify leaves the outbox alone."""
        from unified_agent import completion_return

        drains: list[dict] = []
        monkeypatch.setattr(
            completion_return, "drain_pending", lambda **kw: drains.append(kw) or {"ok": True}
        )
        res = queue_hygiene.run_hygiene_pass(send_notifications=False)
        assert res["completion_returns"] is None and drains == []
        res = queue_hygiene.run_hygiene_pass()
        assert res["completion_returns"] == {"ok": True} and len(drains) == 1

    def test_dry_run_makes_no_writes(self, _silent_notify):
        t = task_add("Dry-run stale")
        task_claim(t["id"], agent="Desktop_Agent1", timeout_seconds=3600)
        _set(
            t["id"],
            lock_expires_at=(FIXED_NOW - timedelta(hours=1)).isoformat(),
            cancel_requested=1,
        )

        res = queue_hygiene.run_hygiene_pass(dry_run=True)
        assert [r["task_id"] for r in res["released"]] == [t["id"]]
        assert res["dry_run"] is True

        row = _get_task(t["id"])
        assert row["status"] == "doing"  # unchanged
        assert row["lock_expires_at"] is not None
        assert "[hygiene:" not in (row["notes"] or "")
        assert _silent_notify == []  # dry run never notifies

    def test_all_three_actions_in_one_pass_one_notification(self, _silent_notify):
        stale = task_add("Stale")
        task_claim(stale["id"], agent="Desktop_Agent1", timeout_seconds=3600)
        _set(
            stale["id"],
            lock_expires_at=(FIXED_NOW - timedelta(hours=1)).isoformat(),
            cancel_requested=1,
        )

        aged = task_add("Aged", priority="high")
        _set(aged["id"], created_at=(FIXED_NOW - timedelta(days=20)).isoformat())

        old = task_add("Old done")
        task_update(old["id"], status="done", actor="test-agent")
        _set(old["id"], completed_at=(FIXED_NOW - timedelta(days=40)).isoformat())

        res = queue_hygiene.run_hygiene_pass()
        assert len(res["released"]) == 1
        assert len(res["demoted"]) == 1
        assert len(res["archived"]) == 1
        assert len(_silent_notify) == 1
        assert all(f"#{tid}" in _silent_notify[0] for tid in (stale["id"], aged["id"], old["id"]))


# --- cancellation blocks live-claim renewal (task #1525) --------------------


def _upsert_agent(label: str, *, last_seen: str, last_seen_via: str = "heartbeat") -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agents (label, kind, known, first_seen, last_seen, last_seen_via, "
            "created_at, updated_at) VALUES (?, 'agent', 1, ?, ?, ?, ?, ?) "
            "ON CONFLICT(label) DO UPDATE SET last_seen=excluded.last_seen, "
            "last_seen_via=excluded.last_seen_via",
            (label, last_seen, last_seen, last_seen_via, last_seen, last_seen),
        )
        conn.commit()
    finally:
        conn.close()


def test_renew_live_claims_extends_an_active_agents_lapsed_lease():
    """Ground truth for the positive case this module's docstring describes,
    so the cancellation test below is a controlled contrast, not the only
    coverage `renew_live_claims` has."""
    t = task_add("active work", lane="dispatch-ready")
    with_claim = task_claim(t["id"], "server_agent3", expected_lane="dispatch-ready")
    _set(
        t["id"],
        claimed_at=(FIXED_NOW - timedelta(minutes=10)).isoformat(),
        lock_expires_at=(FIXED_NOW - timedelta(minutes=5)).isoformat(),
        updated_at=(FIXED_NOW - timedelta(minutes=5)).isoformat(),
    )
    _upsert_agent("server_agent3", last_seen=(FIXED_NOW - timedelta(minutes=2)).isoformat())
    conn = storage.connect()
    try:
        renewed = queue_hygiene.renew_live_claims(conn, FIXED_NOW)
        conn.commit()
    finally:
        conn.close()
    assert [r["task_id"] for r in renewed] == [with_claim["id"]]
    assert _get_task(t["id"])["lock_expires_at"] > FIXED_NOW.isoformat()


def test_renew_live_claims_never_extends_a_claim_whose_run_is_recorded_ended():
    """Task #2483: the label being busy elsewhere says nothing about this claim. A run
    recorded as ended is not renewed, so the reaper still sees it in the same pass."""
    t = task_add("ended work", lane="dispatch-ready")
    task_claim(t["id"], "server_agent3", expected_lane="dispatch-ready")
    claimed = (FIXED_NOW - timedelta(minutes=10)).isoformat()
    lapsed = (FIXED_NOW - timedelta(minutes=5)).isoformat()
    _set(t["id"], claimed_at=claimed, lock_expires_at=lapsed, updated_at=lapsed)
    _upsert_agent("server_agent3", last_seen=(FIXED_NOW - timedelta(minutes=2)).isoformat())
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
            "created_at, updated_at) VALUES ('run-ended', ?, 'server_agent3', 'failed', "
            "'server_agent3', ?, ?)",
            (t["id"], claimed, lapsed),
        )
        conn.execute("UPDATE tasks SET current_run_id=NULL WHERE id=?", (t["id"],))
        conn.commit()
        renewed = queue_hygiene.renew_live_claims(conn, FIXED_NOW)
        conn.commit()
        assert renewed == []
        assert _get_task(t["id"])["lock_expires_at"] == lapsed
        reaped = queue_hygiene.lease_reaper.reap_stale_claim_leases(conn, now=FIXED_NOW)
        conn.commit()
    finally:
        conn.close()
    assert [(r.task_id, r.action) for r in reaped] == [(t["id"], "released")]
    assert _get_task(t["id"])["claimed_by"] is None


def test_renew_live_claims_never_extends_a_cancelled_run(monkeypatch):
    """PR #866 review: queue hygiene must not resurrect a cancelled lease just
    because its claimant is otherwise still active."""
    t = task_add("cancelled work", lane="dispatch-ready")
    task_claim(t["id"], "server_agent3", expected_lane="dispatch-ready")
    monkeypatch.setattr("unified_agent.config.verified_agent_identity", lambda: "server_agent3")
    task_update(t["id"], request_cancel=True, worktree=False)
    _set(
        t["id"],
        claimed_at=(FIXED_NOW - timedelta(minutes=10)).isoformat(),
        lock_expires_at=(FIXED_NOW - timedelta(minutes=5)).isoformat(),
        updated_at=(FIXED_NOW - timedelta(minutes=5)).isoformat(),
    )
    _upsert_agent("server_agent3", last_seen=(FIXED_NOW - timedelta(minutes=2)).isoformat())
    assert _get_task(t["id"])["cancel_requested"] == 1
    conn = storage.connect()
    try:
        renewed = queue_hygiene.renew_live_claims(conn, FIXED_NOW)
        conn.commit()
    finally:
        conn.close()
    assert renewed == []
    assert _get_task(t["id"])["lock_expires_at"] < FIXED_NOW.isoformat()


# ── finished dispatch runs (task #1916) ──────────────────────────────


def test_pass_reconciles_finished_dispatch_runs(monkeypatch):
    """The hygiene cron is what still runs on the hub after lane A's scheduler
    timer was retired (#1435), so it carries the finished-run self-heal."""
    from unified_agent.dispatch import scheduler

    calls: list[str] = []

    def fake_reconcile(**kwargs):
        calls.append(kwargs.get("actor", ""))
        return {"healed": [{"run_id": "run-1", "task_id": 1}], "errors": []}

    monkeypatch.setattr(scheduler, "reconcile_finished_runs", fake_reconcile)
    res = queue_hygiene.run_hygiene_pass()
    assert calls == ["queue-hygiene"]
    assert res["dispatch_reconciled"] == [{"run_id": "run-1", "task_id": 1}]

    dry = queue_hygiene.run_hygiene_pass(dry_run=True)
    assert len(calls) == 1  # dry run writes nothing
    assert dry["dispatch_reconciled"] == []


def test_pass_survives_a_failing_dispatch_reconcile(monkeypatch):
    from unified_agent.dispatch import scheduler

    def boom(**_kwargs):
        raise RuntimeError("db locked")

    monkeypatch.setattr(scheduler, "reconcile_finished_runs", boom)
    res = queue_hygiene.run_hygiene_pass()
    assert res["dispatch_reconciled"] == []
    assert "db locked" in res["dispatch_reconcile_error"]
