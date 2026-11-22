"""Toolset manifest filtering. Authorization stays on the profile gate."""

from __future__ import annotations

import pytest
from mcp.client import Client

from unified_agent import config, profiles, server, toolsets
from unified_agent.tool_schema_fingerprint import check_schema_drift, fingerprint_from_tool_list
from unified_agent.tools.mcp_health import mcp_health_telemetry
from unified_agent.toolsets import (
    ToolsetAssignmentError,
    ToolsetConfigError,
    assignment_problems,
    build_assignments,
    require_complete_assignments,
    visible_in_manifest,
)


def test_every_registered_tool_is_assigned_exactly_once():
    assert set(toolsets.TOOLSET_MEMBERS) == set(toolsets.TOOLSET_NAMES)
    assert assignment_problems(server.REGISTERED_TOOLS) == []
    assert set(toolsets.ASSIGNMENTS) == set(server.REGISTERED_TOOLS)


def test_skill_registry_is_an_admin_tool():
    assert toolsets.ASSIGNMENTS["skill_registry"] == "admin"


def test_no_standalone_request_cancel_tool():
    # Cancellation ships as task_update(request_cancel=True, ...) (#880); no
    # standalone tool is registered, so none may be pre-assigned either.
    assert "request_cancel" not in server.REGISTERED_TOOLS
    assert "request_cancel" not in toolsets.ASSIGNMENTS
    assert "request_cancel" not in toolsets.tools_in("tasks")
    assert not hasattr(toolsets, "ASSIGNED_BEFORE_REGISTRATION")


def test_assigned_but_unregistered_tool_fails_the_assignment_check():
    problems = assignment_problems(set(server.REGISTERED_TOOLS) - {"task_add"})
    assert problems == ["not registered: task_add"]


def test_unassigned_tool_fails_the_assignment_check():
    problems = assignment_problems(set(server.REGISTERED_TOOLS) | {"brand_new_tool"})
    assert problems == ["unassigned: brand_new_tool"]


def test_duplicate_assignment_is_rejected():
    with pytest.raises(ToolsetAssignmentError, match="assigned twice"):
        build_assignments({"core": ("fs_read",), "ops": ("fs_read",)})


def test_default_manifest_matches_the_registry(monkeypatch):
    monkeypatch.delenv("UA_TOOLSETS", raising=False)
    visible = visible_in_manifest(server.REGISTERED_TOOLS)
    assert visible == frozenset(server.REGISTERED_TOOLS)
    assert len(visible) == len(server.REGISTERED_TOOLS)


def test_blank_toolsets_match_the_registry(monkeypatch):
    monkeypatch.setenv("UA_TOOLSETS", "  ")
    assert config.configured_toolsets() is None
    assert visible_in_manifest(server.REGISTERED_TOOLS) == frozenset(server.REGISTERED_TOOLS)


def test_core_manifest_is_under_40_and_includes_the_claim_cycle(monkeypatch):
    monkeypatch.setenv("UA_TOOLSETS", "core")
    visible = visible_in_manifest(server.REGISTERED_TOOLS)
    assert visible == toolsets.tools_in("core")
    assert len(visible) < 40
    assert {"task_claim", "task_update", "fs_read", "fs_write"} <= visible
    assert {"profile_status", "memory_search"} <= visible


def test_unknown_toolset_is_rejected(monkeypatch):
    monkeypatch.setenv("UA_TOOLSETS", "core:not_a_toolset")
    with pytest.raises(ToolsetConfigError, match="not_a_toolset"):
        visible_in_manifest(server.REGISTERED_TOOLS)


def test_unknown_toolset_fails_at_startup(monkeypatch):
    monkeypatch.setenv("UA_TOOLSETS", "core:not_a_toolset")
    with pytest.raises(ToolsetConfigError, match="not_a_toolset"):
        toolsets.require_known_toolsets()


def test_doctor_runs_with_an_unknown_toolset(monkeypatch):
    """Diagnostic commands dispatch before the hub-start toolset check."""
    from unified_agent import bootstrap

    called = {}

    def fake(argv):
        called["argv"] = argv
        return 0

    monkeypatch.setenv("UA_TOOLSETS", "not_a_toolset")
    monkeypatch.setattr(bootstrap, "doctor_main", fake)
    monkeypatch.setattr(server.sys, "argv", ["unified-agent", "doctor", "--json"])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code == 0
    assert called["argv"] == ["--json"]


