"""Pure computation for the four Delivery SLO metrics (task #1259, Epic C #1188).

Epic C ("the machine that builds the product") exit-tests the autonomy ladder's
L1 rung on two consecutive weeks of dispatch success >= 90%, zero unattributed
completions, and zero false-FAILED review verdicts. None of that was
measurable before this module: no `dispatch_success_rate` / `p90_cycle` /
`delivery_slo` symbol existed anywhere in `src` or `packages/web`.

Every function here is a pure function of its inputs — no I/O, no database
connection, no subprocess. `monitor.delivery_slo()` is the DB-querying wrapper
that reads the *existing* `dispatch_runs` and `tasks` stores (never a second
ledger, never a new table) and calls `compute_delivery_slo()` below. Keeping
the computation pure is what makes it unit-testable on synthetic data without
touching sqlite.

Metric definitions (documented once, here, not scattered across callers):

- **dispatch_success_rate**: among `dispatch_runs` rows whose `created_at`
  falls in the window, restricted to *terminal* states. Terminal is exactly
  ``dispatch_states.TERMINAL_RUN_STATES`` (``finished``, ``failed``,
  ``blocked``, ``denied``, ``cancelled``, ``expired``) — reused verbatim so
  this metric's notion of "terminal" can never drift from the Delivery
  Receipts pane's. ``retryable`` and ``refused`` runs are excluded from both
  numerator and denominator: a retryable run is still in flight, and a
  refused run was never dispatched. ``state == "finished"`` is success.
- **p90_cycle_time_hours**: task ``claimed_at`` -> ``completed_at``, in hours,
  p90 across done tasks whose ``completed_at`` falls in the window. A done
  task with no ``claimed_at`` (pre-attribution-enforcement rows can lack one)
  falls back to ``created_at`` -> ``completed_at`` for that one row; the
  fallback count is reported in ``detail`` so a pane can flag a blended
  sample rather than silently mixing two different start clocks.
- **unattributed_completions**: count (not a rate — the briefing line wants
  "unattributed N") of done tasks in the window whose ``claimed_by`` is
  empty/NULL. ``task_update()`` already refuses a completion with neither an
  existing claimant nor an explicit actor (see ``tools/tasks.py``), so in
  steady state this is always 0; the metric exists to prove that continuously,
  not to discover a bug reactively.
- **ci_green_rate**: share of PR head CI runs that were green, from a
  caller-supplied ``ci_runs`` sequence (each a dict with a ``conclusion`` or
  ``state`` field). ``None`` means "no provider configured" and reports
  ``status="unavailable"`` with a reason instead of inventing a number.

``status`` is one of ``"ok" | "breach" | "unavailable"``. Only two metrics
carry an explicit target from Epic C's exit test (success >= 0.90, unattributed
== 0); p90 cycle time and CI green rate have no stated target here, so they
read ``"ok"`` whenever they have data and ``"unavailable"`` when they don't —
never ``"breach"``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .dispatch_states import TERMINAL_RUN_STATES as _TERMINAL_RECEIPT_STATES

DISPATCH_SUCCESS_TARGET = 0.90
UNATTRIBUTED_TARGET = 0
DEFAULT_WINDOW_DAYS = 7.0
DISPATCH_SUCCESS_STATE = "finished"


@dataclass(frozen=True)
class SLOMetric:
    """One SLO result: enough to render a number and justify it.

    ``value`` is ``None`` exactly when ``status == "unavailable"`` — a pane or
    caller must never treat a missing value as zero.
    """

    name: str
    value: float | None
    numerator: int
    denominator: int
    window_days: float
    status: str  # "ok" | "breach" | "unavailable"
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "window_days": self.window_days,
            "status": self.status,
            "detail": self.detail,
        }


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO timestamp, treating a naive value as UTC (task-store
    convention — see `monitor.metrics_snapshot`'s identical handling)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (numpy's default method), pct in [0, 1].

    Requires a non-empty `values`; callers check that first so an empty
    window can report `status="unavailable"` instead of raising.
    """
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def dispatch_success_rate(
    dispatch_runs: list[dict[str, Any]],
    *,
    window_days: float = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> SLOMetric:
    """Terminal dispatch runs (`created_at` in window) that reached `finished`."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=window_days)
    terminal = []
    for run in dispatch_runs:
        if run.get("state") not in _TERMINAL_RECEIPT_STATES:
            continue
        created = _parse_ts(run.get("created_at"))
        if created is None or created < cutoff:
            continue
        terminal.append(run)
    denom = len(terminal)
    if denom == 0:
        return SLOMetric(
            "dispatch_success_rate",
            None,
            0,
            0,
            window_days,
            "unavailable",
            "no terminal dispatch runs in window",
        )
    numer = sum(1 for r in terminal if r.get("state") == DISPATCH_SUCCESS_STATE)
    raw_value = numer / denom
    value = round(raw_value, 4)
    status = "ok" if raw_value >= DISPATCH_SUCCESS_TARGET else "breach"
    return SLOMetric("dispatch_success_rate", value, numer, denom, window_days, status)


def p90_cycle_time(
    tasks: list[dict[str, Any]],
    *,
    window_days: float = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> SLOMetric:
    """p90 of claimed_at -> completed_at (fallback created_at) over done tasks."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=window_days)
    durations_hours: list[float] = []
    fallback_count = 0
    considered = 0
    for t in tasks:
        completed = _parse_ts(t.get("completed_at"))
        if completed is None or completed < cutoff:
            continue
        considered += 1
        start = _parse_ts(t.get("claimed_at"))
        used_fallback = False
        if start is None:
            start = _parse_ts(t.get("created_at"))
            used_fallback = True
        if start is None:
            continue  # no usable start timestamp at all — excluded, not zeroed
        durations_hours.append(max(0.0, (completed - start).total_seconds() / 3600.0))
        if used_fallback:
            fallback_count += 1
    if not durations_hours:
        return SLOMetric(
            "p90_cycle_time_hours",
            None,
            0,
            considered,
            window_days,
            "unavailable",
            "no completed tasks with a usable start timestamp in window",
        )
    value = round(_percentile(durations_hours, 0.9), 3)
    detail = (
        f"{fallback_count} of {len(durations_hours)} used created_at (no claimed_at)"
        if fallback_count
        else None
    )
    return SLOMetric(
        "p90_cycle_time_hours", value, len(durations_hours), considered, window_days, "ok", detail
    )


def unattributed_completions(
    tasks: list[dict[str, Any]],
    *,
    window_days: float = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> SLOMetric:
    """Count (not a rate) of done tasks in window with no `claimed_by`."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=window_days)
    done_in_window = []
    for t in tasks:
        completed = _parse_ts(t.get("completed_at"))
        if completed is None or completed < cutoff:
            continue
        done_in_window.append(t)
    denom = len(done_in_window)
    if denom == 0:
        return SLOMetric(
            "unattributed_completions",
            None,
            0,
            0,
            window_days,
            "unavailable",
            "no completed tasks in window",
        )
    numer = sum(1 for t in done_in_window if not str(t.get("claimed_by") or "").strip())
    status = "ok" if numer == UNATTRIBUTED_TARGET else "breach"
    return SLOMetric("unattributed_completions", float(numer), numer, denom, window_days, status)


def _is_green(run: dict[str, Any]) -> bool:
    conclusion = str(run.get("conclusion") or "").strip().lower()
    if conclusion:
        return conclusion == "success"
    state = str(run.get("state") or "").strip().lower()
    return state in ("success", "green")


def ci_green_rate(
    ci_runs: list[dict[str, Any]] | None,
    *,
    window_days: float = DEFAULT_WINDOW_DAYS,
    unavailable_reason: str | None = None,
) -> SLOMetric:
    """Share of PR head CI runs that were green.

    `ci_runs=None` means no provider is configured — never a fabricated
    number. Callers pass a reason (e.g. "no CI data source configured") that
    is surfaced verbatim so an operator can see *why* it's unavailable.
    """
    if ci_runs is None:
        return SLOMetric(
            "ci_green_rate",
            None,
            0,
            0,
            window_days,
            "unavailable",
            unavailable_reason or "no CI data source configured",
        )
    denom = len(ci_runs)
    if denom == 0:
        return SLOMetric(
            "ci_green_rate", None, 0, 0, window_days, "unavailable", "no CI runs in window"
        )
    numer = sum(1 for r in ci_runs if _is_green(r))
    value = round(numer / denom, 4)
    return SLOMetric("ci_green_rate", value, numer, denom, window_days, "ok")


def compute_delivery_slo(
    *,
    dispatch_runs: list[dict[str, Any]] | None = None,
    tasks: list[dict[str, Any]] | None = None,
    ci_runs: list[dict[str, Any]] | None = None,
    ci_unavailable_reason: str | None = None,
    window_days: float = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """All four Delivery SLO metrics over one window, keyed for direct JSON export."""
    now = now or datetime.now(UTC)
    if window_days <= 0:
        window_days = DEFAULT_WINDOW_DAYS
    dispatch_runs = dispatch_runs or []
    tasks = tasks or []
    metrics = {
        "dispatch_success_rate": dispatch_success_rate(
            dispatch_runs, window_days=window_days, now=now
        ),
        "p90_cycle_time_hours": p90_cycle_time(tasks, window_days=window_days, now=now),
        "unattributed_completions": unattributed_completions(
            tasks, window_days=window_days, now=now
        ),
        "ci_green_rate": ci_green_rate(
            ci_runs, window_days=window_days, unavailable_reason=ci_unavailable_reason
        ),
    }
    return {
        "window_days": window_days,
        "generated_at": now.isoformat(),
        **{k: v.to_dict() for k, v in metrics.items()},
    }


def format_delivery_slo_line(result: dict[str, Any]) -> str:
    """Render the weekly briefing summary line.

    "Delivery SLO (7d): dispatch X% · p90 Yh · unattributed N · CI green Z%"
    Any metric with `status == "unavailable"` (or a missing/None value)
    renders as "n/a" — never a fabricated number.
    """
    window = result.get("window_days", DEFAULT_WINDOW_DAYS)

    def _metric(key: str) -> dict[str, Any]:
        m = result.get(key)
        return m if isinstance(m, dict) else {}

    def _pct(key: str) -> str:
        m = _metric(key)
        if m.get("status") == "unavailable" or m.get("value") is None:
            return "n/a"
        return f"{m['value'] * 100:.0f}%"

    def _hours(key: str) -> str:
        m = _metric(key)
        if m.get("status") == "unavailable" or m.get("value") is None:
            return "n/a"
        return f"{m['value']:.1f}h"

    def _count(key: str) -> str:
        m = _metric(key)
        if m.get("status") == "unavailable" or m.get("value") is None:
            return "n/a"
        return str(int(m["value"]))

    return (
        f"Delivery SLO ({window:g}d): dispatch {_pct('dispatch_success_rate')} · "
        f"p90 {_hours('p90_cycle_time_hours')} · "
        f"unattributed {_count('unattributed_completions')} · "
        f"CI green {_pct('ci_green_rate')}"
    )
