"""Graph migration regression coverage (#2104): complete, fenced and reversible."""

import copy
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import edition, embeddings, memory_graph, storage
from unified_agent.tools.export import (
    compare_migration_manifests,
    export_memory,
    export_migration_manifest,
    import_migration_manifest,
    rollback_migration_import,
)
from unified_agent.tools.memory import (
    GRAPH_BACKFILL_META_KEY,
    GRAPH_IMPORT_META_PREFIX,
    memory_save,
)


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    def offline(_text):
        raise RuntimeError("lexical-only graph migration test")

    monkeypatch.setattr(embeddings, "generate_embedding", offline)


def snapshot():
    return export_migration_manifest()["manifest"]


def graph(manifest):
    return {table: manifest[table] for table in ("memory_entities", "memory_links")}


def seed():
    base = memory_save("Task #2104 graph base", author="server_agent2", tags=["keep"])["id"]
    newer = memory_save(
        "Task #2104 graph revision",
        author="server_agent2",
        tags=["other"],
        links=[f"extends:{base}", f"derives:{base}"],
        contradicts=[base],
        evidence=[f"memory:{base}"],
    )["id"]
    with storage.connect() as conn:
        memory_graph.add_link(
            conn,
            newer,
            base,
            "supersedes",
            actor="source_actor",
            created_at="2025-01-02T03:04:05+00:00",
            org_id=edition.tenant_org_id(),
        )
        # Persisted entities must survive verbatim, including values an extractor
        # version would not recreate from the current content.
        memory_graph.add_entities(
            conn, base, [("service", "legacy-value")], edition.tenant_org_id()
        )
    return base, newer


def switch_home(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "target"))
    storage.init_db()


@pytest.mark.parametrize("org_id", [None, "source_org"])
def test_roundtrip_dry_run_idempotence_and_exact_graph_rollback(
    isolated_env, tmp_path, monkeypatch, org_id
):
    monkeypatch.setattr(edition, "tenant_org_id", lambda: org_id)
    seed()
    source = snapshot()
    assert {row["rel"] for row in source["memory_links"]} == set(memory_graph.LINK_RELS)
    switch_home(tmp_path, monkeypatch)
    # Target scope replaces source org metadata; actor and source timestamps stay.
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "target_org")
    before = snapshot()
    preview = import_migration_manifest(source)
    assert preview["memory_links"]["inserted"]
    assert snapshot()["fingerprint"] == before["fingerprint"]
    applied = import_migration_manifest(source, apply=True)
    assert graph(snapshot()) == graph(source)
    with storage.connect() as conn:
        for table in graph(source):
            assert {r[0] for r in conn.execute(f"SELECT org_id FROM {table}")} == {"target_org"}
    again = import_migration_manifest(source, apply=True)
    for table in graph(source):
        assert again[table]["inserted"] == []
        assert again[table]["held"] == []
        assert len(again[table]["unchanged"]) == len(source[table])
    undone = rollback_migration_import(applied["rollback_token"])
    assert undone["rolled_back"]["memory_links"] == len(source["memory_links"])
    assert snapshot()["fingerprint"] == before["fingerprint"]
    assert all(
        n == 0 for n in rollback_migration_import(applied["rollback_token"])["rolled_back"].values()
    )


@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_note_exports_keep_shape_and_filter_graph_to_exported_notes(isolated_env, format):
    base, newer = seed()
    out = export_memory(format=format, write_to_vault=False)
    rows = (
        json.loads(out["content"])
        if format == "json"
        else [json.loads(line) for line in out["content"].splitlines()]
    )
    assert isinstance(rows, list) and out["rows"] == 2
    assert sum(len(row["links"]) for row in rows) == 5
    assert all(link["src_id"] == row["id"] for row in rows for link in row["links"])
    assert any(entity["value"] == "legacy-value" for row in rows for entity in row["entities"])
    filtered = json.loads(
        export_memory(format="json", tag="other", write_to_vault=False)["content"]
    )
    assert [row["id"] for row in filtered] == [newer]
    assert filtered[0]["links"] == []
    assert base not in json.dumps(filtered[0]["entities"])


