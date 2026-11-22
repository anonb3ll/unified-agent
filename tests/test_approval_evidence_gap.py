"""Evidence gaps on approval resolution fail loud (task #1672, H12).

Task #1640 turned a *raising* producer stamp into a warning plus an
``audit.evidence.gap`` event. Two holes remained:

1. ``stamp_authenticated_producer_event`` swallows its own failures and returns
   ``None``, so the caller never saw them. A ``None`` for a verified resolver is
   now a gap too.
2. A high-tier approval (a tool in ``approvals.HIGH_TIER_EVIDENCE_TOOLS``) went
   through without its signed attribution. It now refuses: the row stays
   pending and the error names the gap. A low-tier approval still completes,
   records the gap, and the gap shows on ``/api/attention``.
"""

from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, events, server, storage, webui
from unified_agent.audit import producer_sign
from unified_agent.tools import approvals, doctor
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
    check_approval_gate,
)
from unified_agent.webui import api as webui_api

HIGH_TOOL = "work_product_release"
LOW_TOOL = "run_cmd"
RESOLVER = "Desktop_Person"


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    storage.init_db()
    yield


def _raising_stamp(**kwargs):
    raise RuntimeError("signer offline")


def _none_stamp(**kwargs):
    return None


FAILURES = pytest.mark.parametrize(
    ("stamp", "error_type"),
    [(_raising_stamp, "RuntimeError"), (_none_stamp, "NoProducerStamp")],
    ids=["raises", "returns-none"],
)


def _queue(tool: str, payload: dict, monkeypatch) -> int:
    if tool == LOW_TOOL:
        monkeypatch.setenv("UA_APPROVAL_TOOLS", LOW_TOOL)
    with pytest.raises(ApprovalPendingError):
        check_approval_gate(tool, payload, requested_by="Desktop_Agent1", force=True)
    return next(r["id"] for r in approval_pending()["pending"] if r["tool"] == tool)


def _status(approval_id: int) -> str:
    conn = storage.connect()
    try:
        row = conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return row["status"]
    finally:
        conn.close()


def _gaps() -> list[dict]:
    return events.query_events(channels=[approvals.CHANNEL_EVIDENCE_GAP])


def _as_verified_resolver(monkeypatch, stamp) -> None:
    monkeypatch.setattr(config, "verified_agent_identity", lambda: RESOLVER)
    monkeypatch.setattr(producer_sign, "stamp_authenticated_producer_event", stamp)


def test_high_tier_set_is_the_separation_of_duties_set():
    assert HIGH_TOOL in approvals.HIGH_TIER_EVIDENCE_TOOLS
    assert LOW_TOOL not in approvals.HIGH_TIER_EVIDENCE_TOOLS
    assert approvals.evidence_tier(HIGH_TOOL) == "high"
    assert approvals.evidence_tier(LOW_TOOL) == "low"


@FAILURES
def test_high_tier_approval_refuses_without_a_producer_stamp(monkeypatch, stamp, error_type):
    rid = _queue(HIGH_TOOL, {"release_id": "r-1", "task_id": 77}, monkeypatch)
    _as_verified_resolver(monkeypatch, stamp)

    with pytest.raises(ToolError, match="evidence gap") as exc_info:
        approval_resolve(rid, "approve")
    message = str(exc_info.value)
    assert str(rid) in message and HIGH_TOOL in message
    assert "UA_AGENT_TOKENS" in message  # actionable: says what to fix

    assert _status(rid) == "pending"  # fail closed: nothing committed
    resolved = [
        e
        for e in events.query_events(channels=[approvals.CHANNEL_APPROVAL_RESOLVED])
        if e["payload"].get("approval_id") == rid
    ]
    assert resolved == []

    gaps = _gaps()
    assert len(gaps) == 1
    payload = gaps[0]["payload"]
    assert payload["kind"] == "approval"
    assert payload["ref_id"] == str(rid)
    assert payload["task_id"] == "77"
    assert payload["tier"] == "high"
    assert payload["outcome"] == "refused"
    assert payload["error_type"] == error_type
    assert "signer offline" not in str(payload)  # never the exception message


def test_high_tier_approval_with_a_stamp_completes(monkeypatch):
    rid = _queue(HIGH_TOOL, {"release_id": "r-2"}, monkeypatch)
    calls: list[dict] = []

    def ok_stamp(**kwargs):
        calls.append(kwargs)
        return "01STAMPEDEVENT"

    _as_verified_resolver(monkeypatch, ok_stamp)
    row = approval_resolve(rid, "approve")
    assert row["status"] == "approved"
    assert len(calls) == 1 and calls[0]["kind"] == "approval" and calls[0]["ref_id"] == rid
    assert _gaps() == []


