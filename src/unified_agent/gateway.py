"""Call-time gateway policy (task #1357).

Profile tool lists decide who can *see* a tool. This module decides whether
*this call* is allowed once the tool is visible. The two decisions stay
separate: a rate limit or a deny never hides a tool from ``tools/list``, and
an empty policy leaves every current profile's calls allowed.

Rate limits are per tool and per caller, with a hard cap on the window, the
count, and the number of in-memory buckets. Buckets live in this process and
reset when the hub restarts. The HTTP hub is one process, so its callers share
one table; a satellite stdio server is a separate process and a separate table.
Every registered tool call has a durable decision event on the evidence
ledger, with a reason code. Direct policy checks also record their denials.
The event carries an argument digest, and a mutating call fails closed when
its event cannot be written (task #1907, see ``record_call_result``). A
read-only ALLOW is counted in memory and written as an hourly rollup instead
of one row per call (task #1922, see ``_ReadCoalescer``).

Bindings (``company``, ``agent``, ``project``, ``task``) select which calls a
rule applies to. Deny beats allow. Trust presets are task #1421 and are not
accepted here.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, events, storage

logger = logging.getLogger("unified_agent.gateway")

CHANNEL_GATEWAY_DECISION = "agent.gateway.decision"

_DECISION_FAILURE_WINDOW_S = 300
_last_decision_failure: tuple[float, str] | None = None
_decision_write_seen = False


def _note_decision_failure(exc: Exception) -> str:
    """Record a bounded diagnostic without publishing exception text or SQL."""
    global _last_decision_failure
    name = getattr(exc, "sqlite_errorname", None) if isinstance(exc, sqlite3.Error) else None
    kind = name or type(exc).__name__
    _last_decision_failure = (time.monotonic(), kind)
    return kind


def _note_decision_commit() -> None:
    global _decision_write_seen
    _decision_write_seen = True


def decision_write_health() -> tuple[str, str]:
    """Read this gateway process's recent decision-write observations."""
    failure = _last_decision_failure
    if failure is not None and time.monotonic() - failure[0] <= _DECISION_FAILURE_WINDOW_S:
        return "DEGRADED", f"decision write failed in the last five minutes ({failure[1]})"
    if _decision_write_seen:
        return "LIVE", "a decision write committed in this process; no recent observed failure"
    return "UNKNOWN", "no committed decision write observed in this process"


#: The episode a session-credential request is bound to is ``acp:<item_id>``:
#: one node dispatch item, i.e. one run (task #2309). Every event that request
#: writes, deny or allow, carries it in ``event_log.episode_id``.
SESSION_EPISODE_PREFIX = "acp:"
#: The episode an interactive (bearer or device sign-in) request is bound to is
#: ``run:<run_id>``: the caller's one open self-pickup run (task #2313). A
#: caller with no such run, or with more than one live claim, gets no episode.
RUN_EPISODE_PREFIX = "run:"
#: One event per (caller, tool, profile, decision, bucket) of coalesced
#: read-only allows (task #1922). ``count`` is the number of calls it stands for.
CHANNEL_GATEWAY_DECISION_ROLLUP = "agent.gateway.decision.rollup"

# Bounds are code, not operator knobs. A policy file cannot raise them.
MAX_WINDOW_SECONDS = 3600.0
MAX_CALLS = 1000
MAX_BUCKETS = 2048

BIND_KEYS = ("company", "agent", "project", "task")


@dataclass(frozen=True)
class CallScope:
    """Server-side context a binding is matched against.

    ``agent`` is the verified caller, never a client-supplied label inside the
    payload. ``projects``, ``tasks``, and ``companies`` come from that caller's
    active claims when the policy asks for them. An explicit scope (tests, or
    a caller that already resolved claims) is trusted as given.
    """

    agent: str = ""
    profile: str = ""
    companies: frozenset[str] = field(default_factory=frozenset)
    projects: frozenset[str] = field(default_factory=frozenset)
    tasks: frozenset[str] = field(default_factory=frozenset)
    claims: frozenset[ClaimBind] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ClaimBind:
    """One live claim. Bind keys must all match this row, not a union of rows."""

    company: str = ""
    project: str = ""
    task: str = ""


@dataclass
class _Bucket:
    stamps: list[float]
    window: float
    max_calls: int


_Key = tuple[str, str, str, str]


class _RateLimits:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[_Key, _Bucket] = {}
        self._active_rules: frozenset[tuple[str, str]] | None = None

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._active_rules = None

    def reconcile(self, active_rules: frozenset[tuple[str, str]]) -> None:
        """Discard buckets for removed or edited rate rules, preserving unchanged ones."""
        with self._lock:
            if self._active_rules == active_rules:
                return
            self._buckets = {
                key: bucket
                for key, bucket in self._buckets.items()
                if key[1].startswith("explicit:") or (key[0], key[1]) in active_rules
            }
            self._active_rules = active_rules

    def count(self) -> int:
        with self._lock:
            return len(self._buckets)

    def admit(
        self,
        specs: list[tuple[_Key, int, float]],
        now: float,
    ) -> tuple[int | None, bool]:
        """Consume one call against every spec, or report which one is full.

        Returns ``(index, capacity_exhausted)``. The check and the increment
        are one critical section; a refused call consumes nothing. A new
        bucket that cannot be stored because every bucket still holds a live
        window is refused rather than dropping someone else's budget.
        """
        with self._lock:
            # A refused call must not change any bucket. Resets for an edited
            # rule and expiry pruning are staged until every spec can be charged.
            resets: list[_Key] = []
            live_stamps: dict[_Key, list[float]] = {}
            for index, (key, max_calls, window) in enumerate(specs):
                bucket = self._buckets.get(key)
                if bucket is None:
                    continue
                if bucket.window != window or bucket.max_calls != max_calls:
                    resets.append(key)
                    continue
                live = [stamp for stamp in bucket.stamps if now - stamp < window]
                live_stamps[key] = live
                if len(live) >= max_calls:
                    return index, False
            expired = self._expired_keys(now)
            kept = set(self._buckets) - expired - set(resets)
            final = kept | {key for key, _, _ in specs}
            if len(final) > MAX_BUCKETS:
                overflow = next(key for key, _, _ in specs if key not in kept)
                first = next(i for i, spec in enumerate(specs) if spec[0] == overflow)
                return first, True
            for key in resets:
                self._buckets.pop(key, None)
            for key in list(self._buckets):
                if key not in final:
                    del self._buckets[key]
            for key, stamps in live_stamps.items():
                self._buckets[key].stamps = stamps
            for key, spec_max, window in specs:
                bucket = self._buckets.get(key)
                if bucket is None:
                    bucket = _Bucket([], window, spec_max)
                    self._buckets[key] = bucket
                bucket.stamps.append(now)
            return None, False

    def refund(self, keys: list[_Key], now: float) -> None:
        """Return one stamp per key, taken by a call that did not run."""
        with self._lock:
            for key in keys:
                bucket = self._buckets.get(key)
                if bucket is None:
                    continue
                try:
                    bucket.stamps.remove(now)
                except ValueError:
                    continue
                if not bucket.stamps:
                    del self._buckets[key]

    def _expired_keys(self, now: float) -> set[_Key]:
        return {
            key
            for key, bucket in self._buckets.items()
            if all(now - stamp >= bucket.window for stamp in bucket.stamps)
        }


_LIMITS = _RateLimits()


