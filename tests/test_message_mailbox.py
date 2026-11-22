"""Mailbox semantics over the typed-message event log (task #1859)."""

from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, storage
from unified_agent.messaging import index as message_index
from unified_agent.messaging.schema import CHANNEL_MESSAGE_PREFIX
from unified_agent.tools import graph as graph_tool
from unified_agent.tools import messages as message_tools

CLAUDE = "Desktop_Agent1"
HERMES = "Laptop_Agent5"  # a peer; not chief of staff
COS = "cloud_agent13"  # the only chief of staff
CODEX = "Desktop_Agent2"
OPERATOR = config.operator_label()


@pytest.fixture
def as_agent():
    """Run a block as a verified identity; reset afterwards."""
    tokens = []

    def _set(label):
        tokens.append(config.agent_identity_var.set(label))

    yield _set
    for token in reversed(tokens):
        config.agent_identity_var.reset(token)


def _send(frm=CLAUDE, to=HERMES, **kwargs):
    payload = {"act": "inform", "from_agent": frm, "to_agent": to, "body": "hello"}
    payload.update(kwargs)
    return message_tools.message_send(**payload)


def _index_rows():
    conn = storage.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM message_index").fetchall()]
    finally:
        conn.close()


def _ids(listed):
    return [row["message_id"] for row in listed["messages"]]


def test_older_mail_is_not_dropped_by_fleet_traffic():
    first = _send(frm=CLAUDE, to=HERMES, body="old but unread")
    for i in range(230):
        _send(frm=CLAUDE, to=CODEX, body=f"noise {i}")
    listed = message_tools.message_list(to_agent=HERMES)
    assert _ids(listed) == [first["message_id"]]
    assert listed["messages"][0]["body"] == "old but unread"
    assert message_tools.message_list(to_agent=HERMES, count_only=True)["count"] == 1


def test_after_seq_cursor_pages_forward_without_gaps():
    sent = [_send(body=f"m{i}")["message_id"] for i in range(5)]
    newest = message_tools.message_list(to_agent=HERMES, limit=2)
    assert _ids(newest) == sent[-2:]  # no cursor: the newest window
    page = message_tools.message_list(to_agent=HERMES, limit=2, after_seq=0)
    assert _ids(page) == sent[:2]
    nxt = message_tools.message_list(to_agent=HERMES, limit=10, after_seq=page["next_seq"])
    assert _ids(nxt) == sent[2:]
    done = message_tools.message_list(to_agent=HERMES, after_seq=nxt["next_seq"])
    assert done["messages"] == [] and done["next_seq"] == nxt["next_seq"]
    assert "next_cursor" not in done


def test_unread_count_read_and_ack_round_trip():
    a = _send(body="a")["message_id"]
    b = _send(body="b")["message_id"]
    c = _send(body="c")["message_id"]
    assert message_index.unread_count(HERMES) == 3
    assert message_tools.message_list(to_agent=HERMES, count_only=True)["unread"] == 3

    inbox = graph_tool.coordination_manage("message_inbox", {"to_agent": HERMES, "limit": 1})
    assert _ids(inbox) == [c]  # the newest window
    assert inbox["marked_read"] == 1
    assert inbox["messages"][0]["read_at"] and inbox["messages"][0]["status"] == "read"
    assert message_index.unread_count(HERMES) == 2

    unread = message_tools.message_list(to_agent=HERMES, unread_only=True)
    assert _ids(unread) == [a, b]

    one = graph_tool.coordination_manage("message_ack", {"message_id": a, "to_agent": HERMES})
    assert one["acked"] == 1 and one["unread"] == 1
    again = graph_tool.coordination_manage("message_ack", {"message_id": a, "to_agent": HERMES})
    assert again["acked"] == 0
    rows = {r["message_id"]: r for r in _index_rows()}
    assert rows[a]["acked_at"] and rows[a]["read_at"]
    assert rows[b]["read_at"] is None and rows[c]["acked_at"] is None

    swept = graph_tool.coordination_manage("message_ack", {"through": c, "to_agent": HERMES})
    assert swept["acked"] == 2  # b and c; a was already acked
    assert message_index.unread_count(HERMES) == 0
    assert message_tools.message_list(to_agent=HERMES, unread_only=True)["messages"] == []


