"""Reviewed presets only subtract permissions, using verified caller context."""

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, policy


@pytest.fixture
def reviewed_policy(tmp_path, monkeypatch):
    path = tmp_path / "policy.yaml"
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    token = config.agent_identity_var.set("Desktop_Agent2")

    def write(block, **values):
        import yaml

        path.write_text(yaml.safe_dump({"version": 1, "trust_presets": block, **values}))
        return path

    yield write
    config.agent_identity_var.reset(token)


@pytest.mark.parametrize(
    "block",
    [
        None,
        [],
        "inspection",
        {"default": "host"},
        {"roots": ["/"]},
        {"agents": []},
        {"agents": {"Desktop_Agent2": "host"}},
        {"agents": {"Desktop_Cluade": "inspection"}},
        {"agents": {"desktop_agent2": "inspection"}},
        {"default": True},
    ],
)
def test_malformed_preset_policy_fails_closed(reviewed_policy, block):
    reviewed_policy(block)
    with pytest.raises(ToolError):
        policy.load_policy()


@pytest.mark.parametrize("name", ["inspection", "configured-workspace", "active-worktree"])
def test_presets_deny_native_and_indirect_mutations_even_with_allow_rule(reviewed_policy, name):
    reviewed_policy({"default": name}, enabled=False)
    for tool in (
        "run_cmd",
        "git_diff_staged",
        "system_doctor",
        "delegate",
        "workflow_run",
        "task_claim",
        "task_update",
        "config_revision_commit",
        "unknown_tool",
    ):
        with pytest.raises(ToolError):
            policy.evaluate_policy(tool, {"read_only": True, "agent": "Desktop_Person"})


def test_policy_gate_enforces_inspection_on_http_write(reviewed_policy):
    reviewed_policy({"default": "inspection"}, enabled=False)
    with pytest.raises(ToolError):
        policy.evaluate_policy("fs_write", {"path": "readme.md"}, originator="Desktop_Person")


def test_verified_identity_required_and_body_cannot_override_binding(reviewed_policy, monkeypatch):
    reviewed_policy({"agents": {"Desktop_Agent2": "inspection"}})
    monkeypatch.setenv("UA_AGENT_LABEL", "Desktop_Person")
    with pytest.raises(ToolError):
        policy.evaluate_policy("fs_write", {"agent": "Desktop_Person", "trust_preset": None})
    token = config.agent_identity_var.set(None)
    try:
        with pytest.raises(ToolError):
            policy.evaluate_policy("fs_read", {}, originator="Desktop_Person")
    finally:
        config.agent_identity_var.reset(token)


def test_reviewed_read_and_own_report_remain_subject_to_existing_policy(reviewed_policy):
    reviewed_policy({"default": "inspection"})
    policy.evaluate_policy("fs_read", {"path": "readme.md"})
    policy.evaluate_policy("dispatch_report_self", {"state": "finished", "run_id": "own"})
    reviewed_policy(
        {"default": "inspection"},
        rules=[
            {
                "name": "still-denied",
                "type": "tool",
                "tool": "dispatch_report_self",
                "action": "deny",
            }
        ],
    )
    with pytest.raises(ToolError):
        policy.evaluate_policy("dispatch_report_self", {"state": "finished", "run_id": "own"})


def test_snapshot_records_selection_and_changes_with_reviewed_policy(reviewed_policy):
    from unified_agent import trust_presets

    reviewed_policy({"agents": {"Desktop_Agent2": "inspection"}})
    first = trust_presets.snapshot("Desktop_Agent2")
    assert first["preset"] == "inspection"
    assert first["agent"] == "Desktop_Agent2"
    assert first["enforcement"] == {"mediated_tools": True, "native_runtime": False}
    assert len(first["policy_fingerprint"]) == 64
    json.dumps(first)
    reviewed_policy({"agents": {"Desktop_Agent2": "configured-workspace"}})
    second = trust_presets.snapshot("Desktop_Agent2")
    assert first["policy_fingerprint"] != second["policy_fingerprint"]
    assert trust_presets.snapshot("Desktop_Agent1") is None


