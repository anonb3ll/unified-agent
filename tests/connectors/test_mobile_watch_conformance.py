"""Mobile projection/action connector manifest conformance (task #1311).

The routes remain hub-authenticated; the action route also retains its origin
and CSRF guard. The iPhone relay is the current boundary. These tests make no
claim that an Apple Watch client consumes the routes.
"""

from __future__ import annotations

import pytest

from unified_agent import connectors

from .harness import (
    assert_emits_hash_chained_lifecycle_events,
    assert_invalid_manifest_refused,
    assert_invisible_to_profiles_lacking_it,
    assert_registers,
    assert_reports_to_doctor_and_pane,
)

MOBILE_WATCH_MANIFEST = connectors.require_manifest("mobile-watch")


@pytest.fixture(autouse=True)
def _mobile_watch_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UA_MOBILE_WATCH_ENABLED", "1")


def test_mobile_watch_conforms_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    # The connector gate is a boolean opt-in, not a credential: test its exact
    # "1" semantics separately instead of treating any dummy secret as enabled.
    assert_registers(MOBILE_WATCH_MANIFEST, monkeypatch)
    assert_invalid_manifest_refused(MOBILE_WATCH_MANIFEST, monkeypatch)
    assert_reports_to_doctor_and_pane(MOBILE_WATCH_MANIFEST)
    assert_emits_hash_chained_lifecycle_events(MOBILE_WATCH_MANIFEST, monkeypatch)
    assert_invisible_to_profiles_lacking_it()


def test_mobile_watch_manifest_describes_inert_authenticated_routes() -> None:
    assert MOBILE_WATCH_MANIFEST.name == "mobile-watch"
    assert MOBILE_WATCH_MANIFEST.version == "1.0.0"
    assert MOBILE_WATCH_MANIFEST.default_enabled is False
    assert MOBILE_WATCH_MANIFEST.capabilities == frozenset({"projection", "action"})
    assert MOBILE_WATCH_MANIFEST.required_secrets == frozenset({"UA_MOBILE_WATCH_ENABLED"})
    assert MOBILE_WATCH_MANIFEST.entry_points == {
        "projection": "unified_agent.webui.routes:_mobile_projection",
        "action": "unified_agent.webui.routes:action_mobile",
    }


def test_mobile_watch_gate_requires_exact_opt_in_and_reports_names_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UA_MOBILE_WATCH_ENABLED", raising=False)
    with pytest.raises(connectors.ConnectorSecretsMissingError):
        connectors.ensure_can_start(MOBILE_WATCH_MANIFEST)
    off = connectors.secret_status(MOBILE_WATCH_MANIFEST, env={})
    assert off == {
        "present": [],
        "missing": ["UA_MOBILE_WATCH_ENABLED"],
        "satisfied": False,
    }
    for raw_value in ("true", " 1 "):
        not_enabled = connectors.secret_status(
            MOBILE_WATCH_MANIFEST, env={"UA_MOBILE_WATCH_ENABLED": raw_value}
        )
        assert not_enabled["satisfied"] is False
    on = connectors.secret_status(MOBILE_WATCH_MANIFEST, env={"UA_MOBILE_WATCH_ENABLED": "1"})
    assert on == {
        "present": ["UA_MOBILE_WATCH_ENABLED"],
        "missing": [],
        "satisfied": True,
    }
    monkeypatch.setenv("UA_MOBILE_WATCH_ENABLED", "1")
    assert connectors.ensure_can_start(MOBILE_WATCH_MANIFEST)["satisfied"] is True
    assert "1" not in str(on)
