"""Task-space (Discord/Telegram) worker contracts — build-only, token-free.

Every test drives stub gateways/recorded requests: no live Discord or
Telegram connection is ever opened (spec boundary). The round-trip test
covers the DoD path: inbound message → durable event → attributed outbound
reply, all in-process.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from unified_agent import events, storage
from unified_agent.messaging import schema as messaging_schema
from unified_agent.tools import tasks
from unified_agent.ts_worker import cursors as ts_cursors
from unified_agent.ts_worker import transport as ts_transport
from unified_agent.ts_worker import worker as ts_worker

OPERATOR_DISCORD_ID = "111111111111111111"
OPERATOR_TELEGRAM_ID = "222222222"
TARGETED = "[target: Desktop_Agent2] please review task 5"
VERB_TARGETED = "[target: Desktop_Agent2] /task verify wrapped routing"


def _init_isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    return tmp_path / "memory.db"


class StubDiscord(ts_transport.DiscordGateway):
    """Records requests; returns canned channel messages."""

    def __init__(self, messages: list[dict] | None = None):
        self.requests: list[ts_transport.TransportRequest] = []
        self.messages = messages or []
        self.sent: list[tuple[str, str]] = []

    def fetch_messages(self, channel_id, after, limit=10):  # noqa: ARG002
        self.requests.append(("fetch", channel_id, after))
        return [m for m in self.messages if ts_worker._snowflake(m.get("id")) > (after or 0)]

    def send_message(self, channel_id: str, content: str):
        self.requests.append(("send", channel_id, content))
        self.sent.append((channel_id, content))
        return {"id": "999", "channel_id": channel_id}


class StubTelegram(ts_transport.TelegramGateway):
    """Records requests; returns canned updates."""

    def __init__(self, updates: list[dict] | None = None):
        self.requests: list[ts_transport.TransportRequest] = []
        self.updates = updates or []
        self.sent: list[tuple[str, str]] = []

    def get_updates(self, offset=None, timeout_s=0):  # noqa: ARG002
        self.requests.append(("get_updates", offset))
        return [u for u in self.updates if int(u.get("update_id", 0)) >= (offset or 0)]

    def send_message(self, chat_id, text: str):
        self.requests.append(("send", chat_id, text))
        self.sent.append((str(chat_id), text))
        return {"ok": True, "result": {"message_id": 42}}


def _discord_message(mid: int, content: str, sender_id: str, *, bot: bool = False) -> dict:
    return {
        "id": str(mid),
        "content": content,
        "author": {"id": sender_id, "username": "operator", "bot": bot},
    }


def _telegram_update(uid: int, text: str, sender_id: str) -> dict:
    return {
        "update_id": uid,
        "message": {
            "message_id": uid,
            "text": text,
            "chat": {"id": int(sender_id)},
            "from": {"id": int(sender_id), "username": "operator"},
        },
    }


# ---------------------------------------------------------------------------
# cursors


def test_ts_cursor_migration_creates_and_reads_rows(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)

    assert ts_cursors.get_ts_cursor("discord", "chan-1") is None
    saved = ts_cursors.save_ts_cursor("discord", "chan-1", 101)

    assert saved["surface"] == "discord"
    assert saved["channel"] == "chan-1"
    assert saved["last_message_id"] == 101
    assert ts_cursors.get_ts_cursor("discord", "chan-1") == saved


def test_ts_cursor_never_moves_backwards(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)

    ts_cursors.save_ts_cursor("discord", "chan-1", 101)
    saved = ts_cursors.save_ts_cursor("discord", "chan-1", 99)

    assert saved["last_message_id"] == 101


def test_ts_cursors_schema_holds_no_credential_columns(tmp_path, monkeypatch):
    db_path = _init_isolated_db(tmp_path, monkeypatch)

    conn = sqlite3.connect(db_path)
    try:
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='ts_cursors'").fetchone()[0]
    finally:
        conn.close()
    assert "token" not in schema.lower()
    assert "credential" not in schema.lower()
    assert "secret" not in schema.lower()


# ---------------------------------------------------------------------------
# classify


def test_classify_missing_target_is_informational():
    decision = ts_worker.classify_target(
        "hello world", allowed_user_ids=(OPERATOR_DISCORD_ID,), sender_id=OPERATOR_DISCORD_ID
    )
    assert decision["target_reason"] == "missing-target"
    assert decision["actionable_for"] == []
    assert decision["authorized"] is False


def test_classify_invalid_target_is_informational():
    decision = ts_worker.classify_target(
        "[target: Nobody] hi",
        allowed_user_ids=(OPERATOR_DISCORD_ID,),
        sender_id=OPERATOR_DISCORD_ID,
    )
    assert decision["target_reason"] == "invalid-target"
    assert decision["actionable_for"] == []


def test_classify_unauthorized_sender_never_actionable():
    decision = ts_worker.classify_target(
        TARGETED, allowed_user_ids=(OPERATOR_DISCORD_ID,), sender_id="999999"
    )
    assert decision["target_reason"] == "unauthorized-sender"
    assert decision["actionable_for"] == []
    assert decision["authorized"] is False


def test_classify_authorized_explicit_target_is_actionable():
    decision = ts_worker.classify_target(
        TARGETED, allowed_user_ids=(OPERATOR_DISCORD_ID,), sender_id=OPERATOR_DISCORD_ID
    )
    assert decision["target_reason"] == "explicit-target"
    assert decision["actionable_for"] == ["Desktop_Agent2"]
    assert decision["authorized"] is True


# ---------------------------------------------------------------------------
# env gating


def test_surfaces_disabled_without_tokens(monkeypatch):
    for name in (
        "UA_TS_DISCORD_TOKEN",
        "UA_TS_TELEGRAM_TOKEN",
        "UA_TS_DISCORD_ALLOWED_USER_IDS",
        "UA_TS_TELEGRAM_ALLOWED_USER_IDS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert ts_worker.surface_enabled("discord") is False
    assert ts_worker.surface_enabled("telegram") is False
    assert ts_worker.enabled_surfaces() == ()


def test_surfaces_enabled_with_tokens(monkeypatch):
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_TELEGRAM_TOKEN", "tok2")
    assert ts_worker.surface_enabled("discord") is True
    assert ts_worker.surface_enabled("telegram") is True
    assert ts_worker.enabled_surfaces() == ("discord", "telegram")


def test_poll_discord_without_channels_is_misconfigured_not_crash(monkeypatch):
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.delenv("UA_TS_DISCORD_CHANNELS", raising=False)
    worker = ts_worker.TaskSpaceWorker(surfaces=("discord",))
    result = worker.poll_discord()
    assert result["status"] == "misconfigured"


def test_poll_disabled_surface_reports_disabled(monkeypatch):
    monkeypatch.delenv("UA_TS_DISCORD_TOKEN", raising=False)
    worker = ts_worker.TaskSpaceWorker(surfaces=("discord",))
    assert worker.poll_discord() == {
        "surface": "discord",
        "status": "disabled",
        "messages_processed": 0,
    }


# ---------------------------------------------------------------------------
# discord poll: durable write + cursor + bot filter


def test_discord_poll_persists_and_advances_cursor(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_DISCORD_CHANNELS", "chan-1")
    stub = StubDiscord([_discord_message(5, "hello", OPERATOR_DISCORD_ID)])
    worker = ts_worker.TaskSpaceWorker(surfaces=("discord",), discord=stub)

    result = worker.poll_discord()

    assert result["status"] == "healthy"
    assert result["messages_processed"] == 1
    rows = events.query_events(channels=[ts_worker.INBOUND_CHANNEL])
    assert len(rows) == 1
    assert rows[0]["dedupe_key"] == "ts:discord:5"
    assert rows[0]["payload"]["content"] == "hello"
    assert ts_cursors.get_ts_cursor("discord", "chan-1")["last_message_id"] == 5


def test_discord_poll_dedupes_replayed_message(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_DISCORD_CHANNELS", "chan-1")
    stub = StubDiscord([_discord_message(5, "hello", OPERATOR_DISCORD_ID)])
    worker = ts_worker.TaskSpaceWorker(surfaces=("discord",), discord=stub)

    worker.poll_discord()
    # Same message returned again (cursor ignored by stub): dedupe keeps one row.
    stub.messages = [_discord_message(5, "hello", OPERATOR_DISCORD_ID)]
    worker.poll_discord()

    rows = events.query_events(channels=[ts_worker.INBOUND_CHANNEL])
    assert len(rows) == 1


def test_discord_poll_skips_bot_messages(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_DISCORD_CHANNELS", "chan-1")
    stub = StubDiscord(
        [_discord_message(7, "[target: Desktop_Agent2] hi", OPERATOR_DISCORD_ID, bot=True)]
    )
    worker = ts_worker.TaskSpaceWorker(surfaces=("discord",), discord=stub)

    result = worker.poll_discord()

    assert result["messages_processed"] == 0
    assert events.query_events(channels=[ts_worker.INBOUND_CHANNEL]) == []


# ---------------------------------------------------------------------------
# round trip (DoD): inbound -> durable event -> attributed outbound reply


def test_roundtrip_inbound_authorized_target_produces_attributed_reply(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_DISCORD_ALLOWED_USER_IDS", OPERATOR_DISCORD_ID)
    monkeypatch.setenv("UA_TS_DISCORD_CHANNELS", "chan-1")
    replies: list[dict] = []

    def fake_send(**kwargs):
        replies.append(kwargs)
        return {"accepted": True, "message_id": "m1"}

    stub = StubDiscord([_discord_message(11, TARGETED, OPERATOR_DISCORD_ID)])
    worker = ts_worker.TaskSpaceWorker(surfaces=("discord",), discord=stub, reply_sender=fake_send)

    result = worker.poll_discord()

    assert result["status"] == "healthy"
    # 1. durable inbound record
    rows = events.query_events(channels=[ts_worker.INBOUND_CHANNEL])
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["actionable_for"] == ["Desktop_Agent2"]
    assert payload["authorized"] is True
    # 2. attributed outbound reply recorded via messaging.send
    assert len(replies) == 1
    reply = replies[0]
    assert reply["act"] == messaging_schema.MessageAct.INFORM.value
    assert reply["to_agent"] == "Desktop_Agent2"
    assert reply["from_agent"] == ts_worker.WORKER_AGENT_LABEL
    assert reply["body"] == (
        f"[{ts_worker.WORKER_AGENT_LABEL}] ack: received your request targeted at "
        "'Desktop_Agent2' via discord. "
        "Messages may start with [target: <agent-label>]"
    )
    # 3. reply actually posted back to the chat
    assert stub.sent == [("chan-1", reply["body"])]


# ---------------------------------------------------------------------------
# bounded verbs: authenticated target -> wrapped tool -> durable audit


def test_discord_task_verb_routes_through_registered_tools_and_records_outcome(
    tmp_path, monkeypatch
):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_DISCORD_ALLOWED_USER_IDS", OPERATOR_DISCORD_ID)
    monkeypatch.setenv("UA_TS_DISCORD_CHANNELS", "chan-1")
    from unified_agent import server

    wrapped = server.REGISTERED_TOOLS["task_add"]
    calls: list[dict] = []

    def spy(**kwargs):
        calls.append(kwargs)
        return wrapped(**kwargs)

    monkeypatch.setitem(server.REGISTERED_TOOLS, "task_add", spy)
    stub = StubDiscord([_discord_message(14, VERB_TARGETED, OPERATOR_DISCORD_ID)])
    worker = ts_worker.TaskSpaceWorker(
        surfaces=("discord",), discord=stub, reply_sender=lambda **kwargs: kwargs
    )

    assert worker.poll_discord()["messages_processed"] == 1
    assert calls[0]["title"] == "verify wrapped routing"
    assert len(tasks.task_list(status="all")["tasks"]) == 1
    audit = [
        row["payload"]
        for row in events.query_events(channels=[ts_worker.INBOUND_CHANNEL])
        if row["payload"].get("outcome") == "succeeded"
    ]
    assert audit == [
        {
            "source": "discord",
            "message_id": "14",
            "channel_id": "chan-1",
            "verb": "task",
            "actor": OPERATOR_DISCORD_ID,
            "outcome": "succeeded",
        }
    ]


@pytest.mark.parametrize(
    ("command", "tool_name"),
    [
        ("/assign Desktop_Agent2 inspect this", "delegate"),
        ("/progress task-1 started", "task_update"),
        ("/blocker task-1 waiting", "task_update"),
        ("/review task-1 ready", "task_update"),
    ],
)
def test_discord_mutating_verbs_route_through_registered_tools(
    tmp_path, monkeypatch, command, tool_name
):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_DISCORD_ALLOWED_USER_IDS", OPERATOR_DISCORD_ID)
    monkeypatch.setenv("UA_TS_DISCORD_CHANNELS", "chan-1")
    from unified_agent import server

    if tool_name == "task_update":
        tasks.task_add(title="existing task", notes="[review: no-merge]")
    wrapped = server.REGISTERED_TOOLS[tool_name]
    calls: list[dict] = []

    def spy(**kwargs):
        calls.append(kwargs)
        return wrapped(**kwargs)

    monkeypatch.setitem(server.REGISTERED_TOOLS, tool_name, spy)
    stub = StubDiscord(
        [_discord_message(15, f"[target: Desktop_Agent2] {command}", OPERATOR_DISCORD_ID)]
    )
    worker = ts_worker.TaskSpaceWorker(
        surfaces=("discord",), discord=stub, reply_sender=lambda **kwargs: kwargs
    )

    worker.poll_discord()

    assert len(calls) == 1


@pytest.mark.parametrize(
    ("content", "sender_id", "reason"),
    [
        ("[target: Desktop_Agent2] /task forbidden", "999999", "unauthorized-sender"),
        ("[target: Desktop_Agent2] /task", OPERATOR_DISCORD_ID, "requires text"),
    ],
)
def test_discord_refused_verb_replies_records_and_never_mutates(
    tmp_path, monkeypatch, content, sender_id, reason
):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_DISCORD_ALLOWED_USER_IDS", OPERATOR_DISCORD_ID)
    monkeypatch.setenv("UA_TS_DISCORD_CHANNELS", "chan-1")
    replies: list[dict] = []
    stub = StubDiscord([_discord_message(16, content, sender_id)])
    worker = ts_worker.TaskSpaceWorker(
        surfaces=("discord",), discord=stub, reply_sender=lambda **kwargs: replies.append(kwargs)
    )

    worker.poll_discord()

    assert tasks.task_list(status="all")["tasks"] == []
    assert len(replies) == 1
    assert "refused" in replies[0]["body"]
    assert stub.sent == [("chan-1", replies[0]["body"])]
    audits = [
        row["payload"]
        for row in events.query_events(channels=[ts_worker.INBOUND_CHANNEL])
        if row["payload"].get("outcome") == "refused"
    ]
    assert len(audits) == 1
    assert audits[0]["verb"] == "task"
    assert audits[0]["actor"] == sender_id
    assert reason in audits[0]["reason"]


def test_roundtrip_unauthorized_sender_gets_no_reply(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_DISCORD_ALLOWED_USER_IDS", OPERATOR_DISCORD_ID)
    monkeypatch.setenv("UA_TS_DISCORD_CHANNELS", "chan-1")
    replies: list[dict] = []
    stub = StubDiscord([_discord_message(12, TARGETED, "999999")])
    worker = ts_worker.TaskSpaceWorker(
        surfaces=("discord",), discord=stub, reply_sender=lambda **kw: replies.append(kw)
    )

    result = worker.poll_discord()

    assert result["messages_processed"] == 1
    assert replies == []
    assert stub.sent == []


def test_self_target_ack_is_recorded_without_a_self_message_and_posted(tmp_path, monkeypatch):
    """#2290: `[target: <worker label>] ping` must still reach chat."""
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_DISCORD_CHANNELS", "chan-1")
    monkeypatch.setenv("UA_TS_DISCORD_ALLOWED_USER_IDS", OPERATOR_DISCORD_ID)
    from unified_agent import config

    label = ts_worker.WORKER_AGENT_LABEL
    monkeypatch.setattr(config, "fleet_agent_labels", lambda: frozenset({label, "Desktop_Agent2"}))
    replies: list[dict] = []
    stub = StubDiscord([_discord_message(15, f"[target: {label}] ping", OPERATOR_DISCORD_ID)])
    worker = ts_worker.TaskSpaceWorker(
        surfaces=("discord",), discord=stub, reply_sender=lambda **kw: replies.append(kw)
    )

    worker.poll_discord()

    assert replies == []  # no inter-agent self-message (send.py refuses it)
    assert len(stub.sent) == 1
    assert "ack: received your request" in stub.sent[0][1]
    kinds = [
        r["payload"].get("kind") for r in events.query_events(channels=[ts_worker.INBOUND_CHANNEL])
    ]
    assert "self-ack" in kinds


