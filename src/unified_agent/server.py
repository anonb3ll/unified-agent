"""unified-agent: an agent-operations platform whose MCP adapter serves every registered IDE, with HTTP/JSON, CLI, Web UI, and SDK surfaces over the same core."""

import functools
import inspect
import logging
import os
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp.server.mcpserver import Context, MCPServer

# Harden every tool's argument model against unknown keywords. mcpserver builds a
# pydantic model per tool from the function signature (base: ArgModelBase), and
# pydantic's default extra="ignore" means a client that sends a keyword the tool
# doesn't declare has it silently dropped — the call then runs with defaults.
# On 2026-09-08 `vault_write(..., mode="append")` lost `mode`, `append` defaulted
# False, and the call overwrote 6 Wiki pages (task #854; earlier near-miss
# 2026-08-20). extra="forbid" turns that into a validation error the caller sees.
# skip_names already excludes Context and resolver-injected params, so this only
# affects genuinely caller-supplied arguments.
from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase as _ArgModelBase
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    PaginatedRequestParams,
    ToolAnnotations,
)

from . import (
    agent_access,
    components,
    config,
    edition,
    elicitation,
    gateway,
    hub_runtime,
    mcp_live_state,
    post_init,
    registry,
    safety,
    source_host,
    storage,
    tailnet,
    task_comments,
    tool_errors,
    tool_surface,
    toolsets,
    trust_presets,
)
from . import profiles as operational_profiles
from .tools import (
    approvals,
    delegate,
    tasks,
)
from .tools import (
    profiles as profile_tools,
)

# Components outside the kernel and the adapter are loaded only when
# UA_COMPONENTS selects them (K4b, task #2603), so their modules are imported
# at the call site that needs them, inside an `components.enabled(...)` guard.
# A hub started with `UA_COMPONENTS=kernel` has no auth, control-center,
# cloud, telemetry, dispatch or dev-tools modules to call, and every site
# below takes its documented fallback instead. The imports stay literal (never
# `importlib.import_module`) so the static readers follow them.

_ArgModelBase.model_config["extra"] = "forbid"

logger = logging.getLogger(__name__)


def _oauth() -> Any:
    """The OAuth provider module, or None when the auth component is not loaded."""
    if not components.enabled("auth"):
        return None
    from . import oauth

    return oauth


def _workos_hub_auth() -> Any:
    """The hub WorkOS leg, or None when the auth component is not loaded."""
    if not components.enabled("auth"):
        return None
    from . import workos_hub_auth

    return workos_hub_auth


def _webui() -> Any:
    """The Control Center module, or None when its component is not loaded."""
    if not components.enabled("control-center"):
        return None
    from . import webui

    return webui


def _webui_auth() -> Any:
    """The Control Center cookie auth module, or None when not loaded."""
    if not components.enabled("control-center"):
        return None
    from .webui import auth

    return auth


def _webui_workos() -> Any:
    """The Control Center WorkOS sign-in module, or None when not loaded."""
    if not components.enabled("control-center"):
        return None
    from .webui import workos_signin

    return workos_signin


def _native_self_gated(fn: Any) -> bool:
    """Whether ``fn`` runs its own native approval gate (task #2535).

    ``delegate`` is a kernel tool, so it stays self-gated in a kernel-only
    process: conditional loading must never move an approval gate behind a
    component check. ``run_cmd`` and ``fs_write`` are dev-tools, so they can
    only be self-gated when that component is loaded.
    """
    if fn is delegate.delegate:
        return True
    if not components.enabled("dev-tools"):
        return False
    from .tools import files, shell

    return fn in (shell.run_cmd, files.fs_write)


def _health_routes() -> list[Any]:
    """The always-on ``/healthz`` and ``/readyz`` routes (task #2603 review).

    They belong to the adapter, not to the Control Center, so a hub that loads
    only the kernel still answers a deployment health check.
    """
    from .health import health_routes

    return health_routes()


# Component modules that used to be module-level imports here. Loading is
# conditional now (K4b, task #2603), so each is resolved on first access
# instead of at start-up. The names stay reachable through this module for the
# callers that name them that way -- a test patching a module attribute, or an
# operator reading a live hub -- and every branch is a literal import so the
# static readers still follow the edge.
_COMPONENT_MODULE_OF: dict[str, str] = {
    "oauth": "auth",
    "workos_hub_auth": "auth",
    "hosted_logging": "cloud",
    "mcp_progress": "telemetry",
    "mcp_runtime_telemetry": "telemetry",
    "monitor": "telemetry",
    "routines": "dispatch",
    "webui": "control-center",
    "webui_auth": "control-center",
    "webui_workos": "control-center",
    "files": "dev-tools",
    "shell": "dev-tools",
}


def __getattr__(name: str) -> Any:
    """Import a component module on first access (PEP 562).

    Raises ``AttributeError`` for an unknown name, and for a known name whose
    component this process did not load -- an unloaded component is absent, not
    silently imported.
    """
    component = _COMPONENT_MODULE_OF.get(name)
    if component is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if not components.enabled(component):
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}: component {component!r} is not loaded"
        )
    if name == "oauth":
        from . import oauth as module
    elif name == "workos_hub_auth":
        from . import workos_hub_auth as module
    elif name == "hosted_logging":
        from . import hosted_logging as module
    elif name == "mcp_progress":
        from . import mcp_progress as module
    elif name == "mcp_runtime_telemetry":
        from . import mcp_runtime_telemetry as module
    elif name == "monitor":
        from . import monitor as module
    elif name == "routines":
        from . import routines as module
    elif name == "webui":
        from . import webui as module
    elif name == "webui_auth":
        from .webui import auth as module
    elif name == "webui_workos":
        from .webui import workos_signin as module
    elif name == "files":
        from .tools import files as module
    else:  # shell
        from .tools import shell as module
    globals()[name] = module
    return module


@asynccontextmanager
async def _hub_lifespan(server: MCPServer) -> AsyncIterator[dict]:
    """Fan out SQLite board/run changes onto the MCP 2.x subscription bus.

    On shutdown, write the pending read-only gateway rollups (task #1922).
    """
    toolsets.require_known_toolsets()
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(mcp_live_state.poll_loop, server)
            sweep_s = _node_wait_sweep_interval()
            if sweep_s:
                tg.start_soon(_node_wait_sweep_loop, sweep_s)
            yield {}
            tg.cancel_scope.cancel()
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(gateway.flush_coalesced)


def _node_wait_sweep_interval() -> int:
    """Seconds between hub wait sweeps, or 0 for none (task #2110 review).

    Only the HTTP hub owns the node queue; a stdio child is a client (on the
    desktop it has no database at all). Never raises: ``main`` has already
    refused to start on a malformed value.
    """
    from . import node_queue

    try:
        if config.transport() != "http":
            return 0
        return node_queue.node_wait_sweep_s()
    except ValueError:
        logger.warning("node wait sweep disabled: invalid setting", exc_info=True)
        return 0


def _node_wait_sweep_once() -> int:
    """One hub wait sweep; returns the number of expired items. Never raises."""
    from . import node_queue

    try:
        from . import node_acceptance

        node_acceptance.expire_pending()
        return len(node_queue.expire_waiting(actor=node_queue.WAIT_SWEEP_ACTOR))
    except Exception:
        logger.warning("node wait sweep failed; retrying next interval", exc_info=True)
        return 0


async def _node_wait_sweep_loop(interval_s: float) -> None:
    """Expire overdue waiting node work every ``interval_s`` (task #2110 review).

    Lease reclamation also runs the sweep, but only from a scheduler tick or
    an explicit dispatch; a hub with dispatch off would otherwise leave an
    offline node's work queued, and its run open, past the deadline. The
    sweep is bounded and runs in a worker thread; cancelling the lifespan's
    task group stops the loop at its next sleep.
    """
    while True:
        await anyio.sleep(interval_s)
        await anyio.to_thread.run_sync(_node_wait_sweep_once)


def _require_node_queue_settings() -> None:
    """Refuse to start on a malformed node-queue wait setting (task #2110)."""
    from . import node_queue

    try:
        node_queue.node_queue_wait_s()
        node_queue.node_wait_sweep_s()
    except ValueError as exc:
        print(f"ua: refusing to start: {exc}", file=sys.stderr)
        sys.exit(1)


def _http_settings() -> dict:
    """Streamable-HTTP hub settings consumed by run()/streamable_http_app() (inert under stdio)."""
    settings: dict = {"host": config.http_host(), "port": config.http_port()}
    allowed = config.http_allowed_hosts()
    if allowed:
        settings["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed,
            # Origins come from config.http_allowed_origins(): the hub's own
            # origin plus UA_HTTP_ALLOWED_HOSTS. "null" (sandboxed iframes,
            # file://) is included only when UA_HTTP_ALLOWED_ORIGINS lists it.
            allowed_origins=config.http_allowed_origins(),
        )
    # UA_MCP_SESSION_IDLE_TIMEOUT_S (task #1298): unset means the key is
    # omitted entirely, so the SDK's own default (1800s) applies unchanged.
    # config.mcp_session_idle_timeout_s() already translates "0" to None
    # (the SDK's real disable value) and validates negative/non-numeric input.
    if config.mcp_session_idle_timeout_configured():
        settings["session_idle_timeout"] = config.mcp_session_idle_timeout_s()
    return settings


# The self-description every MCP client receives at initialize. Pinned by
# tests/test_docs_self_description.py (UA #1074): frame the platform, name MCP
# as one adapter among several — never open by calling the project a tool
# server, which mis-teaches every reader and every connected agent.
MCP_INSTRUCTIONS = (
    "Agent-operations platform: one authority store (identity, policy), an "
    "approval gate, domain objects (tasks, plans, delegations, runs, sessions, "
    "memory notes, evidence events), a tamper-evident evidence ledger, executors, "
    "and a web control center. This MCP surface is one adapter over that platform, "
    "alongside HTTP/JSON, CLI, Web UI, and SDK. Tool groups: web_* (search/fetch), "
    "gemini_ask (Google "
    "AI Studio API-key text generation, GEMINI_API_KEY — also backs "
    "llm_client's ai_studio leg), fs_* + run_cmd "
    "(sandboxed to the workspace root; run_cmd_check dry-run vets a command "
    "first), security_audit (read-only AI-dev "
    "best-practice checks over workspace or UA_AUDIT_ROOTS), skill_review "
    "(read-only lint for agent SKILL.md files: frontmatter contract, trigger "
    "quality, sibling-description overlap), detect_dev_servers (read-only scan "
    "for launchable dev servers — package.json/pyproject.toml/docker-compose/"
    "Procfile — proposing .claude/launch.json entries with a best-effort port-"
    "conflict check), git_* (staged "
    "diffs, Conventional Commits, worktrees — workspace repos only), "
    "python_lint_and_format (ruff fix + format), fs_*_symbols (AST symbol indexer), "
    "memory_* (persistent notes shared across Claude Code and Antigravity sessions, "
    "hybrid FTS + semantic search — search memory before asking the user to repeat "
    "themselves), task_* (durable task list), plan_* (dumb-storage plan/checkpoint/"
    "self-correction primitives — no LLM, no auto-execution; the agent drives; "
    "plan_step_claim/plan_handoff add atomic claiming + outputs handoff for "
    "multi-agent chains), delegate/delegate_list (agent-to-agent task handoff "
    "with audit trail), workflow_run/workflow_list (repo-reviewed workflow "
    "definitions; steps call the same gated tools), approval_pending/"
    "approval_resolve (opt-in global approval queue, UA_APPROVAL_TOOLS), "
    "vault_* (Obsidian vault at UA_VAULT_PATH: "
    "read/search/link notes anywhere, write only to UA_VAULT_WRITE_DIRS (writes refuse "
    "notes marked `locked: true` unless force=True); vault_query does embedding-free "
    "graph retrieval (lexical seeds + Personalized PageRank over [[wikilinks]], no index/"
    "network); vault_ingest_status is the content-hash pre-ingest gate; vault_journal "
    "appends agent-attributed entries to the daily log — record notable actions there; "
    "vault_briefing_digest merges today's journal entries into one deduped, "
    "chronological digest and lists each inbox capture's "
    "ingest state so a briefing never re-proposes an already-wikified note), "
    "system_doctor (read-only LIVE/DORMANT/DEGRADED/MISCONFIGURED/UNKNOWN report over every config-gated "
    "capability — hub auth, approval gates, search provider, per-agent workspaces, "
    "automation timers, optional extras). "
    "All limits are stated in each tool's description."
)

