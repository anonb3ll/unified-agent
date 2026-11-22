"""Friction rollup: rank what keeps going wrong for agents, and close it only on proof (#2399).

Friction (a tool that times out, a denied path, a flaky test) is reported in
scattered places. This module gives it one ledger and one ranking.

* **Reports** (``friction_reports``). One row per sighting: a ``key`` slug such as
  ``tool:run_cmd:timeout``, a severity, the channel it came in on, who reported
  it and a redacted one-line summary. ``source_ref`` makes ingest idempotent: a
  ``(channel, source_ref)`` pair is stored once per tenant.
* **Ingest.** :func:`ingest_session_nudges` turns the session-end friction
  nudges (``session_friction_nudges``) into reports on channel ``session_end``,
  one per (session, signal), attributed to the session's agent.
* **Rollup.** :func:`rollup` groups the reports in a window by key and ranks
  them: ``SEV[max] * log2(1 + count) * (1 + 0.5 * (channels - 1)) *
  (1 + 0.25 * (reporters - 1))`` with low=1, med=3, high=9. A problem seen on
  several channels by several agents outranks one agent's repeated complaint.
* **Verdicts** (``friction_verdicts``). A key is ``open`` until someone claims
  a fix with evidence (``fix_claimed``). Saying "fixed" does not close it:
  :func:`recheck` closes it only once ``recheck_after`` has passed with no new
  report since the claim, and a report after the claim reopens it (at once in
  :func:`report`, and again at recheck time for any report that reached the
  table another way). Only a verified human may :func:`accept` a key (a known
  cost the fleet lives with); a later report on an accepted key is counted but
  does not reopen it. A report on a ``closed`` key reopens it.

Every verdict transition appends one ``agent.friction.verdict`` event inside
the same ``BEGIN IMMEDIATE`` transaction, with ids and states only. Every read
and write is fenced to the caller's tenant (``org_id IS edition.tenant_org_id()``).
Timestamps in both tables are microsecond ISO strings so ordering a report
against a fix claim made in the same second is exact.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, events, storage
from .telemetry.redaction import redact_sensitive_content
from .tool_errors import PermissionDeniedError

CHANNEL = "agent.friction.verdict"
KIND = "friction.verdict"

SEVERITIES = ("low", "med", "high")
SEV_WEIGHT = {"low": 1, "med": 3, "high": 9}
_SEV_RANK = {"low": 1, "med": 2, "high": 3}
_RANK_SEV = {v: k for k, v in _SEV_RANK.items()}

CHANNELS = ("report", "session_end", "operator")
#: Channels a caller may file on through the coordination action.
ACTION_CHANNELS = ("report", "operator")

STATES = ("open", "fix_claimed", "closed", "accepted")
CLOSED_STATES = ("closed", "accepted")

KEY_RE = re.compile(r"^[a-z0-9_.:/-]{1,80}$")
SUMMARY_MAX = 300
NOTE_MAX = 500
EVIDENCE_MAX = 300
SOURCE_REF_MAX = 200
RECHECK_SWEEP_CAP = 100
#: How far back session-end nudges are read, by ingest and by the re-check that
#: must not close a claim while any of them is still unread.
INGEST_WINDOW_HOURS = 72
RECHECK_ACTOR = "friction:recheck"
INGEST_ACTOR = "friction:ingest"

#: Session-end nudge phrase prefixes (``session_friction.friction_reasons``) → key.
SESSION_REASON_KEYS: tuple[tuple[str, str], ...] = (
    ("you interrupted the agent", "session:interrupts"),
    ("you denied ", "session:denied_tools"),
    ("you corrected the agent", "session:corrections"),
    ("the agent retried failing tools", "session:tool_retries"),
)
SESSION_OTHER_KEY = "session:other"

_TENANT_SQL = "org_id IS ?"


# --- schema ---------------------------------------------------------------------


def ensure_schema(conn) -> None:
    """Create both tables (``table_schemas.friction``). Idempotent."""
    from . import table_schemas

    table_schemas.friction(conn)


def _tenant() -> str | None:
    return edition.tenant_org_id()


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _now_dt() -> datetime:
    return datetime.now(UTC)


def _parse(value: Any) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ToolError(f"invalid timestamp {value!r}: use ISO 8601") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def _line(text: Any) -> str:
    return " ".join(str(text or "").split())


def _clean_key(key: Any) -> str:
    text = str(key or "").strip().lower()
    if not KEY_RE.match(text):
        raise ToolError(
            f"invalid friction key {key!r}: use 1-80 chars of [a-z0-9_.:/-], "
            "e.g. 'tool:run_cmd:timeout'"
        )
    return text


def _clean_severity(severity: Any) -> str:
    text = str(severity or "").strip().lower()
    if text not in SEVERITIES:
        raise ToolError(f"invalid severity {severity!r}: use one of {', '.join(SEVERITIES)}")
    return text


def _clean_summary(summary: Any) -> str:
    text = _line(summary)
    if not text:
        raise ToolError("summary is required: one line saying what went wrong")
    if len(text) > SUMMARY_MAX:
        raise ToolError(f"summary must be at most {SUMMARY_MAX} characters (got {len(text)})")
    return redact_sensitive_content(text, max_len=SUMMARY_MAX)


def _clean_note(note: Any, *, required: bool = False, label: str = "note") -> str:
    text = _line(note)
    if required and not text:
        raise ToolError(f"{label} is required")
    if len(text) > NOTE_MAX:
        raise ToolError(f"{label} must be at most {NOTE_MAX} characters")
    return redact_sensitive_content(text, max_len=NOTE_MAX)


def _resolve_reporter(reporter: str | None) -> str:
    """The verified caller, unless a verified human files on someone's behalf."""
    given = _line(reporter)
    verified = config.verified_agent_identity()
    if verified:
        if given and config.identity_fold(given) != config.identity_fold(verified):
            if not config.is_human_identity(verified):
                raise PermissionDeniedError(
                    f"{verified!r} may not report friction as {given!r}: omit reporter "
                    "to report as yourself"
                )
            return given
        return verified
    if not given:
        raise ToolError("reporter is required when no verified identity is present")
    return given


