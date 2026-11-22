"""Per-task workflow record: stage, resume capsule and graded checks (task #2398).

A task's notes say what happened, in prose, in order. Two questions they answer
badly are the ones an agent asks right after compaction or a handoff: *which
stage is this task in, and what was I in the middle of?* and *which of the
checks I am about to rely on did anyone other than an agent confirm?*

Stage and resume capsule
    One row per task: ``stage`` (one of ``STAGES``), ``goal``, ``done`` (a short
    list) and ``focus``, plus the git ``head_sha`` the capsule describes. The
    claimant writes it as the work moves; ``session_packet`` returns it, so a
    resumed or compacted session starts from the stage instead of re-deriving
    it from notes. Every write bumps ``revision``; pass ``expected_revision`` to
    refuse a write over one you did not see. The capsule is agent-written text
    (trust tier T2): a pointer, never an instruction.

Checks and proof grades
    A check is one named result (``tests``, ``lint``, ``ci``...) at one head.
    Each carries a grade:

    * ``attested``: an agent said so. Every recorded check starts here.
    * ``verified``: the hub resolved the check's reference itself (a CI run,
      through ``gh``) and it agrees: same head, finished, the stated outcome.
      A run's overall conclusion is not a job's: a run is green with a job
      skipped, and a check's name ties it to nothing. A reference that names
      a ``job`` is graded on that job (it must exist, have run and agree).
      One that names none is verified for the whole run only, and every view
      says so (``verified_scope: "run"``, ``summary.verified_run_only``).
    * ``unknown``: nobody has said anything about this check at the head that
      matters: no row, or only a row for an older head (``stale_head``).

    A reference that contradicts the claim (another head, another conclusion)
    leaves the grade at ``attested`` and records the contradiction; it never
    raises the grade, and it is listed in ``summary.contradicted``. Re-recording
    a check at the same head resets it to ``attested``, because the new claim
    has not been verified. Report mode: nothing here gates a claim or merge.

Writers are the task's claimant, its stored ``next_owner``, a lifecycle
operator or the human operator. Rows carry ``org_id`` and every read and write
is fenced to the caller's tenant; another tenant's task reads as unknown.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, events, storage
from .tool_errors import PermissionDeniedError

STAGES = ("plan", "implement", "verify", "review", "merge", "deploy", "prove", "done")
ATTESTED, VERIFIED, UNKNOWN = "attested", "verified", "unknown"
GRADES = (ATTESTED, VERIFIED, UNKNOWN)
PASS, FAIL = "pass", "fail"
OUTCOMES = (PASS, FAIL)
#: Reference kinds the hub can resolve on its own. Anything else stays attested.
REF_KINDS = ("ci_run",)
CHANNEL = "agent.task.workflow"

GOAL_MAX = 500
FOCUS_MAX = 500
DONE_MAX_ITEMS = 20
DONE_ITEM_MAX = 300
DETAIL_MAX = 500
EXPECTED_MAX = 12
CHECKS_PER_TASK = 200

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
_SHA_RE = re.compile(r"[0-9a-f]{7,40}")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_RUN_RE = re.compile(r"[0-9]{1,15}")

JOB_MAX = 120
#: What a verified grade stands on: one named job of the run, or only the run.
SCOPE_JOB, SCOPE_RUN = "job", "run"

#: (claim kind, ref, repo, expect, head_sha) -> (verdict, detail, observed).
Verifier = Callable[[str, str, str, str, str], tuple[str, str, dict[str, Any]]]
#: (run id, repo, job name, expect) -> (verdict, detail, observed).
JobVerifier = Callable[[str, str, str, str], tuple[str, str, dict[str, Any]]]


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create both tables (``table_schemas.task_workflow``). Idempotent."""
    from . import table_schemas

    table_schemas.task_workflow(conn)


# --- validation ---------------------------------------------------------------------


def _now() -> str:
    # Microseconds, not seconds: ``verify_check`` fences its write on the row's
    # ``recorded_at``, so two records inside one second must not share a value.
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ToolError(f"{name} must be a string of at most {limit} chars")
    return value.strip()


