"""Fleet control actions (task #799): approval-gated local service restart."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, fleet_actions, safety, storage
from unified_agent.tools.approvals import ApprovalPendingError, approval_resolve


def test_prod_action_vocabulary_includes_fleet_restart():
    assert safety.PROD_ACTIONS["fleet_host_action"] == "restart"


def test_stub_actions_denied_by_default(monkeypatch):
    monkeypatch.setenv("UA_AGENT_SCOPES", "Desktop_Person:restart")
    identity = config.agent_identity_var.set("Desktop_Person")
    try:
        with pytest.raises(ToolError, match="denied-by-default"):
            fleet_actions.fleet_host_action(
                "reap_executor",
                config.current_source_host(),
                actor="Desktop_Person",
            )
    finally:
        config.agent_identity_var.reset(identity)


def test_unknown_host_refused(monkeypatch):
    monkeypatch.setenv("UA_AGENT_SCOPES", "Desktop_Person:restart")
    monkeypatch.setattr(fleet_actions, "_registered_hosts", lambda: {"hub.local"})
    identity = config.agent_identity_var.set("Desktop_Person")
    try:
        with pytest.raises(ToolError, match="not a registered fleet host"):
            fleet_actions.fleet_host_action(
                "restart_service",
                "evil.example",
                unit="unified-agent-executor.service",
                actor="Desktop_Person",
            )
    finally:
        config.agent_identity_var.reset(identity)


def test_remote_host_refused_even_when_registered(monkeypatch):
    monkeypatch.setenv("UA_AGENT_SCOPES", "Desktop_Person:restart")
    monkeypatch.setattr(config, "current_source_host", lambda: "hub.local")
    monkeypatch.setattr(
        fleet_actions, "_registered_hosts", lambda: {"hub.local", "desktop-host.local"}
    )
    identity = config.agent_identity_var.set("Desktop_Person")
    try:
        with pytest.raises(ToolError, match="remote host control is not available"):
            fleet_actions.fleet_host_action(
                "restart_service",
                "desktop-host.local",
                unit="unified-agent-executor.service",
                actor="Desktop_Person",
            )
    finally:
        config.agent_identity_var.reset(identity)


def test_restart_queues_approval_then_executes_with_audit(monkeypatch):
    # Desktop_Person and Operator are one person, so the requester is an agent.
    monkeypatch.setenv("UA_AGENT_SCOPES", "Desktop_Agent5:restart")
    host = "hub.local"
    monkeypatch.setattr(config, "current_source_host", lambda: host)
    monkeypatch.setattr(fleet_actions, "_registered_hosts", lambda: {host})
    monkeypatch.setattr(
        fleet_actions,
        "_probe_unit",
        lambda unit: {"LoadState": "loaded", "ActiveState": "active", "NRestarts": "1"},
    )
    calls = []

    def fake_run(cmd, check=False, capture_output=True, text=True, timeout=30):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(fleet_actions.subprocess, "run", fake_run)

    identity = config.agent_identity_var.set("Desktop_Agent5")
    try:
        with pytest.raises(ApprovalPendingError) as pending:
            fleet_actions.fleet_host_action(
                "restart_service",
                host,
                unit="unified-agent-executor.service",
                actor="Desktop_Agent5",
            )
        assert pending.value.approval_id is not None
        human = config.agent_identity_var.set("Operator")
        try:
            approval_resolve(pending.value.approval_id, "approve")
        finally:
            config.agent_identity_var.reset(human)

        result = fleet_actions.fleet_host_action(
            "restart_service",
            host,
            unit="unified-agent-executor.service",
            actor="Desktop_Agent5",
        )
    finally:
        config.agent_identity_var.reset(identity)

    assert result["status"] == "ok"
    assert result["host"] == host
    assert result["unit"] == "unified-agent-executor.service"
    assert result["actor"] == "Desktop_Agent5"
    assert result["audit_event_id"]
    assert result["state_after"]["ActiveState"] == "active"
    assert calls, "expected a restart command to run after approval"

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT channel, payload, originator FROM event_log WHERE ulid=?",
            (result["audit_event_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["channel"] == "agent.fleet.control"
    assert row["originator"] == "Desktop_Agent5"
    assert "restart_service" in row["payload"]


def test_unscoped_agent_denied_before_approval(monkeypatch):
    monkeypatch.setenv("UA_AGENT_SCOPES", "Desktop_Person:restart")
    monkeypatch.setattr(config, "current_source_host", lambda: "hub.local")
    monkeypatch.setattr(fleet_actions, "_registered_hosts", lambda: {"hub.local"})
    identity = config.agent_identity_var.set("Desktop_Agent2")
    try:
        with pytest.raises(ToolError, match="not scoped"):
            fleet_actions.fleet_host_action(
                "restart_service",
                "hub.local",
                unit="unified-agent-executor.service",
                actor="Desktop_Agent2",
            )
    finally:
        config.agent_identity_var.reset(identity)
