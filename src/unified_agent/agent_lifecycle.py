"""Agent lifecycle controls: pause, resume, terminate and retire an agent (#1351, #2305).

An agent is ``active``, ``paused``, ``terminated`` or ``retired``. The state is not a
column: each change is an event on the hash-chained ``event_log`` channel
``agent.lifecycle`` (kind ``lifecycle.changed``) carrying the new state, the
reason and the actor, and the current state is the newest such event for the
agent. An agent with no event is ``active``.

What a state change does:

* **Claims.** A ``paused`` or ``terminated`` agent cannot claim work:
  ``task_claim`` and ``task_update(status="doing")`` refuse it inside their
  write transaction (:func:`claim_block`). A lock renewal is refused too, so a
  running executor cannot keep extending a lease once it is paused.
* **Work in flight.** Stopping is requested on every execution path the hub
  can reach, and each result says what actually happened rather than
  promising a stop:

  ==============  =====================================================  =========================
  path            what the hub does                                      ``control``
  ==============  =====================================================  =========================
  dispatch (A)    ``coordinator.cancel_run`` on each live run            ``confirmed`` / ``requested``
  node (C)        ``node_queue.cancel``; queued items never start        ``requested``
  executor (B)    the daemon's kill check reads this state each tick     ``cooperative``
  self-pickup     no process the hub controls; the claim is held         ``unsupported``
  ==============  =====================================================  =========================

  The dispatch and node stops are registered by their owning modules
  (:func:`register_stop_handler`, task #1723). A process missing one refuses
  to pause or terminate rather than skip the cancel.

  The executor daemon writes ``lifecycle.stop_confirmed`` when it terminates
  a child because of a pause, so the ledger distinguishes the request from the
  confirmed stop.
* **Terminate** also releases the agent's ``doing`` claims (status back to
  ``open``) so another agent can pick the work up. Pause keeps them.
* **Retire** (task #2305) is the decommission marker. It is refused while the
  agent still holds ``doing`` claims (release or cancel them first), and in the
  same transaction as the state event it marks the agent's ``agent_tokens``
  rows ``revoked``, revokes its session credentials and deletes its device
  sign-ins (OAuth refresh and access tokens, task #2315). ``resume`` cannot
  undo it; only an explicit operator ``unretire`` can, and that re-issues
  nothing: each device signs in again. A bearer still configured in
  ``UA_AGENT_TOKENS`` is refused on every request (:func:`refuse_retired_identity`).
  Nothing is deleted: tasks, runs and events keep their attribution. Each
  agent has an owner of record: the ``owner`` recorded on a retire, otherwise
  its office in the org registry, otherwise none. No individual is guessed.

Authority. An operator (``UA_LIFECYCLE_OPERATORS``, default the configured
operator label, name and aliases) may change any agent. An agent may pause or terminate
itself, and may resume only from a pause it imposed on itself. Nobody else may
change another agent's state: that touches another agent's identity. Retire and
unretire are operator actions only, including for the agent itself.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, events, storage

logger = logging.getLogger("unified_agent.agent_lifecycle")

CHANNEL = "agent.lifecycle"
KIND_CHANGED = "lifecycle.changed"
KIND_STOP_CONFIRMED = "lifecycle.stop_confirmed"
KIND_REFUSED = "lifecycle.refused"
TOKEN_REVOKED_CHANNEL = "security.agent_token.revoked"
#: The device sign-in revoke a retire writes (same channel as a device revoke).
OAUTH_REVOKED_CHANNEL = "hub.oauth.sessions_revoked"
STATES = ("active", "paused", "terminated", "retired")
ACTIONS = {
    "pause": "paused",
    "resume": "active",
    "terminate": "terminated",
    "retire": "retired",
    "unretire": "active",
}
#: Actions only an operator may take, on any agent including itself.
OPERATOR_ONLY_ACTIONS = frozenset({"retire", "unretire"})
#: Actions that need a non-empty reason.
REASON_REQUIRED = frozenset({"pause", "terminate", "retire"})
#: Session-credential revoke reason a retirement writes (session_credentials.REVOKE_REASONS).
SESSION_REVOKE_REASON = "agent_retired"
MAX_REASON = 500
MAX_OWNER = 120
#: A refusal of the same label at the same site is recorded at most this often.
REFUSED_LOG_INTERVAL_S = 60.0


class LifecycleUnavailableError(ToolError):
    """The lifecycle state could not be read, so an agent credential is refused for now.

    Review of #2315: the request path fails closed. The hub answers it as a
    temporary 503 with ``code`` ``lifecycle_unavailable``; a retry may succeed.
    """

    reason = "lifecycle_unavailable"


class AgentRetiredError(ToolError):
    """A retired identity was refused (task #2305). ``reason`` is machine-readable."""

    reason = "agent_retired"

    def __init__(self, agent: str, message: str) -> None:
        super().__init__(message)
        self.agent = agent


def operators() -> frozenset[str]:
    raw = os.environ.get("UA_LIFECYCLE_OPERATORS")
    if raw is None:
        return frozenset({config.operator_label(), *config.operator_aliases()})
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _payload(row: Any) -> dict[str, Any]:
    import json

    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return {}


def _latest(conn: Any, agent: str) -> dict[str, Any] | None:
    # Case-insensitive: an event recorded under a case variant of the label is
    # the same identity's (review of #2315).
    row = conn.execute(
        "SELECT payload, created_at FROM event_log WHERE channel=? "
        "AND json_extract(payload, '$.kind')=? "
        "AND json_extract(payload, '$.agent')=? COLLATE NOCASE "
        "AND org_id IS ? "
        "ORDER BY ulid DESC LIMIT 1",
        (CHANNEL, KIND_CHANGED, agent, edition.tenant_org_id()),
    ).fetchone()
    return _payload(row) if row else None


def _state_from(event: dict[str, Any] | None, agent: str) -> dict[str, Any]:
    if not event:
        return {"agent": agent, "state": "active", "reason": None, "actor": None, "since": None}
    return {
        "agent": agent,
        "state": event.get("state") or "active",
        "reason": event.get("reason"),
        "actor": event.get("actor"),
        "since": event.get("at"),
    }


_UNREAD_OFFICES = object()


def _offices() -> list[dict[str, Any]] | None:
    """The org registry's offices, or None when the chart cannot be read."""
    from . import org_registry

    try:
        return org_registry.load_registry()
    except Exception:  # noqa: BLE001 - an unreadable chart means "no default owner"
        logger.warning("org registry unreadable; owner of record has no default", exc_info=True)
        return None


def _owner_of_record(conn: Any, agent: str, offices: Any = _UNREAD_OFFICES) -> dict[str, Any]:
    """Who owns ``agent``: an owner recorded on a lifecycle event, else its office.

    ``owner_source`` is ``explicit`` (recorded on a retire), ``org_registry``
    (the agent's first office in the chart), ``unavailable`` (the chart could
    not be read) or None (no owner). An individual is never inferred.
    """
    row = conn.execute(
        "SELECT payload FROM event_log WHERE channel=? AND json_extract(payload, '$.kind')=? "
        "AND json_extract(payload, '$.agent')=? AND json_extract(payload, '$.owner') IS NOT NULL "
        "AND org_id IS ? "
        "ORDER BY ulid DESC LIMIT 1",
        (CHANNEL, KIND_CHANGED, agent, edition.tenant_org_id()),
    ).fetchone()
    if row is not None:
        owner = _payload(row).get("owner")
        if owner:
            return {"owner_of_record": owner, "owner_source": "explicit"}
    from . import org_registry

    if offices is _UNREAD_OFFICES:
        offices = _offices()
    if offices is None:
        return {"owner_of_record": None, "owner_source": "unavailable"}
    office = org_registry.office_for_identity(agent, offices)
    return {"owner_of_record": office, "owner_source": "org_registry" if office else None}


def state_on(conn: Any, agent: str, *, offices: Any = _UNREAD_OFFICES) -> dict[str, Any]:
    """The agent's current lifecycle state and owner of record, read on ``conn``."""
    return {**_state_from(_latest(conn, agent), agent), **_owner_of_record(conn, agent, offices)}


def agent_state(agent: str) -> dict[str, Any]:
    """The current lifecycle state of ``agent`` (``active`` when never changed)."""
    label = _label(agent)
    conn = storage.connect()
    try:
        return state_on(conn, label)
    finally:
        conn.close()


def _registered_labels(changed: list[str], offices: list[dict[str, Any]] | None) -> list[str]:
    labels = set(changed) | config.agent_labels()
    for office in offices or ():
        labels.update(office.get("members") or ())
    return sorted(label for label in labels if label)


def agent_states() -> dict[str, Any]:
    """Lifecycle state and owner of record per agent.

    ``agents`` lists every agent whose lifecycle was ever changed (what the
    Control Center renders); ``registered`` lists every registered label (the
    ``UA_AGENT_LABELS`` roster, org registry members and any changed agent),
    so the owner of record is queryable for each (task #2305).
    """
    offices = _offices()
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT json_extract(payload, '$.agent') AS agent FROM event_log "
            "WHERE channel=? AND json_extract(payload, '$.kind')=? AND org_id IS ?",
            (CHANNEL, KIND_CHANGED, edition.tenant_org_id()),
        ).fetchall()
        changed = sorted(r["agent"] for r in rows if r["agent"])
        states = [state_on(conn, a, offices=offices) for a in changed]
        registered = [
            state_on(conn, a, offices=offices) for a in _registered_labels(changed, offices)
        ]
    finally:
        conn.close()
    return {"agents": states, "registered": registered, "operators": sorted(operators())}


