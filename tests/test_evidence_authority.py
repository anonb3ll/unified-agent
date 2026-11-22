"""Evidence reports say what the evidence proves (task #1673).

- A bearer-attributed human approval is ``credential-attributed``, never
  ``verified``: a credential was used, which does not prove a present human.
  ``presence-verified`` is reserved for statement-bound passkey assertions.
- A single-operator deployment reports ``sod: not_applicable_single_operator``
  instead of a pass/fail separation-of-duties claim.
- A pre-1.7 export carrying ``human_authority: verified`` still renders, as
  credential-attributed.
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from unified_agent import config, storage
from unified_agent.audit import evidence_cli, evidence_report, provenance_graph
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
    check_approval_gate,
)

_TASK = 1673


@pytest.fixture(autouse=True)
def _clean_db(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    # A neutral operator login label (the suite default names a real person).
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Operator")
    monkeypatch.setenv("UA_OPERATOR_ALIASES", "Operator")
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    monkeypatch.setattr(config, "current_source_host", lambda: "test-device.local")
    storage.init_db()
    yield


def _insert_task(task_id: int = _TASK) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, "
            "completed_at, source_host, claimed_by, evidence_ref, notes, project, "
            "domain, lane) VALUES (?, 'authority wording', 'done', 'high', "
            "'2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00', "
            "'2026-10-01T00:00:00+00:00', 'test-device.local', 'Desktop_Agent4', "
            "NULL, '', 'unified-agent', 'hobby', 'hobby')",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _bearer_approved_report(monkeypatch) -> dict:
    """A human approves a gated call over a bearer credential; build the report."""
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        "Desktop_Agent4:test-bearer-cursor-1673,Desktop_Operator:test-bearer-operator-1673",
    )
    _insert_task()
    with pytest.raises(ApprovalPendingError):
        check_approval_gate(
            "run_cmd",
            {"command": "echo authority", "task_id": _TASK},
            requested_by="Desktop_Agent4",
        )
    rid = approval_pending()["pending"][0]["id"]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET payload=? WHERE id=?",
            (json.dumps({"command": "echo authority", "task_id": _TASK}), rid),
        )
        conn.commit()
    finally:
        conn.close()
    token = config.agent_identity_var.set("Desktop_Operator")
    try:
        approval_resolve(rid, "approve")
    finally:
        config.agent_identity_var.reset(token)
    return evidence_report.build_evidence_report(task_id=_TASK)


# --- (a) bearer-attributed approval -> credential-attributed ---------------


def test_bearer_attributed_approval_is_credential_attributed_snapshot(monkeypatch):
    report = _bearer_approved_report(monkeypatch)
    assert report["schema_version"] == "1.8"
    provenance = report["records"][0]["human_provenance"]
    assert {
        key: provenance[key]
        for key in (
            "value",
            "source",
            "actor_kind",
            "human_authority",
            "credential_attributed",
            "verified",
            "verified_via",
            "conflict",
        )
    } == {
        "value": "Desktop_Operator",
        "source": "evidence.producer_authenticated",
        "actor_kind": "human",
        "human_authority": "credential-attributed",
        "credential_attributed": True,
        "verified": True,
        "verified_via": "agent_bearer",
        "conflict": False,
    }

    markdown = evidence_report.format_evidence_report(report, "markdown")
    assert (
        "Desktop_Operator (source=evidence.producer_authenticated; "
        "credential-attributed-by-agent_bearer; actor_kind=human; "
        "human_authority=credential-attributed; candidates="
    ) in markdown
    text = evidence_report.format_evidence_report(report, "text")
    assert "human_authority=credential-attributed" in text

    row = next(csv.DictReader(io.StringIO(evidence_report.format_evidence_report(report, "csv"))))
    assert row["human_authority"] == "credential-attributed"
    assert row["human_credential_attributed"] == "True"

    # No human-authority wording says "verified" unless it is presence-verified.
    for rendered in (markdown, text):
        assert "human_authority=verified" not in rendered
        assert "verified-by-" not in rendered
    assert '"human_authority": "verified"' not in json.dumps(report)


def test_presence_verified_is_reserved_and_not_emitted(monkeypatch):
    report = _bearer_approved_report(monkeypatch)
    assert "presence-verified" not in json.dumps(report)
    assert evidence_report.HUMAN_AUTHORITY_PRESENCE_VERIFIED == "presence-verified"
    # A future passkey assertion would render as itself, not be downgraded.
    assert evidence_report.normalize_human_authority("presence-verified") == "presence-verified"


def test_agent_signature_stays_unattributed():
    provenance = evidence_report._human_provenance(
        {"notes": ""},
        approvals=[],
        delegations=[],
        verified_producers=[{"identity": "Desktop_Agent2", "verified_via": "agent_bearer"}],
    )
    assert provenance["human_authority"] == "unknown"
    assert provenance["credential_attributed"] is False
    label = evidence_report._human_label(
        {"human": "Desktop_Agent2", "human_provenance": provenance}
    )
    assert "unattributed; actor_kind=agent; human_authority=unknown" in label
    assert "Desktop_Agent2 [evidence.producer_authenticated, signed, agent]" in label


# --- (b) single-operator config -> sod not_applicable_single_operator -------


def test_single_operator_reports_sod_not_applicable(monkeypatch):
    assert len(config.human_identity_labels()) == 1
    report = _bearer_approved_report(monkeypatch)
    assert report["sod"] == "not_applicable_single_operator"
    assert report["sod_detail"] == {
        "status": "not_applicable_single_operator",
        "configured_humans": 1,
        "approvals_checked": 0,
        "violations": [],
    }
    assert "_sod_pairs" not in report["records"][0]
    markdown = evidence_report.format_evidence_report(report, "markdown")
    assert "- **Separation of duties**: `not_applicable_single_operator`" in markdown
    text = evidence_report.format_evidence_report(report, "text")
    assert "Separation of duties: not_applicable_single_operator" in text
    for rendered in (markdown, text):
        assert "sod: pass" not in rendered and "sod: fail" not in rendered


def test_multi_operator_sod_passes_and_fails_on_granted_approvals(monkeypatch):
    report = _bearer_approved_report(monkeypatch)
    monkeypatch.setattr(
        config, "human_identity_labels", lambda: frozenset({"Desktop_Operator", "Second_Human"})
    )
    rebuilt = evidence_report.build_evidence_report(task_id=_TASK)
    assert rebuilt["sod"] == "pass"
    assert rebuilt["sod_detail"]["approvals_checked"] == 1

    # A self-approved row (written past the tool's own guard) fails the check.
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET requested_by=resolved_by WHERE id=?",
            (report["records"][0]["action"]["approvals"][0]["id"],),
        )
        conn.commit()
    finally:
        conn.close()
    failed = evidence_report.build_evidence_report(task_id=_TASK)
    assert failed["sod"] == "fail"
    [violation] = failed["sod_detail"]["violations"]
    assert violation["task_id"] == _TASK
    assert violation["requested_by"] == violation["resolved_by"] == "Desktop_Operator"


def test_multi_operator_without_approvals_is_no_data(monkeypatch):
    _insert_task()
    monkeypatch.setattr(
        config, "human_identity_labels", lambda: frozenset({"Desktop_Operator", "Second_Human"})
    )
    assert evidence_report.build_evidence_report(task_id=_TASK)["sod"] == "no_data"


# --- (c) an old export with `verified` renders as credential-attributed -----


def _legacy_export() -> dict:
    """A schema-1.5 export as the hub wrote it before task #1673."""
    return {
        "report_type": "user_attributable_evidence",
        "schema_version": "1.5",
        "generated_at": "2026-09-20T00:00:00+00:00",
        "chain_integrity": {"status": "valid", "verified": True, "head_hash": "ab" * 32},
        "total_records": 1,
        "records": [
            {
                "task_id": 42,
                "project": "unified-agent",
                "human": "Desktop_Operator",
                "human_provenance": {
                    "value": "Desktop_Operator",
                    "source": "evidence.producer_authenticated",
                    "candidates": [],
                    "actor_kind": "human",
                    "human_authority": "verified",
                    "verified": True,
                    "verified_via": "agent_bearer",
                    "conflict": False,
                },
                "device": "unverified-device:test-device.local",
                "agent": "Desktop_Agent4",
                "action": {
                    "type": "task",
                    "title": "historical",
                    "status": "done",
                    "priority": "high",
                    "lane": "hobby",
                    "approvals": [],
                },
                "proof": {
                    "evidence_ref": None,
                    "pr_url": None,
                    "commit_sha": None,
                    "merge_verified": False,
                    "completed_at": "2026-09-20T00:00:00+00:00",
                    "events_count": 0,
                    "tamper_evident": False,
                },
            }
        ],
    }


