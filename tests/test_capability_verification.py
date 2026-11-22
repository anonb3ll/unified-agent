"""Pure-module tests for capability_verification (UA #1067)."""

from __future__ import annotations

import ast
import importlib
from datetime import date
from pathlib import Path

import pytest

from unified_agent.capability_verification import (
    EVIDENCE_MATURITY_ORDER,
    VERIFICATION_CLASSIFICATIONS,
    build_verification,
    classify_item,
    evidence_maturity,
    has_dated_live_evidence,
    is_stale,
    item_freshness,
    recommendation,
    resolve_durable_paths,
)

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "unified_agent" / "capability_verification.py"
)


def _base_item(**overrides: object) -> dict:
    item: dict = {
        "id": "test-item",
        "family": "governance-evidence",
        "capability": "Test capability",
        "consumer": "test consumer",
        "implementation_state": "shipped-and-used",
        "connection_state": "unknown",
        "evidence": [{"type": "code", "ref": "src/x.py"}],
        "boundaries": {"security": "sandbox"},
    }
    item.update(overrides)
    return item


def test_classify_covers_every_canonical_state():
    fixtures = {
        "not-applicable": _base_item(connection_state="not-applicable"),
        "speculative": _base_item(implementation_state="speculative"),
        "superseded": _base_item(implementation_state="superseded"),
        "blocked": _base_item(connection_state="blocked"),
        "misconfigured": _base_item(connection_state="misconfigured"),
        "error": _base_item(connection_state="error"),
        "degraded": _base_item(connection_state="degraded"),
        "dormant": _base_item(connection_state="dormant"),
        "connected-and-live": _base_item(
            connection_state="live",
            evidence=[{"type": "live", "ref": "probe", "date": "2026-09-17"}],
        ),
        "tested": _base_item(
            connection_state="unknown",
            evidence=[{"type": "test", "ref": "tests/t.py", "date": "2026-09-17"}],
        ),
        "implemented-in-source": _base_item(
            connection_state="unknown",
            evidence=[{"type": "code", "ref": "src/x.py"}],
        ),
        "unknown": _base_item(connection_state="unknown", evidence=[]),
    }
    assert set(fixtures) == set(VERIFICATION_CLASSIFICATIONS)
    for expected, item in fixtures.items():
        classification, _note = classify_item(item, date.fromisoformat("2026-09-17"))
        assert classification == expected, (expected, classification, _note)


def test_live_without_dated_live_evidence_is_unknown():
    item = _base_item(
        connection_state="live",
        evidence=[
            {"type": "code", "ref": "src/x.py"},
            {"type": "test", "ref": "tests/t.py"},
        ],
    )
    classification, note = classify_item(item)
    assert classification == "unknown"
    assert "never inferred" in note
    assert classification != "connected-and-live"


def test_connected_and_live_requires_dated_live_evidence():
    with_date = _base_item(
        connection_state="live",
        evidence=[{"type": "live", "ref": "probe", "date": "2026-09-17"}],
    )
    without_date = _base_item(
        connection_state="live",
        evidence=[{"type": "live", "ref": "probe"}],
    )
    assert classify_item(with_date, date.fromisoformat("2026-09-17"))[0] == "connected-and-live"
    assert has_dated_live_evidence(with_date) is True
    assert classify_item(without_date)[0] == "unknown"
    assert has_dated_live_evidence(without_date) is False


@pytest.mark.parametrize(
    "live_date", ["not-a-date", "2026-09-17garbage", "2026-09-18", "2026-08-01", "2026-09-17"]
)
def test_live_requires_real_consumer_recent_nonfuture_evidence(live_date):
    item = _base_item(
        connection_state="live",
        consumer="no verified consumer",
        evidence=[{"type": "live", "ref": "probe", "date": live_date}],
    )
    assert classify_item(item, date.fromisoformat("2026-09-17"))[0] == "unknown"

    item["consumer"] = "operators via evidence report"
    expected = "connected-and-live" if live_date == "2026-09-17" else "unknown"
    assert classify_item(item, date.fromisoformat("2026-09-17"))[0] == expected


def test_live_evidence_does_not_upgrade_unknown_connection():
    item = _base_item(
        connection_state="unknown",
        evidence=[{"type": "live", "ref": "probe", "date": "2026-09-17"}],
    )
    assert classify_item(item, date.fromisoformat("2026-09-17"))[0] == "unknown"


def test_evidence_maturity_ordering():
    assert evidence_maturity({"evidence": []}) == "none"
    assert evidence_maturity({"evidence": [{"type": "code", "ref": "a"}]}) == "code"
    assert (
        evidence_maturity(
            {
                "evidence": [
                    {"type": "code", "ref": "a"},
                    {"type": "test", "ref": "b"},
                ]
            }
        )
        == "test"
    )
    assert (
        evidence_maturity(
            {
                "evidence": [
                    {"type": "test", "ref": "b"},
                    {"type": "ci", "ref": "c"},
                ]
            }
        )
        == "ci"
    )
    assert (
        evidence_maturity(
            {
                "evidence": [
                    {"type": "ci", "ref": "c"},
                    {"type": "deployed", "ref": "d"},
                ]
            }
        )
        == "deployed"
    )
    assert (
        evidence_maturity(
            {
                "evidence": [
                    {"type": "deployed", "ref": "d"},
                    {"type": "live", "ref": "e", "date": "2026-09-17"},
                ]
            }
        )
        == "live"
    )
    # Order tuple itself is ascending maturity.
    assert EVIDENCE_MATURITY_ORDER == ("code", "test", "ci", "deployed", "live")


