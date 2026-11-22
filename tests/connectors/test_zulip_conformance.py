"""Zulip chat and worker connector manifest conformance (task #1268)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unified_agent import connectors

from .harness import assert_connector_conforms

ZULIP_MANIFEST = connectors.require_manifest("zulip")


@pytest.fixture(autouse=True)
def _zulip_site(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UA_ZULIP_SITE", "https://zulip.example.test")


def test_zulip_conforms_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    assert_connector_conforms(ZULIP_MANIFEST, monkeypatch)


def test_zulip_manifest_describes_real_inert_entry_points() -> None:
    assert ZULIP_MANIFEST.default_enabled is False
    assert ZULIP_MANIFEST.capabilities == frozenset({"chat", "worker"})
    assert ZULIP_MANIFEST.entry_points == {
        "chat": "unified_agent.zulip_chat:maybe_reply",
        "worker": "unified_agent.zulip_worker:ZulipPollingWorker",
    }
    assert ZULIP_MANIFEST.required_secrets == frozenset()


def test_web_sample_fixture_matches_python_manifest() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[2] / "packages/web/tests/fixtures/zulip-connector.json"
    )
    fixture = json.loads(fixture_path.read_text())
    manifest = fixture["manifest"]
    assert manifest == {
        "name": ZULIP_MANIFEST.name,
        "version": ZULIP_MANIFEST.version,
        "capabilities": sorted(ZULIP_MANIFEST.capabilities),
        "required_secrets": sorted(ZULIP_MANIFEST.required_secrets),
        "entry_points": dict(ZULIP_MANIFEST.entry_points),
        "default_enabled": ZULIP_MANIFEST.default_enabled,
    }


def test_zulip_doctor_reports_inert_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from unified_agent.tools import doctor

    monkeypatch.setenv("UA_ZULIP_SITE", "https://zulip.example.test")
    check = next(
        row for row in doctor.system_doctor()["checks"] if row["capability"] == "connector:zulip"
    )
    assert check["status"] == "DORMANT"
    # Manifest has no required_secrets; the explicitly configured site is non-secret.
    assert check["gate"] == "no required secrets"
    assert "zuliprc" not in str(check)
