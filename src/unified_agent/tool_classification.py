"""Canonical machine-readable classification of hub-served tools (task #1200, C1).

Classifies every MCP tool served by the hub into exactly one of three categories:
- REACHABLE: intended to be exposed to operator clients and reached by the 120-tool
  baseline operator allowlist (~/.hermes/config.yaml).
- WITHHELD: deliberately not exposed to operator clients (red-tier / high-impact
  operations such as destructive deletion, credential mutation, secret updates,
  migration import/egress, external connector grants, or active intervention).
- OMITTED: accidental gaps where a tool is hub-served and intended for operator
  visibility, but was omitted from the client allowlist (e.g. system_doctor, exports,
  event_log_verify, task_claim, langgraph_*, mcp_health_telemetry, crew_view).

Derives the served tool inventory dynamically from the server tool registry
(server.REGISTERED_TOOLS), making silent drift impossible: new tools cannot land
unclassified without failing the drift check and CI test.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class ToolExposureClass(StrEnum):
    """Classification of an MCP tool's exposure to operator clients."""

    REACHABLE = "REACHABLE"
    WITHHELD = "WITHHELD"
    OMITTED = "OMITTED"


# Operator-reachable tools. The original 120-tool baseline was measured
# 2026-09-18; new reviewed tools are added here as they land.
BASELINE_OPERATOR_REACHABLE_TOOLS: frozenset[str] = frozenset(
    {
        "agent_activity",
        "agent_context",
        "agent_heartbeat",
        "agent_list",
        "audit_triage_tasks",
        "autonomous_cycle_get",
        "autonomous_cycle_kill",
        "autonomous_cycle_list",
        "autonomous_cycle_manage",
        "autonomous_cycle_record_usage",
        "autonomous_cycle_start",
        "autonomous_cycle_transition",
        "batch_notify",
        "bug_worker_manage",
        "capability_catalog",
        "check_agent_capability",
        "cleanup_stale_worktrees",
        "coordination_manage",
        "dash_list",
        "dash_search",
        "dash_view",
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
        "detect_dev_servers",
        "event_log_query",
        "evidence_report",
        "flush_digest",
        "format_render",
        "fs_index_symbols",
        "fs_list",
        "fs_list_symbols",
        "fs_read",
        "fs_repo_map",
        "fs_search",
        "fs_search_symbols",
        "fs_write",
        "gemini_ask",
        "git_commit_staged",
        "git_diff_staged",
        "git_merge_task",
        "git_worktree_add",
        "git_worktree_list",
        "git_worktree_remove",
        "graph_canvas_read",
        "graph_canvas_write",
        "handoff",
        "handoff_list",
        "injection_triage",
        "memory_feedback",
        "memory_get",
        "memory_list",
        "memory_prune_candidates",
        "memory_restore",
        "memory_save",
        "memory_search",
        "message_list",
        "message_send",
        "metrics_snapshot",
        "notify",
        "pi_run_task",
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
        "profile_activate",
        "profile_reset",
        "profile_status",
        "project_build",
        "propose_session_learnings",
        "propose_skill_clusters",
        "read_image",
        "review_and_notify",
        "routine_add",
        "routine_fire",
        "routine_list",
        "routine_pause",
        "rollback_migration_import",
        "run_cmd",
        "run_cmd_check",
        "scan_for_reviewable_tasks",
        "security_audit",
        "session_end",
        "session_list",
        "session_start",
        "skill_review",
        "task_add",
        "task_list",
        "task_update",
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
        "web_fetch",
        "web_search",
        "workflow_list",
        "workflow_run",
        "zulip_fetch_messages",
        "zulip_send_message",
    }
)

# 25 tools deliberately withheld from operator clients: red-tier / high-impact operations
# that require human approval, CSRF-gated Web UI endpoints, or are reserved for developer lanes.
WITHHELD_TOOLS: frozenset[str] = frozenset(
    {
        "synthetic_workflow",  # opt-in disposable acceptance; no live provider
        "agent_token_issue",
        "agent_token_rotate",
        "approval_resolve",
        # Config commit/rollback (task #1352). Reads are on the same tool;
        # the annotation follows the write. Chief-of-staff can call it; the
        # operator client allowlist does not.
        "config_revision_manage",
        "credential_pool_register",
        "credential_pool_revoke",
        "crew_intervene",
        "deployment_claim",
        "dispatch_report_self",
        "export_data",
        "gcloud_ops",
        "horizon2_connector_authorize",
        "horizon2_package_register",
        "horizon2_server_register",
        "horizon2_server_revoke",
        "import_migration_manifest",
        "langgraph_workflow_run",
        "managed_secret_set",
        # Hub-held-credential egress to downstream MCP servers (task #1336):
        # steward and chief-of-staff only, off by default (UA_MCP_GATEWAY).
        "mcp_gateway",
        "memory_delete",
        # Device sign-in revoke (task #1321): credential revocation, approval-gated.
        "oauth_device_manage",
        # Policy-authorization write (task #1798): mints a signed record that
        # can end a merged head's ledger outcome. Developer-lane authority
        # (UA_POLICY_AUTHORIZERS), not an operator-client surface.
        "policy_authorization_record",
        # Public-reply relay (task #1734): outbound speech on an external channel.
        # Every send consumes a human approval; not an operator-client surface.
        "relay_manage",
        "task_delete",
        # Work-product submit (task #1278): off by default (UA_WORK_PRODUCT_
        # ENABLED), always parks a require_approval request, and is pruned from
        # the default profile like the agent_token_* / managed_secret_set trio
        # above — same "human/operator-gated write" class.
        "work_product_submit",
    }
)

