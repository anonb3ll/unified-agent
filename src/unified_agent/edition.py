"""Open-core boundary seam in code (task #782, Phase 3.2).

Draws the architectural boundary between:
- Free Local Hub (Core): context, memory, tasks, gates, multi-client adapters, local UI.
- Attribution & Evidence Layer (Enterprise): tamper-evident ledger, chain re-anchoring,
  compliance evidence reporting (human->device->agent->action->proof).

Defaults to 'enterprise' for zero-behavior-change single-operator use.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EDITION_CORE = "core"
EDITION_ENTERPRISE = "enterprise"
VALID_EDITIONS = (EDITION_CORE, EDITION_ENTERPRISE)

CORE_FEATURES: frozenset[str] = frozenset(
    {
        "local_memory",
        "local_tasks",
        "mcp_tools",
        "process_sandbox",
        "webui_local",
        "agent_registry",
        "local_sqlite",
        "sqlite_backup",
    }
)

ENTERPRISE_FEATURES: frozenset[str] = frozenset(
    {
        *CORE_FEATURES,
        "evidence_reporting",
        "audit_hash_chain",
        "audit_reanchor",
        "multi_machine_fleet",
        "compliance_export",
        "gated_autonomy_policy",
    }
)


class FeatureNotAvailableError(PermissionError):
    """Raised when an enterprise/evidence feature is called in core edition."""


def current_edition() -> str:
    """Return the active edition string: 'core' or 'enterprise' (default)."""
    raw = os.environ.get("UA_EDITION", EDITION_ENTERPRISE).strip().lower()
    return raw if raw in VALID_EDITIONS else EDITION_ENTERPRISE


def is_enterprise() -> bool:
    """True if running in enterprise/evidence edition."""
    return current_edition() == EDITION_ENTERPRISE


def is_core() -> bool:
    """True if running in free local core edition."""
    return current_edition() == EDITION_CORE


def available_features(edition: str | None = None) -> frozenset[str]:
    """Return set of available feature identifiers for the chosen or current edition."""
    ed = edition if edition is not None else current_edition()
    if ed == EDITION_CORE:
        return CORE_FEATURES
    return ENTERPRISE_FEATURES


def is_feature_available(feature_name: str, edition: str | None = None) -> bool:
    """Check if a specific feature capability is available in the edition."""
    return feature_name in available_features(edition)


def require_feature(feature_name: str) -> None:
    """Assert feature availability; raises FeatureNotAvailableError if gated."""
    if not is_feature_available(feature_name):
        raise FeatureNotAvailableError(
            f"Feature '{feature_name}' is part of the Attribution & Evidence Layer (Enterprise edition), "
            f"not available in UA_EDITION='{current_edition()}'. Set UA_EDITION=enterprise to enable."
        )


# One hub, one organization. Isolation is the hub (its data directory), not
# a row-level tenant inside it. Clients are bearer identities in that one
# org, not additional orgs. These two env names are the same setting: a
# pilot install uses WORKOS_ORGANIZATION_ID, a hosted hub usually sets only
# UA_HUB_WORKOS_ORGANIZATION_ID. First non-blank wins. Neither set means the
# private hub, and the stamp stays unset.
_ORG_ENV_KEYS = ("WORKOS_ORGANIZATION_ID", "UA_HUB_WORKOS_ORGANIZATION_ID")


def configured_org_id(environ: Mapping[str, str] | None = None) -> str | None:
    """The hub's single organization id, or ``None`` when none is configured.

    This is the only derivation. Writers, the entrypoint, and
    ``system_doctor`` all resolve the org through here — do not re-order
    these env names anywhere else. ``environ`` defaults to this process.
    """
    env = os.environ if environ is None else environ
    for key in _ORG_ENV_KEYS:
        raw = (env.get(key) or "").strip()
        if raw:
            return raw
    return None


def current_org_id() -> str | None:
    """Return the configured organization id, or ``None`` if none is configured.

    See ``configured_org_id``. A process that inherited only the hosted
    variable still names the hub's one org. The private hub sets neither.
    """
    return configured_org_id()


def tenant_org_id() -> str | None:
    """The ``org_id`` to stamp on a newly written row, or ``None`` when the
    tenant boundary is not enforced (task #1232).

    Gated on *both* ``UA_EDITION=enterprise`` *and* the hub's one org
    (``configured_org_id``). Any other configuration — including today's
    private hub, which sets neither org variable — returns ``None``
    unconditionally, so ``org_id`` stays NULL and every write path is
    bit-for-bit unchanged. This is the single gate every write seam should
    call; do not re-derive it locally. It stamps the hub's org. It does not
    enforce a second tenant per client.
    """
    if not is_enterprise():
        return None
    return current_org_id()


def tenant_for(edition_raw: str | None, org_raw: str | None) -> str | None:
    """``tenant_org_id()`` for another process's ``UA_EDITION`` and
    ``WORKOS_ORGANIZATION_ID`` values (task #1996), e.g. the hub unit's as
    read by ``system_doctor``. Same rules: unknown or empty edition means
    enterprise, and only enterprise with a non-blank org is tenanted."""
    ed = (edition_raw or EDITION_ENTERPRISE).strip().lower()
    if ed not in VALID_EDITIONS:
        ed = EDITION_ENTERPRISE
    if ed != EDITION_ENTERPRISE:
        return None
    return (org_raw or "").strip() or None


_STORE_TENANT_KEY = "tenant_store_binding_v1"
_LEGACY_TENANT_TABLES = frozenset(
    {
        "tasks",
        "memory",
        "event_log",
        "approvals",
        "delegations",
        "agent_tokens",
        "agent_sessions",
        "work_products",
        "goals",
        "plans",
    }
)


class TenantStoreMismatchError(RuntimeError):
    """The configured hosted tenant cannot safely adopt this store."""


def _tenant_transition_refused() -> TenantStoreMismatchError:
    return TenantStoreMismatchError(
        "Hosted store tenant transition refused; existing data or its tenant binding "
        "does not match this configuration. No rows were reassigned. Stop all writers, "
        "preserve the original database and its WAL, obtain a verified backup and "
        "operator-approved ownership/migration evidence, and complete the offline "
        "migration before retrying. Do not delete the binding or widen NULL-tenant reads."
    )


def _store_tenant_binding(conn: sqlite3.Connection) -> dict | None:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='meta' AND type='table'"
    ).fetchone():
        return None
    row = conn.execute(
        "SELECT substr(value, 1, 4097) FROM meta WHERE key=?", (_STORE_TENANT_KEY,)
    ).fetchone()
    if row is None:
        return None
    try:
        raw = row[0]
        if len(raw) > 4096:
            raise ValueError
        binding = json.loads(raw)
        if (
            not isinstance(binding, dict)
            or set(binding) != {"version", "org_id", "schema_version"}
            or type(binding["version"]) is not int
            or binding["version"] != 1
            or not isinstance(binding["org_id"], str)
            or not binding["org_id"]
            or type(binding["schema_version"]) is not int
        ):
            raise ValueError
        return binding
    except (TypeError, ValueError):
        raise _tenant_transition_refused() from None


def _tenant_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def ensure_store_tenant(conn: sqlite3.Connection, *, initialized: bool = False) -> None:
    """Refuse a hosted tenant cutover that would hide existing domain rows.

    ``storage.connect`` calls this before returning a connection. ``init_db``
    calls it again with ``initialized=True`` after migrations and before its
    commit. An empty pre-init database may pass; initialized stores must have
    ``meta``. No legacy rows are automatically assigned to an organization.

    A consistent hosted store is sealed atomically: a meta attestation binds
    its tenant to SQLite's schema generation, and INSERT/UPDATE triggers on
    each org-bearing table reject older, NULL-stamping writers. Normal opens
    read only the attestation and schema generation. A schema change forces a
    new consistency proof under the SQLite writer lock, so adding a table or
    removing a guard cannot leave a stale attestation valid.

    Pilot-only and private stores remain unchanged until a hosted binding is
    established. Once bound, missing/changed organization or edition refuses.
    If already in a transaction, the caller must hold the migration writer
    lock; a savepoint preserves its transaction and leaves commit to it.
    """
    target = tenant_org_id()
    binding = _store_tenant_binding(conn)
    if binding is None and not (
        target and os.environ.get("UA_HUB_WORKOS_ORGANIZATION_ID", "").strip()
    ):
        return

    def current_binding(recorded: dict | None) -> bool:
        if recorded is None:
            return False
        if recorded["org_id"] != target:
            raise _tenant_transition_refused()
        return recorded["schema_version"] == conn.execute("PRAGMA schema_version").fetchone()[0]

    if current_binding(binding):
        return
    own_transaction = not conn.in_transaction
    conn.execute("BEGIN IMMEDIATE" if own_transaction else "SAVEPOINT tenant_store_guard")
    try:
        # A concurrent initializer may already have sealed this generation.
        if not current_binding(_store_tenant_binding(conn)):
            tables = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if not tables and not initialized:
                pass  # init_db seals the newly created schema before its commit.
            else:
                if "meta" not in tables or target is None:
                    raise _tenant_transition_refused()
                tenant_tables = []
                for table in tables:
                    # These accelerators have no org column or authoritative
                    # rows. The canonical memory table is checked instead.
                    # In particular vec0 is loaded only later in connect().
                    if table in {"memory_vec", "memory_chunk_vec", "memory_fts"} and str(
                        tables[table]
                    ).upper().startswith("CREATE VIRTUAL TABLE"):
                        continue
                    identifier = _tenant_identifier(table)
                    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({identifier})")}
                    if "org_id" in columns:
                        if conn.execute(
                            f"SELECT 1 FROM {identifier} WHERE org_id IS NOT ? LIMIT 1", (target,)
                        ).fetchone():
                            raise _tenant_transition_refused()
                        tenant_tables.append(table)
                    elif (
                        table in _LEGACY_TENANT_TABLES
                        and conn.execute(f"SELECT 1 FROM {identifier} LIMIT 1").fetchone()
                    ):
                        raise _tenant_transition_refused()
                literal = "'" + target.replace("'", "''") + "'"
                # Validate every table before making even a schema/marker change.
                for table in tenant_tables:
                    identifier = _tenant_identifier(table)
                    suffix = hashlib.sha256(table.encode()).hexdigest()
                    for operation in ("INSERT", "UPDATE OF org_id"):
                        kind = "insert" if operation == "INSERT" else "update"
                        trigger = f"ua_tenant_{kind}_{suffix}"
                        conn.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
                        conn.execute(
                            f'CREATE TRIGGER "{trigger}" BEFORE {operation} ON {identifier} '
                            f"WHEN NEW.org_id IS NOT {literal} "
                            "BEGIN SELECT RAISE(ABORT, 'store tenant mismatch'); END"
                        )
                if tenant_org_id() != target:
                    raise _tenant_transition_refused()
                generation = conn.execute("PRAGMA schema_version").fetchone()[0]
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (
                        _STORE_TENANT_KEY,
                        json.dumps({"version": 1, "org_id": target, "schema_version": generation}),
                    ),
                )
    except BaseException:
        if own_transaction:
            conn.rollback()
        else:
            conn.execute("ROLLBACK TO tenant_store_guard")
            conn.execute("RELEASE tenant_store_guard")
        raise
    else:
        if own_transaction:
            conn.commit()
        else:
            conn.execute("RELEASE tenant_store_guard")


# --- per-process tenant beacons (task #1996) ----------------------------------
#
# tenant_org_id() reads the process environment, so every process that opens a
# data directory stamps whatever tenant ITS environment names. An executor or
# CLI started without WORKOS_ORGANIZATION_ID against a tenanted store writes
# NULL org_id rows the hub can neither see nor end. Each process that opens the
# store (storage.connect) records the tenant it stamps here, one small file per
# pid, and system_doctor's tenant_consistency row compares the recent ones with
# the hub's tenant. The beacon carries no environment value besides the org id
# the process already writes into every row.

BEACON_DIRNAME = "tenant-beacons"
# A long-lived process re-stamps its beacon this often, so "seen recently"
# covers daemons as well as one-shot CLIs.
BEACON_REFRESH_S = 600.0
# Beacons older than this are deleted by the next process that writes one.
BEACON_RETENTION_S = 7 * 86400.0
# A beacon stamped this far in the future still counts (same-host clock
# adjustments); one further out is skew and is ignored, not trusted forever.
BEACON_CLOCK_SKEW_S = 120.0
# Clocks bound at import: tests (and callers) that monkeypatch the global
# time.monotonic / time.sleep with a finite or fake clock must not have it
# drained by a beacon written inside their storage.connect() or by the
# heartbeat thread.
_monotonic = time.monotonic
_heartbeat_wait = threading.Event().wait
# (pid, data dir) -> (tenant, edition, monotonic time written)
_beacons_written: dict[tuple[int, str], tuple[str | None, str, float]] = {}
# Data dirs this process has opened, re-stamped by one daemon heartbeat thread
# so a process that holds a connection open without reopening the store keeps
# its beacon fresh for as long as it lives.
_heartbeat_dirs: set[str] = set()
# The pid whose dirs _heartbeat_dirs holds, and the pid whose heartbeat thread
# is running (set only once Thread.start() succeeded, so a failed start retries).
_heartbeat_dirs_pid: int | None = None
_heartbeat_pid: int | None = None
_heartbeat_lock = threading.Lock()


def _reset_heartbeat_after_fork() -> None:
    """A forked child inherits neither the thread nor a usable lock: another
    thread may have held ``_heartbeat_lock`` at fork time, and it never
    releases it in the child."""
    global _heartbeat_lock, _heartbeat_pid, _heartbeat_dirs_pid
    _heartbeat_lock = threading.Lock()
    _heartbeat_pid = None
    _heartbeat_dirs_pid = None
    _heartbeat_dirs.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_heartbeat_after_fork)


def _process_name() -> str:
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] else ""
    return Path(argv0).name or "python"


def record_tenant_beacon(data_dir: Path) -> None:
    """Record the tenant this process stamps in ``data_dir``. Never raises.

    Rewritten when the tenant or edition changes, or after
    ``BEACON_REFRESH_S``; otherwise a dictionary lookup, cheap enough for
    every ``storage.connect()``. The first call also registers ``data_dir``
    with this process's heartbeat, which re-stamps it every
    ``BEACON_REFRESH_S`` while the process lives.
    """
    try:
        # Absolute now: a later chdir must not re-point the heartbeat (or the
        # refresh-interval cache) at another directory.
        data_dir = Path(os.path.abspath(data_dir))
    except Exception:  # noqa: BLE001 - a diagnostic must never break the store
        return
    _write_tenant_beacon(data_dir, force=False)
    try:
        _register_heartbeat(data_dir)
    except Exception:  # noqa: BLE001 - a diagnostic must never break the store
        return


def _write_tenant_beacon(data_dir: Path, *, force: bool) -> None:
    try:
        pid = os.getpid()
        tenant = tenant_org_id()
        ed = current_edition()
        key = (pid, str(data_dir))
        now = _monotonic()
        prev = _beacons_written.get(key)
        if (
            not force
            and prev is not None
            and prev[:2] == (tenant, ed)
            and now - prev[2] < BEACON_REFRESH_S
        ):
            return
        directory = Path(data_dir) / BEACON_DIRNAME
        directory.mkdir(mode=0o700, exist_ok=True)
        payload = {
            "pid": pid,
            "process": _process_name(),
            "edition": ed,
            "org_id": tenant,
            "seen_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        # Each call owns a 0600 temp: same-PID threads cannot truncate the
        # file another writer is about to publish.
        fd, tmp_name = tempfile.mkstemp(prefix=f".{pid}.", suffix=".tmp", dir=directory)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, directory / f"{pid}.json")
        finally:
            tmp.unlink(missing_ok=True)
        _beacons_written[key] = (tenant, ed, now)
        _prune_tenant_beacons(directory)
    except Exception:  # noqa: BLE001 - a diagnostic must never break the store
        return


def _register_heartbeat(data_dir: Path) -> None:
    global _heartbeat_pid, _heartbeat_dirs_pid
    pid = os.getpid()
    with _heartbeat_lock:
        if _heartbeat_dirs_pid != pid:
            # The parent's dirs are not a forked child's opens (belt and braces
            # beside the fork hook, for a fork that bypassed it).
            _heartbeat_dirs.clear()
            _heartbeat_dirs_pid = pid
        _heartbeat_dirs.add(str(data_dir))
        if _heartbeat_pid != pid:
            # Threads do not survive fork. Marked started only once the thread
            # is, so a transient start failure is retried on the next open.
            _start_heartbeat_thread()
            _heartbeat_pid = pid


def _start_heartbeat_thread() -> None:
    threading.Thread(
        target=_heartbeat_loop, args=(os.getpid(),), name="ua-tenant-beacon", daemon=True
    ).start()


def _heartbeat_loop(pid: int) -> None:
    while True:
        _heartbeat_wait(BEACON_REFRESH_S)
        if os.getpid() != pid:
            return
        _refresh_tenant_beacons()


def _refresh_tenant_beacons() -> None:
    """Re-stamp every data dir this process has opened. Never raises."""
    with _heartbeat_lock:
        dirs = list(_heartbeat_dirs)
    for raw in dirs:
        data_dir = Path(raw)
        if not data_dir.is_dir():
            with _heartbeat_lock:
                _heartbeat_dirs.discard(raw)
            continue
        _write_tenant_beacon(data_dir, force=True)


def _rename_tenant_beacon_exclusive(tomb: Path, path: Path) -> None:
    """Atomically move a complete beacon only if its destination is absent."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(libc, "renamex_np", None)
        if rename is None:
            raise OSError(errno.ENOTSUP, "exclusive rename is unavailable")
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(tomb), os.fsencode(path), 0x00000004)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOTSUP, "exclusive rename is unavailable")
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        directory = os.open(tomb.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            result = rename(
                directory, os.fsencode(tomb.name), directory, os.fsencode(path.name), 1
            )  # RENAME_NOREPLACE
        finally:
            os.close(directory)
    else:
        raise OSError(errno.ENOTSUP, "exclusive rename is unavailable")
    if result != 0:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, os.strerror(error))


