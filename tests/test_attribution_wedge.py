"""Task #1155: attribution wedge — signed producer events + provenance flip.

TDD contract: signature scheme and hash chain are real (unstubbed). Clock may
be stubbed. Fail-closed on missing/tampered signatures.
"""

from __future__ import annotations

import json

import pytest

from unified_agent import config, events, storage
from unified_agent.audit import evidence_report, producer_sign
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
    check_approval_gate,
)
from unified_agent.tools.delegate import delegate, fire_completion_callbacks

_TOKEN = "test-attribution-bearer-desktop-operator-1155"
_LABEL = "Desktop_Person"


@pytest.fixture(autouse=True)
def _clean_db(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{_LABEL}:{_TOKEN}")
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    monkeypatch.setattr(config, "current_source_host", lambda: "test-device.local")
    storage.init_db()
    yield


def _insert_task(task_id: int = 1155, notes: str = "") -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, "
            "completed_at, source_host, claimed_by, evidence_ref, notes, project, "
            "domain, lane) VALUES (?, 'attribution wedge', 'done', 'high', "
            "'2026-09-19T00:00:00+00:00', '2026-09-19T00:00:00+00:00', "
            "'2026-09-19T00:00:00+00:00', 'test-device.local', 'Desktop_Agent4', "
            "NULL, ?, 'unified-agent', 'hobby', 'hobby')",
            (task_id, notes),
        )
        conn.commit()
    finally:
        conn.close()


def _as_authenticated(label: str = _LABEL):
    return config.agent_identity_var.set(label)


def test_approval_resolve_stamps_signed_producer_and_flips_provenance():
    _insert_task(1155)
    with pytest.raises(ApprovalPendingError):
        check_approval_gate(
            "run_cmd",
            {"command": "echo attribution", "task_id": 1155},
            requested_by="Desktop_Agent4",
        )
    rid = approval_pending()["pending"][0]["id"]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET payload=? WHERE id=?",
            (json.dumps({"command": "echo attribution", "task_id": 1155}), rid),
        )
        conn.commit()
    finally:
        conn.close()

    token = _as_authenticated()
    try:
        row = approval_resolve(rid, "approve", resolved_by="spoofed")
    finally:
        config.agent_identity_var.reset(token)

    assert row["resolved_by"] == _LABEL

    conn = storage.connect()
    try:
        producers = conn.execute(
            "SELECT channel, payload, originator, entry_hash FROM event_log WHERE channel=?",
            (producer_sign.CHANNEL_PRODUCER_AUTHENTICATED,),
        ).fetchall()
    finally:
        conn.close()
    assert len(producers) == 1
    payload = json.loads(producers[0]["payload"])
    assert payload["identity"] == _LABEL
    assert payload["kind"] == "approval"
    assert payload["scheme"] == producer_sign.SCHEME_HMAC_SHA256_V1
    assert "signature" in payload
    assert _TOKEN not in producers[0]["payload"]
    assert producers[0]["entry_hash"]
    assert producer_sign.verify_producer_signature(payload) is True

    report = evidence_report.build_evidence_report(task_id=1155)
    assert report["chain_integrity"]["verified"] is True
    assert report["chain_integrity"]["actor_authentication_verified"] is True
    assert report["chain_integrity"]["provenance_verified"] is True
    rec = report["records"][0]
    assert rec["human"] == _LABEL
    assert rec["human_provenance"]["verified"] is True
    assert _LABEL in evidence_report.format_evidence_report(report, "markdown")
    assert _TOKEN not in json.dumps(report)


def test_chain_without_signed_producer_keeps_provenance_false():
    _insert_task(1156)
    events.record_event(
        "agent.task.completed",
        payload={"task_id": 1156},
        originator="Desktop_Agent4",
    )
    report = evidence_report.build_evidence_report(task_id=1156)
    assert report["chain_integrity"]["verified"] is True
    assert report["chain_integrity"]["actor_authentication_verified"] is False
    assert report["chain_integrity"]["provenance_verified"] is False
    assert report["records"][0]["human_provenance"]["verified"] is False


