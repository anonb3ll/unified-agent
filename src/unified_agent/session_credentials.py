"""Session-scoped hub MCP credentials for node-launched ACP sessions (task #1744).

When ua-node launches a harness over ACP, the hub can hand that one session its
own short-lived, revocable credential for ``/mcp``, bound to the agent label,
node, device, queue item, attempt, task and ACP session, and limited to the
``acp-session`` tool set (``profiles.ACP_SESSION_ALLOWED_TOOLS``). The design
is ``docs/designs/session-scoped-mcp-credentials.md``.

Everything here is off unless ``UA_ACP_SESSION_MCP=on``. That flag is read on
every request, so turning it off disables every outstanding credential at once.

Secret handling:

- The value is ``uas_`` plus 43 base64url characters (32 random bytes). It is
  returned once, by :func:`mint`, and never stored, logged or put in an event.
  The table keeps ``sha256(value)`` only; ``credential_id`` (16 random hex) is
  the public handle and the only one lifecycle events carry.
- :func:`verify` reads the database on every request, with no cache, so the
  next request after any revocation trigger is refused.

Binding comes from rows the hub owns, never from node input: the node names
only ``node``, ``item_id`` and ``attempt``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import (
    agent_lifecycle,
    config,
    edition,
    events,
    oauth_store,
    profiles,
    storage,
    table_schemas,
    tailnet,
    tool_surface,
)
from .node_registry import (
    LEASE_GRACE_S,
    NodeError,
    _load,
    _owned_node,
    _row,
    heartbeat_is_live,
    node_live_beat,
    validate_node_name,
)

logger = logging.getLogger(__name__)

PREFIX = "uas_"
#: The node feature string; also what the hub advertises on registration.
FEATURE = "acp_session_mcp"
#: The MCP server name a node passes to the harness. It shadows any local entry
#: of the same name in the harness's own configuration.
MCP_SERVER_NAME = "unified-agent"
#: Sliding idle window, renewed by node heartbeats for the running item.
IDLE_WINDOW_S = 600
#: Refused-check events are rate-limited per check, so a client in a retry loop
#: cannot flood event_log.
REFUSED_LOG_INTERVAL_S = 60.0
#: ``last_used_at`` is touched at most this often per credential.
LAST_USED_RESOLUTION_S = 30

CHANNEL_MINTED = "hub.session_credential.minted"
CHANNEL_REVOKED = "hub.session_credential.revoked"
CHANNEL_REFUSED = "hub.session_credential.refused"

#: Why a credential stopped working. ``expired`` and ``idle_expired`` are
#: written when a request finds the window lapsed.
REVOKE_REASONS = frozenset(
    {
        "session_end",
        "cancelled",
        "task_closed",
        "task_deleted",
        "lease_expired",
        "device_revoked",
        "operator",
        "superseded",
        "expired",
        "idle_expired",
        "agent_retired",
    }
)

#: Task statuses that end a bound session's access (mirrors tools.tasks).
TERMINAL_TASK_STATUSES = frozenset({"done", "cancelled"})

_VALUE_RE = re.compile(r"^uas_[A-Za-z0-9_-]{43}$")
_ITEM_RE = re.compile(r"^nd-[0-9a-f]{16}$")
_CREDENTIAL_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_MCP_PATHS = frozenset({"/mcp", "/mcp/"})


class SessionCredentialError(Exception):
    """A mint or a request the hub refuses.

    ``reason`` is a short machine-readable check name (never secret);
    ``status`` is the HTTP code to answer with.
    """

    def __init__(self, message: str, *, reason: str, status: int = 403):
        super().__init__(message)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class VerifiedSession:
    """What the hub middleware binds for one request made with a credential."""

    subject: str
    grant: profiles.SessionGrant
    auth_metadata: dict[str, Any]
    peer_device: dict[str, Any] | None


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the credential table (the DDL lives in core ``table_schemas``)."""
    table_schemas.session_credentials(conn)


# --- small helpers ---------------------------------------------------------------


