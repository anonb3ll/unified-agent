"""Slack Block Kit approval cards (task #2303): outbound only, never authority."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time

import httpx
import pytest

from unified_agent import approval_slack, config, gateway, storage
from unified_agent.tools import doctor
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_resolve,
    check_approval_gate,
)

CHANNEL = "C0APPROVALS"
OK = object()


def _fake_bot_token() -> str:
    # Built at runtime so no token-shaped literal sits in the repo.
    return "-".join(("xoxb", secrets.token_hex(6), secrets.token_hex(12)))


@pytest.fixture(autouse=True)
def fresh_worker(monkeypatch):
    monkeypatch.setattr(approval_slack, "_worker_running", False)


@pytest.fixture
def clock(monkeypatch):
    """A fake clock the worker's idle sleep advances, so retries run instantly."""
    now = [time.time()]
    sleeps: list[float] = []
    hooks: list = []

    def sleep(delay):
        sleeps.append(round(delay, 3))
        now[0] += delay
        if hooks:
            hooks.pop(0)()

    monkeypatch.setattr(approval_slack, "_now", lambda: now[0])
    monkeypatch.setattr(approval_slack, "_sleep", sleep)
    return {"now": now, "sleeps": sleeps, "hooks": hooks}


@pytest.fixture
def slack(monkeypatch, clock):
    """Configured, allowlisted, token present; posts captured.

    The worker reads the process environment (it runs in the hub). The doctor
    reads the hub unit on a workstation, so the unit is stubbed explicitly with
    the same settings instead of depending on what this machine has installed.
    """
    token = _fake_bot_token()
    settings = {
        approval_slack.CHANNEL_ENV: CHANNEL,
        "UA_SLACK_CHANNEL_IDS": CHANNEL,
        "UA_SLACK_ENABLED": "1",
        "SLACK_BOT_TOKEN": token,
        "UA_PUBLIC_URL": "https://hub.example.test",
        "UA_APPROVAL_TOOLS": "run_cmd",
    }
    for name in ("UA_SLACK_CHANNEL_MAP", "UA_HOSTED", "RAILWAY_ENVIRONMENT_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    unit = dict(settings)
    monkeypatch.setattr(doctor.service_probes, "_service_env", lambda _unit: unit)

    posts: list[dict] = []
    script: list[object] = []

    def transport(url, data, headers, timeout_s):
        posts.append({"url": url, "data": dict(data), "headers": dict(headers)})
        step = script.pop(0) if script else OK
        if isinstance(step, BaseException):
            raise step
        if step is OK:
            return 200, {}, {"ok": True, "ts": f"1700000000.{len(posts):06d}"}
        return step

    def setting(name, value):
        """Change one setting for both the hub process and the hub unit."""
        if value is None:
            unit.pop(name, None)
            monkeypatch.delenv(name, raising=False)
        else:
            unit[name] = value
            monkeypatch.setenv(name, value)

    monkeypatch.setattr(approval_slack, "_transport", transport)
    monkeypatch.setattr(approval_slack, "_spawn", lambda fn: fn())
    return {"posts": posts, "script": script, "token": token, "setting": setting}


def _request(command: str = "echo hi", **extra) -> int:
    with pytest.raises(ApprovalPendingError) as exc:
        check_approval_gate("run_cmd", {"command": command, **extra}, requested_by="agent-a")
    return int(exc.value.approval_id)


def _deliveries() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? ORDER BY created_at, ulid",
            (approval_slack.EVIDENCE_CHANNEL,),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(r["payload"]) for r in rows]


def _outbox(key: str) -> dict:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT * FROM approval_slack_outbox WHERE dedupe_key=?", (key,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row)


def _set_outbox(sql: str, params: tuple) -> None:
    conn = storage.connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _status() -> str:
    return doctor._approval_slack_cards_check()["status"]


