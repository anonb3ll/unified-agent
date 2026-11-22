"""Query side of the agent activity index (task 64 phases A-C).

`agent_events` (storage.py) is the deterministic, queryable index of cluster
activity — the human-readable vault journal stays canonical. This module reads
that index: `agent_activity` for raw event queries, `metrics_snapshot` for
aggregate counts, and `check_anomalies` for deterministic anomaly rules
(stale doing-tasks, silent agents, task reassignment thrash, agents that never
complete what they claim). No LLM anywhere: every function is plain SQL plus
date arithmetic, so the same inputs always give the same output.

Phase B adds `instrumented()` — the wrapper server.py applies to every
registered tool — which samples latency/errors/memory into the companion
`agent_perf_samples` table, and `perf_summary()` to query it.

Phase C adds two more anomaly rules and `check_anomalies_and_notify()`, which
pushes new/re-triggered anomalies through `notify.notify` (iMessage) with a
cooldown-based dedupe ledger (`anomaly_notifications`) so the same finding
doesn't re-page on every poll.

Phase D adds the operational rules: db_size_growth (fed by `db_size_samples`,
recorded on the check_anomalies_and_notify cron path), approval_queue_age, and
bridge_disconnect (preflight of the iMessage push path — by construction this
one can only page once the bridge is back, but it stays visible in every
check_anomalies read).
"""

import functools
import inspect
import json
import math
import os
import plistlib
import re
import resource
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from . import (
    budgets,
    config,
    profiles,
    run_records,
    safety,
    scorecards,
    storage,
    tool_allowance,
)
from .delivery_slo import DEFAULT_WINDOW_DAYS as DELIVERY_SLO_WINDOW_DAYS_DEFAULT
from .delivery_slo import compute_delivery_slo
from .pr_models import rollup_check_name
from .tools import registry as registry_tools
from .tools.tasks import _CODE_TASK_MARKER_RX, _COMPLETION_REVIEW_RE

STALE_DOING_DAYS_DEFAULT = 5.0
SILENT_AGENT_DAYS_DEFAULT = 2.0
SILENT_AGENT_MIN_EVENTS_DEFAULT = 2
# Agent labels exempt from the silent_agent rule: registered agents that are
# healthy but journal only when the operator actually uses them (e.g. laptop agents
# he hasn't touched in a while). Without this they flag forever once past the
# 2-day threshold. Comma-separated, exact-match, default empty.
SILENT_AGENT_IGNORE_DEFAULT = ""
# Per-label silent thresholds for labels that journal on a slower cadence than
# the global window. `weekly-export-cron` journals only from the Sunday export,
# so at 2 days it was flagged five days of every week. Eight days still pages
# when a weekly run is missed, which an ignore entry would hide.
# Comma-separated `label=days`, exact match; malformed entries are skipped.
SILENT_AGENT_DAYS_BY_LABEL_DEFAULT = "weekly-export-cron=8"
REASSIGN_WINDOW_DAYS_DEFAULT = 7.0
REASSIGN_MIN_CLAIMS_DEFAULT = 3
NEVER_COMPLETES_MIN_CLAIMS_DEFAULT = 3
DB_GROWTH_WINDOW_DAYS_DEFAULT = 7.0
DB_GROWTH_MB_DEFAULT = 50.0
DB_SIZE_RETENTION_DAYS = 90.0
# Task #499: these track how long something has been WAITING, so they are
# calibrated to the cycle they observe, not to a fixed idea of "a while".
# At this repo's cadence a review that has sat for two hours is already an
# outlier worth surfacing; the previous 6h/12h values described a slower
# project and mostly reported yesterday's news. Both stay overridable via
# UA_ANOMALY_REVIEW_WAIT_HOURS / UA_ANOMALY_APPROVAL_AGE_HOURS.
APPROVAL_AGE_HOURS_DEFAULT = 2.0
REVIEW_WAIT_HOURS_DEFAULT = 2.0
ANOMALY_SERVICES_DEFAULT = "unified-agent,gbrain"
SERVICE_RESTARTS_MAX_DEFAULT = 3
SERVICE_CHECK_TIMEOUT_S = 5.0
HERMES_DIR_DEFAULT = "~/.hermes/hermes-agent"
HOST_LOW_MEM_PERCENT_DEFAULT = 15.0
MEMINFO_PATH_DEFAULT = "/proc/meminfo"
# How long a fired notification suppresses its own repeat. At 24h a condition
# that recurred the same working day stayed silent, which is the opposite of
# what a fast cycle needs. PERF_SUMMARY_WINDOW stays at 24h deliberately -- it
# is a reporting window over past samples, not a gate, and shortening it would
# just discard signal.
NOTIFY_COOLDOWN_HOURS_DEFAULT = 2.0
# Re-notify backoff ceiling: the gap doubles per page but never exceeds a day.
NOTIFY_COOLDOWN_MAX_HOURS = 24.0
PERF_SUMMARY_WINDOW_HOURS_DEFAULT = 24.0
_ACTIVITY_LIMIT_CAP = 1000

# review_wait (task #133): a task's notes documenting the annotation as prose
# — e.g. `` `[waiting-for: review]` `` inside backticks, describing the rule
# itself rather than marking real task state — must not trip the rule. The
# SQL LIKE pre-filter below has no anchoring/lookaround, so these compiled
# regexes are the authoritative check applied in Python; SQL only narrows
# the candidate set for query efficiency.
_REVIEW_WAIT_MARKER_RX = re.compile(r"(?<!`)\[waiting-for: review\](?!`)")
_REVIEW_OUTCOME_RX = re.compile(r"(?<!`)\[review:")

# unverified_done: a [code-task]-marked task reached 'done' with neither an
# evidence_ref nor a review-completion marker. tools/tasks.py's
# _evidence_required_error is the write-time gate that should prevent this
# going forward; this rule is the backfill/defense-in-depth net for rows that
# predate the gate, were written directly against the DB, or slipped through
# some other path — it is intentionally opt-in via the marker rather than
# automatic for every task, mirroring review_wait's posture. Once `domain`
# (task #372 Phase 1) lands, this should also fire automatically for every
# domain='hobby-builder' task, code-bearing by construction — not done here.
# _CODE_TASK_MARKER_RX now lives in tools/tasks.py beside the marker constant
# it compiles, and is imported above — task #467 gave it a second consumer.

# task_reassignment_thrash only pages for tasks that are still live. Once a task
# is terminal its claim churn is history, not an actionable thrash (task #1368);
# this subsumes the older per-task `[anomaly: understood]` acknowledgement.
_THRASH_TERMINAL_STATUSES = frozenset({"done", "cancelled"})

# A LIVE task whose churn has a known, already-explained cause still needs a way
# to stop paging (task #1412): #1368's terminal-status filter only covers
# finished tasks, and #1285 is legitimately still open, so it kept reporting
# critical for the rest of the window. The acknowledgement is therefore an
# explicit, attributed marker in the task's own notes:
#
#   [anomaly: understood task_reassignment_thrash @ 2026-09-25T21:30:00Z by Desktop_Agent5]
#
# Properties, each covered by a test: the rule name is required (an ack never
# silences a rule it does not name), the agent is required (an unattributed
# marker is not an acknowledgement), the timestamp is required and must parse
# AND must not be in the future (a malformed or typo'd marker must not hide an
# anomaly), and the ack covers only churn strictly before it — a claim in the
# same second is ambiguous, and ambiguity fails toward visibility. Re-acknowledging
# after fresh churn simply means appending a newer marker; the latest one wins.
# Like `[waiting-for: review]` and `[code-task]`, a backticked mention is a task
# *discussing* the marker, not carrying one, so the lookarounds exclude it.
# Terminal tasks are unaffected: the filter below runs first, so an ack can
# neither be applied to them nor change their state.
_THRASH_ACK_RX = re.compile(
    r"(?<!`)\[anomaly:\s*understood\s+(?P<rule>[a-z_]+)\s*@\s*(?P<ts>[^\s\]]+)"
    r"\s+by\s+(?P<agent>[^\s\]`][^\]`]*?)\s*\](?!`)"
)

# An acknowledgement dated in the future is a typo, not an acknowledgement:
# honouring it would cover every claim for as long as the typo stood, so it is
# disregarded with a small tolerance for clock skew between hosts.
_ACK_FUTURE_SKEW = timedelta(minutes=5)


