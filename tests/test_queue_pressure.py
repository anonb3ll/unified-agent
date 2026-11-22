"""Queue backpressure read (task #1430, defect 3).

Leases, lanes and WIP caps already exist; what was missing is an operator
signal. `queue_pressure()` is a read over the existing task and activity rows:
no new table, no new state, no side effects.
"""

from datetime import UTC, datetime, timedelta

from unified_agent import queue_hygiene, storage
from unified_agent.tools.tasks import task_add

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _set(task_id: int, **cols) -> None:
    conn = storage.connect()
    try:
        assignments = ", ".join(f"{k}=?" for k in cols)
        conn.execute(f"UPDATE tasks SET {assignments} WHERE id=?", (*cols.values(), task_id))
        conn.commit()
    finally:
        conn.close()


def _doing(title: str, agent: str | None, claimed_hours_ago: float, lock_in_hours: float, **kw):
    tid = task_add(title, **kw)["id"]
    _set(
        tid,
        status="doing",
        claimed_by=agent,
        claimed_at=_iso(NOW - timedelta(hours=claimed_hours_ago)) if agent else None,
        lock_expires_at=_iso(NOW + timedelta(hours=lock_in_hours)) if agent else None,
    )
    return tid


def _claim_event(task_id: int, agent: str, hours_ago: float) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_events (ts, agent, kind, ref_id, summary) VALUES (?, ?, 'task', ?, ?)",
            (_iso(NOW - timedelta(hours=hours_ago)), agent, str(task_id), "status open->doing: x"),
        )
        conn.commit()
    finally:
        conn.close()


def test_empty_queue_is_ok():
    report = queue_hygiene.queue_pressure(now=NOW)
    assert report["level"] == "ok"
    assert report["signals"] == []
    assert report["depth"] == {"open": 0, "doing": 0, "waiting_review": 0, "blocked": 0}


def test_depth_counts_active_statuses_and_open_by_lane_excluding_archived():
    task_add("a", lane="dispatch-ready")
    task_add("b", lane="dispatch-ready")
    task_add("c")
    archived = task_add("d", lane="dispatch-ready")["id"]
    _set(archived, archived_at=_iso(NOW))
    blocked = task_add("e")["id"]
    _set(blocked, status="blocked")
    report = queue_hygiene.queue_pressure(now=NOW)
    assert report["depth"]["open"] == 3
    assert report["depth"]["blocked"] == 1
    assert report["open_by_lane"] == {"default": 1, "dispatch-ready": 2}


def test_claim_ages_and_stale_and_unowned_doing():
    _doing("fresh", "Desktop_Agent2", claimed_hours_ago=1, lock_in_hours=1)
    _doing("old", "Desktop_agent3", claimed_hours_ago=9, lock_in_hours=1)
    _doing("lapsed", "Desktop_Agent4", claimed_hours_ago=5, lock_in_hours=-2)
    _doing("nobody", None, claimed_hours_ago=0, lock_in_hours=0)
    claims = queue_hygiene.queue_pressure(now=NOW)["claims"]
    assert claims["doing"] == 4
    assert claims["unowned_doing"] == 1
    assert claims["stale"] == 1
    assert claims["oldest_claim_hours"] == 9.0
    assert claims["median_claim_hours"] == 5.0


def test_unowned_and_stale_doing_are_pressure_signals():
    _doing("lapsed", "Desktop_Agent4", claimed_hours_ago=5, lock_in_hours=-2)
    _doing("nobody", None, claimed_hours_ago=0, lock_in_hours=0)
    report = queue_hygiene.queue_pressure(now=NOW)
    assert report["level"] == "pressure"
    assert any("without a claimant" in s for s in report["signals"])
    assert any("expired lease" in s for s in report["signals"])