def _actor(actor: str | None) -> str:
    verified = config.verified_agent_identity()
    if verified:
        return verified
    text = _line(actor)
    if not text:
        raise ToolError("actor is required when no verified identity is present")
    return text


def _verdict(conn, key: str):
    return conn.execute(
        f"SELECT * FROM friction_verdicts WHERE key=? AND {_TENANT_SQL}", (key, _tenant())
    ).fetchone()


def _verdict_dict(row) -> dict[str, Any]:
    if row is None:
        return {"state": "open"}
    return {
        "state": row["state"],
        "actor": row["actor"],
        "note": row["note"] or None,
        "evidence_ref": row["evidence_ref"],
        "fix_claimed_at": row["fix_claimed_at"],
        "recheck_after": row["recheck_after"],
        "closed_at": row["closed_at"],
        "updated_at": row["updated_at"],
    }


def _reports_after(conn, key: str, since: str | None) -> int:
    if not since:
        return 0
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM friction_reports WHERE key=? AND created_at>? "
        f"AND {_TENANT_SQL}",
        (key, since, _tenant()),
    ).fetchone()
    return int(row["n"])


def _key_has_reports(conn, key: str) -> bool:
    return (
        conn.execute(
            f"SELECT 1 FROM friction_reports WHERE key=? AND {_TENANT_SQL} LIMIT 1",
            (key, _tenant()),
        ).fetchone()
        is not None
    )


def _publish(pending: list[dict[str, Any] | None]) -> None:
    for event in pending:
        if event is not None:
            events.publish(event)


