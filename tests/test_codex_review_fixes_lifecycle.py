"""Codex P1/significant review fixes: events, messaging, lifecycle, session_end."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, storage
from unified_agent.lifecycle.capture import _sanitize_payload
from unified_agent.lifecycle.packet import build_session_packet
from unified_agent.lifecycle.schema import CHANNEL_LIFECYCLE_PREFIX
from unified_agent.messaging.schema import CHANNEL_MESSAGE_PREFIX
from unified_agent.tools import messages as message_tools
from unified_agent.tools import registry as registry_tools
from unified_agent.tools import tasks as task_tools


def test_query_events_newest_first():
    for i in range(5):
        events.record_event(channel="agent.task.updated", payload={"n": i})

    asc = events.query_events(channels=["agent.task.updated"], limit=3)
    desc = events.query_events(channels=["agent.task.updated"], limit=3, newest_first=True)
    recent = events.query_recent_events(channels=["agent.task.updated"], limit=3)

    assert [r["payload"]["n"] for r in asc] == [0, 1, 2]
    assert [r["payload"]["n"] for r in desc] == [4, 3, 2]
    assert [r["payload"]["n"] for r in recent] == [2, 3, 4]

    since = asc[0]["ulid"]
    after = events.query_events(
        channels=["agent.task.updated"], since=since, limit=10, newest_first=True
    )
    assert [r["payload"]["n"] for r in after] == [4, 3, 2, 1]
    assert all(r["ulid"] > since for r in after)


def test_list_messages_returns_newest_when_many_older_events_exist():
    conversation = "01TESTCONV000000000000000099"
    for i in range(520):
        events.record_event(
            channel=f"{CHANNEL_MESSAGE_PREFIX}.inform",
            payload={
                "message_id": f"old-{i:04d}",
                "act": "inform",
                "conversation_id": "01OTHERCONV000000000000000001",
                "from_agent": "Desktop_Agent1",
                "to_agent": "Desktop_Agent5",
                "hop": 0,
                "body": f"noise-{i}",
            },
            originator="Desktop_Agent1",
            notify=False,
        )

    recent = message_tools.message_send(
        act="inform",
        from_agent="Desktop_Agent1",
        to_agent="Desktop_Agent5",
        body="latest signal",
        conversation_id=conversation,
        message_id="01TESTMSG0000000000000000099",
    )
    assert recent["accepted"] is True

    listed = message_tools.message_list(conversation_id=conversation, limit=10)
    assert listed["total"] == 1
    assert listed["messages"][0]["message_id"] == "01TESTMSG0000000000000000099"
    assert listed["messages"][0]["body"] == "latest signal"


def test_recursive_lifecycle_sanitize_strips_nested_prompt():
    cleaned = _sanitize_payload(
        {
            "path": "src/unified_agent/storage.py",
            "nested": {
                "prompt": "should be stripped",
                "ok": "keep-me",
                "deeper": [{"transcript": "nope", "status": "ok"}],
            },
            "PROMPT": "case-insensitive strip",
        }
    )
    assert cleaned["path"] == "src/unified_agent/storage.py"
    assert "prompt" not in cleaned.get("nested", {})
    assert "PROMPT" not in cleaned
    assert cleaned["nested"]["ok"] == "keep-me"
    assert "transcript" not in cleaned["nested"]["deeper"][0]
    assert cleaned["nested"]["deeper"][0]["status"] == "ok"


def test_packet_includes_session_details_when_capture_off(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "0")
    task_id = task_tools.task_add(title="Packet session details")["id"]
    started = registry_tools.session_start(
        kind="work",
        agent="Desktop_Agent4",
        task_id=task_id,
    )
    session_id = started["session_id"]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE agent_sessions SET summary=? WHERE id=?",
            ("bounded session summary for packet", session_id),
        )
        conn.commit()
    finally:
        conn.close()

    packet = build_session_packet(session_id=session_id, agent="Desktop_Agent4")
    assert packet["session"] is not None
    assert packet["session"]["id"] == session_id
    assert packet["session"]["agent"] == "Desktop_Agent4"
    assert packet["session"]["kind"] == "work"
    assert packet["session"]["status"] == "running"
    assert packet["session"]["summary"] == "bounded session summary for packet"
    assert packet["session"]["started_at"]
    assert "transcript_path" not in packet["session"]
    assert "worktree_path" not in packet["session"]


def test_mismatched_task_session_anchors_raise():
    task_a = task_tools.task_add(title="Anchor A")["id"]
    task_b = task_tools.task_add(title="Anchor B")["id"]
    started = registry_tools.session_start(
        kind="work",
        agent="Desktop_Agent4",
        task_id=task_a,
    )
    with pytest.raises(ToolError, match="anchored to task_id"):
        build_session_packet(task_id=task_b, session_id=started["session_id"])


def test_session_end_preserves_task_id_and_enforces_ownership(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "1")
    task_id = task_tools.task_add(title="Session end ownership")["id"]
    started = registry_tools.session_start(
        kind="work",
        agent="Desktop_Agent1",
        task_id=task_id,
    )
    session_id = started["session_id"]

    # Unauthenticated path still allows ending by id.
    closed = registry_tools.session_end(session_id, status="completed", summary="done")
    assert closed["closed"] is True

    rows = events.query_events(
        channels=[f"{CHANNEL_LIFECYCLE_PREFIX}.session_end"], limit=5, newest_first=True
    )
    assert rows
    assert rows[0]["payload"]["task_id"] == task_id

    other = registry_tools.session_start(
        kind="work",
        agent="Desktop_Agent1",
        task_id=task_id,
    )
    token = config.agent_identity_var.set("Desktop_Agent5")
    try:
        with pytest.raises(ToolError, match="owned by"):
            registry_tools.session_end(other["session_id"], status="completed")
    finally:
        config.agent_identity_var.reset(token)

    owner = config.agent_identity_var.set("Desktop_Agent1")
    try:
        ok = registry_tools.session_end(other["session_id"], status="completed")
        assert ok["closed"] is True
    finally:
        config.agent_identity_var.reset(owner)


def test_expired_approved_passes_are_omitted_from_packet(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_PASS_TTL_HOURS", "1")
    task_id = task_tools.task_add(title="Expired approval packet")["id"]
    ts = storage.now()
    old = (datetime.now(UTC) - timedelta(hours=48)).isoformat(timespec="seconds")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO approvals "
            "(tool, payload, requested_by, status, created_at, updated_at, resolved_at, resolved_by) "
            "VALUES (?, ?, ?, 'approved', ?, ?, ?, ?)",
            (
                "run_cmd",
                json.dumps({"command": ["echo", "old"], "task_id": task_id}),
                "Desktop_Agent4",
                old,
                old,
                old,
                "Operator",
            ),
        )
        conn.execute(
            "INSERT INTO approvals "
            "(tool, payload, requested_by, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (
                "run_cmd",
                json.dumps({"command": ["echo", "live"], "task_id": task_id}),
                "Desktop_Agent4",
                ts,
                ts,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    packet = build_session_packet(task_id=task_id, agent="Desktop_Agent4")
    statuses = {row["status"] for row in packet["approvals"]}
    assert "pending" in statuses
    assert all(row["status"] != "approved" for row in packet["approvals"])


def test_event_relates_matches_mirrored_task_ref_id():
    task_id = task_tools.task_add(title="Mirrored task event")["id"]
    events.record_event(
        channel="agent.task.updated",
        payload={"kind": "task", "ref_id": str(task_id), "summary": "claimed"},
        originator="Desktop_Agent1",
        notify=False,
    )
    packet = build_session_packet(task_id=task_id, agent="Desktop_Agent1")
    channels = {row["channel"] for row in packet["recent_events"]}
    assert "agent.task.updated" in channels
