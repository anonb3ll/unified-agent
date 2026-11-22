"""Disk-backed graph rollback boundaries and recovery controls (#2152)."""

import copy
import json

import pytest

from unified_agent import edition, embeddings, storage
from unified_agent.tools import export, memory

TABLES = ("memory_entities", "memory_links")


@pytest.fixture(autouse=True)
def offline_embeddings(monkeypatch):
    def unavailable(_text):
        raise RuntimeError("graph integrity fixtures use lexical storage only")

    monkeypatch.setattr(embeddings, "generate_embedding", unavailable)


def notes():
    return [memory.memory_save("Neutral fixture", author="Desktop_Agent2")["id"] for _ in range(2)]


def graph_row(table, ids, *, later=False):
    if table == "memory_entities":
        return {"note_id": ids[0], "kind": "service", "value": "later" if later else "imported"}
    return {
        "src_id": ids[1] if later else ids[0],
        "dst_id": ids[0] if later else ids[1],
        "rel": "evidence",
        "actor": "fixture",
        "created_at": "2026-10-17",
    }


def insert(conn, table, row, *, org_id=None, replace=False):
    conn.execute(
        f"INSERT {'OR REPLACE ' if replace else ''}INTO {table} "
        f"({', '.join(row)},org_id) VALUES ({','.join('?' for _ in row)},?)",
        (*row.values(), org_id),
    )


def import_graph(table, ids, *, later=False, imported_notes=False):
    manifest = {"manifest_version": 1, table: [graph_row(table, ids, later=later)]}
    if imported_notes:
        manifest["memory"] = [
            row
            for row in export.export_migration_manifest()["manifest"]["memory"]
            if row["id"] in ids
        ]
        with storage.connect() as conn:
            for note in ids:
                storage.hard_delete_memory(conn, note, org_id=edition.tenant_org_id())
    result = export.import_migration_manifest(manifest, apply=True)
    assert len(result[table]["inserted"]) == 1
    return result["rollback_token"]


def rows(table):
    with storage.connect() as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


def marker(note):
    with storage.connect() as conn:
        return conn.execute(
            "SELECT value FROM meta WHERE key=?", (memory.GRAPH_IMPORT_META_PREFIX + note,)
        ).fetchone()


def reconcile():
    # Force the actual reconciler to visit the notes, even after a prior startup.
    with storage.connect() as conn:
        conn.execute("DELETE FROM meta WHERE key=?", (memory.GRAPH_BACKFILL_META_KEY,))
        conn.commit()
        assert memory.backfill_memory_graph(conn) >= 1


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("owner", ["native", "other_import", "unprovable", "foreign"])
def test_graph_only_rollback_keeps_guard_for_any_surviving_graph(isolated_env, table, owner):
    ids = notes()
    token = import_graph(table, ids)
    assert marker(ids[0]) is not None
    if owner == "other_import":
        other = import_graph(table, ids, later=True)
    else:
        with storage.connect() as conn:
            if owner == "foreign":
                # A legacy inconsistent row is still data that rollback must
                # not erase indirectly. Modern writers enforce this fence.
                conn.execute(f"DROP TRIGGER {table}_org_fence_insert")
            insert(
                conn,
                table,
                graph_row(table, ids, later=True),
                org_id="foreign" if owner == "foreign" else None,
            )
            if owner == "unprovable":
                conn.execute(
                    "DELETE FROM memory_graph_import_ownership WHERE graph_table=?", (table,)
                )
    expected = [r for r in rows(table) if r != {**graph_row(table, ids), "org_id": None}]
    result = export.rollback_migration_import(token)["rolled_back"]
    assert result[table] == (0 if owner == "unprovable" else 1)
    assert marker(ids[0]) is not None
    retained = rows(table)
    reconcile()
    storage.init_db()
    assert rows(table) == retained
    assert all(row in retained for row in expected)
    assert not any(export.rollback_migration_import(token)["rolled_back"].values())
    if owner == "other_import":
        assert export.rollback_migration_import(other)["rolled_back"][table] == 1


@pytest.mark.parametrize("table", TABLES)
def test_missing_update_trigger_keeps_native_graph_and_imported_notes(isolated_env, table):
    ids = notes()
    token = import_graph(table, ids, imported_notes=True)
    with storage.connect() as conn:
        conn.execute(f"DROP TRIGGER {table}_import_owner_update")
        conn.execute(
            "UPDATE memory_entities SET value=value"
            if table == "memory_entities"
            else "UPDATE memory_links SET actor='native-later'"
        )
    retained = rows(table)
    result = export.rollback_migration_import(token)["rolled_back"]
    assert result[table] == 0
    assert result["memory"] == (1 if table == "memory_entities" else 0)
    assert rows(table) == retained
    reconcile()
    storage.init_db()
    assert rows(table) == retained


@pytest.mark.parametrize("table", TABLES)
def test_repair_invalidates_only_affected_table_proofs(isolated_env, table):
    ids = notes()
    other_table = next(t for t in TABLES if t != table)
    old = import_graph(table, ids)
    other = import_graph(other_table, ids)
    with storage.connect() as conn:
        conn.execute(f"DROP TRIGGER {table}_import_owner_delete")
    assert export.rollback_migration_import(other)["rolled_back"][other_table] == 1
    assert export.rollback_migration_import(old)["rolled_back"][table] == 0


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("event", ["insert", "update", "delete"])
@pytest.mark.parametrize("repair", ["rollback", "later_import"])
def test_any_missing_trigger_discards_all_old_table_proofs(isolated_env, table, event, repair):
    ids = notes()
    old = import_graph(table, ids)
    with storage.connect() as conn:
        conn.execute(f"DROP TRIGGER {table}_import_owner_{event}")
    # Even untouched rows have no durable proof that they escaped the gap.
    if repair == "later_import":
        fresh = import_graph(table, ids, later=True)
    assert export.rollback_migration_import(old)["rolled_back"][table] == 0
    assert {**graph_row(table, ids), "org_id": None} in rows(table)
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name IN (?,?,?)",
                [f"{table}_import_owner_{e}" for e in ("insert", "update", "delete")],
            ).fetchone()[0]
            == 3
        )
    if repair == "later_import":
        assert export.rollback_migration_import(fresh)["rolled_back"][table] == 1