# Railway treats every stderr line as level=error. MCPServer() installs a
# Rich handler on stderr via logging.basicConfig, and basicConfig does nothing
# once the root logger already has handlers. Install the hosted JSON handlers
# first so HTTP logs retain their real level and stdio stdout stays protocol-only.
if components.enabled("cloud"):
    from . import hosted_logging

    hosted_logging.install()

mcp = MCPServer(
    "unified-agent",
    lifespan=_hub_lifespan,
    instructions=MCP_INSTRUCTIONS,
)


# MCP clients use these hints for confirmation UX. Keep the classification here,
# beside the single registration loop, so a newly registered tool cannot silently
# lose its risk metadata.
_READ_ONLY_TOOLS = frozenset(
    {
        "web_search",
        "web_fetch",
        "dash_list",
        "dash_search",
        "dash_view",
        "fs_read",
        "fs_list",
        "fs_search",
        "gemini_ask",
        "run_cmd_check",
        "injection_triage",
        "skill_review",
        "detect_dev_servers",
        "git_diff_staged",
        "git_worktree_list",
        "fs_list_symbols",
        "fs_search_symbols",
        "fs_repo_map",
        "format_render",
        "evidence_report",
        "dispatch_delivery_receipt",
        "dispatch_delivery_receipts_list",
        "memory_search",
        "memory_list",
        "memory_get",
        "memory_prune_candidates",
        "recall_maintenance",
        "vault_read",
        "vault_list",
        "vault_search",
        "vault_links",
        "vault_briefing_digest",
        "vault_query",
        "export_tasks",
        "export_memory",
        "export_journal",
        "export_migration_manifest",
        "compare_migration_manifests",
        "read_image",
        "task_list",
        "routine_list",
        "dependency_list",
        "handoff_list",
        "graph_canvas_read",
        "autonomous_cycle_get",
        "autonomous_cycle_list",
        "plan_get",
        "plan_list",
        "delegation_ledger",
        "delegate_list",
        "delegation_spec_check",
        "delegation_spec_template",
        "delegation_spec_build",
        "check_agent_capability",
        "capability_catalog",
        "workflow_list",
        "approval_pending",
        "horizon2_server_list",
        "horizon2_package_list",
        "model_quota_status",
        "zulip_fetch_messages",
        "scan_for_reviewable_tasks",
        "autonomous_cycle_green_path_eligibility",
        "metrics_snapshot",
        "check_anomalies",
        "perf_summary",
        "agent_activity",
        "system_doctor",
        "notify_delivery_status",
        "event_log_query",
        "event_log_verify",
        "agent_list",
        "agent_context",
        "session_list",
        "session_packet",
        "message_list",
        "state_sync_verify",
        "browser_derive_task_reference",
        "langgraph_checkpoints",
        "gcloud_inventory",
        "obsidian_status",
        "obsidian_active_note",
        "obsidian_search",
        "profile_status",
        "mcp_health_telemetry",
        "crew_view",
    }
)

# Local classifications are server-owned; new or externally targeted tools
# are unknown/production until reviewed. Client payloads never downgrade them.
_LOCAL_TOOLS = (
    _READ_ONLY_TOOLS
    - {
        "web_search",
        "web_fetch",
        "gemini_ask",
        "gcloud_inventory",
        "zulip_fetch_messages",
        "obsidian_status",
        "obsidian_active_note",
        "obsidian_search",
    }
) | {
    "task_add",
    "task_update",
    "task_claim",
    "routine_add",
    "routine_pause",
    "routine_fire",
    "task_release",
    "memory_save",
    "memory_delete",
    "approval_resolve",
    "managed_secret_set",
    "profile_activate",
    "profile_reset",
}


def _production_operation(name: str) -> bool:
    return name not in _LOCAL_TOOLS


_DESTRUCTIVE_TOOLS = frozenset(
    {
        # Commit and rollback rewrite authority config (task #1352). list/diff/show
        # are read-only, but one tool carries both, so the annotation takes the
        # stronger hint — same posture as oauth_device_manage.
        "config_revision_manage",
        "crew_intervene",
        # Revoke deletes device sign-ins (task #1321); list is read-only but one
        # tool carries both verbs, so the annotation takes the stronger one.
        "oauth_device_manage",
        "run_cmd",
        "managed_secret_set",
        "fs_write",
        "git_commit_staged",
        "git_worktree_remove",
        "git_merge_task",
        "python_lint_and_format",
        "memory_save",
        "memory_restore",
        "memory_feedback",
        "memory_delete",
        "memory_promote",
        "vault_write",
        "vault_move",
        "vault_archive_plan",
        "vault_ingest_status",
        "vault_wiki_custodian",
        "import_migration_manifest",
        "rollback_migration_import",
        "task_update",
        "task_delete",
        "coordination_manage",
        "dependency_remove",
        "graph_canvas_write",
        "autonomous_cycle_manage",
        "autonomous_cycle_transition",
        "autonomous_cycle_kill",
        "bug_worker_manage",
        "plan_update",
        "plan_manage",
        "plan_resume",
        "plan_step_retry",
        "plan_delete",
        "delegation_manage",
        "delegate_release",
        "workflow_run",
        "approval_resolve",
        "horizon2_server_revoke",
        "horizon2_connector_authorize",
        "horizon2_package_register",
        "review_and_notify",
        "observability_manage",
        "check_anomalies_and_notify",
        "session_end",
        "propose_session_learnings",
        "propose_skill_clusters",
        "cleanup_stale_worktrees",
        "vault_to_memory_ingest",
        "obsidian_open",
        "obsidian_append",
        "profile_activate",
        "profile_reset",
        "pi_run_task",
        "gcloud_ops",
        "cloud_worker_recover",
        "langgraph_run",
        "langgraph_progress_note",
        "langgraph_workflow_run",
        "export_data",
        "security_audit",
        "audit_triage_tasks",
    }
)

_IDEMPOTENT_TOOLS = (
    # billed call: repeating it spends AI Studio quota, so not safe to auto-retry
    _READ_ONLY_TOOLS
    - {"gemini_ask"}
    # lifecycle_record without an idempotency_key appends a duplicate observation
    # on retry; only the keyed path is idempotent.
    - {"lifecycle_record"}
) | {
    "fs_index_symbols",
    "format_render",
    "dispatch_report_self",
    "dispatch_cancel",
    "dispatch_resume",
    "dispatch_retry_cancellation",
    "dispatch_confirm_cancellation",
    "agent_heartbeat",
}


def _tool_annotations(fn) -> ToolAnnotations:
    name = fn.__name__
    return ToolAnnotations(
        readOnlyHint=name in _READ_ONLY_TOOLS,
        destructiveHint=name in _DESTRUCTIVE_TOOLS,
        idempotentHint=name in _IDEMPOTENT_TOOLS,
    )


TOOL_REQUIRED_FEATURES: dict[str, str] = {
    "event_log_verify": "audit_hash_chain",
}

#: Manager writes use a dedicated wrapper gate; reads use the generic configured
#: gate instead, since their native action dispatchers do not gate reads (#1934).
_WRAPPER_GATED_APPROVAL_MANAGERS = frozenset(
    {"goal_manage", "task_comment", "skill_registry", "org_proposal"}
)

#: Tools the wrapper's generic ``UA_APPROVAL_TOOLS`` gate skips (task #1811),
#: except for read actions of the wrapper-gated managers above.
#: Each one already gates itself under its own name with its own payload, or a
#: dedicated branch of the wrapper gates it; a second wrapper gate would queue a
#: second request for one call. ``tests/test_event_log_query_registered_gates.py``
#: scans the source for self-gating tools and fails when one is missing here.
_SELF_GATED_APPROVAL_TOOLS = frozenset(
    {
        # The tool body calls check_approval_gate under its own name.
        "synthetic_workflow",
        "run_cmd",
        "fs_write",
        "delegate",
        "git_commit_staged",
        "gcloud_ops",
        # The tool body forces an approval (prod-action gate or a forced pass).
        "git_merge_task",
        "task_delete",
        "oauth_device_manage",
        "horizon2_server_register",
        "horizon2_server_revoke",
        "horizon2_connector_authorize",
        # These force a gate under another name on every effective call
        # (horizon2_server_register, work_product_release). A wrapper pass is
        # spent once the call starts, before that inner gate parks, so one
        # registration or release would cost three approvals instead of one.
        "horizon2_package_register",
        "work_product_submit",
        # Dedicated wrapper branches gate these (writes only for the managers).
        "managed_secret_set",
        "agent_token_issue",
        "agent_token_rotate",
        "deployment_claim",
        "routine_fire",
        "goal_manage",
        "task_comment",
        "skill_registry",
        "org_proposal",
        # The approval queue never parks its own review or resolution.
        "approval_pending",
        "approval_resolve",
    }
)

# Disposable acceptance exposes one tool. task_update stays reachable only as
# that workflow's own registered writer, after the invocation has marked execution.
_SYNTHETIC_MODE_TOOLS = frozenset({"synthetic_workflow"})


def _action_forces_own_gate(tool_name: str, payload: dict) -> bool:
    """Whether this call is the one action of a mixed tool that forces its own gate.

    Task #1896. ``coordination_manage`` action ``team_up`` with ``plan=False``
    boots behind a forced ``team_up`` gate, and ``relay_manage`` action ``send``
    sends behind a forced ``relay.public.send`` gate. A wrapper pass would be
    spent once the call started, before that inner gate parked, so one boot or
    send would cost three approvals. The forced gate is that action's one
    approval, so the generic gate skips exactly that action. The match is
    deliberately narrower than the bodies' own dispatch (for example only a
    literal ``plan is False``): a call it misses is gated twice, never zero
    times. Every other action of these tools keeps the generic gate.
    """
    action = payload.get("action")
    if not isinstance(action, str):
        return False
    action = action.strip().lower()
    if tool_name == "coordination_manage":
        params = payload.get("params")
        return action == "team_up" and isinstance(params, dict) and params.get("plan") is False
    if tool_name == "relay_manage":
        return action == "send"
    return False


#: Pure event-log readers whose ALLOWED calls are not written to the event log
#: they read (task #1811). A consumer pages ``event_log_query`` with
#: ``after_seq`` to the tail; an allow row per call would create the row the
#: next page returns, so it would never reach an empty page. ``/api/ledger/rows``
#: leaves allowed reads unrecorded for the same reason (PR #981). Denials,
#: parked approvals and errors are still recorded.
_UNRECORDED_ALLOWED_READS = frozenset({"event_log_query"})

#: Pure-read actions of tools ``_read_only_call`` classifies as mutating
#: (task #1922). Their ALLOW evidence is coalesced like a read-only tool's;
#: access control and fail-closed recording of every other outcome are
#: unchanged. The executor daemons poll ``delegation_manage(action="list")``
#: every 5 s: ~17.7k rows per daemon per day on the hub before this.
_EVIDENCE_READ_ACTIONS = {"delegation_manage": frozenset({"list", "get"})}


def _evidence_read_action(name: str, args, kwargs) -> bool:
    actions = _EVIDENCE_READ_ACTIONS.get(name)
    if actions is None:
        return False
    action = args[0] if args else kwargs.get("action")
    return isinstance(action, str) and action.strip().lower() in actions


