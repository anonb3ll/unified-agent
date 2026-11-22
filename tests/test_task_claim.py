import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.tool_errors import ConflictError
from unified_agent.tools.tasks import task_add, task_claim, task_update


def _expire_lock(task_id: int) -> None:
    from unified_agent import storage

    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (task_id,)
        )
        conn.commit()
    finally:
        conn.close()


def test_task_claim_unclaimed():
    t = task_add("Unclaimed task", notes="Test task")
    tid = t["id"]

    # Claim it
    claimed = task_claim(tid, agent="Desktop_Agent5", timeout_seconds=10)
    assert claimed["status"] == "doing"
    assert claimed["claimed_by"] == "Desktop_Agent5"
    assert "claimed-by: Desktop_Agent5" in claimed["notes"]


def test_task_claim_reclaim_same_agent():
    t = task_add("Same agent task", notes="Test task")
    tid = t["id"]

    # Claim 1
    task_claim(tid, agent="Desktop_Agent5", timeout_seconds=10)
    # Claim 2 (should succeed and update expires)
    claimed = task_claim(tid, agent="Desktop_Agent5", timeout_seconds=20)
    assert claimed["claimed_by"] == "Desktop_Agent5"


def test_task_claim_locked_by_other():
    t = task_add("Locked task", notes="Test task")
    tid = t["id"]

    # Claim by agent 1
    task_claim(tid, agent="Desktop_Agent5", timeout_seconds=100)

    # Try claim by agent 2: told to wait, not given an error (#2388)
    result = task_claim(tid, agent="Desktop_Agent1", timeout_seconds=10)
    assert result["claimed"] is False
    assert result["safe_wait"]["owner"] == "Desktop_Agent5"


def test_task_claim_expired_lock():
    t = task_add("Expired lock task", notes="Test task")
    tid = t["id"]

    task_claim(tid, agent="Desktop_Agent5", timeout_seconds=100)
    _expire_lock(tid)

    # Claim by agent 2 (should succeed)
    claimed = task_claim(tid, agent="Desktop_Agent1", timeout_seconds=100)
    assert claimed["claimed_by"] == "Desktop_Agent1"


@pytest.mark.parametrize("timeout_seconds", [0, -10, 7 * 86400 + 1, 10**12])
def test_task_claim_refuses_an_out_of_range_lease(timeout_seconds):
    """A lease <= 0 is born expired (any agent may steal it); a huge one overflows."""
    tid = task_add("Lease bounds", notes="Test task")["id"]
    with pytest.raises(ToolError, match="timeout_seconds must be"):
        task_claim(tid, agent="Desktop_Agent5", timeout_seconds=timeout_seconds)


def test_task_claim_done_refusal():
    t = task_add("Done task", notes="Test task")
    tid = t["id"]
    task_update(tid, status="done", actor="test-agent")

    with pytest.raises(ToolError, match="already done"):
        task_claim(tid, agent="Desktop_Agent5")


def test_task_claim_cancelled_refusal():
    t = task_add("Cancelled task", notes="Test task")
    tid = t["id"]
    task_update(tid, status="cancelled")

    with pytest.raises(ToolError, match="already cancelled"):
        task_claim(tid, agent="Desktop_Agent5")


def _claim_events(task_id: int) -> list[str]:
    from unified_agent import storage

    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT agent FROM agent_events WHERE ref_id=? AND summary LIKE 'claimed task%' "
            "ORDER BY id",
            (str(task_id),),
        ).fetchall()
        return [r["agent"] for r in rows]
    finally:
        conn.close()


def test_lock_renewal_is_not_recorded_as_a_claim():
    """Re-claiming a task you already hold renews the lock; it is not a new claim.

    Regression: every renewal recorded a claim event, and the executor daemon
    renews on each 5s poll tick — task #149 accumulated 138 identical claim
    events in 22 minutes. That flooded agent_events and pinned the
    task_reassignment_thrash anomaly rule on, since the rule counts re-entries
    into 'doing'.
    """
    t = task_add("Renewal task", notes="Test task")
    tid = t["id"]

    task_claim(tid, agent="Desktop_agent3", timeout_seconds=60)
    for _ in range(5):
        task_claim(tid, agent="Desktop_agent3", timeout_seconds=60)

    assert _claim_events(tid) == ["Desktop_agent3"]