def _done(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > DONE_MAX_ITEMS:
        raise ToolError(f"done must be a list of at most {DONE_MAX_ITEMS} strings")
    out = [_text(item, "done item", DONE_ITEM_MAX) for item in value]
    return [item for item in out if item]


def _check_name(value: Any) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value.strip().lower()):
        raise ToolError("a check name is 1-64 chars of a-z, 0-9, '_', '.', ':' or '-'")
    return value.strip().lower()


def _expected(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > EXPECTED_MAX:
        raise ToolError(f"expected_checks must be a list of at most {EXPECTED_MAX} names")
    return sorted({_check_name(item) for item in value})


def _sha(value: Any, name: str = "head_sha") -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value.strip().lower()):
        raise ToolError(f"{name} must be a 7-40 character hex commit SHA")
    return value.strip().lower()


def _same_sha(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    return a.startswith(b) or b.startswith(a)


def _same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and config.identity_fold(a) == config.identity_fold(b)


# --- authority ----------------------------------------------------------------------


def _actor(actor: str | None) -> str:
    verified = config.verified_agent_identity()
    if verified:
        if actor and not _same(actor, verified) and not config.is_human_identity(verified):
            raise PermissionDeniedError(f"caller {verified!r} cannot act as {actor!r}")
        return actor or verified
    if not actor or not isinstance(actor, str) or not actor.strip():
        raise ToolError("actor is required when no verified identity is present")
    return actor.strip()


def _load_task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, status, claimed_by, next_owner FROM tasks WHERE id = ? AND org_id IS ?",
        (int(task_id), edition.tenant_org_id()),
    ).fetchone()
    if row is None:
        raise ToolError(f"task {task_id} not found")
    return row


def _may_write(task: sqlite3.Row, actor: str) -> bool:
    if config.is_human_identity(actor):
        return True
    from . import agent_lifecycle

    if any(_same(actor, op) for op in agent_lifecycle.operators()):
        return True
    return _same(actor, task["claimed_by"]) or _same(actor, task["next_owner"])


def _require_writer(task: sqlite3.Row, actor: str) -> None:
    if not _may_write(task, actor):
        raise PermissionDeniedError(
            f"{actor!r} may not write task {task['id']}'s workflow record: only its "
            "claimant, its next owner, a lifecycle operator or the operator may"
        )


# --- reads --------------------------------------------------------------------------


def _record_row(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM task_workflow WHERE task_id = ? AND org_id IS ?",
        (int(task_id), edition.tenant_org_id()),
    ).fetchone()


