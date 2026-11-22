"""Bind a request's ``source_host`` to the verified caller's machine (task #1633).

Every task and memory row carries ``source_host``: the machine that originated
the write. On the hub, every write used to fall back to the hub's own
``UA_SOURCE_HOST`` or hostname, because nothing set ``config.source_host_var``
per request. A task filed by ``Desktop_Agent1`` recorded the Mac hub.

Only an operator says where a label runs. The stamp is the verified identity's
``UA_AGENT_HOSTS`` entry, else the hub's own host. Nothing a caller can choose
reaches it:

* request headers (``Host``, ``X-Forwarded-Host``, custom ones);
* the ``heartbeat`` tool's ``host`` argument (``agents.host``);
* a ua-node's registered name. A node picks its own name at first
  registration, and the hub stores the label and device binding beside it
  without checking that the name is the device's hostname. Device acceptance
  (``node_acceptance``) proves the node runs jobs, not what it is called. A
  node name stays Devices metadata until something binds it to a verified
  device.

``label:hub`` is an explicit answer: the hub's own host.

Snapshot. ``config.agent_hosts()`` may read a file (a path-valued
``UA_AGENT_HOSTS``) and always probes the filesystem, so it never runs on the
request path. :func:`prime` builds the mapping at hub startup, before any
request is served (``server.wrapped_http_app``). A request reads only that
in-memory snapshot. When the snapshot is older than ``REFRESH_S``, or none
matches the current ``UA_AGENT_HOSTS`` value, one daemon thread rebuilds it
(never more than one in flight). Until a matching snapshot exists the request
binds the hub. A request never touches the filesystem or the store, and never
waits on the refresh.

Lifetime. The binding is a :class:`config.RequestHostBinding` that ends when
the request does. ``asyncio`` copies the ContextVar into any task the request
spawns; a copy outliving the request sees an ended binding and stamps the hub.
"""

from __future__ import annotations

import contextvars
import os
import threading
import time
from dataclasses import dataclass

from . import config

# A host is a short name. Anything longer is not a host and is not stamped.
_MAX_HOST_LEN = 255
# Age after which a request asks for a background rebuild of the snapshot
# (it keeps serving the old one meanwhile). Picks up edits to a binding file.
REFRESH_S = 30.0
# Snapshots kept, keyed by the raw UA_AGENT_HOSTS value. One in production;
# more only while the value changes, so a late rebuild of an old value can
# never overwrite the current one.
_SNAPSHOTS_MAX = 4


@dataclass(frozen=True)
class _Snapshot:
    hosts: dict[str, str]
    built_at: float


_snapshots: dict[str, _Snapshot] = {}
_lock = threading.Lock()
_refresh_thread: threading.Thread | None = None


def _raw() -> str:
    # os.environ is an in-memory mapping: no I/O.
    return os.environ.get("UA_AGENT_HOSTS", "")


def _build(raw: str) -> None:
    """Parse ``raw`` (file reads allowed) and store its snapshot. Never raises."""
    try:
        hosts = dict(config.agent_hosts(raw))
    except Exception:  # noqa: BLE001 - a malformed or unreadable value binds nothing
        hosts = {}
    snapshot = _Snapshot(hosts, time.monotonic())
    with _lock:
        _snapshots.pop(raw, None)
        while len(_snapshots) >= _SNAPSHOTS_MAX:
            _snapshots.pop(next(iter(_snapshots)))
        _snapshots[raw] = snapshot


def prime() -> None:
    """Build the snapshot for the current ``UA_AGENT_HOSTS`` now, synchronously.

    For hub startup and tests: never call it on the request path.
    """
    _build(_raw())


def reset_snapshot() -> None:
    """Forget every snapshot. For tests."""
    wait_for_refresh(timeout=5)
    with _lock:
        _snapshots.clear()


def _refresh_in_background(raw: str) -> None:
    global _refresh_thread
    with _lock:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return
        _refresh_thread = threading.Thread(
            target=_build, args=(raw,), name="ua-source-host-refresh", daemon=True
        )
        _refresh_thread.start()


def wait_for_refresh(timeout: float | None = None) -> bool:
    """Wait for an in-flight background rebuild. True when none is running."""
    with _lock:
        thread = _refresh_thread
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()


def _current_hosts() -> dict[str, str] | None:
    """The snapshot for the current value, or ``None``. No I/O, never blocks on a rebuild."""
    raw = _raw()
    with _lock:
        snapshot = _snapshots.get(raw)
    if snapshot is None or time.monotonic() - snapshot.built_at >= REFRESH_S:
        _refresh_in_background(raw)
    return snapshot.hosts if snapshot is not None else None


def _clean(value: object) -> str | None:
    """A stampable host, or ``None`` for the hub (explicit ``hub``, empty, invalid)."""
    if not isinstance(value, str):
        return None
    host = value.strip()
    if not host or len(host) > _MAX_HOST_LEN:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in host):
        return None
    # "hub" is the placeholder Devices maps onto the hub's own row.
    if host.lower() == "hub":
        return None
    return host


def resolve_identity_host(label: str | None) -> str | None:
    """The operator-bound machine for a verified ``label``, or ``None`` for the hub.

    ``label`` must already be verified by the caller (the auth middleware).
    Reads only the in-memory snapshot; ``None`` until one matches the current
    ``UA_AGENT_HOSTS``. Never raises.
    """
    name = (label or "").strip()
    if not name:
        return None
    try:
        hosts = _current_hosts()
    except Exception:  # noqa: BLE001
        return None
    return _clean(hosts.get(name)) if hosts else None


@dataclass(frozen=True)
class Bound:
    """What :func:`bind` set, for :func:`reset`."""

    token: contextvars.Token
    binding: config.RequestHostBinding


def bind(label: str | None) -> Bound:
    """Bind ``config.source_host_var`` for one request. Never raises.

    Always sets a binding, with no host when there is no verified label or no
    operator binding, so nothing from an enclosing context can stand in for
    this request's caller. Call it immediately before the ``try`` whose
    ``finally`` calls :func:`reset`.
    """
    binding = config.RequestHostBinding(resolve_identity_host(label))
    return Bound(config.source_host_var.set(binding), binding)


def reset(bound: Bound) -> None:
    """End the request's binding, then restore the ContextVar.

    Ending it first is what reaches the copies: a task the request spawned
    holds the same binding object and now stamps the hub.
    """
    bound.binding.end()
    config.source_host_var.reset(bound.token)
