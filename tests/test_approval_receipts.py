"""Durable approval-decision receipts back to the requester (task #2394).

A delivery intent is written with (or before) the decision; ``deliver``
re-reads the decision from the approvals table, appends one receipt under a
permanent idempotency key, hands it to the requester and only then deletes the
intent. ``sweep`` finishes intents a crash left behind.
"""

import json

import pytest

from unified_agent import approval_receipts, config, events, storage
from unified_agent.tools import approvals
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_pending,
    approval_resolve,
    check_approval_gate,
)

REQUESTER = "Desktop_agent3"


@pytest.fixture
def gated(monkeypatch):
    """The protocol in isolation: approval_resolve's own intent hook is off, so
    each test drives record_intent / deliver / sweep itself."""
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    monkeypatch.setattr(approvals, "_record_receipt_intent", lambda *a, **k: None)
    monkeypatch.setattr(approvals, "_deliver_receipt", lambda *a, **k: None)


@pytest.fixture
def wired(monkeypatch):
    """approval_resolve with its real receipt wiring."""
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")


def _request(command: str = "echo receipt") -> int:
    with pytest.raises(ApprovalPendingError):
        check_approval_gate("run_cmd", {"command": command}, requested_by=REQUESTER)
    return approval_pending()["pending"][-1]["id"]


def _record(approval_id: int, decision: str, requester: str | None = REQUESTER, **kw) -> None:
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        approval_receipts.record_intent(conn, approval_id, decision, requester, **kw)
        conn.commit()
    finally:
        conn.close()


def _intents() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM approval_receipt_intents ORDER BY approval_id, decision"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _receipts() -> list[dict]:
    rows = events.query_events(channels=[approval_receipts.CHANNEL], limit=100)
    return [r if isinstance(r, dict) else dict(r) for r in rows]


def _payload(row: dict) -> dict:
    body = row["payload"]
    return json.loads(body) if isinstance(body, str) else body


class Sink:
    def __init__(self, result: str = "delivered", fail: int = 0):
        self.calls: list[dict] = []
        self.result = result
        self.fail = fail

    def __call__(self, receipt: dict) -> str:
        self.calls.append(receipt)
        if self.fail:
            self.fail -= 1
            raise ConnectionError("sender down")
        return self.result


def test_schema_is_idempotent_and_created_by_init_db():
    conn = storage.connect()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(approval_receipt_intents)")}
        approval_receipts.ensure_schema(conn)
        approval_receipts.ensure_schema(conn)
    finally:
        conn.close()
    assert {"approval_id", "decision", "requester", "created_at", "org_id"} <= cols


def test_decision_vocabulary_is_normalised_and_bounded(gated):
    aid = _request()
    _record(aid, "deny")
    _record(aid, "denied")  # same intent, not a second row
    assert [(i["approval_id"], i["decision"]) for i in _intents()] == [(aid, "denied")]
    conn = storage.connect()
    try:
        with pytest.raises(ValueError, match="decision"):
            approval_receipts.record_intent(conn, aid, "pending", REQUESTER)
    finally:
        conn.close()
    assert approval_receipts.receipt_key(aid, "approve") == f"approval-receipt:{aid}:approved"


def test_deliver_appends_receipt_notifies_and_deletes_intent(gated):
    aid = _request()
    _record(aid, "approve")
    approval_resolve(aid, "approve", resolved_by="Operator")
    sink = Sink()
    [result] = approval_receipts.deliver(aid, notify=sink)
    assert result["outcome"] == "delivered"
    assert _intents() == []
    [row] = _receipts()
    assert row["dedupe_key"] == f"approval-receipt:{aid}:approved"
    payload = _payload(row)
    assert payload == {
        "approval_id": aid,
        "decision": "approved",
        "requester": REQUESTER,
        "resolution_event_id": payload["resolution_event_id"],
    }
    assert payload["resolution_event_id"]
    assert sink.calls == [
        {
            "receipt_id": row["ulid"],
            "key": row["dedupe_key"],
            "approval_id": aid,
            "decision": "approved",
            "requester": REQUESTER,
        }
    ]


def test_receipt_carries_no_request_body_or_tool_payload(gated):
    aid = _request("echo SECRET-LOOKING-VALUE")
    _record(aid, "deny")
    approval_resolve(aid, "deny", resolved_by="Operator", reason="no thanks SECRET")
    approval_receipts.deliver(aid, notify=Sink())
    [row] = _receipts()
    raw = json.dumps(_payload(row))
    assert "SECRET" not in raw and "echo" not in raw