def _guarded_and_instrumented(fn):
    def _read_only_call(args, kwargs):
        """Classify the bound call, including tools with mixed actions."""
        name = fn.__name__
        if name == "observability_manage":
            action = args[0] if args else kwargs.get("action")
            return action == "run_history"
        if name == "goal_manage":
            action = str(args[0] if args else kwargs.get("action") or "").strip().lower()
            return action in {
                "list",
                "tree",
                "get",
                "ancestry",
                "autonomy_get",
                "autonomy_preview",
                "operating_profiles",
                "operating_resolve",
                "operating_sprint_list",
                "operating_sprint_get",
            }
        if name == "task_comment":
            action = str(args[0] if args else kwargs.get("action") or "").strip().lower()
            return action in {"list", "get"}
        if name == "org_proposal":
            action = str(args[0] if args else kwargs.get("action") or "").strip().lower()
            return action in {"list", "get"}
        if name == "skill_registry":
            action = str(args[0] if args else kwargs.get("action") or "").strip().lower()
            return action in {"list", "get", "resolve", "drift", "catalog"}
        if name == "delegation_manage":
            action = args[0] if args else kwargs.get("action")
            return action in {"list", "get"}
        if name not in _READ_ONLY_TOOLS | {
            "export_data",
            "security_audit",
            "vault_ingest_status",
            "vault_wiki_custodian",
        }:
            return False
        try:
            bound = inspect.signature(fn).bind(*args, **kwargs)
            bound.apply_defaults()
        except (TypeError, ValueError):
            return False
        values = bound.arguments
        if name == "export_data":
            params = values.get("params") or {}
            return values.get("action") in {
                "tasks",
                "memory",
                "journal",
                "manifest",
                "compare",
            } and not params.get("write_to_vault", False)
        if name in {"export_tasks", "export_memory", "export_journal", "export_migration_manifest"}:
            return not values.get("write_to_vault", False)
        if name == "security_audit":
            return not values.get("remediate", False)
        if name == "vault_ingest_status":
            return not values.get("mark", False)
        if name == "vault_wiki_custodian":
            return bool(values.get("dry_run", False))
        return True

    def _policy_guarded_body(*args, **kwargs):

        tool_name = fn.__name__
        if tool_name == "task_update" and config.workos_request_is_bound():
            from mcp.server.mcpserver.exceptions import ToolError

            from .invocation_service import InvocationDenied, principal_from_request

            try:
                principal = principal_from_request()
            except InvocationDenied as exc:
                raise ToolError("a current verified organization request is required") from exc
            if principal.organization != edition.tenant_org_id():
                raise ToolError(
                    "verified request organization does not match the task storage boundary"
                )
        # Validate required arguments before consuming any authority. Bind every
        # supplied positional argument to its signature name. Optional omissions
        # keep the established adapter payload protocol; native self-gates use
        # their own defaults/digests and may require a distinct policy approval.
        signature = inspect.signature(fn)
        bound = signature.bind(*args, **kwargs)
        payload = {}
        for name, value in bound.arguments.items():
            if signature.parameters[name].kind == inspect.Parameter.VAR_KEYWORD:
                payload.update(value)
            else:
                payload[name] = value
        read_only = _read_only_call(args, kwargs)
        if tool_name != "profile_status":
            agent_access.enforce(
                tool_name,
                read_only=read_only,
                production=tool_name
                in {
                    "dispatch_cancel",
                    "dispatch_resume",
                    "dispatch_retry_cancellation",
                    "dispatch_confirm_cancellation",
                    "langgraph_workflow_run",
                    "workflow_run",
                }
                or _production_operation(tool_name),
                # Only a route that already verified the operator on a human
                # auth channel grants this, for one read of one tool.
                human_recovery=agent_access.recovery_read_granted(tool_name, read_only=read_only),
            )

        identity = (
            config.agent_identity_var.get()
            or os.environ.get("UA_AGENT_LABEL")
            or os.environ.get("AGENT_LABEL")
        )
        if identity and identity.strip():
            try:
                registry.touch_agent(identity.strip(), via="tool_call")
            except Exception:
                pass
        # Visibility ceiling. Call permission for an exposed tool is the policy
        # gate below (allow / deny / approval / rate limit), not this check.
        # A session credential (task #1744) has exactly one visibility term,
        # its grant: no recovery or route-authority exception may widen it.
        if operational_profiles.current_session_grant() is not None:
            visible = operational_profiles.tool_is_allowed(fn.__name__)
        else:
            visible = (
                operational_profiles.tool_is_allowed(fn.__name__)
                or operational_profiles.http_read_tool_is_allowed(fn.__name__, payload)
                or _cos_rollback_recovery(fn.__name__, args, kwargs)
            )
        if not visible:
            from mcp.server.mcpserver.exceptions import ToolError

            # Deprecated-name logging (task #1230): a call to a name dropped
            # from the default profile is recorded so real consumers surface
            # instead of failing silently.
            logger.warning(
                "profile denied tool call: tool=%r profile=%s identity=%s%s",
                fn.__name__,
                operational_profiles.current(),
                config.verified_agent_identity(),
                " (pruned from the default profile; still reachable under steward/chief-of-staff)"
                if fn.__name__ in operational_profiles.DEFAULT_PROFILE_PRUNED_TOOLS
                else "",
            )
            gateway.record_denial(
                tool_name=tool_name,
                reason_code="profile_denied",
                rule="",
                caller=config.verified_agent_identity() or "",
                profile=operational_profiles.current(),
                episode_id=config.current_episode_id(),
            )
            raise ToolError(
                f"tool {fn.__name__!r} is not exposed by the active "
                f"{operational_profiles.current()} profile"
            )
        session_grant = operational_profiles.current_session_grant()
        if session_grant is not None and tool_name == "task_comment":
            # A session credential may comment only on its own bound task
            # (task #1744). Runs before the policy gate, like the ceiling.
            _check_session_task_comment(session_grant, payload)
        try:
            bound = inspect.signature(fn).bind(*args, **kwargs)
            bound.apply_defaults()
            preset_payload = dict(bound.arguments)
        except (TypeError, ValueError):
            preset_payload = kwargs
        trust_presets.check_call(tool_name, read_only=read_only, payload=preset_payload)
        required_feature = TOOL_REQUIRED_FEATURES.get(fn.__name__)
        if required_feature:
            edition.require_feature(required_feature)
        if tool_name == "synthetic_workflow":
            # Validate the original durable approval before the native policy gate.
            return fn(*args, **kwargs)
        if tool_name == "task_update":
            replay = tasks.registered_task_replay(payload)
            if replay is not None:
                with approvals.committed_task_replay():
                    safety.check_policy_gate(tool_name, payload)
                    return tasks.acknowledge_registered_task_replay(payload)
        if tool_name == "managed_secret_set":
            bound = inspect.signature(fn).bind(*args, **kwargs)
            bound.apply_defaults()
            payload = profile_tools.managed_secret_payload(**bound.arguments)
            with approvals.approval_invocation():
                safety.check_policy_gate(tool_name, payload)
                with approvals.native_approval_execution(tool_name):
                    return fn(*args, **kwargs)
        if tool_name in {"agent_token_issue", "agent_token_rotate", "deployment_claim"}:
            bound = inspect.signature(fn).bind(*args, **kwargs)
            bound.apply_defaults()
            value_name = (
                "value" if tool_name in {"agent_token_issue", "deployment_claim"} else "new_value"
            )
            caller_identity = None
            if tool_name == "deployment_claim":
                nonce = bound.arguments.get("client_nonce")
                if not config.verified_agent_identity() and nonce and nonce.strip():
                    caller_identity = (
                        f"claimant:{bound.arguments['agent_label']}:nonce:{nonce.strip()}"
                    )
                else:
                    caller_identity = (
                        config.verified_agent_identity()
                        or f"claimant:{bound.arguments['agent_label']}"
                    )
            payload = profile_tools.managed_secret_payload(
                name=f"agent-token:{bound.arguments['agent_label']}",
                value=bound.arguments[value_name],
                consumer="agent_token",
                caller_identity=caller_identity,
            )
            with approvals.approval_invocation():
                safety.check_policy_gate(tool_name, payload)
                with approvals.native_approval_execution("managed_secret_set"):
                    return fn(*args, **kwargs)
        if tool_name == "routine_fire":
            # Keep receipt lookup, one-shot policy approval, and the write in
            # one process-wide critical section. The ordinary wrapper still
            # records agent activity and a performance sample for replays.
            from . import routines  # dispatch component; only its tool gets here

            with routines.manual_fire_guard():
                bound = inspect.signature(fn).bind(*args, **kwargs)
                previous = routines.routine_fire_preflight(
                    bound.arguments["routine_id"],
                    idempotency_key=bound.arguments["idempotency_key"],
                )
                if previous is not None:
                    return previous
                # A receipt replay above needs no approval. A new fire runs the
                # policy gate, then UA_APPROVAL_TOOLS exactly as the generic
                # path below does (task #1811): one pass per invocation, and a
                # parked request returns its rate slot because nothing ran.
                with approvals.approval_invocation(tool_name, payload, join=True) as receipts:
                    safety.check_policy_gate(tool_name, payload)
                    if not any(
                        isinstance(receipt, dict) and receipt.get("approval_id") is not None
                        for receipt in receipts.values()
                    ):
                        approvals.check_approval_gate(
                            tool_name,
                            approvals.supplied_arguments_payload(payload),
                            requested_by=config.verified_agent_identity(),
                        )
                    approvals.mark_governed_execution()
                    return fn(*args, **kwargs)
        if tool_name in _WRAPPER_GATED_APPROVAL_MANAGERS and not read_only:
            payload: dict = {}
            if args:
                payload["action"] = args[0]
            if len(args) > 1:
                payload["params"] = args[1]
            payload.update(kwargs)
            if tool_name == "goal_manage":
                from . import operating_profiles

                operating_profiles.preflight(payload.get("action"), payload.get("params"))
            nested_skill_install = tool_name == "skill_registry" and str(
                payload.get("action") or ""
            ).strip().lower() in {"install", "team_install"}
            with approvals.approval_invocation(restore_unstarted=nested_skill_install) as receipts:
                safety.check_policy_gate(tool_name, payload)
                approvals.check_approval_gate(
                    tool_name, payload, requested_by=config.verified_agent_identity()
                )
                if not nested_skill_install:
                    approvals.mark_governed_execution()
                # Install adapters join this pre-execution action internally.
                # Their actual lower write marks these same receipts spent;
                # pending/refused/no-op installs restore unused outer passes.
                result = fn(*args, **kwargs)
                action = str(payload.get("action") or "").strip().lower()
                if tool_name == "skill_registry" and action == "publish" and result.get("blocked"):
                    # A high-severity skill_review result is a structured
                    # refusal rather than an exception. Restore the one-shot
                    # pass because no publish happened and the corrected retry
                    # should still be authorized by the same human decision.
                    for receipt in receipts.values():
                        if isinstance(receipt, dict) and receipt.get("approval_id") is not None:
                            approvals.restore_consumed_approval(int(receipt["approval_id"]))
                return result
        merge_tool = tool_name == "git_merge_task"
        scope = (
            approvals.approval_invocation()
            if merge_tool
            else approvals.approval_invocation(tool_name, payload, join=True)
        )
        with scope as receipts:
            gate_payload = preset_payload if merge_tool else payload
            if merge_tool:
                gateway._bind_merge_policy_arguments(preset_payload, kwargs)
            safety.check_policy_gate(tool_name, gate_payload)
            if (
                tool_name not in _SELF_GATED_APPROVAL_TOOLS
                or (tool_name in _WRAPPER_GATED_APPROVAL_MANAGERS and read_only)
            ) and not _action_forces_own_gate(tool_name, payload):
                # UA_APPROVAL_TOOLS for every other tool (task #1811), with the
                # payload the HTTP governed reads use: the supplied arguments,
                # None meaning omitted. One invocation, one pass: a policy
                # require_approval, or a joined caller such as a workflow step,
                # that already consumed a pass for this call is not asked again.
                pass_consumed = any(
                    isinstance(receipt, dict) and receipt.get("approval_id") is not None
                    for receipt in receipts.values()
                )
                if not pass_consumed:
                    approvals.check_approval_gate(
                        tool_name,
                        approvals.supplied_arguments_payload(payload),
                        requested_by=config.verified_agent_identity(),
                    )
            native_self_gated = _native_self_gated(fn)
            # Same decision as delegate()'s own human skip (task #2535).
            human_delegation = fn is delegate.delegate and config.verified_caller_is_operator()
            if native_self_gated and not human_delegation:
                # These native gates run after validation and before effects.
                # A pending native payload must restore any wrapper-policy pass.
                with approvals.native_approval_execution(tool_name):
                    return fn(*args, **kwargs)
            # Task merge may return a structured mission pause after entry.
            # Its orchestrator marks execution only at the first Git mutation.
            # The selected task writer owns a SQLite commit boundary. Its
            # validation and rollback must not spend a generic wrapper pass.
            if tool_name not in {"git_merge_task", "task_update"}:
                approvals.mark_governed_execution()
            try:
                return fn(*args, **kwargs)
            except approvals.ApprovalPendingError:
                if tool_name == "git_merge_task" and not approvals.governed_execution_started():
                    for receipt in tuple(receipts.values()):
                        if isinstance(receipt, dict) and receipt.get("approval_id") is not None:
                            approvals.restore_consumed_approval(int(receipt["approval_id"]))
                raise

    def _supplied_arguments(args, kwargs) -> dict:
        """The caller's arguments by parameter name, defaults not applied."""
        try:
            signature = inspect.signature(fn)
            bound = signature.bind(*args, **kwargs)
        except (TypeError, ValueError):
            return {"args": list(args), "kwargs": dict(kwargs)}
        supplied = {}
        for name, value in bound.arguments.items():
            if signature.parameters[name].kind == inspect.Parameter.VAR_KEYWORD:
                supplied.update(value)
            else:
                supplied[name] = value
        return supplied

    @functools.wraps(fn)
    def policy_guarded(*args, **kwargs):
        if (
            os.environ.get("UA_SYNTHETIC_WORKFLOW") == "1"
            and fn.__name__ not in _SYNTHETIC_MODE_TOOLS
        ):
            if fn.__name__ != "task_update" or not approvals.governed_execution_started():
                from mcp.server.mcpserver.exceptions import ToolError

                raise ToolError("disposable synthetic mode exposes only synthetic_workflow")
        # One charging scope per invocation: a tool that re-checks its own
        # policy inside its body must not spend a second rate slot.
        # The decision event is written after the body (task #1907), so it
        # records the outcome together with the argument digest. A mutating
        # call (not _read_only_call) fails closed with a ToolError when that
        # write fails; a read-only call stays best-effort. A read's ALLOW is
        # coalesced into an hourly rollup (task #1922); its other outcomes
        # stay per call.
        arguments = _supplied_arguments(args, kwargs)
        mutating = not _read_only_call(args, kwargs)
        coalesce = not mutating or _evidence_read_action(fn.__name__, args, kwargs)
        with gateway.invocation(fn.__name__) as call:
            call.registered_arguments = arguments
            try:
                if fn.__name__ == "task_update":
                    with tasks.registered_update_binding():
                        result = _policy_guarded_body(*args, **kwargs)
                else:
                    result = _policy_guarded_body(*args, **kwargs)
            except BaseException as exc:
                gateway.record_call_result(
                    fn.__name__, call, exc, arguments=arguments, mutating=mutating
                )
                raise
            if fn.__name__ not in _UNRECORDED_ALLOWED_READS:
                gateway.record_call_result(
                    fn.__name__,
                    call,
                    None,
                    result,
                    arguments=arguments,
                    mutating=mutating,
                    coalesce=coalesce,
                )
            return result

    if components.enabled("telemetry"):
        from . import monitor

        return monitor.instrumented(policy_guarded)
    return policy_guarded


