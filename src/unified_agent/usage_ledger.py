"""Hub-observed provider usage ledger (task #1526).

Cursor's PR #860 tried to instrument usage by wrapping *every* MCP tool call
and resolving "the caller's current run" with a correlated ``event_log`` query
(JSON-extracted claim/finish rows, a ``NOT EXISTS`` on the unindexed
``dedupe_key`` column, joined against ``tasks``). That query ran once per tool
call and was vetoed on cost.

This module records usage only at the narrow set of places the hub actually
talks to a provider — ``llm_broker.py`` (the device-bound OpenAI-compatible
route), ``llm_client.py`` (the claude_cli/openrouter chain plus the
opt-in subscription-pool leg and the unwired ollama and ai_studio helpers), and
``tools/gemini.py`` (the AI Studio leg, and the standalone
``gemini_ask`` tool). It is never wired around ``server.py``'s generic tool
dispatch.

When a caller wants the row attributed to a run, the lookup is a single
indexed read of ``tasks.current_run_agent`` (see :func:`resolve_run_id`) —
never a scan of ``event_log``. That column is a cache run_records.py keeps
current in the same transaction as the claim/finish event it mirrors, so this
module adds no new run-tracking logic and no second source of truth for "is a
run open": the tasks row already says whether one committed.

Every row here is ``observed``: usage the hub itself saw on a provider
response. Agent-*reported* usage (a finishing ``task_update``'s ``run_usage``)
already has its own storage — the ``usage``/``usage_reported`` fields on a
self-pickup finish event (run_records.py) — and this module never writes to
or duplicates that. The two stay in separate tables by construction, so
"observed" can never be relabelled "reported" or vice versa.

Recording is best-effort and never raises: a failed insert is logged and must
never fail the provider call it rides along with. A missing price is stored
as NULL, never as a fabricated 0 — ``money()``/``token_count()`` only ever
pass through a real, already-known number.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from typing import Any

from . import config, storage

logger = logging.getLogger("unified_agent.usage_ledger")

_UNSET = object()


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create ``usage_events`` and its indexes. Idempotent; called from
    ``storage._migrate()`` at startup, not on every insert."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS usage_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          actor TEXT NOT NULL,
          run_id TEXT,
          provider TEXT NOT NULL,
          model TEXT,
          input_tokens INTEGER,
          output_tokens INTEGER,
          cost_usd REAL,
          dedupe_key TEXT,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_usage_events_actor_created
          ON usage_events(actor, created_at);
        CREATE INDEX IF NOT EXISTS idx_usage_events_run ON usage_events(run_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_events_dedupe
          ON usage_events(dedupe_key) WHERE dedupe_key IS NOT NULL;
        """
    )


def token_count(value: object) -> int | None:
    """A non-negative token count the caller already has, or None.

    Never estimates: an absent, negative, non-finite, or fractional value
    comes back None rather than a guessed number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)


def money(value: object) -> float | None:
    """A non-negative cost figure the caller already has, or None.

    A missing price stays NULL — this never estimates a price from tokens or
    substitutes 0 for "unknown".
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def resolve_run_id(actor: str | None) -> str | None:
    """The actor's open self-pickup run id, or None.

    One indexed read of ``tasks.current_run_agent`` — never event_log. If the
    actor holds more than one open run (two claimed tasks), which one a
    provider call belongs to is not knowable from the actor alone, so this
    returns None rather than guessing the newest (the exact ambiguity PR #860
    got wrong). Zero open runs also returns None.
    """
    label = actor.strip() if isinstance(actor, str) else ""
    if not label:
        return None
    try:
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT current_run_id FROM tasks "
                "WHERE current_run_agent = ? AND current_run_id IS NOT NULL",
                (label,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - lookup must never fail the caller
        logger.warning("usage run lookup failed: %s", exc)
        return None
    return rows[0]["current_run_id"] if len(rows) == 1 else None


def _actor(actor: str | None) -> str:
    explicit = actor.strip() if isinstance(actor, str) else ""
    if explicit:
        return explicit
    try:
        verified = config.verified_agent_identity()
    except Exception as exc:  # noqa: BLE001 - identity lookup is auxiliary
        logger.warning("usage actor lookup failed: %s", exc)
        verified = None
    if isinstance(verified, str) and verified.strip():
        return verified.strip()
    return "hub"


def _clip(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def record_observed(
    *,
    provider: str,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    actor: str | None = None,
    run_id: Any = _UNSET,
    dedupe_key: str | None = None,
) -> None:
    """Append one hub-observed usage row for a completed provider call.

    Never raises: a failed insert is logged and dropped, exactly like every
    other best-effort recorder in this codebase (see run_records.safe).
    ``run_id`` defaults to :func:`resolve_run_id` (``actor``); pass an explicit
    value (including ``None``) to skip that lookup. ``dedupe_key`` makes a
    retried call idempotent — a duplicate is silently suppressed, not logged
    as a failure.
    """
    resolved_actor = _actor(actor)
    resolved_run = resolve_run_id(resolved_actor) if run_id is _UNSET else run_id
    try:
        _insert(
            actor=resolved_actor,
            run_id=_clip(resolved_run, 200),
            provider=_clip(provider, 80) or "unknown",
            model=_clip(model, 200),
            input_tokens=token_count(input_tokens),
            output_tokens=token_count(output_tokens),
            cost_usd=money(cost_usd),
            dedupe_key=_clip(dedupe_key, 200),
        )
    except sqlite3.IntegrityError:
        # A duplicate dedupe_key: the call this insert rides along with
        # already recorded once (e.g. a retried broker result). Not a failure.
        logger.debug("usage ledger insert suppressed as a duplicate: %s", dedupe_key)
    except Exception as exc:  # noqa: BLE001 - ledger failure must not fail the caller
        logger.warning("usage ledger insert failed: %s", exc)


def _insert(
    *,
    actor: str,
    run_id: str | None,
    provider: str,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float | None,
    dedupe_key: str | None,
) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO usage_events ("
            "actor, run_id, provider, model, input_tokens, output_tokens, "
            "cost_usd, dedupe_key, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                actor,
                run_id,
                provider,
                model,
                input_tokens,
                output_tokens,
                cost_usd,
                dedupe_key,
                storage.now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def provider_call_count(provider: str | None = None) -> int:
    """Count hub-observed provider calls, optionally filtered to one provider."""
    conn = storage.connect()
    try:
        if provider:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM usage_events WHERE provider = ?", (provider,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM usage_events").fetchone()
    finally:
        conn.close()
    return int(row["n"]) if row is not None else 0
