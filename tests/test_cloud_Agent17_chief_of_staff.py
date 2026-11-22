"""Cloud-hosted identities: one chief of staff, and the same roster boundaries.

``cloud_agent13`` is the only chief of staff and keeps the full surface.
``cloud_agent8`` stays on the roster and defaults to research. On 2026-11-11 the
operator retired ``cloud_agent17`` from chief-of-staff to developer, the same
default ``cloud_agent1`` has (task #2446). What did not move with a profile,
because it is keyed on the cloud-hosted identity roster
(``config.CLOUD_HOSTED_IDENTITIES``) or on actor kind:

* hub workspace access is denied whatever UA_AGENT_WORKSPACES says;
* the Control Center HTTP routes stay closed (tests/test_cloud_assistant_profile.py);
* a chief of staff cannot activate developer, steward or operator, and a
  cloud developer cannot activate a wider profile;
* approvals, broadcasts and token minting still need a human;
* vault writes keep the fail-closed lock, force, journal and write-dir guards.

Other identities and the public profile (server_agent8 included) are pinned
unchanged at the end. The developer section checks the surface ``cloud_agent17``
and ``cloud_agent1`` keep, and the merge, deploy and credential tools they
do not get.
"""

from __future__ import annotations

import datetime
import json
from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, profiles
from unified_agent.tools import approvals, vault

SYNTHETIC_TOKEN = "synthetic-test-token-cloud-0001"
# cloud_agent13 is the only chief of staff. cloud_agent8 is research.
CLOUD_CHIEFS = ("cloud_agent13",)
CLOUD_DEVELOPERS = ("cloud_agent17", "cloud_agent1", "cloud_agent15", "cloud_agent16")
CLOUD_RESEARCH = ("cloud_agent8",)
CLOUD_LEADS = ("cloud_agent14",)
# Every cloud-hosted identity.
CLOUD_HOSTED = (*CLOUD_CHIEFS, *CLOUD_DEVELOPERS, *CLOUD_RESEARCH, *CLOUD_LEADS)


@contextmanager
def _as(identity: str, session: str):
    """Bind a verified identity and MCP session id, as the HTTP middleware does."""
    who = config.agent_identity_var.set(identity)
    sid = config.mcp_session_id_var.set(session)
    try:
        yield
    finally:
        config.mcp_session_id_var.reset(sid)
        config.agent_identity_var.reset(who)
        profiles._active.pop(session, None)


@pytest.fixture
def cos_vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    for directory in ("Wiki", "00 - Inbox", "Logs", "01 - Projects"):
        (root / directory).mkdir(parents=True)
    (root / "Wiki/Home.md").write_text("# Home\n")
    (root / "01 - Projects/Plan.md").write_text("project plan breadcrumb\n")
    monkeypatch.setenv("UA_VAULT_PATH", str(root))
    # The installation baseline: chief-of-staff alone would add PARA on top.
    monkeypatch.setenv("UA_VAULT_WRITE_DIRS", "00 - Inbox:Logs:Wiki")
    monkeypatch.setenv(
        "UA_AGENT_LABELS",
        ":".join(
            (
                *CLOUD_HOSTED,
                "Desktop_Agent5",
                "Desktop_Agent1",
                "Laptop_Agent5",
            )
        ),
    )
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    # Deliberately no UA_AGENT_WORKSPACES entry: the deny is by identity.
    monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
    return root


@pytest.fixture
def as_cloud_Agent17(cos_vault):
    binding = config.workspace_root_var.set(config.resolve_workspace_binding("cloud_agent17"))
    try:
        with _as("cloud_agent17", "cloud-agent17-cos-test"):
            assert profiles.current() == profiles.DEVELOPER
            yield cos_vault
    finally:
        config.workspace_root_var.reset(binding)


@pytest.fixture(params=CLOUD_CHIEFS)
def as_cloud_chief(request, cos_vault):
    """Each cloud-hosted chief of staff in turn, bound as the HTTP middleware would."""
    identity = request.param
    binding = config.workspace_root_var.set(config.resolve_workspace_binding(identity))
    try:
        with _as(identity, f"{identity}-cos-test"):
            assert profiles.current() == profiles.CHIEF_OF_STAFF
            yield identity
    finally:
        config.workspace_root_var.reset(binding)