def _set_verdict(
    conn,
    key: str,
    prev: str,
    state: str,
    who: str,
    *,
    note: str = "",
    evidence_ref: str | None = None,
    fix_claimed_at: str | None = None,
    recheck_after: str | None = None,
    closed_at: str | None = None,
    keep_claim: bool = False,
) -> dict[str, Any] | None:
    """Upsert the verdict row and append its transition event (caller holds the tx)."""
    now = _iso(_now_dt())
    row = _verdict(conn, key)
    if row is None:
        conn.execute(
            "INSERT INTO friction_verdicts (key, org_id, state, actor, note, evidence_ref, "
            "fix_claimed_at, recheck_after, closed_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                _tenant(),
                state,
                who,
                note,
                evidence_ref,
                fix_claimed_at,
                recheck_after,
                closed_at,
                now,
            ),
        )
    else:
        if keep_claim:
            evidence_ref = row["evidence_ref"]
            fix_claimed_at = row["fix_claimed_at"]
        conn.execute(
            "UPDATE friction_verdicts SET state=?, actor=?, note=?, evidence_ref=?, "
            "fix_claimed_at=?, recheck_after=?, closed_at=?, updated_at=? "
            f"WHERE id=? AND {_TENANT_SQL}",
            (
                state,
                who,
                note,
                evidence_ref,
                fix_claimed_at,
                recheck_after,
                closed_at,
                now,
                int(row["id"]),
                _tenant(),
            ),
        )
    _ulid, _inserted, pending = events.record_event_once_on_connection(
        conn,
        CHANNEL,
        payload={"kind": KIND, "key": key, "from": prev, "to": state, "actor": who},
        originator=who,
    )
    return pending


def _reopen_on_report(conn, key: str, who: str) -> dict[str, Any] | None:
    """A new report on a fix-claimed or closed key reopens it; accepted keys stay."""
    row = _verdict(conn, key)
    if row is None or row["state"] in ("open", "accepted"):
        return None
    if row["state"] == "fix_claimed":
        count = _reports_after(conn, key, row["fix_claimed_at"])
        note = f"reopened by {count} report(s) after the fix claim"
    else:  # closed
        count = _reports_after(conn, key, row["closed_at"])
        note = f"reopened by {count} report(s) after it closed"
    if count < 1:
        return None
    return _set_verdict(conn, key, row["state"], "open", who, note=note, keep_claim=True)


def _check_task(conn, task_id: Any) -> int | None:
    if task_id is None:
        return None
    try:
        tid = int(task_id)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"task_id must be an integer, got {task_id!r}") from exc
    found = conn.execute(
        f"SELECT 1 FROM tasks WHERE id=? AND {_TENANT_SQL}", (tid, _tenant())
    ).fetchone()
    if found is None:
        raise ToolError(f"no task with id {tid}")
    return tid


