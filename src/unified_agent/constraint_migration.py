"""One-shot rebuild that gives existing hubs the #1433 key constraints (task #2326).

``CREATE TABLE IF NOT EXISTS`` never changes a table that already exists, and
SQLite cannot add a foreign key or a NOT NULL to a column in place. A fresh
store gets the final shape from its CREATE statements; this module brings an
older store to the same shape by copying each affected table:

* ``task_comments.task_id`` gains ``REFERENCES tasks(id) ON DELETE CASCADE``.
  A comment whose task no longer exists cannot survive the new key and is
  deleted.
* The single text primary keys of seven tables become ``NOT NULL`` (SQLite
  lets an ordinary ``TEXT PRIMARY KEY`` hold NULL). A row with a NULL key is
  deleted.

Safety rules, in order:

1. One shot. A ``meta`` row (``MARKER_KEY``) records the run; once it exists
   nothing here runs again, on any start.
2. Backup first. Inside the write transaction, before any change, every table
   that will be rebuilt is copied into a new SQLite file under
   ``config.backup_dir()``. The copy is integrity-checked and compared with
   the source row by row (count and digest) before the first change is made.
3. All or nothing. Every rebuild, the marker and the evidence event run in one
   ``BEGIN IMMEDIATE`` transaction. Any error rolls all of it back.
4. Dry run. ``plan`` reads the store and reports, per table, the rows a real
   run would delete, without writing anything (``unified-agent db
   migrate-constraints --dry-run``).

No ``SCHEMA_VERSION`` bump: the rebuilt tables keep every column, so older
code opens the stricter schema unchanged, and the marker, not the version,
records that the rebuild ran. The way back is the backup file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)

#: ``meta`` key whose presence means the rebuild has run on this store.
MARKER_KEY = "constraint_rebuild_2326"
#: Evidence channel of the one event a real run that changed tables records.
EVIDENCE_CHANNEL = "agent.storage.constraints_rebuilt"
#: Backup file name prefix; ``backup.prune_backups`` only prunes ``memory-*``.
BACKUP_PREFIX = "constraints-2326"


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _task_comments_fk(sql: str) -> str:
    return re.sub(
        r"\btask_id\s+INTEGER\s+NOT\s+NULL\b(?!\s+REFERENCES\b)",
        "task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE",
        sql,
        count=1,
        flags=re.IGNORECASE,
    )


def _text_pk_not_null(sql: str) -> str:
    return re.sub(
        r"\bTEXT\s+PRIMARY\s+KEY\b(?!\s+NOT\s+NULL\b)",
        "TEXT PRIMARY KEY NOT NULL",
        sql,
        count=1,
        flags=re.IGNORECASE,
    )


def _orphan_comments(_conn: sqlite3.Connection, _table: str) -> str:
    return (
        "task_id IS NULL OR NOT EXISTS (SELECT 1 FROM tasks WHERE tasks.id = task_comments.task_id)"
    )


def _null_text_pk(conn: sqlite3.Connection, table: str) -> str:
    keys = [r for r in conn.execute(f"PRAGMA table_info({_quote(table)})") if r[5]]
    if len(keys) != 1 or keys[0][3]:
        return "0"
    return f"{_quote(keys[0][1])} IS NULL"


@dataclass(frozen=True, slots=True)
class Rebuild:
    """One table to rebuild: how its CREATE text changes and which rows cannot stay."""

    table: str
    transform: Callable[[str], str]
    doomed: Callable[[sqlite3.Connection, str], str]
    reason: str
    #: Check this table's foreign keys before commit (the key is new).
    new_foreign_key: bool = False


REBUILDS: tuple[Rebuild, ...] = (
    Rebuild(
        "task_comments",
        _task_comments_fk,
        _orphan_comments,
        "comment whose task no longer exists",
        new_foreign_key=True,
    ),
    *(
        Rebuild(table, _text_pk_not_null, _null_text_pk, "row with a NULL primary key")
        for table in (
            "memory_embeddings",
            "memory_recall_votes",
            "node_acceptance",
            "ui_session_workos",
            "node_dispatch_cwd",
            "node_dispatch_resume",
            "node_dispatch_wait",
        )
    ),
)


class ConstraintMigrationError(RuntimeError):
    """The rebuild could not be completed; the store was left unchanged."""


def _table_sql(conn: sqlite3.Connection, table: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row and row[0] else None


def marker(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The recorded run on this store, or ``None`` when it has not run."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (MARKER_KEY,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        value = {"raw": row[0]}
    return value if isinstance(value, dict) else {"raw": value}


def plan(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per table: whether it needs the rebuild and how many rows that would delete.

    Read-only. A table that does not exist yet needs nothing: it will be
    created from the current CREATE statement, which already has the shape.
    """
    out: list[dict[str, Any]] = []
    for spec in REBUILDS:
        sql = _table_sql(conn, spec.table)
        entry: dict[str, Any] = {
            "table": spec.table,
            "present": sql is not None,
            "rebuild": False,
            "rows": 0,
            "delete": 0,
            "delete_reason": spec.reason,
        }
        if sql is not None:
            quoted = _quote(spec.table)
            entry["rows"] = int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
            if spec.transform(sql) != sql:
                entry["rebuild"] = True
                where = spec.doomed(conn, spec.table)
                entry["delete"] = int(
                    conn.execute(f"SELECT COUNT(*) FROM {quoted} WHERE {where}").fetchone()[0]
                )
        out.append(entry)
    return out


