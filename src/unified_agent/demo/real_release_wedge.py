"""Real-release wedge demo: merge action → approval → signed evidence (task #1140).

Dogfoods the same shipped surfaces as the a11y compliance slice (#1131), but the
subject is a real unified-agent release SHA on ``main`` — not a synthetic WCAG
fixture. ``release_attest`` is an approval-queue label only (not an MCP tool).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, events, post_init, storage
from ..audit import evidence_report, producer_sign
from ..tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
    check_approval_gate,
)
from ..tools.tasks import task_add, task_claim, task_update

# Approval-queue label only — listed in UA_APPROVAL_TOOLS for this demo.
GATED_TOOL = "release_attest"

# The demo runs as the configured operator login (settings: operator.label).
DEFAULT_OPERATOR = config.operator_label()
DEFAULT_AGENT = "Desktop_Agent4"
DEFAULT_BEARER = "demo-compliance-bearer-task-1140-not-a-secret"
DEFAULT_RELEASE_SHA = "a4a9f801135c382846ece0ff88d1a0137f1f7641"


@dataclass(frozen=True)
class ReleaseAction:
    """A real merge/release the wedge attests (not a synthetic finding)."""

    sha: str
    subject: str
    merged_at: str
    pr: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def resolve_release_action(
    sha: str = DEFAULT_RELEASE_SHA, *, repo: Path | None = None
) -> ReleaseAction:
    """Read subject/date from git for ``sha`` (fail closed if unknown)."""
    root = repo or Path.cwd()
    try:
        subject = subprocess.check_output(
            ["git", "-C", str(root), "log", "-1", "--format=%s", sha],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        merged_at = subprocess.check_output(
            ["git", "-C", str(root), "log", "-1", "--format=%cI", sha],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ToolError(f"could not resolve release commit {sha}: {exc}") from exc
    if not subject:
        raise ToolError(f"empty subject for release commit {sha}")
    pr = None
    if "(#" in subject and subject.rstrip().endswith(")"):
        # Conventional "… (#1165)" trailer.
        try:
            pr = subject[subject.rindex("(#") + 2 : subject.rindex(")")]
        except ValueError:
            pr = None
    return ReleaseAction(sha=sha, subject=subject, merged_at=merged_at, pr=pr)


@dataclass
class AttestationProposal:
    release: ReleaseAction
    proposed_by: str
    artifact_relpath: str = "release-attestation.json"

    def approval_payload(self, task_id: int) -> dict[str, Any]:
        """Exact payload the approval gate matches (must include task_id)."""
        return {
            "task_id": task_id,
            "release_sha": self.release.sha,
            "subject": self.release.subject,
            "action": "attest_release",
            "artifact": self.artifact_relpath,
        }


@dataclass
class SliceResult:
    decision: Literal["approve", "deny"]
    task_id: int
    approval_id: int
    attestation_applied: bool
    evidence_path: str | None
    coverage_gate: str | None
    provenance_verified: bool
    actor_authentication_verified: bool
    denial_recorded: bool
    release: dict[str, Any]
    report_summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def configure_demo_env(
    *,
    ua_home: Path,
    workspace: Path,
    operator: str = DEFAULT_OPERATOR,
    bearer: str = DEFAULT_BEARER,
) -> None:
    """Point storage at an isolated home and gate ``release_attest``."""
    ua_home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ["UA_HOME"] = str(ua_home)
    os.environ["UA_WORKSPACE"] = str(workspace)
    os.environ["UA_STATE_SYNC"] = "0"
    os.environ["UA_APPROVAL_TOOLS"] = GATED_TOOL
    os.environ["UA_AGENT_TOKENS"] = f"{operator}:{bearer}"
    os.environ.pop("UA_OPERATOR_NAME", None)
    os.environ.pop("UA_HUMAN_IDENTITY", None)
    post_init.register_default_hooks()
    storage.init_db()


def write_attestation(workspace: Path, release: ReleaseAction, *, approval_id: int) -> Path:
    path = workspace / "release-attestation.json"
    body = {
        "kind": "release_attestation",
        "release": release.to_dict(),
        "approval_id": approval_id,
        "attested": True,
    }
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def propose_attestation(
    release: ReleaseAction,
    *,
    proposed_by: str = DEFAULT_AGENT,
) -> AttestationProposal:
    return AttestationProposal(release=release, proposed_by=proposed_by)


def _claim_without_worktree(task_id: int, agent: str) -> str | None:
    """Claim through ``task_claim`` so the run is ledgered; return its run id.

    Re-land of PR #866 (task #1681): a direct ``UPDATE tasks SET
    status='doing'`` left the demo's claim with no self-pickup run. The
    workspace is denied for this one call so the claim never mints a git
    worktree in a real checkout.
    """
    token = config.workspace_root_var.set(config.WORKSPACE_DENIED)
    try:
        return task_claim(task_id, agent)["current_run_id"]
    finally:
        config.workspace_root_var.reset(token)


def _create_release_task(proposal: AttestationProposal, *, agent: str) -> tuple[int, str | None]:
    rel = proposal.release
    notes = (
        f"[wedge-demo] [real-release] sha={rel.sha}\n"
        f"subject={rel.subject}\n"
        f"merged_at={rel.merged_at}\n"
        f"pr={rel.pr or ''}\n"
        f"proposed_by={proposal.proposed_by}\n"
        f"claimed-by: {agent}"
    )
    row = task_add(
        title=f"Release attest: {rel.sha[:7]}",
        notes=notes,
        priority="high",
        project="unified-agent",
        domain="professional",
        evidence_ref=rel.sha,
    )
    task_id = int(row["id"])
    return task_id, _claim_without_worktree(task_id, agent)


def _queue_attestation_approval(
    proposal: AttestationProposal,
    task_id: int,
    *,
    requested_by: str,
) -> int:
    payload = proposal.approval_payload(task_id)
    try:
        check_approval_gate(GATED_TOOL, payload, requested_by=requested_by)
    except ApprovalPendingError as exc:
        if exc.approval_id is not None:
            return int(exc.approval_id)
    pending = approval_pending(tool=GATED_TOOL)["pending"]
    if not pending:
        raise ToolError("expected a pending release_attest approval")
    return int(pending[0]["id"])


def _as_operator(operator: str):
    return config.agent_identity_var.set(operator)


def run_slice(
    *,
    workspace: Path,
    decision: Literal["approve", "deny"],
    release: ReleaseAction | None = None,
    operator: str = DEFAULT_OPERATOR,
    agent: str = DEFAULT_AGENT,
    reason: str | None = None,
    artifact_dir: Path | None = None,
    repo: Path | None = None,
) -> SliceResult:
    """Execute one end-to-end approve or deny pass for a real release SHA."""
    release = release or resolve_release_action(DEFAULT_RELEASE_SHA, repo=repo)
    proposal = propose_attestation(release, proposed_by=agent)
    task_id, run_id = _create_release_task(proposal, agent=agent)
    approval_id = _queue_attestation_approval(proposal, task_id, requested_by=agent)
    payload = proposal.approval_payload(task_id)

    # Record the merge/deploy action on the hash chain before the human hop.
    events.record_event(
        "agent.task.updated",
        payload={
            "task_id": task_id,
            "kind": "release_action",
            "release_sha": release.sha,
            "subject": release.subject,
            "host": config.current_source_host(),
        },
        originator=agent,
    )

    token = _as_operator(operator)
    try:
        if decision == "approve":
            approval_resolve(approval_id, "approve", resolved_by=operator)
        else:
            approval_resolve(
                approval_id,
                "deny",
                resolved_by=operator,
                reason=reason or "operator declined release attestation; fail closed",
            )
    finally:
        config.agent_identity_var.reset(token)

    attestation_applied = False
    denial_recorded = decision == "deny"
    evidence_path: str | None = None

    if decision == "approve":
        consumed = check_approval_gate(GATED_TOOL, payload, requested_by=agent)
        if not consumed.get("gated"):
            raise ToolError("approved attestation did not consume a gated pass")
        write_attestation(workspace, release, approval_id=approval_id)
        attestation_applied = True
        events.record_event(
            "agent.task.completed",
            payload={
                "task_id": task_id,
                "host": config.current_source_host(),
                "attestation": {
                    "release_sha": release.sha,
                    "applied": True,
                    "approval_id": approval_id,
                },
            },
            originator=agent,
        )
        task_update(
            task_id,
            status="done",
            evidence_ref=release.sha,
            append_note=(
                f"[attestation: applied @ {storage.now()}] "
                f"approval={approval_id} sha={release.sha[:12]}"
            ),
            worktree=False,
            actor=agent,
            expected_run_id=run_id,
        )
    else:
        try:
            check_approval_gate(GATED_TOOL, payload, requested_by=agent)
            raise ToolError("denied attestation unexpectedly passed the approval gate")
        except ToolError as exc:
            if "DENIED" not in str(exc):
                raise
        events.record_event(
            "agent.approval.denied",
            payload={
                "task_id": task_id,
                "approval_id": approval_id,
                "release_sha": release.sha,
                "attestation_applied": False,
            },
            originator=operator,
        )
        task_update(
            task_id,
            status="blocked",
            append_note=(
                f"[attestation: denied @ {storage.now()}] "
                f"approval={approval_id}; no attest artifact (fail-closed)"
            ),
            worktree=False,
            actor=agent,
            expected_run_id=run_id,
        )
        if (workspace / "release-attestation.json").exists():
            raise ToolError("fail-closed violated: attestation written after denial")

    report = evidence_report.build_evidence_report(task_id=task_id)
    coverage = report.get("attribution_coverage") or {}
    chain = report.get("chain_integrity") or {}
    records = report.get("records") or []
    rec = records[0] if records else {}
    approvals = (rec.get("action") or {}).get("approvals") or []

    if decision == "deny":
        if not any(a.get("status") == "denied" for a in approvals):
            raise ToolError("evidence report missing denied approval row")
        if attestation_applied:
            raise ToolError("denial path claimed attestation_applied")

    if coverage.get("gate") != "pass" and decision == "approve":
        raise ToolError(f"attribution coverage gate did not pass: {coverage!r}")

    summary = {
        "task_id": task_id,
        "decision": decision,
        "attestation_applied": attestation_applied,
        "release": release.to_dict(),
        "human": rec.get("human"),
        "human_provenance": rec.get("human_provenance"),
        "device": rec.get("device"),
        "agent": rec.get("agent"),
        "approvals": approvals,
        "proof": rec.get("proof"),
        "chain_integrity": chain,
        "attribution_coverage": coverage,
        "clean_pass": report.get("clean_pass"),
        "producer_channel": producer_sign.CHANNEL_PRODUCER_AUTHENTICATED,
        "producer_scheme": producer_sign.SCHEME_HMAC_SHA256_V1,
    }

    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        stem = f"real-release-{decision}-task-{task_id}"
        json_path = artifact_dir / f"{stem}.json"
        md_path = artifact_dir / f"{stem}.md"
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        md_path.write_text(
            evidence_report.format_evidence_report(report, fmt="markdown") + "\n",
            encoding="utf-8",
        )
        summary_path = artifact_dir / f"{stem}.summary.json"
        summary_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        evidence_path = str(json_path)

    return SliceResult(
        decision=decision,
        task_id=task_id,
        approval_id=approval_id,
        attestation_applied=attestation_applied,
        evidence_path=evidence_path,
        coverage_gate=coverage.get("gate"),
        provenance_verified=bool(chain.get("provenance_verified")),
        actor_authentication_verified=bool(chain.get("actor_authentication_verified")),
        denial_recorded=denial_recorded,
        release=release.to_dict(),
        report_summary=summary,
    )


def run_both_paths(
    *,
    root: Path,
    release: ReleaseAction | None = None,
    operator: str = DEFAULT_OPERATOR,
    agent: str = DEFAULT_AGENT,
    repo: Path | None = None,
) -> dict[str, Any]:
    """Run approve + deny paths into sibling isolated homes."""
    release = release or resolve_release_action(DEFAULT_RELEASE_SHA, repo=repo)
    results: dict[str, Any] = {
        "operator": operator,
        "agent": agent,
        "release": release.to_dict(),
        "paths": {},
    }
    for decision in ("approve", "deny"):
        home = root / f"ua_home_{decision}"
        workspace = root / f"workspace_{decision}"
        artifacts = root / "artifacts" / decision
        configure_demo_env(ua_home=home, workspace=workspace, operator=operator)
        result = run_slice(
            workspace=workspace,
            decision=decision,  # type: ignore[arg-type]
            release=release,
            operator=operator,
            agent=agent,
            artifact_dir=artifacts,
            reason="demo negative case: operator denied release attestation",
            repo=repo,
        )
        results["paths"][decision] = result.to_dict()
    return results


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    import tempfile

    parser = argparse.ArgumentParser(
        description=(
            "Real-release wedge demo (task #1140): attest a main commit through "
            "the approval gate with a signed evidence report."
        )
    )
    parser.add_argument("--decision", choices=("approve", "deny", "both"), default="both")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--sha", default=DEFAULT_RELEASE_SHA)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--operator", default=DEFAULT_OPERATOR)
    parser.add_argument("--agent", default=DEFAULT_AGENT)
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args(argv)

    root = args.out or Path(tempfile.mkdtemp(prefix="ua-real-release-wedge-"))
    root.mkdir(parents=True, exist_ok=True)
    release = resolve_release_action(args.sha, repo=args.repo)

    if args.decision == "both":
        result = run_both_paths(
            root=root,
            release=release,
            operator=args.operator,
            agent=args.agent,
            repo=args.repo,
        )
    else:
        home = root / "ua_home"
        workspace = root / "workspace"
        artifacts = root / "artifacts" / args.decision
        configure_demo_env(ua_home=home, workspace=workspace, operator=args.operator)
        slice_result = run_slice(
            workspace=workspace,
            decision=args.decision,
            release=release,
            operator=args.operator,
            agent=args.agent,
            artifact_dir=artifacts,
            reason="operator denied release attestation; fail closed",
            repo=args.repo,
        )
        result = {
            "operator": args.operator,
            "agent": args.agent,
            "release": release.to_dict(),
            "paths": {args.decision: slice_result.to_dict()},
        }

    result["root"] = str(root)
    transcript = root / "transcript.json"
    transcript.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"demo root: {root}")
    print(f"release: {release.sha[:12]} — {release.subject}")
    print(f"transcript: {transcript}")
    for name, path_result in result["paths"].items():
        print(
            f"  {name}: task={path_result['task_id']} "
            f"approval={path_result['approval_id']} "
            f"applied={path_result['attestation_applied']} "
            f"coverage={path_result.get('coverage_gate')} "
            f"provenance_verified={path_result['provenance_verified']} "
            f"evidence={path_result.get('evidence_path')}"
        )
    if args.print_json:
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
