"""The explicit backfill of NULL ``org_id`` to the sentinel, and its way back (#1433).

Nothing here runs on deploy. ``apply`` needs ``UA_ORG_PARTITION=1`` and an
operator running ``unified-agent db org-partition --apply``; ``init_db`` only
keeps an already partitioned store whole (``reconcile``). Multi-tenancy stays
off: the sentinel is the one org, and ``org_partition`` hides it.

Safety rules follow ``constraint_migration``:

1. One shot. The ``meta`` row ``org_partition.MARKER_KEY`` records the run and a
   second ``apply`` does nothing. Only ``apply`` writes it, so a fresh store has
   no marker and no wall-clock value for a store comparison to trip on.
2. Backup first. Inside the write transaction, every table with rows to change
   is copied to a verified file under ``config.backup_dir()``.
3. All or nothing. The backfill, the triggers, the marker and the evidence event
   share one ``BEGIN IMMEDIATE`` transaction.
4. Refusal. A tenanted process, a sealed hosted store, or any row that already
   names an org stops the run; no row is ever reassigned from a real org.

``downgrade`` is the exact reverse and needs no flag: a recovery must not depend
on the setting being right. No ``SCHEMA_VERSION`` bump: the schema is unchanged
apart from the triggers.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config, constraint_migration, edition
from .org_partition import (
    MARKER_KEY,
    SCOPED_TABLES,
    SENTINEL_ORG_ID,
    TRIGGER_PREFIX,
    trigger_statements,
)

logger = logging.getLogger(__name__)

#: Evidence channels of a real apply and a real downgrade.
APPLIED_CHANNEL = "agent.storage.org_partition_applied"
DOWNGRADED_CHANNEL = "agent.storage.org_partition_downgraded"
#: Backup file name prefix; ``backup.prune_backups`` only prunes ``memory-*``.
BACKUP_PREFIX = "org-partition-1433"
#: The memory fences compare a child's org with its note's, so they cannot run
#: while the two tables are half stamped. They are restored in the same transaction.
_FENCE_TRIGGER = re.compile(r"^memory_\w+_org_fence_(insert|update)$")


class OrgPartitionError(RuntimeError):
    """The backfill or downgrade could not run; the store was left unchanged."""


def marker(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The recorded run on this store, or ``None`` when it is not partitioned."""
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


def _scoped_present(conn: sqlite3.Connection) -> list[str]:
    tables: list[str] = []
    for table in sorted(SCOPED_TABLES):
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if any(row[1] == "org_id" and not row[3] for row in info):
            tables.append(table)
    return tables


