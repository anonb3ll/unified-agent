"""Tenant (org_id) fence on migration import and rollback (task #1795).

import_migration_manifest and rollback_migration_import looked rows up and
deleted them by bare id. A caller in one tenant could delete another tenant's
rows through a crafted rollback token, and the import's conflict check worked
as an existence/content oracle for another tenant's ids. Imported rows were
also written with a NULL org_id, so they vanished from the importing tenant.

These tests pin the fence: another tenant's row is never deleted, never
reported as unchanged or conflicting, never adopted as a parent, and every
imported row is stamped with the caller's org_id.
"""

import datetime
import sqlite3
import threading

import pytest

from unified_agent import config, storage
from unified_agent.tools import export as export_tools
from unified_agent.tools.export import (
    export_migration_manifest,
    import_migration_manifest,
    rollback_migration_import,
)
from unified_agent.tools.memory import memory_save
from unified_agent.tools.tasks import task_add

ORG_A = "org_a"
ORG_B = "org_b"


@pytest.fixture
def as_org(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")

    def _switch(org: str) -> None:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)

    return _switch


def _switch_home(tmp_path, monkeypatch, name: str) -> None:
    monkeypatch.setenv("UA_HOME", str(tmp_path / name))
    storage.init_db()


def _raw(table: str, row_id) -> dict:
    conn = storage.connect()
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
        return dict(row) if row is not None else {}
    finally:
        conn.close()


def _count(table: str) -> int:
    conn = storage.connect()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# --- rollback ---------------------------------------------------------------


def test_rollback_cannot_delete_another_tenants_rows(isolated_env, as_org):
    as_org(ORG_A)
    note_id = memory_save("org a only note", title="Theirs", author="Desktop_Agent2")["id"]
    task_id = task_add("org a only task")["id"]
    before_note, before_task = _raw("memory", note_id), _raw("tasks", task_id)
    assert before_note and before_task

    as_org(ORG_B)
    foreign = rollback_migration_import({"memory": [note_id], "tasks": [task_id]})
    absent = rollback_migration_import({"memory": ["0123456789ab"], "tasks": [987654]})

    assert _raw("memory", note_id) == before_note
    assert _raw("tasks", task_id) == before_task
    # The response for another tenant's ids is identical to never-existing ids:
    # nothing of the caller's was deleted, so both report zero.
    assert foreign == absent == {"rolled_back": {"tasks": 0, "memory": 0}}


def test_rollback_still_deletes_the_callers_own_import(tmp_path, monkeypatch, isolated_env, as_org):
    as_org(ORG_A)
    parent = task_add("parent")
    task_add("child", parent_id=parent["id"])
    memory_save("note", title="N", author="Desktop_Agent2")
    manifest = export_migration_manifest()["manifest"]

    _switch_home(tmp_path, monkeypatch, "target_home")
    applied = import_migration_manifest(manifest, apply=True)
    assert len(applied["rollback_token"]["tasks"]) == 2
    assert len(applied["rollback_token"]["memory"]) == 1

    # Counts are rows of the caller's tenant actually removed (PR #980 review):
    # a replayed token deletes nothing more and says so.
    first = rollback_migration_import(applied["rollback_token"])
    assert first == {"rolled_back": {"tasks": 2, "memory": 1}}
    assert _count("tasks") == 0
    assert _count("memory") == 0
    again = rollback_migration_import(applied["rollback_token"])
    assert again == {"rolled_back": {"tasks": 0, "memory": 0}}


# --- conflict detection: no oracle ------------------------------------------


