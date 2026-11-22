"""Observed quota / lane-availability projection (task #1081).

Truth comes from local observations only:

* classified dispatch/executor failure reasons
* Codex CLI rollout ``rate_limits`` JSONL under ``~/.codex/sessions``

Never invents numbers, never probes a provider to measure usage, and never
treats "no observations" as available.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, storage

# A plan or account usage ceiling, as opposed to a transient per-minute rate
# limit (task #1841). Only this marks a run's capacity as limit_reached.
from .plan_usage_limit import is_plan_usage_limit as is_plan_usage_limit

FAILURE_PROVIDER_QUOTA_EXHAUSTED = "provider_quota_exhausted"
FAILURE_ITERATION_BUDGET_TIMEOUT = "iteration_budget_timeout"
FAILURE_ZERO_OUTPUT_ATTEMPT = "zero_output_attempt"
FAILURE_AGY_TEARDOWN_RESUME_EXHAUSTED = "agy_teardown_resume_exhausted"
FAILURE_CODE_ERROR = "code_error"

FAILURE_CLASSES = frozenset(
    {
        FAILURE_PROVIDER_QUOTA_EXHAUSTED,
        FAILURE_ITERATION_BUDGET_TIMEOUT,
        FAILURE_ZERO_OUTPUT_ATTEMPT,
        FAILURE_AGY_TEARDOWN_RESUME_EXHAUSTED,
        FAILURE_CODE_ERROR,
    }
)

# Fleet subscription lanes always projected, even when credential_pool is empty.
FLEET_LANES: tuple[dict[str, str], ...] = (
    {"lane": "codex", "provider": "codex", "label": "codex"},
    {"lane": "claude", "provider": "anthropic", "label": "claude"},
    {"lane": "agy", "provider": "gemini", "label": "agy"},
    {"lane": "cursor", "provider": "cursor", "label": "cursor"},
)

# Task #1841: Claude Code's plan-limit messages ("Claude AI usage limit
# reached|<epoch>", "5-hour limit reached ∙ resets 3am", "You've hit your
# limit · resets 10am", "Weekly limit reached") are the same exhaustion.
_QUOTA_EXHAUSTED_RE = re.compile(
    r"(?:usage\s+limit\s+(?:has\s+been\s+)?reached|quota\s+exhausted|provider\s+quota|"
    r"rate[\s_-]?limit\s+reached|"
    r"\b(?:\d+[\s-]?hour|session|weekly|daily|opus|sonnet)\s+limit\s+reached|"
    r"hit\s+your\s+(?:usage\s+)?limit)",
    re.IGNORECASE,
)
_ITERATION_BUDGET_RE = re.compile(r"iteration\s+budget", re.IGNORECASE)
_ZERO_OUTPUT_RE = re.compile(r"zero_output_attempt", re.IGNORECASE)
_AGY_TEARDOWN_RESUME_EXHAUSTED_RE = re.compile(r"agy_teardown_resume_exhausted", re.IGNORECASE)

_CODEX_SESSIONS_ENV = "UA_CODEX_SESSIONS_ROOT"

# Quota-exhaustion evidence has a freshness window. Subscription and rate-limit
# exhaustion is transient (hourly/weekly resets), so a provider_quota_exhausted
# row or a codex rate_limits sample only keeps a lane unavailable while fresh.
# Stale evidence degrades the lane back to availability=unknown — which the
# dispatch quota gate treats as "no data", never a block. Env-tunable for hosts
# whose providers reset on slower cadences.
QUOTA_EXHAUSTION_TTL_ENV = "UA_QUOTA_EXHAUSTION_TTL_S"
QUOTA_EXHAUSTION_TTL_DEFAULT_S = 24 * 3600.0


def quota_exhaustion_ttl_s() -> float:
    raw = (os.environ.get(QUOTA_EXHAUSTION_TTL_ENV) or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return QUOTA_EXHAUSTION_TTL_DEFAULT_S


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _observation_is_fresh(observed_at, *, now: datetime) -> bool:
    ts = _parse_ts(observed_at)
    if ts is None:
        return False
    return (now - ts).total_seconds() <= quota_exhaustion_ttl_s()


def classify_failure_reason(text: str | None) -> str:
    """Classify a recorded failure string into a finite failure_class.

    Distinguishes provider quota exhaustion from Hermes-side iteration-budget
    caps and ordinary code/process errors. Empty text is still a code error:
    a failed outcome without a reason is not quota evidence.
    """
    raw = text or ""
    if _QUOTA_EXHAUSTED_RE.search(raw):
        return FAILURE_PROVIDER_QUOTA_EXHAUSTED
    if _ITERATION_BUDGET_RE.search(raw):
        return FAILURE_ITERATION_BUDGET_TIMEOUT
    if _ZERO_OUTPUT_RE.search(raw):
        return FAILURE_ZERO_OUTPUT_ATTEMPT
    if _AGY_TEARDOWN_RESUME_EXHAUSTED_RE.search(raw):
        return FAILURE_AGY_TEARDOWN_RESUME_EXHAUSTED
    return FAILURE_CODE_ERROR


def lane_for_agent(agent_label: str | None) -> str:
    """Map an agent label onto a fleet subscription lane."""
    family = config.agent_family(agent_label)
    if family is not None:
        lane = family.lower()
        if lane in {"claude", "codex", "agy", "cursor", "hermes", "copilot"}:
            return lane
    label = (agent_label or "").strip().lower()
    if "claude" in label:
        return "claude"
    if "codex" in label:
        return "codex"
    if "agy" in label or "antigravity" in label or "gemini" in label:
        return "agy"
    if "cursor" in label:
        return "cursor"
    if "hermes" in label:
        return "hermes"
    if "copilot" in label:
        return "copilot"
    if "grok" in label:
        return "grok"
    return "unknown"


def codex_sessions_root() -> Path:
    raw = (os.environ.get(_CODEX_SESSIONS_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex" / "sessions"


def _extract_rate_limits(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    payload = obj.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("rate_limits"), dict):
        return payload["rate_limits"]
    if isinstance(obj.get("rate_limits"), dict):
        return obj["rate_limits"]
    return None


def _meter_from_event(
    *,
    rate_limits: dict[str, Any],
    observed_at: str | None,
    source_path: Path,
) -> dict[str, Any] | None:
    primary = rate_limits.get("primary")
    if not isinstance(primary, dict):
        return None
    used = primary.get("used_percent")
    window = primary.get("window_minutes")
    if not isinstance(used, (int, float)):
        return None
    # resets_in_seconds is null in every verified sample (and live rollouts
    # often carry resets_at instead). Never invent a value from resets_at.
    resets = primary.get("resets_in_seconds")
    if not isinstance(resets, (int, float)):
        resets = None
    secondary = rate_limits.get("secondary")
    secondary_used = None
    if isinstance(secondary, dict) and isinstance(secondary.get("used_percent"), (int, float)):
        secondary_used = float(secondary["used_percent"])
    return {
        "limit_id": rate_limits.get("limit_id") or "codex",
        "plan_type": rate_limits.get("plan_type"),
        "used_percent": float(used),
        "window_minutes": int(window) if isinstance(window, (int, float)) else None,
        "resets_in_seconds": resets,
        "secondary_used_percent": secondary_used,
        "rate_limit_reached_type": rate_limits.get("rate_limit_reached_type"),
        "observed_at": observed_at,
        "source_path": str(source_path),
    }


def read_codex_rate_limits(sessions_root: Path | str | None = None) -> dict[str, Any] | None:
    """Read the newest Codex rollout ``rate_limits`` sample from local JSONL.

    Read-only. Does not contact OpenAI/ChatGPT. Returns ``None`` when no
    usable sample exists — absent data is not zero.
    """
    root = Path(sessions_root) if sessions_root is not None else codex_sessions_root()
    if not root.is_dir():
        return None

    best: dict[str, Any] | None = None
    best_key: tuple[str, str] = ("", "")  # (timestamp, path) for stable newest

    for path in sorted(root.rglob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or "rate_limits" not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            rate_limits = _extract_rate_limits(obj)
            if rate_limits is None:
                continue
            observed_at = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None
            meter = _meter_from_event(
                rate_limits=rate_limits,
                observed_at=observed_at,
                source_path=path,
            )
            if meter is None:
                continue
            key = (observed_at or "", str(path))
            if best is None or key >= best_key:
                best = meter
                best_key = key
    return best


def latest_quota_exhaustion_for_lane(lane: str) -> dict[str, Any] | None:
    """Newest provider-quota-exhausted observation for a fleet lane, if any."""
    conn = storage.connect_read_only()
    try:
        dispatch_row = conn.execute(
            "SELECT agent_label, failure_reason, failure_class, updated_at "
            "FROM dispatch_runs "
            "WHERE failure_class=? "
            "ORDER BY updated_at DESC, run_id DESC LIMIT 50",
            (FAILURE_PROVIDER_QUOTA_EXHAUSTED,),
        ).fetchall()
        attempt_row = conn.execute(
            "SELECT agent, detail, failure_class, finished_at "
            "FROM executor_attempts "
            "WHERE failure_class=? "
            "ORDER BY finished_at DESC, id DESC LIMIT 50",
            (FAILURE_PROVIDER_QUOTA_EXHAUSTED,),
        ).fetchall()
    finally:
        conn.close()

    candidates: list[dict[str, Any]] = []
    for row in dispatch_row:
        if lane_for_agent(row["agent_label"]) != lane:
            continue
        candidates.append(
            {
                "source": "dispatch_runs.failure_class",
                "observed_at": row["updated_at"],
                "evidence": row["failure_reason"],
                "failure_class": row["failure_class"],
                "agent_label": row["agent_label"],
            }
        )
    for row in attempt_row:
        if lane_for_agent(row["agent"]) != lane:
            continue
        candidates.append(
            {
                "source": "executor_attempts.failure_class",
                "observed_at": row["finished_at"],
                "evidence": row["detail"],
                "failure_class": row["failure_class"],
                "agent_label": row["agent"],
            }
        )
    if not candidates:
        return None
    candidates.sort(key=lambda c: c.get("observed_at") or "", reverse=True)
    return candidates[0]


def _unknown_row(
    *,
    lane: str,
    provider: str,
    label: str,
    checked_at: str,
) -> dict[str, Any]:
    return {
        "lane": lane,
        "provider": provider,
        "label": label,
        "availability": "unknown",
        "remaining": "unknown",
        "source": "no source",
        "observed_at": None,
        "last_checked": checked_at,
        "evidence": None,
        "failure_class": None,
    }


def _unavailable_from_observation(
    *,
    lane: str,
    provider: str,
    label: str,
    checked_at: str,
    obs: dict[str, Any],
) -> dict[str, Any]:
    return {
        "lane": lane,
        "provider": provider,
        "label": label,
        "availability": "unavailable",
        "remaining": "unknown",
        "source": obs["source"],
        "observed_at": obs.get("observed_at"),
        "last_checked": checked_at,
        "evidence": obs.get("evidence"),
        "failure_class": obs.get("failure_class"),
    }


def _stale_row(
    *,
    lane: str,
    provider: str,
    label: str,
    checked_at: str,
    obs: dict[str, Any],
) -> dict[str, Any]:
    """Aged-out exhaustion evidence: unknown availability, provenance kept."""
    evidence = obs.get("evidence")
    return {
        "lane": lane,
        "provider": provider,
        "label": label,
        "availability": "unknown",
        "remaining": "unknown",
        "source": obs["source"],
        "observed_at": obs.get("observed_at"),
        "last_checked": checked_at,
        "evidence": f"stale (>{quota_exhaustion_ttl_s():.0f}s old): {evidence}"
        if evidence
        else "stale exhaustion observation",
        "failure_class": None,
        "stale": True,
    }


def _from_codex_meter(meter: dict[str, Any], *, checked_at: str) -> dict[str, Any]:
    used = float(meter["used_percent"])
    reached = meter.get("rate_limit_reached_type")
    exhausted = used >= 100.0 or reached not in (None, "")
    availability = "unavailable" if exhausted else "available"
    return {
        "lane": "codex",
        "provider": "codex",
        "label": "codex",
        "availability": availability,
        "remaining": "unknown" if exhausted else "metered",
        "source": "codex_rollout_rate_limits",
        "observed_at": meter.get("observed_at"),
        "last_checked": checked_at,
        "used_percent": used,
        "window_minutes": meter.get("window_minutes"),
        "resets_in_seconds": None,  # always UNKNOWN per verified samples
        "plan_type": meter.get("plan_type"),
        "source_path": meter.get("source_path"),
        "evidence": (
            f"codex rate_limits used_percent={used} window_minutes={meter.get('window_minutes')}"
        ),
        "failure_class": FAILURE_PROVIDER_QUOTA_EXHAUSTED if exhausted else None,
    }


def _fleet_row(
    *,
    lane: str,
    provider: str,
    label: str,
    checked_at: str,
    codex_meter: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """Project one fleet lane: fresh exhaustion evidence blocks; stale is unknown."""
    try:
        obs = latest_quota_exhaustion_for_lane(lane)
    except (storage.LiveStoreError, sqlite3.Error):
        # Missing classified observations cannot erase independently known
        # fresh meter exhaustion. Headroom cannot establish availability
        # without the metadata; stale exhaustion still ages out normally.
        if lane == "codex" and codex_meter is not None:
            row = _from_codex_meter(codex_meter, checked_at=checked_at)
            meter_fresh = _observation_is_fresh(row.get("observed_at"), now=now)
            row["stale"] = not meter_fresh
            if not meter_fresh or not row.get("failure_class"):
                exhausted = bool(row.get("failure_class"))
                row.update(availability="unknown", remaining="unknown", failure_class=None)
                if exhausted:
                    row["evidence"] = (
                        f"stale (>{quota_exhaustion_ttl_s():.0f}s old): {row['evidence']}"
                    )
            row["evidence"] += "; quota observation metadata unavailable"
        else:
            row = _unknown_row(lane=lane, provider=provider, label=label, checked_at=checked_at)
        row["metadata_error"] = "quota observation metadata unavailable"
        return row
    if lane == "codex" and codex_meter is not None:
        row = _from_codex_meter(codex_meter, checked_at=checked_at)
        # Fresh exhaustion evidence (dispatch/executor failure) beats the
        # meter sample — it is the newer observation.
        if obs is not None and _observation_is_fresh(obs.get("observed_at"), now=now):
            return _unavailable_from_observation(
                lane=lane, provider=provider, label=label, checked_at=checked_at, obs=obs
            )
        meter_fresh = _observation_is_fresh(row.get("observed_at"), now=now)
        if meter_fresh or not row.get("failure_class"):
            # A positive meter reading (headroom, no exhaustion) is not
            # blocking evidence — the gate never refuses on "available", so
            # its age cannot latch the lane. Keep the honest reading and its
            # provenance; mark staleness for the UI without degrading it.
            row["stale"] = not meter_fresh
            return row
        # Stale EXHAUSTION reading: keep the reading visible but stop
        # blocking — otherwise one exhausted sample latches the lane forever.
        evidence = row.get("evidence")
        row["availability"] = "unknown"
        row["remaining"] = "unknown"
        row["evidence"] = (
            f"stale (>{quota_exhaustion_ttl_s():.0f}s old): {evidence}"
            if evidence
            else "stale meter sample"
        )
        row["failure_class"] = None
        row["stale"] = True
        return row
    if obs is None:
        return _unknown_row(lane=lane, provider=provider, label=label, checked_at=checked_at)
    if _observation_is_fresh(obs.get("observed_at"), now=now):
        return _unavailable_from_observation(
            lane=lane, provider=provider, label=label, checked_at=checked_at, obs=obs
        )
    return _stale_row(lane=lane, provider=provider, label=label, checked_at=checked_at, obs=obs)


def build_quota_status(*, checked_at: str | None = None) -> dict[str, Any]:
    """Assemble the read-only fleet quota projection."""
    checked = checked_at or storage.now()
    now_dt = _parse_ts(checked) or datetime.now(UTC)
    quotas: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()

    codex_meter = read_codex_rate_limits()

    for lane_meta in FLEET_LANES:
        seen_keys.add((lane_meta["lane"], lane_meta["provider"], lane_meta["label"]))
        quotas.append(
            _fleet_row(
                lane=lane_meta["lane"],
                provider=lane_meta["provider"],
                label=lane_meta["label"],
                checked_at=checked,
                codex_meter=codex_meter,
                now=now_dt,
            )
        )

    # Credential-pool registry rows remain visible; absent quota data stays unknown.
    from . import credential_pool as _pool

    registry_error = None
    try:
        credentials = _pool.credential_pool_list(include_revoked=True)["credentials"]
    except ToolError:
        credentials = []
        registry_error = "credential metadata unavailable"

    for credential in credentials:
        provider = credential["provider"]
        label = credential["label"]
        lane = provider if provider in {m["lane"] for m in FLEET_LANES} else provider
        # Map common registry providers onto fleet lanes when obvious.
        if provider in {"anthropic"}:
            lane = "claude"
        elif provider in {"codex", "openai"}:
            lane = "codex" if provider == "codex" else provider
        elif provider in {"gemini", "google"}:
            lane = "agy"
        key = (lane, provider, label)
        if key in seen_keys:
            continue
        # Skip pure duplicates of the fleet default label rows.
        if (lane, provider, label) in {(m["lane"], m["provider"], m["label"]) for m in FLEET_LANES}:
            continue
        seen_keys.add(key)
        # Prefer fleet observation when the registry row maps to a known lane.
        if lane in {m["lane"] for m in FLEET_LANES}:
            quotas.append(
                _fleet_row(
                    lane=lane,
                    provider=provider,
                    label=label,
                    checked_at=checked,
                    codex_meter=codex_meter,
                    now=now_dt,
                )
            )
            continue
        quotas.append(_unknown_row(lane=lane, provider=provider, label=label, checked_at=checked))

    result = {"quotas": quotas, "checked_at": checked}
    if registry_error:
        result["credential_pool_error"] = registry_error
    return result
