"""Task #1249 (Epic E / E2): IdP-verified producer events for WorkOS sessions.

An approval resolved under a verified WorkOS AuthKit session must stamp a
producer event keyed to the *human* identity, verified-by-idp — closing the
documented #1155 v1 hole where OAuth/IdP identities with no UA_AGENT_TOKENS
bearer could never stamp.

The event is the hub's attestation that the session was cryptographically
verified (JWKS + issuer + org + live re-check in ``workos_hub_auth``) at
request time. It is HMAC-signed with the leg's own configured secret
(``WORKOS_COOKIE_PASSWORD``) so report-time verification needs neither a live
session nor a network call — and it fails closed on a tampered signature, a
wrong organization, or a subject missing from the operator's identity map.
"""

from __future__ import annotations

import json
import time

import pytest

from unified_agent import config, events, storage
from unified_agent.audit import evidence_report, producer_sign
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
    check_approval_gate,
)

_ORG = "org-a"
_SUBJECT = "user-a"
_SESSION = "session-a"
_LABEL = "Desktop_Person"
_COOKIE_KEY = "x" * 44


@pytest.fixture(autouse=True)
def _clean_db(tmp_path, monkeypatch):
    monkeypatch.delenv("UA_OPERATOR_NAME", raising=False)
    monkeypatch.delenv("UA_HUMAN_IDENTITY", raising=False)
    # The human label deliberately has NO bearer — that is the #1155 hole.
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent2:codex-tok-1249")
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", _ORG)
    monkeypatch.setenv("UA_HUB_WORKOS_IDENTITY_MAP", json.dumps({_SUBJECT: _LABEL}))
    monkeypatch.setenv("WORKOS_COOKIE_PASSWORD", _COOKIE_KEY)
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
            "domain, lane, org_id) VALUES (?, 'idp producer wedge', 'done', 'high', "
            "'2026-09-19T00:00:00+00:00', '2026-09-19T00:00:00+00:00', "
            "'2026-09-19T00:00:00+00:00', 'test-device.local', 'Desktop_Agent4', "
            "NULL, ?, 'unified-agent', 'hobby', 'hobby', ?)",
            (task_id, notes, _ORG),
        )
        conn.commit()
    finally:
        conn.close()


def _as_workos_session(
    identity: str = _LABEL,
    *,
    subject: str = _SUBJECT,
    organization_id: str = _ORG,
    session_id: str = _SESSION,
    expires_at: int | None = None,
):
    """Bind the request context the way server.py's WorkOS leg does after
    `hub_workos.resolve()` succeeds (task #1202)."""
    import time

    return config.bind_workos_request_auth(
        identity=identity,
        subject=subject,
        organization_id=organization_id,
        session_id=session_id,
        expires_at=expires_at if expires_at is not None else int(time.time()) + 300,
    )


def _reset(*tokens):
    if len(tokens) == 3:
        config.reset_workos_request_auth(*tokens)
    else:
        id_token, meta_token = tokens
        config.agent_identity_var.reset(id_token)
        config.auth_metadata_var.reset(meta_token)


def _queue_and_resolve(task_id: int) -> dict:
    with pytest.raises(ApprovalPendingError):
        check_approval_gate(
            "run_cmd",
            {"command": "echo idp", "task_id": task_id},
            requested_by="Desktop_Agent4",
        )
    rid = approval_pending()["pending"][0]["id"]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET payload=? WHERE id=?",
            (json.dumps({"command": "echo idp", "task_id": task_id}), rid),
        )
        conn.commit()
    finally:
        conn.close()
    return {"rid": rid}


def _producer_rows():
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT ulid, channel, payload, originator, entry_hash, prev_hash, "
            "episode_id, causal_parent_id, created_at, dedupe_key FROM event_log "
            "WHERE channel=?",
            (producer_sign.CHANNEL_PRODUCER_AUTHENTICATED,),
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Happy path: WorkOS-session approval stamps an IdP-verified producer event and
# the report marks the human link verified-by-idp — with NO bearer for the
# human label (the #1155 v1 hole, closed).
# ---------------------------------------------------------------------------


