"""Task #1217: distinguish authenticated AGENT identity from verified HUMAN authority.

Audit finding: `evidence_report._human_provenance` promoted a unique signed
producer identity straight to `human_provenance.verified = True` regardless of
what *kind* of identity signed — an isolated real delegation-completion probe
returned `human=Desktop_Agent2, verified=True`. Desktop_Agent2 is an agent, not
a person, so that is an accountability misstatement: "an agent's signature was
unique and valid" is not evidence that "a human authorized this".

These tests cover the four cases named in the audit: an authenticated AGENT
(must NOT yield verified human authority), a real verified HUMAN, unknown
(no evidence at all), and a conflict (an agent identity presented where a
human claim was made, and ambiguous signed producers).
"""

from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.audit import evidence_report
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
    check_approval_gate,
)


@pytest.fixture(autouse=True)
def _clean_db(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    monkeypatch.setattr(config, "current_source_host", lambda: "test-device.local")
    storage.init_db()
    yield


def _insert_task(task_id: int, notes: str = "") -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, "
            "completed_at, source_host, claimed_by, evidence_ref, notes, project, "
            "domain, lane) VALUES (?, 'human authority wedge', 'done', 'high', "
            "'2026-09-19T00:00:00+00:00', '2026-09-19T00:00:00+00:00', "
            "'2026-09-19T00:00:00+00:00', 'test-device.local', 'Desktop_Agent4', "
            "NULL, ?, 'unified-agent', 'hobby', 'hobby')",
            (task_id, notes),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unit-level: config.actor_kind_for_identity
# ---------------------------------------------------------------------------


def test_actor_kind_classifies_fleet_agents_as_agent():
    for label in (
        "Desktop_Agent2",
        "Desktop_Agent5",
        "Laptop_Agent1",
        "server_agent3",
        "desktop_agent2",
    ):
        assert config.actor_kind_for_identity(label) == "agent", label


def test_actor_kind_classifies_desktop_person_as_human():
    assert config.actor_kind_for_identity("Desktop_Person") == "human"
    # IDENTITY_ALIAS_ALLOWLIST["Desktop_Person"] == {"Operator"}
    assert config.actor_kind_for_identity("Operator") == "human"


def test_actor_kind_unknown_for_blank_or_malformed():
    assert config.actor_kind_for_identity(None) == "unknown"
    assert config.actor_kind_for_identity("") == "unknown"
    assert config.actor_kind_for_identity("   ") == "unknown"
    assert config.actor_kind_for_identity("not a well formed label!") == "unknown"


# ---------------------------------------------------------------------------
# Case 1: authenticated AGENT — must NOT yield verified human authority
# ---------------------------------------------------------------------------


def test_signed_agent_identity_is_not_verified_human():
    """This is the exact shape of the audit finding: a unique, validly-signed
    producer identity that is an agent (Desktop_Agent2), not a human."""
    provenance = evidence_report._human_provenance(
        {"notes": ""},
        approvals=[],
        delegations=[],
        verified_producers=[{"identity": "Desktop_Agent2"}],
    )
    assert provenance["value"] == "Desktop_Agent2", "actor evidence must be preserved, not dropped"
    assert provenance["actor_kind"] == "agent"
    assert provenance["human_authority"] == "unknown"
    assert provenance["verified"] is False, (
        "an agent's valid signature must never be reported as verified human authority"
    )
    assert provenance["conflict"] is False
    # The candidate itself still records that a valid signature exists.
    signed = [
        c for c in provenance["candidates"] if c["source"] == "evidence.producer_authenticated"
    ]
    assert signed and signed[0]["verified"] is True
    assert signed[0]["actor_kind"] == "agent"


def test_signed_agent_identity_end_to_end_report(monkeypatch):
    """An agent identity cannot approve. A different human can, and that stamp
    is what the report records as verified human authority."""
    _BEARER = "test-bearer-desktop-agent2-1217"
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        f"Desktop_Agent2:{_BEARER},Desktop_Person:test-bearer-desktop-operator-1217",
    )
    _insert_task(1217)
    with pytest.raises(ApprovalPendingError):
        check_approval_gate(
            "run_cmd",
            {"command": "echo agent-signed", "task_id": 1217},
            requested_by="Desktop_Agent4",
        )
    rid = approval_pending()["pending"][0]["id"]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET payload=? WHERE id=?",
            (json.dumps({"command": "echo agent-signed", "task_id": 1217}), rid),
        )
        conn.commit()
    finally:
        conn.close()

    auth_token = config.agent_identity_var.set("Desktop_Agent2")
    try:
        with pytest.raises(ToolError, match="human identity|separation of duties"):
            approval_resolve(rid, "approve")
    finally:
        config.agent_identity_var.reset(auth_token)
    assert approval_pending()["pending"][0]["id"] == rid

    human = config.agent_identity_var.set("Desktop_Person")
    try:
        row = approval_resolve(rid, "approve")
    finally:
        config.agent_identity_var.reset(human)
    assert row["resolved_by"] == "Desktop_Person"

    report = evidence_report.build_evidence_report(task_id=1217)
    assert report["chain_integrity"]["actor_authentication_verified"] is True
    rec = report["records"][0]
    assert rec["human"] == "Desktop_Person"
    assert rec["human_provenance"]["actor_kind"] == "human"
    assert rec["human_provenance"]["human_authority"] == "credential-attributed"


