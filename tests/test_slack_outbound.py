from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time

import pytest

from unified_agent import events, storage
from unified_agent.slack_gateway import (
    MAX_TEXT_CHARS,
    FrontierPolicy,
    SlackConfig,
    SlackStatusLookupConfig,
    handle_http_request,
)
from unified_agent.slack_outbound import (
    SlackOutboundError,
    _ack_text,
    conversation_id,
    maybe_mirror_hub_message,
    maybe_reply_to_inbound,
    send_message,
    shutdown_background_workers,
)
from unified_agent.tools import messages as message_tools

_STATUS_LOOKUP_OFF = SlackStatusLookupConfig(enabled=False, identity_map={}, error=None)


@pytest.fixture(autouse=True)
def _drain_outbound_workers():
    """Finish scheduled acks/mirrors before tmp_path goes away.

    A worker still posting when the test returns writes the completed send to
    a memory.db that pytest has already removed, and the thread dies with an
    unhandled ``LiveStoreError`` attributed to whichever test runs next.
    """
    yield
    shutdown_background_workers(wait=True, timeout_s=5.0)


def _config(**overrides) -> SlackConfig:
    values = dict(
        signing_secret="signing",
        bot_token="xoxb-test",
        app_token=None,
        channel_ids=frozenset({"C_REQUESTS"}),
        channel_names_by_id={"C_REQUESTS": "requests"},
        users=frozenset({"U_OPERATOR"}),
        coordinator_agent="Desktop_Agent5",
        enabled=True,
        frontier=FrontierPolicy(
            models=frozenset({"frontier-test"}),
            provider="openai",
            credential_env="OPENAI_API_KEY",
            max_budget_usd=2.0,
            timeout_s=30,
            max_prompt_chars=1000,
            max_output_tokens=100,
            kill_switch=False,
            credential_present=True,
        ),
        status_lookup=_STATUS_LOOKUP_OFF,
    )
    values.update(overrides)
    return SlackConfig(**values)


def _ok_post(url, data, headers):
    assert data["channel"] == "C_REQUESTS"
    assert "thread_ts" in data
    return {"ok": True, "ts": "1700000099.1"}


