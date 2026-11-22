"""Agents an operator added with ``unified-agent agent add`` (task #2453).

The seat of an added agent (its office, profile and kind) is not a column: like
the lifecycle state, each change is an event on ``event_log``, channel
``agent.roster``, kind ``roster.added`` or ``roster.removed``, and an agent's
seat is its newest such event. No table is added.

The seat is an overlay on configuration the hub already reads, so a label added
here works on the next request with no ``service.env`` edit and no restart:

* ``config.agent_labels`` adds seated labels to a closed ``UA_AGENT_LABELS``
  roster (an open roster stays open), and ``config.is_known_agent_label``
  accepts a seated label, so the bearer check admits it;
* ``org_registry.load_registry`` lists a seated label in its office, so the
  doctor's ``org_registry`` check and owner-of-record see the seat;
* ``config.is_cloud_hosted_identity`` covers a label added with kind
  ``cloud``. It reads the kind from ``agent_tokens.agent_kind`` on every call,
  not from this module's cache: the bearer is read from the database on every
  request, so the boundary must be too, or a new cloud label would act as a
  local one until the cache expired;
* ``profiles.default_for_identity`` uses the seat's profile when it is no more
  privileged than the code default.

Reads are cached per process for :data:`CACHE_TTL_S` seconds, because the
label checks run on every request. A write in this process clears the cache at
once; the hub, a separate process, sees a seat within that window. Only checks
that fail closed while the seat is unseen ride the cache (a closed roster
refuses the label, the profile stays at its code default); the cloud boundary
does not (see :func:`ensure_schema`).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from . import config, edition, events, storage

CHANNEL = "agent.roster"
KIND_ADDED = "roster.added"
KIND_REMOVED = "roster.removed"
KINDS = ("local", "cloud")
CACHE_TTL_S = 5.0

_lock = threading.Lock()
_cache: tuple[str, float, dict[str, dict[str, Any]]] | None = None
_reading = threading.local()


def _read(conn: Any) -> dict[str, dict[str, Any]]:
    # The hub's tenant only (task #2470): a seat recorded under another
    # organization neither lists nor makes its label known here.
    rows = conn.execute(
        "SELECT payload, created_at FROM event_log WHERE channel=? AND org_id IS ? ORDER BY ulid",
        (CHANNEL, edition.tenant_org_id()),
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        label = payload.get("agent")
        if not isinstance(label, str) or not config.agent_label_is_well_formed(label):
            continue
        latest[label.casefold()] = {**payload, "recorded_at": row["created_at"]}
    return {
        seat["agent"]: seat
        for seat in latest.values()
        if seat.get("kind") == KIND_ADDED and seat.get("agent_kind") in KINDS
    }


def ensure_schema(conn: Any) -> None:
    """Add ``agent_tokens.agent_kind`` (task #2453). Idempotent; nullable.

    ``token_lifecycle.operator_issue`` writes the kind in the insert that makes
    a bearer active, and ``config.is_cloud_hosted_identity`` reads it uncached,
    so a ``--kind cloud`` label is cloud-hosted from its first request. Rows
    issued any other way keep NULL (no kind recorded).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_tokens)").fetchall()}
    if cols and "agent_kind" not in cols:
        conn.execute("ALTER TABLE agent_tokens ADD COLUMN agent_kind TEXT")


def seats() -> dict[str, dict[str, Any]]:
    """Every currently seated label, keyed by its spelling. Empty without a database.

    Never creates a database and never raises: the label checks that call it
    run on the request path and on hosts with no hub database.
    """
    global _cache
    path = config.db_path()
    key = f"{path}|{edition.tenant_org_id()}"
    now = time.monotonic()
    with _lock:
        if _cache is not None and _cache[0] == key and now - _cache[1] < CACHE_TTL_S:
            return _cache[2]
    if getattr(_reading, "active", False):
        return {}
    result: dict[str, dict[str, Any]] = {}
    if Path(path).is_file():
        _reading.active = True
        try:
            conn = storage.connect()
            try:
                result = _read(conn)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - an unreadable roster adds nobody
            result = {}
        finally:
            _reading.active = False
    with _lock:
        _cache = (key, now, result)
    return result


def seat(label: str | None) -> dict[str, Any] | None:
    """The seat of ``label`` (case-insensitive), or None."""
    if not label:
        return None
    folded = label.strip().casefold()
    return next((s for name, s in seats().items() if name.casefold() == folded), None)


def labels() -> frozenset[str]:
    return frozenset(seats())


def cloud_labels() -> frozenset[str]:
    return frozenset(name for name, s in seats().items() if s.get("agent_kind") == "cloud")


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


def record_on_connection(conn: Any, kind: str, payload: dict[str, Any], *, actor: str) -> tuple:
    """Append one roster event inside the caller's transaction (no commit)."""
    if kind not in (KIND_ADDED, KIND_REMOVED):
        raise ValueError(f"unknown roster event kind {kind!r}")
    body = {**payload, "kind": kind, "actor": actor, "at": storage.now()}
    result = events.record_event_once_on_connection(conn, CHANNEL, payload=body, originator=actor)
    invalidate()
    return result
