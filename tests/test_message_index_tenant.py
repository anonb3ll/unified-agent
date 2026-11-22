"""The mailbox index is scoped per tenant (task #1979).

``message_index.message_id`` was one global PRIMARY KEY while ``message_send``'s
idempotency receipt is per tenant (#1796). A message id one tenant used was
refused for every other tenant, and the refusal told the second tenant that the
id existed. Reads were not fenced either: ``message_list``, the inbox digest,
replies, acks and the conversation views could reach another tenant's mail.
Each tenant now has its own message-id space and sees only its own mail. The
untenanted private hub (every row ``org_id`` NULL) behaves as before.
"""

from __future__ import annotations

import sqlite3

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.messaging import conversations
from unified_agent.messaging import index as message_index
from unified_agent.messaging import send as messaging_send

ORG_A = "org_a"
ORG_B = "org_b"
SENDER = "Desktop_Agent1"
RECIPIENT = "Desktop_Agent5"


@pytest.fixture
def as_org(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")

    def _switch(org: str) -> None:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)

    return _switch


def _send(body: str = "hi", **kwargs):
    return messaging_send.send_message(
        act=kwargs.pop("act", "inform"),
        from_agent=kwargs.pop("from_agent", SENDER),
        to_agent=kwargs.pop("to_agent", RECIPIENT),
        body=body,
        **kwargs,
    )


def _rows() -> list[dict]:
    conn = storage.connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT message_id, org_id, sender, recipient FROM message_index ORDER BY event_seq"
            )
        ]
    finally:
        conn.close()


def test_the_same_message_id_is_usable_in_each_tenant(as_org):
    as_org(ORG_A)
    a = _send("from a", message_id="shared-id")
    as_org(ORG_B)
    b = _send("from b", message_id="shared-id")
    assert b["accepted"] is True
    assert b["duplicate"] is False
    assert b["event_ulid"] != a["event_ulid"]
    assert sorted((r["message_id"], r["org_id"]) for r in _rows()) == [
        ("shared-id", ORG_A),
        ("shared-id", ORG_B),
    ]
    # Each tenant's retry returns its own receipt.
    assert _send("from b", message_id="shared-id")["event_ulid"] == b["event_ulid"]
    as_org(ORG_A)
    assert _send("from a", message_id="shared-id")["event_ulid"] == a["event_ulid"]


def test_an_id_reused_inside_one_tenant_is_still_refused(as_org):
    as_org(ORG_A)
    _send(message_id="taken-id")
    with pytest.raises(ToolError, match="already used by another message"):
        _send(from_agent="Desktop_Agent2", message_id="taken-id")


def test_message_list_and_inbox_see_only_the_callers_tenant(as_org):
    as_org(ORG_A)
    _send("secret a", message_id="a-1", expects_reply=True)
    as_org(ORG_B)
    _send("b mail", message_id="b-1")

    listed = messaging_send.list_messages(limit=50)
    assert [m["message_id"] for m in listed["messages"]] == ["b-1"]
    assert messaging_send.list_messages(message_id="a-1")["total"] == 0
    assert messaging_send.list_messages(count_only=True)["count"] == 1
    assert messaging_send.list_messages(waiting_on_reply=True)["total"] == 0
    inbox = messaging_send.list_messages(to_agent=RECIPIENT, unread_only=True)
    assert [m["message_id"] for m in inbox["messages"]] == ["b-1"]
    assert inbox["unread"] == 1
    assert message_index.unread_count(RECIPIENT) == 1

    conn = storage.connect()
    try:
        digest = message_index.inbox_summary(conn, RECIPIENT)
    finally:
        conn.close()
    assert digest["unread"] == 1
    assert [m["message_id"] for m in digest["messages"]] == ["b-1"]


def test_reply_to_another_tenants_message_reads_as_missing(as_org):
    as_org(ORG_A)
    _send("question", message_id="a-q", expects_reply=True)
    as_org(ORG_B)
    with pytest.raises(ToolError, match="is not a message you sent or received"):
        _send("answer", from_agent=RECIPIENT, to_agent=SENDER, reply_to="a-q")


def test_a_reply_in_one_tenant_does_not_answer_the_other(as_org):
    as_org(ORG_A)
    _send("question", message_id="q", expects_reply=True)
    as_org(ORG_B)
    _send("question", message_id="q", expects_reply=True)
    _send("answer", from_agent=RECIPIENT, to_agent=SENDER, reply_to="q", message_id="b-ans")
    as_org(ORG_A)
    waiting = messaging_send.list_messages(waiting_on_reply=True, from_agent=SENDER)
    assert [m["message_id"] for m in waiting["messages"]] == ["q"]
    assert waiting["messages"][0]["answered"] is False