def _check_session_task_comment(grant, payload: dict) -> None:
    """Refuse a session credential's ``task_comment`` outside its bound task.

    ``append`` and ``list`` must name the bound task; ``get`` must read a
    comment on it. A session with no bound task gets no comment access.
    Records a deny before raising.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    action = str(payload.get("action") or "").strip().lower()
    params = payload.get("params")
    params = params if isinstance(params, dict) else {}
    target = None
    try:
        if action in {"append", "list"}:
            target = int(params.get("task_id"))
        elif action == "get":
            found = task_comments.get_comment(int(params.get("comment_id")))
            target = int(found["comment"]["task_id"]) if found else None
    except (TypeError, ValueError, KeyError):
        target = None
    if grant.task_id is not None and target == grant.task_id:
        return
    gateway.record_denial(
        tool_name="task_comment",
        reason_code="session_task_scope",
        rule="",
        caller=config.verified_agent_identity() or "",
        profile=operational_profiles.current(),
        episode_id=config.current_episode_id(),
    )
    raise ToolError("a session credential may use task_comment only on its own bound task")


def _cos_rollback_recovery(tool_name: str, args: tuple, kwargs: dict) -> bool:
    """Let a code-default chief of staff roll back after an overlay downgrade.

    The effective profile no longer exposes ``config_revision_manage``. Commit
    stays denied. The rollback function still requires an approval pass.
    """
    if tool_name != "config_revision_manage":
        return False
    if operational_profiles.current_session_grant() is not None:
        # Never for a session credential, whatever its label's code default.
        return False
    action = kwargs.get("action")
    if action is None and args:
        action = args[0]
    from .config_revisions import rollback_recovery_allowed

    identity = config.verified_agent_identity()
    return rollback_recovery_allowed(identity, str(action or ""))


def _with_elicitation(sync_fn):
    """Wrap a sync tool callable in the MCP 2.x elicitation boundary (task #829).

    The sync callable is unchanged and still what ``REGISTERED_TOOLS`` (and the
    ``/ui`` + langgraph dispatch paths) invoke. Only the MCP registration gets
    this async boundary: it runs the tool off the event loop, and if the tool
    parks in the approval queue it offers the pending row to a capable client as
    an in-client form before retrying once on approval. A thin client, a
    decline, or any error leaves the row pending and the original
    ``ApprovalPendingError`` propagates — the existing ``/ui`` flow, untouched.

    ``elicit_ctx`` is injected by MCPServer because it is annotated ``Context``;
    it is absent from the ``__wrapped__`` signature so it never reaches the tool
    schema.
    """

    @functools.wraps(sync_fn)
    async def boundary(elicit_ctx=None, **kwargs):
        if elicit_ctx is not None:
            try:
                proto = getattr(elicit_ctx, "protocol_version", None)
                client_caps = getattr(elicit_ctx, "client_capabilities", None)
                req_ctx = getattr(elicit_ctx, "_request_context", None)
                session = getattr(req_ctx, "session", None) if req_ctx else None
                init_params = getattr(session, "client_params", None) if session else None
                client_info = getattr(init_params, "clientInfo", None) if init_params else None
                client_name = (
                    getattr(client_info, "name", "mcp-client") if client_info else "mcp-client"
                )
                client_ver = getattr(client_info, "version", None) if client_info else None
                caps_dict = (
                    client_caps.model_dump()
                    if hasattr(client_caps, "model_dump")
                    else (client_caps if isinstance(client_caps, dict) else None)
                )
                if components.enabled("telemetry"):
                    from . import mcp_runtime_telemetry

                    mcp_runtime_telemetry.record_connection(
                        client_name=client_name,
                        client_version=client_ver,
                        protocol_version=proto,
                        client_capabilities=caps_dict,
                    )
            except Exception:
                pass
        if components.enabled("telemetry"):
            from . import mcp_progress

            with mcp_progress.progress_scope(elicit_ctx):
                return await elicitation.guarded_call(
                    sync_fn, kwargs, tool_name=sync_fn.__name__, elicit_ctx=elicit_ctx
                )
        return await elicitation.guarded_call(
            sync_fn, kwargs, tool_name=sync_fn.__name__, elicit_ctx=elicit_ctx
        )

    boundary.__annotations__ = {"elicit_ctx": Context | None}
    return boundary


# Name -> wrapped callable for every tool registered below. The web UI dispatches
# through this map so that an /api request traverses the *identical* policy gate and
# perf instrumentation as an MCP tool call. The alternative — a second hand-maintained
# dispatch table — is what tui._local_tool_registry() does today, and it has already
# drifted to fifteen names out of ~100. The map lives in ``tool_surface`` so that
# workflows, workers and the skill catalog reach it without importing this module.
REGISTERED_TOOLS = tool_surface.REGISTERED_TOOLS
tool_surface.install_guard(_guarded_and_instrumented)


# Each component declares its tools in its manifest (src/unified_agent/components/,
# task #2449). The registry is their concatenation: component order from
# docs/architecture/components.toml, then manifest order. There is no central
# list of tool names; add a tool with one line in its component's manifest.
for fn in components.tools():
    # Every tool call is evaluated against the Policy Engine DSL (task #156)
    # and sampled for performance (task 64 phase B). Unit tests import and
    # call bare functions directly, so this wrapper only engages for MCP calls.
    _wrapped = tool_surface.register(fn)
    mcp.tool(annotations=_tool_annotations(fn))(_with_elicitation(_wrapped))

# A hub that starts with a tool missing from TOOLSET_MEMBERS must fail here,
# not only in pytest. Filtered manifests would otherwise hide the new tool.
# A process that loads only some components (UA_COMPONENTS, K4b) is the one
# exception: the toolsets of the components it did not load are expected to be
# missing, and every tool it did register still has to be assigned.
# Unknown UA_TOOLSETS is not checked at import: doctor, init, and audit must
# still dispatch. The server-start branch of main(), the hub lifespan, and
# /healthz refuse a bad value before a transport is healthy.
toolsets.require_complete_assignments(REGISTERED_TOOLS, complete=not components.partial())

# Profile gating (task #1112): only trusted profiles get crew_intervene.
operational_profiles.STEWARD_DENIED_TOOLS = operational_profiles.STEWARD_DENIED_TOOLS | frozenset(
    {"crew_intervene"}
)


def _allowed_tool_names() -> frozenset[str]:
    """Tools the active profile may call. Toolset filtering does not apply."""
    toolsets.require_complete_assignments(REGISTERED_TOOLS, complete=not components.partial())
    return frozenset(operational_profiles.allowed_tools(set(REGISTERED_TOOLS)))


def manifest_tool_names() -> frozenset[str]:
    """Names ``tools/list`` returns: profile allow-list, then toolset filter.

    Re-checks the toolset table against the live registry so a tool registered
    after import cannot disappear from a filtered manifest without an error.
    """
    return toolsets.visible_in_manifest(_allowed_tool_names())


def callable_tool_names() -> frozenset[str]:
    """Tools this session can still invoke, including names hidden from the list."""
    return _allowed_tool_names()


def _schema_payloads(names: frozenset[str]) -> list[dict]:
    return [
        {"name": info.name, "inputSchema": info.parameters}
        for info in mcp._tool_manager.list_tools()
        if info.name in names
    ]


def client_schema_fingerprint() -> str:
    """Fingerprint every tool this session can call, not only the listed subset.

    A toolset can hide ``vault_write`` from ``tools/list`` while the profile
    still allows the call. The drift guard must see that schema. Compare
    ``manifest_schema_fingerprint`` to a hash of ``tools/list``.
    """
    from . import tool_schema_fingerprint

    return tool_schema_fingerprint.fingerprint_from_tool_list(
        _schema_payloads(callable_tool_names())
    )


def manifest_schema_fingerprint() -> str:
    """Fingerprint the tools ``tools/list`` shows this session."""
    from . import tool_schema_fingerprint

    return tool_schema_fingerprint.fingerprint_from_tool_list(
        _schema_payloads(manifest_tool_names())
    )


def registry_schema_fingerprint() -> str:
    """Fingerprint every registered tool, ignoring profile and toolset filters."""
    from . import tool_schema_fingerprint

    return tool_schema_fingerprint.compute_schema_fingerprint(REGISTERED_TOOLS)


async def _list_tools_for_profile(ctx, params: PaginatedRequestParams):
    """Filter the MCP manifest by profile, then by the selected toolsets.

    Profile membership is the authorization boundary. ``UA_TOOLSETS`` only
    drops names from this listing. A name hidden here stays callable through
    ``_guarded_and_instrumented`` when the active profile allows it, and a
    toolset selection cannot add a name the profile omitted. Call permission
    remains a separate policy decision after profile visibility.
    """
    tools = await mcp.list_tools()
    visible = manifest_tool_names()
    return ListToolsResult(tools=[tool for tool in tools if tool.name in visible])


# MCPServer installs its default list handler during construction. Replace only
# that protocol handler; direct in-process tests and the registry remain intact.
mcp._lowlevel_server.add_request_handler(
    "tools/list", PaginatedRequestParams, _list_tools_for_profile
)


async def _list_resources_for_profile(ctx, params: PaginatedRequestParams):
    """Hide live-state resources the session's profile cannot read via tools."""
    resources = await mcp.list_resources()
    return ListResourcesResult(
        resources=[
            resource for resource in resources if mcp_live_state.resource_visible(str(resource.uri))
        ]
    )


async def _list_resource_templates_for_profile(ctx, params: PaginatedRequestParams):
    templates = await mcp.list_resource_templates()
    return ListResourceTemplatesResult(
        resource_templates=[
            template
            for template in templates
            if mcp_live_state.resource_visible(template.uri_template)
        ]
    )


mcp_live_state.register_resources(mcp)
mcp._lowlevel_server.add_request_handler(
    "resources/list", PaginatedRequestParams, _list_resources_for_profile
)
mcp._lowlevel_server.add_request_handler(
    "resources/templates/list", PaginatedRequestParams, _list_resource_templates_for_profile
)
# Typed refusals (conflict, approval_required) carry their code as structuredContent (#2054).
tool_errors.install(mcp)


_ANON_LOG_INTERVAL_S = 60.0
_last_anon_log = 0.0

# Paths that must stay reachable without a credential even under UA_ENFORCE_AUTH=1.
# This is a short list on purpose, and none of these expose cluster state:
#   /healthz      - liveness probe; returns {"ok": true} and nothing else.
#   /readyz       - readiness probe; write+read of the live store and WAL sanity.
#                   503 when the store is missing or read-only. No org, token, or path.
#   /api/session  - the login route itself. Gating it behind auth is a deadlock:
#                   you would need a session to create a session.
#   /ui, /ui/*    - the static shell and its two assets. They contain no data; every
#                   byte the page renders comes from /api, which IS gated. Serving the
#                   login form to an anonymous browser is the entire point.
#   /favicon.ico  - requested automatically by every browser that loads /ui, with no
#                   say from the page. Left non-public it 401s and, worse, lands in the
#                   anonymous-request gauge — the one instrument meant to answer "is a
#                   client still calling without a token?" before enforcing auth. A
#                   browser fetching its tab icon is not that client, and a gauge that
#                   counts it reports work that does not exist. Public here means it
#                   falls through to a plain 404.
#   /             - exact root only (task #1400). It has no route of its own and isn't
#                   a prefix match, so it used to fall through all the way to the 401
#                   gate on every unauthenticated hit — a browser just opening the
#                   hub's URL got "Unauthorized" instead of the login page. The route
#                   registered for it (see `wrapped_http_app`) only ever redirects to
#                   /ui, so making it public leaks nothing; "/foo" is still gated.
_PUBLIC_PATH_PREFIXES = ("/ui",)


def _control_center_public_paths() -> tuple[str, ...]:
    """Control Center sign-in paths, or none when that component is not loaded."""
    workos_signin = _webui_workos()
    return tuple(workos_signin.PUBLIC_PATHS) if workos_signin else ()


_PUBLIC_PATHS = frozenset(
    {
        "/",
        "/healthz",
        "/readyz",
        "/api/session",
        "/api/slack/events",
        "/favicon.ico",
        # Control Center WorkOS sign-in (task #2114): the options probe, the
        # AuthKit redirect and its callback. Each checks its own credential.
        *_control_center_public_paths(),
    }
)


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_PATHS:
        return True
    if any(path == p or path.startswith(p + "/") for p in _PUBLIC_PATH_PREFIXES):
        return True
    oauth = _oauth()
    return bool(oauth and oauth.get_provider() and oauth.is_oauth_public_path(path))


def _is_public_slice_path(path: str) -> bool:
    """The only paths a Host in UA_HTTP_PUBLIC_HOSTS may reach: MCP + OAuth (task #859).

    Everything else — /ui, /api/*, /healthz, /favicon.ico — 404s for a publicly
    exposed Host so a Tailscale Funnel misconfiguration cannot surface the
    control-center UI or its data API on the public internet. Includes
    authenticated OAuth verify paths such as ``/oauth/whoami`` (task #870).
    """
    if path == "/mcp" or path.startswith("/mcp/"):
        return True
    oauth = _oauth()
    return bool(oauth and oauth.get_provider() and oauth.is_oauth_slice_path(path))


def _record_anonymous_request(path: str, headers) -> None:
    """Count unauthenticated hub requests onto the event log, at most once a minute.

    Before this, the only trace of an anonymous request was a stderr warning nobody
    reads — which meant "is anything still calling the hub without a token?" was
    unanswerable, and so flipping UA_ENFORCE_AUTH=1 was a leap rather than a decision.
    Rate-limited because an unauthenticated client in a retry loop must not be able to
    flood event_log.
    """
    global _last_anon_log
    import time

    now = time.monotonic()
    if now - _last_anon_log < _ANON_LOG_INTERVAL_S:
        return
    _last_anon_log = now
    try:
        from . import events

        events.record_event(
            "hub.auth.anonymous",
            payload={"path": path, "user_agent": (headers.get("user-agent") or "")[:120]},
            originator=None,
        )
    except Exception:
        pass


async def _deny_agent_http(scope, receive, send, *, human_recovery=False, identity=None) -> bool:
    from starlette.responses import JSONResponse

    path = scope.get("path", "")
    if path == "/mcp" or _is_public_path(path):
        return False  # Tool/resource gates govern MCP; public login/health stays reachable.
    read_only = scope.get("method", "GET") in {"GET", "HEAD", "OPTIONS"}
    cloud_identity_read = (
        config.is_cloud_hosted_identity(identity)
        and read_only
        and path in {"/api/whoami", "/oauth/whoami"}
    )
    platform_read = read_only and (
        path == "/api/platform/manifest" or path.startswith("/api/platform/objects/")
    )
    routine_fire_http = (
        scope.get("method") == "POST"
        and path.startswith("/api/routines/")
        and path.endswith("/fire")
        and path.removeprefix("/api/routines/").removesuffix("/fire").isdigit()
    )
    operation = (
        "platform_object:http"
        if platform_read
        else "routine_fire"
        if routine_fire_http
        else "llm_broker_completion"
        if path == "/api/llm/v1/chat/completions" and scope.get("method") == "POST"
        else path
    )
    if human_recovery or cloud_identity_read:
        operation = "profile_status"  # Permit human UI recovery; tool gates still apply.
    try:
        agent_access.enforce(
            operation,
            read_only=read_only,
            production=not (
                platform_read
                or cloud_identity_read
                or routine_fire_http
                or path.startswith(("/api/tasks", "/api/queue-health", "/ui"))
            ),
            human_recovery=human_recovery,
        )
    except agent_access.AgentAccessDenied as exc:
        await JSONResponse({"error": str(exc)}, status_code=403)(scope, receive, send)
        return True
    return False


async def _send_json_error(send, status: int, message: str) -> None:
    import json

    body = json.dumps({"error": message}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _serve_session_credential(app, scope, receive, send, token: str, headers) -> None:
    """Serve one request carrying a session-scoped credential (task #1744).

    A ``uas_`` bearer never falls through to the bearer, OAuth or WorkOS legs:
    it passes every check in ``session_credentials.verify`` or the request is
    refused here (403 off ``/mcp``, 401 otherwise). On success the request runs
    as the session's label, narrowed to its grant, with the session's
    attribution bound for gateway decisions and evidence.
    """
    from starlette.concurrency import run_in_threadpool

    from . import session_credentials

    if scope["type"] != "http":
        # The MCP transport is plain HTTP; a session credential opens nothing else.
        await send({"type": "websocket.close", "code": 1008})
        return
    path = scope.get("path", "")
    client = scope.get("client")
    peer_ip = client[0] if isinstance(client, (list, tuple)) and client else None
    public_hosts = config.http_public_hosts()
    req_host = (headers.get("host") or "").lower().split(":", 1)[0]
    if tailnet.is_funnel_request(headers) or req_host in public_hosts:
        # A session credential is for tailnet nodes only — never a public
        # Host, and never a request tailscaled marked as Funnel traffic.
        await _send_json_error(send, 401, "Unauthorized: session credential check failed")
        return
    try:
        verified = await run_in_threadpool(
            lambda: session_credentials.verify(token, path=path, peer_ip=peer_ip)
        )
    except session_credentials.SessionCredentialError as exc:
        status = 403 if exc.status == 403 else 401
        message = (
            "session credentials are valid on /mcp only"
            if status == 403
            else "Unauthorized: invalid, expired or revoked session credential"
        )
        await _send_json_error(send, status, message)
        return
    except Exception:  # noqa: BLE001 - fail closed on any verification fault
        logger.warning("session credential verification failed", exc_info=False)
        await _send_json_error(send, 401, "Unauthorized: session credential check failed")
        return

    subject = verified.subject
    mapped_workspace = config.resolve_workspace_binding(subject)
    token_token = config.agent_identity_var.set(subject)
    auth_metadata_token = config.auth_metadata_var.set(dict(verified.auth_metadata))
    grant_token = operational_profiles.session_grant_var.set(verified.grant)
    episode_token = config.episode_id_var.set(gateway.session_episode_id(verified.grant.item_id))
    peer_device_token = config.peer_device_var.set(verified.peer_device)
    session_id = headers.get("mcp-session-id")
    session_token = config.mcp_session_id_var.set(session_id)
    ws_token = (
        config.workspace_root_var.set(mapped_workspace) if mapped_workspace is not None else None
    )
    scope["ua_identity"] = subject
    scope["ua_identity_via"] = "acp_session"
    scope["ua_device"] = verified.peer_device
    try:
        if components.enabled("telemetry"):
            from . import mcp_runtime_telemetry

            mcp_runtime_telemetry.record_session_touch(session_id=session_id, agent_label=subject)
    except Exception:  # noqa: BLE001 - telemetry never fails a request
        pass
    try:
        registry.touch_agent(subject, via="hub_request")
    except Exception:  # noqa: BLE001
        pass
    host_token = source_host.bind(subject)  # task #1633; nothing can raise before try
    try:
        await app(scope, receive, send)
    finally:
        config.agent_identity_var.reset(token_token)
        config.auth_metadata_var.reset(auth_metadata_token)
        operational_profiles.session_grant_var.reset(grant_token)
        config.episode_id_var.reset(episode_token)
        config.peer_device_var.reset(peer_device_token)
        config.mcp_session_id_var.reset(session_token)
        source_host.reset(host_token)
        if ws_token is not None:
            config.workspace_root_var.reset(ws_token)


#: Credential legs whose verified identity is an agent credential: refused per
#: request once the label is retired (task #2315), and bound to the caller's
#: one open self-pickup run on /mcp (task #2313). A web-UI cookie minted from an
#: agent bearer is an agent credential too (:func:`_is_agent_cookie`); only a
#: verified human sign-in (WorkOS, or the operator's own label) is exempt, so
#: an operator cannot lock out its own recovery path.
_AGENT_CREDENTIAL_LEGS = frozenset({"bearer", "oauth"})


def _is_agent_cookie(identity_via: str | None, workos_claims: Any, label: str | None) -> bool:
    """A cookie session exchanged from an agent bearer (review of #2315).

    WorkOS-established cookies are verified per request as a human; a cookie
    for the operator's own label is the operator's. Every other cookie was
    minted from an agent bearer and is held to that agent's lifecycle.
    """
    return (
        identity_via == "cookie"
        and workos_claims is None
        and bool(label)
        and not config.is_human_identity(str(label))
    )


def _is_mcp_path(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


async def _send_refusal(scope, send, status: int, message: str, code: str) -> None:
    if scope["type"] != "http":
        await send({"type": "websocket.close", "code": 1008 if status == 403 else 1013})
        return
    import json

    body = json.dumps({"error": message, "code": code}).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    if status == 503:
        headers.append((b"retry-after", b"5"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _refuse_retired_identity(scope, receive, send, identity: str, via: str) -> bool:
    """Refuse a retired agent credential with a typed 403 (task #2315). True when refused.

    ``agent_lifecycle.refuse_retired_identity`` raises ``AgentRetiredError``;
    it is answered as ``403 {"error", "code": "agent_retired"}``, not the
    generic 401, so a client can tell retirement from a bad token. When the
    lifecycle state cannot be read the request fails closed with a temporary
    ``503 {"code": "lifecycle_unavailable"}`` (review of #2315). An allowed
    call is answered from the lifecycle cache without a thread hop or a read
    when the cache is fresh, and never writes.
    """
    from . import agent_lifecycle

    fresh, state = agent_lifecycle.cached_identity_state(identity)
    if fresh and state is None:
        return False
    from starlette.concurrency import run_in_threadpool

    try:
        await run_in_threadpool(
            functools.partial(
                agent_lifecycle.refuse_retired_identity, identity, site=f"hub.auth.{via}"
            )
        )
    except agent_lifecycle.AgentRetiredError as exc:
        await _send_refusal(scope, send, 403, str(exc), exc.reason)
        return True
    except agent_lifecycle.LifecycleUnavailableError as exc:
        await _send_refusal(scope, send, 503, str(exc), exc.reason)
        return True
    except Exception:  # noqa: BLE001 - fail closed: an unexpected fault is not an allow
        logger.warning("retired-identity check failed for %s", identity, exc_info=False)
        await _send_refusal(
            scope,
            send,
            503,
            "the hub cannot check this agent's lifecycle state right now; retry shortly",
            agent_lifecycle.LifecycleUnavailableError.reason,
        )
        return True
    return False


class HubAuthASGIMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        from starlette.datastructures import Headers

        headers = Headers(scope=scope)
        auth_header = headers.get("authorization")
        path = scope.get("path", "")

        # A Host reachable from the public internet (Tailscale Funnel, task #859)
        # is served the MCP + OAuth endpoints only; everything else 404s here,
        # before auth resolution, so a Funnel misconfiguration cannot expose the
        # control-center UI or /api. No-op when UA_HTTP_PUBLIC_HOSTS is unset.
        #
        # tailscaled's Tailscale-Funnel-Request marker (task #1892, round 3) is
        # the same signal made explicit, and the stronger one: Funnel's TLS
        # terminates on SNI, so a public client can send Host: localhost — or a
        # matching X-Forwarded-Host — and slip past the Host check below. The
        # marker is stripped from clients and re-set by tailscaled only for
        # Funnel traffic, so its presence forces the slice regardless of what
        # Host claims. It can only restrict, so it is honoured on every
        # listener — which also closes the same hole on the TCP proxy path,
        # where the client Host always survived intact.
        funnel = tailnet.is_funnel_request(headers)
        public_hosts = config.http_public_hosts()
        if funnel or public_hosts:
            req_host = (headers.get("host") or "").lower().split(":", 1)[0]
            if (funnel or req_host in public_hosts) and not _is_public_slice_path(path):
                body = b'{"error":"not found"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 404,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return

        # Session-scoped credential (task #1744). Routed by prefix before any
        # other credential leg, so a `uas_` value can never be tried as a static
        # bearer, an OAuth access token or a WorkOS session.
        if auth_header and auth_header.lower().startswith("bearer "):
            from . import session_credentials

            presented = auth_header[7:].strip()
            if session_credentials.is_session_token(presented):
                await _serve_session_credential(self.app, scope, receive, send, presented, headers)
                return

        tokens = config.agent_tokens()
        oauth = _oauth()
        oauth_provider = oauth.get_provider() if oauth else None
        # Optional third credential leg (task #1202): a WorkOS AuthKit session
        # for a human identity. `None` unless the operator enabled it, in which
        # case the hub is authenticated even with no bearer tokens configured.
        workos_hub_auth = _workos_hub_auth()
        hub_workos = workos_hub_auth.hub_auth() if workos_hub_auth else None
        # Key off whether the operator *configured* the WorkOS leg, not whether
        # it built. A leg that fails to build (bad credential, provider import
        # error) would otherwise drop a WorkOS-only hub onto the anonymous
        # path — a fail-open (Devin review, PR #677). When no credential leg
        # is configured at all, enforcement must still refuse the request:
        # an unreadable token file used to parse as {} and be served
        # anonymously with UA_ENFORCE_AUTH=1. Warn mode (explicit 0) keeps
        # today's anonymous serve.
        if (
            not tokens
            and oauth_provider is None
            and not (workos_hub_auth is not None and workos_hub_auth.enabled())
        ):
            if config.enforce_auth():
                # Public routes remain public when enforcement is enabled but
                # no credential provider is configured. The public-host filter
                # above still limits which of these routes are externally
                # reachable.
                if _is_public_path(path):
                    if not await _deny_agent_http(scope, receive, send):
                        await self.app(scope, receive, send)
                    return
                _record_anonymous_request(path, headers)
                import json

                body = json.dumps(
                    {"error": "Unauthorized: invalid or missing bearer token"}
                ).encode("utf-8")
                resp_headers = [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("utf-8")),
                ]
                challenge = oauth.www_authenticate() if oauth else None
                if challenge:
                    resp_headers.append((b"www-authenticate", challenge.encode("utf-8")))
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": resp_headers,
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
            if not await _deny_agent_http(scope, receive, send):
                await self.app(scope, receive, send)
            return

        token = None
        if auth_header and auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

        verified_agent = None
        identity_via = None
        auth_metadata = None
        workos_bound_token = None
        workos_session_claims = None
        if token and token in tokens:
            candidate = tokens[token]
            if config.is_known_agent_label(candidate):
                verified_agent = candidate
                identity_via = "bearer"
                auth_metadata = {"auth_method": "bearer", "scopes": []}
        if not verified_agent and token and oauth_provider is not None:
            access = await oauth_provider.load_access_token(token)
            if access is not None:
                candidate = access.subject or config.mcp_oauth_agent()
                if config.is_known_agent_label(candidate):
                    verified_agent = candidate
                    identity_via = "oauth"
                    claims = access.claims or {}
                    auth_metadata = {
                        "auth_method": claims.get("auth_method", "oauth"),
                        "iat": claims.get("iat"),
                        "scopes": list(access.scopes),
                    }
                    if claims.get("device_node"):
                        # Device-flow tokens carry the device they were issued
                        # to (task #1320): a tailnet node, or, with no tailnet
                        # peer, the login device-key thumbprint.
                        auth_metadata["device_node"] = claims["device_node"]
                    if isinstance(claims.get("signin_id"), str):
                        # The device sign-in family (task #1744): a random id,
                        # not a credential. Session credentials bind to it.
                        auth_metadata["signin_id"] = claims["signin_id"]
                    if isinstance(claims.get("human_approval"), dict):
                        # Who approved this token's sign-in and how (task
                        # #1322): WorkOS / tailnet+passkey are human-verified,
                        # the passphrase fallback is not.
                        auth_metadata["human_approval"] = claims["human_approval"]

        # WorkOS leg. A sealed AuthKit session arrives under its own
        # `Authorization: WorkOS <session>` scheme rather than a cookie, so it
        # is usable on /mcp without reopening the cross-site hole the
        # bearer-only rule below exists to close. Verification is the pilot's:
        # JWKS, exact issuer, the one configured organization, `sid`/`sub`
        # agreement, a live membership re-check and live session state on every
        # request — so a revocation takes effect on the next call. WorkOS roles
        # and email domains grant nothing; only the reviewed subject-to-label
        # mapping does.
        if not verified_agent and hub_workos is not None and workos_hub_auth is not None:
            sealed = workos_hub_auth.sealed_session_from_header(auth_header)
            if sealed:
                from starlette.concurrency import run_in_threadpool

                workos_session = await run_in_threadpool(hub_workos.resolve, sealed)
                if workos_session and config.is_known_agent_label(workos_session["identity"]):
                    verified_agent = workos_session["identity"]
                    identity_via = "workos"
                    # One dict, one sentinel: HubAuthASGIMiddleware binds both
                    # through bind_workos_request_auth at the contextvar site
                    # below. mark_workos_request_bound stays inside that helper.
                    workos_session_claims = workos_session

        # Cookie leg for the web UI, scoped to /ui and /api only. /mcp stays
        # bearer-only on purpose: a cross-site request riding the cookie must never
        # be able to reach a tool call.
        webui_auth = _webui_auth()
        webui_workos = _webui_workos()
        cookie_leg = bool(webui_auth and webui_auth.cookie_paths_cover(path))
        if not verified_agent and cookie_leg:
            cookie_header = headers.get("cookie")
            if cookie_header:
                from http.cookies import SimpleCookie

                jar = SimpleCookie()
                try:
                    jar.load(cookie_header)
                except Exception:
                    jar = SimpleCookie()
                morsel = jar.get(webui_auth.COOKIE_NAME)
                session = webui_auth.resolve_session(morsel.value) if morsel else None
                if session:
                    # A session minted by the WorkOS sign-in (task #2114) is
                    # re-verified here: live membership on every write and at
                    # least once a minute on reads, refreshing the access
                    # token as needed. Verified, it binds exactly like the
                    # WorkOS header leg, so approvals and evidence see an
                    # IdP-verified human. Denied, the request is anonymous.
                    from starlette.concurrency import run_in_threadpool

                    outcome, ui_workos = await run_in_threadpool(
                        webui_workos.verify_ui_session,
                        morsel.value,
                        identity=session["agent"],
                        method=str(scope.get("method") or "GET").upper(),
                    )
                    if outcome == webui_workos.DENIED:
                        session = None
                    elif outcome == webui_workos.VERIFIED and ui_workos:
                        workos_session_claims = ui_workos
                if session:
                    verified_agent = session["agent"]
                    identity_via = "cookie"
                    scope["ua_csrf"] = session.get("csrf")

        enforce = config.enforce_auth()

        if not verified_agent and _is_public_path(path):
            # Unauthenticated, but this path is meant to be. Don't count it as an
            # anonymous hub request either — the login page loading is not the signal
            # the UA_ENFORCE_AUTH rollout gauge is trying to measure.
            scope["ua_identity"] = None
            scope["ua_identity_via"] = None
            await self.app(scope, receive, send)
            return

        if not verified_agent:
            _record_anonymous_request(path, headers)
            if enforce:
                import json

                body = json.dumps(
                    {"error": "Unauthorized: invalid or missing bearer token"}
                ).encode("utf-8")
                headers = [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("utf-8")),
                ]
                challenge = oauth.www_authenticate() if oauth else None
                if challenge:
                    headers.append((b"www-authenticate", challenge.encode("utf-8")))
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": headers,
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": body,
                    }
                )
                return
            else:
                import sys

                print("ua-audit: [Warning] Unauthenticated access to HTTP hub", file=sys.stderr)

        # One spelling per identity (review of #2315): a case variant the open
        # roster accepts is the catalog label it names, for auth, lifecycle,
        # attribution and revocation alike. WorkOS binds its own mapped label.
        agent_cookie = _is_agent_cookie(identity_via, workos_session_claims, verified_agent)
        if verified_agent and (identity_via in _AGENT_CREDENTIAL_LEGS or agent_cookie):
            verified_agent = config.canonical_identity(verified_agent)

        # A retired label's agent credential (an env bearer the database revoke
        # cannot reach, a device token, or a cookie exchanged from either) is
        # refused on every request (#2315).
        if (
            verified_agent
            and (identity_via in _AGENT_CREDENTIAL_LEGS or agent_cookie)
            and await _refuse_retired_identity(scope, receive, send, verified_agent, identity_via)
        ):
            return

        # Cloud-hosted identities (config.CLOUD_HOSTED_IDENTITIES, including
        # cloud_agent13 and cloud_agent8) are MCP-only clients. Tool allowlists do not protect
        # every Control Center route, so refuse those routes before any handler
        # can mutate or read outside its approved tool surface. Future routes
        # stay closed too; other identities retain their existing HTTP behavior.
        # Keyed on the cloud-hosted roster as well as the cloud profiles, so the
        # chief-of-staff default (task #1799) does not open the Control Center.
        if operational_profiles.cloud_hosted_identity_default(verified_agent):
            cloud_read = scope.get("method", "GET") in {"GET", "HEAD"}
            if path != "/mcp" and not (
                cloud_read and path in {"/healthz", "/api/whoami", "/oauth/whoami"}
            ):
                from starlette.responses import JSONResponse

                await JSONResponse(
                    {"error": "cloud-assistant access is limited to its approved MCP tools"},
                    status_code=403,
                )(scope, receive, send)
                return

        if await _deny_agent_http(
            scope,
            receive,
            send,
            identity=verified_agent,
            human_recovery=agent_access.is_operator_recovery(verified_agent, identity_via),
        ):
            return

        # Resolve the workspace mapping BEFORE setting any ContextVar: if this
        # ever raised, doing it after agent_identity_var.set() would leave that
        # var set with no guaranteed reset (the failure would occur before the
        # try/finally below). Doing it first means a failure here just never
        # sets anything, rather than setting-without-resetting.
        mapped_workspace = (
            config.resolve_workspace_binding(verified_agent) if verified_agent else None
        )

        # Tailnet device provenance (task #1250, E3): once the request is known
        # to be authenticated, resolve the peer's CGNAT address to its Tailscale
        # node identity via local tailscaled. Only authenticated requests carry
        # device provenance; loopback/non-tailnet/unresolvable peers fail closed
        # to None. Runs in a threadpool — whois is a local subprocess.
        peer_device = None
        if verified_agent:
            client = scope.get("client")
            peer_ip = client[0] if isinstance(client, (list, tuple)) and client else None
            if peer_ip:
                from starlette.concurrency import run_in_threadpool

                peer_device = await run_in_threadpool(tailnet.resolve_peer_device, peer_ip)

        # Set contextvars: identity always; workspace only if this identity has
        # one configured (UA_AGENT_WORKSPACES) — otherwise fall through to the
        # process-wide UA_WORKSPACE env var, unchanged from today's behavior.
        if workos_session_claims is not None:
            token_token, workos_bound_token, auth_metadata_token = config.bind_workos_request_auth(
                identity=workos_session_claims["identity"],
                subject=workos_session_claims["subject"],
                organization_id=workos_session_claims["organization_id"],
                session_id=workos_session_claims["session_id"],
                expires_at=workos_session_claims["expires_at"],
            )
        else:
            token_token = config.agent_identity_var.set(verified_agent)
            auth_metadata_token = config.auth_metadata_var.set(auth_metadata)
        peer_device_token = config.peer_device_var.set(peer_device)
        session_id = headers.get("mcp-session-id")
        session_token = config.mcp_session_id_var.set(session_id)
        ws_token = None
        host_token = None
        episode_token = None
        try:
            # A telemetry failure must unwind the same request context as a
            # downstream handler failure (task #2219).
            if components.enabled("telemetry") and (verified_agent or session_id):
                # Liveness is touch_agent below. Recording the agent label as a host
                # made every identity a Devices row (task #1387).
                from . import mcp_runtime_telemetry

                mcp_runtime_telemetry.record_session_touch(
                    session_id=session_id,
                    agent_label=verified_agent,
                )
            ws_token = (
                config.workspace_root_var.set(mapped_workspace)
                if mapped_workspace is not None
                else None
            )

            # Expose the resolved identity to the route layer. /ui and /api need to know
            # *whether* the caller authenticated (and how) without re-parsing credentials,
            # and the ContextVar is not reachable from a Starlette handler's request object.
            scope["ua_identity"] = verified_agent
            scope["ua_identity_via"] = identity_via
            scope["ua_device"] = peer_device

            # An authenticated hub request is the strongest liveness signal in the system:
            # a cryptographically attributed fact, not an inference from a row someone
            # wrote. Throttled inside touch_agent, and never allowed to fail a request.
            if verified_agent:
                try:
                    registry.touch_agent(verified_agent, via="hub_request")
                except Exception:
                    pass

            # Bind only once setup succeeds; telemetry failures still unwind
            # identity while leaving any enclosing host binding untouched.
            host_token = source_host.bind(verified_agent)
            # An interactive MCP request is bound to the caller's one open
            # self-pickup run (task #2313), so its denials and evidence carry
            # ``run:<id>``. One indexed read per request; no run, no episode.
            if verified_agent and identity_via in _AGENT_CREDENTIAL_LEGS and _is_mcp_path(path):
                from starlette.concurrency import run_in_threadpool

                episode = await run_in_threadpool(gateway.bearer_episode_id, verified_agent)
                if episode is not None:
                    episode_token = config.episode_id_var.set(episode)
            await self.app(scope, receive, send)
        finally:
            if episode_token is not None:
                config.episode_id_var.reset(episode_token)
            config.agent_identity_var.reset(token_token)
            config.auth_metadata_var.reset(auth_metadata_token)
            config.peer_device_var.reset(peer_device_token)
            config.mcp_session_id_var.reset(session_token)
            if host_token is not None:
                source_host.reset(host_token)
            if workos_bound_token is not None:
                config.clear_workos_request_bound(workos_bound_token)
            if ws_token is not None:
                config.workspace_root_var.reset(ws_token)


# One host (optionally :port) as tailscaled writes it in X-Forwarded-Host:
# a dotted name or bracketed IPv6 literal. A comma list, whitespace, userinfo
# or path characters mean the value did not come from the trusted proxy's own
# header rewrite, so it must not steer Host-based routing.
_TRUSTED_FORWARDED_HOST_RE = re.compile(
    r"(?:[0-9A-Za-z](?:[0-9A-Za-z-]{0,61}[0-9A-Za-z])?"
    r"(?:\.[0-9A-Za-z](?:[0-9A-Za-z-]{0,61}[0-9A-Za-z])?)*"
    r"|\[[0-9A-Fa-f:]+\])"
    r"(?::[0-9]{1,5})?"
)


def _trusted_forwarded_host(raw: bytes) -> str | None:
    """A single well-formed ``X-Forwarded-Host`` value, else ``None`` (task #1892)."""
    from urllib.parse import urlsplit

    try:
        value = raw.decode("latin-1").strip()
    except UnicodeDecodeError:
        return None
    if not _TRUSTED_FORWARDED_HOST_RE.fullmatch(value):
        return None
    try:
        # Accessing .port is what validates it: the regex already rejected
        # non-numeric ports; this rejects out-of-range ones.
        _ = urlsplit(f"//{value}").port
    except ValueError:
        return None
    return value


class TrustedListenerProxyHeaders:
    """Scope ``X-Forwarded-*`` trust to the listener a request arrived on (task #1892).

    Uvicorn's ``proxy_headers`` decides trust by the *connecting* address, which
    cannot tell a trusted local proxy from any other loopback process: with the
    default ``forwarded_allow_ips=127.0.0.1`` every hub-host process could send
    ``X-Forwarded-For`` and pass device provenance (#1250) and the session
    credential device check (#1744) as an arbitrary tailnet node. Trust is
    scoped to the listener instead, so a forwarded claim is only ever as strong
    as the channel that carried it:

    * ``trusted_socket_path`` — connections on this Unix socket (``server[0]``
      in the ASGI scope; a TCP ``server`` tuple names an IP, never a path) get
      forwarded trust. The socket is bound owner-only (``0600``) by
      :func:`_bind_trusted_proxy_socket`, so reaching it already means running
      as the hub user or root — the level tailscaled needs to proxy Tailscale
      Serve onto it. Two socket-specific details:

      - ``scope["client"]`` is seeded to ``("127.0.0.1", 0)`` and the proxy
        middleware runs the ordinary allowlist, so the *rightmost untrusted*
        X-Forwarded-For hop wins — the hop the proxy itself appended. Uvicorn's
        ``always_trust`` (``"*"``) selection instead returns the leftmost hop,
        which a client could supply when a second proxy appends.
      - The socket also restores ``Host`` from ``X-Forwarded-Host``. Serve's
        ``unix:`` backend proxies as ``http://localhost``: the connection
        arrives with ``Host: localhost`` while the browser's host rides in
        ``X-Forwarded-Host``, a header tailscaled strips and re-sets itself,
        so its value is trustworthy on this listener alone. Exactly one
        well-formed value replaces the ``Host`` header; anything else is left
        as the proxy's own claim (``localhost``). That claim is *not* a
        fail-closed fallback for Host-based gates: tailscaled copies the
        client's own Host into ``X-Forwarded-Host`` and Funnel's TLS selects
        on SNI, so a public client can deliberately claim ``localhost``. What
        closes the ``UA_HTTP_PUBLIC_HOSTS`` slice anyway is tailscaled's
        ``Tailscale-Funnel-Request`` marker, which
        :class:`HubAuthASGIMiddleware` honours on every listener — see
        :func:`tailnet.is_funnel_request`.
    * ``tcp_forwarded_allow_ips`` — when set (``UA_HTTP_PROXY_HEADERS=1``), TCP
      peers in the list keep uvicorn's connecting-IP trust. An escape hatch for
      a hub behind a local reverse proxy; any allowed peer can still assert
      any client address, so prefer the socket. ``X-Forwarded-Host`` is never
      honoured here: a TCP forward keeps the real Host, so the header carries
      no trusted meaning.

    Everything else sees the real socket peer. Both inner wrappers are
    uvicorn's own ``ProxyHeadersMiddleware``, so the header parsing, chain
    handling and proto validation are identical to the behaviour this
    replaces — only the decision of *which* scopes may carry them is ours.
    """

    def __init__(self, app, *, trusted_socket_path=None, tcp_forwarded_allow_ips=None):
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        self._app = app
        self._trusted_socket_path = trusted_socket_path
        # The socket's only client is the local proxy, seeded as 127.0.0.1 in
        # __call__; the ordinary allowlist then makes the rightmost untrusted
        # XFF hop win rather than uvicorn's "*" leftmost selection.
        self._socket_app = (
            ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1") if trusted_socket_path else None
        )
        self._tcp_app = (
            ProxyHeadersMiddleware(app, trusted_hosts=tcp_forwarded_allow_ips)
            if tcp_forwarded_allow_ips
            else None
        )

    def _apply_socket_forwarded_host(self, scope) -> None:
        """Replace ``Host`` with the trusted proxy's ``X-Forwarded-Host`` (task #1892).

        Only ever called for scopes on the trusted socket. Exactly one
        well-formed value wins; a comma list, a malformed value or duplicate
        headers leave the proxy's own ``Host`` (``localhost``) untouched.
        """
        headers = scope.get("headers") or []
        forwarded = [v for n, v in headers if n.lower() == b"x-forwarded-host"]
        if len(forwarded) != 1:
            return
        host = _trusted_forwarded_host(forwarded[0])
        if host is None:
            return
        kept = [(n, v) for n, v in headers if n.lower() != b"host"]
        kept.append((b"host", host.encode("latin-1")))
        scope["headers"] = kept

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            server = scope.get("server")
            if (
                self._socket_app is not None
                and isinstance(server, (list, tuple))
                and len(server) >= 1
                and server[0] == self._trusted_socket_path
            ):
                self._apply_socket_forwarded_host(scope)
                scope["client"] = ("127.0.0.1", 0)
                return await self._socket_app(scope, receive, send)
            if self._tcp_app is not None:
                return await self._tcp_app(scope, receive, send)
        return await self._app(scope, receive, send)


# Wrap streamable_http_app to inject auth middleware
original_http_app = mcp.streamable_http_app


async def _redirect_root_to_ui(request):
    from starlette.responses import RedirectResponse

    return RedirectResponse(url="/ui", status_code=307)


def wrapped_http_app(*args, **kwargs):
    from starlette.routing import Route

    settings = _http_settings()
    kwargs.setdefault("host", settings["host"])
    if "transport_security" in settings:
        kwargs.setdefault("transport_security", settings["transport_security"])
    if "session_idle_timeout" in settings:
        kwargs.setdefault("session_idle_timeout", settings["session_idle_timeout"])
    app = original_http_app(*args, **kwargs)
    source_host.prime()  # UA_AGENT_HOSTS snapshot, read before any request (task #1633)
    # Append the web control center's routes to the real Starlette app BEFORE the
    # auth wrapper goes on, so /ui and /api sit behind exactly the same identity
    # plane as /mcp instead of growing a second one. MCPServer registers its own
    # /mcp route first, so it always wins; ours live under /ui and /api.
    webui = _webui()
    if webui is not None:
        app.router.routes.extend(webui.web_routes())
    else:
        # /healthz and /readyz are not Control Center routes: a kernel-only hub
        # must still answer a deployment health check (task #2603 review).
        app.router.routes.extend(_health_routes())
    oauth = _oauth()
    if oauth is not None:
        app.router.routes.extend(oauth.http_routes())
    # Exact "/" only (task #1400): a bare GET of the hub's root URL had no
    # route at all and fell through to the 401 auth gate. It's public in
    # _PUBLIC_PATHS above for the same reason /ui is — this route only ever
    # redirects there, so unauthenticated is fine.
    app.router.routes.append(Route("/", _redirect_root_to_ui, methods=["GET", "HEAD"]))
    # Outermost first: forwarded headers are rewritten before auth and peer
    # resolution read scope["client"] (task #1892).
    proxy_socket = config.http_trusted_proxy_socket()
    return TrustedListenerProxyHeaders(
        HubAuthASGIMiddleware(app),
        trusted_socket_path=str(proxy_socket) if proxy_socket is not None else None,
        tcp_forwarded_allow_ips=(
            config.http_forwarded_allow_ips() if config.http_proxy_headers() else None
        ),
    )


mcp.streamable_http_app = wrapped_http_app


_event_bus_server = None


def _start_embedded_event_bus() -> None:
    """Host the event bus inside the hub process.

    The bus has existed since #157 but nothing ever ran it, so `publish()` has been
    returning False for every event the cluster has produced. Embedding it here is
    what turns the socket fan-out on; the durable event_log was already being written.

    Never fatal: a hub that refuses to start because a socket would not bind is a
    worse outcome than a hub with no live fan-out, and every publish path already
    treats a dead bus as a no-op.
    """
    global _event_bus_server
    if not (config.event_bus_enabled() and config.event_bus_embed()):
        return
    try:
        from .events import EventBusServer

        server = EventBusServer()
        server.start()
        _event_bus_server = server
    except Exception as exc:  # noqa: BLE001 - reported, never propagated
        import sys

        print(f"ua-audit: [Warning] embedded event bus unavailable: {exc}", file=sys.stderr)


def _check_trusted_proxy_socket_parent(parent) -> None:
    """Refuse a socket parent another local user could reach or swap (task #1892).

    The socket's ``0600`` mode only excludes non-owners if the directory it
    sits in does too: a group- or world-writable parent lets another local
    user rename or replace the socket, and a parent owned by a different user
    is outside the hub user's control entirely. A missing parent is created
    ``0700``; an existing one must be a hub-owned directory with no group or
    other write bit.
    """
    import stat

    if not parent.is_dir():
        if parent.is_symlink() or parent.exists():
            raise RuntimeError(
                f"UA_HTTP_PROXY_SOCKET parent {parent} exists and is not a directory"
            )
        parent.mkdir(mode=0o700, parents=True)
        # mkdir's mode is umask-filtered; the guard is the trust boundary, so
        # make it exact.
        os.chmod(parent, 0o700)
    info = parent.stat()
    if info.st_uid != os.geteuid():
        raise RuntimeError(
            f"UA_HTTP_PROXY_SOCKET parent {parent} is not owned by the hub user; "
            "refusing to bind the trusted-proxy socket there"
        )
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeError(
            f"UA_HTTP_PROXY_SOCKET parent {parent} is group- or world-writable; "
            "refusing to bind the trusted-proxy socket there"
        )


# Advisory locks on `<socket>.lock`, held open for the life of the hub
# process (task #2190). The kernel drops a flock when its holder exits, so a
# crashed hub never leaves a lock behind; keyed by socket path so a second
# bind of the same path in this process reuses the lock it already holds.
_PROXY_SOCKET_LOCKS: dict[str, int] = {}

# uvicorn's default accept backlog; create_server re-applies config.backlog.
_PROXY_SOCKET_BACKLOG = 2048


def _lock_trusted_proxy_socket(path) -> None:
    """Hold an exclusive advisory lock beside ``path`` or refuse to start (task #2190).

    The lock, not the probe, is what proves no other hub owns the path: a
    probe connect cannot tell a stale socket from a live one whose accept
    queue is full on macOS (both answer ``ECONNREFUSED``). The lock file sits
    in the parent directory already verified private and hub-owned.
    """
    import fcntl

    key = str(path)
    if key in _PROXY_SOCKET_LOCKS:
        return
    lock_path = f"{key}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise RuntimeError(
            f"UA_HTTP_PROXY_SOCKET {path} is locked by another process (another hub?); "
            "refusing to take it over. Stop that process or choose a different socket path"
        ) from exc
    _PROXY_SOCKET_LOCKS[key] = fd


