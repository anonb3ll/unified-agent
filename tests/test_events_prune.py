"""Tests for task #963: events.prune_events() must not corrupt the hash chain.

Before this fix, prune_events() re-rooted the surviving chain by rewriting the
new oldest row's prev_hash to GENESIS without recomputing entry_hash — but
compute_entry_hash() includes prev_hash in the hashed fields, so the stored
entry_hash immediately stopped matching. verify_event_chain() reported
"tampered" on the one operation legitimately allowed to mutate the ledger.
These tests pin: a legitimate prune leaves the chain "valid", 0/negative
retention is a no-op, an empty-effect prune is a no-op, real tampering after a
prune is still caught, and unhashed (UA_EDITION=core) rows interact correctly
with the re-root.

Events are backdated by freezing ``storage.now()`` at record time rather than
UPDATE-ing ``created_at`` after the fact: ``created_at`` is one of the hashed
fields (see ``compute_entry_hash``), so mutating it directly on an
already-hashed row would corrupt that row's own entry_hash independently of
anything prune_events does — a test artifact, not something a real "this row
is genuinely old" state ever produces (a real old row was hashed with its true
timestamp at insert time).
"""

from __future__ import annotations

import contextlib
import datetime
import sqlite3
import threading

import pytest

from unified_agent import config, events, storage


@pytest.fixture(autouse=True)
def init_clean_db(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()
    yield


@contextlib.contextmanager
def _frozen_now(days_ago: float):
    """Make storage.now() (and thus record_event's created_at) report a past time."""
    stamp = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days_ago)).isoformat(
        timespec="seconds"
    )
    original = storage.now
    storage.now = lambda: stamp
    try:
        yield stamp
    finally:
        storage.now = original


def test_prune_then_verify_is_valid():
    """The regression this fixes: a legitimate prune must not self-report as tampered."""
    with _frozen_now(90):
        old_ulids = [events.record_event("agent.test.updated", payload={"i": i}) for i in range(2)]
    recent_ulids = [
        events.record_event("agent.test.updated", payload={"i": i}) for i in range(2, 4)
    ]
    assert events.verify_event_chain()["status"] == "valid"

    removed = events.prune_events(older_than_days=30)
    assert removed == len(old_ulids)

    report = events.verify_event_chain()
    assert report["status"] == "valid"
    assert report["verified"] is True
    # 2 surviving originals + the audit-trail event this prune records.
    assert report["total_verified"] == len(recent_ulids) + 1


def test_zero_or_negative_retention_keeps_everything():
    with _frozen_now(9999):
        ulids = [events.record_event("agent.test.updated", payload={"i": i}) for i in range(3)]

    assert events.prune_events(older_than_days=0) == 0
    assert events.prune_events(older_than_days=-5) == 0

    conn = storage.connect()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM event_log").fetchone()["n"]
    finally:
        conn.close()
    assert count == len(ulids)
    assert events.query_events(channels=["agent.audit.pruned"]) == []
    assert events.verify_event_chain()["status"] == "valid"


def test_nothing_old_enough_is_a_no_op():
    for i in range(3):
        events.record_event("agent.test.updated", payload={"i": i})
    before = events.verify_event_chain()

    removed = events.prune_events(older_than_days=30)
    assert removed == 0

    after = events.verify_event_chain()
    assert after["status"] == "valid"
    assert after == before
    assert events.query_events(channels=["agent.audit.pruned"]) == []


def test_real_tampering_after_a_prune_is_still_detected():
    """Proves the fix doesn't just disable the check: a genuinely mutated row
    after a legitimate prune must still surface as "tampered"."""
    with _frozen_now(90):
        old_ulids = [events.record_event("agent.test.updated", payload={"i": i}) for i in range(2)]
    survivor_ulids = [
        events.record_event("agent.test.updated", payload={"i": i}) for i in range(2, 4)
    ]
    events.prune_events(older_than_days=30)
    assert events.verify_event_chain()["status"] == "valid"

    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute(
                "UPDATE event_log SET payload=? WHERE ulid=?",
                ('{"tampered": true}', survivor_ulids[-1]),
            )
        conn.commit()
    finally:
        conn.close()

    report = events.verify_event_chain()
    assert report["status"] == "tampered"
    assert report["verified"] is False
    del old_ulids  # only used to establish the pruned prefix


