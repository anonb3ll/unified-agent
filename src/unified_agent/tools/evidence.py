"""MCP evidence-report surface."""

from __future__ import annotations

from typing import Any, NotRequired

from typing_extensions import TypedDict

from ..audit.evidence_report import build_evidence_report


class ProjectEvidence(TypedDict):
    task_id: int
    evidence_ref: str | None
    pr_url: str | None
    commit_sha: str | None
    merge_verified: bool
    completed_at: str | None


class ProjectChain(TypedDict):
    project_id: int
    status: str
    task_ids: list[int]
    done_task_ids: list[int]
    evidence: list[ProjectEvidence]
    merged_prs: list[ProjectEvidence]
    last_merge: ProjectEvidence | None


class EvidenceReport(TypedDict):
    report_type: str
    schema_version: str
    generated_at: str
    chain_integrity: dict[str, Any]
    # Task #1265 (Epic F, F2): the external witness (if any) for
    # chain_integrity.head_hash — RFC-3161 TSA, signed git commit, or the
    # legacy vault note — plus a short third-party verification recipe.
    anchor: dict[str, Any]
    # Task #1267 (Epic F, F4): who attests to THIS report — the caller that
    # generated it, IdP-verified as human only via a live WorkOS session,
    # otherwise agent-bearer-only and never promoted to human — plus the
    # signing key id, generation time, and filter scope.
    attestation: dict[str, Any]
    filter: dict[str, Any]
    total_records: int
    records: list[dict[str, Any]]
    project_chain: ProjectChain | None
    # Task #1139 / #1140: declare coverage fields so FastMCP keeps them on the
    # wire. NotRequired preserves older fixture payloads in schema tests while
    # still surfacing the gate when build_evidence_report emits it.
    unattributed_excluded: NotRequired[list[int]]
    attribution_coverage: NotRequired[dict[str, Any]]
    gate_failures: NotRequired[list[str]]
    clean_pass: NotRequired[bool]
    # Task #1425: when the `project` stream filter is set, the project's
    # bound source repository (see `project_bindings.resolve`), so a reader
    # of `pr_url`/`commit_sha` evidence records knows which repo they should
    # belong to. Absent when `project` is unset.
    project_repo_binding: NotRequired[dict[str, Any] | None]


def evidence_report(
    *,
    task_id: int | None = None,
    project_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    agent: str | None = None,
    project: str | None = None,
    pr: str | None = None,
    limit: int | None = None,
    item: str | None = None,
) -> EvidenceReport:
    """Read attributable evidence, optionally for a complete project task subtree.

    `item` scopes the report to one node dispatch item, as `nd-…` or its
    `node:<node>:<item>` evidence_ref: that item's task, with proof events
    limited to the ones the item recorded (task #1989).

    Each record's `denials` lists the gateway denials bound by episode id to
    its runs (tool, rule, reason_code, redacted args_sha256). Its `status` is
    `recorded`, `partial` or `not-recorded`; only `recorded` (every run bound,
    no unbound deny by a participating agent in the window) lets an empty list
    mean zero denials (task #2309).
    """
    return build_evidence_report(
        task_id=task_id,
        project_id=project_id,
        since=since,
        until=until,
        agent=agent,
        project=project,
        pr=pr,
        limit=limit,
        item=item,
    )