@dataclass
class _Invocation:
    tool: str | None = None
    charged: set[_Key] = field(default_factory=set)
    reservations: list[Reservation] = field(default_factory=list)
    committed: bool = False
    progressed: bool = False
    parent: _Invocation | None = None
    decision_recorded: bool = False
    registered_arguments: dict[str, Any] | None = None
    transaction_owner: Callable[..., None] | None = None
    merge_policy_arguments: dict[str, Any] | None = None
    merge_approval_payload: dict[str, Any] | None = None

    def commit(self) -> None:
        """Mark that the tool is about to run. Its slots are now spent."""
        self.committed = True
        self.progressed = True
        if self.parent is not None:
            self.parent.progressed = True

    def refund(self) -> None:
        """Return every slot this invocation reserved. Use before execution only."""
        for reservation in self.reservations:
            reservation.refund()
        self.reservations.clear()


# The governed tool invocation in progress. A policy recheck for the same tool
# inside it (gcloud_ops, LangGraph prechecks) is one call, not two.
_CURRENT: contextvars.ContextVar[_Invocation | None] = contextvars.ContextVar(
    "gateway_invocation", default=None
)


def _is_approval_pending(exc: BaseException) -> bool:
    from .tools.approvals import ApprovalPendingError

    return isinstance(exc, ApprovalPendingError)


def _is_denied_approval(exc: BaseException) -> bool:
    from mcp.server.mcpserver.exceptions import ToolError

    return isinstance(exc, ToolError) and " was DENIED:" in str(exc)


@contextlib.contextmanager
def invocation(tool: str | None = None) -> Iterator[_Invocation]:
    """Scope one governed tool invocation.

    A scope for the same ``tool`` nested inside this one joins it, so an
    adapter's prechecks and the registered call it then makes are charged
    once. A different tool opens a fresh scope: each workflow step is its own
    call. An approval that parks before any nested call finishes returns this
    scope's slots. A scope that already finished a child call keeps its slots:
    a later parked step must not erase the outer budget.
    """
    current = _CURRENT.get()
    if current is not None and tool is not None and current.tool == tool:
        yield current
        return
    state = _Invocation(tool=tool, parent=current)
    token = _CURRENT.set(state)
    try:
        yield state
    except BaseException as exc:
        if state.parent is not None and state.progressed:
            state.parent.progressed = True
        if (
            not state.committed
            and not state.progressed
            and (_is_approval_pending(exc) or _is_denied_approval(exc))
        ):
            state.refund()
        raise
    else:
        if state.parent is not None:
            state.parent.progressed = True
    finally:
        _CURRENT.reset(token)


@dataclass
class Reservation:
    """Rate slots taken for a call that may still be refused by approval."""

    keys: list[_Key] = field(default_factory=list)
    moment: float = 0.0
    scope: _Invocation | None = None

    def refund(self) -> None:
        if not self.keys:
            return
        _LIMITS.refund(self.keys, self.moment)
        if self.scope is not None:
            self.scope.charged.difference_update(self.keys)
        self.keys = []


def reset_limits() -> None:
    """Drop in-process counters. Tests use this; a hub restart does the same."""
    _LIMITS.reset()


def bucket_count() -> int:
    return _LIMITS.count()


def live_bucket_count(
    now: float | None = None, *, keep: frozenset[tuple[str, str]] | None = None
) -> int:
    """Buckets that would count against MAX_BUCKETS on the next call (task #2144).

    ``admit`` prunes fully-expired windows before the MAX_BUCKETS check, so a
    dead bucket never refuses a new one. ``keep`` is the ``(rule, generation)``
    set the next ``evaluate_policy`` sweep would keep (``reconcile_rate_rules``
    over the loaded file); a bucket outside it is dropped before ``admit``
    runs, so it is not counted either. ``explicit:`` generations survive the
    sweep and always count. ``None`` counts every live bucket. Read-only: the
    table is not changed. ``now`` overrides the monotonic clock, matching
    ``evaluate_policy``'s ``now``.
    """
    moment = time.monotonic() if now is None else float(now)
    with _LIMITS._lock:
        return sum(
            1
            for key, bucket in _LIMITS._buckets.items()
            if (keep is None or key[1].startswith("explicit:") or (key[0], key[1]) in keep)
            and any(moment - stamp < bucket.window for stamp in bucket.stamps)
        )


def rate_limit_snapshot(
    agent: str | None = None, *, now: float | None = None
) -> list[dict[str, Any]]:
    """Live rate-limit counters for the per-tool allowance view (task #2144).

    Reads the same in-process table ``reserve_rate_limits`` charges, under its
    lock — the enforcement counter itself, not a copy. Each row is one
    ``(rule, rule generation, tool, caller)`` bucket: ``used`` is the number of
    charged calls still inside the sliding window (exactly what ``admit``
    compares to ``limit``) and ``oldest_live`` the monotonic stamp of the
    earliest one, so a caller stopped at ``used == limit`` is refused until
    ``oldest_live + window_seconds``. ``agent`` narrows to one caller;
    ``None`` returns every caller. ``now`` overrides the monotonic clock
    (tests), matching ``evaluate_policy``'s ``now``.
    """
    moment = time.monotonic() if now is None else float(now)
    rows: list[dict[str, Any]] = []
    with _LIMITS._lock:
        for (rule, generation, tool, caller), bucket in _LIMITS._buckets.items():
            if agent is not None and caller != agent:
                continue
            live = sorted(s for s in bucket.stamps if moment - s < bucket.window)
            rows.append(
                {
                    "rule": rule,
                    "generation": generation,
                    "tool": tool,
                    "agent": caller,
                    "used": len(live),
                    "limit": bucket.max_calls,
                    "window_seconds": bucket.window,
                    "oldest_live": live[0] if live else None,
                }
            )
    rows.sort(key=lambda r: (r["agent"], r["tool"], r["rule"], r["generation"]))
    return rows


def registered_task_update_arguments() -> dict | None:
    """Trusted selected-writer arguments, populated only by its registration."""
    call = _CURRENT.get()
    if call is None or call.tool != "task_update" or call.registered_arguments is None:
        return None
    return copy.deepcopy(call.registered_arguments)


@contextlib.contextmanager
def registered_transaction_owner(owner: Callable[..., None]) -> Iterator[None]:
    """Trusted service composition installs its owner for the selected call.

    This private invocation state is not a request parameter. Core gateway code
    depends on the supplied port rather than importing the higher task service.
    """
    call = _CURRENT.get()
    if call is None or call.tool != "task_update" or call.registered_arguments is None:
        yield
        return
    previous = call.transaction_owner
    call.transaction_owner = owner
    try:
        yield
    finally:
        call.transaction_owner = previous


def release_unprogressed_invocation() -> None:
    """Return this scope's slots when a caller swallowed a pre-work park.

    ``workflow_run`` catches ``ApprovalPendingError`` and returns a pending
    result, so the scope exits normally. Refund only when no child call has
    finished. A later parked step keeps the outer charge.
    """
    state = _CURRENT.get()
    if state is not None and not state.committed and not state.progressed:
        state.refund()


def _bind_merge_policy_arguments(arguments: dict, approval_payload: dict) -> None:
    """Capture server-bound merge args separately from its historical pass key.

    Only trusted adapter plumbing calls this. Rule evaluation always receives
    the complete bound arguments; approval lookup retains the exact original
    wrapper kwargs so existing one-shot passes keep their meaning.
    """
    current = _CURRENT.get()
    if current is None or current.tool != "git_merge_task":
        raise RuntimeError("task merge policy binding requires its invocation")
    if current.merge_policy_arguments is not None:
        if current.merge_policy_arguments != arguments:
            raise RuntimeError("task merge arguments changed within one invocation")
        return
    current.merge_policy_arguments = copy.deepcopy(arguments)
    current.merge_approval_payload = copy.deepcopy(approval_payload)