def _insert_report(
    conn,
    *,
    key: str,
    severity: str,
    channel: str,
    reporter: str,
    summary: str,
    task_id: int | None,
    source_ref: str | None,
) -> int | None:
    """Insert one report; ``None`` when its ``(channel, source_ref)`` already exists."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO friction_reports (org_id, key, severity, channel, reporter, "
        "summary, task_id, created_at, source_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _tenant(),
            key,
            severity,
            channel,
            reporter,
            summary,
            task_id,
            _iso(_now_dt()),
            source_ref,
        ),
    )
    return int(cur.lastrowid) if cur.rowcount == 1 else None


# --- reports --------------------------------------------------------------------


def report(
    key: str,
    severity: str,
    summary: str,
    *,
    channel: str = "report",
    reporter: str | None = None,
    task_id: int | None = None,
    source_ref: str | None = None,
) -> dict[str, Any]:
    """File one friction report. Idempotent on ``(channel, source_ref)`` when set.

    A report on a ``fix_claimed`` or ``closed`` key reopens it in the same
    transaction; on an ``accepted`` key it is counted and nothing else changes.
    """
    key = _clean_key(key)
    severity = _clean_severity(severity)
    if channel not in CHANNELS:
        raise ToolError(f"invalid channel {channel!r}: use one of {', '.join(CHANNELS)}")
    summary = _clean_summary(summary)
    who = _resolve_reporter(reporter)
    ref = _line(source_ref) or None
    if ref is not None and len(ref) > SOURCE_REF_MAX:
        raise ToolError(f"source_ref must be at most {SOURCE_REF_MAX} characters")

    conn = storage.connect()
    pending: list[dict[str, Any] | None] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            tid = _check_task(conn, task_id)
            report_id = _insert_report(
                conn,
                key=key,
                severity=severity,
                channel=channel,
                reporter=who,
                summary=summary,
                task_id=tid,
                source_ref=ref,
            )
            if report_id is not None:
                pending.append(_reopen_on_report(conn, key, who))
            verdict = _verdict_dict(_verdict(conn, key))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    _publish(pending)
    return {
        "id": report_id,
        "duplicate": report_id is None,
        "key": key,
        "severity": severity,
        "channel": channel,
        "reporter": who,
        "verdict": verdict,
    }


def _session_reason_key(phrase: Any) -> str:
    text = str(phrase or "").strip().lower()
    for prefix, key in SESSION_REASON_KEYS:
        if text.startswith(prefix):
            return key
    return SESSION_OTHER_KEY


# Nudges of this tenant's sessions in the window, oldest first, each with the
# ``session_end`` reports it already has. ``source_ref`` is "<session_id>:<key>",
# so the range from ":" up to ";" (the next character) is every report of one
# session, and the unique index on (channel, source_ref, tenant) answers it. A
# session is read until every one of its signals is a report, so one that a
# pass's cap cut short finishes on the next pass.
_WINDOW_NUDGES_SQL = (
    "SELECT n.session_id, n.score, n.reasons, s.agent, ("
    "SELECT GROUP_CONCAT(r.source_ref, char(10)) FROM friction_reports r "
    "WHERE r.channel = 'session_end' "
    "AND r.source_ref >= n.session_id || ':' AND r.source_ref < n.session_id || ';' "
    "AND COALESCE(r.org_id, '') = COALESCE(s.org_id, '')) AS done "
    "FROM session_friction_nudges n JOIN agent_sessions s ON s.id = n.session_id "
    "WHERE n.nudged_at >= ? AND s.org_id IS ? ORDER BY n.nudged_at, n.session_id"
)


def _unread_signals(row: Any) -> dict[str, list[str]]:
    """The nudge's signals by key that are not ``session_end`` reports yet."""
    try:
        reasons = json.loads(row["reasons"] or "[]")
    except (TypeError, ValueError):
        reasons = []
    if not isinstance(reasons, list):
        reasons = []
    done = set((row["done"] or "").split("\n"))
    by_key: dict[str, list[str]] = {}
    for phrase in reasons:
        key = _session_reason_key(phrase)
        if f"{row['session_id']}:{key}" not in done:
            by_key.setdefault(key, []).append(_line(phrase))
    return by_key


def _window_cutoff(hours: int) -> str:
    return (_now_dt() - timedelta(hours=hours)).isoformat(timespec="seconds")


def ingest_session_nudges(since_hours: int = INGEST_WINDOW_HOURS, cap: int = 200) -> dict[str, Any]:
    """Turn recent session-end friction nudges into reports on ``session_end``.

    One report per (session, signal), ``source_ref = "<session_id>:<key>"``.
    Sessions are read oldest first and only for signals that are not reports
    yet. ``cap`` limits the reports written, counted per report, not per
    session: a session with more signals than the room left is cut at the cap
    and finishes on the next pass, and a burst larger than one pass drains
    over the next passes instead of leaving its oldest sessions behind the
    newest ones until they expire. ``backlog`` is how many sessions with
    unread signals this pass left, a cut session included.

    Severity is ``high`` when the nudge score is at least twice
    ``config.friction_score_threshold()``, else ``med``. The reporter is the
    session's agent; a nudge whose session is not in the caller's tenant is
    skipped. A nudge with no reasons writes nothing and never counts against
    ``cap`` or ``backlog``.
    """
    hours = max(1, min(int(since_hours), 24 * 30))
    limit = max(1, min(int(cap), 1000))
    high_at = 2 * config.friction_score_threshold()
    cutoff = _window_cutoff(hours)

    conn = storage.connect()
    pending: list[dict[str, Any] | None] = []
    inserted = 0
    existing = 0
    sessions = 0
    backlog = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(_WINDOW_NUDGES_SQL, (cutoff, _tenant())).fetchall()
            for row in rows:
                by_key = _unread_signals(row)
                if not by_key:
                    continue
                if inserted >= limit:
                    backlog += 1
                    continue
                sessions += 1
                severity = "high" if int(row["score"] or 0) >= high_at else "med"
                for key, phrases in by_key.items():
                    if inserted >= limit:
                        backlog += 1
                        break
                    summary = redact_sensitive_content(
                        f"session {row['session_id']}: {'; '.join(phrases)}",
                        max_len=SUMMARY_MAX,
                    )
                    report_id = _insert_report(
                        conn,
                        key=key,
                        severity=severity,
                        channel="session_end",
                        reporter=str(row["agent"]),
                        summary=summary,
                        task_id=None,
                        source_ref=f"{row['session_id']}:{key}",
                    )
                    if report_id is None:
                        existing += 1
                        continue
                    inserted += 1
                    pending.append(_reopen_on_report(conn, key, INGEST_ACTOR))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    _publish(pending)
    return {
        "sessions": sessions,
        "inserted": inserted,
        "already_ingested": existing,
        "backlog": backlog,
    }