def test_card_carries_the_fields_and_never_the_payload(slack):
    api_key = "sk-" + secrets.token_hex(8)
    payload = {"command": "echo do-not-leak-this", "api_key": api_key}
    approval_id = _request(payload["command"], api_key=api_key)

    assert len(slack["posts"]) == 1
    sent = slack["posts"][0]
    assert sent["data"]["channel"] == CHANNEL
    assert "thread_ts" not in sent["data"]
    blocks = json.loads(sent["data"]["blocks"])
    rendered = json.dumps(blocks) + sent["data"]["text"]
    assert f"#{approval_id}" in rendered
    assert "run_cmd" in rendered
    assert "agent-a" in rendered
    assert gateway.arguments_sha256(payload) in rendered
    assert "Created" in rendered
    # The pass runs from approval, not from the request (delivery may be late).
    assert f"{config.approval_pass_ttl_hours():g}h after approval" in rendered
    assert "approved now" not in rendered
    assert "do-not-leak-this" not in rendered
    assert api_key not in rendered
    assert slack["token"] not in rendered

    buttons = [
        el
        for b in blocks
        if b["type"] == "actions"
        for el in b["elements"]
        if el["type"] == "button"
    ]
    assert len(buttons) == 1, "the only action is a link to the Control Center"
    assert (
        buttons[0]["url"] == f"https://hub.example.test/ui/inbox?kind=approval&item={approval_id}"
    )

    [row] = _deliveries()
    assert row == {
        "approval_id": approval_id,
        "event": "requested",
        "channel_id": CHANNEL,
        "attempt": 1,
        "outcome": "delivered",
        "dead_letter": False,
        "ts": "1700000000.000001",
    }


def test_block_limits_hold_for_the_longest_tool_id(slack):
    row = {
        "id": 123456,  # "Approval #123456 waiting: " + 128 chars is 154 > 150
        "tool": "t" + "x" * 127,  # the longest id approval_notification_tool accepts
        "payload": "{}",
        "requested_by": "r" * 500,
        "created_at": "2026-11-03T00:00:00+00:00",
        "run_id": None,
    }
    _, blocks = approval_slack.build_card(row)
    header = blocks[0]["text"]["text"]
    assert len(header) <= approval_slack.HEADER_MAX and header.endswith("…")
    for block in blocks:
        for field in block.get("fields", []):
            assert len(field["text"]) <= approval_slack.FIELD_MAX
        if block["type"] == "section" and "text" in block:
            assert len(block["text"]["text"]) <= approval_slack.SECTION_TEXT_MAX
        for el in block.get("elements", []):
            if el.get("type") == "button":
                assert len(el["text"]["text"]) <= approval_slack.BUTTON_TEXT_MAX
                assert len(el["url"]) <= approval_slack.URL_MAX
    slack["setting"]("UA_PUBLIC_URL", "https://hub.example.test/" + "p" * 3000)
    _, blocks = approval_slack.build_card({**row, "tool": "run_cmd"})
    assert not [b for b in blocks if b["type"] == "actions"], "a cut URL is dropped, not posted"


def test_duplicate_request_posts_once(slack):
    first = _request()
    second = _request()
    assert first == second
    approval_slack.notify_requested(first, "run_cmd", "agent-a")
    assert len(slack["posts"]) == 1


def test_retries_then_dead_letter_and_decision_is_never_unthreaded(slack, clock):
    slack["script"].extend(
        [
            httpx.ConnectError("boom"),
            (503, {}, None),
            (429, {}, {"ok": False, "error": "ratelimited"}),
        ]
    )
    approval_id = _request()
    assert len(slack["posts"]) == approval_slack.MAX_ATTEMPTS
    assert clock["sleeps"] == list(approval_slack.BACKOFF_S)
    rows = _deliveries()
    assert [r["outcome"] for r in rows] == ["retry", "retry", "failed"]
    assert [r["dead_letter"] for r in rows] == [False, False, True]
    assert {r["approval_id"] for r in rows} == {approval_id}
    assert {r["channel_id"] for r in rows} == {CHANNEL}
    assert _status() == "DEGRADED"

    approval_resolve(approval_id, "deny", resolved_by=config.operator_label(), reason="no")
    assert len(slack["posts"]) == approval_slack.MAX_ATTEMPTS, "no unthreaded follow-up"
    last = _deliveries()[-1]
    assert last["event"] == "decided" and last["dead_letter"] is True
    assert last["error"] == "parent-card-failed"


