"""Per-agent cost rollups and budgets with a warning threshold and hard stop (task #1348).

Cost comes from records the platform already writes; this module adds no table:

* **Run cost** is the ``usage`` on a self-pickup finish (``run_records``,
  channel ``agent.run.selfpickup``): provider, model, tokens, ``cost_usd``,
  ``billing``, per-phase seconds and a subscription-contention flag. An open
  run's cost is its latest heartbeat in ``run_usage_heartbeats`` (task #1917);
  the finish takes the heartbeat over, so a run is counted once.
* **Broker cost** is the hub-brokered LLM spend ledger (``llm_spend``,
  table ``llm_broker_spend``, J7 #1326): actual cost when reconciled, the
  reservation while a call is unfinished or its outcome is uncertain.

Direct API cost and native subscription usage stay separate. A run with
``billing: "subscription"`` has no USD price: it is counted in
``subscription_runs`` and its tokens, never folded into dollars, and the
subscription lane's remaining quota is reported from ``quota_observe`` as
``unknown`` unless there is evidence. A finished run that reported no cost
and is not a subscription or local run is ``unknown_cost``: it is counted,
never priced at zero. Explicit ``cost_complete: false`` also classifies a
partially priced run as unknown while retaining known USD plus its reserve.

Budgets are declared in the reviewed policy file (``policy.yaml``, the same
file ``policy.py`` reads) under a top-level ``budgets`` list::

    budgets:
      - name: codex-monthly
        agents: [Desktop_Agent2, server_agent2]   # omit or ["*"] for every agent
        period: month                           # month | day (UTC)
        limit_usd: 200
        warn_pct: 80                            # default 80
        run_reserve_usd: 2.5                    # default 0
        action: block                           # block (default) | warn

``committed`` for an agent in the current period is the API cost of its
finished runs, plus ``run_reserve_usd`` for each finished run of unknown cost,
plus for each task the agent currently holds in ``doing`` the larger of
``run_reserve_usd`` and the cost its open run has reported, plus the reported
cost of its other open runs, plus its broker spend. An explicitly incomplete
open price holds known USD plus its unknown-cost reserve even after expiry.
A new claim is refused when ``committed + run_reserve_usd`` would exceed
``limit_usd`` under a ``block`` policy; at ``warn_pct`` the claim succeeds
with ``budget_warnings``. The check reads inside the claim's ``BEGIN
IMMEDIATE`` transaction, so simultaneous claims serialize against the same
remaining budget and each one's reserve is seen by the next.

A malformed ``budgets`` block fails closed, like the rest of the policy file:
every claim is refused with the parse error until the file is fixed, because
which agents a broken block governs cannot be known. With no ``budgets`` block nothing is read and claims are
unaffected.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import events, run_records, storage
from .budget_policy import (  # noqa: F401 - budget policy parsing; re-exported here
    ACTIONS,
    DEFAULT_WARN_PCT,
    PERIODS,
    BudgetConfigError,
    BudgetPolicy,
    _number,
    _parse,
    load_budgets,
)

logger = logging.getLogger("unified_agent.budgets")

CHANNEL = "agent.budget"
KIND_WARNING = "budget.warning"
KIND_BLOCKED = "budget.blocked"
KIND_RUN_STOP = "budget.run_stop"

BILLING = ("api", "subscription", "local")
GROUP_BY = ("agent", "task", "project", "provider", "model", "billing")


@dataclass
class ClaimDecision:
    """What a budget check concluded for one claim; events are written later."""

    agent: str
    blocked: str | None = None
    warnings: list[str] = field(default_factory=list)
    events: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)
    # Set only by evaluate_run: the run in flight should stop (task #1917).
    stop: str | None = None


# --- configuration -----------------------------------------------------------


# --- spend -------------------------------------------------------------------


def period_start(period: str, now: datetime | None = None) -> datetime:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _billing(usage: dict[str, Any]) -> str | None:
    value = usage.get("billing")
    return value if value in BILLING else None


def _classify(usage: dict[str, Any]) -> str:
    """``priced`` | ``subscription`` | ``local`` | ``unknown_cost`` for one finish."""
    billing = _billing(usage)
    if billing == "subscription":
        return "subscription"
    if billing == "local":
        return "local"
    cost = usage.get("cost_usd")
    priced = isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost)
    return (
        "priced"
        if priced and cost >= 0 and usage.get("cost_complete") is not False
        else "unknown_cost"
    )


def _known_usd(usage: dict[str, Any]) -> float:
    """Known API cost, including the lower bound of incompletely priced runs."""
    if _billing(usage) in ("subscription", "local"):
        return 0.0
    cost = usage.get("cost_usd")
    if (
        isinstance(cost, (int, float))
        and not isinstance(cost, bool)
        and math.isfinite(cost)
        and cost >= 0
    ):
        return float(cost)
    return 0.0


def _finishes(conn: Any, since: str, until: str | None, agent: str | None) -> list[dict[str, Any]]:
    where = [
        "channel=?",
        "json_extract(payload, '$.kind')=?",
        "json_extract(payload, '$.finished_at') >= ?",
    ]
    params: list[Any] = [run_records.CHANNEL, run_records.KIND_FINISHED, since]
    if until:
        where.append("json_extract(payload, '$.finished_at') < ?")
        params.append(until)
    if agent:
        where.append(
            "COALESCE(json_extract(payload, '$.agent'), json_extract(payload, '$.finished_by')) = ?"
        )
        params.append(agent)
    rows = conn.execute(
        f"SELECT payload FROM event_log WHERE {' AND '.join(where)}", params
    ).fetchall()
    finishes = [run_records._payload(r) for r in rows]
    late = run_records._heartbeat_usages(conn, [f["run_id"] for f in finishes if f.get("run_id")])
    for finish in finishes:
        usage = late.get(finish.get("run_id"))
        if usage is not None:
            finish["usage"] = run_records.merge_usage(finish.get("usage"), usage)
            finish["usage_reported"] = True
    return finishes


def _broker_table_exists(conn: Any) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_broker_spend'"
    ).fetchone()
    return row is not None


def _broker_usd(conn: Any, agent: str, since_day: str) -> float:
    if not _broker_table_exists(conn):
        return 0.0
    row = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(actual_usd, reserve_usd)), 0) AS used "
        "FROM llm_broker_spend WHERE agent = ? AND day_utc >= ?",
        (agent, since_day),
    ).fetchone()
    return float(row["used"] or 0.0)


def _live_doing(conn: Any, agent: str, now_ts: str, exclude_id: int | None = None) -> set[int]:
    """Ids of doing tasks this agent is running: claimed by it, or holding its open run.

    A task moved to ``doing`` through ``task_update`` without ``task_claim`` has
    no ``claimed_by`` but does have an open self-pickup run under the actor
    (#1436); it must hold a reserve too, or successive such runs escape it.
    """
    sql = (
        "SELECT t.id FROM tasks t WHERE t.status='doing' AND ("
        "(t.claimed_by=? AND (t.lock_expires_at IS NULL OR t.lock_expires_at > ?)) "
        "OR (t.claimed_by IS NULL AND EXISTS (SELECT 1 FROM event_log c "
        "WHERE c.channel=? AND json_extract(c.payload, '$.kind')=? "
        "AND json_extract(c.payload, '$.task_id')=t.id "
        "AND json_extract(c.payload, '$.agent')=? "
        "AND NOT EXISTS (SELECT 1 FROM event_log f WHERE f.dedupe_key = "
        "'selfpickup:finished:' || json_extract(c.payload, '$.run_id')))))"
    )
    params: list[Any] = [
        agent,
        now_ts,
        run_records.CHANNEL,
        run_records.KIND_CLAIMED,
        agent,
    ]
    if exclude_id is not None:
        sql += " AND t.id != ?"
        params.append(exclude_id)
    return {int(r["id"]) for r in conn.execute(sql, params).fetchall()}


def _open_run_costs(conn: Any, agent: str, since: str) -> tuple[dict[int, float], set[int]]:
    """Known USD by task and incompletely priced open tasks.

    An open run's cumulative usage lives in ``run_usage_heartbeats`` until the
    run finishes; the finish event then carries it and the row is deleted
    (``run_records._append``), so a run is counted here or among the
    finishes, never both (task #1917). Explicitly incomplete prices retain
    their unknown-cost reserve in addition to known USD. Subscription and local
    snapshots carry no USD price and are left out, like their finishes.
    """
    try:
        rows = conn.execute(
            "SELECT task_id, usage_json FROM run_usage_heartbeats "
            "WHERE agent=? AND updated_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM event_log f WHERE f.dedupe_key = "
            "'selfpickup:finished:' || run_usage_heartbeats.run_id)",
            (agent, since),
        ).fetchall()
    except Exception:  # noqa: BLE001 - a store predating the table has no heartbeats
        return {}, set()
    out: dict[int, float] = {}
    incomplete: set[int] = set()
    for row in rows:
        try:
            usage = json.loads(row["usage_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(usage, dict) and _billing(usage) not in ("subscription", "local"):
            tid = int(row["task_id"])
            out[tid] = out.get(tid, 0.0) + _known_usd(usage)
            if usage.get("cost_complete") is False:
                incomplete.add(tid)
    return out, incomplete


def agent_spend(
    conn: Any,
    agent: str,
    policy: BudgetPolicy,
    *,
    now: datetime | None = None,
    exclude_task_id: int | None = None,
) -> dict[str, Any]:
    """One agent's spend against one policy's current period."""
    now = now or datetime.now(UTC)
    start = period_start(policy.period, now)
    since = _iso(start)
    api_usd = 0.0
    counts = {"priced": 0, "subscription": 0, "local": 0, "unknown_cost": 0}
    for f in _finishes(conn, since, None, agent):
        usage = f.get("usage") or {}
        kind = _classify(usage)
        counts[kind] += 1
        api_usd += _known_usd(usage)
    live = _live_doing(conn, agent, _iso(now), exclude_task_id)
    open_costs, incomplete = _open_run_costs(conn, agent, since)
    # A live run holds the larger of its reserve and the cost it has reported;
    # a run whose lease lapsed holds its known cost. Explicitly incomplete
    # pricing also retains the unknown-cost reserve, regardless of lease state.
    reserved = counts["unknown_cost"] * policy.run_reserve_usd
    open_cost = 0.0
    for tid in live:
        cost = open_costs.get(tid, 0.0)
        if tid in incomplete:
            open_cost += cost
            reserved += policy.run_reserve_usd
        elif cost > policy.run_reserve_usd:
            open_cost += cost
        else:
            reserved += policy.run_reserve_usd
    open_cost += sum(c for tid, c in open_costs.items() if tid not in live)
    reserved += len(incomplete - live) * policy.run_reserve_usd
    broker = _broker_usd(conn, agent, start.date().isoformat())
    committed = api_usd + open_cost + reserved + broker
    return {
        "agent": agent,
        "period": policy.period,
        "period_start": since,
        "api_cost_usd": round(api_usd, 6),
        "open_run_cost_usd": round(sum(open_costs.values()), 6),
        "broker_usd": round(broker, 6),
        "reserved_usd": round(reserved, 6),
        "committed_usd": round(committed, 6),
        "priced_runs": counts["priced"],
        "unknown_cost_runs": counts["unknown_cost"],
        "subscription_runs": counts["subscription"],
        "local_runs": counts["local"],
        "in_flight": len(live),
    }


def _state(spend: dict[str, Any], policy: BudgetPolicy) -> str:
    committed = spend["committed_usd"]
    if committed >= policy.limit_usd - 1e-9:
        return "exhausted"
    if committed >= policy.limit_usd * policy.warn_pct / 100.0 - 1e-9:
        return "warning"
    return "ok"


# --- enforcement -------------------------------------------------------------


def _event(kind: str, policy: BudgetPolicy, spend: dict[str, Any], **extra: Any) -> tuple:
    payload = {
        "kind": kind,
        "policy": policy.name,
        "agent": spend["agent"],
        "period_start": spend["period_start"],
        "limit_usd": policy.limit_usd,
        "committed_usd": spend["committed_usd"],
        **extra,
    }
    # One event per (kind, policy, agent, period): the executor retries a
    # refused claim every poll tick and must not flood the ledger.
    key = f"budget:{kind}:{policy.name}:{spend['agent']}:{spend['period_start']}"
    return (kind, payload, key)


def evaluate_claim(
    conn: Any, agent: str, *, task_id: int | None = None, now: datetime | None = None
) -> ClaimDecision:
    """Decide whether ``agent`` may open one more run; read-only on ``conn``.

    Call it inside the claim's write transaction. ``task_id`` is the task being
    claimed; it is excluded from the in-flight count because the claim adds its
    reserve explicitly.
    """
    decision = ClaimDecision(agent=agent)
    policies = load_budgets()
    if not policies:
        return decision
    applicable = [p for p in policies if p.applies_to(agent)]
    for policy in applicable:
        spend = agent_spend(conn, agent, policy, now=now, exclude_task_id=task_id)
        after = spend["committed_usd"] + policy.run_reserve_usd
        over = spend["committed_usd"] >= policy.limit_usd - 1e-9 or (
            policy.run_reserve_usd > 0 and after > policy.limit_usd + 1e-9
        )
        summary = (
            f"budget {policy.name!r}: {agent} committed ${after:.2f} "
            f"of ${policy.limit_usd:.2f} this {policy.period} with this claim"
        )
        if over and policy.action == "block":
            decision.blocked = f"budget hard stop: {summary}" + (
                f" (a new run reserves ${policy.run_reserve_usd:.2f})"
                if policy.run_reserve_usd
                else ""
            )
            decision.events.append(_event(KIND_BLOCKED, policy, spend, task_id=task_id))
            return decision
        warn_at = policy.limit_usd * policy.warn_pct / 100.0 - 1e-9
        if over or after >= warn_at:
            decision.warnings.append(summary + (" — limit reached (warn only)" if over else ""))
            decision.events.append(_event(KIND_WARNING, policy, spend, task_id=task_id))
    return decision


def evaluate_run(
    conn: Any, agent: str, *, task_id: int, now: datetime | None = None
) -> ClaimDecision:
    """Judge a running run after it reports usage on a renewal (task #1917).

    Read-only on ``conn``; call it inside the renewal's transaction after the
    heartbeat is stored, so ``committed`` already includes the run's reported
    cost. Past ``warn_pct`` it warns. Once ``committed`` goes past
    ``limit_usd`` under a ``block`` budget it sets ``stop``: the renewal still
    succeeds, so the runner keeps its lease while it stops the child and
    releases the task. Reserves alone filling the budget exactly do not stop
    a run, because claims are admitted up to that point.
    """
    decision = ClaimDecision(agent=agent)
    policies = load_budgets()
    for policy in (p for p in policies if p.applies_to(agent)):
        spend = agent_spend(conn, agent, policy, now=now)
        committed = spend["committed_usd"]
        summary = (
            f"budget {policy.name!r}: {agent} committed ${committed:.2f} "
            f"of ${policy.limit_usd:.2f} this {policy.period} with this run"
        )
        over = committed > policy.limit_usd + 1e-9
        if over and policy.action == "block":
            decision.stop = f"budget hard stop: {summary}"
            decision.events.append(_event(KIND_RUN_STOP, policy, spend, task_id=task_id))
            return decision
        if committed >= policy.limit_usd * policy.warn_pct / 100.0 - 1e-9:
            reached = committed >= policy.limit_usd - 1e-9
            decision.warnings.append(summary + (" — limit reached (warn only)" if reached else ""))
            decision.events.append(_event(KIND_WARNING, policy, spend, task_id=task_id))
    return decision


def evaluate_claim_or_config_error(
    conn: Any, agent: str, *, task_id: int | None = None
) -> ClaimDecision:
    """``evaluate_claim``, turning a malformed budgets block into a refusal."""
    try:
        return evaluate_claim(conn, agent, task_id=task_id)
    except BudgetConfigError as exc:
        return ClaimDecision(agent=agent, blocked=f"budget policy fails closed: {exc}")


def record_decision(decision: ClaimDecision) -> None:
    """Write a decision's warning/blocked events. Best-effort; never raises."""
    for _kind, payload, key in decision.events:
        try:
            events.record_event_once(
                CHANNEL, payload=payload, originator=decision.agent, dedupe_key=key
            )
        except Exception:  # noqa: BLE001 - auditing a decision never fails the caller
            logger.warning("budget event %s not recorded", key, exc_info=True)


# --- readers -----------------------------------------------------------------


def _quota_for(agent: str, quota: dict[str, Any] | None) -> dict[str, Any]:
    """The agent's subscription lane availability, ``unknown`` without evidence."""
    try:
        from .quota_observe import lane_for_agent

        lane = lane_for_agent(agent)
    except Exception:  # noqa: BLE001
        return {"lane": None, "availability": "unknown", "remaining": "unknown", "source": None}
    for row in (quota or {}).get("quotas", []) or []:
        if row.get("lane") == lane:
            return {
                "lane": lane,
                "availability": row.get("availability") or "unknown",
                "remaining": row.get("remaining")
                if row.get("remaining") is not None
                else "unknown",
                "source": row.get("source"),
            }
    return {"lane": lane, "availability": "unknown", "remaining": "unknown", "source": None}


def budget_status(agent: str | None = None) -> dict[str, Any]:
    """Every configured budget with each governed agent's spend and state.

    Agents are those named by a policy, plus (for ``*`` policies) every agent
    with a run or doing task this period. ``state`` is ``ok``, ``warning`` or
    ``exhausted``. A malformed budgets block is reported, not raised.
    """
    try:
        policies = load_budgets()
    except BudgetConfigError as exc:
        return {"configured": True, "error": str(exc), "budgets": []}
    try:
        from .quota_observe import build_quota_status

        quota = build_quota_status()
    except Exception:  # noqa: BLE001 - quota is advisory; unknown is honest
        quota = None
    out = []
    conn = storage.connect()
    try:
        for policy in policies:
            agents = set(a for a in policy.agents if a != "*")
            if not policy.agents or "*" in policy.agents:
                since = _iso(period_start(policy.period))
                for f in _finishes(conn, since, None, None):
                    who = f.get("agent") or f.get("finished_by")
                    if who:
                        agents.add(who)
                for row in conn.execute(
                    "SELECT DISTINCT claimed_by FROM tasks WHERE status='doing' "
                    "AND claimed_by IS NOT NULL"
                ).fetchall():
                    agents.add(row["claimed_by"])
                for row in conn.execute(
                    "SELECT DISTINCT agent FROM run_usage_heartbeats WHERE updated_at>=?",
                    (since,),
                ).fetchall():
                    if row["agent"]:
                        agents.add(row["agent"])
            if agent:
                agents = {agent} if policy.applies_to(agent) else set()
            rows = []
            for who in sorted(agents):
                spend = agent_spend(conn, who, policy)
                rows.append(
                    {
                        **spend,
                        "state": _state(spend, policy),
                        "remaining_usd": round(
                            max(0.0, policy.limit_usd - spend["committed_usd"]), 6
                        ),
                        "subscription_quota": _quota_for(who, quota),
                    }
                )
            out.append({**policy.as_dict(), "agents_status": rows})
    finally:
        conn.close()
    return {"configured": bool(policies), "budgets": out}


def _parse_bound(value: str | None, name: str) -> str | None:
    """An ISO bound as a whole-second UTC string, rounded up.

    Finish times are whole seconds, so a fractional bound rounds up: for
    ``since`` a finish earlier in that second is excluded, for the exclusive
    ``until`` a finish in that second (before the bound) is kept.
    """
    if value is None or value == "":
        return None
    parsed = run_records._parse_ts(value)
    if parsed is None:
        raise ToolError(f"{name} must be an ISO 8601 timestamp")
    parsed = parsed.astimezone(UTC)
    if parsed.microsecond:
        parsed = parsed.replace(microsecond=0) + timedelta(seconds=1)
    return _iso(parsed)


def _finish_ref(f: dict[str, Any]) -> str:
    return f.get("run_id") or f"task:{f.get('task_id')}"


def _projects_for(conn: Any, finishes: list[dict[str, Any]]) -> dict[str, str | None]:
    """Each finish's project as recorded on its claim, so history does not move.

    A finish with no recorded claim falls back to the task's current project.
    """
    out: dict[str, str | None] = {}
    run_ids = [f["run_id"] for f in finishes if f.get("run_id")]
    for i in range(0, len(run_ids), 500):
        chunk = run_ids[i : i + 500]
        for row in conn.execute(
            "SELECT json_extract(payload, '$.run_id') AS run_id, "
            "json_extract(payload, '$.project') AS project FROM event_log "
            f"WHERE channel=? AND json_extract(payload, '$.kind')=? "
            f"AND json_extract(payload, '$.run_id') IN ({','.join('?' * len(chunk))})",
            [run_records.CHANNEL, run_records.KIND_CLAIMED, *chunk],
        ).fetchall():
            out[row["run_id"]] = row["project"]
    orphans = sorted(
        {
            int(f["task_id"])
            for f in finishes
            if not f.get("run_id") and f.get("task_id") is not None
        }
    )
    for i in range(0, len(orphans), 500):
        chunk = orphans[i : i + 500]
        for row in conn.execute(
            f"SELECT id, project FROM tasks WHERE id IN ({','.join('?' * len(chunk))})", chunk
        ).fetchall():
            out[f"task:{row['id']}"] = row["project"]
    return out


def cost_rollup(
    group_by: str = "agent",
    since: str | None = None,
    until: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    """Run cost and usage grouped by agent, task, project, provider, model or billing.

    Reads self-pickup finishes with ``finished_at`` in ``[since, until)``
    (default: the current UTC month). Each group reports API cost, run counts
    by cost class (priced / unknown_cost / subscription / local), tokens,
    wall time, per-phase seconds and contended runs. Broker spend is listed
    per agent under ``broker`` for the same window, never merged into run
    cost (a brokered call is not attributed to a run).
    """
    key = (group_by or "agent").strip().lower()
    if key not in GROUP_BY:
        raise ToolError(f"group_by must be one of {GROUP_BY}")
    lo = _parse_bound(since, "since") or _iso(period_start("month"))
    hi = _parse_bound(until, "until")
    conn = storage.connect()
    try:
        finishes = _finishes(conn, lo, hi, agent.strip() if agent else None)
        need_project = key == "project"
        projects: dict[str, str | None] = {}
        if need_project:
            projects = _projects_for(conn, finishes)
        groups: dict[str, dict[str, Any]] = {}
        for f in finishes:
            usage = f.get("usage") or {}
            if key == "agent":
                g = f.get("agent") or f.get("finished_by")
            elif key == "task":
                g = f.get("task_id")
            elif key == "project":
                g = projects.get(_finish_ref(f))
            elif key == "billing":
                g = _billing(usage)
            else:
                g = usage.get(key)
            label = str(g) if g is not None else "(unknown)"
            row = groups.setdefault(
                label,
                {
                    key: label,
                    "runs": 0,
                    "usage_reported": 0,
                    "api_cost_usd": 0.0,
                    "priced_runs": 0,
                    "unknown_cost_runs": 0,
                    "subscription_runs": 0,
                    "local_runs": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "subscription_input_tokens": 0,
                    "subscription_output_tokens": 0,
                    "wall_s": 0.0,
                    "phases_s": {},
                    "contended_runs": 0,
                    "outcomes": {},
                },
            )
            row["runs"] += 1
            row["usage_reported"] += 1 if f.get("usage_reported") else 0
            cls = _classify(usage)
            row[f"{cls}_runs"] += 1
            row["api_cost_usd"] += _known_usd(usage)
            prefix = "subscription_" if cls == "subscription" else ""
            for tok in ("input_tokens", "output_tokens"):
                if isinstance(usage.get(tok), int):
                    row[prefix + tok] += usage[tok]
            if isinstance(f.get("wall_s"), (int, float)):
                row["wall_s"] += float(f["wall_s"])
            for phase, secs in (usage.get("phases") or {}).items():
                if isinstance(secs, (int, float)):
                    row["phases_s"][phase] = round(row["phases_s"].get(phase, 0.0) + secs, 3)
            if usage.get("contended") is True:
                row["contended_runs"] += 1
            outcome = f.get("outcome") or "unknown"
            row["outcomes"][outcome] = row["outcomes"].get(outcome, 0) + 1
        for row in groups.values():
            row["api_cost_usd"] = round(row["api_cost_usd"], 6)
            row["wall_s"] = round(row["wall_s"], 3)
        broker: list[dict[str, Any]] = []
        broker_last_day = None
        if hi:
            hi_dt = run_records._parse_ts(hi)
            broker_last_day = (hi_dt - timedelta(seconds=1)).date().isoformat()
        if _broker_table_exists(conn):
            sql = (
                "SELECT agent, COUNT(*) AS calls, "
                "COALESCE(SUM(actual_usd), 0) AS actual_usd, "
                "COALESCE(SUM(CASE WHEN actual_usd IS NULL THEN reserve_usd END), 0) AS held_usd "
                "FROM llm_broker_spend WHERE day_utc >= ?"
            )
            # Broker rows carry only a UTC day, so broker spend covers every
            # whole day the window touches (reported as broker_days).
            params: list[Any] = [lo[:10]]
            if hi:
                sql += " AND day_utc <= ?"
                params.append(broker_last_day)
            if agent:
                sql += " AND agent = ?"
                params.append(agent.strip())
            for r in conn.execute(sql + " GROUP BY agent ORDER BY agent", params).fetchall():
                broker.append(
                    {
                        "agent": r["agent"],
                        "calls": int(r["calls"]),
                        "actual_usd": round(float(r["actual_usd"]), 6),
                        "unreconciled_reserve_usd": round(float(r["held_usd"]), 6),
                    }
                )
    finally:
        conn.close()
    rows = sorted(groups.values(), key=lambda r: (-r["api_cost_usd"], -r["runs"], r[key]))
    totals = {
        "runs": sum(r["runs"] for r in rows),
        "api_cost_usd": round(sum(r["api_cost_usd"] for r in rows), 6),
        "unknown_cost_runs": sum(r["unknown_cost_runs"] for r in rows),
        "subscription_runs": sum(r["subscription_runs"] for r in rows),
        "broker_usd": round(
            sum(b["actual_usd"] + b["unreconciled_reserve_usd"] for b in broker), 6
        ),
    }
    return {
        "group_by": key,
        "since": lo,
        "until": hi,
        "groups": rows,
        "broker": broker,
        "broker_days": {"from": lo[:10], "to": broker_last_day, "granularity": "utc_day"},
        "totals": totals,
    }


def costs_payload(since: str | None = None, until: str | None = None) -> dict[str, Any]:
    """The Control Center cost view: rollup by agent plus budget status."""
    return {
        "rollup": cost_rollup("agent", since=since, until=until),
        "budgets": budget_status(),
    }