def test_message_list_tool_never_changes_read_state():
    _send()
    message_tools.message_list(to_agent=HERMES)
    message_tools.message_list(to_agent=HERMES, unread_only=True)
    assert message_index.unread_count(HERMES) == 1


def test_only_the_recipient_can_ack_or_read(as_agent):
    mid = _send()["message_id"]
    as_agent(CLAUDE)  # the sender
    with pytest.raises(ToolError, match="no message"):
        graph_tool.coordination_manage("message_ack", {"message_id": mid})
    with pytest.raises(ToolError, match="only your own mailbox"):
        graph_tool.coordination_manage("message_ack", {"message_id": mid, "to_agent": HERMES})
    assert message_index.unread_count(HERMES) == 1
    # Reading as someone else's mailbox marks nothing: mark applies to own rows.
    inbox = graph_tool.coordination_manage("message_inbox", {})
    assert inbox["messages"] == []
    assert message_index.unread_count(HERMES) == 1


def test_sender_sees_delivered_read_answered_and_waiting():
    sent = _send(body="status?", act="query", expects_reply=True)
    quiet = _send(body="fyi", act="inform")
    mid = sent["message_id"]

    def status(msg):
        return message_tools.message_list(message_id=msg)["messages"][0]["status"]

    assert status(mid) == "delivered"
    graph_tool.coordination_manage("message_inbox", {"to_agent": HERMES})
    assert status(mid) == "read"

    waiting = message_tools.message_list(from_agent=CLAUDE, waiting_on_reply=True)
    assert _ids(waiting) == [mid]  # the inform did not ask for a reply

    # A follow-up from the sender is not an answer.
    _send(frm=CLAUDE, to=HERMES, reply_to=mid, body="ping")
    assert status(mid) == "read"
    reply = _send(frm=HERMES, to=CLAUDE, reply_to=mid, body="all green")
    assert status(mid) == "answered"
    row = message_tools.message_list(message_id=mid)["messages"][0]
    assert row["answered"] and row["answered_by"] == reply["message_id"]
    assert message_tools.message_list(from_agent=CLAUDE, waiting_on_reply=True)["messages"] == []
    assert status(quiet["message_id"]) == "read"


def test_reply_inherits_the_parents_conversation():
    root = _send(body="start")
    reply = _send(frm=HERMES, to=CLAUDE, reply_to=root["message_id"], body="re")
    assert reply["conversation_id"] == root["conversation_id"]
    assert reply["reply_to"] == root["message_id"]
    thread = message_tools.message_list(conversation_id=root["conversation_id"])
    assert _ids(thread) == [root["message_id"], reply["message_id"]]
    # An explicit conversation still wins, and a plain send still starts a new one.
    other = _send(frm=HERMES, to=CLAUDE, reply_to=root["message_id"], conversation_id="c-explicit")
    assert other["conversation_id"] == "c-explicit"
    assert _send()["conversation_id"] != root["conversation_id"]


def test_reply_to_must_name_a_message_the_replier_was_party_to():
    root = _send(frm=CLAUDE, to=HERMES)
    with pytest.raises(ToolError, match="reply_to"):
        _send(frm=HERMES, to=CLAUDE, reply_to="does-not-exist")
    with pytest.raises(ToolError, match="reply_to"):
        _send(frm=CODEX, to=CLAUDE, reply_to=root["message_id"])  # not a party
    # Neither refusal wrote an event or an index row.
    assert len(_index_rows()) == 1
    assert len(events.query_events(channels=[f"{CHANNEL_MESSAGE_PREFIX}.*"], limit=10)) == 1
    # The refused message id is free to use for a valid send.
    ok = _send(frm=CLAUDE, to=HERMES, reply_to=root["message_id"], message_id="reuse-me")
    assert ok["accepted"]


def test_external_task_space_reply_reference_is_not_checked():
    result = _send(frm=CLAUDE, to=HERMES, reply_to="ts:discord:12345")
    assert result["accepted"] and result["reply_to"] == "ts:discord:12345"


