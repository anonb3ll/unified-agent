"""Task #1140: real-release wedge — attest a main SHA through approval + evidence."""

from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.audit import evidence_report, producer_sign
from unified_agent.demo.real_release_wedge import (
    DEFAULT_AGENT,
    DEFAULT_BEARER,
    DEFAULT_OPERATOR,
    DEFAULT_RELEASE_SHA,
    GATED_TOOL,
    ReleaseAction,
    configure_demo_env,
    propose_attestation,
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


@pytest.fixture
def release() -> ReleaseAction:
    return ReleaseAction(
        sha=DEFAULT_RELEASE_SHA,
        subject="fix(vault): vault_write result echoes effective mode + post-write shape (#1165)",
        merged_at="2026-09-19T09:55:55+00:00",
        pr="1165",
    )


def test_approve_path_attests_release_and_passes_coverage_gate(demo_root, release):
    workspace = demo_root / "workspace"
    artifacts = demo_root / "artifacts" / "approve"
    result = run_slice(
        workspace=workspace,
        decision="approve",
        release=release,
        artifact_dir=artifacts,
    )

    assert result.attestation_applied is True
    assert result.denial_recorded is False
    assert result.provenance_verified is True
    assert result.actor_authentication_verified is True
    assert result.coverage_gate == "pass"
    attest = json.loads((workspace / "release-attestation.json").read_text(encoding="utf-8"))
    assert attest["release"]["sha"] == release.sha
    assert attest["attested"] is True

    report = evidence_report.build_evidence_report(task_id=result.task_id)
    assert report["chain_integrity"]["verified"] is True
    assert report["attribution_coverage"]["gate"] == "pass"
    assert report["clean_pass"] is True
    rec = report["records"][0]
    assert rec["human"] == DEFAULT_OPERATOR
    assert rec["human_provenance"]["verified"] is True
    assert rec["agent"] == DEFAULT_AGENT
    statuses = {a["status"] for a in rec["action"]["approvals"]}
    assert "consumed" in statuses or "approved" in statuses
    assert rec["proof"].get("evidence_ref") == release.sha

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
    assert str(payload.get("task_id")) == str(result.task_id)
    assert producer_sign.verify_producer_signature(payload) is True
    assert DEFAULT_BEARER not in producers[0]["payload"]
    assert DEFAULT_BEARER not in json.dumps(report)


def test_deny_path_fail_closed_no_attestation(demo_root, release):
    workspace = demo_root / "workspace"
    artifacts = demo_root / "artifacts" / "deny"

    result = run_slice(
        workspace=workspace,
        decision="deny",
        release=release,
        artifact_dir=artifacts,
        reason="unit-test denial",
    )

    assert result.attestation_applied is False
    assert result.denial_recorded is True
    assert not (workspace / "release-attestation.json").exists()

    report = evidence_report.build_evidence_report(task_id=result.task_id)
    rec = report["records"][0]
    assert any(a.get("status") == "denied" for a in rec["action"]["approvals"])
    assert result.report_summary["attestation_applied"] is False

    proposal = propose_attestation(release)
    with pytest.raises(ToolError, match="DENIED"):
        check_approval_gate(
            GATED_TOOL,
            proposal.approval_payload(result.task_id),
            requested_by=DEFAULT_AGENT,
        )


def test_both_paths_transcript_shape(tmp_path, monkeypatch, release):
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    monkeypatch.setattr(config, "current_source_host", lambda: "demo-device.local")
    root = tmp_path / "both"
    combined = run_both_paths(root=root, release=release)
    assert set(combined["paths"]) == {"approve", "deny"}
    assert combined["release"]["sha"] == release.sha
    assert combined["paths"]["approve"]["attestation_applied"] is True
    assert combined["paths"]["deny"]["attestation_applied"] is False
    assert combined["paths"]["approve"]["coverage_gate"] == "pass"
    assert (root / "artifacts" / "approve").is_dir()


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
def test_demo_claim_records_one_run_closed_by_the_demo(demo_root, decision, outcome, release):
    result = run_slice(
        workspace=demo_root / "workspace",
        decision=decision,
        release=release,
        artifact_dir=demo_root / "artifacts" / decision,
        reason="unit-test denial" if decision == "deny" else None,
    )

    [run] = _demo_runs(result.task_id)
    assert run["agent"] == DEFAULT_AGENT
    assert run["claim_source"] == "task_claim"
    assert run["state"] == "finished"
    assert run["outcome"] == outcome
    assert _worktree_path(result.task_id) is None
