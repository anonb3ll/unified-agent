"""Mailbox paging and ack by commit-order seq, not ULID (task #1870)."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, storage
from unified_agent.messaging import index as message_index
from unified_agent.messaging.schema import CHANNEL_MESSAGE_PREFIX
from unified_agent.tools import graph as graph_tool
from unified_agent.tools import messages as message_tools
from unified_agent.tools import registry as registry_tools

CLAUDE = "Desktop_Agent1"
HERMES = "Desktop_Agent5"
CODEX = "Desktop_Agent2"

# Sorts before every ULID minted now, so a row carrying it is "older" than any
# cursor a consumer already holds.
EARLY_ULID = "0" * 25 + "1"


def _send(frm=CLAUDE, to=HERMES, **kwargs):
    payload = {"act": "inform", "from_agent": frm, "to_agent": to, "body": "hello"}
    payload.update(kwargs)
    return message_tools.message_send(**payload)


def _ids(listed):
    return [row["message_id"] for row in listed["messages"]]


def _late_message(message_id, *, to=HERMES, ulid=EARLY_ULID, to_session=None):
    """Commit a message whose ULID sorts before everything already in the log.

    Appended last, so its ROWID is the highest: the shape a writer that committed
    late leaves behind. Returns the new row's seq.
    """
    payload = {
        "message_id": message_id,
        "act": "inform",
        "conversation_id": "late-conv",
        "reply_to": None,
        "from_agent": CLAUDE,
        "to_agent": to,
        "to_session": to_session,
        "from_session": None,
        "hop": 0,
        "body": "committed late",
        "task_id": None,
        "delegation_id": None,
        "event_id": None,
        "expects_reply": False,
    }
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO event_log (ulid, channel, payload, originator, episode_id, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (
                ulid,
                f"{CHANNEL_MESSAGE_PREFIX}.inform",
                json.dumps(payload),
                CLAUDE,
                "late-conv",
                storage.now(),
            ),
        )
        message_index.insert_on_connection(
            conn,
            message_id=message_id,
            recipient=to,
            sender=CLAUDE,
            act="inform",
            conversation_id="late-conv",
            reply_to=None,
            expects_reply=False,
            task_id=None,
            delegation_id=None,
            event_ulid=ulid,
            created_at=storage.now(),
            target_session=to_session,
        )
        conn.commit()
        return conn.execute("SELECT ROWID FROM event_log WHERE ulid=?", (ulid,)).fetchone()[0]
    finally:
        conn.close()


def _row(message_id):
    conn = storage.connect()
    try:
        return dict(
            conn.execute("SELECT * FROM message_index WHERE message_id=?", (message_id,)).fetchone()
        )
    finally:
        conn.close()


def _event_seq(ulid):
    conn = storage.connect()
    try:
        return conn.execute("SELECT ROWID FROM event_log WHERE ulid=?", (ulid,)).fetchone()[0]
    finally:
        conn.close()


def _all_rows():
    conn = storage.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM message_index ORDER BY event_seq")]
    finally:
        conn.close()


def _ack(**params):
    return graph_tool.coordination_manage("message_ack", {"to_agent": HERMES, **params})


# --- live write path, paging ------------------------------------------------------


def test_live_rows_carry_the_event_rowid_as_seq():
    sent = _send()
    row = _row(sent["message_id"])
    assert row["event_seq"] == _event_seq(sent["event_ulid"])
    listed = message_tools.message_list(to_agent=HERMES)
    assert listed["messages"][0]["seq"] == row["event_seq"]
    assert listed["next_seq"] == row["event_seq"]


def test_after_seq_pages_forward_and_next_seq_is_monotonic():
    sent = [_send(body=f"m{i}")["message_id"] for i in range(5)]
    seen: list[str] = []
    cursor = 0
    previous = -1
    for _ in range(10):
        page = message_tools.message_list(to_agent=HERMES, limit=2, after_seq=cursor)
        assert page["next_seq"] >= previous
        previous = page["next_seq"]
        if not page["messages"]:
            assert page["next_seq"] == cursor  # an empty page does not move the cursor
            break
        seen.extend(_ids(page))
        cursor = page["next_seq"]
    assert seen == sent
    # after_seq=0 is "from the start", and a mid-stream cursor skips what it passed.
    seqs = [r["seq"] for r in message_tools.message_list(to_agent=HERMES)["messages"]]
    assert seqs == sorted(seqs)
    tail = message_tools.message_list(to_agent=HERMES, after_seq=seqs[2])
    assert _ids(tail) == sent[3:]


def test_an_empty_mailbox_returns_a_zero_next_seq():
    listed = message_tools.message_list(to_agent=HERMES)
    assert listed["messages"] == [] and listed["next_seq"] == 0


def test_the_ulid_since_cursor_is_retired_and_bad_cursors_are_errors():
    first = _send()
    retired = "since \\(ULID\\) cursors are retired; page with after_seq and next_seq"
    for since in (first["event_ulid"], "anything"):
        with pytest.raises(ToolError, match=retired):
            message_tools.message_list(to_agent=HERMES, since=since)
        with pytest.raises(ToolError, match=retired):
            message_tools.message_list(to_agent=HERMES, since=since, after_seq=1)
        with pytest.raises(ToolError, match=retired):
            graph_tool.coordination_manage("message_inbox", {"to_agent": HERMES, "since": since})
    assert "next_cursor" not in message_tools.message_list(to_agent=HERMES)
    for bad in (-1, True, "3"):
        with pytest.raises(ToolError, match="after_seq"):
            message_tools.message_list(to_agent=HERMES, after_seq=bad)


# --- the late-committed row -------------------------------------------------------


def test_a_late_committed_row_is_returned_by_after_seq():
    first, second = _send(body="a")["message_id"], _send(body="b")["message_id"]
    page = message_tools.message_list(to_agent=HERMES, after_seq=0)
    assert _ids(page) == [first, second]
    late_seq = _late_message("late-1")
    assert late_seq > page["next_seq"]
    assert EARLY_ULID < page["messages"][-1]["ulid"]  # older ULID than the row already paged
    resumed = message_tools.message_list(to_agent=HERMES, after_seq=page["next_seq"])
    assert _ids(resumed) == ["late-1"]
    assert resumed["next_seq"] == late_seq > page["next_seq"]


def test_an_after_seq_pager_terminates_across_a_late_row_without_redelivery():
    sent = [_send(body=f"m{i}")["message_id"] for i in range(3)]
    first = message_tools.message_list(to_agent=HERMES, limit=2, after_seq=0)
    _late_message("late-loop")
    after = _send(body="after the late row")["message_id"]
    delivered = list(_ids(first))
    cursor = first["next_seq"]
    for _ in range(10):  # bounded: a looping pager would never see an empty page
        page = message_tools.message_list(to_agent=HERMES, limit=1, after_seq=cursor)
        if not page["messages"]:
            break
        assert page["next_seq"] > cursor
        delivered.extend(_ids(page))
        cursor = page["next_seq"]
    else:
        pytest.fail("after_seq pager did not reach an empty page")
    # Every message exactly once, in commit order, with the late row after the earlier ones.
    assert delivered == [*sent, "late-loop", after]


def test_the_inbox_surfaces_a_late_row_and_marks_it_read():
    _send(body="a")
    page = graph_tool.coordination_manage("message_inbox", {"to_agent": HERMES})
    _late_message("late-3")
    inbox = graph_tool.coordination_manage(
        "message_inbox", {"to_agent": HERMES, "after_seq": page["next_seq"]}
    )
    assert _ids(inbox) == ["late-3"] and inbox["marked_read"] == 1


def test_ack_through_an_earlier_cursor_does_not_ack_a_late_row():
    a, b = _send(body="a")["message_id"], _send(body="b")["message_id"]
    page = message_tools.message_list(to_agent=HERMES)
    _late_message("late-4")
    swept = _ack(through=page["next_seq"])
    assert swept["acked"] == 2 and swept["through_seq"] == page["next_seq"]
    assert _row("late-4")["acked_at"] is None and _row("late-4")["read_at"] is None
    assert _row(a)["acked_at"] and _row(b)["acked_at"]
    # The late row is still unread, and the next page from the cursor finds it.
    assert message_tools.message_list(to_agent=HERMES, unread_only=True)["unread"] == 1


def test_ack_through_a_message_id_stops_at_that_row():
    _send(body="a")
    last = _send(body="b")
    _late_message("late-5")
    assert _ack(through=last["message_id"])["acked"] == 2
    assert _row("late-5")["acked_at"] is None
    # An ack through the late row's own id sweeps it up and everything before it.
    swept = _ack(through="late-5")
    assert swept["acked"] == 1 and swept["through_seq"] == _row("late-5")["event_seq"]
    assert _row("late-5")["acked_at"]


def test_ack_through_a_ulid_is_refused_and_acks_nothing():
    _send(body="a")
    cursor_msg = _send(body="b")
    _late_message("late-6")
    for ulid in (cursor_msg["event_ulid"], EARLY_ULID):
        with pytest.raises(ToolError, match="ULID cursors are retired.*next_seq"):
            _ack(through=ulid)
    assert [r["acked_at"] for r in _all_rows()] == [None, None, None]


def test_ack_through_rejects_bad_cursors():
    _send()
    with pytest.raises(ToolError, match="exactly one"):
        _ack()
    with pytest.raises(ToolError, match="exactly one"):
        _ack(message_id="x", through=3)
    with pytest.raises(ToolError, match="non-negative"):
        _ack(through=-1)
    with pytest.raises(ToolError, match="seq"):
        _ack(through=True)
    with pytest.raises(ToolError, match="not a message_id"):
        _ack(through="12345")  # a digit string is an id, never a seq
    assert _ack(through=0)["acked"] == 0


def test_ack_through_a_seq_acks_only_rows_at_or_below_it():
    ids = [_send(body=f"m{i}")["message_id"] for i in range(4)]
    seq = _row(ids[1])["event_seq"]
    assert _ack(through=seq)["acked"] == 2
    assert [bool(_row(i)["acked_at"]) for i in ids] == [True, True, False, False]


# --- sessions (#1860) -------------------------------------------------------------


def _session(agent=CODEX, external="ext-1"):
    return registry_tools.session_start(
        kind="claude_code", agent=agent, external_session_id=external
    )["session_id"]


def test_session_paths_page_and_ack_by_seq():
    a, b = _session(external="a"), _session(external="b")
    label = _send(to=CODEX, body="label")["message_id"]
    mine = _send(to=CODEX, body="for a", to_session=a)["message_id"]
    other = _send(to=CODEX, body="for b", to_session=b)["message_id"]

    page = message_tools.message_list(to_agent=CODEX, session_id=a, after_seq=0)
    assert _ids(page) == [label, mine]  # never another session's mail
    late_label = _late_message("late-label", to=CODEX)
    late_mine = _late_message("late-mine", to=CODEX, to_session=a, ulid="0" * 24 + "20")
    late_other = _late_message("late-other", to=CODEX, to_session=b, ulid="0" * 24 + "30")
    assert late_other > late_mine > late_label

    resumed = graph_tool.coordination_manage(
        "message_inbox", {"to_agent": CODEX, "session_id": a, "after_seq": page["next_seq"]}
    )
    assert _ids(resumed) == ["late-label", "late-mine"]
    assert resumed["next_seq"] == late_mine

    # Ack through the first page's cursor as session a: only that page, only a's receipts.
    acked = graph_tool.coordination_manage(
        "message_ack", {"to_agent": CODEX, "session_id": a, "through": page["next_seq"]}
    )
    assert acked["acked"] == 2 and acked["session_id"] == a
    assert _row(mine)["acked_at"]  # session mail: on the row
    assert _row(label)["acked_at"] is None  # label mail: a receipt, not the row
    assert _row(other)["acked_at"] is None
    assert _row("late-label")["acked_at"] is None and _row("late-mine")["acked_at"] is None
    # Session b still sees the label mail unread, and the legacy cursor works there too.
    seen_b = message_tools.message_list(
        to_agent=CODEX, session_id=b, after_seq=_row(mine)["event_seq"]
    )
    assert _ids(seen_b) == [other, "late-label", "late-other"]


# --- backfill, repair, migration --------------------------------------------------


def _legacy_event(message_id, to=CODEX):
    return events.record_event(
        channel=f"{CHANNEL_MESSAGE_PREFIX}.request",
        payload={
            "message_id": message_id,
            "act": "request",
            "conversation_id": "legacy-conv",
            "from_agent": CLAUDE,
            "to_agent": to,
            "hop": 0,
            "body": "from before the index",
        },
        originator=CLAUDE,
        notify=False,
    )


def test_backfill_fills_event_seq_from_the_event_rowid():
    ulid = _legacy_event("legacy-seq")
    conn = storage.connect()
    try:
        assert message_index.backfill(conn) == 1
        conn.commit()
    finally:
        conn.close()
    assert _row("legacy-seq")["event_seq"] == _event_seq(ulid)


def test_ensure_schema_repairs_rows_without_event_seq_idempotently():
    kept = _send(body="live")
    ulid = _legacy_event("legacy-repair")
    conn = storage.connect()
    try:
        message_index.backfill(conn)
        conn.execute("UPDATE message_index SET event_seq = NULL")
        conn.commit()
        message_index.ensure_schema(conn)
        conn.commit()
        assert message_index.repair_event_seq(conn) == 0  # nothing left to fix
        conn.commit()
    finally:
        conn.close()
    assert _row("legacy-repair")["event_seq"] == _event_seq(ulid)
    assert _row(kept["message_id"])["event_seq"] == _event_seq(kept["event_ulid"])


def test_a_pre_1870_table_gains_the_column_and_index_and_is_repaired():
    sent = _send(body="indexed before the column existed")
    conn = storage.connect()
    try:
        conn.execute("DROP INDEX idx_message_index_recipient_seq")
        conn.execute("ALTER TABLE message_index DROP COLUMN event_seq")
        conn.commit()
        message_index.ensure_schema(conn)
        conn.commit()
        index_names = {r["name"] for r in conn.execute("PRAGMA index_list(message_index)")}
        assert "idx_message_index_recipient_seq" in index_names
    finally:
        conn.close()
    assert _row(sent["message_id"])["event_seq"] == _event_seq(sent["event_ulid"])
    assert _ids(message_tools.message_list(to_agent=HERMES, after_seq=0)) == [sent["message_id"]]


def test_a_row_whose_event_was_pruned_keeps_a_null_seq_and_is_acked_with_include_pruned():
    old = _send(body="old")
    live = _send(body="live")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE message_index SET event_seq = NULL WHERE message_id=?", (old["message_id"],)
        )
        with events.ledger_maintenance(conn):
            conn.execute("DELETE FROM event_log WHERE ulid=?", (old["event_ulid"],))
        conn.commit()
        message_index.ensure_schema(conn)  # nothing to join: stays NULL
        conn.commit()
    finally:
        conn.close()
    assert _row(old["message_id"])["event_seq"] is None
    newest = message_tools.message_list(to_agent=HERMES)
    assert _ids(newest) == [old["message_id"], live["message_id"]]
    assert _ids(message_tools.message_list(to_agent=HERMES, after_seq=0)) == [live["message_id"]]
    with pytest.raises(ToolError, match="pruned"):
        _ack(through=old["message_id"])
    # A seq cursor has no position for it; include_pruned acks it with the rest.
    seq = _row(live["message_id"])["event_seq"]
    assert _ack(through=seq)["acked"] == 1 and _row(old["message_id"])["acked_at"] is None
    swept = graph_tool.coordination_manage(
        "message_ack", {"to_agent": HERMES, "through": seq, "include_pruned": True}
    )
    assert swept["acked"] == 1 and _row(old["message_id"])["acked_at"]
    assert _row(live["message_id"])["acked_at"]
    # message_id= still acks a pruned row on its own.
    other = _send(body="old 2")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE message_index SET event_seq=NULL WHERE message_id=?", (other["message_id"],)
        )
        conn.commit()
    finally:
        conn.close()
    assert _ack(message_id=other["message_id"])["acked"] == 1


def test_identity_is_not_widened_by_seq_paging(monkeypatch):
    """A verified caller still sees only its own mail, whatever cursor it passes."""
    _send(frm=CLAUDE, to=HERMES, body="not for codex")
    token = config.agent_identity_var.set(CODEX)
    try:
        listed = message_tools.message_list(after_seq=0)
        assert listed["messages"] == []
        with pytest.raises(ToolError):
            message_tools.message_list(to_agent=HERMES, after_seq=0)
    finally:
        config.agent_identity_var.reset(token)


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_include_pruned_must_be_a_real_boolean(value):
    message_tools.message_send(act="inform", from_agent=CLAUDE, to_agent=HERMES, body="x")
    with pytest.raises(ToolError, match="include_pruned must be a boolean"):
        graph_tool.coordination_manage(
            "message_ack", {"to_agent": HERMES, "through": 10**9, "include_pruned": value}
        )