def test_takeover_after_expiry_is_recorded():
    """A different agent picking up an expired lock IS a real claim."""
    t = task_add("Takeover task", notes="Test task")
    tid = t["id"]

    task_claim(tid, agent="Desktop_agent3", timeout_seconds=60)
    _expire_lock(tid)
    task_claim(tid, agent="Desktop_Agent5", timeout_seconds=60)

    assert _claim_events(tid) == ["Desktop_agent3", "Desktop_Agent5"]


def test_task_claim_lock_expires_after_writer_lock_wait(monkeypatch):
    """Lease duration starts after BEGIN IMMEDIATE returns, not before the lock wait.

    Anchored to the moment the writer lock was RELEASED, not to `datetime.now()`
    at assert time. Two things make the naive version flaky (task #1015):

    * `lock_expires_at` is truncated to whole seconds
      (`isoformat(timespec="seconds")`), so up to 1s of the lease is shaved off
      the stored value depending on where the claim lands inside a second;
    * measuring against `now()` at assert time also charges whatever the test
      itself spent getting there to the budget.

    Together those made `remaining > 1.0` fail outright whenever the claim
    happened to land within a hair of a second boundary — observed in CI as
    `assert 0.999415 > 1.0` on a PR touching nothing near task claiming.

    So: compare against `released_at` and allow exactly the 1s the truncation
    can remove. `hold_s` is sized so the bound still discriminates — if the
    lease clock wrongly started at the barrier instead of after the wait,
    `lock_expires` would be about `released + timeout - hold`, comfortably
    below the bound.
    """
    from unified_agent import storage
    from unified_agent.tools import tasks as tasks_module

    monkeypatch.setattr(tasks_module, "schedule_sync_to_vault", lambda: None)
    monkeypatch.setattr(storage, "record_agent_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks_module, "_ensure_task_worktree", lambda *args, **kwargs: None)

    t = task_add("Writer-lock wait task", notes="Test task")
    tid = t["id"]
    timeout_seconds = 5
    hold_s = 1.5
    barrier = threading.Barrier(2)
    released_at: list[datetime] = []

    def hold_writer_lock() -> None:
        conn = storage.connect()
        conn.execute("BEGIN IMMEDIATE")
        barrier.wait()
        time.sleep(hold_s)
        conn.commit()
        released_at.append(datetime.now(UTC))
        conn.close()

    holder = threading.Thread(target=hold_writer_lock)
    holder.start()
    barrier.wait()

    claimed = task_claim(tid, agent="Desktop_Agent5", timeout_seconds=timeout_seconds)
    holder.join()

    assert released_at, "holder thread did not record a release time"
    lock_expires = datetime.fromisoformat(claimed["lock_expires_at"])
    lease_from_release = (lock_expires - released_at[0]).total_seconds()

    # Clock started after the wait -> ~timeout (minus <1s of truncation).
    # Clock started at the barrier -> ~timeout - hold_s = 3.5s, which fails this.
    truncation_allowance = 1.0
    assert lease_from_release >= timeout_seconds - truncation_allowance, (
        f"lease measured {lease_from_release:.3f}s from lock release; expected about "
        f"{timeout_seconds}s. Below {timeout_seconds - truncation_allowance}s means the "
        "lease clock started before the writer-lock wait rather than after it."
    )

    # And the stored lease is the requested one, give or take the second both
    # timestamps are independently truncated to.
    claimed_at = datetime.fromisoformat(claimed["claimed_at"])
    assert abs((lock_expires - claimed_at).total_seconds() - timeout_seconds) <= 1.0


def _pin_claim_clock(monkeypatch, now):
    from unified_agent import storage
    from unified_agent.tools import tasks

    class ClaimClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(now)

    monkeypatch.setattr(storage, "now", lambda: now)
    monkeypatch.setattr(tasks, "datetime", ClaimClock)


@pytest.mark.parametrize("agent", ["Desktop_Agent5", "Desktop_Agent1"])
def test_claim_at_exact_expiry_creates_new_run(monkeypatch, agent):
    from unified_agent import storage
    from unified_agent.tools import tasks

    now = "2026-09-30T04:00:00+00:00"
    _pin_claim_clock(monkeypatch, now)
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *args, **kwargs: None)
    tid = task_add("Equality expiry")["id"]
    first = task_claim(tid, agent="Desktop_Agent5", timeout_seconds=60)
    assert first["lock_expires_at"] == "2026-09-30T04:01:00+00:00"
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET lock_expires_at=? WHERE id=?", (now, tid))
        conn.commit()
    finally:
        conn.close()
    successor = task_claim(tid, agent=agent, timeout_seconds=60)
    assert successor["claimed_by"] == agent
    assert successor["current_run_id"] != first["current_run_id"]
    assert _claim_events(tid) == ["Desktop_Agent5", agent]