def dry_run(conn: sqlite3.Connection) -> dict[str, Any]:
    """What a real run would do on this store. Writes nothing."""
    tables = plan(conn)
    done = marker(conn)
    pending = [t for t in tables if t["rebuild"]]
    return {
        "dry_run": True,
        "marker": done,
        "would_run": done is None,
        "tables": tables,
        "rebuild": [t["table"] for t in pending],
        "would_delete": {t["table"]: t["delete"] for t in pending},
        "would_delete_total": sum(t["delete"] for t in pending),
        "backup_dir": str(config.backup_dir()),
    }


# --- backup -----------------------------------------------------------------------


def _private_dir(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(directory)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PermissionError(f"backup directory is not a real directory: {directory}")
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise PermissionError(f"backup directory is not owned by the current user: {directory}")
    if st.st_mode & 0o022:
        raise PermissionError(f"backup directory is writable by other users: {directory}")


def _digest(conn: sqlite3.Connection, table: str) -> tuple[int, str]:
    """Row count and a digest of every row in rowid order."""
    hasher = hashlib.sha256()
    count = 0
    for row in conn.execute(f"SELECT * FROM {_quote(table)} ORDER BY rowid"):
        hasher.update(repr(tuple(row)).encode())
        count += 1
    return count, hasher.hexdigest()


def write_backup(
    conn: sqlite3.Connection,
    tables: list[str],
    directory: Path,
    stamp: str,
    *,
    prefix: str = BACKUP_PREFIX,
    purpose: str = f"pre-rebuild copy for task #2326 ({MARKER_KEY})",
) -> dict[str, Any]:
    """Copy ``tables`` into a new verified SQLite file; return its path and counts.

    Runs on ``conn`` inside the caller's write transaction, so the copy is
    exactly the state the rebuild starts from. The file is renamed into place
    only after ``integrity_check`` passes and every table's rows match.
    """
    _private_dir(directory)
    # A retry after a failed run can land in the same second; never overwrite.
    destination = directory / f"{prefix}-{stamp}.db"
    attempt = 1
    while destination.exists():
        attempt += 1
        destination = directory / f"{prefix}-{stamp}-{attempt}.db"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}-", suffix=".tmp", dir=directory
    )
    os.close(fd)
    temporary = Path(temporary_name)
    source: dict[str, tuple[int, str]] = {}
    try:
        backup = sqlite3.connect(temporary)
        try:
            backup.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            backup.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [
                    ("schema_version", version[0] if version else None),
                    ("purpose", purpose),
                    ("source", str(config.db_path())),
                    ("created_at", stamp),
                    ("tables", json.dumps(tables)),
                ],
            )
            for table in tables:
                backup.execute(_table_sql(conn, table))
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_quote(table)})")]
                names = ", ".join(_quote(c) for c in cols)
                marks = ", ".join("?" for _ in cols)
                cursor = conn.execute(f"SELECT {names} FROM {_quote(table)} ORDER BY rowid")
                while batch := cursor.fetchmany(500):
                    backup.executemany(
                        f"INSERT INTO {_quote(table)} ({names}) VALUES ({marks})",
                        [tuple(r) for r in batch],
                    )
                source[table] = _digest(conn, table)
            backup.commit()
        finally:
            backup.close()
        check = sqlite3.connect(f"file:{temporary}?mode=ro", uri=True)
        try:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            copied = {table: _digest(check, table) for table in tables}
        finally:
            check.close()
        if integrity != "ok":
            raise ConstraintMigrationError(f"backup integrity check failed: {integrity}")
        mismatched = sorted(t for t in tables if copied[t] != source[t])
        if mismatched:
            raise ConstraintMigrationError(f"backup does not match the store: {mismatched}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
        for sidecar in (f"{temporary}-wal", f"{temporary}-shm", f"{temporary}-journal"):
            Path(sidecar).unlink(missing_ok=True)
    return {
        "path": str(destination),
        "integrity": integrity,
        "rows": {table: source[table][0] for table in tables},
    }


# --- rebuild ----------------------------------------------------------------------


def _rebuild_table(conn: sqlite3.Connection, spec: Rebuild) -> int:
    """Copy ``spec.table`` into its new shape inside the caller's transaction.

    Returns the rows deleted. Indexes and triggers on the table are recreated;
    triggers and views elsewhere that name it are parked and replayed, because
    SQLite re-checks them when the scratch copy is renamed over the original.
    The AUTOINCREMENT high-water mark is kept, so a deleted id is never reused.
    """
    table = spec.table
    old_sql = _table_sql(conn, table)
    if old_sql is None:
        return 0
    new_sql = spec.transform(old_sql)
    head = re.match(
        r"""^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"""
        rf"""(?:"{re.escape(table)}"|`{re.escape(table)}`|\[{re.escape(table)}\]|{re.escape(table)})(?=\s*\()""",
        new_sql,
        flags=re.IGNORECASE,
    )
    if new_sql == old_sql:
        return 0
    if head is None:
        raise ConstraintMigrationError(f"{table}: stored CREATE statement not recognized")
    scratch = f"{table}__rebuild"
    quoted, quoted_scratch = _quote(table), _quote(scratch)
    cols = ", ".join(_quote(r[1]) for r in conn.execute(f"PRAGMA table_info({quoted})").fetchall())
    own = [
        r[0]
        for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index', 'trigger') "
            "AND sql IS NOT NULL ORDER BY rowid",
            (table,),
        )
    ]
    mention = re.compile(rf"\b{re.escape(table)}\b", re.IGNORECASE)
    dependents = [
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE type IN ('trigger', 'view') "
            "AND tbl_name!=? AND sql IS NOT NULL ORDER BY rowid",
            (table,),
        )
        if mention.search(r[2])
    ]
    high_water = None
    if re.search(r"\bAUTOINCREMENT\b", old_sql, re.IGNORECASE):
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
        high_water = int(row[0]) if row else None

    where = spec.doomed(conn, table)
    deleted = int(conn.execute(f"SELECT COUNT(*) FROM {quoted} WHERE {where}").fetchone()[0])
    for kind, name, _sql in dependents:
        conn.execute(f"DROP {kind.upper()} IF EXISTS {_quote(name)}")
    conn.execute(f"DELETE FROM {quoted} WHERE {where}")
    conn.execute(f"DROP TABLE IF EXISTS {quoted_scratch}")
    conn.execute("CREATE TABLE " + quoted_scratch + new_sql[head.end() :])
    conn.execute(
        f"INSERT INTO {quoted_scratch} ({cols}) SELECT {cols} FROM {quoted} ORDER BY rowid"
    )
    conn.execute(f"DROP TABLE {quoted}")
    conn.execute(f"ALTER TABLE {quoted_scratch} RENAME TO {quoted}")
    for sql in own:
        conn.execute(sql)
    for _kind, _name, sql in dependents:
        conn.execute(sql)
    if high_water is not None:
        conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (high_water, table)
        )
        conn.execute(
            "INSERT INTO sqlite_sequence (name, seq) SELECT ?, ? "
            "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = ?)",
            (table, high_water, table),
        )
    return deleted


