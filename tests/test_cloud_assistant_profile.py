"""Security boundary for the cloud profiles and the cloud-hosted identities.

cloud_agent17 defaults to developer (2026-11-11). The two cloud profiles remain
policy-overlay downgrades and keep the allow-list checks below. The HTTP tests
use each cloud identity at its real default (developer for cloud_agent17,
chief-of-staff for cloud_agent13, research for cloud_agent8), so they prove the
MCP-only boundary is keyed on identity, not on the profile.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, profiles
from unified_agent.config_revisions import _overlay_widens_tools


def test_cloud_Agent17_defaults_to_developer_and_cloud_Agent13_to_chief_of_staff():
    assert profiles.default_for_identity("cloud_agent17") == profiles.DEVELOPER
    assert profiles.default_for_identity("cloud_agent13") == profiles.CHIEF_OF_STAFF
    assert profiles.default_for_identity("cloud_agent8") == profiles.RESEARCH
    assert profiles.default_for_identity("server_agent2") == profiles.DEVELOPER
    # server_agent5 is the hub-hosted CTO surface since #1884.
    assert profiles.default_for_identity("server_agent5") == profiles.CTO


@pytest.mark.parametrize(
    "tool",
    [
        "run_cmd",
        "approval_resolve",
        "delegate",
        "workflow_run",
        "fs_read",
        "fs_write",
        "task_add",
        "task_update",
        "memory_save",
        "profile_activate",
        "future_unknown_tool",
    ],
)
@pytest.mark.parametrize("profile", sorted(profiles.CLOUD_PROFILES))
def test_cloud_Agent17_cannot_reach_unapproved_tools(tool, profile):
    assert not profiles.tool_is_allowed_for(profile, tool)


@pytest.mark.parametrize(
    "tool",
    [
        "profile_status",
        "observability_manage",
        "task_list",
        "memory_search",
        "memory_get",
        "memory_list",
        "message_list",
        "message_send",
    ],
)
@pytest.mark.parametrize("profile", sorted(profiles.CLOUD_PROFILES))
def test_approved_tools_are_reachable(tool, profile):
    assert profiles.tool_is_allowed_for(profile, tool)


@pytest.mark.parametrize(
    "action",
    ["agent_heartbeat", "check_anomalies_and_notify", "agent_activity", "unknown_future_action"],
)
@pytest.mark.parametrize("profile", sorted(profiles.CLOUD_PROFILES))
def test_observability_cannot_expand_to_other_actions(action, profile):
    assert not profiles.consolidated_action_is_allowed_for(profile, "observability_manage", action)


@pytest.mark.parametrize("action", ["agent_context", "agent_list"])
@pytest.mark.parametrize("profile", sorted(profiles.CLOUD_PROFILES))
def test_context_and_roster_actions_are_reachable(action, profile):
    assert profiles.consolidated_action_is_allowed_for(profile, "observability_manage", action)
    assert not profiles.consolidated_action_is_allowed_for(profile, "workflow_manage", action)


@pytest.mark.parametrize(
    "chosen", [profiles.DEVELOPER, profiles.CHIEF_OF_STAFF, profiles.RESEARCH, profiles.OPERATOR]
)
@pytest.mark.parametrize("profile", sorted(profiles.CLOUD_PROFILES))
def test_policy_overlay_cannot_widen_cloud_profile(chosen, profile):
    assert _overlay_widens_tools(chosen, profile)


@pytest.mark.parametrize("profile", sorted(profiles.CLOUD_PROFILES))
def test_authenticated_session_cannot_activate_broader_profile(monkeypatch, profile):
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "cloud_agent17")
    monkeypatch.setattr(profiles, "_current_session_id", lambda: "cloud-agent17-test")
    monkeypatch.setattr(config, "enforce_auth", lambda: True)
    monkeypatch.setitem(profiles.DEFAULT_PROFILES_BY_IDENTITY, "cloud_agent17", profile)
    profiles.activate(profile)
    try:
        assert profiles.vault_write_dirs() == (
            ("Wiki",) if profile == profiles.CLOUD_VAULT_STEWARD else ()
        )
        assert profiles.consolidated_action_is_allowed("observability_manage", "agent_list")
        assert not profiles.consolidated_action_is_allowed(
            "observability_manage", "agent_heartbeat"
        )
        with pytest.raises(ToolError):
            profiles.activate(profiles.DEVELOPER)
    finally:
        profiles._active.pop("cloud-agent17-test", None)


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/action/handoff", "POST"),
        ("/api/action/task", "POST"),
        ("/api/action/approval", "POST"),
        ("/api/action/workflow", "POST"),
        ("/api/overview", "GET"),
        ("/api/future-route", "GET"),
        ("/api/whoami", "POST"),
    ],
)
@pytest.mark.parametrize("identity", ["cloud_agent17", "cloud_agent8", "cloud_agent13"])
def test_cloud_identity_cannot_bypass_mcp_via_http(monkeypatch, path, method, identity):
    expected = {
        "cloud_agent17": profiles.DEVELOPER,
        "cloud_agent8": profiles.RESEARCH,
        "cloud_agent13": profiles.CHIEF_OF_STAFF,
    }[identity]
    assert profiles.default_for_identity(identity) == expected
    import secrets

    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from unified_agent.server import HubAuthASGIMiddleware

    token = secrets.token_urlsafe(24)
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{identity}:" + token)
    monkeypatch.setenv("UA_AGENT_LABELS", identity)
    monkeypatch.setenv("UA_AGENT_WORKSPACES", f"{identity}:DENY")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    reached = []

    async def handler(request):
        reached.append(True)
        return PlainTextResponse("unexpected handler execution")

    app = HubAuthASGIMiddleware(Starlette(routes=[Route(path, handler, methods=[method])]))
    with TestClient(app) as client:
        response = client.request(method, path, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 403
    assert not reached


@pytest.mark.parametrize("path", ["/mcp", "/healthz", "/api/whoami", "/oauth/whoami"])
def test_cloud_identity_reaches_only_explicit_http_paths(monkeypatch, path):
    import secrets

    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from unified_agent.server import HubAuthASGIMiddleware

    token = secrets.token_urlsafe(24)
    monkeypatch.setenv("UA_AGENT_TOKENS", "cloud_agent17:" + token)
    monkeypatch.setenv("UA_AGENT_LABELS", "cloud_agent17")
    monkeypatch.setenv("UA_AGENT_WORKSPACES", "cloud_agent17:DENY")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")

    async def handler(request):
        return PlainTextResponse("allowed")

    app = HubAuthASGIMiddleware(Starlette(routes=[Route(path, handler)]))
    with TestClient(app) as client:
        response = client.get(path, headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200


@pytest.mark.parametrize("identity", ["cloud_agent17", "cloud_agent8", "cloud_agent13"])
def test_cloud_token_cannot_create_browser_cookie_without_auth_header(monkeypatch, identity):
    import secrets

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from unified_agent import webui
    from unified_agent.server import HubAuthASGIMiddleware
    from unified_agent.webui import auth

    token = secrets.token_urlsafe(24)
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{identity}:" + token)
    monkeypatch.setenv("UA_AGENT_LABELS", identity)
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "testserver:*")
    minted = []
    monkeypatch.setattr(auth, "create_session", lambda *a: minted.append(True))
    app = HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        response = client.post("/api/session", json={"token": token})
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert not minted