def test_legacy_verified_export_renders_as_credential_attributed():
    legacy = _legacy_export()
    snapshot = (
        "Desktop_Operator (source=evidence.producer_authenticated; "
        "credential-attributed-by-agent_bearer; actor_kind=human; "
        "human_authority=credential-attributed; candidates=none)"
    )
    # The renderer reads the old value directly...
    assert snapshot in evidence_report.format_evidence_report(legacy, "markdown")
    assert snapshot in evidence_report.format_evidence_report(legacy, "text")
    row = next(csv.DictReader(io.StringIO(evidence_report.format_evidence_report(legacy, "csv"))))
    assert row["human_authority"] == "credential-attributed"
    assert row["human_credential_attributed"] == "True"
    # ...and the reader upgrades a copy without touching the original.
    upgraded = evidence_report.read_historical_report(legacy)
    assert upgraded["records"][0]["human_provenance"]["human_authority"] == "credential-attributed"
    assert upgraded["schema_version"] == "1.5"
    assert legacy["records"][0]["human_provenance"]["human_authority"] == "verified"


def test_schema_1_0_verified_flag_is_actor_authentication_not_human_authority():
    """Schema 1.0 set ``verified`` for any uniquely verifying producer signature,
    agent or human (task #1217 split the meanings in 1.1). A 1.0 record with no
    ``human_authority`` therefore never reads as human credential attribution;
    its actor-authentication bit is kept as exported."""
    legacy = _legacy_export()
    legacy["schema_version"] = "1.0"
    record = legacy["records"][0]
    record["human"] = "Desktop_Agent2"
    record["human_provenance"] = {
        "value": "Desktop_Agent2",
        "source": "evidence.producer_authenticated",
        "candidates": [],
        "verified": True,
        "conflict": False,
    }
    upgraded = evidence_report.read_historical_report(legacy)
    provenance = upgraded["records"][0]["human_provenance"]
    assert provenance["human_authority"] == "unknown"
    assert provenance["verified"] is True
    for fmt in ("markdown", "text"):
        rendered = evidence_report.format_evidence_report(legacy, fmt)
        assert "human_authority=unknown" in rendered
        assert "credential-attributed" not in rendered
    row = next(csv.DictReader(io.StringIO(evidence_report.format_evidence_report(legacy, "csv"))))
    assert row["human_authority"] == "unknown"
    assert row["human_credential_attributed"] == "False"
    assert evidence_report.normalize_human_authority(None, verified=False) == "unknown"
    assert evidence_report.normalize_human_authority(None, verified=True) == "unknown"


