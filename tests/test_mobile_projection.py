"""Deterministic tests for the mobile.v1 projection validator (task #416)."""

from __future__ import annotations

import copy
import json

import pytest

from unified_agent.projection import mobile_v1 as mobile


def _item(**overrides: object) -> dict:
    value = {
        "item_ref": "attention:approval:123",
        "kind": "approval",
        "severity": "high",
        "age_s": 42,
        "safe_title": "Human decision needed",
        "deep_link_ref": "ua://control-center/attention/approval/123",
    }
    value.update(overrides)
    return value


def _projection(**overrides: object) -> dict:
    value = {
        "schema_version": "mobile.v1",
        "projection_id": "mp_01J9Z6Q4X0",
        "generated_at": "2026-08-16T20:00:00Z",
        "expires_at": "2026-08-16T20:05:00Z",
        "freshness": "live",
        "source": {
            "source_host": "desktop",
            "hub_origin": "local",
            "surface": "unified-agent",
        },
        "attention": {
            "total": 1,
            "highest_severity": "high",
            "items": [_item()],
        },
        "health": {"state": "healthy", "safe_summary": "Service available"},
        "notification": {
            "event_ref": "attention:approval:123",
            "safe_title": "Unified-agent needs attention",
            "deep_link_ref": "ua://control-center/attention/approval/123",
        },
        "error": None,
        "fallback": {"surface": "iphone", "deep_link_ref": "ua://control-center"},
    }
    value.update(overrides)
    return value


def test_valid_projection_round_trips():
    parsed = mobile.parse_mobile_projection(_projection())
    assert parsed.schema_version == "mobile.v1"
    assert parsed.attention.items[0].safe_title == "Human decision needed"
    assert mobile.to_wire(parsed) == _projection()


def test_json_input_is_supported():
    assert mobile.parse_mobile_projection(
        json.dumps(_projection())
    ) == mobile.parse_mobile_projection(_projection())


def test_expired_projection_is_accepted_but_marked_by_its_freshness():
    parsed = mobile.parse_mobile_projection(_projection(freshness="stale"))
    assert parsed.freshness == "stale"


def test_expiry_must_follow_generation():
    with pytest.raises(mobile.MobileProjectionError, match="expires_at"):
        mobile.parse_mobile_projection(_projection(expires_at="2026-08-16T19:59:59Z"))


def test_unknown_top_level_field_is_rejected():
    value = _projection()
    value["task_notes"] = "private"
    with pytest.raises(mobile.MobileProjectionError, match="top-level"):
        mobile.parse_mobile_projection(value)


def test_raw_reason_and_title_cannot_cross_the_item_boundary():
    value = _projection()
    value["attention"]["items"][0]["reason"] = "private detail"
    with pytest.raises(mobile.MobileProjectionError, match="attention item"):
        mobile.parse_mobile_projection(value)


def test_safe_title_is_allowlisted_by_kind():
    value = _projection()
    value["attention"]["items"][0]["safe_title"] = "Private approval for payroll"
    with pytest.raises(mobile.MobileProjectionError, match="safe_title"):
        mobile.parse_mobile_projection(value)


def test_deep_link_must_be_opaque_ua_reference():
    value = _projection()
    value["attention"]["items"][0]["deep_link_ref"] = "https://example.com/private"
    with pytest.raises(mobile.MobileProjectionError, match="deep_link_ref"):
        mobile.parse_mobile_projection(value)


def test_notification_is_generic_and_bounded():
    value = _projection()
    value["notification"]["safe_title"] = "Private task: payroll"
    with pytest.raises(mobile.MobileProjectionError, match="notification"):
        mobile.parse_mobile_projection(value)


def test_notification_can_be_absent():
    value = _projection()
    value["notification"] = None
    assert mobile.parse_mobile_projection(value).notification is None


def test_source_is_provenance_not_authority():
    value = _projection()
    value["source"]["source_host"] = "remote-host"
    parsed = mobile.parse_mobile_projection(value)
    assert parsed.source.source_host == "remote-host"


