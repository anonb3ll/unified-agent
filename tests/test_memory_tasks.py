from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.tools.memory import (
    memory_delete,
    memory_get,
    memory_list,
    memory_save,
    memory_search,
)
from unified_agent.tools.tasks import task_add, task_claim, task_delete, task_list, task_update


@contextmanager
def _identity(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


class TestMemory:
    def test_save_and_search(self):
        r = memory_save(
            "The deploy pipeline uses blue-green rollouts",
            title="Deploys",
            tags=["project:acme", "fact"],
        )
        assert r["created"] is True
        hits = memory_search("blue-green rollouts")["results"]
        assert len(hits) == 1
        assert hits[0]["id"] == r["id"]
        assert "blue-green" in hits[0]["snippet"]

        # Test quote handling and preventing FTS5 syntax errors
        hits2 = memory_search('Deploys "blue-green"')["results"]
        assert len(hits2) == 1

        with pytest.raises(ToolError, match="query must contain"):
            memory_search('"')

    def test_fts_update_trigger_scoped_to_title_content(self):
        # The memory_au reindex trigger must be scoped to `OF title, content` so
        # access_count/last_accessed bumps (a hot path) don't re-index FTS on reads.

        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='memory_au'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "memory_au trigger missing"
        assert "UPDATE OF" in row["sql"]
        assert "title" in row["sql"] and "content" in row["sql"]

    def test_access_bump_does_not_corrupt_fts(self):
        # A raw access-stat bump (what memory_search/get do) must not desync FTS.

        r = memory_save("searchable widget content", title="Widget")
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE memory SET access_count = access_count + 1 WHERE id = ?", (r["id"],)
            )
            conn.commit()
        finally:
            conn.close()
        hits = memory_search("searchable widget")["results"]
        assert any(h["id"] == r["id"] for h in hits)

    def test_search_tag_filter(self):
        memory_save("python style: use ruff", tags=["pref"])
        memory_save("python version is 3.12", tags=["fact"])
        hits = memory_search("python", tags=["pref"])["results"]
        assert len(hits) == 1
        assert hits[0]["tags"] == ["pref"]

    def test_update_by_id(self):
        r = memory_save("v1")
        r2 = memory_save("v2 content", id=r["id"])
        assert r2 == {"id": r["id"], "created": False, "revision": memory_get(r["id"])["revision"]}
        assert memory_get(r["id"])["content"] == "v2 content"
        # The old text is gone from the index. Since #1913 a short query can
        # still return the note semantically, but never on the stale "v1" FTS row.
        assert all(hit["score"] is None for hit in memory_search("v1")["results"])

    def test_update_missing_id(self):
        with pytest.raises(ToolError, match="no memory note"):
            memory_save("x", id="doesnotexist")

    def test_author_recorded_on_create(self):
        r = memory_save("owned note", author="Laptop_Agent1")
        note = memory_get(r["id"])
        assert note["author"] == "Laptop_Agent1"
        assert note["updated_by"] == "Laptop_Agent1"

    def test_same_author_can_replace(self):
        r = memory_save("v1", author="Laptop_Agent1")
        memory_save("v2", id=r["id"], author="Laptop_Agent1")
        assert memory_get(r["id"])["content"] == "v2"

    def test_cross_author_replace_is_blocked(self):
        r = memory_save("desktop's note", author="Desktop_Agent1")
        with pytest.raises(ToolError, match="owned by 'Desktop_Agent1'"):
            memory_save("clobbered", id=r["id"], author="Laptop_Agent5")
        # Original content is untouched.
        assert memory_get(r["id"])["content"] == "desktop's note"

    def test_anonymous_cannot_replace_owned_note(self):
        r = memory_save("owned", author="Desktop_Agent1")
        with pytest.raises(ToolError, match="anonymous"):
            memory_save("clobbered", id=r["id"])
        assert memory_get(r["id"])["content"] == "owned"

    def test_overwrite_flag_allows_deliberate_replace(self):
        r = memory_save("desktop's note", author="Desktop_Agent1")
        memory_save("replaced", id=r["id"], author="Laptop_Agent5", overwrite=True)
        note = memory_get(r["id"])
        # The new content leads; the peer's replaced work is not destroyed but
        # preserved as an attributed tombstone below it.
        assert note["content"].startswith("replaced")
        assert "desktop's note" in note["content"]
        assert "tombstone" in note["content"]
        assert "Desktop_Agent1" in note["content"]
        # Ownership stays with the creator; updated_by tracks the last writer.
        assert note["author"] == "Desktop_Agent1"
        assert note["updated_by"] == "Laptop_Agent5"

    def test_cross_author_overwrite_preserves_old_title(self):
        r = memory_save("v1 body", title="Original Title", author="Desktop_Agent1")
        memory_save("v2 body", id=r["id"], author="Laptop_Agent5", overwrite=True)
        note = memory_get(r["id"])
        assert "was titled: Original Title" in note["content"]

    def test_same_author_overwrite_does_not_tombstone(self):
        # Editing your own note is not clobbering a peer — no tombstone.
        r = memory_save("v1", author="Desktop_Agent1")
        memory_save("v2", id=r["id"], author="Desktop_Agent1", overwrite=True)
        note = memory_get(r["id"])
        assert note["content"] == "v2"
        assert "tombstone" not in note["content"]

    def test_anonymous_overwrite_of_owned_note_tombstones(self):
        # An anonymous replace of an owned note is still a cross-identity clobber.
        r = memory_save("owned body", author="Desktop_Agent1")
        memory_save("anon replacement", id=r["id"], overwrite=True)
        note = memory_get(r["id"])
        assert note["content"].startswith("anon replacement")
        assert "owned body" in note["content"]

    def test_append_preserves_and_attributes(self):
        r = memory_save("original body", author="Desktop_Agent1", tags=["fact"])
        memory_save("a peer's addition", id=r["id"], author="Laptop_Agent5", append=True)
        note = memory_get(r["id"])
        assert "original body" in note["content"]
        assert "a peer's addition" in note["content"]
        assert "Laptop_Agent5" in note["content"]
        # Append keeps existing tags when none are supplied.
        assert note["tags"] == ["fact"]
        assert note["author"] == "Desktop_Agent1"
        assert note["updated_by"] == "Laptop_Agent5"

    def test_guard_refusal_does_not_coach_the_bypass(self):
        # 2026-07-12 guard drill: an agent refused a plain replace followed the
        # error message's own "or overwrite=True" hint straight through the
        # guard. The refusal must not name the escape hatch.
        r = memory_save("desktop's note", author="Desktop_Agent1")
        with pytest.raises(ToolError) as exc:
            memory_save("clobbered", id=r["id"], author="Laptop_Agent5")
        assert "overwrite" not in str(exc.value)

    def test_replace_keeps_title_and_tags_when_omitted(self):
        r = memory_save("v1", title="Keep me", tags=["fact"], author="Desktop_Agent1")
        memory_save("v2", id=r["id"], author="Desktop_Agent1")
        note = memory_get(r["id"])
        assert note["content"] == "v2"
        assert note["title"] == "Keep me"
        assert note["tags"] == ["fact"]

    def test_replace_with_explicit_empty_tags_clears_them(self):
        r = memory_save("v1", tags=["fact"], author="Desktop_Agent1")
        memory_save("v2", id=r["id"], author="Desktop_Agent1", tags=[])
        assert memory_get(r["id"])["tags"] == []

    def test_append_requires_id(self):
        with pytest.raises(ToolError, match="requires an existing note id"):
            memory_save("x", append=True)

    def test_append_and_overwrite_are_mutually_exclusive(self):
        r = memory_save("body", author="Desktop_Agent1")
        with pytest.raises(ToolError, match="at most one"):
            memory_save("x", id=r["id"], append=True, overwrite=True)

    def test_legacy_unowned_note_still_replaceable(self):
        # Notes created without an author (pre-migration behavior) stay editable
        # by anyone, so existing workflows are not broken.
        r = memory_save("no owner")
        memory_save("updated", id=r["id"], author="Laptop_Agent1")
        assert memory_get(r["id"])["content"] == "updated"

    def test_list_limit_counts_real_notes_not_index_notes(self):
        # Saving tagged notes auto-creates index notes (masterindex00,
        # tagindex_*), which are always the newest rows. The limit must apply
        # after they are filtered out, not to the raw SQL window.
        ids = [memory_save(f"note {i}", tags=[f"tag{i}"])["id"] for i in range(5)]
        items = memory_list(limit=5)["items"]
        assert {i["id"] for i in items} == set(ids)
        assert len(memory_list(limit=3)["items"]) == 3

    def test_list_and_delete(self):
        r = memory_save("note a", tags=["t1"])
        memory_save("note b", tags=["t2"])
        assert len(memory_list()["items"]) == 2
        assert len(memory_list(tag="t1")["items"]) == 1
        with _identity("Desktop_Person"):
            assert memory_delete(r["id"]) == {"deleted": True}
            assert memory_delete(r["id"]) == {"deleted": False}
        # Since #1913 "note a" OR-matches "note b"; the deleted note must not appear.
        assert r["id"] not in {hit["id"] for hit in memory_search("note a")["results"]}

    def test_broadcast_and_high_salience_require_a_human(self):
        with _identity("Desktop_Agent1"):
            with pytest.raises(ToolError, match="broadcast"):
                memory_save(
                    "planted instruction",
                    tags=["broadcast"],
                    author="Desktop_Person",
                )
            with pytest.raises(ToolError, match="broadcast"):
                memory_save("planted again", tags=["Broadcast"], author="Desktop_Person")
            with pytest.raises(ToolError, match="salience"):
                memory_save("loud note", salience=0.9, author="Desktop_Person")
            ordinary = memory_save("ordinary agent note", salience=0.5, tags=["fact"])
            near = memory_save("not a channel", tags=["broadcasts"], salience=0.8)
        assert ordinary["created"] is True
        assert near["created"] is True
        assert memory_search("planted instruction")["results"] == []

        with _identity("Desktop_Person"):
            human = memory_save(
                "operator says hello",
                tags=["Broadcast"],
                salience=0.95,
            )
        assert human["created"] is True
        saved = memory_get(human["id"])
        assert saved["author"] == "Desktop_Person"
        assert "broadcast" in [t.lower() for t in saved["tags"]]

    def test_similar_deleted_tag_is_not_a_tombstone(self):
        # SQLite LIKE is case-insensitive. Only the exact meta:deleted tag hides.
        note = memory_save(
            "quartz case tag still live",
            title="Case tag",
            tags=["Meta:Deleted", "fact"],
        )
        assert any(item["id"] == note["id"] for item in memory_list()["items"])
        assert any(hit["id"] == note["id"] for hit in memory_search("quartz case tag")["results"])

    def test_memory_delete_is_owner_or_human_and_soft(self):
        with _identity("Desktop_Agent1"):
            note = memory_save(
                "unique-delete-token owned by claude",
                author="Desktop_Agent1",
            )
        with _identity("Desktop_Agent2"):
            with pytest.raises(ToolError, match="owned by"):
                memory_delete(note["id"])
        with pytest.raises(ToolError, match="verified identity"):
            memory_delete(note["id"])
        with _identity("Desktop_Agent1"):
            assert memory_delete(note["id"]) == {"deleted": True}
        assert all(item["id"] != note["id"] for item in memory_list()["items"])
        assert memory_search("unique-delete-token")["results"] == []
        got = memory_get(note["id"])
        assert got["deleted"] is True
        assert "tombstone" in got["content"]
        assert "unique-delete-token" in got["content"]
        conn = storage.connect()
        try:
            row = conn.execute("SELECT id FROM memory WHERE id=?", (note["id"],)).fetchone()
        finally:
            conn.close()
        assert row is not None
        with _identity("Desktop_Agent1"):
            with pytest.raises(ToolError, match="deleted"):
                memory_save("revived", id=note["id"], tags=[])

    def test_idempotent_broadcast_replay_requires_a_human(self):
        with _identity("Desktop_Person"):
            note = memory_save(
                "broadcast body for replay",
                tags=["broadcast"],
                idempotency_key="bcast-replay-1",
            )
        with _identity("Desktop_Agent1"):
            with pytest.raises(ToolError, match="broadcast"):
                memory_save(
                    "broadcast body for replay",
                    id=note["id"],
                    idempotency_key="bcast-replay-1",
                )
        assert memory_get(note["id"])["content"] == "broadcast body for replay"

    def test_idempotent_retry_after_delete_does_not_report_success(self):
        with _identity("Desktop_Agent1"):
            note = memory_save("live body before delete", idempotency_key="del-retry-1")
            assert memory_delete(note["id"]) == {"deleted": True}
            with pytest.raises(ToolError, match="deleted"):
                memory_save(
                    "live body before delete",
                    id=note["id"],
                    idempotency_key="del-retry-1",
                )
        assert memory_get(note["id"])["deleted"] is True

    @pytest.mark.embeddings
    def test_memory_save_dedup_surfaces_near_dup(self, monkeypatch):
        # Create a note
        r1 = memory_save("The deploy pipeline uses blue-green rollouts", title="Deploys")
        # Try to save a near duplicate
        r2 = memory_save("The deploy pipeline uses blue-green rollouts", title="Deploys")
        assert "similar" in r2
        assert len(r2["similar"]) == 1
        assert r2["similar"][0]["id"] == r1["id"]
        assert r2["similar"][0]["similarity"] >= 0.85
        assert r2["similar"][0]["conflict"] is False

    def test_memory_save_dedup_threshold(self, monkeypatch):
        # Create a note
        memory_save("The deploy pipeline uses blue-green rollouts", title="Deploys")
        # Set threshold to 0.99 so it's not surfaced
        monkeypatch.setenv("UA_MEMORY_DEDUP_THRESHOLD", "0.99")
        # Try to save a near duplicate but with some variation
        r2 = memory_save("The deployment process uses a blue green rollout", title="Deployments")
        assert "similar" in r2
        # It shouldn't be surfaced because similarity is below 0.99
        assert len(r2["similar"]) == 0

    @pytest.mark.embeddings
    def test_memory_save_check_only_writes_nothing(self):
        content = "The deploy pipeline uses blue-green rollouts"
        # check_only=True returns similar but writes nothing
        res = memory_save(content, title="Deploys", check_only=True)
        assert "similar" in res
        assert res["similar"] == []

        # Verify it wasn't written
        assert len(memory_list()["items"]) == 0

        # Now save a note first
        r1 = memory_save(content, title="Deploys")
        # Check only on same content
        res2 = memory_save(content, title="Deploys", check_only=True)
        assert len(res2["similar"]) == 1
        assert res2["similar"][0]["id"] == r1["id"]
        # Verify no second note was created
        assert len(memory_list()["items"]) == 1

    def test_memory_save_check_only_with_id_raises(self):
        r1 = memory_save("v1")
        with pytest.raises(ToolError, match="check_only=True is only supported"):
            memory_save("v2", id=r1["id"], check_only=True)

    def test_memory_save_degrades_when_embeddings_unavailable(self, monkeypatch):
        # Mock generate_embedding to raise exception
        from unified_agent import embeddings

        monkeypatch.setattr(embeddings, "generate_embedding", lambda x: RuntimeError("offline"))

        # Create note (should succeed offline)
        r1 = memory_save("Offline content", title="Offline")
        assert r1["created"] is True

        # Attempt to save a duplicate, should not raise, similar should be empty
        r2 = memory_save("Offline content", title="Offline")
        assert r2["created"] is True
        assert r2["similar"] == []

    @pytest.mark.embeddings
    def test_memory_save_conflict_detection_kv(self):
        # Save a note with kv
        memory_save("port: 8765\nstatus: active", title="Config")
        # Save conflicting kv
        res = memory_save("port: 8766\nstatus: active", title="Config")
        assert len(res["similar"]) == 1
        assert res["similar"][0]["conflict"] is True

        # Save non-conflicting kv
        res2 = memory_save("port: 8765\nstatus: active", title="Config")
        assert len(res2["similar"]) == 2
        conflicts = {r["conflict"] for r in res2["similar"]}
        assert conflicts == {True, False}

    @pytest.mark.embeddings
    def test_memory_save_conflict_detection_antonyms(self):
        # Save a note
        memory_save("The database backup is enabled on the server.", title="Backup")
        # Save conflicting antonym
        res = memory_save("The database backup is disabled on the server.", title="Backup")
        assert len(res["similar"]) == 1
        assert res["similar"][0]["conflict"] is True

    def test_memory_migration_upgrade(self, tmp_path, monkeypatch):
        # Create a pre-existing DB with schema version 2 (missing salience, confidence, last_accessed, access_count)
        db_file = tmp_path / "legacy.db"
        import sqlite3

        conn = sqlite3.connect(db_file)
        # Create old schema
        conn.execute("""
        CREATE TABLE memory (
          id TEXT PRIMARY KEY,
          title TEXT,
          content TEXT NOT NULL,
          tags TEXT NOT NULL DEFAULT '[]',
          author TEXT,
          updated_by TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """)
        # Insert legacy note
        conn.execute(
            "INSERT INTO memory (id, title, content, tags, created_at, updated_at) "
            "VALUES ('legacyid1234', 'Legacy Note', 'Legacy Content', '[]', '2026-07-10T12:00:00Z', '2026-07-10T12:00:00Z')"
        )
        conn.commit()
        conn.close()

        # Run migration on this DB

        monkeypatch.setattr(storage.config, "db_path", lambda: str(db_file))

        # This will call _migrate
        storage.init_db()

        # Connect and verify columns and backfilled values
        conn = storage.connect()
        row = conn.execute("SELECT * FROM memory WHERE id = 'legacyid1234'").fetchone()
        assert row["title"] == "Legacy Note"
        assert row["salience"] == 0.5
        assert row["confidence"] is None
        assert row["last_accessed"] is None
        assert row["access_count"] == 0
        conn.close()

    def test_memory_save_custom_salience_confidence(self):
        r = memory_save(
            "Salience test content", title="Custom Salience", salience=0.8, confidence=0.9
        )
        note = memory_get(r["id"])
        assert note["salience"] == 0.8
        assert note["confidence"] == 0.9

        # Update note and verify it preserves if omitted
        memory_save("Updated salience test content", id=r["id"])
        note2 = memory_get(r["id"])
        assert note2["salience"] == 0.8
        assert note2["confidence"] == 0.9

        # Update note with new values
        memory_save("Updated salience test content", id=r["id"], salience=0.2, confidence=0.1)
        note3 = memory_get(r["id"])
        assert note3["salience"] == 0.2
        assert note3["confidence"] == 0.1

    def test_memory_get_bumps_access_stats(self):
        r = memory_save("Access test content", title="Access Stats")
        note = memory_get(r["id"])
        assert note["access_count"] == 1
        assert note["last_accessed"] is not None

        # Get again and verify access count increments
        note2 = memory_get(r["id"])
        assert note2["access_count"] == 2

    def test_memory_search_bumps_access_stats_and_ranks(self, monkeypatch):
        # Disable semantic search for deterministic FTS tests
        from unified_agent import embeddings

        monkeypatch.setattr(embeddings, "generate_embedding", lambda x: RuntimeError("offline"))

        r1 = memory_save("Target match word alpha", title="Alpha Note", salience=0.1)
        r2 = memory_save("Target match word alpha", title="Beta Note", salience=0.8)

        # Search for alpha
        res = memory_search("alpha")
        results = res["results"]
        # Beta Note should rank first because of higher salience
        assert results[0]["id"] == r2["id"]
        assert results[1]["id"] == r1["id"]

        # Search bumps access stats for returned notes
        note1 = memory_get(r1["id"])
        note2 = memory_get(r2["id"])
        # They were accessed during search (1) and then get (2)
        assert note1["access_count"] == 2
        assert note2["access_count"] == 2


