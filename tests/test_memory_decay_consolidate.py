from datetime import UTC, datetime, timedelta

import pytest

from unified_agent import consolidate, storage
from unified_agent.consolidate import generate_proposal
from unified_agent.tools.memory import memory_save, memory_search


@pytest.fixture(autouse=True)
def _stubbed_adjudication(monkeypatch):
    """`generate_proposal()` reaches a live LLM since #437; the suite must not.

    These tests are about decay and stale-note selection, not adjudication. See
    the matching fixture in test_consolidate.py for the full rationale.
    """
    monkeypatch.setattr(
        consolidate.llm_client,
        "complete",
        lambda prompt, system=None, **kwargs: {
            "text": "VERDICT: DUPLICATE\nREASON: canned adjudication for tests.",
            "provider": "stub",
        },
    )


class TestMemoryDecayAndStaleNotes:
    def test_memory_search_recency_field(self, monkeypatch):
        # Disable semantic search for deterministic test
        from unified_agent import embeddings

        monkeypatch.setattr(embeddings, "generate_embedding", lambda x: RuntimeError("offline"))

        memory_save("Widget specifications document for testing", title="Widget Doc")

        res = memory_search("Widget")
        assert len(res["results"]) > 0
        note = res["results"][0]
        assert "recency" in note
        assert isinstance(note["recency"], float)

    def test_memory_search_decay_discount(self, monkeypatch):
        # Disable semantic search for deterministic test
        from unified_agent import embeddings

        monkeypatch.setattr(embeddings, "generate_embedding", lambda x: RuntimeError("offline"))

        # Create two notes. Update one to be 10 months old (untouched).
        r1 = memory_save("Common keyword test match", title="Fresh Note")
        r2 = memory_save("Common keyword test match", title="Old Note")

        # Manually update updated_at of the old note in the database
        conn = storage.connect()
        try:
            # 10 months ago (10 * 30.44 days)
            old_date = (datetime.now(UTC) - timedelta(days=10 * 30.44)).isoformat()
            conn.execute("UPDATE memory SET updated_at = ? WHERE id = ?", (old_date, r2["id"]))
            conn.commit()
        finally:
            conn.close()

        # Set UA_MEMORY_DECAY_MONTHS to 6
        monkeypatch.setenv("UA_MEMORY_DECAY_MONTHS", "6.0")

        # Search for keyword
        res = memory_search("Common")
        results = res["results"]
        # The fresh note should rank first since the old note was gently discounted due to age
        assert results[0]["id"] == r1["id"]
        assert results[1]["id"] == r2["id"]

    def test_consolidate_stale_high_salience_notes(self, monkeypatch):
        # Create a high-salience note (salience >= 0.7) and a low-salience note
        r1 = memory_save("Important architecture choices", title="Important Decision", salience=0.8)
        r2 = memory_save("Trivial detail notes", title="Trivial Info", salience=0.3)

        # Set UA_MEMORY_STALE_THRESHOLD_MONTHS to 3
        monkeypatch.setenv("UA_MEMORY_STALE_THRESHOLD_MONTHS", "3.0")

        # Manually update updated_at for both notes to be 4 months ago
        conn = storage.connect()
        try:
            old_date = (datetime.now(UTC) - timedelta(days=4 * 30.44)).isoformat()
            conn.execute("UPDATE memory SET updated_at = ? WHERE id = ?", (old_date, r1["id"]))
            conn.execute("UPDATE memory SET updated_at = ? WHERE id = ?", (old_date, r2["id"]))
            conn.commit()
        finally:
            conn.close()

        # Generate proposal
        res = generate_proposal(write=False)
        stale = res["stale_notes"]

        # Only the high-salience note should be in stale_notes
        stale_ids = [sn["id"] for sn in stale]
        assert r1["id"] in stale_ids
        assert r2["id"] not in stale_ids

        # The proposal markdown should include the Stale section
        assert "## Stale High-Salience Notes for Re-confirmation" in res["markdown"]
        assert r1["id"] in res["markdown"]
