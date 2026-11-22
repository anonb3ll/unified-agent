"""Online, non-blocking SQLite backup and restore for memory.db (task #783, Phase 3.3).

Uses Python stdlib's `sqlite3.Connection.backup()` to take atomic, consistent snapshots
without interrupting active daemon connections or violating WAL mode semantics.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config, storage


def _compute_sha256(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_database_integrity(db_path: Path | str | None = None) -> dict[str, Any]:
    """Run SQLite integrity and foreign key checks on a database file."""
    path = Path(db_path if db_path is not None else config.db_path()).expanduser()
    if not path.exists():
        return {"status": "missing", "valid": False, "error": f"file not found: {path}"}

    conn = sqlite3.connect(path)
    try:
        cur = conn.execute("PRAGMA quick_check")
        quick_check_rows = [r[0] for r in cur.fetchall()]
        quick_ok = quick_check_rows == ["ok"]

        cur = conn.execute("PRAGMA foreign_key_check")
        fk_errors = cur.fetchall()
        fk_ok = len(fk_errors) == 0

        valid = quick_ok and fk_ok
        return {
            "status": "ok" if valid else "corrupt",
            "valid": valid,
            "quick_check": quick_check_rows,
            "foreign_key_errors": fk_errors,
            "path": str(path),
        }
    except Exception as exc:
        return {"status": "error", "valid": False, "error": str(exc), "path": str(path)}
    finally:
        conn.close()


def backup_database(
    dest_path: Path | str | None = None,
    *,
    pages: int = 100,
    source_conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Take an atomic online backup of the active database into dest_path.

    Args:
        dest_path: Path to target backup file. If None, saves to
                   UA_BACKUP_DIR with timestamp memory_YYYYMMDD_HHMMSS.db.
        pages: Number of pages to copy per step (default 100, -1 copies all at once).
        source_conn: Optional existing open connection to back up.
    """
    if dest_path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        out_dir = config.backup_dir()
        # Owner-only: backup snapshots carry the full memory/task database.
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = out_dir / f"memory_{stamp}.db"
    else:
        dest = Path(dest_path).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)

    close_source = False
    if source_conn is None:
        source_conn = storage.connect()
        close_source = True

    try:
        dest_conn = sqlite3.connect(dest)
        try:
            source_conn.backup(dest_conn, pages=pages)
        finally:
            dest_conn.close()

        check = verify_database_integrity(dest)
        if not check["valid"]:
            raise RuntimeError(f"backup failed integrity verification: {check}")

        checksum = _compute_sha256(dest)

        dest_conn = sqlite3.connect(dest)
        manifest = {}
        try:
            for table in ("tasks", "event_log", "memory", "approvals", "delegations"):
                try:
                    cur = dest_conn.execute(f"SELECT COUNT(*) FROM {table}")
                    manifest[table] = cur.fetchone()[0]
                except Exception:
                    pass
        finally:
            dest_conn.close()

        # Keep the default filename in sync with backup._BACKUP_RE
        # (memory-YYYYMMDDTHHMMSSZ.db) so backup_status()/doctor retention
        # recognize these snapshots.
        if dest_path is None:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            canonical = dest.with_name(f"memory-{stamp}.db")
            dest.rename(canonical)
            dest = canonical

        return {
            "status": "success",
            "backup_path": str(dest),
            "file_size_bytes": dest.stat().st_size,
            "sha256": checksum,
            "integrity": check["status"],
            "manifest": manifest,
            "created_at": storage.now(),
        }
    finally:
        if close_source:
            source_conn.close()


def restore_database(
    backup_path: Path | str,
    target_path: Path | str | None = None,
    *,
    verify_first: bool = True,
) -> dict[str, Any]:
    """Restore target database from backup_path with integrity pre-check and post-check.

    Args:
        backup_path: Path to the verified backup file to restore from.
        target_path: Destination DB path (defaults to config.db_path()).
        verify_first: If True, validates backup file before modifying target.
    """
    src = Path(backup_path).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"backup file does not exist: {src}")

    if verify_first:
        pre_check = verify_database_integrity(src)
        if not pre_check["valid"]:
            raise ValueError(f"refusing restore: backup file failed integrity check: {pre_check}")

    target = Path(target_path if target_path is not None else config.db_path()).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)

    # Guard against restoring a non-UA database (any valid SQLite file passes
    # quick_check) over the authoritative memory.db.
    backup_conn = sqlite3.connect(src)
    try:
        schema_version_row = backup_conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise ValueError(
            f"refusing restore: {src} is not a unified-agent database "
            f"(no schema_version in meta): {exc}"
        ) from exc
    finally:
        backup_conn.close()
    if schema_version_row is None:
        raise ValueError(f"refusing restore: {src} has no schema_version in meta")

    backup_conn = sqlite3.connect(src)
    target_conn = sqlite3.connect(target)
    try:
        backup_conn.backup(target_conn, pages=-1)
    finally:
        backup_conn.close()
        target_conn.close()

    # The restored file may predate tables this process created; the next
    # request-time init_db_once() must migrate it again.
    storage.forget_initialized(target)
    if target.resolve() == Path(config.db_path()).expanduser().resolve():
        storage.forget_initialized(config.db_path())

    post_check = verify_database_integrity(target)
    if not post_check["valid"]:
        raise RuntimeError(f"restore failed post-verification on target {target}: {post_check}")

    target_conn = sqlite3.connect(target)
    manifest = {}
    try:
        for table in ("tasks", "event_log", "memory", "approvals", "delegations"):
            try:
                cur = target_conn.execute(f"SELECT COUNT(*) FROM {table}")
                manifest[table] = cur.fetchone()[0]
            except Exception:
                pass
    finally:
        target_conn.close()

    return {
        "status": "success",
        "restored_to": str(target),
        "source_backup": str(src),
        "file_size_bytes": target.stat().st_size,
        "sha256": _compute_sha256(target),
        "integrity": post_check["status"],
        "manifest": manifest,
        "restored_at": storage.now(),
    }
