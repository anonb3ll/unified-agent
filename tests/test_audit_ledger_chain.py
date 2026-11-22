"""Tests for Task #746: Tamper-evident audit ledger hash chain and verify command."""

from __future__ import annotations

import json
import sqlite3

import pytest

from unified_agent import config, events, storage
from unified_agent.audit import ledger_cli
from unified_agent.tools import events as tools_events


@pytest.fixture(autouse=True)
def init_clean_db(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()
    yield


def test_empty_ledger_verification():
    report = events.verify_event_chain()
    assert report["status"] == "empty"
    assert report["verified"] is True
    assert report["total_verified"] == 0
    assert report["head_hash"] is None


def test_sequential_hash_chain_valid():
    _ulid1 = events.record_event("agent.task.updated", payload={"task_id": 1, "status": "doing"})
    _ulid2 = events.record_event(
        "agent.approval.requested", payload={"tool": "run_cmd", "cmd": "ls"}
    )
    ulid3 = events.record_event("agent.task.updated", payload={"task_id": 1, "status": "done"})

    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT ulid, prev_hash, entry_hash FROM event_log ORDER BY ROWID ASC"
        ).fetchall()
        assert len(rows) == 3
        assert rows[0]["prev_hash"] == events.GENESIS_HASH
        assert rows[0]["entry_hash"] is not None
        assert rows[1]["prev_hash"] == rows[0]["entry_hash"]
        assert rows[2]["prev_hash"] == rows[1]["entry_hash"]
    finally:
        conn.close()

    report = events.verify_event_chain()
    assert report["status"] == "valid"
    assert report["verified"] is True
    assert report["total_verified"] == 3
    assert report["head_hash"] == rows[2]["entry_hash"]
    assert report["head_ulid"] == ulid3


def test_tamper_payload_detected():
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO event_log (ulid, channel, payload, created_at) "
            "VALUES ('COREMODE1', 'agent.coremode', '{}', '2026-09-04T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    events.record_event("agent.task.updated", payload={"task_id": 10, "state": "doing"})
    tamper_ulid = events.record_event("agent.approval.requested", payload={"approval_id": 42})
    events.record_event("agent.task.updated", payload={"task_id": 10, "state": "done"})

    # Deliberately mutate the payload of the 2nd row in SQLite directly
    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute(
                "UPDATE event_log SET payload=? WHERE ulid=?",
                (json.dumps({"approval_id": 42, "malicious_injection": True}), tamper_ulid),
            )
        conn.commit()
    finally:
        conn.close()

    report = events.verify_event_chain()
    assert report["status"] == "tampered"
    assert report["verified"] is False
    assert report["total_verified"] == 1
    assert report["unhashed_skipped"] == 1
    t = report["tampered_at"]
    assert t["ulid"] == tamper_ulid
    assert t["reason"] == "hash_mismatch"


def test_tamper_prev_hash_detected():
    events.record_event("agent.task.updated", payload={"task_id": 1})
    events.record_event("agent.task.updated", payload={"task_id": 2})
    tamper_ulid = events.record_event("agent.task.updated", payload={"task_id": 3})

    # Tamper prev_hash of 3rd row
    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute("UPDATE event_log SET prev_hash='BADPREV' WHERE ulid=?", (tamper_ulid,))
        conn.commit()
    finally:
        conn.close()

    report = events.verify_event_chain()
    assert report["status"] == "tampered"
    assert report["verified"] is False
    assert report["total_verified"] == 2
    t = report["tampered_at"]
    assert t["ulid"] == tamper_ulid
    assert t["reason"] == "prev_hash_mismatch"