def test_unhashed_core_rows_interact_correctly_with_reroot(monkeypatch):
    """UA_EDITION=core writes entry_hash=NULL rows. The re-root selects
    MIN(ROWID) among hashed rows only, so an unhashed row must not become the
    (impossible) new root, and the hashed suffix must still verify."""
    monkeypatch.delenv("UA_EDITION", raising=False)
    with _frozen_now(90):
        events.record_event("agent.e1", payload={"n": 1})
        monkeypatch.setenv("UA_EDITION", "core")
        events.record_event("agent.core1", payload={"n": 2})
    monkeypatch.setenv("UA_EDITION", "enterprise")
    events.record_event("agent.e2", payload={"n": 3})

    assert events.verify_event_chain()["status"] == "tampered"
    with pytest.raises(ValueError, match="tampered"):
        events.prune_events(older_than_days=30)
    assert events.verify_event_chain()["status"] == "tampered"


def test_prune_records_an_auditable_event_for_the_gap():
    """The prune itself is recorded on the log so a downstream consumer can see
    the gap instead of it silently vanishing (docstring's own stated principle)."""
    with _frozen_now(90):
        old_ulid = events.record_event("agent.test.updated", payload={"i": 0})
    for i in range(1, 3):
        events.record_event("agent.test.updated", payload={"i": i})

    removed = events.prune_events(older_than_days=30)
    assert removed == 1
    del old_ulid

    audit_events = events.query_events(channels=["agent.audit.pruned"])
    assert len(audit_events) == 1
    payload = audit_events[0]["payload"]
    assert payload["rows_pruned"] == 1
    assert payload["retention_days"] == 30

    # And the audit event's own hash extends the now-valid chain.
    assert events.verify_event_chain()["status"] == "valid"


def test_prune_with_nothing_removed_records_no_audit_event():
    for i in range(2):
        events.record_event("agent.test.updated", payload={"i": i})
    assert events.prune_events(older_than_days=30) == 0
    assert events.query_events(channels=["agent.audit.pruned"]) == []


def _ledger_rows():
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT ROWID, ulid, channel, payload, originator, episode_id, "
            "causal_parent_id, created_at, dedupe_key, prev_hash, entry_hash "
            "FROM event_log ORDER BY ROWID ASC"
        ).fetchall()
        return [tuple(row) for row in rows]
    finally:
        conn.close()


def test_tampered_survivor_aborts_prune_without_changing_ledger():
    with _frozen_now(90):
        events.record_event("agent.test.updated", payload={"old": True})
    survivor = events.record_event("agent.test.updated", payload={"survivor": True})

    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute(
                "UPDATE event_log SET payload=? WHERE ulid=?",
                ('{"tampered": true}', survivor),
            )
        conn.commit()
    finally:
        conn.close()
    before = _ledger_rows()

    with pytest.raises(ValueError, match="tampered event_log row"):
        events.prune_events(older_than_days=30)

    assert _ledger_rows() == before
    assert events.query_events(limit=100)[0]["payload"] == {"old": True}


def test_rebuild_failure_rolls_back_delete(monkeypatch):
    with _frozen_now(90):
        old = events.record_event("agent.test.updated", payload={"old": True})
    survivor = events.record_event("agent.test.updated", payload={"survivor": True})
    before = _ledger_rows()
    original_compute = events.compute_entry_hash

    def fail_when_rebuilding(prev_hash, *args):
        if prev_hash == events.GENESIS_HASH:
            raise RuntimeError("injected rebuild failure")
        return original_compute(prev_hash, *args)

    monkeypatch.setattr(events, "compute_entry_hash", fail_when_rebuilding)
    with pytest.raises(RuntimeError, match="injected rebuild failure"):
        events.prune_events(older_than_days=30)

    assert _ledger_rows() == before
    assert {row[1] for row in _ledger_rows()} == {old, survivor}


def test_concurrent_record_event_waits_for_prune_transaction(monkeypatch):
    with _frozen_now(90):
        events.record_event("agent.test.updated", payload={"old": True})
    events.record_event("agent.test.updated", payload={"survivor": True})
    original_compute = events.compute_entry_hash
    writer_started = threading.Event()
    writer_done = threading.Event()
    writer = None
    writer_finished_while_pruning = False

    def append_concurrently():
        writer_started.set()
        events.record_event("agent.concurrent", payload={"writer": True})
        writer_done.set()

    def observe_rebuild(prev_hash, *args):
        nonlocal writer, writer_finished_while_pruning
        if prev_hash == events.GENESIS_HASH and writer is None:
            writer = threading.Thread(target=append_concurrently)
            writer.start()
            assert writer_started.wait(timeout=2)
            writer_finished_while_pruning = writer_done.wait(timeout=1)
        return original_compute(prev_hash, *args)

    monkeypatch.setattr(events, "compute_entry_hash", observe_rebuild)
    events.prune_events(older_than_days=30)
    assert writer is not None
    writer.join(timeout=2)
    assert not writer.is_alive()
    assert writer_finished_while_pruning is False
    assert events.verify_event_chain()["status"] == "valid"


