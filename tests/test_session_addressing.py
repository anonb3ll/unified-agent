"""Address a specific session of an agent (task #1860)."""

import inspect
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, crew, registry, storage
from unified_agent.messaging import index as message_index
from unified_agent.tools import graph as graph_tool
from unified_agent.tools import messages as message_tools
from unified_agent.tools import registry as registry_tools
from unified_agent.tools import tasks as task_tools

CLAUDE = "Desktop_Agent1"
CODEX = "Desktop_Agent2"


@pytest.fixture
def as_agent():
    tokens = []

    def _set(label):
        tokens.append(config.agent_identity_var.set(label))

    yield _set
    for token in reversed(tokens):
        config.agent_identity_var.reset(token)


def _session(agent=CODEX, external="ext-1", **kwargs):
    return registry_tools.session_start(
        kind="claude_code", agent=agent, external_session_id=external, **kwargs
    )["session_id"]


def _send(to_agent=CODEX, to_session=None, frm=CLAUDE, **kwargs):
    return message_tools.message_send(
        act=kwargs.pop("act", "inform"),
        body=kwargs.pop("body", "hello"),
        to_agent=to_agent,
        to_session=to_session,
        from_agent=frm,
        **kwargs,
    )


def _inbox(session_id=None, **params):
    return graph_tool.coordination_manage("message_inbox", {"session_id": session_id, **params})


def _ids(listed):
    return [row["message_id"] for row in listed["messages"]]


def _age(session_id, minutes):
    stamp = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE agent_sessions SET last_heartbeat_at=? WHERE id=?", (stamp, session_id)
        )
        conn.commit()
    finally:
        conn.close()


# --- registration ---------------------------------------------------------------


def test_session_start_is_idempotent_on_agent_and_external_id():
    first = registry_tools.session_start(kind="claude_code", agent=CODEX, external_session_id="x")
    assert first["created"] is True
    _age(first["session_id"], 10)
    aged = registry.get_session(first["session_id"])["last_seen_at"]
    again = registry_tools.session_start(
        kind="claude_code", agent=CODEX, external_session_id="x", branch="task/1", host="desk-1"
    )
    assert again["session_id"] == first["session_id"] and again["created"] is False
    assert again["last_seen_at"] > aged  # the second call is a heartbeat
    row = registry.get_session(first["session_id"])
    assert row["branch"] == "task/1" and row["host"] == "desk-1"
    # The same external id under another label, or another external id, is a new session.
    other_label = registry_tools.session_start(
        kind="claude_code", agent=CLAUDE, external_session_id="x"
    )
    other_ext = registry_tools.session_start(
        kind="claude_code", agent=CODEX, external_session_id="y"
    )
    assert len({first["session_id"], other_label["session_id"], other_ext["session_id"]}) == 3
    # No external id: every call is its own session, as before.
    a = registry_tools.session_start(kind="k", agent=CODEX)
    b = registry_tools.session_start(kind="k", agent=CODEX)
    assert a["session_id"] != b["session_id"]
    rows = registry.list_sessions(agent=CODEX, limit=50)
    assert len([r for r in rows if r["external_session_id"] == "x"]) == 1


def test_reregistering_revives_a_reaped_or_ended_session():
    sid = _session(external="r")
    registry.end_session(sid, status="abandoned")
    again = registry_tools.session_start(kind="claude_code", agent=CODEX, external_session_id="r")
    assert again["session_id"] == sid
    row = registry.get_session(sid)
    assert row["status"] == "running" and row["ended_at"] is None


def test_verified_caller_registers_only_under_its_own_label(as_agent):
    as_agent(CLAUDE)
    with pytest.raises(ToolError, match="only under its own label"):
        registry_tools.session_start(kind="claude_code", agent=CODEX, external_session_id="z")
    mine = registry_tools.session_start(kind="claude_code", external_session_id="z")
    assert mine["agent"] == CLAUDE
    same = registry_tools.session_start(kind="claude_code", agent=CLAUDE, external_session_id="z")
    assert same["session_id"] == mine["session_id"]


# --- liveness -------------------------------------------------------------------


def test_stale_session_is_not_live_and_a_mailbox_read_refreshes_it(monkeypatch, as_agent):
    monkeypatch.setenv("UA_SESSION_LIVE_MINUTES", "5")
    fresh, stale = _session(external="f"), _session(external="s")
    _age(stale, 10)  # open, but not seen for longer than the live window
    live = {s["session_id"] for s in registry.list_live_sessions()}
    assert live == {fresh}
    listing = registry_tools.session_list(agent=CODEX)
    flags = {s["id"]: s["live"] for s in listing["sessions"]}
    assert flags == {fresh: True, stale: False} and listing["live"] == 1
    assert [s["id"] for s in registry_tools.session_list(live_only=True)["sessions"]] == [fresh]

    as_agent(CODEX)
    message_tools.message_list(session_id=stale)  # a read-only call counts as a heartbeat
    assert {s["session_id"] for s in registry.list_live_sessions()} == {fresh, stale}


