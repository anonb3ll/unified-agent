"""Task liveness invariant (task #1749, Parity 25).

Ground truth:
- the classifier is pure and names every action path in a fixed order;
- the sweep is a no-op when off, idempotent in report and enforce, never
  touches a live claim, and enforce writes the recovery note;
- a monitor fires exactly once; a declared wait stops counting once expired;
  both are bounded, need a UTC offset, and only the task's owner (claimant,
  lane agent when unclaimed, lifecycle or human operator) may set them;
- an agent's own lane is its backlog while it was seen within a day;
- an unclaimed doing row's run counts while its agent is seen acting;
- an unfinished executor attempt counts only inside a lease;
- a wait chain past four weeks is reported once a day, not reclassified;
- three consecutive lease-lapse abandonments (last 30 days) escalate like a
  dead letter; other abandonments do not count.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import (
    config,
    events,
    hub_runtime,
    monitor,
    node_queue,
    queue_hygiene,
    registry,
    run_records,
    storage,
    task_liveness,
)
from unified_agent.task_liveness import ActionPath, LivenessContext, action_path
from unified_agent.tools.tasks import task_add, task_claim, task_update

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
AGENT = "Desktop_Agent2"
OTHER = "server_agent2"


def _operator() -> str:
    return config.operator_label()


@contextlib.contextmanager
def _verified(agent: str):
    reset = config.agent_identity_var.set(agent)
    try:
        yield
    finally:
        config.agent_identity_var.reset(reset)


def _seen(label: str, at: datetime, via: str = "heartbeat") -> None:
    """Stamp an agent's last_seen directly, as the registry heartbeat would."""
    ts = at.isoformat(timespec="seconds")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agents (label, kind, known, first_seen, last_seen, last_seen_via, "
            "created_at, updated_at) VALUES (?, 'agent', 1, ?, ?, ?, ?, ?) "
            "ON CONFLICT(label) DO UPDATE SET last_seen=excluded.last_seen, "
            "last_seen_via=excluded.last_seen_via",
            (label, ts, ts, via, ts, ts),
        )
        conn.commit()
    finally:
        conn.close()


def _task(**overrides):
    row = {
        "id": 7,
        "status": "doing",
        "lane": "default",
        "claimed_by": None,
        "lock_expires_at": None,
        "current_run_id": None,
        "updated_at": (NOW - timedelta(days=1)).isoformat(),
        "notes": "",
        "archived_at": None,
    }
    row.update(overrides)
    return row


def _iso(delta: timedelta) -> str:
    return (NOW + delta).isoformat()


# ── classifier table ─────────────────────────────────────────────────


CLASSIFIER_CASES = [
    pytest.param(
        _task(claimed_by=AGENT, lock_expires_at=_iso(timedelta(hours=1))),
        LivenessContext(),
        "run",
        id="live-claim-lease",
    ),
    pytest.param(
        _task(claimed_by=AGENT, lock_expires_at=None),
        LivenessContext(),
        "run",
        id="claim-without-expiry-is-live",
    ),
    pytest.param(
        _task(),
        LivenessContext(open_runs={7: "dispatch run r1 (running)"}),
        "run",
        id="open-dispatch-run",
    ),
    pytest.param(
        _task(current_run_id="sp-1", updated_at=_iso(-timedelta(minutes=10))),
        LivenessContext(),
        "run",
        id="unclaimed-doing-inside-implicit-lease",
    ),
    pytest.param(
        _task(current_run_id="sp-1"),
        LivenessContext(live_run_agents={7: AGENT}),
        "run",
        id="unclaimed-doing-run-agent-seen",
    ),
    pytest.param(
        _task(status="waiting_review"),
        LivenessContext(),
        "review_participant",
        id="waiting-review-status",
    ),
    pytest.param(
        _task(notes="done coding\n[waiting-for: review]"),
        LivenessContext(),
        "review_participant",
        id="review-marker",
    ),
    pytest.param(
        _task(),
        LivenessContext(approvals={7: (3,)}),
        "approval",
        id="pending-approval",
    ),
    pytest.param(
        _task(),
        LivenessContext(prerequisites={7: (5, 4)}),
        "blocked_on:4,5",
        id="prerequisites",
    ),
    pytest.param(
        _task(),
        LivenessContext(waits={7: ("vendor reply", _iso(timedelta(hours=3)))}),
        "declared_wait",
        id="live-declared-wait",
    ),
    pytest.param(
        _task(),
        LivenessContext(monitors={7: _iso(timedelta(hours=2))}),
        "monitor",
        id="future-monitor",
    ),
    pytest.param(
        _task(lane="operator-only"),
        LivenessContext(),
        "human_owner",
        id="operator-only-lane",
    ),
    pytest.param(
        _task(lane="interactive-only"),
        LivenessContext(),
        "human_owner",
        id="interactive-only-lane",
    ),
    pytest.param(
        _task(claimed_by="Desktop_Human", lock_expires_at=_iso(-timedelta(hours=1))),
        LivenessContext(human_labels=frozenset({"Desktop_Human"})),
        "human_owner",
        id="claimed-by-operator",
    ),
    pytest.param(
        _task(status="open", lane=AGENT),
        LivenessContext(queued={7: "pending delegation #2 to Desktop_Agent2"}),
        "queued",
        id="queued-delegation",
    ),
    pytest.param(
        _task(status="open", lane="default"),
        LivenessContext(),
        "queued",
        id="shared-pool-is-never-none",
    ),
    pytest.param(
        _task(status="open", lane="dispatch-ready"),
        LivenessContext(),
        "queued",
        id="dispatch-ready-pool",
    ),
    pytest.param(
        _task(status="open", lane=AGENT),
        LivenessContext(seen_lane_agents=frozenset({AGENT})),
        "queued",
        id="agent-lane-backlog-while-seen",
    ),
    pytest.param(
        _task(claimed_by=AGENT, lock_expires_at=_iso(-timedelta(minutes=5))),
        LivenessContext(),
        "NONE",
        id="doing-lapsed-claim",
    ),
    pytest.param(
        _task(current_run_id="sp-1"),
        LivenessContext(),
        "NONE",
        id="unclaimed-doing-run-agent-not-seen",
    ),
    pytest.param(
        _task(),
        LivenessContext(),
        "NONE",
        id="doing-unclaimed",
    ),
    pytest.param(
        _task(status="open", lane=AGENT),
        LivenessContext(seen_lane_agents=frozenset({OTHER})),
        "NONE",
        id="open-in-unseen-agent-lane",
    ),
    pytest.param(
        _task(),
        LivenessContext(waits={7: ("vendor reply", _iso(-timedelta(minutes=1)))}),
        "NONE",
        id="expired-declared-wait",
    ),
    pytest.param(
        _task(),
        LivenessContext(monitors={7: _iso(-timedelta(minutes=1))}),
        "NONE",
        id="past-monitor-is-not-future",
    ),
    pytest.param(
        _task(status="done"),
        LivenessContext(),
        "out_of_scope",
        id="terminal",
    ),
    pytest.param(
        _task(lane="tracking"),
        LivenessContext(),
        "out_of_scope",
        id="tracking-lane",
    ),
]