def test_ack_and_mark_read_touch_only_the_callers_tenant(as_org):
    as_org(ORG_A)
    _send("a mail", message_id="same")
    as_org(ORG_B)
    _send("b mail", message_id="same")
    messaging_send.ack_messages(message_id="same", to_agent=RECIPIENT)
    assert message_index.unread_count(RECIPIENT) == 0
    as_org(ORG_A)
    assert message_index.unread_count(RECIPIENT) == 1
    with pytest.raises(ToolError, match="no message"):
        messaging_send.ack_messages(message_id="b-only", to_agent=RECIPIENT)
    marked = messaging_send.list_messages(to_agent=RECIPIENT, mark_read=True)
    assert marked["marked_read"] == 1
    as_org(ORG_B)
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT org_id, read_at, acked_at FROM message_index ORDER BY org_id"
        ).fetchall()
    finally:
        conn.close()
    a_row, b_row = (dict(r) for r in rows)
    assert a_row["org_id"] == ORG_A and a_row["acked_at"] is None
    assert b_row["org_id"] == ORG_B and b_row["acked_at"] is not None


def test_conversation_views_see_only_the_callers_tenant(as_org):
    as_org(ORG_A)
    a = _send("a mail", message_id="a-1", conversation_id="conv-shared")
    as_org(ORG_B)
    token = config.agent_identity_var.set(SENDER)
    try:
        listed = conversations.list_conversations()
        assert listed["conversations"] == []
        assert conversations.conversation_thread(a["conversation_id"]) is None
        _send("b mail", message_id="b-1", conversation_id="conv-shared")
        thread = conversations.conversation_thread("conv-shared")
        assert [m["message_id"] for m in thread["messages"]] == ["b-1"]
        assert [c["total"] for c in conversations.list_conversations()["conversations"]] == [1]
    finally:
        config.agent_identity_var.reset(token)


def test_untenanted_hub_keeps_global_ids_and_reads(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "core")
    _send(message_id="plain-1")
    assert _rows()[0]["org_id"] is None
    with pytest.raises(ToolError, match="already used by another message"):
        _send(from_agent="Desktop_Agent2", message_id="plain-1")
    assert messaging_send.list_messages()["total"] == 1


_LEGACY_TABLE = """
CREATE TABLE message_index (
  message_id TEXT PRIMARY KEY,
  recipient TEXT NOT NULL,
  sender TEXT NOT NULL,
  act TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  reply_to TEXT,
  expects_reply INTEGER NOT NULL DEFAULT 0,
  task_id INTEGER,
  delegation_id INTEGER,
  event_ulid TEXT NOT NULL,
  created_at TEXT NOT NULL,
  read_at TEXT,
  acked_at TEXT,
  target_session TEXT,
  sender_session TEXT,
  event_seq INTEGER
)
"""


def test_a_global_key_index_is_rebuilt_per_tenant_keeping_its_rows(as_org):
    as_org(ORG_A)
    _send("a mail", message_id="old-a")
    messaging_send.list_messages(to_agent=RECIPIENT, mark_read=True)
    conn = storage.connect()
    try:
        before = [dict(r) for r in conn.execute("SELECT * FROM message_index")]
        cols = ", ".join(k for k in before[0] if k != "org_id")
        # Recreate the pre-#1979 table: a global PRIMARY KEY and no org_id.
        conn.executescript("DROP TABLE message_index;" + _LEGACY_TABLE + ";")
        names = cols.split(", ")
        conn.execute(
            f"INSERT INTO message_index ({cols}) VALUES ({', '.join('?' for _ in names)})",
            [before[0][k] for k in names],
        )
        conn.commit()
        message_index.ensure_schema(conn)
        conn.commit()
        info = {r["name"]: r["pk"] for r in conn.execute("PRAGMA table_info(message_index)")}
        after = [dict(r) for r in conn.execute("SELECT * FROM message_index")]
    finally:
        conn.close()
    assert info["message_id"] == 0
    assert "org_id" in info
    assert after == before
    # The rebuilt table takes the same id in another tenant.
    as_org(ORG_B)
    assert _send("b mail", message_id="old-a")["accepted"] is True


def test_rebuild_is_idempotent_and_keeps_uniqueness(as_org):
    as_org(ORG_A)
    _send(message_id="u-1")
    conn = storage.connect()
    try:
        message_index.ensure_schema(conn)
        message_index.ensure_schema(conn)
        conn.commit()
        row = dict(conn.execute("SELECT * FROM message_index").fetchone())
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO message_index (message_id, recipient, sender, act, "
                "conversation_id, event_ulid, created_at, org_id) VALUES (?,?,?,?,?,?,?,?)",
                ("u-1", "x", "y", "inform", "c", "other-ulid", row["created_at"], ORG_A),
            )
    finally:
        conn.close()
