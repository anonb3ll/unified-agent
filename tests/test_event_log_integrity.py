"""event_log integrity: no hashed row is ever deleted, commit order is cursor order.

Two defects found in the desktop-app design review (task #1640):

1. ``slack_outbound`` reserved its send slot as a *hashed* ``event_log`` row and,
   when the Slack POST failed, freed the dedupe key with a plain ``DELETE``. Any
   event appended in between kept a ``prev_hash`` pointing at a row that no longer
   existed, and ``verify_event_chain`` reported the ledger as tampered.
2. ULIDs were minted outside the write transaction (and are only monotonic per
   process), so a row could commit *after* a row carrying a newer ULID. A
   ``ulid > since`` consumer whose cursor had already passed the late row skipped
   it for good. ``seq`` (the ROWID, i.e. commit order) is the gap-free cursor.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys

import anyio
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, events, server, slack_outbound, storage, webui
from unified_agent.webui import routes, sse


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.delenv("UA_EDITION", raising=False)
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    monkeypatch.setenv("UA_UI_STREAM_POLL_S", "0.02")
    storage.init_db()
    yield


def _slack_config():
    from unified_agent.slack_gateway import FrontierPolicy, SlackConfig, SlackStatusLookupConfig

    return SlackConfig(
        signing_secret="signing",
        bot_token="xoxb-test",
        app_token=None,
        channel_ids=frozenset({"C_REQUESTS"}),
        channel_names_by_id={"C_REQUESTS": "requests"},
        users=frozenset({"U_OPERATOR"}),
        coordinator_agent="Desktop_Agent5",
        enabled=True,
        frontier=FrontierPolicy(
            models=frozenset({"frontier-test"}),
            provider="openai",
            credential_env="OPENAI_API_KEY",
            max_budget_usd=2.0,
            timeout_s=30,
            max_prompt_chars=1000,
            max_output_tokens=100,
            kill_switch=False,
            credential_present=True,
        ),
        status_lookup=SlackStatusLookupConfig(enabled=False, identity_map={}, error=None),
    )


def _raw_insert(ulid: str, channel: str = "agent.foreign.updated", payload: str = "{}") -> None:
    """A row committed by another writer (another process, or pre-fix code).

    Unhashed so it does not disturb the chain; what matters here is only *when*
    it commits relative to its ULID.
    """
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO event_log (ulid, channel, payload, created_at) VALUES (?, ?, ?, ?)",
            (ulid, channel, payload, storage.now()),
        )
        conn.commit()
    finally:
        conn.close()


def _rows_by_rowid() -> list:
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT ROWID AS seq, ulid, channel FROM event_log ORDER BY ROWID ASC"
        ).fetchall()
    finally:
        conn.close()


def _future_ulid(ms_ahead: int = 60_000) -> str:
    """A ULID another process with a faster clock (or higher same-ms suffix) minted."""
    return events._encode(events._now_ms() + ms_ahead, events._TIME_LEN) + events._encode(
        0, events._RAND_LEN
    )


# --- defect 1: the Slack reservation must never delete a hashed row ------------


def test_failed_slack_send_leaves_the_chain_valid():
    """Reserve, append an unrelated event, fail the send: the chain must verify."""

    def post_after_unrelated_event(url, data, headers):
        # Lands between the reservation and its release, exactly the window in
        # which the old DELETE orphaned this row's prev_hash.
        events.record_event("agent.task.updated", payload={"between": True})
        raise OSError("slack down")

    result = slack_outbound.send_message(
        channel_id="C_REQUESTS",
        text="ack",
        agent_label="Desktop_Agent5",
        event_ts="9.1",
        dedupe_key="chain-key",
        config=_slack_config(),
        post_fn=post_after_unrelated_event,
    )
    assert result["reason"] == "transport"

    report = events.verify_event_chain()
    assert report["status"] == "valid", report
    assert report["verified"] is True


def test_failed_send_frees_the_key_and_success_is_recorded_once():
    def boom(url, data, headers):
        raise OSError("down")

    calls = []

    def ok(url, data, headers):
        calls.append(data)
        return {"ok": True, "ts": "1.2"}

    kwargs = dict(
        channel_id="C_REQUESTS",
        text="retry-me",
        agent_label="Desktop_Agent5",
        event_ts="3.1",
        dedupe_key="retry-chain",
        config=_slack_config(),
    )
    assert slack_outbound.send_message(post_fn=boom, **kwargs)["reason"] == "transport"
    events.record_event("agent.task.updated", payload={"unrelated": 1})
    sent = slack_outbound.send_message(post_fn=ok, **kwargs)
    assert sent["sent"] is True
    dup = slack_outbound.send_message(post_fn=ok, **kwargs)
    assert dup["duplicate"] is True
    assert dup["event_ulid"] == sent["event_ulid"]
    assert len(calls) == 1

    outbound = events.query_events(channels=[slack_outbound.CHANNEL_OUTBOUND])
    assert [e["ulid"] for e in outbound] == [sent["event_ulid"]]
    assert outbound[0]["dedupe_key"] == "retry-chain"
    assert events.verify_event_chain()["status"] == "valid"


def test_in_flight_reservation_counts_against_the_hourly_cap(monkeypatch):
    monkeypatch.setenv("UA_SLACK_MAX_PER_HOUR", "1")
    nested: list[dict] = []

    def post_while_another_send_tries(url, data, headers):
        nested.append(
            slack_outbound.send_message(
                channel_id="C_REQUESTS",
                text="second",
                agent_label="Desktop_Agent5",
                event_ts="4.2",
                dedupe_key="cap-2",
                config=_slack_config(),
                post_fn=lambda *a: {"ok": True, "ts": "4.3"},
            )
        )
        return {"ok": True, "ts": "4.1"}

    first = slack_outbound.send_message(
        channel_id="C_REQUESTS",
        text="first",
        agent_label="Desktop_Agent5",
        event_ts="4.1",
        dedupe_key="cap-1",
        config=_slack_config(),
        post_fn=post_while_another_send_tries,
    )
    assert first["sent"] is True
    assert nested[0]["reason"] == "rate-limited"


def test_legacy_ledger_reservation_still_dedupes_and_counts(monkeypatch):
    """A key already sent through the pre-fix path (a slack.outbound ledger row)."""
    events.record_event(
        slack_outbound.CHANNEL_OUTBOUND,
        payload={"channel_id": "C_REQUESTS"},
        dedupe_key="legacy-key",
        notify=False,
    )
    calls = []

    def ok(url, data, headers):
        calls.append(data)
        return {"ok": True, "ts": "1.2"}

    dup = slack_outbound.send_message(
        channel_id="C_REQUESTS",
        text="again",
        agent_label="Desktop_Agent5",
        event_ts="5.1",
        dedupe_key="legacy-key",
        config=_slack_config(),
        post_fn=ok,
    )
    assert dup["duplicate"] is True
    assert calls == []

    monkeypatch.setenv("UA_SLACK_MAX_PER_HOUR", "1")
    limited = slack_outbound.send_message(
        channel_id="C_REQUESTS",
        text="new",
        agent_label="Desktop_Agent5",
        event_ts="5.2",
        dedupe_key="new-key",
        config=_slack_config(),
        post_fn=ok,
    )
    assert limited["reason"] == "rate-limited"


def test_events_module_exposes_no_hashed_row_delete():
    assert not hasattr(events, "delete_event_by_dedupe_key")


def test_only_events_py_inserts_into_event_log():
    """Every append goes through the one writer that mints its ULID in-txn."""
    import pathlib
    import re

    src = pathlib.Path(events.__file__).parent
    # \b: event_log_maintenance is a different table.
    insert = re.compile(r"INSERT INTO event_log\b")
    offenders = [
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if p.name != "events.py" and insert.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# --- defect 2: commit order vs ULID order ----------------------------------------


def test_ulid_minted_before_the_write_lock_cannot_commit_behind_a_newer_one(monkeypatch):
    """Mint a ULID, let another event commit, then commit the first.

    The old slack path minted before ``BEGIN IMMEDIATE``; this reproduces that
    interleaving by committing another event the moment slack_outbound mints.
    After the fix slack_outbound never mints its own ULID, so ULID order and
    commit (ROWID) order agree.
    """
    real = events.new_ulid
    fired = []

    def interleaving_new_ulid(*a, **k):
        value = real(*a, **k)
        caller = sys._getframe(1).f_globals.get("__name__")
        if caller == "unified_agent.slack_outbound" and not fired:
            fired.append(True)
            events.record_event("agent.task.updated", payload={"interleaved": True})
        return value

    monkeypatch.setattr(events, "new_ulid", interleaving_new_ulid)
    slack_outbound.send_message(
        channel_id="C_REQUESTS",
        text="ack",
        agent_label="Desktop_Agent5",
        event_ts="6.1",
        dedupe_key="order-key",
        config=_slack_config(),
        post_fn=lambda *a: {"ok": True, "ts": "6.2"},
    )
    rows = _rows_by_rowid()
    ulids = [r["ulid"] for r in rows]
    assert ulids == sorted(ulids), "a row committed behind a newer ULID"


def test_append_mints_above_a_newer_ulid_committed_by_another_process():
    """Per-process monotonicity is not enough: another writer may hold a higher ULID."""
    foreign = _future_ulid()
    _raw_insert(foreign)
    mine = events.record_event("agent.task.updated", payload={"n": 1})
    assert mine > foreign
    after = events.record_event("agent.task.updated", payload={"n": 2})
    assert after > mine
    assert events.verify_event_chain()["status"] == "valid"


def test_since_cursor_misses_a_late_commit_but_after_seq_does_not():
    early = events.new_ulid()  # minted first ...
    newer = events.record_event("agent.task.updated", payload={"n": "newer"})
    seen = events.query_events(since=None, limit=100)
    since_cursor = seen[-1]["ulid"]
    seq_cursor = seen[-1]["seq"]
    assert since_cursor == newer
    _raw_insert(early, payload=json.dumps({"n": "late"}))  # ... committed last

    # The ULID cursor has already passed `early`: this is the skip.
    assert events.query_events(since=since_cursor, limit=100) == []

    resumed = events.query_events(after_seq=seq_cursor, limit=100)
    assert [e["ulid"] for e in resumed] == [early]
    assert resumed[0]["seq"] > seq_cursor


def test_rows_carry_a_strictly_increasing_seq():
    ulids = [events.record_event("agent.task.updated", payload={"i": i}) for i in range(3)]
    rows = events.query_events(limit=10)
    assert [r["ulid"] for r in rows] == ulids
    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3


def test_full_prune_never_rewinds_seq():
    """A ROWID cursor is only gap-free if the max ROWID is never freed for reuse."""
    import contextlib
    import datetime

    @contextlib.contextmanager
    def frozen(days_ago):
        stamp = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days_ago)).isoformat(
            timespec="seconds"
        )
        original = storage.now
        storage.now = lambda: stamp
        try:
            yield
        finally:
            storage.now = original

    with frozen(90):
        for i in range(3):
            events.record_event("agent.task.updated", payload={"i": i})
    high_water = max(r["seq"] for r in _rows_by_rowid())
    events.prune_events(older_than_days=30)
    pruned = events.query_events(channels=["agent.audit.pruned"])
    assert pruned and pruned[0]["seq"] > high_water
    assert events.verify_event_chain()["status"] == "valid"


# --- SSE ----------------------------------------------------------------------------


async def _next(gen, timeout=5.0):
    with anyio.fail_after(timeout):
        return await gen.__anext__()


def test_sse_frames_carry_seq_and_deliver_a_late_commit():
    events.record_event("agent.task.updated", payload={"n": "history"})

    async def scenario():
        gen = sse.event_stream()
        try:
            opening = await _next(gen)
            assert opening["event"] == "cursor"
            assert isinstance(json.loads(opening["data"])["seq"], int)

            early = events.new_ulid()
            await anyio.to_thread.run_sync(
                lambda: events.record_event("agent.task.updated", payload={"n": "newer"})
            )
            newer = json.loads((await _next(gen))["data"])
            assert newer["payload"] == {"n": "newer"}
            assert isinstance(newer["seq"], int)

            await anyio.to_thread.run_sync(
                lambda: _raw_insert(early, channel="agent.task.updated", payload='{"n": "late"}')
            )
            late = json.loads((await _next(gen))["data"])
            assert late["ulid"] == early
            assert late["seq"] > newer["seq"]
        finally:
            await gen.aclose()

    anyio.run(scenario)


def test_sse_after_seq_resumes_exclusively():
    events.record_event("agent.task.updated", payload={"n": 1})
    second = events.record_event("agent.task.updated", payload={"n": 2})
    seq_of_first = events.query_events(limit=10)[0]["seq"]
    del second

    async def scenario():
        gen = sse.event_stream(after_seq=seq_of_first)
        try:
            opening = await _next(gen)
            assert json.loads(opening["data"])["seq"] == seq_of_first
            frame = json.loads((await _next(gen))["data"])
            assert frame["payload"] == {"n": 2}
        finally:
            await gen.aclose()

    anyio.run(scenario)


# --- GET /api/ledger/rows -------------------------------------------------------------


def _client():
    return TestClient(
        server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes())),
        base_url="http://127.0.0.1",
    )


def test_ledger_rows_payload_raw_rehashes_to_entry_hash(monkeypatch):
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    events.record_event("agent.task.updated", payload={"b": 2, "a": [1, "x"]})
    events.record_event("agent.approval.requested", payload={"tool": "run_cmd"}, dedupe_key="k1")
    _raw_insert(events.new_ulid())  # unhashed core-edition row: listed, not chained

    with _client() as client:
        response = client.get("/api/ledger/rows?after_seq=0&limit=10")
    assert response.status_code == 200, response.text
    body = response.json()
    rows = body["rows"]
    assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows)
    assert body["next_after_seq"] == rows[-1]["seq"]

    prev = events.GENESIS_HASH
    hashed = [r for r in rows if r["entry_hash"] is not None]
    assert len(hashed) == 2
    for row in hashed:
        assert isinstance(row["payload_raw"], str)
        assert "payload" not in row
        assert row["prev_hash"] == prev
        recomputed = events.compute_entry_hash(
            row["prev_hash"],
            row["ulid"],
            row["channel"],
            row["payload_raw"],
            row["originator"],
            row["episode_id"],
            row["causal_parent_id"],
            row["created_at"],
            row["dedupe_key"],
        )
        assert recomputed == row["entry_hash"]
        prev = row["entry_hash"]

    with _client() as client:
        tail = client.get(f"/api/ledger/rows?after_seq={rows[0]['seq']}&limit=1").json()
    assert [r["seq"] for r in tail["rows"]] == [rows[1]["seq"]]


def test_ledger_rows_requires_auth_under_enforce(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:tok")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    with _client() as client:
        assert client.get("/api/ledger/rows").status_code == 401


def test_ledger_rows_refused_for_a_profile_without_event_log_query(monkeypatch):
    from unified_agent import profiles

    identity = "server_agent8"
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{identity}:fixture-token")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    assert not profiles.tool_is_allowed_for(
        profiles.default_for_identity(identity), "event_log_query"
    )
    events.record_event("agent.approval.requested", payload={"private": "fixture-private"})
    with _client() as client:
        response = client.get("/api/ledger/rows", headers={"Authorization": "Bearer fixture-token"})
    assert response.status_code == 403
    assert "fixture-private" not in response.text


# --- approvals: a failed producer stamp is a visible evidence gap ------------------------


def test_failed_producer_stamp_records_an_evidence_gap(monkeypatch, caplog):
    from unified_agent.audit import producer_sign
    from unified_agent.tools.approvals import (
        ApprovalPendingError,
        approval_pending,
        approval_resolve,
        check_approval_gate,
    )

    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    with pytest.raises(ApprovalPendingError):
        check_approval_gate("run_cmd", {"command": "echo gap"})
    rid = approval_pending()["pending"][0]["id"]

    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desktop_Person")

    def exploding_stamp(**kwargs):
        raise RuntimeError("signer offline")

    monkeypatch.setattr(producer_sign, "stamp_authenticated_producer_event", exploding_stamp)
    with caplog.at_level(logging.WARNING, logger="unified_agent.tools.approvals"):
        row = approval_resolve(rid, "deny", reason="no")
    assert row["status"] == "denied"  # the approval itself never fails

    gaps = events.query_events(channels=["audit.evidence.gap"])
    assert len(gaps) == 1
    payload = gaps[0]["payload"]
    assert payload["kind"] == "approval"
    assert payload["ref_id"] == str(rid)
    assert payload["error_type"] == "RuntimeError"
    assert any("evidence gap" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "error",
    [
        storage.LiveStoreError("memory.db is unusable: disk I/O error"),
        sqlite3.OperationalError("database is locked"),
    ],
)
def test_ledger_failure_after_delivery_reports_sent_and_keeps_the_slot(monkeypatch, error):
    """Slack already accepted the message: never raise (a caller would resend)."""

    def failing_record_sent(**kwargs):
        raise error

    monkeypatch.setattr(slack_outbound, "_record_sent", failing_record_sent)
    calls = []

    def ok(url, data, headers):
        calls.append(data)
        return {"ok": True, "ts": "7.2"}

    kwargs = dict(
        channel_id="C_REQUESTS",
        text="delivered",
        agent_label="Desktop_Agent5",
        event_ts="7.1",
        dedupe_key="delivered-key",
        config=_slack_config(),
        post_fn=ok,
    )
    result = slack_outbound.send_message(**kwargs)
    assert result["sent"] is True
    assert result["event_ulid"] is None
    assert slack_outbound.send_message(**kwargs)["duplicate"] is True
    assert len(calls) == 1


# --- review follow-ups (Devin on #924) ----------------------------------------------


def test_fresh_stream_does_not_skip_an_event_committed_during_the_opening_read(monkeypatch):
    """The opening ULID and seq must come from one snapshot.

    Simulates a commit landing between two separate opening reads: whichever
    tail reader runs first appends an event. That event must still be streamed.
    """
    events.record_event("agent.task.updated", payload={"n": "history"})
    fired = []

    def racing(reader):
        def wrapped(*a, **k):
            value = reader(*a, **k)
            if not fired:
                fired.append(True)
                events.record_event("agent.task.updated", payload={"n": "racer"})
            return value

        return wrapped

    # The pre-fix opening read: latest_ulid, then a separate MAX(ROWID) read.
    monkeypatch.setattr(events, "latest_ulid", racing(events.latest_ulid))

    async def scenario():
        gen = sse.event_stream()
        try:
            opening = json.loads((await _next(gen))["data"])
            if opening["cursor"] is not None:
                row = next(
                    e for e in events.query_events(limit=100) if e["ulid"] == opening["cursor"]
                )
                assert row["seq"] == opening["seq"], "opening cursor mixes two snapshots"
            await anyio.to_thread.run_sync(
                lambda: events.record_event("agent.task.updated", payload={"n": "after"})
            )
            seen = []
            while "after" not in seen:
                seen.append(json.loads((await _next(gen))["data"])["payload"]["n"])
            if fired:
                assert "racer" in seen
        finally:
            await gen.aclose()

    anyio.run(scenario)


@pytest.mark.parametrize("path", ["/api/events", "/api/events/stream"])
def test_http_rejects_both_cursors(monkeypatch, path):
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    first = events.record_event("agent.task.updated", payload={"n": 1})
    with _client() as client:
        response = client.get(f"{path}?since={first}&after_seq=0")
    assert response.status_code == 400
    assert "since" in response.json()["error"]


def _raw_ulid(ms: int) -> str:
    return events._encode(ms, events._TIME_LEN) + events._encode(0, events._RAND_LEN)


def test_ulid_cursor_translation_never_skips_reordered_history():
    """Pre-#1640 rows can commit out of ULID order. A ``since`` client that
    stopped at C (limit-truncated) has not seen D, which has a higher ULID but
    committed first. Translating C to its own seq would skip D for good."""
    base = events._now_ms() - 10_000
    d = _raw_ulid(base + 9)
    c = _raw_ulid(base + 5)
    _raw_insert(d)  # higher ULID, committed first
    _raw_insert(c)  # lower ULID, committed later
    start = events.seq_for_ulid_cursor(c)
    delivered = [e["ulid"] for e in events.query_events(after_seq=start, limit=10)]
    assert d in delivered


def test_pruned_ulid_cursor_translation_never_skips_reordered_history():
    base = events._now_ms() - 10_000
    gone = _raw_ulid(base + 4)  # the client's cursor row, since pruned
    d = _raw_ulid(base + 9)
    late = _raw_ulid(base + 6)
    _raw_insert(d)
    _raw_insert(late)
    start = events.seq_for_ulid_cursor(gone)
    delivered = {e["ulid"] for e in events.query_events(after_seq=start, limit=10)}
    assert {d, late} <= delivered


def test_ulid_cursor_translation_is_exact_for_commit_ordered_rows():
    """Rows written since #1640 agree on both orders: no redelivery."""
    first = events.record_event("agent.task.updated", payload={"n": 1})
    second = events.record_event("agent.task.updated", payload={"n": 2})
    start = events.seq_for_ulid_cursor(first)
    assert [e["ulid"] for e in events.query_events(after_seq=start, limit=10)] == [second]


