"""project_build MCP tool (task #830): fail-closed root registration + hash chain."""

from __future__ import annotations

import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, storage
from unified_agent.server import mcp
from unified_agent.tools.tasks import project_build, task_add, task_list


def _task_count() -> int:
    conn = storage.connect()
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    finally:
        conn.close()


def _event_rows() -> list:
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT ulid, channel, payload, originator, prev_hash, entry_hash "
            "FROM event_log ORDER BY ROWID ASC"
        ).fetchall()
    finally:
        conn.close()


def _set_claimed_by(task_id: int, agent: str | None) -> None:
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET claimed_by=? WHERE id=?", (agent, task_id))
        conn.commit()
    finally:
        conn.close()


def test_project_build_requires_registered_identity(monkeypatch):
    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    token = config.agent_identity_var.set(None)
    try:
        spec = task_add("spec", notes="[project-spec]\nbuild it")
        before_tasks = _task_count()
        before_events = len(_event_rows())
        with pytest.raises(ToolError, match="registered agent identity"):
            project_build(spec["id"])
        assert _task_count() == before_tasks
        assert len(_event_rows()) == before_events
    finally:
        config.agent_identity_var.reset(token)


def test_project_build_requires_audit_hash_chain(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "core")
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        spec = task_add("core spec", notes="[project-spec]")
        before_events = len(_event_rows())
        with pytest.raises(PermissionError, match="audit_hash_chain"):
            project_build(spec["id"])
        assert len(_event_rows()) == before_events
    finally:
        config.agent_identity_var.reset(token)


def test_project_build_missing_task_no_partial_state():
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        before_tasks = _task_count()
        before_events = len(_event_rows())
        with pytest.raises(ToolError, match="no task with id 99999"):
            project_build(99999)
        assert _task_count() == before_tasks
        assert len(_event_rows()) == before_events
    finally:
        config.agent_identity_var.reset(token)


def test_project_build_missing_marker_no_partial_state():
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        spec = task_add("almost", notes="this discusses `[project-spec]` only")
        before_tasks = _task_count()
        before_events = len(_event_rows())
        with pytest.raises(ToolError, match=r"does not carry \[project-spec\]"):
            project_build(spec["id"])
        assert _task_count() == before_tasks
        assert len(_event_rows()) == before_events
    finally:
        config.agent_identity_var.reset(token)


def test_project_build_wrong_claimant_no_partial_state():
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        spec = task_add("claimed spec", notes="[project-spec]\ndo the work")
        _set_claimed_by(spec["id"], "Desktop_Agent1")
        before_tasks = _task_count()
        before_events = len(_event_rows())
        with pytest.raises(ToolError, match="claimed by Desktop_Agent1, not Desktop_Agent4"):
            project_build(spec["id"])
        assert _task_count() == before_tasks
        assert len(_event_rows()) == before_events
    finally:
        config.agent_identity_var.reset(token)


def test_project_build_revalidates_inside_event_transaction(monkeypatch):
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        spec = task_add("racing spec", notes="[project-spec]")
        before_events = len(_event_rows())
        record_event = events.record_event

        def claim_immediately_before_append(*args, **kwargs):
            _set_claimed_by(spec["id"], "Desktop_Agent1")
            return record_event(*args, **kwargs)

        monkeypatch.setattr(events, "record_event", claim_immediately_before_append)
        with pytest.raises(ToolError, match="claimed by Desktop_Agent1, not Desktop_Agent4"):
            project_build(spec["id"])
        assert len(_event_rows()) == before_events
    finally:
        config.agent_identity_var.reset(token)


def test_project_build_success_records_identity_on_hash_chain():
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        spec = task_add("real spec", notes="[project-spec]\npointer-to-task")
        before_tasks = _task_count()
        result = project_build(spec["id"])
        assert result["project_id"] == spec["id"]
        assert result["parent_task_id"] == spec["id"]
        assert result["identity"] == "Desktop_Agent4"
        assert result["event_ulid"]
        # Root only: no extra task row.
        assert _task_count() == before_tasks
        listed = {t["id"]: t for t in task_list(status="all")["tasks"]}
        assert listed[spec["id"]]["lineage"] == []

        rows = _event_rows()
        assert len(rows) >= 1
        built = [r for r in rows if r["channel"] == events.CHANNEL_PROJECT_BUILT]
        assert len(built) == 1
        assert built[0]["originator"] == "Desktop_Agent4"
        assert built[0]["ulid"] == result["event_ulid"]
        assert built[0]["entry_hash"]
        assert built[0]["prev_hash"]
        report = events.verify_event_chain()
        assert report["verified"] is True
        assert report["head_hash"] == rows[-1]["entry_hash"]
    finally:
        config.agent_identity_var.reset(token)


def test_project_build_allows_self_claimed_spec():
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        spec = task_add("mine", notes="[project-spec]")
        _set_claimed_by(spec["id"], "Desktop_Agent4")
        result = project_build(spec["id"])
        assert result["project_id"] == spec["id"]
        assert result["identity"] == "Desktop_Agent4"
    finally:
        config.agent_identity_var.reset(token)


@pytest.mark.anyio
async def test_project_build_mcp_roundtrip_has_structured_output_and_text_fallback():
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        spec = task_add("MCP project spec", notes="[project-spec]\nship it")
        async with Client(mcp) as client:
            result = await client.session.call_tool("project_build", {"parent_task_id": spec["id"]})
        assert not result.is_error
        assert result.structured_content["project_id"] == spec["id"]
        assert result.structured_content["identity"] == "Desktop_Agent4"
        assert result.content[0].type == "text"
    finally:
        config.agent_identity_var.reset(token)
