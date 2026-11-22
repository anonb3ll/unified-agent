"""Tests for the sqlite-vec accelerated KNN path (System Leverage Plan T1-2).

memory_search's semantic step and memory_save's dedup check both go through
memory.py's `_semantic_similarity_rows`, which tries the sqlite-vec `memory_vec`
virtual table first and falls back to the original per-row `cosine_similarity()`
UDF scan over `memory_embeddings` on any failure. `memory_embeddings` stays the
source of truth throughout — these tests exist to prove the accelerated path
returns the *same* results as the fallback, and that the fallback genuinely
engages when `memory_vec` is unusable, not just when it's empty.
"""

import pytest

from unified_agent import memory_semantic, storage
from unified_agent.embeddings import embedding_to_bytes, generate_embedding
from unified_agent.tools.memory import (
    _semantic_similarity_rows,
    memory_save,
    memory_search,
)


@pytest.mark.embeddings
def test_upsert_memory_vec_inserts_then_updates(isolated_env):
    n = memory_save("insert-then-update probe", title="Probe")
    conn = storage.connect()
    try:
        row = conn.execute("SELECT rowid FROM memory WHERE id=?", (n["id"],)).fetchone()
        rowid = row["rowid"]

        # Freshly saved via memory_save/_store_embedding: already present.
        before = conn.execute("SELECT embedding FROM memory_vec WHERE rowid=?", (rowid,)).fetchone()
        assert before is not None

        # A second upsert with a different vector must UPDATE in place, not
        # raise a UNIQUE-constraint error (vec0 doesn't support INSERT OR
        # REPLACE — see storage.upsert_memory_vec's docstring).
        new_bytes = embedding_to_bytes(generate_embedding("a completely different sentence"))
        storage.upsert_memory_vec(conn, n["id"], new_bytes)
        conn.commit()
        after = conn.execute("SELECT embedding FROM memory_vec WHERE rowid=?", (rowid,)).fetchone()
        assert after["embedding"] == new_bytes
        assert after["embedding"] != before["embedding"]
    finally:
        conn.close()


def test_upsert_memory_vec_is_a_noop_for_unknown_note(isolated_env):
    conn = storage.connect()
    try:
        # Must not raise even though 'does-not-exist' has no memory row.
        storage.upsert_memory_vec(conn, "does-not-exist", b"\x00" * (storage.MEMORY_VEC_DIM * 4))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.embeddings
def test_memory_save_mirrors_into_memory_vec(isolated_env):
    n1 = memory_save("git worktrees let you check out two branches at once", title="Worktrees")
    n2 = memory_save("javascript heap memory leaks from forgotten intervals", title="Leaks")

    conn = storage.connect()
    try:
        embeddings_ids = {
            r["note_id"] for r in conn.execute("SELECT note_id FROM memory_embeddings").fetchall()
        }
        vec_ids = {
            r["id"]
            for r in conn.execute(
                "SELECT m.id FROM memory_vec v JOIN memory m ON m.rowid = v.rowid"
            ).fetchall()
        }
        assert embeddings_ids == vec_ids == {n1["id"], n2["id"]}
    finally:
        conn.close()


@pytest.mark.embeddings
def test_semantic_similarity_rows_vec0_matches_udf_fallback(isolated_env):
    """The accelerated path and the fallback must agree (within float
    tolerance) on the same data — this is an engine swap, not a behavior
    change."""
    n1 = memory_save("git worktrees let you check out two branches at once", title="Worktrees")
    n2 = memory_save("javascript heap memory leaks from forgotten intervals", title="Leaks")

    query_bytes = embedding_to_bytes(generate_embedding("parallel checked-out directories"))

    conn = storage.connect()
    try:
        vec0_rows = {
            r["note_id"]: r["similarity"] for r in _semantic_similarity_rows(conn, query_bytes)
        }
        assert set(vec0_rows) == {n1["id"], n2["id"]}

        # Force the fallback by making memory_vec unusable, then compare.
        conn.execute("DROP TABLE memory_vec")
        udf_rows = {
            r["note_id"]: r["similarity"] for r in _semantic_similarity_rows(conn, query_bytes)
        }
        assert set(udf_rows) == {n1["id"], n2["id"]}

        for note_id in (n1["id"], n2["id"]):
            assert vec0_rows[note_id] == pytest.approx(udf_rows[note_id], abs=1e-4)
    finally:
        conn.close()


