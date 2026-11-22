"""Generic conformance harness proof (task #1204).

Runs the five-clause harness against a synthetic "widget" connector — not
Slack — to prove the harness itself is connector-agnostic: any future
Epic G connector that defines a well-formed ``ConnectorManifest`` gets these
guarantees for free, without special-casing Slack's code paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from export_guard import needs_exported_paths

from unified_agent import connectors, storage

from .harness import (
    assert_connector_conforms,
    assert_emits_hash_chained_lifecycle_events,
    assert_invalid_manifest_refused,
    assert_invisible_to_profiles_lacking_it,
    assert_refuses_to_start_without_secrets,
    assert_registers,
    assert_reports_to_doctor_and_pane,
)

WIDGET_MANIFEST = connectors.ConnectorManifest(
    name="widget",
    version="1.0.0",
    capabilities=frozenset({"ingress"}),
    required_secrets=frozenset({"WIDGET_TOKEN"}),
    entry_points={"ingress": "unified_agent.connectors:get_manifest"},
    default_enabled=False,
)


@pytest.fixture(autouse=True)
def _registered_widget():
    connectors.register_manifest(WIDGET_MANIFEST, replace=True)
    yield
    connectors.unregister_manifest("widget")


def test_widget_connector_conforms_end_to_end(monkeypatch):
    monkeypatch.setenv("WIDGET_TOKEN", "test-value-not-a-real-secret")
    assert_connector_conforms(WIDGET_MANIFEST, monkeypatch)


def test_clause_registers(monkeypatch):
    assert_registers(WIDGET_MANIFEST, monkeypatch)


def test_harness_registration_does_not_touch_callers_database(monkeypatch):
    storage.init_db()
    assert_registers(WIDGET_MANIFEST, monkeypatch, server_id="widget-isolated")

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM governed_servers WHERE server_id=?", ("widget-isolated",)
        ).fetchone()
    finally:
        conn.close()
    assert row is None


def test_clause_invalid_manifest_refused(monkeypatch):
    assert_invalid_manifest_refused(WIDGET_MANIFEST, monkeypatch)
    # The registry must be restored to the valid manifest afterward.
    assert connectors.get_manifest("widget") == WIDGET_MANIFEST


def test_clause_reports_to_doctor(monkeypatch):
    assert_reports_to_doctor_and_pane(WIDGET_MANIFEST)


def test_clause_refuses_without_secrets(monkeypatch):
    assert_refuses_to_start_without_secrets(WIDGET_MANIFEST, monkeypatch)


def test_clause_hash_chained_events(monkeypatch):
    assert_emits_hash_chained_lifecycle_events(WIDGET_MANIFEST, monkeypatch)


def test_clause_invisible_to_profiles_lacking_it():
    assert_invisible_to_profiles_lacking_it()


def test_manifest_with_no_secrets_skips_the_start_gate_clause(monkeypatch):
    no_secrets = connectors.ConnectorManifest(
        name="widget",
        version="1.0.0",
        capabilities=frozenset({"ingress"}),
        entry_points={"ingress": "unified_agent.connectors:get_manifest"},
    )
    connectors.register_manifest(no_secrets, replace=True)
    with pytest.raises(pytest.skip.Exception):
        assert_refuses_to_start_without_secrets(no_secrets, monkeypatch)


# ci.yml ships as two hand-maintained copies: the GitHub original and the Depot CI
# mirror. The gate below must hold in BOTH, or the live CI silently diverges from the
# copy anyone reading .github/ would expect.
CI_WORKFLOWS = {
    "github": Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml",
    "depot": Path(__file__).parents[2] / ".depot" / "workflows" / "ci.yml",
}


@pytest.mark.parametrize(
    "workflow_path",
    [
        pytest.param(
            path, id=name, marks=needs_exported_paths(path, repo=Path(__file__).parents[2])
        )
        for name, path in CI_WORKFLOWS.items()
    ],
)
def test_ci_connector_gate_covers_connector_paths(workflow_path):
    workflow = workflow_path.read_text()
    if "python3 scripts/ci_changes.py classify" in workflow:
        # Both copies delegate path rules to the tested classifier (#1498, #1787).
        workflow = (Path(__file__).parents[2] / "scripts" / "ci_changes.py").read_text()
    assert "connectors(\\.py|/.*)" in workflow
    assert "slack_.*\\.py" in workflow