@pytest.mark.parametrize(("task", "context", "kind"), CLASSIFIER_CASES)
def test_action_path_table(task, context, kind):
    path = action_path(task, now=NOW, context=context)
    assert isinstance(path, ActionPath)
    assert path.kind == kind
    assert path.detail


def test_every_kind_is_covered_by_the_table():
    covered = {case.values[2].split(":", 1)[0] for case in CLASSIFIER_CASES}
    assert set(task_liveness.KINDS) <= covered


def test_live_claim_wins_over_everything():
    task = _task(
        claimed_by=AGENT,
        lock_expires_at=_iso(timedelta(hours=1)),
        notes="[waiting-for: review]",
        lane="operator-only",
    )
    ctx = LivenessContext(approvals={7: (1,)}, prerequisites={7: (2,)})
    assert action_path(task, now=NOW, context=ctx).kind == "run"


def test_sweep_scope():
    assert task_liveness.in_sweep_scope(_task())
    assert task_liveness.in_sweep_scope(_task(status="open", lane=AGENT))
    assert not task_liveness.in_sweep_scope(_task(status="open", lane="default"))
    assert not task_liveness.in_sweep_scope(_task(status="open", lane="operator-only"))
    assert not task_liveness.in_sweep_scope(_task(status="blocked"))
    assert not task_liveness.in_sweep_scope(_task(lane="tracking"))
    assert not task_liveness.in_sweep_scope(_task(archived_at=NOW.isoformat()))


# ── batch context from the database ──────────────────────────────────


def test_gather_context_reads_prerequisites_approvals_and_delegations():
    prereq = task_add("Prerequisite")
    task = task_add("Dependent")
    conn = storage.connect()
    try:
        ts = storage.now()
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id, created_by, created_at) "
            "VALUES (?, ?, 'test', ?)",
            (task["id"], prereq["id"], ts),
        )
        conn.execute(
            "INSERT INTO approvals (tool, payload, status, created_at, updated_at) "
            "VALUES ('delegate', ?, 'pending', ?, ?)",
            (f'{{"task_id": {task["id"]}}}', ts, ts),
        )
        conn.execute(
            "INSERT INTO approvals (tool, payload, status, created_at, updated_at) "
            "VALUES ('delegate', 'not json', 'pending', ?, ?)",
            (ts, ts),
        )
        conn.execute(
            "INSERT INTO delegations (from_agent, to_agent, task_id, task_spec, created_at, "
            "updated_at) VALUES ('Desktop_Agent1', ?, ?, 'spec', ?, ?)",
            (AGENT, task["id"], ts, ts),
        )
        conn.commit()
        ctx = task_liveness.gather_context(conn, [task["id"], prereq["id"]], now=NOW)
    finally:
        conn.close()
    assert ctx.prerequisites == {task["id"]: (prereq["id"],)}
    assert list(ctx.approvals) == [task["id"]]
    assert task["id"] in ctx.queued
    task_update(prereq["id"], status="done", actor="Desktop_Agent1")
    conn = storage.connect()
    try:
        ctx = task_liveness.gather_context(conn, [task["id"]], now=NOW)
    finally:
        conn.close()
    assert ctx.prerequisites == {}


# ── sweep ────────────────────────────────────────────────────────────


def _later(hours: float = 2) -> datetime:
    """A sweep time `hours` from now, kept clear of UTC midnight (task #2201).

    The sweep deduplicates per task per UTC day, so a test that sweeps again
    a few minutes later would see a new day, and a second report, whenever
    this lands just before midnight. Such a time moves to just after it.
    """
    at = datetime.now(UTC) + timedelta(hours=hours)
    midnight = (at + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if midnight - at < timedelta(minutes=30):
        at = midnight + timedelta(minutes=1)
    return at


def _row(task_id: int):
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()


def _liveness_events(kind: str | None = None) -> list[dict]:
    rows = events.query_events(channels=[task_liveness.CHANNEL], limit=500)
    return [r for r in rows if kind is None or r["payload"].get("kind") == kind]


def test_later_keeps_a_follow_up_sweep_on_the_same_utc_day(monkeypatch):
    """A clock 2h before midnight still gives sweep pairs one UTC day (#2201)."""

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 20, 21, 56, 30, tzinfo=UTC)

    monkeypatch.setattr(sys.modules[__name__], "datetime", _Clock)
    at = _later()
    assert (at + timedelta(minutes=5)).date() == at.date()
    assert at >= datetime(2026, 10, 20, 23, 56, 30, tzinfo=UTC)


def _stalled_task(title: str = "Stalled") -> dict:
    return task_add(title, lane=AGENT)


def test_off_does_nothing():
    t = _stalled_task()
    res = task_liveness.run_liveness_sweep(mode="off", now=_later())
    assert res["none"] == [] and res["reported"] == [] and res["monitors"] == []
    assert _liveness_events() == []
    assert _row(t["id"])["status"] == "open"


def test_env_setting_selects_mode(monkeypatch):
    monkeypatch.setenv("UA_LIVENESS_SWEEP", "off")
    assert task_liveness.run_liveness_sweep(now=_later())["mode"] == "off"
    monkeypatch.setenv("UA_LIVENESS_SWEEP", "bogus")
    assert task_liveness.run_liveness_sweep(now=_later())["mode"] == "report"
    monkeypatch.delenv("UA_LIVENESS_SWEEP")
    assert task_liveness.run_liveness_sweep(now=_later())["mode"] == "report"


def test_report_is_idempotent_and_moves_nothing():
    t = _stalled_task()
    pool = task_add("Pool task")  # shared pool: never NONE
    now = _later()
    first = task_liveness.run_liveness_sweep(mode="report", now=now)
    assert [n["task_id"] for n in first["none"]] == [t["id"]]
    assert first["reported"] == [t["id"]]
    assert first["blocked"] == []
    before = _row(t["id"])
    second = task_liveness.run_liveness_sweep(mode="report", now=now + timedelta(minutes=5))
    assert second["reported"] == []
    assert len(_liveness_events("no_action_path")) == 1
    after = _row(t["id"])
    assert after["status"] == "open" and after["updated_at"] == before["updated_at"]
    assert _row(pool["id"])["status"] == "open"
    # A new UTC day is a new digest entry.
    third = task_liveness.run_liveness_sweep(mode="report", now=now + timedelta(days=1))
    assert third["reported"] == [t["id"]]


