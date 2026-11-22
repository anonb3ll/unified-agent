"""Tests for Task #781: P3.1 Evidence report surface (human->device->agent->action->proof)."""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, events, server, storage, webui
from unified_agent.audit import evidence_cli, evidence_report


@pytest.fixture(autouse=True)
def init_clean_db(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    monkeypatch.setattr(config, "current_source_host", lambda: "test-device.local")
    storage.init_db()
    yield


def _insert_task(
    *,
    id: int,
    title: str,
    status: str = "done",
    claimed_by: str | None = "Desktop_agent3",
    source_host: str | None = "desktop-linux",
    evidence_ref: str | None = "https://github.com/anonb3ll/unified-agent/pull/383",
    notes: str = "",
    project: str = "unified-agent",
    completed_at: str | None = "2026-09-06T10:25:15+00:00",
    created_at: str = "2026-09-06T09:00:00+00:00",
    updated_at: str = "2026-09-06T10:25:15+00:00",
    parent_id: int | None = None,
) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, completed_at, "
            "source_host, claimed_by, evidence_ref, notes, project, domain, lane, parent_id) "
            "VALUES (?, ?, ?, 'high', ?, ?, ?, ?, ?, ?, ?, ?, 'hobby', 'hobby', ?)",
            (
                id,
                title,
                status,
                created_at,
                updated_at,
                completed_at,
                source_host,
                claimed_by,
                evidence_ref,
                notes,
                project,
                parent_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_build_evidence_report_single_task(monkeypatch):
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    _insert_task(
        id=777,
        title="feat(langgraph): governed plan-build-verify-merge chain",
        claimed_by="Desktop_Agent4",
        source_host="example-host.local",
        evidence_ref="https://github.com/anonb3ll/unified-agent/pull/383",
        notes="PR: https://github.com/anonb3ll/unified-agent/pull/383 SHA: baece57",
        completed_at="2026-09-06T10:25:15+00:00",
    )

    events.record_event(
        "agent.task.completed",
        payload={"task_id": 777, "status": "done", "pr": "383"},
        originator="Desktop_Agent4",
    )

    report = evidence_report.build_evidence_report(task_id=777)
    assert report["report_type"] == "user_attributable_evidence"
    assert report["schema_version"] == "1.8"
    assert "complete" not in report
    assert report["total_records"] == 1
    assert report["chain_integrity"]["status"] == "valid"
    assert report["chain_integrity"]["verified"] is True
    assert report["chain_integrity"]["unhashed_skipped"] == 0

    rec = report["records"][0]
    assert rec["task_id"] == 777
    assert rec["human"] == "unknown"
    assert rec["human_provenance"]["verified"] is False
    assert rec["device"] == "unverified-device:example-host.local"
    assert rec["agent"] == "Desktop_Agent4"
    assert rec["action"]["title"] == "feat(langgraph): governed plan-build-verify-merge chain"
    assert rec["action"]["status"] == "done"
    assert rec["proof"]["evidence_ref"] == "https://github.com/anonb3ll/unified-agent/pull/383"
    assert rec["proof"]["pr_url"] == "https://github.com/anonb3ll/unified-agent/pull/383"
    assert rec["proof"]["commit_sha"] == "baece57"
    assert rec["proof"]["completed_at"] == "2026-09-06T10:25:15+00:00"
    assert len(rec["proof"]["event_hashes"]) == 1
    assert rec["proof"]["tamper_evident"] is True


def test_missing_human_is_unknown_and_valid_chain_does_not_authenticate_actor(monkeypatch):
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    _insert_task(id=778, title="unattributed task", notes="")
    events.record_event("agent.task.completed", payload={"task_id": 778}, originator="forged-label")
    rec = evidence_report.build_evidence_report(task_id=778)["records"][0]
    assert rec["human"] == "unknown"
    assert rec["human_provenance"] == {
        "value": "unknown",
        "source": "unknown",
        "candidates": [],
        "actor_kind": "unknown",
        "human_authority": "unknown",
        "credential_attributed": False,
        "verified": False,
        "conflict": False,
    }
    report = evidence_report.build_evidence_report(task_id=778)
    assert report["chain_integrity"]["verified"] is True
    assert report["chain_integrity"]["actor_authentication_verified"] is False
    assert report["chain_integrity"]["provenance_verified"] is False


def test_conflicting_human_candidates_are_visible_and_unverified():
    _insert_task(id=779, title="conflicting attribution", notes="[human: Another Person]")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO approvals (tool, payload, status, requested_by, resolved_by, created_at, updated_at) "
            "VALUES ('tool', '{\"task_id\": 779}', 'approved', 'agent', 'Desktop_Person', 'now', 'now')"
        )
        conn.commit()
    finally:
        conn.close()
    rec = evidence_report.build_evidence_report(task_id=779)["records"][0]
    assert rec["human"] == "Desktop_Person"
    assert rec["human_provenance"]["conflict"] is True
    assert rec["human_provenance"]["verified"] is False
    assert {candidate["value"] for candidate in rec["human_provenance"]["candidates"]} == {
        "Desktop_Person",
        "Another Person",
    }


def test_build_evidence_report_with_approvals_and_delegations():
    _insert_task(
        id=780,
        title="P2.F7 Connector pruning",
        status="open",
        claimed_by="Desktop_Agent5",
        source_host="DesktopHost",
        evidence_ref="commit cd780ba",
        completed_at=None,
    )

    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO approvals (id, tool, payload, status, requested_by, resolved_by, reason, created_at, updated_at, resolved_at) "
            "VALUES (1, 'git_merge_task', '{\"task_id\":780}', 'approved', 'Desktop_Agent5', 'Desktop_Person', 'LGTM', "
            "'2026-09-06T10:00:00+00:00', '2026-09-06T10:05:00+00:00', '2026-09-06T10:05:00+00:00')",
        )
        conn.commit()
    finally:
        conn.close()

    report = evidence_report.build_evidence_report(task_id=780)
    rec = report["records"][0]
    assert rec["human"] == "Desktop_Person"
    assert len(rec["action"]["approvals"]) == 1
    assert rec["action"]["approvals"][0]["status"] == "approved"
    assert rec["action"]["approvals"][0]["resolved_by"] == "Desktop_Person"


def test_build_evidence_report_filters():
    _insert_task(id=101, title="Task A", claimed_by="Desktop_agent3", project="unified-agent")
    _insert_task(id=102, title="Task B", claimed_by="Desktop_Agent2", project="browser")
    _insert_task(id=103, title="Task C", claimed_by="Desktop_agent3", project="browser")

    rep_agent = evidence_report.build_evidence_report(agent="Desktop_agent3")
    assert rep_agent["total_records"] == 2

    rep_proj = evidence_report.build_evidence_report(project="browser")
    assert rep_proj["total_records"] == 2

    rep_both = evidence_report.build_evidence_report(agent="Desktop_agent3", project="browser")
    assert rep_both["total_records"] == 1
    assert rep_both["records"][0]["task_id"] == 103