@pytest.mark.parametrize("apply", [False, True], ids=["dry_run", "apply"])
def test_import_does_not_reveal_another_tenants_rows(isolated_env, as_org, apply):
    as_org(ORG_A)
    note_id = memory_save("org a secret content", title="Theirs", author="Desktop_Agent2")["id"]
    task_id = task_add("org a secret title")["id"]
    same = export_migration_manifest()["manifest"]
    changed = export_migration_manifest()["manifest"]
    changed["memory"][0]["content"] = "a guess at the content"
    changed["tasks"][0]["title"] = "a guess at the title"
    before_note, before_task = _raw("memory", note_id), _raw("tasks", task_id)

    as_org(ORG_B)
    same_result = import_migration_manifest(same, apply=apply)
    changed_result = import_migration_manifest(changed, apply=apply)

    # Exact-copy and edited guesses must be indistinguishable: neither
    # "unchanged" nor "conflicting_edit" may answer for another tenant's row.
    same_result.pop("rollback_token", None)
    changed_result.pop("rollback_token", None)
    assert same_result == changed_result
    for table, row_id in (("memory", note_id), ("tasks", task_id)):
        assert same_result[table]["unchanged"] == []
        assert same_result[table]["inserted"] == []
        assert same_result[table]["held"] == [{"id": row_id, "reason": "insert_rejected"}]

    # The other tenant's rows are byte-for-byte untouched.
    as_org(ORG_A)
    assert _raw("memory", note_id) == before_note
    assert _raw("tasks", task_id) == before_task


def test_id_collision_reason_is_the_generic_insert_failure(
    tmp_path, monkeypatch, isolated_env, as_org
):
    """A row rejected by any integrity constraint (here a NULL in a NOT NULL
    column, in an empty database) reports the same reason as a primary-key
    collision with another tenant's row, so the reason names no tenant."""
    as_org(ORG_A)
    memory_save("source note", title="S", author="Desktop_Agent2")
    task_add("source task")
    manifest = export_migration_manifest()["manifest"]
    manifest["memory"][0]["content"] = None
    manifest["tasks"][0]["title"] = None

    _switch_home(tmp_path, monkeypatch, "target_home")
    result = import_migration_manifest(manifest, apply=True)

    assert result["memory"]["held"] == [
        {"id": manifest["memory"][0]["id"], "reason": "insert_rejected"}
    ]
    assert result["tasks"]["held"] == [
        {"id": manifest["tasks"][0]["id"], "reason": "insert_rejected"}
    ]
    assert result["rollback_token"] == {"tasks": [], "memory": []}
    assert _count("memory") == 0
    assert _count("tasks") == 0


def test_child_is_not_attached_to_another_tenants_parent(
    tmp_path, monkeypatch, isolated_env, as_org
):
    as_org(ORG_B)
    parent = task_add("source parent")
    child = task_add("source child", parent_id=parent["id"])
    manifest = export_migration_manifest()["manifest"]
    manifest["tasks"] = [t for t in manifest["tasks"] if t["id"] == child["id"]]

    _switch_home(tmp_path, monkeypatch, "target_home")
    as_org(ORG_A)
    foreign_parent = task_add("org a task with the same id")
    assert foreign_parent["id"] == parent["id"]

    as_org(ORG_B)
    result = import_migration_manifest(manifest, apply=True)

    assert result["tasks"]["inserted"] == []
    assert result["tasks"]["held"] == [{"id": child["id"], "reason": "missing_parent"}]
    assert _raw("tasks", child["id"]) == {}


# --- stamping ---------------------------------------------------------------


def test_imported_rows_carry_the_callers_org_id(tmp_path, monkeypatch, isolated_env, as_org):
    as_org(ORG_A)
    parent = task_add("parent")
    child = task_add("child", parent_id=parent["id"])
    note_id = memory_save("note", title="N", author="Desktop_Agent2")["id"]
    manifest = export_migration_manifest()["manifest"]

    _switch_home(tmp_path, monkeypatch, "target_home")
    as_org(ORG_B)
    result = import_migration_manifest(manifest, apply=True)
    assert set(result["tasks"]["inserted"]) == {parent["id"], child["id"]}
    assert result["memory"]["inserted"] == [note_id]

    assert _raw("memory", note_id)["org_id"] == ORG_B
    assert _raw("tasks", parent["id"])["org_id"] == ORG_B
    assert _raw("tasks", child["id"])["org_id"] == ORG_B
    # Visible to the importing tenant, invisible to every other one.
    mine = export_migration_manifest()["manifest"]
    assert {t["id"] for t in mine["tasks"]} == {parent["id"], child["id"]}
    assert [m["id"] for m in mine["memory"]] == [note_id]
    as_org(ORG_A)
    theirs = export_migration_manifest()["manifest"]
    assert theirs["tasks"] == [] and theirs["memory"] == []