def test_agent_at_wip_cap_is_reported(monkeypatch):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "2")
    _doing("one", "Desktop_Agent2", 1, 1)
    _doing("two", "Desktop_Agent2", 2, 1)
    _doing("three", "Desktop_agent3", 1, 1)
    wip = queue_hygiene.queue_pressure(now=NOW)["wip"]
    assert wip["agent_max"] == 2
    by_agent = {row["agent"]: row for row in wip["agents"]}
    assert by_agent["Desktop_Agent2"] == {
        "agent": "Desktop_Agent2",
        "doing": 2,
        "wip_max": 2,
        "at_cap": True,
    }
    assert by_agent["Desktop_agent3"]["at_cap"] is False


def test_hard_lane_cap_with_waiting_work_is_backpressure(monkeypatch):
    monkeypatch.setenv("UA_LANE_WIP", "dispatch-ready:1:hard")
    _doing("running", "Desktop_Agent2", 1, 1, lane="dispatch-ready")
    task_add("waiting", lane="dispatch-ready")
    report = queue_hygiene.queue_pressure(now=NOW)
    lane = next(row for row in report["wip"]["lanes"] if row["lane"] == "dispatch-ready")
    assert lane == {
        "lane": "dispatch-ready",
        "doing": 1,
        "wip_max": 1,
        "mode": "hard",
        "at_cap": True,
        "open_waiting": 1,
    }
    assert report["level"] == "pressure"
    assert any("dispatch-ready" in s and "hard WIP cap" in s for s in report["signals"])


def test_old_unclaimed_dispatch_ready_work_is_a_signal():
    tid = task_add("waiting forever", lane="dispatch-ready")["id"]
    _set(tid, created_at=_iso(NOW - timedelta(hours=100)))
    report = queue_hygiene.queue_pressure(now=NOW)
    assert report["oldest_open"]["dispatch-ready"]["id"] == tid
    assert report["oldest_open"]["dispatch-ready"]["age_hours"] == 100.0
    # Aging backlog is a note, not a fault: it must not degrade the queue.
    assert any("unclaimed for" in s for s in report["notes"])
    assert report["signals"] == [] and report["level"] == "ok"


def test_contention_counts_live_tasks_claimed_by_two_agents_in_window():
    live = task_add("traded")["id"]
    _claim_event(live, "Desktop_Agent2", 30)
    _claim_event(live, "Desktop_agent3", 10)
    finished = task_add("traded then done")["id"]
    _claim_event(finished, "Desktop_Agent2", 30)
    _claim_event(finished, "Desktop_agent3", 10)
    _set(finished, status="done", completed_at=_iso(NOW))
    old = task_add("traded long ago")["id"]
    _claim_event(old, "Desktop_Agent2", 24 * 20)
    _claim_event(old, "Desktop_agent3", 24 * 19)
    solo = task_add("resumed by one agent")["id"]
    _claim_event(solo, "Desktop_Agent2", 30)
    _claim_event(solo, "Desktop_Agent2", 10)
    contention = queue_hygiene.queue_pressure(now=NOW)["contention"]
    assert contention["window_days"] == 7
    assert contention["tasks"] == [{"task_id": live, "claims": 2, "agents": 2}]


def test_the_read_writes_nothing():
    _doing("lapsed", "Desktop_Agent4", claimed_hours_ago=5, lock_in_hours=-2)
    conn = storage.connect()
    try:
        before = [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]
    finally:
        conn.close()
    queue_hygiene.queue_pressure(now=NOW)
    conn = storage.connect()
    try:
        after = [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]
    finally:
        conn.close()
    assert before == after


