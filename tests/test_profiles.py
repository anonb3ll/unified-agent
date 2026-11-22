import asyncio
import hashlib

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, policy, profiles, project_bindings
from unified_agent.safety import resolve_in_vault
from unified_agent.tools import graph as graph_tool
from unified_agent.tools import profiles as profile_tool
from unified_agent.tools import vault as vault_tool
from unified_agent.tools.tasks import task_add, task_claim, task_update


@pytest.fixture(autouse=True)
def clean_profile_state():
    with profiles._lock:
        profiles._active.clear()
    yield
    with profiles._lock:
        profiles._active.clear()


@pytest.fixture
def authenticated_session(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    session = config.mcp_session_id_var.set("mcp-session-a")
    try:
        yield
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


def test_developer_cannot_activate_a_more_privileged_profile(authenticated_session):
    """Calling activate() in-process skips tool exposure. The ceiling still holds."""
    assert profiles.current() == "developer"
    assert profiles.tool_is_allowed("profile_activate") is False
    assert profiles.tool_is_allowed_for("steward", "profile_activate") is False
    assert profiles.tool_is_allowed_for("chief-of-staff", "profile_activate") is True
    with pytest.raises(ToolError, match="more privileged"):
        profiles.activate("chief-of-staff")
    assert profiles.current() == "developer"
    assert profiles.activate("steward").name == "steward"
    assert profiles.activate("developer").name == "developer"
    assert profiles.activate("operator").name == "operator"


def test_operator_cannot_activate_chief_of_staff(monkeypatch):
    """The operator allow-list excludes profile_activate, and activate() refuses too.

    Direct activate() is not the MCP path. The list the hub serves is
    ``allowed_tools`` over the registered names, and that list omits
    ``profile_activate`` for Desktop_Person and the Operator alias.
    """
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    registered = {"profile_activate", "approval_resolve", "task_list", "fs_write"}
    # Operator is the same person. The alias's code default is developer, and
    # the operator ceiling still refuses chief-of-staff.
    for label, expected in (("Desktop_Person", "operator"), ("Operator", "developer")):
        identity = config.agent_identity_var.set(label)
        session = config.mcp_session_id_var.set(f"mcp-session-{label}")
        try:
            assert profiles.current() == expected
            exposed = profiles.allowed_tools(registered)
            assert "profile_activate" not in exposed
            if label == "Desktop_Person":
                assert "approval_resolve" in exposed
            with pytest.raises(ToolError, match="operator profile"):
                profiles.activate("chief-of-staff")
            assert profiles.current() == expected
        finally:
            config.mcp_session_id_var.reset(session)
            config.agent_identity_var.reset(identity)
            with profiles._lock:
                profiles._active.clear()


def test_cos_identity_can_switch_back_to_its_default(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("cloud_agent13")
    session = config.mcp_session_id_var.set("mcp-session-t3tots-switch")
    try:
        assert profiles.activate("research").name == "research"
        with pytest.raises(ToolError):
            profiles.activate("chief-of-staff")
        assert profiles.reset() == "chief-of-staff"
        assert profiles.current() == "chief-of-staff"
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)
        with profiles._lock:
            profiles._active.clear()


def test_activation_requires_authenticated_session(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    with pytest.raises(ToolError, match="authenticated HTTP MCP session"):
        profiles.activate("chief-of-staff")


def test_profile_is_bound_to_verified_identity_and_session(authenticated_session):
    assert profiles.activate("steward").name == "steward"
    assert profiles.current() == "steward"

    other = config.agent_identity_var.set("Desktop_Agent1")
    try:
        assert profiles.current() == "developer"
    finally:
        config.agent_identity_var.reset(other)

    new_session = config.mcp_session_id_var.set("mcp-session-b")
    try:
        assert profiles.current() == "developer"
    finally:
        config.mcp_session_id_var.reset(new_session)


def test_profile_status_reports_nonsecret_verified_token_metadata(authenticated_session):
    metadata = config.auth_metadata_var.set(
        {"auth_method": "client_credentials", "iat": 1000, "scopes": ["mcp"]}
    )
    try:
        status = profile_tool.profile_status()
    finally:
        config.auth_metadata_var.reset(metadata)
    assert status["identity"] == "Desktop_Agent2"
    assert status["auth_method"] == "client_credentials"
    assert status["scopes"] == ["mcp"]
    assert status["token_age_s"] >= 0


@pytest.mark.parametrize(
    "identity", ["Desktop_Agent1", "Desktop_Agent2", "Desktop_agent3", "cloud_agent17"]
)
def test_developer_identities_default_to_developer(identity):
    token = config.agent_identity_var.set(identity)
    try:
        assert profiles.current() == "developer"
    finally:
        config.agent_identity_var.reset(token)


def test_cto_identity_defaults_to_cto():
    """Desktop_Agent5 is the CTO seat (#1788, #1884)."""
    token = config.agent_identity_var.set("Desktop_Agent5")
    try:
        assert profiles.current() == "cto"
    finally:
        config.agent_identity_var.reset(token)


def test_cto_cannot_climb_to_chief_of_staff_but_can_step_down(monkeypatch):
    """The CTO default is a ceiling like any other non-human default (#1884)."""
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Desktop_Agent5")
    session = config.mcp_session_id_var.set("mcp-session-cto")
    try:
        assert profiles.current() == "cto"
        assert profiles.tool_is_allowed("profile_activate") is False
        with pytest.raises(ToolError, match="more privileged"):
            profiles.activate("chief-of-staff")
        assert profiles.current() == "cto"
        assert profiles.activate("developer").name == "developer"
        assert profiles.current() == "developer"
        assert profiles.activate("cto").name == "cto"
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)
        with profiles._lock:
            profiles._active.clear()


def test_developer_cannot_activate_cto(authenticated_session):
    with pytest.raises(ToolError, match="more privileged"):
        profiles.activate("cto")
    assert profiles.current() == "developer"


def test_cto_overlay_may_downgrade_but_never_widen():
    from unified_agent.config_revisions import _overlay_is_allowed

    assert _overlay_is_allowed("developer", "cto")
    assert _overlay_is_allowed("steward", "cto")
    assert not _overlay_is_allowed("cto", "developer")
    assert not _overlay_is_allowed("chief-of-staff", "cto")
    assert _overlay_is_allowed("cto", "chief-of-staff")


def test_cto_vault_scope_is_the_developer_baseline(monkeypatch):
    """CTO gets no PARA write scope; that stays with the chief of staff."""
    monkeypatch.setattr(profiles, "current", lambda: "cto")
    assert profiles.vault_write_dirs() == tuple(config.vault_write_dirs())


def test_cos_surfaces_default_to_cos():
    """cloud_agent13 is the only chief-of-staff surface.

    cloud_agent8 is research. cloud_agent17 and Laptop_Agent5 are developers.
    Desktop_Agent5 is the CTO.
    """
    hermes = config.agent_identity_var.set("cloud_agent13")
    try:
        assert profiles.current() == "chief-of-staff"
    finally:
        config.agent_identity_var.reset(hermes)


def test_desktop_person_web_ui_defaults_to_operator(monkeypatch):
    """The human Web UI operator resolves approvals; no git/vault mutation."""
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Desktop_Person")
    session = config.mcp_session_id_var.set("mcp-session-operator")
    try:
        assert profiles.current() == "operator"
        assert profiles.tool_is_allowed("approval_resolve")
        assert profiles.tool_is_allowed("approval_pending")
        assert not profiles.tool_is_allowed("git_merge_task")
        assert not profiles.tool_is_allowed("vault_move")
        assert profiles.vault_write_dirs() == ()
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


@pytest.mark.parametrize("identity", ["Desktop_Agent8", "Laptop_Agent8"])
def test_grok_defaults_to_research(identity, monkeypatch):
    """Grok Bot is a fleet supplement: memory/vault/web, not run_cmd or claim."""
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    token = config.agent_identity_var.set(identity)
    session = config.mcp_session_id_var.set("mcp-session-grok")
    try:
        assert profiles.current() == "research"
        assert profiles.tool_is_allowed("memory_search")
        assert profiles.tool_is_allowed("task_list")
        assert not profiles.tool_is_allowed("run_cmd")
        assert not profiles.tool_is_allowed("task_claim")
        assert not profiles.tool_is_allowed("fs_write")
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(token)


def test_cos_allows_para_but_steward_does_not(tmp_path, monkeypatch, authenticated_session):
    vault = tmp_path / "vault"
    for folder in (
        "00 - Inbox",
        "01 - Projects",
        "02 - Areas",
        "03 - Resources",
        "04 - Archive",
        "Logs",
        "Wiki",
    ):
        (vault / folder).mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))

    with pytest.raises(ToolError, match="restricted to"):
        resolve_in_vault("01 - Projects/brief.md", for_write=True)

    profiles.activate("steward")
    with pytest.raises(ToolError, match="restricted to"):
        resolve_in_vault("01 - Projects/brief.md", for_write=True)

    human = config.agent_identity_var.set("cloud_agent13")
    try:
        assert profiles.current() == "chief-of-staff"
        assert (
            resolve_in_vault("01 - Projects/brief.md", for_write=True)
            == vault / "01 - Projects/brief.md"
        )
    finally:
        config.agent_identity_var.reset(human)