def test_audit_insert_failure_rolls_back_prune(monkeypatch):
    with _frozen_now(90):
        old = events.record_event("agent.test.updated", payload={"old": True})
    survivor = events.record_event("agent.test.updated", payload={"survivor": True})
    before = _ledger_rows()

    def fail_audit_insert(*args, **kwargs):
        raise RuntimeError("injected audit insert failure")

    monkeypatch.setattr(events, "record_event_once_on_connection", fail_audit_insert)
    with pytest.raises(RuntimeError, match="injected audit insert failure"):
        events.prune_events(older_than_days=30)

    assert _ledger_rows() == before
    assert {row[1] for row in _ledger_rows()} == {old, survivor}
    _assert_no_bypass_row()


def _assert_no_bypass_row():
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM event_log_maintenance").fetchone()[0] == 0
    finally:
        conn.close()


def test_prune_works_through_append_only_triggers():
    """Task #1908: event_log refuses raw UPDATE/DELETE and prune_events is the
    sanctioned path. It must still delete and re-root, and it must leave no
    bypass row behind for a later raw statement to ride on."""
    with _frozen_now(90):
        events.record_event("agent.test.updated", payload={"i": 0})
    keep = events.record_event("agent.test.updated", payload={"i": 1})

    assert events.prune_events(older_than_days=30) == 1
    assert events.verify_event_chain()["status"] == "valid"
    _assert_no_bypass_row()

    conn = storage.connect()
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM event_log WHERE ulid=?", (keep,))
        conn.rollback()
    finally:
        conn.close()


def test_pruning_cannot_turn_a_retained_gap_into_a_legacy_prefix(monkeypatch):
    with _frozen_now(90):
        events.record_event("agent.old", payload={})
    monkeypatch.setenv("UA_EDITION", "core")
    gap = events.record_event("agent.gap", payload={})
    monkeypatch.setenv("UA_EDITION", "enterprise")
    events.record_event("agent.new", payload={})
    before = _ledger_rows()
    with pytest.raises(ValueError, match="tampered"):
        events.prune_events(older_than_days=30)
    assert _ledger_rows() == before
    report = events.verify_event_chain()
    assert report["tampered_at"]["ulid"] == gap


def test_prune_keeps_proof_channels_and_chain_stays_valid():
    """Receipt and consumption proofs survive retention pruning (#1973 review).

    They sit between pruned rows, so the surviving chain has holes rather than
    a missing prefix; the prune must still re-root it into a valid chain.
    """
    with _frozen_now(90):
        events.record_event("agent.test.updated", payload={"i": 0})
        kept = [
            events.record_event(channel, payload={"proof": channel})
            for channel in sorted(events.PRUNE_PROTECTED_CHANNELS)
        ]
        events.record_event("agent.test.updated", payload={"i": 1})
        events.record_event("agent.test.updated", payload={"i": 2})
    events.record_event("agent.test.updated", payload={"i": 3})

    assert events.prune_events(older_than_days=30) == 3
    conn = storage.connect()
    try:
        survivors = {r[0] for r in conn.execute("SELECT ulid FROM event_log").fetchall()}
    finally:
        conn.close()
    assert set(kept) <= survivors
    report = events.verify_event_chain()
    assert report["status"] == "valid"


def test_prune_still_detects_a_broken_link_outside_its_holes():
    """Tolerating pruned holes must not hide a tampered link between survivors."""
    with _frozen_now(90):
        events.record_event("agent.test.updated", payload={"i": 0})
    first = events.record_event("agent.test.updated", payload={"i": 1})
    events.record_event("agent.test.updated", payload={"i": 2})
    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn, "test-tamper"):
            conn.execute(
                "UPDATE event_log SET prev_hash=? WHERE ROWID=(SELECT MAX(ROWID) FROM event_log)",
                ("0" * 64,),
            )
        conn.commit()
    finally:
        conn.close()
    assert first
    with pytest.raises(ValueError, match="tampered"):
        events.prune_events(older_than_days=30)


def test_protected_channels_match_the_proof_channels():
    from unified_agent.messaging import handoff
    from unified_agent.tools import approvals

    # Task #2019: a conversation handoff authorizes its task's completion
    # notices, so a pruned one refused every notice for good.
    assert events.PRUNE_PROTECTED_CHANNELS == {
        approvals._CONSUMPTION_CHANNEL,
        "agent.synthetic_workflow",
        "agent.synthetic_workflow.completed",
        approvals.CHANNEL_TASK_RECEIPT,
        approvals.CHANNEL_TASK_COMMITTED,
        handoff.HANDOFF_CHANNEL,
    }