def _unread_sessions(conn: Any) -> int:
    """Sessions in the ingest window whose friction signals are not reports yet."""
    rows = conn.execute(
        _WINDOW_NUDGES_SQL, (_window_cutoff(INGEST_WINDOW_HOURS), _tenant())
    ).fetchall()
    return sum(1 for row in rows if _unread_signals(row))


# --- rollup ---------------------------------------------------------------------


def score(max_severity: str, count: int, channels: int, reporters: int) -> float:
    """Rank weight: severity x log2(1 + count), boosted by channel and reporter spread."""
    return (
        SEV_WEIGHT[max_severity]
        * math.log2(1 + max(0, count))
        * (1 + 0.5 * (max(1, channels) - 1))
        * (1 + 0.25 * (max(1, reporters) - 1))
    )


def rollup(
    *, window_days: int = 14, limit: int = 50, include_closed: bool = False
) -> dict[str, Any]:
    """Rank friction keys reported in the last ``window_days``.

    Each entry carries count, distinct channels and reporters, max severity,
    first and last seen, the latest three summaries, the score and the verdict.
    Closed and accepted keys are left out unless ``include_closed``.
    """
    days = int(window_days)
    cap = int(limit)
    cutoff = _iso(_now_dt() - timedelta(days=days))
    tenant = _tenant()
    conn = storage.connect()
    try:
        groups = conn.execute(
            "SELECT key, COUNT(*) AS n, COUNT(DISTINCT channel) AS channels, "
            "COUNT(DISTINCT reporter) AS reporters, "
            "MAX(CASE severity WHEN 'high' THEN 3 WHEN 'med' THEN 2 ELSE 1 END) AS sev, "
            "MIN(created_at) AS first_seen, MAX(created_at) AS last_seen "
            f"FROM friction_reports WHERE created_at >= ? AND {_TENANT_SQL} GROUP BY key",
            (cutoff, tenant),
        ).fetchall()
        latest: dict[str, list[str]] = {}
        for row in conn.execute(
            "SELECT key, summary FROM ("
            " SELECT key, summary, ROW_NUMBER() OVER "
            "  (PARTITION BY key ORDER BY created_at DESC, id DESC) AS rn"
            f" FROM friction_reports WHERE created_at >= ? AND {_TENANT_SQL}"
            ") WHERE rn <= 3 ORDER BY key, rn",
            (cutoff, tenant),
        ).fetchall():
            latest.setdefault(row["key"], []).append(row["summary"])
        verdicts = {
            row["key"]: row
            for row in conn.execute(
                f"SELECT * FROM friction_verdicts WHERE {_TENANT_SQL}", (tenant,)
            ).fetchall()
        }
    finally:
        conn.close()

    items: list[dict[str, Any]] = []
    for row in groups:
        verdict = _verdict_dict(verdicts.get(row["key"]))
        if not include_closed and verdict["state"] in CLOSED_STATES:
            continue
        sev = _RANK_SEV[int(row["sev"])]
        items.append(
            {
                "key": row["key"],
                "score": round(
                    score(sev, int(row["n"]), int(row["channels"]), int(row["reporters"])), 3
                ),
                "count": int(row["n"]),
                "channels": int(row["channels"]),
                "reporters": int(row["reporters"]),
                "max_severity": sev,
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "latest_summaries": latest.get(row["key"], []),
                "verdict": verdict,
            }
        )
    # Two stable sorts: last_seen desc breaks score ties.
    items.sort(key=lambda item: item["last_seen"], reverse=True)
    items.sort(key=lambda item: item["score"], reverse=True)
    total = len(items)
    return {"window_days": days, "total": total, "items": items[:cap], "truncated": total > cap}


