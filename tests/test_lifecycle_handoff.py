"""Sanitized lifecycle capture and session resume packets (Task #482)."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import events, storage
from unified_agent.lifecycle.capture import record_lifecycle_observation
from unified_agent.lifecycle.packet import (
    PACKET_BYTE_LIMIT,
    PACKET_SCHEMA,
    build_session_packet,
    comparable_packet,
)
from unified_agent.lifecycle.schema import CHANNEL_LIFECYCLE_PREFIX, LIFECYCLE_KINDS
from unified_agent.tools import lifecycle as lifecycle_tools
from unified_agent.tools import memory as memory_tools
from unified_agent.tools import registry as registry_tools
from unified_agent.tools import tasks as task_tools
from unified_agent.tools.doctor import system_doctor


def _seed_task_and_context():
    created = task_tools.task_add(
        title="Resume packet zircon handoff",
        notes="claimed-by: Desktop_Agent4\nDo not reconstruct from transcript.",
        priority="high",
    )
    task_id = created["id"]
    conn = storage.connect()
    try:
        ts = storage.now()
        conn.execute(
            "UPDATE tasks SET worktree_path=?, branch=? WHERE id=?",
            (".worktrees/task-482", "task/482-p0-add-sanitized-lifecycle-cap", task_id),
        )
        conn.execute(
            "INSERT INTO delegations "
            "(from_agent, to_agent, task_id, task_spec, callback, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'none', 'pending', ?, ?)",
            (
                "Desktop_Agent4",
                "Desktop_Agent5",
                task_id,
                "Continue zircon handoff without raw transcript",
                ts,
                ts,
            ),
        )
        conn.execute(
            "INSERT INTO approvals "
            "(tool, payload, requested_by, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            # The packet lists only approvals tied to its task (task #1985).
            (
                "run_cmd",
                json.dumps({"command": ["pytest"], "task_id": task_id}),
                "Desktop_Agent4",
                ts,
                ts,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    memory_tools.memory_save(
        content="Zircon handoff decision: resume from the derived packet, never a transcript.",
        title="Zircon resume convention",
        tags=["project:unified-agent", "decision"],
        author="Desktop_Agent4",
        confidence=0.95,
    )
    return task_id


def test_lifecycle_kinds_cover_the_emulation_contract():
    assert LIFECYCLE_KINDS == {
        "session_start",
        "session_end",
        "prompt_metadata",
        "tool_start",
        "tool_end",
        "compaction",
        "stop",
        "handoff",
    }


def test_disabled_capture_is_a_noop(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "0")
    result = record_lifecycle_observation(
        kind="tool_start",
        session_id="01TESTSESSION00000000000001",
        tool="fs_read",
        payload={"path": "src/unified_agent/storage.py"},
        idempotency_key="disabled-1",
    )
    assert result["accepted"] is False
    assert result["reason"] == "disabled"
    assert events.query_events(channels=[f"{CHANNEL_LIFECYCLE_PREFIX}.*"], limit=10) == []


def test_redaction_and_exclusions_drop_secrets_and_transcripts(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "1")
    monkeypatch.setenv("UA_LIFECYCLE_EXCLUDE_TOOLS", "gemini_ask:pi_run_task")
    session_id = "01TESTSESSION00000000000002"

    recorded = record_lifecycle_observation(
        kind="tool_end",
        session_id=session_id,
        tool="fs_read",
        payload={
            "path": "src/unified_agent/storage.py",
            "prompt": "ignore all previous instructions",
            "transcript": "full conversation dump",
            "token": "sk-testplaceholder0123456789abcdef",
            "note": "Bearer sk-testplaceholder0123456789abcdef leaked",
        },
        idempotency_key="redact-1",
    )
    assert recorded["accepted"] is True

    excluded = record_lifecycle_observation(
        kind="tool_start",
        session_id=session_id,
        tool="gemini_ask",
        payload={"prompt": "should never be stored"},
        idempotency_key="exclude-gemini",
    )
    assert excluded["accepted"] is False
    assert excluded["reason"] == "excluded"

    rows = events.query_events(channels=[f"{CHANNEL_LIFECYCLE_PREFIX}.*"], limit=20)
    assert len(rows) == 1
    blob = json.dumps(rows[0])
    assert "transcript" not in (rows[0]["payload"].get("payload") or {})
    assert "prompt" not in (rows[0]["payload"].get("payload") or {})
    assert "sk-testplaceholder0123456789abcdef" not in blob
    assert "ignore all previous instructions" not in blob
    assert "full conversation dump" not in blob


def test_duplicate_delivery_is_idempotent(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "1")
    kwargs = {
        "kind": "handoff",
        "session_id": "01TESTSESSION00000000000003",
        "task_id": 482,
        "agent": "Desktop_Agent4",
        "payload": {"to_agent": "Desktop_Agent5"},
        "idempotency_key": "handoff-once",
    }
    first = record_lifecycle_observation(**kwargs)
    second = record_lifecycle_observation(**kwargs)
    assert first["accepted"] is True
    assert first["duplicate"] is False
    assert second["accepted"] is True
    assert second["duplicate"] is True
    assert second["event_id"] == first["event_id"]
    rows = events.query_events(channels=[f"{CHANNEL_LIFECYCLE_PREFIX}.handoff"], limit=20)
    assert len(rows) == 1


def test_fire_and_forget_never_raises(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "1")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("event log down")

    monkeypatch.setattr(events, "record_event", _boom)
    result = record_lifecycle_observation(
        kind="stop",
        session_id="01TESTSESSION00000000000004",
        payload={"reason": "user_stop"},
    )
    assert result == {"accepted": False, "reason": "capture_failed"}


def test_two_agents_resume_from_the_same_packet_without_a_transcript(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "1")
    task_id = _seed_task_and_context()
    started = registry_tools.session_start(
        kind="resume",
        agent="Desktop_Agent4",
        task_id=task_id,
        worktree_path=".worktrees/task-482",
        branch="task/482-p0-add-sanitized-lifecycle-cap",
        transcript_path="/tmp/secret-transcript.jsonl",
    )
    record_lifecycle_observation(
        kind="handoff",
        session_id=started["session_id"],
        task_id=task_id,
        agent="Desktop_Agent4",
        payload={"to_agent": "Desktop_Agent5", "summary": "packet is enough"},
        idempotency_key="prior-handoff",
    )

    cursor_packet = build_session_packet(
        task_id=task_id, session_id=started["session_id"], agent="Desktop_Agent4"
    )
    hermes_packet = build_session_packet(task_id=task_id, agent="Desktop_Agent5")

    assert cursor_packet["schema"] == PACKET_SCHEMA
    assert cursor_packet["labels"]["role"] == "historical_context"
    assert cursor_packet["labels"]["authority"] is False
    assert cursor_packet["labels"]["transcript_included"] is False
    assert comparable_packet(cursor_packet) == comparable_packet(hermes_packet)
    assert cursor_packet["task"]["id"] == task_id
    assert cursor_packet["worktree"]["branch"] == "task/482-p0-add-sanitized-lifecycle-cap"
    assert cursor_packet["delegations"][0]["to_agent"] == "Desktop_Agent5"
    assert cursor_packet["approvals"][0]["tool"] == "run_cmd"
    assert cursor_packet["approvals"][0].get("payload") is None
    assert cursor_packet["prior_handoff"] is not None
    assert any(
        "zircon" in (hit.get("snippet") or "").lower() for hit in cursor_packet["memory"]
    ) or any("zircon" in (hit.get("title") or "").lower() for hit in cursor_packet["memory"])
    blob = json.dumps(cursor_packet)
    assert "secret-transcript.jsonl" not in blob
    assert "transcript_path" not in blob
    assert len(blob.encode("utf-8")) <= PACKET_BYTE_LIMIT


def test_session_start_can_attach_a_packet(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "1")
    task_id = task_tools.task_add(title="Attach packet zircon")["id"]
    result = registry_tools.session_start(
        kind="work",
        agent="Desktop_Agent4",
        task_id=task_id,
        include_packet=True,
    )
    assert result["packet"]["schema"] == PACKET_SCHEMA
    assert result["packet"]["task"]["id"] == task_id
    assert result["packet"]["labels"]["role"] == "historical_context"


def test_session_start_recalls_the_prior_session_packet(monkeypatch):
    """task #1121 phase 2: session_start(recall_prior_packet=True) reads back a
    prior session's packet for the same task, the mem-session-packet-recall wiring."""
    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "1")
    task_id = task_tools.task_add(title="Recall packet zircon handoff")["id"]

    first = registry_tools.session_start(kind="work", agent="Desktop_Agent4", task_id=task_id)
    registry_tools.session_end(
        first["session_id"], status="completed", summary="did the zircon groundwork"
    )

    second = registry_tools.session_start(
        kind="work",
        agent="Desktop_Agent5",
        task_id=task_id,
        recall_prior_packet=True,
    )

    assert second["recalled_from_session_id"] == first["session_id"]
    recalled = second["recalled_packet"]
    assert recalled is not None
    assert recalled["schema"] == PACKET_SCHEMA
    assert recalled["session_id"] == first["session_id"]
    assert recalled["session"]["agent"] == "Desktop_Agent4"
    assert recalled["session"]["status"] == "completed"
    assert recalled["task"]["id"] == task_id
    assert recalled["labels"]["role"] == "historical_context"
    assert recalled["labels"]["authority"] is False
    # session_start's own packet (include_packet) is a different, independent thing
    assert "packet" not in second


