"""Live-claimant lease renewal (task #1337).

Queue hygiene and system_doctor used to reopen any ``doing`` task whose 1-hour
claim lease had lapsed, even while the claiming agent was online and working.
The agent then re-claimed it, which read as ``task_reassignment_thrash`` and made
briefings report live work as "UNCLAIMED". A lapsed lease is now renewed when the
claimant has been seen on the hub recently *and* the task itself was touched
recently; genuinely abandoned claims are still released.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import monitor, queue_hygiene, storage
from unified_agent.tools import doctor, tasks
from unified_agent.tools.tasks import task_add, task_claim

FIXED_NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(queue_hygiene, "_clock", lambda: FIXED_NOW)
    monkeypatch.setattr(queue_hygiene, "_now_iso", lambda: FIXED_NOW.isoformat(timespec="seconds"))
    monkeypatch.setattr(queue_hygiene, "_notify_best_effort", lambda message: True)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _exec(sql: str, params: tuple) -> None:
    conn = storage.connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _row(task_id: int):
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()


def _seen(label: str, when: datetime) -> None:
    ts = _iso(when)
    _exec(
        "INSERT INTO agents(label, last_seen, last_seen_via, created_at, updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(label) DO UPDATE SET last_seen=excluded.last_seen, "
        "last_seen_via=excluded.last_seen_via",
        (label, ts, "hub_request", ts, ts),
    )


def _claimed(title: str, *, agent: str = "Desktop_Agent2", touched_hours_ago: float = 1) -> int:
    t = task_add(title)
    task_claim(t["id"], agent=agent, timeout_seconds=3600)
    claimed_at = FIXED_NOW - timedelta(hours=2)
    _exec(
        "UPDATE tasks SET claimed_at=?, lock_expires_at=?, updated_at=? WHERE id=?",
        (
            _iso(claimed_at),
            _iso(claimed_at + timedelta(hours=1)),
            _iso(FIXED_NOW - timedelta(hours=touched_hours_ago)),
            t["id"],
        ),
    )
    # task_claim stamps agents.last_seen with the real wall clock. Pin that
    # stamp to the (fixed) claim time too; left on the real clock it lands
    # after FIXED_NOW once the real day passes 12:00 UTC, and reads as live
    # post-claim activity for a claimant that was never seen.
    _exec(
        "UPDATE agents SET last_seen=? WHERE label=? AND last_seen_via='agent_event'",
        (_iso(claimed_at), agent),
    )
    return t["id"]


class TestHygieneRenewal:
    def test_live_claimant_on_active_task_is_renewed_not_released(self):
        task_id = _claimed("Long running work")
        _seen("Desktop_Agent2", FIXED_NOW - timedelta(minutes=5))
        before = _row(task_id)

        res = queue_hygiene.run_hygiene_pass()

        assert res["released"] == []
        assert [r["task_id"] for r in res["renewed"]] == [task_id]
        row = _row(task_id)
        assert row["status"] == "doing"
        assert row["claimed_by"] == "Desktop_Agent2"
        assert row["lock_expires_at"] == _iso(FIXED_NOW + timedelta(hours=1))
        # Renewal must not look like task activity, or an idle agent would keep
        # an abandoned claim forever.
        assert row["updated_at"] == before["updated_at"]
        assert "[hygiene: stale-claim @" not in (row["notes"] or "")

    def test_a_parked_task_never_has_its_lapsed_lease_renewed(self):
        # Task #1848: park refreshes updated_at; that must not make a hidden task
        # look "recently touched" and earn a renewed lease.
        task_id = _claimed("Parked while its lease lapsed")
        _seen("Desktop_Agent2", FIXED_NOW - timedelta(minutes=5))
        _exec(
            "UPDATE tasks SET archived_at=?, updated_at=? WHERE id=?",
            (_iso(FIXED_NOW), _iso(FIXED_NOW), task_id),
        )

        res = queue_hygiene.run_hygiene_pass()

        assert res["renewed"] == []
        assert _row(task_id)["lock_expires_at"] != _iso(FIXED_NOW + timedelta(hours=1))

    def test_offline_claimant_is_kept_without_a_recorded_end(self):
        task_id = _claimed("Quiet lane")
        _seen("Desktop_Agent2", FIXED_NOW - timedelta(hours=3))

        res = queue_hygiene.run_hygiene_pass()

        assert res["released"] == []
        assert [k["task_id"] for k in res["leases_kept"]] == [task_id]
        assert _row(task_id)["status"] == "doing"

    def test_offline_claimant_with_a_recorded_end_is_released(self):
        task_id = _claimed("Cancelled run, dead lane")
        _seen("Desktop_Agent2", FIXED_NOW - timedelta(hours=3))
        _exec("UPDATE tasks SET cancel_requested=1 WHERE id=?", (task_id,))

        res = queue_hygiene.run_hygiene_pass()

        assert [r["task_id"] for r in res["released"]] == [task_id]
        row = _row(task_id)
        assert (row["status"], row["claimed_by"], row["cancel_requested"]) == ("open", None, 0)

    def test_live_claimant_with_an_untouched_task_keeps_it_unrenewed(self):
        task_id = _claimed("Claimed then quiet", touched_hours_ago=25)
        _seen("Desktop_Agent2", FIXED_NOW - timedelta(minutes=5))

        res = queue_hygiene.run_hygiene_pass()

        assert res["released"] == []
        assert res["renewed"] == []
        assert _row(task_id)["status"] == "doing"

    def test_a_claimant_with_no_agent_row_is_released(self):
        task_id = _claimed("Nobody ever seen", agent="Desktop_Agent4")
        _exec("DELETE FROM agents WHERE label=?", ("Desktop_Agent4",))

        res = queue_hygiene.run_hygiene_pass()

        assert [r["task_id"] for r in res["released"]] == [task_id]

    def test_windows_are_configurable(self, monkeypatch):
        task_id = _claimed("Tight window")
        _seen("Desktop_Agent2", FIXED_NOW - timedelta(minutes=20))
        monkeypatch.setenv("UA_HYGIENE_LIVE_CLAIMANT_MINUTES", "10")

        res = queue_hygiene.run_hygiene_pass()

        assert res["renewed"] == []
        assert _row(task_id)["status"] == "doing"

    def test_dry_run_renews_nothing(self):
        task_id = _claimed("Dry run")
        _seen("Desktop_Agent2", FIXED_NOW - timedelta(minutes=5))
        before = _row(task_id)["lock_expires_at"]

        res = queue_hygiene.run_hygiene_pass(dry_run=True)

        assert [r["task_id"] for r in res["renewed"]] == [task_id]
        assert _row(task_id)["lock_expires_at"] == before


class TestDoctorRenewal:
    def test_doctor_reclaim_spares_a_live_claimant(self, monkeypatch):
        monkeypatch.setattr(doctor.storage, "now", lambda: _iso(FIXED_NOW))
        live = _claimed("Doctor must not steal this")
        dead = _claimed("Doctor may release this", agent="Desktop_Agent4")
        _seen("Desktop_Agent2", FIXED_NOW - timedelta(minutes=5))
        _exec("DELETE FROM agents WHERE label=?", ("Desktop_Agent4",))

        res = doctor.reclaim_stale_task_locks()

        assert [t["id"] for t in res["reclaimed_tasks"]] == [dead]
        assert res["renewed_count"] == 1
        assert _row(live)["status"] == "doing"
        # Its claim had an open run and its owner is gone: interrupted, held.
        assert _row(dead)["status"] == "blocked"


class TestClaimRenewalEvent:
    @pytest.mark.parametrize("expired", [False, True])
    def test_same_agent_reclaim_only_renews_a_live_lease(self, monkeypatch, expired):
        class ClaimClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return FIXED_NOW

        monkeypatch.setattr(storage, "now", lambda: _iso(FIXED_NOW))
        monkeypatch.setattr(tasks, "datetime", ClaimClock)
        t = task_add("Lease renewal or new claim")
        first = task_claim(t["id"], agent="Desktop_Agent2", timeout_seconds=3600)
        assert first["current_run_id"] is not None
        if expired:
            _exec(
                "UPDATE tasks SET lock_expires_at=? WHERE id=?",
                (_iso(FIXED_NOW - timedelta(minutes=5)), t["id"]),
            )
        successor = task_claim(t["id"], agent="Desktop_Agent2", timeout_seconds=3600)

        conn = storage.connect()
        try:
            claims = conn.execute(
                "SELECT COUNT(*) FROM agent_events WHERE kind='task' AND ref_id=? "
                "AND summary LIKE '%->doing%'",
                (str(t["id"]),),
            ).fetchone()[0]
        finally:
            conn.close()
        assert claims == (2 if expired else 1)
        # A lapsed lease needs a new fencing token: an old executor may still
        # be running even when the same agent starts the successor claim.
        assert (successor["current_run_id"] != first["current_run_id"]) is expired


class TestThrashRule:
    def _enter_doing(self, task_id: int, agent: str, n: int) -> None:
        for _ in range(n):
            storage.record_agent_event(
                "task", ref_id=str(task_id), agent=agent, summary="status open->doing: x"
            )

    def _thrash_subjects(self) -> set[str]:
        return {
            a["subject"]
            for a in monitor.check_anomalies()["anomalies"]
            if a["rule"] == "task_reassignment_thrash"
        }

    def test_single_agent_resuming_its_own_task_is_not_thrash(self):
        t = task_add("One agent, blocked and resumed")
        self._enter_doing(t["id"], "Desktop_Agent2", 3)
        assert f"task:{t['id']}" not in self._thrash_subjects()

    def test_two_agents_trading_a_task_is_thrash(self):
        t = task_add("Traded between lanes")
        self._enter_doing(t["id"], "Desktop_Agent2", 2)
        self._enter_doing(t["id"], "Desktop_Agent1", 1)
        assert f"task:{t['id']}" in self._thrash_subjects()

    def test_single_agent_flapping_hard_is_still_thrash(self):
        t = task_add("Flapping")
        self._enter_doing(t["id"], "Desktop_Agent2", 6)
        assert f"task:{t['id']}" in self._thrash_subjects()


class TestReviewFollowups:
    """PR #754 review: no lease growth, event liveness, WIP cap on lapsed re-claim."""

    def test_repeated_renewals_do_not_grow_the_lease(self, monkeypatch):
        task_id = _claimed("Renewed many times")
        _seen("Desktop_Agent2", FIXED_NOW - timedelta(minutes=5))
        queue_hygiene.run_hygiene_pass()
        assert _row(task_id)["lock_expires_at"] == _iso(FIXED_NOW + timedelta(hours=1))
        later = FIXED_NOW + timedelta(hours=2)
        monkeypatch.setattr(queue_hygiene, "_clock", lambda: later)
        _seen("Desktop_Agent2", later - timedelta(minutes=5))
        queue_hygiene.run_hygiene_pass()
        assert _row(task_id)["lock_expires_at"] == _iso(later + timedelta(hours=1))

    def test_agent_event_after_claim_counts_as_live(self):
        task_id = _claimed("Agent journaling, no raw hub stamp")
        ts = _iso(FIXED_NOW - timedelta(minutes=5))
        _exec(
            "INSERT INTO agents(label, last_seen, last_seen_via, created_at, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(label) DO UPDATE SET last_seen=excluded.last_seen, "
            "last_seen_via=excluded.last_seen_via",
            ("Desktop_Agent2", ts, "agent_event", ts, ts),
        )
        res = queue_hygiene.run_hygiene_pass()
        assert [r["task_id"] for r in res["renewed"]] == [task_id]

    def test_claim_stamp_alone_is_not_liveness(self):
        task_id = _claimed("Claimed then died")
        claimed_at = _row(task_id)["claimed_at"]
        _exec(
            "UPDATE agents SET last_seen=?, last_seen_via='agent_event' WHERE label=?",
            (claimed_at, "Desktop_Agent2"),
        )
        res = queue_hygiene.run_hygiene_pass()
        assert res["renewed"] == []

    def test_doctor_stamp_is_not_liveness(self):
        task_id = _claimed("Only doctor saw it")
        ts = _iso(FIXED_NOW - timedelta(minutes=5))
        _exec(
            "UPDATE agents SET last_seen=?, last_seen_via='system_doctor' WHERE label=?",
            (ts, "Desktop_Agent2"),
        )
        res = queue_hygiene.run_hygiene_pass()
        assert [r["task_id"] for r in res["released"]] == [task_id]


