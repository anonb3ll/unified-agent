"""One closed list of what waits on the operator (task #2411)."""

from __future__ import annotations

import pytest

from unified_agent import config, needs_operator, registry, storage
from unified_agent.messaging import wake
from unified_agent.messaging.send import send_message
from unified_agent.tools.tasks import task_add

AGENT = "Desktop_Agent5"


def _rows(kind: str) -> list[dict]:
    return [row for row in needs_operator.payload()["rows"] if row["kind"] == kind]


def _note(notes: str, **kwargs) -> int:
    return task_add("held", notes=notes, **kwargs)["id"]


def _finish(task_id: int) -> None:
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()


def test_a_pending_approval_is_listed_with_where_to_act():
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO approvals (tool, status, created_at, updated_at) "
            "VALUES ('delegate', 'pending', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:00:00+00:00')"
        )
        approval = cur.lastrowid
        conn.execute(
            "INSERT INTO approvals (tool, status, created_at, updated_at) "
            "VALUES ('delegate', 'denied', '2026-11-09T00:00:00+00:00', "
            "'2026-11-09T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    rows = _rows("approval")
    assert [r["subject"] for r in rows] == [str(approval)]
    assert rows[0]["act"] == {"pane": "approvals", "params": {"approval": str(approval)}}


def test_an_unfinished_operator_only_task_is_listed():
    open_task = task_add("for the operator", lane="operator-only")["id"]
    done = task_add("finished", lane="operator-only")["id"]
    task_add("not for the operator")
    _finish(done)
    rows = _rows("operator_task")
    assert [r["subject"] for r in rows] == [str(open_task)]
    assert rows[0]["act"] == {"pane": "tasks", "params": {"task": str(open_task)}}


def test_a_red_tier_hold_is_listed_once_and_not_also_as_a_held_pr():
    both = _note("[review: red-tier @ 2026-11-09] [waiting-for: human-merge]")
    done = _note("[review: red-tier @ 2026-11-09]")
    _finish(done)
    assert [r["subject"] for r in _rows("red_tier_hold")] == [str(both)]
    assert _rows("held_pr") == []


@pytest.mark.parametrize(
    "marker", ["[waiting-for: human-merge]", "[review: held-no-checkout @ 2026-11-09]"]
)
def test_a_held_pr_is_listed(marker):
    task_id = _note(marker)
    assert [r["subject"] for r in _rows("held_pr")] == [str(task_id)]


def test_an_unanswered_request_to_the_operator_is_listed_without_its_body():
    sent = send_message(
        act="request",
        to_agent=config.operator_label(),
        body="secret-looking detail",
        from_agent=AGENT,
        expects_reply=True,
    )
    send_message(act="inform", to_agent=config.operator_label(), body="fyi", from_agent=AGENT)
    rows = _rows("operator_reply")
    assert [r["subject"] for r in rows] == [sent["message_id"]]
    assert "secret-looking" not in str(rows[0])
    assert AGENT in rows[0]["title"]
    assert rows[0]["act"]["pane"] == "messages"


def test_a_session_waiting_for_a_wake_grant_is_listed_until_granted():
    sid, _ = registry.register_session(AGENT, "claude_code", external_session_id="w", host="n")
    wake.set_opt_in(sid, True)
    assert [r["subject"] for r in _rows("wake_grant")] == [sid]
    wake.set_grant(sid, True, actor=config.operator_label())
    assert _rows("wake_grant") == []


def test_every_declared_kind_is_counted_even_at_zero():
    report = needs_operator.payload()
    assert set(report["counts"]) == set(needs_operator.DECLARED)
    assert report["errors"] == [] and report["total"] == 0


def test_a_producer_added_without_declaring_it_breaks_composition(monkeypatch):
    monkeypatch.setitem(needs_operator._PRODUCERS, "new_source", lambda conn: [])
    with pytest.raises(needs_operator.CatalogError, match="undeclared .*new_source"):
        needs_operator.payload()


def test_a_declared_kind_without_a_producer_breaks_composition(monkeypatch):
    monkeypatch.setattr(needs_operator, "DECLARED", (*needs_operator.DECLARED, "lost_source"))
    with pytest.raises(needs_operator.CatalogError, match="missing .*lost_source"):
        needs_operator.payload()


def test_an_unreadable_source_is_reported_not_silently_empty(monkeypatch):
    import sqlite3

    def broken(conn):
        raise sqlite3.OperationalError("no such table")

    monkeypatch.setitem(needs_operator._PRODUCERS, "wake_grant", broken)
    report = needs_operator.payload()
    assert any(error.startswith("wake_grant:") for error in report["errors"])
