import pytest

from unified_agent import config, storage
from unified_agent.tools.memory import memory_delete, memory_get, memory_save, memory_search


@pytest.mark.embeddings
def test_semantic_search_roundtrip(isolated_env):
    # 1. Save two notes with distinct semantics
    n1 = memory_save(
        title="Git Worktrees Guide",
        content="Git worktrees allow you to have multiple checked-out directories "
        "for the same repository. This is useful for working on two branches "
        "in parallel without switching your working tree.",
        tags=["git", "dev"],
    )

    n2 = memory_save(
        title="JS Heap Memory Leaks",
        content="A memory leak in JavaScript happens when variables or objects are "
        "retained in the heap and cannot be garbage collected. Common causes "
        "include accidental globals and forgotten setIntervals.",
        tags=["js", "perf"],
    )

    # Verify both notes got stored in the embeddings table
    conn = storage.connect()
    try:
        rows = conn.execute("SELECT note_id FROM memory_embeddings").fetchall()
        note_ids = {r[0] for r in rows}
        assert note_ids == {n1["id"], n2["id"]}
    finally:
        conn.close()

    # 2. Search using query semantic meaning (similar to Git worktrees but no overlapping keywords)
    res = memory_search("parallel local checking out directory folders")
    results = res["results"]

    # Verify Git Worktrees Guide is first
    assert len(results) > 0
    assert results[0]["id"] == n1["id"]
    assert results[0]["similarity"] > 0.4

    # 3. Search using JS memory leak semantics
    res2 = memory_search("garbage collector heap retention bugs")
    results2 = res2["results"]
    assert len(results2) > 0
    assert results2[0]["id"] == n2["id"]
    assert results2[0]["similarity"] > 0.4


def test_save_survives_embedding_failure(isolated_env, monkeypatch):
    # If fastembed can't load (model not cached, offline), notes must still
    # save and be findable via FTS; only semantic ranking degrades.
    from unified_agent import embeddings

    def boom(text):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(embeddings, "generate_embedding", boom)

    n = memory_save("blue-green deploys are the rollout strategy", title="Deploys")
    assert n["created"] is True

    conn = storage.connect()
    try:
        count = conn.execute(
            "SELECT count(*) FROM memory_embeddings WHERE note_id=?", (n["id"],)
        ).fetchone()[0]
        assert count == 0  # no embedding stored, but the note exists
    finally:
        conn.close()

    hits = memory_search("blue-green deploys")["results"]
    assert [h["id"] for h in hits] == [n["id"]]

    # Updating the note must also survive
    r = memory_save("updated content about blue-green deploys", id=n["id"])
    assert r == {"id": n["id"], "created": False, "revision": memory_get(n["id"])["revision"]}


@pytest.mark.embeddings
def test_cascade_delete(isolated_env):
    # Save a note
    n = memory_save(title="Temporary Note", content="Delete me soon.", tags=["temp"])

    # Verify embedding exists
    conn = storage.connect()
    try:
        emb_before = conn.execute(
            "SELECT count(*) FROM memory_embeddings WHERE note_id=?", (n["id"],)
        ).fetchone()[0]
        assert emb_before == 1
    finally:
        conn.close()

    # Soft-delete keeps the row and its embedding. Search hides the note;
    # dropping the vector would make startup backfill re-embed the tombstone.
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        memory_delete(n["id"])
    finally:
        config.agent_identity_var.reset(token)

    assert memory_search("Delete me soon")["results"] == []
    conn = storage.connect()
    try:
        still = conn.execute("SELECT id FROM memory WHERE id=?", (n["id"],)).fetchone()
        emb_after = conn.execute(
            "SELECT count(*) FROM memory_embeddings WHERE note_id=?", (n["id"],)
        ).fetchone()[0]
        assert still is not None
        assert emb_after == 1
    finally:
        conn.close()