def _utcnow() -> datetime:
    """The hub clock. Tests replace it to move time without sleeping."""
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _parse(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def digest(value: str) -> str:
    """SHA-256 of a credential value; the only form the table holds."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_session_token(token: str | None) -> bool:
    """Whether a bearer value is in the session-credential namespace.

    Routing is by prefix alone, so a malformed ``uas_`` value is still refused
    here and never reaches the bearer, OAuth or WorkOS legs.
    """
    return isinstance(token, str) and token.startswith(PREFIX)


def _tool_ceiling(*labels: str) -> frozenset[str]:
    """Every label's identity ceiling, intersected with the phase-1 session set.

    Called with the subject and the device agent, so a profile-mapped session
    never reaches a tool the node's own label could not.
    """
    names = set(tool_surface.registry())
    tools = profiles.ACP_SESSION_ALLOWED_TOOLS
    for label in labels:
        ceiling = profiles._allowed_tools_for_profile(profiles.default_for_identity(label), names)
        tools = tools & frozenset(ceiling)
    return tools


def _is_human(label: str) -> bool:
    """A session credential is never minted for a human identity (the operator)."""
    return label in config.human_identity_labels() or (
        config.actor_kind_for_identity(label) == "human"
    )


def _signin_live(
    conn: sqlite3.Connection, label: str, signin_id: str | None, ref: datetime
) -> bool | None:
    """Whether ``label`` still holds the device sign-in family ``signin_id``.

    True or False from the store; None when it could not be read. Independent
    of the revocation cascade: an OAuth device or session revoke deletes the
    rows this reads, so the next request is refused even when writing the
    credential's own revocation failed. A refresh rotation replaces the row in
    one transaction and keeps the id, so it does not end a session.
    """
    if not signin_id:
        return False
    try:
        return oauth_store.live_signin_exists(label, signin_id, now=int(ref.timestamp()), conn=conn)
    except sqlite3.Error:
        return None


def _cancel_requested(conn: sqlite3.Connection, item: dict[str, Any]) -> bool:
    """Mirrors ``node_queue._cancel_requested``: a direct or run-level cancel."""
    if item.get("cancel_requested"):
        return True
    run_id = item.get("run_id")
    if not run_id:
        return False
    try:
        run = conn.execute(
            "SELECT termination_outcome, state FROM dispatch_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return run is not None and (
        run["termination_outcome"] is not None or run["state"] == "cancelled"
    )


def _label_refused(label: str) -> bool:
    """Unknown, env-revoked (``UA_REVOKED_AGENTS``) or retired (task #2305)."""
    return (
        not config.is_known_agent_label(label) or agent_lifecycle.refused_label(label) is not None
    )


def _retired_label(conn: sqlite3.Connection, *labels: str, site: str) -> str | None:
    """The first of ``labels`` that is retired, read on ``conn``, or None.

    A hit is also recorded as ``lifecycle.refused`` through the lifecycle
    helper, which defers the write when ``conn`` holds a write transaction, so
    the evidence survives the refused mint's rollback (review of #2305).
    """
    for label in dict.fromkeys(labels):
        if label and agent_lifecycle.retired_state(label, conn=conn) is not None:
            agent_lifecycle.record_refusal(label, site=site, conn=conn)
            return label
    return None


_refused_lock = threading.Lock()
_refused_last: dict[str, float] = {}


def _record_refused(check: str, *, credential_id: str | None = None) -> None:
    """Rate-limited ``.refused`` event. Carries the failed check, never a value."""
    now = time.monotonic()
    with _refused_lock:
        last = _refused_last.get(check)
        if last is not None and now - last < REFUSED_LOG_INTERVAL_S:
            return
        _refused_last[check] = now
    payload: dict[str, Any] = {"check": check}
    if credential_id:
        payload["credential_id"] = credential_id
    try:
        events.record_event(CHANNEL_REFUSED, payload, originator=None, notify=False)
    except Exception:  # noqa: BLE001 - evidence is best-effort; the refusal stands
        logger.warning("session credential refusal was not recorded")


def _record(channel: str, payload: dict[str, Any], originator: str | None) -> None:
    try:
        events.record_event(channel, payload, originator=originator)
    except Exception:  # noqa: BLE001 - the table row is the source of truth
        logger.warning("session credential event %s was not recorded", channel)


# --- revocation --------------------------------------------------------------------


def _revoke_where(
    conn: sqlite3.Connection,
    where: str,
    params: tuple[Any, ...],
    reason: str,
    now: datetime | None = None,
) -> list[str]:
    """Revoke every active row matching ``where`` inside the caller's transaction.

    ``where`` is always one of this module's own constant clauses. A store that
    predates the table (no ``init_db`` since this shipped) has nothing to revoke,
    and a revocation hook must never sink the transaction that called it.
    """
    if reason not in REVOKE_REASONS:
        raise ValueError(f"unknown revoke reason {reason!r}")
    try:
        rows = conn.execute(
            f"SELECT credential_id FROM session_credentials WHERE revoked_at IS NULL AND ({where})",
            params,
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    ids = [row["credential_id"] for row in rows]
    if ids:
        conn.execute(
            "UPDATE session_credentials SET revoked_at=?, revoke_reason=? "
            f"WHERE revoked_at IS NULL AND ({where})",
            (_iso(now or _utcnow()), reason, *params),
        )
    return ids


def revoke_for_item(
    conn: sqlite3.Connection, item_id: str, reason: str, *, now: datetime | None = None
) -> list[str]:
    """Revoke the item's credentials in the caller's transaction."""
    return _revoke_where(conn, "item_id=?", (str(item_id),), reason, now)


def revoke_for_items(conn: sqlite3.Connection, item_ids: Iterable[str], reason: str) -> list[str]:
    out: list[str] = []
    for item_id in dict.fromkeys(str(i) for i in item_ids if i):
        out.extend(revoke_for_item(conn, item_id, reason))
    return out


def revoke_for_agent(conn: sqlite3.Connection, label: str, reason: str) -> list[str]:
    """Revoke this org's credentials running as, or on a device of, ``label``."""
    return _revoke_where(
        conn,
        "(agent_label=? COLLATE NOCASE OR device_agent=? COLLATE NOCASE) AND org_id IS ?",
        (label, label, edition.tenant_org_id()),
        reason,
    )


def revoke_for_tasks(conn: sqlite3.Connection, task_ids: Iterable[int], reason: str) -> list[str]:
    """Revoke every credential bound to one of these tasks (close or delete)."""
    out: list[str] = []
    for task_id in dict.fromkeys(int(t) for t in task_ids):
        out.extend(_revoke_where(conn, "task_id=?", (task_id,), reason))
    return out


def revoke_for_devices(conn: sqlite3.Connection, devices: Iterable[str], reason: str) -> list[str]:
    """Revoke every credential whose node is bound to one of these devices."""
    out: list[str] = []
    for device in dict.fromkeys(str(d) for d in devices if d):
        out.extend(_revoke_where(conn, "device_node=?", (device,), reason))
    return out


def revoke_device_cascade(
    *, devices: Iterable[str], unbound_agents: Iterable[str], actor: str | None
) -> list[str]:
    """Cascade an OAuth sign-in revoke onto session credentials (task #1744).

    ``devices`` are the node names and ids the revoked sign-ins were bound to.
    ``unbound_agents`` are labels whose revoked sign-in carried no device
    binding: their credentials on nodes that also have no device binding are
    revoked too, since such a node may have signed in with that session. This
    can over-revoke, never under-revoke; a node signed in some other way keeps
    working through its next mint.
    """
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ids = revoke_for_devices(conn, devices, "device_revoked")
        for label in dict.fromkeys(str(a) for a in unbound_agents if a):
            ids.extend(
                _revoke_where(
                    conn,
                    "(device_node IS NULL OR device_node='') AND device_agent=?",
                    (label,),
                    "device_revoked",
                )
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    emit_revoked(ids, "device_revoked", actor=actor)
    return ids


def emit_revoked(credential_ids: Iterable[str], reason: str, *, actor: str | None) -> None:
    """One ``.revoked`` event per credential. Call after the revoking commit."""
    for credential_id in credential_ids:
        _record(
            CHANNEL_REVOKED,
            {"credential_id": credential_id, "reason": reason, "actor": actor},
            actor,
        )


def check_selectors(
    *,
    credential_id: str | None = None,
    item_id: str | None = None,
    node: str | None = None,
    devices: Iterable[str] | None = None,
) -> str | None:
    """Validate ``revoke``'s selectors before anything is written; the node, normalized.

    Raises ``ValueError`` or ``NodeError``. A caller that must tell a refusal
    from a failed write (a keyed revoke, task #2516) calls this first.
    """
    chosen = [v for v in (credential_id, item_id, node, devices) if v is not None]
    if len(chosen) != 1:
        raise ValueError("pass exactly one of credential_id, item_id, node or devices")
    if credential_id is not None and not _CREDENTIAL_ID_RE.fullmatch(credential_id):
        raise ValueError("credential_id must be 16 hex characters")
    if item_id is not None and not _ITEM_RE.fullmatch(item_id):
        raise ValueError("item_id is malformed")
    return validate_node_name(node) if node is not None else None


def revoke(
    *,
    reason: str,
    actor: str | None,
    credential_id: str | None = None,
    item_id: str | None = None,
    node: str | None = None,
    devices: Iterable[str] | None = None,
) -> list[str]:
    """Revoke in a transaction of its own, then record the events.

    Exactly one selector: ``credential_id``, ``item_id``, ``node`` or
    ``devices``. Used by the operator route and the device-revoke cascade.
    """
    node = check_selectors(credential_id=credential_id, item_id=item_id, node=node, devices=devices)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if credential_id is not None:
            ids = _revoke_where(conn, "credential_id=?", (credential_id,), reason)
        elif item_id is not None:
            ids = revoke_for_item(conn, item_id, reason)
        elif node is not None:
            ids = _revoke_where(conn, "node=?", (node,), reason)
        else:
            ids = revoke_for_devices(conn, devices or (), reason)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    emit_revoked(ids, reason, actor=actor)
    return ids


# --- renewal -----------------------------------------------------------------------


def slide_idle(
    conn: sqlite3.Connection, item_id: str, node: str, *, now: datetime | None = None
) -> None:
    """Move the idle window forward for a running item, in the heartbeat's transaction.

    Never moves ``expires_at``, never extends past it, and never revives a
    window that has already lapsed.
    """
    ref = now or _utcnow()
    try:
        rows = conn.execute(
            "SELECT credential_id, idle_expires_at, expires_at FROM session_credentials "
            "WHERE item_id=? AND node=? AND revoked_at IS NULL",
            (str(item_id), node),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return
        raise
    for row in rows:
        idle = _parse(row["idle_expires_at"])
        cap = _parse(row["expires_at"])
        if idle is None or cap is None or idle <= ref:
            continue
        renewed = min(ref + timedelta(seconds=IDLE_WINDOW_S), cap)
        if renewed > idle:
            conn.execute(
                "UPDATE session_credentials SET idle_expires_at=? "
                "WHERE credential_id=? AND revoked_at IS NULL",
                (_iso(renewed), row["credential_id"]),
            )


# --- mint --------------------------------------------------------------------------


def mint(
    node: Any,
    item_id: Any,
    attempt: Any,
    *,
    identity: str,
    device_node: str | None,
    signin_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Issue one credential for a claimed ACP item. The value is returned once.

    The caller (the HTTP route) has already checked that the request came from
    a device-code OAuth sign-in. Raises :class:`SessionCredentialError`.
    """
    try:
        return _mint(
            node,
            item_id,
            attempt,
            identity=identity,
            device_node=device_node,
            signin_id=signin_id,
            now=now,
        )
    except SessionCredentialError as exc:
        if exc.reason not in {"disabled", "bad_request"}:
            _record_refused(f"mint:{exc.reason}")
        raise


def _mint(
    node: Any,
    item_id: Any,
    attempt: Any,
    *,
    identity: str,
    device_node: str | None,
    signin_id: str | None,
    now: datetime | None,
) -> dict[str, Any]:
    if not config.acp_session_mcp_enabled():
        raise SessionCredentialError(
            "session credentials are off on this hub (UA_ACP_SESSION_MCP)",
            reason="disabled",
        )
    try:
        name = validate_node_name(node)
    except NodeError as exc:
        raise SessionCredentialError(str(exc), reason="bad_request", status=400) from None
    item_key = str(item_id or "")
    if not _ITEM_RE.fullmatch(item_key):
        raise SessionCredentialError("item_id is malformed", reason="bad_request", status=400)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise SessionCredentialError(
            "attempt must be a positive integer", reason="bad_request", status=400
        )
    allowed = config.acp_session_mcp_nodes()
    if allowed is not None and name not in allowed:
        raise SessionCredentialError(
            f"node {name!r} is not in UA_ACP_SESSION_MCP_NODES", reason="node_not_allowed"
        )
    ref = now or _utcnow()
    as_of = _iso(ref)
    value = PREFIX + secrets.token_urlsafe(32)
    if not _VALUE_RE.fullmatch(value):  # pragma: no cover - token_urlsafe(32) is 43 chars
        raise SessionCredentialError("credential generation failed", reason="internal", status=500)
    credential_id = secrets.token_hex(8)
    superseded: list[str] = []
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            node_row = _owned_node(conn, name, identity, device_node)
        except NodeError as exc:
            # A retired owner keeps its typed reason (task #2315); every other
            # ownership refusal is a binding failure.
            reason = exc.code if exc.code == "agent_retired" else "node_binding"
            raise SessionCredentialError(str(exc), reason=reason, status=exc.status) from None
        facts = _load(node_row.get("facts_json")) or {}
        features = facts.get("node_features") if isinstance(facts, dict) else None
        if not isinstance(features, list) or FEATURE not in features:
            raise SessionCredentialError(
                f"node {name!r} did not advertise {FEATURE}",
                reason="node_feature_missing",
                status=409,
            )
        item = _row(
            conn.execute(
                "SELECT * FROM node_dispatch WHERE id=? AND node=?", (item_key, name)
            ).fetchone()
        )
        if item is None:
            raise SessionCredentialError(
                f"item {item_key!r} is not queued for node {name!r}",
                reason="item_not_found",
                status=404,
            )
        if not str(item.get("backend") or "").startswith("acp:"):
            raise SessionCredentialError(
                "session credentials are issued for ACP items only", reason="not_acp", status=409
            )
        if item["status"] != "claimed":
            raise SessionCredentialError(
                f"item {item_key!r} is {item['status']}, not claimed",
                reason="item_not_claimed",
                status=409,
            )
        if int(item.get("attempts") or 0) != attempt:
            raise SessionCredentialError(
                f"attempt {attempt} is not the item's current claim",
                reason="stale_attempt",
                status=409,
            )
        lease = _parse(item.get("lease_expires_at"))
        claimed = _parse(item.get("claimed_at"))
        if lease is None or claimed is None or lease <= ref:
            raise SessionCredentialError(
                "the item's lease has expired", reason="lease_expired", status=409
            )
        if _cancel_requested(conn, item):
            raise SessionCredentialError(
                "the item is being cancelled", reason="cancel_requested", status=409
            )
        # The item's lease deadline at claim time; heartbeats never move it.
        expires = claimed + timedelta(seconds=int(item["timeout_s"]) + LEASE_GRACE_S)
        if expires <= ref:
            raise SessionCredentialError(
                "the item's lease has expired", reason="lease_expired", status=409
            )
        device_agent = str(item["agent"])
        subject = str(item.get("profile_agent") or device_agent)
        retired = _retired_label(conn, subject, device_agent, site="session_credentials.mint")
        if retired is not None:
            raise SessionCredentialError(
                f"agent {retired!r} is retired; no session credential is minted for it",
                reason="agent_retired",
            )
        if _label_refused(subject) or _label_refused(device_agent):
            raise SessionCredentialError(
                "the session's agent label is unknown or revoked", reason="label_refused"
            )
        if _is_human(subject):
            raise SessionCredentialError(
                "a session credential is never minted for a human identity",
                reason="human_subject",
            )
        task_id = item.get("task_id")
        if task_id is not None:
            task = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None or task["status"] in TERMINAL_TASK_STATUSES:
                raise SessionCredentialError(
                    "the item's task is closed or missing", reason="task_closed", status=409
                )
        signin_id = signin_id if isinstance(signin_id, str) and signin_id else None
        if _signin_live(conn, device_agent, signin_id, ref) is not True:
            # No id (a sign-in from before signin_id: run `unified-agent login`
            # again), or no durable live row behind it.
            raise SessionCredentialError(
                "the minting device sign-in is not a durable, live sign-in",
                reason="signin_missing",
            )
        tools = _tool_ceiling(subject, device_agent)
        idle = min(ref + timedelta(seconds=IDLE_WINDOW_S), expires)
        # At most one active credential per (item, attempt): a second mint
        # revokes the first in this same transaction.
        superseded = _revoke_where(
            conn, "item_id=? AND attempt=?", (item_key, attempt), "superseded", ref
        )
        conn.execute(
            "INSERT INTO session_credentials (credential_id, token_sha256, agent_label, "
            "device_agent, node, device_node, item_id, attempt, task_id, run_id, tools_json, "
            "issued_at, idle_expires_at, expires_at, org_id, signin_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                credential_id,
                digest(value),
                subject,
                device_agent,
                name,
                node_row.get("device_node"),
                item_key,
                attempt,
                item.get("task_id"),
                item.get("run_id"),
                json.dumps(sorted(tools)),
                as_of,
                _iso(idle),
                _iso(expires),
                edition.tenant_org_id(),
                signin_id,
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    emit_revoked(superseded, "superseded", actor=identity)
    _record(
        CHANNEL_MINTED,
        {
            "credential_id": credential_id,
            "item_id": item_key,
            "attempt": attempt,
            "node": name,
            "task_id": item.get("task_id"),
            "run_id": item.get("run_id"),
            "agent_label": subject,
            "device_agent": device_agent,
            "tools": sorted(tools),
            "idle_expires_at": _iso(idle),
            "expires_at": _iso(expires),
        },
        identity,
    )
    return {
        "credential": value,
        "credential_id": credential_id,
        "expires_at": _iso(expires),
        "idle_expires_at": _iso(idle),
        "tools": sorted(tools),
        "mcp_server_name": MCP_SERVER_NAME,
    }


# --- per-request verification ------------------------------------------------------


def _is_loopback(peer_ip: str | None) -> bool:
    try:
        return bool(peer_ip) and ipaddress.ip_address(str(peer_ip).strip()).is_loopback
    except ValueError:
        return False


def _device_check(row_device: str | None, peer_ip: str | None) -> tuple[str, dict | None]:
    """Check 7: the peer's tailnet device must be the node's device.

    A resolvable peer must match under either mode. An unresolvable peer is
    admitted only when the bound node has no device binding and the peer is
    loopback (a node on the hub host itself), or under ``best_effort``, which
    records ``unavailable`` on every decision.
    """
    peer = tailnet.resolve_peer_device(peer_ip) if peer_ip else None
    if peer is not None:
        names = {peer.get("node"), peer.get("node_id")} - {None, ""}
        if row_device and row_device in names:
            return "verified", peer
        raise SessionCredentialError(
            "the calling device is not the session's node", reason="device_mismatch", status=401
        )
    if config.acp_session_device_check() == "best_effort":
        return "unavailable", None
    if not row_device and _is_loopback(peer_ip):
        return "loopback", None
    raise SessionCredentialError(
        "the calling device could not be verified", reason="device_unresolved", status=401
    )


def verify(
    token: str, *, path: str, peer_ip: str | None, now: datetime | None = None
) -> VerifiedSession:
    """Run every per-request check; return the binding or raise.

    Path first (403), then the flag, then the row and every live-state check
    (401). Each request reads the database; nothing is cached.
    """
    try:
        return _verify(token, path=path, peer_ip=peer_ip, now=now)
    except SessionCredentialError as exc:
        _record_refused(exc.reason, credential_id=getattr(exc, "credential_id", None))
        raise


def _refuse(message: str, reason: str, credential_id: str | None = None) -> None:
    exc = SessionCredentialError(message, reason=reason, status=401)
    exc.credential_id = credential_id  # type: ignore[attr-defined]
    raise exc


def _verify(token: str, *, path: str, peer_ip: str | None, now: datetime | None) -> VerifiedSession:
    if path not in _MCP_PATHS:
        raise SessionCredentialError(
            "session credentials are valid on /mcp only", reason="path", status=403
        )
    if not config.acp_session_mcp_enabled():
        _refuse("session credentials are off on this hub", "flag_off")
    if not isinstance(token, str) or not _VALUE_RE.fullmatch(token):
        _refuse("invalid session credential", "unknown")
    ref = now or _utcnow()
    lapsed: str | None = None
    conn = storage.connect()
    try:
        try:
            row = _row(
                conn.execute(
                    "SELECT * FROM session_credentials WHERE token_sha256=?", (digest(token),)
                ).fetchone()
            )
        except sqlite3.OperationalError:
            row = None
        if row is None or row.get("revoked_at"):
            _refuse("invalid or revoked session credential", "unknown_or_revoked")
        assert row is not None
        cid = row["credential_id"]
        expires = _parse(row["expires_at"])
        idle = _parse(row["idle_expires_at"])
        if expires is None or ref >= expires:
            lapsed = "expired"
        elif idle is None or ref >= idle:
            lapsed = "idle_expired"
        if lapsed is None:
            item = _row(
                conn.execute("SELECT * FROM node_dispatch WHERE id=?", (row["item_id"],)).fetchone()
            )
            if (
                item is None
                or item["status"] != "claimed"
                or item["node"] != row["node"]
                or int(item.get("attempts") or 0) != int(row["attempt"])
            ):
                _refuse("the session's item is no longer running", "item_not_live", cid)
            assert item is not None
            if _cancel_requested(conn, item):
                _refuse("the session's item is being cancelled", "cancel_requested", cid)
            # The bound task, read live: a task closed by any path (task_update,
            # a direct run completion, a merge closeout, plain SQL) ends access.
            if row.get("task_id") is not None:
                task = conn.execute(
                    "SELECT status FROM tasks WHERE id=?", (row["task_id"],)
                ).fetchone()
                if task is None:
                    lapsed = "task_deleted"
                elif task["status"] in TERMINAL_TASK_STATUSES:
                    lapsed = "task_closed"
        if lapsed is None:
            node_row = _row(
                conn.execute(
                    "SELECT agent, device_node, last_heartbeat, heartbeat_floor "
                    "FROM nodes WHERE node=?",
                    (row["node"],),
                ).fetchone()
            )
            # The shared node liveness rule: a beat past the clock-skew
            # allowance is not live either (task #1997), nor one from before
            # the last retire/un-retire (task #2196).
            if (
                node_row is None
                or node_row["agent"] != row["device_agent"]
                or (node_row.get("device_node") or None) != (row.get("device_node") or None)
                or not heartbeat_is_live(node_live_beat(node_row), ref)
            ):
                _refuse("the session's node is not live", "node_stale", cid)
            retired = _retired_label(
                conn, row["agent_label"], row["device_agent"], site="session_credentials.verify"
            )
            if retired is not None:
                _refuse(f"agent {retired!r} is retired", "agent_retired", cid)
            if _label_refused(row["agent_label"]) or _label_refused(row["device_agent"]):
                _refuse("the session's agent label is revoked", "label_revoked", cid)
            # The device sign-in family it was minted under, read live (review
            # of #1744): an OAuth revoke ends access even if the cascade that
            # should have revoked the row failed. A miss refuses and writes
            # nothing, like the other live-state checks; the cascade or an
            # explicit revoke does the permanent write.
            if _signin_live(conn, row["device_agent"], row.get("signin_id"), ref) is not True:
                _refuse("the session's device sign-in is not live", "signin_missing", cid)
    finally:
        conn.close()
    if lapsed is not None:
        revoke(reason=lapsed, actor=None, credential_id=cid)
        _refuse("the session credential is no longer valid", lapsed, cid)
    try:
        device_check, peer_device = _device_check(row.get("device_node"), peer_ip)
    except SessionCredentialError as exc:
        exc.credential_id = cid  # type: ignore[attr-defined]
        raise
    try:
        stored = json.loads(row["tools_json"] or "[]")
    except (TypeError, ValueError):
        stored = []
    tools = frozenset(t for t in stored if isinstance(t, str)) & _tool_ceiling(
        row["agent_label"], row["device_agent"]
    )
    _touch(cid, row.get("last_used_at"), ref)
    task_id = row.get("task_id")
    grant = profiles.SessionGrant(
        tools=tools,
        credential_id=cid,
        item_id=row["item_id"],
        task_id=int(task_id) if task_id is not None else None,
    )
    metadata = {
        "auth_method": "acp_session",
        "scopes": [],
        "credential_id": cid,
        "item_id": row["item_id"],
        "attempt": int(row["attempt"]),
        "node": row["node"],
        "task_id": grant.task_id,
        "run_id": row.get("run_id"),
        "acp_session_id": row.get("acp_session_id"),
        "device_agent": row["device_agent"],
        "device_check": device_check,
        "expires_at": row["expires_at"],
    }
    return VerifiedSession(
        subject=row["agent_label"],
        grant=grant,
        auth_metadata=metadata,
        peer_device=peer_device,
    )


def _touch(credential_id: str, last_used: Any, ref: datetime) -> None:
    previous = _parse(last_used)
    if previous is not None and (ref - previous).total_seconds() < LAST_USED_RESOLUTION_S:
        return
    try:
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE session_credentials SET last_used_at=? WHERE credential_id=?",
                (_iso(ref), credential_id),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


# --- session_open ingest -----------------------------------------------------------


def bind_acp_session(item_id: str, node: str, acp_session_id: str | None) -> str | None:
    """Record the ACP session on the item's live credential (task #1744, design section 7).

    Fills ``acp_session_id`` and ``agent_session_id`` on the credential row, and
    writes ``credential_id`` into the mirrored ``agent_sessions.meta``. A resumed
    session keeps its row but gets a new credential, so prior ids are kept in
    ``meta.credential_ids``. The row is touched only while it is bound to this
    item and node. Best-effort; returns the credential id or None.
    """
    if not acp_session_id:
        return None
    try:
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cred = conn.execute(
                "SELECT credential_id FROM session_credentials WHERE item_id=? AND node=? "
                "AND revoked_at IS NULL ORDER BY issued_at DESC LIMIT 1",
                (str(item_id), node),
            ).fetchone()
            if cred is None:
                conn.commit()
                return None
            cid = cred["credential_id"]
            session = conn.execute(
                "SELECT id, meta FROM agent_sessions WHERE external_session_id=? "
                "ORDER BY started_at DESC LIMIT 1",
                (acp_session_id,),
            ).fetchone()
            agent_session_id = None
            if session is not None:
                try:
                    meta = json.loads(session["meta"] or "{}")
                except (TypeError, ValueError):
                    meta = None
                if (
                    isinstance(meta, dict)
                    and meta.get("item_id") == item_id
                    and meta.get("node") == node
                ):
                    agent_session_id = session["id"]
                    prior = meta.get("credential_ids")
                    ids = (
                        [i for i in prior if isinstance(i, str)] if isinstance(prior, list) else []
                    )
                    previous = meta.get("credential_id")
                    if isinstance(previous, str) and previous not in ids:
                        ids.append(previous)
                    if cid not in ids:
                        ids.append(cid)
                    meta["credential_id"] = cid
                    meta["credential_ids"] = ids[-20:]
                    conn.execute(
                        "UPDATE agent_sessions SET meta=? WHERE id=?",
                        (json.dumps(meta), session["id"]),
                    )
            conn.execute(
                "UPDATE session_credentials SET "
                "acp_session_id=COALESCE(acp_session_id, ?), "
                "agent_session_id=COALESCE(agent_session_id, ?) WHERE credential_id=?",
                (acp_session_id, agent_session_id, cid),
            )
            conn.commit()
            return cid
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("session credential could not be bound to its ACP session")
        return None


def credential_row(credential_id: str) -> dict[str, Any] | None:
    """The non-secret columns of one credential row, for operators and tests."""
    conn = storage.connect()
    try:
        row = _row(
            conn.execute(
                "SELECT * FROM session_credentials WHERE credential_id=?", (credential_id,)
            ).fetchone()
        )
    finally:
        conn.close()
    if row is not None:
        row.pop("token_sha256", None)
    return row


#: Columns an operator may read; never ``token_sha256``.
_PUBLIC_COLUMNS = (
    "credential_id",
    "agent_label",
    "device_agent",
    "node",
    "device_node",
    "item_id",
    "attempt",
    "task_id",
    "run_id",
    "acp_session_id",
    "agent_session_id",
    "tools_json",
    "issued_at",
    "idle_expires_at",
    "expires_at",
    "last_used_at",
    "revoked_at",
    "revoke_reason",
)
#: What ``list_credentials`` can select.
LIST_STATES = ("active", "expired", "revoked", "all")
LIST_MAX = 500


def _public(row: sqlite3.Row, now: datetime) -> dict[str, Any]:
    out = {column: row[column] for column in _PUBLIC_COLUMNS if column != "tools_json"}
    try:
        out["tools"] = json.loads(row["tools_json"] or "[]")
    except ValueError:
        out["tools"] = []
    expiry = [_parse(row["expires_at"]), _parse(row["idle_expires_at"])]
    if row["revoked_at"]:
        out["state"] = "revoked"
    elif any(ts is None or ts <= now for ts in expiry):
        out["state"] = "expired"
    else:
        out["state"] = "active"
    return out


def list_credentials(
    *, state: str = "active", node: str | None = None, limit: int = 200
) -> dict[str, Any]:
    """Credential metadata for the operator's Settings page (task #2516).

    Tenant-scoped (``org_id IS`` the hub's tenant), newest first, never the value
    or its digest. ``state`` is ``active`` (not revoked, inside both windows),
    ``expired``, ``revoked`` or ``all``. ``truncated`` says more rows matched.
    """
    if state not in LIST_STATES:
        raise ValueError(f"state must be one of {', '.join(LIST_STATES)}")
    limit = max(1, min(int(limit), LIST_MAX))
    where = ["org_id IS ?"]
    params: list[Any] = [edition.tenant_org_id()]
    if node is not None:
        where.append("node=?")
        params.append(validate_node_name(node))
    now = _utcnow()
    # Stored timestamps are _iso() strings, so they compare as text.
    if state == "revoked":
        where.append("revoked_at IS NOT NULL")
    elif state == "active":
        where.append("revoked_at IS NULL AND expires_at > ? AND idle_expires_at > ?")
        params.extend([_iso(now), _iso(now)])
    elif state == "expired":
        where.append("revoked_at IS NULL AND (expires_at <= ? OR idle_expires_at <= ?)")
        params.extend([_iso(now), _iso(now)])
    conn = storage.connect()
    try:
        ensure_schema(conn)
        rows = conn.execute(
            f"SELECT {', '.join(_PUBLIC_COLUMNS)} FROM session_credentials "
            f"WHERE {' AND '.join(where)} ORDER BY issued_at DESC, credential_id DESC LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
    finally:
        conn.close()
    listed = [_public(row, now) for row in rows[:limit]]
    return {"credentials": listed, "count": len(listed), "truncated": len(rows) > limit}


def credential_metadata(credential_ids: Iterable[str]) -> list[dict[str, Any]]:
    """The public metadata of these credentials, in the given order (task #2516)."""
    ids = list(dict.fromkeys(credential_ids))
    if not ids:
        return []
    now = _utcnow()
    conn = storage.connect()
    try:
        rows = conn.execute(
            f"SELECT {', '.join(_PUBLIC_COLUMNS)} FROM session_credentials "
            f"WHERE credential_id IN ({', '.join('?' for _ in ids)})",
            ids,
        ).fetchall()
    finally:
        conn.close()
    by_id = {row["credential_id"]: _public(row, now) for row in rows}
    return [by_id[i] for i in ids if i in by_id]