def test_historical_graph_appendix_matches_its_normalized_records():
    legacy = _legacy_export()
    legacy["provenance_graph"] = provenance_graph.build_graph_from_records(
        legacy["records"], scope={"kind": "task_scope", "task_id": 42}
    )
    original_hash = legacy["provenance_graph"]["canonical_hash"]
    upgraded = evidence_report.read_historical_report(legacy)
    graph = upgraded["provenance_graph"]
    humans = [n for n in graph["nodes"] if n["kind"] == "human"]
    assert humans
    assert (
        {n["attrs"]["human_authority"] for n in humans}
        == {upgraded["records"][0]["human_provenance"]["human_authority"]}
        == {"credential-attributed"}
    )
    assert graph["canonical_hash"] == provenance_graph.canonical_hash(
        graph["nodes"], graph["edges"]
    )
    assert graph["canonical_hash"] != original_hash
    assert graph["scope"] == legacy["provenance_graph"]["scope"]
    # The parsed export itself is untouched.
    legacy_humans = [n for n in legacy["provenance_graph"]["nodes"] if n["kind"] == "human"]
    assert {n["attrs"]["human_authority"] for n in legacy_humans} == {"verified"}
    assert legacy["provenance_graph"]["canonical_hash"] == original_hash
    # A current-schema graph passes through unchanged.
    current = evidence_report.read_historical_report(upgraded)
    assert current["provenance_graph"]["canonical_hash"] == graph["canonical_hash"]


def test_cli_render_reads_a_historical_export(tmp_path, capsys):
    path = tmp_path / "old-export.json"
    path.write_text(json.dumps(_legacy_export()), encoding="utf-8")
    assert evidence_cli.run_evidence_cli(["render", str(path)]) == 0
    out = capsys.readouterr().out
    assert "human_authority=credential-attributed" in out
    assert "human_authority=verified" not in out
    assert evidence_cli.run_evidence_cli(["render", str(path), "--format", "json"]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["records"][0]["human_provenance"]["human_authority"] == (
        "credential-attributed"
    )
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    assert evidence_cli.run_evidence_cli(["render", str(bad)]) == 2
