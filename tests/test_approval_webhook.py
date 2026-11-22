"""Signed approval webhook: HMAC, retries, and no secrets in logs."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import secrets
import socket
import threading
import time
from collections import deque

import pytest

from unified_agent import approval_webhook
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_resolve,
    check_approval_gate,
)

URL = "https://hooks.example/inbound/sekrit-path?token=query-secret"
SECRET = secrets.token_urlsafe(32)


@pytest.fixture(autouse=True)
def bounded_state(monkeypatch):
    # Every test owns its queues and permits; none can reuse a previous send.
    monkeypatch.setattr(approval_webhook, "_pending", deque())
    monkeypatch.setattr(approval_webhook, "_outstanding", set())
    monkeypatch.setattr(approval_webhook, "_workers", 0)
    monkeypatch.setattr(approval_webhook, "_overflow_reported", False)
    monkeypatch.setattr(approval_webhook, "_transport_slots", threading.BoundedSemaphore(2))


def _eventually(check, timeout=2):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if check():
            return
        time.sleep(0.005)
    assert check()


def _sign(body: bytes) -> str:
    digest = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture
def delivery(monkeypatch):
    sent: list[dict] = []
    sleeps: list[float] = []
    script: list[object] = []

    def post(url, body, headers):
        sent.append({"url": url, "body": body, "headers": dict(headers)})
        if not script:
            return 204
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    monkeypatch.setattr(approval_webhook, "_post", post)
    monkeypatch.setattr(approval_webhook, "_sleep", sleeps.append)
    monkeypatch.setattr(approval_webhook, "_spawn", lambda fn: fn())
    monkeypatch.setenv(approval_webhook.URL_ENV, URL)
    monkeypatch.setenv(approval_webhook.SECRET_ENV, SECRET)
    return {"sent": sent, "sleeps": sleeps, "script": script}


def test_requested_and_decided_are_signed_and_omit_the_payload(delivery, monkeypatch, caplog):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    caplog.set_level(logging.DEBUG)
    payload = {"command": "echo hi"}
    with pytest.raises(ApprovalPendingError):
        check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3")
    with pytest.raises(ApprovalPendingError):
        check_approval_gate("run_cmd", payload, requested_by="Desktop_agent3")
    assert len(delivery["sent"]) == 1
    first = delivery["sent"][0]
    body = json.loads(first["body"])
    assert body["event"] == "approval.requested"
    assert body["tool"] == "run_cmd"
    assert body["status"] == "pending"
    assert body["requested_by"] == "Desktop_agent3"
    assert "command" not in first["body"].decode()
    assert first["headers"][approval_webhook.SIGNATURE_HEADER] == _sign(first["body"])
    assert first["url"] == URL

    approval_id = body["approval_id"]
    approval_resolve(approval_id, "approve", resolved_by="Operator")
    assert len(delivery["sent"]) == 2
    decided = json.loads(delivery["sent"][1]["body"])
    assert decided["event"] == "approval.decided"
    assert decided["decision"] == "approve"
    assert decided["status"] == "approved"
    assert decided["resolved_by"] == "Operator"
    assert delivery["sent"][1]["headers"][approval_webhook.SIGNATURE_HEADER] == _sign(
        delivery["sent"][1]["body"]
    )
    assert SECRET not in caplog.text
    assert "sekrit-path" not in caplog.text
    assert "query-secret" not in caplog.text
    assert "hooks.example" in caplog.text


def test_retries_transport_and_5xx_then_stops_on_4xx(delivery, caplog):
    caplog.set_level(logging.WARNING)
    delivery["script"].extend([TimeoutError(URL), 503, 204])
    approval_webhook.notify_requested(7, "run_cmd", "Desktop_agent3")
    assert len(delivery["sent"]) == 3
    assert delivery["sleeps"] == [0.5, 2.0]

    delivery["sent"].clear()
    delivery["sleeps"].clear()
    delivery["script"].append(400)
    approval_webhook.notify_decided(7, "run_cmd", "denied", "deny", "Desktop_agent3", "Operator")
    assert len(delivery["sent"]) == 1
    assert delivery["sleeps"] == []
    assert URL not in caplog.text
    assert SECRET not in caplog.text


def test_gives_up_without_raising_or_logging_the_url(delivery, caplog):
    caplog.set_level(logging.WARNING)
    delivery["script"].extend([OSError(URL), OSError(URL), OSError(URL)])
    approval_webhook.notify_requested(3, "delegate", None)
    assert len(delivery["sent"]) == 3
    assert "gave up" in caplog.text
    assert URL not in caplog.text
    assert SECRET not in caplog.text


def test_unset_or_incomplete_config_does_not_send(delivery, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.delenv(approval_webhook.URL_ENV)
    monkeypatch.delenv(approval_webhook.SECRET_ENV)
    approval_webhook.notify_requested(1, "run_cmd", None)
    assert delivery["sent"] == []

    monkeypatch.setenv(approval_webhook.URL_ENV, URL)
    monkeypatch.delenv(approval_webhook.SECRET_ENV, raising=False)
    approval_webhook.notify_requested(1, "run_cmd", None)
    assert delivery["sent"] == []
    assert approval_webhook.SECRET_ENV in caplog.text
    assert "sekrit-path" not in caplog.text
    assert SECRET not in caplog.text


def test_forty_notifications_bound_queue_workers_and_coalesce_duplicates(monkeypatch):
    queued = []
    monkeypatch.setattr(approval_webhook, "_spawn", queued.append)
    monkeypatch.setenv(approval_webhook.URL_ENV, URL)
    monkeypatch.setenv(approval_webhook.SECRET_ENV, secrets.token_urlsafe(32))
    for _ in range(40):
        approval_webhook.notify_requested(1, "run_cmd", "review-agent")
    assert len(queued) == 1
    assert len(approval_webhook._pending) == 1
    for number in range(2, 41):
        approval_webhook.notify_requested(number, "run_cmd", "review-agent")
    assert len(queued) == approval_webhook.MAX_WORKERS
    assert len(approval_webhook._pending) == approval_webhook.MAX_QUEUED
    assert len(approval_webhook._outstanding) == approval_webhook.MAX_QUEUED
    # No body is delivered during this admission/capacity test.


def test_hung_dns_deadline_does_not_replace_still_running_transport_workers(monkeypatch):
    release = threading.Event()
    entered = []
    original_post = approval_webhook._post
    monkeypatch.setenv(approval_webhook.URL_ENV, "https://webhook.invalid/callback")
    monkeypatch.setenv(approval_webhook.SECRET_ENV, secrets.token_urlsafe(32))
    monkeypatch.setattr(approval_webhook, "DEADLINE_S", 0.05)
    monkeypatch.setattr(approval_webhook, "MAX_ATTEMPTS", 1)

    def stalled_dns(*args, **kwargs):
        entered.append(threading.current_thread())
        release.wait(3)
        raise OSError("synthetic resolver failure")

    monkeypatch.setattr(socket, "getaddrinfo", stalled_dns)
    # Keep the actual urllib/HTTPS transport: the deadline includes getaddrinfo.
    assert approval_webhook._post is original_post
    try:
        for number in range(40):
            approval_webhook.notify_requested(number + 100, "run_cmd", "review-agent")
        _eventually(lambda: len(entered) == approval_webhook.MAX_TRANSPORTS)
        _eventually(lambda: approval_webhook._workers == 0)
        assert not release.is_set()
        assert len(entered) == 2
        assert len(approval_webhook._pending) == 0
        assert len(approval_webhook._outstanding) == 0
        # New work after both deadlines still cannot start replacement lookups.
        for number in range(40):
            approval_webhook.notify_requested(number + 200, "run_cmd", "review-agent")
        _eventually(lambda: approval_webhook._workers == 0)
        assert len(entered) == 2
    finally:
        release.set()
        for worker in entered:
            worker.join(2)
        assert all(not worker.is_alive() for worker in entered)


def test_retries_keep_exact_body_signature_and_identity(delivery):
    delivery["script"].extend([503, 429, 204])
    approval_webhook.notify_requested(88, "run_cmd", "review-agent")
    sent = delivery["sent"]
    assert len(sent) == 3
    assert len({row["body"] for row in sent}) == 1
    assert len({row["headers"][approval_webhook.SIGNATURE_HEADER] for row in sent}) == 1
    assert json.loads(sent[0]["body"])["delivery_id"] == "approval.requested:88"


def test_late_connect_cannot_send_after_deadline_and_retains_slot_until_exit(monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    transmitted = []
    connections = []
    monkeypatch.setattr(approval_webhook, "DEADLINE_S", 0.05)

    class Connection:
        timeout = 5
        sock = None

        def __init__(self, *args, **kwargs):
            pass

        def connect(self):
            entered.set()
            release.wait(3)

        def close(self):
            pass

    def fake_open(handler, factory, request, **kwargs):
        connection = factory("webhook.invalid")
        connections.append(connection)
        connection.connect()
        transmitted.append(True)
        raise AssertionError("expired connection must never send the signed request")

    monkeypatch.setattr(approval_webhook.urllib.request.HTTPSHandler, "do_open", fake_open)
    # The fake base opener runs the actual deadline handler's connection factory.
    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    try:
        with pytest.raises(TimeoutError, match="deadline passed"):
            approval_webhook._post_within_deadline("https://webhook.invalid/callback", b"{}", {})
        assert entered.is_set()
        assert approval_webhook._transport_slots.acquire(blocking=False)
        assert not approval_webhook._transport_slots.acquire(blocking=False)
        approval_webhook._transport_slots.release()
    finally:
        release.set()
        _eventually(
            lambda: not any(t.name == "approval-webhook-send" for t in threading.enumerate())
        )
    assert connections
    assert transmitted == []
    monkeypatch.setattr(approval_webhook, "_post", lambda *args: 204)
    assert (
        approval_webhook._post_within_deadline("https://webhook.invalid/callback", b"{}", {}) == 204
    )
