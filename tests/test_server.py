"""In-process MCP protocol smoke test: list tools and exercise a save→search round-trip."""

import json

import pytest
from mcp.client import Client
from pydantic import ValidationError

from unified_agent import config, profiles
from unified_agent.server import REGISTERED_TOOLS, mcp
from unified_agent.tools import approvals

EXPECTED_TOOLS = {
    "synthetic_workflow",
    "web_search",
    "web_fetch",
    "dash_list",
    "dash_search",
    "dash_view",
    "fs_read",
    "fs_write",
    "fs_list",
    "fs_search",
    "gemini_ask",
    "run_cmd",
    "run_cmd_check",
    "security_audit",
    "injection_triage",
    "audit_triage_tasks",
    "skill_review",
    "detect_dev_servers",
    "git_diff_staged",
    "git_commit_staged",
    "git_worktree_list",
    "git_worktree_add",
    "git_worktree_remove",
    "git_merge_task",
    "python_lint_and_format",
    "fs_index_symbols",
    "fs_list_symbols",
    "fs_search_symbols",
    "fs_repo_map",
    "memory_save",
    "memory_search",
    "memory_list",
    "memory_get",
    "memory_restore",
    "memory_feedback",
    "memory_delete",
    "memory_prune_candidates",
    "memory_promote",
    "recall_maintenance",
    "task_add",
    "task_update",
    "task_list",
    "task_delete",
    "task_claim",
    "goal_manage",
    "task_comment",
    "org_proposal",
    "skill_registry",
    "routine_add",
    "routine_list",
    "routine_pause",
    "routine_fire",
    "project_build",
    "dependency_list",
    "coordination_manage",
    "dependency_add",
    "dependency_remove",
    "handoff",
    "handoff_list",
    "graph_canvas_read",
    "graph_canvas_write",
    "evidence_report",
    "dispatch_delivery_receipt",
    "autonomous_cycle_start",
    "autonomous_cycle_manage",
    "autonomous_cycle_get",
    "autonomous_cycle_transition",
    "autonomous_cycle_record_usage",
    "autonomous_cycle_list",
    "autonomous_cycle_kill",
    "bug_worker_manage",
    "plan_create",
    "plan_manage",
    "plan_update",
    "plan_get",
    "plan_list",
    "plan_checkpoint",
    "plan_resume",
    "plan_step_retry",
    "plan_step_claim",
    "plan_handoff",
    "plan_delete",
    "delegate",
    "delegation_manage",
    "delegate_list",
    "delegate_claim",
    "delegate_release",
    "delegation_ledger",
    "dispatch_report_self",
    "dispatch_cancel",
    "dispatch_resume",
    "dispatch_retry_cancellation",
    "dispatch_confirm_cancellation",
    "delegation_spec_check",
    "delegation_spec_template",
    "delegation_spec_build",
    "profile_activate",
    "profile_reset",
    "profile_status",
    "config_revision_manage",
    "managed_secret_set",
    "agent_token_issue",
    "agent_token_rotate",
    "agent_token_list",
    "deployment_claim",
    "oauth_device_manage",
    "work_product_submit",
    "check_agent_capability",
    "capability_catalog",
    "workflow_run",
    "workflow_list",
    "approval_pending",
    "approval_resolve",
    "policy_authorization_record",
    "policy_authorization_list",
    "horizon2_server_list",
    "horizon2_server_register",
    "horizon2_server_revoke",
    "horizon2_connector_authorize",
    "horizon2_package_list",
    "horizon2_package_register",
    "credential_pool_register",
    "credential_pool_list",
    "credential_pool_revoke",
    "model_quota_status",
    "notify",
    "notify_delivery_status",
    "dispatch_delivery_receipts_list",
    "zulip_send_message",
    "zulip_fetch_messages",
    "relay_manage",
    "batch_notify",
    "flush_digest",
    "scan_for_reviewable_tasks",
    "review_and_notify",
    "autonomous_cycle_green_path_eligibility",
    "agent_activity",
    "observability_manage",
    "metrics_snapshot",
    "check_anomalies",
    "perf_summary",
    "check_anomalies_and_notify",
    "system_doctor",
    "event_log_query",
    "event_log_verify",
    "agent_heartbeat",
    "agent_list",
    "agent_context",
    "session_list",
    "session_start",
    "session_end",
    "propose_session_learnings",
    "propose_skill_clusters",
    "lifecycle_record",
    "mcp_health_telemetry",
    "mcp_gateway",
    "session_packet",
    "message_send",
    "message_list",
    "state_sync_verify",
    "red_team_qa",
    "cleanup_stale_worktrees",
    "vault_to_memory_ingest",
    "vault_read",
    "vault_write",
    "vault_move",
    "vault_archive_plan",
    "vault_list",
    "vault_search",
    "vault_query",
    "vault_links",
    "vault_journal",
    "vault_briefing_digest",
    "vault_ingest_status",
    "vault_wiki_custodian",
    "obsidian_status",
    "obsidian_open",
    "obsidian_active_note",
    "obsidian_search",
    "obsidian_append",
    "langgraph_run",
    "langgraph_progress_note",
    "langgraph_workflow_run",
    "langgraph_checkpoints",
    "export_tasks",
    "export_data",
    "export_memory",
    "export_journal",
    "export_migration_manifest",
    "compare_migration_manifests",
    "import_migration_manifest",
    "rollback_migration_import",
    "read_image",
    "format_render",
    "pi_run_task",
    "browser_derive_task_reference",
    "gcloud_inventory",
    "gcloud_ops",
    "cloud_worker_recover",
    "crew_view",
    "crew_intervene",
}