@FAILURES
def test_high_tier_deny_still_completes_and_records_the_gap(monkeypatch, stamp, error_type):
    """Refusing a deny would keep a high-risk request alive; the safe direction proceeds."""
    rid = _queue(HIGH_TOOL, {"release_id": "r-3"}, monkeypatch)
    _as_verified_resolver(monkeypatch, stamp)
    row = approval_resolve(rid, "deny", reason="no")
    assert row["status"] == "denied"
    gaps = _gaps()
    assert len(gaps) == 1
    assert gaps[0]["payload"]["tier"] == "high"
    assert gaps[0]["payload"]["outcome"] == "proceeded"
    assert gaps[0]["payload"]["error_type"] == error_type


def test_unverified_resolver_expects_no_stamp(monkeypatch):
    """No verified identity, no stamp expected: no gap (enforced mode refuses earlier)."""
    rid = _queue(LOW_TOOL, {"command": "echo unverified"}, monkeypatch)
    monkeypatch.setattr(producer_sign, "stamp_authenticated_producer_event", _none_stamp)
    row = approval_resolve(rid, "approve", resolved_by="Operator")
    assert row["status"] == "approved"
    assert _gaps() == []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("UA_ZULIP_SITE", raising=False)
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    real_probe = doctor._service_probe
    executor_unit = doctor._SERVICE_UNITS["active_executor_service"]

    def _probe(unit: str, timeout: float):
        if unit == executor_unit:
            return {"LoadState": "not-found"}
        return real_probe(unit, timeout)

    monkeypatch.setattr(doctor, "_service_probe", _probe)
    monkeypatch.setattr(webui_api, "survey_worktrees", lambda: [])
    webui_api._doctor_cache = None
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c
    webui_api._doctor_cache = None


@FAILURES
def test_low_tier_approval_completes_records_gap_and_shows_in_attention(
    monkeypatch, client, stamp, error_type
):
    rid = _queue(LOW_TOOL, {"command": "echo low", "task_id": 12}, monkeypatch)
    _as_verified_resolver(monkeypatch, stamp)

    row = approval_resolve(rid, "approve")
    assert row["status"] == "approved"

    gaps = _gaps()
    assert len(gaps) == 1
    payload = gaps[0]["payload"]
    assert payload["tier"] == "low"
    assert payload["outcome"] == "proceeded"
    assert payload["error_type"] == error_type

    monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
    body = client.get("/api/attention").json()
    gap_items = [i for i in body["items"] if i["kind"] == "evidence_gap"]
    assert len(gap_items) == 1, body
    item = gap_items[0]
    assert item["source"] == {"evidence_gap": gaps[0]["ulid"]}
    assert item["source_subject"] == f"evidence_gap:{gaps[0]['ulid']}"
    assert f"approval {rid}" in item["title"]
    assert error_type in item["reason"]
    assert body["counts"]["evidence_gap"] == 1


def test_refused_high_tier_gap_is_high_severity_in_attention(monkeypatch, client):
    rid = _queue(HIGH_TOOL, {"release_id": "r-4"}, monkeypatch)
    _as_verified_resolver(monkeypatch, _none_stamp)
    with pytest.raises(ToolError):
        approval_resolve(rid, "approve")

    monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
    body = client.get("/api/attention").json()
    gap_items = [i for i in body["items"] if i["kind"] == "evidence_gap"]
    assert len(gap_items) == 1
    assert gap_items[0]["severity"] == "high"
    assert "refused" in gap_items[0]["reason"]


# --- review follow-ups (PR #1082) ----------------------------------------------


@pytest.mark.parametrize("winner_status", ["denied", "approved"], ids=["vs-deny", "vs-approve"])
def test_losing_high_tier_approve_leaves_no_signed_stamp(monkeypatch, winner_status):
    """A resolver that loses the status race must not leave a producer stamp.

    Evidence readers join stamps by approval id, so a stray stamp from the
    losing approver would attribute the request to someone who never resolved it.
    """
    rid = _queue(HIGH_TOOL, {"release_id": "r-race"}, monkeypatch)
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:synthetic-bearer-h")
    monkeypatch.setattr(config, "verified_agent_identity", lambda: RESOLVER)

    real_kind = config.actor_kind_for_identity
    fired: list[bool] = []

    def deny_concurrently(label):
        if not fired:
            fired.append(True)
            conn = storage.connect()
            try:
                conn.execute(
                    "UPDATE approvals SET status=?, resolved_by='Operator' WHERE id=?",
                    (winner_status, rid),
                )
                conn.commit()
            finally:
                conn.close()
        return real_kind(label)

    monkeypatch.setattr(config, "actor_kind_for_identity", deny_concurrently)
    with pytest.raises(ToolError, match="changed concurrently"):
        approval_resolve(rid, "approve")
    assert fired
    assert _status(rid) == winner_status
    stamps = [
        e
        for e in events.query_events(channels=[producer_sign.CHANNEL_PRODUCER_AUTHENTICATED])
        if e["payload"].get("ref_id") == str(rid)
    ]
    assert stamps == []
    # Nor any resolution record for the losing attempt.
    resolved = [
        e
        for e in events.query_events(channels=[approvals.CHANNEL_APPROVAL_RESOLVED])
        if e["payload"].get("approval_id") == rid
    ]
    assert resolved == []