def test_export_graph_is_complete_and_affects_fingerprint_and_comparison(isolated_env):
    base, newer = seed()
    first = snapshot()
    with storage.connect() as conn:
        conn.execute("UPDATE memory_links SET actor='corrected source actor' WHERE rel='extends'")
        conn.executemany(
            "INSERT INTO memory_entities(note_id,kind,value,org_id) VALUES (?,?,?,?)",
            [(base, "service", f"service-{i}", edition.tenant_org_id()) for i in range(120)],
        )
    second = snapshot()
    assert len(second["memory_entities"]) >= 120
    assert first["fingerprint"] != second["fingerprint"]
    diff = compare_migration_manifests(first, second)
    assert diff["summary"]["memory_links"]["changed"] == 1
    assert diff["summary"]["memory_entities"]["right_only"] == 120
    assert diff["summary"]["memory"]["changed"] == 0
    assert diff["requires_human_disposition"]
    # Index notes are excluded even if they have graph rows.
    with storage.connect() as conn:
        idx = conn.execute("SELECT id FROM memory WHERE id LIKE 'tagindex_%' LIMIT 1").fetchone()[0]
        memory_graph.add_link(
            conn, newer, idx, "extends", actor=None, created_at="2026-01-01", org_id=None
        )
    assert all(r["dst_id"] != idx for r in snapshot()["memory_links"])


@pytest.mark.parametrize("org_id", [None, "org_a"])
def test_exports_fence_graph_rows_and_both_endpoints(isolated_env, monkeypatch, org_id):
    monkeypatch.setattr(edition, "tenant_org_id", lambda: org_id)
    base, newer = seed()
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "foreign")
    foreign = memory_save("foreign graph note", author="foreign_actor")["id"]
    with storage.connect() as conn:
        # Simulate legacy corruption written before triggers existed.
        conn.execute("DROP TRIGGER memory_links_org_fence_insert")
        conn.execute("DROP TRIGGER memory_entities_org_fence_insert")
        conn.execute(
            "INSERT INTO memory_links(src_id,dst_id,rel,actor,created_at,org_id) VALUES (?,?,?,?,?,?)",
            (newer, foreign, "extends", "secret_actor", "2026-01-01", org_id),
        )
        conn.execute(
            "INSERT INTO memory_entities(note_id,kind,value,org_id) VALUES (?,?,?,?)",
            (foreign, "service", "secret_entity", org_id),
        )
        conn.execute(
            "INSERT INTO memory_links(src_id,dst_id,rel,actor,created_at,org_id) VALUES (?,?,?,?,?,?)",
            (base, newer, "evidence", "wrong_scope_actor", "2026-01-01", "foreign"),
        )
    monkeypatch.setattr(edition, "tenant_org_id", lambda: org_id)
    exported = (
        json.dumps(graph(snapshot()))
        + export_memory(format="json", write_to_vault=False)["content"]
    )
    assert foreign not in exported
    assert "secret_actor" not in exported and "secret_entity" not in exported
    assert "wrong_scope_actor" not in exported


def test_graph_only_import_rollback_preserves_existing_and_later_graph(isolated_env):
    base, newer = seed()
    original = snapshot()
    added = {
        "src_id": base,
        "dst_id": newer,
        "rel": "extends",
        "actor": "importer",
        "created_at": "2026-02-01",
    }
    manifest = {"manifest_version": 1, "memory_links": [*original["memory_links"], added]}
    result = import_migration_manifest(manifest, apply=True)
    assert result["rollback_token"]["memory"] == []
    assert result["rollback_token"]["memory_links"] == [
        {k: added[k] for k in ("src_id", "dst_id", "rel")}
    ]
    with storage.connect() as conn:
        memory_graph.add_entities(conn, base, [("service", "later-native")], None)
    rollback_migration_import(result["rollback_token"])
    after = snapshot()
    assert after["memory"] == original["memory"]
    assert after["memory_links"] == original["memory_links"]
    assert len(after["memory_entities"]) == len(original["memory_entities"]) + 1


