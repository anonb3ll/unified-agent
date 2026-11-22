"""Chunked embeddings for long memory notes (task #2182)."""

import sqlite3
import struct
from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, edition, embeddings, memory_chunks, storage, table_schemas
from unified_agent.embeddings import embedding_to_bytes, generate_embedding
from unified_agent.tools.memory import (
    _semantic_similarity_rows,
    memory_delete,
    memory_restore,
    memory_save,
    memory_search,
)

VECTOR = struct.pack("384f", 1.0, *([0.0] * 383))
OTHER = struct.pack("384f", 0.0, 1.0, *([0.0] * 382))


@contextmanager
def _identity(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


def _one_hot(text: str) -> list[float]:
    slot = sum(text.encode()) % storage.MEMORY_VEC_DIM
    vector = [0.0] * storage.MEMORY_VEC_DIM
    vector[slot] = 1.0
    return vector


def _patch_embeddings(monkeypatch):
    monkeypatch.setattr(
        embeddings, "generate_embeddings", lambda texts: [_one_hot(text) for text in texts]
    )


def _long_text() -> str:
    return "word " * 800  # 4,000 characters, past the single-vector window


def _insert_note(conn, note_id: str, *, org_id=None, content: str = "body") -> None:
    stamp = storage.now()
    conn.execute(
        "INSERT INTO memory (id, title, content, tags, created_at, updated_at, org_id) "
        "VALUES (?, ?, ?, '[]', ?, ?, ?)",
        (note_id, note_id, content, stamp, stamp, org_id),
    )


def _chunk_count(conn, note_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM memory_chunks WHERE note_id=?", (note_id,)
    ).fetchone()[0]


def test_chunk_text_keeps_short_notes_whole():
    assert memory_chunks.chunk_text("a" * memory_chunks.SINGLE_VECTOR_MAX_CHARS) == []
    assert memory_chunks.chunk_text("") == []


def test_chunk_text_overlaps_and_covers_a_medium_note():
    text = "a" * 10_000
    chunks = memory_chunks.chunk_text(text)
    assert 2 <= len(chunks) <= memory_chunks.MAX_CHUNKS
    assert chunks[0] == text[: memory_chunks.CHUNK_CHARS]
    assert text.endswith(chunks[-1])
    # The second window starts one overlap before the first cut.
    assert chunks[1].startswith(text[memory_chunks.CHUNK_CHARS - memory_chunks.OVERLAP_CHARS])


def test_chunk_text_snaps_to_a_word_and_caps_the_tail():
    words = memory_chunks.chunk_text("word " * 2_000)
    assert words
    assert all(piece.split()[-1] == "word" for piece in words)

    text = ("a" * 40_000) + "UNIQUE_TAIL"
    capped = memory_chunks.chunk_text(text)
    assert len(capped) == memory_chunks.MAX_CHUNKS
    assert "UNIQUE_TAIL" not in "".join(capped)


def test_flag_parses_on_off_and_falls_back(monkeypatch):
    monkeypatch.delenv("UA_MEMORY_CHUNK_EMBEDDINGS", raising=False)
    assert config.memory_chunk_embeddings() is config.DEFAULT_MEMORY_CHUNK_EMBEDDINGS
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "yes")
    assert config.memory_chunk_embeddings() is True
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "off")
    assert config.memory_chunk_embeddings() is False
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "maybe")
    assert config.memory_chunk_embeddings() is config.DEFAULT_MEMORY_CHUNK_EMBEDDINGS


def test_schema_is_idempotent_and_does_not_bump_the_version(isolated_env):
    conn = storage.connect()
    try:
        table_schemas.memory_chunks(conn)
        table_schemas.memory_chunks(conn)
        conn.commit()
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert version == str(storage.SCHEMA_VERSION)
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'memory_chunk%'"
            )
        }
        assert "memory_chunks" in names
        assert "memory_chunk_vec" in names
    finally:
        conn.close()