class TestMemoryOverwriteAllowlist:
    """UA_OVERWRITE_AUTHORS: named cross-author pairs may skip the tombstone.

    The allowlist is a feature, not a gate: it does NOT bypass the clobber
    guard (overwrite=True is still required), it only suppresses the
    tombstone that would otherwise be appended on a cross-author replace,
    and emits a structured `ua-audit:` line to stderr.
    """

    def test_allowlist_pair_skips_tombstone_and_logs(self, monkeypatch, capsys):
        monkeypatch.setenv("UA_OVERWRITE_AUTHORS", "Laptop_Agent5:Desktop_agent3")
        r = memory_save("peer note", author="Desktop_agent3")
        memory_save(
            "clean replace",
            id=r["id"],
            author="Laptop_Agent5",
            overwrite=True,
        )
        note = memory_get(r["id"])
        # The new content is the only thing in the note — no tombstone.
        assert note["content"] == "clean replace"
        assert "tombstone" not in note["content"]
        assert "peer note" not in note["content"]
        # Ownership stays with the creator; updated_by tracks the writer.
        assert note["author"] == "Desktop_agent3"
        assert note["updated_by"] == "Laptop_Agent5"
        # A structured ua-audit line was emitted to stderr.
        captured = capsys.readouterr()
        assert "ua-audit: tombstone-skipped" in captured.err
        assert f"note={r['id']!r}" in captured.err
        assert "actor='Laptop_Agent5'" in captured.err
        assert "owner='Desktop_agent3'" in captured.err

    def test_allowlist_pair_is_order_independent(self, monkeypatch):
        # `B:A` should match the same way `A:B` does.
        monkeypatch.setenv("UA_OVERWRITE_AUTHORS", "Desktop_agent3:Laptop_Agent5")
        r = memory_save("peer note", author="Desktop_agent3")
        memory_save(
            "clean replace",
            id=r["id"],
            author="Laptop_Agent5",
            overwrite=True,
        )
        assert memory_get(r["id"])["content"] == "clean replace"

    def test_allowlist_does_not_bypass_clobber_guard(self, monkeypatch):
        # The allowlist only suppresses the tombstone, not the refusal.
        # overwrite=False must still be refused for a cross-author replace.
        monkeypatch.setenv("UA_OVERWRITE_AUTHORS", "Laptop_Agent5:Desktop_agent3")
        r = memory_save("peer note", author="Desktop_agent3")
        with pytest.raises(ToolError, match="owned by 'Desktop_agent3'"):
            memory_save("clean replace", id=r["id"], author="Laptop_Agent5")
        # Original content untouched.
        assert memory_get(r["id"])["content"] == "peer note"

    def test_pair_not_in_allowlist_still_tombstones(self, monkeypatch):
        # The allowlist names a *different* pair; this cross-author write
        # must still tombstone (regression guard for the bypass logic).
        monkeypatch.setenv("UA_OVERWRITE_AUTHORS", "A:B")
        r = memory_save("peer note", author="Desktop_agent3")
        memory_save(
            "replaced",
            id=r["id"],
            author="Laptop_Agent5",
            overwrite=True,
        )
        note = memory_get(r["id"])
        assert note["content"].startswith("replaced")
        assert "peer note" in note["content"]
        assert "tombstone" in note["content"]

    def test_empty_allowlist_preserves_existing_behavior(self, monkeypatch):
        # No env var set — same-author tombstone policy from (b) is intact.
        monkeypatch.delenv("UA_OVERWRITE_AUTHORS", raising=False)
        r = memory_save("v1", author="Desktop_Agent1")
        memory_save("v2", id=r["id"], author="Laptop_Agent5", overwrite=True)
        note = memory_get(r["id"])
        assert "tombstone" in note["content"]

    def test_malformed_entries_are_silently_dropped(self, monkeypatch):
        # Bad entries (no colon, empty halves, self-pair) don't break the
        # rest of the allowlist, and don't accidentally match anything.
        monkeypatch.setenv(
            "UA_OVERWRITE_AUTHORS",
            "Laptop_Agent5:Desktop_agent3,malformed,Laptop_Agent5: , :Desktop_agent3,Laptop_Agent5:Laptop_Agent5",
        )
        r = memory_save("peer note", author="Desktop_agent3")
        # The valid pair Laptop_Agent5:Desktop_agent3 still matches.
        memory_save(
            "clean replace",
            id=r["id"],
            author="Laptop_Agent5",
            overwrite=True,
        )
        assert memory_get(r["id"])["content"] == "clean replace"

    def test_anonymous_writer_never_matches_allowlist(self, monkeypatch):
        # Anonymous (author=None) writes never benefit from the allowlist —
        # the existing (b) tombstone behavior covers them.
        monkeypatch.setenv("UA_OVERWRITE_AUTHORS", "anonymous:Desktop_agent3")
        r = memory_save("owned body", author="Desktop_agent3")
        memory_save("anon replacement", id=r["id"], overwrite=True)
        note = memory_get(r["id"])
        assert note["content"].startswith("anon replacement")
        assert "owned body" in note["content"]
        assert "tombstone" in note["content"]

    def test_config_parser_handles_whitespace_and_empty(self, monkeypatch):
        # Comma-separated, whitespace tolerated, empty string -> empty set.
        from unified_agent.config import overwrite_author_pairs

        monkeypatch.setenv("UA_OVERWRITE_AUTHORS", "")
        assert overwrite_author_pairs() == set()
        monkeypatch.setenv("UA_OVERWRITE_AUTHORS", "  ")
        assert overwrite_author_pairs() == set()
        monkeypatch.setenv("UA_OVERWRITE_AUTHORS", " A : B , C:D ")
        pairs = overwrite_author_pairs()
        assert frozenset({"A", "B"}) in pairs
        assert frozenset({"C", "D"}) in pairs
        assert len(pairs) == 2

    def test_config_parser_drops_self_pairs_and_malformed(self, monkeypatch):
        from unified_agent.config import overwrite_author_pairs

        monkeypatch.setenv(
            "UA_OVERWRITE_AUTHORS",
            "A:A, A:B, B:A, no-colon, :, : , A:",
        )
        pairs = overwrite_author_pairs()
        # A:A dropped (self-pair), A:B and B:A collapse to one frozenset,
        # no-colon/: /A: are dropped. Only the A:B pair survives.
        assert pairs == {frozenset({"A", "B"})}