def test_project_evidence_report_walks_subtree_and_orders_merge_chain():
    _insert_task(
        id=900,
        title="Project spec",
        status="doing",
        completed_at=None,
        evidence_ref=None,
        notes="[project-spec]",
    )
    _insert_task(
        id=901,
        title="Step one",
        parent_id=900,
        evidence_ref="https://github.com/anonb3ll/unified-agent/pull/423",
        notes="[review: merged]",
        completed_at="2026-09-07T10:00:00+00:00",
    )
    _insert_task(
        id=902,
        title="Nested step",
        parent_id=901,
        evidence_ref="https://github.com/anonb3ll/unified-agent/pull/424 commit abc1234",
        notes="[review: merged-upstream]",
        completed_at="2026-09-08T10:00:00+00:00",
    )
    _insert_task(
        id=903,
        title="Open sibling",
        status="open",
        parent_id=900,
        evidence_ref=None,
        completed_at=None,
    )
    storage.record_agent_event("task", ref_id="901", agent="Desktop_Agent4", summary="done")
    storage.record_agent_event("task", ref_id="902", agent="Desktop_Agent4", summary="done")

    report = evidence_report.build_evidence_report(project_id=900)

    assert [record["task_id"] for record in report["records"]] == [900, 901, 902, 903]
    assert [record["action"]["project_depth"] for record in report["records"]] == [0, 1, 2, 1]
    assert report["filter"]["limit"] == 500
    chain = report["project_chain"]
    assert chain["status"] == "partial"
    assert chain["task_ids"] == [900, 901, 902, 903]
    assert chain["done_task_ids"] == [901, 902]
    assert chain["merged_prs"] == []
    assert chain["last_merge"] is None


def test_project_evidence_report_missing_or_incomplete_is_defined_not_fabricated():
    with pytest.raises(ToolError, match="no task with id 999"):
        evidence_report.build_evidence_report(project_id=999)

    _insert_task(
        id=910,
        title="Project without completed steps",
        status="doing",
        completed_at=None,
        evidence_ref=None,
        notes="[project-spec]",
    )
    _insert_task(
        id=911,
        title="Pending step",
        status="open",
        parent_id=910,
        completed_at=None,
        evidence_ref=None,
    )
    partial = evidence_report.build_evidence_report(project_id=910)
    assert partial["project_chain"]["status"] == "partial"
    assert partial["project_chain"]["done_task_ids"] == []
    assert partial["project_chain"]["last_merge"] is None


def test_project_chain_does_not_treat_open_pr_as_merge_proof():
    _insert_task(
        id=930,
        title="Project spec",
        status="doing",
        completed_at=None,
        evidence_ref=None,
        notes="[project-spec]",
    )
    _insert_task(
        id=931,
        title="Done but unmerged",
        parent_id=930,
        evidence_ref="https://github.com/anonb3ll/unified-agent/pull/999",
    )

    chain = evidence_report.build_evidence_report(project_id=930)["project_chain"]

    assert chain["status"] == "partial"
    assert [item["task_id"] for item in chain["evidence"]] == [931]
    assert chain["merged_prs"] == []
    assert chain["last_merge"] is None


def test_stale_merged_note_does_not_bless_new_evidence_ref_pr():
    """UA #845: a leftover [review: merged] note must not verify a newer PR URL."""
    _insert_task(
        id=970,
        title="Project spec",
        status="doing",
        completed_at=None,
        evidence_ref=None,
        notes="[project-spec]",
    )
    _insert_task(
        id=971,
        title="Stale merge note plus new PR",
        parent_id=970,
        evidence_ref="https://github.com/anonb3ll/unified-agent/pull/999",
        notes=("[review: merged] https://github.com/anonb3ll/unified-agent/pull/423"),
    )

    report = evidence_report.build_evidence_report(project_id=970)
    rec = next(r for r in report["records"] if r["task_id"] == 971)
    assert rec["proof"]["pr_url"] == "https://github.com/anonb3ll/unified-agent/pull/999"
    assert rec["proof"]["merge_verified"] is False
    chain = report["project_chain"]
    assert chain["status"] == "partial"
    assert chain["merged_prs"] == []
    assert chain["last_merge"] is None


def test_matching_evidence_ref_keeps_merge_note_as_unverified_claim():
    _insert_task(
        id=980,
        title="Project spec",
        status="doing",
        completed_at=None,
        evidence_ref=None,
        notes="[project-spec]",
    )
    _insert_task(
        id=981,
        title="Same PR in note and evidence_ref",
        parent_id=980,
        evidence_ref="https://github.com/anonb3ll/unified-agent/pull/423",
        notes="[review: merged] https://github.com/anonb3ll/unified-agent/pull/423",
    )

    rec = evidence_report.build_evidence_report(task_id=981)["records"][0]
    assert rec["proof"]["merge_verified"] is False
    assert rec["proof"]["merge_claim"]["status"] == "inferred"


def test_evidence_report_docs_state_both_limit_defaults():
    text = (Path(__file__).resolve().parents[1] / "docs/evidence-report.md").read_text(
        encoding="utf-8"
    )
    assert "default 50" in text
    assert re.search(
        r"project(?:_id| subtree| queries).{0,80}default(?:s)? to 500",
        text,
        re.I | re.S,
    )


def test_project_chain_includes_note_derived_unverified_merge_claim():
    _insert_task(
        id=940,
        title="Project spec",
        status="doing",
        completed_at=None,
        evidence_ref=None,
        notes="[project-spec]",
    )
    _insert_task(
        id=941,
        title="Merged from notes",
        parent_id=940,
        evidence_ref=None,
        notes=(
            "PR: https://github.com/anonb3ll/unified-agent/pull/941 "
            "commit: abc9410 [review: merged]"
        ),
    )

    chain = evidence_report.build_evidence_report(project_id=940)["project_chain"]

    assert chain["status"] == "partial"
    assert chain["evidence"][0]["evidence_ref"] is None
    assert chain["merged_prs"] == []


def test_project_chain_uses_full_subtree_before_display_filters_and_limit():
    _insert_task(
        id=950,
        title="Project spec",
        status="doing",
        completed_at=None,
        evidence_ref=None,
        notes="[project-spec]",
    )
    _insert_task(
        id=951,
        title="First merged step",
        parent_id=950,
        evidence_ref=None,
        notes=("https://github.com/anonb3ll/unified-agent/pull/951 [review: merged]"),
    )
    _insert_task(
        id=952,
        title="Last merged step",
        parent_id=950,
        evidence_ref=None,
        notes=("https://github.com/anonb3ll/unified-agent/pull/952 [review: merged]"),
        completed_at="2026-09-08T12:00:00+00:00",
    )

    report = evidence_report.build_evidence_report(project_id=950, task_id=951, limit=1)

    assert [record["task_id"] for record in report["records"]] == [951]
    assert report["project_chain"]["task_ids"] == [950, 951, 952]
    assert report["project_chain"]["last_merge"] is None