# --- verdicts -------------------------------------------------------------------


def claim_fixed(
    key: str,
    evidence_ref: str,
    note: str | None = None,
    *,
    recheck_days: int = 3,
    actor: str | None = None,
) -> dict[str, Any]:
    """Claim a fix for an open key. It stays unclosed until :func:`recheck` proves it."""
    key = _clean_key(key)
    evidence = _line(evidence_ref)
    if not evidence:
        raise ToolError(
            "evidence_ref is required to claim a fix: name the PR, commit or run that fixed it"
        )
    if len(evidence) > EVIDENCE_MAX:
        raise ToolError(f"evidence_ref must be at most {EVIDENCE_MAX} characters")
    evidence = redact_sensitive_content(evidence, max_len=EVIDENCE_MAX)
    text = _clean_note(note)
    days = int(recheck_days)
    if not 1 <= days <= 30:
        raise ToolError("recheck_days must be between 1 and 30")
    who = _actor(actor)

    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not _key_has_reports(conn, key):
                raise ToolError(f"no friction reports for key {key!r}: nothing to claim fixed")
            row = _verdict(conn, key)
            prev = row["state"] if row is not None else "open"
            if prev != "open":
                raise ToolError(
                    f"friction key {key!r} is {prev}, not open: a fix can be claimed only "
                    "on an open key"
                )
            now = _now_dt()
            pending = _set_verdict(
                conn,
                key,
                prev,
                "fix_claimed",
                who,
                note=text,
                evidence_ref=evidence,
                fix_claimed_at=_iso(now),
                recheck_after=_iso(now + timedelta(days=days)),
            )
            verdict = _verdict_dict(_verdict(conn, key))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    _publish([pending])
    return {"key": key, "verdict": verdict}


def accept(key: str, note: str) -> dict[str, Any]:
    """Close a key as accepted. Only a verified human caller may."""
    key = _clean_key(key)
    verified = config.verified_agent_identity()
    if not verified or not config.is_human_identity(verified):
        raise PermissionDeniedError(
            "only the human operator may accept a friction key; an agent closes one by "
            "claiming a fix with evidence and letting the recheck confirm it"
        )
    text = _clean_note(note, required=True, label="note (why this friction is accepted)")
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not _key_has_reports(conn, key):
                raise ToolError(f"no friction reports for key {key!r}: nothing to accept")
            row = _verdict(conn, key)
            prev = row["state"] if row is not None else "open"
            if prev == "accepted":
                raise ToolError(f"friction key {key!r} is already accepted")
            pending = _set_verdict(
                conn,
                key,
                prev,
                "accepted",
                verified,
                note=text,
                closed_at=_iso(_now_dt()),
                keep_claim=True,
            )
            verdict = _verdict_dict(_verdict(conn, key))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    _publish([pending])
    return {"key": key, "verdict": verdict}