def test_agent_context_lists_live_sessions_with_host_and_task(monkeypatch):
    task = task_tools.task_add("session target")
    with_task = registry_tools.session_start(
        kind="claude_code", agent=CODEX, external_session_id="t", task_id=task["id"], host="desk-1"
    )["session_id"]
    plain = registry_tools.session_start(
        kind="claude_code", agent=CODEX, external_session_id="p", host="desk-2"
    )["session_id"]
    gone = _session(external="g")
    _age(gone, 120)
    registry.end_session(plain, status="completed")
    peers = {p["label"]: p for p in registry_tools.agent_context()["peers"]}
    sessions = {s["session_id"]: s for s in peers[CODEX]["live_sessions"]}
    assert set(sessions) == {with_task}
    row = sessions[with_task]
    assert row["host"] == "desk-1" and row["task_id"] == task["id"]
    assert row["task_source"] == "session" and row["last_seen_at"]
    assert "worktree_path" not in row


def test_sole_claimed_task_is_derived_when_the_session_names_none():
    task = task_tools.task_add("claimed work")
    task_tools.task_claim(task["id"], agent=CODEX)
    sid = _session(external="c")
    row = next(s for s in registry.list_live_sessions() if s["session_id"] == sid)
    assert row["task_id"] == task["id"] and row["task_source"] == "claim"
    second = task_tools.task_add("more claimed work")
    task_tools.task_claim(second["id"], agent=CODEX)
    row = next(s for s in registry.list_live_sessions() if s["session_id"] == sid)
    assert row["task_id"] is None and row["task_source"] is None  # ambiguous


# --- addressing -----------------------------------------------------------------


def test_targeted_message_is_seen_only_by_the_target_session(as_agent):
    a, b = _session(external="a"), _session(external="b")
    sent = _send(to_agent=None, to_session=a, body="just for a")
    assert sent["to_agent"] == CODEX and sent["to_session"] == a and sent["target_live"] is True
    mid = sent["message_id"]

    as_agent(CODEX)
    assert _ids(message_tools.message_list(session_id=a)) == [mid]
    assert message_tools.message_list(session_id=a)["messages"][0]["to_session"] == a
    assert message_tools.message_list(session_id=b)["messages"] == []
    assert message_tools.message_list()["messages"] == []  # label view: label mail only
    assert message_tools.message_list(session_id=b, count_only=True)["count"] == 0
    assert message_tools.message_list(session_id=a, unread_only=True)["unread"] == 1
    assert message_tools.message_list(session_id=b, unread_only=True)["unread"] == 0
    # Reading as b neither marks it nor can ack it.
    assert _inbox(b)["messages"] == []
    with pytest.raises(ToolError, match="no message"):
        graph_tool.coordination_manage("message_ack", {"message_id": mid, "session_id": b})
    with pytest.raises(ToolError, match="no message"):
        graph_tool.coordination_manage("message_ack", {"message_id": mid})
    read = _inbox(a)
    assert _ids(read) == [mid] and read["marked_read"] == 1 and read["unread"] == 0
    assert read["messages"][0]["status"] == "read"
    assert _inbox(a)["messages"] == []


def test_the_sender_still_sees_what_it_sent_to_a_session(as_agent):
    a = _session(external="a")
    mid = _send(to_agent=None, to_session=a)["message_id"]
    as_agent(CLAUDE)
    assert _ids(message_tools.message_list(from_agent=CLAUDE)) == [mid]
    assert _ids(message_tools.message_list()) == [mid]


def test_label_mail_read_by_one_session_stays_unread_for_the_other(as_agent):
    a, b = _session(external="a"), _session(external="b")
    label = _send(body="for any codex")["message_id"]
    only_a = _send(to_agent=None, to_session=a, body="for a")["message_id"]

    as_agent(CODEX)
    assert _ids(_inbox(a)) == [label, only_a]
    assert message_tools.message_list(session_id=a, unread_only=True)["messages"] == []
    # b still has the label message unread, and never sees a's own mail.
    assert _ids(message_tools.message_list(session_id=b, unread_only=True)) == [label]
    assert message_index.unread_count(CODEX, session_id=b) == 1
    assert message_index.unread_count(CODEX, session_id=a) == 0
    # So does the label itself: no session-less caller has read it.
    assert _ids(message_tools.message_list(unread_only=True)) == [label]
    row = message_tools.message_list(session_id=b)["messages"][0]
    assert row["status"] == "delivered" and row["read_at"] is None
    seen_by_a = message_tools.message_list(session_id=a, message_id=label)["messages"][0]
    assert seen_by_a["status"] == "read" and seen_by_a["read_at"]

    assert _ids(_inbox(b)) == [label]
    assert _inbox(b)["messages"] == []
    receipts = _receipts()
    assert {(r["message_id"], r["session_id"]) for r in receipts} == {(label, a), (label, b)}