def test_workos_session_approval_stamps_idp_producer_and_marks_verified_by_idp():
    _insert_task(1249)
    rid = _queue_and_resolve(1249)["rid"]
    tokens = _as_workos_session()
    try:
        row = approval_resolve(rid, "approve", resolved_by="spoofed")
    finally:
        _reset(*tokens)
    assert row["resolved_by"] == _LABEL

    producers = _producer_rows()
    assert len(producers) == 1
    payload = json.loads(producers[0]["payload"])
    assert payload["identity"] == _LABEL
    assert payload["kind"] == "approval"
    assert payload["ref_id"] == str(rid)
    assert payload["scheme"] == producer_sign.SCHEME_WORKOS_IDP_V1
    assert payload["workos_subject"] == _SUBJECT
    assert payload["workos_organization_id"] == _ORG
    assert payload["workos_session_id"] == _SESSION
    assert "signature" in payload
    # No credential material may appear in the ledger event.
    assert _COOKIE_KEY not in producers[0]["payload"]
    assert producers[0]["entry_hash"]
    assert producer_sign.verify_producer_signature(payload) is True

    report = evidence_report.build_evidence_report(task_id=1249)
    assert report["chain_integrity"]["verified"] is True
    assert report["chain_integrity"]["actor_authentication_verified"] is True
    assert report["chain_integrity"]["provenance_verified"] is True
    rec = report["records"][0]
    assert rec["human"] == _LABEL
    provenance = rec["human_provenance"]
    assert provenance["actor_kind"] == "human"
    assert provenance["human_authority"] == "credential-attributed"
    assert provenance["verified"] is True
    assert provenance["verified_via"] == "idp"
    signed = [
        c for c in provenance["candidates"] if c["source"] == "evidence.producer_authenticated"
    ]
    assert signed and signed[0]["verified_via"] == "idp"

    markdown = evidence_report.format_evidence_report(report, "markdown")
    assert _LABEL in markdown
    assert "credential-attributed-by-idp" in markdown
    assert "human_authority=verified" not in markdown
    assert _COOKIE_KEY not in json.dumps(report)


def test_idp_stamp_does_not_require_a_bearer_for_the_identity():
    """Regression guard for the actual gap: Desktop_Person has no
    UA_AGENT_TOKENS entry in this fixture, so the bearer scheme cannot have
    produced the stamp."""
    _insert_task(1250)
    rid = _queue_and_resolve(1250)["rid"]
    tokens = _as_workos_session()
    try:
        approval_resolve(rid, "approve")
    finally:
        _reset(*tokens)
    producers = _producer_rows()
    assert len(producers) == 1
    payload = json.loads(producers[0]["payload"])
    assert payload["scheme"] == producer_sign.SCHEME_WORKOS_IDP_V1
    assert producer_sign.verify_producer_signature(payload) is True


# ---------------------------------------------------------------------------
# Stamp-time fail-closed cases: a malformed or inconsistent session context
# must produce no event at all (the approval itself still resolves).
# ---------------------------------------------------------------------------


def test_workos_metadata_with_wrong_org_does_not_stamp():
    _insert_task(1251)
    rid = _queue_and_resolve(1251)["rid"]
    tokens = _as_workos_session(organization_id="org-b")
    try:
        row = approval_resolve(rid, "approve")
    finally:
        _reset(*tokens)
    assert row["resolved_by"] == _LABEL
    assert _producer_rows() == []


def test_workos_subject_missing_from_identity_map_does_not_stamp():
    _insert_task(1252)
    rid = _queue_and_resolve(1252)["rid"]
    tokens = _as_workos_session(subject="user-not-mapped")
    try:
        approval_resolve(rid, "approve")
    finally:
        _reset(*tokens)
    assert _producer_rows() == []


def test_expired_session_expiry_does_not_stamp():
    _insert_task(1253)
    rid = _queue_and_resolve(1253)["rid"]
    tokens = _as_workos_session(expires_at=int(time.time()) - 1)
    try:
        approval_resolve(rid, "approve")
    finally:
        _reset(*tokens)
    assert _producer_rows() == []


def test_workos_metadata_without_cookie_key_does_not_stamp(monkeypatch):
    monkeypatch.delenv("WORKOS_COOKIE_PASSWORD", raising=False)
    _insert_task(1254)
    rid = _queue_and_resolve(1254)["rid"]
    tokens = _as_workos_session()
    try:
        approval_resolve(rid, "approve")
    finally:
        _reset(*tokens)
    assert _producer_rows() == []