@pytest.mark.parametrize("freshness", ["live", "stale", "offline", "error"])
def test_freshness_values_are_pinned(freshness: str):
    assert mobile.parse_mobile_projection(_projection(freshness=freshness)).freshness == freshness


def test_unknown_freshness_is_rejected():
    with pytest.raises(mobile.MobileProjectionError, match="freshness"):
        mobile.parse_mobile_projection(_projection(freshness="maybe"))


def test_attention_count_and_severity_must_agree():
    value = _projection()
    value["attention"] = {"total": 0, "highest_severity": "none", "items": [_item()]}
    with pytest.raises(mobile.MobileProjectionError, match="total"):
        mobile.parse_mobile_projection(value)


def test_empty_attention_uses_none_severity():
    value = _projection()
    value["attention"] = {"total": 0, "highest_severity": "none", "items": []}
    parsed = mobile.parse_mobile_projection(value)
    assert parsed.attention.highest_severity == "none"


def test_duplicate_projection_id_is_still_a_valid_transport_identifier():
    first = mobile.parse_mobile_projection(_projection())
    second = mobile.parse_mobile_projection(_projection())
    assert first.projection_id == second.projection_id


def test_nested_mutation_does_not_change_fixture_expectation():
    value = _projection()
    original = copy.deepcopy(value)
    mobile.parse_mobile_projection(value)
    assert value == original


def test_size_limit_is_enforced():
    value = _projection()
    value["source"]["source_host"] = "x" * 100
    with pytest.raises(mobile.MobileProjectionError, match="source_host"):
        mobile.parse_mobile_projection(value)


def test_size_boundary_can_be_selected_explicitly():
    with pytest.raises(mobile.MobileProjectionError, match="iPhone size limit"):
        mobile.parse_mobile_projection(_projection(), max_bytes=100)


@pytest.mark.parametrize("raw", [None, 42, [], "not-json", b"bytes"])
def test_malformed_input_is_rejected(raw: object):
    with pytest.raises(mobile.MobileProjectionError):
        mobile.parse_mobile_projection(raw)


def test_build_mobile_projection_from_attention_and_health():
    attention_data = {
        "items": [
            {
                "kind": "review_wait",
                "severity": "high",
                "source_subject": "task:101",
                "age_s": 120,
                "reason": "Waiting for review",
            },
            {
                "kind": "approval",
                "severity": "medium",
                "source_subject": "approval:42",
                "age_s": 60,
                "reason": "Approval pending",
            },
        ]
    }
    health_data = {"state": "healthy", "summary": {"status": "healthy"}}
    proj = mobile.build_mobile_projection(
        attention_data=attention_data,
        health_data=health_data,
        source_host="desktop-host",
        hub_origin="local",
        freshness="live",
    )
    wire = mobile.to_wire(proj)
    parsed = mobile.parse_mobile_projection(wire)
    assert parsed.schema_version == "mobile.v1"
    assert parsed.attention.total == 2
    assert parsed.attention.highest_severity == "high"
    assert parsed.health.state == "healthy"
    assert parsed.health.safe_summary == "Service available"
    assert parsed.notification is not None
    assert parsed.notification.safe_title == "Unified-agent needs attention"
    assert parsed.fallback.surface == "iphone"


def test_build_mobile_projection_empty_attention():
    proj = mobile.build_mobile_projection(
        attention_data={"items": []},
        health_data={"state": "degraded"},
        freshness="stale",
        error_code="hub_unavailable",
    )
    wire = mobile.to_wire(proj)
    parsed = mobile.parse_mobile_projection(wire)
    assert parsed.attention.total == 0
    assert parsed.attention.highest_severity == "none"
    assert parsed.health.state == "degraded"
    assert parsed.health.safe_summary == "Service degraded"
    assert parsed.freshness == "stale"
    assert parsed.error == "hub_unavailable"
    assert parsed.notification is None