def _refuse_live_proxy_socket(path) -> None:
    """Raise unless the existing socket at ``path`` is stale (task #2190).

    Defence in depth behind the advisory lock, for a listener that does not
    take the lock (an older hub build). Only a refused connect counts as
    stale: ``ECONNREFUSED`` is the file a crashed hub leaves behind, and
    ``ENOENT`` means it was removed in the meantime. A connect that succeeds
    is a live listener, and any other failure (a timeout, a permission
    error) cannot tell the two apart, so both refuse rather than steal it.
    """
    import errno
    import socket

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
    except OSError as exc:
        if exc.errno in (errno.ECONNREFUSED, errno.ENOENT):
            return
        raise RuntimeError(
            f"UA_HTTP_PROXY_SOCKET {path} exists and a probe connect failed "
            f"({exc.__class__.__name__}); refusing to replace a socket that may be live"
        ) from exc
    finally:
        probe.close()
    raise RuntimeError(
        f"UA_HTTP_PROXY_SOCKET {path} is in use by a live listener (another hub?); "
        "refusing to take it over. Stop that process or choose a different socket path"
    )


def _bind_trusted_proxy_socket(path):
    """Bind the owner-only Unix listener forwarded headers are trusted on (task #1892).

    Bound here, not via ``uvicorn.Config(uds=...)``: uvicorn chmods its socket
    ``0666``, which would hand the forwarded-header trust to every local user.
    ``0600`` is applied to the socket fd before it ever listens, so there is
    no window where it accepts at a laxer mode. ``os.fchmod`` lands on the
    socket's vnode on macOS/BSD; on Linux it silently no-ops on unix sockets,
    so the mode is verified on the fs node and ``chmod`` on the path is the
    fallback — safe because the parent directory is verified private and
    hub-owned first, which keeps the brief lax-mode window unreachable to
    other users.

    A path another hub owns is never taken over (task #2190), because that
    hub would silently lose its Serve traffic to this one. The hub holds an
    exclusive advisory lock on ``<path>.lock`` for its whole life, and only
    the lock holder may unlink a stale socket; the probe connect is a second
    guard against a listener that holds no lock. The socket listens before
    it is returned, so the window between bind and uvicorn's own ``listen``
    (the whole ASGI lifespan) never shows a bound socket as refused. A
    non-socket file at the path is an operator error and is never deleted.
    Tailscale Serve proxies through tailscaled, which runs as root, so the
    owner-only mode does not exclude the intended client.
    """
    import socket
    import stat

    _check_trusted_proxy_socket_parent(path.parent)
    _lock_trusted_proxy_socket(path)
    if path.is_symlink() or path.exists():
        if path.is_socket():
            _refuse_live_proxy_socket(path)
            path.unlink(missing_ok=True)
        else:
            raise RuntimeError(
                f"UA_HTTP_PROXY_SOCKET {path} exists and is not a socket; refusing to replace it"
            )
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
        try:
            os.fchmod(sock.fileno(), 0o600)
        except OSError:
            pass
        if stat.S_IMODE(os.stat(path).st_mode) != 0o600:
            os.chmod(path, 0o600)
        if stat.S_IMODE(os.stat(path).st_mode) != 0o600:
            raise RuntimeError(f"UA_HTTP_PROXY_SOCKET {path} could not be set to owner-only 0600")
        sock.listen(_PROXY_SOCKET_BACKLOG)
        sock.set_inheritable(True)
    except BaseException:
        sock.close()
        raise
    return sock