def test_import_holds_conflicting_note_graph_and_existing_link_edits(isolated_env):
    base, newer = seed()
    manifest = snapshot()
    manifest["memory"][0]["content"] = "conflicting source note"
    rejected_id = manifest["memory"][0]["id"]
    manifest["memory_entities"].append(
        {"note_id": rejected_id, "kind": "service", "value": "new-source"}
    )
    before = graph(snapshot())
    result = import_migration_manifest(manifest, apply=True)
    assert graph(snapshot()) == before
    assert any(row["reason"] == "unresolved_memory" for row in result["memory_links"]["held"])
    # Even when note rows are unchanged, link attribution conflicts are held.
    manifest = snapshot()
    manifest["memory_links"][0]["actor"] = "upstream mutation"
    result = import_migration_manifest(manifest, apply=True)
    assert result["memory_links"]["held"][0]["reason"] == "conflicting_edit"
    assert graph(snapshot()) == before


@pytest.mark.parametrize("org_id", [None, "org_a"])
def test_foreign_endpoints_match_missing_and_foreign_rollback_is_noop(
    isolated_env, monkeypatch, org_id
):
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "foreign")
    base, newer = seed()
    source = snapshot()
    monkeypatch.setattr(edition, "tenant_org_id", lambda: org_id)
    owned = memory_save("owned endpoint", author="server_agent2")["id"]
    foreign = {
        "manifest_version": 1,
        "memory_entities": [{"note_id": base, "kind": "service", "value": "attempt"}],
        "memory_links": [
            {"src_id": owned, "dst_id": newer, "rel": "extends", "created_at": "2026-01-01"}
        ],
    }
    absent = copy.deepcopy(foreign)
    absent["memory_entities"][0]["note_id"] = "missing-base"
    absent["memory_links"][0]["dst_id"] = "missing-newer"
    for manifest in (foreign, absent):
        result = import_migration_manifest(manifest, apply=True)
        assert result["memory_entities"]["held"][0]["reason"] == "unresolved_memory"
        assert result["memory_links"]["held"][0]["reason"] == "unresolved_memory"
    token = {
        table: [{k: row[k] for k in memory_graph_keys(table)} for row in rows]
        for table, rows in graph(source).items()
    }
    token["memory_graph_import_id"] = "0" * 32
    assert all(n == 0 for n in rollback_migration_import(token)["rolled_back"].values())
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "foreign")
    assert graph(snapshot()) == graph(source)


def memory_graph_keys(table):
    return (
        ("note_id", "kind", "value") if table == "memory_entities" else ("src_id", "dst_id", "rel")
    )


@pytest.mark.parametrize(
    "bad", [None, {}, [None], [{"note_id": 1, "kind": "service", "value": "x"}]]
)
def test_malformed_graph_manifest_rejected_before_writes(isolated_env, bad):
    seed()
    manifest = snapshot()
    manifest["memory_entities"] = bad
    before = snapshot()["fingerprint"]
    with pytest.raises(ToolError, match="memory_entities"):
        import_migration_manifest(manifest, apply=True)
    assert snapshot()["fingerprint"] == before


def test_legacy_manifest_without_graph_collections_is_accepted(isolated_env, tmp_path, monkeypatch):
    seed()
    manifest = snapshot()
    del manifest["memory_entities"], manifest["memory_links"]
    switch_home(tmp_path, monkeypatch)
    result = import_migration_manifest(manifest, apply=True)
    assert len(result["memory"]["inserted"]) == 2
    assert graph(snapshot()) == {"memory_entities": [], "memory_links": []}
    legacy = rollback_migration_import({"memory": result["rollback_token"]["memory"]})
    assert legacy["rolled_back"]["memory"] == 0
    rollback_migration_import(result["rollback_token"])
    assert snapshot()["memory"] == []


