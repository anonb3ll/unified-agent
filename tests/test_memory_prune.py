"""Tests for memory_prune_candidates tool."""

import json
from datetime import UTC, datetime, timedelta

from unified_agent import storage
from unified_agent.tools.memory_prune import memory_prune_candidates


def test_memory_prune_candidates_basic():
    conn = storage.connect()

    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=60)).isoformat()
    recent_ts = now.isoformat()

    try:
        # Insert test notes
        # 1. Superseded note
        conn.execute(
            "INSERT INTO memory (id, title, content, tags, created_at, updated_at, salience, access_count, superseded_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "note1",
                "Old Note",
                "content",
                json.dumps(["tag1"]),
                recent_ts,
                recent_ts,
                0.5,
                5,
                "note2",
            ),
        )
        # 2. Low salience note
        conn.execute(
            "INSERT INTO memory (id, title, content, tags, created_at, updated_at, salience, access_count, superseded_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                "note2",
                "Low Salience Note",
                "content",
                json.dumps(["tag2"]),
                recent_ts,
                recent_ts,
                0.1,
                0,
            ),
        )
        # 3. Decayed unused note
        conn.execute(
            "INSERT INTO memory (id, title, content, tags, created_at, updated_at, salience, access_count, superseded_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            ("note3", "Decayed Note", "content", json.dumps(["tag3"]), old_ts, old_ts, 0.4, 0),
        )
        # 4. Healthy active note
        conn.execute(
            "INSERT INTO memory (id, title, content, tags, created_at, updated_at, salience, access_count, superseded_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                "note4",
                "Healthy Note",
                "content",
                json.dumps(["tag4"]),
                recent_ts,
                recent_ts,
                0.8,
                10,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = memory_prune_candidates(salience_threshold=0.3, max_age_days=30, limit=10)

    assert result["candidate_count"] == 3
    candidate_ids = {c["id"] for c in result["candidates"]}
    assert "note1" in candidate_ids
    assert "note2" in candidate_ids
    assert "note3" in candidate_ids
    assert "note4" not in candidate_ids