@pytest.mark.parametrize("cleanup", ["doctor", "hygiene"])
@pytest.mark.parametrize("live", [False, True])
def test_exact_expiry_listing_and_cleanup_agree(monkeypatch, cleanup, live):
    monkeypatch.setattr(storage, "now", lambda: _iso(FIXED_NOW))
    task_id = _claimed("Exact expiry cleanup")
    _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_iso(FIXED_NOW), task_id))
    _seen("Desktop_Agent2", FIXED_NOW - timedelta(minutes=5 if live else 180))
    assert task_id in {t["id"] for t in tasks.task_list(status="doing", stale_claims=True)["tasks"]}
    if cleanup == "doctor":
        result = doctor.reclaim_stale_task_locks()
        assert result["renewed_count"] == int(live)
        assert result["reclaimed_count"] == 0
        assert [k["id"] for k in result["kept_unknown"]] == ([] if live else [task_id])
    else:
        result = queue_hygiene.run_hygiene_pass()
        assert [r["task_id"] for r in result["renewed"]] == ([task_id] if live else [])
        assert result["released"] == []
    row = _row(task_id)
    assert row["status"] == "doing"
    assert row["claimed_by"] == "Desktop_Agent2"
    still_listed = {t["id"] for t in tasks.task_list(status="doing", stale_claims=True)["tasks"]}
    assert still_listed == (set() if live else {task_id})