@pytest.mark.embeddings
def test_semantic_similarity_rows_empty_when_no_notes_embedded(isolated_env):
    conn = storage.connect()
    try:
        query_bytes = embedding_to_bytes(generate_embedding("anything"))
        assert _semantic_similarity_rows(conn, query_bytes) == []
    finally:
        conn.close()


@pytest.mark.embeddings
def test_semantic_similarity_rows_exclude_index_filters_index_notes(isolated_env):
    # memory_save auto-maintains a masterindex00 note as a side effect; embed
    # it directly (its content mentions the query on purpose, so a filter
    # bug would surface it as the top hit) to prove exclude_index holds
    # under the vec0 path too.
    n = memory_save("a normal note about deploy pipelines", title="Deploys")
    conn = storage.connect()
    try:
        from unified_agent.tools.memory import _store_embedding

        conn.execute(
            "UPDATE memory SET content=? WHERE id='masterindex00'",
            ("deploy pipelines index of all notes",),
        )
        conn.commit()
        _store_embedding(conn, "masterindex00", "Index", "deploy pipelines index of all notes")
        conn.commit()

        query_bytes = embedding_to_bytes(generate_embedding("deploy pipelines"))
        rows = _semantic_similarity_rows(conn, query_bytes, exclude_index=True)
        ids = {r["note_id"] for r in rows}
        assert n["id"] in ids
        assert "masterindex00" not in ids
    finally:
        conn.close()


@pytest.mark.embeddings
def test_memory_search_end_to_end_falls_back_without_memory_vec(isolated_env):
    """memory_search must still return correct semantic results with
    memory_vec entirely missing (simulates sqlite-vec being unavailable on
    this host)."""
    n1 = memory_save("git worktrees let you check out two branches at once", title="Worktrees")
    memory_save("javascript heap memory leaks from forgotten intervals", title="Leaks")

    conn = storage.connect()
    try:
        conn.execute("DROP TABLE memory_vec")
        conn.commit()
    finally:
        conn.close()

    res = memory_search("parallel checked-out directories for branches")
    assert res["results"]
    assert res["results"][0]["id"] == n1["id"]


@pytest.mark.embeddings
def test_backfill_mirrors_pre_existing_embeddings_into_memory_vec(isolated_env):
    """A note whose embedding predates memory_vec (inserted directly into
    memory_embeddings, bypassing _store_embedding) must be mirrored in by
    memory_semantic.backfill_memory_embeddings's mirror pass (storage's
    _backfill_embeddings before task #1720) — the pre-existing-DB upgrade
    path."""
    n = memory_save("a note saved before sqlite-vec existed", title="Legacy")

    conn = storage.connect()
    try:
        conn.execute("DELETE FROM memory_vec")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS n FROM memory_vec").fetchone()["n"] == 0
    finally:
        conn.close()

    memory_semantic.backfill_memory_embeddings()

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT m.id FROM memory_vec v JOIN memory m ON m.rowid = v.rowid WHERE m.id=?",
            (n["id"],),
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_ensure_memory_vec_table_is_idempotent(isolated_env):
    conn = storage.connect()
    try:
        storage._ensure_memory_vec_table(conn)
        storage._ensure_memory_vec_table(conn)  # must not raise on re-create
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_vec'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def test_upsert_memory_vec_silently_no_ops_when_table_missing(isolated_env):
    conn = storage.connect()
    try:
        conn.execute("DROP TABLE memory_vec")
        conn.commit()
        # Must not raise sqlite3.OperationalError.
        storage.upsert_memory_vec(conn, "some-id", b"\x00" * (storage.MEMORY_VEC_DIM * 4))
    finally:
        conn.close()
