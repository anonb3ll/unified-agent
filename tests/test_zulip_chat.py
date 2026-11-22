"""Live Zulip chat reply contracts."""

from unified_agent import storage, zulip_chat


def _init_isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    return tmp_path / "memory.db"


def test_chat_disabled_by_default(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.delenv("UA_ZULIP_CHAT", raising=False)
    assert zulip_chat.chat_enabled() is False
    result = zulip_chat.maybe_reply(
        "Desktop_Agent4",
        {
            "id": 1,
            "channel": "requests",
            "topic": "chat",
            "content": "[target: Desktop_Agent4] hello",
            "sender_full_name": "Operator",
        },
    )
    assert result == {"replied": False, "reason": "chat-disabled"}


def test_resolve_explicit_target_and_topic_continuity(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_ZULIP_CHAT", "1")

    assert (
        zulip_chat.resolve_reply_agent(
            "Desktop_Agent4",
            content="[target: Desktop_Agent4] hi",
            channel="requests",
            topic="live",
        )["should_reply"]
        is True
    )

    assert (
        zulip_chat.resolve_reply_agent(
            "Desktop_Agent2",
            content="[target: Desktop_Agent4] hi",
            channel="requests",
            topic="live",
        )["should_reply"]
        is False
    )

    assert (
        zulip_chat.resolve_reply_agent(
            "Desktop_Agent5",
            content="[broadcast] everyone",
            channel="requests",
            topic="live",
        )["reason"]
        == "broadcast-no-auto-reply"
    )

    storage.bind_zulip_chat_topic("requests", "live", "Desktop_Agent4")
    assert (
        zulip_chat.resolve_reply_agent(
            "Desktop_Agent4",
            content="follow up without marker",
            channel="requests",
            topic="live",
        )["reason"]
        == "topic-continuity"
    )


def test_mention_routing(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    decision = zulip_chat.resolve_reply_agent(
        "Desktop_Agent5",
        content="@**Desktop_Agent5** status?",
        channel="coord",
        topic="ops-check",
    )
    assert decision["should_reply"] is True
    assert decision["reason"] == "mention"


def test_maybe_reply_sends_once_and_binds_topic(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_ZULIP_CHAT", "1")
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")  # an approved rollback re-opens Zulip
    sends: list[tuple[str, str, str, str]] = []

    def complete(prompt, system=None, **_kwargs):
        assert "Desktop_Agent4" in (system or "")
        assert "hello there" in prompt
        return {"text": "Hello Operator — I am here.", "provider": "test", "model": "test"}

    def send(agent, channel, topic, content):
        sends.append((agent, channel, topic, content))
        return {
            "sent": True,
            "message_id": 9001,
            "agent": agent,
            "channel": channel,
            "topic": topic,
        }

    def fetch(*_args, **_kwargs):
        return {"received": True, "messages": []}

    message = {
        "id": 42,
        "channel": "requests",
        "topic": "live-chat",
        "content": "[target: Desktop_Agent4] hello there",
        "sender_full_name": "Operator",
        "sender_email": "operator@example.com",
    }
    first = zulip_chat.maybe_reply(
        "Desktop_Agent4", message, complete=complete, send=send, fetch=fetch
    )
    second = zulip_chat.maybe_reply(
        "Desktop_Agent4", message, complete=complete, send=send, fetch=fetch
    )

    assert first["replied"] is True
    assert first["reply_message_id"] == 9001
    assert second == {"replied": False, "reason": "already-claimed"}
    assert len(sends) == 1
    assert sends[0][0] == "Desktop_Agent4"
    assert "chat reply" in sends[0][3]
    bound = storage.get_zulip_chat_topic("requests", "live-chat")
    assert bound is not None
    assert bound["agent_label"] == "Desktop_Agent4"
    row = storage.get_zulip_chat_reply(42)
    assert row is not None
    assert row["status"] == "sent"


def test_chat_claim_is_atomic(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    assert storage.claim_zulip_chat_reply(
        7, agent_label="Desktop_Agent4", channel="requests", topic="t"
    )
    assert not storage.claim_zulip_chat_reply(
        7, agent_label="Desktop_Agent2", channel="requests", topic="t"
    )


def test_html_wrapped_target_still_routes(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    decision = zulip_chat.resolve_reply_agent(
        "Desktop_Agent4",
        content="<p>[target: Desktop_Agent4] hello from zulip html</p>",
        channel="requests",
        topic="live",
    )
    assert decision["should_reply"] is True
    assert decision["reason"] == "explicit-target"


def test_history_excludes_newer_messages():
    history = zulip_chat._history_before(
        [
            {"id": 10, "content": "old"},
            {"id": 20, "content": "current-peer"},
            {"id": 30, "content": "newer"},
        ],
        before_message_id=20,
    )
    assert [item["id"] for item in history] == [10]


def test_strip_zulip_markup():
    assert zulip_chat.strip_zulip_markup("<p>Hello &amp; world</p>") == "Hello & world"


def test_retired_zulip_makes_no_model_call_and_claims_nothing(tmp_path, monkeypatch):
    """Task #1714: with Zulip retired, a routed message used to claim a reply
    row and call the model before the send was refused; the failed reply was
    billable and uncounted by the hourly cap (which counts only 'sent')."""
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_ZULIP_CHAT", "1")
    monkeypatch.delenv("UA_ZULIP_RETIRED", raising=False)  # unset = retired (fail closed)

    def complete(*_args, **_kwargs):
        raise AssertionError("the model must not be called while Zulip is retired")

    def send(*_args, **_kwargs):
        raise AssertionError("nothing may be sent while Zulip is retired")

    def fetch(*_args, **_kwargs):
        raise AssertionError("history must not be fetched while Zulip is retired")

    message = {
        "id": 77,
        "channel": "requests",
        "topic": "live-chat",
        "content": "[target: Desktop_Agent4] hello",
        "sender_full_name": "Operator",
    }
    result = zulip_chat.maybe_reply(
        "Desktop_Agent4", message, complete=complete, send=send, fetch=fetch
    )
    assert result == {"replied": False, "reason": "zulip-retired"}
    assert storage.get_zulip_chat_reply(77) is None


def test_doctor_reports_chat_inert_while_zulip_is_retired(monkeypatch):
    """Independent review of PR #949: doctor called chat LIVE on a retired hub."""
    from unified_agent.tools import doctor

    monkeypatch.setattr(doctor, "_service_env", lambda unit: None)
    monkeypatch.setenv("UA_ZULIP_CHAT", "1")

    monkeypatch.delenv("UA_ZULIP_RETIRED", raising=False)  # unset = retired
    row = doctor._zulip_chat_check()
    assert row["status"] == "DORMANT" and "retired" in row["detail"]

    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")
    assert doctor._zulip_chat_check()["status"] == "LIVE"

    monkeypatch.setenv("UA_ZULIP_CHAT", "0")
    assert doctor._zulip_chat_check()["status"] == "DORMANT"


def test_doctor_uses_the_worker_unit_env_for_retirement(monkeypatch):
    """Devin on #949: a worker unit env without UA_ZULIP_RETIRED is retired,
    even when the doctor process itself has UA_ZULIP_RETIRED=0."""
    from unified_agent.tools import doctor

    monkeypatch.setattr(doctor, "_service_env", lambda unit: {"UA_ZULIP_CHAT": "1"})
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")
    assert doctor._zulip_chat_check()["status"] == "DORMANT"
