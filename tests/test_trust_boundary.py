import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.tools.delegate import delegate
from unified_agent.tools.tasks import task_add


def test_untrusted_agent_delegation_blocked(monkeypatch):
    # Desktop_Agent5 is untrusted, Desktop_Agent1 is trusted
    monkeypatch.setenv("UA_UNTRUSTED_AGENTS", "Desktop_Agent5")

    assert config.is_agent_untrusted("Desktop_Agent5") is True
    assert config.is_agent_untrusted("Desktop_Agent1") is False

    with pytest.raises(ToolError, match="Trust boundary violation"):
        delegate(
            task_spec="Run privileged command",
            from_agent="Desktop_Agent5",
            to_agent="Desktop_Agent1",
        )


def test_untrusted_agent_delegation_allowed_to_untrusted(monkeypatch):
    # Desktop_Agent5 is untrusted, Laptop_Agent1 is untrusted
    monkeypatch.setenv("UA_UNTRUSTED_AGENTS", "Desktop_Agent5,Laptop_Agent1")

    r = delegate(
        task_spec="Run untrusted commands",
        from_agent="Desktop_Agent5",
        to_agent="Laptop_Agent1",
    )

    # Verify child task was created and is marked untrusted
    conn = storage.connect()
    try:
        row = conn.execute("SELECT untrusted FROM tasks WHERE id=?", (r["task_id"],)).fetchone()
        assert row["untrusted"] == 1
    finally:
        conn.close()


def test_untrusted_parent_task_delegation_blocked(monkeypatch):
    # Desktop_Agent5 is untrusted, Desktop_Agent1 is trusted
    monkeypatch.setenv("UA_UNTRUSTED_AGENTS", "Desktop_Agent5")

    # Create an untrusted parent task
    t_parent = task_add("Untrusted parent task", untrusted=True)

    # Trusted delegator attempts to delegate child task of untrusted parent to another trusted agent
    with pytest.raises(ToolError, match="Trust boundary violation"):
        delegate(
            task_spec="Run privileged child command",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
            parent_task_id=t_parent["id"],
        )


def test_untrusted_parent_task_propagation_allowed(monkeypatch):
    monkeypatch.setenv("UA_UNTRUSTED_AGENTS", "Desktop_Agent5")

    # Create an untrusted parent task
    t_parent = task_add("Untrusted parent task", untrusted=True)

    # Delegate child task of untrusted parent to untrusted agent
    r = delegate(
        task_spec="Run untrusted child command",
        from_agent="Desktop_Agent1",
        to_agent="Desktop_Agent5",
        parent_task_id=t_parent["id"],
    )

    # Verify child task is marked untrusted
    conn = storage.connect()
    try:
        row = conn.execute("SELECT untrusted FROM tasks WHERE id=?", (r["task_id"],)).fetchone()
        assert row["untrusted"] == 1
    finally:
        conn.close()
