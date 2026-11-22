"""Tests for machine-readable tool allowlist classification and drift detection (task #1200).

Pins the repo-owned classification of every hub-served MCP tool into exactly one of:
- REACHABLE: intended to be exposed to operator clients (120 baseline allowlist entries)
- WITHHELD: deliberately not exposed (red-tier / high-impact operations)
- OMITTED: accidental gaps to be fixed / added to allowlist upon approval

Enforces zero silent drift: fails when a served tool is unclassified or when
a classification names a tool that no longer exists in server.REGISTERED_TOOLS.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

from unified_agent import server
from unified_agent.tool_classification import (
    BASELINE_OPERATOR_REACHABLE_TOOLS,
    OMITTED_TOOLS,
    WITHHELD_TOOLS,
    ClassificationReport,
    DriftReport,
    ToolExposureClass,
    check_classification_drift,
    classify_all,
    classify_tool,
    get_served_tools,
)


def test_get_served_tools_matches_server_registered_tools():
    """Served tools must be derived dynamically from the server registry."""
    served = get_served_tools()
    assert served == frozenset(server.REGISTERED_TOOLS.keys())
    assert len(served) == len(server.REGISTERED_TOOLS)


def test_all_served_tools_classified():
    """100% of served tools must be classified into REACHABLE, WITHHELD, or OMITTED."""
    served = get_served_tools()
    report: ClassificationReport = classify_all()

    assert not report.unclassified, (
        f"Unclassified tools detected ({len(report.unclassified)}): "
        f"{sorted(report.unclassified)}. Every served tool must be classified."
    )
    assert report.served_count == len(served)
    assert report.unclassified_count == 0


def test_classifications_are_mutually_disjoint():
    """No tool may belong to more than one classification category."""
    reach_with = BASELINE_OPERATOR_REACHABLE_TOOLS & WITHHELD_TOOLS
    assert not reach_with, f"Overlap between REACHABLE and WITHHELD: {sorted(reach_with)}"

    reach_omit = BASELINE_OPERATOR_REACHABLE_TOOLS & OMITTED_TOOLS
    assert not reach_omit, f"Overlap between REACHABLE and OMITTED: {sorted(reach_omit)}"

    with_omit = WITHHELD_TOOLS & OMITTED_TOOLS
    assert not with_omit, f"Overlap between WITHHELD and OMITTED: {sorted(with_omit)}"


def test_exact_tool_counts():
    """Pin the exact counts: 201 served = 124 REACHABLE + 26 WITHHELD + 51 OMITTED."""
    report = classify_all()
    assert report.served_count == 201
    assert report.reachable_count == 124
    assert report.withheld_count == 26
    assert report.omitted_count == 51
    assert report.unclassified_count == 0
    assert (
        report.reachable_count + report.withheld_count + report.omitted_count == report.served_count
    )


def test_key_tool_classifications():
    """Verify specific tools required by task #1200 spec are properly classified."""
    # Red-tier deliberate withholds
    assert classify_tool("approval_resolve") == ToolExposureClass.WITHHELD
    assert classify_tool("memory_delete") == ToolExposureClass.WITHHELD
    assert classify_tool("task_delete") == ToolExposureClass.WITHHELD
    assert classify_tool("managed_secret_set") == ToolExposureClass.WITHHELD
    assert classify_tool("deployment_claim") == ToolExposureClass.WITHHELD
    assert classify_tool("import_migration_manifest") == ToolExposureClass.WITHHELD
    assert classify_tool("export_data") == ToolExposureClass.WITHHELD
    assert classify_tool("gcloud_ops") == ToolExposureClass.WITHHELD
    assert classify_tool("crew_intervene") == ToolExposureClass.WITHHELD
    assert classify_tool("work_product_submit") == ToolExposureClass.WITHHELD

    # Accidental gaps (omissions) to be fixed
    assert classify_tool("system_doctor") == ToolExposureClass.OMITTED
    assert classify_tool("event_log_verify") == ToolExposureClass.OMITTED
    assert classify_tool("task_claim") == ToolExposureClass.OMITTED
    assert classify_tool("mcp_health_telemetry") == ToolExposureClass.OMITTED
    assert classify_tool("export_tasks") == ToolExposureClass.OMITTED
    assert classify_tool("export_memory") == ToolExposureClass.OMITTED
    assert classify_tool("export_journal") == ToolExposureClass.OMITTED
    assert classify_tool("export_migration_manifest") == ToolExposureClass.OMITTED
    assert classify_tool("langgraph_run") == ToolExposureClass.OMITTED
    assert classify_tool("langgraph_checkpoints") == ToolExposureClass.OMITTED
    assert classify_tool("langgraph_progress_note") == ToolExposureClass.OMITTED
    assert classify_tool("crew_view") == ToolExposureClass.OMITTED
    assert classify_tool("dispatch_delivery_receipts_list") == ToolExposureClass.OMITTED

    # Baseline operator reachable tools
    assert classify_tool("fs_read") == ToolExposureClass.REACHABLE
    assert classify_tool("run_cmd") == ToolExposureClass.REACHABLE
    assert classify_tool("task_add") == ToolExposureClass.REACHABLE
    assert classify_tool("task_list") == ToolExposureClass.REACHABLE
    assert classify_tool("task_update") == ToolExposureClass.REACHABLE


