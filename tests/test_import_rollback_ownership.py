"""Import rollback keeps later row revisions and their dependent data (#2143)."""

import copy

import pytest

from unified_agent import edition, embeddings, storage
from unified_agent.tools.export import (
    export_migration_manifest,
    import_migration_manifest,
    rollback_migration_import,
)
from unified_agent.tools.memory import memory_save
from unified_agent.tools.tasks import task_add


@pytest.fixture(autouse=True)
def offline_embeddings(monkeypatch):
    def unavailable(_text):
        raise RuntimeError("rollback ownership tests are lexical only")

    monkeypatch.setattr(embeddings, "generate_embedding", unavailable)


def snapshot():
    return export_migration_manifest()["manifest"]


def import_note():
    note = memory_save("Original neutral text", author="Desktop_Agent2")["id"]
    row = next(row for row in snapshot()["memory"] if row["id"] == note)
    with storage.connect() as conn:
        storage.hard_delete_memory(conn, note, org_id=edition.tenant_org_id())
    result = import_migration_manifest(
        {"manifest_version": 1, "memory": [row], "memory_entities": [], "memory_links": []},
        apply=True,
    )
    assert result["memory"]["inserted"] == [note]
    return note, row, result["rollback_token"]


def import_tasks():
    parent = task_add("Imported parent")["id"]
    child = task_add("Imported child", parent_id=parent)["id"]
    rows = [row for row in snapshot()["tasks"] if row["id"] in (parent, child)]
    with storage.connect() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (parent,))
    result = import_migration_manifest({"manifest_version": 1, "tasks": rows}, apply=True)
    assert set(result["tasks"]["inserted"]) == {parent, child}
    return parent, child, rows, result["rollback_token"]


def test_owner_edit_prevents_note_rollback_and_later_graph_cascade(isolated_env):
    note, _row, first = import_note()
    memory_save("Owner-edited note", id=note, author="Desktop_Agent2")
    entity = {"note_id": note, "kind": "service", "value": "second-import"}
    second = import_migration_manifest(
        {"manifest_version": 1, "memory_entities": [entity]}, apply=True
    )

    result = rollback_migration_import(first)

    assert result["rolled_back"]["memory"] == 0
    assert any(row["id"] == note for row in snapshot()["memory"])
    assert entity in snapshot()["memory_entities"]
    assert (
        rollback_migration_import(second["rollback_token"])["rolled_back"]["memory_entities"] == 1
    )


def test_later_graph_alone_prevents_unchanged_note_cascade(isolated_env):
    note, _row, first = import_note()
    entity = {"note_id": note, "kind": "service", "value": "later-owned"}
    second = import_migration_manifest(
        {"manifest_version": 1, "memory_entities": [entity]}, apply=True
    )
    assert rollback_migration_import(first)["rolled_back"]["memory"] == 0
    assert entity in snapshot()["memory_entities"]
    assert (
        rollback_migration_import(second["rollback_token"])["rolled_back"]["memory_entities"] == 1
    )


@pytest.mark.parametrize("mutation", ["same_bytes", "delete_reinsert", "replace"])
def test_note_revision_and_recreation_revoke_old_rollback(isolated_env, mutation):
    note, row, token = import_note()
    with storage.connect() as conn:
        if mutation == "same_bytes":
            conn.execute("UPDATE memory SET content=content WHERE id=?", (note,))
        else:
            if mutation == "delete_reinsert":
                conn.execute("DELETE FROM memory WHERE id=?", (note,))
            conn.execute("PRAGMA recursive_triggers=OFF")
            conn.execute(
                f"INSERT OR REPLACE INTO memory ({', '.join(row)}, org_id) "
                f"VALUES ({', '.join('?' for _ in row)}, ?)",
                (*row.values(), edition.tenant_org_id()),
            )
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 0
    assert any(row["id"] == note for row in snapshot()["memory"])


def test_receipt_cannot_authorize_arbitrary_note_ids(isolated_env):
    _note, _row, token = import_note()
    native = memory_save("Native unrelated note", author="Desktop_Agent2")["id"]
    forged = copy.deepcopy(token)
    forged["memory"] = [native]
    forged.pop("memory_graph_backfill", None)
    assert rollback_migration_import(forged)["rolled_back"]["memory"] == 0
    assert any(row["id"] == native for row in snapshot()["memory"])