def test_comms_policy_still_applies_to_replies(monkeypatch):
    from unified_agent import operating_profiles

    root = _send()
    calls = []
    monkeypatch.setattr(operating_profiles, "check_comms", lambda *a, **k: calls.append((a, k)))
    _send(frm=HERMES, to=CLAUDE, reply_to=root["message_id"])
    assert [c[0] for c in calls] == [(HERMES, CLAUDE)]


def test_verified_caller_lists_only_its_own_mail(as_agent):
    to_hermes = _send(frm=CLAUDE, to=HERMES)["message_id"]
    to_codex = _send(frm=CLAUDE, to=CODEX)["message_id"]
    codex_to_hermes = _send(frm=CODEX, to=HERMES)["message_id"]

    as_agent(CODEX)
    assert _ids(message_tools.message_list()) == [to_codex, codex_to_hermes]
    with pytest.raises(ToolError, match="outside your mailbox"):
        message_tools.message_list(to_agent=HERMES)
    # Another agent's inbox stays closed by id and by conversation, too.
    assert message_tools.message_list(message_id=to_hermes)["messages"] == []
    # What I sent to someone is mine to look at, and a foreign from_agent is
    # narrowed to that agent's mail to me.
    assert _ids(message_tools.message_list(from_agent=CODEX, to_agent=HERMES)) == [codex_to_hermes]
    assert _ids(message_tools.message_list(from_agent=CLAUDE)) == [to_codex]
    # Counting is scoped the same way.
    assert message_tools.message_list(to_agent=CODEX, count_only=True)["count"] == 1
    with pytest.raises(ToolError, match="outside your mailbox"):
        message_tools.message_list(to_agent=HERMES, count_only=True)


@pytest.mark.parametrize("who", [OPERATOR, COS])
def test_operator_and_chief_of_staff_can_list_all_mail(as_agent, who):
    ids = [_send(frm=CLAUDE, to=CODEX)["message_id"], _send(frm=CODEX, to=CLAUDE)["message_id"]]
    as_agent(who)
    assert _ids(message_tools.message_list()) == ids
    assert _ids(message_tools.message_list(to_agent=CODEX)) == ids[:1]
    assert message_tools.message_list(count_only=True)["count"] == 2


def test_chief_of_staff_reading_does_not_mark_other_agents_mail(as_agent):
    _send(frm=CLAUDE, to=CODEX)
    as_agent(COS)
    graph_tool.coordination_manage("message_inbox", {"unread_only": False})
    assert message_index.unread_count(CODEX) == 1


def test_unverified_caller_keeps_unrestricted_reads_but_names_its_mailbox():
    _send(frm=CLAUDE, to=CODEX)
    assert len(message_tools.message_list()["messages"]) == 1
    with pytest.raises(ToolError, match="to_agent is required"):
        graph_tool.coordination_manage("message_inbox", {})
    with pytest.raises(ToolError, match="to_agent is required"):
        graph_tool.coordination_manage("message_ack", {"through": "x"})


def test_backfill_is_idempotent_and_rebuilds_from_the_event_log():
    kept = _send(body="indexed live")
    # An event written before the index existed: no payload extras, no row.
    events.record_event(
        channel=f"{CHANNEL_MESSAGE_PREFIX}.request",
        payload={
            "message_id": "legacy-1",
            "act": "request",
            "conversation_id": "legacy-conv",
            "reply_to": None,
            "from_agent": CLAUDE,
            "to_agent": CODEX,
            "hop": 0,
            "body": "from before the index",
            "task_id": 7,
        },
        originator=CLAUDE,
        notify=False,
    )
    events.record_event(  # not a mailbox message: no recipient
        channel=f"{CHANNEL_MESSAGE_PREFIX}.inform", payload={"message_id": "bad"}, notify=False
    )
    conn = storage.connect()
    try:
        assert message_index.backfill(conn) == 1
        conn.commit()
        assert message_index.backfill(conn) == 0
        conn.commit()
        conn.execute("DROP TABLE message_index")
        conn.execute("DELETE FROM meta WHERE key='message_index_backfill'")
        conn.commit()
        message_index.ensure_schema(conn)  # a new table replays the whole log
        conn.commit()
        message_index.ensure_schema(conn)  # and an existing one is left alone
        conn.commit()
    finally:
        conn.close()
    rows = {r["message_id"]: r for r in _index_rows()}
    assert set(rows) == {kept["message_id"], "legacy-1"}
    assert rows["legacy-1"]["task_id"] == 7 and rows["legacy-1"]["read_at"] is None
    listed = message_tools.message_list(to_agent=CODEX)
    assert listed["messages"][0]["body"] == "from before the index"