def test_drift_check_passes_on_current_registry():
    """Drift check must be completely clean on current codebase."""
    drift: DriftReport = check_classification_drift()
    assert drift.passed, (
        f"Drift check failed: unclassified={drift.unclassified_tools}, "
        f"stale={drift.stale_tools}, overlaps={drift.overlapping_tools}"
    )


def test_drift_check_fails_on_injected_unclassified_tool():
    """Drift check must fail when an unclassified tool is served."""
    served_with_extra = set(get_served_tools()) | {"synthetic_unclassified_tool"}
    drift = check_classification_drift(served_tools=served_with_extra)
    assert not drift.passed
    assert "synthetic_unclassified_tool" in drift.unclassified_tools
    assert any("synthetic_unclassified_tool" in msg for msg in drift.errors)


def test_drift_check_fails_on_missing_stale_tool():
    """Drift check must fail when a tool in classification is not served."""
    # Omit one tool from served
    served_missing = set(get_served_tools()) - {"system_doctor"}
    drift = check_classification_drift(served_tools=served_missing)
    assert not drift.passed
    assert "system_doctor" in drift.stale_tools
    assert any("system_doctor" in msg for msg in drift.errors)


def test_structured_output_generation():
    """Verify structured output (dict/JSON and markdown table) generation."""
    report = classify_all()
    data = report.to_dict()

    assert data["served_count"] == 201
    assert data["counts"]["REACHABLE"] == 124
    assert data["counts"]["WITHHELD"] == 26
    assert data["counts"]["OMITTED"] == 51
    assert data["counts"]["UNCLASSIFIED"] == 0
    assert len(data["tools"]) == 201

    md_table = report.to_markdown_table()
    assert "| Tool | Classification | Category |" in md_table
    assert "| `approval_resolve` | `WITHHELD` |" in md_table
    assert "| `system_doctor` | `OMITTED` |" in md_table
    assert "| `run_cmd` | `REACHABLE` |" in md_table


def test_cli_entry_point_is_declared_and_callable():
    """The classification CLI is discoverable through the project package."""
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    target = project["project"]["scripts"]["unified-agent-tool-classification"]
    module_name, function_name = target.split(":")
    cli = getattr(importlib.import_module(module_name), function_name)

    assert callable(cli)
    assert cli(["--check"]) == 0


def test_doctor_tool_classification_check():
    """Verify system_doctor includes tool_classification check and reports LIVE."""
    from unified_agent.tools.doctor import system_doctor

    doc = system_doctor()
    by_cap = {c["capability"]: c for c in doc["checks"]}

    assert "tool_classification" in by_cap, "tool_classification check missing from system_doctor"
    check = by_cap["tool_classification"]
    assert check["status"] == "LIVE"
    assert check["gate"] == "tool_classification.py classification sets"
    assert "0 drift across 201 served tools" in check["detail"]


def test_doctor_tool_classification_check_reports_misconfigured_on_drift(monkeypatch):
    """Verify system_doctor reports MISCONFIGURED when drift is detected."""
    from unified_agent import tool_classification
    from unified_agent.tools.doctor import _tool_classification_drift_check

    # Mock check_classification_drift to return an error
    fake_drift = DriftReport(
        passed=False,
        served_count=178,
        unclassified_tools=["injected_tool"],
        stale_tools=[],
        overlapping_tools=[],
        errors=["Found 1 unclassified served tools: injected_tool."],
    )
    monkeypatch.setattr(tool_classification, "check_classification_drift", lambda: fake_drift)

    res = _tool_classification_drift_check()
    assert res["status"] == "MISCONFIGURED"
    assert "injected_tool" in res["detail"]