def test_refused_verb_with_invalid_target_still_posts_to_chat(tmp_path, monkeypatch):
    """The refusal reply targets the worker itself; it used to be dropped (#2290)."""
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_DISCORD_CHANNELS", "chan-1")
    monkeypatch.setenv("UA_TS_DISCORD_ALLOWED_USER_IDS", OPERATOR_DISCORD_ID)
    replies: list[dict] = []
    stub = StubDiscord([_discord_message(17, "[target: nobody] /task x", OPERATOR_DISCORD_ID)])
    worker = ts_worker.TaskSpaceWorker(
        surfaces=("discord",), discord=stub, reply_sender=lambda **kw: replies.append(kw)
    )

    worker.poll_discord()

    assert replies == []
    assert len(stub.sent) == 1
    assert "command refused: invalid-target" in stub.sent[0][1]


def test_assign_to_the_worker_itself_gets_a_clear_refusal(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_DISCORD_CHANNELS", "chan-1")
    monkeypatch.setenv("UA_TS_DISCORD_ALLOWED_USER_IDS", OPERATOR_DISCORD_ID)
    label = ts_worker.WORKER_AGENT_LABEL
    stub = StubDiscord(
        [
            _discord_message(
                16, f"[target: Desktop_Agent2] /assign {label} do it", OPERATOR_DISCORD_ID
            )
        ]
    )
    worker = ts_worker.TaskSpaceWorker(
        surfaces=("discord",), discord=stub, reply_sender=lambda **kw: kw
    )

    worker.poll_discord()

    assert len(stub.sent) == 1
    assert "cannot name the chat worker itself" in stub.sent[0][1]


def test_reply_record_failure_does_not_break_ingestion(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_DISCORD_CHANNELS", "chan-1")
    monkeypatch.setenv("UA_TS_DISCORD_ALLOWED_USER_IDS", OPERATOR_DISCORD_ID)
    stub = StubDiscord([_discord_message(13, TARGETED, OPERATOR_DISCORD_ID)])

    def exploding_send(**kwargs):
        raise ValueError("unknown agent label")

    worker = ts_worker.TaskSpaceWorker(
        surfaces=("discord",), discord=stub, reply_sender=exploding_send
    )
    result = worker.poll_discord()
    assert result["status"] == "healthy"
    assert result["messages_processed"] == 1


# ---------------------------------------------------------------------------
# telegram


def test_telegram_roundtrip_with_offset_cursor(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_TS_TELEGRAM_TOKEN", "tok")
    monkeypatch.setenv("UA_TS_TELEGRAM_ALLOWED_USER_IDS", OPERATOR_TELEGRAM_ID)
    replies: list[dict] = []
    stub = StubTelegram([_telegram_update(21, TARGETED, OPERATOR_TELEGRAM_ID)])
    worker = ts_worker.TaskSpaceWorker(
        surfaces=("telegram",), telegram=stub, reply_sender=lambda **kw: replies.append(kw)
    )

    result = worker.poll_telegram()

    assert result["status"] == "healthy"
    assert result["messages_processed"] == 1
    rows = events.query_events(channels=[ts_worker.INBOUND_CHANNEL])
    assert rows[0]["dedupe_key"] == "ts:telegram:21"
    assert rows[0]["payload"]["sender_id"] == OPERATOR_TELEGRAM_ID
    assert len(replies) == 1
    assert stub.sent  # ack posted back to the chat
    # offset cursor advanced past the handled update
    assert ts_cursors.get_ts_cursor("telegram", "*")["last_message_id"] == 21


def test_telegram_transport_error_is_health_not_crash(monkeypatch):
    monkeypatch.setenv("UA_TS_TELEGRAM_TOKEN", "tok")

    class Boom(ts_transport.TelegramGateway):
        def get_updates(self, offset=None, timeout_s=0):
            raise RuntimeError("network down")

    worker = ts_worker.TaskSpaceWorker(surfaces=("telegram",), telegram=Boom())
    result = worker.poll_telegram()
    assert result["status"] == "error"
    assert "RuntimeError" in result["error"]


# ---------------------------------------------------------------------------
# transport: token handling never lands in payloads/logs


def test_discord_gateway_uses_bearer_header_without_logging_token():
    calls: list[ts_transport.TransportRequest] = []

    class RecordingTransport(ts_transport.TsBotTransport):
        def request(self, req):
            calls.append(req)
            return {"raw": []}

    gateway = ts_transport.DiscordGateway(transport=RecordingTransport())
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("UA_TS_DISCORD_TOKEN", "super-secret-token")
        gateway.fetch_messages("chan-1", after=None)

    req = calls[0]
    assert req.headers["Authorization"] == "Bot super-secret-token"
    # The token lives only in the auth header, never in params or body.
    assert "super-secret-token" not in json.dumps(req.params)
    assert req.json_body is None


def test_transport_request_body_never_contains_token(monkeypatch):
    monkeypatch.setenv("UA_TS_DISCORD_TOKEN", "super-secret-token")
    calls: list[ts_transport.TransportRequest] = []

    class RecordingTransport(ts_transport.TsBotTransport):
        def request(self, req):
            calls.append(req)
            return {"id": "1"}

    gateway = ts_transport.DiscordGateway(transport=RecordingTransport())
    gateway.send_message("chan-1", "hello")

    req = calls[0]
    assert req.json_body == {"content": "hello"}
    assert "super-secret-token" not in json.dumps(req.json_body)


# ---------------------------------------------------------------------------
# worker loop mechanics


def test_worker_rejects_unknown_surface():
    try:
        ts_worker.TaskSpaceWorker(surfaces=("irc",))
    except ValueError as exc:
        assert "irc" in str(exc)
    else:
        raise AssertionError("expected ValueError for unsupported surface")


def test_poll_once_aggregates_disabled_surfaces(monkeypatch):
    monkeypatch.delenv("UA_TS_DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("UA_TS_TELEGRAM_TOKEN", raising=False)
    worker = ts_worker.TaskSpaceWorker()
    result = worker.poll_once()
    assert result["status"] == "disabled"
    assert result["messages_processed"] == 0
    assert len(result["results"]) == 2


def test_env_int_clamps_above_maximum_and_warns(monkeypatch, caplog):
    monkeypatch.setenv("UA_TS_FETCH_LIMIT", "60")
    with caplog.at_level("WARNING", logger=ts_worker.log.name):
        value = ts_worker._env_int("UA_TS_FETCH_LIMIT", default=10, maximum=50)
    assert value == 50
    assert "UA_TS_FETCH_LIMIT" in caplog.text


@pytest.mark.parametrize(
    ("raw", "expected"), [("0", 10), ("-3", 10), ("abc", 10), ("25", 25), ("50", 50)]
)
def test_env_int_fallback_and_in_range(monkeypatch, raw, expected):
    monkeypatch.setenv("UA_TS_FETCH_LIMIT", raw)
    assert ts_worker._env_int("UA_TS_FETCH_LIMIT", default=10, maximum=50) == expected


def test_main_clamps_oversized_fetch_limit_instead_of_crashing(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_TS_FETCH_LIMIT", "60")
    seen: dict[str, int] = {}

    class _Stub(ts_worker.TaskSpaceWorker):
        def run_forever(self) -> None:
            seen["fetch_limit"] = self.fetch_limit

    monkeypatch.setattr(ts_worker, "TaskSpaceWorker", _Stub)
    ts_worker.main()
    assert seen["fetch_limit"] == 50


def test_worker_label_is_an_always_on_identity():
    """The unattended worker speaks as an always-on hub identity (task #1879)."""
    from unified_agent import profiles

    assert ts_worker.WORKER_AGENT_LABEL in profiles.ALWAYS_ON_HERMES_IDENTITIES
