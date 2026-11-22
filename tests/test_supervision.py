"""FirstMate-style zero-token event-driven supervision (task #1028)."""

from __future__ import annotations

import pytest

from unified_agent import events
from unified_agent.supervision import (
    CHANNEL_SUPERVISOR_WAKE,
    classify_task_transition,
    drain_supervisor_wakes,
    maybe_emit_supervisor_wake,
    recent_supervisor_wakes,
)


@pytest.mark.parametrize(
    ("old", "new", "kind", "reason"),
    [
        ("doing", "done", "wake", "completion"),
        ("doing", "blocked", "wake", "block"),
        ("doing", "waiting_review", "wake", "needs-decision"),
        ("open", "doing", "silence", "routine"),
        ("doing", "doing", "silence", "no-change"),
        (None, "doing", "silence", "routine"),
        ("blocked", "doing", "silence", "routine"),
        ("doing", "cancelled", "silence", "routine"),
        ("done", "done", "silence", "no-change"),
    ],
)
def test_classify_task_transition(old, new, kind, reason):
    decision = classify_task_transition(old, new)
    assert decision.kind == kind
    assert decision.reason == reason
    assert decision.should_wake is (kind == "wake")


def test_completion_emits_wake_without_poll(monkeypatch):
    """DoD (1): completion triggers a durable wake (mutation-site, not poll)."""
    monkeypatch.delenv("UA_EVENT_SUPERVISION", raising=False)
    wake = maybe_emit_supervisor_wake(
        task_id=1028,
        old_status="doing",
        new_status="done",
        title="ship supervision",
        agent="Desktop_Agent4",
        project="unified-agent",
        updated_at="2026-09-16T12:00:00+00:00",
    )
    assert wake is not None
    assert wake["reason"] == "completion"
    assert wake["task_id"] == 1028
    assert wake["channel"] == CHANNEL_SUPERVISOR_WAKE
    assert wake["event_ulid"]

    rows = events.query_events(channels=[CHANNEL_SUPERVISOR_WAKE])
    assert any(r["ulid"] == wake["event_ulid"] for r in rows)
    match = next(r for r in rows if r["ulid"] == wake["event_ulid"])
    assert match["payload"]["new_status"] == "done"
    assert match["originator"] == "Desktop_Agent4"


def test_block_emits_wake(monkeypatch):
    monkeypatch.delenv("UA_EVENT_SUPERVISION", raising=False)
    wake = maybe_emit_supervisor_wake(
        task_id=55,
        old_status="doing",
        new_status="blocked",
        title="needs Operator",
        agent="Desktop_agent3",
        updated_at="2026-09-16T12:01:00+00:00",
    )
    assert wake is not None
    assert wake["reason"] == "block"


def test_routine_progress_emits_no_wake(monkeypatch):
    """DoD (2): routine progress stays silent — no supervisor wake row."""
    monkeypatch.delenv("UA_EVENT_SUPERVISION", raising=False)
    before = events.query_events(channels=[CHANNEL_SUPERVISOR_WAKE])
    before_ids = {r["ulid"] for r in before}

    assert (
        maybe_emit_supervisor_wake(
            task_id=99,
            old_status="open",
            new_status="doing",
            title="starting work",
            agent="Desktop_Agent1",
            updated_at="2026-09-16T12:02:00+00:00",
        )
        is None
    )
    assert (
        maybe_emit_supervisor_wake(
            task_id=99,
            old_status="doing",
            new_status="doing",
            title="noop",
            agent="Desktop_Agent1",
            updated_at="2026-09-16T12:02:01+00:00",
        )
        is None
    )

    after = events.query_events(channels=[CHANNEL_SUPERVISOR_WAKE])
    assert {r["ulid"] for r in after} == before_ids


def test_gate_disables_emission(monkeypatch):
    monkeypatch.setenv("UA_EVENT_SUPERVISION", "0")
    assert (
        maybe_emit_supervisor_wake(
            task_id=1,
            old_status="doing",
            new_status="done",
            title="gated",
            updated_at="2026-09-16T12:03:00+00:00",
        )
        is None
    )