def recheck(key: str | None = None, now: datetime | str | None = None) -> dict[str, Any]:
    """Settle fix claims: close when due and quiet, reopen when reported since.

    With ``key``, that key must be ``fix_claimed``. Without it, every
    ``fix_claimed`` key in the tenant is swept (at most 100, oldest due first).
    Returns ``{"closed": [...], "reopened": [...], "pending": [...]}``.

    "Quiet" means every signal was read. While session-end nudges in the
    ingest window are still unread, a due claim is not closed: one of them may
    be the recurrence. It stays ``pending`` and the result carries
    ``unread_sessions``; the next pass, after ingest has drained, settles it.
    A reopening needs no such care, because a report that exists is enough.
    """
    at = _parse(now) if now is not None else _now_dt()
    # ``now`` only decides what is due; a transition is stamped with the real time,
    # so a report filed after this close is ordered after ``closed_at``.
    stamp = _iso(_now_dt())
    clean = _clean_key(key) if key is not None else None
    who = config.verified_agent_identity() or RECHECK_ACTOR
    result: dict[str, Any] = {"closed": [], "reopened": [], "pending": []}
    pending: list[dict[str, Any] | None] = []

    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if clean is not None:
                row = _verdict(conn, clean)
                if row is None or row["state"] != "fix_claimed":
                    state = row["state"] if row is not None else "open"
                    raise ToolError(
                        f"friction key {clean!r} is {state}, not fix_claimed: nothing to recheck"
                    )
                rows = [row]
            else:
                rows = conn.execute(
                    "SELECT * FROM friction_verdicts WHERE state='fix_claimed' "
                    f"AND {_TENANT_SQL} ORDER BY recheck_after, key LIMIT ?",
                    (_tenant(), RECHECK_SWEEP_CAP),
                ).fetchall()
            unread = _unread_sessions(conn) if rows else 0
            if unread:
                result["unread_sessions"] = unread
            for row in rows:
                name = row["key"]
                count = _reports_after(conn, name, row["fix_claimed_at"])
                if count > 0:
                    pending.append(
                        _set_verdict(
                            conn,
                            name,
                            "fix_claimed",
                            "open",
                            who,
                            note=f"reopened by {count} report(s) after the fix claim",
                            keep_claim=True,
                        )
                    )
                    result["reopened"].append(name)
                elif unread:
                    result["pending"].append(name)
                elif row["recheck_after"] and _parse(row["recheck_after"]) <= at:
                    pending.append(
                        _set_verdict(
                            conn,
                            name,
                            "fix_claimed",
                            "closed",
                            who,
                            note=row["note"] or "",
                            closed_at=stamp,
                            keep_claim=True,
                        )
                    )
                    result["closed"].append(name)
                else:
                    result["pending"].append(name)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    _publish(pending)
    return result


# --- coordination-action wrappers ------------------------------------------------


def friction_report(
    key: str,
    severity: str,
    summary: str,
    channel: str = "report",
    reporter: str | None = None,
    task_id: int | None = None,
    source_ref: str | None = None,
) -> dict[str, Any]:
    """Action: file a friction report (channel ``report``, or ``operator`` for the human)."""
    if channel not in ACTION_CHANNELS:
        raise ToolError(
            f"channel {channel!r} cannot be filed directly: use one of "
            f"{', '.join(ACTION_CHANNELS)} (session_end comes from ingest)"
        )
    if channel == "operator":
        verified = config.verified_agent_identity()
        if verified and not config.is_human_identity(verified):
            raise PermissionDeniedError(
                "only the human operator files on the operator channel; use channel 'report'"
            )
    return {
        "ok": True,
        **report(
            key,
            severity,
            summary,
            channel=channel,
            reporter=reporter,
            task_id=task_id,
            source_ref=source_ref,
        ),
    }


def friction_rollup(
    window_days: int = 14, limit: int = 50, include_closed: bool = False
) -> dict[str, Any]:
    """Action: the ranked friction rollup."""
    try:
        days = int(window_days)
        cap = int(limit)
    except (TypeError, ValueError) as exc:
        raise ToolError("window_days and limit must be integers") from exc
    if not 1 <= days <= 365:
        raise ToolError("window_days must be between 1 and 365")
    if not 1 <= cap <= 200:
        raise ToolError("limit must be between 1 and 200")
    return {
        "ok": True,
        **rollup(window_days=days, limit=cap, include_closed=bool(include_closed)),
    }


def friction_claim_fixed(
    key: str,
    evidence_ref: str,
    note: str | None = None,
    recheck_days: int = 3,
    actor: str | None = None,
) -> dict[str, Any]:
    """Action: claim a fix with evidence; a later recheck closes or reopens it."""
    try:
        days = int(recheck_days)
    except (TypeError, ValueError) as exc:
        raise ToolError("recheck_days must be an integer") from exc
    return {
        "ok": True,
        **claim_fixed(key, evidence_ref, note, recheck_days=days, actor=actor),
    }


def friction_accept(key: str, note: str) -> dict[str, Any]:
    """Action: the human operator accepts a friction key as a known cost."""
    return {"ok": True, **accept(key, note)}


def friction_recheck(key: str | None = None) -> dict[str, Any]:
    """Action: settle one key's fix claim, or sweep every due claim."""
    return {"ok": True, **recheck(key)}
