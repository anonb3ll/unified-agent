"""Owner liveness is one read over the agent, node and task sources (task #2395).

Ground truth:
- a probe is active, terminal, absent or unknown;
- online and idle agent readings, a live node heartbeat, and this owner's
  live claim or open run are active;
- a done task is terminal, and the claim row is left as it was;
- no agent, no node and no task is absent;
- a quiet agent, a quiet node, an unreadable source or a stale lease with
  nothing else live is unknown: the result keeps the owner, warns, and does
  not write the claim;
- a stale lease is a candidate flag. It does not demote a live owner or an
  open run, and it is not itself terminal or absent.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import node_queue, node_registry, owner_liveness, storage
from unified_agent.owner_liveness import ABSENT, ACTIVE, TERMINAL, UNKNOWN
from unified_agent.tools.tasks import task_add, task_claim, task_update

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
OWNER = "Desktop_Agent2"
NODE = "owner-probe.local"


def _probe(owner: str = OWNER, task_id: int | None = None) -> owner_liveness.OwnerProbe:
    conn = storage.connect()
    try:
        return owner_liveness.probe_owner(conn, owner, task_id=task_id, now=NOW)
    finally:
        conn.close()


def _seen(at: datetime | None) -> None:
    stamp = NOW.isoformat()
    seen = None if at is None else at.isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agents (label, kind, known, first_seen, last_seen, last_seen_via, "
            "created_at, updated_at) VALUES (?, 'agent', 1, ?, ?, 'heartbeat', ?, ?)",
            (OWNER, stamp, seen, stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()


def _claim(*, status: str = "doing", lease: datetime | None) -> int:
    task = task_add("owned work")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status=?, claimed_by=?, lock_expires_at=?, updated_at=? WHERE id=?",
            (
                status,
                OWNER,
                None if lease is None else lease.isoformat(),
                NOW.isoformat(),
                task["id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return int(task["id"])


def _task_row(task_id: int) -> tuple:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT status, claimed_by, lock_expires_at FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()
    return (row["status"], row["claimed_by"], row["lock_expires_at"])


def _running_dispatch(task_id: int) -> None:
    stamp = NOW.isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
            "created_at, updated_at) VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (f"run-{task_id}", task_id, OWNER, OWNER, stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("age", "reading"),
    [(timedelta(0), "online"), (timedelta(minutes=5), "idle")],
)
def test_a_recently_seen_agent_is_active(age, reading):
    _seen(NOW - age)
    probe = _probe()
    assert probe.outcome == ACTIVE
    assert probe.owner == OWNER
    assert probe.keep_owner is True
    assert probe.warning is None
    assert probe.candidate is False
    assert probe.source("agent") == reading
    assert probe.source("node") == ABSENT
    assert probe.source("task") == ABSENT


def test_a_live_node_heartbeat_is_active_when_the_agent_is_quiet():
    _seen(NOW - timedelta(days=2))
    node_queue.register(NODE, identity=OWNER, now=NOW)
    probe = _probe()
    assert probe.outcome == ACTIVE
    assert probe.keep_owner is True
    assert probe.source("agent") == "offline"
    assert probe.source("node") == "online"


def test_a_live_claim_is_active_when_the_agent_is_quiet():
    _seen(NOW - timedelta(days=2))
    task_id = _claim(lease=NOW + timedelta(hours=1))
    probe = _probe(task_id=task_id)
    assert probe.outcome == ACTIVE
    assert probe.candidate is False
    assert probe.source("agent") == "offline"
    assert probe.source("task") == "run"
    assert _task_row(task_id)[1] == OWNER


def test_an_open_run_stays_active_when_the_lease_is_stale():
    task_id = _claim(lease=NOW - timedelta(hours=1))
    _running_dispatch(task_id)
    before = _task_row(task_id)
    probe = _probe(task_id=task_id)
    assert probe.outcome == ACTIVE
    assert probe.candidate is True
    assert probe.source("task") == "run"
    assert probe.warning is None
    assert _task_row(task_id) == before


def test_a_finished_task_is_terminal_and_the_claim_stays():
    _seen(NOW - timedelta(days=2))
    task_id = _claim(status="done", lease=NOW - timedelta(hours=1))
    before = _task_row(task_id)
    probe = _probe(task_id=task_id)
    assert probe.outcome == TERMINAL
    assert probe.keep_owner is False
    assert probe.warning is None
    assert probe.candidate is False
    assert probe.source("task") == TERMINAL
    assert _task_row(task_id) == before


def test_a_finished_task_is_terminal_even_while_its_owner_is_online():
    """A label is shared by its sessions, so a busy owner is the usual case."""
    _seen(NOW)
    task_id = _claim(status="done", lease=NOW - timedelta(hours=1))
    probe = _probe(task_id=task_id)
    assert probe.outcome == owner_liveness.TERMINAL and probe.keep_owner is False
    assert probe.source("agent") == "online"


def test_an_owner_with_no_record_is_absent():
    probe = _probe("missing_owner")
    assert probe.outcome == ABSENT
    assert probe.owner == "missing_owner"
    assert probe.keep_owner is False
    assert probe.warning is None
    assert probe.candidate is False
    assert probe.source("agent") == ABSENT
    assert probe.source("node") == ABSENT
    assert probe.source("task") == ABSENT


def test_a_quiet_agent_is_unknown_and_keeps_the_owner(caplog):
    _seen(NOW - timedelta(days=2))
    with caplog.at_level(logging.WARNING, logger="unified_agent.owner_liveness"):
        probe = _probe()
    assert probe.outcome == UNKNOWN
    assert probe.owner == OWNER
    assert probe.keep_owner is True
    assert probe.warning
    assert "owner kept" in probe.warning
    assert probe.candidate is False
    assert probe.source("agent") == "offline"
    assert any("owner kept" in rec.message for rec in caplog.records)


def test_a_stale_lease_is_only_a_candidate_and_unknown_keeps_the_owner():
    _seen(None)
    task_id = _claim(lease=NOW - timedelta(minutes=5))
    before = _task_row(task_id)
    probe = _probe(task_id=task_id)
    assert probe.outcome == UNKNOWN
    assert probe.keep_owner is True
    assert probe.candidate is True
    assert probe.source("agent") == "unknown"
    assert probe.source("task") == "lapsed"
    assert probe.warning is not None
    assert "candidate" in probe.warning
    assert "owner kept" in probe.warning
    assert _task_row(task_id) == before
    assert before[1] == OWNER


def test_a_stale_lease_does_not_override_a_live_agent():
    _seen(NOW)
    task_id = _claim(lease=NOW - timedelta(minutes=5))
    probe = _probe(task_id=task_id)
    assert probe.outcome == ACTIVE
    assert probe.keep_owner is True
    assert probe.candidate is True
    assert probe.warning is None
    assert probe.source("agent") == "online"
    assert probe.source("task") == "lapsed"


def test_a_quiet_node_is_not_absence():
    node_queue.register(NODE, identity=OWNER, now=NOW - timedelta(days=2))
    probe = _probe()
    assert probe.outcome == UNKNOWN
    assert probe.keep_owner is True
    assert probe.source("agent") == ABSENT
    assert probe.source("node") == "offline"
    assert probe.warning is not None


def test_an_unreadable_node_keeps_the_owner(monkeypatch):
    _seen(NOW - timedelta(days=2))

    def locked() -> dict:
        raise sqlite3.Error("disk I/O error")

    monkeypatch.setattr(node_registry, "read_nodes", locked)
    probe = _probe()
    assert probe.outcome == UNKNOWN
    assert probe.keep_owner is True
    assert probe.source("node") == "unreadable"
    assert probe.warning is not None
    assert "owner kept" in probe.warning


def test_another_owners_live_claim_does_not_count():
    task = task_add("someone else's work")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status='doing', claimed_by=?, lock_expires_at=? WHERE id=?",
            ("Desktop_Agent1", (NOW + timedelta(hours=1)).isoformat(), task["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    probe = _probe(task_id=int(task["id"]))
    assert probe.outcome == UNKNOWN
    assert probe.keep_owner is True
    assert probe.source("task") == "quiet"
    assert probe.candidate is False


def _lapsed_claim_with(*, claimed_at: datetime | None = None, **columns) -> int:
    task_id = _claim(lease=NOW - timedelta(minutes=5))
    stamp = None if claimed_at is None else claimed_at.isoformat()
    sets = ", ".join(f"{name}=?" for name in ("claimed_at", *columns))
    conn = storage.connect()
    try:
        conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (stamp, *columns.values(), task_id))
        conn.commit()
    finally:
        conn.close()
    return task_id


def _dispatch(task_id: int, state: str, at: datetime) -> None:
    stamp = at.isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"run-{task_id}-{state}", task_id, OWNER, state, OWNER, stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()


def _lapsed_after_recorded_release() -> int:
    """A real self-pickup finish through the recorder, then a lapsed claim on top."""
    task_id = int(task_add("owned work")["id"])
    task_claim(task_id, agent=OWNER)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET lock_expires_at=? WHERE id=?",
            ((NOW - timedelta(days=30)).isoformat(), task_id),
        )
        conn.commit()
    finally:
        conn.close()
    task_update(task_id, release_claim=True)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status='doing', claimed_by=?, lock_expires_at=?, claimed_at=NULL "
            "WHERE id=?",
            (OWNER, (NOW - timedelta(minutes=5)).isoformat(), task_id),
        )
        conn.commit()
    finally:
        conn.close()
    return task_id


def test_a_cancel_request_on_the_run_is_terminal_with_the_owner_online():
    _seen(NOW)
    task_id = _lapsed_claim_with(cancel_requested=1, current_run_agent=OWNER)
    probe = _probe(task_id=task_id)
    assert probe.outcome == TERMINAL
    assert probe.source("task") == "ended"
    assert probe.keep_owner is False


@pytest.mark.parametrize(
    ("state", "outcome"),
    [("failed", TERMINAL), ("retryable", UNKNOWN), ("finished", UNKNOWN)],
)
def test_only_a_failed_dispatch_of_this_claim_ends_it(state, outcome):
    _seen(NOW - timedelta(days=2))
    task_id = _lapsed_claim_with(claimed_at=NOW - timedelta(hours=2))
    _dispatch(task_id, state, NOW - timedelta(hours=1))
    assert _probe(task_id=task_id).outcome == outcome


def test_a_failure_before_this_claim_is_not_its_end():
    _seen(NOW - timedelta(days=2))
    task_id = _lapsed_claim_with(claimed_at=NOW - timedelta(hours=1))
    _dispatch(task_id, "failed", NOW - timedelta(hours=3))
    probe = _probe(task_id=task_id)
    assert probe.outcome == UNKNOWN
    assert probe.source("task") == "lapsed"


def test_a_recorded_released_run_is_terminal_unless_the_owner_acted_after():
    _seen(NOW - timedelta(days=2))
    task_id = _lapsed_after_recorded_release()
    assert _probe(task_id=task_id).outcome == TERMINAL

    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO task_liveness (task_id, updated_at) VALUES (?, ?)",
            (task_id, (datetime.now(UTC) + timedelta(hours=1)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    probe = _probe(task_id=task_id)
    assert probe.outcome != TERMINAL
    assert probe.source("task") == "lapsed"


def test_a_lapsed_claim_by_an_owner_with_no_record_is_absent():
    task_id = _claim(lease=NOW - timedelta(minutes=5))
    probe = _probe(task_id=task_id)
    assert probe.outcome == ABSENT
    assert probe.candidate is True
    assert probe.source("task") == "lapsed"


def _session(status: str, *, ended_at: datetime | None, sid: str) -> None:
    stamp = (NOW - timedelta(days=1)).isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO agent_sessions (id, agent, kind, status, started_at, "
            "last_heartbeat_at, ended_at) VALUES (?, ?, 'interactive', ?, ?, ?, ?)",
            (sid, OWNER, status, stamp, stamp, None if ended_at is None else ended_at.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("sessions", "agent_age", "outcome"),
    [
        ([("abandoned", 10), ("completed", 30)], timedelta(days=2), TERMINAL),
        ([("abandoned", 10), ("running", None)], timedelta(days=2), UNKNOWN),
        ([("abandoned", 90)], timedelta(days=2), UNKNOWN),
        ([("abandoned", 10)], timedelta(0), ACTIVE),
        ([], timedelta(days=2), UNKNOWN),
    ],
    ids=["all-ended-after-claim", "one-open", "ended-before-claim", "agent-live", "no-sessions"],
)
def test_every_owner_session_ended_after_the_claim_is_terminal_only_without_live_signs(
    sessions, agent_age, outcome
):
    _seen(NOW - agent_age)
    task_id = _lapsed_claim_with(claimed_at=NOW - timedelta(hours=1))
    for n, (status, ended_minutes_ago) in enumerate(sessions):
        ended = None if ended_minutes_ago is None else NOW - timedelta(minutes=ended_minutes_ago)
        _session(status, ended_at=ended, sid=f"s{n}")
    probe = _probe(task_id=task_id)
    assert probe.outcome == outcome
    if outcome == TERMINAL:
        assert probe.source("task") == "sessions_ended"