def test_native_requires_both_target_and_originator_unbound(reviewed_policy):
    from unified_agent import trust_presets

    reviewed_policy({"agents": {"Desktop_Agent2": "inspection"}})
    with pytest.raises(ToolError):
        trust_presets.require_native_allowed("Desktop_Agent1", originator="Desktop_Agent1")
    token = config.agent_identity_var.set(None)
    try:
        with pytest.raises(ToolError):
            trust_presets.require_native_allowed("Desktop_Agent1", originator="Desktop_Agent2")
        trust_presets.require_native_allowed("Desktop_Agent1", originator="Desktop_Agent1")
    finally:
        config.agent_identity_var.reset(token)


def test_missing_policy_preserves_legacy_noop(tmp_path, monkeypatch):
    from unified_agent import trust_presets

    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "missing.yaml"))
    assert trust_presets.selection() is None
    assert trust_presets.snapshot() is None
    trust_presets.check_call("run_cmd", read_only=False)
    trust_presets.require_native_allowed()


@pytest.mark.parametrize("tool", ["fs_read", "dispatch_report_self"])
def test_mcp_preset_does_not_widen_profile_visibility(reviewed_policy, monkeypatch, tool):
    from unified_agent import server

    reviewed_policy({"default": "inspection"})
    monkeypatch.setattr(server.operational_profiles, "tool_is_allowed", lambda _name: False)
    with pytest.raises(ToolError, match="not exposed"):
        server.REGISTERED_TOOLS[tool](
            **({"path": "."} if tool == "fs_read" else {"state": "finished", "run_id": "own"})
        )


def test_mcp_positional_read_action_is_classified_before_execution(reviewed_policy, monkeypatch):
    from unified_agent import server

    reviewed_policy({"default": "inspection"})
    monkeypatch.setattr(server.operational_profiles, "tool_is_allowed", lambda _name: True)
    calls = []

    def task_comment(action, params=None):
        calls.append(action)
        return {"action": action}

    wrapped = server._guarded_and_instrumented(task_comment)
    assert wrapped("list") == {"action": "list"}
    with pytest.raises(ToolError):
        wrapped("append", {"body": "unauthorized"})
    assert calls == ["list"]


def test_http_object_writer_cannot_bypass_preset(reviewed_policy, monkeypatch):
    from unified_agent import profiles
    from unified_agent.webui import api

    reviewed_policy({"default": "inspection"}, enabled=False)
    monkeypatch.setattr(profiles, "tool_is_allowed_for", lambda *_args: True)
    with pytest.raises(ToolError, match="trust preset"):
        api._registered_platform_write(
            "task",
            "update",
            actor="Desktop_Person",
            operation="task_update",
            production=False,
            id=1,
            status="done",
        )


@pytest.mark.parametrize("restricted", ["Desktop_Agent2", "Desktop_Agent5", "Desktop_Agent1"])
def test_native_caller_originator_and_target_each_constrain_launch(reviewed_policy, restricted):
    from unified_agent import trust_presets

    reviewed_policy({"agents": {restricted: "inspection"}})
    with pytest.raises(ToolError):
        trust_presets.require_native_allowed("Desktop_Agent1", originator="Desktop_Agent5")


@pytest.mark.parametrize("read_only", [False, True])
def test_audit_read_hint_cannot_admit_native_git_callbacks(reviewed_policy, read_only):
    from unified_agent import trust_presets

    reviewed_policy({"default": "inspection"})
    with pytest.raises(ToolError):
        trust_presets.check_call(
            "security_audit", read_only=read_only, payload={"remediate": False}
        )
    with pytest.raises(ToolError):
        policy.evaluate_policy("security_audit", {"remediate": False, "read_only": True})


def test_mcp_audit_is_denied_before_native_engine(reviewed_policy, monkeypatch):
    from unified_agent import server

    reviewed_policy({"default": "inspection"})
    monkeypatch.setattr(server.operational_profiles, "tool_is_allowed", lambda _name: True)
    with pytest.raises(ToolError, match="trust preset"):
        server.REGISTERED_TOOLS["security_audit"](path=".", remediate=False)


def test_unknown_verified_identity_cannot_bypass_agent_only_binding(reviewed_policy, monkeypatch):
    reviewed_policy({"agents": {"Desktop_Agent2": "inspection"}})
    monkeypatch.setenv("UA_AGENT_LABELS", "")
    token = config.agent_identity_var.set("Unknown_Caller")
    try:
        with pytest.raises(ToolError):
            policy.evaluate_policy("fs_write", {})
    finally:
        config.agent_identity_var.reset(token)