# ---------------------------------------------------------------------------
# Case 2: a real verified HUMAN
# ---------------------------------------------------------------------------


def test_signed_human_identity_is_verified():
    provenance = evidence_report._human_provenance(
        {"notes": ""},
        approvals=[],
        delegations=[],
        verified_producers=[{"identity": "Desktop_Person"}],
    )
    assert provenance["value"] == "Desktop_Person"
    assert provenance["actor_kind"] == "human"
    assert provenance["human_authority"] == "credential-attributed"
    assert provenance["verified"] is True
    assert provenance["conflict"] is False


# ---------------------------------------------------------------------------
# Case 3: unknown — no evidence at all
# ---------------------------------------------------------------------------


def test_no_candidates_is_unknown_not_false_positive():
    provenance = evidence_report._human_provenance({"notes": ""}, approvals=[], delegations=[])
    assert provenance["value"] == "unknown"
    assert provenance["actor_kind"] == "unknown"
    assert provenance["human_authority"] == "unknown"
    assert provenance["verified"] is False
    assert provenance["conflict"] is False


# ---------------------------------------------------------------------------
# Case 4: conflicts
# ---------------------------------------------------------------------------


def test_ambiguous_signed_producers_is_conflict_not_verified():
    """Two different signed identities disagree — fail closed, as before."""
    provenance = evidence_report._human_provenance(
        {"notes": ""},
        approvals=[],
        delegations=[],
        verified_producers=[{"identity": "Desktop_Person"}, {"identity": "Desktop_Agent2"}],
    )
    assert provenance["value"] == "conflict"
    assert provenance["actor_kind"] == "conflict"
    assert provenance["human_authority"] == "conflict"
    assert provenance["verified"] is False
    assert provenance["conflict"] is True


def test_agent_identity_presented_as_human_claim_is_conflict():
    """An explicit [human: ...] annotation naming a *known agent* identity is
    a direct conflict — a human-authority claim was made for a label the
    fleet map says is an agent — not a silently-accepted unverified human."""
    provenance = evidence_report._human_provenance(
        {"notes": "[human: Desktop_Agent2]"}, approvals=[], delegations=[]
    )
    assert provenance["value"] == "conflict"
    assert provenance["actor_kind"] == "conflict"
    assert provenance["human_authority"] == "conflict"
    assert provenance["verified"] is False
    assert provenance["conflict"] is True
    values = {c["value"] for c in provenance["candidates"]}
    assert "Desktop_Agent2" in values


def test_agent_identity_presented_as_human_claim_yields_to_real_verified_human():
    """A genuine verified human signature still settles the question even
    when an unrelated inferred candidate confusingly claims an agent as
    human — the strongest evidence (a real signature) wins."""
    provenance = evidence_report._human_provenance(
        {"notes": "[human: Desktop_Agent2]"},
        approvals=[],
        delegations=[],
        verified_producers=[{"identity": "Desktop_Person"}],
    )
    assert provenance["value"] == "Desktop_Person"
    assert provenance["actor_kind"] == "human"
    assert provenance["human_authority"] == "credential-attributed"
    assert provenance["verified"] is True