def claim_block(conn: Any, agent: str) -> str | None:
    """Why ``agent`` may not claim work right now, or None. Read-only on ``conn``.

    A store with no ``event_log`` table can hold no lifecycle change, so the
    agent is active; any other read error propagates.
    """
    try:
        current = _state_from(_latest(conn, agent), agent)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    if current["state"] == "active":
        return None
    why = f": {current['reason']}" if current.get("reason") else ""
    return f"agent {agent!r} is {current['state']} (by {current.get('actor') or 'unknown'}){why}"


def is_stopped(agent: str) -> bool:
    """True when ``agent`` is paused, terminated or retired. Raises if the store is unreadable."""
    return agent_state(agent)["state"] != "active"


# --- stopped-identity refusal (tasks #2305, #2315) ----------------------------


def retired_state(label: str | None, *, conn: Any = None) -> dict[str, Any] | None:
    """``label``'s lifecycle state when it is retired, else None.

    Reads on ``conn`` when given (a caller's transaction), else on a connection
    of its own. A store with no ``event_log`` table holds no retirement.
    """
    return stopped_state(label, conn=conn, states=("retired",))


def _retired_message(label: str, current: dict[str, Any]) -> str:
    why = f": {current['reason']}" if current.get("reason") else ""
    return (
        f"agent {label!r} is retired (by {current.get('actor') or 'unknown'}){why}. "
        "A retired identity is refused; an operator must unretire it explicitly"
    )