def _restore_tenant_beacon(tomb: Path, path: Path) -> None:
    """Restore without replacing a newer publication, even without hard links.

    Exclusive rename publishes the complete file atomically: a failed restore
    cannot leave a partial canonical beacon, and a newer publisher always wins.
    Unsupported platforms/filesystems retain the tomb rather than overwrite.
    """
    try:
        os.link(tomb, path)
        return
    except FileExistsError:
        return
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise
    try:
        _rename_tenant_beacon_exclusive(tomb, path)
    except FileExistsError:
        pass  # a newer beacon is already in place


def _prune_tenant_beacons(directory: Path) -> None:
    """Delete beacons older than ``BEACON_RETENTION_S``. Never raises.

    A process that reuses an expired beacon's pid may publish a fresh one
    between the expiry check and the removal (task #2018), so the file is
    first renamed to a unique tombstone and deleted only if it is still the
    file judged expired. Otherwise it is restored, which never clobbers a
    beacon published after the rename: the newest one wins either way.
    """
    cutoff = time.time() - BEACON_RETENTION_S
    for path in directory.glob("*.json"):
        try:
            seen = path.stat()
            if seen.st_mtime >= cutoff:
                continue
            tomb = directory / f".{path.stem}.{os.getpid()}.{os.urandom(4).hex()}.prune"
            os.rename(path, tomb)
        except OSError:
            continue
        try:
            moved = tomb.stat()
            if (moved.st_dev, moved.st_ino, moved.st_mtime_ns) != (
                seen.st_dev,
                seen.st_ino,
                seen.st_mtime_ns,
            ):
                _restore_tenant_beacon(tomb, path)
            tomb.unlink()
        except OSError:
            continue
    # A pruner killed between rename and unlink leaves its tombstone behind.
    # rename keeps the mtime, so an old one held an expired beacon.
    for tomb in directory.glob(".*.prune"):
        try:
            if tomb.stat().st_mtime < cutoff:
                tomb.unlink()
        except OSError:
            continue


