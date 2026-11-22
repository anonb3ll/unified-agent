"""Compliance wedge demo: accessibility remediation with signed evidence (task #1131).

Regulated-services buyer story — finding → agent-proposed fix → human approval
→ signed evidence report — reusing the shipped approval gate, producer HMAC
signing, ``evidence_report``, and ``compute_attribution_coverage``.

This is not a new vertical or connector. The gated tool name
``a11y_remediate`` exists only as an approval-queue label; it is not an MCP
tool registration.
"""

from __future__ import annotations

import json
import os
import re
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
GATED_TOOL = "a11y_remediate"

# The demo runs as the configured operator login (settings: operator.label).
DEFAULT_OPERATOR = config.operator_label()
DEFAULT_AGENT = "Desktop_Agent4"
DEFAULT_BEARER = "demo-compliance-bearer-task-1131-not-a-secret"

FIXTURE_BEFORE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Compliance demo fixture</title></head>
<body>
  <header>
    <!-- WCAG 1.1.1: image missing accessible name -->
    <img id="hero-logo" src="/logo.png" width="120" height="40">
  </header>
</body>
</html>
"""

_ALT_RX = re.compile(
    r'(<img\b[^>]*\bid=["\']hero-logo["\'][^>]*)(>)',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AccessibilityFinding:
    """A single WCAG finding the agent proposes to remediate."""

    finding_id: str
    rule: str
    severity: str
    target: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


DEFAULT_FINDING = AccessibilityFinding(
    finding_id="a11y-img-alt-001",
    rule="WCAG 2.2 Success Criterion 1.1.1 (Non-text Content)",
    severity="serious",
    target="fixture.html#hero-logo",
    message="Image missing accessible name (alt text)",
)


@dataclass
class RemediationProposal:
    finding: AccessibilityFinding
    proposed_by: str
    patch: dict[str, str]
    fixture_relpath: str = "fixture.html"

    def approval_payload(self, task_id: int) -> dict[str, Any]:
        """Exact payload the approval gate matches (must include task_id)."""
        return {
            "task_id": task_id,
            "finding_id": self.finding.finding_id,
            "rule": self.finding.rule,
            "target": self.finding.target,
            "patch": self.patch,
            "fixture": self.fixture_relpath,
        }


@dataclass
class SliceResult:
    """Outcome of one approve or deny run of the demo slice."""

    decision: Literal["approve", "deny"]
    task_id: int
    approval_id: int
    remediation_applied: bool
    evidence_path: str | None
    coverage_gate: str | None
    provenance_verified: bool
    actor_authentication_verified: bool
    denial_recorded: bool
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
    """Point storage at an isolated home and gate ``a11y_remediate``."""
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


def write_fixture(workspace: Path, content: str = FIXTURE_BEFORE) -> Path:
    path = workspace / "fixture.html"
    path.write_text(content, encoding="utf-8")
    return path


def apply_alt_text_remediation(fixture: Path, alt: str = "Company logo") -> bool:
    """Apply the proposed alt-text fix. Returns True when the file changed."""
    text = fixture.read_text(encoding="utf-8")
    if f'alt="{alt}"' in text or f"alt='{alt}'" in text:
        return False

    def _inject(match: re.Match[str]) -> str:
        tag = match.group(1)
        if re.search(r"\balt\s*=", tag, re.IGNORECASE):
            tag = re.sub(r'\balt\s*=\s*["\'][^"\']*["\']', f'alt="{alt}"', tag, count=1)
            return tag + match.group(2)
        return f'{tag} alt="{alt}"{match.group(2)}'

    updated, n = _ALT_RX.subn(_inject, text, count=1)
    if n != 1:
        raise ToolError(f"could not locate #hero-logo image in {fixture}")
    fixture.write_text(updated, encoding="utf-8")
    return True


def propose_remediation(
    finding: AccessibilityFinding = DEFAULT_FINDING,
    *,
    proposed_by: str = DEFAULT_AGENT,
) -> RemediationProposal:
    return RemediationProposal(
        finding=finding,
        proposed_by=proposed_by,
        patch={"attr": "alt", "value": "Company logo"},
    )


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


def _create_finding_task(proposal: RemediationProposal, *, agent: str) -> tuple[int, str | None]:
    notes = (
        f"[compliance-demo] finding={proposal.finding.finding_id}\n"
        f"rule={proposal.finding.rule}\n"
        f"target={proposal.finding.target}\n"
        f"severity={proposal.finding.severity}\n"
        f"message={proposal.finding.message}\n"
        f"proposed_by={proposal.proposed_by}\n"
        f"claimed-by: {agent}"
    )
    row = task_add(
        title=f"A11y remediation: {proposal.finding.finding_id}",
        notes=notes,
        priority="high",
        project="unified-agent",
        domain="professional",
    )
    task_id = int(row["id"])
    return task_id, _claim_without_worktree(task_id, agent)


def _queue_remediation_approval(
    proposal: RemediationProposal,
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
        raise ToolError("expected a pending a11y_remediate approval")
    return int(pending[0]["id"])


def _as_operator(operator: str):
    return config.agent_identity_var.set(operator)


def run_slice(
    *,
    workspace: Path,
    decision: Literal["approve", "deny"],
    operator: str = DEFAULT_OPERATOR,
    agent: str = DEFAULT_AGENT,
    finding: AccessibilityFinding = DEFAULT_FINDING,
    reason: str | None = None,
    artifact_dir: Path | None = None,
) -> SliceResult:
    """Execute one end-to-end approve or deny pass against a real approval gate."""
    fixture = write_fixture(workspace)
    proposal = propose_remediation(finding, proposed_by=agent)
    task_id, run_id = _create_finding_task(proposal, agent=agent)
    approval_id = _queue_remediation_approval(proposal, task_id, requested_by=agent)
    payload = proposal.approval_payload(task_id)

    token = _as_operator(operator)
    try:
        if decision == "approve":
            approval_resolve(approval_id, "approve", resolved_by=operator)
        else:
            approval_resolve(
                approval_id,
                "deny",
                resolved_by=operator,
                reason=reason or "operator declined remediation; fail closed",
            )
    finally:
        config.agent_identity_var.reset(token)

    remediation_applied = False
    denial_recorded = decision == "deny"
    evidence_path: str | None = None

    if decision == "approve":
        # Consume the one-shot pass, then apply the fixture patch.
        consumed = check_approval_gate(GATED_TOOL, payload, requested_by=agent)
        if not consumed.get("gated"):
            raise ToolError("approved remediation did not consume a gated pass")
        apply_alt_text_remediation(fixture, alt=str(proposal.patch["value"]))
        remediation_applied = True
        events.record_event(
            "agent.task.completed",
            payload={
                "task_id": task_id,
                "host": config.current_source_host(),
                "remediation": {
                    "finding_id": finding.finding_id,
                    "applied": True,
                    "approval_id": approval_id,
                },
            },
            originator=agent,
        )
        evidence_ref = f"compliance-demo:{finding.finding_id}:approval-{approval_id}"
        task_update(
            task_id,
            status="done",
            evidence_ref=evidence_ref,
            append_note=(
                f"[remediation: applied @ {storage.now()}] "
                f"approval={approval_id} alt={proposal.patch['value']!r}"
            ),
            worktree=False,
            actor=agent,
            expected_run_id=run_id,
        )
    else:
        # Fail-closed: denied exact payload must refuse retries; do not patch.
        try:
            check_approval_gate(GATED_TOOL, payload, requested_by=agent)
            raise ToolError("denied remediation unexpectedly passed the approval gate")
        except ToolError as exc:
            if "DENIED" not in str(exc):
                raise
        events.record_event(
            "agent.approval.denied",
            payload={
                "task_id": task_id,
                "approval_id": approval_id,
                "finding_id": finding.finding_id,
                "remediation_applied": False,
            },
            originator=operator,
        )
        task_update(
            task_id,
            status="blocked",
            append_note=(
                f"[remediation: denied @ {storage.now()}] "
                f"approval={approval_id}; no fix applied (fail-closed)"
            ),
            worktree=False,
            actor=agent,
            expected_run_id=run_id,
        )
        # Fixture must remain unremediated.
        if 'alt="Company logo"' in fixture.read_text(encoding="utf-8"):
            raise ToolError("fail-closed violated: fixture was patched after denial")

    report = evidence_report.build_evidence_report(task_id=task_id)
    coverage = report.get("attribution_coverage") or {}
    chain = report.get("chain_integrity") or {}
    records = report.get("records") or []
    rec = records[0] if records else {}
    approvals = (rec.get("action") or {}).get("approvals") or []

    if decision == "deny":
        if not any(a.get("status") == "denied" for a in approvals):
            raise ToolError("evidence report missing denied approval row")
        if remediation_applied:
            raise ToolError("denial path claimed remediation_applied")

    summary = {
        "task_id": task_id,
        "decision": decision,
        "remediation_applied": remediation_applied,
        "finding": finding.to_dict(),
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
        stem = f"a11y-remediation-{decision}-task-{task_id}"
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
        remediation_applied=remediation_applied,
        evidence_path=evidence_path,
        coverage_gate=coverage.get("gate"),
        provenance_verified=bool(chain.get("provenance_verified")),
        actor_authentication_verified=bool(chain.get("actor_authentication_verified")),
        denial_recorded=denial_recorded,
        report_summary=summary,
    )


def run_both_paths(
    *,
    root: Path,
    operator: str = DEFAULT_OPERATOR,
    agent: str = DEFAULT_AGENT,
) -> dict[str, Any]:
    """Run approve + deny paths into sibling isolated homes; return combined transcript."""
    results: dict[str, Any] = {"operator": operator, "agent": agent, "paths": {}}
    for decision in ("approve", "deny"):
        home = root / f"ua_home_{decision}"
        workspace = root / f"workspace_{decision}"
        artifacts = root / "artifacts" / decision
        configure_demo_env(ua_home=home, workspace=workspace, operator=operator)
        result = run_slice(
            workspace=workspace,
            decision=decision,  # type: ignore[arg-type]
            operator=operator,
            agent=agent,
            artifact_dir=artifacts,
            reason="demo negative case: operator denied remediation",
        )
        results["paths"][decision] = result.to_dict()
    return results


def main(argv: list[str] | None = None) -> int:
    """CLI entry for ``python -m unified_agent.demo.a11y_compliance_remediation``."""
    import argparse
    import sys
    import tempfile

    parser = argparse.ArgumentParser(
        description=(
            "Compliance wedge demo (task #1131): accessibility remediation "
            "through the real approval gate with a signed evidence report."
        )
    )
    parser.add_argument(
        "--decision",
        choices=("approve", "deny", "both"),
        default="both",
        help="Which path to run (default: both approve + deny)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Directory for isolated UA_HOME/workspace/artifacts (default: temp dir)",
    )
    parser.add_argument("--operator", default=DEFAULT_OPERATOR)
    parser.add_argument("--agent", default=DEFAULT_AGENT)
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print the machine-readable result to stdout",
    )
    args = parser.parse_args(argv)

    root = args.out or Path(tempfile.mkdtemp(prefix="ua-a11y-compliance-"))
    root.mkdir(parents=True, exist_ok=True)

    if args.decision == "both":
        result = run_both_paths(root=root, operator=args.operator, agent=args.agent)
    else:
        home = root / "ua_home"
        workspace = root / "workspace"
        artifacts = root / "artifacts" / args.decision
        configure_demo_env(ua_home=home, workspace=workspace, operator=args.operator)
        slice_result = run_slice(
            workspace=workspace,
            decision=args.decision,
            operator=args.operator,
            agent=args.agent,
            artifact_dir=artifacts,
            reason="operator denied remediation; fail closed",
        )
        result = {
            "operator": args.operator,
            "agent": args.agent,
            "paths": {args.decision: slice_result.to_dict()},
        }

    result["root"] = str(root)
    transcript = root / "transcript.json"
    transcript.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"demo root: {root}")
    print(f"transcript: {transcript}")
    for name, path_result in result["paths"].items():
        print(
            f"  {name}: task={path_result['task_id']} "
            f"approval={path_result['approval_id']} "
            f"applied={path_result['remediation_applied']} "
            f"provenance_verified={path_result['provenance_verified']} "
            f"evidence={path_result.get('evidence_path')}"
        )
    if args.print_json:
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