def test_legacy_row_id_token_fails_closed(isolated_env):
    note, _row, token = import_note()
    assert rollback_migration_import({"memory": [note]})["rolled_back"]["memory"] == 0
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 1


@pytest.mark.parametrize("later", ["native_child", "edited_child"])
def test_task_parent_cannot_cascade_later_or_changed_children(isolated_env, later):
    parent, child, _rows, token = import_tasks()
    if later == "native_child":
        native = task_add("Later native child", parent_id=parent)["id"]
    else:
        native = child
        with storage.connect() as conn:
            conn.execute("UPDATE tasks SET title='Owner-edited child' WHERE id=?", (child,))

    result = rollback_migration_import(token)

    assert any(row["id"] == parent for row in snapshot()["tasks"])
    assert any(row["id"] == native for row in snapshot()["tasks"])
    assert result["rolled_back"]["tasks"] == (1 if later == "native_child" else 0)


def test_unmodified_rows_still_roll_back_after_restart(isolated_env):
    note, _row, note_token = import_note()
    parent, child, _rows, task_token = import_tasks()
    storage.init_db()
    assert rollback_migration_import(note_token)["rolled_back"]["memory"] == 1
    assert rollback_migration_import(task_token)["rolled_back"]["tasks"] == 2
    assert all(row["id"] not in (parent, child) for row in snapshot()["tasks"])
    assert all(row["id"] != note for row in snapshot()["memory"])


@pytest.mark.parametrize("table", ["memory", "tasks"])
def test_foreign_tenant_cannot_consume_row_receipt(isolated_env, monkeypatch, table):
    if table == "memory":
        _note, _row, token = import_note()
        count = 1
    else:
        _parent, _child, _rows, token = import_tasks()
        count = 2
    monkeypatch.setattr(edition, "tenant_org_id", lambda: "foreign")
    assert not any(rollback_migration_import(token)["rolled_back"].values())
    monkeypatch.setattr(edition, "tenant_org_id", lambda: None)
    assert rollback_migration_import(token)["rolled_back"][table] == count
    assert not any(rollback_migration_import(token)["rolled_back"].values())


@pytest.mark.parametrize("mutation", ["native_api", "same_bytes", "delete_reinsert", "replace"])
def test_task_revision_and_recreation_revoke_rollback(isolated_env, mutation):
    from unified_agent.tools.tasks import task_update

    parent, child, rows, token = import_tasks()
    if mutation == "native_api":
        task_update(child, title="Owner-edited child")
    else:
        row = next(row for row in rows if row["id"] == child)
        with storage.connect() as conn:
            if mutation == "same_bytes":
                conn.execute("UPDATE tasks SET title=title WHERE id=?", (child,))
            else:
                if mutation == "delete_reinsert":
                    conn.execute("DELETE FROM tasks WHERE id=?", (child,))
                conn.execute("PRAGMA recursive_triggers=OFF")
                conn.execute(
                    f"INSERT OR REPLACE INTO tasks ({', '.join(row)}, org_id) "
                    f"VALUES ({', '.join('?' for _ in row)}, ?)",
                    (*row.values(), edition.tenant_org_id()),
                )
    assert rollback_migration_import(token)["rolled_back"]["tasks"] == 0
    assert {parent, child} <= {row["id"] for row in snapshot()["tasks"]}


def test_task_dependency_prevents_task_and_ancestor_rollback(isolated_env):
    parent, child, _rows, token = import_tasks()
    native = task_add("Later prerequisite")["id"]
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO task_dependencies(task_id,depends_on_task_id,created_by,created_at) "
            "VALUES (?,?,?,?)",
            (child, native, "Desktop_Agent2", "2026-10-17"),
        )
    assert rollback_migration_import(token)["rolled_back"]["tasks"] == 0
    with storage.connect() as conn:
        assert conn.execute("SELECT 1 FROM task_dependencies WHERE task_id=?", (child,)).fetchone()
    assert {parent, child, native} <= {row["id"] for row in snapshot()["tasks"]}


def test_retained_note_keeps_native_graph_across_startup(isolated_env):
    note, _row, token = import_note()
    entity = {"note_id": note, "kind": "service", "value": "native-new-data"}
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO memory_entities(note_id,kind,value,org_id) VALUES (?,?,?,?)",
            (*entity.values(), edition.tenant_org_id()),
        )
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 0
    storage.init_db()
    assert entity in snapshot()["memory_entities"]


