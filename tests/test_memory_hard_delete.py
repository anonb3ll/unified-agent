"""Hard deletion must not leave vectors on a reusable memory rowid (#1815)."""

import sqlite3
import struct

import pytest

from unified_agent import edition, storage
from unified_agent.tools.export import import_migration_manifest, rollback_migration_import
from unified_agent.tools.memory import _refresh_indexes, _tag_index_id

VECTOR = struct.pack("384f", 1.0, *([0.0] * 383))
OTHER_VECTOR = struct.pack("384f", 0.0, 1.0, *([0.0] * 382))


@pytest.fixture
def store(isolated_env):
    conn = storage.connect()
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_vec'").fetchone():
        # The cleanup contract also applies to hosts without the optional extension.
        conn.execute("CREATE TABLE memory_vec(rowid INTEGER PRIMARY KEY, embedding BLOB)")
    try:
        yield conn
    finally:
        conn.close()


def seed(conn, note_id, *, org_id=None, vector=VECTOR):
    stamp = storage.now()
    conn.execute(
        "INSERT INTO memory(id,title,content,tags,created_at,updated_at,org_id) "
        "VALUES (?,?,'fixture','[]',?,?,?)",
        (note_id, note_id, stamp, stamp, org_id),
    )
    return seed_auxiliary(conn, note_id, vector=vector)


def seed_auxiliary(conn, note_id, *, vector=VECTOR, recall=True):
    rowid = conn.execute("SELECT rowid FROM memory WHERE id=?", (note_id,)).fetchone()[0]
    conn.execute("INSERT INTO memory_embeddings(note_id,embedding) VALUES (?,?)", (note_id, vector))
    conn.execute("INSERT INTO memory_vec(rowid,embedding) VALUES (?,?)", (rowid, vector))
    if recall:
        conn.execute(
            "INSERT INTO memory_recall_votes(note_id,recalled_count) VALUES (?,7)", (note_id,)
        )
    return rowid


def import_notes(*note_ids):
    stamp = storage.now()
    result = import_migration_manifest(
        {
            "manifest_version": 1,
            "tasks": [],
            "memory": [
                {
                    "id": note_id,
                    "title": note_id,
                    "content": "fixture",
                    "tags": "[]",
                    "author": "Desktop_Agent2",
                    "created_at": stamp,
                    "updated_at": stamp,
                    "salience": 0.5,
                    "access_count": 0,
                    "feedback_helpful": 0,
                    "feedback_not_helpful": 0,
                    "feedback_stale": 0,
                    "feedback_wrong": 0,
                }
                for note_id in note_ids
            ],
        },
        apply=True,
    )
    assert result["memory"]["inserted"] == list(note_ids)
    assert result["rollback_token"]["migration_import_id"]
    return result["rollback_token"]


def assert_note_and_aux_present(conn, note_id, rowid, *, recall=True):
    assert conn.execute("SELECT 1 FROM memory WHERE id=?", (note_id,)).fetchone()
    assert (
        conn.execute("SELECT embedding FROM memory_vec WHERE rowid=?", (rowid,)).fetchone()[0]
        == VECTOR
    )
    assert (
        conn.execute(
            "SELECT embedding FROM memory_embeddings WHERE note_id=?", (note_id,)
        ).fetchone()[0]
        == VECTOR
    )
    vote = conn.execute(
        "SELECT recalled_count FROM memory_recall_votes WHERE note_id=?", (note_id,)
    ).fetchone()
    assert (vote[0] if vote else None) == (7 if recall else None)


def assert_aux_absent(conn, note_id, rowid):
    assert conn.execute("SELECT 1 FROM memory_vec WHERE rowid=?", (rowid,)).fetchone() is None
    assert (
        conn.execute("SELECT 1 FROM memory_recall_votes WHERE note_id=?", (note_id,)).fetchone()
        is None
    )
    assert (
        conn.execute("SELECT 1 FROM memory_embeddings WHERE note_id=?", (note_id,)).fetchone()
        is None
    )