def _merge_policy_approval_payload(arguments: dict | None) -> dict:
    """Return the exact pass key while refusing a changed internal action."""
    current = _CURRENT.get()
    if current is None or current.tool != "git_merge_task":
        return copy.deepcopy(arguments or {})
    if current.merge_policy_arguments is None:
        _bind_merge_policy_arguments(arguments or {}, arguments or {})
    elif current.merge_policy_arguments != (arguments or {}):
        raise RuntimeError("task merge arguments changed within one invocation")
    return copy.deepcopy(current.merge_approval_payload)


def mark_execution_started() -> None:
    """Commit the current call's reservations at its real execution boundary."""
    current = _CURRENT.get()
    if current is not None:
        current.commit()


def bind_needs_claims(bind: dict[str, str]) -> bool:
    """True when matching this bind requires the caller's active claims."""
    if bind.get("project") not in (None, "*") or bind.get("task") not in (None, "*"):
        return True
    company = bind.get("company")
    return bool(company and company != "*")


def binding_matches(
    bind: dict[str, str], scope: CallScope, *, unclaimed_matches: bool = False
) -> bool:
    """Every specified bind key must match. ``*`` and an omitted key are open.

    ``agent`` is the verified identity only. The active profile is visibility,
    not a bind target.

    ``company``, ``project``, and ``task`` come from live claims. Specified
    claim keys must all match one claim row, not a union across tasks. A
    caller with no live claim of that kind is not known to be outside the
    scope, so with ``unclaimed_matches`` (restrictive rules) it is treated as
    inside. A blank field on a claim is not a known conflict. A restrictive
    rule still matches when some claim has no conflicting value. A known
    value that disagrees with the bind, on every claim, is a miss.
    """
    if not bind:
        return True
    if bind.get("agent") not in (None, "*") and scope.agent != bind["agent"]:
        return False
    claim_keys = [key for key in ("company", "project", "task") if bind.get(key) not in (None, "*")]
    if not claim_keys:
        return True
    if scope.claims:
        if any(
            all(getattr(claim, key) == bind[key] for key in claim_keys) for claim in scope.claims
        ):
            return True
        if not unclaimed_matches:
            return False

        def _possibly_in_scope(claim: ClaimBind) -> bool:
            for key in claim_keys:
                value = str(getattr(claim, key) or "")
                if value and value != bind[key]:
                    return False
            return True

        return any(_possibly_in_scope(claim) for claim in scope.claims)
    claimed = {"company": scope.companies, "project": scope.projects, "task": scope.tasks}
    for key in claim_keys:
        values = claimed[key]
        if bind[key] in values:
            continue
        if unclaimed_matches and not values:
            continue
        return False
    return True


def resolve_claims(agent: str) -> CallScope:
    """One agent's live claim scope: the 'doing' tasks it holds.

    The same query ``resolve_scope`` folds into the caller's scope — factored
    so the allowance view can scope a subject that is not the caller (task
    #2144). Raises ``sqlite3.Error`` on a store failure; callers convert it
    the same way ``resolve_scope`` does.
    """
    conn = storage.connect()
    try:
        return resolve_claims_on_connection(conn, agent)
    finally:
        conn.close()


def resolve_claims_on_connection(conn: Any, agent: str) -> CallScope:
    """Resolve the same claim scope inside an existing owner transaction.

    Read-only port: never opens a second connection, records a denial, or
    commits. Lets trusted composition fence policy context under its lock.
    """
    companies: set[str] = set()
    projects: set[str] = set()
    tasks: set[str] = set()
    claims: set[ClaimBind] = set()
    rows = conn.execute(
        "SELECT id, project, org_id FROM tasks "
        "WHERE claimed_by = ? AND status = 'doing' "
        "AND (lock_expires_at IS NULL OR lock_expires_at > ?)",
        (agent, storage.now()),
    ).fetchall()
    for row in rows:
        task = str(row["id"])
        tasks.add(task)
        project = row["project"]
        project_id = project.strip() if isinstance(project, str) and project.strip() else ""
        if project_id:
            projects.add(project_id)
        org = row["org_id"]
        org_id = org.strip() if isinstance(org, str) and org.strip() else ""
        if org_id:
            companies.add(org_id)
        claims.add(ClaimBind(company=org_id, project=project_id, task=task))
    return CallScope(
        agent=agent,
        companies=frozenset(companies),
        projects=frozenset(projects),
        tasks=frozenset(tasks),
        claims=frozenset(claims),
    )


def resolve_scope(originator: str | None, *, need_claims: bool) -> CallScope:
    """Build the call scope from the verified identity and, if needed, claims.

    An adapter-supplied ``originator`` (a GitHub login, a web actor) is not an
    agent identity. Without a verified identity it is namespaced so it can
    neither match an ``agent`` bind nor borrow a real agent's claims. An MCP
    session outranks it, so rotating originator labels cannot split one
    session's budget. Only a caller with none of the three (stdio, one client
    per process) uses the empty key.
    """
    agent = (config.verified_agent_identity() or "").strip()
    if not agent:
        session = config.current_mcp_session_id()
        if session:
            agent = f"session:{session}"
    if not agent and originator and originator.strip():
        agent = f"unverified:{originator.strip()}"
    try:
        from . import profiles

        profile = profiles.current()
    except Exception:
        profile = ""
    companies: set[str] = set()
    projects: set[str] = set()
    tasks: set[str] = set()
    claims: set[ClaimBind] = set()
    if need_claims and agent:
        try:
            claim_scope = resolve_claims(agent)
        except sqlite3.Error as exc:
            record_denial(
                tool_name="",
                reason_code="policy_unavailable",
                rule="",
                caller=agent,
                profile=profile,
                episode_id=config.current_episode_id(),
            )
            from .policy import PolicyViolation

            raise PolicyViolation(
                f"Database error while resolving gateway bindings: {exc}",
                reason_code="policy_unavailable",
            ) from exc
        companies = set(claim_scope.companies)
        projects = set(claim_scope.projects)
        tasks = set(claim_scope.tasks)
        claims = set(claim_scope.claims)
    return CallScope(
        agent=agent,
        profile=profile,
        companies=frozenset(companies),
        projects=frozenset(projects),
        tasks=frozenset(tasks),
        claims=frozenset(claims),
    )


#: Non-secret fields of a session credential's auth metadata that every
#: gateway decision carries (task #1744). The credential value and its hash are
#: never among them; ``credential_id`` is the only handle.
SESSION_BLOCK_FIELDS = (
    "credential_id",
    "item_id",
    "attempt",
    "node",
    "task_id",
    "run_id",
    "acp_session_id",
    "device_agent",
    "device_check",
)


def session_episode_id(item_id: str) -> str:
    """The ``event_log.episode_id`` of the run that executes dispatch ``item_id``."""
    return f"{SESSION_EPISODE_PREFIX}{item_id}"


def run_episode_id(run_id: str) -> str:
    """The ``event_log.episode_id`` of self-pickup run ``run_id`` (task #2313)."""
    return f"{RUN_EPISODE_PREFIX}{run_id}"


#: A self-pickup run id (``run_records.RUN_ID_PREFIX``); kept here so the
#: request path does not import the run recorder.
_SELF_PICKUP_RUN_PREFIX = "sp-"
#: ``run_records.CHANNEL`` / ``KIND_CLAIMED`` (a test pins them equal).
SELF_PICKUP_CHANNEL = "agent.run.selfpickup"
SELF_PICKUP_CLAIMED = "selfpickup.claimed"