def test_label_level_read_counts_as_read_for_every_session(as_agent):
    a = _session(external="a")
    label = _send()["message_id"]
    as_agent(CODEX)
    assert _ids(_inbox()) == [label]  # no session_id: the label reads it
    assert message_tools.message_list(session_id=a, unread_only=True)["messages"] == []
    assert message_index.unread_count(CODEX, session_id=a) == 0


def test_ack_by_a_session_leaves_label_mail_unacked_for_the_others(as_agent):
    a, b = _session(external="a"), _session(external="b")
    first = _send(body="1")["message_id"]
    second = _send(body="2")["message_id"]
    targeted = _send(to_agent=None, to_session=a, body="3")["message_id"]
    as_agent(CODEX)
    one = graph_tool.coordination_manage("message_ack", {"message_id": first, "session_id": a})
    assert one["acked"] == 1 and one["unread"] == 2 and one["session_id"] == a
    again = graph_tool.coordination_manage("message_ack", {"message_id": first, "session_id": a})
    assert again["acked"] == 0
    swept = graph_tool.coordination_manage("message_ack", {"through": targeted, "session_id": a})
    assert swept["acked"] == 2 and swept["unread"] == 0  # second (receipt) and the targeted one
    by_b = message_tools.message_list(session_id=b)["messages"]
    assert [(m["message_id"], m["status"], m["acked_at"]) for m in by_b] == [
        (first, "delivered", None),
        (second, "delivered", None),
    ]
    by_a = {m["message_id"]: m for m in message_tools.message_list(session_id=a)["messages"]}
    assert all(by_a[m]["acked_at"] and by_a[m]["read_at"] for m in (first, second, targeted))
    # The label's own mailbox is untouched until it acks without a session.
    assert message_index.unread_count(CODEX) == 2
    swept_label = graph_tool.coordination_manage("message_ack", {"through": second})
    assert swept_label["acked"] == 2 and swept_label["unread"] == 0
    assert message_index.unread_count(CODEX, session_id=b) == 0


def test_a_session_must_belong_to_the_caller(as_agent):
    mine = _session(agent=CODEX, external="m")
    theirs = _session(agent=CLAUDE, external="t")
    as_agent(CODEX)
    for call in (
        lambda: message_tools.message_list(session_id=theirs),
        lambda: _inbox(theirs),
        lambda: graph_tool.coordination_manage(
            "message_ack", {"through": "x", "session_id": theirs}
        ),
        lambda: message_tools.message_list(session_id="no-such-session"),
    ):
        with pytest.raises(ToolError, match="is not a session of"):
            call()
    assert message_tools.message_list(session_id=mine)["messages"] == []


def test_unverified_caller_names_the_mailbox_and_the_session():
    a = _session(external="a")
    mid = _send(to_agent=None, to_session=a)["message_id"]
    assert _ids(message_tools.message_list(to_agent=CODEX, session_id=a)) == [mid]
    assert message_tools.message_list(to_agent=CODEX)["messages"] == []
    with pytest.raises(ToolError, match="to_agent is required"):
        message_tools.message_list(session_id=a)
    with pytest.raises(ToolError, match="is not a session of"):
        message_tools.message_list(to_agent=CLAUDE, session_id=a)


def test_to_session_validation():
    live = _session(external="a")
    ended = _session(external="e")
    registry.end_session(ended, status="completed")
    with pytest.raises(ToolError, match="unknown session"):
        _send(to_agent=None, to_session="nope")
    with pytest.raises(ToolError, match="has ended"):
        _send(to_agent=None, to_session=ended)
    with pytest.raises(ToolError, match="does not match"):
        _send(to_agent=CLAUDE, to_session=live)
    with pytest.raises(ToolError, match="is required"):
        _send(to_agent=None)
    assert _send(to_agent=CODEX, to_session=live)["to_session"] == live
    _age(live, 10)
    stale = _send(to_agent=None, to_session=live)
    assert stale["accepted"] and stale["to_agent"] == CODEX


def test_session_addressing_survives_an_index_rebuild_and_the_column_migration():
    a, b = _session(external="a"), _session(agent=CLAUDE, external="b")
    mid = _send(to_agent=None, to_session=a, from_session=b)["message_id"]
    conn = storage.connect()
    try:
        conn.execute("DROP INDEX idx_message_index_target")
        conn.execute("ALTER TABLE message_index DROP COLUMN target_session")
        conn.execute("ALTER TABLE message_index DROP COLUMN sender_session")
        conn.commit()
        message_index.ensure_schema(conn)  # a #1859-era table gains both columns
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM message_index").fetchone()[0] == 1
        conn.execute("DROP TABLE message_index")
        conn.execute("DELETE FROM meta WHERE key='message_index_backfill'")
        conn.commit()
        message_index.ensure_schema(conn)  # a rebuild replays both from the event
        conn.commit()
        row = conn.execute(
            "SELECT target_session, sender_session FROM message_index WHERE message_id=?", (mid,)
        ).fetchone()
        assert (row["target_session"], row["sender_session"]) == (a, b)
    finally:
        conn.close()