def _serve_http_hub() -> None:
    """Serve the streamable-HTTP hub with a bounded graceful shutdown (task #1812).

    Equivalent to ``mcp.run(transport="streamable-http", **_http_settings())``
    except for ``timeout_graceful_shutdown``: the SDK builds its uvicorn config
    without one, so on SIGTERM uvicorn waited forever for long-lived MCP
    streams and launchd SIGKILLed the hub on every restart, skipping cleanup.
    ``mcp.streamable_http_app()`` is the wrapped app (auth, /ui, /api, OAuth),
    and it applies the same ``_http_settings()`` host, transport security and
    session idle timeout that ``mcp.run`` would have passed.

    After a graceful shutdown uvicorn re-raises the captured SIGTERM under the
    handler that was installed before it started. With the default handler
    that kills the process on the spot and the caller's ``finally`` never
    runs, so SIGTERM is mapped to ``SystemExit(0)`` for the duration.
    """
    import signal

    import anyio
    import uvicorn

    settings = _http_settings()
    uvicorn_kwargs: dict = {
        "host": settings["host"],
        "port": settings["port"],
        "log_level": mcp.settings.log_level.lower(),
        "timeout_graceful_shutdown": config.http_graceful_shutdown_s(),
        # Forwarded-header trust is per-listener (task #1892):
        # TrustedListenerProxyHeaders inside wrapped_http_app decides which
        # sockets may supply X-Forwarded-*, so uvicorn's connecting-IP trust
        # stays off everywhere — including its loopback default.
        "proxy_headers": False,
    }
    # None would disable uvicorn logging. Omit the argument to keep the default.
    log_config = None
    if components.enabled("cloud"):
        from . import hosted_logging

        log_config = hosted_logging.uvicorn_log_config()
    if log_config is not None:
        uvicorn_kwargs["log_config"] = log_config
    uvicorn_config = uvicorn.Config(mcp.streamable_http_app(), **uvicorn_kwargs)

    # Bind every listener up front: the TCP socket through uvicorn's own binder
    # (same SO_REUSEADDR + startup-failure behaviour as serving without
    # sockets=), plus the owner-only Unix socket when UA_HTTP_PROXY_SOCKET names
    # one. Passing sockets= is what lets one Server accept on both under a
    # single signal handler and shutdown bound.
    sockets = [uvicorn_config.bind_socket()]
    proxy_socket = config.http_trusted_proxy_socket()
    if proxy_socket is not None:
        sockets.append(_bind_trusted_proxy_socket(proxy_socket))

    def _exit_after_shutdown(signum, frame):
        raise SystemExit(0)

    previous = signal.signal(signal.SIGTERM, _exit_after_shutdown)
    try:
        serve = uvicorn.Server(uvicorn_config).serve
        anyio.run(functools.partial(serve, sockets=sockets))
    finally:
        signal.signal(signal.SIGTERM, previous)