def test_drain_replays_from_cursor(monkeypatch):
    monkeypatch.delenv("UA_EVENT_SUPERVISION", raising=False)
    first = maybe_emit_supervisor_wake(
        task_id=10,
        old_status="doing",
        new_status="done",
        title="a",
        updated_at="2026-09-16T12:10:00+00:00",
    )
    second = maybe_emit_supervisor_wake(
        task_id=11,
        old_status="doing",
        new_status="blocked",
        title="b",
        updated_at="2026-09-16T12:11:00+00:00",
    )
    assert first and second

    all_wakes = drain_supervisor_wakes(limit=50)
    assert all_wakes["count"] >= 2
    assert all_wakes["channel"] == CHANNEL_SUPERVISOR_WAKE

    after_first = drain_supervisor_wakes(since=first["event_ulid"], limit=50)
    ulids = {w["ulid"] for w in after_first["wakes"]}
    assert first["event_ulid"] not in ulids
    assert second["event_ulid"] in ulids


def test_dedupe_same_transition_stamp(monkeypatch):
    monkeypatch.delenv("UA_EVENT_SUPERVISION", raising=False)
    stamp = "2026-09-16T12:20:00+00:00"
    a = maybe_emit_supervisor_wake(
        task_id=77,
        old_status="doing",
        new_status="done",
        title="once",
        updated_at=stamp,
    )
    b = maybe_emit_supervisor_wake(
        task_id=77,
        old_status="doing",
        new_status="done",
        title="once",
        updated_at=stamp,
    )
    assert a is not None and b is not None
    assert a["event_ulid"] == b["event_ulid"]


def test_recent_supervisor_wakes_orders_chronologically(monkeypatch):
    monkeypatch.delenv("UA_EVENT_SUPERVISION", raising=False)
    maybe_emit_supervisor_wake(
        task_id=201,
        old_status="doing",
        new_status="done",
        title="earlier",
        updated_at="2026-09-16T12:30:00+00:00",
    )
    maybe_emit_supervisor_wake(
        task_id=202,
        old_status="doing",
        new_status="blocked",
        title="later",
        updated_at="2026-09-16T12:31:00+00:00",
    )
    recent = recent_supervisor_wakes(limit=10)
    assert recent["count"] >= 2
    ulids = [w["ulid"] for w in recent["wakes"]]
    assert ulids == sorted(ulids)


def test_channel_constant_matches_events_module():
    assert CHANNEL_SUPERVISOR_WAKE == events.CHANNEL_SUPERVISOR_WAKE
    assert CHANNEL_SUPERVISOR_WAKE == "agent.supervisor.wake"


def test_task_update_blocked_emits_wake_and_doing_does_not(monkeypatch):
    """Mutation-site hook: blocked wakes; open→doing stays silent."""
    from unified_agent.tools import tasks as tasks_tool

    monkeypatch.delenv("UA_EVENT_SUPERVISION", raising=False)
    monkeypatch.setenv("UA_AGENT_IDENTITY", "Desktop_Agent4")

    before = {r["ulid"] for r in events.query_events(channels=[CHANNEL_SUPERVISOR_WAKE])}
    created = tasks_tool.task_add(title="supervise me", notes="n", priority="med")
    tid = created["id"]

    tasks_tool.task_update(tid, status="doing")
    mid = {r["ulid"] for r in events.query_events(channels=[CHANNEL_SUPERVISOR_WAKE])}
    assert mid == before

    tasks_tool.task_update(tid, status="blocked")
    after = events.query_events(channels=[CHANNEL_SUPERVISOR_WAKE])
    new_rows = [r for r in after if r["ulid"] not in before]
    assert len(new_rows) == 1
    assert new_rows[0]["payload"]["task_id"] == tid
    assert new_rows[0]["payload"]["reason"] == "block"
    assert new_rows[0]["payload"]["new_status"] == "blocked"
