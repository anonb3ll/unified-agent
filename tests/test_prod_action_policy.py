import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, safety


def test_prod_action_requires_scope_and_queues_approval(monkeypatch):
    monkeypatch.setenv("UA_AGENT_SCOPES", "Desktop_Agent2:merge")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    calls = []
    monkeypatch.setattr(
        "unified_agent.tools.approvals.check_approval_gate",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    try:
        safety.check_prod_action_gate("git_merge_task", {"task_id": 42})
    finally:
        config.agent_identity_var.reset(identity)
    assert calls == [
        (
            ("git_merge_task", {"task_id": 42, "action": "merge", "agent": "Desktop_Agent2"}),
            {"requested_by": "Desktop_Agent2", "force": True},
        )
    ]


def test_prod_action_denies_missing_scope(monkeypatch):
    monkeypatch.setenv("UA_AGENT_SCOPES", "Desktop_Agent2:delete")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    try:
        with pytest.raises(ToolError, match="not scoped.*merge"):
            safety.check_prod_action_gate("git_merge_task", {"task_id": 42})
    finally:
        config.agent_identity_var.reset(identity)


def test_revoked_agent_is_denied_before_approval(monkeypatch):
    monkeypatch.setenv("UA_AGENT_SCOPES", "Desktop_Agent2:merge")
    monkeypatch.setenv("UA_REVOKED_AGENTS", "Desktop_Agent2")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    try:
        with pytest.raises(ToolError, match="revoked"):
            safety.check_prod_action_gate("git_merge_task", {"task_id": 42})
    finally:
        config.agent_identity_var.reset(identity)


def test_merge_dry_run_skips_mutating_approval(monkeypatch):
    monkeypatch.setenv("UA_AGENT_SCOPES", "Desktop_Agent2:merge")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    calls = []
    monkeypatch.setattr(
        "unified_agent.tools.approvals.check_approval_gate",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    try:
        safety.check_prod_action_gate("git_merge_task", {"task_id": 42, "dry_run": True})
    finally:
        config.agent_identity_var.reset(identity)
    assert calls == []