def test_routine_intake_counts_queued_and_skipped_fires():
    """#1430: routines are the intake policy, so their outcomes are the intake signal."""
    from unified_agent import routines

    conn = storage.connect()
    try:
        routines.ensure_schema(conn)
        ts = _iso(NOW)
        cur = conn.execute(
            "INSERT INTO routines (title,trigger,schedule,lane,project,domain,concurrency,"
            "catch_up,paused,cursor_at,created_at,updated_at) "
            "VALUES ('inbound','api',NULL,'default','unified-agent','hobby','queue','last',0,?,?,?)",
            (ts, ts, ts),
        )
        rid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    waiting = task_add("queued fire")["id"]
    _set(waiting, status="blocked")
    released = task_add("released fire")["id"]
    conn = storage.connect()
    try:
        rows = [
            (rid, "k1", ts, "queued", waiting, ts, None),
            (rid, "k2", ts, "queued", released, ts, None),  # released: no longer blocked
            (rid, "k3", ts, "skipped", None, _iso(NOW - timedelta(days=1)), "overlap"),
            (rid, "k4", ts, "skipped", None, _iso(NOW - timedelta(days=30)), "overlap"),
            # A passed-over slot is schedule policy, not contention.
            (rid, "k5", ts, "skipped", None, _iso(NOW - timedelta(days=1)), "catch_up_none"),
        ]
        conn.executemany(
            "INSERT INTO routine_runs "
            "(routine_id,fire_key,scheduled_for,status,task_id,created_at,reason) "
            "VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    report = queue_hygiene.queue_pressure(now=NOW)
    assert report["intake"] == {"active_routines": 1, "queued_fires": 1, "skipped_fires": 1}
    assert any("queued behind an active run" in n for n in report["notes"])
    assert any("skipped" in n for n in report["notes"])
    assert report["level"] == "ok"


def test_intake_is_zero_without_routine_tables():
    assert queue_hygiene.queue_pressure(now=NOW)["intake"] == {
        "active_routines": 0,
        "queued_fires": 0,
        "skipped_fires": 0,
    }


def test_expired_lease_does_not_hold_a_hard_cap(monkeypatch):
    """#814 (Devin): the claim gate ignores expired leases for WIP, so an
    expired doing row must not raise a false hard-cap fault."""
    monkeypatch.setenv("UA_LANE_WIP", "dispatch-ready:1:hard")
    _doing("lapsed", "Desktop_Agent2", 5, -2, lane="dispatch-ready")
    task_add("waiting", lane="dispatch-ready")
    report = queue_hygiene.queue_pressure(now=NOW)
    lane = next(row for row in report["wip"]["lanes"] if row["lane"] == "dispatch-ready")
    assert lane["doing"] == 0 and lane["at_cap"] is False
    assert not any("hard WIP cap" in s for s in report["signals"])
    # ...but the lapsed lease itself is still a fault.
    assert any("expired lease" in s for s in report["signals"])
    assert report["wip"]["agents"] == []


def test_claim_without_expiry_is_a_fault():
    """#814 (Devin): task_claim treats a claim with no lock_expires_at as
    locked forever; the report must say so."""
    tid = task_add("forever")["id"]
    _set(
        tid, status="doing", claimed_by="Desktop_Agent4", claimed_at=_iso(NOW), lock_expires_at=None
    )
    report = queue_hygiene.queue_pressure(now=NOW)
    assert report["claims"]["no_expiry"] == 1
    assert report["level"] == "pressure"
    assert any("no lease expiry" in s for s in report["signals"])


def test_oldest_open_covers_agent_lanes_but_not_tracking():
    """#814 (Devin): agent-named lanes are claimable by their agent; only
    tracking rows are never claimable."""
    agent_task = task_add("mine", lane="Desktop_Agent4")["id"]
    task_add("epic", lane="tracking")
    oldest = queue_hygiene.queue_pressure(now=NOW)["oldest_open"]
    assert oldest["Desktop_Agent4"]["id"] == agent_task
    assert "tracking" not in oldest


def test_queue_pressure_reports_each_agents_effective_cap(monkeypatch):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "2")
    monkeypatch.setenv("UA_AGENT_WIP_OVERRIDES", "Desktop_Agent2:3")
    for agent in ("Desktop_Agent2", "Desktop_agent3"):
        _doing("one", agent, 1, 1)
        _doing("two", agent, 2, 1)
    wip = queue_hygiene.queue_pressure(now=NOW)["wip"]
    by_agent = {row["agent"]: row for row in wip["agents"]}
    assert wip["agent_max"] == 2
    assert (by_agent["Desktop_Agent2"]["wip_max"], by_agent["Desktop_Agent2"]["at_cap"]) == (
        3,
        False,
    )
    assert (by_agent["Desktop_agent3"]["wip_max"], by_agent["Desktop_agent3"]["at_cap"]) == (
        2,
        True,
    )