def test_send_posts_in_thread_and_correlates(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    result = send_message(
        channel_id="C_REQUESTS",
        text="ack",
        agent_label="Desktop_Agent5",
        thread_ts="1700000000.0",
        dedupe_key="k-send-1",
        config=_config(),
        post_fn=_ok_post,
    )
    assert result["sent"] is True
    assert result["conversation_id"] == conversation_id("C_REQUESTS", "1700000000.0")
    assert result["thread_ts"] == "1700000000.0"
    rows = events.query_events(channels=["slack.outbound"])
    assert len(rows) == 1
    assert rows[0]["payload"]["conversation_id"] == result["conversation_id"]


def test_blocks_share_the_send_path_and_stay_out_of_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    sent = []

    def post(url, data, headers):
        sent.append(data)
        return {"ok": True, "ts": "1700000000.5"}

    blocks = [{"type": "section", "text": {"type": "plain_text", "text": "card body"}}]
    kwargs = dict(
        channel_id="C_REQUESTS",
        text="fallback",
        agent_label="agent-x",
        dedupe_key="k-blocks-1",
        config=_config(),
        post_fn=post,
        allow_unthreaded=True,
    )
    assert send_message(blocks=blocks, **kwargs)["sent"] is True
    assert send_message(blocks=blocks, **kwargs)["duplicate"] is True
    assert len(sent) == 1
    assert json.loads(sent[0]["blocks"]) == blocks
    assert sent[0]["text"] == "[agent-x] fallback"
    [row] = events.query_events(channels=["slack.outbound"])
    assert row["payload"]["blocks"] == 1
    assert "card body" not in json.dumps(row["payload"])
    with pytest.raises(SlackOutboundError, match="blocks exceed"):
        send_message(blocks=[{"type": "divider"}] * 51, **{**kwargs, "dedupe_key": "k-2"})
    with pytest.raises(SlackOutboundError, match="non-empty"):
        send_message(blocks=[], **{**kwargs, "dedupe_key": "k-3"})
    assert len(sent) == 1


def test_duplicate_dedupe_key_does_not_repost(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    calls = []

    def counting_post(url, data, headers):
        calls.append(data)
        return {"ok": True, "ts": "1.2"}

    kwargs = dict(
        channel_id="C_REQUESTS",
        text="ack",
        agent_label="Desktop_Agent5",
        event_ts="111.1",
        dedupe_key="same-key",
        config=_config(),
        post_fn=counting_post,
    )
    first = send_message(**kwargs)
    second = send_message(**kwargs)
    assert first["sent"] is True
    assert second["duplicate"] is True
    assert len(calls) == 1


def test_oversize_and_missing_token_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    with pytest.raises(SlackOutboundError, match="exceeds"):
        send_message(
            channel_id="C_REQUESTS",
            text="x" * 4001,
            agent_label="Desktop_Agent5",
            event_ts="1.1",
            dedupe_key="big",
            config=_config(),
            post_fn=_ok_post,
        )
    label = "Desktop_Agent5"
    body = "y" * (MAX_TEXT_CHARS - len(f"[{label}] ") + 1)
    assert len(body) < MAX_TEXT_CHARS
    assert len(f"[{label}] {body}") > MAX_TEXT_CHARS
    with pytest.raises(SlackOutboundError, match="exceeds"):
        send_message(
            channel_id="C_REQUESTS",
            text=body,
            agent_label=label,
            event_ts="1.2",
            dedupe_key="rendered-big",
            config=_config(),
            post_fn=_ok_post,
        )
    with pytest.raises(SlackOutboundError, match="SLACK_BOT_TOKEN"):
        send_message(
            channel_id="C_REQUESTS",
            text="ack",
            agent_label="Desktop_Agent5",
            event_ts="1.1",
            dedupe_key="notoken",
            config=_config(bot_token=None),
            post_fn=_ok_post,
        )


def test_rate_limit_and_failed_post_does_not_consume_dedupe(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_SLACK_MAX_PER_HOUR", "1")
    storage.init_db()
    first = send_message(
        channel_id="C_REQUESTS",
        text="one",
        agent_label="Desktop_Agent5",
        event_ts="2.1",
        dedupe_key="rate-1",
        config=_config(),
        post_fn=_ok_post,
    )
    assert first["sent"] is True
    limited = send_message(
        channel_id="C_REQUESTS",
        text="two",
        agent_label="Desktop_Agent5",
        event_ts="2.2",
        dedupe_key="rate-2",
        config=_config(),
        post_fn=_ok_post,
    )
    assert limited["reason"] == "rate-limited"

    monkeypatch.setenv("UA_SLACK_MAX_PER_HOUR", "30")

    def boom(url, data, headers):
        raise OSError("down")

    failed = send_message(
        channel_id="C_REQUESTS",
        text="retry-me",
        agent_label="Desktop_Agent5",
        event_ts="3.1",
        dedupe_key="retry-key",
        config=_config(),
        post_fn=boom,
    )
    assert failed["reason"] == "transport"
    retried = send_message(
        channel_id="C_REQUESTS",
        text="retry-me",
        agent_label="Desktop_Agent5",
        event_ts="3.1",
        dedupe_key="retry-key",
        config=_config(),
        post_fn=_ok_post,
    )
    assert retried["sent"] is True


def test_concurrent_same_key_posts_once(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    started = threading.Barrier(2)
    calls: list[dict] = []
    lock = threading.Lock()

    def counting_post(url, data, headers):
        with lock:
            calls.append(data)
        return {"ok": True, "ts": "1.2"}

    results: list[dict | None] = [None, None]

    def worker(idx: int) -> None:
        started.wait()
        results[idx] = send_message(
            channel_id="C_REQUESTS",
            text="ack",
            agent_label="Desktop_Agent5",
            event_ts="111.1",
            dedupe_key="same-concurrent",
            config=_config(),
            post_fn=counting_post,
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    sent = [row for row in results if row and row.get("sent")]
    dupes = [row for row in results if row and row.get("duplicate")]
    assert len(sent) == 1
    assert len(dupes) == 1
    assert len(calls) == 1


def test_reply_and_mirror_are_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    calls = []

    def counting_post(url, data, headers):
        calls.append(data)
        return {"ok": True, "ts": "9.9"}

    assert (
        maybe_reply_to_inbound(
            handle_result={"dispatched": True, "result": {"id": 12}},
            channel_id="C_REQUESTS",
            thread_ts="1.0",
            event_ts="1.1",
            event_id="Ev-ack",
            config=_config(),
            post_fn=counting_post,
        )
        is None
    )
    monkeypatch.setenv("UA_SLACK_REPLY", "1")
    ack = maybe_reply_to_inbound(
        handle_result={"dispatched": True, "result": {"id": 12}},
        channel_id="C_REQUESTS",
        thread_ts="1.0",
        event_ts="1.1",
        event_id="Ev-ack",
        config=_config(),
        post_fn=counting_post,
    )
    assert ack["sent"] is True
    assert "accepted task-12" in calls[0]["text"]

    assert (
        maybe_mirror_hub_message(
            message_result={
                "from_agent": "Desktop_Agent1",
                "to_agent": "Desktop_Agent5",
                "act": "inform",
                "message_id": "m1",
            },
            body="status update",
            config=_config(),
            post_fn=counting_post,
        )
        is None
    )
    monkeypatch.setenv("UA_SLACK_MIRROR_MESSAGES", "1")
    monkeypatch.setenv("UA_SLACK_MIRROR_CHANNEL_ID", "C_REQUESTS")
    mirrored = maybe_mirror_hub_message(
        message_result={
            "from_agent": "Desktop_Agent1",
            "to_agent": "Desktop_Agent5",
            "act": "inform",
            "message_id": "m1",
        },
        body="status update",
        config=_config(),
        post_fn=counting_post,
    )
    assert mirrored["sent"] is True
    assert "status update" in calls[-1]["text"]


def _signed(body: bytes, timestamp: str = "1700000000", secret: str = "signing") -> dict[str, str]:
    digest = hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": f"v0={digest}",
    }


def _wait_for(calls: list, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if calls:
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for background Slack outbound")


def test_http_adapter_acks_in_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_SLACK_REPLY", "1")
    storage.init_db()
    calls = []

    def counting_post(url, data, headers, **_kwargs):
        calls.append(data)
        return {"ok": True, "ts": "1700000000.2"}

    monkeypatch.setattr("unified_agent.slack_outbound._http_post", counting_post)
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-thread",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": "/task investigate queue",
                "ts": "1700000000.1",
                "thread_ts": "1700000000.0",
            },
        }
    ).encode()
    result = handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
    assert result["dispatched"] is True
    assert result["conversation_id"] == "slack:C_REQUESTS:1700000000.0"
    assert result["slack_reply"] == {"scheduled": True}
    _wait_for(calls)
    assert calls[0]["thread_ts"] == "1700000000.0"


def test_slash_command_acks_unthreaded(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_SLACK_REPLY", "1")
    storage.init_db()
    calls = []

    def counting_post(url, data, headers, **_kwargs):
        calls.append(data)
        return {"ok": True, "ts": "1700000000.9"}

    monkeypatch.setattr("unified_agent.slack_outbound._http_post", counting_post)
    body = b"command=%2Ftask&text=slash+request&user_id=U_OPERATOR&channel_id=C_REQUESTS"
    headers = _signed(body)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    result = handle_http_request(body, headers=headers, config=_config(), now=1700000000)
    assert result["dispatched"] is True
    assert result["slack_reply"] == {"scheduled": True}
    _wait_for(calls)
    assert "thread_ts" not in calls[0]


def test_http_returns_before_slack_post(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_SLACK_REPLY", "1")
    storage.init_db()
    started = threading.Event()
    release = threading.Event()

    def blocking_post(url, data, headers, **_kwargs):
        started.set()
        release.wait(timeout=2.0)
        return {"ok": True, "ts": "1700000000.2"}

    monkeypatch.setattr("unified_agent.slack_outbound._http_post", blocking_post)
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev-async",
            "event": {
                "type": "app_mention",
                "user": "U_OPERATOR",
                "channel": "C_REQUESTS",
                "text": "/task investigate queue",
                "ts": "1700000000.1",
                "thread_ts": "1700000000.0",
            },
        }
    ).encode()
    result = handle_http_request(body, headers=_signed(body), config=_config(), now=1700000000)
    assert result["slack_reply"] == {"scheduled": True}
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not started.is_set():
        time.sleep(0.01)
    assert started.is_set()
    release.set()


def test_message_send_survives_mirror_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_SLACK_MIRROR_MESSAGES", "1")
    monkeypatch.setenv("UA_SLACK_MIRROR_CHANNEL_ID", "C_REQUESTS")
    storage.init_db()
    failures: list[str] = []

    def boom(*args, **kwargs):
        failures.append("boom")
        raise RuntimeError("mirror down")

    monkeypatch.setattr("unified_agent.slack_outbound.send_message", boom)
    result = message_tools.message_send(
        act="inform",
        from_agent="Desktop_Agent1",
        to_agent="Desktop_Agent5",
        body="still delivered",
        message_id="01TESTMSGSLACK0000000000001",
    )
    assert result["accepted"] is True
    assert result["duplicate"] is False
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not failures:
        time.sleep(0.01)
    assert failures == ["boom"]


def test_ack_text_task_status_usage_and_not_found():
    assert (
        _ack_text({"dispatched": True, "result": {"kind": "task_status", "reason": "usage"}})
        == "usage: status task-<id>"
    )
    assert (
        _ack_text(
            {
                "dispatched": True,
                "result": {"kind": "task_status", "reason": "not_found", "task_id": "999"},
            }
        )
        == "task-999 not found"
    )


def test_ack_text_task_status_ok_escapes_mrkdwn_and_is_bounded():
    result = {
        "dispatched": True,
        "result": {
            "kind": "task_status",
            "reason": "ok",
            "task": {
                "id": 42,
                "title": "<script>alert(1)</script> & friends " + "x" * 4000,
                "status": "doing",
                "project": "unified-agent",
                "owner": "Desktop_Agent1 & co <admin>",
            },
        },
    }
    text = _ack_text(result)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&amp; friends" in text
    assert "Desktop_Agent1 &amp; co &lt;admin&gt;" in text
    assert "task-42" in text
    assert "[doing]" in text
    assert len(text) <= MAX_TEXT_CHARS


def test_ack_text_task_status_ok_reports_unclaimed_owner():
    result = {
        "dispatched": True,
        "result": {
            "kind": "task_status",
            "reason": "ok",
            "task": {
                "id": 7,
                "title": "no owner yet",
                "status": "open",
                "project": "unified-agent",
                "owner": None,
            },
        },
    }
    assert "owner: unclaimed" in _ack_text(result)