def test_abandoned_send_slot_is_visible_and_releasable_with_an_audit_event():
    """A process killed mid-POST leaves a slot with no ledger row. It stays a
    duplicate (at-most-once: Slack may have delivered it), is reported as in
    flight, and an operator can list and release it; the release is ledgered."""
    kwargs = dict(
        channel_id="C_REQUESTS",
        text="maybe-sent",
        agent_label="Desktop_Agent5",
        event_ts="8.1",
        dedupe_key="abandoned-key",
        config=_slack_config(),
    )
    status = slack_outbound._reserve_outbound(
        dedupe_key="abandoned-key", cap=30, cutoff="1970-01-01T00:00:00+00:00"
    )
    assert status == (None, "ok")  # ... and then the process died

    calls = []

    def ok(url, data, headers):
        calls.append(data)
        return {"ok": True, "ts": "8.2"}

    dup = slack_outbound.send_message(post_fn=ok, **kwargs)
    assert dup["duplicate"] is True and dup["in_flight"] is True
    assert calls == []

    assert slack_outbound.unconfirmed_slots(older_than_s=3600) == []
    stale = slack_outbound.unconfirmed_slots(older_than_s=0)
    assert [s["dedupe_key"] for s in stale] == ["abandoned-key"]

    # Too young to be abandoned: it may still be mid-POST, so it is not released.
    assert not slack_outbound.release_unconfirmed_slot("abandoned-key", released_by="x")
    assert slack_outbound.release_unconfirmed_slot(
        "abandoned-key", released_by="Desktop_Person", min_age_s=0
    )
    released = events.query_events(channels=[slack_outbound.CHANNEL_OUTBOUND_RELEASED])
    assert released[0]["payload"]["dedupe_key"] == "abandoned-key"
    assert released[0]["originator"] == "Desktop_Person"

    assert slack_outbound.send_message(post_fn=ok, **kwargs)["sent"] is True
    assert len(calls) == 1
    # A delivered (ledgered) send is never releasable.
    assert not slack_outbound.release_unconfirmed_slot(
        "abandoned-key", released_by="x", min_age_s=0
    )
    assert events.verify_event_chain()["status"] == "valid"