def test_duplicate_graph_keys_and_malformed_token_are_atomic(isolated_env):
    seed()
    manifest = snapshot()
    before = manifest["fingerprint"]
    manifest["memory_links"].append(dict(manifest["memory_links"][0]))
    with pytest.raises(ToolError, match="duplicate graph keys"):
        import_migration_manifest(manifest, apply=True)
    token = {"memory": [manifest["memory"][0]["id"]], "memory_links": [None]}
    with pytest.raises(ToolError, match="memory_links"):
        rollback_migration_import(token)
    assert snapshot()["fingerprint"] == before


@pytest.mark.parametrize("invalid", ["self", "bad_relation"])
def test_invalid_graph_constraint_is_held_without_blocking_safe_rows(isolated_env, invalid):
    base, newer = seed()
    row = {
        "src_id": base,
        "dst_id": base if invalid == "self" else newer,
        "rel": "extends" if invalid == "self" else "unknown",
        "created_at": "2026-01-01",
    }
    entity = {
        "note_id": base,
        "kind": "service",
        "value": "safe-import",
        "org_id": "untrusted-source-org",
    }
    result = import_migration_manifest(
        {"manifest_version": 1, "memory_links": [row], "memory_entities": [entity]}, apply=True
    )
    assert result["memory_links"]["held"][0]["reason"] == "insert_rejected"
    assert result["memory_entities"]["inserted"]
    with storage.connect() as conn:
        assert (
            conn.execute("SELECT org_id FROM memory_entities WHERE value='safe-import'").fetchone()[
                0
            ]
            is None
        )


def test_foreign_note_collision_never_acquires_graph(isolated_env, monkeypatch):
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "foreign")
    seed()
    manifest = snapshot()
    monkeypatch.setattr(edition, "tenant_org_id", lambda: None)
    result = import_migration_manifest(manifest, apply=True)
    assert all(row["reason"] == "insert_rejected" for row in result["memory"]["held"])
    for table in graph(manifest):
        assert result[table]["inserted"] == []
        assert all(row["reason"] == "unresolved_memory" for row in result[table]["held"])
    assert graph(snapshot()) == {"memory_entities": [], "memory_links": []}
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "foreign")
    assert graph(snapshot()) == graph(manifest)


def test_restart_preserves_restored_graph_and_backfills_unrelated_legacy_note(
    isolated_env, tmp_path, monkeypatch
):
    seed()
    source = snapshot()
    switch_home(tmp_path, monkeypatch)
    legacy = memory_save("Legacy task #9876", author="server_agent2")["id"]
    with storage.connect() as conn:
        conn.execute("DELETE FROM memory_entities WHERE note_id=?", (legacy,))
        conn.execute("DELETE FROM meta WHERE key=?", (GRAPH_BACKFILL_META_KEY,))
    applied = import_migration_manifest(source, apply=True)
    assert applied["rollback_token"]["memory_graph_backfill"]
    storage.init_db()  # Runs the actual graph startup hook, not a mocked backfill.
    after = snapshot()
    imported = {r["id"] for r in source["memory"]}
    assert after["memory_links"] == source["memory_links"]
    assert [r for r in after["memory_entities"] if r["note_id"] in imported] == source[
        "memory_entities"
    ]
    assert {"note_id": legacy, "kind": "task", "value": "9876"} in after["memory_entities"]
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM meta WHERE key LIKE ?", (GRAPH_IMPORT_META_PREFIX + "%",)
            ).fetchone()
            is None
        )
    rollback_migration_import(applied["rollback_token"])
    assert {r["id"] for r in snapshot()["memory"]} == {legacy}
    assert snapshot()["memory_entities"] == [
        r for r in after["memory_entities"] if r["note_id"] == legacy
    ]