def test_steward_can_reach_vault_write_scoped_to_curated_dirs(
    tmp_path, monkeypatch, authenticated_session
):
    """Steward's write scope is exactly Inbox/Logs/Wiki (task #544)."""
    vault = tmp_path / "vault"
    for folder in ("00 - Inbox", "01 - Projects", "Logs", "Wiki"):
        (vault / folder).mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))

    profiles.activate("steward")
    assert profiles.tool_is_allowed("vault_write")
    assert resolve_in_vault("Wiki/note.md", for_write=True) == vault / "Wiki/note.md"
    with pytest.raises(ToolError, match="restricted to"):
        resolve_in_vault("01 - Projects/brief.md", for_write=True)


def test_reset_restores_developer_scope(tmp_path, monkeypatch, authenticated_session):
    vault = tmp_path / "vault"
    (vault / "01 - Projects").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    profiles.activate("steward")
    assert profiles.current() == "steward"
    assert profiles.reset() == "developer"
    with pytest.raises(ToolError, match="restricted to"):
        resolve_in_vault("01 - Projects/brief.md", for_write=True)


def test_hermes_reset_restores_cos_scope(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "01 - Projects").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("cloud_agent13")
    session = config.mcp_session_id_var.set("mcp-session-t3tots")
    try:
        profiles.activate("research")
        with pytest.raises(ToolError, match="restricted to"):
            resolve_in_vault("01 - Projects/brief.md", for_write=True)
        assert profiles.reset() == "chief-of-staff"
        assert (
            resolve_in_vault("01 - Projects/brief.md", for_write=True)
            == vault / "01 - Projects/brief.md"
        )
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