def _receipts():
    conn = storage.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM message_reads").fetchall()]
    finally:
        conn.close()


# --- crew -----------------------------------------------------------------------


def test_crew_intervene_reaches_only_the_session(as_agent):
    a, b = _session(agent=CLAUDE, external="a"), _session(agent=CLAUDE, external="b")
    result = crew.intervene_on_session(session_id=a, body="pause", from_agent="operator")
    assert result["to_session"] == a and result["message"]["to_session"] == a

    as_agent(CLAUDE)
    assert [m["body"] for m in message_tools.message_list(session_id=a)["messages"]] == ["pause"]
    assert message_tools.message_list(session_id=b)["messages"] == []
    assert message_tools.message_list()["messages"] == []
    view = crew.crew_live_view(a)  # the crew view reads the index directly
    assert [m["body"] for m in view["messages"]] == ["pause"]


def test_crew_intervene_refuses_an_ended_session():
    sid = _session(agent=CLAUDE, external="done")
    registry.end_session(sid, status="completed")
    with pytest.raises(ToolError, match="has ended"):
        crew.intervene_on_session(session_id=sid, body="too late", from_agent="operator")


# --- from_session and session-to-session replies ----------------------------------


def test_from_session_is_recorded_and_must_be_the_senders_own(as_agent):
    mine = _session(agent=CLAUDE, external="m")
    theirs = _session(agent=CODEX, external="t")
    sent = _send(to_agent=CODEX, from_session=mine)
    assert sent["from_session"] == mine
    row = message_tools.message_list(message_id=sent["message_id"])["messages"][0]
    assert row["from_session"] == mine and row["to_session"] is None
    with pytest.raises(ToolError, match="is not a session of"):
        _send(to_agent=CODEX, from_session=theirs)
    with pytest.raises(ToolError, match="is not a session of"):
        _send(to_agent=CODEX, from_session="no-such-session")
    as_agent(CLAUDE)  # a verified sender is bound to its own label the same way
    with pytest.raises(ToolError, match="is not a session of"):
        _send(to_agent=CODEX, frm=None, from_session=theirs)
    assert _send(to_agent=CODEX, frm=None, from_session=mine)["from_session"] == mine


def test_from_session_counts_as_a_heartbeat(monkeypatch):
    monkeypatch.setenv("UA_SESSION_LIVE_MINUTES", "5")
    mine = _session(agent=CLAUDE, external="m")
    _age(mine, 10)
    assert registry.list_live_sessions(agent=CLAUDE) == []
    _send(to_agent=CODEX, from_session=mine)
    assert [s["session_id"] for s in registry.list_live_sessions(agent=CLAUDE)] == [mine]


def test_retrying_a_committed_send_still_heartbeats_and_checks_from_session(monkeypatch):
    monkeypatch.setenv("UA_SESSION_LIVE_MINUTES", "5")
    mine = _session(agent=CLAUDE, external="m")
    theirs = _session(agent=CODEX, external="t")
    first = _send(to_agent=None, to_session=theirs, from_session=mine, message_id="hb-retry")
    registry.end_session(theirs, status="completed")
    _age(mine, 10)
    assert registry.list_live_sessions(agent=CLAUDE) == []
    # The retry returns the saved result even though the target ended, and it counts
    # as the sender session's heartbeat.
    again = _send(to_agent=None, to_session=theirs, from_session=mine, message_id="hb-retry")
    assert again["duplicate"] is True and again["event_ulid"] == first["event_ulid"]
    assert [s["session_id"] for s in registry.list_live_sessions(agent=CLAUDE)] == [mine]
    # A from_session that is not the sender's fails on a retry as it does on a first send.
    with pytest.raises(ToolError, match="is not a session of"):
        _send(to_agent=None, to_session=theirs, from_session=theirs, message_id="hb-retry")
    with pytest.raises(ToolError, match="is not a session of"):
        _send(to_agent=None, to_session=theirs, from_session="no-such", message_id="hb-retry")