def test_requester_is_reread_from_the_approvals_table(gated):
    aid = _request()
    _record(aid, "approve", requester="Spoofed_Label")
    approval_resolve(aid, "approve", resolved_by="Operator")
    sink = Sink()
    approval_receipts.deliver(aid, notify=sink)
    assert sink.calls[0]["requester"] == REQUESTER
    assert _payload(_receipts()[0])["requester"] == REQUESTER


def test_uncommitted_decision_is_kept_while_young_then_abandoned(gated):
    aid = _request()
    _record(aid, "approve")  # intent written, decision never committed
    sink = Sink()
    [young] = approval_receipts.deliver(aid, notify=sink)
    assert young["outcome"] == "in_flight"
    assert len(_intents()) == 1
    later = approval_receipts._now() + approval_receipts.ABANDON_AFTER_S + 1
    [old] = approval_receipts.deliver(aid, notify=sink, now=later)
    assert old["outcome"] == "abandoned"
    assert _intents() == [] and _receipts() == [] and sink.calls == []


def test_notify_failure_keeps_intent_and_retry_reuses_the_receipt(gated):
    aid = _request()
    _record(aid, "deny")
    approval_resolve(aid, "deny", resolved_by="Operator")
    sink = Sink(fail=1)
    [first] = approval_receipts.deliver(aid, notify=sink)
    assert first["outcome"] == "failed" and first["error"] == "ConnectionError"
    [intent] = _intents()
    assert intent["attempts"] == 1 and intent["last_error"] == "ConnectionError"
    [second] = approval_receipts.deliver(aid, notify=sink)
    assert second["outcome"] == "delivered"
    assert len(_receipts()) == 1  # permanent key: one receipt across retries
    assert sink.calls[0]["receipt_id"] == sink.calls[1]["receipt_id"]
    assert _intents() == []


def _receipt_mail(aid: int, decision: str) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT message_id, sender, recipient, act, json_extract(e.payload, '$.body') AS body "
            "FROM message_index i "
            "JOIN event_log e ON e.ulid = i.event_ulid WHERE i.message_id = ?",
            (approval_receipts.message_id_for(aid, decision),),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def test_default_notify_messages_the_requester_once(gated):
    """Task #2435: the receipt reaches the requesting agent as one hub inform."""
    aid = _request()
    _record(aid, "deny")
    approval_resolve(aid, "deny", resolved_by="Operator")
    [result] = approval_receipts.deliver(aid)
    assert result["outcome"] == "messaged"
    assert _intents() == [] and len(_receipts()) == 1
    [mail] = _receipt_mail(aid, "denied")
    assert mail["sender"] == "hub-approvals" and mail["recipient"] == REQUESTER
    assert mail["act"] == "inform" and "run_cmd" not in mail["body"]
    # A crash after the send leaves the intent; the retry is a duplicate, not a second message.
    _record(aid, "deny")
    [again] = approval_receipts.deliver(aid)
    assert again["outcome"] == "duplicate" and len(_receipt_mail(aid, "denied")) == 1


def test_default_notify_skips_a_requester_without_a_mailbox(gated):
    aid = _request()
    _record(aid, "approve", requester="Operator")
    approval_resolve(aid, "approve", resolved_by="Operator")
    receipt = {
        "approval_id": aid,
        "decision": "approved",
        "receipt_id": "x",
        "requester": "Operator",
    }
    assert approval_receipts.hub_message_sender(receipt) == "no_agent_mailbox"
    assert approval_receipts.hub_message_sender({**receipt, "requester": ""}) == "no_agent_mailbox"
    assert _receipt_mail(aid, "approved") == []


def test_service_sender_and_prefix_are_reserved_for_the_hub(gated):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.messaging.send import send_message, send_service_notice

    with pytest.raises(ToolError, match="reserved for the hub"):
        send_message(act="inform", to_agent=REQUESTER, body="x", from_agent="hub-approvals")
    with pytest.raises(ToolError, match="reserved for hub service notices"):
        send_message(
            act="inform",
            to_agent=REQUESTER,
            body="x",
            from_agent="Desktop_Agent2",
            message_id="approval-receipt-1-approved",
        )
    with pytest.raises(ToolError, match="not registered"):
        send_service_notice(
            sender="hub-approvals",
            message_id="delivery-1",
            recipient=REQUESTER,
            body="x",
            task_id=None,
        )


