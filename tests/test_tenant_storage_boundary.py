"""Exercise the tenant guard through real storage initialization and opens."""

import json
import sqlite3

import pytest

from unified_agent import config, edition, storage
from unified_agent.tools import tasks


def test_upgrade_refuses_before_connection_side_effects(isolated_env, monkeypatch):
    monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    task = tasks.task_add("preserve legacy task")
    path = config.db_path()

    def snapshot(raw):
        return (
            raw.execute("SELECT * FROM tasks").fetchall(),
            raw.execute("SELECT * FROM meta").fetchall(),
            raw.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall(),
        )

    with sqlite3.connect(path) as raw:
        before = snapshot(raw)
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org_storage_test")

    def forbidden(*args, **kwargs):
        pytest.fail("a refused connection reached permission or beacon writes")

    monkeypatch.setattr(storage, "_restrict_store_permissions", forbidden)
    monkeypatch.setattr(edition, "record_tenant_beacon", forbidden)
    with pytest.raises(storage.LiveStoreError, match="No rows were reassigned"):
        storage.connect()
    with sqlite3.connect(path) as raw:
        assert snapshot(raw) == before
        assert raw.execute("SELECT org_id FROM tasks WHERE id=?", (task["id"],)).fetchone() == (
            None,
        )


def test_real_hosted_initialization_seals_schema_and_rejects_old_writers(
    isolated_env, tmp_path, monkeypatch
):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "fresh-hosted"))
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org_storage_test")
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    storage.init_db()
    created = tasks.task_add("new hosted task")
    with sqlite3.connect(config.db_path()) as raw:
        binding = json.loads(
            raw.execute("SELECT value FROM meta WHERE key='tenant_store_binding_v1'").fetchone()[0]
        )
        assert binding["schema_version"] == raw.execute("PRAGMA schema_version").fetchone()[0]
        assert binding["org_id"] == "org_storage_test"
        with pytest.raises(sqlite3.IntegrityError, match="store tenant mismatch"):
            raw.execute("UPDATE tasks SET org_id=NULL WHERE id=?", (created["id"],))
    assert created["id"] in {row["id"] for row in tasks.task_list(status="all")["tasks"]}
    # The complete initialization path must also remain restart-safe.
    storage.init_db()


def test_connection_is_closed_when_guard_fails(isolated_env, monkeypatch):
    connection = sqlite3.connect(config.db_path())
    monkeypatch.setattr(storage.sqlite3, "connect", lambda *args, **kwargs: connection)

    def refuse(conn):
        raise edition.TenantStoreMismatchError("refused")

    monkeypatch.setattr(edition, "ensure_store_tenant", refuse)
    with pytest.raises(storage.LiveStoreError, match="refused"):
        storage.connect()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