def test_session_to_session_conversation(as_agent):
    a1, a2 = _session(agent=CLAUDE, external="a1"), _session(agent=CLAUDE, external="a2")
    b1, b2 = _session(agent=CODEX, external="b1"), _session(agent=CODEX, external="b2")
    ask = _send(to_agent=None, to_session=b2, from_session=a1, act="query", expects_reply=True)

    # b2 answers with reply_to alone: it goes back to a1, not to the label or a2.
    as_agent(CODEX)
    reply = message_tools.message_send(
        act="inform",
        body="done",
        to_agent=CLAUDE,
        reply_to=ask["message_id"],
        from_session=b2,
    )
    assert reply["to_session"] == a1 and reply["from_session"] == b2
    # to_agent can be omitted: the parent says who to answer.
    quiet = message_tools.message_send(
        act="inform", body="and more", reply_to=ask["message_id"], from_session=b2
    )
    assert quiet["to_session"] == a1 and quiet["to_agent"] == CLAUDE

    as_agent(CLAUDE)
    got = message_tools.message_list(session_id=a1, unread_only=True)["messages"]
    assert [m["message_id"] for m in got] == [reply["message_id"], quiet["message_id"]]
    assert message_tools.message_list(session_id=a2, unread_only=True)["messages"] == []
    assert message_tools.message_list(unread_only=True)["messages"] == []
    # The question counts as answered (recipient label back to the sender label).
    waiting = message_tools.message_list(from_agent=CLAUDE, waiting_on_reply=True)
    assert waiting["messages"] == []
    # And the conversation continues: a1 replies to the reply, which targets b2.
    follow = message_tools.message_send(
        act="inform", body="thanks", reply_to=reply["message_id"], from_session=a1
    )
    assert follow["to_session"] == b2 and follow["to_agent"] == CODEX
    as_agent(CODEX)
    inbox = message_tools.message_list(session_id=b2, unread_only=True)
    assert [m["message_id"] for m in inbox["messages"]] == [ask["message_id"], follow["message_id"]]
    assert message_tools.message_list(session_id=b1, unread_only=True)["messages"] == []


def test_reply_default_can_be_overridden_and_falls_back_to_the_label(as_agent):
    a1, a2 = _session(agent=CLAUDE, external="a1"), _session(agent=CLAUDE, external="a2")
    b = _session(agent=CODEX, external="b")
    ask = _send(to_agent=CODEX, from_session=a1, act="query")
    # An explicit to_session wins over the default.
    other = _send(
        to_agent=None, to_session=a2, frm=CODEX, reply_to=ask["message_id"], from_session=b
    )
    assert other["to_session"] == a2
    # No from_session on the parent: a reply is plain label mail.
    plain = _send(to_agent=CODEX)
    assert _send(to_agent=CLAUDE, frm=CODEX, reply_to=plain["message_id"])["to_session"] is None
    # The parent's session is open but stale (the asker restarted): label mail,
    # so the reply reaches the agent's live session instead of dead-lettering.
    _age(a1, 60)  # past the default 30-minute live window
    stale = _send(to_agent=CLAUDE, frm=CODEX, reply_to=ask["message_id"])
    assert stale["accepted"] and stale["to_session"] is None
    # The parent's session has ended: fall back to label mail rather than refuse.
    registry.end_session(a1, status="completed")
    late = _send(to_agent=CLAUDE, frm=CODEX, reply_to=ask["message_id"])
    assert late["accepted"] and late["to_session"] is None
    # A reply addressed to someone else is not defaulted.
    third = _send(to_agent="Desktop_Agent5", frm=CODEX, reply_to=ask["message_id"])
    assert third["to_session"] is None
    # Only the parent's recipient inherits the default (the sender following up does not).
    followup = _send(to_agent=CODEX, frm=CLAUDE, reply_to=ask["message_id"])
    assert followup["to_session"] is None


def test_the_answered_and_waiting_rules_hold_for_session_mail(as_agent):
    a1 = _session(agent=CLAUDE, external="a1")
    b1, b2 = _session(agent=CODEX, external="b1"), _session(agent=CODEX, external="b2")
    ask = _send(to_agent=None, to_session=b1, from_session=a1, act="query", expects_reply=True)
    # A reply from the right label but to a third agent does not answer it.
    _send(to_agent="Desktop_Agent5", frm=CODEX, reply_to=ask["message_id"], from_session=b1)
    as_agent(CLAUDE)
    waiting = message_tools.message_list(from_agent=CLAUDE, waiting_on_reply=True, session_id=a1)
    assert [m["message_id"] for m in waiting["messages"]] == [ask["message_id"]]
    # Any session of the recipient label answering back counts.
    as_agent(CODEX)
    message_tools.message_send(
        act="inform", body="ok", to_agent=CLAUDE, reply_to=ask["message_id"], from_session=b2
    )
    as_agent(CLAUDE)
    assert message_tools.message_list(from_agent=CLAUDE, waiting_on_reply=True)["messages"] == []
    row = message_tools.message_list(message_id=ask["message_id"])["messages"][0]
    assert row["status"] == "answered"


def test_retrying_a_committed_send_survives_the_target_session_ending():
    a = _session(external="a")
    first = _send(to_agent=None, to_session=a, message_id="retry-1")
    registry.end_session(a, status="completed")
    again = _send(to_agent=None, to_session=a, message_id="retry-1")
    assert again["duplicate"] is True and again["event_ulid"] == first["event_ulid"]
    assert again["to_session"] == a
    # Only the sender's own saved result is replayed; a new id still hits the check.
    with pytest.raises(ToolError, match="has ended"):
        _send(to_agent=None, to_session=a, message_id="retry-2")
    with pytest.raises(ToolError, match="has ended"):
        _send(to_agent=None, to_session=a, frm=CODEX, message_id="retry-1")