def test_retry_after_is_honoured_and_bounded(slack, clock):
    slack["script"].append((429, {"Retry-After": "30"}, {"ok": False, "error": "ratelimited"}))
    first = _request("echo one")
    assert clock["sleeps"] == [30.0]
    assert _outbox(f"approval:{first}:requested")["status"] == "done"

    slack["script"].append((429, {"retry-after": "9999"}, {"ok": False, "error": "ratelimited"}))
    second = _request("echo two")
    assert clock["sleeps"] == [30.0]
    last = _deliveries()[-1]
    assert last["approval_id"] == second and last["error"] == "retry-after-exceeds-cap"
    assert last["dead_letter"] is True


def test_permanent_api_error_dead_letters_without_retry(slack):
    slack["script"].append((200, {}, {"ok": False, "error": "not_in_channel"}))
    _request()
    assert len(slack["posts"]) == 1
    [row] = _deliveries()
    assert row["outcome"] == "failed" and row["dead_letter"] is True
    assert row["error"] == "not_in_channel"


def test_non_allowlisted_channel_is_refused(slack):
    slack["setting"]("UA_SLACK_CHANNEL_IDS", "C0SOMETHINGELSE")
    approval_id = _request()
    assert slack["posts"] == []
    [row] = _deliveries()
    assert row["approval_id"] == approval_id
    assert row["outcome"] == "refused" and row["dead_letter"] is True
    assert row["error"] == "channel-not-allowlisted"
    assert _status() == "MISCONFIGURED"


def test_malformed_channel_map_is_misconfigured_not_a_crash(slack):
    slack["setting"]("UA_SLACK_CHANNEL_MAP", "not-a-mapping")
    res = doctor._approval_slack_cards_check()
    assert res["status"] == "MISCONFIGURED"
    assert "UA_SLACK_CHANNEL_MAP is malformed" in res["detail"]
    names = {c["capability"] for c in doctor._run_system_doctor()["checks"]}
    assert "approval_slack_cards" in names

    _request()
    assert slack["posts"] == []
    assert _deliveries()[-1]["error"] == "slack-config-invalid"


def test_disabled_makes_zero_posts(slack):
    slack["setting"](approval_slack.CHANNEL_ENV, None)
    approval_id = _request()
    approval_resolve(approval_id, "deny", resolved_by=config.operator_label(), reason="no")
    assert slack["posts"] == []
    assert _deliveries() == []
    assert _status() == "DORMANT"


def test_decision_threads_under_the_card_and_doctor_needs_a_delivery(slack):
    assert _status() == "UNKNOWN", "configured is not LIVE until a card lands"
    approval_id = _request()
    human = config.operator_label()
    approval_resolve(approval_id, "approve", resolved_by=human)
    assert len(slack["posts"]) == 2
    follow = slack["posts"][1]["data"]
    assert follow["thread_ts"] == "1700000000.000001"
    assert f"approved by {human}" in follow["text"]
    assert "blocks" not in follow
    assert [r["event"] for r in _deliveries()] == ["requested", "decided"]
    res = doctor._approval_slack_cards_check()
    assert res["status"] == "LIVE" and "last delivery" in res["detail"]


def test_decision_waits_for_a_card_another_process_is_sending(slack, clock, monkeypatch):
    monkeypatch.setattr(approval_slack, "MAX_DEFER_S", 10.0)
    spawn = approval_slack._spawn
    monkeypatch.setattr(approval_slack, "_spawn", lambda fn: None)
    first = _request("echo one")
    second = _request("echo two")
    # Both cards are mid-send in another process (a fresh claim).
    _set_outbox(
        "UPDATE approval_slack_outbox SET status='sending', claimed_at=?", (clock["now"][0],)
    )
    monkeypatch.setattr(approval_slack, "_spawn", spawn)
    monkeypatch.setattr(approval_slack, "_worker_running", False)  # a fresh process

    # The first card is confirmed while its decision waits: the follow-up threads.
    clock["hooks"].append(
        lambda: _set_outbox(
            "UPDATE approval_slack_outbox SET status='done', ts='1700000009.1', channel_id=? "
            "WHERE dedupe_key=?",
            (CHANNEL, f"approval:{first}:requested"),
        )
    )
    approval_slack.notify_decided(first, "approved", "someone")
    assert [p["data"].get("thread_ts") for p in slack["posts"]] == ["1700000009.1"]

    # The second card never confirms: the decision dead-letters, never unthreaded.
    approval_slack.notify_decided(second, "denied", "someone")
    assert len(slack["posts"]) == 1
    last = _deliveries()[-1]
    assert last["approval_id"] == second and last["error"] == "parent-card-unconfirmed"


