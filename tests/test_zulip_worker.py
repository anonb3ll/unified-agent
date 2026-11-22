"""Durable Zulip polling contracts."""

import sqlite3

from unified_agent import events, storage, zulip_worker


def _init_isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    return tmp_path / "memory.db"


def test_zulip_cursor_migration_creates_and_reads_rows(tmp_path, monkeypatch):
    db_path = _init_isolated_db(tmp_path, monkeypatch)

    assert storage.get_zulip_cursor("Desktop_Agent2", "coord") is None
    saved = storage.save_zulip_cursor("Desktop_Agent2", "coord", 101)

    assert saved["agent_label"] == "Desktop_Agent2"
    assert saved["channel"] == "coord"
    assert saved["last_message_id"] == 101
    assert storage.get_zulip_cursor("Desktop_Agent2", "coord") == saved

    conn = sqlite3.connect(db_path)
    try:
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='zulip_cursors'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "api_key" not in schema.lower()
    assert "credential" not in schema.lower()


def test_zulip_cursors_are_isolated_by_agent_and_channel(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)

    storage.save_zulip_cursor("Desktop_Agent2", "coord", 101)
    storage.save_zulip_cursor("Desktop_Agent2", "review", 202)
    storage.save_zulip_cursor("Desktop_Agent1", "coord", 303)

    assert storage.get_zulip_cursor("Desktop_Agent2", "coord")["last_message_id"] == 101
    assert storage.get_zulip_cursor("Desktop_Agent2", "review")["last_message_id"] == 202
    assert storage.get_zulip_cursor("Desktop_Agent1", "coord")["last_message_id"] == 303
    assert storage.get_zulip_cursor("Desktop_Agent1", "review") is None


def test_zulip_cursor_never_moves_backwards(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)

    storage.save_zulip_cursor("Desktop_Agent2", "coord", 101)
    saved = storage.save_zulip_cursor("Desktop_Agent2", "coord", 99)

    assert saved["last_message_id"] == 101
    assert storage.get_zulip_cursor("Desktop_Agent2", "coord")["last_message_id"] == 101


def test_zulip_event_dedupe_key_returns_existing_event(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)

    first = events.record_event(
        "zulip.inbound",
        payload={"message_id": 101, "content": "first"},
        dedupe_key="zulip:101",
        notify=False,
    )
    second = events.record_event(
        "zulip.inbound",
        payload={"message_id": 101, "content": "replayed"},
        dedupe_key="zulip:101",
        notify=False,
    )

    assert second == first
    rows = events.query_events(channels=["zulip.inbound"])
    assert len(rows) == 1
    assert rows[0]["dedupe_key"] == "zulip:101"
    assert rows[0]["payload"]["content"] == "first"


def test_worker_defaults_cover_only_the_five_bots_and_allowlisted_channels():
    worker = zulip_worker.ZulipPollingWorker()

    assert worker.agents == (
        "Desktop_Agent5",
        "Desktop_Agent1",
        "Desktop_Agent2",
        "Desktop_agent3",
        "Desktop_Agent4",
    )
    assert worker.channels == ("ops", "requests", "coord", "review", "meta")


def _message(message_id: int, content: str) -> dict[str, object]:
    return {
        "id": message_id,
        "sender_email": "desktop-agent5-bot@example.zulipchat.com",
        "sender_full_name": "Desktop_Agent5",
        "type": "stream",
        "channel": "coord",
        "topic": "worker",
        "content": content,
        "timestamp": 1_700_000_000 + message_id,
    }


def _worker(*, agents=("Desktop_Agent2",), channels=("coord",)):
    return zulip_worker.ZulipPollingWorker(agents=agents, channels=channels)


