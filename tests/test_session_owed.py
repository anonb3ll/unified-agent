"""What an ended session still owes (task #2389). Report mode, real SQLite."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import registry, session_owed, storage
from unified_agent.messaging.send import send_message
from unified_agent.tools import registry as registry_tools
from unified_agent.tools.tasks import task_add, task_claim, task_update

ME = "Desktop_Agent1"
PEER = "Desktop_Agent5"


def _open(ext: str = "ext-1") -> str:
    sid, _ = registry.register_session(ME, "claude_code", external_session_id=ext, host="n")
    return sid


def _ask(*, to_session: str | None = None, expects_reply: bool = True) -> str:
    return send_message(
        act="request",
        to_agent=None if to_session else ME,
        to_session=to_session,
        body="please look",
        from_agent=PEER,
        expects_reply=expects_reply,
    )["message_id"]


def _read_all() -> None:
    conn = storage.connect()
    try:
        conn.execute("UPDATE message_index SET read_at='2099-01-01T00:00:00+00:00'")
        conn.commit()
    finally:
        conn.close()


def _end(sid: str) -> dict:
    return registry_tools.session_end(sid)["owed"]


def test_a_reply_the_session_read_and_never_sent_is_owed_without_its_body():
    sid = _open()
    message_id = _ask()
    _read_all()
    owed = _end(sid)
    assert [m["message_id"] for m in owed["unanswered"]] == [message_id]
    assert owed["count"] == 1 and owed["report_only"] is True
    assert "please look" not in str(owed)


def test_mail_addressed_to_the_session_is_owed_even_unread():
    sid = _open()
    message_id = _ask(to_session=sid)
    assert [m["message_id"] for m in _end(sid)["unanswered"]] == [message_id]


def test_unread_label_mail_is_not_this_sessions_debt():
    sid = _open()
    _ask()
    assert _end(sid)["unanswered"] == []


def test_mail_from_before_the_session_is_not_owed():
    message_id = _ask()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE message_index SET created_at='2000-01-01T00:00:00+00:00', "
            "read_at='2000-01-01T00:00:00+00:00' WHERE message_id=?",
            (message_id,),
        )
        conn.commit()
    finally:
        conn.close()
    assert _end(_open())["unanswered"] == []


def test_a_message_with_no_reply_expected_or_already_answered_is_not_owed():
    sid = _open()
    _ask(expects_reply=False)
    answered = _ask()
    send_message(act="inform", to_agent=PEER, body="done", from_agent=ME, reply_to=answered)
    _read_all()
    assert _end(sid)["unanswered"] == []


def test_a_next_move_stored_against_the_agent_is_owed_unless_it_holds_the_task():
    sid = _open()
    stored = task_add("handed to me", lane=ME)["id"]
    task_update(stored, next_owner=ME, actor=ME)
    held = task_add("held")["id"]
    task_claim(held, agent=ME, timeout_seconds=3600)
    assert [t["id"] for t in _end(sid)["next_moves"]] == [stored]


def test_a_claim_with_nothing_else_moving_it_is_owed():
    sid = _open()
    task_id = task_add("mine")["id"]
    task_claim(task_id, agent=ME, timeout_seconds=3600)
    assert [t["id"] for t in _end(sid)["claims"]] == [task_id]


def test_a_claim_with_a_declared_wait_is_not_owed():
    sid = _open()
    task_id = task_add("waiting")["id"]
    task_claim(task_id, agent=ME, timeout_seconds=3600)
    until = (datetime.now(UTC) + timedelta(hours=2)).isoformat(timespec="seconds")
    task_update(task_id, declared_wait={"reason": "ci", "until": until}, actor=ME)
    assert _end(sid)["claims"] == []


def test_the_report_counts_the_labels_other_open_sessions():
    first = _open("ext-1")
    _open("ext-2")
    assert _end(first)["other_open_sessions"] == 1


def test_a_broken_report_never_stops_the_close(monkeypatch):
    sid = _open()

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(session_owed, "owed_by", broken)
    result = registry_tools.session_end(sid)
    assert result["closed"] is True and "owed" not in result


def _abandon(sid: str, ended_at: datetime) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE agent_sessions SET status='abandoned', ended_at=? WHERE id=?",
            (ended_at.isoformat(timespec="seconds"), sid),
        )
        conn.commit()
    finally:
        conn.close()


def test_the_sweep_reports_a_reaped_session_once_and_skips_old_ones():
    now = datetime.now(UTC)
    recent, old = _open("ext-1"), _open("ext-2")
    _abandon(recent, now - timedelta(minutes=10))
    _abandon(old, now - session_owed.SWEEP_WINDOW - timedelta(minutes=1))
    assert session_owed.record_recently_ended(now) == 1
    assert session_owed.record_recently_ended(now) == 0
    assert session_owed.summary(now)["sessions"] == 1


def test_a_closed_session_is_not_reported_again_by_the_sweep():
    sid = _open()
    _end(sid)
    assert session_owed.record_recently_ended() == 0


@pytest.mark.parametrize("others", [0, 1])
def test_summary_separates_shared_labels_for_false_positive_measurement(others):
    sid = _open("ext-1")
    if others:
        _open("ext-2")
    task_id = task_add("mine")["id"]
    task_claim(task_id, agent=ME, timeout_seconds=3600)
    _end(sid)
    report = session_owed.summary()
    assert (report["with_owed"], report["shared_label"]) == (1, others)


def _turn_events() -> int:
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE channel=?", (session_owed.TURN_CHANNEL,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_a_turn_end_records_each_distinct_owed_set_once():
    sid = _open()
    assert session_owed.check_turn(ME, sid)["count"] == 0
    assert _turn_events() == 0
    task_id = task_add("mine")["id"]
    task_claim(task_id, agent=ME, timeout_seconds=3600)
    for _ in range(3):
        assert [t["id"] for t in session_owed.check_turn(ME, sid)["claims"]] == [task_id]
    assert _turn_events() == 1
    other = task_add("mine too")["id"]
    task_claim(other, agent=ME, timeout_seconds=3600)
    session_owed.check_turn(ME, sid)
    assert _turn_events() == 2
    assert session_owed.summary()["turn_sets"] == 2


def test_a_turn_end_check_refuses_another_agents_session():
    assert session_owed.check_turn(PEER, _open()) is None


def test_the_summary_counts_this_tenant_only(monkeypatch):
    _end(_open("ext-mine"))
    assert session_owed.summary()["sessions"] == 1
    monkeypatch.setattr(session_owed.edition, "tenant_org_id", lambda: "other-org")
    assert session_owed.summary()["sessions"] == 0