def _parse_ack_ts(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 acknowledgement/claim timestamp, or None when unusable.

    Returning None (rather than guessing) keeps the caller paging: a marker whose
    timestamp we cannot read must fail toward visibility, never toward silence.
    """
    if not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _thrash_ack_suppresses(notes: str | None, last_claim_ts: str | None) -> bool:
    """True when `notes` acknowledge this rule strictly after the newest claim.

    The comparison against the most recent claim is what makes the marker
    time-boxed instead of permanent: churn recorded after the acknowledgement is
    not covered by it, so the anomaly returns on its own.
    """
    if not notes:
        return False
    deadline = datetime.now(UTC) + _ACK_FUTURE_SKEW
    acks = [
        parsed
        for match in _THRASH_ACK_RX.finditer(notes)
        if match.group("rule") == "task_reassignment_thrash"
        and (parsed := _parse_ack_ts(match.group("ts"))) is not None
        and parsed <= deadline
    ]
    if not acks:
        return False
    newest_claim = _parse_ack_ts(last_claim_ts)
    if newest_claim is None:
        # Nothing countable to page about; nothing for the ack to cover.
        return False
    # Strictly later: at second resolution a claim landing in the same second as
    # the acknowledgement cannot be told from churn the ack was written to cover,
    # so it keeps reporting.
    return max(acks) > newest_claim


def _env_positive_float(name: str, default: float) -> float:
    """Positive-float env threshold with a hard fallback: a misconfigured
    value must not disable or invert an anomaly rule."""
    try:
        v = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return v if v > 0 else default


def _usable_days(days: float) -> bool:
    """True when `days` is a positive window `_cutoff_iso` can represent.

    `inf`, `1e309` or `1e7` pass a plain `> 0` check but overflow `timedelta`
    or the datetime range, and that raise would abort every anomaly rule.
    """
    if not (math.isfinite(days) and days > 0):
        return False
    try:
        _cutoff_iso(days)
    except (OverflowError, ValueError):
        return False
    return True


def _env_days(name: str, default: float) -> float:
    days = _env_positive_float(name, default)
    return days if _usable_days(days) else default


def _env_positive_int(name: str, default: int) -> int:
    """Positive-int env threshold with a hard fallback."""
    try:
        v = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return v if v > 0 else default


def _silent_days_by_label() -> dict[str, float]:
    """Parse UA_ANOMALY_SILENT_AGENT_DAYS_BY_LABEL into {label: days}.

    Entries without `=`, with an empty label, or with a day count that is not
    a usable window (non-numeric, non-positive, infinite, or too large for a
    cutoff date) are skipped. The other labels keep their entries and the rule
    keeps running, so one bad value cannot stop the anomaly check.
    """
    raw = os.environ.get(
        "UA_ANOMALY_SILENT_AGENT_DAYS_BY_LABEL", SILENT_AGENT_DAYS_BY_LABEL_DEFAULT
    )
    out: dict[str, float] = {}
    for item in raw.split(","):
        label, sep, value = item.partition("=")
        label = label.strip()
        if not sep or not label:
            continue
        try:
            days = float(value)
        except ValueError:
            continue
        if _usable_days(days):
            out[label] = days
    return out


def _cutoff_iso(days: float) -> str:
    """UTC ISO timestamp `days` ago; comparable as text against storage.now()."""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def agent_activity(
    agent: str | None = None,
    since: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> dict:
    """Query the agent_events activity index, newest first.

    Filters (all optional, AND-combined): `agent` = exact agent label;
    `since` = ISO-8601 timestamp lower bound (e.g. '2026-07-10' or
    '2026-07-10T12:00:00+00:00'); `kind` = event kind ('task', 'memory',
    'journal'). `limit` defaults to 200, capped at 1000.

    Returns {events: [{id, agent, ts, kind, ref_id, summary}], truncated}.
    """
    if since is not None:
        try:
            datetime.fromisoformat(since)
        except ValueError:
            raise ToolError(
                f"since must be an ISO-8601 timestamp (e.g. 2026-07-10T00:00:00+00:00), got {since!r}"
            ) from None
    where, params = [], []
    if agent is not None:
        where.append("agent=?")
        params.append(agent)
    if since is not None:
        where.append("ts>=?")
        params.append(since)
    if kind is not None:
        where.append("kind=?")
        params.append(kind)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    capped = max(1, min(limit, _ACTIVITY_LIMIT_CAP))
    conn = storage.connect()
    try:
        rows = conn.execute(
            f"SELECT id, agent, ts, kind, ref_id, summary FROM agent_events {clause} "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            params + [capped + 1],
        ).fetchall()
    finally:
        conn.close()
    truncated = len(rows) > capped
    return {
        "events": [{k: r[k] for k in r.keys()} for r in rows[:capped]],
        "truncated": truncated,
    }


def metrics_snapshot() -> dict:
    """Deterministic health snapshot of the agent cluster's shared state.

    Returns {
      generated_at: ISO timestamp,
      events_last_7_days: {agent_label_or_'(unattributed)': {kind: count}},
      tasks_by_status: {open|doing|blocked|done: count},
      done_task_cycle_days: {count, avg, min, max} (created_at→completed_at,
        in days, over done tasks with a completed_at; zeros/None when none),
      db_size_bytes: size of memory.db,
    }.
    """
    week_cutoff = _cutoff_iso(7.0)
    conn = storage.connect()
    try:
        event_rows = conn.execute(
            "SELECT COALESCE(agent, '(unattributed)') AS agent, kind, COUNT(*) AS n "
            "FROM agent_events WHERE ts>=? GROUP BY 1, 2",
            (week_cutoff,),
        ).fetchall()
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
        done_rows = conn.execute(
            "SELECT created_at, completed_at FROM tasks "
            "WHERE status='done' AND completed_at IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    events_by_agent: dict[str, dict[str, int]] = {}
    for r in event_rows:
        events_by_agent.setdefault(r["agent"], {})[r["kind"]] = r["n"]

    cycle_days = []
    for r in done_rows:
        try:
            created = datetime.fromisoformat(r["created_at"])
            completed = datetime.fromisoformat(r["completed_at"])
        except (TypeError, ValueError):
            continue
        # Older/direct SQL writers may leave timestamps naive while normal
        # task writes use offset-aware ISO-8601 values. Treat naive storage
        # timestamps as UTC so one malformed row cannot blank the metrics pane.
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=UTC)
        cycle_days.append(max(0.0, (completed - created).total_seconds() / 86400.0))

    return {
        "generated_at": storage.now(),
        "events_last_7_days": events_by_agent,
        "tasks_by_status": {r["status"]: r["n"] for r in status_rows},
        "done_task_cycle_days": {
            "count": len(cycle_days),
            "avg": round(sum(cycle_days) / len(cycle_days), 3) if cycle_days else None,
            "min": round(min(cycle_days), 3) if cycle_days else None,
            "max": round(max(cycle_days), 3) if cycle_days else None,
        },
        "db_size_bytes": config.db_path().stat().st_size if config.db_path().exists() else 0,
    }


# --- Delivery SLO (task #1259, Epic C #1188) ---------------------------------
# Not a registered MCP tool: Epic B is shrinking the default tool count, and
# this reads the existing `dispatch_runs`/`tasks` stores cheaply enough that
# it doesn't need one — see webui/api.py's `delivery_slo_payload` (HTTP) and
# tools/vault.py's `vault_briefing_digest` (weekly briefing line) for the two
# read surfaces this feeds instead.

# No cached CI-check-run source exists in this hub yet: pr_watch.py's cursor
# tracks PR lifecycle events (opened/reviewed/merged), never per-run CI
# conclusions. Rather than invent a number, ci_green_rate stays "unavailable"
# unless an operator opts in.
CI_GREEN_RATE_PROVIDER_ENV = "UA_CI_GREEN_RATE_PROVIDER"
CI_GREEN_RATE_REPO_ENV = "UA_CI_GREEN_RATE_REPO"
CI_GREEN_RATE_CACHE_S_DEFAULT = 900.0
CI_GREEN_RATE_GH_TIMEOUT_S = 20.0
# (provider, repo, window_days) -> (monotonic_ts, runs, unavailable_reason).
# Failures are cached too, so a provider outage cannot make every HTTP read
# spend another 20 seconds waiting on `gh`.
_CI_GREEN_RATE_CACHE: dict[tuple[str, str, float], tuple[float, list[dict] | None, str | None]] = {}
_CI_GREEN_RATE_LOCK = threading.Lock()


def _ci_green_rate_runs(window_days: float) -> tuple[list[dict] | None, str | None]:
    """(runs, unavailable_reason) for `delivery_slo`'s CI green-rate metric.

    `runs=None` (unavailable_reason set) unless `UA_CI_GREEN_RATE_PROVIDER=gh`
    is explicitly set, in which case this shells out to `gh pr list` with a
    hard timeout and an in-process TTL cache (`UA_CI_GREEN_RATE_CACHE_S`,
    default 15 minutes). Concurrent cold calls may overlap, but warm reads
    use the cache without waiting on `gh`. Any failure (missing `gh`, no repo
    configured, timeout, bad JSON) degrades to unavailable with a reason;
    it never raises, and it never invents a number.
    """
    provider = os.environ.get(CI_GREEN_RATE_PROVIDER_ENV, "").strip().lower()
    if provider != "gh":
        return None, f"no CI data source configured (set {CI_GREEN_RATE_PROVIDER_ENV}=gh)"

    repo = os.environ.get(CI_GREEN_RATE_REPO_ENV, "").strip()
    if not repo:
        return None, f"{CI_GREEN_RATE_PROVIDER_ENV}=gh set but {CI_GREEN_RATE_REPO_ENV} is empty"

    ttl = _env_positive_float("UA_CI_GREEN_RATE_CACHE_S", CI_GREEN_RATE_CACHE_S_DEFAULT)
    key = (provider, repo, window_days)

    def cached_result(now: float) -> tuple[list[dict] | None, str | None] | None:
        entry = _CI_GREEN_RATE_CACHE.get(key)
        if entry is not None and (now - entry[0]) < ttl:
            return entry[1], entry[2]
        return None

    with _CI_GREEN_RATE_LOCK:
        cached = cached_result(time.monotonic())
        if cached is not None:
            return cached

    # A cold provider call may take 20 seconds. Do not hold the cache lock
    # across the subprocess: unrelated HTTP reads and briefing calls must
    # be able to inspect or fill the cache while it runs.
    runs, reason = _ci_green_rate_runs_uncached(window_days, repo)
    with _CI_GREEN_RATE_LOCK:
        cached = cached_result(time.monotonic())
        if cached is not None:
            return cached
        now = time.monotonic()
        # Keep independent windows warm and discard entries past the TTL.
        for stale_key, entry in list(_CI_GREEN_RATE_CACHE.items()):
            if now - entry[0] >= ttl:
                del _CI_GREEN_RATE_CACHE[stale_key]
        _CI_GREEN_RATE_CACHE[key] = (now, runs, reason)
    return runs, reason


def _ci_green_rate_runs_uncached(
    window_days: float, repo: str
) -> tuple[list[dict] | None, str | None]:
    cutoff = datetime.fromisoformat(_cutoff_iso(window_days))
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "all",
                "--search",
                f"updated:>={cutoff.date().isoformat()}",
                "--json",
                "number,isDraft,statusCheckRollup",
                "--limit",
                "1000",
            ],
            capture_output=True,
            text=True,
            timeout=CI_GREEN_RATE_GH_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"gh CI provider errored: {exc}"
    if proc.returncode != 0:
        return None, f"gh CI provider errored: {(proc.stderr or '').strip()[:200]}"
    try:
        prs = json.loads(proc.stdout or "[]")
    except ValueError:
        return None, "gh CI provider returned unparseable JSON"
    if not isinstance(prs, list):
        return None, "gh CI provider returned an unexpected response"
    if len(prs) >= 1000:
        return None, "gh CI provider reached its 1000-PR window coverage limit"

    runs: list[dict] = []
    for pr in prs:
        if not isinstance(pr, dict) or not isinstance(pr.get("isDraft"), bool):
            return None, "gh CI provider returned missing or invalid draft metadata"
        # Drafts deliberately lack full integration evidence. Their failed
        # full-verification gate blocks merging, not the ready-head green rate.
        if pr["isDraft"]:
            continue
        # A ready PR head is one sample. Ignore review-bot status contexts and
        # skipped checks; neither proves that substantive CI passed.
        checks = {}
        pending = False
        for i, check in enumerate(pr.get("statusCheckRollup") or []):
            if check.get("__typename") != "CheckRun":
                continue
            # GitHub Actions reports "test", Depot "CI / test" (#1787).
            name = rollup_check_name(check, i)
            if not name.startswith("CI / ") or name == "CI / changes":
                continue
            if str(check.get("status") or "").upper() != "COMPLETED":
                pending = True
                continue
            completed_at = check.get("completedAt")
            if not completed_at:
                continue
            try:
                completed = datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
            except ValueError:
                continue
            conclusion = str(check.get("conclusion") or "").upper()
            if conclusion in ("SKIPPED", "NEUTRAL"):
                continue
            previous = checks.get(name)
            if previous is None or completed > previous[0]:
                checks[name] = (completed, conclusion)
        if not pending and checks and any(completed >= cutoff for completed, _ in checks.values()):
            runs.append(
                {
                    "conclusion": "success"
                    if all(conclusion == "SUCCESS" for _, conclusion in checks.values())
                    else "failure"
                }
            )

    return runs, None


def delivery_slo(window_days: float = DELIVERY_SLO_WINDOW_DAYS_DEFAULT) -> dict:
    """Epic C (#1188) exit-test metrics: dispatch success, p90 cycle time,
    unattributed completions, CI green rate.

    Read-only over the *existing* `dispatch_runs` and `tasks` stores — never
    a new table. The computation itself is pure (see `delivery_slo.py`'s
    module docstring for the metric definitions, including why
    'retryable'/'refused' dispatch runs are excluded and why an unavailable
    metric reports a reason instead of inventing a number); this function is
    only the SQL-reading wrapper around it.
    """
    if window_days <= 0:
        window_days = DELIVERY_SLO_WINDOW_DAYS_DEFAULT
    now = datetime.now(UTC)
    cutoff = (now - timedelta(days=window_days)).isoformat(timespec="seconds")
    conn = storage.connect()
    try:
        dispatch_rows = conn.execute(
            "SELECT state, created_at FROM dispatch_runs WHERE created_at>=?",
            (cutoff,),
        ).fetchall()
        task_rows = conn.execute(
            "SELECT claimed_by, claimed_at, created_at, completed_at FROM tasks "
            "WHERE status='done' AND completed_at IS NOT NULL AND completed_at>=?",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    ci_runs, ci_reason = _ci_green_rate_runs(window_days)

    return compute_delivery_slo(
        dispatch_runs=[dict(r) for r in dispatch_rows],
        tasks=[dict(r) for r in task_rows],
        ci_runs=ci_runs,
        ci_unavailable_reason=ci_reason,
        window_days=window_days,
        now=now,
    )


# systemd unit names (doctor + UA_ANOMALY_SERVICES) → macOS LaunchAgent labels
# from deploy/mac-hub/*.plist. Unmapped units are Linux-only (timers, gbrain).
MAC_LAUNCHD_LABELS: dict[str, str] = {
    "unified-agent": "com.unified-agent.hub",
    "unified-agent.service": "com.unified-agent.hub",
    "unified-agent-executor": "com.unified-agent.executor-agy",
    "unified-agent-executor.service": "com.unified-agent.executor-agy",
    "unified-agent-zulip-worker": "com.unified-agent.zulip-worker",
    "unified-agent-zulip-worker.service": "com.unified-agent.zulip-worker",
    "ua-dispatch-tick.timer": "com.unified-agent.dispatch-tick",
    "ua-review-pipeline.timer": "com.unified-agent.review-pipeline",
    "ua-anomaly-check.timer": "com.unified-agent.anomaly-check",
    "ua-consolidate-proposal.timer": "com.unified-agent.consolidate-proposal",
    "ua-sleep-pass.timer": "com.unified-agent.sleep-pass",
    "ua-planning-loop.timer": "com.unified-agent.planning-loop",
    "ua-steward-audit.timer": "com.unified-agent.steward-audit",
    "cluster-briefing.timer": "com.unified-agent.cluster-briefing",
}


def launchd_label_for_unit(unit: str) -> str | None:
    """LaunchAgent label for a systemd-shaped unit name, or None if Linux-only."""
    if unit in MAC_LAUNCHD_LABELS:
        return MAC_LAUNCHD_LABELS[unit]
    if unit.startswith("com.unified-agent."):
        return unit
    return None


def _not_installed_props() -> dict[str, str]:
    return {"LoadState": "not-found", "ActiveState": "inactive", "NRestarts": "0"}


def _apple_weekday_to_python(weekday: int) -> int:
    """Apple StartCalendarInterval Weekday (0 or 7=Sunday, 1=Monday, ...) to
    Python's `date.weekday()` (0=Monday, ..., 6=Sunday)."""
    return (weekday - 1) % 7


def _calendar_spec_matches_date(spec: dict, day) -> bool:
    if "Month" in spec and day.month != spec["Month"]:
        return False
    if "Day" in spec and day.day != spec["Day"]:
        return False
    if "Weekday" in spec and day.weekday() != _apple_weekday_to_python(spec["Weekday"]):
        return False
    return True


def _next_calendar_occurrence(spec: dict, after: datetime) -> datetime | None:
    """Earliest datetime >= `after` matching one StartCalendarInterval spec.

    `after` and the returned value are naive datetimes in the SYSTEM's local
    timezone, matching launchd's own wall-clock semantics: StartCalendarInterval
    Hour/Minute fields are local time, not UTC, so comparing them against a
    UTC `after` would silently misjudge "overdue" by the host's UTC offset.

    Month/Day/Weekday/Hour/Minute are each optional wildcards per Apple's
    schema; only the fields present must match. Searches forward day by day,
    bounded to just past a year — every combination this repo's timers use
    (daily, weekly) repeats well within that window. Returns None if no match
    is found in that window (an unusual spec, e.g. Feb 30), AND if the spec's
    fields are the wrong type or out of range (TypeError/ValueError building
    the candidate) — the loaded job's on-disk plist can be edited or
    redeployed without a bootout+bootstrap cycle, so its content isn't
    guaranteed to be well-formed relative to what's actually running. Either
    way the caller treats the schedule as unreadable rather than guessing,
    never letting a malformed field abort the whole probe.
    """
    hour = spec.get("Hour", 0)
    minute = spec.get("Minute", 0)
    day = after.date()
    for _ in range(370):
        try:
            matches = _calendar_spec_matches_date(spec, day)
        except TypeError:
            return None
        if matches:
            try:
                candidate = datetime(day.year, day.month, day.day, hour, minute)
            except (TypeError, ValueError):
                return None
            if candidate >= after:
                return candidate
        day += timedelta(days=1)
    return None


def _read_launchd_schedule(plist_path: Path) -> tuple[str, object] | None:
    """(`"interval"`, seconds) or (`"calendar"`, spec-or-list) from a LaunchAgent
    plist, or None if the file is unreadable or declares neither key."""
    try:
        with plist_path.open("rb") as f:
            data = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return None
    if "StartInterval" in data:
        return ("interval", data["StartInterval"])
    if "StartCalendarInterval" in data:
        return ("calendar", data["StartCalendarInterval"])
    return None


def _first_fire_overdue(schedule: tuple[str, object], since: datetime, now: datetime) -> bool:
    """True if `schedule` guarantees at least one fire should have happened
    between `since` (the plist's install/last-edit time, our best proxy for
    "when this job started being scheduled") and `now`.

    Conservative on anything unrecognized or uncomputable: returns True (i.e.
    does NOT excuse a zero-runs job) rather than risk hiding a real
    misconfiguration behind a schedule we failed to parse.
    """
    kind, value = schedule
    if kind == "interval":
        try:
            return (now - since).total_seconds() >= float(value)
        except (TypeError, ValueError):
            return True
    if kind == "calendar":
        specs = value if isinstance(value, list) else [value]
        occurrences = [
            occ
            for occ in (
                _next_calendar_occurrence(spec, since) for spec in specs if isinstance(spec, dict)
            )
            if occ is not None
        ]
        if not occurrences:
            return True
        return min(occurrences) <= now
    return True


# A grace window on top of storage's first-seen anchor (see launchd_first_seen's
# schema comment) for the residual gap it can't close: the FIRST time this
# process ever observes a label loaded-with-zero-runs is only guaranteed to be
# AT OR AFTER its true bootstrap, never before — so if doctor's very first
# look at a label lands soon after an unrelated redeploy rewrote an
# already-long-broken job's plist, that single observation still starts the
# clock late. This window keeps that one-observation lag from immediately
# reading as a confident MISCONFIGURED; it cannot recur for the same label,
# since the anchor is never rewritten once recorded.
_LAUNCHD_ENABLE_GRACE = timedelta(days=3)


def _launchd_boot_time() -> datetime | None:
    """Last system boot as a naive local datetime, or None if unprobeable.

    launchd reloads every LaunchAgent at boot, resetting `runs` to 0 and
    `last exit code` to `(never exited)`, so a zero-runs observation can
    never be older than the current boot no matter how stale the first-seen
    anchor is — the 2026-09-19 steward-audit / cluster-briefing MISCONFIGURED
    false positive was exactly this: both timers were healthy, but the Mac
    had rebooted before their daily slots and their weeks-old anchors read
    as overdue (task #1178).

    `sysctl -n kern.boottime` prints `{ sec = <epoch>, usec = <n> }`; only the
    sec field is parsed. Naive local, matching _next_calendar_occurrence's
    wall-clock convention. None means the probe itself is unusable and the
    caller keeps the anchor as-is — the pre-clamp behavior.
    """
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            capture_output=True,
            text=True,
            timeout=SERVICE_CHECK_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r"sec\s*=\s*(\d+)", proc.stdout)
    if match is None:
        return None
    return datetime.fromtimestamp(int(match.group(1)))


def _launchd_zero_runs_status(label: str, plist_path_str: str | None) -> str:
    """ "active" (confidently not due yet), "unknown" (ambiguous — may be
    within one observation's worth of lag, may be broken), or "inactive"
    (overdue even after the grace window, or nothing to reason from) for a
    zero-runs LaunchAgent.

    A freshly-installed low-frequency (weekly/monthly) calendar-interval job
    reads identically to a broken one — `runs = 0`, `last exit code = (never
    exited)` — right up until its first trigger. Telling those apart needs
    the schedule and how long the job has existed; launchd exposes no
    "bootstrapped at" timestamp, and the plist's own mtime is unreliable in
    BOTH directions for this repo's deploy scripts — it can predate the
    actual `launchctl bootstrap` (install-then-enable-later flow), or
    postdate it when an unrelated redeploy rewrites the file without a
    bootout+bootstrap cycle, which would make a long-broken timer read as
    freshly installed. `storage.get/record_launchd_first_seen` instead
    anchors on the first time THIS process observed `label` loaded with zero
    runs — set once, never moved by a later, unrelated file rewrite.
    """
    plist_path = Path(plist_path_str) if plist_path_str else None
    schedule = _read_launchd_schedule(plist_path) if plist_path else None
    if schedule is None:
        return "inactive"
    now = datetime.now()  # naive, local — matches _next_calendar_occurrence
    since_str = storage.get_launchd_first_seen(label)
    if since_str is None:
        storage.record_launchd_first_seen(label, now.isoformat())
        return "active"  # first-ever observation: cannot be overdue yet
    try:
        since = datetime.fromisoformat(since_str)
    except ValueError:
        return "inactive"
    boot = _launchd_boot_time()
    if boot is not None and boot > since:
        # The runs counter this anchor describes was reset when launchd
        # reloaded the job at boot, so the pre-boot anchor can no longer
        # bound the current zero-runs window — clamp it. (A
        # bootout+bootstrap without a reboot resets the counter too, but
        # launchd exposes no bootstrap timestamp; that residual gap stays.)
        since = boot
    if not _first_fire_overdue(schedule, since, now):
        return "active"
    if not _first_fire_overdue(schedule, since + _LAUNCHD_ENABLE_GRACE, now):
        return "unknown"
    return "inactive"


def _launchd_probe(unit: str, timeout_s: float) -> dict[str, str] | None:
    """`launchctl print gui/$UID/<label>` mapped onto the systemd probe contract.

    LoadState/ActiveState/NRestarts stay the doctor and anomaly-rule vocabulary
    so Darwin does not need a second status model. A Linux-only unit (timers,
    gbrain, anything without a LaunchAgent) is `not-found` rather than a probe
    failure — "not installed on this host", not "systemctl missing".

    A mapped timer is expected to read `state = not running` between firings —
    that alone is not a failure signal for it the way it is for a persistent
    daemon. This covers both StartInterval (`run interval` in launchctl output)
    and StartCalendarInterval (`event triggers`). Such a job is `active` when
    its most recent run exited 0. When it has never run at all (`last exit
    code = (never exited)`), `ActiveState` becomes `active` if its first
    scheduled fire clearly hasn't happened yet, `unknown` if it's overdue but
    within this repo's install-then-enable-later grace window (ambiguous —
    see `_launchd_zero_runs_status`), and `inactive` only once genuinely
    overdue beyond that. Any other last exit code (a real, non-zero failure)
    stays `inactive` regardless of timing.

    A definitive launchctl service-not-found response maps to `not-found`;
    timeouts, unrelated errors, and malformed successful output stay None.
    """
    label = launchd_label_for_unit(unit)
    if label is None:
        return _not_installed_props()
    try:
        proc = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        error_lines = proc.stderr.strip().splitlines()
        if error_lines[:1] == ["Bad request."]:
            error_lines = error_lines[1:]
        missing_service = len(error_lines) == 1 and re.fullmatch(
            r'Could not find service "'
            + re.escape(label)
            + rf'" in domain(?: for user gui: {os.getuid()})?',
            error_lines[0],
        )
        if proc.returncode == 113 and missing_service and not proc.stdout:
            return _not_installed_props()
        return None
    state = None
    is_periodic = unit.endswith(".timer")
    last_exit_code = None
    plist_path = None
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("state = ") and state is None:
            raw = stripped.split("=", 1)[1].strip()
            state = "active" if raw == "running" else "inactive"
        elif stripped.startswith("run interval = "):
            is_periodic = True
        elif stripped.startswith("last exit code = ") and last_exit_code is None:
            last_exit_code = stripped.split("=", 1)[1].strip()
        elif stripped.startswith("path = ") and plist_path is None:
            plist_path = stripped.split("=", 1)[1].strip()
    if state is None:
        return None
    if state == "inactive" and is_periodic:
        if last_exit_code == "0":
            state = "active"
        elif last_exit_code in (None, "(never exited)"):
            zero_runs_status = _launchd_zero_runs_status(label, plist_path)
            if zero_runs_status != "inactive":
                state = zero_runs_status
    return {
        "LoadState": "loaded",
        "ActiveState": state,
        "NRestarts": "0",
        # Raw last-exit-code text ("0", "1", ..., or "(never exited)"/None),
        # kept alongside the derived ActiveState so a caller that cares about
        # *why* a periodic job is inactive (task #1484 — was it a real
        # failure, or did the job run and deliberately exit nonzero to flag
        # something?) does not have to re-parse `launchctl print` itself.
        "LastExitCode": last_exit_code,
    }


def _launchd_disabled_override(label: str, timeout_s: float) -> bool | None:
    """Read an exact launchd disabled override; return None when inconclusive.

    ``print-disabled`` is read-only. Its result establishes a retired job only
    when ``launchctl print`` also definitively reports that label absent.
    Other labels and malformed output cannot establish this label's override.
    """
    try:
        proc = subprocess.run(
            ["launchctl", "print-disabled", f"gui/{os.getuid()}"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or proc.stderr.strip() or len(proc.stdout) > 65536:
        return None
    target = re.compile(r'^\s*"' + re.escape(label) + r'"')
    entry = re.compile(r'^\s*"' + re.escape(label) + r'"\s*=>\s*(disabled|enabled)\s*$')
    # Count every exact-target record, including malformed ones, before parsing.
    records = [line for line in proc.stdout.splitlines() if target.match(line)]
    if len(records) != 1 or not (match := entry.fullmatch(records[0])):
        return None
    return match.group(1) == "disabled"


def _systemd_probe(unit: str, timeout_s: float) -> dict[str, str] | None:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=LoadState,ActiveState,NRestarts"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in {0, 1, 4}:
        return None
    props: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in {"LoadState", "ActiveState", "NRestarts"} or key in props:
            return None
        props[key] = value
    if not props.get("LoadState") or not props.get("ActiveState"):
        return None
    if props["LoadState"] == "not-found":
        # NRestarts belongs to services and is normally absent for timers.
        # An error diagnostic or retained runtime state cannot prove retirement.
        if (
            props["ActiveState"] != "inactive"
            or props.get("NRestarts", "0") != "0"
            or proc.stderr.strip() not in {"", f"Unit {unit} could not be found."}
        ):
            return None
    elif proc.returncode != 0:
        # Local v261.2 show returns zero for missing units. Retain narrowly
        # validated absence above for compatibility, never partial live output.
        return None
    return props


def _service_probe(unit: str, timeout_s: float) -> dict[str, str] | None:
    """Service liveness as `{LoadState, ActiveState, NRestarts}`, or None on probe failure.

    Linux: `systemctl --user show`. Darwin: `launchctl print` for units that
    have a LaunchAgent in `deploy/mac-hub/`; anything else is not-found.
    None means the probe binary itself is unusable (missing, timeout, no
    session); the caller skips the unit silently in that case. A successful
    call always returns a dict, even for a unit this host has never heard of
    (`LoadState='not-found'`) — the caller decides what to do with that.
    """
    if sys.platform == "darwin":
        return _launchd_probe(unit, timeout_s)
    return _systemd_probe(unit, timeout_s)


def _hermes_env_findings() -> list[dict]:
    """Deterministic probe of the hermes-agent install for the recurring
    "venv rebuilt without the `[mcp]` optional extra" silent fail-open.

    The install root comes from UA_ANOMALY_HERMES_DIR (default
    ~/.hermes/hermes-agent; empty string disables the rule). An absent root
    means "not deployed here" and stays silent. A present root with no venv,
    or a venv whose site-packages lacks the `mcp` package, is flagged —
    hermes drops its MCP integration without any error in either case.
    """
    raw = os.environ.get("UA_ANOMALY_HERMES_DIR", HERMES_DIR_DEFAULT)
    if not raw.strip():
        return []
    root = Path(raw).expanduser()
    if not root.is_dir():
        return []
    venv = root / "venv"
    if not venv.is_dir():
        return [
            {
                "rule": "hermes_env",
                "subject": "hermes:venv",
                "detail": (
                    f"hermes-agent install at {root} has no venv/ — hermes cannot run; "
                    "rebuild the venv (with the [mcp] extra) via its setup script"
                ),
            }
        ]
    site_dirs = list(venv.glob("lib/python*/site-packages"))
    site_dirs.append(venv / "Lib" / "site-packages")  # Windows venv layout
    if not any((d / "mcp").is_dir() for d in site_dirs):
        return [
            {
                "rule": "hermes_env",
                "subject": "hermes:venv",
                "detail": (
                    f"hermes-agent venv at {venv} has no `mcp` package — likely rebuilt "
                    "without the [mcp] optional extra, so hermes MCP integration fails "
                    "open (tools silently absent); reinstall with the [mcp] extra"
                ),
            }
        ]
    return []


def _host_memory_pressure() -> dict | None:
    """Parse MemTotal/MemAvailable out of /proc/meminfo (path overridable via
    UA_ANOMALY_MEMINFO_PATH, for tests — an empty override degrades to
    "unavailable" exactly like an unreadable file).

    None means the probe itself is unusable (non-Linux host with no procfs,
    permission error, or a malformed file missing either field) — the caller
    skips the rule silently, same posture as every other probe-based rule
    here (_service_probe, _hermes_env_findings).

    Motivated by the 2026-07-20 desktop freeze: free RAM had a 4-hour visible
    run-up (6.8 -> 3.1 GiB) in the logs before a snapd watchdog SIGABRT and a
    full reboot, and nothing was watching it.
    """
    path = os.environ.get("UA_ANOMALY_MEMINFO_PATH", MEMINFO_PATH_DEFAULT)
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        rest = rest.strip()
        if rest.endswith("kB"):
            try:
                values[key] = int(rest[:-2].strip())
            except ValueError:
                continue
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return None
    return {
        "total_kb": total,
        "available_kb": available,
        "percent_available": (available / total) * 100.0,
    }


def check_anomalies() -> dict:
    """Run the deterministic anomaly rules over tasks and agent_events.

    Rules (pure SQL + date arithmetic, no LLM):
      - stale_doing_task: a task with status='doing' whose updated_at is older
        than UA_ANOMALY_STALE_DOING_DAYS days (default 5).
      - silent_agent: an agent label with at least
        UA_ANOMALY_SILENT_AGENT_MIN_EVENTS journal events (default 2) whose
        latest 'journal'-kind event is older than UA_ANOMALY_SILENT_AGENT_DAYS
        days (default 2), AND that either claimed/entered 'doing' work in
        that window (or currently holds a doing claim) OR whose hub
        last_seen is also stale/missing. Idle executors that still heartbeat
        the hub without claimed work are reported once as idle_lane (info)
        instead — they are not silent failures. Tradeoff: a label below the
        journal floor is not flagged — labels that only ever appear on
        task/approval events (humans, one-off attributions) and labels
        emitted by a single tool run (`pi_worker`, from `pi_run_task`) never
        establish a cadence. Labels listed in
        UA_ANOMALY_SILENT_AGENT_IGNORE (comma-separated, exact match) are
        also skipped. UA_ANOMALY_SILENT_AGENT_DAYS_BY_LABEL (`label=days`,
        comma-separated; default `weekly-export-cron=8`) gives a label its
        own journal threshold in place of the global one.
      - idle_lane: companion to silent_agent — hub-alive, journal-stale,
        no claimed work in the window. Surfaced in digests as info; excluded
        from iMessage paging so daily briefings stay quiet.
      - review_wait: a task whose notes carry '[waiting-for: review]' but no
        '[review: …]' outcome yet, isn't done, and hasn't been updated in
        UA_ANOMALY_REVIEW_WAIT_HOURS hours (default 2) — the review
        pipeline/scan cron has likely stalled.
      - task_reassignment_thrash: a task that re-entered 'doing' at least
        UA_ANOMALY_REASSIGN_MIN_CLAIMS times (default 3) within the last
        UA_ANOMALY_REASSIGN_WINDOW_DAYS days (default 7) across 2+ agents —
        churn from being repeatedly claimed, dropped, and reclaimed. A single
        agent resuming its own task is only flagged at twice the threshold.
        Tasks whose current status is done/cancelled are skipped — their
        churn is history, not an actionable thrash.
      - agent_never_completes: an agent that has claimed (entered 'doing' on)
        at least UA_ANOMALY_NEVER_COMPLETES_MIN_CLAIMS distinct tasks (default
        3) none of which currently has status 'done'.
      - db_size_growth: memory.db grew more than UA_ANOMALY_DB_GROWTH_MB
        (default 50) over the last UA_ANOMALY_DB_GROWTH_WINDOW_DAYS (default
        7). Fed by db_size_samples, which only the check_anomalies_and_notify
        cron path records — silent until two samples span the window.
      - approval_queue_age: pending approval rows older than
        UA_ANOMALY_APPROVAL_AGE_HOURS (default 2), reported as one aggregate
        finding.
      - bridge_disconnect: the iMessage push path is expected here (bridge dir
        present or UA_BRIDGE_DIR set) but cannot work (send.ts missing, or bun
        not on PATH). Soft delivery failures return delivered=False without
        raising, so this is a preflight of the invariants, not a delivery
        probe; hosts without the bridge deployed stay silent.
      - service_down: a systemd --user unit named in UA_ANOMALY_SERVICES
        (comma-separated, default "unified-agent,gbrain") whose `systemctl
        --user show` ActiveState is not 'active'. Setting UA_ANOMALY_SERVICES
        to the empty string disables the rule.
      - service_crash_loop: one of those same units has restarted more than
        UA_ANOMALY_SERVICE_RESTARTS_MAX times (default 3), per that same
        probe's NRestarts property. Catches a unit stuck in a crash loop that
        happens to be momentarily 'active' when it's sampled, which
        service_down alone would miss.
        Both rules share a single per-unit probe (`systemctl --user show
        <unit> --property=LoadState,ActiveState,NRestarts`): systemctl
        missing, timing out (5s), or erroring unexpectedly (e.g. no systemd
        user session in CI) skips the unit silently — never raised, never a
        false finding. A unit systemd can't load (LoadState != 'loaded' —
        not installed on this host, e.g. a CI runner or a machine that never
        deployed it) is likewise skipped silently: absence of the unit means
        "not deployed here", not "down".
      - hermes_env: the hermes-agent install named in UA_ANOMALY_HERMES_DIR
        (default ~/.hermes/hermes-agent; empty string disables) exists but its
        venv is missing, or the venv's site-packages lacks the `mcp` package —
        the recurring "venv rebuilt without the [mcp] extra" failure, which
        hermes fails open on (MCP tools silently absent). Hosts without the
        install dir stay silent.
      - host_low_memory: available RAM (/proc/meminfo's MemAvailable, the
        "can actually be allocated" figure — not MemFree, which undercounts
        reclaimable cache/buffers) is below UA_ANOMALY_LOW_MEM_PERCENT percent
        of total (default 15). Hosts without /proc/meminfo (non-Linux, or a
        sandboxed environment without procfs) stay silent — the same
        skip-on-unusable-probe posture as service_down/hermes_env.
      - task_liveness: a digest entry the liveness sweep recorded in the
        last 24h (task #1749) for a task that is still unchanged and not
        terminal: no action path, exhausted self-pickup retries, or a monitor
        wake nobody could receive. Severity warning; listed in digests, never
        paged.
      - unverified_done: a task whose notes carry '[code-task]' reached
        status='done' with neither an evidence_ref nor a '[review: …]'/
        '[merged commit: …]' completion marker — opt-in via the marker, same
        posture as review_wait.

    Returns {anomalies: [{rule, subject, detail}]} — empty list when healthy.
    Subjects are 'task:<id>', 'agent:<label>', 'db:…', 'approvals:…',
    'bridge:…', 'service:<unit>', 'hermes:venv', or 'host:memory'.
    """
    stale_days = _env_days("UA_ANOMALY_STALE_DOING_DAYS", STALE_DOING_DAYS_DEFAULT)
    silent_days = _env_days("UA_ANOMALY_SILENT_AGENT_DAYS", SILENT_AGENT_DAYS_DEFAULT)
    silent_min_events = _env_positive_int(
        "UA_ANOMALY_SILENT_AGENT_MIN_EVENTS", SILENT_AGENT_MIN_EVENTS_DEFAULT
    )
    silent_ignore = {
        s.strip()
        for s in os.environ.get(
            "UA_ANOMALY_SILENT_AGENT_IGNORE", SILENT_AGENT_IGNORE_DEFAULT
        ).split(",")
        if s.strip()
    }
    silent_days_by_label = _silent_days_by_label()
    reassign_window_days = _env_days(
        "UA_ANOMALY_REASSIGN_WINDOW_DAYS", REASSIGN_WINDOW_DAYS_DEFAULT
    )
    reassign_min_claims = _env_positive_int(
        "UA_ANOMALY_REASSIGN_MIN_CLAIMS", REASSIGN_MIN_CLAIMS_DEFAULT
    )
    never_completes_min = _env_positive_int(
        "UA_ANOMALY_NEVER_COMPLETES_MIN_CLAIMS", NEVER_COMPLETES_MIN_CLAIMS_DEFAULT
    )
    db_growth_window_days = _env_days(
        "UA_ANOMALY_DB_GROWTH_WINDOW_DAYS", DB_GROWTH_WINDOW_DAYS_DEFAULT
    )
    db_growth_mb = _env_positive_float("UA_ANOMALY_DB_GROWTH_MB", DB_GROWTH_MB_DEFAULT)
    approval_age_hours = _env_positive_float(
        "UA_ANOMALY_APPROVAL_AGE_HOURS", APPROVAL_AGE_HOURS_DEFAULT
    )
    review_wait_hours = _env_positive_float(
        "UA_ANOMALY_REVIEW_WAIT_HOURS", REVIEW_WAIT_HOURS_DEFAULT
    )
    services_env = os.environ.get("UA_ANOMALY_SERVICES", ANOMALY_SERVICES_DEFAULT)
    services = [s.strip() for s in services_env.split(",") if s.strip()]
    service_restarts_max = _env_positive_int(
        "UA_ANOMALY_SERVICE_RESTARTS_MAX", SERVICE_RESTARTS_MAX_DEFAULT
    )
    low_mem_percent = _env_positive_float(
        "UA_ANOMALY_LOW_MEM_PERCENT", HOST_LOW_MEM_PERCENT_DEFAULT
    )
    anomalies = []
    conn = storage.connect()
    try:
        stale_cutoff = _cutoff_iso(stale_days)
        for r in conn.execute(
            "SELECT id, title, updated_at FROM tasks WHERE status='doing' AND updated_at<? "
            "ORDER BY updated_at",
            (stale_cutoff,),
        ).fetchall():
            anomalies.append(
                {
                    "rule": "stale_doing_task",
                    "subject": f"task:{r['id']}",
                    "detail": (
                        f"task {r['id']} ({r['title']!r}) has been 'doing' with no update "
                        f"since {r['updated_at']} (threshold {stale_days:g} days)"
                    ),
                }
            )

        # Only agents that have established a journaling cadence are held to
        # it. Two exemptions, both of which would otherwise be flagged forever:
        #   - Labels seen solely on task/approval events (humans, one-off
        #     attributions, garbage labels) — they never journal at all.
        #   - Labels with fewer than silent_min_events journals. A single
        #     journal is a tool invocation, not a daemon: `pi_run_task`
        #     attributes its one journal to `pi_worker` and nothing runs
        #     afterwards. You cannot be late to a cadence of one.
        # Truly inactive long-running agents are other rules' problem.
        silent_cutoff = _cutoff_iso(silent_days)
        # Hub-fresh = last_seen within the silent window (any via). Journal
        # bookkeeping and hub_request both update last_seen; once an agent goes
        # quiet, last_seen ages with them. Filtering on last_seen_via alone is
        # wrong: a later agent_event overwrites hub_request and would false-page
        # a still-alive executor (Devin on #751). Judged per label below, since
        # a label may carry its own window (UA_ANOMALY_SILENT_AGENT_DAYS_BY_LABEL).
        hub_last_seen = {
            row["label"]: row["last_seen"]
            for row in conn.execute(
                "SELECT label, last_seen FROM agents WHERE last_seen IS NOT NULL"
            ).fetchall()
        }
        # Prefer claimed_by / structured claims over every open->doing updater
        # (a coordinator moving someone else's task must not inherit had_work).
        had_work = {
            row["claimed_by"]
            for row in conn.execute(
                "SELECT DISTINCT claimed_by FROM tasks "
                "WHERE claimed_by IS NOT NULL AND status='doing'"
            ).fetchall()
        }
        had_work.update(
            row["claimed_by"]
            for row in conn.execute(
                "SELECT DISTINCT claimed_by FROM tasks "
                "WHERE claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
                "AND claimed_at>=?",
                (silent_cutoff,),
            ).fetchall()
        )
        # A per-label threshold may be longer or shorter than the global one,
        # so the SQL pre-filter uses the shortest and each row is judged
        # against its own cutoff below.
        label_cutoffs = {label: _cutoff_iso(days) for label, days in silent_days_by_label.items()}
        query_cutoff = max([silent_cutoff, *label_cutoffs.values()])
        for r in conn.execute(
            "SELECT agent, MAX(ts) AS last_journal FROM agent_events "
            "WHERE agent IS NOT NULL AND kind='journal' "
            "GROUP BY agent HAVING MAX(ts)<? AND COUNT(*)>=? ORDER BY agent",
            (query_cutoff, silent_min_events),
        ).fetchall():
            if r["agent"] in silent_ignore:
                continue
            agent = r["agent"]
            agent_days = silent_days_by_label.get(agent, silent_days)
            agent_cutoff = label_cutoffs.get(agent, silent_cutoff)
            if r["last_journal"] >= agent_cutoff:
                continue
            hub_fresh = hub_last_seen.get(agent, "") >= agent_cutoff
            if agent in had_work or not hub_fresh:
                anomalies.append(
                    {
                        "rule": "silent_agent",
                        "subject": f"agent:{agent}",
                        "detail": (
                            f"agent {agent!r} has no journal event since "
                            f"{r['last_journal']} (threshold {agent_days:g} days)"
                        ),
                    }
                )
            else:
                anomalies.append(
                    {
                        "rule": "idle_lane",
                        "severity": "info",
                        "subject": f"agent:{agent}",
                        "detail": (
                            f"agent {agent!r} is hub-alive but idle: no journal "
                            f"since {r['last_journal']} and no claimed work in "
                            f"the last {silent_days:g} days"
                        ),
                    }
                )

        review_wait_cutoff = _cutoff_iso(review_wait_hours / 24.0)
        for r in conn.execute(
            # SQL LIKE is a coarse pre-filter only — no anchoring, so it also
            # matches a backtick-wrapped documentation mention of the same
            # text. _REVIEW_WAIT_MARKER_RX/_REVIEW_OUTCOME_RX below are the
            # real (anchored) check.
            "SELECT id, title, notes, updated_at FROM tasks "
            "WHERE notes LIKE '%[waiting-for: review]%' "
            "AND status NOT IN ('done','cancelled') AND updated_at<? ORDER BY updated_at",
            (review_wait_cutoff,),
        ).fetchall():
            notes = r["notes"] or ""
            if not _REVIEW_WAIT_MARKER_RX.search(notes):
                continue  # only a documentation/prose mention, not a real state marker
            if _REVIEW_OUTCOME_RX.search(notes):
                continue  # a real [review: ...] outcome already exists
            anomalies.append(
                {
                    "rule": "review_wait",
                    "subject": f"task:{r['id']}",
                    "detail": (
                        f"task {r['id']} ({r['title']!r}) has been [waiting-for: review] "
                        f"with no [review: …] outcome since {r['updated_at']} "
                        f"(threshold {review_wait_hours:g}h) — check the review "
                        "pipeline/scan cron"
                    ),
                }
            )

        # Same rule, real status (task-lifecycle-contract.md §6 Q5): a task
        # created/moved directly into 'waiting_review' has no notes marker to
        # LIKE-match above, so it needs its own query rather than relying on
        # the marker pre-filter.
        for r in conn.execute(
            "SELECT id, title, notes, updated_at FROM tasks "
            "WHERE status='waiting_review' AND updated_at<? ORDER BY updated_at",
            (review_wait_cutoff,),
        ).fetchall():
            notes = r["notes"] or ""
            if _REVIEW_OUTCOME_RX.search(notes):
                continue  # a real [review: ...] outcome already exists
            anomalies.append(
                {
                    "rule": "review_wait",
                    "subject": f"task:{r['id']}",
                    "detail": (
                        f"task {r['id']} ({r['title']!r}) has status 'waiting_review' "
                        f"with no [review: …] outcome since {r['updated_at']} "
                        f"(threshold {review_wait_hours:g}h) — check the review "
                        "pipeline/scan cron"
                    ),
                }
            )

        for r in conn.execute(
            # SQL LIKE is a coarse pre-filter only, same posture as review_wait
            # above; _CODE_TASK_MARKER_RX/_REVIEW_OUTCOME_RX are the real checks.
            "SELECT id, title, notes, evidence_ref FROM tasks "
            "WHERE status='done' AND notes LIKE '%[code-task]%' "
            "AND (evidence_ref IS NULL OR evidence_ref='') ORDER BY id",
        ).fetchall():
            notes = r["notes"] or ""
            if not _CODE_TASK_MARKER_RX.search(notes):
                continue  # only a documentation/prose mention, not a real marker
            if _COMPLETION_REVIEW_RE.search(notes):
                continue  # a review-completion marker ([review: merged/...] or
                # [merged commit: ...]) is itself evidence
            anomalies.append(
                {
                    "rule": "unverified_done",
                    "subject": f"task:{r['id']}",
                    "detail": (
                        f"task {r['id']} ({r['title']!r}) is marked [code-task] and "
                        "'done' but carries no evidence_ref and no [review: …]/"
                        "[merged commit: …] marker — code-bearing completion is unverified"
                    ),
                }
            )

        reassign_cutoff = _cutoff_iso(reassign_window_days)
        for r in conn.execute(
            "SELECT e.ref_id, COUNT(*) AS claims, COUNT(DISTINCT e.agent) AS agents, "
            "t.status AS task_status, t.notes AS task_notes, MAX(e.ts) AS last_claim_ts "
            "FROM agent_events e LEFT JOIN tasks t ON t.id = e.ref_id "
            "WHERE e.kind='task' AND e.ref_id IS NOT NULL "
            "AND e.summary LIKE '%->doing%' AND e.ts>=? "
            "GROUP BY e.ref_id HAVING COUNT(*)>=? ORDER BY e.ref_id",
            (reassign_cutoff, reassign_min_claims),
        ).fetchall():
            # "Reassignment" means the task changed hands. One agent resuming
            # its own task (blocked -> doing, or re-claiming after a lease
            # release) is normal progress; only flag it when it flaps at twice
            # the threshold (task #1337).
            if r["agents"] < 2 and r["claims"] < 2 * reassign_min_claims:
                continue
            # A finished task's churn is no longer actionable: without this the
            # rule kept paging on done tasks for the rest of the window after
            # #1337 fixed the lease-renewal root cause (task #1368). Blocked,
            # open, doing and waiting_review tasks still fire.
            if r["task_status"] in _THRASH_TERMINAL_STATUSES:
                continue
            # A live task can be acknowledged in its own notes instead (task
            # #1412): the marker is rule-scoped and dated, so it covers only the
            # churn that already happened and a fresh claim re-pages.
            if _thrash_ack_suppresses(r["task_notes"], r["last_claim_ts"]):
                continue
            anomalies.append(
                {
                    "rule": "task_reassignment_thrash",
                    "subject": f"task:{r['ref_id']}",
                    "detail": (
                        f"task {r['ref_id']} re-entered 'doing' {r['claims']} times "
                        f"across {r['agents']} distinct agent(s) in the last "
                        f"{reassign_window_days:g} days (threshold {reassign_min_claims})"
                    ),
                }
            )

        claims_by_agent: dict[str, set[int]] = {}
        for r in conn.execute(
            "SELECT agent, ref_id FROM agent_events "
            "WHERE kind='task' AND agent IS NOT NULL AND summary LIKE '%->doing%'"
        ).fetchall():
            try:
                ref_id = int(r["ref_id"])
            except (TypeError, ValueError):
                continue
            claims_by_agent.setdefault(r["agent"], set()).add(ref_id)

        for agent_label in sorted(claims_by_agent):
            ref_ids = claims_by_agent[agent_label]
            if len(ref_ids) < never_completes_min:
                continue
            placeholders = ",".join("?" * len(ref_ids))
            done_count = conn.execute(
                f"SELECT COUNT(*) AS n FROM tasks WHERE id IN ({placeholders}) "
                "AND status IN ('done','cancelled')",
                tuple(ref_ids),
            ).fetchone()["n"]
            if done_count == 0:
                anomalies.append(
                    {
                        "rule": "agent_never_completes",
                        "subject": f"agent:{agent_label}",
                        "detail": (
                            f"agent {agent_label!r} has claimed {len(ref_ids)} distinct "
                            f"task(s) (threshold {never_completes_min}) with none marked done"
                        ),
                    }
                )

        size_rows = conn.execute(
            "SELECT bytes FROM db_size_samples WHERE ts>=? ORDER BY ts",
            (_cutoff_iso(db_growth_window_days),),
        ).fetchall()
        if len(size_rows) >= 2:
            growth_mb = (size_rows[-1]["bytes"] - size_rows[0]["bytes"]) / (1024.0 * 1024.0)
            if growth_mb > db_growth_mb:
                anomalies.append(
                    {
                        "rule": "db_size_growth",
                        "subject": "db:memory.db",
                        "detail": (
                            f"memory.db grew {growth_mb:.1f} MB over the last "
                            f"{db_growth_window_days:g} days (threshold {db_growth_mb:g} MB) "
                            "— consider retention/rollup before it crowds the brain"
                        ),
                    }
                )

        stale_approvals = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM approvals "
            "WHERE status='pending' AND created_at<?",
            (_cutoff_iso(approval_age_hours / 24.0),),
        ).fetchone()
        if stale_approvals["n"]:
            anomalies.append(
                {
                    "rule": "approval_queue_age",
                    "subject": "approvals:queue",
                    "detail": (
                        f"{stale_approvals['n']} pending approval(s) older than "
                        f"{approval_age_hours:g}h (oldest since {stale_approvals['oldest']}) "
                        "— review with approval_pending() / approval_resolve()"
                    ),
                }
            )

        # task_liveness (task #1749): the liveness sweep's digest entries — a task
        # with no action path, exhausted self-pickup retries, or a monitor wake
        # nobody could receive. Read-only; a failed read never breaks the rest.
        try:
            from .task_liveness import digest_findings

            anomalies.extend(digest_findings(conn))
        except Exception:
            pass

        # bridge_disconnect: preflight only — a broken bridge cannot page about
        # itself, but the finding stays visible to every check_anomalies caller
        # and pages as soon as delivery is restored. Deployments without the
        # bridge (default dir absent, UA_BRIDGE_DIR unset) stay silent: absence
        # of the whole project means "not deployed here", not "broken".
        try:
            from .tools.notify import _BRIDGE_DIR_ENV, _bridge_dir, _send_script

            expected = bool(os.environ.get(_BRIDGE_DIR_ENV)) or _bridge_dir().exists()
            if expected:
                script = _send_script()
                if not script.exists():
                    anomalies.append(
                        {
                            "rule": "bridge_disconnect",
                            "subject": "bridge:imessage",
                            "detail": (
                                f"imessage-bridge send script missing at {script} — push "
                                "delivery is down (fix UA_BRIDGE_DIR or restore the project)"
                            ),
                        }
                    )
                elif shutil.which("bun") is None:
                    anomalies.append(
                        {
                            "rule": "bridge_disconnect",
                            "subject": "bridge:imessage",
                            "detail": (
                                "bun executable not on PATH — send.ts cannot run; push "
                                "delivery is down"
                            ),
                        }
                    )
        except Exception:
            pass

        # service_down / service_crash_loop: systemd --user units named in
        # UA_ANOMALY_SERVICES. A unit can be 'active' while still restarting
        # under systemd's supervision, so both the current state and the
        # restart counter are checked from one probe per unit. A unit
        # systemd can't load (not installed on this host — e.g. a CI runner)
        # is skipped entirely: no findings, never an alarm for something
        # that was never deployed here.
        for unit in services:
            props = _service_probe(unit, SERVICE_CHECK_TIMEOUT_S)
            if props is None or props.get("LoadState") != "loaded":
                continue

            active_state = props.get("ActiveState")
            if active_state != "active":
                anomalies.append(
                    {
                        "rule": "service_down",
                        "subject": f"service:{unit}",
                        "detail": f"{unit} is {active_state}",
                    }
                )

            try:
                restarts = int(props.get("NRestarts", ""))
            except ValueError:
                restarts = None
            if restarts is not None and restarts > service_restarts_max:
                anomalies.append(
                    {
                        "rule": "service_crash_loop",
                        "subject": f"service:{unit}",
                        "detail": (
                            f"{unit} has restarted {restarts} times "
                            f"(threshold {service_restarts_max}) — possible crash loop"
                        ),
                    }
                )
    finally:
        conn.close()

    # hermes_env: pure filesystem probe, no DB needed.
    anomalies.extend(_hermes_env_findings())

    # host_low_memory: pure filesystem probe, no DB needed.
    mem = _host_memory_pressure()
    if mem is not None and mem["percent_available"] < low_mem_percent:
        anomalies.append(
            {
                "rule": "host_low_memory",
                "subject": "host:memory",
                "detail": (
                    f"only {mem['percent_available']:.1f}% RAM available "
                    f"({mem['available_kb'] // 1024} MiB of {mem['total_kb'] // 1024} MiB) "
                    f"— threshold {low_mem_percent:g}%"
                ),
            }
        )

    return {"anomalies": anomalies}


def record_db_size_sample() -> None:
    """Append the current memory.db on-disk size to db_size_samples; prune old rows.

    Called from check_anomalies_and_notify (the cron path) so plain
    check_anomalies stays read-only. Retention: DB_SIZE_RETENTION_DAYS
    (90 days ≈ 4.3k rows at the */30 cron cadence — negligible).
    """
    size = config.db_path().stat().st_size if config.db_path().exists() else 0
    conn = storage.connect()
    try:
        conn.execute("INSERT INTO db_size_samples (ts, bytes) VALUES (?, ?)", (storage.now(), size))
        conn.execute(
            "DELETE FROM db_size_samples WHERE ts<?", (_cutoff_iso(DB_SIZE_RETENTION_DAYS),)
        )
        conn.commit()
    finally:
        conn.close()


# --- phase B: perf instrumentation --------------------------------------------


def _rss_bytes() -> int | None:
    """Current process peak RSS in bytes, or None if unavailable.

    `ru_maxrss` is reported in kilobytes on Linux but bytes on macOS/BSD —
    normalize to bytes so `perf_summary` readings are platform-independent.
    """
    try:
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:
        return None
    return ru if sys.platform == "darwin" else ru * 1024


def instrumented(fn):
    """Wrap a tool callable with best-effort latency/error/memory sampling.

    Applied to every tool at MCP registration time (server.py), not to the
    bare functions tests call directly — unwrapped unit tests never populate
    `agent_perf_samples`. `functools.wraps` preserves `__name__`/`__doc__`/
    the signature (via `__wrapped__`) so MCPServer's schema introspection is
    unaffected.

    One row is appended to `agent_perf_samples` per call (subject to
    sampling or exclusion policies on OK samples; errors are always recorded,
    task #925): wall-clock latency_ms, ok/error, and a process RSS sample.
    Sampling failures (and the write itself, since `storage.record_perf_sample`
    never raises) can never affect the wrapped call's behavior, return value, or
    raised exception — this mirrors the `record_agent_event` best-effort contract
    from phase A.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            try:
                storage.record_perf_sample(
                    tool=fn.__name__,
                    latency_ms=(time.monotonic() - start) * 1000.0,
                    ok=False,
                    error=str(e),
                    rss_bytes=_rss_bytes(),
                )
            except Exception:
                pass
            raise
        try:
            storage.record_perf_sample(
                tool=fn.__name__,
                latency_ms=(time.monotonic() - start) * 1000.0,
                ok=True,
                rss_bytes=_rss_bytes(),
            )
        except Exception:
            pass
        return result

    return wrapper


should_record_perf_sample = storage.should_record_perf_sample


def perf_summary(since_hours: float = PERF_SUMMARY_WINDOW_HOURS_DEFAULT) -> dict:
    """Deterministic latency/error-rate/memory summary from agent_perf_samples.

    Aggregates instrumented tool calls over the last `since_hours` hours
    (default 24; non-positive values fall back to the default).

    Returns {
      window_hours: float,
      by_tool: {tool: {calls, errors, error_rate, avg_latency_ms,
        min_latency_ms, max_latency_ms}},
      memory_rss_bytes: {count, avg, min, max, latest} (bytes; None fields
        when no samples carried a memory reading in the window),
    }.
    """
    if since_hours <= 0:
        since_hours = PERF_SUMMARY_WINDOW_HOURS_DEFAULT
    cutoff = _cutoff_iso(since_hours / 24.0)
    conn = storage.connect()
    try:
        tool_rows = conn.execute(
            "SELECT tool, COUNT(*) AS calls, SUM(1-ok) AS errors, "
            "AVG(latency_ms) AS avg_latency_ms, MIN(latency_ms) AS min_latency_ms, "
            "MAX(latency_ms) AS max_latency_ms "
            "FROM agent_perf_samples WHERE ts>=? GROUP BY tool ORDER BY tool",
            (cutoff,),
        ).fetchall()
        mem_rows = conn.execute(
            "SELECT rss_bytes FROM agent_perf_samples "
            "WHERE ts>=? AND rss_bytes IS NOT NULL ORDER BY ts",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    by_tool = {}
    for r in tool_rows:
        calls = r["calls"]
        errors = r["errors"] or 0
        by_tool[r["tool"]] = {
            "calls": calls,
            "errors": errors,
            "error_rate": round(errors / calls, 4) if calls else 0.0,
            "avg_latency_ms": (
                round(r["avg_latency_ms"], 3) if r["avg_latency_ms"] is not None else None
            ),
            "min_latency_ms": (
                round(r["min_latency_ms"], 3) if r["min_latency_ms"] is not None else None
            ),
            "max_latency_ms": (
                round(r["max_latency_ms"], 3) if r["max_latency_ms"] is not None else None
            ),
        }

    rss_values = [r["rss_bytes"] for r in mem_rows]
    memory_rss_bytes = {
        "count": len(rss_values),
        "avg": round(sum(rss_values) / len(rss_values), 1) if rss_values else None,
        "min": min(rss_values) if rss_values else None,
        "max": max(rss_values) if rss_values else None,
        "latest": rss_values[-1] if rss_values else None,
    }

    return {
        "window_hours": since_hours,
        "by_tool": by_tool,
        "memory_rss_bytes": memory_rss_bytes,
    }


# --- phase C: anomaly push delivery -------------------------------------------


def _backoff_cooldown_hours(base_hours: float, notify_count: int) -> float:
    """Cooldown after `notify_count` pages: base, 2x, 4x, ... capped at 24 h.

    The cap never drops below the base, so a base above 24 h stays flat.
    """
    exponent = max(int(notify_count or 1) - 1, 0)
    # Clamp the exponent so a corrupted huge count cannot build a giant float.
    scaled = base_hours * (2 ** min(exponent, 16))
    return min(scaled, max(NOTIFY_COOLDOWN_MAX_HOURS, base_hours))


def check_anomalies_and_notify(
    cooldown_hours: float = NOTIFY_COOLDOWN_HOURS_DEFAULT,
) -> dict:
    """Run check_anomalies() and push new/re-triggered anomalies to the operator's phone.

    De-duplication: each (rule, subject) pair is tracked in the
    `anomaly_notifications` companion table with its last-notified timestamp.
    A pair already notified within its cooldown is not re-sent — this is a
    periodic digest, not a per-poll spam channel. The base cooldown is
    `cooldown_hours` (UA_ANOMALY_NOTIFY_COOLDOWN_HOURS, default 2;
    non-positive values fall back to the default) and it doubles with each
    re-notify of a still-active pair (base, 2x, 4x, ...), capped at 24 h, so a
    persistent finding pages less and less often (`notify_count` column). An anomaly that has cleared since the last check
    has its tracking row removed, so if the same (rule, subject) recurs later
    it notifies immediately, regardless of the old cooldown.

    Delivery is best-effort via `notify.notify` (iMessage): a delivery
    failure is swallowed and reported in `notify_error`, never raised —
    anomaly detection itself must never break because paging failed.

    Returns {anomalies, notified: bool, newly_notified: [{rule, subject}],
    notify_error: str | None}.
    """
    cooldown_hours = _env_positive_float("UA_ANOMALY_NOTIFY_COOLDOWN_HOURS", cooldown_hours)
    # Feed the db_size_growth rule: this (the cron path) is the designated
    # sample recorder. Best-effort — sampling must never break anomaly checks.
    try:
        record_db_size_sample()
    except Exception:
        pass
    anomalies = check_anomalies()["anomalies"]
    active_keys = {(a["rule"], a["subject"]) for a in anomalies}

    conn = storage.connect()
    try:
        # Prune cleared anomalies so a recurrence notifies fresh next time.
        tracked = conn.execute("SELECT rule, subject FROM anomaly_notifications").fetchall()
        for t in tracked:
            if (t["rule"], t["subject"]) not in active_keys:
                conn.execute(
                    "DELETE FROM anomaly_notifications WHERE rule=? AND subject=?",
                    (t["rule"], t["subject"]),
                )
        conn.commit()

        to_notify = []
        for a in anomalies:
            # idle_lane is digest-info only — do not page the operator for healthy
            # idle executors that still heartbeat the hub. task_liveness entries
            # are digest entries by design (task #1749), not pages.
            if a["rule"] in ("idle_lane", "task_liveness"):
                continue
            row = conn.execute(
                "SELECT last_notified_at, notify_count FROM anomaly_notifications "
                "WHERE rule=? AND subject=?",
                (a["rule"], a["subject"]),
            ).fetchone()
            if row is None:
                to_notify.append(a)
                continue
            gap_hours = _backoff_cooldown_hours(cooldown_hours, row["notify_count"])
            if row["last_notified_at"] < _cutoff_iso(gap_hours / 24.0):
                to_notify.append(a)

        ts = storage.now()
        for a in to_notify:
            conn.execute(
                "INSERT INTO anomaly_notifications "
                "(rule, subject, first_seen, last_notified_at, notify_count) "
                "VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(rule, subject) DO UPDATE SET "
                "last_notified_at=excluded.last_notified_at, "
                "notify_count=anomaly_notifications.notify_count + 1",
                (a["rule"], a["subject"], ts, ts),
            )
        conn.commit()
    finally:
        conn.close()

    notified = False
    notify_error = None
    if to_notify:
        lines = [f"- [{a['rule']}] {a['detail']}" for a in to_notify]
        message = f"unified-agent: {len(to_notify)} anomaly alert(s)\n" + "\n".join(lines)
        try:
            from .tools.notify import internal_route, notify

            with internal_route("anomaly_alert"):
                notify_result = notify(message)
            notified = bool(notify_result.get("delivered", False))
        except Exception as e:
            notify_error = str(e)[:200]

        # Best-effort: every detection should be inspectable in the vault
        # afterward, but a journal-write failure must never mask the alert
        # itself — notified/notify_error above are already set regardless.
        try:
            from .tools.vault import vault_journal

            vault_journal(
                entry=(
                    f"Anomaly check: {len(to_notify)} new/re-triggered finding(s) — "
                    + "; ".join(f"[{a['rule']}] {a['subject']}" for a in to_notify)
                ),
                agent="unified-agent-cron",
                tags=["anomaly", "automated"],
            )
        except Exception:
            pass

    return {
        "anomalies": anomalies,
        "notified": notified,
        "newly_notified": [{"rule": a["rule"], "subject": a["subject"]} for a in to_notify],
        "notify_error": notify_error,
    }


_OBSERVABILITY_ACTIONS = {
    "activity": agent_activity,
    "metrics": metrics_snapshot,
    "anomalies": check_anomalies,
    "performance": perf_summary,
    "anomalies_notify": check_anomalies_and_notify,
    "heartbeat": registry_tools.agent_heartbeat,
    "agents": registry_tools.agent_list,
    "context": registry_tools.agent_context,
    "runs_in_flight": run_records.runs_in_flight,
    "run_history": run_records.run_history,
    "run_coverage": run_records.run_coverage,
    "cost_rollup": budgets.cost_rollup,
    "budget_status": budgets.budget_status,
    "tool_allowance": tool_allowance.tool_allowance,
    "scorecard": scorecards.agent_scorecard,
    "eval_suites": scorecards.eval_suites,
    "eval_record": scorecards.eval_record,
}


def observability_manage(action: str, params: dict | None = None) -> dict:
    """Dispatch agent visibility, metrics, anomaly, and performance operations.

    ``action`` is one of activity, metrics, anomalies, performance,
    anomalies_notify, heartbeat, agents, context, runs_in_flight, run_history,
    run_coverage, cost_rollup, budget_status, tool_allowance, scorecard,
    eval_suites or eval_record.
    The three run actions read the self-pickup run record (task #1436,
    ``run_records``); the cost actions use ``budgets`` (task #1348); scorecard
    and eval actions use ``scorecards`` (task #1354). The notification-capable
    ``anomalies_notify`` action remains
    independently profile-gated through ``check_anomalies_and_notify``;
    read-only observability does not grant notification access.
    Legacy tools remain registered for stdio and older clients.
    """
    if not isinstance(action, str):
        raise ToolError("action must be a string")
    action = action.strip().lower()
    fn = _OBSERVABILITY_ACTIONS.get(action)
    if fn is None:
        raise ToolError(f"action must be one of {tuple(_OBSERVABILITY_ACTIONS)}")
    if not profiles.consolidated_action_is_allowed("observability_manage", fn.__name__):
        raise ToolError(f"observability action {action!r} is not exposed by the active profile")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ToolError("params must be an object")
    try:
        bound = inspect.signature(fn).bind(**params)
    except TypeError as exc:
        raise ToolError(f"invalid params for observability action {action!r}: {exc}") from exc
    safety.check_policy_gate(fn.__name__, bound.arguments)
    return fn(*bound.args, **bound.kwargs)
