"""Durable, fail-closed daily reservations for hub-brokered LLM calls."""

from __future__ import annotations

import math
import os
import uuid
from datetime import UTC, datetime

from . import events


class BudgetError(ValueError):
    """The broker's spend configuration or remaining budget is insufficient."""


def _limits() -> tuple[float, float]:
    try:
        daily = float(os.environ.get("UA_LLM_BROKER_DAILY_USD", "0"))
        reserve = float(os.environ.get("UA_LLM_BROKER_RESERVE_USD", "0"))
    except ValueError as exc:
        raise BudgetError("broker spend limit is not configured") from exc
    if not all(math.isfinite(value) and value > 0 for value in (daily, reserve)):
        raise BudgetError("broker spend limit is not configured")
    if reserve > daily:
        raise BudgetError("broker request reservation exceeds daily limit")
    return daily, reserve


def _table(conn: object) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS llm_broker_spend ("
        "request_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, agent TEXT NOT NULL, "
        "device_node TEXT NOT NULL, day_utc TEXT NOT NULL, reserve_usd REAL NOT NULL, "
        "actual_usd REAL, outcome TEXT, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_broker_spend_day "
        "ON llm_broker_spend(org_id, agent, day_utc)"
    )


def reserve(
    *, org_id: str | None, agent: str, node: str, model: str, provider: str = "openrouter"
) -> tuple[str, str, str]:
    """Atomically reserve one call and append its audit attempt before spending."""
    daily, amount = _limits()
    request_id = uuid.uuid4().hex
    day = datetime.now(UTC).date().isoformat()
    tenant = org_id or ""

    def _precondition(conn: object) -> None:
        _table(conn)
        row = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(actual_usd, reserve_usd)), 0) AS used "
            "FROM llm_broker_spend WHERE org_id = ? AND agent = ? AND day_utc = ?",
            (tenant, agent, day),
        ).fetchone()
        if float(row["used"]) + amount > daily + 1e-9:
            raise BudgetError("agent daily broker budget exhausted")
        conn.execute(
            "INSERT INTO llm_broker_spend "
            "(request_id, org_id, agent, device_node, day_utc, reserve_usd, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (request_id, tenant, agent, node, day, amount, datetime.now(UTC).isoformat()),
        )

    attempt = events.record_event(
        "hub.llm.broker_attempt",
        payload={
            "request_id": request_id,
            "provider": provider,
            "model": model,
            "agent": agent,
            "device_node": node,
            "reserved_usd": amount,
        },
        originator=agent,
        precondition=_precondition,
    )
    return request_id, attempt, provider


def finish(
    request_id: str,
    attempt: str,
    *,
    agent: str,
    node: str,
    model: str,
    outcome: str,
    usage: dict[str, int | float] | None,
    provider: str = "openrouter",
) -> None:
    """Commit result and reconciliation together; failed writes retain the reserve."""
    actual = usage.get("cost_usd") if usage and outcome == "completed" else None

    def _precondition(conn: object) -> None:
        _table(conn)
        conn.execute(
            "UPDATE llm_broker_spend SET actual_usd = ?, outcome = ?, updated_at = ? "
            "WHERE request_id = ? AND outcome IS NULL",
            (actual, outcome, datetime.now(UTC).isoformat(), request_id),
        )

    events.record_event(
        "hub.llm.broker_result",
        payload={
            "request_id": request_id,
            "provider": provider,
            "model": model,
            "agent": agent,
            "device_node": node,
            "outcome": outcome,
            "usage": usage,
        },
        originator=agent,
        causal_parent_id=attempt,
        precondition=_precondition,
    )