def test_a_failed_event_append_leaves_no_index_row(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("append failed")

    with monkeypatch.context() as patch:
        patch.setattr(events, "record_event_once_on_connection", boom)
        with pytest.raises(RuntimeError, match="append failed"):
            _send(message_id="atomic-1")
    assert _index_rows() == []
    # The idempotency claim rolled back too, so the same id can be sent.
    assert _send(message_id="atomic-1")["duplicate"] is False
    assert [r["message_id"] for r in _index_rows()] == ["atomic-1"]


def test_a_failed_index_write_leaves_no_event(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("index failed")

    monkeypatch.setattr(message_index, "insert_on_connection", boom)
    with pytest.raises(RuntimeError, match="index failed"):
        _send(message_id="atomic-2")
    assert events.query_events(channels=[f"{CHANNEL_MESSAGE_PREFIX}.*"], limit=10) == []
    assert _index_rows() == []


def test_duplicate_send_writes_one_index_row():
    first = _send(message_id="dup-1")
    second = _send(message_id="dup-1")
    assert second["duplicate"] is True and second["event_ulid"] == first["event_ulid"]
    assert len(_index_rows()) == 1


def test_pruned_event_keeps_the_mailbox_row_without_a_body():
    mid = _send(body="will be pruned")["message_id"]
    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute("DELETE FROM event_log WHERE channel LIKE 'agent.message.%'")
        conn.commit()
    finally:
        conn.close()
    row = message_tools.message_list(message_id=mid)["messages"][0]
    assert row["body"] is None and row["body_pruned"] is True
    assert row["from_agent"] == CLAUDE and row["status"] == "delivered"


def test_backfill_marks_old_mail_read_and_keeps_recent_mail_unread():
    def legacy(mid: str, created_at: str) -> None:
        events.record_event(
            channel=f"{CHANNEL_MESSAGE_PREFIX}.inform",
            payload={"message_id": mid, "act": "inform", "from_agent": CLAUDE, "to_agent": CODEX},
            originator=CLAUDE,
            notify=False,
        )
        conn = storage.connect()
        try:
            with events.ledger_maintenance(conn):
                conn.execute(
                    "UPDATE event_log SET created_at=? WHERE payload LIKE ?",
                    (created_at, f'%"{mid}"%'),
                )
            conn.commit()
        finally:
            conn.close()

    old = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
    recent = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    legacy("legacy-old", old)
    legacy("legacy-recent", recent)
    conn = storage.connect()
    try:
        conn.execute("DELETE FROM message_index")
        assert message_index.backfill(conn) == 2
        conn.commit()
        rows = {
            r["message_id"]: r["read_at"]
            for r in conn.execute("SELECT message_id, read_at FROM message_index")
        }
    finally:
        conn.close()
    assert rows["legacy-old"] is not None
    assert rows["legacy-recent"] is None


def test_interrupted_backfill_is_retried_on_the_next_start(monkeypatch):
    for i in range(3):
        _send(message_id=f"hist-{i}")
    conn = storage.connect()
    try:
        conn.execute("DROP TABLE message_index")
        conn.execute("DELETE FROM meta WHERE key='message_index_backfill'")
        conn.commit()
        real = message_index.backfill

        def partial_then_stop(c):
            real(c)
            raise RuntimeError("stopped part-way")

        with monkeypatch.context() as patch:
            patch.setattr(message_index, "backfill", partial_then_stop)
            with pytest.raises(RuntimeError, match="part-way"):
                message_index.ensure_schema(conn)
        conn.rollback()  # what closing the init connection does
    finally:
        conn.close()
    assert _index_rows() == []  # the table survived, the rows did not

    storage.init_db()  # a fresh start finds no marker and backfills again
    assert sorted(r["message_id"] for r in _index_rows()) == ["hist-0", "hist-1", "hist-2"]
    conn = storage.connect()
    try:
        assert conn.execute("SELECT 1 FROM meta WHERE key='message_index_backfill'").fetchone()
    finally:
        conn.close()


def test_reply_to_a_third_agent_does_not_answer_the_request():
    ask = _send(frm=CLAUDE, to=HERMES, act="query", expects_reply=True)["message_id"]
    # Hermes passes the answer to Codex instead of back to the asker.
    _send(frm=HERMES, to=CODEX, reply_to=ask, body="forwarding")
    assert _ids(message_tools.message_list(from_agent=CLAUDE, waiting_on_reply=True)) == [ask]
    assert message_tools.message_list(message_id=ask)["messages"][0]["status"] != "answered"
    _send(frm=HERMES, to=CLAUDE, reply_to=ask, body="here you go")
    assert message_tools.message_list(from_agent=CLAUDE, waiting_on_reply=True)["messages"] == []


def test_waiting_view_is_the_callers_own_sent_mail(as_agent):
    ask = _send(frm=CLAUDE, to=CODEX, act="query", expects_reply=True)["message_id"]
    mine = _send(frm=CODEX, to=HERMES, act="query", expects_reply=True)["message_id"]
    as_agent(CODEX)
    # What Codex owes Claude is not Codex's waiting view.
    assert _ids(message_tools.message_list(waiting_on_reply=True)) == [mine]
    with pytest.raises(ToolError, match="from_agent must be you"):
        message_tools.message_list(from_agent=CLAUDE, waiting_on_reply=True)
    assert ask not in _ids(message_tools.message_list(waiting_on_reply=True, from_agent=CODEX))


def test_a_recreated_index_replays_history_even_with_the_marker_set():
    sent = _send(body="survives a rebuild")
    conn = storage.connect()
    try:
        conn.execute("DROP TABLE message_index")
        conn.commit()
        message_index.ensure_schema(conn)  # marker still present in meta
        conn.commit()
        ids = {r["message_id"] for r in conn.execute("SELECT message_id FROM message_index")}
    finally:
        conn.close()
    assert sent["message_id"] in ids


def test_an_interrupted_rebuild_is_retried_on_the_next_start(monkeypatch):
    sent = _send(body="must come back")
    conn = storage.connect()
    try:
        conn.execute("DROP TABLE message_index")
        conn.commit()

        def boom(_conn):
            raise RuntimeError("stopped mid-replay")

        monkeypatch.setattr(message_index, "backfill", boom)
        with pytest.raises(RuntimeError):
            message_index.ensure_schema(conn)
        conn.rollback()
        monkeypatch.undo()
        message_index.ensure_schema(conn)  # next start
        conn.commit()
        ids = {r["message_id"] for r in conn.execute("SELECT message_id FROM message_index")}
    finally:
        conn.close()
    assert sent["message_id"] in ids


# --- Delivery facts and dead letters (task #2391) ---------------------------------


def _age(message_id: str, hours: float) -> None:
    stamp = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with storage.connect() as conn:
        conn.execute(
            "UPDATE message_index SET created_at=? WHERE message_id=?", (stamp, message_id)
        )
        conn.commit()


def _delivery(message_id: str) -> dict:
    row = message_tools.message_list(message_id=message_id)["messages"][0]
    return row["delivery"]


def test_a_stored_message_is_admitted_not_delivered():
    sent = _send(act="request", body="please look")
    facts = _delivery(sent["message_id"])
    assert facts["state"] == "admitted"
    assert facts["appended"] == "no" and facts["appended_at"] is None
    assert facts["admitted_at"] and facts["terminal"] is None


def test_read_acked_and_answered_are_separate_facts():
    read = _send(act="request", body="read me")
    acked = _send(act="request", body="ack me")
    answered = _send(act="request", body="answer me")
    graph_tool.coordination_manage(
        "message_inbox", {"to_agent": HERMES, "conversation_id": read["conversation_id"]}
    )
    graph_tool.coordination_manage(
        "message_ack", {"to_agent": HERMES, "message_id": acked["message_id"]}
    )
    _send(frm=HERMES, to=CLAUDE, body="done", reply_to=answered["message_id"])
    assert _delivery(read["message_id"])["state"] == "read"
    assert _delivery(acked["message_id"])["terminal"] == "acked"
    facts = _delivery(answered["message_id"])
    assert facts["state"] == "answered" and facts["terminal"] == "answered"


def test_unread_attention_mail_dead_letters_once_and_tells_the_sender_once():
    from unified_agent.messaging import delivery

    stale = _send(act="request", body="SECRET-BODY please review", task_id=2391)
    fresh = _send(act="request", body="just sent")
    chatter = _send(act="inform", body="fyi")
    ancient = _send(act="request", body="from last week")
    for mid, hours in ((stale["message_id"], 30), (chatter["message_id"], 30)):
        _age(mid, hours)
    _age(ancient["message_id"], 24 * 7)

    first = delivery.sweep_dead_letters()
    assert [d["message_id"] for d in first["dead_letters"]] == [stale["message_id"]]
    assert first["notices"] == 1 and first["errors"] == []
    again = delivery.sweep_dead_letters()
    assert again["dead_letters"] == [] and again["notices"] == 0

    notices = message_tools.message_list(to_agent=CLAUDE, from_agent=delivery.NOTICE_SENDER)
    assert len(notices["messages"]) == 1
    notice = notices["messages"][0]
    assert notice["act"] == "inform" and notice["task_id"] == 2391
    assert stale["message_id"] in notice["body"] and "SECRET-BODY" not in notice["body"]
    assert _delivery(stale["message_id"])["state"] == "dead_letter"
    assert _delivery(fresh["message_id"])["state"] == "admitted"
    logged = events.query_events(channels=[delivery.DEAD_LETTER_CHANNEL], limit=5)
    assert len(logged) == 1 and "body" not in logged[0]["payload"]


def test_report_mode_records_facts_without_notices(monkeypatch):
    from unified_agent.messaging import delivery

    monkeypatch.setenv("UA_MESSAGE_DEAD_LETTER", "report")
    stale = _send(act="query", body="anyone?")
    _age(stale["message_id"], 30)
    swept = delivery.sweep_dead_letters()
    assert swept["mode"] == "report" and swept["notices"] == 0
    assert _delivery(stale["message_id"])["terminal"] == "dead_letter"
    assert message_tools.message_list(from_agent=delivery.NOTICE_SENDER)["messages"] == []
    # Switching to notify later still tells the sender, once.
    monkeypatch.setenv("UA_MESSAGE_DEAD_LETTER", "notify")
    assert delivery.sweep_dead_letters()["notices"] == 1
    assert delivery.sweep_dead_letters()["notices"] == 0


def test_the_notice_sender_and_id_space_are_reserved():
    from unified_agent.messaging import delivery, send

    with pytest.raises(ToolError, match="reserved for the hub"):
        _send(frm=delivery.NOTICE_SENDER, to=CLAUDE)
    with pytest.raises(ToolError, match="reserved for hub service notices"):
        _send(message_id=f"{delivery.NOTICE_ID_PREFIX}abc")
    # Inside the notice path, a send that is not exactly the notice is refused.
    token = send._delivery_notice.set(
        send._DeliveryNotice("orig", CLAUDE, f"{delivery.NOTICE_ID_PREFIX}orig", None)
    )
    try:
        with pytest.raises(ToolError, match="does not match its message"):
            send.send_message(
                act="inform",
                from_agent=delivery.NOTICE_SENDER,
                to_agent=CODEX,
                body="x",
                message_id=f"{delivery.NOTICE_ID_PREFIX}orig",
            )
    finally:
        send._delivery_notice.reset(token)