_MAIN_USAGE = """\
usage: unified-agent [--version] [--help] <command> [args]

With no command, starts the MCP server (stdio, or HTTP when UA_TRANSPORT=http).

commands:
  platform  audit  evidence  init  settings  doctor  verify  db
  agent  login  llm-proxy  node  work-product

Run `unified-agent <command> --help` for a command's options.
"""


def _print_version_or_help(argv: list[str]) -> bool:
    """Answer ``--version`` / ``--help`` without touching config or the task store.

    A hub-client host has ``NO_LOCAL_MEMORY_DB``, so reaching ``storage.init_db()``
    raises ``LiveStoreError``; these flags must exit 0 before that point (#2297).
    """
    if len(argv) != 2 or argv[1] not in ("--version", "-V", "--help", "-h"):
        return False
    if argv[1] in ("--version", "-V"):
        try:
            from importlib.metadata import version

            print(f"unified-agent {version('unified-agent')}")
        except Exception:
            print("unified-agent unknown")
    else:
        print(_MAIN_USAGE, end="")
    return True


def main() -> None:
    if _print_version_or_help(sys.argv):
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "platform":
        from .platform_cli import run_platform_cli

        sys.exit(run_platform_cli(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "audit":
        if len(sys.argv) > 2 and sys.argv[2] == "verify":
            from .audit.ledger_cli import run_verify_cli

            sys.exit(run_verify_cli(sys.argv[3:]))
        if len(sys.argv) > 2 and sys.argv[2] == "re-anchor":
            from .audit.ledger_cli import run_reanchor_cli

            sys.exit(run_reanchor_cli(sys.argv[3:]))
        if len(sys.argv) > 2 and sys.argv[2] == "evidence":
            from .audit.evidence_cli import run_evidence_cli

            sys.exit(run_evidence_cli(sys.argv[3:]))
        from .audit.cli import main as audit_main

        sys.exit(audit_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "evidence":
        from .audit.evidence_cli import run_evidence_cli

        sys.exit(run_evidence_cli(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "init":
        from .bootstrap import init_main

        sys.exit(init_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "settings":
        from .settings import run_settings_cli

        sys.exit(run_settings_cli(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        from .bootstrap import doctor_main

        sys.exit(doctor_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "verify":
        from .bootstrap import verify_main

        sys.exit(verify_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "db":
        from .db_cli import run_db_cli

        sys.exit(run_db_cli(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "agent":
        from .agent_cli import run_agent_cli

        sys.exit(run_agent_cli(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "login":
        from .device_login import main as login_main

        sys.exit(login_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "llm-proxy":
        from .llm_proxy import main as llm_proxy_main

        sys.exit(llm_proxy_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "node":
        from . import execution_locality

        if execution_locality.devices_only():
            # Task #2109: a node runs agents; run it on the device, not the hub host.
            print(
                f"ua: refusing to start: {execution_locality.refusal_message('unified-agent node')}",
                file=sys.stderr,
            )
            sys.exit(2)
        from .node_agent import main as node_main

        sys.exit(node_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "work-product":
        from .tools.work_product_cli import run_work_product_cli

        sys.exit(run_work_product_cli(sys.argv[2:]))

    # After every CLI subcommand has returned. An unknown UA_TOOLSETS must
    # not block doctor, init, or audit, and must fail before either transport.
    toolsets.require_known_toolsets()
    try:
        config.validate_deployment_mode()
    except config.DeploymentModeError as exc:
        print(f"ua: refusing to start: {exc}", file=sys.stderr)
        sys.exit(1)
    from . import execution_locality

    try:
        execution_locality.validate()
    except ValueError as exc:
        print(f"ua: refusing to start: {exc}", file=sys.stderr)
        sys.exit(1)
    # Task #2109: a devices-only hub refuses any agent launch in its own
    # process, whichever code path attempts it.
    execution_locality.install_process_guard()
    _require_node_queue_settings()
    config.require_configured_http_credentials()
    config.ensure_dirs()
    post_init.register_default_hooks()
    storage.init_db()
    if config.transport() == "http":
        # The code this hub runs, for system_doctor's hub_checkout_drift (#1771).
        hub_runtime.record_hub_start()
        # Start the bus before uvicorn: EventBusServer._bind() mutates the
        # process-global umask, which must not race anything else creating files.
        _start_embedded_event_bus()
        try:
            _serve_http_hub()
        finally:
            if _event_bus_server is not None:
                try:
                    _event_bus_server.stop()
                except Exception:
                    pass
    else:
        mcp.run()


if __name__ == "__main__":
    main()
