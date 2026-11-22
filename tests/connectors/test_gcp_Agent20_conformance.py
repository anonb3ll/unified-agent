"""GCP agent20 connector manifest and governed registration conformance."""

from __future__ import annotations

import pytest

from unified_agent import connectors
from unified_agent.tools import gcloud

from .harness import assert_connector_conforms

GCP_Agent20_MANIFEST = connectors.require_manifest("int-gcp-agent20")


@pytest.fixture(autouse=True)
def _explicit_cloud_configuration(monkeypatch):
    monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")


def test_gcp_Agent20_conforms_without_claiming_ambient_auth_as_a_secret(monkeypatch):
    monkeypatch.setattr(
        gcloud,
        "gcp_Agent20_health",
        lambda: {"status": "LIVE", "detail": "example-worker is RUNNING"},
    )
    assert GCP_Agent20_MANIFEST.default_enabled is False
    assert GCP_Agent20_MANIFEST.required_secrets == frozenset()
    assert GCP_Agent20_MANIFEST.capabilities == frozenset(
        {"inventory", "operations", "worker_recovery"}
    )
    assert GCP_Agent20_MANIFEST.resolve_entry_point("inventory") is gcloud.gcloud_inventory
    assert GCP_Agent20_MANIFEST.resolve_entry_point("operations") is gcloud.gcloud_ops
    assert callable(GCP_Agent20_MANIFEST.resolve_entry_point("worker_recovery"))
    assert_connector_conforms(GCP_Agent20_MANIFEST, monkeypatch)


@pytest.mark.parametrize(
    "inventory,expected",
    [
        ({"status": "ok", "instances": [], "errors": []}, "DEGRADED"),
        (
            {
                "status": "degraded",
                "sections": {"instances": "unavailable"},
                "instances": [],
                "errors": [{"section": "instances", "error": "permission denied"}],
            },
            "DEGRADED",
        ),
        (
            {
                "status": "ok",
                "sections": {"instances": "ok"},
                "instances": [{"name": "example-worker", "status": "RUNNING"}],
                "errors": [],
            },
            "LIVE",
        ),
    ],
)
def test_health_requires_successful_inventory_and_running_worker(inventory, expected):
    assert gcloud.classify_gcp_Agent20_health(inventory)["status"] == expected