def test_project_evidence_report_rejects_non_project_root():
    _insert_task(id=960, title="Ordinary task", notes="")
    _insert_task(id=961, title="Marker documentation", notes="`[project-spec]`")

    with pytest.raises(ToolError, match=r"does not carry \[project-spec\]"):
        evidence_report.build_evidence_report(project_id=960)
    with pytest.raises(ToolError, match=r"does not carry \[project-spec\]"):
        evidence_report.build_evidence_report(project_id=961)


@pytest.mark.anyio
async def test_project_evidence_report_mcp_roundtrip_returns_structured_content():
    _insert_task(
        id=920,
        title="MCP project spec",
        status="doing",
        completed_at=None,
        evidence_ref=None,
        notes="[project-spec]",
    )
    _insert_task(id=921, title="MCP completed step", parent_id=920)

    async with Client(server.mcp) as client:
        result = await client.session.call_tool("evidence_report", {"project_id": 920})

    assert not result.is_error
    assert result.structured_content["project_chain"]["project_id"] == 920
    assert result.structured_content["project_chain"]["done_task_ids"] == [921]
    assert result.content[0].type == "text"


def test_format_evidence_report():
    completed_at = storage.now()
    _insert_task(
        id=201,
        title="Evidence format test",
        claimed_by="Desktop_agent3",
        completed_at=completed_at,
        updated_at=completed_at,
    )
    report = evidence_report.build_evidence_report(task_id=201)
    record = report["records"][0]
    assert report["attribution_coverage"]["gate"] == "pass"
    assert report["clean_pass"] is True
    assert record["human_provenance"]["verified"] is False
    assert record["device_link"]["verified"] is False

    json_str = evidence_report.format_evidence_report(report, fmt="json")
    parsed = json.loads(json_str)
    assert parsed["report_type"] == "user_attributable_evidence"

    md_str = evidence_report.format_evidence_report(report, fmt="markdown")
    assert "# User-Attributable Agent Evidence Report" in md_str
    assert "| #201 Evidence format test |" in md_str
    assert "Desktop_agent3" in md_str
    assert "Actor authentication" in md_str
    assert "unverified" in md_str
    assert "Attribution coverage (platform-wide coverage window" in md_str
    assert "trailing window" not in md_str
    assert "independent of the displayed task filter" in md_str
    assert "do not certify each row's human or device attribution" in md_str
    assert "**Clean pass**: `true`" in md_str
    assert "**Gate failures**: none" in md_str
    coverage = report["attribution_coverage"]
    assert (
        f"platform-wide coverage window {coverage['window_start']} through {coverage['window_end']}"
    ) in md_str

    text_str = evidence_report.format_evidence_report(report, fmt="text")
    assert "EVIDENCE REPORT" in text_str
    assert "[Task #201]" in text_str
    assert "actor authentication unavailable" in text_str
    assert "Attribution coverage (platform-wide coverage window" in text_str
    assert "trailing window" not in text_str
    assert "independent of the displayed task filter" in text_str
    assert "do not certify each row's human or device attribution" in text_str
    assert "Clean pass: true" in text_str
    assert "Gate failures: none" in text_str

    csv_str = evidence_report.format_evidence_report(report, fmt="csv")
    csv_rows = list(csv.reader(io.StringIO(csv_str)))
    assert csv_rows[0][:8] == [
        "record_type",
        "task_id",
        "project",
        "human",
        "device",
        "agent",
        "action_type",
        "action_title",
    ]
    assert "human_source" in csv_rows[0]
    assert "human_credential_attributed" in csv_rows[0]
    assert "human_verified" not in csv_rows[0]
    data_row = dict(zip(csv_rows[0], csv_rows[1], strict=True))
    assert data_row["record_type"] == "record"
    assert data_row["task_id"] == "201"
    assert data_row["action_title"] == "Evidence format test"
    assert data_row["agent"] == "Desktop_agent3"
    assert "chain_status" in data_row and "chain_head_hash" in data_row


@pytest.mark.parametrize("fmt", ["json", "markdown", "text", "csv", "pdf"])
def test_project_repo_binding_is_visible_in_every_export_format(fmt):
    _insert_task(id=212, title="Project binding export")
    report = evidence_report.build_evidence_report(task_id=212)
    binding = {
        "project": "unified-agent",
        "binding": {
            "repo": "acme/unified-agent",
            "default_branch": "main",
            "source": "db",
        },
    }
    report["project_repo_binding"] = binding

    rendered = evidence_report.format_evidence_report(report, fmt=fmt)
    if fmt == "pdf":
        assert isinstance(rendered, bytes)
        assert rendered.startswith(b"%PDF")
        rendered = _pdf_text(rendered)
        assert "Task #212" in rendered
        assert "Project binding export" in rendered

    assert "acme/unified-agent" in rendered
    if fmt == "json":
        assert json.loads(rendered)["project_repo_binding"] == binding
    elif fmt == "csv":
        rows = list(csv.DictReader(io.StringIO(rendered)))
        assert rows
        for row in rows:
            assert "project_repo_binding" in row
            assert json.loads(row["project_repo_binding"]) == binding
    else:
        assert "Project Repository Binding" in rendered
        assert "unified-agent" in rendered
        assert "main" in rendered
        assert "db" in rendered


def test_empty_csv_export_keeps_project_repo_binding_on_metadata_row():
    report = evidence_report.build_evidence_report(task_id=9999999)
    binding = {
        "project": "unified-agent",
        "binding": {"repo": "acme/ua", "default_branch": "main", "source": "db"},
    }
    report["project_repo_binding"] = binding

    rendered = evidence_report.format_evidence_report(report, fmt="csv")
    rows = list(csv.DictReader(io.StringIO(rendered)))

    assert len(rows) == 1
    assert rows[0]["record_type"] == "report_metadata"
    assert rows[0]["attestation_attested_at"]
    assert "project_repo_binding" in rows[0]
    assert json.loads(rows[0]["project_repo_binding"]) == binding


@pytest.mark.parametrize("fmt", ["json", "markdown", "text", "csv", "pdf"])
def test_contradictory_human_candidates_are_visible_in_every_export(fmt):
    _insert_task(
        id=202,
        title="Contradictory attribution",
        notes="[human: Note Person] [created-by: Created Person]",
    )
    report = evidence_report.build_evidence_report(task_id=202)
    rendered = evidence_report.format_evidence_report(report, fmt=fmt)
    if isinstance(rendered, bytes):
        rendered = _pdf_text(rendered)
    assert "Note Person" in rendered
    assert "Created Person" in rendered
    if fmt in ("json", "csv"):
        assert "task.notes" in rendered
        assert "false" in rendered.lower()
    else:
        assert "unverified" in rendered.lower()


