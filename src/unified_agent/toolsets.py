"""Named toolsets for the MCP tool manifest.

Every registered tool belongs to exactly one toolset in ``TOOLSET_MEMBERS``.
``UA_TOOLSETS`` (hub-wide, colon-separated) selects which of those toolsets
``tools/list`` returns. Unset or blank means every toolset, so the manifest
matches the registry. ``core`` is nine tools. ``profile_status`` and
``memory_search`` stay on every filtered manifest (``ALWAYS_LISTED``),
including ``UA_TOOLSETS=memory`` and ``UA_TOOLSETS=admin``. The selection
only hides names from the listing; it is not consulted when a tool is called.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from . import config

# Stable order. A new toolset needs a real tool that does not fit these.
TOOLSET_NAMES: tuple[str, ...] = (
    "core",
    "tasks",
    "memory",
    "vault",
    "git",
    "ops",
    "admin",
    "connectors",
)

# One table: toolset name -> member tool names. ``core`` is the claim → work →
# finish cycle plus the two tools startup guidance tells every client to call:
# ``profile_status`` and ``memory_search``.
TOOLSET_MEMBERS: dict[str, tuple[str, ...]] = {
    "core": (
        "fs_list",
        "fs_read",
        "fs_search",
        "fs_write",
        "memory_search",
        "profile_status",
        "task_claim",
        "task_list",
        "task_update",
    ),
    "tasks": (
        "synthetic_workflow",
        "autonomous_cycle_get",
        "autonomous_cycle_green_path_eligibility",
        "autonomous_cycle_kill",
        "autonomous_cycle_list",
        "autonomous_cycle_manage",
        "autonomous_cycle_record_usage",
        "autonomous_cycle_start",
        "autonomous_cycle_transition",
        "bug_worker_manage",
        "capability_catalog",
        "check_agent_capability",
        "coordination_manage",
        "delegate",
        "delegate_claim",
        "delegate_list",
        "delegate_release",
        "delegation_ledger",
        "delegation_manage",
        "delegation_spec_build",
        "delegation_spec_check",
        "delegation_spec_template",
        "dependency_add",
        "dependency_list",
        "dependency_remove",
        "dispatch_cancel",
        "dispatch_confirm_cancellation",
        "dispatch_delivery_receipt",
        "dispatch_delivery_receipts_list",
        "dispatch_report_self",
        "dispatch_resume",
        "dispatch_retry_cancellation",
        "evidence_report",
        "browser_derive_task_reference",
        "goal_manage",
        "graph_canvas_read",
        "graph_canvas_write",
        "handoff",
        "handoff_list",
        "langgraph_checkpoints",
        "langgraph_progress_note",
        "langgraph_run",
        "langgraph_workflow_run",
        "org_proposal",
        "plan_checkpoint",
        "plan_create",
        "plan_delete",
        "plan_get",
        "plan_handoff",
        "plan_list",
        "plan_manage",
        "plan_resume",
        "plan_step_claim",
        "plan_step_retry",
        "plan_update",
        "project_build",
        "review_and_notify",
        "routine_add",
        "routine_fire",
        "routine_list",
        "routine_pause",
        "scan_for_reviewable_tasks",
        "task_add",
        "task_comment",
        "task_delete",
        "work_product_submit",
        "workflow_list",
        "workflow_run",
    ),
    "memory": (
        "lifecycle_record",
        "memory_delete",
        "memory_feedback",
        "memory_get",
        "memory_list",
        "memory_promote",
        "memory_prune_candidates",
        "memory_restore",
        "memory_save",
        "propose_session_learnings",
        "propose_skill_clusters",
        "recall_maintenance",
        "session_packet",
    ),
    "vault": (
        "obsidian_active_note",
        "obsidian_append",
        "obsidian_open",
        "obsidian_search",
        "obsidian_status",
        "vault_archive_plan",
        "vault_briefing_digest",
        "vault_ingest_status",
        "vault_journal",
        "vault_links",
        "vault_list",
        "vault_move",
        "vault_query",
        "vault_read",
        "vault_search",
        "vault_to_memory_ingest",
        "vault_wiki_custodian",
        "vault_write",
    ),
    "git": (
        "cleanup_stale_worktrees",
        "git_commit_staged",
        "git_diff_staged",
        "git_merge_task",
        "git_worktree_add",
        "git_worktree_list",
        "git_worktree_remove",
    ),
    "ops": (
        "agent_activity",
        "agent_context",
        "agent_heartbeat",
        "agent_list",
        "audit_triage_tasks",
        "batch_notify",
        "check_anomalies",
        "check_anomalies_and_notify",
        "compare_migration_manifests",
        "dash_list",
        "dash_search",
        "dash_view",
        "detect_dev_servers",
        "event_log_query",
        "event_log_verify",
        "export_data",
        "export_journal",
        "export_memory",
        "export_migration_manifest",
        "export_tasks",
        "flush_digest",
        "format_render",
        "fs_index_symbols",
        "fs_list_symbols",
        "fs_repo_map",
        "fs_search_symbols",
        "import_migration_manifest",
        "injection_triage",
        "mcp_health_telemetry",
        "message_list",
        "message_send",
        "metrics_snapshot",
        "notify",
        "notify_delivery_status",
        "observability_manage",
        "perf_summary",
        "pi_run_task",
        "python_lint_and_format",
        "read_image",
        "red_team_qa",
        "relay_manage",
        "rollback_migration_import",
        "run_cmd",
        "run_cmd_check",
        "security_audit",
        "session_end",
        "session_list",
        "session_start",
        "skill_review",
        "state_sync_verify",
        "system_doctor",
        "web_fetch",
        "web_search",
    ),
    "admin": (
        "agent_token_issue",
        "agent_token_list",
        "agent_token_rotate",
        "approval_pending",
        "approval_resolve",
        "cloud_worker_recover",
        "config_revision_manage",
        "credential_pool_list",
        "credential_pool_register",
        "credential_pool_revoke",
        "crew_intervene",
        "crew_view",
        "deployment_claim",
        "managed_secret_set",
        "model_quota_status",
        "oauth_device_manage",
        "policy_authorization_list",
        "policy_authorization_record",
        "profile_activate",
        "profile_reset",
        "skill_registry",
    ),
    "connectors": (
        "gcloud_inventory",
        "gcloud_ops",
        "gemini_ask",
        "horizon2_connector_authorize",
        "horizon2_package_list",
        "horizon2_package_register",
        "horizon2_server_list",
        "horizon2_server_register",
        "horizon2_server_revoke",
        "mcp_gateway",
        "zulip_fetch_messages",
        "zulip_send_message",
    ),
}


class ToolsetAssignmentError(ValueError):
    """The toolset table assigns a tool twice or names an unknown toolset."""


class ToolsetConfigError(ValueError):
    """``UA_TOOLSETS`` names a toolset that is not in the table."""


def build_assignments(members: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """Map each tool name to its single toolset. Raises if a name appears twice."""
    assigned: dict[str, str] = {}
    duplicates: set[str] = set()
    for toolset, names in members.items():
        if toolset not in TOOLSET_NAMES:
            raise ToolsetAssignmentError(f"unknown toolset {toolset!r}")
        for name in names:
            if name in assigned:
                duplicates.add(name)
                continue
            assigned[name] = toolset
    if duplicates:
        listed = ", ".join(sorted(duplicates))
        raise ToolsetAssignmentError(f"tools assigned twice: {listed}")
    return assigned


ASSIGNMENTS: dict[str, str] = build_assignments(TOOLSET_MEMBERS)

# Startup guidance tells every client to call these. They stay assigned to
# ``core`` and are also unioned into every other toolset's manifest so a
# client on ``memory`` or ``admin`` can still discover them.
ALWAYS_LISTED: frozenset[str] = frozenset({"memory_search", "profile_status"})


def assignment_problems(registered: Iterable[str], *, complete: bool = True) -> list[str]:
    """Problems that mean a registered tool is unassigned or assigned twice.

    Also reports table entries that are not registered, so a renamed tool
    cannot linger in the table after it leaves the server. With
    ``complete=False`` -- a partial ``UA_COMPONENTS`` selection loads only some
    components (K4b, task #2603) -- an unregistered table entry is expected and
    is not a problem; the rest of the check still applies.
    """
    counts: dict[str, int] = {}
    for names in TOOLSET_MEMBERS.values():
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    problems: list[str] = []
    twice = sorted(name for name, count in counts.items() if count > 1)
    if twice:
        problems.append("assigned twice: " + ", ".join(twice))
    registered_names = set(registered)
    unassigned = sorted(registered_names - set(counts))
    if unassigned:
        problems.append("unassigned: " + ", ".join(unassigned))
    if complete:
        extra = sorted(set(counts) - registered_names)
        if extra:
            problems.append("not registered: " + ", ".join(extra))
    return problems


def require_complete_assignments(registered: Iterable[str], *, complete: bool = True) -> None:
    """Fail closed when the live registry and the toolset table disagree.

    Called when the server finishes registering tools, and again when it
    builds a manifest. A deploy that skips the assignment tests still
    refuses to advertise a partial toolset. ``complete=False`` is a process
    that loads only some components: the table's entries for the components it
    did not load are expected to be missing, and only a registered tool with no
    assignment (or a duplicate) fails.
    """
    problems = assignment_problems(registered, complete=complete)
    if problems:
        raise ToolsetAssignmentError("; ".join(problems))


def tools_in(toolset: str) -> frozenset[str]:
    """Tool names declared for one toolset."""
    if toolset not in TOOLSET_MEMBERS:
        raise ToolsetConfigError(f"unknown toolset {toolset!r}")
    return frozenset(TOOLSET_MEMBERS[toolset])


def selected_toolsets() -> frozenset[str] | None:
    """Toolsets ``UA_TOOLSETS`` selects, or None when the manifest is unfiltered."""
    names = config.configured_toolsets()
    if names is None:
        return None
    unknown = sorted(names - set(TOOLSET_NAMES))
    if unknown:
        listed = ", ".join(unknown)
        raise ToolsetConfigError(f"UA_TOOLSETS has unknown toolsets: {listed}")
    return names


def require_known_toolsets() -> None:
    """Refuse startup when ``UA_TOOLSETS`` names a toolset that is not in the table.

    ``selected_toolsets`` raises on an unknown name, but only once a manifest
    is built. ``main`` calls this on the server-start branch, before either
    transport runs. The HTTP lifespan and ``/healthz`` call it again, so a
    bad value cannot pass a health check before the first ``tools/list``.
    Diagnostic commands (``doctor``, ``init``, ``audit``) return before this
    check and still run.
    """
    selected_toolsets()


def visible_in_manifest(names: Iterable[str]) -> frozenset[str]:
    """Subset of ``names`` the current toolset selection lists.

    Returns ``names`` unchanged when ``UA_TOOLSETS`` is unset. Never adds a
    name that was not already in ``names``.
    """
    pool = frozenset(names)
    selected = selected_toolsets()
    if selected is None:
        return pool
    listed = {name for name in pool if ASSIGNMENTS.get(name) in selected}
    listed.update(ALWAYS_LISTED & pool)
    return frozenset(listed)
