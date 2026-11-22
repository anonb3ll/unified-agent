"""Cloud mirror connector registration, doctor, and pane conformance."""

from __future__ import annotations

import pytest

from unified_agent import connectors
from unified_agent.tools import doctor

from .harness import _isolated_database, assert_connector_conforms

CLOUD_MIRROR_MANIFEST = connectors.require_manifest("cloud-mirror")


def test_cloud_mirror_conforms_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    assert CLOUD_MIRROR_MANIFEST.default_enabled is False
    assert CLOUD_MIRROR_MANIFEST.required_secrets == frozenset()
    assert_connector_conforms(CLOUD_MIRROR_MANIFEST, monkeypatch)


def test_cloud_mirror_doctor_row_is_dormant_without_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _isolated_database(monkeypatch):
        row = next(
            check
            for check in doctor._connectors_check()
            if check["capability"] == "connector:cloud-mirror"
        )

    assert row["status"] == "DORMANT"
    assert "cloud-mirror" not in str(row.get("detail", ""))