def test_freshness_staleness_at_window_boundary():
    assert is_stale("2026-09-17", "2026-09-03") is True  # exactly 14 days
    assert is_stale("2026-09-17", "2026-09-04") is False  # 13 days
    freshness = item_freshness(
        {
            "evidence": [
                {"type": "test", "ref": "t", "date": "2026-09-03"},
                {"type": "live", "ref": "p", "date": "2026-09-03"},
            ]
        },
        "2026-09-17",
    )
    assert freshness["stale"] is True
    assert freshness["live_evidence_date"] == "2026-09-03"
    assert freshness["latest_evidence_date"] == "2026-09-03"


def test_durable_path_resolution():
    task_only = _base_item(incomplete={"task_ref": "#1060", "smallest_next_action": "do the thing"})
    paths = resolve_durable_paths(task_only, [])
    assert paths == [{"kind": "task", "ref": "#1060", "label": "#1060"}]

    queue_match = _base_item(
        id="gov-evidence-reports",
        family="governance-evidence",
        incomplete={"smallest_next_action": "wire consumer"},
    )
    coverage = [
        {
            "task_ref": "#944",
            "status": "doing",
            "coverage_note": "covered by gov-evidence-reports",
        },
        {
            "task_ref": "#944",
            "status": "doing",
            "coverage_note": "also gov-evidence-reports",
        },
        {
            "task_ref": "#999",
            "status": "open",
            "coverage_note": "unrelated",
        },
    ]
    paths = resolve_durable_paths(queue_match, coverage)
    assert paths[0]["ref"] == "#944"
    assert "doing" in paths[0]["label"]
    assert len(paths) == 1  # deduped by ref

    named = _base_item(
        incomplete={"smallest_next_action": "named gap action that is quite long " * 3}
    )
    paths = resolve_durable_paths(named, [])
    assert len(paths) == 1
    assert paths[0]["kind"] == "named-gap"
    assert paths[0]["ref"] == ""
    assert len(paths[0]["label"]) <= 80

    assert resolve_durable_paths(_base_item(), []) == []


def test_recommendation_is_review_only():
    with_incomplete = _base_item(incomplete={"smallest_next_action": "rehearse model_quota_status"})
    rec = recommendation(with_incomplete)
    assert rec is not None
    assert rec["actionable"] is False
    assert rec["review_only"] is True
    assert "auto-creates" in rec["note"] or "review-only" in rec["note"]
    assert "rehearse" in rec["source_signal"]
    assert recommendation(_base_item()) is None
    assert recommendation(_base_item(incomplete={"owner": "x"})) is None


def test_build_verification_shape_and_missing_flags():
    no_consumer = _base_item(consumer="no verified consumer", evidence=[])
    v = build_verification(no_consumer, {"as_of": "2026-09-17"}, [])
    assert v["classification"]
    assert "consumer" in v["missing"]
    assert "evidence" in v["missing"]
    assert v["consumer_present"] is False
    assert v["evidence_present"] is False
    assert v["recommendation"] is None

    complete = _base_item(
        consumer="operators via evidence_report",
        evidence=[{"type": "test", "ref": "t", "date": "2026-09-17"}],
        incomplete={"smallest_next_action": "keep monitoring", "owner": "Operator"},
    )
    v2 = build_verification(complete, {"as_of": "2026-09-17"}, [])
    assert v2["missing"] == []
    assert v2["consumer_present"] is True
    assert v2["evidence_present"] is True
    assert v2["owner"] == "Operator"
    assert v2["recommendation"] is not None
    assert v2["recommendation"]["actionable"] is False
    assert set(v2) >= {
        "classification",
        "classification_note",
        "evidence_maturity",
        "freshness",
        "consumer_present",
        "evidence_present",
        "missing",
        "durable_path",
        "recommendation",
        "next_action",
        "owner",
        "security_boundary",
    }


def test_freshness_uses_injected_evaluation_date_over_catalog_snapshot():
    item = _base_item(
        connection_state="live",
        evidence=[{"type": "live", "ref": "probe", "date": "2026-09-17"}],
    )
    fresh = build_verification(
        item, {"as_of": "2020-01-01"}, [], now=date.fromisoformat("2026-09-17")
    )
    stale = build_verification(
        item, {"as_of": "2020-01-01"}, [], now=date.fromisoformat("2026-10-02")
    )
    assert fresh["freshness"]["stale"] is False
    assert stale["freshness"]["stale"] is True


def test_no_auto_creation_path_in_module_source():
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in (
        "task_add",
        "dispatch",
        "approval_resolve",
        "delegate",
        "vault_write",
        "message_send",
        "run_cmd",
    ):
        assert forbidden not in source, forbidden

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("unified_agent.tools")
                assert alias.name != "unified_agent"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("unified_agent.tools")
            assert mod != "unified_agent"
            # Only stdlib / relative-free: datetime, typing
            assert mod in {"datetime", "typing", "__future__"} or mod.startswith("datetime")

    # Importable and does not pull tools.
    mod = importlib.import_module("unified_agent.capability_verification")
    assert not hasattr(mod, "task_add")
    assert "task_add" not in dir(mod)