def test_rollback_failure_restores_receipt_and_ownership(isolated_env, monkeypatch):
    note, _row, token = import_note()
    hard_delete = storage.hard_delete_memory

    def fail_after_delete(conn, row_id, *, org_id):
        hard_delete(conn, row_id, org_id=org_id)
        raise RuntimeError("injected transactional delete failure")

    monkeypatch.setattr(storage, "hard_delete_memory", fail_after_delete)
    with pytest.raises(RuntimeError, match="transactional delete failure"):
        rollback_migration_import(token)
    assert any(row["id"] == note for row in snapshot()["memory"])
    monkeypatch.setattr(storage, "hard_delete_memory", hard_delete)
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 1


def test_rolled_back_native_mutation_preserves_import_ownership(isolated_env):
    note, _row, token = import_note()
    with storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE memory SET content='Uncommitted edit' WHERE id=?", (note,))
        conn.rollback()
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 1


def test_import_failure_leaves_no_rows_ownership_or_receipt(isolated_env, monkeypatch):
    from unified_agent.tools import export

    note = memory_save("Source for failed import", author="Desktop_Agent2")["id"]
    row = next(row for row in snapshot()["memory"] if row["id"] == note)
    with storage.connect() as conn:
        storage.hard_delete_memory(conn, note, org_id=edition.tenant_org_id())
    with storage.connect() as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM meta ORDER BY key")]
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='migration_import_row_ownership'"
        ).fetchone()

    def fail_after_ownership(*_args):
        raise RuntimeError("injected import failure")

    monkeypatch.setattr(export, "_preserve_imported_graph", fail_after_ownership)
    with pytest.raises(RuntimeError, match="injected import failure"):
        import_migration_manifest({"manifest_version": 1, "memory": [row]}, apply=True)
    assert all(row["id"] != note for row in snapshot()["memory"])
    with storage.connect() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM meta ORDER BY key")] == before
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='migration_import_row_ownership' "
            "OR (type='trigger' AND name LIKE 'migration_%_owner_%')"
        ).fetchone()


def test_missing_trigger_invalidates_unprovable_old_ownership(isolated_env):
    note, _row, token = import_note()
    with storage.connect() as conn:
        conn.execute("DROP TRIGGER migration_memory_owner_update")
        conn.execute("UPDATE memory SET content=content WHERE id=?", (note,))
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 0
    assert any(row["id"] == note for row in snapshot()["memory"])


def test_mismatched_row_and_graph_receipts_reject_before_consumption(isolated_env):
    from mcp.server.mcpserver.exceptions import ToolError

    _note, _row, first = import_note()
    _other, _other_row, second = import_note()
    forged = copy.deepcopy(first)
    forged["migration_import_id"] = second["migration_import_id"]
    with pytest.raises(ToolError, match="must match"):
        rollback_migration_import(forged)
    assert rollback_migration_import(first)["rolled_back"]["memory"] == 1
    assert rollback_migration_import(second)["rolled_back"]["memory"] == 1


@pytest.mark.parametrize("trim", ["empty", "missing"])
def test_trimmed_token_keeps_retained_imported_note_startup_guard(isolated_env, trim):
    note, _row, token = import_note()
    entity = {"note_id": note, "kind": "service", "value": "native-after-import"}
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO memory_entities(note_id,kind,value,org_id) VALUES (?,?,?,?)",
            (*entity.values(), edition.tenant_org_id()),
        )
    forged = copy.deepcopy(token)
    if trim == "empty":
        forged["memory"] = []
    else:
        forged.pop("memory")
    assert rollback_migration_import(forged)["rolled_back"]["memory"] == 0
    storage.init_db()
    assert entity in snapshot()["memory_entities"]