@pytest.mark.anyio
async def test_list_tools_and_roundtrip():
    async with Client(mcp) as client:
        tools = await client.session.list_tools()
        names = {t.name for t in tools.tools}
        assert names == EXPECTED_TOOLS

        save = await client.session.call_tool(
            "memory_save",
            {"content": "protocol smoke test note", "tags": ["test"]},
        )
        assert not save.is_error
        note_id = json.loads(save.content[0].text)["id"]

        search = await client.session.call_tool("memory_search", {"query": "protocol smoke"})
        results = json.loads(search.content[0].text)["results"]
        assert [r["id"] for r in results] == [note_id]

        err = await client.session.call_tool("fs_read", {"path": "../../etc/passwd"})
        assert err.is_error


@pytest.mark.anyio
async def test_unknown_tool_kwarg_is_rejected_not_dropped():
    """Task #854: ArgModelBase extra='forbid'. An undeclared keyword must fail the
    call, not be silently discarded (which let vault_write(..., mode=...) clobber
    files before the mode= parameter existed)."""
    async with Client(mcp) as client:
        res = await client.session.call_tool(
            "memory_search",
            {"query": "anything", "totally_made_up_kwarg": 1},
        )
        assert res.is_error
        assert "totally_made_up_kwarg" in res.content[0].text


