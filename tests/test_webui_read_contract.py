"""Reviewed authenticated read-pane contracts, including pruned MCP helpers.

Real HTTP handlers and guarded callables run in private fixtures. Tool bodies are
controlled read projections: no provider, host census or credential read occurs.
The existing quota/pool/delivery/vault/plan suites cover their real stores too.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import JSONResponse
from starlette.routing import Route
from test_route_scopes import OPERATOR, _bearer, _client
from test_route_scopes import _isolated as _isolated

from unified_agent import config, profiles, registry, server, tailnet
from unified_agent.tools import tasks
from unified_agent.webui import api, route_scopes, routes

# Source-reviewed finite call graph, not derived from the permission table.
READ_CASES = {
    "/api/overview": (
        "approval_pending",
        "check_anomalies",
        "delegate_list",
        "dependency_list",
        "event_log_query",
        "handoff_list",
        "message_list",
        "metrics_snapshot",
        "plan_list",
        "system_doctor",
        "task_list",
        "workflow_list",
    ),
    "/api/attention": (
        "approval_pending",
        "autonomous_cycle_list",
        "check_anomalies",
        "system_doctor",
        "task_list",
    ),
    "/api/digest": (
        "approval_pending",
        "autonomous_cycle_list",
        "check_anomalies",
        "system_doctor",
        "task_list",
    ),
    "/api/agents": ("task_list",),
    "/api/org": ("task_list",),
    "/api/agents/{label}": ("agent_activity",),
    "/api/tasks": ("task_list",),
    "/api/board": ("delegate_list", "dependency_list", "task_list"),
    "/api/flow-metrics": ("task_list",),
    "/api/queue-health": ("task_list",),
    "/api/review-approval": ("approval_pending", "task_list"),
    "/api/executor-delegation": ("delegate_list", "system_doctor"),
    "/api/approvals": ("approval_pending",),
    "/api/memory": ("memory_list", "memory_search"),
    "/api/vault/search": ("vault_search",),
    "/api/vault/page": ("vault_links", "vault_read"),
    "/api/vault/ingest-status": ("vault_ingest_status",),
    "/api/vault/briefing": ("vault_briefing_digest",),
    "/api/connectors": ("gcloud_inventory", "horizon2_package_list", "horizon2_server_list"),
    "/api/quota": ("model_quota_status",),
    "/api/credential_pool": ("credential_pool_list",),
    "/api/slack_gateway": ("event_log_query",),
    "/api/notify_delivery": ("notify_delivery_status",),
    "/api/dispatch_receipts": ("dispatch_delivery_receipts_list",),
    "/api/agent-graph": ("delegate_list", "message_list", "system_doctor", "task_list"),
    "/api/execution-graph": (
        "delegate_list",
        "dependency_list",
        "handoff_list",
        "system_doctor",
        "task_list",
    ),
    "/api/dispatch-stack": ("dependency_list", "task_list"),
    "/api/cycles": ("autonomous_cycle_list",),
    "/api/cycles/{cycle_id}": ("autonomous_cycle_get",),
    "/api/plans": ("plan_manage",),
    "/api/plans/{plan_id}": ("plan_manage",),
    "/api/health": ("system_doctor",),
    "/api/cloud": ("gcloud_inventory",),
    "/api/devices": ("system_doctor",),
    "/api/mobile/v1/projection": (
        "approval_pending",
        "autonomous_cycle_list",
        "check_anomalies",
        "system_doctor",
        "task_list",
    ),
    "/api/langgraph/checkpoints": ("langgraph_checkpoints",),
    "/api/perf": ("perf_summary",),
    "/api/events": ("event_log_query",),
}


def _projection(tool):
    if tool == "dependency_list":
        return {"dependencies": [], "truncated": False}
    if tool == "plan_manage":
        return {"plans": [], "plan": {"id": "fixture", "goal": "visible read"}}
    if tool == "vault_read":
        return {
            "path": "Wiki/fixture.md",
            "content": "visible read",
            "total_lines": 1,
            "truncated": False,
        }
    if tool == "vault_search":
        return {"matches": [{"path": "Wiki/fixture.md", "line": 1, "text": "visible read"}]}
    if tool == "credential_pool_list":
        return {
            "credentials": [
                {
                    "id": "fixture",
                    "provider": "fixture",
                    "label": "visible read",
                    "unexpected_secret": "must-be-dropped",
                }
            ]
        }
    if tool == "model_quota_status":
        return {"quotas": [{"lane": "fixture", "availability": "available"}]}
    if tool == "horizon2_package_list":
        return {"packages": [{"package": "int-gcp-agent20"}]}
    if tool == "autonomous_cycle_get":
        return {"cycle": {"id": "fixture", "state": "plan"}, "transitions": []}
    return {
        "tasks": [],
        "plans": [],
        "delegations": [],
        "dependencies": [],
        "handoffs": [],
        "messages": [],
        "results": [],
        "items": [],
        "cycles": [],
        "pending": [],
        "approved_unconsumed": [],
        "expired_passes": [],
        "gated_tools": [],
        "checks": [],
        "summary": {},
        "workflows": [],
        "servers": [],
        "packages": [],
        "instances": [],
        "buckets": [],
        "budgets": [],
        "sections": {},
        "errors": [],
        "deliveries": [],
        "receipts": [],
        "checkpoints": [],
        "entries": [],
        "events": [],
        "count": 0,
        "exists": True,
        "truncated": False,
        "status": "fixture-read",
    }


@pytest.fixture
def read_projections(tmp_path, monkeypatch):
    effects = []
    tasks.task_add("read fixture task")
    registry.touch_agent("Desktop_Agent2", via="test_fixture", force=True)
    for tool in {name for names in READ_CASES.values() for name in names}:

        def make_reader(name):
            def reader(**kwargs):
                if name == "plan_manage":
                    action = "plan_" + kwargs["action"]
                    assert profiles.consolidated_action_is_allowed(name, action)
                    assert profiles.consolidated_action_is_allowed(
                        name, "plan_create"
                    ) == profiles.consolidated_action_is_allowed_for(
                        profiles.current(), name, "plan_create"
                    )
                if name == "vault_ingest_status":
                    assert kwargs.get("mark", False) is False
                assert not profiles.http_read_tool_is_allowed("task_update", {})
                effects.append(name)
                if name == "task_list":
                    return tasks.task_list(**kwargs)
                return _projection(name)

            reader.__name__ = name
            return reader

        monkeypatch.setitem(
            server.REGISTERED_TOOLS, tool, server._guarded_and_instrumented(make_reader(tool))
        )
    monkeypatch.setattr(api, "_doctor_cache", None)
    monkeypatch.setattr(api, "_doctor_inflight", None)
    monkeypatch.setattr(api, "_cloud_cache", None)
    monkeypatch.setattr(tailnet, "peers", lambda: None)
    monkeypatch.setenv("UA_GCP_PROJECT", "fixture-project")
    monkeypatch.setenv("UA_GCP_WORKER", "fixture-worker")
    monkeypatch.setenv("UA_MOBILE_WATCH_ENABLED", "1")
    vault = config.vault_root()
    (vault / "Wiki").mkdir(parents=True, exist_ok=True)
    (vault / "Wiki/fixture.md").write_text("visible read")
    return effects


@pytest.mark.parametrize("path,names", list(READ_CASES.items()))
@pytest.mark.parametrize("identity", ["Desktop_Agent2", OPERATOR])
def test_authenticated_read_handlers_reach_their_guarded_projections(
    path, names, identity, read_projections
):
    url = (
        path.replace("{label}", "Desktop_Agent2")
        .replace("{cycle_id}", "fixture")
        .replace("{plan_id}", "fixture")
    )
    params = {"path": "Wiki/fixture.md", "pattern": "visible"} if "/vault/" in path else {}
    if path == "/api/dispatch-stack":
        params["task_ids"] = "1"
    response = _client(routes.web_routes()).get(url, headers=_bearer(identity), params=params)
    assert response.status_code == 200, response.text
    # Every declared direct read executes through the guarded wrapper, rather
    # than being replaced by a profile-denied unavailable/empty fallback.
    selected = set(names) - ({"memory_search"} if path == "/api/memory" else set())
    assert selected <= set(read_projections), (path, response.text, read_projections)
    assert "not exposed" not in response.text
    assert "must-be-dropped" not in response.text
    scope = route_scopes.scope_for("GET", path)
    assert set(scope.read_tools) == set(names) - {"plan_manage"}
    if "plan_manage" in names:
        assert scope.read_actions == (
            ("plan_manage", "plan_get" if "{plan_id}" in path else "plan_list"),
        )
    assert _client(routes.web_routes()).get(url, params=params).status_code == 401


def test_memory_search_branch_keeps_guarded_read_authority(read_projections):
    response = _client(routes.web_routes()).get(
        "/api/memory", headers=_bearer(OPERATOR), params={"query": "fixture"}
    )
    assert response.status_code == 200, response.text
    assert "memory_search" in read_projections
    assert response.json()["mode"] == "search"


def test_mixed_ingest_read_authority_cannot_mark_ingested():
    async def handler(request):
        with pytest.raises(ToolError, match="not exposed"):
            await api.call_tool(
                "vault_ingest_status", path="Wiki/fixture.md", mark=True, agent=OPERATOR
            )
        return JSONResponse({"denied_write": True})

    response = _client(
        [routes._scoped(Route("/api/vault/ingest-status", handler, methods=["GET"]))]
    ).get("/api/vault/ingest-status", headers=_bearer(OPERATOR))
    assert response.status_code == 200, response.text
    assert response.json()["denied_write"]


def test_read_authority_keeps_mcp_ceiling_identity_and_lifetime(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    token = config.agent_identity_var.set(OPERATOR)
    try:
        with profiles.http_route_authority(OPERATOR, (), (), read_tools=("model_quota_status",)):
            assert profiles.http_read_tool_is_allowed("model_quota_status", {})
            assert not profiles.tool_is_allowed("model_quota_status")
            assert "model_quota_status" not in profiles.allowed_tools(server.REGISTERED_TOOLS)
            changed = config.agent_identity_var.set("server_agent8")
            try:
                assert not profiles.http_read_tool_is_allowed("model_quota_status", {})
            finally:
                config.agent_identity_var.reset(changed)
        assert not profiles.http_read_tool_is_allowed("model_quota_status", {})
        assert not profiles.tool_is_allowed("model_quota_status")
    finally:
        config.agent_identity_var.reset(token)


@pytest.mark.asyncio
async def test_read_authority_expires_in_copied_background_context():
    import asyncio

    ready = asyncio.Event()
    release = asyncio.Event()
    token = config.agent_identity_var.set(OPERATOR)
    try:

        async def inherited():
            assert profiles.http_read_tool_is_allowed("model_quota_status", {})
            ready.set()
            await release.wait()
            return profiles.http_read_tool_is_allowed("model_quota_status", {})

        with profiles.http_route_authority(OPERATOR, (), (), read_tools=("model_quota_status",)):
            task = asyncio.create_task(inherited())
            await asyncio.wait_for(ready.wait(), timeout=2)
        release.set()
        assert not await task
    finally:
        config.agent_identity_var.reset(token)
