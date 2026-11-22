"""Evidence-export connector manifest conformance (task #1266).

Runs the generic five-clause harness against
``connectors.EVIDENCE_EXPORT_MANIFEST`` and proves two connector-specific
things the generic harness cannot:

1. The manifest has no static ``required_secrets``/``required_secret_alternatives``
   — readiness is delegated to a hook that requires the active destination
   selector and one of the signing alternatives, the same shape as
   github-app's mode-aware hook. The generic clause 3
   (``assert_refuses_to_start_without_secrets``) has nothing to assert
   against a manifest with no static secrets, so it is skipped for this
   connector by design; ``tests/test_evidence_export_sink.py`` covers the
   real (hook-driven) missing-config refusal end to end.
2. The connector stays inert even with a destination fully configured and an
   active Horizon 2 registration, because ``evidence_export_sink.enabled()``
   is a separate runtime gate (``UA_EVIDENCE_EXPORT_ENABLED``) the manifest
   alone does not flip.
"""

from __future__ import annotations

import pytest

from unified_agent import connectors
from unified_agent.audit import evidence_export_sink as sink

from .harness import assert_connector_conforms

EVIDENCE_EXPORT_MANIFEST = connectors.require_manifest("evidence-export")


@pytest.fixture(autouse=True)
def _no_export_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "UA_EVIDENCE_EXPORT_ENABLED",
        "UA_EVIDENCE_EXPORT_DESTINATION",
        "UA_EVIDENCE_EXPORT_LOCAL_PATH",
        "UA_EVIDENCE_EXPORT_GCS_BUCKET",
        "UA_EVIDENCE_EXPORT_S3_BUCKET",
        "UA_EVIDENCE_SIGNING_KEY",
        "UA_EVIDENCE_SIGNING_SECRET_NAME",
    ):
        monkeypatch.delenv(name, raising=False)


def test_evidence_export_conforms_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    assert_connector_conforms(EVIDENCE_EXPORT_MANIFEST, monkeypatch)


def test_manifest_describes_real_resolvable_entry_point() -> None:
    assert EVIDENCE_EXPORT_MANIFEST.default_enabled is False
    assert EVIDENCE_EXPORT_MANIFEST.capabilities == frozenset({"export"})
    assert EVIDENCE_EXPORT_MANIFEST.entry_points == {
        "export": "unified_agent.audit.evidence_export_sink:run_once",
    }
    connectors.validate_entry_points_resolve(EVIDENCE_EXPORT_MANIFEST)


def test_manifest_has_no_static_required_secrets() -> None:
    """Readiness is entirely delegated to the destination-aware hook."""
    assert EVIDENCE_EXPORT_MANIFEST.required_secrets == frozenset()
    assert EVIDENCE_EXPORT_MANIFEST.required_secret_alternatives == ()
    assert EVIDENCE_EXPORT_MANIFEST.secret_status_hook == (
        "unified_agent.audit.evidence_export_sink:manifest_secret_status"
    )


def test_secret_status_delegates_to_the_hook() -> None:
    status = connectors.secret_status(EVIDENCE_EXPORT_MANIFEST, env={})
    assert status == {
        "present": [],
        "missing": [
            "UA_EVIDENCE_EXPORT_DESTINATION",
            "UA_EVIDENCE_SIGNING_KEY",
            "UA_EVIDENCE_SIGNING_SECRET_NAME",
        ],
        "satisfied": False,
    }
    status = connectors.secret_status(
        EVIDENCE_EXPORT_MANIFEST,
        env={
            "UA_EVIDENCE_EXPORT_DESTINATION": "local",
            "UA_EVIDENCE_EXPORT_LOCAL_PATH": "/tmp/wherever",
            "UA_EVIDENCE_SIGNING_SECRET_NAME": "pilot-key",
        },
    )
    assert status["satisfied"] is True
    assert status["missing"] == []


def test_stays_off_with_destination_configured_but_gate_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("UA_EVIDENCE_EXPORT_DESTINATION", "local")
    monkeypatch.setenv("UA_EVIDENCE_EXPORT_LOCAL_PATH", str(tmp_path))
    status = connectors.secret_status(EVIDENCE_EXPORT_MANIFEST)
    assert status["satisfied"] is False
    assert status["missing"] == [
        "UA_EVIDENCE_SIGNING_KEY",
        "UA_EVIDENCE_SIGNING_SECRET_NAME",
    ]
    assert sink.enabled() is False
    assert sink.run_once()["status"] == "disabled"