def test_worker_persists_targeted_and_informational_messages(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(storage, "now", lambda: "2026-08-21T12:00:00+00:00")
    calls: list[dict[str, object]] = []

    def fetch(agent_label, channel, *, anchor, limit):
        calls.append({"agent": agent_label, "channel": channel, "anchor": anchor, "limit": limit})
        return {
            "received": True,
            "messages": [
                _message(101, "[target: Desktop_Agent2] handle this"),
                _message(102, "[target: Desktop_Agent1] for another bot"),
                _message(103, "no target, keep informational"),
                _message(104, "[broadcast] share with every bot"),
            ],
            "next_anchor": 104,
        }

    monkeypatch.setattr(zulip_worker.zulip, "zulip_fetch_messages", fetch)
    worker = _worker()

    result = worker.poll_once(agent_label="Desktop_Agent2", channel="coord")

    assert result["status"] == "healthy"
    assert result["messages_processed"] == 4
    assert calls == [
        {"agent": "Desktop_Agent2", "channel": "coord", "anchor": "newest", "limit": 100}
    ]
    assert storage.get_zulip_cursor("Desktop_Agent2", "coord")["last_message_id"] == 104

    payloads = {
        event["payload"]["message_id"]: event["payload"]
        for event in events.query_events(channels=["zulip.inbound"])
    }
    assert payloads[101]["actionable_for"] == ["Desktop_Agent2"]
    assert payloads[102]["actionable_for"] == ["Desktop_Agent1"]
    assert payloads[103]["actionable_for"] == []
    assert payloads[103]["target_reason"] == "missing-target"
    assert payloads[104]["actionable_for"] == list(zulip_worker.BOT_IDENTITIES)


def test_worker_does_not_advance_cursor_when_event_persistence_fails(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(storage, "now", lambda: "2026-08-21T12:00:00+00:00")
    monkeypatch.setattr(
        zulip_worker.zulip,
        "zulip_fetch_messages",
        lambda *_args, **_kwargs: {
            "received": True,
            "messages": [_message(101, "[target: Desktop_Agent2] first"), _message(102, "second")],
            "next_anchor": 102,
        },
    )
    real_record = events.record_event

    def record(channel, *, payload, **kwargs):
        if payload["message_id"] == 102:
            raise RuntimeError("event log unavailable")
        return real_record(channel, payload=payload, **kwargs)

    monkeypatch.setattr(zulip_worker.events, "record_event", record)
    worker = _worker()

    result = worker.poll_once(agent_label="Desktop_Agent2", channel="coord")

    assert result["status"] == "error"
    assert storage.get_zulip_cursor("Desktop_Agent2", "coord")["last_message_id"] == 101
    health = worker.health_payload()["channels"][0]
    assert health["last_error"] == "event log unavailable"


def test_worker_retries_failed_message_without_duplicate_prior_event(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(storage, "now", lambda: "2026-08-21T12:00:00+00:00")
    monkeypatch.setattr(
        zulip_worker.zulip,
        "zulip_fetch_messages",
        lambda *_args, **_kwargs: {
            "received": True,
            "messages": [_message(101, "first"), _message(102, "second")],
            "next_anchor": 102,
        },
    )
    real_record = events.record_event
    failed = True

    def record(channel, *, payload, **kwargs):
        nonlocal failed
        if payload["message_id"] == 102 and failed:
            failed = False
            raise RuntimeError("event log unavailable")
        return real_record(channel, payload=payload, **kwargs)

    monkeypatch.setattr(zulip_worker.events, "record_event", record)
    worker = _worker()

    first = worker.poll_once(agent_label="Desktop_Agent2", channel="coord")
    second = worker.poll_once(agent_label="Desktop_Agent2", channel="coord")

    assert first["status"] == "error"
    assert second["status"] == "healthy"
    assert storage.get_zulip_cursor("Desktop_Agent2", "coord")["last_message_id"] == 102
    assert len(events.query_events(channels=["zulip.inbound"])) == 2


def test_worker_api_failure_does_not_advance_cursor_and_records_backoff(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(storage, "now", lambda: "2026-08-21T12:00:00+00:00")
    monkeypatch.setattr(
        zulip_worker.zulip,
        "zulip_fetch_messages",
        lambda *_args, **_kwargs: {
            "received": False,
            "error": "Zulip API request failed",
        },
    )
    worker = zulip_worker.ZulipPollingWorker(
        agents=("Desktop_Agent2",), channels=("coord",), retry_base_s=2, retry_max_s=5
    )

    result = worker.poll_once(agent_label="Desktop_Agent2", channel="coord")

    assert result["status"] == "error"
    assert storage.get_zulip_cursor("Desktop_Agent2", "coord") is None
    health = worker.health_payload()["channels"][0]
    assert health["status"] == "error"
    assert health["consecutive_errors"] == 1
    assert health["next_retry_at"] == "2026-08-21T12:00:02+00:00"
    assert zulip_worker.bounded_retry_delay(1, base_s=2, max_s=5) == 2
    assert zulip_worker.bounded_retry_delay(2, base_s=2, max_s=5) == 4
    assert zulip_worker.bounded_retry_delay(3, base_s=2, max_s=5) == 5


def test_new_worker_honors_persisted_retry_deadline(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(storage, "now", lambda: "2026-08-21T12:00:00+00:00")
    storage.record_zulip_worker_error(
        "Desktop_Agent2",
        "coord",
        error="temporary outage",
        next_retry_at="2026-08-21T12:00:05+00:00",
    )
    worker = zulip_worker.ZulipPollingWorker(agents=("Desktop_Agent2",), channels=("coord",))

    assert worker._retry_is_due("Desktop_Agent2", "coord") is False
    monkeypatch.setattr(storage, "now", lambda: "2026-08-21T12:00:05+00:00")
    assert worker._retry_is_due("Desktop_Agent2", "coord") is True


def test_worker_health_reports_stale_success(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setenv("UA_ZULIP_RETIRED", "0")
    monkeypatch.setattr(storage, "now", lambda: "2026-08-21T12:00:00+00:00")
    monkeypatch.setattr(
        zulip_worker.zulip,
        "zulip_fetch_messages",
        lambda *_args, **_kwargs: {"received": True, "messages": [], "next_anchor": None},
    )
    worker = zulip_worker.ZulipPollingWorker(
        agents=("Desktop_Agent2",), channels=("coord",), poll_interval_s=10
    )
    worker.poll_once(agent_label="Desktop_Agent2", channel="coord")

    health = worker.health_payload(now="2026-08-21T12:01:00+00:00")

    assert health["status"] == "stale"
    assert health["channels"][0]["status"] == "stale"
    assert health["channels"][0]["cursor_age_seconds"] is None


def test_worker_health_is_retired_when_the_gate_is_unset(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.delenv("UA_ZULIP_RETIRED", raising=False)
    storage.record_zulip_worker_success(
        "Desktop_Agent2",
        "coord",
        messages_processed=0,
        timestamp="2026-08-21T11:00:00+00:00",
    )
    worker = zulip_worker.ZulipPollingWorker(
        agents=("Desktop_Agent2",), channels=("coord",), poll_interval_s=10
    )

    health = worker.health_payload(now="2026-08-21T12:01:00+00:00")

    assert health["status"] == "retired"


def test_classify_request_requires_explicit_target_and_task_id():
    targeted = zulip_worker.classify_target("[target: Desktop_Agent2] task-521 review this")
    request = zulip_worker.classify_request(
        "[target: Desktop_Agent2] task-521 review this", targeted
    )
    assert request == {
        "request_kind": "reviewable",
        "request_reason": "explicit-target-and-task-id",
        "task_id": 521,
    }
    missing = zulip_worker.classify_request("[target: Desktop_Agent2] do this", targeted)
    assert missing["request_kind"] == "informational"


def test_event_queue_initialization_backfills_and_persists_state(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(storage, "now", lambda: "2026-08-21T12:00:00+00:00")
    monkeypatch.setattr(
        zulip_worker.zulip,
        "zulip_register_event_queue",
        lambda *_args, **_kwargs: {
            "registered": True,
            "queue_id": "q-1",
            "last_event_id": 10,
        },
    )
    monkeypatch.setattr(
        zulip_worker.zulip,
        "zulip_fetch_messages",
        lambda *_args, **_kwargs: {
            "received": True,
            "messages": [_message(101, "[target: Desktop_Agent2] task-521 inspect")],
            "next_anchor": 101,
        },
    )
    worker = _worker()

    result = worker.poll_once(agent_label="Desktop_Agent2")

    assert result["status"] == "healthy"
    assert result["messages_processed"] == 1
    assert storage.get_zulip_queue("Desktop_Agent2")["queue_id"] == "q-1"
    payload = events.query_events(channels=["zulip.inbound"])[0]["payload"]
    assert payload["request_kind"] == "reviewable"
    assert payload["task_id"] == 521


def test_event_queue_backfill_failure_clears_queue_id(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(storage, "now", lambda: "2026-08-21T12:00:00+00:00")
    monkeypatch.setattr(
        zulip_worker.zulip,
        "zulip_register_event_queue",
        lambda *_args, **_kwargs: {
            "registered": True,
            "queue_id": "q-1",
            "last_event_id": 10,
        },
    )
    monkeypatch.setattr(
        zulip_worker.zulip,
        "zulip_fetch_messages",
        lambda *_args, **_kwargs: {
            "received": False,
            "error": "REST backfill failed",
        },
    )

    result = _worker().poll_once(agent_label="Desktop_Agent2")

    assert result["status"] == "error"
    queue = storage.get_zulip_queue("Desktop_Agent2")
    assert queue["queue_id"] is None
    assert queue["status"] == "error"


def test_event_queue_backfill_failures_increase_retry_delay(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    monkeypatch.setattr(storage, "now", lambda: "2026-08-21T12:00:00+00:00")
    monkeypatch.setattr(
        zulip_worker.zulip,
        "zulip_register_event_queue",
        lambda *_args, **_kwargs: {
            "registered": True,
            "queue_id": "q-1",
            "last_event_id": 10,
        },
    )
    monkeypatch.setattr(
        zulip_worker.zulip,
        "zulip_fetch_messages",
        lambda *_args, **_kwargs: {"received": False, "error": "backfill failed"},
    )
    worker = zulip_worker.ZulipPollingWorker(
        agents=("Desktop_Agent2",),
        channels=("coord",),
        retry_base_s=2,
        retry_max_s=30,
    )

    first = worker.poll_once(agent_label="Desktop_Agent2")
    worker._queue_retry_until.clear()
    second = worker.poll_once(agent_label="Desktop_Agent2")

    assert first["next_retry_at"] == "2026-08-21T12:00:02+00:00"
    assert second["next_retry_at"] == "2026-08-21T12:00:04+00:00"


def test_event_queue_reset_re_registers_then_rest_backfills(tmp_path, monkeypatch):
    _init_isolated_db(tmp_path, monkeypatch)
    storage.save_zulip_queue("Desktop_Agent2", "q-old", 20)
    monkeypatch.setattr(storage, "now", lambda: "2026-08-21T12:00:00+00:00")
    register_calls = []

    def register(*_args, **_kwargs):
        register_calls.append(True)
        return {"registered": True, "queue_id": "q-new", "last_event_id": 30}

    monkeypatch.setattr(zulip_worker.zulip, "zulip_register_event_queue", register)
    monkeypatch.setattr(
        zulip_worker.zulip,
        "zulip_fetch_events",
        lambda *_args, **_kwargs: {
            "received": False,
            "code": "BAD_EVENT_QUEUE_ID",
            "error": "expired",
        },
    )
    monkeypatch.setattr(
        zulip_worker.zulip,
        "zulip_fetch_messages",
        lambda *_args, **_kwargs: {"received": True, "messages": [], "next_anchor": None},
    )

    result = _worker().poll_once(agent_label="Desktop_Agent2")

    assert result["status"] == "recovered"
    assert register_calls == [True]
    queue = storage.get_zulip_queue("Desktop_Agent2")
    assert queue["queue_id"] == "q-new"
    assert queue["reset_count"] == 1