def test_rollback_graph_only_import_releases_guard_when_no_graph_survives(isolated_env):
    base, _newer = seed()
    with storage.connect() as conn:
        conn.execute("DELETE FROM memory_entities WHERE note_id=?", (base,))
        conn.execute("DELETE FROM memory_links WHERE src_id=? OR dst_id=?", (base, base))
    manifest = {
        "manifest_version": 1,
        "memory_entities": [{"note_id": base, "kind": "service", "value": "imported-only"}],
    }
    result = import_migration_manifest(manifest, apply=True)
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key=?", (GRAPH_IMPORT_META_PREFIX + base,)
            ).fetchone()
            is not None
        )
    rollback_migration_import(result["rollback_token"])
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT value FROM meta WHERE key=?", (GRAPH_IMPORT_META_PREFIX + base,)
            ).fetchone()
            is None
        )
        conn.execute("DELETE FROM memory_entities WHERE note_id=?", (base,))
    storage.init_db()
    entities = snapshot()["memory_entities"]
    assert {"note_id": base, "kind": "task", "value": "2104"} in entities
    assert not any(r["value"] == "imported-only" for r in entities)


def test_schema_valid_empty_graph_values_survive_roundtrip(isolated_env, tmp_path, monkeypatch):
    base, _newer = seed()
    with storage.connect() as conn:
        conn.execute("INSERT INTO memory_entities(note_id,kind,value) VALUES (?, '', '')", (base,))
        conn.execute("UPDATE memory_links SET created_at='', actor='' WHERE rel='extends'")
    source = snapshot()
    switch_home(tmp_path, monkeypatch)
    result = import_migration_manifest(source, apply=True)
    assert result["memory_links"]["held"] == []
    assert result["memory_entities"]["held"] == []
    storage.init_db()
    assert graph(snapshot()) == graph(source)


def test_completed_cursor_needs_no_markers_and_reincarnated_note_backfills(isolated_env):
    base, _newer = seed()
    manifest = snapshot()
    manifest["memory_entities"].append(
        {"note_id": base, "kind": "service", "value": "imported-reuse"}
    )
    import_migration_manifest(manifest, apply=True)
    with storage.connect() as conn:
        # Simulate replacement with the same ID and reused rowid before startup.
        conn.execute(
            "UPDATE memory SET created_at='2030-01-01', content='Task #5432' WHERE id=?", (base,)
        )
    storage.init_db()
    assert {"note_id": base, "kind": "task", "value": "5432"} in snapshot()["memory_entities"]
    again = import_migration_manifest(snapshot(), apply=True)
    assert "memory_graph_backfill" not in again["rollback_token"]
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM meta WHERE key LIKE ?", (GRAPH_IMPORT_META_PREFIX + "%",)
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("first_rollback", [0, 1])
def test_overlapping_graph_import_rollback_preserves_other_backfill_owner(
    isolated_env, first_rollback
):
    base, _newer = seed()
    results = []
    for value in ("import-a", "import-b"):
        result = import_migration_manifest(
            {
                "manifest_version": 1,
                "memory_entities": [{"note_id": base, "kind": "service", "value": value}],
            },
            apply=True,
        )
        assert result["rollback_token"]["memory_graph_backfill"]
        results.append(result)
    rollback_migration_import(results[first_rollback]["rollback_token"])
    # Replaying the first rollback cannot release the surviving import's owner.
    rollback_migration_import(results[first_rollback]["rollback_token"])
    storage.init_db()
    surviving_value = ("import-b", "import-a")[first_rollback]
    assert {"note_id": base, "kind": "service", "value": surviving_value} in snapshot()[
        "memory_entities"
    ]
    rollback_migration_import(results[1 - first_rollback]["rollback_token"])
    assert not any(r["value"] in ("import-a", "import-b") for r in snapshot()["memory_entities"])