def refused_label(label: str | None, *, conn: Any = None) -> str | None:
    """Why ``label`` is refused as an identity, or None.

    Refused when listed in ``UA_REVOKED_AGENTS`` (the env revocation) or when
    its lifecycle state is ``retired``. Read-only.
    """
    name = config.canonical_identity(label)
    if not name:
        return None
    if config.identity_fold(name) in {config.identity_fold(r) for r in config.revoked_agents()}:
        return f"agent {name!r} is revoked (UA_REVOKED_AGENTS)"
    current = retired_state(name, conn=conn)
    return _retired_message(name, current) if current else None


_refused_lock = threading.Lock()
_refused_last: dict[tuple[str, str, str, str, str | None], float] = {}
_refusal_writers: list[threading.Thread] = []


def _write_refusal(payload: dict[str, Any], agent: str) -> None:
    try:
        events.record_event(CHANNEL, payload=payload, originator=agent, notify=False)
    except Exception:  # noqa: BLE001 - evidence is best-effort; the refusal stands
        logger.warning("lifecycle refusal for %s was not recorded", agent, exc_info=True)


def record_refusal(agent: str, *, site: str, conn: Any = None, state: str = "retired") -> None:
    """Record that a stopped ``agent`` was refused at ``site``. Rate-limited, best-effort.

    ``state`` is the lifecycle state the refusal was for (``retired`` unless a
    caller of :func:`lifecycle_refusal` refused a paused or terminated agent).

    When ``conn`` holds an open write transaction the event is written from a
    short-lived thread instead, which waits for that transaction to end: a
    second connection writing now would only wait out the busy timeout, and an
    event written on ``conn`` would roll back with the refused call.
    """
    key = (str(config.db_path()), agent, site, state, edition.tenant_org_id())
    now = time.monotonic()
    with _refused_lock:
        last = _refused_last.get(key)
        if last is not None and now - last < REFUSED_LOG_INTERVAL_S:
            return
        _refused_last[key] = now
    payload = {"kind": KIND_REFUSED, "agent": agent, "site": site, "state": state}
    payload["at"] = storage.now()
    if conn is not None and getattr(conn, "in_transaction", False):
        writer = threading.Thread(target=_write_refusal, args=(payload, agent), daemon=True)
        with _refused_lock:
            _refusal_writers[:] = [t for t in _refusal_writers if t.is_alive()]
            _refusal_writers.append(writer)
        writer.start()
        return
    _write_refusal(payload, agent)


def join_refusal_writers(timeout: float = 10.0) -> None:
    """Wait for deferred refusal events (tests and orderly shutdown)."""
    with _refused_lock:
        writers = list(_refusal_writers)
    for writer in writers:
        writer.join(timeout)


def retired_refusal(label: str | None, *, site: str, conn: Any = None) -> str | None:
    """The refusal message for a retired ``label`` (recorded as evidence), or None."""
    refusal = lifecycle_refusal(label, site=site, conn=conn, states=("retired",))
    return refusal["message"] if refusal else None


#: The lifecycle states that stop an identity from running work (task #2315).
STOPPED_STATES = ("paused", "terminated", "retired")
#: Machine-readable refusal code per stopped state. A device keys on it: a
#: paused or terminated owner waits for a resume, a retired one does not.
REFUSAL_CODES = {state: f"agent_{state}" for state in STOPPED_STATES}