def test_hub_start_rejects_an_unknown_toolset_before_either_transport(monkeypatch):
    monkeypatch.setenv("UA_TOOLSETS", "not_a_toolset")
    monkeypatch.setattr(server.sys, "argv", ["unified-agent"])
    monkeypatch.setattr(server.config, "require_configured_http_credentials", lambda: None)
    monkeypatch.setattr(server.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(server.storage, "init_db", lambda: None)
    monkeypatch.setattr(server.config, "transport", lambda: "stdio")

    def boom(*_args, **_kwargs):
        raise AssertionError("transport started")

    monkeypatch.setattr(server.mcp, "run", boom)
    with pytest.raises(ToolsetConfigError, match="not_a_toolset"):
        server.main()


@pytest.mark.anyio
async def test_hub_lifespan_rejects_an_unknown_toolset(monkeypatch):
    monkeypatch.setenv("UA_TOOLSETS", "not_a_toolset")
    with pytest.raises(ToolsetConfigError, match="not_a_toolset"):
        async with server._hub_lifespan(server.mcp):
            pass


@pytest.mark.anyio
async def test_healthz_fails_an_unknown_toolset_without_naming_it(monkeypatch):
    from unified_agent.webui.routes import healthz

    monkeypatch.setenv("UA_TOOLSETS", "not_a_toolset")
    response = await healthz(None)  # type: ignore[arg-type]
    assert response.status_code == 503
    assert response.body == b'{"ok":false}'


def test_startup_tools_stay_listed_outside_core(monkeypatch):
    for selection in ("memory", "admin", "ops", "vault"):
        monkeypatch.setenv("UA_TOOLSETS", selection)
        visible = visible_in_manifest(server.REGISTERED_TOOLS)
        assert {"memory_search", "profile_status"} <= visible
        assert len(visible) < len(server.REGISTERED_TOOLS)


def test_manifest_filter_never_adds_names(monkeypatch):
    monkeypatch.setenv("UA_TOOLSETS", "core")
    visible = visible_in_manifest(["fs_read", "web_search", "not_registered"])
    assert visible == frozenset({"fs_read"})


@pytest.mark.anyio
async def test_tools_list_default_length_equals_the_registry(monkeypatch):
    monkeypatch.delenv("UA_TOOLSETS", raising=False)
    async with Client(server.mcp) as client:
        listed = (await client.session.list_tools()).tools
    assert len(listed) == len(server.REGISTERED_TOOLS)
    assert {tool.name for tool in listed} == set(server.REGISTERED_TOOLS)


@pytest.mark.anyio
async def test_tools_list_core_hides_tools_that_stay_callable(monkeypatch):
    monkeypatch.setenv("UA_TOOLSETS", "core")
    async with Client(server.mcp) as client:
        listed = (await client.session.list_tools()).tools
        names = {tool.name for tool in listed}
        assert len(names) < 40
        assert "task_claim" in names
        assert "task_update" in names
        assert "profile_status" in names
        assert "memory_search" in names
        assert "agent_list" not in names
        called = await client.session.call_tool("agent_list", {})
    assert not called.is_error, called.content


@pytest.mark.anyio
async def test_toolset_selection_does_not_widen_the_profile(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_TOOLSETS", "core:tasks:git:admin")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    session = config.mcp_session_id_var.set("toolset-profile-session")
    try:
        async with Client(server.mcp) as client:
            names = {tool.name for tool in (await client.session.list_tools()).tools}
        assert "fs_read" in names
        assert "task_claim" in names
        assert "plan_create" not in names
        assert "git_merge_task" not in names
        assert "memory_search" in names
        assert "web_search" not in names
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)
        with profiles._lock:
            profiles._active.pop("toolset-profile-session", None)


def test_require_complete_assignments_fails_when_a_tool_is_missing():
    with pytest.raises(ToolsetAssignmentError, match="unassigned"):
        require_complete_assignments(set(server.REGISTERED_TOOLS) | {"brand_new_tool"})


def test_manifest_path_refuses_an_unassigned_registered_tool(monkeypatch):
    monkeypatch.setitem(server.REGISTERED_TOOLS, "brand_new_tool", lambda: None)
    with pytest.raises(ToolsetAssignmentError, match="unassigned"):
        server.manifest_tool_names()


def _listed_schema(tool) -> dict:
    if hasattr(tool, "model_dump"):
        data = tool.model_dump(by_alias=True)
        schema = data.get("inputSchema") or data.get("input_schema")
        if isinstance(schema, dict):
            return schema
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
    return schema if isinstance(schema, dict) else {}


@pytest.mark.anyio
async def test_manifest_fingerprint_matches_listed_tools_under_core(monkeypatch):
    monkeypatch.setenv("UA_TOOLSETS", "core")
    async with Client(server.mcp) as client:
        listed = (await client.session.list_tools()).tools
    payloads = [{"name": tool.name, "inputSchema": _listed_schema(tool)} for tool in listed]
    health = mcp_health_telemetry()
    assert health["manifest_schema_fingerprint"] == fingerprint_from_tool_list(payloads)
    assert {tool.name for tool in listed} == toolsets.tools_in("core")
    assert len(listed) == 9


def test_drift_guard_sees_a_hidden_callable_schema(monkeypatch):
    """UA_TOOLSETS=core:ops hides vault_write, but the call is still allowed."""
    monkeypatch.setenv("UA_TOOLSETS", "core:ops")
    listed = server.manifest_tool_names()
    callable_names = server.callable_tool_names()
    assert "vault_write" not in listed
    assert "vault_write" in callable_names
    health = mcp_health_telemetry()
    current = server._schema_payloads(callable_names)
    assert health["schema_fingerprint"] == fingerprint_from_tool_list(current)
    assert health["schema_fingerprint"] != health["manifest_schema_fingerprint"]
    mutated = []
    for payload in current:
        if payload["name"] != "vault_write":
            mutated.append(payload)
            continue
        schema = dict(payload["inputSchema"])
        properties = dict(schema.get("properties") or {})
        properties["drift_probe"] = {"type": "string"}
        schema["properties"] = properties
        mutated.append({"name": payload["name"], "inputSchema": schema})
    drifted = check_schema_drift(health["schema_fingerprint"], fingerprint_from_tool_list(mutated))
    assert drifted["drifted"] is True
    assert drifted["action"] == "relist_tools"