def test_recent_task_is_inside_the_grace_window():
    _stalled_task()
    res = task_liveness.run_liveness_sweep(mode="report", now=datetime.now(UTC))
    assert res["none"] == []
    assert res["checked"] == 1


def test_dry_run_writes_nothing():
    t = _stalled_task()
    res = task_liveness.run_liveness_sweep(mode="enforce", now=_later(), dry_run=True)
    assert [n["task_id"] for n in res["none"]] == [t["id"]]
    assert res["reported"] == [] and res["blocked"] == []
    assert _liveness_events() == []
    assert _row(t["id"])["status"] == "open"


def test_enforce_blocks_with_recovery_note_and_is_idempotent():
    t = _stalled_task()
    now = _later()
    res = task_liveness.run_liveness_sweep(mode="enforce", now=now)
    assert res["blocked"] == [t["id"]], res["errors"]
    row = _row(t["id"])
    assert row["status"] == "blocked"
    assert "[liveness: no action path @ " in row["notes"]
    assert "recovery: claim it, declare a wait, or set a monitor" in row["notes"]
    again = task_liveness.run_liveness_sweep(mode="enforce", now=now + timedelta(minutes=1))
    assert again["blocked"] == [] and again["reported"] == []
    assert _row(t["id"])["notes"].count("[liveness: no action path") == 1


def test_enforce_never_touches_a_live_claim():
    t = _stalled_task()
    task_claim(t["id"], agent=AGENT, timeout_seconds=24 * 3600)
    before = _row(t["id"])
    res = task_liveness.run_liveness_sweep(mode="enforce", now=_later())
    assert res["blocked"] == [] and res["none"] == []
    after = _row(t["id"])
    assert after["status"] == "doing"
    assert after["claimed_by"] == AGENT
    assert after["updated_at"] == before["updated_at"]


def test_enforce_blocks_doing_task_whose_claim_lapsed():
    t = _stalled_task()
    task_claim(t["id"], agent=AGENT, timeout_seconds=600)
    res = task_liveness.run_liveness_sweep(mode="enforce", now=_later())
    assert res["blocked"] == [t["id"]], res["errors"]
    assert "lapsed" in _row(t["id"])["notes"]


def test_enforce_is_fenced_on_the_revision_it_read(monkeypatch):
    t = _stalled_task()
    real_gather = task_liveness.gather_context

    def gather_then_touch(conn, ids, *, now):
        ctx = real_gather(conn, ids, now=now)
        task_update(t["id"], append_note="someone acted meanwhile", actor="Desktop_Agent1")
        return ctx

    monkeypatch.setattr(task_liveness, "gather_context", gather_then_touch)
    res = task_liveness.run_liveness_sweep(mode="enforce", now=_later())
    assert res["blocked"] == []
    assert res["errors"] and res["errors"][0]["task_id"] == t["id"]
    assert _row(t["id"])["status"] == "open"


# ── digest ───────────────────────────────────────────────────────────


def test_report_entry_reaches_anomalies_until_the_task_changes(monkeypatch):
    monkeypatch.setenv("UA_ANOMALY_SERVICES", "")
    t = _stalled_task()
    task_liveness.run_liveness_sweep(mode="report", now=_later())
    found = [a for a in monitor.check_anomalies()["anomalies"] if a["rule"] == "task_liveness"]
    assert [a["subject"] for a in found] == [f"task:{t['id']}"]
    assert found[0]["severity"] == "warning"
    assert "no action path" in found[0]["detail"]
    task_claim(t["id"], agent=AGENT)
    found = [a for a in monitor.check_anomalies()["anomalies"] if a["rule"] == "task_liveness"]
    assert found == []


# ── task_update parameters ───────────────────────────────────────────


def test_task_update_sets_and_clears_monitor_and_wait():
    t = _stalled_task()
    at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    until = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    res = task_update(
        t["id"],
        monitor_at=at,
        monitor_note="check CI",
        declared_wait={"reason": "vendor reply", "until": until},
        actor=_operator(),
    )
    assert res["liveness"]["monitor"]["note"] == "check CI"
    assert res["liveness"]["declared_wait"]["reason"] == "vendor reply"
    res = task_update(t["id"], clear_monitor=True, declared_wait={}, actor=_operator())
    assert res["liveness"]["monitor"] is None
    assert res["liveness"]["declared_wait"] is None
    assert "liveness" not in task_update(t["id"], priority="high", actor="Desktop_Agent1")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"monitor_note": "orphan"}, "monitor_note needs monitor_at"),
        ({"monitor_at": "tomorrow"}, "ISO 8601"),
        ({"monitor_at": "2026-10-10T00:00:00Z", "clear_monitor": True}, "not both"),
        ({"declared_wait": {"reason": "x"}}, "exactly"),
        ({"declared_wait": {"reason": "", "until": "2099-01-01T00:00:00Z"}}, "empty"),
        ({"declared_wait": {"reason": "x", "until": "2000-01-01T00:00:00Z"}}, "future"),
        ({"declared_wait": {"reason": "x", "until": "2099-01-01T00:00:00Z"}}, "14 days"),
        ({"declared_wait": ["x"]}, "declared_wait must be"),
        ({"monitor_at": "2099-01-01T00:00:00Z"}, "monitor_at may be at most 14 days"),
        ({"monitor_at": "2026-10-10T00:00:00"}, "monitor_at must include a UTC offset"),
        (
            {"declared_wait": {"reason": "x", "until": "2099-01-01T00:00:00"}},
            "declared_wait.until must include a UTC offset",
        ),
    ],
)
def test_task_update_refuses_bad_liveness_params(kwargs, message):
    t = _stalled_task()
    with pytest.raises(ToolError, match=message):
        task_update(t["id"], actor=AGENT, **kwargs)
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM task_liveness").fetchone()[0] == 0
    finally:
        conn.close()