def stopped_state(
    label: str | None, *, conn: Any = None, states: tuple[str, ...] = STOPPED_STATES
) -> dict[str, Any] | None:
    """``label``'s lifecycle state when it is one of ``states``, else None. Read-only.

    Reads on ``conn`` when given (a caller's transaction), else on a connection
    of its own. A store with no ``event_log`` table holds no lifecycle change.
    """
    name = config.canonical_identity(label)
    if not name:
        return None
    own = conn is None
    db = storage.connect() if own else conn
    try:
        current = _state_from(_latest(db, name), name)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    finally:
        if own:
            db.close()
    return current if current["state"] in states else None


def _stopped_message(label: str, current: dict[str, Any]) -> str:
    if current["state"] == "retired":
        return _retired_message(label, current)
    why = f": {current['reason']}" if current.get("reason") else ""
    return (
        f"agent {label!r} is {current['state']} (by {current.get('actor') or 'unknown'}){why}. "
        f"A {current['state']} identity runs no work until it is resumed"
    )


def lifecycle_refusal(
    label: str | None,
    *,
    site: str,
    conn: Any = None,
    states: tuple[str, ...] = STOPPED_STATES,
) -> dict[str, Any] | None:
    """Refuse ``label`` when its lifecycle state is in ``states`` (task #2315).

    Returns None for an identity that may run, else ``{"agent", "state",
    "code", "message"}``, and records a ``lifecycle.refused`` event carrying
    the state (rate-limited, best-effort; see :func:`record_refusal`). The
    default refuses paused, terminated and retired identities;
    :func:`retired_refusal` narrows it to ``retired``. ``UA_REVOKED_AGENTS`` is
    not read here: :func:`refused_label` keeps that check, unchanged.
    """
    current = stopped_state(label, conn=conn, states=states)
    if current is None:
        return None
    name = config.canonical_identity(label)
    record_refusal(name, site=site, conn=conn, state=current["state"])
    return {
        "agent": name,
        "state": current["state"],
        "code": REFUSAL_CODES[current["state"]],
        "message": _stopped_message(name, current),
    }


def refuse_if_retired(label: str | None, *, site: str, conn: Any = None) -> None:
    """Raise :class:`AgentRetiredError` when ``label`` is retired, recording the refusal."""
    message = retired_refusal(label, site=site, conn=conn)
    if message is not None:
        raise AgentRetiredError(config.canonical_identity(label), message)


# --- per-request identity refusal (task #2315) --------------------------------

#: How long one label's retired state is trusted on the request path. A retire
#: in this process clears its entry at once (:func:`invalidate_identity_cache`);
#: this bounds the lag for a change made by another process.
IDENTITY_CACHE_TTL_S = 5.0
#: Labels whose state is cached; past this the oldest entries are dropped.
IDENTITY_CACHE_MAX = 4096

_identity_lock = threading.Lock()
_identity_cache: dict[tuple[str, str, str | None], tuple[float, dict[str, Any] | None]] = {}


def _identity_key(label: str) -> tuple[str, str, str | None]:
    return (str(config.db_path()), config.identity_fold(label), edition.tenant_org_id())


def cached_identity_state(label: str) -> tuple[bool, dict[str, Any] | None]:
    """``(fresh, retired_state)`` from the request-path cache, without a read."""
    with _identity_lock:
        hit = _identity_cache.get(_identity_key(label))
    if hit is None:
        return False, None
    return time.monotonic() - hit[0] < IDENTITY_CACHE_TTL_S, hit[1]


def retired_identity_state(label: str) -> dict[str, Any] | None:
    """``label``'s retired state for the request path: cached up to the TTL, read-only.

    Fails closed (review of #2315). A cached "active" is used only within
    :data:`IDENTITY_CACHE_TTL_S`; past it the state is read again, and a read
    failure raises :class:`LifecycleUnavailableError` rather than letting the
    credential through. A cached retirement is kept through a read failure,
    so a store outage never reopens a retired credential.
    """
    key = _identity_key(label)
    fresh, state = cached_identity_state(label)
    if fresh:
        return state
    try:
        state = retired_state(label)
    except Exception as exc:  # noqa: BLE001 - any read fault fails closed below
        with _identity_lock:
            hit = _identity_cache.get(key)
        if hit is not None and hit[1] is not None:
            return hit[1]  # a known retirement stands through the outage
        logger.warning("lifecycle state for %s is unreadable; refusing for now", label)
        raise LifecycleUnavailableError(
            "the hub cannot read this agent's lifecycle state right now; retry shortly"
        ) from exc
    with _identity_lock:
        if len(_identity_cache) >= IDENTITY_CACHE_MAX:
            for stale in sorted(_identity_cache, key=lambda k: _identity_cache[k][0])[
                : IDENTITY_CACHE_MAX // 4
            ]:
                _identity_cache.pop(stale, None)
        _identity_cache[key] = (time.monotonic(), state)
    return state


def invalidate_identity_cache(label: str | None = None) -> None:
    """Drop the cached state for ``label`` and its case variants (every label when None)."""
    with _identity_lock:
        if label is None:
            _identity_cache.clear()
            return
        folded = config.identity_fold(label)
        for key in [k for k in _identity_cache if k[1] == folded]:
            _identity_cache.pop(key, None)