def run(
    conn: sqlite3.Connection,
    *,
    backup_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Apply the rebuild once. Returns the recorded summary, or ``None`` if it already ran.

    A store that needs no rebuild (a fresh one) only gets the marker: no
    backup, no event. Otherwise the backup is written and verified, the tables
    are rebuilt, the foreign keys checked, the marker written and one evidence
    event appended, all in one transaction. Any failure rolls the store back
    and raises ``ConstraintMigrationError``; a backup written before the
    failure is left in place (it is a copy of the unchanged store).
    """
    if marker(conn) is not None:
        return None
    if conn.in_transaction:
        conn.commit()
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    # PRAGMA foreign_keys is a no-op inside a transaction, so it is switched
    # before BEGIN: with it on, DROP TABLE would run ON DELETE actions.
    conn.execute("PRAGMA foreign_keys=OFF")
    publish: dict[str, Any] | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if marker(conn) is not None:  # another process won the race
                conn.rollback()
                return None
            pending = [t for t in plan(conn) if t["rebuild"]]
            summary: dict[str, Any] = {
                "task": 2326,
                "at": moment.isoformat(timespec="seconds"),
                "rebuilt": [t["table"] for t in pending],
                "deleted": {},
                "rows_before": {t["table"]: t["rows"] for t in pending},
                "backup": None,
            }
            if pending:
                backup = write_backup(
                    conn,
                    summary["rebuilt"],
                    Path(backup_dir) if backup_dir is not None else config.backup_dir(),
                    stamp,
                )
                summary["backup"] = backup["path"]
                logger.warning("constraint rebuild (#2326): backup written to %s", backup["path"])
                by_table = {spec.table: spec for spec in REBUILDS}
                for entry in pending:
                    spec = by_table[entry["table"]]
                    summary["deleted"][spec.table] = _rebuild_table(conn, spec)
                    if spec.new_foreign_key:
                        bad = conn.execute(
                            f"PRAGMA foreign_key_check({_quote(spec.table)})"
                        ).fetchall()
                        if bad:
                            raise ConstraintMigrationError(
                                f"{spec.table}: {len(bad)} row(s) still violate the new key"
                            )
                    after = int(
                        conn.execute(f"SELECT COUNT(*) FROM {_quote(spec.table)}").fetchone()[0]
                    )
                    expected = entry["rows"] - summary["deleted"][spec.table]
                    if after != expected:
                        raise ConstraintMigrationError(
                            f"{spec.table}: {after} row(s) after the copy, expected {expected}"
                        )
            summary["deleted_total"] = sum(summary["deleted"].values())
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (MARKER_KEY, json.dumps(summary, sort_keys=True)),
            )
            if pending:
                from . import events

                event_id, _won, publish = events.record_event_once_on_connection(
                    conn, EVIDENCE_CHANNEL, payload=summary
                )
                summary["event_id"] = event_id
            conn.commit()
        except BaseException as exc:
            if conn.in_transaction:
                conn.rollback()
            if isinstance(exc, ConstraintMigrationError | sqlite3.Error | OSError):
                raise ConstraintMigrationError(
                    f"constraint rebuild (#2326) failed and was rolled back: {exc}"
                ) from exc
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    if summary["rebuilt"]:
        logger.warning(
            "constraint rebuild (#2326): rebuilt %s, deleted %s, backup %s",
            ", ".join(summary["rebuilt"]),
            json.dumps(summary["deleted"], sort_keys=True),
            summary["backup"],
        )
    if publish is not None:
        from . import events

        try:
            events.publish(publish)
        except Exception:  # fan-out is best effort; the event is durable
            logger.debug("constraint rebuild event fan-out failed", exc_info=True)
    return summary
