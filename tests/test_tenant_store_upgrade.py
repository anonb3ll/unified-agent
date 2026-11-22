"""A hosted stamp cannot silently adopt legacy rows or race older writers."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading

import pytest

from unified_agent import config, edition
from unified_agent.tools import tasks

ORG = "org_upgrade_test"


@pytest.fixture(autouse=True)
def hosted(monkeypatch, isolated_env):
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", ORG)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)


def store(tmp_path):
    path = tmp_path / "upgrade.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE tasks(id INTEGER PRIMARY KEY, org_id TEXT, author TEXT, payload TEXT);"
    )
    return path, conn


def snapshot(conn):
    return tuple(conn.iterdump())


@pytest.mark.parametrize("orgs", [[None], ["foreign"], [ORG, None], [ORG, "foreign"]])
def test_ambiguous_store_refuses_without_data_or_schema_mutation(tmp_path, orgs):
    _path, conn = store(tmp_path)
    with conn:
        conn.executemany(
            "INSERT INTO tasks(org_id, author, payload) VALUES (?, 'original-owner', 'keep')",
            [(org,) for org in orgs],
        )
    before = snapshot(conn)
    with pytest.raises(edition.TenantStoreMismatchError, match="verified backup"):
        edition.ensure_store_tenant(conn)
    assert snapshot(conn) == before
    assert not conn.in_transaction
    conn.close()


def test_unchanged_environment_upgrade_refuses_before_hiding_a_real_task(monkeypatch):
    # The previous published derivation ignored the hosted variable entirely.
    with monkeypatch.context() as old_code:
        old_code.setattr(edition, "current_org_id", lambda: None)
        old = tasks.task_add("preserve the existing task")
        assert old["id"] in {row["id"] for row in tasks.task_list(status="all")["tasks"]}
    conn = sqlite3.connect(config.db_path())
    before = (
        conn.execute("SELECT * FROM tasks").fetchall(),
        conn.execute("SELECT * FROM meta").fetchall(),
        conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall(),
    )
    with pytest.raises(edition.TenantStoreMismatchError, match="No rows were reassigned"):
        edition.ensure_store_tenant(conn)
    assert (
        conn.execute("SELECT * FROM tasks").fetchall(),
        conn.execute("SELECT * FROM meta").fetchall(),
        conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall(),
    ) == before
    assert conn.execute("SELECT org_id FROM tasks WHERE id=?", (old["id"],)).fetchone() == (None,)
    conn.close()


def test_legacy_populated_table_without_org_column_is_not_adopted(tmp_path):
    _path, conn = store(tmp_path)
    conn.executescript(
        "DROP TABLE tasks; CREATE TABLE tasks(id INTEGER); INSERT INTO tasks VALUES(1)"
    )
    before = snapshot(conn)
    with pytest.raises(edition.TenantStoreMismatchError):
        edition.ensure_store_tenant(conn)
    assert snapshot(conn) == before
    conn.close()


def test_empty_preinit_database_is_allowed_but_must_be_sealed_after_initialization(tmp_path):
    conn = sqlite3.connect(tmp_path / "fresh.db")
    edition.ensure_store_tenant(conn)
    assert snapshot(conn) == ("BEGIN TRANSACTION;", "COMMIT;")
    with pytest.raises(edition.TenantStoreMismatchError):
        edition.ensure_store_tenant(conn, initialized=True)
    conn.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    edition.ensure_store_tenant(conn, initialized=True)
    assert conn.execute("SELECT 1 FROM meta WHERE key=?", (edition._STORE_TENANT_KEY,)).fetchone()
    conn.close()


def test_consistent_real_store_seals_without_changing_domain_rows():
    conn = sqlite3.connect(config.db_path())
    edition.ensure_store_tenant(conn, initialized=True)
    conn.close()
    created = tasks.task_add("new hosted task after the guard")
    with sqlite3.connect(config.db_path()) as conn:
        before = conn.execute("SELECT * FROM tasks WHERE id=?", (created["id"],)).fetchone()
        edition.ensure_store_tenant(conn)
        assert conn.execute("SELECT * FROM tasks WHERE id=?", (created["id"],)).fetchone() == before
    assert created["id"] in {row["id"] for row in tasks.task_list(status="all")["tasks"]}


@pytest.mark.parametrize("pilot_org", [None, ORG])
def test_unbound_private_and_pilot_stores_keep_their_existing_behavior(
    tmp_path, monkeypatch, pilot_org
):
    monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID")
    if pilot_org is not None:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", pilot_org)
    _path, conn = store(tmp_path)
    with conn:
        conn.execute("INSERT INTO tasks VALUES(1, NULL, 'owner', 'preserved')")
    before = snapshot(conn)
    edition.ensure_store_tenant(conn)
    assert snapshot(conn) == before
    conn.close()


def test_organization_literal_cannot_change_the_guard_sql(tmp_path, monkeypatch):
    target = "org_quote'; DROP TABLE tasks; --"
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", target)
    _path, conn = store(tmp_path)
    edition.ensure_store_tenant(conn)
    with conn:
        conn.execute("INSERT INTO tasks VALUES(1, ?, 'owner', 'retained')", (target,))
    assert conn.execute("SELECT org_id FROM tasks").fetchall() == [(target,)]
    with pytest.raises(sqlite3.IntegrityError, match="store tenant mismatch"):
        conn.execute("INSERT INTO tasks VALUES(2, NULL, 'other', 'rejected')")
    conn.close()


def test_restart_checks_only_attestation_and_schema_generation(tmp_path):
    path, conn = store(tmp_path)
    with conn:
        conn.execute("INSERT INTO tasks VALUES(1, ?, 'owner', 'unaltered')", (ORG,))
    edition.ensure_store_tenant(conn)
    conn.close()
    with sqlite3.connect(path) as restarted:
        statements = []
        restarted.set_trace_callback(statements.append)
        edition.ensure_store_tenant(restarted)
        restarted.set_trace_callback(None)
        assert all('FROM "tasks"' not in statement for statement in statements)
        assert not any(statement.startswith("BEGIN") for statement in statements)
        assert restarted.execute("SELECT * FROM tasks").fetchall() == [
            (1, ORG, "owner", "unaltered")
        ]


@pytest.mark.parametrize("change", ["foreign-org", "missing-org", "core-edition"])
def test_bound_store_refuses_configuration_cutovers(tmp_path, monkeypatch, change):
    _path, conn = store(tmp_path)
    edition.ensure_store_tenant(conn)
    before = snapshot(conn)
    if change == "foreign-org":
        monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "foreign")
    elif change == "missing-org":
        monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID")
    else:
        monkeypatch.setenv("UA_EDITION", "core")
    with pytest.raises(edition.TenantStoreMismatchError):
        edition.ensure_store_tenant(conn)
    assert snapshot(conn) == before
    conn.close()


@pytest.mark.parametrize("bad_org", [None, "foreign"])
def test_guards_reject_older_or_foreign_writers_after_the_proof(tmp_path, bad_org):
    path, conn = store(tmp_path)
    edition.ensure_store_tenant(conn)
    with conn:
        conn.execute("INSERT INTO tasks VALUES(1, ?, 'owner', 'kept')", (ORG,))
    with sqlite3.connect(path) as old_writer:
        with pytest.raises(sqlite3.IntegrityError, match="store tenant mismatch"):
            old_writer.execute(
                "INSERT OR IGNORE INTO tasks VALUES(2, ?, 'other', 'bad')", (bad_org,)
            )
        with pytest.raises(sqlite3.IntegrityError, match="store tenant mismatch"):
            old_writer.execute("UPDATE tasks SET org_id=? WHERE id=1", (bad_org,))
    assert conn.execute("SELECT * FROM tasks").fetchall() == [(1, ORG, "owner", "kept")]
    conn.close()


def test_new_org_table_invalidates_attestation_and_gets_a_guard(tmp_path):
    _path, conn = store(tmp_path)
    edition.ensure_store_tenant(conn)
    conn.execute('CREATE TABLE "future""table"(org_id TEXT, payload TEXT)')
    edition.ensure_store_tenant(conn)
    with pytest.raises(sqlite3.IntegrityError, match="store tenant mismatch"):
        conn.execute('INSERT INTO "future""table" VALUES(NULL, \'bad\')')
    conn.close()


def test_schema_change_with_ambiguous_rows_refuses_without_resealing(tmp_path):
    _path, conn = store(tmp_path)
    edition.ensure_store_tenant(conn)
    with conn:
        conn.execute("CREATE TABLE later(org_id TEXT)")
        conn.execute("INSERT INTO later VALUES(NULL)")
    before = snapshot(conn)
    with pytest.raises(edition.TenantStoreMismatchError):
        edition.ensure_store_tenant(conn)
    assert snapshot(conn) == before
    conn.close()


def test_failed_trigger_install_rolls_back_all_guard_changes(tmp_path):
    _path, conn = store(tmp_path)
    before = snapshot(conn)
    installed = []

    def authorizer(action, *_args):
        if action == sqlite3.SQLITE_CREATE_TRIGGER:
            installed.append(True)
            if len(installed) == 2:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    with pytest.raises(sqlite3.DatabaseError):
        edition.ensure_store_tenant(conn)
    conn.set_authorizer(None)
    assert len(installed) == 2
    assert snapshot(conn) == before
    assert not conn.in_transaction
    conn.close()


def test_initializer_keeps_ownership_of_its_transaction(tmp_path):
    _path, conn = store(tmp_path)
    before = snapshot(conn)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO tasks VALUES(1, ?, 'owner', 'pending')", (ORG,))
    edition.ensure_store_tenant(conn, initialized=True)
    assert conn.in_transaction
    conn.rollback()
    assert snapshot(conn) == before
    conn.close()


@pytest.mark.parametrize(
    "raw", ["broken", "[]", '{"version": 1}', "x" * 4097], ids=["json", "type", "keys", "size"]
)
def test_malformed_binding_is_never_overwritten(tmp_path, raw):
    _path, conn = store(tmp_path)
    with conn:
        conn.execute("INSERT INTO meta VALUES(?, ?)", (edition._STORE_TENANT_KEY, raw))
    before = snapshot(conn)
    with pytest.raises(edition.TenantStoreMismatchError):
        edition.ensure_store_tenant(conn)
    assert snapshot(conn) == before
    conn.close()


def test_concurrent_initializers_share_one_durable_binding(tmp_path):
    path, conn = store(tmp_path)
    conn.close()
    barrier = threading.Barrier(2)
    results = queue.Queue()

    def seal():
        with sqlite3.connect(path) as worker:
            try:
                barrier.wait(timeout=5)
                edition.ensure_store_tenant(worker)
                results.put(None)
            except Exception as exc:
                results.put(exc)

    workers = [threading.Thread(target=seal) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert not worker.is_alive()
    assert [results.get_nowait() for _ in workers] == [None, None]
    with sqlite3.connect(path) as conn:
        raw = conn.execute(
            "SELECT value FROM meta WHERE key=?", (edition._STORE_TENANT_KEY,)
        ).fetchone()[0]
        assert json.loads(raw)["org_id"] == ORG


def test_writer_that_wins_before_the_guard_is_not_silently_adopted(tmp_path):
    path, writer = store(tmp_path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO tasks VALUES(1, NULL, 'old-owner', 'retained')")
    waiting = threading.Event()
    results = queue.Queue()

    def seal():
        with sqlite3.connect(path) as worker:
            worker.set_trace_callback(
                lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None
            )
            try:
                edition.ensure_store_tenant(worker)
            except Exception as exc:
                results.put(exc)

    worker = threading.Thread(target=seal)
    worker.start()
    try:
        assert waiting.wait(timeout=5)
    finally:
        writer.commit()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert isinstance(results.get_nowait(), edition.TenantStoreMismatchError)
    assert writer.execute("SELECT * FROM tasks").fetchall() == [(1, None, "old-owner", "retained")]
    assert writer.execute("SELECT * FROM meta").fetchall() == []
    writer.close()