# ---------------------------------------------------------------------------
# Review findings on PR #641 (task #1217 follow-up):
#
# Finding 1: a source-aware classifier must not tag an ordinary human name
# (e.g. "Alice", from an explicit [human: ...] note or UA_OPERATOR_NAME) as
# an agent merely because it satisfies the open agent-label regex. Only a
# CONCRETE GOVERNED agent identity (a catalogued fleet label, or a member of
# a configured UA_AGENT_LABELS roster) may resolve to "agent" for those
# sources.
#
# Finding 2: an agent-signed producer must not suppress a genuine
# [human: <that same agent>] conflict — the unique-producer branch checked
# human_claim_conflict only for the verified-human path, not the
# agent/unknown path.
#
# These two pull in opposite directions and are tested side by side.
# ---------------------------------------------------------------------------


def test_is_governed_agent_identity_distinguishes_fleet_labels_from_ordinary_names():
    """Unit-level check of the new predicate driving source-aware classification."""
    assert config.is_governed_agent_identity("Desktop_Agent2") is True
    assert config.is_governed_agent_identity("desktop_agent2") is True  # case-insensitive
    assert config.is_governed_agent_identity("Alice") is False
    assert config.is_governed_agent_identity("Fallback Operator") is False
    assert config.is_governed_agent_identity("Desktop_Person") is False  # human, not agent
    assert config.is_governed_agent_identity(None) is False
    assert config.is_governed_agent_identity("") is False


def test_actor_kind_for_claim_never_uses_open_roster_fallback():
    """A well-formed but ungoverned label stays "unknown" via the claim-scoped
    classifier, unlike actor_kind_for_identity's open-roster fallback."""
    assert config.actor_kind_for_claim("Alice") == "unknown"
    assert config.actor_kind_for_identity("Alice") == "agent", (
        "sanity check: the open-roster classifier used for signed producer "
        "evidence is expected to still tag a well-formed custom label as agent"
    )
    assert config.actor_kind_for_claim("Desktop_Agent2") == "agent"
    assert config.actor_kind_for_claim("Desktop_Person") == "human"


def test_human_name_via_operator_env_is_preserved_as_human_evidence_not_agent(monkeypatch):
    """Finding 1 exact repro: UA_OPERATOR_NAME=Alice must not be reported as
    an agent, and must not manufacture a spurious conflict — it is
    unresolved human evidence with no signed producer to settle it."""
    monkeypatch.setenv("UA_OPERATOR_NAME", "Alice")
    provenance = evidence_report._human_provenance({"notes": ""}, approvals=[], delegations=[])
    assert provenance["value"] == "Alice"
    assert provenance["conflict"] is False
    operator_candidates = [
        c for c in provenance["candidates"] if c["source"] == "operator.environment"
    ]
    assert operator_candidates and operator_candidates[0]["value"] == "Alice"
    assert operator_candidates[0]["actor_kind"] != "agent", (
        "an ordinary human name that merely matches the agent-label regex "
        "must not be classified as an agent (task #1217 finding 1)"
    )
    assert provenance["actor_kind"] != "agent"
    assert provenance["human_authority"] != "conflict"


def test_human_note_alice_is_preserved_as_human_evidence_not_agent_or_conflict():
    """Finding 1, task-note form: [human: Alice] must not become a conflict —
    Alice is not a known agent identity, just a label-shaped ordinary name."""
    provenance = evidence_report._human_provenance(
        {"notes": "[human: Alice]"}, approvals=[], delegations=[]
    )
    assert provenance["value"] == "Alice"
    assert provenance["conflict"] is False
    assert provenance["actor_kind"] != "agent"
    assert provenance["human_authority"] != "conflict"
    note_candidates = [c for c in provenance["candidates"] if c["source"] == "task.notes"]
    assert note_candidates and note_candidates[0]["actor_kind"] != "agent"