def test_bearer_resolver_still_stamps_hmac_sha256_v1(monkeypatch):
    """Regression: the bearer path is unchanged when auth_method is not workos."""
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:operator-tok-1249")
    _insert_task(1255)
    rid = _queue_and_resolve(1255)["rid"]
    token = config.agent_identity_var.set(_LABEL)
    try:
        approval_resolve(rid, "approve")
    finally:
        config.agent_identity_var.reset(token)
    producers = _producer_rows()
    assert len(producers) == 1
    payload = json.loads(producers[0]["payload"])
    assert payload["scheme"] == producer_sign.SCHEME_HMAC_SHA256_V1
    assert producer_sign.verify_producer_signature(payload) is True
    report = evidence_report.build_evidence_report(task_id=1255)
    provenance = report["records"][0]["human_provenance"]
    assert provenance["verified"] is True
    assert provenance["verified_via"] == "agent_bearer"


# ---------------------------------------------------------------------------
# Verify-time fail-closed cases: tampering or config drift flips the report
# back to unverified.
# ---------------------------------------------------------------------------


def _stamp_one(task_id: int) -> dict:
    _insert_task(task_id)
    rid = _queue_and_resolve(task_id)["rid"]
    tokens = _as_workos_session()
    try:
        approval_resolve(rid, "approve")
    finally:
        _reset(*tokens)
    rows = _producer_rows()
    assert len(rows) == 1
    return json.loads(rows[0]["payload"])


def test_tampered_idp_signature_fails_closed():
    payload = _stamp_one(1256)
    payload["signature"] = "0" * 64
    assert producer_sign.verify_producer_signature(payload) is False


def test_tampered_idp_identity_fails_closed():
    payload = _stamp_one(1257)
    payload["identity"] = "Desktop_Agent2"
    assert producer_sign.verify_producer_signature(payload) is False


def test_tampered_idp_subject_fails_closed():
    payload = _stamp_one(1258)
    payload["workos_subject"] = "user-b"
    assert producer_sign.verify_producer_signature(payload) is False


def test_idp_event_under_wrong_configured_org_fails_closed(monkeypatch):
    payload = _stamp_one(1259)
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org-b")
    assert producer_sign.verify_producer_signature(payload) is False


def test_idp_event_under_missing_identity_mapping_fails_closed(monkeypatch):
    payload = _stamp_one(1260)
    monkeypatch.setenv("UA_HUB_WORKOS_IDENTITY_MAP", json.dumps({"user-b": _LABEL}))
    assert producer_sign.verify_producer_signature(payload) is False


def test_idp_event_without_leg_config_fails_closed(monkeypatch):
    payload = _stamp_one(1261)
    for name in (
        "UA_HUB_WORKOS_ORGANIZATION_ID",
        "UA_HUB_WORKOS_IDENTITY_MAP",
        "WORKOS_COOKIE_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    assert producer_sign.verify_producer_signature(payload) is False


def test_idp_payload_missing_fields_fails_closed():
    base = {
        "identity": _LABEL,
        "kind": "approval",
        "ref_id": "1",
        "scheme": producer_sign.SCHEME_WORKOS_IDP_V1,
        "workos_subject": _SUBJECT,
        "workos_organization_id": _ORG,
        "workos_session_id": _SESSION,
        "workos_expires_at": int(time.time()) + 300,
        "signature": "0" * 64,
    }
    for field in (
        "identity",
        "kind",
        "ref_id",
        "workos_subject",
        "workos_organization_id",
        "workos_session_id",
        "workos_expires_at",
        "signature",
    ):
        payload = {k: v for k, v in base.items() if k != field}
        assert producer_sign.verify_producer_signature(payload) is False, field


def test_tampered_idp_event_flips_report_provenance_false():
    payload = _stamp_one(1262)
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT ulid, payload, prev_hash, entry_hash, channel, originator, "
            "episode_id, causal_parent_id, created_at, dedupe_key FROM event_log "
            "WHERE channel=?",
            (producer_sign.CHANNEL_PRODUCER_AUTHENTICATED,),
        ).fetchone()
        payload["signature"] = "0" * 64
        body = json.dumps(payload)
        # Keep the hash chain valid so only the signature check is exercised.
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

    report = evidence_report.build_evidence_report(task_id=1262)
    assert report["chain_integrity"]["verified"] is True
    assert report["chain_integrity"]["actor_authentication_verified"] is False
    assert report["chain_integrity"]["provenance_verified"] is False
    assert report["records"][0]["human_provenance"]["verified"] is False