def refuse_retired_identity(label: str | None, *, site: str) -> None:
    """Raise :class:`AgentRetiredError` when the verified ``label`` is retired (task #2315).

    The hub's agent-credential legs call this once per request. An allowed
    call reads at most once per :data:`IDENTITY_CACHE_TTL_S` and never writes;
    a refusal records a rate-limited ``lifecycle.refused`` event. Raises
    :class:`LifecycleUnavailableError` when the state cannot be read.
    """
    name = config.canonical_identity(label)
    if not name:
        return
    current = retired_identity_state(name)
    if current is None:
        return
    record_refusal(name, site=site, state="retired")
    raise AgentRetiredError(name, _retired_message(name, current))


def _label(agent: str) -> str:
    if not isinstance(agent, str) or not agent.strip():
        raise ToolError("agent label must be a non-empty string")
    return config.canonical_identity(agent)


def _authorize(actor: str | None, agent: str, action: str, current: dict[str, Any]) -> None:
    if not actor:
        raise ToolError("agent lifecycle changes require an authenticated identity")
    if action in OPERATOR_ONLY_ACTIONS and actor not in operators():
        raise ToolError(
            f"{actor!r} may not {action} {agent!r}: retirement is an operator action "
            "(UA_LIFECYCLE_OPERATORS), including for an agent's own label"
        )
    if actor in operators():
        return
    if actor != agent:
        raise ToolError(
            f"{actor!r} may not {action} {agent!r}: changing another agent's lifecycle "
            "is an operator action (UA_LIFECYCLE_OPERATORS)"
        )
    if action == "resume" and current.get("actor") != agent:
        raise ToolError(
            f"{agent!r} was {current['state']} by {current.get('actor')!r}; only an operator "
            "may resume it"
        )


# --- stop fan-out ------------------------------------------------------------


# The dispatch and node stops live with the subsystems that own them (task
# #1723): ``dispatch.coordinator`` and ``node_queue`` sit in the runtime layer,
# above this module, so they register a handler here when they are imported
# instead of this module importing them. The set of handlers a stop needs is
# fixed below, so a process that never loaded one is detectable: a pause or
# terminate there is refused before anything is written, never recorded with
# a silently skipped cancel.

StopHandler = Callable[[str, str, str], list[dict[str, Any]]]

#: Every handler a pause or terminate must run, in fan-out order. ``dispatch``
#: cancels the agent's live dispatch runs (``coordinator.cancel_run``);
#: ``node`` cancels its queued and claimed ua-node items (``node_queue.cancel``).
REQUIRED_STOP_HANDLERS: tuple[str, ...] = ("dispatch", "node")

_STOP_HANDLERS: dict[str, StopHandler] = {}


def register_stop_handler(name: str, handler: StopHandler) -> None:
    """Register the stop for one execution path. Called at import by its owner.

    ``handler(agent, actor, reason)`` requests a stop on each of ``agent``'s
    in-flight items on that path and returns one row per item (``path``,
    ``ref``, ``control``, ``detail``). Registering a name again replaces the
    handler, so a module reload is harmless. A name outside
    :data:`REQUIRED_STOP_HANDLERS` is refused: an unlisted handler would never
    be checked for, so it could go missing without anyone noticing.
    """
    if name not in REQUIRED_STOP_HANDLERS:
        raise ValueError(f"unknown stop handler {name!r}; add it to REQUIRED_STOP_HANDLERS first")
    if not callable(handler):
        raise TypeError(f"stop handler {name!r} must be callable")
    _STOP_HANDLERS[name] = handler


def missing_stop_handlers() -> tuple[str, ...]:
    """The required stop handlers this process has not registered, in order."""
    return tuple(n for n in REQUIRED_STOP_HANDLERS if n not in _STOP_HANDLERS)


def _require_stop_handlers(agent: str, verb: str) -> dict[str, StopHandler]:
    missing = missing_stop_handlers()
    if missing:
        raise ToolError(
            f"cannot {verb} {agent!r}: this process has not registered the stop "
            f"handler(s) {', '.join(missing)}, so the agent's in-flight work would not "
            "be cancelled. Nothing was changed. Run the change through the hub (MCP "
            "coordination_manage or the Control Center), which loads them."
        )
    return {n: _STOP_HANDLERS[n] for n in REQUIRED_STOP_HANDLERS}