# --- same-tenant behavior unchanged -----------------------------------------


def test_same_tenant_reimport_and_conflict_are_unchanged(isolated_env, as_org):
    as_org(ORG_A)
    note_id = memory_save("mine", title="Mine", author="Desktop_Agent2")["id"]
    task_id = task_add("mine")["id"]
    same = export_migration_manifest()["manifest"]
    changed = export_migration_manifest()["manifest"]
    changed["memory"][0]["content"] = "edited elsewhere"
    changed["tasks"][0]["title"] = "edited elsewhere"

    unchanged = import_migration_manifest(same, apply=True)
    assert unchanged["memory"]["unchanged"] == [note_id]
    assert unchanged["tasks"]["unchanged"] == [task_id]

    held = import_migration_manifest(changed, apply=True)
    assert held["memory"]["held"] == [{"id": note_id, "reason": "conflicting_edit"}]
    assert held["tasks"]["held"] == [{"id": task_id, "reason": "conflicting_edit"}]


# --- concurrency: a writer committing mid-import ----------------------------


@pytest.mark.parametrize("apply", [False, True], ids=["dry_run", "apply"])
def test_import_survives_a_writer_committing_after_its_first_read(
    tmp_path, monkeypatch, isolated_env, apply
):
    """PR #980 review: a deferred BEGIN let the import read an older WAL
    snapshot, so a commit by another connection between the first lookup and
    the first insert failed the insert with SQLITE_BUSY_SNAPSHOT. With the
    write reservation taken up front, the other writer waits instead and both
    writes land."""
    memory_save("source note", title="S", author="Desktop_Agent2")
    task_add("source task")
    manifest = export_migration_manifest()["manifest"]
    _switch_home(tmp_path, monkeypatch, "target_home")

    real_local_row = export_tools._local_row
    outcome: dict = {}

    def _other_writer() -> None:
        conn = sqlite3.connect(config.db_path(), timeout=30)
        try:
            now = datetime.datetime.now(datetime.UTC).isoformat()
            conn.execute(
                "INSERT INTO memory (id, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
                ("concurrent01", "written by another agent", now, now),
            )
            conn.commit()
            outcome["ok"] = True
        except sqlite3.Error as exc:  # pragma: no cover - reported via assert
            outcome["error"] = repr(exc)
        finally:
            conn.close()

    writer = threading.Thread(target=_other_writer)

    def _local_row_then_concurrent_commit(*args, **kwargs):
        row = real_local_row(*args, **kwargs)
        if not writer.is_alive() and "ok" not in outcome and "error" not in outcome:
            writer.start()
            # Unblocked writer: finishes well inside this window (the old
            # deferred-BEGIN failure). Reserved writer: still waiting after it.
            writer.join(timeout=1.0)
        return row

    monkeypatch.setattr(export_tools, "_local_row", _local_row_then_concurrent_commit)
    result = import_migration_manifest(manifest, apply=apply)
    writer.join(timeout=30)

    assert outcome == {"ok": True}
    assert len(result["memory"]["inserted"]) == 1
    assert len(result["tasks"]["inserted"]) == 1
    assert _raw("memory", "concurrent01")["content"] == "written by another agent"
    assert _count("memory") == (2 if apply else 1)
    assert _count("tasks") == (1 if apply else 0)