@pytest.mark.parametrize(
    "reference", ["workflow_instances", "workflow_task_steps", "routine_runs", "task_comments"]
)
def test_later_workflow_routine_and_comments_preserve_tasks(isolated_env, reference):
    parent, child, _rows, token = import_tasks()
    with storage.connect() as conn:
        if reference in ("workflow_instances", "workflow_task_steps"):
            conn.execute(
                "INSERT INTO workflow_instances "
                "(id,name,spec,inputs,bindings,outputs,step_index,task_id,status,execution,creator,created_at) "
                "VALUES ('later','later','{}','{}','{}','{}',0,?,'waiting','{}','Desktop_Agent2','now')",
                (child if reference == "workflow_instances" else None,),
            )
            if reference == "workflow_task_steps":
                conn.execute(
                    "INSERT INTO workflow_task_steps(task_id,instance_id,step_index) VALUES (?,'later',0)",
                    (child,),
                )
        elif reference == "routine_runs":
            routine = conn.execute(
                "INSERT INTO routines(title,trigger,lane,project,domain,concurrency,catch_up,"
                "cursor_at,created_at,updated_at) VALUES "
                "('later','api','dispatch-ready','unified-agent','engineering','skip','none','now','now','now')"
            ).lastrowid
            conn.execute(
                "INSERT INTO routine_runs(routine_id,fire_key,scheduled_for,status,task_id,created_at) "
                "VALUES (?,'later','now','created',?,'now')",
                (routine, child),
            )
        else:
            conn.execute(
                "INSERT INTO task_comments(task_id,author,body,thread_revision,created_at) "
                "VALUES (?,'Desktop_Agent2','Later discussion',1,'now')",
                (child,),
            )
    assert rollback_migration_import(token)["rolled_back"]["tasks"] == 0
    assert {parent, child} <= {row["id"] for row in snapshot()["tasks"]}
    with storage.connect() as conn:
        assert conn.execute(f"SELECT 1 FROM {reference} WHERE task_id=?", (child,)).fetchone()


def test_later_native_recall_feedback_preserves_note(isolated_env):
    from unified_agent import recall_curation

    note, _row, token = import_note()
    recall_curation.record_upvote(note)
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT upvoted_count FROM memory_recall_votes WHERE note_id=?", (note,)
            ).fetchone()[0]
            == 1
        )
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 0
    with storage.connect() as conn:
        assert (
            conn.execute(
                "SELECT upvoted_count FROM memory_recall_votes WHERE note_id=?", (note,)
            ).fetchone()[0]
            == 1
        )


def test_rollback_operational_reference_inventory_matches_initialized_schema(isolated_env):
    from unified_agent.tools.export import _MEMORY_IMPORT_REFERENCES, _TASK_IMPORT_REFERENCES

    tasks, notes = set(), set()
    with storage.connect() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql NOT LIKE 'CREATE VIRTUAL%'"
        ).fetchall()
        for table in tables:
            quoted = '"' + table[0].replace('"', '""') + '"'
            for column in conn.execute(f"PRAGMA table_info({quoted})"):
                if column[1] in ("task_id", "root_task_id", "depends_on_task_id"):
                    tasks.add((table[0], column[1]))
                if column[1] in ("note_id", "memory_id", "src_id", "dst_id"):
                    notes.add((table[0], column[1]))
            for fk in conn.execute(f"PRAGMA foreign_key_list({quoted})"):
                if fk[2] == "tasks" and table[0] != "tasks":
                    tasks.add((table[0], fk[3]))
                if fk[2] == "memory":
                    notes.add((table[0], fk[3]))
    assert tasks - {("task_embeddings", "task_id")} == set(_TASK_IMPORT_REFERENCES)
    # Source/vector embeddings and long-note chunks are rebuildable; graph
    # references have the stronger per-composite-key import-owner check in
    # the deletion planner. hard_delete_memory removes the chunk rows.
    # memory_versions (task #2097) is the note's own edit history, erased with
    # it by a hard delete (foreign key cascade plus a delete trigger).
    assert notes - {
        ("memory_embeddings", "note_id"),
        ("memory_chunks", "note_id"),
        ("memory_versions", "note_id"),
        ("memory_entities", "note_id"),
        ("memory_links", "src_id"),
        ("memory_links", "dst_id"),
    } == set(_MEMORY_IMPORT_REFERENCES)


@pytest.mark.parametrize("field", ["superseded_by", "contradicts", "evidence"])
def test_structured_note_reference_without_graph_preserves_target(isolated_env, field):
    import json

    note, _row, token = import_note()
    source = memory_save("Later structured source", author="Desktop_Agent2")["id"]
    value = (
        note
        if field == "superseded_by"
        else json.dumps([f"memory:{note}" if field == "evidence" else note])
    )
    with storage.connect() as conn:
        conn.execute(f"UPDATE memory SET {field}=? WHERE id=?", (value, source))
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 0
    assert {note, source} <= {row["id"] for row in snapshot()["memory"]}