def test_high_tier_stamp_commits_with_the_approval(monkeypatch):
    """With a real bearer the stamp lands, in the same commit as the status change."""
    rid = _queue(HIGH_TOOL, {"release_id": "r-real"}, monkeypatch)
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:synthetic-bearer-h")
    monkeypatch.setattr(config, "verified_agent_identity", lambda: RESOLVER)
    assert approval_resolve(rid, "approve")["status"] == "approved"
    stamps = [
        e
        for e in events.query_events(channels=[producer_sign.CHANNEL_PRODUCER_AUTHENTICATED])
        if e["payload"].get("ref_id") == str(rid)
    ]
    assert len(stamps) == 1
    assert producer_sign.verify_producer_signature(stamps[0]["payload"])
    assert _gaps() == []


def test_refusal_does_not_claim_an_unrecorded_gap(monkeypatch):
    rid = _queue(HIGH_TOOL, {"release_id": "r-norecord"}, monkeypatch)
    _as_verified_resolver(monkeypatch, _none_stamp)
    real_record = events.record_event

    def failing_gap_record(channel, *args, **kwargs):
        if channel == approvals.CHANNEL_EVIDENCE_GAP:
            raise RuntimeError("ledger offline")
        return real_record(channel, *args, **kwargs)

    monkeypatch.setattr(events, "record_event", failing_gap_record)
    with pytest.raises(ToolError) as exc_info:
        approval_resolve(rid, "approve")
    message = str(exc_info.value)
    assert "could not be recorded" in message
    assert "is recorded as" not in message
    assert _status(rid) == "pending"


def test_relay_gap_gets_its_own_attention_reason(monkeypatch, client):
    events.record_event(
        approvals.CHANNEL_EVIDENCE_GAP,
        payload={
            "kind": "relay_delivery",
            "ref_id": "attempt-1",
            "approval_id": 5,
            "stage": "sent_record",
            "error_type": "OperationalError",
        },
    )
    body = client.get("/api/attention").json()
    gap_items = [i for i in body["items"] if i["kind"] == "evidence_gap"]
    assert len(gap_items) == 1
    reason = gap_items[0]["reason"]
    assert "relay.public.sent" in reason
    assert "delivered" in reason
    assert "without signed evidence" not in reason


def test_digest_lists_evidence_gaps_for_the_operator(monkeypatch, client):
    rid = _queue(HIGH_TOOL, {"release_id": "r-digest"}, monkeypatch)
    _as_verified_resolver(monkeypatch, _none_stamp)
    with pytest.raises(ToolError):
        approval_resolve(rid, "approve")
    monkeypatch.setattr(config, "verified_agent_identity", lambda: None)
    body = client.get("/api/digest").json()
    assert "evidence_gaps" in body["scanned_sources"]
    waiting = body["waiting_on_operator"]
    assert any(it["kind"] == "evidence_gap" for it in waiting), waiting


def test_mobile_projection_carries_evidence_gaps_as_a_generic_anomaly():
    from unified_agent.projection import attention_ack
    from unified_agent.projection import mobile_v1 as mobile

    item = {
        "kind": "evidence_gap",
        "severity": "high",
        "source_subject": "evidence_gap:01JABCDEF",
        "age_s": 5,
        "reason": "high-tier approval refused: producer stamp missing (NoProducerStamp)",
        "title": "approval 7 (work_product_release)",
    }
    proj = mobile.build_mobile_projection(
        attention_data={"items": [item]},
        health_data={"state": "healthy"},
        freshness="live",
    )
    parsed = mobile.parse_mobile_projection(mobile.to_wire(proj))
    assert parsed.attention.total == 1
    wrist = parsed.attention.items[0]
    # An existing kind and title, so deployed Watch clients (which reject an
    # unknown kind) still accept the projection; no gap detail leaves the hub.
    assert wrist.kind == "anomaly"
    assert wrist.safe_title == mobile.SAFE_TITLES["anomaly"]
    assert "work_product_release" not in json.dumps(mobile.to_wire(proj))

    # A wrist ack of that ref suppresses the hub-side item.
    attention_ack.record_acknowledgement(
        wrist.item_ref, action="ack", acked_by=RESOLVER, action_id="ack-gap-1"
    )
    kept, suppressed = attention_ack.filter_acknowledged([item])
    assert kept == [] and suppressed == [wrist.item_ref]
