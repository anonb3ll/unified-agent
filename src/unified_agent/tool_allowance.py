"""Per-tool call allowance view (task #2144).

The policy gate's ``rate_limit`` rules are enforced against an in-process,
per-caller sliding-window counter in ``gateway``. This module only *reads* the
same state enforcement uses — ``gateway.rate_limit_snapshot`` over the live
bucket table — plus the allowed-call decisions the gateway already records on
the evidence ledger (per-call rows and hourly read rollups, counted the
canonical way by ``gateway.decision_call_counts``, with the coalescer's
not-yet-written counts folded in). It keeps no counter of its own and adds no
table. The view is strictly read-only: it never runs the generation sweep
(``gateway.reconcile_rate_rules``) — that stays with ``evaluate_policy`` — so
a read cannot change what the gate will decide. A superseded bucket still
never reports ``stopped``, because ``stopped`` requires the bucket's
generation to be the loaded rule's.

The view is tenant-fenced: a caller reads the identity its own calls are
charged under. The verified operator (``UA_OPERATOR_LABEL``) may read any
identity, every identity with ``agent="*"``, or the whole fleet by omitting
``agent`` — an operator's own rows are usually empty, so the fleet view is
the useful default for them.
"""

from __future__ import annotations

import fnmatch
import glob
import json
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, gateway, policy, storage


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


#: Decision-ledger reads behind ``used_today``; the composite
#: ``idx_event_log_channel_created`` index keeps both off a full-log walk
#: on a private hub where every ``org_id`` is NULL (task #2144).
_LEDGER_SQL_FLEET = (
    "SELECT channel, payload FROM event_log "
    "WHERE channel IN (?, ?) AND created_at >= ? AND org_id IS ?"
)
_LEDGER_SQL_SUBJECT = _LEDGER_SQL_FLEET + " AND COALESCE(json_extract(payload, '$.caller'), '') = ?"


def _rollup_bucket_start(payload: dict[str, Any]) -> float | None:
    """Epoch of the bucket a rollup counts, from its own ``bucket`` stamp."""
    raw = payload.get("bucket")
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.timestamp()