def test_approve_then_revoke_yields_two_receipts_in_order(gated):
    aid = _request()
    _record(aid, "approve")
    approval_resolve(aid, "approve", resolved_by="Operator")
    _record(aid, "revoke")
    approval_resolve(aid, "revoke", resolved_by="Operator")
    sink = Sink()
    results = approval_receipts.deliver(aid, notify=sink)
    assert [r["decision"] for r in results] == ["approved", "revoked"]
    assert all(r["outcome"] == "delivered" for r in results)
    assert {r["dedupe_key"] for r in _receipts()} == {
        f"approval-receipt:{aid}:approved",
        f"approval-receipt:{aid}:revoked",
    }


def test_consumed_pass_still_confirms_the_approval(gated):
    aid = _request()
    _record(aid, "approve")
    approval_resolve(aid, "approve", resolved_by="Operator")
    check_approval_gate("run_cmd", {"command": "echo receipt"}, requested_by=REQUESTER)
    assert approvals.get_approval(aid)["status"] == "consumed"
    [result] = approval_receipts.deliver(aid, notify=Sink())
    assert result["outcome"] == "delivered"


def test_sweep_finishes_crashed_intents_and_is_idempotent(gated):
    old_id, new_id = _request("echo one"), _request("echo two")
    for aid in (old_id, new_id):
        _record(aid, "deny")
        approval_resolve(aid, "deny", resolved_by="Operator")
    # Simulate a crash: old_id's intent is past the threshold, new_id's is not.
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approval_receipt_intents SET created_epoch=created_epoch-120 "
            "WHERE approval_id=?",
            (old_id,),
        )
        conn.commit()
    finally:
        conn.close()
    sink = Sink()
    counts = approval_receipts.sweep(older_than_s=60, notify=sink)
    assert counts == {"scanned": 1, "delivered": 1}
    assert [i["approval_id"] for i in _intents()] == [new_id]
    assert approval_receipts.sweep(older_than_s=60, notify=sink) == {"scanned": 0}
    later = approval_receipts._now() + 3600
    assert approval_receipts.sweep(now=later, notify=sink) == {"scanned": 1, "delivered": 1}
    assert len(_receipts()) == 2 and len(sink.calls) == 2


def test_live_claim_blocks_a_concurrent_deliverer(gated):
    aid = _request()
    _record(aid, "deny")
    approval_resolve(aid, "deny", resolved_by="Operator")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approval_receipt_intents SET claimed_epoch=? WHERE approval_id=?",
            (approval_receipts._now(), aid),
        )
        conn.commit()
    finally:
        conn.close()
    sink = Sink()
    [busy] = approval_receipts.deliver(aid, notify=sink)
    assert busy["outcome"] == "claimed" and sink.calls == []
    stale = approval_receipts._now() + approval_receipts.STALE_CLAIM_S + 1
    [done] = approval_receipts.deliver(aid, notify=sink, now=stale)
    assert done["outcome"] == "delivered"


