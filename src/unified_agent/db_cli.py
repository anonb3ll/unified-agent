"""CLI commands for database operations, backup, restore, and integrity (task #783).

Commands:
  unified-agent db backup [--out <file>] [--pages <int>] [--json]
  unified-agent db restore <backup_path> [--target <file>] [--no-verify] [--json]
  unified-agent db verify [--path <file>] [--json]
  unified-agent db status [--json]
  unified-agent db migrate-constraints [--dry-run] [--path <file>] [--json]
  unified-agent db org-partition [--apply | --downgrade] [--path <file>] [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import (
    config,
    constraint_migration,
    org_partition_migration,
    post_init,
    storage,
    storage_backup,
)


def run_db_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unified-agent db",
        description="Database operations: backup, restore, integrity, and schema verification.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # backup
    p_backup = subparsers.add_parser("backup", help="Take an online atomic backup of memory.db")
    p_backup.add_argument(
        "--out", "-o", help="Target backup file path (default: UA_BACKUP_DIR timestamped)"
    )
    p_backup.add_argument(
        "--pages", type=int, default=100, help="Pages to copy per step (default 100, -1 for all)"
    )
    p_backup.add_argument("--json", action="store_true", help="Emit result as JSON")

    # restore
    p_restore = subparsers.add_parser(
        "restore", help="Restore database from a verified backup file"
    )
    p_restore.add_argument("backup_path", help="Path to the backup file to restore from")
    p_restore.add_argument(
        "--target", "-t", help="Target database path (default: active memory.db)"
    )
    p_restore.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip pre-restore integrity check (not recommended)",
    )
    p_restore.add_argument("--json", action="store_true", help="Emit result as JSON")

    # verify
    p_verify = subparsers.add_parser("verify", help="Check database integrity and consistency")
    p_verify.add_argument(
        "--path", "-p", help="Database file path to inspect (default: active memory.db)"
    )
    p_verify.add_argument("--json", action="store_true", help="Emit check details as JSON")

    # status
    p_status = subparsers.add_parser(
        "status", help="Show database path, schema version, and integrity"
    )
    p_status.add_argument("--json", action="store_true", help="Emit status as JSON")

    # migrate-constraints (task #2326)
    p_constraints = subparsers.add_parser(
        "migrate-constraints",
        help="Apply, or with --dry-run report, the one-shot #1433 key-constraint rebuild",
    )
    p_constraints.add_argument(
        "--dry-run",
        action="store_true",
        help="Report per table the rows a real run would delete; change nothing",
    )
    p_constraints.add_argument(
        "--path", "-p", help="Database file to inspect (dry run only; default: active memory.db)"
    )
    p_constraints.add_argument("--json", action="store_true", help="Emit the report as JSON")

    # org-partition (task #1433)
    p_partition = subparsers.add_parser(
        "org-partition",
        help="Report, apply or undo the #1433 org_id sentinel backfill (default: report only)",
    )
    partition_mode = p_partition.add_mutually_exclusive_group()
    partition_mode.add_argument(
        "--apply", action="store_true", help="Backfill now; needs UA_ORG_PARTITION=1"
    )
    partition_mode.add_argument(
        "--downgrade", action="store_true", help="Turn the sentinel back into NULL"
    )
    p_partition.add_argument(
        "--path", "-p", help="Database file to inspect (report only; default: active memory.db)"
    )
    p_partition.add_argument("--json", action="store_true", help="Emit the result as JSON")

    args = parser.parse_args(argv)

    if args.subcommand == "migrate-constraints":
        return _migrate_constraints(args)

    if args.subcommand == "org-partition":
        return _org_partition(args)

    if args.subcommand == "backup":
        post_init.register_default_hooks()
        storage.init_db()
        try:
            res = storage_backup.backup_database(dest_path=args.out, pages=args.pages)
            if args.json:
                print(json.dumps(res, indent=2, sort_keys=True))
            else:
                print(f"backup: success -> {res['backup_path']}")
                print(
                    f"        size: {res['file_size_bytes']} bytes, sha256: {res['sha256'][:16]}..."
                )
                print(f"        integrity: {res['integrity']}")
                print(f"        manifest: {res['manifest']}")
            return 0
        except Exception as exc:
            print(f"backup: failed: {exc}", file=sys.stderr)
            return 1

    if args.subcommand == "restore":
        try:
            res = storage_backup.restore_database(
                backup_path=args.backup_path,
                target_path=args.target,
                verify_first=not args.no_verify,
            )
            if args.json:
                print(json.dumps(res, indent=2, sort_keys=True))
            else:
                print(f"restore: success -> {res['restored_to']}")
                print(f"         source: {res['source_backup']}")
                print(
                    f"         size: {res['file_size_bytes']} bytes, sha256: {res['sha256'][:16]}..."
                )
                print(f"         integrity: {res['integrity']}")
                print(f"         manifest: {res['manifest']}")
            return 0
        except Exception as exc:
            print(f"restore: failed: {exc}", file=sys.stderr)
            return 1

    if args.subcommand == "verify":
        db_path = Path(args.path) if args.path else config.db_path()
        res = storage_backup.verify_database_integrity(db_path)
        if args.json:
            print(json.dumps(res, indent=2, sort_keys=True))
        else:
            print(
                f"verify: {res.get('path', str(db_path))} -> status={res['status']} valid={res['valid']}"
            )
            if not res["valid"] and res.get("error"):
                print(f"        error: {res['error']}")
        return 0 if res["valid"] else 1

    if args.subcommand == "status":
        post_init.register_default_hooks()
        storage.init_db()
        integrity = storage.verify_schema_integrity()
        db_path = config.db_path()
        status_info = {
            "db_path": str(db_path),
            "exists": db_path.exists(),
            "size_bytes": db_path.stat().st_size if db_path.exists() else 0,
            "backup_dir": str(config.backup_dir()),
            "schema_version": storage.SCHEMA_VERSION,
            "stored_schema_version": integrity.get("stored_schema_version", 0),
            "schema_integrity": integrity["status"],
            "valid": integrity["valid"],
            "tables_count": integrity.get("tables_count", 0),
        }
        if args.json:
            print(json.dumps(status_info, indent=2, sort_keys=True))
        else:
            print(f"database: {status_info['db_path']} ({status_info['size_bytes']} bytes)")
            print(
                f"schema:   version={status_info['schema_version']} (stored={status_info['stored_schema_version']}) status={status_info['schema_integrity']}"
            )
            print(f"backups:  directory={status_info['backup_dir']}")
        return 0 if status_info["valid"] else 1

    return 2


def _print_constraint_tables(tables: list[dict]) -> None:
    for t in tables:
        if not t["present"]:
            state = "absent (created in the final shape)"
        elif t["rebuild"]:
            state = f"rebuild, {t['rows']} rows, would delete {t['delete']} ({t['delete_reason']})"
        else:
            state = f"already in shape, {t['rows']} rows"
        print(f"  {t['table']}: {state}")


def _migrate_constraints(args) -> int:
    """``db migrate-constraints``: the dry run opens the store read-only."""
    if args.dry_run:
        try:
            if args.path:
                path = Path(args.path).expanduser()
                if not path.is_file():
                    print(f"migrate-constraints: no database at {path}", file=sys.stderr)
                    return 1
                conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
                conn.execute("PRAGMA query_only=ON")
            else:
                path = config.db_path()
                conn = storage.connect_read_only()
            try:
                report = constraint_migration.dry_run(conn)
            finally:
                conn.close()
        except (sqlite3.Error, storage.LiveStoreError) as exc:
            print(f"migrate-constraints: cannot read the store: {exc}", file=sys.stderr)
            return 1
        report["path"] = str(path)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        done = report["marker"]
        print(f"migrate-constraints (dry run, nothing changed): {path}")
        print(
            f"  marker: set at {done.get('at')}, a real run does nothing"
            if done
            else "  marker: not set, a real run would apply"
        )
        _print_constraint_tables(report["tables"])
        print(f"  rows a real run would delete: {report['would_delete_total']}")
        print(f"  backup directory: {report['backup_dir']}")
        return 0
    if args.path:
        print("migrate-constraints: --path is for --dry-run only", file=sys.stderr)
        return 2
    try:
        conn = storage.connect()
        try:
            summary = constraint_migration.run(conn)
        finally:
            conn.close()
    except (constraint_migration.ConstraintMigrationError, storage.LiveStoreError) as exc:
        print(f"migrate-constraints: {exc}", file=sys.stderr)
        return 1
    result = {"applied": summary is not None, "summary": summary}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif summary is None:
        print("migrate-constraints: already applied on this store; nothing done")
    else:
        print(f"migrate-constraints: rebuilt {', '.join(summary['rebuilt']) or 'nothing'}")
        print(f"  deleted: {json.dumps(summary['deleted'], sort_keys=True)}")
        print(f"  backup: {summary['backup']}")
    return 0


def _org_partition_report(args) -> int:
    """The default ``db org-partition``: read-only, and on an unscoped connection."""
    try:
        if args.path:
            path = Path(args.path).expanduser()
            if not path.is_file():
                print(f"org-partition: no database at {path}", file=sys.stderr)
                return 1
        else:
            path = storage.require_live_task_store()
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            report = org_partition_migration.dry_run(conn)
        finally:
            conn.close()
    except (sqlite3.Error, storage.LiveStoreError) as exc:
        print(f"org-partition: cannot read the store: {exc}", file=sys.stderr)
        return 1
    report["path"] = str(path)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    done = report["marker"]
    print(f"org-partition (report only, nothing changed): {path}")
    print(
        f"  marker: set at {done.get('at')}, the store is partitioned"
        if done
        else "  marker: not set, the store is unchanged"
    )
    print(f"  UA_ORG_PARTITION: {'set' if report['enabled'] else 'not set'}")
    for table, count in sorted(report["would_stamp"].items()):
        print(f"  {table}: {count} row(s) to stamp")
    print(f"  rows an apply would stamp: {report['would_stamp_total']}")
    if report["foreign"]:
        print(f"  refused, rows already name an org: {json.dumps(report['foreign'])}")
    print(f"  backup directory: {report['backup_dir']}")
    return 0


def _org_partition(args) -> int:
    if not (args.apply or args.downgrade):
        return _org_partition_report(args)
    if args.path:
        print("org-partition: --path is for the report only", file=sys.stderr)
        return 2
    try:
        conn = storage.connect(scoped=False)
        try:
            if args.apply:
                summary = org_partition_migration.apply(conn)
            else:
                summary = org_partition_migration.downgrade(conn)
        finally:
            conn.close()
    except (org_partition_migration.OrgPartitionError, storage.LiveStoreError) as exc:
        print(f"org-partition: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"changed": summary is not None, "summary": summary}, indent=2))
    elif summary is None:
        print("org-partition: nothing to do on this store")
    elif args.apply:
        print(f"org-partition: stamped {json.dumps(summary['stamped'], sort_keys=True)}")
        print(f"  backup: {summary['backup']}")
    else:
        print(f"org-partition: cleared {json.dumps(summary['cleared'], sort_keys=True)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    return run_db_cli(args)


if __name__ == "__main__":
    sys.exit(main())
