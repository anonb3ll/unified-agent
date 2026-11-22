"""Backups must stay private even in a pre-existing readable directory."""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from unified_agent import backup

NOW = datetime(2026, 10, 2, tzinfo=UTC)


def _database(path: Path, value: str = "synthetic") -> Path:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (value,))
    return path


@pytest.mark.parametrize("mask", [0o000, 0o022, 0o077])
@pytest.mark.parametrize("existing", [False, True])
def test_backup_is_owner_only_before_sqlite_writes(tmp_path, monkeypatch, mask, existing):
    source = _database(tmp_path / "source.db")
    directory = tmp_path / "backups"
    if existing:
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
    connect = sqlite3.connect
    modes = []

    def checked_connect(path, *args, **kwargs):
        if not kwargs.get("uri") and Path(path) != source:
            modes.append(stat.S_IMODE(Path(path).stat().st_mode) if Path(path).exists() else None)
        return connect(path, *args, **kwargs)

    monkeypatch.setattr(backup.sqlite3, "connect", checked_connect)
    old = os.umask(mask)
    try:
        result = backup.create_backup(source=source, directory=directory, now=NOW)
    finally:
        os.umask(old)
    assert modes == [0o600]
    assert stat.S_IMODE(Path(result["path"]).stat().st_mode) == 0o600
    assert result["verification"]["ok"]
    assert not list(directory.glob(".*.tmp"))


def test_backup_does_not_open_predictable_stale_temp_symlink(tmp_path):
    source = _database(tmp_path / "source.db")
    victim = _database(tmp_path / "other.db", "leave-me-alone")
    before = victim.read_bytes()
    directory = tmp_path / "backups"
    directory.mkdir()
    stale = directory / "memory-20261002T000000Z.tmp"
    stale.symlink_to(victim)

    result = backup.create_backup(source=source, directory=directory, now=NOW)

    assert result["verification"]["ok"]
    assert victim.read_bytes() == before
    assert stale.is_symlink()


def test_backup_refuses_directory_writable_by_other_users(tmp_path):
    source = _database(tmp_path / "source.db")
    directory = tmp_path / "backups"
    directory.mkdir()
    directory.chmod(0o777)
    with pytest.raises(PermissionError, match="writable"):
        backup.create_backup(source=source, directory=directory, now=NOW)
    assert list(directory.iterdir()) == []


def test_failed_verification_removes_only_its_private_temp(tmp_path, monkeypatch):
    source = _database(tmp_path / "source.db")
    directory = tmp_path / "backups"
    directory.mkdir()
    stale = directory / "memory-20261002T000000Z.tmp"
    stale.write_text("pre-existing file")
    monkeypatch.setattr(backup, "verify_backup", lambda _path: {"ok": False, "detail": "fixture"})
    with pytest.raises(RuntimeError, match="integrity"):
        backup.create_backup(source=source, directory=directory, now=NOW)
    assert list(directory.iterdir()) == [stale]
    assert stale.read_text() == "pre-existing file"


def test_backup_refuses_a_symlinked_directory(tmp_path):
    source = _database(tmp_path / "source.db")
    real = tmp_path / "elsewhere"
    real.mkdir(mode=0o700)
    directory = tmp_path / "backups"
    directory.symlink_to(real, target_is_directory=True)
    with pytest.raises(PermissionError, match="symlink"):
        backup.create_backup(source=source, directory=directory, now=NOW)
    assert list(real.iterdir()) == []


def test_backup_refuses_a_directory_owned_by_another_user(tmp_path, monkeypatch):
    source = _database(tmp_path / "source.db")
    directory = tmp_path / "backups"
    directory.mkdir(mode=0o700)
    # Simulate another owner without needing root: report a different uid.
    real_uid = os.getuid()
    monkeypatch.setattr(backup.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(PermissionError, match="not owned"):
        backup.create_backup(source=source, directory=directory, now=NOW)
    assert list(directory.iterdir()) == []


def test_backup_refuses_a_destination_that_is_a_file(tmp_path):
    source = _database(tmp_path / "source.db")
    directory = tmp_path / "backups"
    directory.write_text("not a directory")
    with pytest.raises((NotADirectoryError, FileExistsError)):
        backup.create_backup(source=source, directory=directory, now=NOW)
