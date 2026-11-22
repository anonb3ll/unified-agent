"""Tests for the projection-contract MCP tool (task #373 Tier 4, 5.3): the
vertical slice wiring focus_anchor -> task_reference to a real tool.

Golden focus-anchor payload duplicated (not imported) from
test_browser_projection.py's `_focus_anchor` on purpose — this file tests
the tool boundary, not the contract itself, and should not break just because
the contract's internal fixture shape changes.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent.tools.projection import browser_derive_task_reference
from unified_agent.tools.tasks import task_add, task_update


def _focus_anchor(**overrides: object) -> dict:
    payload = {
        "focus_entity": "S2 projection contract",
        "intent_category": "Deep Work (Drafting/Coding)",
        "cognitive_load_level": "High",
        "state_digest": "Drafting the versioned projection schema for gate G2.",
        "origin_ref": "b7e4f1a2c9d03e58",
    }
    payload.update(overrides.pop("payload", {}))  # type: ignore[arg-type]
    record = {
        "contract_version": "1.0",
        "kind": "focus_anchor",
        "record_id": "fa_01J9Z6Q4X0",
        "provenance": {
            "origin_machine": "desktop",
            "origin_surface": "browser",
            "captured_at": 1_754_524_800_000,
            "projected_at": 1_754_524_801_000,
            "consent_basis": "standing_local_capture",
            "trust_tier": "local",
        },
        "payload": payload,
    }
    record.update(overrides)
    return record


def test_derives_a_task_reference_for_a_real_open_task():
    task = task_add("Draft the versioned projection schema", notes="Test task")
    result = browser_derive_task_reference(_focus_anchor(), task["id"], 0.82)

    assert result["kind"] == "task_reference"
    assert result["contract_version"] == "1.0"
    assert result["payload"]["task_ref"] == f"ua://task/{task['id']}"
    assert result["payload"]["relation"] == "suggested_by_focus"
    assert result["payload"]["confidence"] == 0.82
    assert result["payload"]["focus_entity"] == "S2 projection contract"
    # Provenance carries through untouched (contract's own guarantee).
    assert result["provenance"]["origin_surface"] == "browser"
    assert result["provenance"]["trust_tier"] == "local"


def test_rejects_an_untrusted_record_that_fails_the_contract():
    bad = _focus_anchor()
    bad["payload"]["state_digest"] = "see https://example.com/secret for details"
    task = task_add("Task for a bad record", notes="Test task")
    with pytest.raises(ToolError, match="invalid projection record"):
        browser_derive_task_reference(bad, task["id"], 0.5)


def test_rejects_a_non_focus_anchor_kind():
    telemetry = _focus_anchor(kind="telemetry")
    telemetry["payload"] = {
        "cognitive_load_score": 0.5,
        "fatigue_index": 0.1,
        "context_switch_cost": 0.2,
        "intent_category": "Deep Work (Drafting/Coding)",
        "tab_switches_5m": 3,
        "session_minutes": 12,
    }
    task = task_add("Task for a telemetry record", notes="Test task")
    with pytest.raises(ToolError, match="expected a focus_anchor record"):
        browser_derive_task_reference(telemetry, task["id"], 0.5)


def test_rejects_a_task_id_that_does_not_exist():
    with pytest.raises(ToolError, match="no task with id"):
        browser_derive_task_reference(_focus_anchor(), 999_999_999, 0.5)


def test_rejects_a_terminal_task():
    task = task_add("Already finished task", notes="Test task")
    task_update(task["id"], status="done", evidence_ref="n/a", actor="test-agent")
    with pytest.raises(ToolError, match="is done"):
        browser_derive_task_reference(_focus_anchor(), task["id"], 0.5)


def test_rejects_out_of_range_confidence():
    task = task_add("Task for bad confidence", notes="Test task")
    with pytest.raises(ToolError, match="could not derive task reference"):
        browser_derive_task_reference(_focus_anchor(), task["id"], 1.5)