def test_csv_export_is_tamper_evident_and_self_certifying_per_row():
    """Every row repeats the chain status/head so a re-sorted or filtered
    export (e.g. opened in a spreadsheet and re-saved) still carries the
    tamper-evidence signal without needing the original JSON alongside it."""
    _insert_task(id=202, title="First row", claimed_by="Desktop_agent3")
    _insert_task(id=203, title="Second row", claimed_by="Desktop_Agent1")
    report = evidence_report.build_evidence_report()

    csv_str = evidence_report.format_evidence_report(report, fmt="csv")
    rows = list(csv.DictReader(io.StringIO(csv_str)))
    assert len(rows) >= 2
    chain_statuses = {r["chain_status"] for r in rows}
    chain_heads = {r["chain_head_hash"] for r in rows}
    # A single evaluation of the chain applies to the whole report — every
    # row must agree, not carry independently-computed values.
    assert len(chain_statuses) == 1
    assert len(chain_heads) == 1


def test_csv_export_handles_an_empty_report():
    report = evidence_report.build_evidence_report(task_id=9999999)
    csv_str = evidence_report.format_evidence_report(report, fmt="csv")
    rows = list(csv.DictReader(io.StringIO(csv_str)))
    assert len(rows) == 1
    assert rows[0]["record_type"] == "report_metadata"
    assert rows[0]["attestation_attested_at"]


def _pdf_text(pdf_bytes: bytes) -> str:
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_pdf_export_carries_evidence_and_chain_header():
    _insert_task(id=204, title="PDF export test", claimed_by="Desktop_agent3")
    report = evidence_report.build_evidence_report(task_id=204)

    pdf_bytes = evidence_report.format_evidence_report(report, fmt="pdf")
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF")

    text = _pdf_text(pdf_bytes)
    chain = report["chain_integrity"]
    assert "Task #204" in text
    assert "PDF export test" in text
    assert "Desktop_agent3" in text
    assert chain["status"] in text
    assert (chain.get("head_hash") or "")[:16] in text or not chain.get("head_hash")


def test_pdf_export_rejects_empty_report():
    report = evidence_report.build_evidence_report(task_id=9999999)
    with pytest.raises(ValueError, match="no evidence records"):
        evidence_report.format_evidence_report(report, fmt="pdf")


def test_pdf_export_rejects_tampered_chain():
    _insert_task(id=205, title="Tampered chain test", claimed_by="Desktop_agent3")
    report = evidence_report.build_evidence_report(task_id=205)
    report["chain_integrity"] = dict(report["chain_integrity"], status="tampered")
    with pytest.raises(ValueError, match="tampered"):
        evidence_report.format_evidence_report(report, fmt="pdf")


def test_pdf_export_reports_a_clear_error_when_fpdf_is_not_installed(monkeypatch):
    """The `pdf` extra (fpdf2) is optional — a minimal install must get a
    clear, actionable error instead of a bare ModuleNotFoundError."""
    import builtins

    _insert_task(id=206, title="Missing dependency test", claimed_by="Desktop_agent3")
    report = evidence_report.build_evidence_report(task_id=206)

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fpdf":
            raise ModuleNotFoundError("No module named 'fpdf'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="optional 'pdf' extra"):
        evidence_report.format_evidence_report(report, fmt="pdf")


def test_completed_task_without_events_is_not_tamper_evident():
    _insert_task(id=7, title="Done without ledger events")
    report = evidence_report.build_evidence_report(task_id=7)
    rec = report["records"][0]
    assert rec["proof"]["completed_at"] is not None
    assert rec["proof"]["event_hashes"] == []
    assert rec["proof"]["tamper_evident"] is False


def test_events_and_approvals_match_exact_task_id():
    _insert_task(id=7, title="Task 7", completed_at=None)
    _insert_task(id=70, title="Task 70", completed_at=None)

    events.record_event(
        "agent.task.completed",
        payload={"task_id": 70, "status": "done"},
        originator="Desktop_agent3",
    )

    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO approvals (id, tool, payload, status, requested_by, resolved_by, reason, created_at, updated_at, resolved_at) "
            "VALUES (2, 'git_merge_task', '{\"task_id\":70}', 'approved', 'Desktop_agent3', 'Desktop_Person', 'ok', "
            "'2026-09-06T10:00:00+00:00', '2026-09-06T10:05:00+00:00', '2026-09-06T10:05:00+00:00')",
        )
        conn.commit()
    finally:
        conn.close()

    rec7 = evidence_report.build_evidence_report(task_id=7)["records"][0]
    rec70 = evidence_report.build_evidence_report(task_id=70)["records"][0]
    assert rec7["proof"]["event_hashes"] == []
    assert rec7["action"]["approvals"] == []
    assert rec7["human"] == "unknown"
    assert len(rec70["proof"]["event_hashes"]) == 1
    assert rec70["action"]["approvals"][0]["resolved_by"] == "Desktop_Person"
    assert rec70["human"] == "Desktop_Person"


