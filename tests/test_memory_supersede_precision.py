"""Superseding uses a precise clock before refusing a fractional validity start."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import consolidate, embeddings, storage
from unified_agent.tools import memory

NOW = datetime(2026, 6, 1, 12, 0, 0, 500000, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(memory, "datetime", FrozenDatetime)
    monkeypatch.setattr(storage, "now", lambda: NOW.isoformat(timespec="seconds"))

    def offline(_text):
        raise RuntimeError("lexical-only precision test")

    monkeypatch.setattr(embeddings, "generate_embedding", offline)


def save(content, **kwargs):
    return memory.memory_save(
        content, title="Chrononic precision", author="Desktop_Agent2", **kwargs
    )["id"]


def snapshot():
    conn = storage.connect()
    try:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("memory", "memory_links", "agent_events")
        }
    finally:
        conn.close()


@pytest.mark.parametrize("path", ["save", "consolidate"])
@pytest.mark.parametrize("delta_us", [-1, 0, 1], ids=["past", "equal", "future"])
@pytest.mark.parametrize("start_field", ["valid_from", "created_at"])
def test_fractional_supersede_boundary(path, delta_us, start_field):
    start = NOW + timedelta(microseconds=delta_us)
    # An offset representation must compare as an instant, including microseconds.
    offset_start = start.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()
    old = save(
        "Chrononic old claim.",
        **({"valid_from": offset_start} if start_field == "valid_from" else {}),
    )
    if start_field == "created_at":
        with storage.connect() as conn:
            conn.execute("UPDATE memory SET created_at=? WHERE id=?", (offset_start, old))
    keep = save("Chrononic survivor.") if path == "consolidate" else None
    before = snapshot()

    def supersede():
        if path == "save":
            return save("Chrononic new claim.", supersedes=old)
        consolidate.apply_supersede(keep, [old], actor="Desktop_Agent2")
        return keep

    if delta_us > 0:
        with pytest.raises(ToolError, match="no valid instant"):
            supersede()
        assert snapshot() == before  # no replacement, graph, demotion or audit partial writes
        return

    replacement = supersede()
    raw = memory.memory_get(old)
    end = NOW if delta_us < 0 else NOW + timedelta(microseconds=1)
    assert raw["superseded_by"] == replacement
    assert datetime.fromisoformat(raw["valid_to"]) == end
    assert memory.memory_get(old, as_of=start.isoformat())["id"] == old
    assert old in {
        hit["id"] for hit in memory.memory_search("Chrononic", as_of=start.isoformat())["results"]
    }
    with pytest.raises(ToolError, match="not valid at"):
        memory.memory_get(old, as_of=end.isoformat())
    assert old not in {
        hit["id"] for hit in memory.memory_search("Chrononic", as_of=end.isoformat())["results"]
    }