def test_tampered_signature_flips_provenance_false():
    _insert_task(1157)
    with pytest.raises(ApprovalPendingError):
        check_approval_gate(
            "run_cmd",
            {"command": "echo tamper", "task_id": 1157},
            requested_by="Desktop_Agent4",
        )
    rid = approval_pending()["pending"][0]["id"]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET payload=? WHERE id=?",
            (json.dumps({"command": "echo tamper", "task_id": 1157}), rid),
        )
        conn.commit()
    finally:
        conn.close()

    token = _as_authenticated()
    try:
        approval_resolve(rid, "approve")
    finally:
        config.agent_identity_var.reset(token)

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT ulid, payload, prev_hash, entry_hash, channel, originator, "
            "episode_id, causal_parent_id, created_at, dedupe_key FROM event_log "
            "WHERE channel=?",
            (producer_sign.CHANNEL_PRODUCER_AUTHENTICATED,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        payload["signature"] = "0" * 64
        body = json.dumps(payload)
        # Recompute entry_hash so the global chain stays valid — isolates the
        # HMAC failure from hash-chain failure (both must independently fail closed).
        new_hash = events.compute_entry_hash(
            row["prev_hash"],
            row["ulid"],
            row["channel"],
            body,
            row["originator"],
            row["episode_id"],
            row["causal_parent_id"],
            row["created_at"],
            row["dedupe_key"],
        )
        with events.ledger_maintenance(conn):
            conn.execute(
                "UPDATE event_log SET payload=?, entry_hash=? WHERE ulid=?",
                (body, new_hash, row["ulid"]),
            )
        conn.commit()
    finally:
        conn.close()

    assert producer_sign.verify_producer_signature(payload) is False
    report = evidence_report.build_evidence_report(task_id=1157)
    assert report["chain_integrity"]["verified"] is True
    assert report["chain_integrity"]["actor_authentication_verified"] is False
    assert report["chain_integrity"]["provenance_verified"] is False
    assert report["records"][0]["human_provenance"]["verified"] is False


def test_missing_signature_field_fails_closed():
    payload = {
        "identity": _LABEL,
        "kind": "approval",
        "ref_id": "1",
        "task_id": "1",
        "scheme": producer_sign.SCHEME_HMAC_SHA256_V1,
    }
    assert producer_sign.verify_producer_signature(payload) is False


def test_unauthenticated_approval_resolve_does_not_stamp_producer():
    _insert_task(1158)
    with pytest.raises(ApprovalPendingError):
        check_approval_gate("run_cmd", {"command": "echo anon", "task_id": 1158})
    rid = approval_pending()["pending"][0]["id"]
    approval_resolve(rid, "approve", resolved_by="Operator")
    conn = storage.connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM event_log WHERE channel=?",
            (producer_sign.CHANNEL_PRODUCER_AUTHENTICATED,),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 0
    report = evidence_report.build_evidence_report(task_id=1158)
    assert report["chain_integrity"]["actor_authentication_verified"] is False
    assert report["chain_integrity"]["provenance_verified"] is False


def test_delegation_complete_stamps_signed_producer(monkeypatch):
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        f"{_LABEL}:{_TOKEN},Desktop_Agent1:claude-tok-1155,Desktop_agent3:agy-tok-1155",
    )
    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        res = delegate(
            task_spec="Attribution wedge delegation fixture",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
            callback="none",
        )
    finally:
        config.agent_identity_var.reset(token)

    task_id = res["task_id"]
    token = _as_authenticated()
    try:
        fire_completion_callbacks(task_id)
    finally:
        config.agent_identity_var.reset(token)

    conn = storage.connect()
    try:
        producers = conn.execute(
            "SELECT payload FROM event_log WHERE channel=?",
            (producer_sign.CHANNEL_PRODUCER_AUTHENTICATED,),
        ).fetchall()
    finally:
        conn.close()
    assert len(producers) >= 1
    payload = json.loads(producers[-1]["payload"])
    assert payload["kind"] == "delegation"
    assert payload["identity"] == _LABEL
    assert producer_sign.verify_producer_signature(payload) is True

    report = evidence_report.build_evidence_report(task_id=task_id)
    assert report["chain_integrity"]["actor_authentication_verified"] is True
    assert report["chain_integrity"]["provenance_verified"] is True
    assert report["records"][0]["human"] == _LABEL
    assert report["records"][0]["human_provenance"]["verified"] is True


def test_authenticated_without_bearer_mapping_does_not_stamp(monkeypatch):
    """A human alias with no UA_AGENT_TOKENS bearer still approves, but does not stamp.

    An agent identity cannot approve at all. The alias ``Operator`` is a human
    and is absent from this token map, so the resolve succeeds and the producer
    stamp stays fail-closed.
    """
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:other-token-only")
    _insert_task(1159)
    with pytest.raises(ApprovalPendingError):
        check_approval_gate("run_cmd", {"command": "echo nobearer", "task_id": 1159})
    rid = approval_pending()["pending"][0]["id"]
    token = config.agent_identity_var.set("Operator")
    try:
        approval_resolve(rid, "approve")
    finally:
        config.agent_identity_var.reset(token)
    conn = storage.connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM event_log WHERE channel=?",
            (producer_sign.CHANNEL_PRODUCER_AUTHENTICATED,),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 0
