"""Tests for Task #782: Open-core boundary seam (core vs enterprise edition)."""

from __future__ import annotations

import pytest

from unified_agent import config, edition, events, storage
from unified_agent.webui import api


def test_default_edition_is_enterprise(monkeypatch):
    monkeypatch.delenv("UA_EDITION", raising=False)
    assert edition.current_edition() == "enterprise"
    assert edition.is_enterprise() is True
    assert edition.is_core() is False
    assert config.edition() == "enterprise"
    assert config.is_enterprise_edition() is True


def test_core_edition_override(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "core")
    assert edition.current_edition() == "core"
    assert edition.is_enterprise() is False
    assert edition.is_core() is True
    assert config.edition() == "core"
    assert config.is_enterprise_edition() is False


def test_invalid_edition_falls_back_to_enterprise(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "invalid_custom_tier")
    assert edition.current_edition() == "enterprise"


def test_feature_availability_mapping(monkeypatch):
    # Core edition has developer features, lacks enterprise compliance features
    monkeypatch.setenv("UA_EDITION", "core")
    assert edition.is_feature_available("local_memory") is True
    assert edition.is_feature_available("local_tasks") is True
    assert edition.is_feature_available("process_sandbox") is True
    assert edition.is_feature_available("evidence_reporting") is False
    assert edition.is_feature_available("audit_hash_chain") is False

    with pytest.raises(edition.FeatureNotAvailableError, match=r"(?i)Enterprise edition"):
        edition.require_feature("evidence_reporting")

    # Enterprise edition has all features
    monkeypatch.setenv("UA_EDITION", "enterprise")
    assert edition.is_feature_available("local_memory") is True
    assert edition.is_feature_available("evidence_reporting") is True
    assert edition.is_feature_available("audit_hash_chain") is True
    edition.require_feature("evidence_reporting")  # Does not raise


def test_edition_info_diagnostics(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "core")
    info = edition.edition_info()
    assert info["edition"] == "core"
    assert info["is_core"] is True
    assert info["is_enterprise"] is False
    assert "local_memory" in info["features"]
    assert "evidence_reporting" not in info["features"]


@pytest.mark.anyio
async def test_overview_payload_includes_edition(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "core")
    payload = await api.overview_payload()
    assert payload["edition"] == "core"
    assert payload["is_enterprise"] is False


def test_event_log_verify_gated_by_edition(monkeypatch):
    from unified_agent.tools import events as tools_events

    # In core edition, event_log_verify raises FeatureNotAvailableError
    monkeypatch.setenv("UA_EDITION", "core")
    with pytest.raises(edition.FeatureNotAvailableError, match=r"audit_hash_chain"):
        tools_events.event_log_verify()

    # In enterprise edition, event_log_verify succeeds
    monkeypatch.setenv("UA_EDITION", "enterprise")
    res = tools_events.event_log_verify()
    assert res["status"] in ("valid", "empty")


def test_audit_chain_writes_gated_by_edition(monkeypatch, tmp_path):
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()

    # In core edition, event writes do NOT compute/write hash chain columns
    monkeypatch.setenv("UA_EDITION", "core")
    ulid_core = events.record_event("test.core.event", payload={"k": "v1"})
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT ulid, prev_hash, entry_hash FROM event_log WHERE ulid=?", (ulid_core,)
        ).fetchone()
        assert row["prev_hash"] is None
        assert row["entry_hash"] is None
    finally:
        conn.close()

    # In enterprise edition, event writes construct the hash chain
    monkeypatch.setenv("UA_EDITION", "enterprise")
    ulid_ent = events.record_event("test.enterprise.event", payload={"k": "v2"})
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT ulid, prev_hash, entry_hash FROM event_log WHERE ulid=?", (ulid_ent,)
        ).fetchone()
        assert row["prev_hash"] is not None
        assert row["entry_hash"] is not None
    finally:
        conn.close()


def test_audit_reanchor_gated_by_edition(monkeypatch):
    from unified_agent.audit import ledger_cli, reanchor

    # In core edition, reanchor_chain_head raises FeatureNotAvailableError
    monkeypatch.setenv("UA_EDITION", "core")
    with pytest.raises(edition.FeatureNotAvailableError, match=r"audit_reanchor"):
        reanchor.reanchor_chain_head()

    # In core edition, CLI exits with code 2
    rc = ledger_cli.run_reanchor_cli([])
    assert rc == 2

    # In enterprise edition, feature check passes
    monkeypatch.setenv("UA_EDITION", "enterprise")
    edition.require_feature("audit_reanchor")


def test_evidence_report_surface_gated_by_edition(monkeypatch, capsys):
    from unified_agent.audit import evidence_cli, evidence_report

    # In core edition, build_evidence_report raises FeatureNotAvailableError
    monkeypatch.setenv("UA_EDITION", "core")
    with pytest.raises(edition.FeatureNotAvailableError, match=r"evidence_reporting"):
        evidence_report.build_evidence_report()

    # In core edition, evidence CLI exits with code 2
    rc = evidence_cli.run_evidence_cli([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "evidence_reporting" in err or "Enterprise edition" in err

    # In enterprise edition, build_evidence_report succeeds
    monkeypatch.setenv("UA_EDITION", "enterprise")
    report = evidence_report.build_evidence_report()
    assert "records" in report


def test_server_tool_wrapper_gated_by_edition(monkeypatch):
    from unified_agent import server

    monkeypatch.setenv("UA_EDITION", "core")
    with pytest.raises(edition.FeatureNotAvailableError, match=r"audit_hash_chain"):
        server.REGISTERED_TOOLS["event_log_verify"]()

    monkeypatch.setenv("UA_EDITION", "enterprise")
    res = server.REGISTERED_TOOLS["event_log_verify"]()
    assert res["status"] in ("valid", "empty")