# 51 tools that are hub-served, intended for operator clients, but omitted from the
# baseline allowlist. These represent accidental gaps to be closed by updating client
# configs following the operator's decision-packet approval. (task #1183 adds
# notify_delivery_status and dispatch_delivery_receipts_list here — new read-only
# operator status surfaces not yet wired into ~/.hermes/config.yaml.)
OMITTED_TOOLS: frozenset[str] = frozenset(
    {
        "agent_token_list",
        "approval_pending",
        "autonomous_cycle_green_path_eligibility",
        "check_anomalies",
        "check_anomalies_and_notify",
        "cloud_worker_recover",
        "compare_migration_manifests",
        "credential_pool_list",
        "crew_view",
        "dispatch_cancel",
        "dispatch_confirm_cancellation",
        "dispatch_delivery_receipt",
        "dispatch_delivery_receipts_list",
        "dispatch_resume",
        "dispatch_retry_cancellation",
        "event_log_verify",
        "browser_derive_task_reference",
        "export_journal",
        "export_memory",
        "export_migration_manifest",
        "export_tasks",
        "gcloud_inventory",
        "goal_manage",
        "horizon2_package_list",
        "horizon2_server_list",
        "langgraph_checkpoints",
        "langgraph_progress_note",
        "langgraph_run",
        "lifecycle_record",
        "mcp_health_telemetry",
        "memory_promote",
        "model_quota_status",
        "notify_delivery_status",
        "observability_manage",
        "obsidian_active_note",
        "obsidian_append",
        "obsidian_open",
        "obsidian_search",
        "obsidian_status",
        "org_proposal",
        "perf_summary",
        "policy_authorization_list",
        "python_lint_and_format",
        "recall_maintenance",
        "red_team_qa",
        "session_packet",
        "skill_registry",
        "state_sync_verify",
        "system_doctor",
        "task_claim",
        "task_comment",
    }
)


def get_served_tools() -> frozenset[str]:
    """Return the set of tool names registered and served by the hub server."""
    from . import server

    return frozenset(server.REGISTERED_TOOLS.keys())


def classify_tool(tool_name: str) -> ToolExposureClass | None:
    """Return the classification for a given tool name, or None if unclassified."""
    if tool_name in BASELINE_OPERATOR_REACHABLE_TOOLS:
        return ToolExposureClass.REACHABLE
    if tool_name in WITHHELD_TOOLS:
        return ToolExposureClass.WITHHELD
    if tool_name in OMITTED_TOOLS:
        return ToolExposureClass.OMITTED
    return None