def test_explicit_reviewed_custom_identity_binding_remains_enforceable(
    reviewed_policy, monkeypatch
):
    reviewed_policy({"agents": {"Custom_Agent": "inspection"}})
    monkeypatch.setenv("UA_AGENT_LABELS", "")
    token = config.agent_identity_var.set("Custom_Agent")
    try:
        policy.evaluate_policy("fs_read", {})
        with pytest.raises(ToolError):
            policy.evaluate_policy("fs_write", {})
    finally:
        config.agent_identity_var.reset(token)


def test_run_history_observability_action_is_bounded_read_admission(reviewed_policy, monkeypatch):
    from unified_agent import profiles, server

    reviewed_policy({"default": "inspection"})
    monkeypatch.setattr(server.operational_profiles, "tool_is_allowed", lambda _name: True)
    monkeypatch.setattr(profiles, "consolidated_action_is_allowed", lambda *_args: True)
    result = server.REGISTERED_TOOLS["observability_manage"]("run_history", {"task_id": 1})
    assert result["runs"] == []
    with pytest.raises(ToolError):
        server.REGISTERED_TOOLS["observability_manage"]("anomalies_notify")
    with pytest.raises(ToolError):
        policy.evaluate_policy("observability_manage", {"action": "heartbeat", "read_only": True})


@pytest.mark.parametrize(
    "second", ["live", "missing-root", "missing-token", "missing-expiry", "expired"]
)
def test_active_worktree_never_chooses_between_unverifiable_claims(
    reviewed_policy, tmp_path, second
):
    from datetime import UTC, datetime, timedelta

    from unified_agent import storage, trust_presets

    reviewed_policy({"default": "active-worktree"})
    storage.init_db()
    first_root = tmp_path / "first-worktree"
    first_root.mkdir()
    second_root = tmp_path / "second-worktree"
    second_root.mkdir()
    now = datetime.now(UTC)
    future = (now + timedelta(hours=1)).isoformat()
    conn = storage.connect()
    try:
        for index, root, token, expiry in (
            (1, str(first_root), "sp-first", future),
            (
                2,
                None if second == "missing-root" else str(second_root),
                None if second == "missing-token" else "sp-second",
                None
                if second == "missing-expiry"
                else ((now - timedelta(hours=1)).isoformat() if second == "expired" else future),
            ),
        ):
            conn.execute(
                "INSERT INTO tasks(title,status,claimed_by,worktree_path,current_run_id,lock_expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    f"claim-{index}",
                    "doing",
                    "Desktop_Agent2",
                    root,
                    token,
                    expiry,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    if second == "expired":
        trust_presets.check_path(first_root / "allowed.txt")
    else:
        with pytest.raises(ToolError):
            trust_presets.check_path(first_root / "must-not-choose.txt")


def test_preset_binding_outside_closed_roster_is_rejected(reviewed_policy, monkeypatch):
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent2")
    reviewed_policy({"agents": {"Desktop_Agent1": "inspection"}})
    with pytest.raises(ToolError):
        policy.load_policy()


@pytest.mark.parametrize("shape", ["relative", "absolute-project"])
def test_active_worktree_root_uses_verified_workspace_not_process_cwd(
    reviewed_policy, tmp_path, monkeypatch, shape
):
    from datetime import UTC, datetime, timedelta

    from unified_agent import storage, trust_presets

    reviewed_policy({"agents": {"Desktop_Agent2": "active-worktree"}})
    workspace = tmp_path / "verified-workspace"
    workspace.mkdir()
    process_cwd = tmp_path / "process-cwd"
    process_cwd.mkdir()
    relative = Path(".worktrees/task-123")
    expected = workspace / relative if shape == "relative" else tmp_path / "project" / relative
    expected.mkdir(parents=True)
    decoy = process_cwd / relative
    decoy.mkdir(parents=True)
    monkeypatch.setenv("UA_WORKSPACE", str(workspace))
    monkeypatch.chdir(process_cwd)
    storage.init_db()
    now = datetime.now(UTC)
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks(title,status,claimed_by,worktree_path,current_run_id,lock_expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                "owned",
                "doing",
                "Desktop_Agent2",
                str(relative if shape == "relative" else expected),
                "sp-owned",
                (now + timedelta(hours=1)).isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    trust_presets.check_path(expected / "allowed.txt")
    with pytest.raises(ToolError, match="escapes"):
        trust_presets.check_path(decoy / "must-not-read.txt")