def test_active_send_slot_cannot_be_released_mid_post():
    """Devin on #924: an in-progress send looks like a crashed one in the slot
    table. Releasing it would let a retry post the same key twice."""
    released: list[bool] = []

    def post_while_operator_releases(url, data, headers):
        released.append(
            slack_outbound.release_unconfirmed_slot("busy-key", released_by="Desktop_Person")
        )
        return {"ok": True, "ts": "9.2"}

    result = slack_outbound.send_message(
        channel_id="C_REQUESTS",
        text="busy",
        agent_label="Desktop_Agent5",
        event_ts="9.1",
        dedupe_key="busy-key",
        config=_slack_config(),
        post_fn=post_while_operator_releases,
    )
    assert released == [False]
    assert result["sent"] is True
    assert events.query_events(channels=[slack_outbound.CHANNEL_OUTBOUND_RELEASED]) == []


def test_slot_cli_lists_and_releases(capsys):
    slack_outbound._reserve_outbound(
        dedupe_key="cli-key", cap=30, cutoff="1970-01-01T00:00:00+00:00"
    )
    assert slack_outbound.slots_main(["list", "--older-than-s", "0"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [s["dedupe_key"] for s in listed["unconfirmed"]] == ["cli-key"]

    # Default age guard refuses a slot that may still be sending.
    assert slack_outbound.slots_main(["release", "cli-key", "--by", "Desktop_Person"]) == 1
    assert json.loads(capsys.readouterr().out)["released"] is False

    assert (
        slack_outbound.slots_main(
            ["release", "cli-key", "--by", "Desktop_Person", "--min-age-s", "0"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["released"] is True
    with pytest.raises(SystemExit):
        slack_outbound.slots_main(["release", "cli-key"])  # --by is required


# Task #1683: a malformed after_seq used to reach int() and return HTTP 500
# (superscript digits pass str.isdigit(); 2**63 overflows SQLite INTEGER).
BAD_SEQS = ["²", "١٢", "-1", "abc", "1.5", str(2**63), "9" * 40]


@pytest.mark.parametrize("path", ["/api/events", "/api/ledger/rows", "/api/events/stream"])
@pytest.mark.parametrize("raw", BAD_SEQS)
def test_http_rejects_malformed_after_seq_with_400(monkeypatch, path, raw):
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    events.record_event("agent.task.updated", payload={"n": 1})
    with _client() as client:
        response = client.get(path, params={"after_seq": raw})
    assert response.status_code == 400
    assert "after_seq" in response.json()["error"]


@pytest.mark.parametrize("raw", ["", " ", "0", " 7 ", str(2**63 - 1), "0" * 25 + "7"])
def test_ledger_rows_accepts_absent_and_in_range_after_seq(monkeypatch, raw):
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    events.record_event("agent.task.updated", payload={"n": 1})
    with _client() as client:
        response = client.get("/api/ledger/rows", params={"after_seq": raw, "limit": 5})
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/api/events", "/api/events/stream"])
def test_whitespace_after_seq_does_not_block_a_since_resume(monkeypatch, path):
    """Devin on #948: since=X&after_seq=%20 was a both-cursors 400."""
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    first = events.record_event("agent.task.updated", payload={"n": 1})
    if path.endswith("stream"):
        assert not routes._both_cursors({"since": first, "after_seq": " "})
        return
    with _client() as client:
        response = client.get(path, params={"since": first, "after_seq": " "})
    assert response.status_code == 200