def _check_public(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        verification = json.loads(row["verification_json"]) if row["verification_json"] else None
    except ValueError:
        verification = None
    return {
        "name": row["name"],
        "head_sha": row["head_sha"],
        "outcome": row["outcome"],
        "grade": row["grade"],
        "ref": (
            {
                "kind": row["ref_kind"],
                "ref": row["ref"],
                "repo": row["repo"],
                "job": row["ref_job"],
            }
            if row["ref_kind"]
            else None
        ),
        "detail": row["detail"],
        "verification": verification,
        # What the verified grade stands on: the named job, or only the whole run.
        "verified_scope": (
            ((verification or {}).get("observed") or {}).get("scope")
            if row["grade"] == VERIFIED
            else None
        ),
        "recorded_by": row["recorded_by"],
        "recorded_at": row["recorded_at"],
        "verified_at": row["verified_at"],
    }


def graded_checks(
    rows: list[Mapping[str, Any]], head_sha: str | None, expected: list[str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One graded entry per check name, judged at ``head_sha``.

    A check recorded only at another head is ``unknown`` at this one, with the
    newest such head as ``stale_head``. An expected check nobody recorded is
    ``unknown`` too. With no ``head_sha`` the newest row per name is shown as is.
    """
    newest: dict[str, Mapping[str, Any]] = {}
    at_head: dict[str, Mapping[str, Any]] = {}
    for row in sorted(rows, key=lambda r: (r["recorded_at"], r["id"])):
        newest[row["name"]] = row
        if head_sha and _same_sha(row["head_sha"], head_sha):
            at_head[row["name"]] = row
    names = sorted(set(newest) | set(expected))
    checks: list[dict[str, Any]] = []
    for name in names:
        row = at_head.get(name) if head_sha else newest.get(name)
        if row is not None:
            checks.append(_check_public(row))
            continue
        stale = newest.get(name)
        checks.append(
            {
                "name": name,
                "head_sha": head_sha,
                "outcome": None,
                "grade": UNKNOWN,
                "stale_head": stale["head_sha"] if stale else None,
                "expected": name in expected,
            }
        )
    summary = {grade: sorted(c["name"] for c in checks if c["grade"] == grade) for grade in GRADES}
    # Verified against the run's overall conclusion only: no job was named, so
    # nothing ties the check's name to a job that ran (a skipped one included).
    summary["verified_run_only"] = sorted(
        c["name"] for c in checks if c["grade"] == VERIFIED and c.get("verified_scope") != SCOPE_JOB
    )
    summary["failing"] = sorted(c["name"] for c in checks if c.get("outcome") == FAIL)
    summary["contradicted"] = sorted(
        c["name"]
        for c in checks
        if c["grade"] == ATTESTED and (c.get("verification") or {}).get("verdict") == "mismatch"
    )
    return checks, summary


def _view(conn: sqlite3.Connection, task_id: int, head_sha: str | None = None) -> dict[str, Any]:
    row = _record_row(conn, task_id)
    expected: list[str] = []
    record: dict[str, Any] = {
        "task_id": int(task_id),
        "stage": None,
        "capsule": None,
        "head_sha": None,
        "revision": 0,
        "updated_by": None,
        "updated_at": None,
    }
    if row is not None:
        expected = json.loads(row["expected_json"] or "[]")
        record.update(
            stage=row["stage"],
            capsule={
                "goal": row["goal"],
                "done": json.loads(row["done_json"] or "[]"),
                "focus": row["focus"],
            },
            head_sha=row["head_sha"],
            revision=row["revision"],
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
        )
    head = head_sha or record["head_sha"]
    rows = conn.execute(
        "SELECT * FROM task_checks WHERE task_id = ? AND org_id IS ? "
        "ORDER BY recorded_at DESC, id DESC LIMIT ?",
        (int(task_id), edition.tenant_org_id(), CHECKS_PER_TASK),
    ).fetchall()
    checks, summary = graded_checks([dict(r) for r in rows], head, expected)
    record.update(expected_checks=expected, judged_at_head=head, checks=checks, summary=summary)
    record["trust"] = {
        "level": "untrusted",
        "source": "agent_written",
        "note": "Stage, capsule and attested checks are agent-written (T2); only a "
        "verified grade was confirmed by the hub.",
    }
    return record


def get(task_id: int, head_sha: str | None = None) -> dict[str, Any]:
    """The task's workflow record with every check graded at ``head_sha``
    (default: the head the capsule names)."""
    head = _sha(head_sha) if head_sha else None
    conn = storage.connect()
    try:
        _load_task(conn, task_id)
        return _view(conn, task_id, head)
    finally:
        conn.close()


def packet_section(conn: Any, task_id: int | None) -> dict[str, Any] | None:
    """The compact block ``session_packet`` carries; ``None`` when nothing is recorded."""
    if task_id is None:
        return None
    try:
        view = _view(conn, int(task_id))
    except (sqlite3.Error, ValueError):
        return None
    if view["revision"] == 0 and not view["checks"]:
        return None
    return {
        "stage": view["stage"],
        "capsule": view["capsule"],
        "head_sha": view["head_sha"],
        "revision": view["revision"],
        "updated_at": view["updated_at"],
        "checks": {c["name"]: c["grade"] for c in view["checks"]},
        "failing": view["summary"]["failing"],
        "contradicted": view["summary"]["contradicted"],
    }


# --- writes -------------------------------------------------------------------------


def set_capsule(
    task_id: int,
    *,
    stage: str | None = None,
    goal: str | None = None,
    done: list[str] | None = None,
    focus: str | None = None,
    head_sha: str | None = None,
    expected_checks: list[str] | None = None,
    expected_revision: int | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Set the stage and resume capsule. Omitted fields keep their value; ``done``
    replaces the list. Returns the record."""
    who = _actor(actor)
    fields: dict[str, Any] = {}
    if stage is not None:
        if stage not in STAGES:
            raise ToolError(f"stage must be one of {STAGES}")
        fields["stage"] = stage
    if goal is not None:
        fields["goal"] = _text(goal, "goal", GOAL_MAX) or None
    if done is not None:
        fields["done_json"] = json.dumps(_done(done))
    if focus is not None:
        fields["focus"] = _text(focus, "focus", FOCUS_MAX) or None
    if head_sha is not None:
        fields["head_sha"] = _sha(head_sha)
    if expected_checks is not None:
        fields["expected_json"] = json.dumps(_expected(expected_checks))
    if not fields:
        raise ToolError(
            "nothing to set: pass stage, goal, done, focus, head_sha or expected_checks"
        )
    if expected_revision is not None and (
        isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
    ):
        raise ToolError("expected_revision must be an integer")
    tenant = edition.tenant_org_id()
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = _load_task(conn, task_id)
        _require_writer(task, who)
        current = _record_row(conn, task_id)
        revision = current["revision"] if current is not None else 0
        if expected_revision is not None and expected_revision != revision:
            raise ToolError(
                f"task {task_id}'s workflow record is at revision {revision}, "
                f"not {expected_revision}; read it again"
            )
        now = _now()
        if current is None:
            conn.execute(
                "INSERT INTO task_workflow (task_id, org_id, revision, updated_by, updated_at) "
                "VALUES (?, ?, 0, ?, ?)",
                (int(task_id), tenant, who, now),
            )
        assignments = ", ".join(f"{col} = ?" for col in fields)
        conn.execute(
            f"UPDATE task_workflow SET {assignments}, revision = revision + 1, "
            "updated_by = ?, updated_at = ? WHERE task_id = ? AND org_id IS ?",
            (*fields.values(), who, now, int(task_id), tenant),
        )
        events.record_event_once_on_connection(
            conn,
            CHANNEL,
            {
                "kind": "capsule",
                "task_id": int(task_id),
                "stage": fields.get("stage"),
                "fields": sorted(fields),
                "revision": revision + 1,
            },
            originator=who,
        )
        conn.commit()
        return _view(conn, task_id)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ref(ref: Any) -> tuple[str, str, str | None, str | None] | None:
    if ref is None:
        return None
    if not isinstance(ref, Mapping):
        raise ToolError("ref must be an object {kind, ref, repo, job}")
    kind = ref.get("kind")
    value = str(ref.get("ref") or "").strip().lstrip("#")
    repo = ref.get("repo")
    job = ref.get("job")
    if kind not in REF_KINDS:
        raise ToolError(f"ref.kind must be one of {REF_KINDS}")
    if not _RUN_RE.fullmatch(value):
        raise ToolError("a ci_run ref is a numeric run id")
    if repo is not None and (not isinstance(repo, str) or not _REPO_RE.fullmatch(repo)):
        raise ToolError("ref.repo must be owner/name")
    if job is not None:
        if not isinstance(job, str) or not job.strip() or len(job.strip()) > JOB_MAX:
            raise ToolError(f"ref.job must be the job's name, 1-{JOB_MAX} characters")
        job = job.strip()
    return kind, value, repo, job


def default_verifier(gh: Callable | None = None) -> Verifier:
    """Resolve a reference with the same resolvers handoff grounding uses."""
    from .messaging import grounding

    resolvers = grounding.default_resolvers(gh)

    def verify(
        kind: str, ref: str, repo: str, expect: str, head_sha: str
    ) -> tuple[str, str, dict[str, Any]]:
        outcome = resolvers[kind](
            grounding.Claim(kind=kind, ref=ref, repo=repo, expect=expect),
            grounding.Context(sender="", caller=None, reviewed_head_sha=head_sha),
        )
        return outcome.verdict, outcome.detail, dict(outcome.observed or {})

    return verify


def default_job_verifier(gh: Callable | None = None) -> JobVerifier:
    """Resolve one named job of a CI run: it must exist, have run and match.

    A run's overall conclusion says nothing about one job: a run is green with
    a job skipped, and a name that matches no job matches nothing. So the job
    is read by its exact name. Missing, skipped or with another conclusion is
    a ``mismatch``; not finished yet is ``insufficient``. A job that ran more
    than once in the run (a matrix or a re-run sharing the name) must agree.
    """
    from .messaging import grounding

    run = gh or grounding._run_gh

    def verify(ref: str, repo: str, job: str, expect: str) -> tuple[str, str, dict[str, Any]]:
        data, err = grounding._gh_json(
            run, ["run", "view", ref, "--repo", repo, "--json", "jobs,attempt"]
        )
        if err == "missing":
            return "mismatch", f"no CI run {ref} in {repo}", {}
        if data is None:
            return "insufficient", err or "unreadable", {}
        jobs = [j for j in data.get("jobs") or [] if isinstance(j, dict) and j.get("name") == job]
        observed: dict[str, Any] = {"job": job, "attempt": data.get("attempt")}
        if not jobs:
            return "mismatch", f"run {ref} has no job named {job!r}", observed
        states = sorted({str(j.get("status") or "").lower() for j in jobs})
        conclusions = sorted({str(j.get("conclusion") or "").lower() for j in jobs})
        observed.update(job_status=states, job_conclusion=conclusions)
        if states != ["completed"]:
            return "insufficient", f"job {job!r} is {', '.join(states)}, not finished", observed
        if conclusions == ["skipped"]:
            return "mismatch", f"job {job!r} was skipped: it ran nothing", observed
        if conclusions != [expect]:
            return (
                "mismatch",
                f"job {job!r} concluded {', '.join(conclusions)}, not {expect}",
                observed,
            )
        return "verified", f"job {job!r} concluded {expect}", observed

    return verify


def _verify_row(
    row: Mapping[str, Any], verifier: Verifier, job_verifier: JobVerifier | None = None
) -> tuple[str, dict[str, Any] | None, str | None]:
    """``(grade, verification, verified_at)`` for one stored check.

    The run is resolved first (it must be this head's and have the claimed
    conclusion). With a job named in the reference, that job is then resolved
    and its verdict is the check's. Without one, a verified grade stands on
    the run's overall conclusion only and says so (``scope: "run"``).
    """
    if not row["ref_kind"] or not row["repo"]:
        return ATTESTED, None, None
    expect = "success" if row["outcome"] == PASS else "failure"
    try:
        verdict, detail, observed = verifier(
            row["ref_kind"], row["ref"], row["repo"], expect, row["head_sha"]
        )
        observed = dict(observed or {})
        job = row.get("ref_job")
        if verdict == "verified" and job:
            verdict, detail, seen = (job_verifier or default_job_verifier())(
                row["ref"], row["repo"], job, expect
            )
            observed.update(seen or {}, scope=SCOPE_JOB)
        elif verdict == "verified":
            observed["scope"] = SCOPE_RUN
            detail = f"{detail}; the whole run only, no job was named"
    except Exception as exc:  # noqa: BLE001 - a resolver failure is "not verified", never an error
        verdict, detail, observed = "insufficient", f"resolver failed: {type(exc).__name__}", {}
    verification = {"verdict": verdict, "detail": str(detail)[:DETAIL_MAX], "observed": observed}
    if verdict == "verified":
        return VERIFIED, verification, _now()
    return ATTESTED, verification, None


def record_check(
    task_id: int,
    name: str,
    outcome: str,
    head_sha: str,
    *,
    ref: Mapping[str, Any] | None = None,
    detail: str | None = None,
    verify: bool = True,
    actor: str | None = None,
    verifier: Verifier | None = None,
    job_verifier: JobVerifier | None = None,
) -> dict[str, Any]:
    """Record one check result at one head, as ``attested``; with a resolvable
    ``ref`` and ``verify``, try to raise it to ``verified`` at once.

    ``ref.job`` names the CI job the check stands for. With it, the grade is
    that job's: missing or skipped contradicts the claim. Without it, a
    verified grade covers the run's overall conclusion only.
    """
    who = _actor(actor)
    check = _check_name(name)
    if outcome not in OUTCOMES:
        raise ToolError(f"outcome must be one of {OUTCOMES}")
    head = _sha(head_sha)
    parsed = _ref(ref)
    note = _text(detail, "detail", DETAIL_MAX) if detail is not None else None
    tenant = edition.tenant_org_id()
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = _load_task(conn, task_id)
        _require_writer(task, who)
        kind, value, repo, job = parsed if parsed else (None, None, None, None)
        conn.execute(
            "INSERT INTO task_checks (task_id, org_id, name, head_sha, outcome, grade, "
            "ref_kind, ref, repo, ref_job, detail, verification_json, recorded_by, recorded_at, "
            "verified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL) "
            "ON CONFLICT (task_id, name, head_sha) DO UPDATE SET outcome = excluded.outcome, "
            "grade = excluded.grade, ref_kind = excluded.ref_kind, ref = excluded.ref, "
            "repo = excluded.repo, ref_job = excluded.ref_job, detail = excluded.detail, "
            "verification_json = NULL, "
            "recorded_by = excluded.recorded_by, recorded_at = excluded.recorded_at, "
            "verified_at = NULL",
            (
                int(task_id),
                tenant,
                check,
                head,
                outcome,
                ATTESTED,
                kind,
                value,
                repo,
                job,
                note,
                who,
                _now(),
            ),
        )
        events.record_event_once_on_connection(
            conn,
            CHANNEL,
            {
                "kind": "check",
                "task_id": int(task_id),
                "name": check,
                "head_sha": head,
                "outcome": outcome,
                "grade": ATTESTED,
            },
            originator=who,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    if verify and parsed and repo:
        return verify_check(
            task_id, check, head, actor=who, verifier=verifier, job_verifier=job_verifier
        )
    return get(task_id, head)


def verify_check(
    task_id: int,
    name: str,
    head_sha: str,
    *,
    actor: str | None = None,
    verifier: Verifier | None = None,
    job_verifier: JobVerifier | None = None,
) -> dict[str, Any]:
    """Resolve a stored check's reference now and store the grade it earns.

    Any caller who can see the task may ask: verification only reads live state,
    and a failed resolution never lowers anything but the claim it tested. The
    resolver runs outside any transaction; the write is fenced on the row's
    ``recorded_at`` so a re-record during resolution is not overwritten.
    """
    check = _check_name(name)
    head = _sha(head_sha)
    who = _actor(actor)
    tenant = edition.tenant_org_id()
    conn = storage.connect()
    try:
        _load_task(conn, task_id)
        row = conn.execute(
            "SELECT * FROM task_checks WHERE task_id = ? AND org_id IS ? AND name = ? "
            "AND head_sha = ?",
            (int(task_id), tenant, check, head),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ToolError(f"no check {check!r} recorded for task {task_id} at {head[:12]}")
    grade, verification, verified_at = _verify_row(
        dict(row), verifier or default_verifier(), job_verifier
    )
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            "UPDATE task_checks SET grade = ?, verification_json = ?, verified_at = ? "
            "WHERE id = ? AND recorded_at = ? AND org_id IS ?",
            (
                grade,
                json.dumps(verification) if verification else None,
                verified_at,
                row["id"],
                row["recorded_at"],
                tenant,
            ),
        ).rowcount
        if updated:
            events.record_event_once_on_connection(
                conn,
                CHANNEL,
                {
                    "kind": "verify",
                    "task_id": int(task_id),
                    "name": check,
                    "head_sha": head,
                    "grade": grade,
                    "verdict": (verification or {}).get("verdict"),
                },
                originator=who,
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get(task_id, head)


# --- coordination actions -----------------------------------------------------------


def task_workflow_get(task_id: int, head_sha: str | None = None) -> dict[str, Any]:
    return get(task_id, head_sha)


def task_workflow_set(
    task_id: int,
    stage: str | None = None,
    goal: str | None = None,
    done: list[str] | None = None,
    focus: str | None = None,
    head_sha: str | None = None,
    expected_checks: list[str] | None = None,
    expected_revision: int | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    return set_capsule(
        task_id,
        stage=stage,
        goal=goal,
        done=done,
        focus=focus,
        head_sha=head_sha,
        expected_checks=expected_checks,
        expected_revision=expected_revision,
        actor=actor,
    )


def task_check_record(
    task_id: int,
    name: str,
    outcome: str,
    head_sha: str,
    ref: dict[str, Any] | None = None,
    detail: str | None = None,
    verify: bool = True,
    actor: str | None = None,
) -> dict[str, Any]:
    return record_check(
        task_id, name, outcome, head_sha, ref=ref, detail=detail, verify=verify, actor=actor
    )


def task_check_verify(
    task_id: int, name: str, head_sha: str, actor: str | None = None
) -> dict[str, Any]:
    return verify_check(task_id, name, head_sha, actor=actor)
