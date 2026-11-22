"""Readiness of the hub's real SQLite store.

``/healthz`` is liveness: a process is listening. ``/readyz`` asks whether
that process can write and read the store it will actually serve, and whether
WAL checkpointing is sane. A WorkOS TCP check is reported and never fails
the probe — a network blip must not take the hub out of rotation by itself.
"""

from __future__ import annotations

import os
import secrets
import socket
import sqlite3
import threading
import time
from collections.abc import Mapping
from typing import Any

_PROBE_KEY = "readyz.probe"
_WORKOS_HOST = "api.workos.com"
_WORKOS_PORT = 443
_WORKOS_TIMEOUT_S = 1.0
_WORKOS_CACHE_S = 5.0
_WORKOS_LOCK = threading.Lock()


class _WorkOSProbe:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result = "unreachable"
        self.completed_at = 0.0


_WORKOS_PROBE: _WorkOSProbe | None = None
# Set when the hosted hub or the pilot WorkOS app is configured. Absent, the
# check is skipped so a private hub is not marked unreachable.
_WORKOS_ENV_KEYS = (
    "UA_HUB_WORKOS_ORGANIZATION_ID",
    "WORKOS_API_KEY",
    "WORKOS_CLIENT_ID",
)


def workos_configured(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return any((env.get(key) or "").strip() for key in _WORKOS_ENV_KEYS)


def probe_workos() -> str:
    """Advisory reachability, bounded across DNS and connection setup.

    One retained worker prevents a stalled resolver from growing a thread per
    healthcheck. A completed result is cached briefly; a pending probe reports
    unreachable after the caller's budget without affecting SQLite readiness.
    """
    if not workos_configured():
        return "skipped"
    global _WORKOS_PROBE
    with _WORKOS_LOCK:
        probe = _WORKOS_PROBE
        if probe is None or (
            probe.done.is_set() and time.monotonic() - probe.completed_at >= _WORKOS_CACHE_S
        ):
            probe = _WorkOSProbe()
            _WORKOS_PROBE = probe
            try:
                threading.Thread(
                    target=_run_workos_probe, args=(probe,), name="ready-workos", daemon=True
                ).start()
            except RuntimeError:
                probe.completed_at = time.monotonic()
                probe.done.set()
    probe.done.wait(_WORKOS_TIMEOUT_S)
    return probe.result if probe.done.is_set() else "unreachable"


def _run_workos_probe(probe: _WorkOSProbe) -> None:
    try:
        with socket.create_connection((_WORKOS_HOST, _WORKOS_PORT), timeout=_WORKOS_TIMEOUT_S):
            probe.result = "reachable"
    except OSError:
        probe.result = "unreachable"
    finally:
        probe.completed_at = time.monotonic()
        probe.done.set()


def _readonly(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "readonly" in text or "read-only" in text or "permission denied" in text


def probe_sqlite() -> tuple[str, str]:
    """``(sqlite, wal)`` for the live store. Does not create a missing file.

    ``sqlite`` is ``ok``, ``readonly``, or ``unavailable``. ``wal`` is ``ok``
    only after ``journal_mode`` is wal and ``wal_checkpoint(PASSIVE)`` returns.
    A busy checkpoint is healthy: PASSIVE is allowed to leave frames behind.
    """
    from . import config, storage

    path = config.db_path()
    if not path.is_file():
        return "unavailable", "unchecked"
    conn: sqlite3.Connection | None = None
    try:
        conn = storage.connect()
        conn.execute("BEGIN IMMEDIATE")
        nonce = secrets.token_hex(8)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_PROBE_KEY, nonce),
        )
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (_PROBE_KEY,)).fetchone()
        if row is None or row["value"] != nonce:
            conn.rollback()
            return "unavailable", "unchecked"
        conn.execute("DELETE FROM meta WHERE key = ?", (_PROBE_KEY,))
        conn.commit()
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            return "ok", "unchecked"
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return "ok", "ok"
    except (storage.LiveStoreError, sqlite3.Error, OSError) as exc:
        if _readonly(exc):
            return "readonly", "unchecked"
        return "unavailable", "unchecked"
    finally:
        if conn is not None:
            conn.close()


def readiness() -> dict[str, Any]:
    """Probe result. ``ok`` is the store only; ``workos`` is advisory."""
    sqlite_state, wal = probe_sqlite()
    return {
        "ok": sqlite_state == "ok" and wal == "ok",
        "sqlite": sqlite_state,
        "wal": wal,
        "workos": probe_workos(),
    }
