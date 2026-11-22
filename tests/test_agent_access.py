from __future__ import annotations

import pytest

from unified_agent import agent_access


def clear_access_env(monkeypatch):
    for name in (
        "UA_AGENT_ACCESS_ENABLED",
        "UA_AGENT_ACCESS_READ_ONLY",
        "UA_AGENT_ACCESS_PRODUCTION_BLOCKED",
        "UA_RUNTIME_ENV",
        "UA_MANAGED_SECRETS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_legacy_defaults_are_explicit_and_enabled(monkeypatch):
    clear_access_env(monkeypatch)
    result = agent_access.status()
    assert result == {
        "enabled": True,
        "read_only": False,
        "production_blocked": False,
        "known": True,
        "source": "legacy-default",
    }


def test_invalid_config_fails_closed(monkeypatch):
    clear_access_env(monkeypatch)
    monkeypatch.setenv("UA_AGENT_ACCESS_READ_ONLY", "maybe")
    with pytest.raises(agent_access.AgentAccessDenied, match="unknown"):
        agent_access.enforce("task_update", read_only=False)


def test_independent_read_only_and_production_switches(monkeypatch):
    clear_access_env(monkeypatch)
    monkeypatch.setenv("UA_AGENT_ACCESS_READ_ONLY", "1")
    agent_access.enforce("task_list", read_only=True)
    with pytest.raises(agent_access.AgentAccessDenied, match="read-only"):
        agent_access.enforce("task_update", read_only=False)

    monkeypatch.setenv("UA_AGENT_ACCESS_READ_ONLY", "0")
    monkeypatch.setenv("UA_AGENT_ACCESS_PRODUCTION_BLOCKED", "1")
    with pytest.raises(agent_access.AgentAccessDenied, match="classification"):
        agent_access.enforce("task_update", read_only=False)
    monkeypatch.setenv("UA_RUNTIME_ENV", "development")
    agent_access.enforce("task_update", read_only=False)
    with pytest.raises(agent_access.AgentAccessDenied, match="production"):
        agent_access.enforce("dispatch_resume", read_only=False, production=True)


def test_managed_secret_mode_blocks_arbitrary_execution(monkeypatch):
    clear_access_env(monkeypatch)
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    with pytest.raises(agent_access.AgentAccessDenied, match="arbitrary"):
        agent_access.enforce("run_cmd", read_only=False)
    agent_access.enforce("llm_broker_completion", read_only=False)
    monkeypatch.setenv("UA_AGENT_ACCESS_READ_ONLY", "1")
    with pytest.raises(agent_access.AgentAccessDenied, match="read-only"):
        agent_access.enforce("llm_broker_completion", read_only=False)


def test_profile_status_is_a_diagnostic_even_when_disabled(monkeypatch):
    clear_access_env(monkeypatch)
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")
    assert agent_access.status()["enabled"] is False


def test_actual_registered_tools_deny_before_execution(monkeypatch):
    from unified_agent import server

    clear_access_env(monkeypatch)
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")
    with pytest.raises(agent_access.AgentAccessDenied, match="disabled"):
        server.REGISTERED_TOOLS["task_list"]()
    assert server.REGISTERED_TOOLS["profile_status"]()["agent_access"]["enabled"] is False
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "1")
    monkeypatch.setenv("UA_AGENT_ACCESS_READ_ONLY", "1")
    with pytest.raises(agent_access.AgentAccessDenied, match="read-only"):
        server.REGISTERED_TOOLS["export_migration_manifest"](True)
    monkeypatch.setenv("UA_AGENT_ACCESS_READ_ONLY", "0")
    monkeypatch.setenv("UA_AGENT_ACCESS_PRODUCTION_BLOCKED", "1")
    monkeypatch.setenv("UA_RUNTIME_ENV", "production")
    with pytest.raises(agent_access.AgentAccessDenied, match="production"):
        server.REGISTERED_TOOLS["task_list"]()
    monkeypatch.setenv("UA_RUNTIME_ENV", "development")
    with pytest.raises(agent_access.AgentAccessDenied, match="production"):
        server.REGISTERED_TOOLS["gcloud_inventory"]()


def test_managed_mode_blocks_indirect_and_unknown_execution(monkeypatch):
    from unified_agent import server

    clear_access_env(monkeypatch)
    monkeypatch.setenv("UA_MANAGED_SECRETS", "typo")
    import inspect

    calls = {
        "run_cmd": {"command": "pwd"},
        "workflow_run": {"name": "unknown-test-workflow"},
        "delegation_manage": {"action": "list"},
        "autonomous_cycle_manage": {"action": "get"},
    }
    executed = []
    for name, arguments in calls.items():
        registered = server.REGISTERED_TOOLS[name]
        with pytest.raises(agent_access.AgentAccessDenied, match="unclassified"):
            registered(**arguments)

        def bare(*args, _tool_name=name, **kwargs):
            executed.append(_tool_name)

        bare.__name__ = name
        bare.__signature__ = inspect.signature(registered)
        with pytest.raises(agent_access.AgentAccessDenied, match="unclassified"):
            server._guarded_and_instrumented(bare)(**arguments)
    assert executed == []


@pytest.mark.asyncio
async def test_actual_http_boundary_and_resources_deny(monkeypatch):
    import httpx
    from starlette.responses import JSONResponse

    from unified_agent import mcp_live_state, object_model, platform_http, server

    clear_access_env(monkeypatch)
    monkeypatch.setenv("UA_AGENT_ACCESS_ENABLED", "0")
    monkeypatch.setattr(server.config, "agent_tokens", lambda: {})
    monkeypatch.setattr(server.oauth, "get_provider", lambda: None)

    async def app(scope, receive, send):
        await JSONResponse({"called": True})(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(server.HubAuthASGIMiddleware(app)), base_url="http://test"
    ) as client:
        result = await client.get("/api/tasks")
        assert result.status_code == 403 and "disabled" in result.json()["error"]
        assert (await client.get("/healthz")).status_code == 200
    with pytest.raises(agent_access.AgentAccessDenied):
        platform_http.manifest_payload()
    with pytest.raises(agent_access.AgentAccessDenied):
        mcp_live_state._handler_for(object_model.by_kind("board"))()