@pytest.mark.parametrize("table", ["memory_entities", "memory_links"])
def test_consumed_rollback_cannot_delete_reimported_same_key_after_restart(isolated_env, table):
    base, newer = seed()
    row = (
        {"note_id": base, "kind": "service", "value": "reused-key"}
        if table == "memory_entities"
        else {
            "src_id": base,
            "dst_id": newer,
            "rel": "extends",
            "actor": "original",
            "created_at": "2026-01-01",
        }
    )
    manifest = {"manifest_version": 1, table: [row]}
    first = import_migration_manifest(manifest, apply=True)
    rollback_migration_import(first["rollback_token"])
    if table == "memory_links":
        row["actor"] = "new-import-owner"
    second = import_migration_manifest(manifest, apply=True)
    storage.init_db()
    before = graph(snapshot())
    replay = rollback_migration_import(first["rollback_token"])
    assert all(count == 0 for count in replay["rolled_back"].values())
    assert graph(snapshot()) == before
    assert row in snapshot()[table]
    assert rollback_migration_import(second["rollback_token"])["rolled_back"][table] == 1


def test_wrong_tenant_cannot_consume_graph_import_receipt(isolated_env, monkeypatch):
    base, _newer = seed()
    manifest = {
        "manifest_version": 1,
        "memory_entities": [{"note_id": base, "kind": "service", "value": "receipt-owned"}],
    }
    result = import_migration_manifest(manifest, apply=True)
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "foreign")
    assert all(
        count == 0
        for count in rollback_migration_import(result["rollback_token"])["rolled_back"].values()
    )
    monkeypatch.setattr(edition, "tenant_org_id", lambda: None)
    assert (
        rollback_migration_import(result["rollback_token"])["rolled_back"]["memory_entities"] == 1
    )


@pytest.mark.parametrize("table", ["memory_entities", "memory_links"])
@pytest.mark.parametrize("restart", [False, True])
def test_unconsumed_rollback_preserves_recreated_graph_row(isolated_env, table, restart):
    base = memory_save("Original neutral text", author="server_agent2")["id"]
    other = memory_save("Other neutral note", author="server_agent2")["id"]
    row = (
        {"note_id": base, "kind": "service", "value": "recreated-by-second-import"}
        if table == "memory_entities"
        else {
            "src_id": base,
            "dst_id": other,
            "rel": "extends",
            "actor": "first-import",
            "created_at": "2026-01-01",
        }
    )
    manifest = {"manifest_version": 1, table: [row]}
    first = import_migration_manifest(manifest, apply=True)
    memory_save("Owner edit recomputes graph", id=base, author="server_agent2", links=[])
    assert row not in snapshot()[table]
    second = import_migration_manifest(manifest, apply=True)
    assert second[table]["inserted"]
    if restart:
        storage.init_db()
    before = graph(snapshot())
    undone = rollback_migration_import(first["rollback_token"])
    assert undone["rolled_back"][table] == 0
    assert graph(snapshot()) == before
    assert rollback_migration_import(second["rollback_token"])["rolled_back"][table] == 1


def test_unconsumed_rollback_preserves_entity_rebuilt_by_owner(isolated_env):
    note = memory_save("Original neutral text", author="server_agent2")["id"]
    row = {"note_id": note, "kind": "task", "value": "2104"}
    first = import_migration_manifest({"manifest_version": 1, "memory_entities": [row]}, apply=True)
    memory_save("Current task #2104", id=note, author="server_agent2")
    assert row in snapshot()["memory_entities"]
    undone = rollback_migration_import(first["rollback_token"])
    assert undone["rolled_back"]["memory_entities"] == 0
    assert row in snapshot()["memory_entities"]


def test_unconsumed_rollback_preserves_link_edited_in_place(isolated_env):
    base, other = seed()
    row = {
        "src_id": base,
        "dst_id": other,
        "rel": "extends",
        "actor": "importer",
        "created_at": "2026-01-01",
    }
    first = import_migration_manifest({"manifest_version": 1, "memory_links": [row]}, apply=True)
    with storage.connect() as conn:
        conn.execute(
            "UPDATE memory_links SET actor='local-editor' WHERE src_id=? AND dst_id=? AND rel=?",
            (base, other, "extends"),
        )
    undone = rollback_migration_import(first["rollback_token"])
    assert undone["rolled_back"]["memory_links"] == 0
    assert {**row, "actor": "local-editor"} in snapshot()["memory_links"]