def _executor_attempts(agent: str) -> list[dict[str, Any]]:
    conn = storage.connect()
    try:
        try:
            org = edition.tenant_org_id()
            # Unbound legacy work is private-only; a configured org needs a scoped parent.
            rows = conn.execute(
                "SELECT id, task_id FROM executor_attempts WHERE agent=? AND finished_at IS NULL "
                "AND (task_id IS NULL OR task_id IN (SELECT id FROM tasks WHERE org_id IS ?)) "
                "AND (delegation_id IS NULL OR delegation_id IN "
                "(SELECT id FROM delegations WHERE org_id IS ?)) "
                "AND (? IS NULL OR task_id IS NOT NULL OR delegation_id IS NOT NULL)",
                (agent, org, org, org),
            ).fetchall()
        except Exception:  # noqa: BLE001
            rows = []
    finally:
        conn.close()
    return [
        {
            "path": "executor",
            "ref": f"attempt:{r['id']}",
            "task_id": r["task_id"],
            "control": "cooperative",
            "detail": "the executor daemon's kill check stops the child on its next tick",
        }
        for r in rows
    ]


def _doing_tasks(agent: str) -> list[int]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE claimed_by=? AND status='doing' AND org_id IS ? ORDER BY id",
            (agent, edition.tenant_org_id()),
        ).fetchall()
    finally:
        conn.close()
    return [int(r["id"]) for r in rows]


def _release_claims(agent: str, task_ids: list[int], reason: str) -> list[dict[str, Any]]:
    """Put a terminated agent's ``doing`` tasks back to ``open`` and close their runs."""
    from . import run_records

    out = []
    for tid in task_ids:
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE tasks SET status='open', claimed_by=NULL, claimed_at=NULL, "
                "lock_expires_at=NULL, updated_at=? WHERE id=? AND claimed_by=? "
                "AND status='doing' AND org_id IS ?",
                (storage.next_task_revision(conn, tid), tid, agent, edition.tenant_org_id()),
            )
            conn.commit()
            released = cur.rowcount == 1
        finally:
            conn.close()
        if released:
            run_records.safe(
                run_records.record_abandoned,
                tid,
                agent=agent,
                closed_by="agent_lifecycle",
                reason=f"agent terminated: {reason}",
            )
        out.append(
            {
                "path": "claim",
                "ref": f"task:{tid}",
                "control": "released" if released else "unchanged",
                "detail": "returned to open" if released else "task moved on before release",
            }
        )
    return out


def _fan_out(
    agent: str,
    actor: str,
    new_state: str,
    reason: str,
    handlers: dict[str, StopHandler],
) -> list[dict[str, Any]]:
    stops: list[dict[str, Any]] = []
    for name in REQUIRED_STOP_HANDLERS:
        stops += handlers[name](agent, actor, reason)
    attempts = _executor_attempts(agent)
    stops += attempts
    executor_tasks = {a["task_id"] for a in attempts}
    doing = _doing_tasks(agent)
    for tid in doing:
        if tid in executor_tasks:
            continue
        stops.append(
            {
                "path": "self-pickup",
                "ref": f"task:{tid}",
                "control": "unsupported",
                "detail": "no hub-controllable process; new claims and renewals are refused",
            }
        )
    if new_state == "terminated":
        stops += _release_claims(agent, doing, reason)
    return stops


# --- writer ------------------------------------------------------------------


def _owner_value(owner: str | None) -> str | None:
    """An explicit owner of record: an org office (stored by id) or a known identity label."""
    if owner is None:
        return None
    if not isinstance(owner, str):
        raise ToolError("owner must be a string: an org office id or name, or an identity label")
    text = owner.strip()
    if not text:
        return None
    if len(text) > MAX_OWNER:
        raise ToolError(f"owner is longer than {MAX_OWNER} characters")
    from . import org_registry

    try:
        office = org_registry.find_office(text)
    except Exception:  # noqa: BLE001 - an unreadable chart still allows a label owner
        office = None
    if office is not None:
        return str(office["id"])
    if (
        text == config.operator_label()
        or text in config.human_identity_labels()
        or config.is_known_agent_label(text)
    ):
        return text
    raise ToolError(
        f"owner {text!r} is neither an org office nor a known identity label; "
        "the owner of record is never guessed"
    )


def _doing_on(conn: Any, agent: str) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM tasks WHERE claimed_by=? COLLATE NOCASE AND status='doing' "
        "AND org_id IS ? ORDER BY id",
        (agent, edition.tenant_org_id()),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def _doing_refusal(agent: str, task_ids: list[int]) -> ToolError:
    shown = ", ".join(f"#{t}" for t in task_ids[:20])
    more = f" and {len(task_ids) - 20} more" if len(task_ids) > 20 else ""
    return ToolError(
        f"cannot retire {agent!r}: it holds {len(task_ids)} doing claim(s) ({shown}{more}). "
        "Release or cancel them first (task_update, coordination_manage task_claim_release, "
        "or agent_terminate). Nothing was changed."
    )


def _check_transition(label: str, verb: str, current: dict[str, Any]) -> None:
    """Refuse a transition the current state does not allow. Pure; runs under the lock."""
    if current["state"] == "retired" and verb in ("pause", "resume", "terminate"):
        raise ToolError(
            f"{label!r} is retired; {verb} cannot change a retired agent. An operator must "
            "unretire it explicitly (coordination_manage agent_unretire)"
        )
    if verb == "unretire" and current["state"] not in ("retired", "active"):
        raise ToolError(f"{label!r} is not retired (it is {current['state']}); use resume")
    if current["state"] == "terminated" and verb == "pause":
        raise ToolError(f"{label!r} is terminated; resume it before pausing")