def bearer_episode_id(agent: str | None, *, now: str | None = None) -> str | None:
    """The episode for an interactive request by ``agent``, or None (task #2313).

    ``run:<current_run_id>`` when ``agent`` holds exactly one live ``doing``
    claim (``claimed_by`` is the agent and the lease has not lapsed) and that
    task's open self-pickup run is the agent's own. Zero claims, two or more,
    a lapsed lease or a claim with no open run of its own all mean no episode:
    the request stays unbound and ``evidence_report`` says so, rather than
    guessing which run a call belongs to.

    One indexed read on ``tasks`` (``idx_tasks_status_priority`` narrows it to
    the ``doing`` rows) and no write. It runs on every interactive MCP request,
    so any failure is logged and read as no episode; it never raises.
    """
    name = agent.strip() if isinstance(agent, str) else ""
    if not name:
        return None
    try:
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT current_run_id, current_run_agent FROM tasks "
                "WHERE status='doing' AND claimed_by=? "
                "AND (lock_expires_at IS NULL OR lock_expires_at > ?) LIMIT 2",
                (name, now or storage.now()),
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - attribution is best-effort; the call proceeds
        logger.debug("bearer episode lookup failed; the request is unbound", exc_info=True)
        return None
    if len(rows) != 1:
        return None
    run_id = rows[0]["current_run_id"]
    if (
        not isinstance(run_id, str)
        or not run_id.startswith(_SELF_PICKUP_RUN_PREFIX)
        or rows[0]["current_run_agent"] != name
    ):
        return None
    return run_episode_id(run_id)


def is_run_episode(episode_id: str | None) -> bool:
    """Whether ``episode_id`` is a self-pickup run episode (``run:<id>``)."""
    return isinstance(episode_id, str) and episode_id.startswith(RUN_EPISODE_PREFIX)


def session_block() -> dict[str, Any] | None:
    """The ``session`` block for a decision made with a session credential.

    Present only while the hub middleware has bound a session grant, so
    auth metadata that merely claims ``acp_session`` adds nothing.
    """
    from . import profiles

    if profiles.current_session_grant() is None:
        return None
    meta = config.current_auth_metadata() or {}
    if meta.get("auth_method") != "acp_session":
        return None
    return {key: meta.get(key) for key in SESSION_BLOCK_FIELDS}


def record_denial(
    *,
    tool_name: str,
    reason_code: str,
    rule: str,
    caller: str,
    profile: str,
    episode_id: str | None,
) -> None:
    """Append a deny to the evidence ledger.

    The payload uses ``tool_name``, not ``tool``: episode counters match
    ``$.tool`` and must not treat a deny as another call. A ledger failure
    is logged and swallowed here so the caller can still raise the deny.

    The row's ``episode_id`` and ``causal_parent_id`` columns bind the deny
    to the run that caused it (task #2309): ``episode_id`` falls back to the
    request's bound episode (``acp:<item>`` for a session credential), so a
    caller passing None never drops the binding. A deny of a registered call
    also carries ``args_sha256``, the redacted digest of its supplied
    arguments; arguments themselves are never written.
    """
    payload = {
        "decision": "deny",
        "reason_code": reason_code,
        "rule": rule,
        "tool_name": tool_name,
        "caller": caller,
        "profile": profile,
    }
    state = _CURRENT.get()
    if state is not None and state.tool == tool_name and state.registered_arguments is not None:
        try:
            payload["args_sha256"] = arguments_sha256(state.registered_arguments)
        except Exception:
            logger.warning("gateway deny argument digest was not computed", exc_info=True)
    session = session_block()
    if session is not None:
        payload["session"] = session
    try:
        events.record_event(
            channel=CHANNEL_GATEWAY_DECISION,
            payload=payload,
            originator=caller or None,
            episode_id=episode_id if episode_id is not None else config.current_episode_id(),
            causal_parent_id=config.current_causal_parent_id(),
            notify=False,
        )
        if state is not None and state.tool == tool_name:
            state.decision_recorded = True
    except Exception:
        logger.warning("gateway deny evidence was not recorded", exc_info=True)


#: Byte cap on the readable ``args_summary`` of a gateway event (task #1907).
ARGS_SUMMARY_MAX_BYTES = 2048
_SUMMARY_STRING_MAX = 256
_SUMMARY_LIST_MAX = 32

#: Mutating tools whose decision event carries a readable ``args_summary``
#: (task #1907): ``(readable keys, hashed keys)``. A readable key is copied,
#: redacted and capped; a hashed key becomes ``<key>_sha256`` and
#: ``<key>_bytes``, so file, note and message bodies never enter the ledger.
#: A ``parent.child`` key reads one field of a dict argument, so a structured
#: argument can keep its bounded fields readable and hash only its free text;
#: it appears whenever the parent is a dict (None when the field is absent).
#: ``run_cmd`` is summarised separately as an argv list. Only supplied
#: arguments appear; defaults the caller did not send are left out.
_ARGS_SUMMARY_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "fs_write": (("path", "append", "expected_content_hash"), ("content",)),
    "vault_write": (
        ("path", "mode", "append", "force", "template", "expected_revision"),
        ("content",),
    ),
    "vault_journal": (("agent", "date", "tags"), ("entry",)),
    "obsidian_append": (("path", "target_type", "target"), ("content",)),
    "memory_save": (
        ("id", "title", "tags", "author", "append", "overwrite", "check_only", "supersedes"),
        ("content",),
    ),
    "memory_delete": (("id",), ()),
    "git_commit_staged": (("message", "repo_path"), ()),
    "git_worktree_add": (("path", "branch", "repo_path"), ()),
    "git_worktree_remove": (("path", "force", "repo_path"), ()),
    "git_merge_task": (("task_id", "force", "dry_run", "allow_in_flight"), ()),
    "delegate": (
        ("from_agent", "to_agent", "priority", "parent_task_id", "project", "deadline"),
        ("task_spec",),
    ),
    "task_add": (("title", "priority", "parent_id", "project", "lane", "domain"), ("notes",)),
    "task_update": (
        (
            "id",
            "status",
            "title",
            "priority",
            "parent_id",
            "project",
            "lane",
            "archived",
            "release_claim",
            "request_cancel",
            "monitor_at",
            "clear_monitor",
            "declared_wait.until",
            "next_owner",
        ),
        (
            "notes",
            "append_note",
            "monitor_note",
            "declared_wait.reason",
            "declared_wait.next_step",
            "waiting_on",
            "terminal_predicate",
        ),
    ),
    "task_claim": (("id", "agent", "expected_project", "expected_lane"), ()),
    "task_delete": (("id",), ()),
    "message_send": (
        (
            "act",
            "to_agent",
            "from_agent",
            "task_id",
            "to_session",
            "from_session",
            "reply_to",
            "conversation_id",
        ),
        ("body",),
    ),
}


