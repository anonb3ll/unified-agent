from __future__ import annotations

import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, server, storage, webui
from unified_agent.audit import evidence_cli, evidence_signing


def test_signature_verifies_untouched_bytes_and_rejects_one_byte_change(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY_ID", "test-key-1")
    artifact = b'{"report":true}\n'
    sidecar = evidence_signing.sign_export(
        artifact,
        chain_head="abc123",
        generated_at="2026-09-21T10:00:00+00:00",
    )

    assert sidecar["scheme"] == "hmac-sha256-v1"
    assert sidecar["key_id"] == "test-key-1"
    assert evidence_signing.verify_export(artifact, sidecar) == (True, "valid")
    assert evidence_signing.verify_export(artifact.replace(b"true", b"false"), sidecar)[0] is False

    sig_path = tmp_path / "report.json.sig.json"
    sig_path.write_text(json.dumps(sidecar), encoding="utf-8")
    assert evidence_signing.load_sidecar(sig_path)["artifact_sha256"] == sidecar["artifact_sha256"]


def test_cli_writes_sidecar_that_verify_cli_accepts(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY_ID", "test-key-1")
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, "
            "completed_at, source_host, claimed_by, evidence_ref, notes, project, domain, lane) "
            "VALUES (1203, 'signed export', 'done', 'med', ?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
            (
                "2026-09-21T10:00:00+00:00",
                "2026-09-21T10:00:00+00:00",
                "2026-09-21T10:00:00+00:00",
                "test-host",
                "Desktop_Agent2",
                "commit:test",
                "unified-agent",
                "professional",
                "default",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    artifact_path = tmp_path / "evidence.json"
    assert (
        evidence_cli.run_evidence_cli(["--task", "1203", "--json", "--out", str(artifact_path)])
        == 0
    )
    assert evidence_cli.run_evidence_cli(["verify", str(artifact_path)]) == 0
    assert "valid" in capsys.readouterr().out
    assert Path(f"{artifact_path}.sig.json").exists()

    artifact_path.write_bytes(artifact_path.read_bytes() + b"x")
    assert evidence_cli.run_evidence_cli(["verify", str(artifact_path)]) == 1

    Path(f"{artifact_path}.sig.json").write_text("{}", encoding="utf-8")
    assert evidence_cli.run_evidence_cli(["verify", str(artifact_path)]) == 2


def test_signed_http_export_returns_signature_headers(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY", "test-signing-key")
    monkeypatch.setenv("UA_EVIDENCE_SIGNING_KEY_ID", "test-key-1")
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:test-token")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    monkeypatch.setattr(config, "db_path", lambda: tmp_path / "memory.db")
    storage.init_db()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, "
            "completed_at, source_host, claimed_by, evidence_ref, notes, project, domain, lane) "
            "VALUES (1203, 'signed HTTP export', 'done', 'med', ?, ?, ?, ?, ?, ?, '', ?, ?, ?)",
            (
                "2026-09-21T10:00:00+00:00",
                "2026-09-21T10:00:00+00:00",
                "2026-09-21T10:00:00+00:00",
                "test-host",
                "Desktop_Agent2",
                "commit:test",
                "unified-agent",
                "professional",
                "default",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        response = client.get("/api/evidence?task_id=1203")

    assert response.status_code == 200
    assert response.headers["x-evidence-signature-key-id"] == "test-key-1"
    assert response.headers["x-evidence-signature"]
    assert response.json()["signature"]["key_id"] == "test-key-1"
    sidecar = {
        "schema": evidence_signing.SIGNATURE_SCHEMA,
        "version": "1",
        "scheme": response.headers["x-evidence-signature-scheme"],
        "key_id": response.headers["x-evidence-signature-key-id"],
        "artifact_sha256": response.headers["x-evidence-signature-sha256"],
        "signature": response.headers["x-evidence-signature"],
        "chain_head": response.headers["x-evidence-signature-chain-head"],
        "generated_at": response.headers["x-evidence-signature-generated-at"],
    }
    assert evidence_signing.verify_export(response.content, sidecar) == (True, "valid")