def test_declared_wait_counts_until_it_expires():
    t = _stalled_task()
    until = datetime.now(UTC) + timedelta(hours=3)
    task_update(
        t["id"],
        declared_wait={"reason": "upstream release", "until": until.isoformat()},
        actor=_operator(),
    )
    during = task_liveness.run_liveness_sweep(mode="report", now=until - timedelta(minutes=30))
    assert during["none"] == []
    after = task_liveness.run_liveness_sweep(mode="report", now=until + timedelta(minutes=1))
    assert [n["task_id"] for n in after["none"]] == [t["id"]]


# ── monitors ─────────────────────────────────────────────────────────


def _messages_to(agent: str) -> list[dict]:
    rows = events.query_events(channels=["agent.message.inform"], limit=100)
    return [r for r in rows if r["payload"].get("to_agent") == agent]


def test_monitor_fires_exactly_once_to_the_claimant():
    t = _stalled_task()
    task_claim(t["id"], agent=AGENT, timeout_seconds=24 * 3600)
    at = datetime.now(UTC) + timedelta(minutes=30)
    task_update(t["id"], monitor_at=at.isoformat(), monitor_note="did CI pass?", actor=AGENT)

    early = task_liveness.run_liveness_sweep(mode="report", now=at - timedelta(minutes=1))
    assert early["monitors"] == []
    now = at + timedelta(minutes=1)
    first = task_liveness.run_liveness_sweep(mode="report", now=now)
    assert [m["delivery"] for m in first["monitors"]] == ["message"]
    second = task_liveness.run_liveness_sweep(mode="report", now=now + timedelta(minutes=5))
    assert second["monitors"] == []

    sent = _messages_to(AGENT)
    assert len(sent) == 1
    assert "did CI pass?" in sent[0]["payload"]["body"]
    assert sent[0]["payload"]["task_id"] == t["id"]
    conn = storage.connect()
    try:
        state = task_liveness.state(conn, t["id"])
    finally:
        conn.close()
    assert state["monitor"] is None
    assert state["monitor_attempts"] == 1
    assert state["monitor_result"].startswith(f"message to {AGENT}")


def test_unclaimed_monitor_becomes_a_digest_entry():
    t = _stalled_task()
    at = datetime.now(UTC) + timedelta(minutes=30)
    task_update(t["id"], monitor_at=at.isoformat(), actor=_operator())
    res = task_liveness.run_liveness_sweep(mode="report", now=at + timedelta(minutes=1))
    assert [m["delivery"] for m in res["monitors"]] == ["digest"]
    fired = _liveness_events("monitor_fired")
    assert len(fired) == 1 and fired[0]["payload"]["delivery"] == "digest"
    assert _messages_to(AGENT) == []


def test_refused_wake_message_falls_back_to_digest(monkeypatch):
    t = _stalled_task()
    task_claim(t["id"], agent=AGENT, timeout_seconds=24 * 3600)
    at = datetime.now(UTC) + timedelta(minutes=30)
    task_update(t["id"], monitor_at=at.isoformat(), actor=AGENT)

    def refuse(*_args, **_kwargs):
        raise ToolError("from_agent is not on the roster")

    monkeypatch.setattr(task_liveness, "_wake_claimant", refuse)
    res = task_liveness.run_liveness_sweep(mode="report", now=at + timedelta(minutes=1))
    assert [m["delivery"] for m in res["monitors"]] == ["digest"]
    again = task_liveness.run_liveness_sweep(mode="report", now=at + timedelta(minutes=2))
    assert again["monitors"] == []


def test_off_does_not_fire_monitors():
    t = _stalled_task()
    at = datetime.now(UTC) + timedelta(minutes=30)
    task_update(t["id"], monitor_at=at.isoformat(), actor=_operator())
    res = task_liveness.run_liveness_sweep(mode="off", now=at + timedelta(hours=1))
    assert res["monitors"] == []
    conn = storage.connect()
    try:
        assert task_liveness.state(conn, t["id"])["monitor"] is not None
    finally:
        conn.close()


# ── retry exhaustion ─────────────────────────────────────────────────


def _abandon_once(task_id: int) -> None:
    task_claim(task_id, agent=AGENT, timeout_seconds=600)
    conn = storage.connect()
    try:
        # A cancel request is a recorded stop, so the release reopens the task
        # (an open run with no recorded end would be held as interrupted).
        conn.execute(
            "UPDATE tasks SET lock_expires_at=?, cancel_requested=1 WHERE id=?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds"), task_id),
        )
        conn.commit()
    finally:
        conn.close()
    released = queue_hygiene.run_hygiene_pass(send_notifications=False)["released"]
    assert [r["task_id"] for r in released] == [task_id]


def test_three_abandoned_self_pickup_runs_escalate(monkeypatch):
    monkeypatch.setenv("UA_LIVENESS_SWEEP", "off")
    t = task_add("Keeps lapsing")
    for _ in range(2):
        _abandon_once(t["id"])
    now = datetime.now(UTC)
    assert task_liveness.run_liveness_sweep(mode="enforce", now=now)["retries_exhausted"] == []
    _abandon_once(t["id"])

    report = task_liveness.run_liveness_sweep(mode="report", now=now)
    assert [r["task_id"] for r in report["retries_exhausted"]] == [t["id"]]
    assert report["reported"] == [t["id"]]
    assert _row(t["id"])["status"] == "open"

    enforced = task_liveness.run_liveness_sweep(mode="enforce", now=now)
    assert enforced["blocked"] == [t["id"]], enforced["errors"]
    assert enforced["reported"] == []  # already reported for this run
    row = _row(t["id"])
    assert row["status"] == "blocked"
    assert "[liveness: retries exhausted @ " in row["notes"]
    assert "last_run=sp-" in row["notes"]

    # Reopened by an operator: the same streak does not re-block it.
    task_update(t["id"], status="open", actor="Desktop_Agent1")
    again = task_liveness.run_liveness_sweep(mode="enforce", now=now)
    assert again["blocked"] == []
    assert _row(t["id"])["status"] == "open"


def test_a_completed_run_resets_the_streak(monkeypatch):
    monkeypatch.setenv("UA_LIVENESS_SWEEP", "off")
    t = task_add("Recovered")
    for _ in range(2):
        _abandon_once(t["id"])
    task_claim(t["id"], agent=AGENT)
    task_update(t["id"], status="blocked", actor=AGENT)  # a real outcome: "blocked"
    task_update(t["id"], status="open", actor=AGENT)
    _abandon_once(t["id"])
    conn = storage.connect()
    try:
        streaks = task_liveness.abandoned_streaks(conn, [t["id"]])
    finally:
        conn.close()
    assert streaks[t["id"]][0] == 1


# ── hygiene integration ──────────────────────────────────────────────