@pytest.fixture(params=CLOUD_HOSTED)
def as_cloud_hosted(request, cos_vault):
    """Each cloud-hosted identity at its default profile, bound as the middleware would."""
    identity = request.param
    binding = config.workspace_root_var.set(config.resolve_workspace_binding(identity))
    try:
        with _as(identity, f"{identity}-hosted-test"):
            yield identity
    finally:
        config.workspace_root_var.reset(binding)


# --- identity and tool surface ----------------------------------------------


@pytest.mark.parametrize("identity", CLOUD_CHIEFS)
def test_cloud_chief_defaults_to_chief_of_staff_by_identity_roster(identity):
    assert profiles.default_for_identity(identity) == profiles.CHIEF_OF_STAFF
    assert config.is_cloud_hosted_identity(identity)
    # Not a Hermes process: the unattended-Hermes roster stays as it was.
    assert identity not in profiles.ALWAYS_ON_HERMES_IDENTITIES
    for label in ("Desktop_Agent5", "server_agent5", "gcp_agent5", "Desktop_Agent1", None, ""):
        assert not config.is_cloud_hosted_identity(label)


@pytest.mark.parametrize("identity", CLOUD_CHIEFS)
def test_cloud_chief_is_served_the_full_chief_of_staff_surface(cos_vault, identity):
    from unified_agent import server

    with _as(identity, f"{identity}-surface-test"):
        cloud = server.callable_tool_names()
    assert cloud == set(server.REGISTERED_TOOLS)
    for name in (
        "task_add",
        "task_update",
        "task_claim",
        "task_comment",
        "delegate",
        "delegate_list",
        "handoff",
        "plan_manage",
        "approval_pending",
        "vault_briefing_digest",
        "vault_write",
        "vault_journal",
    ):
        assert name in cloud, name


def _payload(result) -> dict:
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_cloud_chief_runs_chief_of_staff_tools_over_mcp(as_cloud_chief):
    from mcp.client import Client

    from unified_agent import server

    async with Client(server.mcp) as client:
        names = {tool.name for tool in (await client.session.list_tools()).tools}
        assert {"task_add", "task_update", "task_claim", "approval_pending"} <= names
        added = _payload(
            await client.session.call_tool("task_add", {"title": "cloud chief of staff task"})
        )
        task_id = added["id"]
        _payload(
            await client.session.call_tool(
                "task_update", {"id": task_id, "append_note": "groomed by the chief of staff"}
            )
        )
        claimed = _payload(
            await client.session.call_tool("task_claim", {"id": task_id, "agent": as_cloud_chief})
        )
        # The claim is durable; hub-side worktree provisioning is refused by DENY.
        assert claimed["provisioning"]["reason"] == "workspace_denied"
        _payload(
            await client.session.call_tool(
                "task_comment",
                {"action": "append", "params": {"task_id": task_id, "body": "status noted"}},
            )
        )
        for name, arguments in (
            ("delegate_list", {}),
            ("approval_pending", {}),
        ):
            _payload(await client.session.call_tool(name, arguments))


# --- workspace DENY -----------------------------------------------------------


@pytest.mark.parametrize("identity", CLOUD_HOSTED)
def test_cloud_chief_workspace_is_denied_whatever_the_configuration(
    monkeypatch, tmp_path, identity
):
    monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
    assert config.resolve_workspace_binding(identity) is config.WORKSPACE_DENIED
    assert config.resolve_workspace_binding("Desktop_Agent5") is None
    # A path entry cannot grant a cloud-hosted identity the hub's disk.
    monkeypatch.setenv("UA_AGENT_WORKSPACES", f"{identity}:{tmp_path}")
    assert config.resolve_workspace_binding(identity) is config.WORKSPACE_DENIED
    monkeypatch.setenv("UA_AGENT_WORKSPACES", f"{identity}:DENY")
    assert config.resolve_workspace_binding(identity) is config.WORKSPACE_DENIED


def test_cloud_chief_cannot_use_hub_fs_shell_or_git(as_cloud_hosted):
    from unified_agent.tools.files import fs_list, fs_read, fs_write
    from unified_agent.tools.git import git_diff_staged
    from unified_agent.tools.shell import run_cmd

    for tool, args in (
        (fs_list, (".",)),
        (fs_read, ("README.md",)),
        (fs_write, ("note.txt", "must not land")),
        (run_cmd, ("echo hi",)),
        (git_diff_staged, ()),
    ):
        with pytest.raises(ToolError, match="filesystem access is denied"):
            tool(*args)