def test_overflow_is_dead_lettered_not_silent(slack, monkeypatch):
    monkeypatch.setattr(approval_slack, "MAX_PENDING", 1)
    monkeypatch.setattr(approval_slack, "_spawn", lambda fn: None)
    _request("echo one")
    second = _request("echo two")
    assert slack["posts"] == []
    [row] = _deliveries()
    assert row["approval_id"] == second
    assert row["outcome"] == "dropped" and row["error"] == "queue-overflow"
    assert row["dead_letter"] is True


def test_worker_exception_is_dead_lettered_and_the_worker_continues(slack, monkeypatch):
    real = approval_slack.build_card
    calls = []

    def flaky(row):
        calls.append(row["id"])
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(row)

    monkeypatch.setattr(approval_slack, "build_card", flaky)
    first = _request("echo one")
    second = _request("echo two")
    rows = _deliveries()
    assert rows[0]["approval_id"] == first and rows[0]["error"] == "worker-runtimeerror"
    assert rows[0]["dead_letter"] is True
    assert rows[1]["approval_id"] == second and rows[1]["outcome"] == "delivered"


def test_restart_resumes_an_unfinished_delivery(slack, clock, monkeypatch):
    spawn = approval_slack._spawn
    monkeypatch.setattr(approval_slack, "_spawn", lambda fn: None)  # the process "dies"
    approval_id = _request()
    key = f"approval:{approval_id}:requested"
    _set_outbox(
        "UPDATE approval_slack_outbox SET status='sending', claimed_at=? WHERE dedupe_key=?",
        (clock["now"][0] - approval_slack.STALE_CLAIM_S - 1, key),
    )
    assert slack["posts"] == []
    monkeypatch.setattr(approval_slack, "_spawn", spawn)
    monkeypatch.setattr(approval_slack, "_worker_running", False)  # a fresh process
    approval_slack.startup_drain()
    assert len(slack["posts"]) == 1
    assert _outbox(key)["status"] == "done"


def test_slow_slack_never_blocks_the_approval_commit(slack, monkeypatch):
    release = threading.Event()
    posted = threading.Event()

    def slow(url, data, headers, timeout_s):
        posted.set()
        release.wait(5)
        return 200, {}, {"ok": True, "ts": "1700000001.000001"}

    monkeypatch.setattr(approval_slack, "_transport", slow)
    monkeypatch.setattr(
        approval_slack, "_spawn", lambda fn: threading.Thread(target=fn, daemon=True).start()
    )
    started = time.monotonic()
    approval_id = _request()
    assert time.monotonic() - started < 2.0
    conn = storage.connect()
    try:
        row = conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending"
    assert posted.wait(5)
    release.set()
    until = time.monotonic() + 5
    while time.monotonic() < until and not _deliveries():
        time.sleep(0.01)
    assert [r["outcome"] for r in _deliveries()] == ["delivered"]


class TestDoctorSources:
    def test_workstation_without_token_is_misconfigured(self, slack):
        slack["setting"]("SLACK_BOT_TOKEN", None)
        res = doctor._approval_slack_cards_check()
        assert res["status"] == "MISCONFIGURED"
        assert "SLACK_BOT_TOKEN" in res["detail"]

    def test_unreadable_hub_unit_is_unknown(self, slack, monkeypatch):
        monkeypatch.setattr(doctor.service_probes, "_service_env", lambda _unit: None)
        assert _status() == "UNKNOWN"

    def test_hosted_reads_the_process_environment(self, slack, monkeypatch):
        monkeypatch.setattr(doctor.service_probes, "_service_env", lambda _unit: None)
        monkeypatch.setenv("UA_HOSTED", "1")
        assert _status() == "UNKNOWN"  # configured, nothing delivered yet
        _request()
        assert _status() == "LIVE"
        monkeypatch.delenv(approval_slack.CHANNEL_ENV)
        assert _status() == "DORMANT"

    def test_unreadable_ledger_is_unknown(self, slack, monkeypatch):
        def broken(*_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(approval_slack, "dead_letters_since", broken)
        res = doctor._approval_slack_cards_check()
        assert res["status"] == "UNKNOWN"
        assert "OperationalError" in res["detail"]
        assert slack["token"] not in repr(res)