def test_legacy_rows_without_entry_hash_skipped_gracefully():
    # Insert legacy row without entry_hash
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO event_log (ulid, channel, payload, created_at) VALUES ('OLD1', 'agent.old', '{}', '2026-07-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    # Now append new hashed events
    events.record_event("agent.new.event", payload={"ok": True})
    report = events.verify_event_chain()
    assert report["status"] == "valid"
    assert report["verified"] is True
    assert report["total_verified"] == 1


def test_only_legacy_rows_returns_empty():
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO event_log (ulid, channel, payload, created_at) "
            "VALUES ('OLD1', 'agent.old', '{}', '2026-07-01T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    report = events.verify_event_chain()
    assert report["status"] == "empty"
    assert report["verified"] is True
    assert report["total_verified"] == 0


def test_unhashed_row_after_chain_start_is_not_valid():
    # Task #1908: once the chain has started, every row must be hashed. A row
    # with entry_hash NULL after the first hashed row is an append that went
    # around the ledger (direct SQL, or a writer that skipped the chain), so the
    # report is not valid and names the offending rows. Before this, such a row
    # was skipped and the chain still reported "valid".
    events.record_event("agent.valid.event", payload={"step": 1})

    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO event_log (ulid, channel, payload, created_at) "
            "VALUES ('COREMODE1', 'agent.coremode', '{}', '2026-09-04T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    events.record_event("agent.valid.event", payload={"step": 2})

    report = events.verify_event_chain()
    assert report["status"] == "tampered"
    assert report["verified"] is False
    assert report["total_verified"] == 2
    assert report["unhashed_skipped"] == 0
    assert report["unhashed_after_chain_start"] == 1
    assert [r["ulid"] for r in report["unhashed_rows"]] == ["COREMODE1"]
    t = report["tampered_at"]
    assert t["ulid"] == "COREMODE1"
    assert t["reason"] == "unhashed_row_after_chain_start"


def test_unhashed_rows_before_chain_start_stay_allowed_and_counted():
    conn = storage.connect()
    try:
        for ulid in ("OLD1", "OLD2"):
            conn.execute(
                "INSERT INTO event_log (ulid, channel, payload, created_at) "
                "VALUES (?, 'agent.old', '{}', '2026-07-01T00:00:00Z')",
                (ulid,),
            )
        conn.commit()
    finally:
        conn.close()
    events.record_event("agent.new.event", payload={"ok": True})
    events.record_event("agent.new.event", payload={"ok": True})

    report = events.verify_event_chain()
    assert report["status"] == "valid"
    assert report["verified"] is True
    assert report["total_verified"] == 2
    assert report["unhashed_skipped"] == 2
    assert report["unhashed_after_chain_start"] == 0


def test_core_mode_gap_inside_enterprise_chain_is_reported(monkeypatch):
    # The downgrade/upgrade cycle: hashed rows, then core-mode unhashed rows,
    # then hashed rows again. The hashed rows still link, but a hole in the
    # ledger is no longer reported as "valid" (task #1908): an operator who
    # really ran core mode mid-chain sees exactly which rows were written
    # outside the ledger.
    monkeypatch.delenv("UA_EDITION", raising=False)
    events.record_event("agent.e1", payload={"n": 1})
    monkeypatch.setenv("UA_EDITION", "core")
    gap = events.record_event("agent.c1", payload={"n": 2})
    monkeypatch.setenv("UA_EDITION", "enterprise")
    events.record_event("agent.e2", payload={"n": 3})

    report = events.verify_event_chain()
    assert report["status"] == "tampered"
    assert report["verified"] is False
    assert report["total_verified"] == 2
    assert report["tampered_at"]["reason"] == "unhashed_row_after_chain_start"
    assert [r["ulid"] for r in report["unhashed_rows"]] == [gap]


def test_hash_mismatch_wins_over_unhashed_gap():
    events.record_event("agent.e1", payload={"n": 1})
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO event_log (ulid, channel, payload, created_at) "
            "VALUES ('GAP1', 'agent.gap', '{}', '2026-09-04T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()
    tamper_ulid = events.record_event("agent.e2", payload={"n": 2})
    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute("UPDATE event_log SET payload='{}' WHERE ulid=?", (tamper_ulid,))
        conn.commit()
    finally:
        conn.close()

    report = events.verify_event_chain()
    assert report["verified"] is False
    assert report["tampered_at"]["reason"] == "hash_mismatch"
    assert report["unhashed_after_chain_start"] == 1


def test_raw_update_of_event_log_is_refused_by_trigger():
    ulid = events.record_event("agent.e1", payload={"n": 1})
    conn = storage.connect()
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE event_log SET payload='{}' WHERE ulid=?", (ulid,))
        conn.rollback()
    finally:
        conn.close()
    assert events.verify_event_chain()["status"] == "valid"


def test_raw_delete_of_event_log_is_refused_by_trigger():
    ulid = events.record_event("agent.e1", payload={"n": 1})
    conn = storage.connect()
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM event_log WHERE ulid=?", (ulid,))
        conn.rollback()
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM event_log")
        conn.rollback()
    finally:
        conn.close()
    assert [e["ulid"] for e in events.query_events(limit=10)] == [ulid]


def test_append_only_triggers_survive_re_init():
    storage.init_db()
    storage.init_db()
    conn = storage.connect()
    try:
        names = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='event_log'"
            )
        }
    finally:
        conn.close()
    assert {"event_log_append_only_update", "event_log_append_only_delete"} <= names