def _before_append(conn: Any, label: str, verb: str) -> None:
    """Test seam: runs inside the transition's transaction, just before its event append."""


def _transition(
    label: str, verb: str, who: str | None, payload: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, list[Any]] | None]:
    """Read, authorize, check and append one transition in one ``BEGIN IMMEDIATE``.

    The state a transition is judged against is the state its event follows:
    no other transition can append between the read and the write (review of
    #2305, where a pause read before an operator's retire and appended after
    it, overwriting the retirement). Returns ``(current, revoked)``:
    ``current`` is the unchanged state for a no-op (nothing written), else
    None; ``revoked`` lists what a retire revoked.

    A retire also refuses here while the agent holds ``doing`` claims, and
    revokes its ``agent_tokens`` rows, session credentials and device sign-ins
    (OAuth refresh and access tokens, task #2315) in the same transaction. An
    unretire restores none of them: the device signs in again.
    OAuth identities remain hub-global under the one-org-per-store contract;
    the row fences here do not introduce multi-org OAuth ownership.
    """
    from . import identity_core, oauth_store, session_credentials
    from .tools import token_lifecycle

    new_state = ACTIONS[verb]
    published: list[dict[str, Any]] = []
    revoked: dict[str, list[Any]] | None = None
    signins: list[dict[str, Any]] = []
    # A retire also holds the OAuth provider's rotation lock, so a refresh
    # rotation cannot mint a successor between the delete and the commit.
    provider = identity_core.oauth_provider() if verb == "retire" else None
    rotation = getattr(provider, "_rotation_lock", None) or contextlib.nullcontext()
    lock = token_lifecycle.credential_lock() if verb == "retire" else contextlib.nullcontext()
    with lock, rotation:
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # State only: the owner of record reads the org chart, which has no
            # place inside the write lock and plays no part in authorization.
            current = _state_from(_latest(conn, label), label)
            _authorize(who, label, verb, current)
            _check_transition(label, verb, current)
            if current["state"] == new_state:
                conn.rollback()
                return current, None
            if verb == "retire":
                doing = _doing_on(conn, label)
                if doing:
                    raise _doing_refusal(label, doing)
                now = payload["at"]
                signins = oauth_store.revoke_for_agent(conn, label)
                revoked = {
                    "agent_tokens": token_lifecycle.revoke_for_agent(conn, label, revoked_at=now),
                    "session_credentials": session_credentials.revoke_for_agent(
                        conn, label, SESSION_REVOKE_REASON
                    ),
                    # The public session handle of each sign-in (refresh row);
                    # its access tokens go with it and are not listed.
                    "device_signins": sorted(
                        {row["session_id"] for row in signins if row["kind"] == "refresh"}
                    ),
                }
                payload["revoked"] = revoked
            payload["previous"] = current["state"]
            _before_append(conn, label, verb)
            _, _, event = events.record_event_once_on_connection(
                conn, CHANNEL, payload=payload, originator=who
            )
            published.append(event)
            if revoked and revoked["agent_tokens"]:
                _, _, event = events.record_event_once_on_connection(
                    conn,
                    TOKEN_REVOKED_CHANNEL,
                    payload={
                        "agent_label": label,
                        "token_ids": revoked["agent_tokens"],
                        "reason": "agent_retired",
                        "revoked_at": payload["at"],
                    },
                    originator=who,
                )
                published.append(event)
            if signins:
                _, _, event = events.record_event_once_on_connection(
                    conn,
                    OAUTH_REVOKED_CHANNEL,
                    payload={
                        "device": None,
                        "session_id": None,
                        "revoked": len(revoked["device_signins"]),
                        "agents": [label],
                        "session_ids": revoked["device_signins"],
                        "reason": "agent_retired",
                        "actor": who,
                    },
                    originator=who,
                )
                published.append(event)
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        if revoked is not None and provider is not None:
            # Still under the rotation lock: drop the provider's in-memory copies
            # of what the commit deleted (and any memory-only token the store
            # never held), so no cached token outlives the retire.
            try:
                provider.forget_subject(label, {row["token_sha256"] for row in signins})
            except Exception:  # noqa: BLE001 - the durable rows are gone either way
                logger.warning("in-memory OAuth tokens for %s were not dropped", label)
    invalidate_identity_cache(label)
    for event in published:
        if event is not None:
            try:
                events.publish(event)
            except Exception:  # noqa: BLE001 - the rows are durable; fan-out is best-effort
                logger.warning("lifecycle event fan-out for %s failed", label, exc_info=True)
    if revoked:
        session_credentials.emit_revoked(
            revoked["session_credentials"], SESSION_REVOKE_REASON, actor=who
        )
    return None, revoked