@dataclass(frozen=True)
class ClassificationReport:
    """Summary and inventory of tool classifications."""

    served_count: int
    reachable_count: int
    withheld_count: int
    omitted_count: int
    unclassified_count: int
    reachable: list[str]
    withheld: list[str]
    omitted: list[str]
    unclassified: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Return a structured dictionary suitable for JSON serialization."""
        tools_dict = {name: ToolExposureClass.REACHABLE.value for name in self.reachable}
        tools_dict.update({name: ToolExposureClass.WITHHELD.value for name in self.withheld})
        tools_dict.update({name: ToolExposureClass.OMITTED.value for name in self.omitted})
        for name in self.unclassified:
            tools_dict[name] = "UNCLASSIFIED"

        return {
            "served_count": self.served_count,
            "counts": {
                "REACHABLE": self.reachable_count,
                "WITHHELD": self.withheld_count,
                "OMITTED": self.omitted_count,
                "UNCLASSIFIED": self.unclassified_count,
            },
            "tools": tools_dict,
        }

    def to_markdown_table(self) -> str:
        """Render the complete classification as a GitHub-flavored Markdown table."""
        lines = [
            f"# Tool Classification Inventory ({self.served_count} served tools)",
            "",
            f"**Counts:** {self.reachable_count} REACHABLE · "
            f"{self.withheld_count} WITHHELD · {self.omitted_count} OMITTED · "
            f"{self.unclassified_count} UNCLASSIFIED",
            "",
            "| Tool | Classification | Category |",
            "|---|---|---|",
        ]
        all_sorted = sorted(
            [
                (t, ToolExposureClass.REACHABLE.value, "Baseline Operator Allowlist")
                for t in self.reachable
            ]
            + [
                (t, ToolExposureClass.WITHHELD.value, "Deliberate Withhold (Red-Tier)")
                for t in self.withheld
            ]
            + [
                (t, ToolExposureClass.OMITTED.value, "Omitted Gap (Pending Approval)")
                for t in self.omitted
            ]
            + [(t, "UNCLASSIFIED", "Unclassified Drift") for t in self.unclassified],
            key=lambda x: x[0],
        )
        for tool, cls, cat in all_sorted:
            lines.append(f"| `{tool}` | `{cls}` | {cat} |")
        return "\n".join(lines)


@dataclass(frozen=True)
class DriftReport:
    """Report on tool classification drift against registered server tools."""

    passed: bool
    served_count: int
    unclassified_tools: list[str]
    stale_tools: list[str]
    overlapping_tools: list[str]
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_all(served_tools: Iterable[str] | None = None) -> ClassificationReport:
    """Classify all served tools into REACHABLE, WITHHELD, OMITTED, or UNCLASSIFIED."""
    tools = frozenset(served_tools) if served_tools is not None else get_served_tools()

    reachable = sorted(tools & BASELINE_OPERATOR_REACHABLE_TOOLS)
    withheld = sorted(tools & WITHHELD_TOOLS)
    omitted = sorted(tools & OMITTED_TOOLS)
    unclassified = sorted(
        tools - (BASELINE_OPERATOR_REACHABLE_TOOLS | WITHHELD_TOOLS | OMITTED_TOOLS)
    )

    return ClassificationReport(
        served_count=len(tools),
        reachable_count=len(reachable),
        withheld_count=len(withheld),
        omitted_count=len(omitted),
        unclassified_count=len(unclassified),
        reachable=reachable,
        withheld=withheld,
        omitted=omitted,
        unclassified=unclassified,
    )


def check_classification_drift(served_tools: Iterable[str] | None = None) -> DriftReport:
    """Check for classification drift against the registered server tools.

    Fails when:
    - Any served tool is unclassified.
    - Any tool named in classification sets is no longer served (stale entry).
    - Any tool appears in more than one category (overlap).
    """
    tools = frozenset(served_tools) if served_tools is not None else get_served_tools()
    all_classified = BASELINE_OPERATOR_REACHABLE_TOOLS | WITHHELD_TOOLS | OMITTED_TOOLS

    unclassified = sorted(tools - all_classified)
    stale = sorted(all_classified - tools)

    # Overlaps between sets
    reach_with = BASELINE_OPERATOR_REACHABLE_TOOLS & WITHHELD_TOOLS
    reach_omit = BASELINE_OPERATOR_REACHABLE_TOOLS & OMITTED_TOOLS
    with_omit = WITHHELD_TOOLS & OMITTED_TOOLS
    overlapping = sorted(reach_with | reach_omit | with_omit)

    errors: list[str] = []
    if unclassified:
        errors.append(
            f"Found {len(unclassified)} unclassified served tools: {', '.join(unclassified)}. "
            "Every tool served by the hub must be classified into REACHABLE, WITHHELD, or OMITTED."
        )
    if stale:
        errors.append(
            f"Found {len(stale)} stale tools in classification sets that are not served by hub: {', '.join(stale)}. "
            "Remove or rename tools that no longer exist in server.REGISTERED_TOOLS."
        )
    if overlapping:
        errors.append(
            f"Found {len(overlapping)} tools appearing in multiple classification sets: {', '.join(overlapping)}. "
            "Tool classification sets must be strictly mutually disjoint."
        )

    passed = len(errors) == 0
    return DriftReport(
        passed=passed,
        served_count=len(tools),
        unclassified_tools=unclassified,
        stale_tools=stale,
        overlapping_tools=overlapping,
        errors=errors,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for classification inspection and drift checks."""
    parser = argparse.ArgumentParser(
        description="Unified-Agent MCP Tool Allowlist Classification and Drift Checker"
    )
    parser.add_argument("--json", action="store_true", help="Emit classification report as JSON")
    parser.add_argument(
        "--markdown", action="store_true", help="Emit classification report as Markdown table"
    )
    parser.add_argument(
        "--check", action="store_true", help="Perform drift check and exit non-zero on failure"
    )

    args = parser.parse_args(argv)

    if args.check:
        drift = check_classification_drift()
        if not drift.passed:
            print("ERROR: Tool classification drift detected!", file=sys.stderr)
            for err in drift.errors:
                print(f"  - {err}", file=sys.stderr)
            return 1
        report = classify_all()
        print(
            f"PASS: 0 drift across {report.served_count} served tools: "
            f"{report.reachable_count} REACHABLE, {report.withheld_count} WITHHELD, {report.omitted_count} OMITTED."
        )
        return 0

    report = classify_all()
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    elif args.markdown:
        print(report.to_markdown_table())
    else:
        print(
            f"Tool Classification: {report.served_count} served tools\n"
            f"  - REACHABLE:    {report.reachable_count}\n"
            f"  - WITHHELD:     {report.withheld_count}\n"
            f"  - OMITTED:      {report.omitted_count}\n"
            f"  - UNCLASSIFIED: {report.unclassified_count}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