def test_hygiene_pass_runs_the_sweep(monkeypatch):
    monkeypatch.setenv("UA_LIVENESS_SWEEP", "report")
    res = queue_hygiene.run_hygiene_pass(send_notifications=False)
    assert res["liveness"]["mode"] == "report"

    def broken(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(task_liveness, "run_liveness_sweep", broken)
    res = queue_hygiene.run_hygiene_pass(send_notifications=False)
    assert res["liveness"] == {"error": "RuntimeError: boom"}


# ── bounds and offsets on the parameters ─────────────────────────────


def _normalize(**kwargs):
    base = {"monitor_at": None, "monitor_note": None, "clear_monitor": False, "declared_wait": None}
    return task_liveness.normalize_update(**{**base, **kwargs}, now=NOW)


def test_monitor_at_is_capped_like_a_declared_wait():
    edge = NOW + task_liveness.DECLARED_WAIT_MAX
    assert _normalize(monitor_at=edge.isoformat())["monitor"][0] == edge.isoformat()
    with pytest.raises(ToolError, match="14 days"):
        _normalize(monitor_at=(edge + timedelta(seconds=1)).isoformat())


def test_naive_datetimes_are_refused_and_offsets_are_normalized():
    with pytest.raises(ToolError, match="UTC offset"):
        _normalize(monitor_at="2026-10-09T13:00:00")
    with pytest.raises(ToolError, match="UTC offset"):
        _normalize(declared_wait={"reason": "x", "until": "2026-10-10T00:00:00"})
    spec = _normalize(
        monitor_at="2026-10-09T09:00:00-05:00",
        declared_wait={"reason": "x", "until": "2026-10-10T00:00:00Z"},
    )
    assert spec["monitor"][0] == "2026-10-09T14:00:00+00:00"
    assert spec["wait"][1] == "2026-10-10T00:00:00+00:00"


# ── who may set a monitor or a declared wait ─────────────────────────


def _liveness_rows() -> int:
    conn = storage.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM task_liveness").fetchone()[0]
    finally:
        conn.close()


def _soon() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def test_lane_agent_may_set_on_an_unclaimed_task_and_others_may_not():
    t = _stalled_task()
    with pytest.raises(ToolError, match=f"{OTHER} may not set or clear"):
        task_update(t["id"], monitor_at=_soon(), actor=OTHER)
    with pytest.raises(ToolError, match="attributed caller"):
        task_update(t["id"], declared_wait={"reason": "x", "until": _soon()})
    assert _liveness_rows() == 0
    res = task_update(t["id"], monitor_at=_soon(), actor=AGENT)
    assert res["liveness"]["monitor"]["set_by"] == AGENT


def test_the_self_pickup_agent_may_declare_a_wait_on_an_unclaimed_task():
    # Moving a shared-pool task into doing without task_claim opens a
    # self-pickup run; that run's agent is the one holding the task.
    t = task_add("pool work")
    with _verified(OTHER):
        moved = task_update(t["id"], status="doing", worktree=False)
        assert moved["claimed_by"] is None and moved["current_run_agent"] == OTHER
        res = task_update(t["id"], declared_wait={"reason": "waiting on CI", "until": _soon()})
    assert res["liveness"]["declared_wait"]["reason"] == "waiting on CI"
    with pytest.raises(ToolError, match="may not set or clear"):
        task_update(t["id"], clear_monitor=True, actor=AGENT)


def test_claimant_holds_authority_over_a_claimed_task():
    t = _stalled_task()
    task_claim(t["id"], agent=OTHER, timeout_seconds=3600, expected_lane=AGENT)
    # The lane's agent loses authority once someone else holds the task.
    with pytest.raises(ToolError, match="its claimant"):
        task_update(t["id"], monitor_at=_soon(), actor=AGENT)
    with pytest.raises(ToolError, match="may not set or clear"):
        task_update(t["id"], clear_monitor=True, actor=AGENT)
    assert _liveness_rows() == 0
    task_update(t["id"], monitor_at=_soon(), actor=OTHER)
    task_update(t["id"], clear_monitor=True, actor=OTHER)


def test_operators_may_always_set_and_clear(monkeypatch):
    t = _stalled_task()
    task_claim(t["id"], agent=OTHER, timeout_seconds=3600, expected_lane=AGENT)
    task_update(t["id"], declared_wait={"reason": "x", "until": _soon()}, actor=_operator())
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Desktop_Agent1")
    res = task_update(t["id"], declared_wait={}, actor="Desktop_Agent1")
    assert res["liveness"]["declared_wait"] is None


def test_shared_pool_task_has_no_lane_agent():
    t = task_add("Pool task")
    with pytest.raises(ToolError, match="may not set or clear"):
        task_update(t["id"], monitor_at=_soon(), actor=AGENT)


# ── agent-lane backlog ───────────────────────────────────────────────


def test_agent_lane_is_backlog_while_its_agent_is_seen():
    t = _stalled_task()
    now = _later()
    _seen(AGENT, now - timedelta(hours=task_liveness.AGENT_LANE_SEEN_HOURS - 1))
    res = task_liveness.run_liveness_sweep(mode="report", now=now)
    assert res["none"] == [] and res["checked"] == 1
    _seen(AGENT, now - timedelta(hours=task_liveness.AGENT_LANE_SEEN_HOURS + 1))
    res = task_liveness.run_liveness_sweep(mode="report", now=now)
    assert [n["task_id"] for n in res["none"]] == [t["id"]]
    assert "not seen on the hub" in res["none"][0]["detail"]


def test_a_doctor_stamp_is_not_the_agent_being_seen():
    t = _stalled_task()
    now = _later()
    _seen(AGENT, now - timedelta(hours=1), via="system_doctor")
    res = task_liveness.run_liveness_sweep(mode="report", now=now)
    assert [n["task_id"] for n in res["none"]] == [t["id"]]


# ── unclaimed doing row with a live run agent ────────────────────────


def _unclaimed_run(task_id: int) -> str:
    """Move a task to doing as AGENT without a claim; return its open run id."""
    with _verified(AGENT):
        task_update(task_id, status="doing")
    row = _row(task_id)
    assert row["claimed_by"] is None and row["current_run_id"]
    assert row["current_run_agent"] == AGENT
    return row["current_run_id"]


def _live_run_agents(task_id: int, now: datetime) -> dict:
    conn = storage.connect()
    try:
        return dict(task_liveness.gather_context(conn, [task_id], now=now).live_run_agents)
    finally:
        conn.close()


def test_unclaimed_doing_run_is_live_while_its_agent_acts():
    t = task_add("Self-pickup")
    opened = datetime.now(UTC)
    _unclaimed_run(t["id"])
    window = registry.live_agent_window()
    # Seen only as the run opened: not evidence the agent is still on it.
    _seen(AGENT, opened)
    assert _live_run_agents(t["id"], opened + timedelta(minutes=30)) == {}
    _seen(AGENT, opened + timedelta(minutes=5))
    assert _live_run_agents(t["id"], opened + timedelta(minutes=30)) == {t["id"]: AGENT}
    stale = opened + timedelta(minutes=6) + window
    assert _live_run_agents(t["id"], stale) == {}

    # Through the sweep, past the implicit lease: a seen agent keeps it off NONE.
    now = _later()
    _seen(AGENT, now - timedelta(minutes=10))
    assert task_liveness.run_liveness_sweep(mode="report", now=now)["none"] == []
    _seen(AGENT, now - window - timedelta(minutes=1))
    res = task_liveness.run_liveness_sweep(mode="report", now=now)
    assert [n["task_id"] for n in res["none"]] == [t["id"]]


def test_run_record_names_the_agent_when_the_cache_does_not():
    t = task_add("Self-pickup")
    _unclaimed_run(t["id"])
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET current_run_agent=NULL WHERE id=?", (t["id"],))
        conn.commit()
    finally:
        conn.close()
    now = _later()
    _seen(AGENT, now - timedelta(minutes=10))
    conn = storage.connect()
    try:
        ctx = task_liveness.gather_context(conn, [t["id"]], now=now)
    finally:
        conn.close()
    assert ctx.live_run_agents == {t["id"]: AGENT}


# ── executor attempts that never finish ──────────────────────────────


def _attempt(task_id: int, started: datetime, delegation_id: int | None = None) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO executor_attempts (task_id, delegation_id, agent, backend, started_at) "
            "VALUES (?, ?, ?, 'codex', ?)",
            (task_id, delegation_id, AGENT, started.isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def _open_runs(task_id: int, now: datetime) -> dict:
    conn = storage.connect()
    try:
        return dict(task_liveness.gather_context(conn, [task_id], now=now).open_runs)
    finally:
        conn.close()


def test_unfinished_attempt_counts_only_inside_its_lease():
    t = task_add("Executor work")
    now = datetime.now(UTC)
    _attempt(t["id"], now - task_liveness.EXECUTOR_ATTEMPT_LEASE - timedelta(minutes=1))
    assert _open_runs(t["id"], now) == {}
    _attempt(t["id"], now - timedelta(minutes=5))
    assert "executor attempt" in _open_runs(t["id"], now)[t["id"]]


def test_stale_attempt_counts_while_the_task_claim_is_live():
    t = task_add("Executor work")
    now = datetime.now(UTC)
    _attempt(t["id"], now - timedelta(hours=3))
    task_claim(t["id"], agent=AGENT, timeout_seconds=3600)
    assert t["id"] in _open_runs(t["id"], now)


def test_stale_attempt_counts_while_its_delegation_claim_is_live():
    t = task_add("Executor work")
    now = datetime.now(UTC)
    claimed = now - timedelta(seconds=60)
    conn = storage.connect()
    try:
        ts = claimed.isoformat(timespec="seconds")
        delegation = conn.execute(
            "INSERT INTO delegations (from_agent, to_agent, task_id, task_spec, status, "
            "last_attempt_at, created_at, updated_at) "
            "VALUES ('Desktop_Agent1', ?, ?, 'spec', 'in_flight', ?, ?, ?)",
            (AGENT, t["id"], ts, ts, ts),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()
    _attempt(t["id"], now - timedelta(hours=3), delegation_id=delegation)
    assert t["id"] in _open_runs(t["id"], now)
    ttl = timedelta(seconds=config.dispatch_scheduler_lease_ttl_s())
    assert _open_runs(t["id"], now + ttl) == {}


# ── wait chains ──────────────────────────────────────────────────────


def _wait_state(task_id: int) -> dict:
    conn = storage.connect()
    try:
        return task_liveness.state(conn, task_id)["declared_wait"]
    finally:
        conn.close()


def test_waits_declared_back_to_back_form_one_chain(monkeypatch):
    t = _stalled_task()
    start = datetime.now(UTC).replace(microsecond=0)
    clock = {"now": start}
    monkeypatch.setattr(task_liveness, "_clock", lambda: clock["now"])

    def declare(day: int) -> None:
        clock["now"] = start + timedelta(days=day)
        until = clock["now"] + timedelta(days=10)
        task_update(
            t["id"],
            declared_wait={"reason": "vendor reply", "until": until.isoformat()},
            actor=_operator(),
        )

    for day in (0, 9, 18, 27):
        declare(day)
    first = _wait_state(t["id"])["first_declared_at"]
    assert task_liveness.parse_ts(first) == start

    early = task_liveness.run_liveness_sweep(mode="report", now=start + timedelta(days=28))
    assert early["wait_chains"] == []
    now = start + timedelta(days=29)
    res = task_liveness.run_liveness_sweep(mode="report", now=now)
    assert [w["task_id"] for w in res["wait_chains"]] == [t["id"]]
    assert res["none"] == [] and res["blocked"] == []  # still declared_wait
    again = task_liveness.run_liveness_sweep(mode="enforce", now=now)
    assert again["reported"] == [] and again["blocked"] == []
    assert len(_liveness_events("wait_chain")) == 1
    assert _row(t["id"])["status"] == "open"
    task_liveness.run_liveness_sweep(mode="report", now=now + timedelta(days=1))
    assert len(_liveness_events("wait_chain")) == 2


def test_a_clear_or_an_expiry_starts_a_new_chain(monkeypatch):
    t = _stalled_task()
    start = datetime.now(UTC)
    clock = {"now": start}
    monkeypatch.setattr(task_liveness, "_clock", lambda: clock["now"])

    def declare(at: datetime, days: int = 2) -> str:
        clock["now"] = at
        until = (at + timedelta(days=days)).isoformat()
        task_update(t["id"], declared_wait={"reason": "x", "until": until}, actor=AGENT)
        return _wait_state(t["id"])["first_declared_at"]

    first = declare(start)
    assert declare(start + timedelta(days=1)) == first
    # Expired before the next declaration: a new chain.
    renewed = declare(start + timedelta(days=4))
    assert renewed != first
    task_update(t["id"], declared_wait={}, actor=AGENT)
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT wait_first_declared_at FROM task_liveness WHERE task_id=?", (t["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["wait_first_declared_at"] is None
    assert declare(start + timedelta(days=4, hours=1)) != renewed


def test_wait_chain_reaches_the_digest(monkeypatch):
    monkeypatch.setenv("UA_ANOMALY_SERVICES", "")
    t = _stalled_task()
    until = datetime.now(UTC) + timedelta(days=3)
    task_update(t["id"], declared_wait={"reason": "x", "until": until.isoformat()}, actor=AGENT)
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE task_liveness SET wait_first_declared_at=? WHERE task_id=?", (old, t["id"])
        )
        conn.commit()
    finally:
        conn.close()
    task_liveness.run_liveness_sweep(mode="report", now=datetime.now(UTC))
    found = [a for a in monitor.check_anomalies()["anomalies"] if a["rule"] == "task_liveness"]
    assert [a["subject"] for a in found] == [f"task:{t['id']}"]
    assert found[0]["detail"].startswith("declared wait chain:")


# ── which abandonments count toward retry exhaustion ─────────────────


def _finish_event(task_id: int, run_id: str, *, reason: str, reason_code: str | None) -> None:
    events.record_event(
        channel=run_records.CHANNEL,
        payload={
            "kind": run_records.KIND_FINISHED,
            "run_id": run_id,
            "task_id": task_id,
            "outcome": "abandoned",
            "reason": reason,
            "reason_code": reason_code,
        },
        originator=AGENT,
    )


def _streak(task_id: int, now: datetime | None = None) -> int:
    conn = storage.connect()
    try:
        return task_liveness.abandoned_streaks(conn, [task_id], now=now).get(task_id, (0, ""))[0]
    finally:
        conn.close()


def test_only_lease_lapse_abandonments_count(monkeypatch):
    monkeypatch.setenv("UA_LIVENESS_SWEEP", "off")
    t = task_add("Mixed abandonments")
    _abandon_once(t["id"])
    assert _streak(t["id"]) == 1
    for n, reason in enumerate(
        (
            "finish not recorded",
            "task re-entered doing; this run's finish was not recorded",
            "agent terminated: crashed",
        )
    ):
        _finish_event(t["id"], f"sp-x{n}", reason=reason, reason_code=None)
    assert _streak(t["id"]) == 1  # neither counted nor reset
    # A takeover after the lapse counts, with or without a reason_code.
    _finish_event(
        t["id"],
        "sp-y1",
        reason="claimed after the lease lapsed",
        reason_code=run_records.REASON_LEASE_EXPIRED,
    )
    _finish_event(t["id"], "sp-y2", reason="claimed after the lease lapsed", reason_code=None)
    assert _streak(t["id"]) == 3


def test_streaks_only_read_the_last_thirty_days(monkeypatch):
    monkeypatch.setenv("UA_LIVENESS_SWEEP", "off")
    t = task_add("Old lapses")
    for _ in range(3):
        _abandon_once(t["id"])
    assert _streak(t["id"]) == 3
    later = datetime.now(UTC) + task_liveness.STREAK_WINDOW + timedelta(days=1)
    assert _streak(t["id"], later) == 0
    res = task_liveness.run_liveness_sweep(mode="report", now=later)
    assert res["retries_exhausted"] == []


# ── stored ownership does not change liveness (#2388) ────────────────


@pytest.mark.parametrize(("task", "context", "_kind"), CLASSIFIER_CASES)
@pytest.mark.parametrize("stored_owner", [AGENT, OTHER])
def test_stored_owner_does_not_change_the_classification(task, context, _kind, stored_owner):
    """A stored owner can be stale until a write path keeps it current.

    A lease belongs to claimed_by, so reading next_owner as the claimant would
    report a released task as a live claim and hide a real stall.
    """
    stored = {
        **task,
        "next_owner": stored_owner,
        "waiting_on": "ci",
        "terminal_predicate": "merged",
        "ownership_generation": 3,
    }
    assert action_path(stored, now=NOW, context=context) == action_path(
        task, now=NOW, context=context
    )


def test_sweep_reports_the_same_with_and_without_a_stored_owner():
    plain = _stalled_task("No stored owner")
    stored = _stalled_task("Stored owner")
    leases = {}
    for task in (plain, stored):
        claimed = task_claim(task["id"], agent=AGENT, timeout_seconds=600)
        leases[task["id"]] = datetime.fromisoformat(claimed["lock_expires_at"])
    task_update(stored["id"], next_owner=OTHER, actor=AGENT)
    res = task_liveness.run_liveness_sweep(mode="report", now=_later(), dry_run=True)
    details = {entry["task_id"]: entry["detail"] for entry in res["none"]}
    assert "lapsed" in details[plain["id"]]
    # Each detail must name its own task's lease, so the masked comparison
    # below cannot pass on a sweep that read the wrong row's expiry.
    for task_id, lease in leases.items():
        assert f"lapsed at {task_liveness._iso(lease)}" in details[task_id]
    # Each detail names its own lease expiry, and the two claims can land in
    # different wall-clock seconds; compare everything else.
    stamp = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?\+00:00")
    assert stamp.sub("<ts>", details[stored["id"]]) == stamp.sub("<ts>", details[plain["id"]])


def _hold(task_id: int, reason: str, actor: str = AGENT) -> dict:
    until = (datetime.now(UTC) + timedelta(hours=2)).isoformat(timespec="seconds")
    return task_update(task_id, declared_wait={"reason": reason, "until": until}, actor=actor)


def _claimed(agent: str = AGENT) -> int:
    task_id = task_add("hold quota")["id"]
    task_claim(task_id, agent=agent, timeout_seconds=3600)
    return task_id


def test_the_fourth_hold_in_an_hour_is_refused_and_names_the_other_three():
    task_id = _claimed()
    for reason in ("first wait", "second wait", "third wait"):
        _hold(task_id, reason)
    before = _wait_state(task_id)
    with pytest.raises(ToolError) as refused:
        _hold(task_id, "fourth wait")
    for reason in ("first wait", "second wait", "third wait"):
        assert reason in str(refused.value)
    assert _wait_state(task_id) == before


def test_a_monitor_counts_as_a_hold_and_clearing_does_not_refund_one():
    task_id = _claimed()
    at = (datetime.now(UTC) + timedelta(hours=1)).isoformat(timespec="seconds")
    task_update(task_id, monitor_at=at, monitor_note="look at ci", actor=AGENT)
    _hold(task_id, "second")
    task_update(task_id, declared_wait={}, actor=AGENT)
    _hold(task_id, "third")
    with pytest.raises(ToolError, match="look at ci"):
        _hold(task_id, "fourth")


def test_the_hold_quota_is_per_task_and_agent():
    first = _claimed()
    for reason in ("a", "b", "c"):
        _hold(first, reason)
    second = _claimed()
    _hold(second, "another task")
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET claimed_by=? WHERE id=?", (OTHER, first))
        conn.commit()
    finally:
        conn.close()
    _hold(first, "another agent", actor=OTHER)


def test_the_hold_quota_is_a_rolling_hour(monkeypatch):
    task_id = _claimed()
    for reason in ("a", "b", "c"):
        _hold(task_id, reason)
    later = datetime.now(UTC) + timedelta(minutes=61)
    monkeypatch.setattr(task_liveness, "_clock", lambda: later)
    until = (later + timedelta(hours=2)).isoformat(timespec="seconds")
    task_update(task_id, declared_wait={"reason": "d", "until": until}, actor=AGENT)


def test_the_operator_is_not_counted_against_the_hold_quota():
    task_id = _claimed()
    for reason in ("a", "b", "c", "d"):
        _hold(task_id, reason, actor=config.operator_label())


def test_a_declared_wait_carries_an_optional_next_step():
    task_id = _claimed()
    until = (datetime.now(UTC) + timedelta(hours=2)).isoformat(timespec="seconds")
    result = task_update(
        task_id,
        declared_wait={"reason": "ci", "until": until, "next_step": "merge the batch"},
        actor=AGENT,
    )
    assert result["liveness"]["declared_wait"]["next_step"] == "merge the batch"
    assert _hold(task_id, "again")["liveness"]["declared_wait"]["next_step"] is None
    with pytest.raises(ToolError, match="next_step"):
        task_update(
            task_id, declared_wait={"reason": "x", "until": until, "other": "y"}, actor=AGENT
        )


SHA = "a" * 40
OLD_SHA = "b" * 40
DEPLOY_NODE = "deploy-node.local"


def _node(name: str = DEPLOY_NODE, sha: str | None = SHA, *, at: datetime | None = None) -> None:
    node_queue.register(name, identity=AGENT, now=at or datetime.now(UTC))
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE nodes SET facts_json=? WHERE node=?", (json.dumps({"code_sha": sha}), name)
        )
        conn.commit()
    finally:
        conn.close()


def _deploy_wait(task_id: int, target: str = DEPLOY_NODE, sha: str = SHA) -> dict:
    until = (datetime.now(UTC) + timedelta(hours=2)).isoformat(timespec="seconds")
    return task_update(
        task_id,
        declared_wait={
            "reason": "deploy",
            "until": until,
            "deployment": {"target": target, "sha": sha},
        },
        actor=AGENT,
    )


def _sweep(**kwargs) -> list[dict]:
    return task_liveness.run_liveness_sweep(mode="report", **kwargs)["deployments"]


def test_a_deployment_wait_clears_when_that_target_reports_that_sha():
    _node(sha=OLD_SHA)
    task_id = _claimed()
    state = _deploy_wait(task_id)["liveness"]["declared_wait"]
    assert state["deployment"] == {"target": DEPLOY_NODE, "sha": SHA}
    assert _sweep() == []
    assert _wait_state(task_id) is not None

    _node(sha=SHA)
    reached = _sweep()
    assert [(r["task_id"], r["target"]) for r in reached] == [(task_id, DEPLOY_NODE)]
    assert _wait_state(task_id) is None
    assert _sweep() == []


def test_a_merge_or_another_target_cannot_clear_a_deployment_wait(monkeypatch):
    _node(sha=OLD_SHA)
    _node("other-node.local", sha=SHA)
    monkeypatch.setattr(hub_runtime, "read_hub_start", lambda: {"sha": SHA, "pid": os.getpid()})
    task_id = _claimed()
    _deploy_wait(task_id)
    assert _sweep() == []
    assert _wait_state(task_id) is not None


def test_an_offline_node_cannot_clear_a_deployment_wait():
    _node()
    task_id = _claimed()
    _deploy_wait(task_id)
    _node(at=datetime.now(UTC) - timedelta(days=2))
    assert _sweep() == []


@pytest.mark.parametrize(
    ("sha", "alive", "cleared"), [(SHA, True, True), (OLD_SHA, True, False), (SHA, False, False)]
)
def test_the_hub_target_reads_the_running_process(monkeypatch, sha, alive, cleared):
    task_id = _claimed()
    _deploy_wait(task_id, target="hub")
    monkeypatch.setattr(hub_runtime, "read_hub_start", lambda: {"sha": sha, "pid": 4242})
    monkeypatch.setattr(hub_runtime, "pid_alive", lambda pid: alive)
    assert bool(_sweep()) is cleared


def test_reaching_a_deployment_wakes_the_claimant_once():
    _node()
    task_id = _claimed()
    _deploy_wait(task_id)
    _sweep()
    _sweep()
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT recipient FROM message_index WHERE task_id=? AND sender=?",
            (task_id, task_liveness.ACTOR),
        ).fetchall()
    finally:
        conn.close()
    assert [r["recipient"] for r in rows] == [AGENT]