def test_vault_write_records_verified_identity(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Wiki").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    identity = config.agent_identity_var.set("cloud_agent17")
    recorded = []
    monkeypatch.setattr(
        vault_tool.events,
        "record_event",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    try:
        result = vault_tool.vault_write("Wiki/attributed.md", "test")
    finally:
        config.agent_identity_var.reset(identity)
    assert result == {
        "path": "Wiki/attributed.md",
        "bytes_written": 4,
        "mode": "overwrite",
        "append": False,
        "total_lines_after": 1,
        "revision": "sha256:" + hashlib.sha256(b"test").hexdigest(),
    }
    assert recorded == [
        (
            ("vault.write",),
            {
                "payload": {
                    "path": "Wiki/attributed.md",
                    "bytes_written": 4,
                    "mode": "overwrite",
                    "append": False,
                },
                "originator": "cloud_agent17",
            },
        )
    ]


def test_authenticated_profiles_filter_tool_exposure(monkeypatch, authenticated_session):
    tools = {
        "fs_write",
        "git_merge_task",
        "task_list",
        "vault_move",
        "notify",
        "plan_manage",
        "plan_create",
        "export_data",
        "export_tasks",
        "delegation_manage",
        "delegate_list",
        "coordination_manage",
        "dependency_list",
        "autonomous_cycle_manage",
        "autonomous_cycle_start",
        "bug_worker_manage",
        "observability_manage",
        "agent_activity",
        "check_anomalies_and_notify",
    }
    assert profiles.allowed_tools(tools) == {
        "fs_write",
        "task_list",
        "plan_manage",
        "export_data",
        "delegation_manage",
        "coordination_manage",
        "autonomous_cycle_manage",
        "bug_worker_manage",
        "observability_manage",
    }

    profiles.activate("steward")
    assert profiles.allowed_tools(tools) == {"task_list", "observability_manage"}

    human = config.agent_identity_var.set("cloud_agent13")
    try:
        assert profiles.current() == "chief-of-staff"
        assert profiles.allowed_tools(tools) == tools
    finally:
        config.agent_identity_var.reset(human)


def test_stdio_or_legacy_mode_keeps_existing_exposure(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
    assert profiles.allowed_tools({"git_merge_task", "vault_move"}) == {
        "git_merge_task",
        "vault_move",
    }
    assert "plan_create" in profiles.allowed_tools({"plan_create"})
    assert "export_tasks" in profiles.allowed_tools({"export_tasks"})


def test_consolidated_actions_keep_legacy_aliases_hidden_but_callable(
    monkeypatch, authenticated_session
):
    assert "observability_manage" in profiles.allowed_tools({"observability_manage"})
    assert "agent_activity" not in profiles.allowed_tools({"agent_activity"})
    assert profiles.consolidated_action_is_allowed("observability_manage", "agent_activity")
    assert not profiles.consolidated_action_is_allowed(
        "observability_manage", "check_anomalies_and_notify"
    )
    assert profiles.consolidated_action_is_allowed("export_data", "export_tasks")
    assert profiles.consolidated_action_is_allowed("delegation_manage", "delegate_list")
    assert profiles.consolidated_action_is_allowed("delegation_manage", "delegate_release")
    assert profiles.consolidated_action_is_allowed("delegation_manage", "delegate_cancel")
    assert profiles.consolidated_action_is_allowed("coordination_manage", "dependency_list")
    assert profiles.consolidated_action_is_allowed(
        "autonomous_cycle_manage", "autonomous_cycle_list"
    )
    profiles.activate("steward")
    assert not profiles.consolidated_action_is_allowed("delegation_manage", "delegate_release")
    assert not profiles.consolidated_action_is_allowed("delegation_manage", "delegate_cancel")


# --- research profile (task #498) -------------------------------------------
#
# The regression these guard against: developer and steward are deny lists, so
# every tool the server registers is reachable until denied. Research is an
# allow list, and these tests exist to keep it one.


def test_research_denies_execution_and_mutation(authenticated_session):
    profiles.activate("research")
    for tool in (
        "run_cmd",
        "run_cmd_check",
        "pi_run_task",
        "gemini_ask",
        "fs_write",
        "python_lint_and_format",
        "git_commit_staged",
        "git_merge_task",
        "git_worktree_add",
        "workflow_run",
        "delegate",
        "notify",
        "memory_delete",
        "task_delete",
        "task_update",
        "task_claim",
        "vault_move",
        "export_data",
    ):
        assert not profiles.tool_is_allowed(tool), f"{tool} must not be reachable"


def test_research_allows_its_research_and_planning_surface(authenticated_session):
    profiles.activate("research")
    for tool in (
        "fs_read",
        "fs_search",
        "web_search",
        "web_fetch",
        "memory_save",
        "memory_search",
        "task_add",
        "task_list",
        "plan_manage",
        "coordination_manage",
        "vault_read",
        "vault_write",
        "vault_journal",
        "event_log_query",
        "format_render",
        "capability_catalog",
        "check_agent_capability",
    ):
        assert profiles.tool_is_allowed(tool), f"{tool} must be reachable"


def test_research_excludes_unknown_tools_by_default(authenticated_session):
    """An allow list must not widen when the server registers a new tool."""
    profiles.activate("research")
    allowed = profiles.allowed_tools({"fs_read", "some_tool_added_next_quarter"})
    assert allowed == {"fs_read"}


def test_research_consolidated_manager_does_not_leak_hidden_actions(
    authenticated_session,
):
    """plan_manage is allowed; the destructive aliases behind it are not."""
    profiles.activate("research")
    assert profiles.consolidated_action_is_allowed("plan_manage", "plan_create")
    assert profiles.consolidated_action_is_allowed("plan_manage", "plan_list")
    assert profiles.consolidated_action_is_allowed("plan_manage", "plan_preview")
    assert not profiles.consolidated_action_is_allowed("plan_manage", "plan_review")
    assert not profiles.consolidated_action_is_allowed("plan_manage", "plan_delete")
    assert not profiles.consolidated_action_is_allowed("plan_manage", "plan_handoff")
    assert not profiles.consolidated_action_is_allowed("plan_manage", "plan_step_claim")
    assert not profiles.consolidated_action_is_allowed("plan_manage", "plan_close")
    # A manager that is not allowed at all cannot be used as a side door.
    assert not profiles.consolidated_action_is_allowed("delegation_manage", "delegate")


@pytest.fixture
def project_repo_binding(authenticated_session):
    return project_bindings.bind("browser", "acme/browser", default_branch="main")["binding"]


@pytest.mark.parametrize("action", ["project_repo_get", "project_repo_list"])
def test_research_can_read_project_repo_bindings_through_coordination_manage(
    project_repo_binding, action, monkeypatch
):
    profiles.activate("research")
    policy_calls = []
    real_gate = graph_tool.safety.check_policy_gate

    def checked_gate(tool, payload):
        policy_calls.append((tool, dict(payload)))
        real_gate(tool, payload)

    monkeypatch.setattr(graph_tool.safety, "check_policy_gate", checked_gate)
    params = {"project": "browser"} if action == "project_repo_get" else {}
    result = graph_tool.coordination_manage(action, params)

    if action == "project_repo_get":
        assert result == {"project": "browser", "binding": project_repo_binding}
        assert policy_calls == [("get", params)]
    else:
        row = next(row for row in result["projects"] if row["project"] == "browser")
        assert row["binding"] == project_repo_binding
        assert policy_calls == [("list_bindings", params)]


@pytest.mark.parametrize("action", ["project_repo_bind", "project_repo_unbind"])
def test_research_cannot_write_project_repo_bindings_through_coordination_manage(
    project_repo_binding, action
):
    profiles.activate("research")
    params = {"project": "browser"}
    if action == "project_repo_bind":
        params.update(repo="acme/replacement", default_branch="replacement")

    with pytest.raises(ToolError, match="not exposed by the active profile"):
        graph_tool.coordination_manage(action, params)

    assert project_bindings.get("browser")["binding"] == project_repo_binding


@pytest.mark.parametrize("profile_name", ["developer", "research"])
@pytest.mark.parametrize("action", ["project_repo_get", "project_repo_list"])
def test_project_repo_binding_reads_remain_subject_to_policy(
    project_repo_binding, profile_name, action, tmp_path, monkeypatch
):
    profiles.activate(profile_name)
    policy_file = tmp_path / "binding-policy.yaml"
    policy_file.write_text("enabled: true\ndefault_action: deny\nrules: []\n", encoding="utf-8")
    monkeypatch.setattr(config, "policy_file_path", lambda: policy_file)
    params = {"project": "browser"} if action == "project_repo_get" else {}

    with pytest.raises(policy.PolicyViolation) as denied:
        graph_tool.coordination_manage(action, params)

    assert denied.value.reason_code == "default_deny"
    assert project_bindings.get("browser")["binding"] == project_repo_binding


def test_research_cannot_escalate_to_another_profile(authenticated_session):
    profiles.activate("research")
    assert not profiles.tool_is_allowed("profile_activate")
    with pytest.raises(ToolError, match="cannot activate another profile"):
        profiles.activate("chief-of-staff")
    assert profiles.current() == "research"


def test_research_resets_back_to_identity_default(authenticated_session):
    profiles.activate("research")
    assert profiles.reset() == "developer"
    assert profiles.current() == "developer"


def test_research_vault_writes_are_confined_to_inbox_and_logs(authenticated_session):
    profiles.activate("research")
    assert profiles.vault_write_dirs() == ("00 - Inbox", "Logs")
    assert "Wiki" not in profiles.vault_write_dirs()


def test_research_is_a_valid_profile_name():
    assert "research" in profiles.VALID_PROFILES


def test_research_cannot_use_profile_reset_or_session_end(authenticated_session):
    profiles.activate("research")
    assert not profiles.tool_is_allowed("profile_reset")
    assert not profiles.tool_is_allowed("session_end")


def test_research_rejects_vault_ingest_mark(tmp_path, monkeypatch, authenticated_session):
    from unified_agent.tools import vault as vault_tool

    vault = tmp_path / "vault"
    inbox = vault / "00 - Inbox"
    inbox.mkdir(parents=True)
    note = inbox / "capture.md"
    note.write_text("some capture\n", encoding="utf-8")
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    profiles.activate("research")
    with pytest.raises(ToolError, match="cannot mark vault ingest"):
        vault_tool.vault_ingest_status(
            str(note.relative_to(vault)), mark=True, agent="Desktop_agent3"
        )


# --- public profile (task #859) --------------------------------------------
#
# The public profile is the surface a client reachable from the public
# internet (the Grok Bot connector via Tailscale Funnel) gets. It is an
# allow list even tighter than research: tasks/projects status reads plus
# task_add proposals, nothing else. These tests keep it that way.


@pytest.fixture
def grok_bot_session(monkeypatch):
    """An authenticated HTTP session presenting the public OAuth identity."""
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("server_agent8")
    session = config.mcp_session_id_var.set("mcp-session-grok-bot")
    try:
        yield
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


def test_server_Agent8_defaults_to_public(grok_bot_session):
    """The OAuth front-door identity gets status reads + task_add, nothing else."""
    assert profiles.current() == "public"
    for tool in (
        "task_list",
        "task_add",
        "dash_list",
        "dash_search",
        "dash_view",
        "profile_status",
        "format_render",
    ):
        assert profiles.tool_is_allowed(tool), f"{tool} must be reachable"
    for tool in (
        "task_update",
        "task_claim",
        "task_delete",
        "approval_pending",
        "approval_resolve",
        "memory_search",
        "memory_save",
        "vault_read",
        "vault_write",
        "web_search",
        "web_fetch",
        "fs_read",
        "fs_write",
        "run_cmd",
        "run_cmd_check",
        "gemini_ask",
        "message_send",
        "message_list",
        "event_log_query",
        "session_list",
        "capability_catalog",
        "delegate",
        "workflow_run",
        "profile_activate",
        "profile_reset",
        "session_end",
    ):
        assert not profiles.tool_is_allowed(tool), f"{tool} must not be reachable"


def test_desktop_Agent8_tailnet_cli_is_unchanged():
    """Splitting out server_agent8 must not narrow the tailnet grok CLI."""
    token = config.agent_identity_var.set("Desktop_Agent8")
    try:
        assert profiles.current() == "research"
    finally:
        config.agent_identity_var.reset(token)


def test_public_excludes_unknown_tools_by_default(grok_bot_session):
    """An allow list must not widen when the server registers a new tool."""
    allowed = profiles.allowed_tools({"task_list", "some_tool_added_next_quarter"})
    assert allowed == {"task_list"}


def test_tool_is_allowed_for_agrees_with_ambient_check_under_a_session(grok_bot_session):
    """The pure per-profile check must answer exactly like ``tool_is_allowed``
    when the ambient MCP session is active — the two cannot drift."""
    for tool in ("task_list", "task_add", "approval_pending", "event_log_query", "run_cmd"):
        assert profiles.tool_is_allowed_for(profiles.current(), tool) == profiles.tool_is_allowed(
            tool
        )


def test_tool_is_allowed_for_bites_without_an_mcp_session(monkeypatch):
    """Authenticated requests without a session still obey the identity default."""
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Desktop_Agent8")
    session = config.mcp_session_id_var.set(None)
    try:
        assert profiles.current() == "research"
        assert profiles.tool_is_allowed("approval_pending") is False
        assert profiles.tool_is_allowed_for("research", "approval_pending") is False
        assert profiles.tool_is_allowed_for("research", "event_log_query") is True
        assert profiles.tool_is_allowed_for("developer", "approval_pending") is True
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


@pytest.mark.parametrize("session_id", [None, "established-session", "recreated-session"])
@pytest.mark.parametrize(
    ("identity", "profile", "denied"),
    [
        ("Desktop_Agent2", "developer", "routine_list"),
        ("Desktop_Person", "operator", "fs_write"),
        ("Desktop_Agent8", "research", "approval_pending"),
        ("server_agent8", "public", "fs_write"),
    ],
)
def test_identity_default_ceiling_survives_missing_or_recreated_session(
    monkeypatch, identity, profile, denied, session_id
):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity_token = config.agent_identity_var.set(identity)
    session_token = config.mcp_session_id_var.set(session_id)
    try:
        assert profiles.current() == profile
        assert profiles.allowed_tools({"task_list", denied}) == {"task_list"}
        assert profiles.tool_is_allowed("task_list")
        assert not profiles.tool_is_allowed(denied)
    finally:
        config.mcp_session_id_var.reset(session_token)
        config.agent_identity_var.reset(identity_token)


def test_profile_ceiling_survives_reset_then_session_context_loss(authenticated_session):
    profiles.activate("steward")
    assert profiles.current() == "steward"
    # Simulate a new hub process losing its session overlay cache, without a restart.
    with profiles._lock:
        profiles._active.clear()
    assert profiles.current() == "developer"
    for session_id in (None, "recreated-after-reset"):
        token = config.mcp_session_id_var.set(session_id)
        try:
            assert profiles.current() == "developer"
            assert not profiles.tool_is_allowed("routine_list")
            assert not profiles.tool_is_allowed("profile_activate")
            assert profiles.tool_is_allowed("task_list")
        finally:
            config.mcp_session_id_var.reset(token)


def test_cos_default_without_session_keeps_its_legitimate_surface(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("cloud_agent13")
    session = config.mcp_session_id_var.set(None)
    try:
        assert profiles.current() == "chief-of-staff"
        assert profiles.allowed_tools({"task_list", "routine_list"}) == {
            "task_list",
            "routine_list",
        }
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


def test_authenticated_missing_session_emits_bounded_diagnostic(monkeypatch, caplog):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    session = config.mcp_session_id_var.set(None)
    try:
        with caplog.at_level("WARNING", logger="unified_agent.profiles"):
            profiles.allowed_tools({"task_list", "routine_list"})
        assert any(
            record.name == "unified_agent.profiles"
            and record.getMessage()
            == (
                "authenticated profile request has no MCP session id; "
                "using identity default ceiling"
            )
            for record in caplog.records
        )
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)


def test_public_cannot_escalate_to_another_profile(grok_bot_session):
    assert not profiles.tool_is_allowed("profile_activate")
    with pytest.raises(ToolError, match="cannot activate another profile"):
        profiles.activate("research")
    assert profiles.current() == "public"


def test_public_has_no_vault_write_dirs(grok_bot_session):
    assert profiles.vault_write_dirs() == ()


def test_public_consolidated_managers_are_shut(grok_bot_session):
    """No manager is on the allow list, so no legacy alias leaks through one."""
    for manager, action in (
        ("plan_manage", "plan_list"),
        ("plan_manage", "plan_create"),
        ("coordination_manage", "dependency_list"),
        ("observability_manage", "agent_activity"),
    ):
        assert not profiles.consolidated_action_is_allowed(manager, action)


def test_public_is_a_valid_profile_name():
    assert "public" in profiles.VALID_PROFILES


def test_server_Agent8_is_a_governed_fleet_identity():
    assert config.is_known_agent_label("server_agent8")
    assert config.canonical_agent_label("SERVER_Agent8") == "server_agent8"


# --- steward task_update claim guard (task #591) ----------------------------
#
# task_update is exposed to steward (unlike task_claim/task_delete, which stay
# denied), but grooming another agent's claimed task is refused — the tool
# itself enforces this by comparing the caller's identity against claimed_by,
# not the profile allow/deny list, since that layer never sees a task id.


def test_steward_can_update_an_unclaimed_task(authenticated_session):
    task_id = task_add("unclaimed steward test task")["id"]
    profiles.activate("steward")
    updated = task_update(task_id, notes="groomed by steward")
    assert updated["notes"] == "groomed by steward"


def test_steward_can_update_a_task_it_claimed_itself(authenticated_session):
    task_id = task_add("self-claimed steward test task")["id"]
    task_claim(task_id, agent="Desktop_Agent2")  # authenticated_session identity
    profiles.activate("steward")
    updated = task_update(task_id, notes="groomed by steward")
    assert updated["notes"] == "groomed by steward"


def _set_claimed_by(task_id: int, agent: str) -> None:
    """Write claimed_by directly, bypassing task_claim's identity verification —
    task_claim refuses to claim on behalf of another identity under enforced
    auth, but a task genuinely claimed by a different agent's own session is
    exactly the real-world state this guard exists for."""
    from unified_agent import storage

    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET claimed_by=? WHERE id=?", (agent, task_id))
        conn.commit()
    finally:
        conn.close()


def test_steward_cannot_update_a_task_claimed_by_another_agent(authenticated_session):
    task_id = task_add("someone else's claimed task")["id"]
    _set_claimed_by(task_id, "Desktop_Agent4")
    profiles.activate("steward")
    with pytest.raises(ToolError, match="claimed by 'Desktop_Agent4'"):
        task_update(task_id, notes="steward should not land this")


def test_developer_is_unaffected_by_the_steward_claim_guard(authenticated_session):
    """The guard is steward-specific — developer's existing ability to update
    another agent's claimed task (e.g. picking up a handoff) is unchanged."""
    task_id = task_add("developer picks up a handoff")["id"]
    _set_claimed_by(task_id, "Desktop_Agent4")
    assert profiles.current() == "developer"
    updated = task_update(task_id, notes="picked up by a different developer")
    assert updated["notes"] == "picked up by a different developer"


# --- crew_view / crew_intervene exposure matrix (task #1151) -----------------
#
# the operator's 2026-09-19 authorization granted crew_view to dev agents; the same
# change extends read-only crew_view to the operator profile (the human
# intervenes through the CSRF-gated POST /api/action/crew-intervene, so the
# MCP view carries no intervention capability). The matrix pins all six
# profiles so a future profile edit cannot silently widen or narrow the
# surface: crew_intervene stays developer/chief-of-staff only.


def test_crew_tools_profile_exposure_matrix():
    # server.py applies its steward union (crew_intervene denied) at import;
    # importing it here makes the pin evaluate the same sets production
    # enforces even when this module runs standalone.
    from unified_agent import server  # noqa: F401

    expected = {
        profiles.DEVELOPER: {"crew_view": True, "crew_intervene": True},
        profiles.CHIEF_OF_STAFF: {"crew_view": True, "crew_intervene": True},
        profiles.STEWARD: {"crew_view": True, "crew_intervene": False},
        profiles.OPERATOR: {"crew_view": True, "crew_intervene": False},
        profiles.RESEARCH: {"crew_view": False, "crew_intervene": False},
        profiles.PUBLIC: {"crew_view": False, "crew_intervene": False},
    }
    for profile, tools in expected.items():
        for tool, allowed in tools.items():
            assert profiles.tool_is_allowed_for(profile, tool) is allowed, (
                f"{profile} must {'reach' if allowed else 'be denied'} {tool}"
            )


# --- default-profile ceiling (task #1230 / Epic B, B2) -----------------------
#
# The developer profile is the default every engineering identity lands on.
# B2 shrinks it under 100 tools by profile only — the pruned names stay
# registered and reachable under an explicit profile, and a call to one under
# the default is denied and logged (never silently), so real consumers
# surface. The ceiling test exists so the surface cannot silently regrow.

DEFAULT_PROFILE_MAX_TOOLS = 100


def test_default_profile_tool_count_is_capped_at_100():
    """Measured ceiling — importing server pins the real registry, so a new
    tool landing cannot push the default profile back over the cap silently."""
    from unified_agent import server

    exposed = profiles._allowed_tools_for_profile(profiles.DEVELOPER, set(server.REGISTERED_TOOLS))
    assert len(exposed) <= DEFAULT_PROFILE_MAX_TOOLS


def test_pruned_tools_stay_reachable_under_an_explicit_profile():
    """Removal is by profile, not deletion: every pruned name must remain
    registered and reachable under chief-of-staff."""
    from unified_agent import server

    pruned = profiles.DEFAULT_PROFILE_PRUNED_TOOLS
    assert pruned, "the prune set must not be empty"
    assert pruned <= set(server.REGISTERED_TOOLS), (
        f"pruned names not registered: {sorted(pruned - set(server.REGISTERED_TOOLS))}"
    )
    for name in pruned:
        assert not profiles.tool_is_allowed_for(profiles.DEVELOPER, name), name
        assert profiles.tool_is_allowed_for(profiles.CHIEF_OF_STAFF, name), name


def test_developer_profile_exposes_the_wiki_custodian():
    """Task #1957: steward mode runs on the developer default (profile_activate
    is hidden there, #869), and the steward lint procedure starts from
    vault_wiki_custodian(dry_run=True). The tool was pruned from the default
    profile in #687, so the documented baseline was unreachable. It must stay
    in the served developer surface, inside the 100-tool ceiling."""
    from unified_agent import server

    exposed = profiles._allowed_tools_for_profile(profiles.DEVELOPER, set(server.REGISTERED_TOOLS))
    assert "vault_wiki_custodian" in exposed
    assert profiles.tool_is_allowed_for(profiles.DEVELOPER, "vault_wiki_custodian")
    assert "vault_wiki_custodian" not in profiles.DEFAULT_PROFILE_PRUNED_TOOLS
    assert profiles.tool_is_allowed_for(profiles.STEWARD, "vault_wiki_custodian")
    # The swap that keeps the ceiling: obsidian_open drives the Obsidian UI on
    # the hub host and has no agent-side consumer.
    assert "obsidian_open" in profiles.DEFAULT_PROFILE_PRUNED_TOOLS
    assert "obsidian_open" not in exposed


def test_skill_registry_readers_are_limited_to_trusted_profiles():
    for profile in (profiles.PUBLIC, profiles.RESEARCH, profiles.OPERATOR, profiles.DEVELOPER):
        assert not profiles.tool_is_allowed_for(profile, "skill_registry"), profile
    assert profiles.tool_is_allowed_for(profiles.STEWARD, "skill_registry")
    assert profiles.tool_is_allowed_for(profiles.CHIEF_OF_STAFF, "skill_registry")


def test_pruned_names_do_not_overlap_existing_developer_denials():
    """A name already denied to developer contributes nothing to the shrink —
    the prune set must name only newly-pruned tools."""
    assert not (profiles.DEFAULT_PROFILE_PRUNED_TOOLS & profiles.DEVELOPER_DENIED_TOOLS)


def test_pruned_tool_call_is_denied_and_logged(authenticated_session, caplog):
    """A call to a name pruned from the default profile must log the
    deprecated name (so real consumers surface), then fail closed."""
    import logging

    from unified_agent import server

    assert profiles.current() == profiles.DEVELOPER
    assert "horizon2_server_list" in profiles.DEFAULT_PROFILE_PRUNED_TOOLS
    with caplog.at_level(logging.WARNING, logger="unified_agent.server"):
        with pytest.raises(ToolError, match="not exposed by the active developer profile"):
            server.REGISTERED_TOOLS["horizon2_server_list"]()
    assert any("horizon2_server_list" in record.getMessage() for record in caplog.records), (
        "the denied call must log the deprecated tool name"
    )


def test_unpruned_denial_still_logs_the_tool_name(authenticated_session, caplog):
    """Profile denials log the name whether or not it came from the B2 prune
    set — the log is the consumer-discovery mechanism."""
    import logging

    from unified_agent import server

    assert "metrics_snapshot" not in profiles.DEFAULT_PROFILE_PRUNED_TOOLS
    with caplog.at_level(logging.WARNING, logger="unified_agent.server"):
        with pytest.raises(ToolError, match="not exposed by the active developer profile"):
            server.REGISTERED_TOOLS["metrics_snapshot"]()
    assert any("metrics_snapshot" in record.getMessage() for record in caplog.records)


def test_developer_can_close_a_plan_through_plan_manage(authenticated_session):
    """Task #1496: plan_close is reachable only via the consolidated manager."""
    profiles.activate("developer")
    assert profiles.consolidated_action_is_allowed("plan_manage", "plan_close")


@pytest.mark.anyio
async def test_http_route_authority_expires_in_an_inherited_background_task(authenticated_session):
    ready = asyncio.Event()
    release = asyncio.Event()

    async def inherited():
        ready.set()
        await release.wait()
        return profiles.tool_is_allowed("routine_list")

    with profiles.http_route_authority("Desktop_Agent2", ("routine_list",), ()):
        task = asyncio.create_task(inherited())
        await ready.wait()
        assert profiles.tool_is_allowed("routine_list")
    release.set()
    assert not await task
    assert not profiles.tool_is_allowed("routine_list")


@pytest.mark.parametrize("revoke", ["step", "parent"])
async def test_http_workflow_step_authority_expires_with_step_or_parent(
    authenticated_session, revoke, monkeypatch
):
    monkeypatch.setattr(config, "operator_label", lambda: "Desktop_Agent2")
    ready = asyncio.Event()
    release = asyncio.Event()

    async def inherited_step():
        with profiles.http_workflow_step_authority("routine_list"):
            assert profiles.tool_is_allowed("routine_list")
            ready.set()
            await release.wait()
            return profiles.tool_is_allowed("routine_list")

    with profiles.http_route_authority(
        "Desktop_Agent2", ("workflow_run",), (), human_workflow=True
    ):
        if revoke == "parent":
            task = asyncio.create_task(inherited_step())
            await asyncio.wait_for(ready.wait(), timeout=2)
        else:
            with profiles.http_workflow_step_authority("routine_list"):

                async def inherited():
                    ready.set()
                    await release.wait()
                    return profiles.tool_is_allowed("routine_list")

                task = asyncio.create_task(inherited())
                await asyncio.wait_for(ready.wait(), timeout=2)
            release.set()
            assert not await task
    if revoke == "parent":
        release.set()
        assert not await task
    assert not profiles.tool_is_allowed("routine_list")


def test_mcp_workflow_step_context_does_not_grant_authority(authenticated_session):
    with profiles.http_workflow_step_authority("routine_list"):
        assert not profiles.tool_is_allowed("routine_list")


@pytest.mark.parametrize("purpose", [False, True])
def test_dispatch_preview_is_explicit_and_has_only_two_reads(authenticated_session, purpose):
    with profiles.http_route_authority(
        "Desktop_Agent2", ("task_update",), (), dispatch_preview=purpose
    ):
        assert not profiles.http_read_tool_is_allowed("dependency_list", {})
        with profiles.http_dispatch_preview_authority():
            assert profiles.http_read_tool_is_allowed("dependency_list", {}) is purpose
            assert profiles.http_read_tool_is_allowed("task_list", {}) is purpose
            assert not profiles.http_read_tool_is_allowed("vault_read", {})
            assert not profiles.tool_is_allowed("dependency_add")
        assert not profiles.http_read_tool_is_allowed("dependency_list", {})


@pytest.mark.parametrize("revoke", ["preview", "parent"])
async def test_dispatch_preview_expires_in_inherited_context(authenticated_session, revoke):
    ready = asyncio.Event()
    release = asyncio.Event()

    async def inherited():
        ready.set()
        await release.wait()
        return profiles.http_read_tool_is_allowed("dependency_list", {})

    with profiles.http_route_authority(
        "Desktop_Agent2", ("task_update",), (), dispatch_preview=True
    ):
        with profiles.http_dispatch_preview_authority():
            assert profiles.http_read_tool_is_allowed("dependency_list", {})
            task = asyncio.create_task(inherited())
            await asyncio.wait_for(ready.wait(), timeout=2)
        if revoke == "preview":
            release.set()
            assert not await task
    if revoke == "parent":
        release.set()
        assert not await task


def test_workflow_legacy_alias_preserves_manager_permission_and_hard_denial(authenticated_session):
    with profiles.http_route_authority("Desktop_Agent2", ("workflow_run",), ()):
        with profiles.http_workflow_step_authority("plan_list"):
            assert profiles.tool_is_allowed("plan_list")
        with profiles.http_workflow_step_authority("review_and_notify"):
            assert not profiles.tool_is_allowed("review_and_notify")
        with profiles.http_workflow_step_authority("routine_list"):
            assert not profiles.tool_is_allowed("routine_list")


@pytest.mark.parametrize("alias,manager", sorted(profiles._WORKFLOW_LEGACY_MANAGERS.items()))
def test_workflow_alias_mapping_uses_registered_manager_and_curated_step(alias, manager):
    from unified_agent import server
    from unified_agent.tools import workflow

    assert manager in server.REGISTERED_TOOLS
    assert alias in workflow._tool_registry()
    assert alias in profiles.CONSOLIDATED_LEGACY_TOOLS
    assert profiles.consolidated_action_is_allowed_for(profiles.DEVELOPER, manager, alias)


def test_route_invocation_session_binds_only_the_callers_open_session(monkeypatch, tmp_path):
    """Task #2480: a browser route may bind a registered session of its own caller."""
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent import registry, storage

    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    own = registry.start_session("Desktop_Agent2", "control_center")
    other = registry.start_session("Laptop_Agent5", "control_center")
    ended = registry.start_session("Desktop_Agent2", "control_center")
    registry.end_session(ended)
    with pytest.raises(ToolError, match="checked HTTP route"):
        with profiles.http_route_invocation_session(own):
            pass
    token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        with profiles.http_route_authority("Desktop_Agent2", ("langgraph_workflow_run",), ()):
            for refused in (other, ended, "no-such-session"):
                with pytest.raises(ToolError, match="open session of the verified caller"):
                    with profiles.http_route_invocation_session(refused):
                        pass
            with profiles.http_route_invocation_session(own):
                assert profiles._current_session_id() == own
            assert profiles._current_session_id() is None
    finally:
        config.agent_identity_var.reset(token)