def read_tenant_beacons(data_dir: Path, *, max_age_s: float) -> list[dict[str, Any]]:
    """Beacons in ``data_dir`` seen within ``max_age_s``, newest first.

    An unreadable or malformed beacon is skipped: it says nothing about the
    tenant its process stamps.
    """
    directory = Path(data_dir) / BEACON_DIRNAME
    if not directory.is_dir():
        return []
    now = datetime.now(UTC)
    beacons = []
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            seen = datetime.fromisoformat(data["seen_at"])
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=UTC)
            org = data.get("org_id")
            if not isinstance(org, str | None):
                continue
            beacon = {
                "pid": int(data["pid"]),
                "process": str(data.get("process") or "unknown"),
                "org_id": org or None,
                "seen_at": seen,
            }
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            continue
        age_s = (now - seen).total_seconds()
        if -BEACON_CLOCK_SKEW_S <= age_s <= max_age_s:
            beacons.append(beacon)
    beacons.sort(key=lambda b: b["seen_at"], reverse=True)
    return beacons


def edition_info() -> dict[str, Any]:
    """Return a diagnostic dictionary describing active edition and capabilities."""
    ed = current_edition()
    return {
        "edition": ed,
        "is_enterprise": is_enterprise(),
        "is_core": is_core(),
        "features": sorted(available_features(ed)),
    }