@pytest.mark.parametrize("table", TABLES)
def test_intact_native_update_revokes_graph_authority(isolated_env, table):
    ids = notes()
    token = import_graph(table, ids)
    with storage.connect() as conn:
        conn.execute(
            "UPDATE memory_entities SET value=value"
            if table == "memory_entities"
            else "UPDATE memory_links SET actor='native-later'"
        )
    retained = rows(table)
    assert export.rollback_migration_import(token)["rolled_back"][table] == 0
    reconcile()
    assert rows(table) == retained


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("boundary", ["savepoint", "transaction"])
def test_trigger_repair_and_proof_invalidation_rollback_together(isolated_env, table, boundary):
    ids = notes()
    token = import_graph(table, ids)
    with storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if boundary == "savepoint":
            conn.execute("SAVEPOINT native")
        conn.execute(f"DROP TRIGGER {table}_import_owner_update")
        conn.execute(
            "UPDATE memory_entities SET value=value"
            if table == "memory_entities"
            else "UPDATE memory_links SET actor='temporary-native'"
        )
        export._ensure_graph_import_ownership(conn)
        assert not conn.execute(
            "SELECT 1 FROM memory_graph_import_ownership WHERE graph_table=?", (table,)
        ).fetchone()
        if boundary == "savepoint":
            conn.execute("ROLLBACK TO native")
            conn.execute("RELEASE native")
        else:
            conn.rollback()
    assert export.rollback_migration_import(token)["rolled_back"][table] == 1


@pytest.mark.parametrize("table", TABLES)
def test_foreign_attempt_does_not_commit_trigger_repair_or_consume_receipt(
    isolated_env, monkeypatch, table
):
    ids = notes()
    token = import_graph(table, ids)
    with storage.connect() as conn:
        conn.execute(f"DROP TRIGGER {table}_import_owner_update")
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "foreign")
    assert not any(export.rollback_migration_import(token)["rolled_back"].values())
    with storage.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM memory_graph_import_ownership WHERE owner=?",
            (token["memory_graph_import_id"],),
        ).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
            (f"{table}_import_owner_update",),
        ).fetchone()
    monkeypatch.setattr(edition, "tenant_org_id", lambda: None)
    assert export.rollback_migration_import(token)["rolled_back"][table] == 0
    with storage.connect() as conn:
        assert not conn.execute(
            "SELECT 1 FROM meta WHERE key=?",
            ("memory_graph_import_receipt:" + token["memory_graph_import_id"],),
        ).fetchone()


@pytest.mark.parametrize("table", TABLES)
def test_failed_rollback_restores_graph_receipt_and_guard(isolated_env, monkeypatch, table):
    ids = notes()
    token = import_graph(table, ids)
    before = rows(table)
    guard = marker(ids[0])["value"]
    real_delete = export._delete_owned

    def fail(*args):
        raise RuntimeError("injected rollback failure")

    monkeypatch.setattr(export, "_delete_owned", fail)
    with pytest.raises(RuntimeError, match="injected rollback failure"):
        export.rollback_migration_import(token)
    assert rows(table) == before
    assert marker(ids[0])["value"] == guard
    monkeypatch.setattr(export, "_delete_owned", real_delete)
    assert export.rollback_migration_import(token)["rolled_back"][table] == 1
    assert marker(ids[0]) is None  # No surviving graph: legitimate release.


@pytest.mark.parametrize("table", TABLES)
def test_dry_run_repair_does_not_commit_proof_or_receipt_changes(isolated_env, table):
    ids = notes()
    token = import_graph(table, ids)
    with storage.connect() as conn:
        conn.execute(f"DROP TRIGGER {table}_import_owner_update")
    result = export.import_migration_manifest(
        {"manifest_version": 1, table: [graph_row(table, ids, later=True)]}, apply=False
    )
    assert len(result[table]["inserted"]) == 1
    with storage.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM memory_graph_import_ownership WHERE owner=?",
            (token["memory_graph_import_id"],),
        ).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
            (f"{table}_import_owner_update",),
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM meta WHERE key=?",
            ("memory_graph_import_receipt:" + token["memory_graph_import_id"],),
        ).fetchone()
    assert export.rollback_migration_import(token)["rolled_back"][table] == 0


@pytest.mark.parametrize("trim", ["graph", "memory", "legacy"])
def test_trimmed_graph_tokens_keep_surviving_note_protection(isolated_env, trim):
    ids = notes()
    token = import_graph("memory_entities", ids, imported_notes=True)
    modified = copy.deepcopy(token)
    if trim == "graph":
        modified.pop("memory_entities")
    else:
        modified.pop("memory")
    if trim == "legacy":
        with storage.connect() as conn:
            conn.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (
                    json.dumps(None),
                    "memory_graph_import_receipt:" + token["memory_graph_import_id"],
                ),
            )
    assert export.rollback_migration_import(modified)["rolled_back"]["memory"] == (
        1 if trim == "graph" else 0
    )
    if trim == "graph":
        assert marker(ids[0]) is not None
        reconcile()
        assert rows("memory_entities")
