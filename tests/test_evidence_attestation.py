"""Task #1267 (Epic F / F4): report-level attestation block.

Distinct from `human_provenance` (task #1217), which is per-record and
answers "who is accountable for the recorded task action". This block is
report-level: who attests to THIS report's generation, under which identity
class, with which signing key id, at what time, and over what scope — so a
standalone export (Epic #1191) names its own accountable attester without a
live hub lookup.

Honesty rule (extends #1217 to the report header): a caller is promoted to
`attester_kind: "human"` only when the request authenticated through the
hub's live WorkOS/IdP leg. A plain bearer token never earns human
attestation, even for a human-classified label (e.g. Desktop_Person) —
same failure mode #1217 fixed for `human_provenance.verified`.
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from unified_agent import config, storage
from unified_agent.audit import evidence_cli, evidence_report, evidence_signing


@pytest.fixture(autouse=True)
def init_clean_db(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    monkeypatch.delenv("UA_EVIDENCE_SIGNING_KEY", raising=False)
    monkeypatch.delenv("UA_EVIDENCE_SIGNING_KEY_ID", raising=False)
    monkeypatch.delenv("UA_EVIDENCE_SIGNING_SECRET_NAME", raising=False)
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    monkeypatch.setattr(config, "current_source_host", lambda: "test-device.local")
    storage.init_db()
    yield


def _insert_task(task_id: int, *, claimed_by: str = "Desktop_Agent4") -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, "
            "completed_at, source_host, claimed_by, evidence_ref, notes, project, "
            "domain, lane) VALUES (?, 'attestation wedge', 'done', 'high', "
            "'2026-09-23T00:00:00+00:00', '2026-09-23T00:00:00+00:00', "
            "'2026-09-23T00:00:00+00:00', 'test-device.local', ?, "
            "'https://github.com/anonb3ll/unified-agent/pull/1', '', 'unified-agent', "
            "'hobby', 'hobby')",
            (task_id, claimed_by),
        )
        conn.commit()
    finally:
        conn.close()


def _as_bearer(identity: str):
    id_token = config.agent_identity_var.set(identity)
    meta_token = config.auth_metadata_var.set({"auth_method": "bearer", "scopes": []})
    return id_token, meta_token


def _as_workos(
    identity: str,
    *,
    subject: str = "user-a",
    organization_id: str = "org-a",
    session_id: str = "session-a",
):
    return config.bind_workos_request_auth(
        identity=identity,
        subject=subject,
        organization_id=organization_id,
        session_id=session_id,
        expires_at=9999999999,
    )


def _reset(*tokens):
    if len(tokens) == 3:
        config.reset_workos_request_auth(*tokens)
    else:
        id_token, meta_token = tokens
        config.agent_identity_var.reset(id_token)
        config.auth_metadata_var.reset(meta_token)


# ---------------------------------------------------------------------------
# Construction: attester_kind classification
# ---------------------------------------------------------------------------


def test_unauthenticated_build_has_unknown_attestation():
    _insert_task(1)
    report = evidence_report.build_evidence_report(task_id=1)
    attestation = report["attestation"]
    assert attestation["attester"] is None
    assert attestation["attester_kind"] == "unknown"
    assert attestation["verified_via"] is None
    assert attestation["human_attested"] is False
    assert attestation["key_id"] is None
    assert attestation["scope"]["task_id"] == 1


def test_bearer_authenticated_agent_renders_agent_bearer_only():
    _insert_task(2)
    tokens = _as_bearer("Desktop_Agent2")
    try:
        report = evidence_report.build_evidence_report(task_id=2)
    finally:
        _reset(*tokens)
    attestation = report["attestation"]
    assert attestation["attester"] == "Desktop_Agent2"
    assert attestation["attester_kind"] == "agent-bearer-only"
    assert attestation["verified_via"] == "agent_bearer"
    assert attestation["human_attested"] is False


def test_bearer_authenticated_human_labeled_identity_is_not_promoted_to_human():
    """The forged/near-forged case: a bearer token resolves to a
    human-classified label (Desktop_Person), but a bearer HMAC alone never
    proves a human was present — the #1217 honesty rule applies to the
    report header the same way it applies to human_provenance."""
    _insert_task(3)
    tokens = _as_bearer("Desktop_Person")
    try:
        report = evidence_report.build_evidence_report(task_id=3)
    finally:
        _reset(*tokens)
    attestation = report["attestation"]
    assert attestation["attester"] == "Desktop_Person"
    assert attestation["attester_kind"] == "agent-bearer-only"
    assert attestation["human_attested"] is False
    assert attestation["verified_via"] == "agent_bearer"
    assert "workos_subject" not in attestation


def test_workos_session_renders_human_attested_with_idp_claims():
    _insert_task(4)
    tokens = _as_workos("Desktop_Person", subject="user-a", organization_id="org-a")
    try:
        report = evidence_report.build_evidence_report(task_id=4)
    finally:
        _reset(*tokens)
    attestation = report["attestation"]
    assert attestation["attester"] == "Desktop_Person"
    assert attestation["attester_kind"] == "human"
    assert attestation["human_attested"] is True
    assert attestation["verified_via"] == "idp"
    assert attestation["workos_subject"] == "user-a"
    assert attestation["workos_organization_id"] == "org-a"
    assert attestation["workos_session_id"] == "session-a"


def test_workos_auth_label_without_verified_claims_is_not_human():
    _insert_task(14)
    tokens = _as_workos("Desktop_Person", session_id="")
    try:
        report = evidence_report.build_evidence_report(task_id=14)
    finally:
        _reset(*tokens)
    assert report["attestation"]["attester_kind"] == "unknown"
    assert report["attestation"]["human_attested"] is False
    assert report["attestation"]["attester"] is None


def test_fabricated_workos_auth_without_idp_verified_marker_is_not_human():
    """Task #1313: claim strings / forgeable dict flags must not earn human
    attestation — only bind_workos_request_auth (middleware) does."""
    _insert_task(15)
    id_token = config.agent_identity_var.set("Desktop_Person")
    meta_token = config.auth_metadata_var.set(
        {
            "auth_method": "workos",
            "scopes": [],
            "idp_verified": True,  # forgeable dict flag — must not suffice
            "workos_user_id": "user-forged",
            "workos_organization_id": "org-forged",
            "workos_session_id": "session-forged",
            "exp": 9999999999,
        }
    )
    try:
        report = evidence_report.build_evidence_report(task_id=15)
    finally:
        _reset(id_token, meta_token)
    assert report["attestation"]["attester_kind"] == "unknown"
    assert report["attestation"]["human_attested"] is False
    assert report["attestation"]["attester"] is None
    assert config.workos_request_is_bound() is False


def test_key_id_present_only_when_signing_configured(monkeypatch):
    _insert_task(5)
    report = evidence_report.build_evidence_report(task_id=5)
    assert report["attestation"]["key_id"] is None

    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY_ID", "test-key-9")
    report = evidence_report.build_evidence_report(task_id=5)
    assert report["attestation"]["key_id"] == "test-key-9"


def test_schema_version_is_1_5():
    _insert_task(6)
    report = evidence_report.build_evidence_report(task_id=6)
    assert report["schema_version"] == "1.8"


# ---------------------------------------------------------------------------
# Rendering: Markdown/text never upgrade agent-bearer-only to human
# ---------------------------------------------------------------------------


def test_markdown_renders_attestation_without_promoting_agent_to_human():
    _insert_task(7)
    tokens = _as_bearer("Desktop_Agent2")
    try:
        report = evidence_report.build_evidence_report(task_id=7)
    finally:
        _reset(*tokens)
    markdown = evidence_report.format_evidence_report(report, "markdown")
    assert "Attestation: Desktop_Agent2" in markdown
    assert "attester_kind=agent-bearer-only" in markdown
    assert "attester_kind=human" not in markdown


def test_text_renders_attestation_with_idp_verified_human():
    _insert_task(8)
    tokens = _as_workos("Desktop_Person")
    try:
        report = evidence_report.build_evidence_report(task_id=8)
    finally:
        _reset(*tokens)
    text = evidence_report.format_evidence_report(report, "text")
    assert "Attestation: Desktop_Person" in text
    assert "attester_kind=human" in text
    assert "verified-by-idp" in text


def test_unauthenticated_markdown_shows_unattested():
    _insert_task(9)
    report = evidence_report.build_evidence_report(task_id=9)
    markdown = evidence_report.format_evidence_report(report, "markdown")
    assert "Attestation: unattested" in markdown


def test_csv_export_carries_attestation_columns():
    _insert_task(10)
    tokens = _as_bearer("Desktop_Agent2")
    try:
        report = evidence_report.build_evidence_report(task_id=10)
    finally:
        _reset(*tokens)
    csv_str = evidence_report.format_evidence_report(report, "csv")
    header = csv_str.splitlines()[0]
    assert "attestation_attester" in header
    assert "attestation_attester_kind" in header
    row = csv_str.splitlines()[1]
    assert "Desktop_Agent2" in row
    assert "agent-bearer-only" in row


def test_empty_csv_export_carries_report_metadata_row():
    tokens = _as_bearer("Desktop_Agent2")
    try:
        report = evidence_report.build_evidence_report(task_id=987654)
    finally:
        _reset(*tokens)
    rows = list(csv.DictReader(io.StringIO(evidence_report.format_evidence_report(report, "csv"))))
    assert len(rows) == 1
    assert rows[0]["record_type"] == "report_metadata"
    assert rows[0]["attestation_attester"] == "Desktop_Agent2"
    assert rows[0]["attestation_attester_kind"] == "agent-bearer-only"
    assert rows[0]["attestation_attested_at"] == report["generated_at"]
    assert json.loads(rows[0]["attestation_scope"]) == report["filter"]


# ---------------------------------------------------------------------------
# `unified-agent evidence verify`: attestation reporting + consistency checks
# ---------------------------------------------------------------------------


def test_verify_cli_reports_attestation_and_key_id_matches(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY_ID", "test-key-1267")
    _insert_task(11)
    tokens = _as_bearer("Desktop_Agent2")
    try:
        artifact_path = tmp_path / "evidence.json"
        assert (
            evidence_cli.run_evidence_cli(["--task", "11", "--json", "--out", str(artifact_path)])
            == 0
        )
    finally:
        _reset(*tokens)

    assert evidence_cli.run_evidence_cli(["verify", str(artifact_path)]) == 0
    out = capsys.readouterr().out
    assert "evidence-verify: attestation: attester=Desktop_Agent2" in out
    assert "kind=agent-bearer-only" in out
    assert "key_id=test-key-1267" in out


def test_export_uses_one_signing_key_snapshot_for_attestation_and_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "configured-key")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY_ID", "configured-id")
    _insert_task(15)
    report = evidence_report.build_evidence_report(task_id=15)
    assert report["attestation"]["key_id"] == "configured-id"
    monkeypatch.setattr(evidence_signing, "_key_material", lambda: (b"snapshot-key", "snapshot-id"))
    output = tmp_path / "snapshot.json"
    assert evidence_cli._emit_report(report, "json", str(output)) == 0
    exported = json.loads(output.read_text())
    sidecar = evidence_signing.load_sidecar(evidence_signing.sidecar_path(output))
    assert exported["attestation"]["key_id"] == sidecar["key_id"] == "snapshot-id"


def test_verify_cli_reports_no_block_for_export_without_attestation(tmp_path):
    """Direct `_check_attestation` unit check for a pre-1.5-shaped export
    (no `attestation` key at all) — must not fail closed on an old export."""
    artifact = json.dumps({"report_type": "user_attributable_evidence"}).encode()
    result = evidence_cli._check_attestation(artifact, None)
    assert result["code"] == 0
    assert "no attestation block" in result["message"]


def test_check_attestation_not_checked_for_non_json_export():
    result = evidence_cli._check_attestation(b"not json at all", None)
    assert result["code"] == 0
    assert "not checked" in result["message"]


def test_schema_1_5_requires_attestation_block():
    result = evidence_cli._check_attestation(
        json.dumps({"schema_version": "1.5", "filter": {}}).encode(), None
    )
    assert result["code"] == 2
    assert "missing its attestation block" in result["message"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"workos_session_id": None}, "without IdP evidence"),
        ({"human_attested": False}, "human_attested flag"),
    ],
)
def test_schema_1_5_rejects_incomplete_or_inconsistent_human_claim(changes, message):
    block = {
        "attester": "Desktop_Person",
        "attester_kind": "human",
        "human_attested": True,
        "verified_via": "idp",
        "workos_subject": "user-a",
        "workos_organization_id": "org-a",
        "workos_session_id": "session-a",
        "key_id": "key-a",
        "attested_at": "2026-09-23T00:00:00Z",
        "scope": {},
    }
    block.update(changes)
    artifact = json.dumps(
        {
            "schema_version": "1.5",
            "generated_at": "2026-09-23T00:00:00Z",
            "filter": {},
            "attestation": block,
        }
    ).encode()
    result = evidence_cli._check_attestation(artifact, None)
    assert result["code"] == 2
    assert message in result["message"]


def test_check_attestation_rejects_forged_human_claim_without_idp_evidence():
    """A crafted/forged report body claims human attestation but carries no
    IdP session evidence — rejected even independent of signature status."""
    artifact = json.dumps(
        {
            "attestation": {
                "attester": "Desktop_Agent2",
                "attester_kind": "human",
                "verified_via": None,
                "human_attested": True,
                "key_id": None,
            }
        }
    ).encode()
    result = evidence_cli._check_attestation(artifact, None)
    assert result["code"] == 2
    assert "without IdP evidence" in result["message"]


def test_check_attestation_rejects_human_claim_missing_session_claims():
    artifact = json.dumps(
        {
            "attestation": {
                "attester": "Desktop_Person",
                "attester_kind": "human",
                "verified_via": "idp",
                # workos_subject / workos_organization_id deliberately absent
            }
        }
    ).encode()
    result = evidence_cli._check_attestation(artifact, None)
    assert result["code"] == 2
    assert "without IdP evidence" in result["message"]


def test_check_attestation_flags_key_id_mismatch():
    artifact = json.dumps(
        {
            "attestation": {
                "attester": "Desktop_Agent2",
                "attester_kind": "agent-bearer-only",
                "verified_via": "agent_bearer",
                "key_id": "key-a",
            }
        }
    ).encode()
    sidecar = {"key_id": "key-b"}
    result = evidence_cli._check_attestation(artifact, sidecar)
    assert result["code"] == 2
    assert "does not match signing key" in result["message"]


def test_check_attestation_passes_when_key_ids_match():
    artifact = json.dumps(
        {
            "attestation": {
                "attester": "Desktop_Agent2",
                "attester_kind": "agent-bearer-only",
                "verified_via": "agent_bearer",
                "key_id": "key-a",
            }
        }
    ).encode()
    sidecar = {"key_id": "key-a"}
    result = evidence_cli._check_attestation(artifact, sidecar)
    assert result["code"] == 0
    assert "attester=Desktop_Agent2" in result["message"]


def test_one_byte_tamper_of_attestation_block_fails_verify(tmp_path, monkeypatch):
    """The attestation block ships inside the signed bytes like every other
    field — a single tampered character breaks the HMAC digest, same as any
    other part of the report."""
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY_ID", "test-key-1267")
    _insert_task(12)
    tokens = _as_bearer("Desktop_Agent2")
    try:
        artifact_path = tmp_path / "evidence.json"
        assert (
            evidence_cli.run_evidence_cli(["--task", "12", "--json", "--out", str(artifact_path)])
            == 0
        )
    finally:
        _reset(*tokens)

    assert evidence_cli.run_evidence_cli(["verify", str(artifact_path)]) == 0

    body = artifact_path.read_text(encoding="utf-8")
    assert '"attester": "Desktop_Agent2"' in body
    # Tamper only the attester name (same length, same attester_kind) so this
    # exercises purely the byte-level HMAC digest check, not the separate
    # structural "human claim without IdP evidence" check below.
    tampered = body.replace('"attester": "Desktop_Agent2"', '"attester": "Desktop_Agent5"')
    assert tampered != body
    artifact_path.write_text(tampered, encoding="utf-8")

    exit_code = evidence_cli.run_evidence_cli(["verify", str(artifact_path)])
    assert exit_code == 1


def test_forged_human_attestation_tamper_is_caught_by_both_checks(tmp_path, monkeypatch):
    """A tamper that flips `attester_kind` to a forged `"human"` claim trips
    BOTH the byte-level digest check (1) and the structural honesty check
    (2, no IdP evidence backs the claim) — the combined exit code is the max
    of every failing check, so the stronger signal wins."""
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY_ID", "test-key-1267")
    _insert_task(13)
    tokens = _as_bearer("Desktop_Agent2")
    try:
        artifact_path = tmp_path / "evidence.json"
        assert (
            evidence_cli.run_evidence_cli(["--task", "13", "--json", "--out", str(artifact_path)])
            == 0
        )
    finally:
        _reset(*tokens)

    body = artifact_path.read_text(encoding="utf-8")
    tampered = body.replace(
        '"attester_kind": "agent-bearer-only"', '"attester_kind": "human"       '
    )
    assert tampered != body
    artifact_path.write_text(tampered, encoding="utf-8")

    exit_code = evidence_cli.run_evidence_cli(["verify", str(artifact_path)])
    assert exit_code == 2


def test_signature_covers_attestation_block_directly(monkeypatch):
    """Lower-level proof of the same guarantee: `evidence_signing.verify_export`
    rejects a byte-for-byte change inside the attestation block."""
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY_ID", "test-key-1267")
    artifact = json.dumps(
        {"attestation": {"attester": "Desktop_Agent2", "attester_kind": "agent-bearer-only"}}
    ).encode()
    sidecar = evidence_signing.sign_export(
        artifact, chain_head="deadbeef", generated_at="2026-09-23T00:00:00+00:00"
    )
    assert evidence_signing.verify_export(artifact, sidecar) == (True, "valid")

    tampered = artifact.replace(b"agent-bearer-only", b"human-attested-fake")
    valid, reason = evidence_signing.verify_export(tampered, sidecar)
    assert valid is False
    assert reason in evidence_signing.CRYPTO_FAILURES