def test_a_dry_run_reports_a_reached_deployment_and_leaves_the_wait():
    _node()
    task_id = _claimed()
    _deploy_wait(task_id)
    assert [r["task_id"] for r in _sweep(dry_run=True)] == [task_id]
    assert _wait_state(task_id) is not None


@pytest.mark.parametrize(
    ("deployment", "message"),
    [
        ({"target": "nowhere.local", "sha": SHA}, "unknown deployment target"),
        ({"target": "hub", "sha": "abc123"}, "40-character"),
        ({"target": "hub"}, "exactly"),
    ],
)
def test_a_bad_deployment_is_refused_before_any_write(deployment, message):
    task_id = _claimed()
    until = (datetime.now(UTC) + timedelta(hours=2)).isoformat(timespec="seconds")
    with pytest.raises(ToolError, match=message):
        task_update(
            task_id,
            declared_wait={"reason": "x", "until": until, "deployment": deployment},
            actor=AGENT,
        )
    assert _wait_state(task_id) is None


def test_a_plain_wait_replaces_a_deployment_wait():
    task_id = _claimed()
    _deploy_wait(task_id, target="hub")
    assert _hold(task_id, "plain")["liveness"]["declared_wait"]["deployment"] is None


def test_a_sha256_commit_can_be_waited_on():
    """The hub and node readers accept 64-character ids, so a wait must too (#2429)."""
    _node(sha="c" * 64)
    task_id = _claimed()
    assert _deploy_wait(task_id, sha="c" * 64)["liveness"]["declared_wait"]["deployment"]["sha"]