def set_agent_state(
    agent: str,
    action: str,
    reason: str = "",
    actor: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """Pause, resume, terminate, retire or unretire ``agent``; record it and request stops.

    ``actor`` is used only when there is no verified identity (the Control
    Center passes its operator session). ``owner`` (retire only) records the
    owner of record. Returns the new state and one row per stop target with
    its honest ``control`` result; a retire also returns what it ``revoked``.
    Authorization and the state checks run against the state read inside the
    transaction that appends the event (:func:`_transition`).
    """
    label = _label(agent)
    verb = (action or "").strip().lower()
    if verb not in ACTIONS:
        raise ToolError(f"action must be one of {tuple(ACTIONS)}")
    who = config.verified_agent_identity() or (actor.strip() if actor and actor.strip() else None)
    text = (reason or "").strip()[:MAX_REASON]
    if verb in REASON_REQUIRED and not text:
        raise ToolError(f"{verb} requires a reason")
    if owner is not None and verb != "retire":
        raise ToolError("owner is recorded on retire only")
    if not who:
        raise ToolError("agent lifecycle changes require an authenticated identity")
    owner_value = _owner_value(owner) if verb == "retire" else None
    new_state = ACTIONS[verb]
    # Fail closed (task #1723): resolve the stop handlers before writing
    # anything, so a process that cannot cancel the agent's work refuses the
    # change instead of recording a stop it never requested.
    handlers = _require_stop_handlers(label, verb) if new_state != "active" else {}
    payload: dict[str, Any] = {
        "kind": KIND_CHANGED,
        "agent": label,
        "state": new_state,
        "previous": None,
        "action": verb,
        "reason": text or None,
        "actor": who,
        "at": storage.now(),
    }
    if verb == "retire":
        payload["owner"] = owner_value
    unchanged, revoked = _transition(label, verb, who, payload)
    if unchanged is not None:
        return {**state_on_label(label), "changed": False, "stops": []}
    stops: list[dict[str, Any]] = []
    if new_state != "active":
        stops = _fan_out(label, who, new_state, text, handlers)
        try:
            events.record_event(
                CHANNEL,
                payload={
                    "kind": "lifecycle.stops_requested",
                    "agent": label,
                    "actor": who,
                    "stops": stops,
                },
                originator=who,
            )
        except Exception:  # noqa: BLE001 - the state change itself is recorded
            logger.warning("stop fan-out for %s not recorded", label, exc_info=True)
    try:
        storage.record_agent_event(
            "lifecycle", ref_id=label, agent=who, summary=f"{verb} {label}: {text}"[:200]
        )
    except Exception:  # noqa: BLE001
        pass
    result = {**state_on_label(label), "changed": True, "stops": stops}
    if revoked is not None:
        result["revoked"] = revoked
    return result


def state_on_label(label: str) -> dict[str, Any]:
    """:func:`state_on` on a connection of its own."""
    conn = storage.connect()
    try:
        return state_on(conn, label)
    finally:
        conn.close()


def record_stop_confirmed(agent: str, *, task_id: int | None, detail: str) -> None:
    """Executor-side evidence that a child was stopped because of a pause. Best-effort."""
    try:
        events.record_event(
            CHANNEL,
            payload={
                "kind": KIND_STOP_CONFIRMED,
                "agent": agent,
                "task_id": task_id,
                "detail": detail[:300],
                "at": storage.now(),
            },
            originator=agent,
        )
    except Exception:  # noqa: BLE001
        logger.warning("lifecycle stop confirmation for %s not recorded", agent, exc_info=True)


def agent_pause(agent: str, reason: str) -> dict[str, Any]:
    """Pause ``agent``: refuse its claims and request stops on its in-flight work."""
    return set_agent_state(agent, "pause", reason)


def agent_resume(agent: str, reason: str = "") -> dict[str, Any]:
    """Resume a paused or terminated ``agent``. A retired agent needs :func:`agent_unretire`."""
    return set_agent_state(agent, "resume", reason)


def agent_terminate(agent: str, reason: str) -> dict[str, Any]:
    """Terminate ``agent``: pause semantics plus releasing its ``doing`` claims."""
    return set_agent_state(agent, "terminate", reason)


def agent_retire(agent: str, reason: str, owner: str | None = None) -> dict[str, Any]:
    """Retire ``agent`` (operators only): refuse it everywhere and revoke its credentials.

    Refused while the agent holds ``doing`` claims. ``owner`` records the owner
    of record (an org office or identity label); omitted, the org registry
    office stands. Retiring twice is a no-op; ``resume`` cannot undo it.
    """
    return set_agent_state(agent, "retire", reason, owner=owner)


def agent_unretire(agent: str, reason: str = "") -> dict[str, Any]:
    """Unretire ``agent`` (operators only). Revoked credentials stay revoked."""
    return set_agent_state(agent, "unretire", reason)
