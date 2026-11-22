"""Connector state for the contract-first Browser projection receiver."""

from __future__ import annotations

import pytest

from unified_agent import config, connectors, profiles, storage
from unified_agent.tools import doctor, horizon2
from unified_agent.tools.projection import browser_derive_task_reference

BROWSER_PROJECTION_MANIFEST = connectors.require_manifest("browser-projection")


def test_browser_projection_manifest_is_default_off_and_resolves():
    manifest = BROWSER_PROJECTION_MANIFEST

    assert manifest.default_enabled is False
    assert manifest.horizon2_registration_supported is False
    assert manifest.capabilities == frozenset({"projection"})
    assert manifest.required_secrets == frozenset()
    assert manifest.resolve_entry_point("projection") is browser_derive_task_reference
    assert "browser-projection" not in horizon2.CONNECTOR_PACKAGES


def test_manifest_only_projection_is_visible_but_dormant_without_registration(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "db_path", lambda: tmp_path / "memory.db")
    storage.init_db()

    package = next(
        item
        for item in horizon2.horizon2_package_list()["packages"]
        if item["package"] == "browser-projection"
    )
    assert package["scopes"] == []
    assert package["manifest"] == BROWSER_PROJECTION_MANIFEST.to_dict()
    assert package["manifest"]["default_enabled"] is False
    assert package["secrets"] == {"present": [], "missing": [], "satisfied": True}

    health = next(
        check
        for check in doctor._connectors_check()
        if check["capability"] == "connector:browser-projection"
    )
    assert health["status"] == "DORMANT"
    assert "no Horizon 2 transport" in health["detail"]


def test_registration_rejects_browser_before_consuming_approval(monkeypatch):
    approved = []
    monkeypatch.setattr(horizon2, "_identity", lambda: "Desktop_Agent2")
    monkeypatch.setattr(
        horizon2.approvals,
        "check_approval_gate",
        lambda *args, **kwargs: approved.append((args, kwargs)),
    )

    with pytest.raises(horizon2.ToolError, match="does not support Horizon 2 registration"):
        horizon2.horizon2_server_register(
            "browser-prod", "browser-projection", "sha256:reviewed", ["read"], "review:1312"
        )

    assert approved == []


def test_active_browser_registration_cannot_report_live(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "db_path", lambda: tmp_path / "memory.db")
    storage.init_db()
    conn = storage.connect()
    try:
        timestamp = storage.now()
        conn.execute(
            "INSERT INTO governed_servers "
            "(server_id, connector, provenance, scopes_json, reviewed_by, review_ref, revoked_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                "legacy-browser",
                "browser-projection",
                "sha256:" + "a" * 64,
                '["read"]',
                "Desktop_Agent2",
                "review:legacy",
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    approved = []
    monkeypatch.setattr(horizon2, "_identity", lambda: "Desktop_Agent2")
    monkeypatch.setattr(
        horizon2.approvals,
        "check_approval_gate",
        lambda *args, **kwargs: approved.append((args, kwargs)),
    )
    with pytest.raises(horizon2.ToolError, match="does not support Horizon 2 authorization"):
        horizon2.horizon2_connector_authorize(
            "legacy-browser", "read", task_id=42, trace_id="legacy-trace"
        )
    assert approved == []

    health = next(
        check
        for check in doctor._connectors_check()
        if check["capability"] == "connector:browser-projection"
    )
    assert health["status"] == "DORMANT"
    assert "no Horizon 2 transport" in health["detail"]
    assert "active registration row is ignored" in health["detail"]


def test_browser_receiver_remains_out_of_general_profiles():
    tool = "browser_derive_task_reference"

    for profile in (profiles.DEVELOPER, profiles.RESEARCH, profiles.OPERATOR, profiles.PUBLIC):
        assert not profiles.tool_is_allowed_for(profile, tool)
    assert profiles.tool_is_allowed_for(profiles.STEWARD, tool)
    assert profiles.tool_is_allowed_for(profiles.CHIEF_OF_STAFF, tool)