def test_live_filter_applies_before_the_limit(monkeypatch):
    monkeypatch.setenv("UA_SESSION_LIVE_MINUTES", "5")
    live = _session(external="live")
    stale = [_session(external=f"stale-{i}") for i in range(3)]
    conn = storage.connect()
    try:  # the stale sessions started later than the live one
        for n, sid in enumerate(stale):
            conn.execute(
                "UPDATE agent_sessions SET started_at=? WHERE id=?",
                (f"2999-01-01T00:00:0{n}+00:00", sid),
            )
        conn.commit()
    finally:
        conn.close()
    for sid in stale:
        _age(sid, 10)
    assert [s["session_id"] for s in registry.list_live_sessions(limit=1)] == [live]
    listed = registry_tools.session_list(live_only=True, limit=1)
    assert [s["id"] for s in listed["sessions"]] == [live]


def test_live_cutoff_is_one_value_for_the_sql_filter_and_the_live_flag(monkeypatch):
    monkeypatch.setenv("UA_SESSION_LIVE_MINUTES", "5")
    base = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            # 0.7 s past a whole second, as a real call almost always is
            return base + timedelta(minutes=5, microseconds=700_000)

    live = _session(external="live")
    at_cutoff = _session(external="at-cutoff")
    conn = storage.connect()
    try:  # heartbeats are stored in whole seconds; the newer session is 0.7 s too old
        conn.execute(
            "UPDATE agent_sessions SET last_heartbeat_at=?, started_at=? WHERE id=?",
            ("2030-01-01T12:00:01+00:00", "2030-01-01T00:00:00+00:00", live),
        )
        conn.execute(
            "UPDATE agent_sessions SET last_heartbeat_at=?, started_at=? WHERE id=?",
            ("2030-01-01T12:00:00+00:00", "2030-01-01T00:00:05+00:00", at_cutoff),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(registry, "datetime", _Frozen)
    flags = {s["id"]: s["live"] for s in registry.list_sessions(reap=False)}
    assert flags == {live: True, at_cutoff: False}
    listed = registry.list_sessions(live_only=True, limit=1, reap=False)
    assert [s["id"] for s in listed] == [live]
    assert all(s["live"] for s in registry.list_sessions(live_only=True, reap=False))


def test_message_send_keeps_its_positional_parameter_order():
    result = message_tools.message_send("inform", CODEX, "positional body", CLAUDE)
    assert result["to_agent"] == CODEX and result["from_agent"] == CLAUDE
    row = message_tools.message_list(message_id=result["message_id"])["messages"][0]
    assert row["body"] == "positional body"
    with pytest.raises(ToolError, match="body is required"):
        message_tools.message_send(act="inform", to_agent=CODEX, from_agent=CLAUDE)
    # The session parameters are trailing keywords, after expects_reply.
    names = list(inspect.signature(message_tools.message_send).parameters)
    assert names[:3] == ["act", "to_agent", "body"] and names[-2:] == ["to_session", "from_session"]


def test_a_heartbeat_without_host_keeps_the_sessions_explicit_host():
    registry.touch_agent(CODEX, via="heartbeat_tool", host="label-host", force=True)
    sid = registry_tools.session_start(
        kind="claude_code", agent=CODEX, external_session_id="h", host="session-host"
    )["session_id"]
    again = registry_tools.session_start(kind="claude_code", agent=CODEX, external_session_id="h")
    assert again["session_id"] == sid and again["host"] == "session-host"
    assert registry.get_session(sid)["host"] == "session-host"
    # A new session with no host still falls back to the label's last host.
    other = registry_tools.session_start(kind="claude_code", agent=CODEX, external_session_id="o")
    assert other["host"] == "label-host"


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "0", "-5", "junk", ""])
def test_a_malformed_live_window_falls_back_to_thirty_minutes(monkeypatch, raw):
    monkeypatch.setenv("UA_SESSION_LIVE_MINUTES", raw)
    assert config.session_live_minutes() == 30.0
    sid = _session(external="n")
    assert [s["session_id"] for s in registry.list_live_sessions()] == [sid]
    monkeypatch.setenv("UA_SESSION_LIVE_MINUTES", "1e300")
    assert config.session_live_minutes() == 525600.0
    assert [s["session_id"] for s in registry.list_live_sessions()] == [sid]


def test_a_row_newer_than_the_read_never_reports_a_negative_age():
    from datetime import UTC, datetime, timedelta

    from unified_agent import registry

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    later = (now + timedelta(seconds=1)).isoformat(timespec="seconds")
    row = {
        "id": "s1",
        "agent": "agent_a",
        "status": "active",
        "started_at": later,
        "last_heartbeat_at": later,
        "ended_at": None,
    }

    class _Row(dict):
        def __missing__(self, key):
            return None

    result = registry._session_result(_Row(row), now)
    assert result["duration_s"] == 0.0
    assert result["heartbeat_age_s"] == 0.0


# --- mail to a dead or dying session (task #2547) ----------------------------------