def test_native_note_task_evidence_preserves_task_subtree(isolated_env):
    parent, child, _rows, token = import_tasks()
    memory_save("Later task evidence", evidence=[f"task:{child}"], author="Desktop_Agent2")
    assert rollback_migration_import(token)["rolled_back"]["tasks"] == 0
    assert {parent, child} <= {row["id"] for row in snapshot()["tasks"]}


def test_comment_decision_reference_preserves_note(isolated_env):
    import json

    note, _row, token = import_note()
    task = task_add("Later decision discussion")["id"]
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO task_comments(task_id,author,body,refs,thread_revision,created_at) "
            "VALUES (?,'Desktop_Agent2','Later decision',?,1,'now')",
            (task, json.dumps({"decision_id": note})),
        )
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 0
    assert any(row["id"] == note for row in snapshot()["memory"])


def import_related_notes():
    import json

    target = memory_save("Related target", author="Desktop_Agent2")["id"]
    source = memory_save("Related source", author="Desktop_Agent2")["id"]
    with storage.connect() as conn:
        conn.execute("UPDATE memory SET contradicts=? WHERE id=?", (json.dumps([target]), source))
    rows = [row for row in snapshot()["memory"] if row["id"] in (target, source)]
    with storage.connect() as conn:
        for note in (source, target):
            storage.hard_delete_memory(conn, note, org_id=edition.tenant_org_id())
    imported = import_migration_manifest(
        {"manifest_version": 1, "memory": rows, "memory_entities": [], "memory_links": []},
        apply=True,
    )
    return source, target, imported["rollback_token"]


def test_jointly_owned_structured_notes_still_roll_back(isolated_env):
    _source, _target, token = import_related_notes()
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 2


def test_structured_retention_propagates_from_retained_source(isolated_env):
    import json

    source, target, token = import_related_notes()
    native = memory_save("Later source reference", author="Desktop_Agent2")["id"]
    with storage.connect() as conn:
        conn.execute(
            "UPDATE memory SET evidence=? WHERE id=?", (json.dumps([f"memory:{source}"]), native)
        )
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 0
    assert {source, target} <= {row["id"] for row in snapshot()["memory"]}


def test_legacy_scalar_receipt_retains_unprovable_surviving_note_guard(isolated_env):
    import json

    note, _row, token = import_note()
    entity = {"note_id": note, "kind": "service", "value": "native-with-legacy-receipt"}
    with storage.connect() as conn:
        conn.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (
                json.dumps(edition.tenant_org_id()),
                "memory_graph_import_receipt:" + token["migration_import_id"],
            ),
        )
        conn.execute(
            "INSERT INTO memory_entities(note_id,kind,value,org_id) VALUES (?,?,?,?)",
            (*entity.values(), edition.tenant_org_id()),
        )
    token["memory"] = []
    assert rollback_migration_import(token)["rolled_back"]["memory"] == 0
    storage.init_db()
    assert entity in snapshot()["memory_entities"]


def test_unrelated_operational_data_and_derived_vectors_do_not_block_rollback(isolated_env):
    from unified_agent import recall_curation

    note, _row, note_token = import_note()
    parent, child, _rows, task_token = import_tasks()
    other_note = memory_save("Unrelated feedback note", author="Desktop_Agent2")["id"]
    recall_curation.record_upvote(other_note)
    other_task = task_add("Unrelated comment task")["id"]
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO task_comments(task_id,author,body,thread_revision,created_at) "
            "VALUES (?,'Desktop_Agent2','Unrelated discussion',1,'now')",
            (other_task,),
        )
        conn.execute("INSERT INTO task_embeddings(task_id,embedding) VALUES (?,x'00')", (child,))
        conn.execute("INSERT INTO memory_embeddings(note_id,embedding) VALUES (?,x'00')", (note,))
        conn.execute(
            "INSERT INTO memory_chunks(note_id,chunk_index,embedding,org_id) VALUES (?,0,x'00',?)",
            (note, edition.tenant_org_id()),
        )
    assert rollback_migration_import(note_token)["rolled_back"]["memory"] == 1
    with storage.connect() as conn:
        assert (
            conn.execute("SELECT 1 FROM memory_chunks WHERE note_id=?", (note,)).fetchone() is None
        )
    assert rollback_migration_import(task_token)["rolled_back"]["tasks"] == 2
    assert {other_task} <= {row["id"] for row in snapshot()["tasks"]}
    assert {other_note} <= {row["id"] for row in snapshot()["memory"]}
    assert parent not in {row["id"] for row in snapshot()["tasks"]}
