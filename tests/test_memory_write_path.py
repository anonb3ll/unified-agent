"""Memory write-path defects (task #1914).

1. An append must not overwrite a concurrent append committed between the
   read and the write (lost update).
2. Embedding inference never runs while the hub write lock is held.
3. Index-note maintenance runs after the note write commits, without the
   per-tag ``tags LIKE`` scans, and a refresh that cannot run is retried.
5. Soft-deleted notes stay out of the prune and maintenance reports, and
   ``consolidate.apply_supersede`` is tenant-fenced, never re-points an
   already superseded note, records its actor and refreshes the indexes.
"""

import sqlite3

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, consolidate, embeddings, storage
from unified_agent.recall_curation import recall_maintenance
from unified_agent.tools import memory as memory_tools
from unified_agent.tools.memory_prune import memory_prune_candidates

DIM = 384


def _vector(seed: float) -> list[float]:
    return [seed] * DIM


def _content(note_id: str) -> str:
    conn = storage.connect()
    try:
        return conn.execute("SELECT content FROM memory WHERE id=?", (note_id,)).fetchone()[0]
    finally:
        conn.close()


def _write_lock_free() -> bool:
    """True when no connection holds the database write lock right now."""
    probe = sqlite3.connect(config.db_path(), timeout=0)
    try:
        probe.execute("PRAGMA busy_timeout=0")
        try:
            probe.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return False
        probe.execute("ROLLBACK")
        return True
    finally:
        probe.close()


# --- 1. lost update on append ------------------------------------------------


def test_concurrent_append_between_read_and_write_is_kept(isolated_env, monkeypatch):
    note_id = memory_tools.memory_save("base", title="Shared", author="Desktop_Agent1")["id"]
    real_fetch = memory_tools._fetch_note
    fired = []

    def interleaving_fetch(conn, fetched_id, columns="*"):
        row = real_fetch(conn, fetched_id, columns)
        if fetched_id == note_id and "content" in columns and not fired:
            fired.append(True)
            # A second agent's append commits on its own connection after
            # this save has read the note.
            other = storage.connect()
            try:
                other.execute(
                    "UPDATE memory SET content = content || ? WHERE id=?",
                    ("\n\nPEER-BLOCK", note_id),
                )
                other.commit()
            finally:
                other.close()
        return row

    monkeypatch.setattr(memory_tools, "_fetch_note", interleaving_fetch)
    memory_tools.memory_save("MINE", id=note_id, append=True, author="Laptop_Agent1")

    final = _content(note_id)
    assert fired
    assert "PEER-BLOCK" in final, "the concurrent append was overwritten"
    assert "MINE" in final
    assert final.index("PEER-BLOCK") < final.index("MINE")