@pytest.mark.parametrize("identity", CLOUD_HOSTED)
def test_http_middleware_binds_workspace_deny_for_cloud_chief(monkeypatch, identity):
    import secrets

    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from unified_agent.server import HubAuthASGIMiddleware

    token = secrets.token_urlsafe(24)
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{identity}:" + token)
    monkeypatch.setenv("UA_AGENT_LABELS", identity)
    monkeypatch.delenv("UA_AGENT_WORKSPACES", raising=False)
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    seen = []

    async def handler(request):
        seen.append(config.workspace_root_var.get())
        return PlainTextResponse("ok")

    app = HubAuthASGIMiddleware(Starlette(routes=[Route("/mcp", handler)]))
    with TestClient(app) as client:
        response = client.get("/mcp", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    assert seen == [config.WORKSPACE_DENIED]


def _hub_skill(tmp_path):
    workspace = config.process_workspace_root()
    skill = workspace / "skills" / "probe"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("---\nname: probe\ndescription: probe skill\n---\nbody\n")
    return skill


def test_cloud_chief_cannot_audit_or_inspect_the_hub_disk(as_cloud_hosted, tmp_path):
    """security_audit and the skill readers resolve through resolve_for_audit,
    which reads the process workspace even under DENY (PR #985 review)."""
    from unified_agent import skills_registry
    from unified_agent.tools.audit import security_audit
    from unified_agent.tools.devservers import detect_dev_servers
    from unified_agent.tools.skills import skill_review

    skill = _hub_skill(tmp_path)
    denied = "filesystem access is denied"
    for call in (
        lambda: security_audit("."),
        lambda: security_audit(str(skill)),
        lambda: detect_dev_servers("."),
        lambda: skill_review(str(skill)),
        lambda: skills_registry.publish("probe", str(skill)),
        lambda: skills_registry.drift(str(skill.parent)),
    ):
        with pytest.raises(ToolError, match=denied):
            call()
    # A vault-scoped audit reads the vault, which this profile may read.
    assert security_audit("Wiki", vault=True)["root"].endswith("Wiki")


def test_other_deny_identities_keep_their_read_only_audit(cos_vault, tmp_path):
    """The #891 exemption for DENY identities on the operator's machines stays."""
    from unified_agent.tools.audit import security_audit

    _hub_skill(tmp_path)
    binding = config.workspace_root_var.set(config.WORKSPACE_DENIED)
    try:
        with _as("Desktop_Agent5", "hermes-audit-test"):
            assert security_audit(".")["files_scanned"] >= 1
    finally:
        config.workspace_root_var.reset(binding)


def test_cloud_Agent17_cannot_append_through_the_obsidian_plugin(as_cloud_Agent17, monkeypatch):
    """The plugin writes after the hub's lock check; cloud callers use vault_write."""
    from unified_agent.tools import obsidian

    def unexpected(*args, **kwargs):
        raise AssertionError("cloud append reached the Obsidian plugin")

    monkeypatch.setattr(obsidian, "_request", unexpected)
    monkeypatch.delenv("UA_OBSIDIAN_API_URL", raising=False)
    monkeypatch.delenv("UA_OBSIDIAN_API_KEY", raising=False)
    with pytest.raises(ToolError, match="vault_write"):
        obsidian.obsidian_append("Wiki/Home.md", "appended")
    with pytest.raises(ToolError, match="vault_write"):
        obsidian.obsidian_append(
            "Wiki/Home.md", "locked: false", target_type="frontmatter", target="locked"
        )
    vault.vault_write("Wiki/Home.md", "appended\n", mode="append")
    assert (as_cloud_Agent17 / "Wiki/Home.md").read_text() == "# Home\nappended\n"
    # Non-cloud callers keep the plugin path (here: not configured).
    with _as("Desktop_Agent5", "hermes-obsidian-test"):
        assert obsidian.obsidian_append("Wiki/Home.md", "x").get("configured") is False


# --- profile activation -------------------------------------------------------


@pytest.fixture
def authenticated(monkeypatch):
    monkeypatch.setattr(config, "enforce_auth", lambda: True)


@pytest.mark.parametrize("identity", CLOUD_CHIEFS)
@pytest.mark.parametrize("target", [profiles.DEVELOPER, profiles.STEWARD, profiles.OPERATOR])
def test_cloud_chief_cannot_activate_developer_steward_or_operator(authenticated, target, identity):
    with _as(identity, f"{identity}-activate-test"):
        with pytest.raises(ToolError):
            profiles.activate(target)
        assert profiles.current() == profiles.CHIEF_OF_STAFF


@pytest.mark.parametrize("identity", CLOUD_CHIEFS)
def test_cloud_chief_may_only_downgrade_and_cannot_climb_back(authenticated, identity):
    with _as(identity, f"{identity}-downgrade-test"):
        profiles.activate(profiles.CHIEF_OF_STAFF)
        profiles.activate(profiles.CLOUD_VAULT_STEWARD)
        assert profiles.current() == profiles.CLOUD_VAULT_STEWARD
        for target in (profiles.CHIEF_OF_STAFF, profiles.DEVELOPER, profiles.OPERATOR):
            with pytest.raises(ToolError):
                profiles.activate(target)
    # A fresh session starts at the identity default again.
    with _as(identity, f"{identity}-reconnect-test"):
        assert profiles.current() == profiles.CHIEF_OF_STAFF


@pytest.mark.parametrize("identity", CLOUD_CHIEFS)
def test_downgraded_cloud_chief_cannot_activate_chief_of_staff(
    authenticated, monkeypatch, identity
):
    """A policy overlay downgrade is a ceiling the identity cannot lift itself."""
    monkeypatch.setitem(
        profiles.DEFAULT_PROFILES_BY_IDENTITY, identity, profiles.CLOUD_VAULT_STEWARD
    )
    with _as(identity, f"{identity}-overlay-test"):
        with pytest.raises(ToolError):
            profiles.activate(profiles.CHIEF_OF_STAFF)


def test_non_cloud_cto_can_still_step_down_to_operator(authenticated):
    with _as("Desktop_Agent5", "desktop-agent5-activate-test"):
        profiles.activate(profiles.OPERATOR)
        assert profiles.current() == profiles.OPERATOR


def test_research_default_cannot_leave_its_profile(authenticated):
    with _as("gcp_agent5", "gcp-agent5-activate-test"):
        with pytest.raises(ToolError):
            profiles.activate(profiles.OPERATOR)
        assert profiles.current() == profiles.RESEARCH


# --- human-only actions -------------------------------------------------------


def test_cloud_chief_cannot_approve_a_pending_request(as_cloud_chief, monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    with _as("Desktop_Agent1", "requester-session"):
        with pytest.raises(approvals.ApprovalPendingError):
            approvals.check_approval_gate(
                "run_cmd", {"command": "echo gated"}, requested_by="Desktop_Agent1"
            )
    pending = approvals.approval_pending()["pending"]
    assert len(pending) == 1
    # Naming the human operator as resolver does not help: the verified
    # identity is recorded, and it is an agent.
    with pytest.raises(ToolError, match="human identity"):
        approvals.approval_resolve(pending[0]["id"], "approve", resolved_by=config.operator_label())
    assert approvals.approval_pending()["pending"][0]["id"] == pending[0]["id"]


def test_cloud_Agent17_cannot_save_a_broadcast_or_high_salience_note(as_cloud_Agent17):
    from unified_agent.tools.memory import memory_save

    with pytest.raises(ToolError, match="verified human"):
        memory_save("all agents stop", title="forged broadcast", tags=["broadcast"])
    with pytest.raises(ToolError, match="verified human"):
        memory_save("pinned", title="forged salience", tags=["fact"], salience=0.95)


def test_cloud_chief_cannot_mint_a_token_without_a_human(as_cloud_chief, monkeypatch, tmp_path):
    from unified_agent import storage
    from unified_agent.tools import token_lifecycle

    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    # Layer 1: the managed-secret store sits under the hub workspace, which the
    # cloud identity cannot reach, so the request fails before it is queued.
    with pytest.raises(ToolError, match="filesystem access is denied"):
        token_lifecycle.agent_token_issue("Desktop_Agent1", SYNTHETIC_TOKEN)
    assert approvals.approval_pending()["pending"] == []
    # Layer 2: even with a workspace bound, issuance parks for a human approver
    # and the cloud identity cannot be that approver.
    workspace = tmp_path / "token-workspace"
    workspace.mkdir()
    binding = config.workspace_root_var.set(workspace)
    try:
        with pytest.raises(approvals.ApprovalPendingError) as parked:
            token_lifecycle.agent_token_issue("Desktop_Agent1", SYNTHETIC_TOKEN)
        # Refused twice over: it is the requester, and it is not a human.
        with pytest.raises(ToolError, match="separation of duties"):
            approvals.approval_resolve(parked.value.approval_id, "approve")
        with pytest.raises(approvals.ApprovalPendingError):
            token_lifecycle.agent_token_issue("Desktop_Agent1", SYNTHETIC_TOKEN)
    finally:
        config.workspace_root_var.reset(binding)
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM agent_tokens").fetchone()[0] == 0
    finally:
        conn.close()


# --- vault: wider reach, same cloud write guards ------------------------------


def test_cloud_Agent17_vault_reach_is_the_developer_scope(as_cloud_Agent17):
    """Reads stay open. Writes stay on the installation baseline, not PARA."""
    root = as_cloud_Agent17
    assert "project plan breadcrumb" in vault.vault_read("01 - Projects/Plan.md")["content"]
    names = {e["name"] for e in vault.vault_list(".")["entries"]}
    assert {"01 - Projects", "00 - Inbox", "Wiki"} <= names
    vault.vault_write("00 - Inbox/capture.md", "triage me")
    assert (root / "00 - Inbox/capture.md").read_text() == "triage me"


def test_cloud_Agent17_cannot_file_into_para(as_cloud_Agent17):
    """PARA writes stay with chief-of-staff. cloud_agent17 no longer has that profile."""
    root = as_cloud_Agent17
    with pytest.raises(ToolError, match="restricted to"):
        vault.vault_write("01 - Projects/New.md", "filed by the chief of staff")
    assert not (root / "01 - Projects/New.md").exists()
    with _as("cloud_agent13", "t3tots-para-test"):
        vault.vault_write("01 - Projects/Other.md", "allowed")
    assert (root / "01 - Projects/Other.md").read_text() == "allowed"


def test_cloud_chief_para_writes_keep_the_lock_and_force_guards(as_cloud_chief, cos_vault):
    root = cos_vault
    note = root / "01 - Projects/Locked.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\nLOCKED: YES\n---\nprotected\n")
    with pytest.raises(ToolError, match="locked"):
        vault.vault_write("01 - Projects/Locked.md", "clobber")
    with pytest.raises(ToolError, match="force"):
        vault.vault_write("01 - Projects/Locked.md", "clobber", force=True)
    assert note.read_text() == "---\nLOCKED: YES\n---\nprotected\n"


def test_cloud_Agent17_lock_check_fails_closed_and_force_is_refused(as_cloud_Agent17):
    root = as_cloud_Agent17
    note = root / "Wiki/Alias.md"
    note.write_text("---\nLOCKED: YES\n---\nprotected\n")
    with pytest.raises(ToolError, match="locked"):
        vault.vault_write("Wiki/Alias.md", "clobber")
    with pytest.raises(ToolError, match="force"):
        vault.vault_write("Wiki/Home.md", "clobber", force=True)
    with pytest.raises(ToolError, match="chief-of-staff"):
        vault.vault_move("Wiki/Home.md", "Wiki/Moved.md", force=True)
    assert note.read_text() == "---\nLOCKED: YES\n---\nprotected\n"
    assert (root / "Wiki/Home.md").read_text() == "# Home\n"
    # Local callers share the cloud detector (#1817): the alias locks them too,
    # but they keep the explicit force override the cloud scope never gets.
    with _as("Desktop_Agent5", "hermes-lock-test"):
        with pytest.raises(ToolError, match="locked"):
            vault.vault_write("Wiki/Alias.md", "edited")
        assert note.read_text() == "---\nLOCKED: YES\n---\nprotected\n"
        vault.vault_write("Wiki/Alias.md", "edited", force=True)
    assert note.read_text() == "edited"


def test_cloud_chief_journal_keeps_date_tag_and_target_bounds(as_cloud_chief, cos_vault):
    root, agent = cos_vault, as_cloud_chief
    distant = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    with pytest.raises(ToolError, match="within one day"):
        vault.vault_journal("rewrite history", agent=agent, date=distant)
    with pytest.raises(ToolError, match="single-line"):
        vault.vault_journal("entry", agent=agent, tags=["tag\nforged"])
    today = root / f"Logs/{datetime.date.today().isoformat()}.md"
    today.symlink_to(root / "Wiki/Home.md")
    with pytest.raises(ToolError, match="canonical daily Logs note"):
        vault.vault_journal("redirected", agent=agent)
    assert (root / "Wiki/Home.md").read_text() == "# Home\n"
    today.unlink()
    assert vault.vault_journal("briefing posted", agent=agent)["created_note"]
    # A non-cloud identity may still backfill an older day.
    with _as("Laptop_Agent5", "hermes-journal-test"):
        vault.vault_journal("backfill", agent="Laptop_Agent5", date=distant)


def test_cloud_Agent17_glob_cannot_traverse(as_cloud_Agent17):
    with pytest.raises(ToolError, match="relative glob"):
        vault.vault_list(glob="../*")


# --- cloud_agent1: a cloud-hosted developer (task #2446) ----------------------

# Withheld from a developer and so from cloud_agent1: merge, deploy, approvals,
# profile switching and the credential and token tools.
CLOUD_DEVELOPER_WITHHELD = (
    "git_merge_task",
    "git_commit_staged",
    "deployment_claim",
    "approval_resolve",
    "profile_activate",
    "agent_token_issue",
    "agent_token_rotate",
    "managed_secret_set",
    "credential_pool_register",
    "oauth_device_manage",
    "config_revision_manage",
)


def test_cloud_Agent17_is_a_cloud_hosted_developer():
    assert profiles.default_for_identity("cloud_agent17") == profiles.DEVELOPER
    assert profiles.default_for_identity("cloud_agent13") == profiles.CHIEF_OF_STAFF
    assert profiles.default_for_identity("cloud_agent8") == profiles.RESEARCH
    assert config.is_cloud_hosted_identity("cloud_agent17")
    assert "cloud_agent17" not in profiles.ALWAYS_ON_HERMES_IDENTITIES
    assert profiles.cloud_hosted_identity_default("cloud_agent17")


def test_cloud_Agent1_is_a_cloud_hosted_developer():
    assert profiles.default_for_identity("cloud_agent1") == profiles.DEVELOPER
    assert config.is_cloud_hosted_identity("cloud_agent1")
    assert "cloud_agent1" not in profiles.ALWAYS_ON_HERMES_IDENTITIES
    assert profiles.cloud_hosted_identity_default("cloud_agent1")
    # The desktop and laptop Agent1 identities stay off the roster.
    for label in ("Desktop_Agent1", "Laptop_Agent1", "server_agent1"):
        assert not config.is_cloud_hosted_identity(label)


@pytest.mark.parametrize("identity", CLOUD_DEVELOPERS)
def test_cloud_developer_keeps_the_developer_surface_without_merge_deploy_or_credentials(
    cos_vault, identity
):
    from unified_agent import server

    with _as("Desktop_Agent1", "desktop-agent1-surface-test"):
        desktop = server.callable_tool_names()
    with _as(identity, f"{identity}-surface-test"):
        cloud = server.callable_tool_names()
    # The same developer surface: the cloud boundary is the workspace DENY and
    # the closed routes, not a second tool list.
    assert cloud == desktop
    for name in (
        "profile_status",
        "task_list",
        "task_add",
        "task_claim",
        "task_update",
        "task_comment",
        "message_send",
        "message_list",
        "memory_save",
        "memory_search",
        "vault_journal",
        "vault_read",
        "evidence_report",
    ):
        assert name in cloud, name
    for name in CLOUD_DEVELOPER_WITHHELD:
        assert name not in cloud, name


@pytest.mark.parametrize("identity", CLOUD_DEVELOPERS)
@pytest.mark.parametrize(
    "target", [profiles.STEWARD, profiles.CHIEF_OF_STAFF, profiles.CTO, profiles.OPERATOR]
)
def test_cloud_developer_cannot_activate_a_wider_or_lateral_profile(
    authenticated, identity, target
):
    with _as(identity, f"{identity}-activate-{target}"):
        with pytest.raises(ToolError):
            profiles.activate(target)
        assert profiles.current() == profiles.DEVELOPER
        profiles.activate(profiles.RESEARCH)
        assert profiles.current() == profiles.RESEARCH
        with pytest.raises(ToolError):
            profiles.activate(profiles.DEVELOPER)


@pytest.fixture
def as_cloud_Agent1(cos_vault):
    binding = config.workspace_root_var.set(config.resolve_workspace_binding("cloud_agent1"))
    try:
        with _as("cloud_agent1", "cloud-agent1-mcp-test"):
            assert profiles.current() == profiles.DEVELOPER
            yield cos_vault
    finally:
        config.workspace_root_var.reset(binding)


@pytest.mark.anyio
async def test_cloud_Agent1_does_real_bookkeeping_over_mcp(as_cloud_Agent1):
    from mcp.client import Client

    from unified_agent import server

    async with Client(server.mcp) as client:
        names = {tool.name for tool in (await client.session.list_tools()).tools}
        assert not names & set(CLOUD_DEVELOPER_WITHHELD)
        status = _payload(await client.session.call_tool("profile_status", {}))
        assert (status["identity"], status["profile"]) == ("cloud_agent1", "developer")
        task_id = _payload(
            await client.session.call_tool("task_add", {"title": "cloud agent1 task"})
        )["id"]
        claimed = _payload(
            await client.session.call_tool("task_claim", {"id": task_id, "agent": "cloud_agent1"})
        )
        assert claimed["claimed_by"] == "cloud_agent1"
        # The claim is durable; the work happens in the cloud VM's own clone.
        assert claimed["provisioning"]["reason"] == "workspace_denied"
        _payload(
            await client.session.call_tool(
                "task_update",
                {
                    "id": task_id,
                    "append_note": "PR opened",
                    "evidence_ref": "https://github.com/example/repo/pull/1",
                    "worktree": False,
                },
            )
        )
        evidence = _payload(await client.session.call_tool("evidence_report", {"task_id": task_id}))
        assert "cloud_agent1" in json.dumps(evidence)
        _payload(
            await client.session.call_tool(
                "vault_journal", {"entry": "cloud agent1 journal", "agent": "cloud_agent1"}
            )
        )
        # A denied call: the hub's disk is out of reach whatever the tool list says.
        denied = await client.session.call_tool("fs_write", {"path": "x.txt", "content": "no"})
        assert denied.is_error
        assert "filesystem access is denied" in denied.content[0].text
        hidden = await client.session.call_tool("git_merge_task", {"task_id": task_id})
        assert hidden.is_error


# --- nothing else widened -----------------------------------------------------


def test_server_Agent8_stays_public_and_off_the_cloud_roster():
    """The Grok CLI labels and server_agent8 stay off the cloud roster."""
    assert profiles.default_for_identity("server_agent8") == profiles.PUBLIC
    assert not config.is_cloud_hosted_identity("server_agent8")
    for label in ("Desktop_Agent8", "Laptop_Agent8"):
        assert profiles.default_for_identity(label) == profiles.RESEARCH
        assert not config.is_cloud_hosted_identity(label)
    assert config.CLOUD_HOSTED_IDENTITIES == frozenset(CLOUD_HOSTED)


def test_other_identities_and_the_public_profile_are_unchanged(cos_vault):
    from unified_agent import server

    assert profiles.default_for_identity("server_agent8") == profiles.PUBLIC
    assert profiles.default_for_identity("Desktop_Agent8") == profiles.RESEARCH
    assert profiles.default_for_identity("some_unmapped_agent") == profiles.DEVELOPER
    assert profiles.PUBLIC_ALLOWED_TOOLS == frozenset(
        {
            "task_list",
            "task_add",
            "dash_list",
            "dash_search",
            "dash_view",
            "profile_status",
            "format_render",
        }
    )
    assert profiles.CLOUD_ASSISTANT_ALLOWED_TOOLS == frozenset(
        {
            "profile_status",
            "observability_manage",
            "task_list",
            "memory_search",
            "memory_get",
            "memory_list",
            "message_list",
            "message_send",
        }
    )
    assert profiles.CLOUD_VAULT_STEWARD_ALLOWED_TOOLS == profiles.CLOUD_ASSISTANT_ALLOWED_TOOLS | {
        "vault_read",
        "vault_list",
        "vault_search",
        "vault_write",
        "vault_journal",
    }
    with _as("server_agent8", "public-surface-test"):
        assert server.callable_tool_names() <= profiles.PUBLIC_ALLOWED_TOOLS
        assert "task_add" in server.callable_tool_names()