class TestSourceHost:
    """source_host: audit-trail column stamping the machine that originated a write.

    Additive and auto-populated (never a caller-supplied argument): memory notes
    and tasks carry the originating host so the hub can tell its own writes apart
    from a peer machine's mirror.
    """

    @staticmethod
    def _memory_source_host(note_id: str):

        conn = storage.connect()
        try:
            return conn.execute("SELECT source_host FROM memory WHERE id=?", (note_id,)).fetchone()[
                "source_host"
            ]
        finally:
            conn.close()

    @staticmethod
    def _task_source_host(task_id: int):

        conn = storage.connect()
        try:
            return conn.execute("SELECT source_host FROM tasks WHERE id=?", (task_id,)).fetchone()[
                "source_host"
            ]
        finally:
            conn.close()

    def test_source_host_column_exists_on_both_tables(self):

        conn = storage.connect()
        try:
            mem_cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory)").fetchall()}
            task_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        finally:
            conn.close()
        assert "source_host" in mem_cols
        assert "source_host" in task_cols

    def test_memory_save_stamps_source_host(self, monkeypatch):
        monkeypatch.setenv("UA_SOURCE_HOST", "desktop-hub")
        r = memory_save("a stamped note")
        assert self._memory_source_host(r["id"]) == "desktop-hub"

    def test_source_host_env_override_changes_stamped_value(self, monkeypatch):
        monkeypatch.setenv("UA_SOURCE_HOST", "laptop-mirror")
        r = memory_save("note from the laptop")
        assert self._memory_source_host(r["id"]) == "laptop-mirror"

    def test_source_host_defaults_to_hostname_when_env_unset(self, monkeypatch):
        import socket

        monkeypatch.delenv("UA_SOURCE_HOST", raising=False)
        r = memory_save("note with no override")
        assert self._memory_source_host(r["id"]) == socket.gethostname()

    def test_contextvar_override_takes_precedence_over_env(self, monkeypatch):
        # A request-scoped middleware can pin the caller's identity; it wins over env.

        monkeypatch.setenv("UA_SOURCE_HOST", "env-host")
        token = config.source_host_var.set("ctx-host")
        try:
            r = memory_save("note attributed by middleware")
        finally:
            config.source_host_var.reset(token)
        assert self._memory_source_host(r["id"]) == "ctx-host"

    def test_task_add_stamps_source_host(self, monkeypatch):
        monkeypatch.setenv("UA_SOURCE_HOST", "desktop-hub")
        t = task_add("a stamped task")
        assert self._task_source_host(t["id"]) == "desktop-hub"

    def test_legacy_rows_without_source_host_read_back_fine(self, tmp_path, monkeypatch):
        # A pre-source_host DB: existing rows must migrate to NULL and stay readable,
        # and new writes on the upgraded DB get stamped.
        import sqlite3

        db_file = tmp_path / "legacy_sh.db"
        conn = sqlite3.connect(db_file)
        conn.execute(
            """
            CREATE TABLE memory (
              id TEXT PRIMARY KEY, title TEXT, content TEXT NOT NULL,
              tags TEXT NOT NULL DEFAULT '[]', author TEXT, updated_by TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, notes TEXT,
              status TEXT NOT NULL DEFAULT 'open', priority TEXT NOT NULL DEFAULT 'med',
              parent_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              completed_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO memory (id, title, content, tags, created_at, updated_at) "
            "VALUES ('legacyid5678', 'Legacy', 'Legacy body', '[]', "
            "'2026-07-10T12:00:00Z', '2026-07-10T12:00:00Z')"
        )
        conn.execute(
            "INSERT INTO tasks (title, created_at, updated_at) "
            "VALUES ('legacy task', '2026-07-10T12:00:00Z', '2026-07-10T12:00:00Z')"
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(storage.config, "db_path", lambda: str(db_file))
        storage.init_db()

        conn = storage.connect()
        try:
            mrow = conn.execute("SELECT * FROM memory WHERE id='legacyid5678'").fetchone()
            trow = conn.execute("SELECT * FROM tasks WHERE title='legacy task'").fetchone()
        finally:
            conn.close()
        # Old rows migrate to NULL origin and remain fully readable.
        assert mrow["content"] == "Legacy body"
        assert mrow["source_host"] is None
        assert trow["source_host"] is None
        assert trow["archived_at"] is None

    def test_source_host_read_paths_and_filtering(self, monkeypatch):
        # 1. Memory paths
        monkeypatch.setenv("UA_SOURCE_HOST", "desktop-host")
        note1 = memory_save("desktop note body", title="Desktop Note")

        monkeypatch.setenv("UA_SOURCE_HOST", "laptop-mac")
        note2 = memory_save("laptop note body", title="Laptop Note")

        # Test memory_get returns source_host
        res_get1 = memory_get(note1["id"])
        assert res_get1["source_host"] == "desktop-host"
        res_get2 = memory_get(note2["id"])
        assert res_get2["source_host"] == "laptop-mac"

        # Test memory_list returns source_host
        res_list = memory_list(limit=10)
        items = res_list["items"]
        item1 = next(item for item in items if item["id"] == note1["id"])
        assert item1["source_host"] == "desktop-host"
        item2 = next(item for item in items if item["id"] == note2["id"])
        assert item2["source_host"] == "laptop-mac"

        # Test memory_search filters and case-insensitivity
        res_search_desktop = memory_search("note body", source_host="desktop-host")["results"]
        assert any(r["id"] == note1["id"] for r in res_search_desktop)
        assert all(r["id"] != note2["id"] for r in res_search_desktop)

        # Case insensitive test
        res_search_desktop_ci = memory_search("note body", source_host="desktop-host")["results"]
        assert any(r["id"] == note1["id"] for r in res_search_desktop_ci)
        assert all(r["id"] != note2["id"] for r in res_search_desktop_ci)

        # Test memory_search returns source_host field
        assert res_search_desktop[0]["source_host"] == "desktop-host"

        # 2. Task paths
        monkeypatch.setenv("UA_SOURCE_HOST", "desktop-host")
        task1 = task_add("desktop task")

        monkeypatch.setenv("UA_SOURCE_HOST", "laptop-mac")
        task2 = task_add("laptop task")

        # Test task_list filters and case-insensitivity
        desktop_tasks = task_list(status="all", source_host="desktop-host")["tasks"]
        assert any(t["id"] == task1["id"] for t in desktop_tasks)
        assert all(t["id"] != task2["id"] for t in desktop_tasks)

        desktop_tasks_ci = task_list(status="all", source_host="desktop-host")["tasks"]
        assert any(t["id"] == task1["id"] for t in desktop_tasks_ci)
        assert all(t["id"] != task2["id"] for t in desktop_tasks_ci)

        # Test task_list / task_update results contain source_host
        item_t1 = next(t for t in desktop_tasks if t["id"] == task1["id"])
        assert item_t1["source_host"] == "desktop-host"
        res_update = task_update(task1["id"], status="doing")
        assert res_update["source_host"] == "desktop-host"


class TestTasks:
    def test_archive_is_typed_and_reversible_without_status_backfill(self):
        task = task_add("historical task")

        archived = task_update(task["id"], archived=True)
        assert archived["archived_at"] is not None
        assert task_list(status="all")["tasks"] == []
        assert task_list(status="all", archived=True)["tasks"][0]["id"] == task["id"]

        reopened = task_update(task["id"], archived=False)
        assert reopened["archived_at"] is None
        assert task_list(status="all")["tasks"][0]["id"] == task["id"]

    def test_add_update_complete(self):
        t = task_add("ship v1", priority="high")
        row = task_update(t["id"], status="doing")
        assert row["status"] == "doing"
        assert row["completed_at"] is None
        row = task_update(t["id"], status="done", actor="test-agent")
        assert row["completed_at"] is not None

    def test_list_ordering_and_filter(self):
        task_add("low prio", priority="low")
        task_add("high prio", priority="high")
        t = task_add("done one")
        task_update(t["id"], status="done", actor="test-agent")
        open_tasks = task_list()["tasks"]
        assert [x["title"] for x in open_tasks] == ["high prio", "low prio"]
        assert [x["title"] for x in task_list(status="done")["tasks"]] == ["done one"]
        assert len(task_list(status="all")["tasks"]) == 3

    def test_subtasks_and_cascade_delete(self):
        parent = task_add("epic")
        task_add("child 1", parent_id=parent["id"])
        task_add("child 2", parent_id=parent["id"])
        subs = task_list(parent_id=parent["id"])["tasks"]
        assert len(subs) == 2
        out = task_delete(parent["id"])
        # No task here was ever claimed, so none has a worktree to release.
        assert out == {"deleted": True, "children_deleted": 2, "worktrees": {}}
        assert task_list(status="all")["tasks"] == []

    def test_bad_parent(self):
        with pytest.raises(ToolError, match="no task with id"):
            task_add("orphan", parent_id=999)

    def test_bad_status(self):
        t = task_add("x")
        with pytest.raises(ToolError, match="status must be"):
            task_update(t["id"], status="wontfix")


class TestTaskReparent:
    """task_update parent_id — reparent/detach after creation (task #80.3)."""

    def test_reparent_and_detach(self):
        parent = task_add("epic")
        child = task_add("stray")
        row = task_update(child["id"], parent_id=parent["id"])
        assert row["parent_id"] == parent["id"]
        assert [t["id"] for t in task_list(parent_id=parent["id"])["tasks"]] == [child["id"]]
        row = task_update(child["id"], parent_id=0)
        assert row["parent_id"] is None
        assert task_list(parent_id=parent["id"])["tasks"] == []

    def test_omitted_parent_id_leaves_parent_unchanged(self):
        parent = task_add("epic")
        child = task_add("sub", parent_id=parent["id"])
        row = task_update(child["id"], status="doing")
        assert row["parent_id"] == parent["id"]

    def test_reparent_validation(self):
        parent = task_add("epic")
        child = task_add("sub", parent_id=parent["id"])
        lone = task_add("lone")
        with pytest.raises(ToolError, match="own parent"):
            task_update(lone["id"], parent_id=lone["id"])
        with pytest.raises(ToolError, match="no task with id 999"):
            task_update(lone["id"], parent_id=999)
        # Multi-level: a subtask can itself be a parent.
        row = task_update(lone["id"], parent_id=child["id"])
        assert row["parent_id"] == child["id"]
        # A task with children may move as a subtree under a different root.
        other = task_add("other-epic")
        row = task_update(parent["id"], parent_id=other["id"])
        assert row["parent_id"] == other["id"]
        # Moving a root under one of its descendants is a cycle.
        with pytest.raises(ToolError, match="cycle"):
            task_update(other["id"], parent_id=child["id"])


class TestTaskNesting:
    """Multi-level parent_id, lineage, cycle prevention, depth cap (task #830)."""

    def test_add_grandchild_and_list_lineage(self):
        root = task_add("root")
        step = task_add("step", parent_id=root["id"])
        sub = task_add("substep", parent_id=step["id"])
        kids = task_list(parent_id=step["id"], status="all")["tasks"]
        assert [t["id"] for t in kids] == [sub["id"]]
        assert kids[0]["lineage"] == [root["id"], step["id"]]
        assert kids[0]["parent_id"] == step["id"]
        roots = task_list(status="all")["tasks"]
        by_id = {t["id"]: t for t in roots}
        assert by_id[root["id"]]["lineage"] == []
        assert by_id[step["id"]]["lineage"] == [root["id"]]
        compact = {
            t["id"]: t
            for t in task_list(parent_id=step["id"], status="all", fields="compact")["tasks"]
        }
        assert compact[sub["id"]]["lineage"] == [root["id"], step["id"]]

    def test_one_level_parents_still_work(self):
        parent = task_add("epic")
        child = task_add("sub", parent_id=parent["id"])
        listed = task_list(parent_id=parent["id"], status="all")["tasks"]
        assert [t["id"] for t in listed] == [child["id"]]
        assert listed[0]["lineage"] == [parent["id"]]

    def test_reparent_cycle_rejected(self):
        a = task_add("a")
        b = task_add("b", parent_id=a["id"])
        c = task_add("c", parent_id=b["id"])
        with pytest.raises(ToolError, match="cycle"):
            task_update(a["id"], parent_id=c["id"])
        with pytest.raises(ToolError, match="cycle"):
            task_update(a["id"], parent_id=b["id"])
        # Unchanged on failure.
        assert task_list(status="all")["tasks"]
        by_id = {t["id"]: t for t in task_list(status="all")["tasks"]}
        assert by_id[a["id"]]["parent_id"] is None
        assert by_id[b["id"]]["parent_id"] == a["id"]
        assert by_id[c["id"]]["parent_id"] == b["id"]

    def test_depth_cap_on_add_and_reparent(self):
        from unified_agent.tools.tasks import TASK_MAX_DEPTH

        cursor = task_add("d0")
        chain = [cursor["id"]]
        for i in range(TASK_MAX_DEPTH):
            cursor = task_add(f"d{i + 1}", parent_id=cursor["id"])
            chain.append(cursor["id"])
        with pytest.raises(ToolError, match="nesting depth cap"):
            task_add("too-deep", parent_id=chain[-1])
        stray = task_add("stray")
        with pytest.raises(ToolError, match="nesting depth cap"):
            task_update(stray["id"], parent_id=chain[-1])
        # Reparenting a non-empty subtree that would push a leaf past the cap.
        leaf_parent = task_add("leaf-parent")
        task_add("leaf", parent_id=leaf_parent["id"])
        with pytest.raises(ToolError, match="nesting depth cap"):
            task_update(leaf_parent["id"], parent_id=chain[-2])

    def test_cascade_delete_counts_nested_descendants(self):
        root = task_add("root")
        mid = task_add("mid", parent_id=root["id"])
        task_add("leaf", parent_id=mid["id"])
        out = task_delete(root["id"])
        assert out["deleted"] is True
        assert out["children_deleted"] == 2
        assert task_list(status="all")["tasks"] == []

    def test_reparent_under_untrusted_parent_taints_entire_subtree(self):
        untrusted_root = task_add("untrusted root", untrusted=True)
        subtree = task_add("subtree")
        child = task_add("child", parent_id=subtree["id"])
        leaf = task_add("leaf", parent_id=child["id"])

        task_update(subtree["id"], parent_id=untrusted_root["id"])

        rows = {row["id"]: row for row in task_list(status="all")["tasks"]}
        assert rows[subtree["id"]]["untrusted"] == 1
        assert rows[child["id"]]["untrusted"] == 1
        assert rows[leaf["id"]]["untrusted"] == 1

    def test_marking_parent_untrusted_taints_existing_descendants(self):
        root = task_add("root")
        child = task_add("child", parent_id=root["id"])
        task_update(root["id"], untrusted=True)

        rows = {row["id"]: row for row in task_list(status="all")["tasks"]}
        assert rows[root["id"]]["untrusted"] == 1
        assert rows[child["id"]]["untrusted"] == 1

    @pytest.mark.parametrize("shape", ["same_second", "microsecond_ahead"])
    def test_taint_gives_descendants_a_strictly_newer_revision(self, monkeypatch, shape):
        """Task #1818: the taint step bumps each descendant's own revision, so a
        stale expected_updated_at on a descendant is refused afterwards."""
        root = task_add("root")
        child = task_add("child", parent_id=root["id"])
        task_update(child["id"], notes="touch")  # a microsecond-precision revision

        def _set_revision(revision: str) -> None:
            conn = storage.connect()
            try:
                conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (revision, child["id"]))
                conn.commit()
            finally:
                conn.close()

        def _child_row() -> dict:
            return next(t for t in task_list(status="all")["tasks"] if t["id"] == child["id"])

        if shape == "same_second":
            # The taint lands in the same second as a second-precision revision.
            frozen = storage.now()
            monkeypatch.setattr(storage, "now", lambda: frozen)
            _set_revision(frozen)
        else:
            ahead = (datetime.now(UTC) + timedelta(seconds=5)).isoformat(timespec="microseconds")
            _set_revision(ahead)
        stale = _child_row()["updated_at"]

        task_update(root["id"], untrusted=True)

        tainted = _child_row()
        assert tainted["untrusted"] == 1
        assert datetime.fromisoformat(tainted["updated_at"]) > datetime.fromisoformat(stale)
        with pytest.raises(ToolError, match="expected_updated_at|revision"):
            task_update(child["id"], notes="stale edit", expected_updated_at=stale)

    def test_cancelled_root_prevents_nested_descendant_claim(self):
        root = task_add("root")
        child = task_add("child", parent_id=root["id"])
        leaf = task_add("leaf", parent_id=child["id"])
        task_update(root["id"], status="cancelled")

        with pytest.raises(ToolError, match=f"cancelled ancestor {root['id']}"):
            task_claim(leaf["id"], "test-agent")

        rows = {row["id"]: row for row in task_list(status="all")["tasks"]}
        assert rows[leaf["id"]]["status"] == "open"
        assert rows[leaf["id"]]["claimed_by"] is None


class TestTaskListCompact:
    def test_compact_drops_notes_and_adds_summary_fields(self):
        long_notes = "claimed-by: Desktop_Agent1 @ 2026-07-19\n" + "x" * 5000
        t = task_add("big task", notes=long_notes)
        unclaimed = task_add("no notes task")

        rows = task_list(fields="compact")["tasks"]
        by_id = {r["id"]: r for r in rows}
        big = by_id[t["id"]]
        assert "notes" not in big
        assert big["notes_chars"] == len(long_notes)
        # Note prose is descriptive history; only task_claim populates ownership.
        assert big["claimed_by"] is None
        assert big["title"] == "big task"  # non-notes columns intact
        assert big["status"] == "open"

        bare = by_id[unclaimed["id"]]
        assert bare["notes_chars"] == 0
        assert bare["claimed_by"] is None

    def test_claimed_by_placeholder_markup_is_unclaimed(self):
        """'claimed-by: [waiting-for: dev-claim]' is template markup, not an
        agent label — claimed_by must be None, not '[waiting-for:'."""
        t = task_add("templated", notes="claimed-by: [waiting-for: dev-claim]")
        rows = task_list(fields="compact")["tasks"]
        assert next(r for r in rows if r["id"] == t["id"])["claimed_by"] is None

    def test_full_remains_default_and_unchanged(self):
        task_add("t", notes="some notes")
        rows = task_list()["tasks"]
        assert rows[0]["notes"] == "some notes"
        assert "notes_chars" not in rows[0]

    def test_compact_composes_with_filters(self):
        task_add("hi", notes="[waiting-for: review]", priority="high")
        task_add("lo", priority="low")
        rows = task_list(fields="compact", notes_contain="[waiting-for: review]")["tasks"]
        assert [r["title"] for r in rows] == ["hi"]

    def test_invalid_fields_rejected(self):
        with pytest.raises(ToolError, match="fields must be"):
            task_list(fields="minimal")