def test_stale_precomputed_embedding_is_repaired_after_commit(isolated_env, monkeypatch):
    """A raced append gets a replacement embedding of the committed text."""
    embedded = []

    def embed(text):
        embedded.append(text)
        assert _write_lock_free()
        return _vector(0.1)

    monkeypatch.setattr(embeddings, "generate_embedding", embed)
    note_id = memory_tools.memory_save("base", title="Shared", author="Desktop_Agent1")["id"]
    real_fetch = memory_tools._fetch_note
    fired = []

    def interleaving_fetch(conn, fetched_id, columns="*"):
        row = real_fetch(conn, fetched_id, columns)
        if fetched_id == note_id and "content" in columns and not fired:
            fired.append(True)
            other = storage.connect()
            try:
                other.execute("UPDATE memory SET content = content || '!' WHERE id=?", (note_id,))
                other.commit()
            finally:
                other.close()
        return row

    monkeypatch.setattr(memory_tools, "_fetch_note", interleaving_fetch)
    memory_tools.memory_save("more", id=note_id, append=True, author="Desktop_Agent1")

    conn = storage.connect()
    try:
        row = conn.execute("SELECT 1 FROM memory_embeddings WHERE note_id=?", (note_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert embedded[-1] == f"Shared\n\n{_content(note_id)}"


# --- 2. no embedding inside the write transaction ----------------------------


@pytest.mark.parametrize("mode", ["create", "replace", "append"])
def test_embedding_runs_with_no_write_lock_held(isolated_env, monkeypatch, mode):
    note_id = None
    if mode != "create":
        monkeypatch.setattr(embeddings, "generate_embedding", lambda text: _vector(0.2))
        note_id = memory_tools.memory_save("first", title="T", author="Desktop_Agent1")["id"]

    calls: list[bool] = []

    def recording_embedding(text: str) -> list[float]:
        calls.append(_write_lock_free())
        return _vector(0.3)

    monkeypatch.setattr(embeddings, "generate_embedding", recording_embedding)
    if mode == "create":
        note_id = memory_tools.memory_save("second note", title="T2", author="Desktop_Agent1")["id"]
    else:
        memory_tools.memory_save(
            "second", id=note_id, append=mode == "append", author="Desktop_Agent1"
        )

    assert calls, "the save never computed an embedding"
    assert all(calls), "generate_embedding ran while the write lock was held"
    conn = storage.connect()
    try:
        stored = conn.execute(
            "SELECT embedding FROM memory_embeddings WHERE note_id=?", (note_id,)
        ).fetchone()
    finally:
        conn.close()
    assert stored is not None
    assert stored[0] == embeddings.embedding_to_bytes(_vector(0.3))


def test_failed_precompute_is_not_retried_inside_the_transaction(isolated_env, monkeypatch):
    calls: list[bool] = []

    def failing_embedding(text: str) -> list[float]:
        calls.append(_write_lock_free())
        raise OSError("model unavailable")

    monkeypatch.setattr(embeddings, "generate_embedding", failing_embedding)
    note_id = memory_tools.memory_save("note", title="T", author="Desktop_Agent1")["id"]
    memory_tools.memory_save("more", id=note_id, append=True, author="Desktop_Agent1")
    assert calls and all(calls)


# --- 3. index maintenance off the note's write transaction --------------------


def test_index_refresh_runs_after_the_note_commits(isolated_env, monkeypatch):
    note_id = memory_tools.memory_save("v1", title="Doc", tags=["alpha"], author="A")["id"]
    real_refresh = memory_tools._render_indexes
    seen: list[str] = []

    def checking_refresh(conn, affected):
        seen.append(_content(note_id))
        return real_refresh(conn, affected)

    monkeypatch.setattr(memory_tools, "_render_indexes", checking_refresh)
    memory_tools.memory_save("v2", id=note_id, author="A")
    assert seen and seen[-1] == "v2", "index refresh ran inside the note's write transaction"

    tag_index = memory_tools.memory_get(memory_tools._tag_index_id("alpha"))["content"]
    assert "Doc" in tag_index
    assert "Summary: v2" in tag_index


def test_refresh_does_not_scan_tags_with_like(isolated_env):
    for i, tag in enumerate(["alpha", "beta", "gamma"]):
        memory_tools.memory_save(f"note {i}", title=f"N{i}", tags=[tag], author="A")
    conn = storage.connect()
    statements: list[str] = []
    try:
        conn.set_trace_callback(statements.append)
        memory_tools._refresh_indexes(conn, {"alpha", "beta", "gamma"})
        conn.commit()
    finally:
        conn.close()
    assert not any("tags LIKE" in s for s in statements), statements
    # One pass over the tenant's notes, not one per affected tag plus one.
    tag_reads = [s for s in statements if "SELECT" in s and "tags" in s and "FROM memory" in s]
    assert len(tag_reads) == 1, tag_reads


def test_index_content_matches_rebuild(isolated_env):
    """The refresh after a save produces the same notes as a full rebuild."""
    a = memory_tools.memory_save("alpha one", title="A1", tags=["alpha"], author="A")["id"]
    memory_tools.memory_save("alpha two", title="A2", tags=["alpha", "beta"], author="A")
    memory_tools.memory_save("untagged", title="U", author="A")
    memory_tools.memory_save("alpha one v2", id=a, author="A")
    ids = [
        memory_tools._master_index_id(),
        memory_tools._tag_index_id("alpha"),
        memory_tools._tag_index_id("beta"),
        memory_tools._tag_index_id(memory_tools.UNTAGGED),
    ]
    before = {i: memory_tools.memory_get(i)["content"] for i in ids}
    conn = storage.connect()
    try:
        memory_tools._refresh_indexes(conn, {"alpha", "beta", memory_tools.UNTAGGED})
        conn.commit()
    finally:
        conn.close()
    after = {i: memory_tools.memory_get(i)["content"] for i in ids}
    assert before == after
    assert "A1" in after[ids[1]] and "A2" in after[ids[1]]
    assert "U" in after[ids[3]]


def test_failed_refresh_is_retried_from_the_dirty_set(isolated_env, monkeypatch):
    real_refresh = memory_tools._render_indexes
    failures = []

    def busy_refresh(conn, affected):
        if not failures:
            failures.append(True)
            raise sqlite3.OperationalError("database is locked")
        return real_refresh(conn, affected)

    monkeypatch.setattr(memory_tools, "_render_indexes", busy_refresh)
    note_id = memory_tools.memory_save("body", title="Kept", tags=["delta"], author="A")["id"]
    assert failures
    # The note itself committed.
    assert _content(note_id) == "body"
    # Reading an index note flushes the pending refresh.
    tag_index = memory_tools.memory_get(memory_tools._tag_index_id("delta"))
    assert "Kept" in tag_index["content"]
    master = memory_tools.memory_get(memory_tools.MASTER_INDEX_ID)
    assert "delta" in master["content"]


# --- 5. reports and apply_supersede -------------------------------------------


def _soft_deleted_note(monkeypatch) -> str:
    note_id = memory_tools.memory_save(
        "to delete", title="Gone", author=config.operator_label(), salience=0.1, confidence=0.1
    )["id"]
    token = config.agent_identity_var.set(config.operator_label())
    try:
        assert memory_tools.memory_delete(note_id)["deleted"]
    finally:
        config.agent_identity_var.reset(token)
    return note_id


def test_prune_candidates_skip_soft_deleted(isolated_env, monkeypatch):
    note_id = _soft_deleted_note(monkeypatch)
    ids = [c["id"] for c in memory_prune_candidates(salience_threshold=0.5)["candidates"]]
    assert note_id not in ids


def test_recall_maintenance_skips_soft_deleted(isolated_env, monkeypatch):
    note_id = _soft_deleted_note(monkeypatch)
    report = recall_maintenance(confidence_floor=0.5, stale_days=0)
    ids = [c["id"] for c in report["low_confidence"] + report["stale"]]
    assert note_id not in ids


def test_apply_supersede_refuses_repointing(isolated_env):
    a = memory_tools.memory_save("a body", title="A")["id"]
    b = memory_tools.memory_save("b body", title="B")["id"]
    c = memory_tools.memory_save("c body", title="C")["id"]
    consolidate.apply_supersede(a, [c])
    with pytest.raises(ToolError, match="already superseded"):
        consolidate.apply_supersede(b, [c])
    assert memory_tools.memory_get(c)["superseded_by"] == a


def test_apply_supersede_is_tenant_fenced(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    foreign = memory_tools.memory_save("tenant a body", title="Foreign")["id"]
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    mine = memory_tools.memory_save("tenant b body", title="Mine")["id"]
    with pytest.raises(ToolError, match="does not exist"):
        consolidate.apply_supersede(mine, [foreign])
    with pytest.raises(ToolError, match="does not exist"):
        consolidate.apply_supersede(foreign, [mine])
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_a")
    assert memory_tools.memory_get(foreign)["superseded_by"] is None


def test_apply_supersede_records_actor_and_refreshes_indexes(isolated_env):
    keep = memory_tools.memory_save("keep body", title="Keeper", tags=["omega"])["id"]
    drop = memory_tools.memory_save("drop body", title="Dropped", tags=["omega"])["id"]
    assert "Dropped" in memory_tools.memory_get(memory_tools._tag_index_id("omega"))["content"]

    consolidate.apply_supersede(keep, [drop], actor="Desktop_Steward")

    tag_index = memory_tools.memory_get(memory_tools._tag_index_id("omega"))["content"]
    assert "Dropped" not in tag_index
    assert "Keeper" in tag_index
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT agent, summary FROM agent_events WHERE ref_id=? AND kind='memory' "
            "AND summary LIKE '%supersede%'",
            (drop,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["agent"] == "Desktop_Steward"
    assert keep in row["summary"]


def test_render_allows_peer_write_and_does_not_publish_stale_snapshot(isolated_env, monkeypatch):
    note = memory_tools.memory_save("old", title="Concurrent", tags=["alpha"], author="A")["id"]
    real_render = memory_tools._render_indexes
    fired = []

    def interleave(conn, tags):
        rendered = real_render(conn, tags)
        assert _write_lock_free(), "render reserves the writer slot"
        if not fired:
            fired.append(True)
            peer = storage.connect()
            try:
                peer.execute("UPDATE memory SET content=? WHERE id=?", ("peer committed", note))
                memory_tools._mark_indexes_dirty(peer, {"alpha"})
                peer.commit()
            finally:
                peer.close()
        return rendered

    monkeypatch.setattr(memory_tools, "_render_indexes", interleave)
    memory_tools.memory_save("my update", id=note, author="A")
    assert _content(note) == "peer committed"
    conn = storage.connect()
    try:
        assert memory_tools._indexes_pending(conn)
        content = conn.execute(
            "SELECT content FROM memory WHERE id=?", (memory_tools._tag_index_id("alpha"),)
        ).fetchone()[0]
        assert "my update" not in content
    finally:
        conn.close()
    assert (
        "peer committed" in memory_tools.memory_get(memory_tools._tag_index_id("alpha"))["content"]
    )


def test_pending_indexes_cannot_leak_deleted_summary(isolated_env, monkeypatch):
    note = memory_tools.memory_save("private summary", title="Private", tags=["alpha"], author="A")[
        "id"
    ]
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "A")

    def busy(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(memory_tools, "_flush_dirty_indexes", busy)
    assert memory_tools.memory_delete(note)["deleted"]
    assert memory_tools.memory_search("private", tags=["meta:index"])["results"] == []
    assert memory_tools.memory_list(tag="meta:index")["items"] == []
    with pytest.raises(ToolError, match="awaiting refresh"):
        memory_tools.memory_get(memory_tools._tag_index_id("alpha"))


def test_agent_consolidation_cannot_hide_peer_note(isolated_env, monkeypatch):
    survivor = memory_tools.memory_save("keep", author="A")["id"]
    peer = memory_tools.memory_save("peer", author="B")["id"]
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "A")
    with pytest.raises(ToolError, match="note owner"):
        consolidate.apply_supersede(survivor, [peer], actor="A")
    assert memory_tools.memory_get(peer).get("superseded_by") is None


def test_consolidation_mirrors_committed_mutation(isolated_env, monkeypatch):
    from unified_agent import events

    survivor = memory_tools.memory_save("keep", author="A")["id"]
    loser = memory_tools.memory_save("drop", author="A")["id"]
    mirrored = []

    def mirror(kind, **kwargs):
        assert memory_tools.memory_get(loser)["superseded_by"] == survivor
        mirrored.append((kind, kwargs))

    monkeypatch.setattr(events, "mirror_agent_event", mirror)
    consolidate.apply_supersede(survivor, [loser], actor="A")
    assert mirrored[0][0] == "memory"
    assert mirrored[0][1]["ref_id"] == loser


@pytest.mark.parametrize("update", [False, True])
def test_retry_receipt_is_rechecked_under_write_lock(isolated_env, monkeypatch, update):
    note = memory_tools.memory_save("base", author="A")["id"] if update else None
    kwargs = {"author": "A", "idempotency_key": "raced-retry"}
    if update:
        kwargs.update(id=note, append=True)
    fired = []
    winner = []

    def embed(text):
        if not fired:
            fired.append(True)
            winner.append(memory_tools.memory_save("one block", **kwargs))
        return _vector(0.1)

    monkeypatch.setattr(embeddings, "generate_embedding", embed)
    result = memory_tools.memory_save("one block", **kwargs)
    assert result == winner[0]
    assert _content(result["id"]).count("one block") == 1
    if not update:
        conn = storage.connect()
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM memory WHERE id NOT LIKE 'masterindex%' AND id NOT LIKE 'tagindex_%'"
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()


@pytest.mark.parametrize("kind", ["deleted", "index"])
@pytest.mark.parametrize("invalid_survivor", [True, False])
def test_apply_supersede_refuses_hidden_notes(isolated_env, monkeypatch, kind, invalid_survivor):
    valid = memory_tools.memory_save("keep visible", title="Visible")["id"]
    if kind == "deleted":
        hidden = memory_tools.memory_save("deleted body", title="Deleted")["id"]
        with monkeypatch.context() as m:
            m.setattr(config, "verified_agent_identity", lambda: "Desktop_Person")
            m.setattr(config, "actor_kind_for_identity", lambda _: "human")
            assert memory_tools.memory_delete(hidden)["deleted"]
    else:
        hidden = memory_tools._tag_index_id(memory_tools.UNTAGGED)
    survivor, candidate = (hidden, valid) if invalid_survivor else (valid, hidden)
    with pytest.raises(ToolError, match="deleted or an index"):
        consolidate.apply_supersede(survivor, [candidate])
    conn = storage.connect()
    try:
        assert (
            conn.execute("SELECT superseded_by FROM memory WHERE id=?", (valid,)).fetchone()[0]
            is None
        )
    finally:
        conn.close()


def test_index_search_refreshes_before_collecting_snippets(isolated_env, monkeypatch):
    note = memory_tools.memory_save("removed fact", title="UniqueRemovedSummary", tags=["snap"])[
        "id"
    ]
    original_flush = memory_tools._flush_dirty_indexes
    monkeypatch.setattr(memory_tools, "_flush_dirty_indexes", lambda _: None)
    with monkeypatch.context() as m:
        m.setattr(config, "verified_agent_identity", lambda: "Desktop_Person")
        m.setattr(config, "actor_kind_for_identity", lambda _: "human")
        assert memory_tools.memory_delete(note)["deleted"]
    monkeypatch.setattr(memory_tools, "_flush_dirty_indexes", original_flush)
    monkeypatch.setattr(
        embeddings, "generate_embedding", lambda _: (_ for _ in ()).throw(RuntimeError("no model"))
    )
    assert memory_tools.memory_search("UniqueRemovedSummary", tags=["meta:index"])["results"] == []