@pytest.mark.anyio
async def test_task_tools_reject_unknown_kwargs():
    """Task #1014: task_update(..., notes_append=...) — a typo of the real
    `append_note` parameter — must fail closed, not silently drop half the
    call and return a 200.

    The server side already does. `extra="forbid"` landed for task #854
    (server.py:114, PR #427, see test_unknown_tool_kwarg_is_rejected_not_dropped
    above) and covers every registered tool through the shared
    `_ArgModelBase.model_config`, task_* included; all 171 tools publish
    `additionalProperties: false` in their input schema. This test pins that
    coverage for the task_* family specifically, so a future change to task
    registration — a hand-built schema, or a resolver that bypasses the shared
    arg model — cannot regress it unnoticed.

    Worth recording because #1014 was filed against a LIVE repro that this
    test cannot reproduce, and the difference is the point: calling
    `task_update(id=..., notes_append=...)` through an MCP *client* against
    the hub still returns a cheerful 200 with the argument gone. The hub's
    checkout contains 7ecd135, the schema it publishes says
    `additionalProperties: false`, and the in-process client here gets a
    validation error — so the key never reaches the server. It is stripped
    client-side, by the caller validating against the published schema before
    sending. That is outside this repo; what this repo owes is the strict
    schema and the strict server, and both are pinned here.
    """
    async with Client(mcp) as client:
        add = await client.session.call_tool("task_add", {"title": "task-1014 probe"})
        assert not add.is_error, add.content
        task_id = json.loads(add.content[0].text)["id"]

        for tool_name, args, unknown_key in (
            ("task_add", {"title": "x", "bogus_param": 1}, "bogus_param"),
            ("task_update", {"id": task_id, "notes_append": "hello"}, "notes_append"),
            ("task_claim", {"id": task_id, "agent": "probe", "timeout": 60}, "timeout"),
            ("task_list", {"status": "open", "notes_contains": "x"}, "notes_contains"),
            ("task_delete", {"id": task_id, "force": True}, "force"),
        ):
            res = await client.session.call_tool(tool_name, args)
            assert res.is_error, f"{tool_name} accepted an unknown kwarg: {args}"
            assert unknown_key in res.content[0].text, (
                f"{tool_name} error does not name the offending key {unknown_key!r}: "
                f"{res.content[0].text}"
            )

        # The documented parameter names still work, and the task is still there
        # (none of the rejected calls above should have partially applied).
        update = await client.session.call_tool(
            "task_update",
            {"id": task_id, "append_note": "hello"},
        )
        assert not update.is_error, update.content
        updated = json.loads(update.content[0].text)
        assert "hello" in updated["notes"]

        claim = await client.session.call_tool(
            "task_claim",
            {"id": task_id, "agent": "probe", "timeout_seconds": 60},
        )
        assert not claim.is_error, claim.content

        listed = await client.session.call_tool(
            "task_list",
            {"status": "all", "notes_contain": "hello"},
        )
        assert not listed.is_error, listed.content
        assert any(t["id"] == task_id for t in json.loads(listed.content[0].text)["tasks"])

        deleted = await client.session.call_tool("task_delete", {"id": task_id})
        assert not deleted.is_error, deleted.content
        assert json.loads(deleted.content[0].text)["deleted"] is True


@pytest.mark.anyio
async def test_mcp_annotations_and_structured_outputs():
    async with Client(mcp) as client:
        tools = (await client.session.list_tools()).tools
        assert all(tool.annotations is not None for tool in tools)
        assert all(
            getattr(tool.annotations, field) is not None
            for tool in tools
            for field in ("read_only_hint", "destructive_hint", "idempotent_hint")
        )

        graph_tool = next(tool for tool in tools if tool.name == "graph_canvas_read")
        claim_tool = next(tool for tool in tools if tool.name == "task_claim")
        assert claim_tool.input_schema["properties"]["usage_only"]["default"] is False
        cycle_usage = next(tool for tool in tools if tool.name == "autonomous_cycle_record_usage")
        assert {"usage_run_id", "usage_id"} <= cycle_usage.input_schema["properties"].keys()
        assert graph_tool.annotations.read_only_hint is True
        assert graph_tool.annotations.destructive_hint is False
        assert graph_tool.output_schema is not None

        evidence_tool = next(tool for tool in tools if tool.name == "evidence_report")
        assert evidence_tool.annotations.read_only_hint is True
        assert evidence_tool.output_schema is not None

        project_tool = next(tool for tool in tools if tool.name == "project_build")
        assert project_tool.annotations.read_only_hint is False
        assert project_tool.output_schema is not None

        graph = await client.session.call_tool("graph_canvas_read", {"board_id": "t828-empty"})
        assert not graph.is_error
        assert graph.structured_content["exists"] is False
        assert graph.content[0].type == "text"

        evidence = await client.session.call_tool("evidence_report", {"task_id": 828})
        assert not evidence.is_error
        assert evidence.structured_content["report_type"] == "user_attributable_evidence"
        assert evidence.content[0].type == "text"


def test_structured_output_schema_rejects_missing_graph_fields():
    tool = mcp._tool_manager._tools["graph_canvas_read"]
    adapter = tool.fn_metadata._output_adapter(tool.fn_metadata.output_model)
    with pytest.raises(ValidationError):
        adapter.validate_python({"board_id": "t828-empty"})