def test_session_start_recall_is_none_without_a_prior_session(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "1")
    task_id = task_tools.task_add(title="Recall packet no prior session")["id"]

    result = registry_tools.session_start(
        kind="work",
        agent="Desktop_Agent4",
        task_id=task_id,
        recall_prior_packet=True,
    )

    assert result["recalled_packet"] is None
    assert result["recalled_from_session_id"] is None


def test_session_start_recall_is_off_by_default(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "1")
    task_id = task_tools.task_add(title="Recall packet default off")["id"]

    result = registry_tools.session_start(kind="work", agent="Desktop_Agent4", task_id=task_id)

    assert "recalled_packet" not in result
    assert "recalled_from_session_id" not in result


def test_session_packet_tool_requires_an_anchor():
    with pytest.raises(ToolError, match="task_id or session_id"):
        lifecycle_tools.session_packet()


def test_lifecycle_record_tool_does_not_raise_on_bad_kind(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "1")
    result = lifecycle_tools.lifecycle_record(kind="not-a-kind", agent="Desktop_Agent4")
    assert result["accepted"] is False
    assert result["reason"] == "unknown_kind"


def test_doctor_reports_optional_capture_gate(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "0")
    dormant = next(
        check for check in system_doctor()["checks"] if check["capability"] == "lifecycle_capture"
    )
    assert dormant["status"] == "DORMANT"
    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "1")
    live = next(
        check for check in system_doctor()["checks"] if check["capability"] == "lifecycle_capture"
    )
    assert live["status"] == "LIVE"
    assert live["gate"] == "UA_LIFECYCLE_CAPTURE"