def plan(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per scoped table: rows, rows a backfill would stamp, rows naming another org."""
    out: list[dict[str, Any]] = []
    for table in _scoped_present(conn):
        rows, nulls, stamped = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(org_id IS NULL), 0), "
            f'COALESCE(SUM(org_id IS ?), 0) FROM "{table}"',
            (SENTINEL_ORG_ID,),
        ).fetchone()
        out.append(
            {
                "table": table,
                "rows": int(rows),
                "null": int(nulls),
                "sentinel": int(stamped),
                "foreign": int(rows) - int(nulls) - int(stamped),
            }
        )
    return out


def dry_run(conn: sqlite3.Connection) -> dict[str, Any]:
    """What a real ``apply`` would do on this store. Writes nothing."""
    tables = plan(conn)
    done = marker(conn)
    foreign = {t["table"]: t["foreign"] for t in tables if t["foreign"]}
    return {
        "dry_run": True,
        "marker": done,
        "enabled": config.org_partition_enabled(),
        "would_run": done is None and not foreign,
        "tables": tables,
        "would_stamp": {t["table"]: t["null"] for t in tables if t["null"]},
        "would_stamp_total": sum(t["null"] for t in tables),
        "foreign": foreign,
        "backup_dir": str(config.backup_dir()),
    }


@contextmanager
def _org_fences_parked(conn: sqlite3.Connection) -> Iterator[None]:
    """Drop the memory org fences for the body; recreate them in the same transaction."""
    parked = [
        (row[0], row[1])
        for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
        if _FENCE_TRIGGER.match(row[0])
    ]
    for name, _sql in parked:
        conn.execute(f'DROP TRIGGER "{name}"')
    yield
    for _name, sql in parked:
        conn.execute(sql)


def _set_org(
    conn: sqlite3.Connection, tables: list[str], old: str | None, new: str | None
) -> dict[str, int]:
    """Rewrite ``org_id`` from ``old`` to ``new`` on every table, event_log included."""
    from . import events

    changed: dict[str, int] = {}
    with _org_fences_parked(conn):
        for table in tables:
            if table == "event_log":
                # The chain does not hash org_id, so no entry_hash changes.
                with events.ledger_maintenance(conn, "org partition #1433"):
                    cursor = conn.execute(
                        "UPDATE event_log SET org_id=? WHERE org_id IS ?", (new, old)
                    )
            else:
                cursor = conn.execute(
                    f'UPDATE "{table}" SET org_id=? WHERE org_id IS ?', (new, old)
                )
            if cursor.rowcount:
                changed[table] = cursor.rowcount
    return changed


def _install_triggers(conn: sqlite3.Connection, tables: list[str]) -> None:
    for table in tables:
        for statement in trigger_statements(table):
            conn.execute(statement)


def _record(conn: sqlite3.Connection, channel: str, summary: dict[str, Any]):
    from . import events

    return events.record_event_once_on_connection(conn, channel, payload=summary)


def _publish(publish: dict[str, Any] | None) -> None:
    if publish is None:
        return
    from . import events

    try:
        events.publish(publish)
    except Exception:  # fan-out is best effort; the event is durable
        logger.debug("org partition event fan-out failed", exc_info=True)


def _refuse_tenant(conn: sqlite3.Connection) -> None:
    if edition.tenant_org_id() is not None:
        raise OrgPartitionError(
            "this process stamps a real organization; the sentinel is for a single-org hub only"
        )
    if edition._store_tenant_binding(conn) is not None:
        raise OrgPartitionError("this store is sealed to a hosted tenant; nothing was changed")


def apply(
    conn: sqlite3.Connection, *, backup_dir: Path | None = None, now: datetime | None = None
) -> dict[str, Any] | None:
    """Backfill once on an unscoped connection. ``None`` if the store is already done."""
    if marker(conn) is not None:
        return None
    if not config.org_partition_enabled():
        raise OrgPartitionError("UA_ORG_PARTITION is not set; nothing was changed")
    _refuse_tenant(conn)
    if conn.in_transaction:
        conn.commit()
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    publish = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        if marker(conn) is not None:  # another process won the race
            conn.rollback()
            return None
        entries = plan(conn)
        foreign = {t["table"]: t["foreign"] for t in entries if t["foreign"]}
        if foreign:
            raise OrgPartitionError(f"rows already name an organization: {foreign}")
        tables = [t["table"] for t in entries]
        pending = [t["table"] for t in entries if t["null"]]
        summary: dict[str, Any] = {
            "task": 1433,
            "at": moment.isoformat(timespec="seconds"),
            "sentinel": SENTINEL_ORG_ID,
            "tables": tables,
            "rows_before": {t["table"]: t["null"] for t in entries if t["null"]},
            "backup": None,
        }
        if pending:
            backup = constraint_migration.write_backup(
                conn,
                pending,
                Path(backup_dir) if backup_dir is not None else config.backup_dir(),
                moment.strftime("%Y%m%dT%H%M%SZ"),
                prefix=BACKUP_PREFIX,
                purpose=f"pre-backfill copy for task #1433 ({MARKER_KEY})",
            )
            summary["backup"] = backup["path"]
            logger.warning("org partition (#1433): backup written to %s", backup["path"])
        # First, so the event is part of what the backfill stamps.
        _event_id, _won, publish = _record(conn, APPLIED_CHANNEL, summary)
        stamped = _set_org(conn, tables, None, SENTINEL_ORG_ID)
        summary["stamped"] = stamped
        left = {
            table: count
            for table in tables
            if (
                count := conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE org_id IS NULL'
                ).fetchone()[0]
            )
        }
        if left:
            raise OrgPartitionError(f"rows still NULL after the backfill: {left}")
        _install_triggers(conn, tables)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            (MARKER_KEY, json.dumps(summary, sort_keys=True)),
        )
        conn.commit()
    except BaseException as exc:
        if conn.in_transaction:
            conn.rollback()
        if isinstance(
            exc,
            OrgPartitionError
            | constraint_migration.ConstraintMigrationError
            | sqlite3.Error
            | OSError,
        ):
            raise OrgPartitionError(f"org partition failed and was rolled back: {exc}") from exc
        raise
    _publish(publish)
    logger.warning(
        "org partition (#1433): stamped %s, backup %s",
        json.dumps(summary["stamped"], sort_keys=True),
        summary["backup"],
    )
    return summary


def reconcile(conn: sqlite3.Connection) -> dict[str, int]:
    """Keep a partitioned store whole. ``init_db`` calls this; it writes no clock value.

    A table that gained ``org_id`` since the backfill needs its rows stamped and
    its triggers. On a store with no marker this does nothing at all.
    """
    if marker(conn) is None:
        return {}
    tables = _scoped_present(conn)
    triggers = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        if row[0].startswith(TRIGGER_PREFIX)
    }
    missing = [t for t in tables if f"{TRIGGER_PREFIX}update_{t}" not in triggers]
    unstamped = [t["table"] for t in plan(conn) if t["null"]]
    if not missing and not unstamped:
        return {}
    if conn.in_transaction:
        conn.commit()
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = _set_org(conn, unstamped, None, SENTINEL_ORG_ID)
        _install_triggers(conn, missing)
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return changed


def downgrade(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Undo ``apply`` on an unscoped connection. ``None`` if the store is not partitioned.

    Removes the triggers and the marker and turns every sentinel back to NULL,
    so older code reads the store as it always did. Rows written while
    partitioned go back with them.
    """
    if marker(conn) is None:
        return None
    if conn.in_transaction:
        conn.commit()
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if marker(conn) is None:
            conn.rollback()
            return None
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            if row[0].startswith(TRIGGER_PREFIX):
                conn.execute(f'DROP TRIGGER "{row[0]}"')
        cleared = _set_org(conn, _scoped_present(conn), SENTINEL_ORG_ID, None)
        conn.execute("DELETE FROM meta WHERE key=?", (MARKER_KEY,))
        summary = {
            "task": 1433,
            "at": moment.isoformat(timespec="seconds"),
            "cleared": cleared,
        }
        _event_id, _won, publish = _record(conn, DOWNGRADED_CHANNEL, summary)
        conn.commit()
    except BaseException as exc:
        if conn.in_transaction:
            conn.rollback()
        if isinstance(exc, sqlite3.Error | OSError):
            raise OrgPartitionError(f"org partition downgrade rolled back: {exc}") from exc
        raise
    _publish(publish)
    logger.warning("org partition (#1433): downgraded, cleared %s", json.dumps(cleared))
    return summary
