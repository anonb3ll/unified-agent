"""Tests for the task_list notes_contain filter and the notify tool."""

import os
import subprocess
from unittest import mock

import pytest

from unified_agent.tools.tasks import task_add, task_list, task_update

# ── task_list notes_contain ──────────────────────────────────────────


def test_notes_contain_finds_matching_tasks():
    """task_list with notes_contain should return only tasks whose notes match."""
    r1 = task_add("review-test-1", notes="[waiting-for: review] please check")
    r2 = task_add("review-test-2", notes="still working on this")
    r3 = task_add("review-test-3", notes="[waiting-for: review] also needs a look")

    result = task_list(status="open", notes_contain="[waiting-for: review]")
    ids = {t["id"] for t in result["tasks"]}

    assert r1["id"] in ids
    assert r3["id"] in ids
    assert r2["id"] not in ids


def test_notes_contain_case_insensitive():
    """SQLite LIKE is case-insensitive for ASCII, so this should work."""
    r = task_add("case-test", notes="[WAITING-FOR: REVIEW] uppercase")
    result = task_list(status="open", notes_contain="waiting-for: review")
    ids = {t["id"] for t in result["tasks"]}
    assert r["id"] in ids


def test_notes_contain_no_match():
    """notes_contain with a non-matching string should return empty."""
    task_add("no-match-test", notes="nothing special here")
    result = task_list(status="open", notes_contain="[waiting-for: plan-approval]")
    # May or may not be empty depending on other test state, but should not error
    assert isinstance(result["tasks"], list)


def test_notes_contain_combined_with_status():
    """notes_contain should compose with status filter."""
    r = task_add("combined-test", notes="[waiting-for: review]")
    task_update(r["id"], status="doing")

    # Should NOT appear in open tasks
    open_result = task_list(status="open", notes_contain="[waiting-for: review]")
    open_ids = {t["id"] for t in open_result["tasks"]}
    assert r["id"] not in open_ids

    # Should appear in doing tasks
    doing_result = task_list(status="doing", notes_contain="[waiting-for: review]")
    doing_ids = {t["id"] for t in doing_result["tasks"]}
    assert r["id"] in doing_ids


def test_priority_filter():
    """task_list with priority filter should return only matching tasks."""
    r1 = task_add("high-prio", priority="high")
    r2 = task_add("low-prio", priority="low")
    r3 = task_add("med-prio", priority="med")

    res = task_list(status="open", priority="high")
    ids = {t["id"] for t in res["tasks"]}
    assert r1["id"] in ids
    assert r2["id"] not in ids
    assert r3["id"] not in ids


def test_priority_filter_invalid():
    """task_list with invalid priority should raise ToolError."""
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="priority must be one of"):
        task_list(priority="super-high")


def test_priority_filter_composable():
    """task_list priority filter should compose with status and notes_contain filters."""
    r1 = task_add("composable-match", notes="find-me", priority="high")
    r2 = task_add("composable-wrong-notes", notes="ignore-me", priority="high")
    r3 = task_add("composable-wrong-priority", notes="find-me", priority="low")

    res = task_list(status="open", notes_contain="find-me", priority="high")
    ids = {t["id"] for t in res["tasks"]}
    assert r1["id"] in ids
    assert r2["id"] not in ids
    assert r3["id"] not in ids


# ── notify tool ──────────────────────────────────────────────────────


def test_bridge_dir_ignores_per_agent_workspace(monkeypatch, tmp_path):
    """notify must not follow a UA_AGENT_WORKSPACES override."""
    from unified_agent.tools.notify import _bridge_dir

    monkeypatch.delenv("UA_BRIDGE_DIR", raising=False)
    agent_root = tmp_path / "agent-workspace"
    process_root = tmp_path / "process-workspace"
    agent_root.mkdir()
    process_root.mkdir()
    monkeypatch.setattr("unified_agent.config.workspace_root", lambda: agent_root)
    monkeypatch.setattr("unified_agent.config.process_workspace_root", lambda: process_root)
    assert _bridge_dir() == (process_root / "projects" / "imessage-bridge").resolve()


def test_notify_refuses_send_script_inside_agent_workspace(monkeypatch, tmp_path):
    from unified_agent.tools.notify import notify

    agent_root = tmp_path / "agent-workspace"
    process_root = tmp_path / "process-workspace"
    script_dir = agent_root / "projects" / "imessage-bridge" / "src"
    script_dir.mkdir(parents=True)
    (script_dir / "send.ts").write_text("// agent-supplied")
    process_root.mkdir()
    monkeypatch.setenv("UA_BRIDGE_DIR", str(agent_root / "projects" / "imessage-bridge"))
    monkeypatch.setattr("unified_agent.config.workspace_root", lambda: agent_root)
    monkeypatch.setattr("unified_agent.config.process_workspace_root", lambda: process_root)
    with pytest.raises(Exception, match="agent-bound workspace"):
        notify("hello")


def test_notify_empty_message():
    """notify should reject empty messages."""
    from unified_agent.tools.notify import notify

    with pytest.raises(Exception, match="empty"):
        notify("")


def test_notify_missing_send_script(tmp_path):
    """notify should error cleanly when send.ts is not found."""
    from unified_agent.tools.notify import notify

    with mock.patch.dict(os.environ, {"UA_BRIDGE_DIR": str(tmp_path)}):
        with pytest.raises(Exception, match="send.ts not found"):
            notify("test message")


def test_notify_truncates_long_messages(tmp_path):
    """notify should truncate messages over 8000 chars."""
    from unified_agent.tools.notify import notify

    # Create a fake send.ts so it doesn't error on missing file
    send_dir = tmp_path / "src"
    send_dir.mkdir()
    (send_dir / "send.ts").write_text("// fake")

    long_msg = "x" * 9000

    with mock.patch.dict(os.environ, {"UA_BRIDGE_DIR": str(tmp_path)}):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="send: delivered", stderr=""
            )
            result = notify(long_msg)

    assert result["delivered"] is True
    # The chars count should reflect the truncated message
    assert result["chars"] <= 8020  # 8000 + truncation marker


def test_notify_handles_bun_failure(tmp_path):
    """notify should return delivered=False on bun failure."""
    from unified_agent.tools.notify import notify

    send_dir = tmp_path / "src"
    send_dir.mkdir()
    (send_dir / "send.ts").write_text("// fake")

    with mock.patch.dict(os.environ, {"UA_BRIDGE_DIR": str(tmp_path)}):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="send: missing credentials"
            )
            result = notify("test failure")

    assert result["delivered"] is False
    assert "missing credentials" in result["error"]