def test_approvals_and_producer_events_bind_via_parent_task_id(monkeypatch):
    """Delegate-shaped payloads use parent_task_id; evidence must still link (task #1140)."""
    from unified_agent.audit import producer_sign

    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent5:parent-task-id-bearer-not-a-secret")
    _insert_task(id=1170, title="Release attest parent", claimed_by="Desktop_Agent4")

    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO approvals (id, tool, payload, status, requested_by, resolved_by, reason, "
            "created_at, updated_at, resolved_at) "
            'VALUES (116, \'delegate\', \'{"parent_task_id":1170,"to_agent":"Desktop_Agent5"}\', '
            "'approved', 'Desktop_Agent4', 'Desktop_Agent5', 'wedge', "
            "'2026-09-19T10:46:00+00:00', '2026-09-19T10:46:10+00:00', '2026-09-19T10:46:10+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    # Stamp without task_id (pre-fix shape) — report still binds via approval ref_id.
    sig = producer_sign.sign_producer_claim(
        identity="Desktop_Agent5",
        kind="approval",
        ref_id="116",
        task_id=None,
        bearer_token="parent-task-id-bearer-not-a-secret",
    )
    events.record_event(
        producer_sign.CHANNEL_PRODUCER_AUTHENTICATED,
        payload={
            "identity": "Desktop_Agent5",
            "kind": "approval",
            "ref_id": "116",
            "scheme": producer_sign.SCHEME_HMAC_SHA256_V1,
            "signature": sig,
            "producer": True,
        },
        originator="Desktop_Agent5",
    )

    report = evidence_report.build_evidence_report(task_id=1170)
    rec = report["records"][0]
    assert rec["action"]["approvals"][0]["id"] == 116
    assert rec["action"]["approvals"][0]["resolved_by"] == "Desktop_Agent5"
    assert report["chain_integrity"]["actor_authentication_verified"] is True
    # Desktop_Agent5 is an AGENT identity (task #1217): its verified signature
    # is preserved as actor evidence (human == "Desktop_Agent5", a verified
    # candidate) but must NOT be promoted to verified human authority — that
    # was exactly the false-positive the #1217 audit flagged.
    assert rec["human_provenance"]["actor_kind"] == "agent"
    assert rec["human_provenance"]["human_authority"] == "unknown"
    assert rec["human_provenance"]["verified"] is False
    assert rec["human"] == "Desktop_Agent5"
    # Report-level provenance_verified requires verified HUMAN authority, not
    # merely a verified agent actor.
    assert report["chain_integrity"]["provenance_verified"] is False


def test_production_task_events_associate_via_kind_ref_id():
    """task_add/update mirror {kind, ref_id}, not task_id (Hermes HOLD P1)."""
    _insert_task(id=777, title="Production writer", claimed_by="Desktop_Agent4")
    storage.record_agent_event(
        "task",
        ref_id="777",
        agent="Desktop_Agent4",
        summary="created (high): Production writer",
    )
    rec = evidence_report.build_evidence_report(task_id=777)["records"][0]
    assert rec["proof"]["events_count"] == 1
    assert len(rec["proof"]["event_hashes"]) == 1
    assert rec["proof"]["tamper_evident"] is True


def test_kind_ref_id_does_not_prefix_match_sibling_ids():
    _insert_task(id=7, title="Task 7", completed_at=None)
    _insert_task(id=70, title="Task 70", completed_at=None)
    storage.record_agent_event("task", ref_id="70", agent="Desktop_agent3", summary="created")
    rec7 = evidence_report.build_evidence_report(task_id=7)["records"][0]
    rec70 = evidence_report.build_evidence_report(task_id=70)["records"][0]
    assert rec7["proof"]["events_count"] == 0
    assert rec7["proof"]["tamper_evident"] is False
    assert rec70["proof"]["events_count"] == 1
    assert rec70["proof"]["tamper_evident"] is True


def test_valid_global_chain_does_not_make_unmatched_task_tamper_evident():
    _insert_task(id=7, title="Done without own events")
    _insert_task(id=70, title="Has ledger events", completed_at=None)
    storage.record_agent_event("task", ref_id="70", agent="Desktop_agent3", summary="created")
    rec7 = evidence_report.build_evidence_report(task_id=7)["records"][0]
    chain = evidence_report.build_evidence_report(task_id=7)["chain_integrity"]
    assert chain["verified"] is True
    assert chain["total_verified"] >= 1
    assert rec7["proof"]["completed_at"] is not None
    assert rec7["proof"]["events_count"] == 0
    assert rec7["proof"]["tamper_evident"] is False


def test_unknown_device_stays_unattributed():
    _insert_task(id=9, title="Legacy row", source_host=None, claimed_by=None)
    rec = evidence_report.build_evidence_report(task_id=9)["records"][0]
    assert rec["device"] == "unattributed"
    assert rec["device"] != "test-device.local"


def test_limit_clamps_unbounded_and_oversized_values():
    for i in range(1, 6):
        _insert_task(id=i, title=f"Task {i}", completed_at=None)
    neg = evidence_report.build_evidence_report(limit=-1)
    assert neg["filter"]["limit"] == 50
    assert neg["total_records"] == 5
    huge = evidence_report.build_evidence_report(limit=10_000)
    assert huge["filter"]["limit"] == 500
    zero = evidence_report.build_evidence_report(limit=0)
    assert zero["filter"]["limit"] == 50


def test_time_window_requires_a_timestamp_inside_bounds():
    _insert_task(
        id=11,
        title="Outside window",
        created_at="2026-08-01T00:00:00+00:00",
        updated_at="2026-09-10T00:00:00+00:00",
        completed_at=None,
    )
    _insert_task(
        id=12,
        title="Inside window",
        created_at="2026-09-03T00:00:00+00:00",
        updated_at="2026-09-03T12:00:00+00:00",
        completed_at=None,
    )
    report = evidence_report.build_evidence_report(
        since="2026-09-01T00:00:00+00:00",
        until="2026-09-06T00:00:00+00:00",
    )
    ids = [r["task_id"] for r in report["records"]]
    assert ids == [12]


def test_evidence_cli(tmp_path, capsys):
    _insert_task(id=301, title="CLI test task", claimed_by="Desktop_agent3")

    code = evidence_cli.run_evidence_cli(["--task", "301", "--json"])
    assert code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["total_records"] == 1

    out_file = tmp_path / "report.md"
    code = evidence_cli.run_evidence_cli(["--task", "301", "--markdown", "--out", str(out_file)])
    assert code == 0
    assert out_file.exists()
    content = out_file.read_text()
    assert "# User-Attributable Agent Evidence Report" in content


def _require_unexported(path: Path) -> None:
    """Skip in a public export, where scripts/public_release/exclude.txt drops ``path``.

    The private repo ships the release tooling, so a missing file there still fails.
    """
    repo = Path(__file__).resolve().parents[1]
    if not path.exists() and not (repo / "scripts" / "public_release").is_dir():
        pytest.skip(f"{path.relative_to(repo)} is excluded from the public export")


_DEMO_SAMPLE = (
    Path(__file__).resolve().parents[1] / "docs/samples/2026-09-03-project-evidence-report.md"
)
_PROJECT_CHAIN_HEADINGS = (
    "## Project spec",
    "## Steps",
    "## Merged PRs",
    "## Last merge",
    "## Audit verification",
)


def _insert_demo_project() -> None:
    """Representative 2026-09-03 office-hours project tree for the buyer demo."""
    _insert_task(
        id=819,
        title="User-attributable agent accountability",
        status="doing",
        claimed_by="Desktop_Agent1",
        source_host="example-host.local",
        evidence_ref=None,
        notes="[project-spec] office-hours 2026-09-03 founding thesis",
        completed_at=None,
        created_at="2026-09-03T23:00:00+00:00",
        updated_at="2026-09-03T23:52:04+00:00",
    )
    _insert_task(
        id=830,
        title="Project trees and spec marker",
        parent_id=819,
        claimed_by="Desktop_Agent4",
        source_host="example-host.local",
        evidence_ref="https://github.com/anonb3ll/unified-agent/pull/423",
        notes="[review: merged] https://github.com/anonb3ll/unified-agent/pull/423",
        completed_at="2026-09-08T22:00:00+00:00",
        created_at="2026-09-08T18:00:00+00:00",
        updated_at="2026-09-08T22:00:00+00:00",
    )
    _insert_task(
        id=831,
        title="Per-project evidence chain",
        parent_id=830,
        claimed_by="Desktop_Agent4",
        source_host="example-host.local",
        evidence_ref="https://github.com/anonb3ll/unified-agent/pull/424 commit 6a6f1d2",
        notes="[review: merged-upstream] https://github.com/anonb3ll/unified-agent/pull/424",
        completed_at="2026-09-09T00:10:00+00:00",
        created_at="2026-09-08T22:30:00+00:00",
        updated_at="2026-09-09T00:10:00+00:00",
    )
    _insert_task(
        id=845,
        title="Bind merge verification to the reported PR",
        status="open",
        parent_id=819,
        claimed_by="Desktop_Agent4",
        evidence_ref=None,
        notes="claimed-by: Desktop_Agent4",
        completed_at=None,
        created_at="2026-09-09T01:44:39+00:00",
        updated_at="2026-09-09T01:44:39+00:00",
    )
    storage.record_agent_event("task", ref_id="830", agent="Desktop_Agent4", summary="merged")
    storage.record_agent_event("task", ref_id="831", agent="Desktop_Agent4", summary="merged")


def test_evidence_report_cli_requires_project_id():
    with pytest.raises(SystemExit) as excinfo:
        evidence_cli.run_evidence_cli(["report"])
    assert excinfo.value.code == 2


def test_evidence_cli_help_mentions_report_subcommand(capsys):
    with pytest.raises(SystemExit) as excinfo:
        evidence_cli.run_evidence_cli(["--help"])
    assert excinfo.value.code == 0
    assert "evidence report" in capsys.readouterr().out


def test_evidence_report_cli_help_documents_project_id(capsys):
    with pytest.raises(SystemExit) as excinfo:
        evidence_cli.run_evidence_cli(["report", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--project" in out
    assert "project" in out.lower()


def test_evidence_report_cli_json_includes_project_chain_and_audit(capsys):
    _insert_demo_project()

    code = evidence_cli.run_evidence_cli(["report", "--project", "819", "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["filter"]["project_id"] == 819
    assert data["filter"]["project"] is None
    chain = data["project_chain"]
    assert chain["project_id"] == 819
    assert chain["status"] == "partial"
    assert chain["task_ids"] == [819, 830, 831, 845]
    assert chain["done_task_ids"] == [830, 831]
    assert chain["merged_prs"] == []
    assert chain["last_merge"] is None
    assert data["chain_integrity"]["verified"] is True
    assert data["chain_integrity"]["status"] == "valid"


def test_evidence_report_cli_markdown_renders_spec_steps_merges_and_audit(capsys):
    _insert_demo_project()

    code = evidence_cli.run_evidence_cli(["report", "--project", "819"])
    assert code == 0
    md = capsys.readouterr().out
    for heading in _PROJECT_CHAIN_HEADINGS:
        assert heading in md
    assert "#819 User-attributable agent accountability" in md
    assert "[project-spec]" in md
    assert "#830 Project trees and spec marker" in md
    assert "#831 Per-project evidence chain" in md
    assert "#845 Bind merge verification to the reported PR" in md
    assert "https://github.com/anonb3ll/unified-agent/pull/423" in md
    assert "https://github.com/anonb3ll/unified-agent/pull/424" in md
    assert "Last merge" in md
    assert "verified=true" in md or "`valid`" in md
    assert "no merge proof" in md


def test_evidence_report_cli_does_not_call_unverified_pr_merged(capsys):
    _insert_task(
        id=930,
        title="Project spec",
        status="doing",
        completed_at=None,
        evidence_ref=None,
        notes="[project-spec]",
    )
    _insert_task(
        id=931,
        title="Done but unmerged",
        parent_id=930,
        evidence_ref="https://github.com/anonb3ll/unified-agent/pull/999",
    )

    code = evidence_cli.run_evidence_cli(["report", "--project", "930"])
    assert code == 0
    md = capsys.readouterr().out
    assert "status=`partial`" in md or "partial" in md
    assert "https://github.com/anonb3ll/unified-agent/pull/999" in md
    assert "None" in md
    merged_section = md.split("## Merged PRs", 1)[1].split("## Last merge", 1)[0]
    assert "pull/999" not in merged_section


def test_evidence_report_cli_rejects_non_project_root(capsys):
    _insert_task(id=960, title="Ordinary task", notes="")

    code = evidence_cli.run_evidence_cli(["report", "--project", "960", "--json"])
    assert code == 2
    assert "does not carry [project-spec]" in capsys.readouterr().err


def test_legacy_project_name_filter_is_not_the_report_subcommand(capsys):
    _insert_task(id=101, title="Task A", claimed_by="Desktop_agent3", project="unified-agent")

    code = evidence_cli.run_evidence_cli(["--project", "unified-agent", "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["filter"]["project"] == "unified-agent"
    assert data["filter"]["project_id"] is None
    assert data["project_chain"] is None


def test_2026_09_03_demo_sample_is_the_renderer_artifact():
    _require_unexported(_DEMO_SAMPLE)
    text = _DEMO_SAMPLE.read_text(encoding="utf-8")
    for heading in _PROJECT_CHAIN_HEADINGS:
        assert heading in text
    assert "2026-09-03" in text
    assert "[project-spec]" in text
    assert "unified-agent evidence report --project" in text


def test_webui_evidence_route(monkeypatch):
    _insert_task(id=401, title="WebUI route test", claimed_by="Desktop_agent3")

    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:test-token-401")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)

    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        res = client.get("/api/evidence?task_id=401")
        assert res.status_code == 200
        data = res.json()
        assert data["report_type"] == "user_attributable_evidence"
        assert data["total_records"] == 1
        assert data["records"][0]["task_id"] == 401

        res_md = client.get("/api/evidence?task_id=401&format=markdown")
        assert res_md.status_code == 200
        assert "text/markdown" in res_md.headers["content-type"]
        assert "# User-Attributable Agent Evidence Report" in res_md.text

        res_csv = client.get("/api/evidence?task_id=401&format=csv")
        assert res_csv.status_code == 200
        assert "text/csv" in res_csv.headers["content-type"]
        assert "attachment" in res_csv.headers["content-disposition"]
        csv_rows = list(csv.reader(io.StringIO(res_csv.text)))
        assert csv_rows[0][:2] == ["record_type", "task_id"]
        csv_record = dict(zip(csv_rows[0], csv_rows[1], strict=True))
        assert csv_record["record_type"] == "record"
        assert csv_record["task_id"] == "401"

        res_pdf = client.get("/api/evidence?task_id=401&format=pdf")
        assert res_pdf.status_code == 200
        assert "application/pdf" in res_pdf.headers["content-type"]
        assert "attachment" in res_pdf.headers["content-disposition"]
        assert res_pdf.content.startswith(b"%PDF")
        pdf_text = _pdf_text(res_pdf.content)
        assert "Task #401" in pdf_text
        assert "WebUI route test" in pdf_text

        res_pdf_empty = client.get("/api/evidence?task_id=9999999&format=pdf")
        assert res_pdf_empty.status_code == 422
        assert "no evidence records" in res_pdf_empty.json()["error"]


def test_webui_evidence_pdf_route_requires_auth(monkeypatch):
    """The pdf format must not open a hole in the /api/evidence auth boundary."""
    _insert_task(id=403, title="PDF auth test", claimed_by="Desktop_agent3")
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:test-token-403")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")

    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        res = client.get("/api/evidence?task_id=403&format=pdf")
        assert res.status_code == 401

        res_ok = client.get(
            "/api/evidence?task_id=403&format=pdf",
            headers={"Authorization": "Bearer test-token-403"},
        )
        assert res_ok.status_code == 200
        assert res_ok.content.startswith(b"%PDF")


def test_webui_evidence_route_denied_in_core_edition(monkeypatch):
    _insert_task(id=402, title="WebUI core test", claimed_by="Desktop_agent3")
    monkeypatch.setenv("UA_EDITION", "core")
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:test-token-402")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)

    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        res = client.get("/api/evidence?task_id=402")
        assert res.status_code == 403
        data = res.json()
        assert "evidence_reporting" in data["error"] or "Enterprise edition" in data["error"]


def test_denied_approval_does_not_resolve_human(monkeypatch):
    monkeypatch.setenv("UA_OPERATOR_NAME", "Fallback Operator")
    _insert_task(id=501, title="Denied first", claimed_by="Desktop_agent3")
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO approvals (tool, payload, status, requested_by, resolved_by, created_at, updated_at) "
            "VALUES ('git_merge_task', '{\"task_id\": 501}', 'denied', 'Desktop_agent3', 'Denying Person', '2026-09-06T10:00:00+00:00', '2026-09-06T10:05:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()

    report = evidence_report.build_evidence_report(task_id=501)
    rec = report["records"][0]
    # The denying human must not be named accountable; fall through to the
    # configured operator instead.
    assert rec["human"] == "Fallback Operator"


def test_execution_event_host_beats_creation_host():
    _insert_task(id=502, title="Dispatched work", source_host="desktop-linux")
    events.record_event(
        "agent.task.completed",
        payload={"task_id": 502, "host": "mac-hub.local"},
        originator="Desktop_agent3",
    )

    report = evidence_report.build_evidence_report(task_id=502)
    assert report["records"][0]["device"] == "unverified-device:mac-hub.local"


def test_markdown_escapes_pipes_and_newlines():
    _insert_task(id=503, title="Title | with pipe\nand newline")

    md = evidence_report.format_evidence_report(
        evidence_report.build_evidence_report(task_id=503), fmt="markdown"
    )
    data_rows = [row for row in md.splitlines() if row.startswith("| #503")]
    assert len(data_rows) == 1
    assert "Title \\| with pipe and newline" in data_rows[0]


def test_date_only_until_bound_is_inclusive_day():
    _insert_task(
        id=504,
        title="Same-day task",
        created_at="2026-09-06T09:00:00+00:00",
        updated_at="2026-09-06T10:25:15+00:00",
        completed_at="2026-09-06T10:25:15+00:00",
    )

    report = evidence_report.build_evidence_report(until="2026-09-06")
    assert report["total_records"] == 1


def test_invalid_task_id_param_is_rejected():
    _insert_task(id=505, title="Should not leak")

    monkeypatch_tokens = {"UA_AGENT_TOKENS": "Desktop_Agent1:test-token-505"}
    import os

    saved = {k: os.environ.get(k) for k in monkeypatch_tokens}
    os.environ.update(monkeypatch_tokens)
    os.environ.pop("UA_ENFORCE_AUTH", None)
    try:
        from starlette.applications import Starlette as _S
        from starlette.testclient import TestClient as _TC

        app = server.HubAuthASGIMiddleware(_S(routes=webui.web_routes()))
        with _TC(app) as client:
            res = client.get("/api/evidence?task_id=-1")
            assert res.status_code == 400
            res = client.get("/api/evidence?task_id=abc")
            assert res.status_code == 400
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_project_report_excludes_historical_unattributed_agent():
    """UA #849: buyer-facing project artifacts omit unresolved agent rows.

    Historical nullable claimed_by is excluded, not invented from source_host.
    The structural project_chain still lists the task id.
    """
    _insert_task(
        id=990,
        title="Attributed spec",
        status="doing",
        claimed_by="Desktop_Agent1",
        source_host="example-host.local",
        evidence_ref=None,
        notes="[project-spec]",
        completed_at=None,
    )
    _insert_task(
        id=991,
        title="Legacy unclaimed child",
        status="open",
        parent_id=990,
        claimed_by=None,
        source_host="desktop-linux",
        evidence_ref=None,
        notes="",
        completed_at=None,
    )

    report = evidence_report.build_evidence_report(project_id=990)
    assert [r["task_id"] for r in report["records"]] == [990]
    assert all(r["agent"] != evidence_report._UNATTRIBUTED for r in report["records"])
    assert all(r["agent"] != "desktop-linux" for r in report["records"])
    assert report["unattributed_excluded"] == [991]
    assert report["project_chain"]["task_ids"] == [990, 991]

    md = evidence_report.format_evidence_report(report, fmt="markdown")
    table = md.split("| Task / Item |", 1)[1]
    assert "| unattributed |" not in table
    assert "Unattributed excluded" in md
    assert "#991 Legacy unclaimed child" in md


def test_non_project_report_still_surfaces_historical_unattributed():
    """Single-task / name-filter reports keep historical nulls visible."""
    _insert_task(id=992, title="Orphan legacy", source_host=None, claimed_by=None)
    rec = evidence_report.build_evidence_report(task_id=992)["records"][0]
    assert rec["agent"] == "unattributed"
    assert rec["device"] == "unattributed"


def test_project_demo_sample_has_no_unattributed_agent_cell():
    _require_unexported(_DEMO_SAMPLE)
    text = _DEMO_SAMPLE.read_text(encoding="utf-8")
    table = text.split("| Task / Item |", 1)[1]
    assert "| unattributed |" not in table


_PROOF_C2_SAMPLE = (
    Path(__file__).resolve().parents[1] / "docs/samples/2026-09-09-proof-c2-project-evidence.md"
)


def test_proof_c2_sample_has_zero_unattributed_records():
    _require_unexported(_PROOF_C2_SAMPLE)
    text = _PROOF_C2_SAMPLE.read_text(encoding="utf-8")
    table = text.split("| Task / Item |", 1)[1]
    assert "| unattributed |" not in table
    assert "Desktop_Agent1" in table
    assert "Desktop_Agent4" in table
    assert "PROOF-C2" in text


def test_blank_human_annotation_does_not_create_a_candidate():
    provenance = evidence_report._human_provenance({"notes": "[human:   ]"}, [], [])
    assert provenance["value"] == "unknown"
    assert provenance["candidates"] == []


# ---------------------------------------------------------------------------
# Task #1139: attribution coverage gate (report-level, >95% enforced)
# ---------------------------------------------------------------------------

_COV_WINDOW_START = "2026-09-01T00:00:00+00:00"
_COV_NOW = "2026-09-15T12:00:00+00:00"


def _seed_done_tasks(
    *,
    attributed: int,
    unattributed: int,
    cancelled_unattributed: int = 0,
    completed_at: str = "2026-09-10T12:00:00+00:00",
    outside_window_unattributed: int = 0,
) -> list[int]:
    """Insert done (and optional cancelled) tasks; return unattributed done ids."""
    conn = storage.connect()
    unattributed_ids: list[int] = []
    next_id = 10_000
    try:
        rows: list[tuple] = []
        for i in range(attributed):
            rows.append(
                (
                    next_id,
                    f"Attributed {i}",
                    "done",
                    "Desktop_agent3",
                    completed_at,
                    completed_at,
                    completed_at,
                )
            )
            next_id += 1
        for i in range(unattributed):
            unattributed_ids.append(next_id)
            rows.append(
                (
                    next_id,
                    f"Unattributed {i}",
                    "done",
                    None,
                    completed_at,
                    completed_at,
                    completed_at,
                )
            )
            next_id += 1
        for i in range(cancelled_unattributed):
            rows.append(
                (
                    next_id,
                    f"Cancelled {i}",
                    "cancelled",
                    None,
                    completed_at,
                    completed_at,
                    completed_at,
                )
            )
            next_id += 1
        for i in range(outside_window_unattributed):
            rows.append(
                (
                    next_id,
                    f"Old unattributed {i}",
                    "done",
                    None,
                    "2026-08-01T12:00:00+00:00",
                    "2026-08-01T12:00:00+00:00",
                    "2026-08-01T12:00:00+00:00",
                )
            )
            next_id += 1
        conn.executemany(
            "INSERT INTO tasks (id, title, status, priority, claimed_by, "
            "created_at, updated_at, completed_at, project, domain, lane) "
            "VALUES (?, ?, ?, 'med', ?, ?, ?, ?, 'unified-agent', 'hobby', 'hobby')",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return unattributed_ids


def test_attribution_coverage_numerator_denominator_exact():
    """Coverage query returns exact counts over a real fixture DB."""
    unattributed = _seed_done_tasks(
        attributed=7, unattributed=3, cancelled_unattributed=5, outside_window_unattributed=4
    )
    cov = evidence_report.compute_attribution_coverage(
        since=_COV_WINDOW_START, until=_COV_NOW, now=_COV_NOW
    )
    assert cov["numerator"] == 7
    assert cov["denominator"] == 10
    assert cov["coverage"] == pytest.approx(0.7)
    assert sorted(cov["unattributed_task_ids"]) == sorted(unattributed)
    assert cov["window_start"] == _COV_WINDOW_START
    assert cov["window_end"] == _COV_NOW


def test_attribution_coverage_gate_passes_at_96_percent():
    unattributed = _seed_done_tasks(attributed=96, unattributed=4)
    cov = evidence_report.compute_attribution_coverage(
        since=_COV_WINDOW_START, until=_COV_NOW, now=_COV_NOW
    )
    assert cov["numerator"] == 96
    assert cov["denominator"] == 100
    assert cov["coverage"] == pytest.approx(0.96)
    assert cov["gate"] == "pass"
    assert cov["gate_failure"] is None
    assert cov["unattributed_task_ids"] == unattributed  # listed but gate still passes
    assert cov["threshold"] == evidence_report.ATTRIBUTION_COVERAGE_THRESHOLD


def test_attribution_coverage_gate_fails_at_94_percent_naming_ids():
    unattributed = _seed_done_tasks(attributed=94, unattributed=6)
    cov = evidence_report.compute_attribution_coverage(
        since=_COV_WINDOW_START, until=_COV_NOW, now=_COV_NOW
    )
    assert cov["numerator"] == 94
    assert cov["denominator"] == 100
    assert cov["coverage"] == pytest.approx(0.94)
    assert cov["gate"] == "fail"
    assert cov["gate_failure"] == "attribution_coverage_below_threshold"
    assert sorted(cov["unattributed_task_ids"]) == sorted(unattributed)


def test_attribution_coverage_empty_window_is_no_data_not_pass():
    cov = evidence_report.compute_attribution_coverage(
        since=_COV_WINDOW_START, until=_COV_NOW, now=_COV_NOW
    )
    assert cov["numerator"] == 0
    assert cov["denominator"] == 0
    assert cov["coverage"] is None
    assert cov["gate"] == "no_data"
    assert cov["gate_failure"] is None
    assert cov["unattributed_task_ids"] == []


def test_evidence_report_includes_attribution_coverage_gate_verdict():
    """Evidence report payload carries attribution_coverage + gate; fail blocks clean pass."""
    unattributed = _seed_done_tasks(attributed=94, unattributed=6)
    report = evidence_report.build_evidence_report(since=_COV_WINDOW_START, until=_COV_NOW, limit=5)
    cov = report["attribution_coverage"]
    assert cov["numerator"] == 94
    assert cov["denominator"] == 100
    assert cov["gate"] == "fail"
    assert cov["gate_failure"] == "attribution_coverage_below_threshold"
    assert sorted(cov["unattributed_task_ids"]) == sorted(unattributed)
    assert "attribution_coverage_below_threshold" in report["gate_failures"]
    assert report["clean_pass"] is False
    text = evidence_report.format_evidence_report(report, fmt="text")
    assert f"platform-wide coverage window {_COV_WINDOW_START} through {_COV_NOW}" in text
    assert "trailing window" not in text
    assert "Clean pass: false" in text
    assert "Gate failures: attribution_coverage_below_threshold" in text

    conn = storage.connect()
    try:
        conn.execute("DELETE FROM tasks")
        conn.commit()
    finally:
        conn.close()
    _seed_done_tasks(attributed=96, unattributed=4)
    report_ok = evidence_report.build_evidence_report(
        since=_COV_WINDOW_START, until=_COV_NOW, limit=5
    )
    assert report_ok["attribution_coverage"]["gate"] == "pass"
    assert report_ok["gate_failures"] == []
    assert report_ok["clean_pass"] is True

    conn = storage.connect()
    try:
        conn.execute("DELETE FROM tasks")
        conn.commit()
    finally:
        conn.close()
    report_empty = evidence_report.build_evidence_report(since=_COV_WINDOW_START, until=_COV_NOW)
    assert report_empty["attribution_coverage"]["gate"] == "no_data"
    assert report_empty["clean_pass"] is False  # skipped, not a clean pass
    assert report_empty["gate_failures"] == []


def test_tampered_chain_clears_clean_pass_even_with_full_attribution():
    task_id = 10_000
    _seed_done_tasks(attributed=1, unattributed=0)
    tamper_ulid = events.record_event(
        "agent.task.completed",
        payload={"task_id": task_id, "status": "done"},
        originator="Desktop_agent3",
    )

    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute(
                "UPDATE event_log SET payload=? WHERE ulid=?",
                (json.dumps({"task_id": task_id, "tampered": True}), tamper_ulid),
            )
        conn.commit()
    finally:
        conn.close()

    report = evidence_report.build_evidence_report(since=_COV_WINDOW_START, until=_COV_NOW, limit=5)
    assert report["attribution_coverage"]["gate"] == "pass"
    assert report["chain_integrity"]["verified"] is False
    assert report["clean_pass"] is False
    assert "evidence_chain_integrity_failed" in report["gate_failures"]
    markdown = evidence_report.format_evidence_report(report, fmt="markdown")
    assert "**Clean pass**: `false`" in markdown
    assert "`evidence_chain_integrity_failed`" in markdown
