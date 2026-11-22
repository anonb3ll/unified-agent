#!/usr/bin/env python3
"""Prove a complete SQLite backup can restore into an isolated UA_HOME.

Usage: uv run python scripts/db_restore_rehearsal.py [--out PATH]
           [--from-replica]

--from-replica (task #1197) restores from the continuous WAL replica at
UA_REPLICA_GCS_URI instead of a local snapshot backup, then runs the audit
chain verification against the restored database. Requires `litestream` on
PATH (or ~/.local/bin) and UA_REPLICA_GCS_URI in the environment or in
~/.unified_agent/service.env.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
os.environ["UA_STATE_SYNC"] = "0"

_LITESTREAM_TIMEOUT_S = 300.0


def _check(checks: list[dict], name: str, ok: bool, detail: str = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def _snapshot(path: Path) -> dict:
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        table_rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        tables = [row[0] for row in table_rows]
        # sqlite-vec is optional at runtime. Its virtual table definition is
        # still included in ``tables``, but a bare sqlite3 connection cannot
        # read it unless the extension is loaded. Count portable tables and
        # compare the complete table set, including the optional index.
        counts = {
            name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name, sql in table_rows
            if "USING vec0" not in (sql or "")
        }
        schema = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        return {"tables": tables, "counts": counts, "schema_version": schema}
    finally:
        conn.close()


def _litestream_binary() -> str | None:
    found = shutil.which("litestream")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "litestream"
    return str(candidate) if candidate.is_file() else None


def _service_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = Path.home() / ".unified_agent" / "service.env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if sep:
                env[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    return env


def run_replica_rehearsal() -> dict:
    """Restore from the live GCS WAL replica into a scratch UA_HOME and verify.

    Checks: litestream present, replica URI configured, restore succeeds with a
    clean integrity check, the restored db opens and carries the schema version,
    and the audit hash chain verifies against the restored copy — the property
    a cold-standby promotion actually depends on. Read-only against the live
    hub; litestream only ever writes into the temp dir.
    """
    checks: list[dict] = []
    binary = _litestream_binary()
    _check(checks, "litestream_binary_found", binary is not None, binary or "not on PATH")
    file_env = _service_env()
    replica_uri = os.environ.get("UA_REPLICA_GCS_URI") or file_env.get("UA_REPLICA_GCS_URI")
    _check(checks, "replica_uri_configured", bool(replica_uri), replica_uri or "unset")
    if not binary or not replica_uri:
        return {
            "ok": False,
            "mode": "replica",
            "generated_at": datetime.now(UTC).isoformat(),
            "checks": checks,
            "summary": {"total": len(checks), "passed": sum(c["ok"] for c in checks)},
        }

    env = {
        **os.environ,
        "UA_REPLICA_GCS_URI": replica_uri,
    }
    if not env.get("GOOGLE_APPLICATION_CREDENTIALS") and file_env.get(
        "GOOGLE_APPLICATION_CREDENTIALS"
    ):
        env["GOOGLE_APPLICATION_CREDENTIALS"] = file_env["GOOGLE_APPLICATION_CREDENTIALS"]

    prior_home = os.environ.get("UA_HOME")
    try:
        with tempfile.TemporaryDirectory(prefix="replica-restore-rehearsal-") as tmp:
            restore_home = Path(tmp) / "restore"
            restore_home.mkdir()
            target = restore_home / "memory.db"
            proc = subprocess.run(
                [
                    binary,
                    "restore",
                    "-o",
                    str(target),
                    "-integrity-check",
                    "quick",
                    replica_uri,
                ],
                capture_output=True,
                text=True,
                timeout=_LITESTREAM_TIMEOUT_S,
                env=env,
            )
            _check(
                checks,
                "litestream_restore_succeeds",
                proc.returncode == 0 and target.is_file(),
                (proc.stderr or proc.stdout).strip().splitlines()[-1]
                if proc.returncode != 0
                else f"restored to {target}",
            )
            if not target.is_file():
                raise _EarlyExit

            restored_state = _snapshot(target)
            _check(
                checks,
                "restored_schema_version_present",
                bool(restored_state["schema_version"]),
                str(restored_state["schema_version"]),
            )
            _check(
                checks,
                "restored_tables_present",
                len(restored_state["tables"]) > 0,
                f"{len(restored_state['tables'])} tables",
            )

            os.environ["UA_HOME"] = str(restore_home)
            from unified_agent import events

            report = events.verify_event_chain()
            _check(
                checks,
                "audit_chain_verifies_on_replica",
                report["verified"] and report["status"] == "valid",
                f"total_verified={report.get('total_verified')} "
                f"head={str(report.get('head_hash'))[:16]}",
            )
            _check(
                checks,
                "rehearsal_uses_only_temp_ua_home",
                str(restore_home).startswith(tmp),
            )
    except _EarlyExit:
        pass
    except Exception as exc:  # noqa: BLE001 - rehearsal reports failures, never crashes
        _check(checks, "replica_rehearsal_completed", False, f"{type(exc).__name__}: {exc}")
    finally:
        if prior_home is None:
            os.environ.pop("UA_HOME", None)
        else:
            os.environ["UA_HOME"] = prior_home
    return {
        "ok": all(check["ok"] for check in checks),
        "mode": "replica",
        "generated_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "summary": {"total": len(checks), "passed": sum(c["ok"] for c in checks)},
    }


class _EarlyExit(Exception):
    pass


def run_rehearsal() -> dict:
    checks: list[dict] = []
    prior_home = os.environ.get("UA_HOME")
    try:
        with tempfile.TemporaryDirectory(prefix="db-restore-rehearsal-") as tmp:
            root = Path(tmp)
            source_home = root / "source"
            os.environ["UA_HOME"] = str(source_home)
            from unified_agent import backup, config, post_init
            from unified_agent.tools.memory import memory_save
            from unified_agent.tools.tasks import task_add

            post_init.init_db()  # storage.init_db() plus the hub's post-init hooks
            memory_save(
                "restore rehearsal memory",
                title="Rehearsal",
                tags=["rehearsal"],
                author="Desktop_Agent2",
            )
            task_add("restore rehearsal task")
            source_state = _snapshot(config.db_path())
            result = backup.create_backup()
            backup_path = Path(result["path"])
            _check(
                checks,
                "backup_integrity_check_passes",
                result["verification"]["ok"],
                str(result["verification"]),
            )

            restore_home = root / "restore"
            restore_home.mkdir()
            shutil.copy2(backup_path, restore_home / "memory.db")
            restored_state = _snapshot(restore_home / "memory.db")
            _check(
                checks,
                "schema_version_matches",
                restored_state["schema_version"] == source_state["schema_version"],
            )
            _check(checks, "table_set_matches", restored_state["tables"] == source_state["tables"])
            _check(checks, "row_counts_match", restored_state["counts"] == source_state["counts"])
            _check(
                checks, "rehearsal_uses_only_temp_ua_home", str(source_home).startswith(str(root))
            )
    finally:
        if prior_home is None:
            os.environ.pop("UA_HOME", None)
        else:
            os.environ["UA_HOME"] = prior_home
    return {
        "ok": all(check["ok"] for check in checks),
        "mode": "snapshot",
        "generated_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "summary": {"total": len(checks), "passed": sum(c["ok"] for c in checks)},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--from-replica",
        action="store_true",
        help="restore from the UA_REPLICA_GCS_URI WAL replica (litestream) instead of a local snapshot",
    )
    args = parser.parse_args()
    report = run_replica_rehearsal() if args.from_replica else run_rehearsal()
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