@pytest.mark.anyio
async def test_authenticated_developer_receives_filtered_tool_list(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    session = config.mcp_session_id_var.set("profile-filter-session")
    try:
        async with Client(mcp) as client:
            names = {tool.name for tool in (await client.session.list_tools()).tools}
            assert "fs_write" in names
            assert "task_list" in names
            assert "plan_manage" in names
            assert "crew_view" in names
            assert "crew_intervene" in names
            assert "plan_create" not in names
            assert "git_merge_task" not in names
            assert "vault_move" not in names
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)
        with profiles._lock:
            profiles._active.pop("profile-filter-session", None)


@pytest.mark.anyio
async def test_authenticated_steward_denied_crew_intervene(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    session = config.mcp_session_id_var.set("steward-filter-session")
    try:
        profiles.activate("steward")
        async with Client(mcp) as client:
            names = {tool.name for tool in (await client.session.list_tools()).tools}
            assert "crew_view" in names
            assert "crew_intervene" not in names

        from mcp.server.mcpserver.exceptions import ToolError

        from unified_agent.server import REGISTERED_TOOLS

        with pytest.raises(ToolError, match="not exposed by the active steward profile"):
            REGISTERED_TOOLS["crew_intervene"](session_id="any", message="hi")
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)
        with profiles._lock:
            profiles._active.pop("steward-filter-session", None)


@pytest.mark.anyio
@pytest.mark.parametrize("session_id", [None, "established-session", "recreated-session"])
async def test_authenticated_mcp_list_and_call_keep_default_ceiling(monkeypatch, session_id):
    """Exercise actual MCP handlers in-process against the isolated fixture database."""
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    session = config.mcp_session_id_var.set(session_id)
    with profiles._lock:
        profiles._active.clear()
    try:
        async with Client(mcp) as client:
            names = {tool.name for tool in (await client.session.list_tools()).tools}
            assert "task_list" in names
            assert {"routine_list", "approval_resolve", "profile_activate"}.isdisjoint(names)
            # A caller can retain an old manifest: call enforcement must deny too.
            denied = await client.session.call_tool("routine_list", {})
            assert denied.is_error
            assert "not exposed by the active developer profile" in denied.content[0].text
            allowed = await client.session.call_tool("task_list", {})
            assert not allowed.is_error
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)
        with profiles._lock:
            profiles._active.clear()


def test_streamable_http_app_monkeypatch_keeps_ui_api_and_auth(monkeypatch):
    """Task #373 Tier 3, 4.7(a). server.py replaces mcp.streamable_http_app
    with a wrapper (wrapped_http_app) that appends /ui + /api routes and
    installs HubAuthASGIMiddleware — an MCPServer internals change that alters
    what streamable_http_app returns, or how it's called, could silently
    drop either half with no error anywhere: /ui and /api would 404 and
    UA_ENFORCE_AUTH would stop applying to them, both silently. This asserts
    the actual object the monkeypatch produces still has both properties.
    """
    from unified_agent import server
    from unified_agent.server import HubAuthASGIMiddleware, TrustedListenerProxyHeaders
    from unified_agent.webui.routes import web_routes

    app = server.mcp.streamable_http_app()

    # Task #1892 adds an outermost per-listener forwarded-header layer; auth
    # must sit directly inside it so scope["client"] is already rewritten.
    assert isinstance(app, TrustedListenerProxyHeaders), (
        "mcp.streamable_http_app() no longer returns a "
        "TrustedListenerProxyHeaders-wrapped app — forwarded-header trust "
        "would silently stop being scoped to the trusted listener."
    )
    assert isinstance(app._app, HubAuthASGIMiddleware), (
        "mcp.streamable_http_app() no longer wraps the app in "
        "HubAuthASGIMiddleware — UA_ENFORCE_AUTH would silently stop applying "
        "to /ui and /api."
    )

    inner_paths = {getattr(route, "path", None) for route in app._app.app.router.routes}
    expected_paths = {route.path for route in web_routes()}
    missing = expected_paths - inner_paths
    assert missing == set(), (
        f"the wrapped app's route table is missing {missing} — the "
        f"app.router.routes.extend(webui.web_routes()) call in "
        f"wrapped_http_app() either didn't run or MCPServer's own "
        f"streamable_http_app() stopped returning a mutable Starlette app."
    )