def test_agent_signature_does_not_suppress_human_claim_conflict():
    """Finding 2 exact repro: [human: Desktop_Agent2] plus a valid
    Desktop_Agent2 producer event must surface a conflict, not silently
    report actor_kind="agent"/human_authority="unknown"/conflict=False."""
    provenance = evidence_report._human_provenance(
        {"notes": "[human: Desktop_Agent2]"},
        approvals=[],
        delegations=[],
        verified_producers=[{"identity": "Desktop_Agent2"}],
    )
    assert provenance["conflict"] is True, (
        "an agent's own valid signature confirms — rather than excuses — the "
        "contradiction with an explicit human claim naming that same agent"
    )
    assert provenance["actor_kind"] == "conflict"
    assert provenance["human_authority"] == "conflict"
    assert provenance["verified"] is False


def test_human_alias_in_agent_roster_does_not_create_claim_conflict(monkeypatch):
    """Configured human aliases must stay human when the roster also lists them."""
    monkeypatch.setenv("UA_AGENT_LABELS", "Operator:Desktop_Agent1")
    provenance = evidence_report._human_provenance(
        {"notes": "[human: Operator]"}, approvals=[], delegations=[]
    )
    assert config.is_governed_agent_identity("Operator") is False
    assert provenance["value"] == "Operator"
    assert provenance["actor_kind"] == "human"
    assert provenance["human_authority"] == "unknown"
    assert provenance["conflict"] is False


def test_agent_signature_preserves_inferred_human_conflict():
    """An authenticated agent identifies the actor but cannot settle humans."""
    provenance = evidence_report._human_provenance(
        {"notes": "[human: Alice] [human: Bob]"},
        approvals=[],
        delegations=[],
        verified_producers=[{"identity": "Desktop_Agent2"}],
    )
    assert provenance["value"] == "Desktop_Agent2"
    assert provenance["source"] == "evidence.producer_authenticated"
    assert provenance["actor_kind"] == "agent"
    assert provenance["human_authority"] == "conflict"
    assert provenance["verified"] is False
    assert provenance["conflict"] is True
    assert {c["value"] for c in provenance["candidates"]} >= {
        "Alice",
        "Bob",
        "Desktop_Agent2",
    }
    signed = [
        c for c in provenance["candidates"] if c["source"] == "evidence.producer_authenticated"
    ]
    assert signed and signed[0]["verified"] is True


def test_valid_desktop_person_producer_overrides_inferred_agent_claim():
    """A real verified human producer still resolves the question even when
    an inferred [human: ...] claim confusingly names a known agent — the
    verified human signature outranks the inferred contradiction (this is
    the counterpart to test_agent_signature_does_not_suppress_human_claim_conflict:
    findings 1 and 2 must both hold at once)."""
    provenance = evidence_report._human_provenance(
        {"notes": "[human: Desktop_Agent2]"},
        approvals=[],
        delegations=[],
        verified_producers=[{"identity": "Desktop_Person"}],
    )
    assert provenance["value"] == "Desktop_Person"
    assert provenance["actor_kind"] == "human"
    assert provenance["human_authority"] == "credential-attributed"
    assert provenance["verified"] is True
    assert provenance["conflict"] is False


# ---------------------------------------------------------------------------
# Finding 3: schema_version bumped for the human_authority semantic change.
# ---------------------------------------------------------------------------


def test_schema_version_bumped_for_human_authority_semantics(monkeypatch):
    _insert_task(1218)
    report = evidence_report.build_evidence_report(task_id=1218)
    # 1.2: `verified_via` (agent_bearer | idp) added by task #1249 (Epic E, E2);
    # `device`/`device_link` semantics from task #1250 (Epic E, E3).
    # 1.3: `provenance_graph` appendix added by task #1287.
    # 1.4: `anchor` header field added by task #1265 (Epic F, F2).
    # 1.5: `attestation` header field added by task #1267 (Epic F, F4).
    # 1.6: complete exports. 1.7: human_authority "verified" renamed
    # "credential-attributed" and report-level `sod` added (task #1673).
    assert report["schema_version"] == "1.8"
    assert report["schema_version"] != "1.0", (
        "human_provenance.verified changed meaning (actor authentication -> "
        "human authority) under task #1217; a version-aware consumer must "
        "be able to tell 1.0 and 1.1+ reports apart"
    )