def test_exact_expiry_is_stale_and_does_not_count_as_wip(monkeypatch):
    from unified_agent import storage
    from unified_agent.tools import tasks

    now = "2026-09-30T04:00:00+00:00"
    _pin_claim_clock(monkeypatch, now)
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *args, **kwargs: None)
    tid = task_add("Equality WIP")["id"]
    task_claim(tid, agent="Desktop_Agent5", timeout_seconds=60)
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET lock_expires_at=? WHERE id=?", (now, tid))
        conn.commit()
        assert tasks._count_agent_wip(conn, "Desktop_Agent5", now) == 0
        assert tasks._count_lane_wip(conn, "default", now) == 0
    finally:
        conn.close()
    assert tid in [t["id"] for t in tasks.task_list(status="doing", stale_claims=True)["tasks"]]


# -- safe_wait (#2388) ------------------------------------------------------

HOLDER = "Desktop_Agent5"
CALLER = "Desktop_Agent1"


def _future(hours: int = 24) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _snapshot(task_id: int) -> dict:
    """Everything a claim could write: the task row and the logs it appends to."""
    conn = storage.connect()
    try:
        return {
            "task": tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()),
            "wait": [tuple(r) for r in conn.execute("SELECT * FROM task_liveness")],
            "events": conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0],
            "agent_events": conn.execute("SELECT COUNT(*) FROM agent_events").fetchone()[0],
        }
    finally:
        conn.close()


def _column(task_id: int, *columns: str) -> tuple:
    conn = storage.connect()
    try:
        row = conn.execute(f"SELECT {', '.join(columns)} FROM tasks WHERE id=?", (task_id,))
        return tuple(row.fetchone())
    finally:
        conn.close()


def _held_task(title: str = "Held") -> int:
    task_id = task_add(title)["id"]
    task_claim(task_id, agent=HOLDER, timeout_seconds=3600)
    return task_id


def test_contested_claim_returns_safe_wait_and_leaves_the_row_byte_identical():
    task_id = _held_task()
    _set_waiting_on = task_update(task_id, waiting_on="ci", actor=HOLDER)
    before = _snapshot(task_id)

    result = task_claim(task_id, agent=CALLER, timeout_seconds=10)

    assert result == {
        "id": task_id,
        "claimed": False,
        "safe_wait": {
            "owner": HOLDER,
            "waiting_on": "ci",
            "until": _set_waiting_on["lock_expires_at"],
            "generation": 1,
            "claimed_by": HOLDER,
        },
    }
    assert _snapshot(task_id) == before


def test_safe_wait_carries_the_verified_caller():
    task_id = _held_task()
    reset = config.agent_identity_var.set(CALLER)
    try:
        assert task_claim(task_id, agent=CALLER)["verified_caller"] == CALLER
    finally:
        config.agent_identity_var.reset(reset)