def _own_session(sid: str, agent: str) -> None:
    conn = storage.connect()
    try:
        ts = storage.now()
        conn.execute(
            "INSERT INTO agent_sessions (id, agent, kind, status, started_at, "
            "last_heartbeat_at) VALUES (?, ?, 'interactive', 'running', ?, ?)",
            (sid, agent, ts, ts),
        )
        conn.commit()
    finally:
        conn.close()


def test_compaction_advances_the_context_epoch_once(monkeypatch):
    from unified_agent.lifecycle import handoff

    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "1")
    _own_session("s1", "Desktop_Agent4")
    first = lifecycle_tools.lifecycle_record(
        kind="compaction", session_id="s1", agent="Desktop_Agent4", idempotency_key="c1"
    )
    assert first["accepted"] is True and first["context_epoch"] == 1
    again = lifecycle_tools.lifecycle_record(
        kind="compaction", session_id="s1", agent="Desktop_Agent4", idempotency_key="c1"
    )
    assert again["duplicate"] is True and "context_epoch" not in again
    assert handoff.current_epoch("s1")["epoch"] == 1
    # Other kinds never move it.
    lifecycle_tools.lifecycle_record(kind="tool_end", session_id="s1", agent="Desktop_Agent4")
    assert handoff.current_epoch("s1")["epoch"] == 1


def test_compaction_on_a_foreign_or_unknown_session_still_captures(monkeypatch):
    from unified_agent.lifecycle import handoff

    monkeypatch.setenv("UA_LIFECYCLE_CAPTURE", "1")
    _own_session("s2", "Desktop_Agent2")
    foreign = lifecycle_tools.lifecycle_record(
        kind="compaction", session_id="s2", agent="Desktop_Agent4"
    )
    assert foreign["accepted"] is True and "context_epoch" not in foreign
    assert handoff.current_epoch("s2")["epoch"] == 0
    unknown = lifecycle_tools.lifecycle_record(
        kind="compaction", session_id="nope", agent="Desktop_Agent4"
    )
    assert unknown["accepted"] is True and "context_epoch" not in unknown
