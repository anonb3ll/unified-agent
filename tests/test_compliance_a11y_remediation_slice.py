"""Task #1131: compliance wedge — a11y remediation through approval + evidence.

Covers the gated remediation path and the fail-closed denial path against the
real approval queue, producer HMAC signing, evidence_report, and
compute_attribution_coverage.
"""

from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.audit import evidence_report, producer_sign
from unified_agent.demo.a11y_compliance_remediation import (
    DEFAULT_AGENT,
    DEFAULT_BEARER,
    DEFAULT_OPERATOR,
    GATED_TOOL,
    configure_demo_env,
    propose_remediation,
    run_both_paths,
    run_slice,
)
from unified_agent.tools.approvals import check_approval_gate


@pytest.fixture
def demo_root(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    root = tmp_path / "demo"
    home = root / "ua_home"
    workspace = root / "workspace"
    configure_demo_env(
        ua_home=home,
        workspace=workspace,
        operator=DEFAULT_OPERATOR,
        bearer=DEFAULT_BEARER,
    )
    monkeypatch.setattr(config, "current_source_host", lambda: "demo-device.local")
    return root


def test_approve_path_applies_fix_and_verifies_signed_evidence(demo_root):
    workspace = demo_root / "workspace"
    artifacts = demo_root / "artifacts" / "approve"
    result = run_slice(
        workspace=workspace,
        decision="approve",
        artifact_dir=artifacts,
    )

    assert result.remediation_applied is True
    assert result.denial_recorded is False
    assert result.provenance_verified is True
    assert result.actor_authentication_verified is True
    fixture = (workspace / "fixture.html").read_text(encoding="utf-8")
    assert 'alt="Company logo"' in fixture

    report = evidence_report.build_evidence_report(task_id=result.task_id)
    assert report["chain_integrity"]["verified"] is True
    assert report["chain_integrity"]["provenance_verified"] is True
    rec = report["records"][0]
    assert rec["human"] == DEFAULT_OPERATOR
    assert rec["human_provenance"]["verified"] is True
    assert rec["agent"] == DEFAULT_AGENT
    statuses = {a["status"] for a in rec["action"]["approvals"]}
    assert "consumed" in statuses or "approved" in statuses

    conn = storage.connect()
    try:
        producers = conn.execute(
            "SELECT payload FROM event_log WHERE channel=?",
            (producer_sign.CHANNEL_PRODUCER_AUTHENTICATED,),
        ).fetchall()
    finally:
        conn.close()
    assert len(producers) == 1
    payload = json.loads(producers[0]["payload"])
    assert payload["kind"] == "approval"
    assert payload["scheme"] == producer_sign.SCHEME_HMAC_SHA256_V1
    assert producer_sign.verify_producer_signature(payload) is True
    assert DEFAULT_BEARER not in producers[0]["payload"]
    assert DEFAULT_BEARER not in json.dumps(report)

    cov = evidence_report.compute_attribution_coverage()
    assert cov["denominator"] >= 1
    assert cov["gate"] == "pass"
    assert result.evidence_path and artifacts.exists()


def test_deny_path_fail_closed_records_denial_without_remediation(demo_root):
    workspace = demo_root / "workspace"
    artifacts = demo_root / "artifacts" / "deny"

    result = run_slice(
        workspace=workspace,
        decision="deny",
        artifact_dir=artifacts,
        reason="unit-test denial",
    )

    assert result.remediation_applied is False
    assert result.denial_recorded is True
    fixture = (workspace / "fixture.html").read_text(encoding="utf-8")
    assert 'alt="Company logo"' not in fixture

    report = evidence_report.build_evidence_report(task_id=result.task_id)
    rec = report["records"][0]
    approvals = rec["action"]["approvals"]
    assert any(a["status"] == "denied" for a in approvals)
    denied = next(a for a in approvals if a["status"] == "denied")
    assert denied["reason"] and "unit-test denial" in denied["reason"]
    # Denied approvals are excluded from inferred approval.resolved_by
    # candidates; the signed producer may still identify who decided.
    assert not any(
        c.get("source") == "approval.resolved_by" for c in rec["human_provenance"]["candidates"]
    )
    assert rec["action"]["status"] == "blocked"
    assert not rec["proof"].get("evidence_ref")
    # Summary must not claim a remediation was applied.
    assert result.report_summary["remediation_applied"] is False

    proposal = propose_remediation()
    with pytest.raises(ToolError, match="DENIED"):
        check_approval_gate(
            GATED_TOOL,
            proposal.approval_payload(result.task_id),
            requested_by=DEFAULT_AGENT,
        )


def test_both_paths_transcript_shape(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    monkeypatch.setattr(config, "current_source_host", lambda: "demo-device.local")
    root = tmp_path / "both"
    combined = run_both_paths(root=root)
    assert set(combined["paths"]) == {"approve", "deny"}
    assert combined["paths"]["approve"]["remediation_applied"] is True
    assert combined["paths"]["deny"]["remediation_applied"] is False
    assert combined["paths"]["approve"]["provenance_verified"] is True
    assert (root / "artifacts" / "approve").is_dir()
    assert (root / "artifacts" / "deny").is_dir()


# --- the demo claim is ledgered (re-land of PR #866, task #1681) -------------


def _demo_runs(task_id: int) -> list[dict]:
    from unified_agent import run_records

    return run_records.run_history(task_id=task_id)["runs"]


def _worktree_path(task_id: int):
    conn = storage.connect()
    try:
        return conn.execute("SELECT worktree_path FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize(("decision", "outcome"), [("approve", "completed"), ("deny", "blocked")])
def test_demo_claim_records_one_run_closed_by_the_demo(demo_root, decision, outcome):
    result = run_slice(
        workspace=demo_root / "workspace",
        decision=decision,
        artifact_dir=demo_root / "artifacts" / decision,
        reason="unit-test denial" if decision == "deny" else None,
    )

    [run] = _demo_runs(result.task_id)
    assert run["agent"] == DEFAULT_AGENT
    assert run["claim_source"] == "task_claim"
    assert run["state"] == "finished"
    assert run["outcome"] == outcome
    assert _worktree_path(result.task_id) is None