@pytest.mark.parametrize("dead", ["abandoned", "stale"])
def test_explicit_mail_to_a_dead_or_stale_session_goes_to_the_label(dead, monkeypatch):
    """The reaper ends almost every session as abandoned, never completed, so the
    ended-session refusal never fired and session mail stranded unseen."""
    gone = _session(external="gone")
    other = _session(external="other")
    if dead == "abandoned":
        registry.end_session(gone, status="abandoned")
    else:
        # Open and not yet reaped, but past the live window.
        monkeypatch.setenv("UA_SESSION_LIVE_MINUTES", "5")
        _age(gone, 10)
    sent = _send(to_agent=None, to_session=gone, body="your read is in")
    assert sent["accepted"] and sent["to_agent"] == CODEX and sent["to_session"] is None
    reason = "abandoned" if dead == "abandoned" else "not_live"
    assert sent["session_fallback"] == {
        "requested": gone,
        "reason": reason,
        "delivered_as": "label",
    }
    # Every session of the label sees it, including the one still running.
    assert sent["message_id"] in _ids(message_tools.message_list(to_agent=CODEX))
    assert sent["message_id"] in _ids(message_tools.message_list(to_agent=CODEX, session_id=other))
    # A live target is still addressed exactly, with no fallback.
    exact = _send(to_agent=None, to_session=other)
    assert exact["to_session"] == other and "session_fallback" not in exact


def _stranded_notices(label):
    from unified_agent.messaging import delivery

    return message_tools.message_list(to_agent=label, from_agent=delivery.STRANDED_NOTICE_SENDER)[
        "messages"
    ]


def test_unread_mail_left_in_an_ended_session_is_noticed_once_without_bodies():
    from unified_agent.messaging import delivery

    sid = _session(external="ending")
    mine = _session(agent=CLAUDE, external="mine")
    query = _send(to_agent=None, to_session=sid, act="query", body="SECRET-Q", task_id=2547)
    inform = _send(to_agent=None, to_session=sid, body="SECRET-I")
    read = _send(to_agent=None, to_session=sid, body="read in time")
    graph_tool.coordination_manage(
        "message_ack", {"to_agent": CODEX, "message_id": read["message_id"], "session_id": sid}
    )
    label_mail = _send(to_agent=CODEX, body="label mail is not stranded")
    assert delivery.sweep_stranded()["stranded"] == []  # still live: nothing to report
    registry.end_session(sid, status="abandoned")

    first = delivery.sweep_stranded()
    stranded = {query["message_id"], inform["message_id"]}
    assert {s["message_id"] for s in first["stranded"]} == stranded
    assert first["notices"] == 2 and first["errors"] == []
    # One notice to the recipient label, naming ids and how to read them; no bodies.
    [to_label] = _stranded_notices(CODEX)
    assert all(m in to_label["body"] for m in stranded) and sid in to_label["body"]
    assert "SECRET" not in to_label["body"] and read["message_id"] not in to_label["body"]
    assert label_mail["message_id"] not in to_label["body"]
    # Any live session of the label sees that notice.
    other = _session(external="next")
    assert to_label["message_id"] in _ids(_inbox(other, to_agent=CODEX))
    # One notice to the sender, so it can re-send to the label.
    [to_sender] = _stranded_notices(CLAUDE)
    assert all(m in to_sender["body"] for m in stranded) and "SECRET" not in to_sender["body"]
    assert to_sender["message_id"] in _ids(
        message_tools.message_list(to_agent=CLAUDE, session_id=mine)
    )
    # Recorded once: a second pass sends nothing and the sender sees the terminal fact.
    again = delivery.sweep_stranded()
    assert again["stranded"] == [] and again["notices"] == 0
    row = message_tools.message_list(message_id=query["message_id"])["messages"][0]
    assert row["delivery"]["terminal"] == "stranded"
    # The dead-letter sweep does not notice the same mail a second time.
    conn = storage.connect()
    try:
        old = (datetime.now(UTC) - timedelta(hours=30)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE message_index SET created_at=? WHERE message_id=?", (old, query["message_id"])
        )
        conn.commit()
    finally:
        conn.close()
    assert delivery.sweep_dead_letters()["dead_letters"] == []


def test_a_revived_session_and_a_dry_run_strand_nothing():
    from unified_agent.messaging import delivery

    sid = _session(external="back")
    mid = _send(to_agent=None, to_session=sid)["message_id"]
    registry.end_session(sid, status="abandoned")
    dry = delivery.sweep_stranded(dry_run=True)
    assert [s["message_id"] for s in dry["stranded"]] == [mid] and dry["notices"] == 0
    assert _stranded_notices(CODEX) == []
    registry_tools.session_start(kind="claude_code", agent=CODEX, external_session_id="back")
    assert delivery.sweep_stranded()["stranded"] == []  # revived: the mail is live again