class LedgerWriteError(ToolError):
    """A mutating call's decision event could not be written (task #1907)."""

    def __init__(self, *args: Any, rolled_back: bool = False) -> None:
        super().__init__(*args)
        self.rolled_back = rolled_back


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _redacted_for_digest(value: Any, key: str | None = None) -> Any:
    """The arguments as ``args_sha256`` sees them (task #1922).

    The same redaction as ``args_summary`` (session credentials and the
    telemetry secret patterns), plus values under secret-looking keys
    (``token``, ``password``, ``api_key``...). The digest therefore cannot
    confirm a guessed secret: two calls that differ only in a redacted value
    hash alike.
    """
    from .telemetry.redaction import _key_looks_sensitive

    if key is not None and _key_looks_sensitive(key):
        return "[REDACTED_SECRET]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if key == "command":
            return _redacted_command(value)[0]
        return _redact(value, max_len=len(str(value)) * 32 + 32)

    if isinstance(value, dict):
        return {str(k): _redacted_for_digest(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redacted_for_digest(item) for item in value]
    return _redact(_canonical_json(value), max_len=len(str(value)) * 32 + 32)


def arguments_sha256(arguments: dict[str, Any]) -> str:
    """sha256 of the canonical JSON of a call's supplied arguments, redacted.

    Hashing the raw arguments let anyone holding the ledger confirm a guessed
    low-entropy secret offline (#1907 review). The redacted form keeps the
    digest useful for "were these the same call" while a secret in it no
    longer changes the hash. An HMAC was not used: the only hub-local evidence
    key is the WorkOS leg's secret (``audit/producer_sign.py``), which is not
    always configured and would tie ledger verification to it.
    """
    redacted = _redacted_for_digest(arguments)
    return hashlib.sha256(_canonical_json(redacted).encode("utf-8")).hexdigest()


def _redact(text: str, max_len: int = _SUMMARY_STRING_MAX) -> str:
    import re

    from .telemetry.redaction import redact_sensitive_content, redact_session_credentials

    text, _count = redact_session_credentials(str(text))
    # Credential context also protects short, guessable values.
    credential_value = r"(?:\"[^\"]*\"|'[^']*'|[^\s'\"]+)"
    text = re.sub(r"(?i)\bBearer\s+" + credential_value, "Bearer [REDACTED_SECRET]", text)
    text = re.sub(
        r"(?i)\b(?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*" + credential_value,
        "[REDACTED_SECRET]",
        text,
    )
    return redact_sensitive_content(text, max_len=max_len)


def _redacted_command(command: str) -> tuple[str, list[str]]:
    """Redact credentials before and after the command's argv normalization.

    Quotes and escapes may split credential context in the source spelling
    (``'Bearer' value``), then disappear during shlex parsing. Keep context
    across normalized argv entries before either summary or digest persists.
    """
    import re
    import shlex

    redacted = _redact(command, max_len=len(command) * 32 + 32)
    try:
        argv = shlex.split(redacted)
    except ValueError:
        # A refused, unparseable command has no reliable argv boundaries.
        return "[UNPARSEABLE_COMMAND]", ["[UNPARSEABLE_COMMAND]"]
    credential_key = r"(?:password|passwd|token|secret|api[_-]?key)"
    # Match the raw redactor's word boundary, including --password and
    # cfg.password, while requiring the credential context at the argv end.
    bare_key = re.compile(r"(?i)\b" + credential_key + r"\s*$")
    assigned_key = re.compile(r"(?i)\b" + credential_key + r"\s*[:=]\s*$")
    safe_argv = []
    for index, arg in enumerate(argv):
        safe = _redact(arg, max_len=len(arg) * 32 + 32)
        previous = argv[index - 1] if index else ""
        assignment_value = bool(
            assigned_key.search(previous)
            or (bare_key.search(previous) and re.match(r"\s*[:=]\s*\S", arg))
            or (
                index >= 2
                and re.fullmatch(r"\s*[:=]\s*", previous)
                and bare_key.search(argv[index - 2])
            )
        )
        if assignment_value or (index and re.search(r"(?i)\bBearer\s*$", previous)):
            safe = "[REDACTED_SECRET]"
        safe_argv.append(safe)
    # Preserve the original redacted spelling unless normalization exposed
    # additional credential context. Secret-only changes then hash alike.
    if safe_argv != argv:
        redacted = shlex.join(safe_argv)
    return redacted, safe_argv


def _summary_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_summary_value(item) for item in list(value)[:_SUMMARY_LIST_MAX]]
    if isinstance(value, str):
        return _redact(value)
    return _redact(_canonical_json(value))


def _content_digest(value: Any) -> tuple[str, int]:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = _canonical_json(value).encode("utf-8")
    redacted = _redacted_for_digest(value)
    digest_raw = (
        redacted.encode("utf-8")
        if isinstance(redacted, str)
        else _canonical_json(redacted).encode("utf-8")
    )
    return hashlib.sha256(digest_raw).hexdigest(), len(raw)


def _summary_size(summary: dict[str, Any]) -> int:
    return len(json.dumps(summary, ensure_ascii=False).encode("utf-8"))


def _cap_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Shrink a summary to ``ARGS_SUMMARY_MAX_BYTES``, marking it truncated."""
    if _summary_size(summary) <= ARGS_SUMMARY_MAX_BYTES:
        return summary
    summary = {**summary, "truncated": True}
    for key, value in list(summary.items()):
        while isinstance(value, list) and value and _summary_size(summary) > ARGS_SUMMARY_MAX_BYTES:
            value = value[: len(value) // 2]
            summary[key] = value
    if _summary_size(summary) > ARGS_SUMMARY_MAX_BYTES:
        for key, value in list(summary.items()):
            if isinstance(value, str) and len(value) > 64:
                summary[key] = value[:61] + "..."
    if _summary_size(summary) > ARGS_SUMMARY_MAX_BYTES:
        return {"truncated": True}
    return summary


def _argument(arguments: dict[str, Any], key: str) -> tuple[bool, Any]:
    """``(supplied, value)`` for a top-level key or a ``parent.child`` key."""
    parent, dot, child = key.partition(".")
    if not dot:
        return key in arguments, arguments.get(key)
    value = arguments.get(parent)
    if not isinstance(value, dict):
        return False, None
    return True, value.get(child)


def arguments_summary(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Readable, redacted, bounded digest of a curated mutating call, else None."""
    if tool_name == "run_cmd":
        command = arguments.get("command")
        command = "" if command is None else str(command)
        _redacted, argv = _redacted_command(command)
        summary: dict[str, Any] = {"argv": [_redact(a) for a in argv[:_SUMMARY_LIST_MAX]]}
        summary["argc"] = len(argv)
        if len(argv) > _SUMMARY_LIST_MAX or any(len(a) > _SUMMARY_STRING_MAX for a in argv):
            summary["truncated"] = True
        for key in ("cwd", "timeout_s"):
            if key in arguments:
                summary[key] = _summary_value(arguments[key])
        return _cap_summary(summary)
    fields = _ARGS_SUMMARY_FIELDS.get(tool_name)
    if fields is None:
        return None
    readable, hashed = fields
    supplied = {}
    for key in readable:
        present, value = _argument(arguments, key)
        if present:
            supplied[key] = value
    summary = {key: _summary_value(value) for key, value in supplied.items()}
    for key in hashed:
        value = _argument(arguments, key)[1]
        if value is None:
            continue
        digest, size = _content_digest(value)
        summary[f"{key}_sha256"] = digest
        summary[f"{key}_bytes"] = size
    if any(_summary_was_shortened(value) for value in supplied.values()):
        summary["truncated"] = True
    return _cap_summary(summary)


#: Flush every pending rollup once this many keys are held (task #1922).
COALESCE_MAX_KEYS = 512
#: Distinct ``args_sha256`` values tracked per rollup, and how many are listed.
ROLLUP_DISTINCT_MAX = 1024
ROLLUP_ARGS_SAMPLE_MAX = 16

#: caller, tool_name, profile, decision, episode_id. The episode is None or a
#: self-pickup run episode (``run:<id>``, task #2313): a run's read-only allows
#: still coalesce, and the rollup carries the run's episode so the run is bound.
_RollupKey = tuple[str, str, str, str, str | None]


def _coalesce_clock() -> float:
    return time.time()


