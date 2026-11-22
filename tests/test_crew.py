"""FirstMate-style crew live view + intervene (task #1025)."""

from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import crew, registry
from unified_agent.tools import tasks as task_tools


def test_crew_live_view_unknown_session():
    with pytest.raises(ToolError, match="unknown session"):
        crew.crew_live_view("nosuch")


def test_crew_live_view_and_intervene_records_message_note_and_worktree(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    task = task_tools.task_add("crew live target")
    sid = registry.start_session(
        "Desktop_Agent1",
        "pi_run",
        task_id=task["id"],
        worktree_path=str(worktree),
    )
    assert sid

    empty = crew.crew_live_view(sid)
    assert empty["live"] is True
    assert empty["conversation_id"] == f"crew:{sid}"
    assert empty["messages"] == []
    assert empty["interventions_path"] == str(worktree / ".ua" / "interventions.jsonl")

    result = crew.intervene_on_session(
        session_id=sid,
        body="please pause and summarize",
        from_agent="Desktop_Person",
        message_id="01CREWMSG0000000000000000001",
    )
    assert result["accepted"] is True
    assert result["duplicate"] is False
    assert result["from_agent"] == "Operator"
    assert result["to_agent"] == "Desktop_Agent1"
    assert result["note_recorded"] is True
    assert result["worktree_written"] is True

    view = crew.crew_live_view(sid)
    assert len(view["messages"]) == 1
    assert view["messages"][0]["body"] == "please pause and summarize"
    assert view["messages"][0]["conversation_id"] == f"crew:{sid}"
    assert len(view["worktree_interventions"]) == 1
    assert view["worktree_interventions"][0]["body"] == "please pause and summarize"

    drop = worktree / ".ua" / "interventions.jsonl"
    lines = [json.loads(line) for line in drop.read_text(encoding="utf-8").splitlines() if line]
    assert lines[0]["from_agent"] == "Operator"
    assert lines[0]["session_id"] == sid

    notes = task_tools.task_list(status="all", limit=20)["tasks"]
    target = next(t for t in notes if t["id"] == task["id"])
    assert "[intervention @" in (target.get("notes") or "")
    assert "please pause and summarize" in (target.get("notes") or "")


def test_intervene_rejects_empty_body_and_self():
    sid = registry.start_session("Desktop_Agent1", "pi_run")
    assert sid
    with pytest.raises(ToolError, match="non-empty"):
        crew.intervene_on_session(session_id=sid, body="  ", from_agent="Operator")
    with pytest.raises(ToolError, match="must differ"):
        crew.intervene_on_session(
            session_id=sid,
            body="hello",
            from_agent="Desktop_Agent1",
        )


def test_intervene_is_idempotent_on_message_id(tmp_path):
    worktree = tmp_path / "wt2"
    worktree.mkdir()
    sid = registry.start_session(
        "Desktop_Agent5",
        "dispatch",
        worktree_path=str(worktree),
    )
    assert sid
    mid = "01CREWMSG0000000000000000002"
    first = crew.intervene_on_session(
        session_id=sid,
        body="nudge once",
        from_agent="Operator",
        message_id=mid,
    )
    second = crew.intervene_on_session(
        session_id=sid,
        body="nudge once",
        from_agent="Operator",
        message_id=mid,
    )
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["worktree_written"] is False
    drop = worktree / ".ua" / "interventions.jsonl"
    assert len(drop.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_crew_view_tool_matches_api_payload(tmp_path):
    worktree = tmp_path / "wt_view"
    worktree.mkdir()
    task = task_tools.task_add("crew view target")
    sid = registry.start_session(
        "Desktop_Agent1",
        "pi_run",
        task_id=task["id"],
        worktree_path=str(worktree),
    )
    assert sid

    # Unknown session failure mode
    with pytest.raises(ToolError, match="unknown session nosuch"):
        crew.crew_view("nosuch")

    payload = crew.crew_view(sid)
    expected = crew.crew_live_view(sid)
    for field in ("duration_s", "heartbeat_age_s"):
        payload["session"].pop(field, None)
        expected["session"].pop(field, None)
    assert payload == expected
    assert payload["live"] is True
    assert payload["conversation_id"] == f"crew:{sid}"
    assert payload["messages"] == []


def test_crew_intervene_tool_guards_and_delivery(tmp_path):
    worktree = tmp_path / "wt_intervene"
    worktree.mkdir()
    task = task_tools.task_add("crew intervene target")
    sid = registry.start_session(
        "Desktop_Agent1",
        "pi_run",
        task_id=task["id"],
        worktree_path=str(worktree),
    )
    assert sid

    # Unknown session failure mode
    with pytest.raises(ToolError, match="unknown session nosuch"):
        crew.crew_intervene("nosuch", "hello", from_agent="Operator")

    # Empty body refusal
    with pytest.raises(ToolError, match="non-empty"):
        crew.crew_intervene(sid, "   ", from_agent="Operator")

    # Message cap refusal (> 2000 chars)
    with pytest.raises(ToolError, match="at most 2000 characters"):
        crew.crew_intervene(sid, "x" * 2001, from_agent="Operator")

    # Self-send refusal
    with pytest.raises(ToolError, match="must differ"):
        crew.crew_intervene(sid, "self intervention", from_agent="Desktop_Agent1")

    # Successful delivery
    mid = "01CREWMCP0000000000000000001"
    res1 = crew.crew_intervene(sid, "pause and report", message_id=mid, from_agent="Operator")
    assert res1["accepted"] is True
    assert res1["duplicate"] is False
    assert res1["from_agent"] == "Operator"
    assert res1["to_agent"] == "Desktop_Agent1"
    assert res1["note_recorded"] is True
    assert res1["worktree_written"] is True

    # Duplicate suppression
    res2 = crew.crew_intervene(sid, "pause and report", message_id=mid, from_agent="Operator")
    assert res2["accepted"] is True
    assert res2["duplicate"] is True
    assert res2["worktree_written"] is False

    # Assert view payload matches live view
    view = crew.crew_view(sid)
    assert len(view["messages"]) == 1
    assert view["messages"][0]["body"] == "pause and report"
    expected_view = crew.crew_live_view(sid)
    for field in ("duration_s", "heartbeat_age_s"):
        view["session"].pop(field, None)
        expected_view["session"].pop(field, None)
    assert view == expected_view


def test_crew_intervene_resolves_verified_caller():
    from unified_agent import config

    sid = registry.start_session("Desktop_Agent1", "pi_run")
    assert sid
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        res = crew.crew_intervene(sid, "delegated note")
        assert res["accepted"] is True
        assert res["from_agent"] == "Desktop_Agent2"
        assert res["to_agent"] == "Desktop_Agent1"
    finally:
        config.agent_identity_var.reset(token)