def test_ledger_maintenance_bypass_is_transaction_scoped():
    ulid = events.record_event("agent.e1", payload={"n": 1})
    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute("UPDATE event_log SET originator='x' WHERE ulid=?", (ulid,))
        # The bypass row is gone before commit, so the next statement is
        # refused again inside the same transaction.
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE event_log SET originator='y' WHERE ulid=?", (ulid,))
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM event_log_maintenance").fetchone()[0] == 0
    finally:
        conn.close()


def test_core_mode_writes_unhashed_events(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "core")
    events.record_event("agent.core.event", payload={"n": 1})

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT entry_hash, prev_hash FROM event_log ORDER BY ROWID DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row["entry_hash"] is None
    assert row["prev_hash"] is None
    report = events.verify_event_chain()
    assert report["status"] == "empty"  # no hashed rows yet
    assert report["unhashed_skipped"] == 1


def test_tools_events_event_log_verify():
    events.record_event("agent.test", payload={"msg": "hello"})
    res = tools_events.event_log_verify()
    assert res["status"] == "valid"
    assert res["verified"] is True
    assert res["total_verified"] == 1


def test_cli_verify(capsys):
    events.record_event("agent.cli.test", payload={"foo": "bar"})

    # Test text output
    rc = ledger_cli.run_verify_cli([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "audit-ledger: valid" in out

    # Test json output
    rc_json = ledger_cli.run_verify_cli(["--json"])
    assert rc_json == 0
    out_json = capsys.readouterr().out
    data = json.loads(out_json)
    assert data["status"] == "valid"
    assert data["verified"] is True


def test_cli_verify_rejects_tampered_fixture():
    """CI gate: `unified-agent audit verify` exits 1 on a mutated payload."""
    events.record_event("agent.task.updated", payload={"task_id": 1})
    tamper_ulid = events.record_event("agent.task.updated", payload={"task_id": 2})
    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute(
                "UPDATE event_log SET payload=? WHERE ulid=?",
                (json.dumps({"task_id": 2, "injected": True}), tamper_ulid),
            )
        conn.commit()
    finally:
        conn.close()
    assert ledger_cli.run_verify_cli([]) == 1


def test_reanchor_writes_dated_note_without_mutating_chain(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Logs").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))

    ulid = events.record_event("agent.task.updated", payload={"task_id": 7})
    before = events.verify_event_chain()
    from unified_agent.audit.reanchor import reanchor_chain_head

    result = reanchor_chain_head(day="2026-09-06")
    after = events.verify_event_chain()
    assert result["status"] == "valid"
    assert result["head_ulid"] == ulid
    assert result["anchor_path"] == "Logs/audit-ledger-heads/2026-09-06.md"
    note = vault / "Logs" / "audit-ledger-heads" / "2026-09-06.md"
    assert note.is_file()
    text = note.read_text()
    assert before["head_hash"] in text
    assert after["head_hash"] == before["head_hash"]
    assert after["total_verified"] == before["total_verified"]
    assert ledger_cli.run_reanchor_cli(["--date", "2026-09-06"]) == 0
    assert result.get("anchor_action") == "created"
    second = reanchor_chain_head(day="2026-09-06")
    assert second["anchor_action"] == "unchanged"
    assert note.read_text().count("head_hash:") == 1


def test_reanchor_appends_when_head_hash_changes(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Logs").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))

    first_ulid = events.record_event("agent.task.updated", payload={"task_id": 7})
    from unified_agent.audit.reanchor import reanchor_chain_head

    first = reanchor_chain_head(day="2026-09-06")
    first_hash = first["head_hash"]
    events.record_event("agent.task.updated", payload={"task_id": 8})
    second = reanchor_chain_head(day="2026-09-06")
    note = vault / "Logs" / "audit-ledger-heads" / "2026-09-06.md"
    text = note.read_text()
    assert first["anchor_action"] == "created"
    assert second["anchor_action"] == "appended"
    assert first_hash in text
    assert second["head_hash"] in text
    assert first_hash != second["head_hash"]
    assert text.index(first_hash) < text.index(second["head_hash"])
    assert text.count("# Audit ledger chain head 2026-09-06") == 2
    assert first_ulid in text
    assert "locked: true" in text


def test_reanchor_tampered_chain_still_writes_note(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Logs").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))

    events.record_event("agent.task.updated", payload={"task_id": 1})
    tamper_ulid = events.record_event("agent.task.updated", payload={"task_id": 2})
    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute(
                "UPDATE event_log SET payload=? WHERE ulid=?",
                (json.dumps({"task_id": 2, "injected": True}), tamper_ulid),
            )
        conn.commit()
    finally:
        conn.close()

    def _chain_rows():
        conn = storage.connect()
        try:
            return conn.execute(
                "SELECT ulid, prev_hash, entry_hash FROM event_log ORDER BY ROWID"
            ).fetchall()
        finally:
            conn.close()

    before_rows = [(r["ulid"], r["prev_hash"], r["entry_hash"]) for r in _chain_rows()]
    before = events.verify_event_chain()
    assert ledger_cli.run_reanchor_cli(["--date", "2026-09-06"]) == 1
    after = events.verify_event_chain()
    after_rows = [(r["ulid"], r["prev_hash"], r["entry_hash"]) for r in _chain_rows()]
    note = vault / "Logs" / "audit-ledger-heads" / "2026-09-06.md"
    assert note.is_file()
    text = note.read_text()
    assert "status: tampered" in text
    assert tamper_ulid in text
    assert after_rows == before_rows
    assert after["status"] == before["status"]
    assert after["total_verified"] == before["total_verified"]


def test_reanchor_rejects_invalid_date(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Logs").mkdir(parents=True)
    monkeypatch.setenv("UA_VAULT_PATH", str(vault))
    assert ledger_cli.run_reanchor_cli(["--date", "09-06-2026"]) == 2
    assert not (vault / "Logs" / "audit-ledger-heads").exists()


def test_hash_mismatch_report_counts_later_gaps(monkeypatch):
    events.record_event("agent.first", payload={})
    monkeypatch.setenv("UA_EDITION", "core")
    gap1 = events.record_event("agent.gap1", payload={})
    monkeypatch.setenv("UA_EDITION", "enterprise")
    corrupt = events.record_event("agent.corrupt", payload={"original": True})
    monkeypatch.setenv("UA_EDITION", "core")
    gap2 = events.record_event("agent.gap2", payload={})
    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute("UPDATE event_log SET payload='{}' WHERE ulid=?", (corrupt,))
        conn.commit()
    finally:
        conn.close()
    report = events.verify_event_chain()
    assert report["tampered_at"]["reason"] == "hash_mismatch"
    assert report["unhashed_after_chain_start"] == 2
    assert [row["ulid"] for row in report["unhashed_rows"]] == [gap1, gap2]