def test_org_fence_rejects_a_foreign_org_id(isolated_env):
    conn = storage.connect()
    try:
        _insert_note(conn, "note-a", org_id=None)
        with pytest.raises(sqlite3.IntegrityError, match="org_id"):
            conn.execute(
                "INSERT INTO memory_chunks (note_id, chunk_index, embedding, model, org_id) "
                "VALUES ('note-a', 0, ?, 'test', 'other')",
                (VECTOR,),
            )
        conn.rollback()
    finally:
        conn.close()


def test_short_note_stays_on_the_single_vector(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    _patch_embeddings(monkeypatch)
    saved = memory_save("a short note about deploys", title="Deploys", author="Desktop_Agent4")
    conn = storage.connect()
    try:
        assert _chunk_count(conn, saved["id"]) == 0
        assert conn.execute(
            "SELECT 1 FROM memory_embeddings WHERE note_id=?", (saved["id"],)
        ).fetchone()
    finally:
        conn.close()


def test_long_note_is_chunked_only_when_the_flag_is_on(isolated_env, monkeypatch):
    _patch_embeddings(monkeypatch)
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
    off = memory_save(_long_text(), title="Off", author="Desktop_Agent4")
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    on = memory_save(_long_text(), title="On", author="Desktop_Agent4")
    conn = storage.connect()
    try:
        assert _chunk_count(conn, off["id"]) == 0
        count = _chunk_count(conn, on["id"])
        assert 2 <= count <= memory_chunks.MAX_CHUNKS
        mirrored = conn.execute(
            "SELECT COUNT(*) FROM memory_chunk_vec v "
            "JOIN memory_chunks c ON c.rowid = v.rowid WHERE c.note_id=?",
            (on["id"],),
        ).fetchone()[0]
        assert mirrored == count
    finally:
        conn.close()


def test_score_uses_the_best_chunk(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    conn = storage.connect()
    try:
        _insert_note(conn, "long-note", content="x" * 3000)
        conn.execute(
            "INSERT INTO memory_embeddings (note_id, embedding, model) VALUES ('long-note', ?, ?)",
            (VECTOR, config.embed_model()),
        )
        rowid = conn.execute("SELECT rowid FROM memory WHERE id='long-note'").fetchone()[0]
        conn.execute("INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)", (rowid, VECTOR))
        memory_chunks.write_chunks(conn, "long-note", [OTHER])
        conn.commit()

        scored = {
            row["note_id"]: row["similarity"] for row in _semantic_similarity_rows(conn, OTHER)
        }
        assert scored["long-note"] == pytest.approx(1.0, abs=1e-4)

        monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
        prefix_only = {
            row["note_id"]: row["similarity"] for row in _semantic_similarity_rows(conn, OTHER)
        }
        assert prefix_only["long-note"] == pytest.approx(0.0, abs=1e-4)
    finally:
        conn.close()


def test_chunks_stay_inside_the_tenant(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    conn = storage.connect()
    try:
        _insert_note(conn, "ours", org_id=None, content="x" * 3000)
        _insert_note(conn, "theirs", org_id="other", content="y" * 3000)
        memory_chunks.write_chunks(conn, "ours", [OTHER])
        memory_chunks.write_chunks(conn, "theirs", [OTHER])
        conn.commit()
        ids = {row["note_id"] for row in _semantic_similarity_rows(conn, OTHER)}
        assert "ours" in ids
        assert "theirs" not in ids

        monkeypatch.setattr(edition, "tenant_org_id", lambda: "other")
        ids = {row["note_id"] for row in _semantic_similarity_rows(conn, OTHER)}
        assert ids == {"theirs"}
    finally:
        conn.close()


def test_supersede_delete_and_restore_keep_chunks_with_the_note(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    _patch_embeddings(monkeypatch)
    with _identity("Desktop_Agent4"):
        original = memory_save(_long_text(), title="Original", author="Desktop_Agent4")
        conn = storage.connect()
        try:
            assert _chunk_count(conn, original["id"]) >= 2
        finally:
            conn.close()
        replacement = memory_save(
            "the replacement fact",
            title="Replacement",
            author="Desktop_Agent4",
            supersedes=original["id"],
        )
        conn = storage.connect()
        try:
            assert _chunk_count(conn, original["id"]) >= 2
            assert (
                conn.execute(
                    "SELECT superseded_by FROM memory WHERE id=?", (original["id"],)
                ).fetchone()[0]
                == replacement["id"]
            )
        finally:
            conn.close()
        memory_restore(original["id"])
        conn = storage.connect()
        try:
            assert _chunk_count(conn, original["id"]) >= 2
        finally:
            conn.close()
        assert memory_delete(original["id"])["deleted"] is True
        conn = storage.connect()
        try:
            assert _chunk_count(conn, original["id"]) == 0
        finally:
            conn.close()


def test_consolidate_restore_rechunks_a_demoted_long_note(isolated_env, monkeypatch):
    """apply_supersede keeps chunks; restore leaves them in place."""
    from unified_agent.consolidate import apply_supersede, restore_note

    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    _patch_embeddings(monkeypatch)
    with _identity("Desktop_Agent4"):
        original = memory_save(_long_text(), title="Original", author="Desktop_Agent4")
        survivor = memory_save("the survivor fact", title="Survivor", author="Desktop_Agent4")
        apply_supersede(survivor["id"], [original["id"]], actor="Desktop_Agent4")
        conn = storage.connect()
        try:
            assert _chunk_count(conn, original["id"]) >= 2
        finally:
            conn.close()
        restore_note(original["id"])
        conn = storage.connect()
        try:
            assert _chunk_count(conn, original["id"]) >= 2
        finally:
            conn.close()


_LATE_FACT = "ZEBRAQUILLFACT"
_LATE_QUERY = "xylophonequery"
_AS_OF_BEFORE = "2026-06-01T00:00:00+00:00"


def _floats(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{storage.MEMORY_VEC_DIM}f", blob))


def _backdate(note_id: str, stamp: str = "2026-01-01T00:00:00+00:00") -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE memory SET created_at=?, updated_at=? WHERE id=?",
            (stamp, stamp, note_id),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("path", ["memory_save", "apply_supersede"])
def test_as_of_search_still_scores_a_late_fact_after_supersede(isolated_env, monkeypatch, path):
    """A fact past the single-vector window stays findable at an earlier as_of.

    Default search still drops the superseded note. The query shares no tokens
    with the note, so only the chunk vector can return it.
    """
    from unified_agent.consolidate import apply_supersede

    prefix = _floats(VECTOR)
    late = _floats(OTHER)

    def _opening(text: str) -> list[float]:
        if text == _LATE_QUERY:
            return late
        head = text[: memory_chunks.SINGLE_VECTOR_MAX_CHARS]
        if _LATE_FACT in head:
            return late
        return prefix

    def _chunks(texts: list[str]) -> list[list[float]]:
        return [late if _LATE_FACT in text else prefix for text in texts]

    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    monkeypatch.setattr(embeddings, "generate_embedding", _opening)
    monkeypatch.setattr(embeddings, "generate_embeddings", _chunks)

    title = "Opening"
    content = ("word " * 600) + _LATE_FACT
    source = memory_chunks.embed_source(title, content)
    assert source.find(_LATE_FACT) > memory_chunks.SINGLE_VECTOR_MAX_CHARS
    assert _LATE_QUERY not in source

    with _identity("note-owner"):
        original = memory_save(content, title=title, author="note-owner")
        _backdate(original["id"])
        if path == "memory_save":
            memory_save(
                "replacement stays in the opening",
                title="Replacement",
                author="note-owner",
                supersedes=original["id"],
            )
        else:
            survivor = memory_save(
                "survivor stays in the opening",
                title="Survivor",
                author="note-owner",
            )
            apply_supersede(survivor["id"], [original["id"]], actor="note-owner")

    conn = storage.connect()
    try:
        assert _chunk_count(conn, original["id"]) >= 2
        stored = conn.execute(
            "SELECT embedding FROM memory_embeddings WHERE note_id=?", (original["id"],)
        ).fetchone()[0]
        assert stored == embedding_to_bytes(prefix)
        windows = [
            row[0]
            for row in conn.execute(
                "SELECT embedding FROM memory_chunks WHERE note_id=?", (original["id"],)
            )
        ]
        assert embedding_to_bytes(late) in windows
    finally:
        conn.close()

    historical = [
        hit
        for hit in memory_search(_LATE_QUERY, as_of=_AS_OF_BEFORE)["results"]
        if hit["id"] == original["id"]
    ]
    assert len(historical) == 1
    assert "semantic" in historical[0]["explanation"]["why"]
    assert "lexical" not in historical[0]["explanation"]["why"]
    assert original["id"] not in [hit["id"] for hit in memory_search(_LATE_QUERY)["results"]]


def test_content_edit_while_chunking_is_off_drops_stale_chunks(isolated_env, monkeypatch):
    """A text edit with the flag off must not leave the previous windows to score."""
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    _patch_embeddings(monkeypatch)
    revised = "other " * 800
    with _identity("Desktop_Agent4"):
        saved = memory_save(_long_text(), title="Original", author="Desktop_Agent4")
        monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
        memory_save(revised, title="Edited", author="Desktop_Agent4", id=saved["id"])
        conn = storage.connect()
        try:
            assert _chunk_count(conn, saved["id"]) == 0
        finally:
            conn.close()
        monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
        memory_chunks.backfill_memory_chunks()
        conn = storage.connect()
        try:
            stored = [
                row[0]
                for row in conn.execute(
                    "SELECT embedding FROM memory_chunks WHERE note_id=? ORDER BY chunk_index",
                    (saved["id"],),
                )
            ]
        finally:
            conn.close()
    assert stored == memory_chunks.embed_chunk_vectors("Edited", revised)
    assert len(stored) >= 2


def _chunk_blobs(note_id: str) -> list[bytes]:
    conn = storage.connect()
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT embedding FROM memory_chunks WHERE note_id=? ORDER BY chunk_index",
                (note_id,),
            )
        ]
    finally:
        conn.close()


@pytest.mark.parametrize("chunking", ["1", "0"])
def test_failed_chunk_delete_refuses_the_text_edit(isolated_env, monkeypatch, chunking):
    """Task #2184: a chunk delete that fails must not commit the new text.

    Repair and backfill skip any note that already has chunks, so a committed
    edit over the old windows would keep scoring the old text forever. The edit
    is refused instead, and the note, its vector and its chunks stay together.
    """
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    _patch_embeddings(monkeypatch)
    original = _long_text()
    with _identity("Desktop_Agent4"):
        saved = memory_save(original, title="Original", author="Desktop_Agent4")
        before = _chunk_blobs(saved["id"])
        assert len(before) >= 2

        working_delete = storage.delete_memory_chunks

        def broken_delete(conn, note_id):
            raise sqlite3.OperationalError("vec0 mirror delete failed")

        monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", chunking)
        monkeypatch.setattr(storage, "delete_memory_chunks", broken_delete)
        with pytest.raises(ToolError, match="chunk"):
            memory_save("other " * 800, title="Edited", author="Desktop_Agent4", id=saved["id"])

        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT title, content FROM memory WHERE id=?", (saved["id"],)
            ).fetchone()
            vector = conn.execute(
                "SELECT 1 FROM memory_embeddings WHERE note_id=?", (saved["id"],)
            ).fetchone()
        finally:
            conn.close()
        assert (row["title"], row["content"]) == ("Original", original)
        assert vector is not None  # the invalidation rolled back with the edit
        assert _chunk_blobs(saved["id"]) == before

        # Once the delete works again, the same edit lands with fresh windows.
        monkeypatch.setattr(storage, "delete_memory_chunks", working_delete)
        monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
        revised = "other " * 800
        memory_save(revised, title="Edited", author="Desktop_Agent4", id=saved["id"])
    assert _chunk_blobs(saved["id"]) == memory_chunks.embed_chunk_vectors("Edited", revised)


def test_failed_chunk_delete_refuses_an_append_edit(isolated_env, monkeypatch):
    """Task #2193: an append changes the text too, so it takes the same fail-closed path."""
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    _patch_embeddings(monkeypatch)
    original = _long_text()
    with _identity("Desktop_Agent4"):
        saved = memory_save(original, title="Original", author="Desktop_Agent4")
        before = _chunk_blobs(saved["id"])
        assert len(before) >= 2

        def broken_delete(conn, note_id):
            raise sqlite3.OperationalError("no such module: vec0")

        monkeypatch.setattr(storage, "delete_memory_chunks", broken_delete)
        with pytest.raises(ToolError, match="could not drop the old chunk vectors"):
            memory_save("appended " * 50, author="Desktop_Agent4", id=saved["id"], append=True)

        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT title, content FROM memory WHERE id=?", (saved["id"],)
            ).fetchone()
        finally:
            conn.close()
        assert (row["title"], row["content"]) == ("Original", original)
        assert _chunk_blobs(saved["id"]) == before


def test_metadata_edit_while_chunking_is_off_keeps_current_chunks(isolated_env, monkeypatch):
    """A tag change is not a text change, so the windows stay until the text does."""
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    _patch_embeddings(monkeypatch)
    text = _long_text()
    with _identity("Desktop_Agent4"):
        saved = memory_save(text, title="Original", author="Desktop_Agent4")
        conn = storage.connect()
        try:
            before = [
                row[0]
                for row in conn.execute(
                    "SELECT embedding FROM memory_chunks WHERE note_id=? ORDER BY chunk_index",
                    (saved["id"],),
                )
            ]
        finally:
            conn.close()
        monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
        memory_save(text, title="Original", author="Desktop_Agent4", id=saved["id"], tags=["kept"])
        conn = storage.connect()
        try:
            after = [
                row[0]
                for row in conn.execute(
                    "SELECT embedding FROM memory_chunks WHERE note_id=? ORDER BY chunk_index",
                    (saved["id"],),
                )
            ]
        finally:
            conn.close()
    assert after == before
    assert len(after) >= 2


def test_chunk_missing_from_the_mirror_is_still_scored(isolated_env, monkeypatch):
    """A chunk blob with no vec0 row is scored on the UDF path, not dropped."""
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    conn = storage.connect()
    try:
        _insert_note(conn, "lagging", content="x" * 3000)
        _insert_note(conn, "present", content="y" * 3000)
        for note_id in ("lagging", "present"):
            conn.execute(
                "INSERT INTO memory_embeddings (note_id, embedding, model) VALUES (?, ?, ?)",
                (note_id, VECTOR, config.embed_model()),
            )
            rowid = conn.execute("SELECT rowid FROM memory WHERE id=?", (note_id,)).fetchone()[0]
            conn.execute("INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)", (rowid, VECTOR))
        memory_chunks.write_chunks(conn, "lagging", [OTHER])
        memory_chunks.write_chunks(conn, "present", [OTHER])
        conn.execute(
            "DELETE FROM memory_chunk_vec WHERE rowid IN "
            "(SELECT rowid FROM memory_chunks WHERE note_id='lagging')"
        )
        conn.commit()
        scored = {
            row["note_id"]: row["similarity"] for row in _semantic_similarity_rows(conn, OTHER)
        }
        assert scored["lagging"] == pytest.approx(1.0, abs=1e-4)
        assert scored["present"] == pytest.approx(1.0, abs=1e-4)
    finally:
        conn.close()


def test_hard_delete_drops_the_chunk_mirror_before_rowid_reuse(isolated_env):
    conn = storage.connect()
    try:
        _insert_note(conn, "gone", content="x" * 3000)
        memory_chunks.write_chunks(conn, "gone", [VECTOR])
        old_rowid = conn.execute("SELECT rowid FROM memory_chunks WHERE note_id='gone'").fetchone()[
            0
        ]
        assert storage.hard_delete_memory(conn, "gone", org_id=None)
        assert (
            conn.execute("SELECT 1 FROM memory_chunk_vec WHERE rowid=?", (old_rowid,)).fetchone()
            is None
        )
        _insert_note(conn, "next", content="y" * 3000)
        memory_chunks.write_chunks(conn, "next", [OTHER])
        new_rowid = conn.execute("SELECT rowid FROM memory_chunks WHERE note_id='next'").fetchone()[
            0
        ]
        mirrored = conn.execute(
            "SELECT embedding FROM memory_chunk_vec WHERE rowid=?", (new_rowid,)
        ).fetchone()
        assert mirrored is not None
        assert mirrored[0] == OTHER
        leftover = [
            row[0]
            for row in conn.execute(
                "SELECT v.embedding FROM memory_chunk_vec v "
                "JOIN memory_chunks c ON c.rowid = v.rowid WHERE c.note_id='next'"
            )
        ]
        assert leftover == [OTHER]
    finally:
        conn.close()


def test_orphan_chunk_mirror_rows_are_repaired(isolated_env):
    conn = storage.connect()
    try:
        _insert_note(conn, "keep", content="x" * 3000)
        memory_chunks.write_chunks(conn, "keep", [VECTOR])
        conn.execute("INSERT INTO memory_chunk_vec (rowid, embedding) VALUES (999999, ?)", (OTHER,))
        conn.commit()
        preview = storage.repair_memory_orphans(conn)
        assert preview["memory_chunk_vec"] == 1
        assert preview["applied"] is False
        assert storage.repair_memory_orphans(conn, apply=True)["memory_chunk_vec"] == 1
        conn.commit()
        assert storage.repair_memory_orphans(conn)["memory_chunk_vec"] == 0
        assert conn.execute("SELECT 1 FROM memory_chunks WHERE note_id='keep'").fetchone()
    finally:
        conn.close()


def test_backfill_is_bounded_and_idempotent(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    calls = {"n": 0}

    def _count(texts):
        calls["n"] += 1
        return [_one_hot(text) for text in texts]

    monkeypatch.setattr(embeddings, "generate_embeddings", _count)
    conn = storage.connect()
    try:
        _insert_note(conn, "old-long", content=_long_text())
        _insert_note(conn, "old-short", content="short")
        _insert_note(conn, "old-gone", content=_long_text())
        conn.execute(
            "UPDATE memory SET tags=? WHERE id='old-gone'",
            ('["meta:deleted"]',),
        )
        conn.commit()
    finally:
        conn.close()

    memory_chunks.backfill_memory_chunks()
    conn = storage.connect()
    try:
        assert _chunk_count(conn, "old-long") >= 2
        assert _chunk_count(conn, "old-short") == 0
        assert _chunk_count(conn, "old-gone") == 0
    finally:
        conn.close()
    first = calls["n"]
    assert first >= 1
    memory_chunks.backfill_memory_chunks()
    assert calls["n"] == first


@pytest.mark.embeddings
def test_real_model_scores_a_tail_above_the_truncated_prefix(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "1")
    secret = "the passphrase I asked you to remember is purple narwhal ledger"
    filler = [
        f"user: on day {i} I went to the market and bought ordinary groceries item {i}."
        for i in range(80)
    ]
    saved = memory_save(
        "\n\n".join(filler + [f"user: {secret}."]),
        title="Session",
        author="Desktop_Agent4",
    )
    query = embedding_to_bytes(generate_embedding(secret))
    conn = storage.connect()
    try:
        with_chunks = {
            row["note_id"]: row["similarity"] for row in _semantic_similarity_rows(conn, query)
        }
        storage.delete_memory_chunks(conn, saved["id"])
        conn.commit()
        monkeypatch.setenv("UA_MEMORY_CHUNK_EMBEDDINGS", "0")
        prefix_only = {
            row["note_id"]: row["similarity"] for row in _semantic_similarity_rows(conn, query)
        }
    finally:
        conn.close()
    assert with_chunks[saved["id"]] > prefix_only[saved["id"]]
    assert with_chunks[saved["id"]] >= 0.65