@pytest.mark.parametrize("table", ["memory_entities", "memory_links"])
@pytest.mark.parametrize("replacement", ["replace", "delete_insert"])
def test_native_sql_replacement_revokes_import_ownership(isolated_env, table, replacement):
    base, other = seed()
    row = (
        {"note_id": base, "kind": "service", "value": 'unicode-雪-"-\\-\x00'}
        if table == "memory_entities"
        else {
            "src_id": base,
            "dst_id": other,
            "rel": "extends",
            "actor": "importer",
            "created_at": "2026-01-01",
        }
    )
    result = import_migration_manifest({"manifest_version": 1, table: [row]}, apply=True)
    keys = memory_graph_keys(table)
    with storage.connect() as conn:
        conn.execute("PRAGMA recursive_triggers=OFF")
        if replacement == "delete_insert":
            conn.execute(
                f"DELETE FROM {table} WHERE " + " AND ".join(f"{k}=?" for k in keys),
                tuple(row[k] for k in keys),
            )
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({', '.join(row)}) "
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
    storage.init_db()
    before = graph(snapshot())
    undone = rollback_migration_import(result["rollback_token"])
    assert undone["rolled_back"][table] == 0
    assert graph(snapshot()) == before


def test_graph_rollback_without_provable_ownership_fails_closed(isolated_env):
    note, _other = seed()
    row = {"note_id": note, "kind": "service", "value": "legacy-unproven"}
    result = import_migration_manifest(
        {"manifest_version": 1, "memory_entities": [row]}, apply=True
    )
    # Pre-upgrade imports have receipts and tokens but no durable row ownership.
    with storage.connect() as conn:
        conn.execute("DELETE FROM memory_graph_import_ownership")
    undone = rollback_migration_import(result["rollback_token"])
    assert undone["rolled_back"]["memory_entities"] == 0
    assert row in snapshot()["memory_entities"]


def test_graph_rollback_receipt_cannot_delete_another_imports_rows(isolated_env):
    note, _other = seed()
    tokens = []
    rows = []
    for value in ("first-owner", "second-owner"):
        row = {"note_id": note, "kind": "service", "value": value}
        rows.append(row)
        tokens.append(
            import_migration_manifest(
                {"manifest_version": 1, "memory_entities": [row]}, apply=True
            )["rollback_token"]
        )
    forged = copy.deepcopy(tokens[0])
    forged["memory_entities"] = [rows[1]]
    assert rollback_migration_import(forged)["rolled_back"]["memory_entities"] == 0
    assert all(row in snapshot()["memory_entities"] for row in rows)
    assert rollback_migration_import(tokens[1])["rolled_back"]["memory_entities"] == 1


def test_rollback_marker_owner_must_match_receipt_without_consuming_it(isolated_env):
    base, newer = seed()
    rows = [
        {"note_id": base, "kind": "service", "value": "first-pending-owner"},
        {"note_id": newer, "kind": "service", "value": "second-pending-owner"},
    ]
    tokens = [
        import_migration_manifest({"manifest_version": 1, "memory_entities": [row]}, apply=True)[
            "rollback_token"
        ]
        for row in rows
    ]
    assert all(token["memory_graph_backfill"] for token in tokens)
    forged = copy.deepcopy(tokens[0])
    forged["memory_graph_backfill"] = copy.deepcopy(tokens[1]["memory_graph_backfill"])
    before = snapshot()["fingerprint"]
    with storage.connect() as conn:
        metadata = list(conn.execute("SELECT key,value FROM meta ORDER BY key"))
    with pytest.raises(ToolError, match="owner.*memory_graph_import_id"):
        rollback_migration_import(forged)
    assert snapshot()["fingerprint"] == before
    with storage.connect() as conn:
        assert [tuple(row) for row in conn.execute("SELECT key,value FROM meta ORDER BY key")] == [
            tuple(row) for row in metadata
        ]
    storage.init_db()
    assert all(row in snapshot()["memory_entities"] for row in rows)
    for token in tokens:
        assert rollback_migration_import(token)["rolled_back"]["memory_entities"] == 1