def test_one_hygiene_pass_strands_old_attention_mail_without_dead_lettering_it_too():
    """Review of #1568: dead letters ran first, so a stranded query older than the
    dead-letter TTL got both notices. The stranded sweep must win."""
    from unified_agent import queue_hygiene
    from unified_agent.messaging import delivery

    sid = _session(external="old")
    ask = _send(to_agent=None, to_session=sid, act="query", body="please review")
    registry.end_session(sid, status="abandoned")
    conn = storage.connect()
    try:
        old = (datetime.now(UTC) - timedelta(hours=30)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE message_index SET created_at=? WHERE message_id=?", (old, ask["message_id"])
        )
        conn.commit()
    finally:
        conn.close()
    result = queue_hygiene.run_hygiene_pass()
    assert [s["message_id"] for s in result["stranded_mail"]["stranded"]] == [ask["message_id"]]
    assert result["dead_letters"]["dead_letters"] == []
    assert len(_stranded_notices(CODEX)) == 1 and len(_stranded_notices(CLAUDE)) == 1
    dead = message_tools.message_list(to_agent=CLAUDE, from_agent=delivery.NOTICE_SENDER)
    assert dead["messages"] == []
    row = message_tools.message_list(message_id=ask["message_id"])["messages"][0]
    assert row["delivery"]["terminal"] == "stranded"


def test_mail_dead_lettered_before_its_session_ended_is_not_noticed_again_as_stranded():
    """Task #2551: the reverse order of #1568. A dead letter recorded first must
    keep the ended session's later stranded sweep quiet too."""
    from unified_agent import queue_hygiene
    from unified_agent.messaging import delivery

    sid = _session(external="later-ended")
    ask = _send(to_agent=None, to_session=sid, act="query", body="please review")
    conn = storage.connect()
    try:
        old = (datetime.now(UTC) - timedelta(hours=30)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE message_index SET created_at=? WHERE message_id=?", (old, ask["message_id"])
        )
        conn.commit()
    finally:
        conn.close()
    first = delivery.sweep_dead_letters()
    assert [d["message_id"] for d in first["dead_letters"]] == [ask["message_id"]]
    dead_notices = message_tools.message_list(to_agent=CLAUDE, from_agent=delivery.NOTICE_SENDER)[
        "messages"
    ]
    assert len(dead_notices) == 1  # the dead-letter notice, sent once
    registry.end_session(sid, status="abandoned")

    result = queue_hygiene.run_hygiene_pass()
    assert result["stranded_mail"]["stranded"] == []
    assert result["dead_letters"]["dead_letters"] == []
    assert _stranded_notices(CODEX) == []
    assert _stranded_notices(CLAUDE) == []  # no stranded notice for dead-lettered mail
    assert (
        message_tools.message_list(to_agent=CLAUDE, from_agent=delivery.NOTICE_SENDER)["messages"]
        == dead_notices
    )


def test_a_report_only_dead_letter_does_not_silence_the_stranded_notice(monkeypatch):
    """Task #2555: a dead-letter fact recorded in report mode told nobody, so the
    stranded sweep must still notice the message once its session ends."""
    from unified_agent import queue_hygiene
    from unified_agent.messaging import delivery

    monkeypatch.setenv("UA_MESSAGE_DEAD_LETTER", "report")
    sid = _session(external="report-only")
    ask = _send(to_agent=None, to_session=sid, act="query", body="please review")
    conn = storage.connect()
    try:
        old = (datetime.now(UTC) - timedelta(hours=30)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE message_index SET created_at=? WHERE message_id=?", (old, ask["message_id"])
        )
        conn.commit()
    finally:
        conn.close()
    first = delivery.sweep_dead_letters()
    assert first["mode"] == "report"
    assert [d["message_id"] for d in first["dead_letters"]] == [ask["message_id"]]
    assert (
        message_tools.message_list(to_agent=CLAUDE, from_agent=delivery.NOTICE_SENDER)["messages"]
        == []
    )  # report mode: the fact is recorded, nobody is told
    registry.end_session(sid, status="abandoned")

    result = queue_hygiene.run_hygiene_pass()
    assert [s["message_id"] for s in result["stranded_mail"]["stranded"]] == [ask["message_id"]]
    assert len(_stranded_notices(CODEX)) == 1 and len(_stranded_notices(CLAUDE)) == 1
    again = queue_hygiene.run_hygiene_pass()
    assert again["stranded_mail"]["stranded"] == []
    assert len(_stranded_notices(CLAUDE)) == 1


def test_reading_stranded_mail_neither_revives_the_session_nor_stays_stranded():
    from unified_agent.messaging import delivery

    sid = _session(external="gone")
    mid = _send(to_agent=None, to_session=sid, body="for the ended session")["message_id"]
    registry.end_session(sid, status="abandoned")
    delivery.sweep_stranded()
    # The notice's own recovery path must not resurrect what it reports.
    assert mid in _ids(message_tools.message_list(to_agent=CODEX, session_id=sid))
    graph_tool.coordination_manage("message_inbox", {"to_agent": CODEX, "session_id": sid})
    assert registry.get_session(sid)["status"] == "abandoned"
    # Once read, the message is read, not stranded.
    facts = message_tools.message_list(message_id=mid)["messages"][0]["delivery"]
    assert facts["state"] == "read" and facts["terminal"] is None
