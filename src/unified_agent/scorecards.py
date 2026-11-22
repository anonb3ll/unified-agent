"""Per-agent performance scorecards and saved eval runs (task #1354).

A scorecard reads what the platform already records; it adds no table and
re-derives nothing another module owns:

* **Runs** come from the self-pickup run record (``run_records``, #1436):
  each claim opened in the window, per claimant, when it has a persisted
  finish or is inferred abandoned by the run reader.
* **Review verdicts** are the review pipeline's own note markers
  (``[review: approved|validated|auto-merged|merged-upstream|...]`` versus
  ``[review: rejected|red-tier|...]``, ``reviewer.py``) on tasks the agent
  delivered. The scorecard counts them; it never re-reviews.
* **Reopens**: a delivered run (``completed`` or ``handed_off``) whose task was
  claimed again afterwards by anyone.
* **Harness health** comes from ``executor_attempts.failure_class``
  (``quota_observe.classify_failure_reason``). Provider quota, iteration
  budget, zero-output and teardown failures are the *harness's* failures and
  are reported apart from task-quality failures (``code_error``). A
  near-always-failing harness must not read as a bad agent.
* **Evals** are saved eval runs on the ``agent.eval`` channel of the event log:
  one event per run of a suite (``evals/*.yaml``) by an agent and model, with a
  per-case pass/fail. A run that could not execute records ``harness_error``
  and is excluded from the pass rate.

Every metric carries its numerator and denominator; ``value`` is ``None`` when
the denominator is zero. ``sample`` flags a denominator under
``MIN_SAMPLE``: a rate over three runs is shown, labelled, never ranked.

CI first-pass rate is reported ``unavailable``: no source attributes PR CI runs
to an agent yet. Nothing is backfilled or estimated.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from . import config, events, run_records, storage

EVAL_CHANNEL = "agent.eval"
KIND_EVAL_RUN = "eval.run"
DEFAULT_WINDOW_DAYS = 14.0
MAX_WINDOW_DAYS = 365.0
MIN_SAMPLE = 5
MAX_CASES = 200
MAX_EVAL_DETAIL = 300
MAX_EVAL_LABEL = 200

DELIVERED = frozenset({"completed", "handed_off"})
# Outcomes that say nothing about the agent's work: the task went back or was
# withdrawn. They are counted but excluded from the delivery-rate denominator.
NEUTRAL = frozenset({"released", "cancelled"})

HARNESS_FAILURE_CLASSES = frozenset(
    {
        "provider_quota_exhausted",
        "iteration_budget_timeout",
        "zero_output_attempt",
        "agy_teardown_resume_exhausted",
    }
)

_REVIEW_POSITIVE = re.compile(
    r"^\[review:\s*(approved|validated|auto-merged|merged-upstream|merged)\b",
    re.IGNORECASE | re.MULTILINE,
)
# A human "rework" closeout is a negative verdict (held_pr treats the latest
# review note as current, so a rework after a validation reopens the work).
_REVIEW_NEGATIVE = re.compile(
    r"^\[review:\s*(rejected|red-tier|yolo-merge-failed|rework)\b", re.IGNORECASE | re.MULTILINE
)


def _metric(numer: int, denom: int) -> dict[str, Any]:
    return {
        "value": round(numer / denom, 4) if denom else None,
        "numerator": numer,
        "denominator": denom,
        "sample": "ok" if denom >= MIN_SAMPLE else ("none" if denom == 0 else "small"),
    }


def _window(window_days: float) -> tuple[float, str]:
    try:
        days = float(window_days)
    except (TypeError, ValueError) as exc:
        raise ToolError("window_days must be a number") from exc
    if not 0 < days <= MAX_WINDOW_DAYS:
        raise ToolError(f"window_days must be in (0, {MAX_WINDOW_DAYS:g}]")
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    return days, since


# --- eval suites and saved eval runs -----------------------------------------


def evals_dir() -> Path:
    """Where eval suite definitions live: ``UA_EVALS_DIR`` or the packaged ``unified_agent/evals``."""
    import os

    raw = os.environ.get("UA_EVALS_DIR")
    if raw:
        return Path(raw).expanduser()
    # Suites ship inside the package so an installed wheel has them too.
    return Path(__file__).resolve().parent / "evals"


def _load_suite(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ToolError(f"eval suite {path.name} must be a mapping")
    name = str(data.get("name") or "").strip()
    if not name or name != path.stem or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ToolError(
            f"eval suite {path.name}: 'name' must equal the file stem and use only "
            "letters, digits, '_' and '-'"
        )
    cases = data.get("cases")
    version = data.get("version")
    if (
        not isinstance(version, (str, int))
        or isinstance(version, bool)
        or not str(version).strip()
        or len(str(version)) > 100
    ):
        raise ToolError(f"eval suite {name}: 'version' must be an explicit short value")
    if not isinstance(cases, list) or not 0 < len(cases) <= MAX_CASES:
        raise ToolError(f"eval suite {name}: 'cases' must contain 1 to {MAX_CASES} cases")
    ids: list[str] = []
    for i, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ToolError(f"eval suite {name}: case {i} must be a mapping")
        cid = str(case.get("id") or "").strip()
        if not cid or cid in ids:
            raise ToolError(f"eval suite {name}: case {i} needs a unique 'id'")
        if not str(case.get("task") or "").strip():
            raise ToolError(f"eval suite {name}: case {cid} needs a 'task' fixture")
        checks = case.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ToolError(f"eval suite {name}: case {cid} needs a non-empty 'checks' list")
        ids.append(cid)
    return {
        "name": name,
        "description": str(data.get("description") or ""),
        "version": str(version).strip(),
        "cases": [
            {"id": str(c["id"]).strip(), "task": str(c["task"]), "checks": list(c["checks"])}
            for c in cases
        ],
    }


def eval_suites() -> dict[str, Any]:
    """Every eval suite definition, validated. A malformed suite is reported, not raised."""
    root = evals_dir()
    suites, errors = [], []
    for path in sorted(root.glob("*.yaml")) if root.is_dir() else []:
        try:
            suites.append(_load_suite(path))
        except Exception as exc:  # noqa: BLE001 - one bad file must not hide the rest
            errors.append({"file": path.name, "error": str(exc)[:300]})
    return {"dir": str(root), "suites": suites, "errors": errors}


def _suite(name: str) -> dict[str, Any]:
    root = evals_dir().resolve()
    if (
        not isinstance(name, str)
        or len(name) > MAX_EVAL_LABEL
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name)
    ):
        raise ToolError(f"unknown eval suite {name!r}")
    path = (root / f"{name}.yaml").resolve()
    if path.parent != root or not path.is_file():
        raise ToolError(f"unknown eval suite {name!r}")
    return _load_suite(path)


def eval_record(
    suite: str,
    agent: str,
    results: list[dict[str, Any]] | None = None,
    model: str | None = None,
    harness_error: str | None = None,
) -> dict[str, Any]:
    """Save one eval run of ``suite`` by ``agent``.

    ``results`` is ``[{case, passed, detail?}]`` naming every case of the suite
    exactly once. A run that could not execute passes ``harness_error`` and no
    results; it is saved and excluded from pass rates. The agent and recorder
    must match the verified caller identity.
    """
    spec = _suite(suite)
    identity = config.verified_agent_identity()
    if not identity:
        raise ToolError("eval_record requires a verified caller identity")
    if agent != identity:
        raise ToolError("agent must match the verified caller identity")
    if not isinstance(agent, str) or len(agent) > MAX_EVAL_LABEL:
        raise ToolError("agent must be a label of at most 200 characters")
    label = agent.strip()
    if not label:
        raise ToolError("agent must be a non-empty label")
    expected = [c["id"] for c in spec["cases"]]
    clean: list[dict[str, Any]] = []
    if model is not None and (not isinstance(model, str) or len(model) > MAX_EVAL_LABEL):
        raise ToolError("model must be at most 200 characters")
    if harness_error is not None:
        if not isinstance(harness_error, str) or len(harness_error) > 500:
            raise ToolError("harness_error must be at most 500 characters")
        harness_error = harness_error.strip()
        if not harness_error:
            raise ToolError("harness_error must be a non-empty description when given")
    if harness_error:
        if results:
            raise ToolError("pass either results or harness_error, not both")
    else:
        if not isinstance(results, list) or len(results) > MAX_CASES:
            raise ToolError("results must be a list of {case, passed, detail}")
        seen: set[str] = set()
        for r in results:
            if not isinstance(r, dict) or not isinstance(r.get("passed"), bool):
                raise ToolError("each result needs 'case' and a boolean 'passed'")
            if not isinstance(r.get("case"), str) or len(r["case"]) > MAX_EVAL_LABEL:
                raise ToolError("result case must be a short string")
            detail = r.get("detail", "")
            if not isinstance(detail, str) or len(detail) > MAX_EVAL_DETAIL:
                raise ToolError("result detail must be at most 300 characters")
            cid = r["case"]
            if cid not in expected or cid in seen:
                raise ToolError(f"result case {cid!r} is unknown or repeated for suite {suite}")
            seen.add(cid)
            clean.append({"case": cid, "passed": r["passed"], "detail": detail})
        missing = sorted(set(expected) - seen)
        if missing:
            raise ToolError(f"results missing case(s) {missing}")
    passed = sum(1 for r in clean if r["passed"])
    payload = {
        "kind": KIND_EVAL_RUN,
        "suite": spec["name"],
        "suite_version": spec["version"],
        "agent": label,
        "model": (model or "").strip() or None,
        "cases": len(expected),
        "passed": passed if not harness_error else None,
        "results": clean,
        "harness_error": (harness_error or "").strip() or None,
        "recorded_by": identity or label,
        "at": storage.now(),
    }
    event_id = events.record_event(EVAL_CHANNEL, payload=payload, originator=payload["recorded_by"])
    return {"event_id": event_id, **payload}


def _eval_runs(conn: Any, since: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload FROM event_log WHERE channel=? AND json_extract(payload, '$.kind')=? "
        "AND json_extract(payload, '$.at') >= ? ORDER BY ulid",
        (EVAL_CHANNEL, KIND_EVAL_RUN, since),
    ).fetchall()
    return [run_records._payload(r) for r in rows]


# --- scorecards ---------------------------------------------------------------


def _run_outcomes(conn: Any, since: str) -> list[dict[str, Any]]:
    """Finished or inferred-abandoned claims opened in the window.

    Use claim time for *both* persisted and inferred outcomes: an open run has
    no reliable finish timestamp, so mixing finish and claim windows would
    skew rates at the boundary.
    """
    rows = conn.execute(
        "SELECT c.payload, f.payload AS finish_payload, f.ulid AS finish_ulid, "
        "t.id AS task_exists, t.status, t.claimed_by, t.lock_expires_at "
        "FROM event_log c LEFT JOIN tasks t ON t.id = json_extract(c.payload, '$.task_id') "
        "LEFT JOIN event_log f ON f.dedupe_key = "
        "'selfpickup:finished:' || json_extract(c.payload, '$.run_id') "
        "WHERE c.channel=? AND json_extract(c.payload, '$.kind')=? "
        "AND json_extract(c.payload, '$.claimed_at') >= ?",
        (run_records.CHANNEL, run_records.KIND_CLAIMED, since),
    ).fetchall()
    now = storage.now()
    outcomes = []
    for row in rows:
        claim = run_records._payload(row)
        if row["finish_payload"] is not None:
            finish = run_records._payload({"payload": row["finish_payload"]})
            outcomes.append({**finish, "_ulid": row["finish_ulid"]})
            continue
        # Terminal task status without a finish is an unclosed run, not proof
        # the claimant abandoned it. This matches runs_in_flight's treatment.
        if row["status"] in run_records._NOT_IN_FLIGHT:
            continue
        if run_records._abandon_reason(claim, row if row["task_exists"] is not None else None, now):
            outcomes.append({"agent": claim.get("agent"), "outcome": "abandoned"})
    return outcomes


def _later_claim(conn: Any, task_id: int, after_ulid: str) -> bool:
    """A claim on ``task_id`` recorded after the finish event (ULID order, not wall time)."""
    row = conn.execute(
        "SELECT 1 FROM event_log WHERE channel=? AND json_extract(payload, '$.kind')=? "
        "AND json_extract(payload, '$.task_id')=? AND ulid > ? LIMIT 1",
        (run_records.CHANNEL, run_records.KIND_CLAIMED, int(task_id), after_ulid),
    ).fetchone()
    return row is not None


def _review(notes: str | None) -> str | None:
    """The latest review verdict on a task: ``positive``, ``negative`` or None."""
    last: tuple[int, str] | None = None
    for m in _REVIEW_POSITIVE.finditer(notes or ""):
        last = max(last or (-1, ""), (m.start(), "positive"))
    for m in _REVIEW_NEGATIVE.finditer(notes or ""):
        last = max(last or (-1, ""), (m.start(), "negative"))
    return last[1] if last else None


def _attempts(conn: Any, since: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT agent, outcome, failure_class FROM executor_attempts WHERE started_at >= ?",
            (since,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - table absent on a fresh store
        return []
    return [dict(r) for r in rows]


def agent_scorecard(agent: str | None = None, window_days: float = DEFAULT_WINDOW_DAYS) -> dict:
    """Per-agent scorecards over the trailing window (default 14 days).

    Each card: run outcomes and delivery rate, review approval rate, reopen
    rate, executor harness health (harness failures kept apart from quality
    failures), eval pass rates per suite and model, and CI first-pass
    (unavailable). ``agent`` restricts to one label.
    """
    days, since = _window(window_days)
    only = agent.strip() if agent else None
    conn = storage.connect()
    try:
        finishes = _run_outcomes(conn, since)
        cards: dict[str, dict[str, Any]] = {}

        def card(label: str) -> dict[str, Any]:
            return cards.setdefault(
                label,
                {
                    "agent": label,
                    "outcomes": {},
                    "_delivered": [],
                    "_evals": [],
                    "_attempts": [],
                },
            )

        for f in finishes:
            # An unmatched finish has no claimant; it must not improve the
            # actor's delivery score merely because they closed the task.
            label = f.get("agent")
            if not label or (only and label != only):
                continue
            c = card(label)
            outcome = f.get("outcome") or "unknown"
            c["outcomes"][outcome] = c["outcomes"].get(outcome, 0) + 1
            if outcome in DELIVERED and f.get("task_id") is not None:
                c["_delivered"].append(f)
        for e in _eval_runs(conn, since):
            if e.get("agent") and (not only or e["agent"] == only):
                card(e["agent"])["_evals"].append(e)
        for a in _attempts(conn, since):
            if a.get("agent") and (not only or a["agent"] == only):
                card(a["agent"])["_attempts"].append(a)

        out = []
        for label in sorted(cards):
            c = cards[label]
            outcomes = c["outcomes"]
            finished = sum(outcomes.values())
            scored = finished - sum(outcomes.get(o, 0) for o in NEUTRAL)
            delivered = sum(outcomes.get(o, 0) for o in DELIVERED)

            task_ids = sorted({int(f["task_id"]) for f in c["_delivered"]})
            notes: dict[int, str] = {}
            for i in range(0, len(task_ids), 500):
                chunk = task_ids[i : i + 500]
                for row in conn.execute(
                    f"SELECT id, notes FROM tasks WHERE id IN ({','.join('?' * len(chunk))})", chunk
                ).fetchall():
                    notes[int(row["id"])] = row["notes"] or ""
            verdicts = [_review(notes.get(t)) for t in task_ids]
            pos = sum(1 for v in verdicts if v == "positive")
            neg = sum(1 for v in verdicts if v == "negative")
            reopened = sum(
                1 for f in c["_delivered"] if _later_claim(conn, int(f["task_id"]), f["_ulid"])
            )

            attempts = c["_attempts"]
            harness = sum(1 for a in attempts if a.get("failure_class") in HARNESS_FAILURE_CLASSES)
            succeeded = sum(
                1 for a in attempts if a.get("outcome") in ("success", "waiting_review")
            )
            quality_fail = sum(
                1
                for a in attempts
                # A kill is an administrative stop, not an observed quality failure.
                if a.get("outcome") in ("failure", "blocked", "quarantined", "cooldown")
                and a.get("failure_class") not in HARNESS_FAILURE_CLASSES
            )
            classes: dict[str, int] = {}
            for a in attempts:
                if a.get("failure_class"):
                    classes[a["failure_class"]] = classes.get(a["failure_class"], 0) + 1

            evals: dict[tuple[str, str, str | None], dict[str, Any]] = {}
            for e in c["_evals"]:
                key = (e.get("suite"), e.get("suite_version"), e.get("model"))
                row = evals.setdefault(
                    key,
                    {
                        "suite": key[0],
                        "suite_version": key[1],
                        "model": key[2],
                        "runs": 0,
                        "harness_errors": 0,
                        "passed": 0,
                        "cases": 0,
                    },
                )
                row["runs"] += 1
                if e.get("harness_error"):
                    row["harness_errors"] += 1
                    continue
                row["passed"] += int(e.get("passed") or 0)
                row["cases"] += int(e.get("cases") or 0)
            eval_rows = [
                {**r, "pass_rate": _metric(r["passed"], r["cases"])}
                for r in sorted(
                    evals.values(),
                    key=lambda r: (r["suite"] or "", r["suite_version"] or "", r["model"] or ""),
                )
            ]

            out.append(
                {
                    "agent": label,
                    "runs": {"finished": finished, "outcomes": outcomes},
                    "delivery_rate": _metric(delivered, scored),
                    "abandon_rate": _metric(outcomes.get("abandoned", 0), scored),
                    # Per delivered *task*, by its latest review verdict: a
                    # task delivered twice is one sample, judged by where its
                    # review ended. Notes carry no per-delivery review link.
                    "review_approval_rate": {
                        **_metric(pos, pos + neg),
                        "unit": "task",
                        "unreviewed": len(task_ids) - pos - neg,
                    },
                    "reopen_rate": _metric(reopened, len(c["_delivered"])),
                    "harness": {
                        "attempts": len(attempts),
                        "harness_failures": harness,
                        "failure_classes": classes,
                        "quality_success_rate": _metric(succeeded, succeeded + quality_fail),
                    },
                    "evals": eval_rows,
                    "ci_first_pass_rate": {
                        "value": None,
                        "status": "unavailable",
                        "detail": "no source attributes PR CI runs to an agent yet",
                    },
                }
            )
    finally:
        conn.close()
    return {
        "window_days": days,
        "since": since,
        "run_cohort": "claimed_at",
        "min_sample": MIN_SAMPLE,
        "agents": out,
    }


def scorecards_payload(window_days: float = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """The Control Center view: every agent's card plus the eval suites."""
    suites = eval_suites()
    return {
        **agent_scorecard(window_days=window_days),
        "suites": [{"name": s["name"], "cases": len(s["cases"])} for s in suites["suites"]],
        "suite_errors": suites["errors"],
    }