class TestRootRedirect:
    """GET / used to have no route at all, falling through to the auth gate
    and 401ing on every unauthenticated hit — including a browser just
    opening the hub's URL (task #1400). Exact "/" now redirects to /ui,
    without auth; "/foo" and every gated route must still 401."""

    def _client(self, monkeypatch, *, enforce: bool = True):
        from starlette.testclient import TestClient

        from unified_agent import server

        monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:static-token")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1" if enforce else "0")
        return TestClient(server.mcp.streamable_http_app(), follow_redirects=False)

    def test_root_redirects_to_ui_without_auth(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get("/")
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/ui"

    def test_root_redirect_requires_no_bearer_token(self, monkeypatch):
        """The whole point: an anonymous browser hitting the bare hub URL
        gets the redirect, not a 401."""
        client = self._client(monkeypatch)
        resp = client.get("/", headers={})
        assert resp.status_code != 401

    def test_root_redirects_when_auth_is_enforced_without_credentials(self, monkeypatch):
        from starlette.testclient import TestClient

        from unified_agent import server

        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        response = TestClient(server.mcp.streamable_http_app(), follow_redirects=False).get("/")
        assert response.status_code in (302, 307)
        assert response.headers["location"] == "/ui"

    def test_enforced_auth_without_credentials_still_gates_nonpublic_routes(self, monkeypatch):
        from starlette.testclient import TestClient

        from unified_agent import server

        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        client = TestClient(server.mcp.streamable_http_app())
        assert client.get("/foo").status_code == 401
        assert client.get("/api/board").status_code == 401

    def test_public_host_filter_still_blocks_root_without_credentials(self, monkeypatch):
        from starlette.testclient import TestClient

        from unified_agent import server

        monkeypatch.setenv("UA_AGENT_TOKENS", "")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "public.example.test")
        response = TestClient(server.mcp.streamable_http_app()).get(
            "/", headers={"host": "public.example.test"}
        )
        assert response.status_code == 404

    def test_unrelated_root_sibling_still_requires_auth(self, monkeypatch):
        """ "/" is an exact match, never a prefix: "/foo" must still 401."""
        client = self._client(monkeypatch)
        resp = client.get("/foo")
        assert resp.status_code == 401

    def test_gated_api_path_still_requires_auth(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get("/api/board")
        assert resp.status_code == 401

    def test_authenticated_root_still_redirects(self, monkeypatch):
        """Being logged in doesn't change what "/" does — it's always a
        redirect to /ui, never a page of its own."""
        client = self._client(monkeypatch)
        resp = client.get("/", headers={"Authorization": "Bearer static-token"})
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/ui"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_blocked_skill_publish_restores_its_one_shot_approval(monkeypatch, isolated_env):
    from unified_agent import server

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "skill_registry")
    monkeypatch.setattr(server.agent_access, "enforce", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.operational_profiles, "tool_is_allowed", lambda *_args: True)
    monkeypatch.setattr(server.safety, "check_policy_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.config, "verified_agent_identity", lambda: "Desktop_Agent2")

    skill_dir = isolated_env / "broken-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("missing required frontmatter\n")
    params = {"name": "broken-skill", "path": "broken-skill", "author": "Desktop_Agent2"}
    payload = {"action": "publish", "params": params}
    with pytest.raises(approvals.ApprovalPendingError) as pending:
        approvals.check_approval_gate("skill_registry", payload, requested_by="Desktop_Agent2")
    approval_id = pending.value.approval_id
    assert approval_id is not None
    # Approval records use the authenticated identity, never the body label.
    # Switch to the verified human for approval, then back to the requester.
    monkeypatch.setattr(server.config, "verified_agent_identity", lambda: "Desktop_Person")
    approvals.approval_resolve(approval_id, "approve", resolved_by="Operator")
    monkeypatch.setattr(server.config, "verified_agent_identity", lambda: "Desktop_Agent2")

    result = REGISTERED_TOOLS["skill_registry"]("publish", params)

    assert result["blocked"] is True
    assert approvals.get_approval(approval_id)["status"] == "approved"
