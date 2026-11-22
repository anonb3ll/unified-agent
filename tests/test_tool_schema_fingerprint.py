"""Schema-surface fingerprint + client drift detection (task #1169).

Root cause of the #1165 vault clobber: long-lived MCP clients keep a connection-time
tool-schema snapshot; after a hub deploy the server signature changes but the client
still strips unknown kwargs. The fingerprint lets a client detect that drift cheaply.
"""

from __future__ import annotations

import logging

from unified_agent import tool_schema_fingerprint as tsf
from unified_agent.tools.mcp_health import mcp_health_telemetry


def _tool(a: str, b: int = 1) -> dict:
    return {"a": a, "b": b}


def _tool_extra(a: str, b: int = 1, mode: str = "replace") -> dict:
    return {"a": a, "b": b, "mode": mode}


def test_fingerprint_stable_for_identical_manifest():
    tools = {"alpha": _tool, "beta": _tool}
    assert tsf.compute_schema_fingerprint(tools) == tsf.compute_schema_fingerprint(tools)


def test_fingerprint_order_independent():
    left = {"alpha": _tool, "beta": _tool}
    right = {"beta": _tool, "alpha": _tool}
    assert tsf.compute_schema_fingerprint(left) == tsf.compute_schema_fingerprint(right)


def test_fingerprint_changes_when_arg_keys_change():
    before = tsf.compute_schema_fingerprint({"vault_write": _tool})
    after = tsf.compute_schema_fingerprint({"vault_write": _tool_extra})
    assert before != after


def test_fingerprint_changes_when_requiredness_changes():
    def optional_only(a: str = "x") -> None:
        return None

    def required(a: str) -> None:
        return None

    assert tsf.compute_schema_fingerprint({"t": optional_only}) != tsf.compute_schema_fingerprint(
        {"t": required}
    )


def test_fingerprint_changes_when_tool_added_or_removed():
    base = tsf.compute_schema_fingerprint({"alpha": _tool})
    added = tsf.compute_schema_fingerprint({"alpha": _tool, "beta": _tool})
    assert base != added


def test_fingerprint_from_tool_list_matches_callable_surface():
    tools = {"alpha": _tool, "beta": _tool_extra}
    callable_fp = tsf.compute_schema_fingerprint(tools)
    listed = [
        {
            "name": "alpha",
            "inputSchema": {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
                "required": ["a"],
            },
        },
        {
            "name": "beta",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "a": {"type": "string"},
                    "b": {"type": "integer"},
                    "mode": {"type": "string"},
                },
                "required": ["a"],
            },
        },
    ]
    assert tsf.fingerprint_from_tool_list(listed) == callable_fp


def test_drift_check_ok_when_fingerprints_match():
    fp = tsf.compute_schema_fingerprint({"alpha": _tool})
    result = tsf.check_schema_drift(fp, fp)
    assert result["drifted"] is False
    assert result["warning"] is None
    assert result["action"] is None


def test_drift_check_warns_and_requests_relist(caplog):
    session = tsf.compute_schema_fingerprint({"vault_write": _tool})
    current = tsf.compute_schema_fingerprint({"vault_write": _tool_extra})
    with caplog.at_level(logging.WARNING, logger="unified_agent.tool_schema_fingerprint"):
        result = tsf.check_schema_drift(session, current)
    assert result["drifted"] is True
    assert result["action"] == "relist_tools"
    assert "schema fingerprint drifted" in (result["warning"] or "").lower()
    assert any("schema fingerprint drifted" in r.message.lower() for r in caplog.records)


def test_schema_drift_guard_forces_relist_before_write():
    guard = tsf.SchemaDriftGuard()
    session = tsf.compute_schema_fingerprint({"vault_write": _tool})
    guard.capture(session)

    ok = guard.check_before_write(session)
    assert ok["drifted"] is False
    assert ok["relist_required"] is False

    current = tsf.compute_schema_fingerprint({"vault_write": _tool_extra})
    drifted = guard.check_before_write(current)
    assert drifted["drifted"] is True
    assert drifted["relist_required"] is True
    assert drifted["action"] == "relist_tools"

    # After acknowledging a re-list, the guard advances to the new fingerprint.
    guard.note_relisted(current)
    assert guard.check_before_write(current)["drifted"] is False


def test_mcp_health_telemetry_exposes_schema_fingerprint():
    snapshot = mcp_health_telemetry()
    fp = snapshot["schema_fingerprint"]
    assert isinstance(fp, str)
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)
    # Live registry fingerprint is stable across two reads of the same process.
    assert mcp_health_telemetry()["schema_fingerprint"] == fp
