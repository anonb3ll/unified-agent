"""Session-end friction scoring → one redacted learning-capture nudge per session.

TeamAI emulation F1 (task #914). Mirrors teamai-cli ``src/contribute-check.ts``
``computeSmartScore``: a session earns points from signals the user had to
correct or the agent had to fight — interrupts, denied tool calls, re-prompted
corrections, retried tool failures — never from raw activity. Scale (tool
diversity, skill use) only nudges and can never cross the threshold alone.

Boundaries (task #914):
  * NUDGE ONLY. This module never calls ``memory_save``, never auto-files a
    learning, never opens an approval pass. It suggests running
    ``propose_session_learnings`` and stops there.
  * No transcripts, no prompts. The one-line task summary carried in the nudge
    is the caller-supplied ``session.summary`` (or the task title) run through
    secret redaction and bounded to one short line — never transcript text.
  * Dedupe-once. A session that has already been nudged is recorded in
    ``session_friction_nudges``; re-evaluating the same closed session returns
    the cached decision without emitting a second nudge.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from . import config, storage
from .lifecycle.schema import CHANNEL_LIFECYCLE_PREFIX
from .telemetry.redaction import redact_sensitive_content

# ─── friction score weights (teamai-cli parity) ────────────────────────────────
# Each strong primary signal lands on the threshold by itself; scale only nudges.
_INTERRUPT_WEIGHT = 20
_REJECT_WEIGHT = 20
_CORRECTION_WEIGHT = 20
# Retried tool failures: a few flukes are noise, a sustained struggle is signal.
_TOOL_RETRY_TIERS: tuple[tuple[int, int], ...] = ((8, 25), (5, 18), (3, 10))
_SKILL_BONUS = 5
_DIVERSITY_BONUS_MAX = 5

_MAX_SUMMARY_CHARS = 120
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_WS_RE = re.compile(r"\s+")

_TOOL_STATUS_FAILED = frozenset({"error", "failed", "fail", "exception", "timeout"})
_TOOL_STATUS_DENIED = frozenset({"denied", "rejected", "blocked", "refused"})


@dataclass
class SessionFriction:
    """The signals that decide whether a session is worth capturing.

    ``interrupts`` / ``tool_rejects`` / ``corrections`` / ``tool_retries`` are the
    primary friction signals. ``tool_calls`` / ``unique_tools`` / ``used_skill``
    only feed the small scale nudge.
    """

    interrupts: int = 0
    tool_rejects: int = 0
    corrections: int = 0
    tool_retries: int = 0
    tool_calls: int = 0
    unique_tools: int = 0
    used_skill: bool = False

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> SessionFriction:
        """Build from a client-supplied ``meta.friction`` dict. Missing keys → 0.

        Accepts both flat keys and a nested ``interventions`` object
        (``{interrupt, toolReject, correction, toolError}``) so a Claude Code
        Stop hook can pass either shape.
        """

        def _int(*keys: str) -> int:
            for key in keys:
                value = data.get(key)
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    return max(0, int(value))
            return 0

        nested = data.get("interventions")
        nested = nested if isinstance(nested, dict) else {}

        return cls(
            interrupts=_int("interrupts", "interrupt") or _mint(nested, "interrupt"),
            tool_rejects=_int("tool_rejects", "toolReject", "rejects")
            or _mint(nested, "toolReject"),
            corrections=_int("corrections", "correction") or _mint(nested, "correction"),
            tool_retries=_int("tool_retries", "toolError", "retries") or _mint(nested, "toolError"),
            tool_calls=_int("tool_calls", "toolCalls"),
            unique_tools=_int("unique_tools", "uniqueTools"),
            used_skill=bool(data.get("used_skill") or data.get("usedSkill")),
        )


def _mint(mapping: dict[str, Any], key: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _tool_retry_points(retries: int) -> int:
    for threshold, points in _TOOL_RETRY_TIERS:
        if retries >= threshold:
            return points
    return 0


def score_friction(friction: SessionFriction) -> int:
    """Friction score. Primary signals dominate; scale only nudges (max ~10)."""
    score = 0
    score += friction.interrupts * _INTERRUPT_WEIGHT
    score += friction.tool_rejects * _REJECT_WEIGHT
    score += friction.corrections * _CORRECTION_WEIGHT
    score += _tool_retry_points(friction.tool_retries)

    if friction.used_skill:
        score += _SKILL_BONUS
    if friction.tool_calls > 0 and friction.unique_tools > 0:
        diversity = friction.unique_tools / min(friction.tool_calls, 10)
        score += min(round(diversity * _DIVERSITY_BONUS_MAX), _DIVERSITY_BONUS_MAX)
    return score


def _times(count: int) -> str:
    if count == 1:
        return "once"
    if count == 2:
        return "twice"
    return f"{count} times"


def friction_reasons(friction: SessionFriction) -> list[str]:
    """Human-facing phrases for the non-zero primary signals only."""
    reasons: list[str] = []
    if friction.interrupts > 0:
        reasons.append(f"you interrupted the agent {_times(friction.interrupts)}")
    if friction.tool_rejects > 0:
        call = "tool call" if friction.tool_rejects == 1 else "tool calls"
        reasons.append(f"you denied {friction.tool_rejects} {call}")
    if friction.corrections > 0:
        reasons.append(f"you corrected the agent {_times(friction.corrections)}")
    if friction.tool_retries > 0:
        reasons.append(f"the agent retried failing tools {_times(friction.tool_retries)}")
    return reasons


@dataclass
class FrictionDecision:
    nudge: bool
    score: int
    threshold: int
    reasons: list[str]
    deduped: bool = False
    gated: str | None = (
        None  # why a nudge was withheld: "below_threshold" | "tool_gate" | "disabled"
    )


def evaluate_friction(
    friction: SessionFriction,
    *,
    threshold: int | None = None,
    tool_gate: int | None = None,
) -> FrictionDecision:
    """Pure decision: does this friction profile warrant a nudge?

    A session below the tool-call hard gate never nudges, whatever its score.
    """
    thr = config.friction_score_threshold() if threshold is None else int(threshold)
    gate = config.friction_tool_call_gate() if tool_gate is None else int(tool_gate)
    score = score_friction(friction)
    reasons = friction_reasons(friction)

    if friction.tool_calls < gate:
        return FrictionDecision(False, score, thr, reasons, gated="tool_gate")
    if score < thr:
        return FrictionDecision(False, score, thr, reasons, gated="below_threshold")
    return FrictionDecision(True, score, thr, reasons)


# ─── friction signal gathering (transcript source is stubbable) ────────────────


def gather_session_friction(
    session: dict[str, Any],
    *,
    override: SessionFriction | None = None,
) -> SessionFriction:
    """Assemble the friction profile for a completed session.

    Precedence:
      1. ``override`` — the test / caller-supplied profile (stub the transcript).
      2. ``session.meta['friction']`` — a Stop hook's own transcript scan.
      3. Lifecycle events for the session — what the hub can observe on its own
         (tool call count, tool diversity, retried failures, denied calls).
         Interrupts and corrections are not hub-observable and stay 0 here.
    """
    if override is not None:
        return override

    meta = session.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("friction"), dict):
        return SessionFriction.from_mapping(meta["friction"])

    return _friction_from_lifecycle(session.get("id") or "", session.get("task_id"))


def _event_relates(event: dict[str, Any], session_id: str, task_id: int | None) -> bool:
    if event.get("episode_id") == session_id:
        return True
    payload = event.get("payload")
    if isinstance(payload, dict):
        if payload.get("session_id") == session_id:
            return True
        if task_id is not None and payload.get("task_id") == task_id:
            return True
    return False


def _inner(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("payload")
    return inner if isinstance(inner, dict) else {}


def _tool_name(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    if isinstance(payload, dict):
        if payload.get("tool"):
            return str(payload["tool"])
        inner = payload.get("payload")
        if isinstance(inner, dict) and inner.get("tool"):
            return str(inner["tool"])
    return ""


def _status_in(payload: dict[str, Any], names: frozenset[str]) -> bool:
    return str(payload.get("status") or "").lower() in names


def _friction_from_lifecycle(session_id: str, task_id: int | None) -> SessionFriction:
    if not session_id:
        return SessionFriction()
    from . import events as events_mod

    try:
        rows = events_mod.query_events(
            channels=[f"{CHANNEL_LIFECYCLE_PREFIX}.*"],
            limit=500,
            newest_first=False,
        )
    except Exception:
        return SessionFriction()

    matched = [r for r in rows if _event_relates(r, session_id, task_id)]
    tool_calls = 0
    retries = 0
    rejects = 0
    tools: set[str] = set()
    used_skill = False
    for row in matched:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        kind = payload.get("kind")
        if kind not in ("tool_start", "tool_end"):
            continue
        name = _tool_name(row) or "tool"
        if kind == "tool_start":
            tool_calls += 1
            tools.add(name)
            if name.lower() in ("skill", "skill_review"):
                used_skill = True
            continue
        inner = _inner(row)
        if _status_in(inner, _TOOL_STATUS_DENIED):
            rejects += 1
        elif _status_in(inner, _TOOL_STATUS_FAILED) or inner.get("ok") is False:
            retries += 1

    return SessionFriction(
        interrupts=0,
        tool_rejects=rejects,
        corrections=0,
        tool_retries=retries,
        tool_calls=tool_calls,
        unique_tools=len(tools),
        used_skill=used_skill,
    )


# ─── redaction + nudge text ───────────────────────────────────────────────────


def redacted_task_summary(session: dict[str, Any]) -> str | None:
    """A secret-redacted, single-line, length-bounded task label for the nudge.

    Source is ``session.summary`` then the joined task title — never transcript
    text. Returns ``None`` when there is nothing safe to show.
    """
    raw = (session.get("summary") or "").strip() or (session.get("task_title") or "").strip()
    if not raw:
        return None
    redacted = redact_sensitive_content(raw, max_len=400)
    collapsed = _WS_RE.sub(" ", _CONTROL_CHARS_RE.sub("", redacted)).strip()
    if not collapsed or "[REDACTED_SECRET]" == collapsed:
        return None
    if len(collapsed) > _MAX_SUMMARY_CHARS:
        collapsed = collapsed[: _MAX_SUMMARY_CHARS - 1].rstrip() + "…"
    return collapsed


def build_nudge_message(
    session_id: str,
    decision: FrictionDecision,
    summary: str | None,
) -> str:
    """The one-line-summary nudge. Names only the non-zero signals; no raw content."""
    reasons = ", ".join(decision.reasons)
    headline = (
        f"[friction] This session hit friction worth capturing: {reasons}."
        if reasons
        else "[friction] This session hit enough friction to be worth capturing."
    )
    task_line = f"\n\nTask: {summary}" if summary else ""
    action = (
        f'\n\nRun propose_session_learnings("{session_id}", dry_run=True) to review a '
        "learning proposal, then file it if it holds up. Nothing is written automatically."
    )
    return f"{headline}{task_line}{action}"


# ─── dedupe-once ledger ───────────────────────────────────────────────────────


def _nudge_record(session_id: str) -> dict[str, Any] | None:
    try:
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT session_id, score, reasons, nudged_at "
                "FROM session_friction_nudges WHERE session_id=?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if row is None:
        return None
    try:
        reasons = json.loads(row["reasons"])
    except Exception:
        reasons = []
    return {
        "session_id": row["session_id"],
        "score": row["score"],
        "reasons": reasons if isinstance(reasons, list) else [],
        "nudged_at": row["nudged_at"],
    }


def _record_nudge(session_id: str, score: int, reasons: list[str]) -> bool:
    """Insert the dedupe row. INSERT OR IGNORE — a second call is a no-op."""
    try:
        conn = storage.connect()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO session_friction_nudges "
                "(session_id, score, reasons, nudged_at) VALUES (?, ?, ?, ?)",
                (session_id, int(score), json.dumps(list(reasons)), storage.now()),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()
    except Exception:
        return False


# ─── session_end entry point ──────────────────────────────────────────────────


def maybe_nudge_session_learning(
    session_id: str,
    status: str,
    *,
    override: SessionFriction | None = None,
) -> dict[str, Any] | None:
    """Best-effort friction nudge for a just-closed session. Never raises.

    Returns ``None`` when nudging is disabled, the session did not complete, or
    the session is missing. Otherwise returns a decision dict; ``nudge`` is True
    only on the first crossing evaluation for that session.
    """
    try:
        if status != "completed":
            return None
        if not config.friction_nudge_enabled():
            return None

        from . import registry

        session = registry.get_session(session_id)
        if session is None:
            return None

        existing = _nudge_record(session_id)
        if existing is not None:
            return {
                "nudge": False,
                "deduped": True,
                "score": existing["score"],
                "reasons": existing["reasons"],
            }

        friction = gather_session_friction(session, override=override)
        decision = evaluate_friction(friction)
        if not decision.nudge:
            return {
                "nudge": False,
                "deduped": False,
                "score": decision.score,
                "threshold": decision.threshold,
                "gated": decision.gated,
                "reasons": decision.reasons,
            }

        summary = redacted_task_summary(session)
        message = build_nudge_message(session_id, decision, summary)
        wrote = _record_nudge(session_id, decision.score, decision.reasons)
        return {
            "nudge": True,
            "deduped": not wrote,
            "score": decision.score,
            "threshold": decision.threshold,
            "reasons": decision.reasons,
            "summary": summary,
            "message": message,
            "follow_up": "propose_session_learnings",
        }
    except Exception:
        return None