def test_a_lease_with_no_expiry_waits_with_a_null_until():
    task_id = _held_task()
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET lock_expires_at=NULL WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()
    assert task_claim(task_id, agent=CALLER)["safe_wait"]["until"] is None


def test_a_declared_wait_keeps_a_task_owned_after_its_lease_lapsed():
    task_id = _held_task()
    wait = task_update(task_id, declared_wait={"reason": "ci", "until": _future()}, actor=HOLDER)
    _expire_lock(task_id)
    before = _snapshot(task_id)

    result = task_claim(task_id, agent=CALLER)

    assert result["claimed"] is False
    assert result["safe_wait"]["owner"] == HOLDER
    assert result["safe_wait"]["until"] == wait["liveness"]["declared_wait"]["until"]
    assert _snapshot(task_id) == before


def test_a_pending_monitor_keeps_a_released_owner_waiting():
    task_id = task_add("Handed off", lane=HOLDER)["id"]
    state = task_update(task_id, monitor_at=_future(), next_owner=HOLDER, actor=HOLDER)
    result = task_claim(task_id, agent=CALLER, expected_lane=HOLDER)
    assert result["safe_wait"] == {
        "owner": HOLDER,
        "waiting_on": None,
        "until": state["liveness"]["monitor"]["at"],
        "generation": 1,
        "claimed_by": None,
    }


def test_a_stale_owner_with_no_wait_does_not_wedge_the_task():
    task_id = _held_task()
    _expire_lock(task_id)
    claimed = task_claim(task_id, agent=CALLER)
    assert claimed["claimed_by"] == CALLER
    assert claimed["next_owner"] == CALLER


def test_an_expired_wait_does_not_hold_the_task():
    task_id = _held_task()
    task_update(task_id, declared_wait={"reason": "ci", "until": _future()}, actor=HOLDER)
    _expire_lock(task_id)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE task_liveness SET wait_until='2000-01-01T00:00:00+00:00' WHERE task_id=?",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()
    assert task_claim(task_id, agent=CALLER)["claimed_by"] == CALLER


def test_the_stored_owner_may_claim_through_its_own_wait():
    task_id = _held_task()
    task_update(task_id, declared_wait={"reason": "ci", "until": _future()}, actor=HOLDER)
    _expire_lock(task_id)
    assert task_claim(task_id, agent=HOLDER)["claimed_by"] == HOLDER


def test_a_fenced_caller_keeps_the_conflict():
    """expected_run_id holds a fencing token: it is not asking to queue."""
    task_id = task_add("Fenced")["id"]
    claimed = task_claim(task_id, agent=HOLDER, timeout_seconds=3600)
    with pytest.raises(ConflictError, match=f"already claimed/locked by '{HOLDER}'"):
        task_claim(task_id, agent=CALLER, expected_run_id=claimed["current_run_id"])


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"status": "done"}, "already done"),
        ({"archived": True}, "is parked"),
    ],
)
def test_refusals_stay_ahead_of_safe_wait(change, message):
    task_id = _held_task()
    task_update(task_id, actor=HOLDER, **change)
    with pytest.raises(ToolError, match=message):
        task_claim(task_id, agent=CALLER)


def test_lane_refusal_stays_ahead_of_safe_wait():
    task_id = task_add("Lane", lane=HOLDER)["id"]
    task_claim(task_id, agent=HOLDER, expected_lane=HOLDER, timeout_seconds=3600)
    with pytest.raises(ToolError, match="claim scoped to"):
        task_claim(task_id, agent=CALLER)


def test_a_new_claim_takes_ownership_and_a_renewal_does_not():
    task_id = task_add("Transitions")["id"]
    claimed = task_claim(task_id, agent=HOLDER, timeout_seconds=3600)
    assert (claimed["next_owner"], claimed["ownership_generation"]) == (HOLDER, 1)
    task_update(task_id, waiting_on="ci", actor=HOLDER)

    renewed = task_claim(task_id, agent=HOLDER, timeout_seconds=3600)
    assert (renewed["next_owner"], renewed["waiting_on"]) == (HOLDER, "ci")
    assert renewed["ownership_generation"] == 1

    _expire_lock(task_id)
    taken = task_claim(task_id, agent=CALLER, timeout_seconds=3600)
    assert (taken["next_owner"], taken["waiting_on"]) == (CALLER, None)
    assert taken["ownership_generation"] == 2