@pytest.mark.parametrize("path", ["empty_tag", "stale_index"])
def test_index_cleanup_removes_auxiliary_rows(store, path):
    note_id = _tag_index_id("retired")
    rowid = seed(store, note_id)
    _refresh_indexes(store, {"retired"} if path == "empty_tag" else set())
    assert store.execute("SELECT 1 FROM memory WHERE id=?", (note_id,)).fetchone() is None
    assert_aux_absent(store, note_id, rowid)


def test_migration_rollback_removes_auxiliary_rows(store):
    token = import_notes("imported-note")
    # Derived caches do not edit the imported row or add native recall state.
    rowid = seed_auxiliary(store, "imported-note", recall=False)
    control = seed(store, "unrelated-note")
    store.commit()
    assert_note_and_aux_present(store, "imported-note", rowid, recall=False)
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 1
    assert store.execute("SELECT 1 FROM memory WHERE id='imported-note'").fetchone() is None
    assert_aux_absent(store, "imported-note", rowid)
    assert_note_and_aux_present(store, "unrelated-note", control)
    assert not any(rollback_migration_import(token)["rolled_back"].values())
    assert store.execute("SELECT 1 FROM memory WHERE id='imported-note'").fetchone() is None
    assert_aux_absent(store, "imported-note", rowid)
    assert_note_and_aux_present(store, "unrelated-note", control)


def test_legacy_rollback_keeps_note_and_all_auxiliary_rows(store):
    rowid = seed(store, "native-note")
    store.commit()
    assert rollback_migration_import({"memory": ["native-note"]}) == {
        "rolled_back": {"tasks": 0, "memory": 0}
    }
    assert_note_and_aux_present(store, "native-note", rowid)


def test_migration_rollback_keeps_later_recall_and_all_auxiliary_rows(store):
    token = import_notes("imported-note")
    # A recall vote created after import is native state, unlike derived caches.
    rowid = seed_auxiliary(store, "imported-note")
    store.commit()
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 0
    assert_note_and_aux_present(store, "imported-note", rowid)


def test_migration_rollback_keeps_another_tenants_auxiliary_rows(store, monkeypatch):
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "other-tenant")
    token = import_notes("foreign-note", "foreign-voted-note")
    rowid = seed_auxiliary(store, "foreign-note", recall=False)
    voted_rowid = seed_auxiliary(store, "foreign-voted-note")
    store.commit()

    with monkeypatch.context() as foreign:
        foreign.setattr(edition, "tenant_org_id", lambda: "caller-tenant")
        assert not any(rollback_migration_import(token)["rolled_back"].values())
    assert_note_and_aux_present(store, "foreign-note", rowid, recall=False)
    assert_note_and_aux_present(store, "foreign-voted-note", voted_rowid)

    # The foreign call must leave the receipt usable by its original owner.
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 1
    assert store.execute("SELECT 1 FROM memory WHERE id='foreign-note'").fetchone() is None
    assert_aux_absent(store, "foreign-note", rowid)
    assert_note_and_aux_present(store, "foreign-voted-note", voted_rowid)
    assert not any(rollback_migration_import(token)["rolled_back"].values())
    assert_note_and_aux_present(store, "foreign-voted-note", voted_rowid)


def test_deleted_vector_cannot_attach_to_a_reused_rowid(store):
    seed(store, "keep")
    rowid = seed(store, "deleted")
    assert storage.hard_delete_memory(store, "deleted", org_id=None)
    assert not storage.hard_delete_memory(store, "deleted", org_id=None)
    stamp = storage.now()
    store.execute(
        "INSERT INTO memory(id,content,created_at,updated_at) VALUES ('replacement','new',?,?)",
        (stamp, stamp),
    )
    assert store.execute("SELECT rowid FROM memory WHERE id='replacement'").fetchone()[0] == rowid
    assert_aux_absent(store, "deleted", rowid)


