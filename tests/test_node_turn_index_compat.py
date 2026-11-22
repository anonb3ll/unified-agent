"""The optional turn index must not change event durability on older SQLite."""

import json
import sqlite3

import pytest

from unified_agent import events, storage
from unified_agent.tools import doctor

INDEX = "idx_event_log_node_item_turn"
requires_json5 = pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 42, 0),
    reason="requires a JSON5 engine to build the optional index",
)


def _has_index(conn):
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)
        ).fetchone()
        is not None
    )


@requires_json5
def test_older_runtime_reopens_indexed_database_without_changing_history(monkeypatch):
    storage.init_db()
    conn = storage.connect()
    try:
        assert _has_index(conn)
    finally:
        conn.close()
    ulid = events.record_event("agent.node", {"item_id": "old", "turn_id": "t", "x": float("nan")})
    conn = storage.connect()
    try:
        before = dict(conn.execute("SELECT * FROM event_log WHERE ulid=?", (ulid,)).fetchone())
    finally:
        conn.close()
    monkeypatch.setattr(storage, "sqlite_runtime_version", lambda conn: "3.40.1", raising=False)
    assert doctor._node_turn_index_check()["status"] == "DEGRADED"
    storage.init_db()
    conn = storage.connect()
    try:
        assert not _has_index(conn)
        assert (
            dict(conn.execute("SELECT * FROM event_log WHERE ulid=?", (ulid,)).fetchone()) == before
        )
    finally:
        conn.close()
    new = events.record_event("agent.node", {"item_id": "new", "x": float("inf")})
    conn = storage.connect()
    try:
        assert (
            "Infinity"
            in conn.execute("SELECT payload FROM event_log WHERE ulid=?", (new,)).fetchone()[0]
        )
    finally:
        conn.close()


def test_older_runtime_initializes_without_optional_index(monkeypatch):
    monkeypatch.setattr(storage, "sqlite_runtime_version", lambda conn: "3.41.2", raising=False)
    storage.init_db()
    conn = storage.connect()
    try:
        assert not _has_index(conn)
    finally:
        conn.close()
    events.record_event("agent.node", {"item_id": "x", "x": float("nan")})
    result = doctor._node_turn_index_check()
    assert result["status"] == "DORMANT"
    assert "3.41.2" in result["detail"]
    assert "3.42" in result["gate"]


@requires_json5
def test_doctor_reports_present_and_missing_supported_index():
    storage.init_db()
    live = doctor._node_turn_index_check()
    assert live["status"] == "LIVE"
    assert sqlite3.sqlite_version in live["detail"]
    conn = storage.connect()
    try:
        conn.execute(f"DROP INDEX {INDEX}")
        conn.commit()
    finally:
        conn.close()
    assert doctor._node_turn_index_check()["status"] == "DEGRADED"
    storage.init_db()
    assert doctor._node_turn_index_check()["status"] == "LIVE"


@requires_json5
def test_supported_runtime_indexes_existing_non_finite_history():
    storage.init_db()
    conn = storage.connect()
    try:
        conn.execute(f"DROP INDEX {INDEX}")
        conn.commit()
    finally:
        conn.close()
    original = {"item_id": "legacy", "turn_id": "t", "x": float("nan")}
    ulid = events.record_event("agent.node", original)
    storage.init_db()
    conn = storage.connect()
    try:
        assert _has_index(conn)
        raw = conn.execute("SELECT payload FROM event_log WHERE ulid=?", (ulid,)).fetchone()[0]
        assert "NaN" in raw
        assert json.loads(raw)["item_id"] == "legacy"
    finally:
        conn.close()