def test_tenant_fence_hides_another_orgs_intents(gated, monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    aid = _request()
    _record(aid, "deny")
    approval_resolve(aid, "deny", resolved_by="Operator")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    assert approval_receipts.deliver(aid, notify=Sink()) == []
    assert approval_receipts.sweep(now=approval_receipts._now() + 3600) == {"scanned": 0}
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    assert _intents()[0]["org_id"] == "org_a"
    [result] = approval_receipts.deliver(aid, notify=Sink())
    assert result["outcome"] == "delivered"


def test_intent_rolls_back_with_a_failed_decision_transaction(gated):
    aid = _request()
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        approval_receipts.record_intent(conn, aid, "approve", REQUESTER)
        conn.rollback()
    finally:
        conn.close()
    assert _intents() == []


# --- Wired into approval_resolve (task #2394, operator-approved red-tier scope) ---


def test_resolving_an_approval_leaves_exactly_one_receipt_for_the_requester(wired):
    rid = _request("echo wired")
    approval_resolve(rid, "deny", resolved_by="Operator", reason="not now")
    receipts = [_payload(r) for r in _receipts() if _payload(r)["approval_id"] == rid]
    assert len(receipts) == 1
    assert receipts[0]["decision"] == "denied" and receipts[0]["requester"] == REQUESTER
    assert "not now" not in json.dumps(receipts[0])
    assert _intents() == []
    # The hygiene sweep finds nothing left to do.
    assert approval_receipts.sweep(older_than_s=0)["scanned"] == 0


def test_a_failing_intent_write_never_blocks_the_decision(wired, monkeypatch):
    rid = _request("echo still decided")

    def broken(*_a, **_k):
        raise RuntimeError("receipt table unavailable")

    monkeypatch.setattr(approval_receipts, "record_intent", broken)
    row = approval_resolve(rid, "deny", resolved_by="Operator")
    assert row["status"] == "denied"
    assert approvals.get_approval(rid)["status"] == "denied"
    assert _intents() == []


# --- Reconcile: a decision whose intent insert was aborted (task #2484) ---


def _resolve_losing_the_intent(monkeypatch, rid: int, decision: str = "deny") -> None:
    """The decision and its resolution event commit; the intent insert is aborted."""

    def broken(*_a, **_k):
        raise RuntimeError("receipt table unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(approval_receipts, "record_intent", broken)
        approval_resolve(rid, decision, resolved_by=config.operator_label())
    assert _intents() == []


def test_sweep_reconciles_a_decision_that_lost_its_intent_exactly_once(wired, monkeypatch):
    rid = _request("echo lost intent")
    _resolve_losing_the_intent(monkeypatch, rid)
    # The default sender, as queue hygiene calls it: one event and one message.
    assert approval_receipts.sweep() == {"scanned": 1, "reconciled": 1, "messaged": 1}
    [row] = _receipts()
    assert row["dedupe_key"] == f"approval-receipt:{rid}:denied"
    assert _payload(row)["requester"] == REQUESTER and _payload(row)["resolution_event_id"]
    assert len(_receipt_mail(rid, "denied")) == 1 and _intents() == []
    # Replays at +0, +1h and +1d find nothing left: still one receipt, one message.
    for later in (0, 3600, 86400):
        now = approval_receipts._now() + later
        assert approval_receipts.sweep(now=now, older_than_s=0) == {"scanned": 0}
    assert [r["ulid"] for r in _receipts()] == [row["ulid"]]
    assert len(_receipt_mail(rid, "denied")) == 1


def test_reconciled_intent_survives_a_failed_send_and_reuses_the_receipt(wired, monkeypatch):
    rid = _request("echo lost then flaky")
    _resolve_losing_the_intent(monkeypatch, rid)
    sink = Sink(fail=1)
    assert approval_receipts.sweep(notify=sink) == {"scanned": 1, "reconciled": 1, "failed": 1}
    [intent] = _intents()
    assert (intent["approval_id"], intent["decision"], intent["attempts"]) == (rid, "denied", 1)
    # The intent now exists, so the next pass retries it and does not reconcile again.
    later = approval_receipts._now() + 3600
    assert approval_receipts.sweep(now=later, notify=sink) == {"scanned": 1, "delivered": 1}
    assert len(_receipts()) == 1 and _intents() == []
    assert sink.calls[0]["receipt_id"] == sink.calls[1]["receipt_id"]


def test_reconcile_is_tenant_scoped_and_its_first_pass_is_bounded(wired, monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    rid = _request("echo other tenant")
    _resolve_losing_the_intent(monkeypatch, rid)
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    sink = Sink()
    assert approval_receipts.sweep(notify=sink) == {"scanned": 0}
    assert _intents() == [] and _receipts() == [] and sink.calls == []
    # org_b's pass stored org_b's watermark only: org_a still reconciles its own.
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    assert approval_receipts.sweep(notify=sink) == {"scanned": 1, "reconciled": 1, "delivered": 1}
    assert sink.calls[0]["approval_id"] == rid
    # A store whose first pass comes later than the back-fill bound leaves older
    # decisions as history: that pass, and every one after it, sends nothing for them.
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_c")
    old = _request("echo before receipts")
    _resolve_losing_the_intent(monkeypatch, old)
    first = approval_receipts._now() + approval_receipts.RECONCILE_FIRST_RUN_S + 120
    assert approval_receipts.sweep(now=first, notify=sink) == {"scanned": 0}
    assert approval_receipts.sweep(now=first + 3600, notify=sink) == {"scanned": 0}
    assert len(sink.calls) == 1


def test_a_decision_is_recovered_however_long_its_intent_keeps_failing(wired, monkeypatch):
    """Task #2494: the watermark waits in front of an unrepaired decision; no horizon."""
    rid = _request("echo lost for a day")
    _resolve_losing_the_intent(monkeypatch, rid)
    start = approval_receipts._now()
    sink = Sink()

    def broken(*_a, **_k):
        raise RuntimeError("receipt table unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(approval_receipts, "record_intent", broken)
        for later in (0, 3600, 86399):
            assert approval_receipts.sweep(now=start + later, notify=sink) == {"scanned": 0}
    assert _intents() == [] and _receipts() == [] and sink.calls == []
    # The failure clears one second past the old 24h horizon.
    assert approval_receipts.sweep(now=start + 86401, notify=sink) == {
        "scanned": 1,
        "reconciled": 1,
        "delivered": 1,
    }
    for later in (86402, 2 * 86400, 30 * 86400):
        assert approval_receipts.sweep(now=start + later, notify=sink, older_than_s=0) == {
            "scanned": 0
        }
    assert len(_receipts()) == 1 and [c["approval_id"] for c in sink.calls] == [rid]


def test_one_unrepairable_decision_does_not_stop_the_others(wired, monkeypatch):
    first = _request("echo stuck")
    second = _request("echo fine")
    _resolve_losing_the_intent(monkeypatch, first)
    _resolve_losing_the_intent(monkeypatch, second)
    real = approval_receipts.record_intent
    sink = Sink()

    def flaky(conn, approval_id, *args, **kwargs):
        if approval_id == first:
            raise RuntimeError("row cannot be written")
        return real(conn, approval_id, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(approval_receipts, "record_intent", flaky)
        assert approval_receipts.sweep(notify=sink) == {
            "scanned": 1,
            "reconciled": 1,
            "delivered": 1,
        }
    assert [c["approval_id"] for c in sink.calls] == [second]
    # The stuck one is still in reach a week later, and the other is not sent again.
    week = approval_receipts._now() + 7 * 86400
    assert approval_receipts.sweep(now=week, notify=sink) == {
        "scanned": 1,
        "reconciled": 1,
        "delivered": 1,
    }
    assert [c["approval_id"] for c in sink.calls] == [second, first]


def test_a_full_settled_page_moves_the_watermark_past_one_timestamp(wired, monkeypatch):
    """Task #2497: a page of settled decisions at one second must not pin the cursor.

    The watermark is the ``(decided_at, ulid)`` of the last decision a pass got
    through, so the next pass continues strictly after it. Before that, a full
    page inside the idle hour left the timestamp-only watermark where it was and
    every later pass re-read the same rows, so a decision behind them was never
    read at all.
    """
    monkeypatch.setattr(approval_receipts, "RECONCILE_SCAN", 2)
    one_second = "2026-11-11T20:00:00+00:00"
    ids = [_request(f"echo same second {i}") for i in range(3)]
    with monkeypatch.context() as patch:
        # Every decision resolves inside one second, as a busy hub's do.
        patch.setattr(storage, "now", lambda: one_second)
        for rid in ids:
            approval_resolve(rid, "deny", resolved_by=config.operator_label())
    key = f"{approval_receipts._WATERMARK_KEY}:"
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # A watermark as an earlier version wrote it: the timestamp only.
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value",
            (key, one_second),
        )
        conn.commit()
    finally:
        conn.close()
    sink = Sink()
    assert approval_receipts.sweep(notify=sink) == {"scanned": 0}
    conn = storage.connect()
    try:
        stored = approval_receipts._watermark(conn, None)
    finally:
        conn.close()
    stamp, _, ulid = (stored or "").partition(approval_receipts._CURSOR_SEP)
    assert stamp == one_second and ulid, "a full settled page leaves the watermark at its last row"
    # Everything the pass read is behind it: the third decision at the same second,
    # and a decision resolved after them, are both reached.
    assert approval_receipts.sweep(notify=sink) == {"scanned": 0}
    lost = _request("echo after the page")
    _resolve_losing_the_intent(monkeypatch, lost)
    assert approval_receipts.sweep(notify=sink) == {"scanned": 1, "reconciled": 1, "delivered": 1}
    assert [c["approval_id"] for c in sink.calls] == [lost]


def test_reconcile_follows_the_approval_row_not_a_stray_ledger_event(wired, monkeypatch):
    # A resolution-shaped event on a request nobody decided is not a decision.
    undecided = _request("echo still pending")
    events.record_event_once(
        approvals.CHANNEL_APPROVAL_RESOLVED,
        payload={"approval_id": undecided, "status": "approved"},
        originator=REQUESTER,
        dedupe_key=f"approval-resolution:{undecided}:approved",
    )
    # A real approval that lost its intent and was consumed since is one.
    used = _request("echo used pass")
    _resolve_losing_the_intent(monkeypatch, used, "approve")
    check_approval_gate("run_cmd", {"command": "echo used pass"}, requested_by=REQUESTER)
    assert approvals.get_approval(used)["status"] == "consumed"
    sink = Sink()
    assert approval_receipts.sweep(notify=sink) == {"scanned": 1, "reconciled": 1, "delivered": 1}
    assert [(c["approval_id"], c["decision"]) for c in sink.calls] == [(used, "approved")]
    assert approvals.get_approval(undecided)["status"] == "pending" and _intents() == []
