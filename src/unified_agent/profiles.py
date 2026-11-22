"""Authenticated, session-scoped operational profiles.

Profiles are capability overlays, not agent identities.  They live only in the
hub process and are keyed by the verified agent plus the MCP session id, so a
fresh session starts as its identity's safe default. Authenticated requests without
a session id still obey that default ceiling. No profile comes from prompt text,
environment variables, or a client-provided identity.

A profile decides who can *see* a tool (``tools/list`` and the call ceiling:
a hidden tool is not callable). Whether a visible tool's call is allowed is
the policy gate, including per-caller rate limits. Configuring a rate limit
does not remove the tool from a profile.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from mcp.server.mcpserver.exceptions import ToolError

from . import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _HttpRouteAuthority:
    identity: str
    tools: frozenset[str]
    actions: frozenset[tuple[str, str]]
    revoked: threading.Event = field(default_factory=threading.Event)
    parent: _HttpRouteAuthority | None = None
    read_tools: frozenset[str] = frozenset()
    dispatch_preview: bool = False
    human_workflow: bool = False
    operator_vault_write: bool = False


_http_route_authority: ContextVar[_HttpRouteAuthority | None] = ContextVar(
    "ua_http_route_authority", default=None
)


@contextmanager
def http_route_authority(
    identity: str | None,
    tools: tuple[str, ...],
    actions: tuple[tuple[str, str], ...],
    *,
    read_tools: tuple[str, ...] = (),
    dispatch_preview: bool = False,
    human_workflow: bool = False,
) -> Iterator[None]:
    """Carry a successfully checked HTTP route's declared capabilities.

    Called only by the route wrapper AFTER its authentication, CSRF and scope
    checks. This is server-owned context, never a header or tool argument.
    It follows the handler into worker threads and is reset even on failure.
    Undeclared calls and a changed identity still use their normal ceiling;
    discovery never sees this authority. Downstream policy/access/approval
    gates remain in the registered wrapper.
    """
    authority = None
    if identity and identity == config.verified_agent_identity():
        authority = _HttpRouteAuthority(
            identity,
            frozenset(tools) | {name for pair in actions for name in pair},
            frozenset(actions),
            read_tools=frozenset(read_tools),
            dispatch_preview=dispatch_preview,
            human_workflow=human_workflow and identity == config.operator_label(),
        )
    token = _http_route_authority.set(authority)
    try:
        yield
    finally:
        # ContextVars are copied into child tasks. Revoke the shared authority
        # too, so a task that outlives its handler cannot keep the capability.
        if authority is not None:
            authority.revoked.set()
        _http_route_authority.reset(token)


def _current_http_route_authority() -> _HttpRouteAuthority | None:
    authority = _http_route_authority.get()
    current = authority
    identity = config.verified_agent_identity()
    while current is not None:
        if current.revoked.is_set() or current.identity != identity:
            return None
        current = current.parent
    return authority


def http_read_tool_is_allowed(name: str, payload: dict[str, object]) -> bool:
    """Admit a declared HTTP read helper at the guarded call, never discovery.

    The mixed ingest tool must retain its read shape. Its HTTP handler pins
    mark=False; even another call inside that handler cannot turn the read grant
    into an ingest write. Pure helpers are closed-vocabulary route declarations.
    """
    if current_session_grant() is not None:
        return False
    authority = _current_http_route_authority()
    if authority is None or name not in authority.read_tools:
        return False
    if name == "vault_ingest_status":
        return payload.get("mark", False) is False
    return True


def http_route_human_operator() -> bool:
    """True inside a checked route whose caller is the verified human operator.

    The route wrapper sets ``human_workflow`` only for an ``operator`` scope on an
    operator-recovery channel with the identity ``UA_OPERATOR_LABEL``; nothing a
    caller sends can set it. Used by the LangGraph research resume (task #2522),
    which lets the operator continue a run another identity owns.
    """
    authority = _current_http_route_authority()
    return authority is not None and authority.human_workflow


@contextmanager
def http_dispatch_preview_authority() -> Iterator[None]:
    """Allow only the admitted dispatch-stack POST's two guarded preview reads.

    The wrapper pins this purpose to that exact method/path after scope checks.
    The handler exits this child context before entering the launch boundary;
    inherited child tasks lose the grant when either preview or handler exits.
    This does not authorize arbitrary read helpers on mutating routes.
    """
    parent = _current_http_route_authority()
    if parent is None or not parent.dispatch_preview:
        yield
        return
    authority = _HttpRouteAuthority(
        parent.identity,
        frozenset(),
        frozenset(),
        parent=parent,
        read_tools=frozenset({"task_list", "dependency_list"}),
    )
    token = _http_route_authority.set(authority)
    try:
        yield
    finally:
        authority.revoked.set()
        _http_route_authority.reset(token)


@contextmanager
def http_operator_vault_write_authority() -> Iterator[None]:
    """Let the verified human operator write the vault from a Control Center route.

    The operator profile has no vault write dirs, so ``operator=True`` admitted the
    browser's vault writes and the resolver then refused them (PR #1507 review). Only
    the CC2 vault write, journal and memory-promote handlers enter this. It grants
    only under a route authority whose ``human_workflow`` is set, which the route
    wrapper sets for an ``operator`` scope on an operator-recovery channel and which
    requires the identity to be ``UA_OPERATOR_LABEL``. The grant resolves to the
    configured ``UA_VAULT_WRITE_DIRS``; the resolver's other checks (locked notes,
    the ledger anchor dir, suffixes) are unchanged.
    """
    parent = _current_http_route_authority()
    if parent is None or not parent.human_workflow:
        yield
        return
    authority = _HttpRouteAuthority(
        parent.identity,
        parent.tools,
        parent.actions,
        parent=parent,
        read_tools=parent.read_tools,
        human_workflow=True,
        operator_vault_write=True,
    )
    token = _http_route_authority.set(authority)
    try:
        yield
    finally:
        authority.revoked.set()
        _http_route_authority.reset(token)


@contextmanager
def http_route_invocation_session(session_id: str) -> Iterator[None]:
    """Bind a registered hub session as a checked HTTP route's invocation session.

    Some tools refuse a call without a session (``_current_session_id()``; the
    LangGraph workflow runner, task #2480), and the hub middleware binds one only
    from an ``Mcp-Session-Id`` header, which a browser request has none of. A route
    may instead register a real ``agent_sessions`` row for the verified caller and
    bind it here, for one guarded call. Refused outside a live route authority, and
    for a row that is missing, ended, or owned by another identity, so no session id
    is invented and none is borrowed. Nothing else changes: the profile is still the
    identity default (no activation exists under that id), and the session id
    selects no authority on its own.
    """
    authority = _current_http_route_authority()
    if authority is None:
        raise ToolError("an invocation session can only be bound by a checked HTTP route")
    from . import registry

    row = registry.get_session(session_id)
    if row is None or row.get("ended_at") or row.get("agent") != authority.identity:
        raise ToolError("the invocation session must be an open session of the verified caller")
    token = config.mcp_session_id_var.set(session_id)
    try:
        yield
    finally:
        config.mcp_session_id_var.reset(token)


# Only curated workflow aliases whose consolidated twins preserve the named
# profile's existing permission. An explicit deny never becomes compatibility.
_WORKFLOW_LEGACY_MANAGERS = {
    **dict.fromkeys(("delegate", "delegate_list"), "delegation_manage"),
    **dict.fromkeys(
        (
            "plan_create",
            "plan_update",
            "plan_get",
            "plan_list",
            "plan_checkpoint",
            "plan_resume",
            "plan_step_retry",
            "plan_step_claim",
            "plan_handoff",
        ),
        "plan_manage",
    ),
    **dict.fromkeys(("export_tasks", "export_memory"), "export_data"),
}


@contextmanager
def http_workflow_step_authority(tool: str) -> Iterator[None]:
    """Narrow an admitted HTTP workflow to one validated repo-defined step.

    Only the workflow runner calls this after loading a trusted definition and
    checking its curated static tool names. Tool arguments never select the
    capability. Only the verified human operator inherits browser step authority;
    agent steps retain explicit profile denials and finite legacy compatibility.
    MCP workflows retain their profile ceiling. Every step still
    invokes its guarded callable, including policy/access/approval checks.
    """
    parent = _current_http_route_authority()
    if parent is None or "workflow_run" not in parent.tools:
        yield
        return
    if not parent.human_workflow:
        profile = current()
        manager = _WORKFLOW_LEGACY_MANAGERS.get(tool)
        if not tool_is_allowed_for(profile, tool) and not (
            manager and consolidated_action_is_allowed_for(profile, manager, tool)
        ):
            yield
            return
    authority = _HttpRouteAuthority(parent.identity, frozenset({tool}), frozenset(), parent=parent)
    token = _http_route_authority.set(authority)
    try:
        yield
    finally:
        authority.revoked.set()
        _http_route_authority.reset(token)


DEVELOPER = "developer"
STEWARD = "steward"
CHIEF_OF_STAFF = "chief-of-staff"
CTO = "cto"
RESEARCH = "research"
OPERATOR = "operator"
PUBLIC = "public"
CLOUD_ASSISTANT = "cloud-assistant"
CLOUD_VAULT_STEWARD = "cloud-vault-steward"
# Code-only profile for a node-launched ACP session holding a session-scoped
# hub credential (task #1744). It cannot be activated; the hub middleware binds
# it through ``session_grant_var`` for the request's duration.
ACP_SESSION = "acp-session"
VALID_PROFILES = frozenset(
    {
        DEVELOPER,
        STEWARD,
        CHIEF_OF_STAFF,
        CTO,
        RESEARCH,
        OPERATOR,
        PUBLIC,
        CLOUD_ASSISTANT,
        CLOUD_VAULT_STEWARD,
        ACP_SESSION,
    }
)
CLOUD_ASSISTANT_ALLOWED_TOOLS = frozenset(
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
CLOUD_ASSISTANT_ALLOWED_ACTIONS = frozenset({"agent_context", "agent_list"})
CLOUD_VAULT_STEWARD_ALLOWED_TOOLS = CLOUD_ASSISTANT_ALLOWED_TOOLS | frozenset(
    {"vault_read", "vault_list", "vault_search", "vault_write", "vault_journal"}
)
CLOUD_PROFILES = frozenset({CLOUD_ASSISTANT, CLOUD_VAULT_STEWARD})

# The phase-1 tool set for a session credential (task #1744, design section 6).
# An intersection, never a subtraction: a tool registered later is unreachable
# from a session until a reviewed change adds it here. Excluded by design and
# only addable with operator approval: anything that writes files, runs
# commands or uses git; task_update, task_claim and task_add; dispatch,
# approval, delegation and messaging tools; vault writes; managed secrets and
# token tools; profile switching.
ACP_SESSION_ALLOWED_TOOLS = frozenset(
    {
        "profile_status",
        "memory_search",
        "memory_get",
        "memory_list",
        "memory_save",
        "task_list",
        "task_comment",
        "session_packet",
        "evidence_report",
    }
)


@dataclass(frozen=True)
class SessionGrant:
    """The frozen authority of one request made with a session credential.

    Bound only by the hub middleware after every per-request check passed.
    ``tools`` is already the effective set: the identity ceiling, intersected
    with ``ACP_SESSION_ALLOWED_TOOLS`` and the credential's own tool list.
    """

    tools: frozenset[str]
    credential_id: str
    item_id: str
    task_id: int | None


session_grant_var: ContextVar[SessionGrant | None] = ContextVar("ua_session_grant", default=None)


def current_session_grant() -> SessionGrant | None:
    """The session-credential grant bound to this request, if any."""
    return session_grant_var.get()


DEFAULT_WRITE_DIRS = ("00 - Inbox", "Logs", "Wiki")
RESEARCH_WRITE_DIRS = ("00 - Inbox", "Logs")
OPERATOR_WRITE_DIRS: tuple[str, ...] = ()
PUBLIC_WRITE_DIRS: tuple[str, ...] = ()

# Allow-list profiles cannot escalate: profile_activate is outside their tool
# set (an MCP caller can't reach it) and activate() refuses in-process/stdio
# callers that bypass the exposure layer.
_NON_ESCALATING_PROFILES = frozenset({RESEARCH, PUBLIC}) | CLOUD_PROFILES
COS_WRITE_DIRS = (
    "00 - Inbox",
    "01 - Projects",
    "02 - Areas",
    "03 - Resources",
    "04 - Archive",
    "Logs",
    "Wiki",
)

# The configured operator is the authenticated human Web UI operator —
# approvals and read-only status, not the full chief-of-staff surface.
# cloud_agent13 is the chief of staff; Laptop_Agent5 holds the same profile as
# the operator's on-device backup (2026-11-14). Engineering identities keep the
# developer default unless a named seat (CTO) says otherwise. A non-human
# cannot activate a profile above its default.
DEFAULT_PROFILES_BY_IDENTITY = {
    # cloud_agent13 (T3 Tots) is the chief of staff. It runs the queue,
    # briefings, dispatch coordination and sweeps. Cloud-host boundaries key
    # on config.CLOUD_HOSTED_IDENTITIES, not on this profile: hub workspace
    # DENY, MCP-only HTTP, activate() only keeps this default or steps down
    # to an allow-list profile, and vault writes keep the cloud guards.
    "cloud_agent13": CHIEF_OF_STAFF,
    # cloud_agent8 stays registered so live tokens and sessions still resolve.
    # It was chief of staff (task #2284) and is now an executive assistant:
    # personal admin, light reads, and light coordination. No profile has a
    # calendar tool. research is the lightest allow-list that still covers
    # notes, vault inbox/logs, web reads, messages, and plans.
    "cloud_agent8": RESEARCH,
    # cloud_agent1 is Claude Code on the web (task #2446) and cloud_agent17 is the
    # same class of engineering member (operator decision 2026-11-11; cloud_agent17
    # was chief of staff from 2026-10-02, task #1799, until then): tasks,
    # messages, memory, vault journal and evidence like any developer.
    # Developer is the fallback for an unmapped label anyway; the explicit
    # entries record that the default is a decision. What makes either a
    # *cloud* developer is config.CLOUD_HOSTED_IDENTITIES, not this profile: the
    # hub workspace is DENY (fs_*/run_cmd/git_* and the audit readers refuse),
    # the Control Center routes are closed, activate() allows only a downgrade
    # to an allow-list profile, and vault writes keep the cloud guards. The
    # developer deny list already withholds merge (git_merge_task), deploy
    # (deployment_claim), approvals and the credential tools.
    "cloud_agent17": DEVELOPER,
    "cloud_agent1": DEVELOPER,
    # cloud_agent14 sits alongside Desktop_Agent5 on the CTO profile.
    # cloud_agent15 stays a developer. cloud_agent16 designs bots; research cannot
    # write the workspace, so it is a developer.
    "cloud_agent14": CTO,
    "cloud_agent15": DEVELOPER,
    "cloud_agent16": DEVELOPER,
    # Desktop_Agent5 is the CTO / Head Engineer (the operator's 2026-10-02
    # decision, task #1788), not the chief of staff: cloud_agent13 owns the
    # queue, briefings, dispatch coordination and sweeps. #1788 first moved it
    # to the DEVELOPER default; the operator then asked for a CTO seat holding
    # some authority developers do not (2026-10-06, task #1884), so it gets the
    # CTO profile: the developer surface plus CTO_EXTRA_TOOLS.
    #
    # What it still withholds, deliberately (DEVELOPER_DENIED_TOOLS): the
    # chief-of-staff broadcast and durable-schedule tools (notify,
    # check_anomalies_and_notify, routine_*), approval_resolve and
    # config_revision_manage (authority config), the dispatch-cancellation
    # pair (operator authority), git_merge_task (the deploy steward merges),
    # and profile_activate. The identity cannot raise itself to
    # chief-of-staff: the tool is denied, and activate() refuses a non-human
    # caller asking for a profile above its default. Widening the CTO surface
    # means editing CTO_EXTRA_TOOLS, which is a reviewed change.
    "Desktop_Agent5": CTO,
    #
    # Fleet coordination is not lost for the CTO seat. The bare A2A names
    # (agent_list, agent_activity, agent_heartbeat, delegate*, handoff*) are
    # hidden from DEVELOPER (task #1230), but it holds the consolidated
    # managers, and consolidated_action_is_allowed authorizes each manager
    # action against the underlying name. tests/test_identity_profiles.py
    # pins both halves.
    #
    # Laptop_Agent5 is the operator's on-device chief-of-staff backup (the
    # operator, 2026-11-14): rarely used, but a non-human cannot activate above
    # its default, so the profile has to be the default. It is not cloud-hosted.
    "Laptop_Agent5": CHIEF_OF_STAFF,
    # gcp_agent5 is the GCP executive-office worker (research, calendar,
    # inbox). research is that allow-list; it is not chief of staff.
    "gcp_agent5": RESEARCH,
    # server_agent5 is the hub-hosted CTO surface (the operator, 2026-10-06,
    # task #1884), so it takes the CTO profile with Desktop_Agent5. It stays
    # on ALWAYS_ON_HERMES_IDENTITIES and coordinates through the consolidated
    # managers. Chief-of-staff duties belong to cloud_agent13.
    "server_agent5": CTO,
    # gcp_agent20 is a background worker (developer: it executes). gcp_agent18 is
    # meeting prep and coordination, and gcp_agent19 keeps the knowledge
    # base. Both are research. None of them is chief of staff.
    "gcp_agent20": DEVELOPER,
    "gcp_agent18": RESEARCH,
    "gcp_agent19": RESEARCH,
    # The human operator's login (config.operator_label()) maps to OPERATOR;
    # it is added by profiles_by_identity() from settings, not compiled in.
    # Grok Bot (and the grok CLI when it presents this identity) is a fleet
    # supplement, not a developer. Research is memory/vault/web plus task_list;
    # it cannot claim, dispatch, or run_cmd.
    "Desktop_Agent8": RESEARCH,
    "Laptop_Agent8": RESEARCH,
    # server_agent8 is the identity the OAuth front door issues for the public
    # Grok Bot connector (task #859). It is reachable from the public internet
    # via Tailscale Funnel, so its default is tighter than research: tasks and
    # projects status reads, plus task_add to file proposals for a human to
    # approve. No memory, vault, web, messaging, shell, approvals, or execution.
    # The Grok CLI labels were retired 2026-09-27; their mappings stay so an
    # unrevoked token keeps its tight profile rather than defaulting to developer.
    "server_agent8": PUBLIC,
}

#: The always-on Hermes surfaces: identities that run unattended and so cannot
#: ask a human for a profile -- the GCP worker's Hermes profiles and the MBP
#: that hosts the hub. Declared as an explicit roster so minting one is a
#: deliberate change *here* and in the mapping above. Their profiles are the
#: map above (research, developer, or CTO). None of them is chief of staff;
#: that seat is cloud_agent13, with the attended Laptop_Agent5 as backup. tests/test_identity_profiles.py asserts the
#: roster and the mapping agree in both directions.
ALWAYS_ON_HERMES_IDENTITIES = (
    "gcp_agent5",
    "server_agent5",
    "gcp_agent20",
    "gcp_agent18",
    "gcp_agent19",
)

# Profile exposure is deliberately deny-list based for the first rollout: new
# tools remain reachable by default until they receive an explicit review, while
# authenticated HTTP sessions get least-privilege boundaries for the existing
# high-impact operations. Chief-of-staff is the full operational profile.
DEVELOPER_DENIED_TOOLS = frozenset(
    {
        "approval_resolve",
        # A developer session that can call this becomes chief-of-staff and
        # then reaches approval_resolve. activate() enforces the same ceiling
        # in-process, where tool exposure does not run. Chief-of-staff keeps
        # the tool; steward inherits this deny.
        "profile_activate",
        "check_anomalies_and_notify",
        "git_commit_staged",
        "git_merge_task",
        "import_migration_manifest",
        "notify",
        "review_and_notify",
        # Durable schedules create future tasks without a live requester. The
        # chief-of-staff profile owns routine policy and activation.
        "routine_add",
        "routine_fire",
        "routine_list",
        "routine_pause",
        "rollback_migration_import",
        "vault_archive_plan",
        "vault_move",
        # Releasing an unconfirmed dispatch slot is human-operator authority.
        "dispatch_retry_cancellation",
        "dispatch_confirm_cancellation",
        # Authority config (task #1352) is a chief-of-staff write. Developer
        # and steward inherit this deny; rollback is also approval-gated.
        "config_revision_manage",
    }
)
# The CTO seat (operator decision 2026-10-06, task #1884) is the developer
# surface plus these tools, which other engineering identities reach only
# through chief-of-staff. Each serves a CTO duty in the charter (PR review
# verdicts, tests and CI, the technical quality bar), and none grants
# authority the CTO does not hold: no approvals, authority config, merge,
# general outbound notification, durable schedules, profile changes, or
# destructive vault and migration surfaces. review_and_notify is the one
# name taken back out of DEVELOPER_DENIED_TOOLS (CTO_DENIED_TOOLS below);
# tests pin that it is the only one.
CTO_EXTRA_TOOLS = frozenset(
    {
        # PR review verdicts: review a task and send the verdict to the
        # operator through the hub's internal review_pipeline route. The
        # bare notify tool stays denied.
        "review_and_notify",
        # PR review queue: tasks waiting for review or plan approval.
        "scan_for_reviewable_tasks",
        # Security quality bar: audit and file triage tasks; triage
        # prompt-injection findings.
        "audit_triage_tasks",
        "injection_triage",
        # Integrity of the evidence chain and replicated state.
        "event_log_verify",
        "state_sync_verify",
        # Platform health and capacity: MCP telemetry and model quota.
        "mcp_health_telemetry",
        "model_quota_status",
        # Engineering standards: the shared skills registry.
        "skill_registry",
    }
)
# The CTO's privilege boundary: the developer deny list less its grants.
CTO_DENIED_TOOLS = DEVELOPER_DENIED_TOOLS - CTO_EXTRA_TOOLS
STEWARD_DENIED_TOOLS = DEVELOPER_DENIED_TOOLS | frozenset(
    {
        "delegate",
        "delegate_claim",
        "delegate_release",
        "delegate_cancel",
        "fs_write",
        "git_worktree_add",
        "git_worktree_remove",
        "memory_delete",
        "memory_restore",
        "plan_delete",
        "plan_handoff",
        "plan_manage",
        "plan_step_retry",
        "plan_update",
        "task_claim",
        "task_delete",
        "vault_to_memory_ingest",
        "workflow_run",
        "export_data",
        "delegation_manage",
        "coordination_manage",
        "autonomous_cycle_manage",
        "bug_worker_manage",
        "gcloud_ops",
    }
)
# task_update is deliberately NOT here (task #591): a steward pass that files
# findings but can never close, reparent, re-prioritize, or annotate them is a
# strictly additive writer to the queue. The tool itself is exposed; the real
# boundary — never clear another agent's claim (CLAUDE.md "MCP task_update
# Caveat") — is enforced inside tools.tasks.task_update by comparing the
# caller's identity against the task's claimed_by, not by denying the tool.
# task_claim stays denied: claiming work to execute it is developer territory.

# ---------------------------------------------------------------------------
# The research profile is ALLOW-list based, and deliberately unlike the two
# above it.
#
# Developer and steward are deny lists: a newly registered tool is reachable
# until someone remembers to deny it. That default is defensible for a trusted
# local IDE client and indefensible for anything reachable from off-host --
# it is why no profile denied ``run_cmd`` (task #498). Research inverts the
# default: a tool is unreachable until it appears here, so adding a tool to the
# server can never widen this profile by accident.
#
# The surface is scoped to reading, researching, and planning: orient in a repo,
# search the web, keep memory, file and read tasks, drive plans and dependency
# graphs, and write notes into the vault's inbox/log dirs. It executes nothing.
# Deliberately absent, and the reasons why:
#   run_cmd / run_cmd_check / pi_run_task / gemini_ask - arbitrary execution.
#   fs_write / python_lint_and_format / fs_index_symbols - mutate the workspace.
#   git_commit_staged / git_merge_task / git_worktree_{add,remove} - mutate history.
#   task_update / task_claim - both run ``git worktree add`` as a side effect
#       (tools/tasks.py:252), so "grooming a task" writes to disk and shells out.
#   profile_activate - a research session must not be able to escalate itself.
#   delegate* / workflow_run / autonomous_cycle_* - start work on other agents.
#   notify / batch_notify / *_and_notify - reach outside the host.
#   memory_delete / task_delete / vault_move / vault_archive_plan - destructive.
#   export_* / import_* / rollback_* - bulk data egress and migration.
RESEARCH_ALLOWED_TOOLS = frozenset(
    {
        # Read-only orientation in the workspace.
        "fs_read",
        "fs_list",
        "fs_search",
        "fs_repo_map",
        "fs_list_symbols",
        "fs_search_symbols",
        "read_image",
        "git_worktree_list",
        "git_diff_staged",
        # Research.
        "web_search",
        "web_fetch",
        # Durable memory. Delete/restore stay out.
        "memory_save",
        "memory_search",
        "memory_get",
        "memory_list",
        "memory_feedback",
        "memory_prune_candidates",
        "recall_maintenance",
        # Planning. task_add files work; claiming and executing it does not
        # belong to this profile.
        "task_add",
        "task_list",
        "plan_manage",
        "coordination_manage",
        # Vault. Writes are additionally confined to RESEARCH_WRITE_DIRS by
        # ``vault_write_dirs`` below, and still pass the normal safety checks.
        "vault_read",
        "vault_search",
        "vault_query",
        "vault_list",
        "vault_links",
        "vault_journal",
        "vault_write",
        "vault_briefing_digest",
        "vault_ingest_status",
        "obsidian_status",
        "obsidian_active_note",
        "obsidian_search",
        # Situational awareness, all read-only.
        "dash_list",
        "dash_search",
        "dash_view",
        "event_log_query",
        "event_log_verify",
        "check_agent_capability",
        "dispatch_delivery_receipt",
        "capability_catalog",
        # Session hygiene and presentation. profile_reset / session_end are
        # deliberately absent: research cannot escape itself via tools;
        # session end / reconnect returns to the identity default.
        "session_start",
        "session_list",
        "session_packet",
        "lifecycle_record",
        "message_send",
        "message_list",
        "profile_status",
        "format_render",
    }
)

# Web UI human operator: approvals plus read-only status. No vault writes, no
# git, no task mutation — least privilege for the control-center bearer.
#
# crew_view is read-only and joins that status surface (task #1151, under
# the operator's 2026-09-19 crew-view authorization): the human watches a live
# session the same way a dev agent does. crew_intervene stays out — the
# operator intervenes through the CSRF-gated POST /api/action/crew-intervene,
# which records the authenticated web identity; a read-only MCP view does not
# need that gate.
OPERATOR_ALLOWED_TOOLS = frozenset(
    {
        "approval_pending",
        "approval_resolve",
        "profile_status",
        "format_render",
        "event_log_query",
        "event_log_verify",
        "task_list",
        "memory_search",
        "memory_get",
        "memory_list",
        "session_list",
        "check_agent_capability",
        "dispatch_delivery_receipt",
        "dispatch_retry_cancellation",
        "dispatch_confirm_cancellation",
        "crew_view",
    }
)

# ---------------------------------------------------------------------------
# The public profile (task #859) is the tightest allow-list: the surface a
# client reachable from the public internet gets. It is what the OAuth front
# door issues for ``server_agent8`` (the Grok Bot connector). Two capabilities
# only:
#   * read the status of tasks and projects (task_list, dash_* views);
#   * file a proposal for a human to approve (task_add).
# format_render and profile_status are utilities with no data access.
#
# Deliberately absent, beyond everything research already excludes:
#   memory_* / vault_* / web_* - no knowledge-base or outbound read access.
#   event_log_* / session_list / capability_catalog / agent_* - fleet-wide
#       information disclosure; "status of my tasks" does not need them.
#   approval_pending / approval_resolve - the human approves, the bot proposes.
#   message_send / message_list - no cross-agent messaging.
#   profile_activate / profile_reset / session_* - cannot escape this profile.
# No consolidated managers: PUBLIC_ALLOWED_ACTIONS is empty, so plan_manage /
# coordination_manage side doors stay shut.
PUBLIC_ALLOWED_TOOLS = frozenset(
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
PUBLIC_ALLOWED_ACTIONS: frozenset[str] = frozenset()

# Consolidated managers can invoke legacy aliases that are hidden from
# discovery, so an allow-list profile must name the actions it permits as well
# as the managers. Without this, allowing ``plan_manage`` would silently grant
# every plan_* alias including plan_delete and plan_handoff.
RESEARCH_ALLOWED_ACTIONS = frozenset(
    {
        "plan_create",
        "plan_update",
        "plan_get",
        "plan_list",
        "plan_checkpoint",
        "plan_resume",
        "plan_preview",
        # Task #2480: plan structure edits follow plan_update's exposure exactly.
        "plan_step_add",
        "plan_step_edit",
        "plan_step_remove",
        "plan_step_reorder",
        "plan_edit",
        "dependency_list",
        "dependency_add",
        "dependency_remove",
        "handoff_list",
        "lineage",
        # Task #1425: reads only -- binding/unbinding a project's repo stays
        # developer/steward territory, mirroring the write/read split above.
        "project_repo_get",
        "project_repo_list",
        # Task #1747: hold reads only; holding a subtree needs task_update.
        "task_holds",
        "task_hold_get",
    }
)

# Authenticated HTTP developer sessions use the consolidated plan entry point.
# The named tools stay available in unauthenticated/stdio compatibility mode.
LEGACY_PLAN_TOOLS = frozenset(
    {
        "plan_create",
        "plan_update",
        "plan_get",
        "plan_list",
        "plan_checkpoint",
        "plan_resume",
        "plan_preview",
        "plan_review",
        "plan_step_retry",
        "plan_step_claim",
        "plan_handoff",
        "plan_delete",
    }
)
LEGACY_EXPORT_TOOLS = frozenset(
    {
        "export_tasks",
        "export_memory",
        "export_journal",
        "export_migration_manifest",
        "compare_migration_manifests",
        "import_migration_manifest",
        "rollback_migration_import",
    }
)
LEGACY_DELEGATION_TOOLS = frozenset(
    {
        "delegate",
        "delegate_list",
        "delegate_claim",
        "delegate_release",
        "delegation_ledger",
        "delegation_spec_check",
        "delegation_spec_template",
        "delegation_spec_build",
    }
)
LEGACY_COORDINATION_TOOLS = frozenset(
    {
        "dependency_list",
        "dependency_add",
        "dependency_remove",
        "handoff",
        "handoff_list",
        # Task subtree holds (task #1747): actions only, never top-level tools.
        "task_hold",
        "task_holds",
        "task_hold_get",
        # Team snapshot and restore (task #1738): coordination_manage actions only,
        # named here so the HTTP route twins can declare the same action ceiling.
        "team_snapshot",
        "team_snapshots",
        "team_restore_preview",
        "team_restore",
        # Approval Ledger re-request (task #1854): a coordination_manage action,
        # named so its HTTP route twin declares the same action ceiling.
        "approval_ledger_rerequest",
        # Approval Ledger poll-now (task #1867): same shape as the re-request.
        "approval_ledger_poll",
        # Device acceptance (task #2112): coordination_manage actions only, named
        # so the HTTP route twin can declare the same action ceiling.
        "node_acceptance_start",
        "node_acceptance_evaluate",
        "node_acceptance_status",
        "node_acceptance_attest",
        # Node retire (task #2181): coordination_manage actions only, named so the
        # HTTP route twin (task #2208) can declare the same action ceiling.
        "node_retire",
        "node_unretire",
        # Work proposals (task #2393): coordination_manage actions only, named so
        # the decide route's HTTP twin can declare the same action ceiling.
        "work_proposal_file",
        "work_proposal_decide",
        "work_proposal_withdraw",
        "work_proposal_get",
        "work_proposal_list",
        "work_proposal_recover",
    }
)
LEGACY_CYCLE_TOOLS = frozenset(
    {
        "autonomous_cycle_start",
        "autonomous_cycle_get",
        "autonomous_cycle_transition",
        "autonomous_cycle_record_usage",
        "autonomous_cycle_list",
        "autonomous_cycle_kill",
    }
)
LEGACY_BUG_WORKER_TOOLS = frozenset(
    {
        "bug_worker_discover",
        "bug_worker_run_once",
        "bug_worker_status",
        "bug_worker_check_eligibility",
    }
)
LEGACY_OBSERVABILITY_TOOLS = frozenset(
    {
        "agent_activity",
        "metrics_snapshot",
        "check_anomalies",
        "perf_summary",
        "check_anomalies_and_notify",
        "agent_heartbeat",
        "agent_list",
        "agent_context",
    }
)
CONSOLIDATED_LEGACY_TOOLS = frozenset(
    LEGACY_PLAN_TOOLS
    | LEGACY_EXPORT_TOOLS
    | LEGACY_DELEGATION_TOOLS
    | LEGACY_COORDINATION_TOOLS
    | LEGACY_CYCLE_TOOLS
    | LEGACY_BUG_WORKER_TOOLS
    | LEGACY_OBSERVABILITY_TOOLS
)

# Default-profile shrink (task #1230 / Epic B, B2). The developer profile is
# the default every engineering identity lands on; this set removes names
# from it to bring the exposed surface under 100 tools, with a stated path to
# <=60 by day 90 — the set grows as the deprecated-call log shows zero real
# consumers per name. Removal is by profile only: every name stays
# registered, remains reachable under an explicit profile (steward and
# chief-of-staff are untouched), and stays callable in-process; a call to one
# of these names under the default profile is denied AND logged by
# server.py's policy_guarded so real consumers surface instead of failing
# silently. Cut basis: the task #1121 consumer-disposition table (PARK'd
# capabilities with no verified consumer) plus operator/admin surfaces that
# were never agent-facing.
DEFAULT_PROFILE_PRUNED_TOOLS = frozenset(
    {
        # Disposable acceptance surface: explicit steward/COS sessions only.
        "synthetic_workflow",
        # Retired transport — the hub forces UA_ZULIP_RETIRED=1.
        "zulip_fetch_messages",
        "zulip_send_message",
        # Horizon2 connector registry — PARK'd by the #1121 disposition
        # table (zero consumers; the pilot is scoped to #1032). Only the two
        # OMITTED read/list surfaces leave the default profile: the register/
        # authorize/reverse verbs are WITHHELD-class "reserved for developer
        # lanes" (#1200 table) and the connector conformance harness pins
        # them reachable under developer/steward/chief-of-staff — verified
        # consumers per B2's "no behavior change" rule.
        "horizon2_package_list",
        "horizon2_server_list",
        # Browser projection is contract-first with a pure derive wrapper,
        # but no authenticated producer client is verified. Keep the tool
        # out of the developer/default profile; the Control Center canvas
        # uses /api/graph-canvas, not this MCP surface.
        "browser_derive_task_reference",
        "graph_canvas_read",
        "graph_canvas_write",
        # Operator/admin provisioning — credential pool, managed secrets,
        # and the token lifecycle are human/operator operations, not agent
        # calls (WITHHELD from operator clients per tool_classification).
        "agent_token_issue",
        "agent_token_list",
        "agent_token_rotate",
        "deployment_claim",
        "oauth_device_manage",
        "credential_pool_list",
        "credential_pool_register",
        "credential_pool_revoke",
        "managed_secret_set",
        # Notify family — the developer profile already denies notify,
        # review_and_notify, and check_anomalies_and_notify.
        "batch_notify",
        "notify_delivery_status",
        # Public-reply relay (task #1734): outbound speech on external channels,
        # like the notify family. No verified developer-lane consumer yet;
        # reachable under steward and chief-of-staff, and every send still
        # needs a human approval.
        "relay_manage",
        # Internal/maintenance surfaces — consumed in-process (workflows,
        # the reviewer pipeline, schedulers), not by MCP clients.
        # event_log_verify / gcloud_inventory / lifecycle_record are
        # OMITTED-class per #1200 (operator-client surfaces, not developer
        # tools): audit-chain verification has the unified-agent-audit-ledger
        # CLI twin, GCP inventory is an operator pane source, and lifecycle
        # records are written in-process.
        "audit_triage_tasks",
        "event_log_verify",
        "gcloud_inventory",
        "lifecycle_record",
        "autonomous_cycle_green_path_eligibility",
        "cleanup_stale_worktrees",
        "cloud_worker_recover",
        "flush_digest",
        "injection_triage",
        "red_team_qa",
        "scan_for_reviewable_tasks",
        "state_sync_verify",
        # Steward maintenance surfaces (steward already denies
        # vault_to_memory_ingest and the destructive vault movers).
        # vault_wiki_custodian is NOT pruned (task #1957): steward mode runs
        # on the developer default (profile_activate is hidden there, #869)
        # and the steward lint procedure starts from
        # vault_wiki_custodian(dry_run=True), so it has a verified consumer.
        "vault_to_memory_ingest",
        # Swapped out for vault_wiki_custodian to hold the 100-tool developer
        # ceiling (task #1957): obsidian_open drives the Obsidian UI on the
        # hub host, a side effect no remote developer session consumes (no
        # skill, workflow or procedure calls it). Stays registered and
        # reachable under steward and chief-of-staff.
        "obsidian_open",
        # Skill-clustering proposal surface — no verified consumer.
        "propose_skill_clusters",
        # Operator telemetry and quota status, swapped out of the default
        # profile so goal_manage and task_comment (#1349, #1423) fit the
        # 100-tool developer ceiling. Both stay registered and reachable
        # under chief-of-staff. Dispatch still reads the quota projection
        # in-process; it does not call the MCP tool.
        "mcp_health_telemetry",
        "model_quota_status",
        # Work-product submit (task #1278, H2 non-code wedge) always parks a
        # require_approval request its submitter can never approve
        # (approvals.SEPARATION_OF_DUTIES_TOOLS); off by UA_WORK_PRODUCT_ENABLED
        # default besides.
        # Reachable under steward/chief-of-staff, same treatment as agent_token_*.
        "work_product_submit",
        # Skills registry (task #1353): swapped out of the default profile for
        # the same reason as goal_manage/task_comment above — headroom under
        # the 100-tool developer ceiling. Stays registered and reachable under
        # steward and chief-of-staff.
        "skill_registry",
        # Policy authorizations (task #1798): the list surface is an
        # OMITTED-class audit/operator read — the authorizer reviews its
        # records under steward or chief-of-staff — so only the record verb
        # joins the developer profile. Swapped out together with
        # dispatch_delivery_receipts_list to keep the 100-tool ceiling: that
        # read's verified consumer is the Control Center
        # /api/dispatch_receipts pane, which reaches it through the route's
        # declared authority, not the developer profile (OMITTED-class per
        # the #1200 table); a dispatched worker posts receipts via
        # dispatch_delivery_receipt, which stays.
        "policy_authorization_list",
        "dispatch_delivery_receipts_list",
        # MCP gateway (task #1336): calls downstream MCP servers with hub-held
        # credentials. Kept off the 100-tool developer default; steward and
        # chief-of-staff reach it, and so does the connectors toolset.
        "mcp_gateway",
    }
)


@dataclass(frozen=True)
class ActiveProfile:
    name: str
    identity: str
    session_id: str


_active: dict[str, ActiveProfile] = {}
_lock = threading.Lock()


def _authenticated_context() -> tuple[str, str]:
    identity = config.verified_agent_identity()
    session_id = _current_session_id()
    if not config.enforce_auth() or not identity or not session_id:
        raise ToolError(
            "profile activation requires an authenticated HTTP MCP session "
            "(UA_ENFORCE_AUTH=1 and a valid bearer token)"
        )
    return identity, session_id


def _current_session_id() -> str | None:
    """Get the hub-bound session id inside an MCP tool invocation.

    Streamable HTTP starts the MCP handler in a task after ASGI middleware has
    returned.  mcp 2.x has no ambient request_ctx; the session id is only the
    ContextVar set by HubAuthASGIMiddleware (unset for stdio/in-process).
    """
    session_id = config.current_mcp_session_id()
    if session_id:
        return session_id
    return None


def profiles_by_identity() -> dict[str, str]:
    """Code default profile per identity, including this deployment's operator."""
    return {**DEFAULT_PROFILES_BY_IDENTITY, config.operator_label(): OPERATOR}


def default_for_identity(identity: str | None) -> str:
    """Return the least-privilege profile default for a verified identity.

    A ``profile_defaults`` overlay in the policy file may repeat this default
    or reduce its privilege. Anything else is ignored so a hand edit cannot
    escalate an identity. The code map remains the authority when the overlay
    is absent or unreadable.
    """
    code = profiles_by_identity().get(identity or "", DEVELOPER)
    if not identity:
        return code
    code = _seat_profile(identity, code)
    try:
        from .config_revisions import applied_profile_overlay

        chosen = applied_profile_overlay(identity, code)
    except Exception:
        return code
    return chosen if chosen else code


def overlay_ceiling_allows(identity: str, name: str) -> bool:
    """Whether ``name`` stays inside a committed profile downgrade.

    No overlay, or an overlay that repeats the code default, leaves activation
    as it was. A real downgrade is a ceiling: activating back to chief-of-staff
    would undo it.
    """
    code = profiles_by_identity().get(identity, DEVELOPER)
    try:
        from .config_revisions import _overlay_is_allowed, applied_profile_overlay
    except Exception:
        return True
    chosen = applied_profile_overlay(identity, code)
    if not chosen or chosen == code:
        return True
    return _overlay_is_allowed(name, chosen)


#: Profiles a seat (``unified-agent agent add --profile``) may never name: the
#: human's own profile and the hub-bound session profile.
_UNSEATABLE_PROFILES = frozenset({OPERATOR, ACP_SESSION})


def seat_profile_problem(identity: str, name: str) -> str | None:
    """Why ``name`` cannot be ``identity``'s seat profile, or None (task #2453).

    Like a policy overlay, a seat may repeat or lower the code default and never
    raise it, so adding an agent cannot mint a more privileged identity than
    the code map already grants that label.
    """
    if name not in VALID_PROFILES or name in _UNSEATABLE_PROFILES:
        seatable = sorted(VALID_PROFILES - _UNSEATABLE_PROFILES)
        return f"unknown or unseatable profile {name!r}; choose from {', '.join(seatable)}"
    code = profiles_by_identity().get(identity, DEVELOPER)
    if _profile_privilege_rank(name) < _profile_privilege_rank(code):
        return (
            f"profile {name!r} is more privileged than {identity!r}'s code default "
            f"{code!r}; a seat may only keep or lower it"
        )
    return None


def _seat_profile(identity: str, code: str) -> str:
    try:
        from . import agent_roster

        seat = agent_roster.seat(identity)
    except Exception:  # noqa: BLE001 - the code map stays the authority
        return code
    name = seat.get("profile") if seat else None
    if isinstance(name, str) and seat_profile_problem(identity, name) is None:
        return name
    return code


# High privilege to low. Steward is below developer because
# STEWARD_DENIED_TOOLS is a superset of DEVELOPER_DENIED_TOOLS.
_PROFILE_PRIVILEGE_HIGH_TO_LOW = (
    CHIEF_OF_STAFF,
    CTO,
    DEVELOPER,
    STEWARD,
    OPERATOR,
    RESEARCH,
    CLOUD_VAULT_STEWARD,
    CLOUD_ASSISTANT,
    PUBLIC,
    ACP_SESSION,
)


def _profile_privilege_rank(name: str) -> int:
    """Index in the privilege order. A lower index is more privileged."""
    return _PROFILE_PRIVILEGE_HIGH_TO_LOW.index(name)


def _profile_ceiling_identity(identity: str) -> str:
    """Fleet label that owns this caller's profile default.

    The operator's claimed name is the same person as their login label. The
    alias must not pick up the developer default and then activate
    chief-of-staff.
    """
    for canonical, aliases in config.identity_alias_allowlist().items():
        if identity in aliases:
            return canonical
    return identity


def activate(name: str) -> ActiveProfile:
    """Activate a fixed profile for the verified caller's current MCP session."""
    if current_session_grant() is not None:
        # A session credential is pinned to its grant for its whole life.
        raise ToolError("a session credential cannot switch profiles")
    if name == ACP_SESSION:
        raise ToolError(f"the {ACP_SESSION} profile is bound by the hub and cannot be activated")
    if name not in VALID_PROFILES:
        raise ToolError(
            f"unknown profile {name!r}; choose from {', '.join(sorted(VALID_PROFILES))}"
        )
    identity, session_id = _authenticated_context()
    if (
        config.is_cloud_hosted_identity(identity)
        and name != default_for_identity(identity)
        and name not in _NON_ESCALATING_PROFILES
    ):
        # A cloud-hosted identity may only stay on its default or step down to
        # an allow-list profile it cannot climb out of. Developer, steward and
        # operator are lateral grants with their own semantics (workspace
        # writes, the operator's approval surface); none is a role this
        # identity was given, whatever the privilege order says (task #1799).
        raise ToolError(
            f"{identity} is a cloud-hosted identity and cannot activate {name!r}; "
            "it may only keep its default or downgrade to "
            f"{', '.join(sorted(_NON_ESCALATING_PROFILES))}"
        )
    if not overlay_ceiling_allows(identity, name):
        raise ToolError(
            f"profile overlay for {identity} does not allow activating {name!r}; "
            "roll back the overlay to restore a higher profile"
        )
    active = current()
    if active in _NON_ESCALATING_PROFILES and name != active:
        # profile_activate is already outside these profiles' allow-lists, so an
        # MCP caller cannot reach this. The guard covers in-process and stdio
        # callers, which bypass the tool-exposure layer entirely. These profiles
        # also cannot call profile_reset; ending the session / reconnecting
        # returns to the identity's default.
        raise ToolError(
            f"the {active} profile cannot activate another profile; "
            "end the session or reconnect to return to this identity's default"
        )
    ceiling = default_for_identity(_profile_ceiling_identity(identity))
    if _profile_privilege_rank(name) < _profile_privilege_rank(ceiling):
        if config.actor_kind_for_identity(identity) != "human":
            raise ToolError(
                f"{identity} cannot activate {name!r}; a non-human caller may not "
                f"activate a profile more privileged than its default {ceiling!r}"
            )
        if ceiling == OPERATOR:
            raise ToolError(
                f"{identity} cannot activate {name!r}; the operator profile "
                "cannot gain a more privileged tool set"
            )
    profile = ActiveProfile(name=name, identity=identity, session_id=session_id)
    with _lock:
        _active[session_id] = profile
    return profile


def reset() -> str:
    """Return the current authenticated session to its identity default."""
    if current_session_grant() is not None:
        raise ToolError("a session credential cannot switch profiles")
    identity, session_id = _authenticated_context()
    with _lock:
        _active.pop(session_id, None)
    return default_for_identity(identity)


def current() -> str:
    """The active profile for this request; never trusts a client-provided label."""
    if current_session_grant() is not None:
        return ACP_SESSION
    identity = config.verified_agent_identity()
    session_id = _current_session_id()
    if not identity or not session_id:
        return default_for_identity(identity)
    with _lock:
        profile = _active.get(session_id)
    if profile is None or profile.identity != identity:
        return default_for_identity(identity)
    return profile.name


def within_configured_write_dirs(scope: tuple[str, ...]) -> tuple[str, ...]:
    """Narrow a fixed profile scope to the installation's UA_VAULT_WRITE_DIRS.

    A configured parent (or the same directory) grants the scope directory; a
    configured subdirectory of it grants only that subdirectory. A profile scope
    can therefore narrow the installation baseline but never widen it.
    """
    allowed: list[str] = []
    for configured in config.vault_write_dirs():
        configured_path = PurePosixPath(configured)
        if ".." in configured_path.parts:
            continue
        for directory in scope:
            if PurePosixPath(directory).is_relative_to(configured_path):
                allowed.append(directory)
            elif configured_path.is_relative_to(directory):
                allowed.append(configured_path.as_posix())
    return tuple(dict.fromkeys(allowed))


def cloud_hosted_caller() -> bool:
    """Whether this request needs the cloud vault guards.

    True for a cloud profile (whoever holds it) and for a cloud-hosted
    identity (``config.CLOUD_HOSTED_IDENTITIES``) under any profile. The guards
    key on both so that moving a cloud-hosted identity off
    ``cloud-vault-steward`` (task #1799) cannot silently drop the
    lock, force, journal and write-dir checks from #968/#1794.
    """
    return current() in CLOUD_PROFILES or config.is_cloud_hosted_identity(
        config.verified_agent_identity()
    )


def cloud_hosted_identity_default(identity: str | None) -> bool:
    """Whether an identity is cloud-hosted by roster or by its default profile.

    For request planes that see only the verified label (HTTP routes, browser
    session creation): either signal keeps the caller on its MCP surface.
    """
    return config.is_cloud_hosted_identity(identity) or (
        default_for_identity(identity) in CLOUD_PROFILES
    )


def vault_write_dirs() -> tuple[str, ...]:
    """Profile-sensitive vault scope, intersected with normal safety checks later.

    A cloud-hosted identity gets its profile's scope like any other caller: on
    chief-of-staff that includes the PARA folders, so ``cloud_agent13`` can file
    notes as the chief of staff. ``cloud_agent8`` is research and keeps that
    profile's inbox and logs. ``cloud_agent17`` is a developer (2026-11-11) and
    keeps that profile's write dirs. Cloud write guards still apply on every
    write in ``safety.resolve_in_vault``
    and ``tools/vault.py``: the strict lock check, ``force`` refusal, and the
    journal date, tag and target bounds. The downgrade profiles narrow further.
    """
    if current() == CHIEF_OF_STAFF:
        return COS_WRITE_DIRS
    if current() == CLOUD_ASSISTANT:
        return ()
    if current() == CLOUD_VAULT_STEWARD:
        # Journaling has a separate append-only exception in the resolver;
        # generic vault_write must never gain Logs access.
        return within_configured_write_dirs(("Wiki",))
    if current() == RESEARCH:
        # Planning output lands in the inbox for human filing; the Wiki layer
        # stays curated by steward and chief-of-staff.
        return RESEARCH_WRITE_DIRS
    if current() == OPERATOR:
        # Operator is approvals + read-only; no vault writes, except the verified
        # human operator inside a CC2 vault-write route (PR #1507 review).
        authority = _current_http_route_authority()
        if authority is not None and authority.operator_vault_write:
            return tuple(config.vault_write_dirs())
        return OPERATOR_WRITE_DIRS
    if current() == PUBLIC:
        # Public front door: status reads and task_add proposals only.
        return PUBLIC_WRITE_DIRS
    if current() == ACP_SESSION:
        # A session credential has no vault tool; fail closed regardless.
        return ()
    # Preserve the existing administrator-configured baseline for developer and
    # steward; the default remains Inbox/Logs/Wiki.
    return tuple(config.vault_write_dirs())


def exposure_is_active() -> bool:
    """Whether server-side profile exposure applies to this request."""
    # A session id scopes an overlay, not the verified identity's ceiling.
    # Losing session context must not select the legacy full-surface path.
    return bool(config.enforce_auth() and config.verified_agent_identity())


def _allowed_tools_for_profile(
    profile: str, tool_names: set[str] | frozenset[str]
) -> frozenset[str]:
    """Tool subset exposed to a *named* profile; no ambient request context.

    Adapters that resolve their own profile can evaluate the same allow/deny
    sets without depending on ambient identity or session context. The two
    entry points cannot drift because ``allowed_tools`` delegates here.
    """
    if profile == CHIEF_OF_STAFF:
        return frozenset(tool_names)
    if profile == CLOUD_ASSISTANT:
        return frozenset(tool_names) & CLOUD_ASSISTANT_ALLOWED_TOOLS
    if profile == CLOUD_VAULT_STEWARD:
        return frozenset(tool_names) & CLOUD_VAULT_STEWARD_ALLOWED_TOOLS
    if profile == RESEARCH:
        # Intersection, not subtraction: tools this profile does not name are
        # unreachable, including ones registered after it was written.
        return frozenset(tool_names) & RESEARCH_ALLOWED_TOOLS
    if profile == OPERATOR:
        return frozenset(tool_names) & OPERATOR_ALLOWED_TOOLS
    if profile == PUBLIC:
        return frozenset(tool_names) & PUBLIC_ALLOWED_TOOLS
    if profile == ACP_SESSION:
        return frozenset(tool_names) & ACP_SESSION_ALLOWED_TOOLS
    denied = STEWARD_DENIED_TOOLS if profile == STEWARD else DEVELOPER_DENIED_TOOLS
    if profile in {DEVELOPER, CTO}:
        denied = denied | LEGACY_PLAN_TOOLS | DEFAULT_PROFILE_PRUNED_TOOLS
    elif profile == STEWARD:
        denied = denied | LEGACY_PLAN_TOOLS
    if profile in {DEVELOPER, STEWARD, CTO}:
        denied = denied | LEGACY_EXPORT_TOOLS
        denied = denied | LEGACY_DELEGATION_TOOLS
        denied = denied | LEGACY_COORDINATION_TOOLS
        denied = denied | LEGACY_CYCLE_TOOLS
        denied = denied | LEGACY_BUG_WORKER_TOOLS
        denied = denied | LEGACY_OBSERVABILITY_TOOLS
    if profile == CTO:
        denied = denied - CTO_EXTRA_TOOLS
    return frozenset(tool_names - denied)


def allowed_tools(tool_names: set[str] | frozenset[str]) -> frozenset[str]:
    """Return the tools exposed to the current authenticated profile."""
    grant = current_session_grant()
    if grant is not None:
        # Checked before exposure_is_active: a session credential is narrowed
        # even on a hub in warn mode (UA_ENFORCE_AUTH unset).
        return frozenset(tool_names) & grant.tools
    if not exposure_is_active():
        # stdio / in-process / unauthenticated compatibility: the ambient
        # profile machinery does not apply, so everything stays reachable.
        return frozenset(tool_names)
    if not _current_session_id() and _current_http_route_authority() is None:
        logger.warning(
            "authenticated profile request has no MCP session id; using identity default ceiling"
        )
    return _allowed_tools_for_profile(current(), tool_names)


def tool_is_allowed_for(profile: str, tool_name: str) -> bool:
    """Whether ``tool_name`` is exposed to a named profile, without the
    ambient-context short-circuit.

    Request planes such as the HTTP adapter over the platform object model
    use this pure check with the profile they resolved, independently of the
    ambient context used by MCP handlers.
    """
    return tool_name in _allowed_tools_for_profile(profile, {tool_name})


def tool_is_allowed(tool_name: str) -> bool:
    """Whether the active profile exposes ``tool_name``.

    MCP calls obey the discovery ceiling, including without a session id.
    A checked HTTP route may instead carry its exact declared call authority;
    this never widens MCP discovery or calls outside that handler. Call policy
    still runs in ``safety.check_policy_gate``, not this function.
    """
    if current_session_grant() is not None:
        # No route authority can widen a session credential's grant.
        return tool_name in allowed_tools({tool_name})
    authority = _current_http_route_authority()
    if authority is not None and tool_name in authority.tools:
        return True
    return tool_name in allowed_tools({tool_name})


def consolidated_action_is_allowed(manager: str, action_tool: str) -> bool:
    """Authorize an action called through a consolidated compatibility tool.

    Authenticated profiles hide legacy aliases from discovery and direct calls,
    but the consolidated entry point must still be able to invoke the same
    underlying operation. Explicit high-impact denies remain effective.
    """
    if current_session_grant() is not None:
        # No consolidated manager is in the session grant; refuse outright.
        return False
    authority = _current_http_route_authority()
    if authority is not None and (manager, action_tool) in authority.actions:
        return True
    if not tool_is_allowed(manager):
        return False
    if current() in CLOUD_PROFILES:
        return manager == "observability_manage" and action_tool in CLOUD_ASSISTANT_ALLOWED_ACTIONS
    if current() == RESEARCH:
        # Allow-list profiles answer from their own action set. Falling through
        # to the deny-list branch below would treat "not denied to a steward"
        # as "allowed", re-opening exactly what the allow-list closed.
        return action_tool in RESEARCH_ALLOWED_ACTIONS
    if current() == PUBLIC:
        # PUBLIC_ALLOWED_ACTIONS is empty and no manager is on the allow-list,
        # but answer from the action set for the same reason research does.
        return action_tool in PUBLIC_ALLOWED_ACTIONS
    if current() == OPERATOR:
        # Operator has no consolidated managers on its allow-list.
        return False
    if tool_is_allowed(action_tool):
        return True
    if action_tool not in CONSOLIDATED_LEGACY_TOOLS:
        return False

    # ``allowed_tools`` hides legacy aliases from discovery for authenticated
    # profiles, but the consolidated manager must still expose aliases that
    # the active profile is allowed to call.  The old union check rejected
    # delegate_claim/release for developers because those actions are denied
    # only to stewards.
    profile = current()
    if profile == CTO:
        denied = CTO_DENIED_TOOLS
    elif profile == DEVELOPER:
        denied = DEVELOPER_DENIED_TOOLS
    else:
        denied = STEWARD_DENIED_TOOLS
    return action_tool not in denied


def consolidated_action_is_allowed_for(profile: str, manager: str, action_tool: str) -> bool:
    """Pure profile check for one action exposed through a consolidated tool.

    HTTP adapters resolve a profile without an active MCP session, so they
    must not use ``tool_is_allowed``'s ambient-context shortcut here.
    """
    if not tool_is_allowed_for(profile, manager):
        return False
    if profile in CLOUD_PROFILES:
        return manager == "observability_manage" and action_tool in CLOUD_ASSISTANT_ALLOWED_ACTIONS
    if profile == RESEARCH:
        return action_tool in RESEARCH_ALLOWED_ACTIONS
    if profile == PUBLIC:
        return action_tool in PUBLIC_ALLOWED_ACTIONS
    if profile in {OPERATOR, ACP_SESSION}:
        return False
    if tool_is_allowed_for(profile, action_tool):
        return True
    if action_tool not in CONSOLIDATED_LEGACY_TOOLS:
        return False
    if profile == STEWARD:
        denied = STEWARD_DENIED_TOOLS
    elif profile == CTO:
        denied = CTO_DENIED_TOOLS
    else:
        denied = DEVELOPER_DENIED_TOOLS
    return action_tool not in denied
