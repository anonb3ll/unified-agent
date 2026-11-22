"""Tests for Task #783: SQLite online backup, restore, and integrity verification."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from unified_agent import config, db_cli, events, storage, storage_backup


@pytest.fixture(autouse=True)
def init_clean_db(tmp_path, monkeypatch):
    db_file = tmp_path / "memory.db"
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    monkeypatch.setattr(config, "backup_dir", lambda: backup_dir)
    storage.init_db()
    yield


def _seed_sample_data():
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, project, domain, lane) "
            "VALUES (1, 'Task One', 'done', 'high', '2026-09-06T10:00:00', '2026-09-06T10:30:00', 'unified-agent', 'hobby', 'default')"
        )
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, project, domain, lane) "
            "VALUES (2, 'Task Two', 'open', 'med', '2026-09-06T10:05:00', '2026-09-06T10:05:00', 'unified-agent', 'hobby', 'default')"
        )
        conn.commit()
    finally:
        conn.close()
    events.record_event("agent.task.created", payload={"task_id": 1}, originator="Desktop_agent3")


def test_backup_database_default(tmp_path):
    _seed_sample_data()

    res = storage_backup.backup_database()
    assert res["status"] == "success"
    assert res["integrity"] == "ok"
    assert res["manifest"]["tasks"] == 2
    assert res["manifest"]["event_log"] == 1

    backup_path = Path(res["backup_path"])
    assert backup_path.exists()
    assert backup_path.parent == config.backup_dir()


def test_backup_database_custom_path(tmp_path):
    _seed_sample_data()

    dest = tmp_path / "custom_backup.db"
    res = storage_backup.backup_database(dest_path=dest)
    assert res["status"] == "success"
    assert Path(res["backup_path"]) == dest
    assert dest.exists()
    assert dest.stat().st_size > 0


def test_restore_database(tmp_path):
    _seed_sample_data()

    dest = tmp_path / "restore_snapshot.db"
    storage_backup.backup_database(dest_path=dest)

    # Mutate live database
    conn = storage.connect()
    try:
        conn.execute("DELETE FROM tasks")
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert count == 0
    finally:
        conn.close()

    # Restore from snapshot
    restore_res = storage_backup.restore_database(backup_path=dest)
    assert restore_res["status"] == "success"
    assert restore_res["integrity"] == "ok"

    # Verify tasks are back
    conn = storage.connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert count == 2
    finally:
        conn.close()


def test_restore_refuses_corrupt_file(tmp_path):
    corrupt_file = tmp_path / "corrupt.db"
    corrupt_file.write_bytes(b"This is not a valid SQLite database header string!!")

    with pytest.raises(ValueError, match="refusing restore"):
        storage_backup.restore_database(backup_path=corrupt_file)


def test_verify_database_integrity():
    check = storage_backup.verify_database_integrity()
    assert check["valid"] is True
    assert check["status"] == "ok"

    missing_check = storage_backup.verify_database_integrity("/path/does/not/exist.db")
    assert missing_check["valid"] is False
    assert missing_check["status"] == "missing"


def test_schema_integrity_and_version():
    ver = storage.get_schema_version()
    assert ver == storage.SCHEMA_VERSION

    report = storage.verify_schema_integrity()
    assert report["valid"] is True
    assert report["status"] == "ok"
    assert report["stored_schema_version"] == storage.SCHEMA_VERSION
    assert len(report["missing_core_tables"]) == 0


def test_db_cli(tmp_path, capsys):
    _seed_sample_data()

    code = db_cli.run_db_cli(["backup", "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "success"

    code = db_cli.run_db_cli(["status", "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["valid"] is True
    assert data["schema_version"] == storage.SCHEMA_VERSION

    code = db_cli.run_db_cli(["verify", "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["valid"] is True


def test_backup_uses_canonical_filename_for_retention(tmp_path):
    _seed_sample_data()
    res = storage_backup.backup_database()
    name = Path(res["backup_path"]).name
    assert name.startswith("memory-") and name.endswith("Z.db"), name


def test_backup_dir_is_owner_only(tmp_path):
    _seed_sample_data()
    res = storage_backup.backup_database()
    import stat

    mode = stat.S_IMODE(Path(res["backup_path"]).parent.stat().st_mode)
    assert mode & 0o077 == 0, oct(mode)


def test_restore_rejects_non_ua_sqlite_file(tmp_path):
    foreign = tmp_path / "foreign.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="not a unified-agent database"):
        storage_backup.restore_database(foreign, target_path=tmp_path / "target.db")


def test_schema_integrity_flags_missing_core_index():
    conn = storage.connect()
    try:
        conn.execute("DROP INDEX idx_tasks_status_priority")
        conn.commit()
    finally:
        conn.close()

    report = storage.verify_schema_integrity()
    assert report["valid"] is False
    assert "idx_tasks_status_priority" in report["missing_core_indexes"]
