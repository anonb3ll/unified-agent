"""Tests for batch_notify and digest tool."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools import digest


def test_batch_notify_and_flush(monkeypatch, tmp_path):
    # Mock notify to prevent actual bun send.ts execution during tests
    sent_messages = []

    def fake_notify(msg, timeout_s=60):
        sent_messages.append(msg)
        return {"delivered": True, "chars": len(msg), "error": None}

    monkeypatch.setattr(digest, "notify", fake_notify)

    # 1. Queue messages without flushing
    res = digest.batch_notify(
        ["Server disk clean", "Backup completed"], category="ops", max_batch_size=5
    )
    assert res["queued"] == 2
    assert res["total_pending"] == 2
    assert res["flushed"] is False
    assert len(sent_messages) == 0

    # 2. Queue message with flush_now=True
    res2 = digest.batch_notify(["Critical audit finding"], category="ops", flush_now=True)
    assert res2["flushed"] is True
    assert len(sent_messages) == 1
    assert "📬 Agent Notification Digest (3 alerts):" in sent_messages[0]
    assert "Critical audit finding" in sent_messages[0]

    # 3. Flush empty queue
    res3 = digest.flush_digest(category="ops")
    assert res3["flushed_count"] == 0
    assert res3["delivered"] is False


def test_batch_notify_auto_flush_threshold(monkeypatch):
    sent_messages = []

    def fake_notify(msg, timeout_s=60):
        sent_messages.append(msg)
        return {"delivered": True, "chars": len(msg), "error": None}

    monkeypatch.setattr(digest, "notify", fake_notify)

    # Adding 3 messages with max_batch_size=3 should trigger auto-flush
    res = digest.batch_notify(["Alert 1", "Alert 2", "Alert 3"], category="auto", max_batch_size=3)
    assert res["flushed"] is True
    assert len(sent_messages) == 1
    assert "Alert 1" in sent_messages[0]


def test_batch_notify_validation():
    with pytest.raises(ToolError):
        digest.batch_notify([])

    with pytest.raises(ToolError):
        digest.batch_notify(["   "])