def _finish_run(task_id: int, outcome: str, agent: str = HOLDER) -> None:
    """Record the holder's run as ended, leaving the claim and lease in place."""
    from unified_agent import run_records

    conn = storage.connect()
    try:
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()[
            0
        ]
        run = {"run_id": run_id, "agent": agent, "claimed_at": storage.now()}
        conn.execute("BEGIN IMMEDIATE")
        payload = run_records._finish_payload(
            run,
            task_id=task_id,
            outcome=outcome,
            to_status=None,
            finished_by=agent,
            finished_at=storage.now(),
            usage=None,
        )
        tx = run_records.attach(conn)
        assert run_records._append(tx, payload, agent, run_records._finish_key(run_id))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("outcome", ["abandoned", "cancelled", "released"])
def test_a_recorded_failed_run_lets_a_successor_replace_a_live_lease(outcome):
    task_id = _held_task()
    _finish_run(task_id, outcome)

    result = task_claim(task_id, agent=CALLER, timeout_seconds=60)

    assert result["claimed_by"] == CALLER
    assert result["next_owner"] == CALLER
    assert result["replaced"]["predecessor"] == HOLDER
    assert result["replaced"]["outcome"] == outcome
    assert result["replaced"]["generation"] == result["ownership_generation"] - 1
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT originator, payload FROM event_log WHERE channel='agent.task.ownership'"
        ).fetchall()
    finally:
        conn.close()
    assert [r["originator"] for r in rows] == [CALLER]
    assert json.loads(rows[0]["payload"])["task_id"] == task_id


@pytest.mark.parametrize("outcome", ["completed", "handed_off", "blocked"])
def test_a_run_that_did_not_fail_never_opens_a_replacement(outcome):
    task_id = _held_task()
    _finish_run(task_id, outcome)
    before = _snapshot(task_id)
    assert "safe_wait" in task_claim(task_id, agent=CALLER)
    assert _snapshot(task_id) == before


def test_a_bare_second_claim_with_the_run_still_open_is_not_a_takeover():
    task_id = _held_task()
    before = _snapshot(task_id)
    assert "safe_wait" in task_claim(task_id, agent=CALLER)
    assert _snapshot(task_id) == before


def test_another_agents_failed_run_does_not_unseat_the_owner():
    task_id = _held_task()
    _finish_run(task_id, "abandoned", agent="server_agent2")
    assert "safe_wait" in task_claim(task_id, agent=CALLER)


def test_an_owner_that_acts_after_the_failure_keeps_the_task(monkeypatch):
    task_id = _held_task()
    _finish_run(task_id, "abandoned")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO task_liveness (task_id, wait_reason, wait_until, wait_set_by, updated_at) "
            "VALUES (?, 'ci', ?, ?, ?)",
            (task_id, _future(), HOLDER, _future(1)),
        )
        conn.commit()
    finally:
        conn.close()
    assert "safe_wait" in task_claim(task_id, agent=CALLER)


def test_a_failed_run_also_ends_a_declared_wait_after_the_lease_lapsed():
    task_id = _held_task()
    task_update(task_id, declared_wait={"reason": "ci", "until": _future()}, actor=HOLDER)
    _expire_lock(task_id)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE task_liveness SET updated_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()
    _finish_run(task_id, "abandoned")

    assert task_claim(task_id, agent=CALLER)["replaced"]["predecessor"] == HOLDER


def test_a_fenced_caller_still_gets_a_conflict_after_a_failed_run():
    task_id = _held_task()
    _finish_run(task_id, "abandoned")
    with pytest.raises(ConflictError):
        task_claim(task_id, agent=CALLER, expected_run_id="sp-not-mine")