def test_hard_delete_does_not_touch_a_foreign_tenant(store):
    rowid = seed(store, "foreign-note", org_id="other-tenant")
    assert not storage.hard_delete_memory(store, "foreign-note", org_id=None)
    assert store.execute("SELECT 1 FROM memory_vec WHERE rowid=?", (rowid,)).fetchone()
    assert store.execute(
        "SELECT 1 FROM memory_recall_votes WHERE note_id='foreign-note'"
    ).fetchone()


def test_hard_delete_failure_rolls_back_auxiliary_cleanup(store):
    rowid = seed(store, "protected")
    store.execute(
        "CREATE TRIGGER refuse_vote_delete BEFORE DELETE ON memory_recall_votes "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="blocked"):
        storage.hard_delete_memory(store, "protected", org_id=None)
    assert store.in_transaction
    assert store.execute("SELECT 1 FROM memory WHERE id='protected'").fetchone()
    assert store.execute("SELECT 1 FROM memory_vec WHERE rowid=?", (rowid,)).fetchone()
    assert store.execute("SELECT 1 FROM memory_recall_votes WHERE note_id='protected'").fetchone()


def test_hard_delete_works_without_the_optional_vector_table(store):
    rowid = seed(store, "deleted")
    store.execute("DROP TABLE memory_vec")
    assert storage.hard_delete_memory(store, "deleted", org_id=None)
    assert store.execute("SELECT 1 FROM memory WHERE rowid=?", (rowid,)).fetchone() is None
    assert (
        store.execute("SELECT 1 FROM memory_recall_votes WHERE note_id='deleted'").fetchone()
        is None
    )


def test_orphan_repair_previews_and_removes_stale_reused_vectors(store):
    keep = seed(store, "keep", org_id="other-tenant")
    reused = seed(store, "old")
    # Reproduce the pre-fix deletion and an SQLite rowid reused by a new note.
    store.execute("DELETE FROM memory WHERE id='old'")
    stamp = storage.now()
    store.execute(
        "INSERT INTO memory(id,content,created_at,updated_at) VALUES ('new','new',?,?)",
        (stamp, stamp),
    )
    assert store.execute("SELECT rowid FROM memory WHERE id='new'").fetchone()[0] == reused
    store.execute(
        "INSERT INTO memory_embeddings(note_id,embedding) VALUES ('new',?)", (OTHER_VECTOR,)
    )
    store.execute("INSERT INTO memory_vec(rowid,embedding) VALUES (999,?)", (VECTOR,))
    store.commit()
    assert storage.repair_memory_orphans(store) == {
        "memory_vec": 2,
        "memory_chunk_vec": 0,
        "memory_recall_votes": 1,
        "applied": False,
    }
    assert store.execute("SELECT COUNT(*) FROM memory_vec").fetchone()[0] == 3
    assert storage.repair_memory_orphans(store, apply=True) == {
        "memory_vec": 2,
        "memory_chunk_vec": 0,
        "memory_recall_votes": 1,
        "applied": True,
    }
    # The caller owns commit/rollback; repair must not commit its transaction.
    assert store.in_transaction
    store.rollback()
    assert storage.repair_memory_orphans(store)["memory_vec"] == 2
    storage.repair_memory_orphans(store, apply=True)
    store.commit()
    assert storage.repair_memory_orphans(store, apply=True) == {
        "memory_vec": 0,
        "memory_chunk_vec": 0,
        "memory_recall_votes": 0,
        "applied": True,
    }
    assert store.execute("SELECT 1 FROM memory_vec WHERE rowid=?", (keep,)).fetchone()
    assert store.execute("SELECT 1 FROM memory_recall_votes WHERE note_id='keep'").fetchone()
    assert (
        store.execute("SELECT embedding FROM memory_embeddings WHERE note_id='new'").fetchone()[0]
        == OTHER_VECTOR
    )


def test_orphan_repair_without_vector_extension_table(store):
    store.execute("DROP TABLE memory_vec")
    store.execute("INSERT INTO memory_recall_votes(note_id) VALUES ('missing')")
    assert storage.repair_memory_orphans(store, apply=True) == {
        "memory_vec": 0,
        "memory_chunk_vec": 0,
        "memory_recall_votes": 1,
        "applied": True,
    }