def _calls_today(
    today: datetime, subject: str | None
) -> tuple[dict[tuple[str, str], int], str | None]:
    """Allowed calls per (caller, tool) since ``today`` (UTC day start).

    Rows are fenced to this hub's tenant (``org_id IS ?``, task #1232).
    Per-call and rollup decision rows are counted by
    ``gateway.decision_call_counts``; read-only calls still pending in the
    coalescer are added on top so the count is live, not an hour behind.
    A failed ledger read is returned as ``ledger_error`` — a silent zero
    would report a busy caller as idle.
    """
    since = today.isoformat(timespec="seconds")
    floor = today.timestamp()
    org = edition.tenant_org_id()
    params: tuple = (
        gateway.CHANNEL_GATEWAY_DECISION,
        gateway.CHANNEL_GATEWAY_DECISION_ROLLUP,
        since,
        org,
    )
    ledger_error: str | None = None
    try:
        conn = storage.connect()
        try:
            if subject is None:
                rows = conn.execute(_LEDGER_SQL_FLEET, params).fetchall()
            else:
                rows = conn.execute(_LEDGER_SQL_SUBJECT, (*params, subject)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        rows = []
        ledger_error = str(exc)[:300]
    parsed = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if row["channel"] == gateway.CHANNEL_GATEWAY_DECISION_ROLLUP:
            # A rollup's calls belong to the day its bucket began, not the
            # day the row flushed: a bucket that ran before UTC midnight but
            # was written after it would otherwise charge yesterday's reads
            # to today. A straddling bucket (possible only when
            # UA_GATEWAY_COALESCE_BUCKET_S does not divide 86400) is
            # attributed whole to its start day — the miscount is bounded by
            # the bucket's own width. A missing or unparseable stamp keeps
            # the write-time fallback.
            start = _rollup_bucket_start(payload)
            if start is not None and start < floor:
                continue
        parsed.append({"channel": row["channel"], "payload": payload})
    out: dict[tuple[str, str], int] = {}
    for (caller, tool, decision), n in gateway.decision_call_counts(parsed).items():
        if decision == "allow" and (subject is None or caller == subject):
            out[(caller, tool)] = out.get((caller, tool), 0) + n
    for (caller, tool), n in gateway.pending_call_counts(floor).items():
        if subject is None or caller == subject:
            out[(caller, tool)] = out.get((caller, tool), 0) + n
    return out, ledger_error


#: Subject prefixes ``resolve_scope`` derives for callers without a verified
#: identity. Enforcement charges buckets under them, but decision rows record
#: the verified caller — ``""`` for every unverified caller — so no
#: per-subject ``used_today`` exists: the row reports ``null`` with a reason,
#: never a false 0 (task #2144 r4).
_UNATTRIBUTED_PREFIXES = ("session:", "unverified:")
_UNATTRIBUTED_TODAY = (
    "unverified callers record decisions under the shared empty caller, "
    "so a per-subject daily count is unavailable"
)


def _ledger_attributed(who: str | None) -> bool:
    """False when the decision ledger cannot attribute calls to this subject."""
    return who is None or not who.startswith(_UNATTRIBUTED_PREFIXES)


def _applies(
    rule: policy.PolicyRule, scope: gateway.CallScope, *, foreign: bool
) -> tuple[bool | None, str | None]:
    """``(applies, reason)``: would the subject's CURRENT scope be charged.

    The bind filter is the gate's own (``gateway.binding_matches``) and is
    exact for any subject, since claims are hub state. The role filter is
    exact only for the calling identity (``foreign=False``), whose live
    profile the request context knows via ``policy._roles_apply``. For another
    identity the session profile is not visible from here, so a role-scoped
    rule is confirmed only when the identity itself is in ``roles``; anything
    else reports ``applies=None`` ("session profile not visible from this
    view"), never a guessed ``False``.
    """
    if not gateway.binding_matches(
        rule.bind, scope, unclaimed_matches=policy._is_restrictive(rule)
    ):
        return False, "bind does not match the caller's current claims"
    if foreign:
        if rule.roles and scope.agent not in rule.roles:
            return None, "session profile not visible from this view"
        return True, None
    if not policy._roles_apply(rule, None, scope):
        return False, "caller is outside the rule's roles"
    return True, None


def _claims_scope(agent: str, need_claims: bool) -> gateway.CallScope:
    """Another identity's scope: live claims, never its session profile."""
    try:
        return gateway.resolve_claims(agent) if need_claims else gateway.CallScope(agent=agent)
    except sqlite3.Error as exc:
        raise policy.PolicyViolation(
            f"Database error while resolving gateway bindings: {exc}",
            reason_code="policy_unavailable",
        ) from exc


def tool_allowance(agent: str | None = None, *, now: float | None = None) -> dict[str, Any]:
    """Per-tool call allowance for the calling identity (task #2144).

    Each row is one ``(rule, tool)`` allowance for the subject: ``used`` is
    the number of calls charged inside the rule's sliding window by the
    enforcement counter itself, ``limit``/``remaining`` the configured cap and
    what is left, ``stopped`` true once ``used`` reaches ``limit`` under the
    current rule generation AND a scope check the view can confirm (exact for
    the caller's own identity; for other identities binds are re-checked
    against live claims and a role scope is confirmed only when the identity
    itself is in ``roles``, else the row reports ``applies: null``), and
    ``resets_at``/``resets_in_seconds`` when the earliest charge leaves the
    window (when the next call will be admitted). ``used_today`` is allowed
    calls since 00:00 UTC from the decision ledger — context, since the window
    is at most one hour. Read rollups are attributed to the UTC day their
    bucket began, not the write time, and pending coalesced reads count only
    while their shared bucket began today. ``used_today`` is ``null`` with a
    ``used_today_reason`` for subjects the ledger cannot attribute:
    ``session:<id>``/``unverified:<label>`` callers are charged under those
    names, but their decisions record the shared empty caller. Buckets are
    per concrete tool: a glob rule keeps a
    ``pattern`` row at full remaining whenever uncharged matching tools can
    remain (always, since the match space cannot be enumerated); a
    literal-tool rule drops it once the subject's own bucket is live. A tool
    with calls today but no matching rule appears with ``limit: null``.

    ``agent`` selects the identity. Callers may read only the identity their
    calls are charged under; the verified operator may name any identity,
    ``"*"``, or omit it for the whole fleet (``agent: null`` in the reply).
    ``now`` overrides the rate-limit clock (tests), matching
    ``evaluate_policy``'s ``now``.
    """
    caller = gateway.resolve_scope(None, need_claims=False).agent
    operator = (config.operator_label() or "").strip()
    is_operator = bool(caller) and caller == operator
    requested = (agent or "").strip()
    if requested and requested != caller:
        if not is_operator:
            raise ToolError(
                "tool allowance is per-caller: only the verified operator may read another identity"
            )
        subject: str | None = None if requested == "*" else requested
    elif requested == caller:
        subject = caller
    else:
        # No agent named: the operator reads the fleet, everyone else self.
        subject = None if is_operator else caller

    moment = time.monotonic() if now is None else float(now)
    wall_now = time.time()
    try:
        cfg = policy.load_policy()
        configured = any(r.type == "rate_limit" for r in cfg.rules)
        # The gate's active rule set is empty while the policy is disabled;
        # matching it keeps ``current`` honest without touching the buckets.
        rules = [r for r in cfg.rules if r.type == "rate_limit"] if cfg.enabled else []
        policy_error = None
    except policy.PolicyViolation as exc:
        configured = False
        rules = []
        policy_error = str(exc)[:300]
    rule_by_key = {(r.name, gateway._rate_rule_fingerprint(r)): r for r in rules}

    # ``stopped`` is only honest when the subject's CURRENT scope would still
    # be charged by that rule — the same bind/role filter the gate applies
    # before reserving (a bucket charged under an earlier claim or profile
    # stops nothing now). For the caller's own identity the check is exact
    # (the request context knows the live profile); for any other identity —
    # an operator's ``agent=<other>`` or a fleet row — binds are re-checked
    # against that identity's live claims and a role scope is confirmed only
    # when the identity itself is in ``roles``, otherwise ``applies`` is null.
    need_claims = any(gateway.bind_needs_claims(r.bind) for r in rules)
    subject_scope: gateway.CallScope | None = None
    if subject is not None and any(r.bind or r.roles for r in rules):
        if subject == caller:
            subject_scope = gateway.resolve_scope(None, need_claims=need_claims)
        else:
            subject_scope = _claims_scope(subject, need_claims)

    fleet_scopes: dict[str, gateway.CallScope] = {}

    def _fleet_scope(bucket_agent: str) -> gateway.CallScope:
        """One bucket owner's claims scope, resolved once per agent."""
        if bucket_agent not in fleet_scopes:
            fleet_scopes[bucket_agent] = (
                gateway.resolve_scope(None, need_claims=need_claims)
                if bucket_agent == caller
                else _claims_scope(bucket_agent, need_claims)
            )
        return fleet_scopes[bucket_agent]

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    calls_today, ledger_error = _calls_today(today, subject)

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for bucket in gateway.rate_limit_snapshot(subject, now=moment):
        if bucket["used"] == 0:
            continue
        rule = rule_by_key.get((bucket["rule"], bucket["generation"]))
        current = rule is not None
        applies: bool | None = True
        applies_reason: str | None = None
        scoped = False
        if current:
            if subject is not None and subject_scope is not None:
                applies, applies_reason = _applies(rule, subject_scope, foreign=subject != caller)
                scoped = True
            elif subject is None and (rule.bind or rule.roles):
                applies, applies_reason = _applies(
                    rule, _fleet_scope(bucket["agent"]), foreign=bucket["agent"] != caller
                )
                scoped = True
        stopped = bool(current and applies is True and bucket["used"] >= bucket["limit"])
        wait = (
            round(bucket["oldest_live"] + bucket["window_seconds"] - moment, 3) if stopped else None
        )
        attributed = _ledger_attributed(bucket["agent"])
        row = {
            "agent": bucket["agent"],
            "tool": bucket["tool"],
            "rule": bucket["rule"],
            "used": bucket["used"],
            "used_today": (
                calls_today.get((bucket["agent"], bucket["tool"]), 0) if attributed else None
            ),
            "limit": bucket["limit"],
            "remaining": max(0, bucket["limit"] - bucket["used"]),
            "window_seconds": bucket["window_seconds"],
            "stopped": stopped,
            "resets_in_seconds": wait,
            "resets_at": _iso(wall_now + wait) if wait is not None else None,
            "current": current,
        }
        if not attributed:
            row["used_today_reason"] = _UNATTRIBUTED_TODAY
        if rule is not None:
            if rule.bind:
                row["bind"] = dict(rule.bind)
            if rule.roles:
                row["roles"] = list(rule.roles)
        if scoped:
            row["applies"] = applies
            if applies_reason is not None:
                row["applies_reason"] = applies_reason
        rows.append(row)
        if current:
            seen.add((bucket["agent"], bucket["rule"], bucket["tool"]))

    # A ``(caller, tool)`` pair already on a bucket row must not be re-counted
    # into a matching pattern row's ``used_today``.
    bucketed = {(r["agent"], r["tool"]) for r in rows}
    for rule in rules:
        if subject is not None:
            # A literal tool has exactly one bucket per caller: once it is
            # live the pattern row would duplicate it. A glob pattern has
            # uncharged matching tools beside every live bucket — each
            # matching tool has its own allowance of ``max_calls``.
            if not glob.has_magic(rule.tool) and (subject, rule.name, rule.tool) in seen:
                continue
            row_agent: str | None = subject
        else:
            # The fleet view cannot enumerate callers, so uncharged
            # (caller, tool) pairs always remain under the pattern row.
            row_agent = None
        attributed = _ledger_attributed(row_agent)
        used_today = (
            sum(
                n
                for (who, tool), n in calls_today.items()
                if (subject is None or who == subject)
                and fnmatch.fnmatch(tool, rule.tool)
                and (who, tool) not in bucketed
            )
            if attributed
            else None
        )
        row = {
            "agent": row_agent,
            "tool": rule.tool,
            "rule": rule.name,
            "used": 0,
            "used_today": used_today,
            "limit": rule.max_calls,
            "remaining": rule.max_calls,
            "window_seconds": rule.window_seconds,
            "stopped": False,
            "resets_in_seconds": None,
            "resets_at": None,
            "current": True,
            "pattern": True,
        }
        if not attributed:
            row["used_today_reason"] = _UNATTRIBUTED_TODAY
        if rule.bind:
            row["bind"] = dict(rule.bind)
        if rule.roles:
            row["roles"] = list(rule.roles)
        if subject_scope is not None:
            applies, applies_reason = _applies(rule, subject_scope, foreign=subject != caller)
            row["applies"] = applies
            if applies_reason is not None:
                row["applies_reason"] = applies_reason
        rows.append(row)

    covered = {(r["agent"], r["tool"]) for r in rows}
    for (who, tool), n in sorted(calls_today.items()):
        if subject is not None and who != subject:
            continue
        if (who, tool) in covered or any(fnmatch.fnmatch(tool, r.tool) for r in rules):
            continue
        rows.append(
            {
                "agent": who,
                "tool": tool,
                "rule": None,
                "used": 0,
                "used_today": n,
                "limit": None,
                "remaining": None,
                "window_seconds": None,
                "stopped": False,
                "resets_in_seconds": None,
                "resets_at": None,
                "current": True,
            }
        )

    rows.sort(
        key=lambda r: (
            not r["stopped"],
            str(r["agent"] or ""),
            str(r["tool"]),
            str(r["rule"] or ""),
        )
    )
    return {
        "agent": subject,
        "as_of": _iso(wall_now),
        "today": today.date().isoformat(),
        "configured": configured,
        # A NEW (rule, tool, caller) bucket can be refused with
        # ``rate_limit_capacity`` at MAX_BUCKETS even while every visible row
        # still shows remaining — the LIVE table fill is part of the answer
        # (``admit`` prunes expired windows before the check).
        "capacity": {
            "buckets": gateway.live_bucket_count(now=moment, keep=frozenset(rule_by_key)),
            "max_buckets": gateway.MAX_BUCKETS,
        },
        **({"policy_error": policy_error} if policy_error else {}),
        **({"ledger_error": ledger_error} if ledger_error else {}),
        "tools": rows,
    }