def _iso(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


@dataclass
class _Rollup:
    count: int
    first_at: float
    last_at: float
    digests: set[str] = field(default_factory=set)
    sample: list[str] = field(default_factory=list)
    capped: bool = False

    def add(self, now: float, digest: str | None) -> None:
        self.count += 1
        self.first_at = min(self.first_at, now)
        self.last_at = max(self.last_at, now)
        if digest is None or digest in self.digests:
            return
        if len(self.digests) >= ROLLUP_DISTINCT_MAX:
            self.capped = True
            return
        self.digests.add(digest)
        if len(self.sample) < ROLLUP_ARGS_SAMPLE_MAX:
            self.sample.append(digest)


class _ReadCoalescer:
    """Per-process counts of read-only ALLOW decisions (task #1922).

    Every pending entry belongs to one bucket, the latest one a call fell in.
    A call in a later bucket (or a changed bucket width) writes all of them
    first, so at any moment at most one bucket of counts is held in memory.
    A deadline timer drains even an idle bucket when its window ends;
    ``flush`` also runs at hub shutdown and interpreter exit. Writes happen outside the lock and are best-effort, like
    the per-call write of a read-only call: a failed write is logged and its
    counts are dropped rather than retried forever.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bucket: tuple[int, int] | None = None  # (start, width) in seconds
        self._pending: dict[_RollupKey, _Rollup] = {}
        self._timer: threading.Timer | None = None
        self._generation = 0

    def add(self, key: _RollupKey, digest: str | None, now: float, width: int) -> None:
        start = int(now // width) * width
        drained: list[tuple[_RollupKey, _Rollup, tuple[int, int]]] = []
        with self._lock:
            if self._pending and self._bucket != (start, width):
                drained = self._drain_locked()
            self._bucket = (start, width)
            entry = self._pending.get(key)
            if entry is None:
                entry = _Rollup(count=0, first_at=now, last_at=now)
                self._pending[key] = entry
            entry.add(now, digest)
            if len(self._pending) >= COALESCE_MAX_KEYS:
                drained += self._drain_locked()
            if self._pending and self._timer is None:
                generation = self._generation
                self._timer = threading.Timer(
                    max(0.001, start + width - now), self._deadline_flush, args=(generation,)
                )
                self._timer.daemon = True
                self._timer.start()
        _write_rollups(drained)

    def _deadline_flush(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            drained = self._drain_locked()
        _write_rollups(drained)

    def flush(self) -> int:
        with self._lock:
            drained = self._drain_locked()
        return _write_rollups(drained)

    def reset(self) -> None:
        """Drop pending counts without writing them. Tests only."""
        with self._lock:
            self._drain_locked()
            self._bucket = None

    def pending(self) -> int:
        with self._lock:
            return sum(entry.count for entry in self._pending.values())

    def counts(self, bucket_floor: float | None = None) -> dict[tuple[str, str], int]:
        """Pending allowed-call counts per (caller, tool); not yet on the ledger.

        Every pending entry belongs to ``self._bucket``; with ``bucket_floor``
        (epoch seconds) an empty map comes back when that bucket began
        earlier, so a "calls today" reader never folds in a prior day's
        bucket (task #2144).
        """
        with self._lock:
            if bucket_floor is not None and (
                self._bucket is None or self._bucket[0] < bucket_floor
            ):
                return {}
            out: dict[tuple[str, str], int] = {}
            for (caller, tool, _profile, decision, _episode), entry in self._pending.items():
                if decision == "allow":
                    out[(caller, tool)] = out.get((caller, tool), 0) + entry.count
            return out

    def episode_calls(self, episode_id: str, tool_name: str) -> int:
        """Pending allowed calls of ``tool_name`` inside ``episode_id``, not yet written."""
        with self._lock:
            return sum(
                entry.count
                for (_caller, tool, _profile, decision, episode), entry in self._pending.items()
                if episode == episode_id and tool == tool_name and decision == "allow"
            )

    def _drain_locked(self) -> list[tuple[_RollupKey, _Rollup, tuple[int, int]]]:
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._pending or self._bucket is None:
            self._pending.clear()
            return []
        bucket = self._bucket
        drained = [(key, entry, bucket) for key, entry in self._pending.items()]
        self._pending = {}
        return drained


def _write_rollups(drained: list[tuple[_RollupKey, _Rollup, tuple[int, int]]]) -> int:
    # A drain can run inside another caller's request; its bound episode must
    # never stamp these rollups, so each carries only its own key's episode.
    unbound = config.episode_id_var.set(None)
    try:
        return _write_rollup_events(drained)
    finally:
        config.episode_id_var.reset(unbound)


def _write_rollup_events(drained: list[tuple[_RollupKey, _Rollup, tuple[int, int]]]) -> int:
    written = 0
    for (caller, tool_name, profile, decision, episode_id), entry, (start, width) in drained:
        payload: dict[str, Any] = {
            "decision": decision,
            "reason_code": "allowed",
            "rule": "",
            "tool_name": tool_name,
            "caller": caller,
            "profile": profile,
            "count": entry.count,
            "bucket": _iso(start),
            "bucket_s": width,
            "first_at": _iso(entry.first_at),
            "last_at": _iso(entry.last_at),
            "distinct_args": len(entry.digests),
            "args_sha256": list(entry.sample),
        }
        if entry.capped:
            payload["distinct_args_capped"] = True
        try:
            events.record_event(
                channel=CHANNEL_GATEWAY_DECISION_ROLLUP,
                payload=payload,
                originator=caller or None,
                episode_id=episode_id,
                notify=False,
            )
            written += 1
        except Exception:
            logger.warning(
                "gateway read rollup for %s (%d calls) was not recorded",
                tool_name,
                entry.count,
                exc_info=True,
            )
    return written


_COALESCER = _ReadCoalescer()


def flush_coalesced() -> int:
    """Write every pending read rollup now; returns how many events were written."""
    try:
        return _COALESCER.flush()
    except Exception:
        logger.warning("gateway read rollups were not flushed", exc_info=True)
        return 0


def reset_coalesced() -> None:
    """Drop pending read counts unwritten. Tests use this between databases."""
    _COALESCER.reset()


def pending_coalesced() -> int:
    """Calls counted in memory and not yet written."""
    return _COALESCER.pending()


def pending_call_counts(bucket_floor: float | None = None) -> dict[tuple[str, str], int]:
    """(caller, tool) -> allowed calls counted but not yet rolled up (task #2144).

    ``bucket_floor`` (epoch seconds) bounds the shared coalescer bucket by
    its start, the same day-boundary rule rollup rows get from their
    ``bucket`` stamp: counts whose bucket began before it are dropped.
    """
    return _COALESCER.counts(bucket_floor)


def episode_call_count(conn: sqlite3.Connection, episode_id: str, tool_name: str) -> int:
    """Calls of ``tool_name`` that ran inside ``episode_id``, as the gateway saw them.

    Per-call decisions that ran (``allow`` or ``error``; a deny or a parked
    approval did not run), plus the counts of written rollups, plus the
    calls coalesced in memory and not yet rolled up (review of #2315/#2313:
    an episode quota must count a bearer run's coalesced reads).
    """
    per_call = conn.execute(
        "SELECT COUNT(*) FROM event_log WHERE channel = ? AND episode_id = ? "
        "AND json_extract(payload, '$.tool_name') = ? "
        "AND json_extract(payload, '$.decision') IN ('allow', 'error')",
        (CHANNEL_GATEWAY_DECISION, episode_id, tool_name),
    ).fetchone()[0]
    rolled = conn.execute(
        "SELECT COALESCE(SUM(json_extract(payload, '$.count')), 0) FROM event_log "
        "WHERE channel = ? AND episode_id = ? AND json_extract(payload, '$.tool_name') = ? "
        "AND json_extract(payload, '$.decision') = 'allow'",
        (CHANNEL_GATEWAY_DECISION_ROLLUP, episode_id, tool_name),
    ).fetchone()[0]
    return int(per_call or 0) + int(rolled or 0) + _COALESCER.episode_calls(episode_id, tool_name)


def run_episode_exists(conn: sqlite3.Connection, episode_id: str) -> bool:
    """Whether ``run:<id>`` names a self-pickup run the hub opened (review of #2313).

    Authoritative: the run's ``selfpickup.claimed`` event, never an event the
    caller's own calls (or denials) wrote under the episode.
    """
    if not is_run_episode(episode_id):
        return False
    run_id = episode_id[len(RUN_EPISODE_PREFIX) :]
    if not run_id.startswith(_SELF_PICKUP_RUN_PREFIX):
        return False
    row = conn.execute("SELECT 1 FROM tasks WHERE current_run_id = ? LIMIT 1", (run_id,)).fetchone()
    if row is not None:
        return True
    return (
        conn.execute(
            "SELECT 1 FROM event_log WHERE channel = ? "
            "AND json_extract(payload, '$.kind') = ? "
            "AND json_extract(payload, '$.run_id') = ? LIMIT 1",
            (SELF_PICKUP_CHANNEL, SELF_PICKUP_CLAIMED, run_id),
        ).fetchone()
        is not None
    )


def _register_exit_flush() -> bool:
    import atexit

    atexit.register(flush_coalesced)
    return True


_EXIT_FLUSH_REGISTERED = _register_exit_flush()


def decision_call_counts(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], int]:
    """Calls per (caller, tool_name, decision) across per-call and rollup events.

    ``rows`` are ``events.query_events`` results. A per-call
    ``agent.gateway.decision`` row is one call; an
    ``agent.gateway.decision.rollup`` row is ``count`` calls. Anything that
    counts tool calls from the ledger must sum this way (task #1922).
    """
    counts: dict[tuple[str, str, str], int] = {}
    for row in rows:
        channel = row.get("channel")
        if channel not in (CHANNEL_GATEWAY_DECISION, CHANNEL_GATEWAY_DECISION_ROLLUP):
            continue
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        n = 1
        if channel == CHANNEL_GATEWAY_DECISION_ROLLUP:
            raw = payload.get("count")
            n = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0
        key = (
            str(payload.get("caller") or ""),
            str(payload.get("tool_name") or ""),
            str(payload.get("decision") or ""),
        )
        counts[key] = counts.get(key, 0) + n
    return counts


def _summary_was_shortened(value: Any) -> bool:
    if isinstance(value, (list, tuple)):
        return len(value) > _SUMMARY_LIST_MAX or any(_summary_was_shortened(v) for v in value)
    if value is None or isinstance(value, (bool, int, float)):
        return False
    text = value if isinstance(value, str) else _canonical_json(value)
    return len(_redact(text, max_len=len(text) * 32 + 32)) > _SUMMARY_STRING_MAX


def _call_result_payload(
    tool_name: str,
    error: BaseException | None,
    result: Any,
    *,
    arguments: dict[str, Any] | None,
    mutating: bool,
) -> tuple[dict[str, Any], str, str | None]:
    """One canonical outcome shape shared by ordinary and transactional evidence."""
    from . import profiles
    from .tools.approvals import ApprovalPendingError

    pending = isinstance(error, ApprovalPendingError) or (
        error is None and isinstance(result, dict) and result.get("status") == "pending-approval"
    )
    denied_approval = error is not None and _is_denied_approval(error)
    reason_code = getattr(error, "reason_code", None) if error is not None else None
    if not isinstance(reason_code, str) or not reason_code:
        reason_code = (
            "allowed"
            if error is None and not pending
            else "approval_pending"
            if pending
            else "approval_denied"
            if denied_approval
            else "tool_error"
        )
    decision = (
        "allow"
        if error is None and not pending
        else "pending"
        if pending
        else "deny"
        if denied_approval or isinstance(getattr(error, "reason_code", None), str)
        else "error"
    )
    caller = config.verified_agent_identity() or ""
    profile = profiles.current()
    payload = {
        "decision": decision,
        "reason_code": reason_code,
        "rule": "",
        "tool_name": tool_name,
        "caller": caller,
        "profile": profile,
    }
    digest = arguments_sha256(arguments) if arguments is not None else None
    if digest is not None:
        payload["args_sha256"] = digest
        # Read-only polling is >90% of this channel (hub, 2026-10: ~80k
        # rows/day), so only a mutating call pays for a readable summary.
        summary = arguments_summary(tool_name, arguments) if mutating else None
        if summary is not None:
            payload["args_summary"] = summary
    session = session_block()
    if session is not None:
        payload["session"] = session
    return payload, caller, config.current_episode_id()


def record_call_result(
    tool_name: str,
    invocation: _Invocation,
    error: BaseException | None,
    result: Any = None,
    *,
    arguments: dict[str, Any] | None = None,
    mutating: bool = False,
    coalesce: bool = False,
) -> None:
    """Record one decision for a registered call not already logged as a deny.

    The event is written after the tool body returns or raises, so it carries
    the outcome. With ``arguments`` it also carries ``args_sha256`` (canonical
    JSON of the supplied arguments) and, for a ``mutating`` call to ``run_cmd``
    or a curated tool in ``_ARGS_SUMMARY_FIELDS``, a redacted, size-capped
    ``args_summary`` (task #1907). Return values and exception text are never
    recorded, and content arguments appear only as a sha256 and byte length.

    A read-only call's evidence write is best-effort. A ``mutating`` call fails
    closed: a failed write raises ``LedgerWriteError``, which the registered
    wrapper turns into a ToolError. The tool body has already run by then, so
    the message says the effect happened but is not on the ledger.

    With ``coalesce`` (the registered wrapper passes it for read-only calls),
    an ALLOW with no session credential is counted in memory and written
    later as an ``agent.gateway.decision.rollup`` event (task #1922,
    ``UA_GATEWAY_COALESCE_READS``) when it has no episode or a self-pickup
    run episode (``run:<id>``, task #2313); that rollup carries the episode.
    A session-credential (``acp:``) episode, every other decision, and every
    call without ``coalesce`` is still one event per call.
    """
    if invocation.decision_recorded:
        return
    try:
        payload, caller, episode_id = _call_result_payload(
            tool_name, error, result, arguments=arguments, mutating=mutating
        )
        decision = payload["decision"]
        profile = payload["profile"]
        digest = payload.get("args_sha256")
        session = payload.get("session")
        if (
            coalesce
            and decision == "allow"
            and session is None
            and (episode_id is None or is_run_episode(episode_id))
            and config.gateway_coalesce_reads()
        ):
            # A session credential attributes the call to one dispatch, so it
            # stays per call. An interactive run's reads coalesce per run: the
            # rollup carries the run episode (task #2313), so a bearer run
            # adds no per-call write. See _ReadCoalescer for the loss bound.
            _COALESCER.add(
                (caller, tool_name, profile, decision, episode_id),
                digest,
                _coalesce_clock(),
                config.gateway_coalesce_bucket_s(),
            )
            invocation.decision_recorded = True
            return
        events.record_event(
            channel=CHANNEL_GATEWAY_DECISION,
            payload=payload,
            originator=caller or None,
            episode_id=episode_id,
            notify=False,
        )
        _note_decision_commit()
        invocation.decision_recorded = True
    except Exception as exc:
        kind = _note_decision_failure(exc)
        if mutating:
            logger.error(
                "gateway evidence for mutating call %s was not recorded (%s)", tool_name, kind
            )
            if isinstance(error, LedgerWriteError) and error.rolled_back:
                raise error from exc
            raise LedgerWriteError(
                f"{tool_name} {'returned' if error is None else 'raised an error'}, but its decision event could not be written to the "
                f"evidence ledger ({kind}). Effects may have occurred and are not audited. "
                "Do not retry automatically; run system_doctor to check the hub database and verify effects first."
            ) from exc
        logger.warning("gateway call evidence was not recorded", exc_info=True)


def record_call_result_on_connection(
    conn: sqlite3.Connection,
    tool_name: str,
    error: BaseException | None,
    result: Any = None,
    *,
    arguments: dict[str, Any] | None,
    dedupe_key: str,
) -> str:
    """Prepare canonical mutating-call evidence in the caller's write transaction.

    The caller must hold SQLite's write lock (BEGIN IMMEDIATE or an earlier
    write) and owns commit/rollback. This port never coalesces, commits, publishes
    or acknowledges the invocation. The returned ULID is not proof of a commit;
    selected tool/wrapper binding must acknowledge only its committed outcome.
    Dedupe keys belong to that binding, independently of the redacted argument
    digest; a key that names a different canonical event is rejected.
    """
    try:
        from . import edition

        call = _CURRENT.get()
        if not conn.in_transaction:
            raise ValueError("active caller transaction required")
        if call is None or call.tool != tool_name or call.decision_recorded:
            raise ValueError("matching unacknowledged invocation required")
        if not isinstance(dedupe_key, str) or not dedupe_key.strip():
            raise ValueError("non-empty evidence key required")
        dedupe_key = dedupe_key.strip()
        payload, caller, episode_id = _call_result_payload(
            tool_name, error, result, arguments=arguments, mutating=True
        )
        originator = caller or None
        causal_parent_id = config.current_causal_parent_id()
        org_id = edition.tenant_org_id()
        existing = conn.execute(
            "SELECT ulid, channel, payload, originator, episode_id, causal_parent_id, org_id "
            "FROM event_log WHERE dedupe_key=?",
            (dedupe_key,),
        ).fetchone()
        if existing is not None:
            if (
                existing[1] != CHANNEL_GATEWAY_DECISION
                or json.loads(existing[2]) != payload
                or tuple(existing)[3:] != (originator, episode_id, causal_parent_id, org_id)
            ):
                raise ValueError("evidence key names a different canonical outcome")
            return existing[0]
        identifier, inserted, _ = events.record_event_once_on_connection(
            conn,
            CHANNEL_GATEWAY_DECISION,
            payload,
            originator=originator,
            episode_id=episode_id,
            causal_parent_id=causal_parent_id,
            dedupe_key=dedupe_key,
        )
        if not inserted:
            raise ValueError("evidence key changed during caller transaction")
        return identifier
    except Exception as exc:
        kind = _note_decision_failure(exc)
        raise LedgerWriteError(
            f"{tool_name} transactional decision evidence could not be prepared "
            f"({kind}). Roll back the caller transaction; do not commit its effects.",
            rolled_back=True,
        ) from exc


def acknowledge_committed_call(tool_name: str) -> None:
    """Trusted native composition has verified its required committed proof.

    Like commit_registered_call_on_connection's acknowledgment, this suppresses
    a second mandatory post-body append. Call only after reading the actual
    committed owner receipt and canonical gateway event; never for an intent
    that failed to commit or for a caller-supplied proof.
    """
    call = _CURRENT.get()
    if call is None or call.tool != tool_name or call.registered_arguments is None:
        raise ValueError("matching registered invocation required")
    call.decision_recorded = True


def commit_registered_call_on_connection(
    conn: sqlite3.Connection,
    tool_name: str,
    result: dict[str, Any],
    *,
    replay: bool = False,
    prepare: Callable[[Any], None] | None = None,
) -> None:
    """Prepare required native state/evidence on the supplied invocation owner.

    Only trusted selected-service composition supplies the owner/preparation
    ports. The gateway acknowledges after its successful commit, without another
    evidence write. Bare native calls retain their existing connection owner.
    """
    call = _CURRENT.get()
    registered = (
        call is not None and call.tool == tool_name and call.registered_arguments is not None
    )

    def prepare_required(transaction) -> None:
        if prepare is not None:
            prepare(transaction)
        if registered and not replay:
            record_call_result_on_connection(
                transaction,
                tool_name,
                None,
                result,
                arguments=call.registered_arguments,
                dedupe_key=f"{tool_name}:gateway:{uuid.uuid4().hex}",
            )

    if registered and call.transaction_owner is not None:
        call.transaction_owner(conn, prepare_required)
    else:
        prepare_required(conn)
        conn.commit()
    if registered:
        call.decision_recorded = True
        if not replay:
            _note_decision_commit()


def _rate_rule_fingerprint(rule: Any) -> str:
    """Stable generation for every field that changes rate-rule matching."""
    data = {
        "name": rule.name,
        "tool": rule.tool,
        "type": rule.type,
        "bind": rule.bind,
        "roles": sorted(rule.roles),
        "max_calls": rule.max_calls,
        "window_seconds": rule.window_seconds,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def reconcile_rate_rules(rules: list[Any]) -> None:
    """Drop old bucket generations when the loaded policy changes."""
    _LIMITS.reconcile(
        frozenset(
            (rule.name, _rate_rule_fingerprint(rule)) for rule in rules if rule.type == "rate_limit"
        )
    )


def reserve_rate_limits(
    rules: list,
    *,
    tool: str,
    scope: CallScope,
    now: float | None,
    episode_id: str | None,
    policy_namespace: str | None = None,
) -> Reservation:
    """Take one slot from every matched rate-limit rule.

    Raises PolicyViolation when a rule is full or the bucket table has no room.
    Keys already charged in this invocation are not charged again.
    """
    moment = time.monotonic() if now is None else float(now)
    state = _CURRENT.get()
    charged = state.charged if state is not None else None
    specs: list[tuple[_Key, int, float]] = []
    owners: list = []
    seen: set[_Key] = set()
    for rule in rules:
        fingerprint = _rate_rule_fingerprint(rule)
        if policy_namespace is not None:
            fingerprint = f"explicit:{policy_namespace}:{fingerprint}"
        key = (rule.name, fingerprint, tool, scope.agent or "")
        if key in seen or (charged is not None and key in charged):
            continue
        seen.add(key)
        specs.append((key, int(rule.max_calls), float(rule.window_seconds)))
        owners.append(rule)
    if not specs:
        return Reservation()
    blocked, capacity = _LIMITS.admit(specs, moment)
    if blocked is None:
        keys = [key for key, _, _ in specs]
        reservation = Reservation(keys=keys, moment=moment, scope=state)
        if state is not None:
            state.charged.update(keys)
            state.reservations.append(reservation)
        return reservation
    rule = owners[blocked]
    reason_code = "rate_limit_capacity" if capacity else "rate_limited"
    record_denial(
        tool_name=tool,
        reason_code=reason_code,
        rule=rule.name,
        caller=scope.agent,
        profile=scope.profile,
        episode_id=episode_id,
    )
    from .policy import PolicyViolation

    if capacity:
        raise PolicyViolation(
            f"Tool '{tool}' refused by policy rule '{rule.name}': the gateway "
            f"rate-limit table is full ({MAX_BUCKETS} live buckets)",
            reason_code=reason_code,
        )
    raise PolicyViolation(
        f"Tool '{tool}' rate limited by policy rule '{rule.name}': "
        f"{int(rule.max_calls)} calls per {float(rule.window_seconds):g}s for this caller",
        reason_code=reason_code,
    )
