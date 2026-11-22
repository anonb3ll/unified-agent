"""Read-only hub-local check of human-attested review and release approval.

Run immediately before the steward's existing --match-head-commit merge.
The merge is refused unless the approved commit equals the live head and
every required CI check is SUCCESS on that head. A failing, skipped,
pending or missing required check does not clear it. This does not merge,
consume approvals, publish checks or change GitHub rules.

It also applies the narrow head-moved rule (task #2259, operator decision 2026-10-31): a
commit that lands after a recorded review read is fine when it changes nothing
red-tier, and is held when it touches a red-tier path and no delta read naming
those files was recorded. It does not require a read to exist — the Ledger
approval and its independent review, both bound to this exact head, remain the
authority — so an unread PR is not refused for being unread here.

It also reads the operator's review-hold decisions (``review_holds``, task #2514),
each hold on its own: while any hold's current decision is a ``keep`` recorded at
or after the approval's resolution, the merge is refused. A ``release`` of that
same hold, or a fresh approval after the keep, clears it; a release of another
hold does not. A release is never an approval; the Ledger approval above stays the
authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from . import (
    config,
    events,
    github_app,
    independent_review,
    org_partition,
    pr_models,
    required_settings,
    review_holds,
    review_provenance,
    review_reads,
    storage,
)
from .cycle_eligibility import _is_red_tier
from .tools import approvals

_HEAD = re.compile(r"[0-9a-f]{40}")
_PR_JSON = "headRefOid,state,isDraft,baseRefName,statusCheckRollup"
#: Kinds that count as a read of the whole head, not of a delta.
_FULL_READ_KINDS = frozenset(review_reads.FULL_READ_KINDS)
_DELTA_READ = review_reads.DELTA_READ
# How many recorded reads to test for ancestry before giving up. Reads are
# newest-first, so the useful one is almost always the first.
_MAX_READ_CANDIDATES = 3
# GitHub's compare endpoint truncates `files` at 300 entries.
_MAX_COMPARE_FILES = 300
_COMPARE_JSON_LIMIT = 2 * 1024 * 1024
# GitHub truncates a check title and a status description here.
_CHECK_SUMMARY_MAX = 140
#: A verdict passes its head only when it opens with one of these words,
#: optionally after a ``code``/``delta``/``full`` qualifier, and asks no
#: question (task #2420). It is the wording the recorded reads actually use:
#: ``pass``, ``pass-after-delta``, ``SHIP: ...``, ``Code PASS; CI pending``,
#: ``delta pass: ...`` and the pipeline's ``validated``. Anything else
#: (``fix-first: ...``, ``hold``, ``fail``, ``pass?``, a sentence that names no
#: verdict) is not a pass: the gate fails closed on wording it cannot read.
_PASSING_VERDICT = re.compile(
    r"(?:(?:code|delta|full)\s+)?(?:pass(?:ed)?|ship|validated)(?![\w?])(?!.*\?)",
    re.IGNORECASE | re.DOTALL,
)


class PreflightError(RuntimeError):
    """A static, safe reason that keeps the selected head held."""


class ReviewHeadUnavailable(PreflightError):
    """GitHub did not answer, which says nothing about whether the head was read."""


class ReadRejected(PreflightError):
    """A recorded read did not pass the head the gate would count (task #2420).

    A ``PreflightError`` like any other refusal; the subclass only lets
    ``review_head_check`` name the status ``read-rejected``.
    """


class RebasedOffReads(PreflightError):
    """Recorded reads exist but a rebase put the head off every one of them.

    A ``PreflightError`` like any other refusal; the subclass only lets
    ``review_head_check`` attach report-only provenance (task #2398).
    """


#: Checks the merge bar adds to ``config.review_ci_required_checks()``. That
#: set decides when a reviewer may skip local pytest and is shard-only; a merge
#: also needs the lint/layer gates and the full-verification join to be green.
MERGE_EXTRA_REQUIRED_CHECKS: tuple[str, ...] = ("CI / gates", "CI / full-verification")


def merge_required_checks() -> list[str]:
    """Every check name that must be SUCCESS on the head before a merge."""
    names = [*config.review_ci_required_checks(), *MERGE_EXTRA_REQUIRED_CHECKS]
    return list(dict.fromkeys(names))


def _required_ci_block(head_sha: str, statuses: list) -> str | None:
    """Why required CI is not SUCCESS on ``head_sha``, or None when it is.

    Same rule as ``reviewer._exact_head_ci_pytest_ok`` over
    ``merge_required_checks()``: a required check must be SUCCESS, and a skipped entry never hides a real result
    (``pr_models.prefer_check_state``). A status pinned to another commit
    does not count for this head.
    """
    conclusions: dict[str, str] = {}
    for index, check in enumerate(statuses):
        if not isinstance(check, dict):
            continue
        pinned = check.get("sha")
        if isinstance(pinned, str) and pinned != head_sha:
            continue
        name = pr_models.rollup_check_name(check, index)
        state = check.get("conclusion") or check.get("state") or check.get("status")
        if name and state:
            conclusions[name] = pr_models.prefer_check_state(conclusions.get(name), str(state))
    missing: list[str] = []
    for name in merge_required_checks():
        state = conclusions.get(name)
        if state is None or state.upper() != "SUCCESS":
            missing.append(f"{name}={state or 'absent'}")
    if not missing:
        return None
    return "required checks not SUCCESS: " + "; ".join(missing[:4])


def merge_admission(
    *,
    approved_sha: str,
    head_sha: str,
    statuses: list | None = None,
) -> dict[str, str]:
    """Whether the steward may merge this head.

    ``refuse`` when the approved commit is not the live head. ``wait`` when
    a required CI check on that head is missing, pending, skipped, failing,
    or otherwise not SUCCESS. ``merge`` only when the head matches and every
    required check is SUCCESS.
    """
    if not isinstance(approved_sha, str) or approved_sha != head_sha:
        shown = approved_sha if isinstance(approved_sha, str) and approved_sha else "(none)"
        return {
            "action": "refuse",
            "reason": f"Approved commit {shown} is not the current head {head_sha}.",
        }
    status_rows = statuses if isinstance(statuses, list) else []
    blocked = _required_ci_block(head_sha, status_rows)
    if blocked:
        return {"action": "wait", "reason": blocked}
    return {"action": "merge", "reason": "Approved head matches and required CI is green."}


def _live_head(repo: str, number: int, head_sha: str, run_gh: Callable) -> None:
    try:
        result = run_gh(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                _PR_JSON,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
            raise PreflightError("Live PR metadata is unavailable.")
        snapshot = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        raise PreflightError("Live PR metadata is unavailable.") from None
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("headRefOid") != head_sha
        or snapshot.get("state") != "OPEN"
        or snapshot.get("isDraft") is not False
        or snapshot.get("baseRefName") != "main"
    ):
        raise PreflightError("Live PR is not the selected open, non-draft main head.")
    statuses = snapshot.get("statusCheckRollup", [])
    if not isinstance(statuses, list):
        raise PreflightError("Live PR CI evidence is unavailable.")
    decision = merge_admission(approved_sha=head_sha, head_sha=head_sha, statuses=statuses)
    if decision["action"] != "merge":
        raise PreflightError(decision["reason"])


def _current_binding(conn: sqlite3.Connection, row: sqlite3.Row, repo: str, number: int) -> None:
    latest = conn.execute(
        "SELECT id FROM approvals WHERE tool=? AND json_extract(payload,'$.connector')=? "
        "AND json_extract(payload,'$.repo')=? AND json_extract(payload,'$.number')=? "
        "ORDER BY id DESC LIMIT 1",
        (github_app.TOOL_NAME, github_app.GITHUB_APP_CONNECTOR, repo, number),
    ).fetchone()
    superseded = conn.execute(
        "SELECT 1 FROM event_log WHERE channel=? AND json_extract(payload,'$.approval_id')=? "
        "LIMIT 1",
        (github_app.SUPERSEDED_CHANNEL, row["id"]),
    ).fetchone()
    if latest is None or latest["id"] != row["id"] or superseded is not None:
        raise PreflightError("Selected approval was superseded; select a fresh current binding.")


def _verified_resolution(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Require the native authenticated resolution event, not a claimed name."""
    resolution = conn.execute(
        "SELECT ulid,channel,payload,originator,episode_id,causal_parent_id,created_at,"
        "dedupe_key,prev_hash,entry_hash FROM event_log WHERE dedupe_key=? AND channel=?",
        (f"approval-resolution:{row['id']}:approved", approvals.CHANNEL_APPROVAL_RESOLVED),
    ).fetchone()
    if resolution is None:
        raise PreflightError("Selected approval lacks verified resolution evidence.")
    evidence = json.loads(resolution["payload"])
    if (
        not isinstance(evidence, dict)
        or type(evidence.get("approval_id")) is not int
        or evidence.get("approval_id") != row["id"]
        or evidence.get("tool") != row["tool"]
        or evidence.get("decision") != "approve"
        or evidence.get("status") != "approved"
        or evidence.get("prior_status") != "pending"
        or evidence.get("requested_by") != row["requested_by"]
        or evidence.get("resolved_by") != row["resolved_by"]
        or not row["resolved_by"]
        or evidence.get("resolver_verified") != row["resolved_by"]
        or resolution["originator"] != row["resolved_by"]
        or not row["resolved_at"]
        or evidence.get("resolved_at") != row["resolved_at"]
        or evidence.get("payload_sha256")
        != hashlib.sha256(row["payload"].encode("utf-8")).hexdigest()
        or not resolution["prev_hash"]
        or resolution["entry_hash"]
        != events.compute_entry_hash(
            resolution["prev_hash"],
            resolution["ulid"],
            resolution["channel"],
            resolution["payload"],
            resolution["originator"],
            resolution["episode_id"],
            resolution["causal_parent_id"],
            resolution["created_at"],
            resolution["dedupe_key"],
        )
    ):
        raise PreflightError("Selected approval lacks matching verified resolution evidence.")


def _operator_hold(conn: sqlite3.Connection, row: sqlite3.Row, repo: str, number: int) -> None:
    """Refuse while any review hold on this PR is kept after the approval (each hold
    judged on its own; ``review_holds.standing_keeps``)."""
    kept = review_holds.standing_keeps(conn, repo, number, row["resolved_at"])
    if kept:
        tasks = ", ".join(f"#{held['task_id']}" for held in kept)
        raise PreflightError(
            f"The operator kept the review hold on task {tasks} at "
            f"{kept[-1]['decided_at']}, after this approval. Release each kept hold in the "
            "Control Center (POST /api/action/review-hold) or approve the current head again."
        )


def _human_binding(repo: str, number: int, head_sha: str, approval_id: int) -> dict:
    try:
        path = storage.require_live_task_store()
        with closing(org_partition.open_store(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id,tool,payload,status,requested_by,resolved_by,resolved_at,created_at "
                "FROM approvals WHERE id=?",
                (approval_id,),
            ).fetchone()
            if row is not None:
                _current_binding(conn, row, repo, number)
                _verified_resolution(conn, row)
                _operator_hold(conn, row, repo, number)
        if row is None:
            raise PreflightError("Selected approval does not exist in the hub Ledger.")
        payload = json.loads(row["payload"])
    except (storage.LiveStoreError, sqlite3.Error, ValueError, TypeError):
        raise PreflightError("Authoritative hub approval evidence is unavailable.") from None
    if isinstance(payload, dict):
        approved_sha = payload.get("head_sha")
        decision = merge_admission(
            approved_sha=approved_sha if isinstance(approved_sha, str) else "",
            head_sha=head_sha,
        )
        if decision["action"] == "refuse":
            raise PreflightError(decision["reason"])
    if (
        row["tool"] != github_app.TOOL_NAME
        or not isinstance(payload, dict)
        or payload.get("connector") != github_app.GITHUB_APP_CONNECTOR
        or payload.get("repo") != repo
        or type(payload.get("number")) is not int
        or payload.get("number") != number
        or payload.get("base_branch") != "main"
        or payload.get("state") != "OPEN"
        or payload.get("merged") is not False
    ):
        raise PreflightError("Selected approval is not bound to this repo, PR and head.")
    if row["status"] not in {"approved", "consumed"}:
        raise PreflightError("Selected approval has no approved human decision.")
    if config.actor_kind_for_identity(row["resolved_by"]) != "human" or approvals._same_person(
        row["requested_by"], row["resolved_by"]
    ):
        raise PreflightError("Selected approval lacks an independent human resolver.")
    if row["status"] == "approved":
        expires_at = approvals._pass_expires_at(row)
        if expires_at is None or expires_at <= datetime.now(UTC):
            raise PreflightError("Selected approval pass has expired or cannot be dated.")
    try:
        review = independent_review.validate(
            payload.get("independent_review"), repo=repo, number=number, head_sha=head_sha
        )
    except independent_review.ReviewEvidenceError as exc:
        raise PreflightError(str(exc)) from None
    return {"approval_status": str(row["status"]), "independent_review": review}


def verdict_passes(verdict: object) -> bool:
    """Whether a recorded read's free-text verdict passes the head it names."""
    return isinstance(verdict, str) and _PASSING_VERDICT.match(verdict.strip()) is not None


def _read_order(row: dict) -> tuple[str, int]:
    return (str(row["created_at"]), int(row["id"]))


def _readers_at(every: list[dict], head_sha: str) -> list[str]:
    """Every reader who recorded a read at ``head_sha``, newest first.

    One row per reader (task #2641), so the gate can name all of them rather
    than only whichever row it happened to select first.
    """
    out: list[str] = []
    for row in every:  # newest first
        if row["head_sha"] == head_sha and row["reader"] not in out:
            out.append(row["reader"])
    return out


def _hold_if_rejected(rejected: list[dict], *, head: str, granting: dict | None = None) -> None:
    """Refuse when a read that did not pass stands against ``head``.

    ``rejected`` is newest first. A head carrying any read that did not pass is
    no head to pass from, whichever other kind passed it: a pipeline
    ``validated`` cannot outvote a steward's ``fix-first`` at the same tree.
    A rejection recorded at any head after ``granting`` also holds, because the
    read that would grant the pass predates it; only a read recorded after the
    rejection can clear it. The negative row itself is never touched.
    """
    for row in rejected:
        newer = granting is not None and _read_order(row) > _read_order(granting)
        if row["head_sha"] == head or newer:
            raise ReadRejected(
                f"Recorded verdict {row['verdict']!r} at {row['head_sha'][:12]} is not a pass "
                f"({row['kind']} by {row['reader']}). It holds until a read recorded after it "
                "passes this head."
            )


def review_head_gate(
    *,
    repo: str,
    number: int,
    head_sha: str,
    run_gh: Callable = subprocess.run,
    conn: Any | None = None,
) -> dict:
    """Whether this head may merge under the narrow head-moved rule (task #2259).

    The rule the operator chose on 2026-10-31: a commit that lands after a recorded
    read is acceptable when the delta it adds touches nothing red-tier, and is
    held when it does and no delta read naming those files was recorded.

    Three ways to pass, in order of strength:

    1. ``read-at-head`` — a steward, policy or human read is recorded for
       exactly this head.
    2. ``delta-read`` — a delta read is recorded at this head and the files it
       names cover every red-tier path the delta actually added.
    3. ``delta-clean`` — the newest recorded *full* read is an ancestor and the
       delta since then touches no red-tier path.

    ``no-read-recorded`` passes too: this gate refuses a head that *moved away
    from* a recorded read, not a PR nobody recorded a read for. The Ledger
    approval plus its independent review, bound to this exact head, remain the
    authority for that case.

    A moved head that a rebase put off every recorded read fails closed: no
    delta can be proven against a base that is no longer an ancestor, which is
    the #1327 class itself.

    A delta read counts only at its own head, and only when it passes there:
    a full read is recorded at its ``from_sha`` (task #2294; the writer refuses
    any other, and a row written around it is ignored here), ``from_sha`` is an
    ancestor, the comparison is not truncated, and its files cover every
    red-tier path. It is never an ancestor for ``delta-clean``: a delta read at
    B that failed those checks would otherwise pass a later docs-only push to C
    on B..C alone, with A..B never proven. After a delta read, a further push
    needs a delta read from the last full read again.

    Every route also needs its read to have passed (task #2420): a read whose
    verdict is not a pass (``verdict_passes``) is honest evidence that is kept,
    and it holds the head it names, any head counted from it, and every pass
    recorded before it. Only a read recorded after it can pass the PR again.
    """
    every = review_reads.for_pr(repo, number, conn=conn)
    if not every:
        return {"status": "no-read-recorded", "read_head": None, "red_tier_delta": []}
    rejected = [row for row in every if not verdict_passes(row["verdict"])]
    _hold_if_rejected(rejected, head=head_sha)
    full_heads = {row["head_sha"] for row in every if row["kind"] in _FULL_READ_KINDS}
    rows = [
        row
        for row in every
        if row["kind"] in _FULL_READ_KINDS
        or (row["kind"] == _DELTA_READ and row["from_sha"] in full_heads)
    ]
    unanchored = [row for row in every if row not in rows]
    if not rows:
        raise PreflightError(_unanchored_reason(unanchored))

    for row in rows:
        if row["head_sha"] == head_sha and row["kind"] in _FULL_READ_KINDS:
            _hold_if_rejected(rejected, head=head_sha, granting=row)
            return {
                "status": "read-at-head",
                "read_head": head_sha,
                "reader": row["reader"],
                "readers": _readers_at(every, head_sha),
                "kind": row["kind"],
                "red_tier_delta": [],
            }

    delta_rows = [row for row in rows if row["head_sha"] == head_sha and row["kind"] == _DELTA_READ]
    for row in delta_rows:
        changed = _changed_between(repo, row["from_sha"], head_sha, run_gh)
        if changed is None:
            continue
        _hold_if_rejected(rejected, head=row["from_sha"], granting=row)
        red = _red_tier(changed)
        missing = [path for path in red if path not in set(row["files"])]
        if missing:
            raise PreflightError(_moved_reason(row["from_sha"], head_sha, missing))
        return {
            "status": "delta-read",
            "read_head": row["from_sha"],
            "reader": row["reader"],
            "readers": _readers_at(every, head_sha),
            "red_tier_delta": red,
        }

    checked = 0
    for row in rows:
        if row["kind"] not in _FULL_READ_KINDS:
            continue
        if row["head_sha"] == head_sha or checked >= _MAX_READ_CANDIDATES:
            continue
        checked += 1
        changed = _changed_between(repo, row["head_sha"], head_sha, run_gh)
        if changed is None:
            continue
        _hold_if_rejected(rejected, head=row["head_sha"], granting=row)
        red = _red_tier(changed)
        if red:
            raise PreflightError(_moved_reason(row["head_sha"], head_sha, red))
        return {
            "status": "delta-clean",
            "read_head": row["head_sha"],
            "reader": row["reader"],
            # The read was recorded at the ancestor head, not the merging head;
            # name the readers of *that* head or the receipt drops every reader
            # but the selected one (task #2641).
            "readers": _readers_at(every, row["head_sha"]),
            "red_tier_delta": [],
        }

    raise RebasedOffReads(
        f"Recorded reads exist for this pull request, but none is an ancestor of {head_sha[:12]}; "
        "a rebased head has to be read again."
        + (f" {_unanchored_reason(unanchored)}" if unanchored else "")
    )


def review_head_check(
    *,
    repo: str,
    number: int,
    head_sha: str,
    run_gh: Callable = subprocess.run,
    conn: Any | None = None,
) -> dict:
    """The strict answer behind the required ``unified-agent/review-head`` check.

    ``review_head_gate`` lets ``no-read-recorded`` through because a preflight
    must not block a PR the Ledger already authorized. A required check has the
    opposite duty, so this entry point (task #2320) maps the same gate without
    that pass:

    ===================  ===================
    gate answer          ``conclusion``
    ===================  ===================
    read-at-head         ``success``
    delta-read           ``success``
    delta-clean          ``success``
    no-read-recorded     ``action_required``
    read-rejected        ``failure``
    refused              ``failure``
    ===================  ===================

    ``read-rejected`` (task #2420) is a recorded read whose verdict is not a
    pass, standing against this head: the conclusion follows the verdict, not
    only the existence of a read, on every route the gate passes by.

    A refusal is a head that moved off what was read: a rebase off every read
    (the #1327 class), a red-tier delta no delta read covers, a delta too large
    to prove clean, or a delta read with no full read beneath it. It stays
    ``failure`` until a covering read is recorded.

    ``ReviewHeadUnavailable`` propagates. When GitHub cannot answer, nothing is
    known about the head, so the caller publishes nothing and retries; that
    answer must never become a conclusion in either direction.
    """
    try:
        gate = review_head_gate(
            repo=repo, number=number, head_sha=head_sha, run_gh=run_gh, conn=conn
        )
    except ReviewHeadUnavailable:
        raise
    except PreflightError as exc:
        answer = {
            "status": "read-rejected" if isinstance(exc, ReadRejected) else "head-moved",
            "conclusion": "failure",
            "summary": str(exc)[:_CHECK_SUMMARY_MAX],
            "read_head": None,
        }
        if isinstance(exc, RebasedOffReads):
            _attach_provenance(
                answer,
                str(exc),
                repo=repo,
                number=number,
                head_sha=head_sha,
                run_gh=run_gh,
                conn=conn,
            )
        return answer
    status = gate["status"]
    read_head = gate.get("read_head")
    if status == "no-read-recorded":
        conclusion, summary = "action_required", "No review read recorded at this head"
    elif status == "read-at-head":
        conclusion, summary = "success", f"Read at this head ({gate['kind']})"
    elif status == "delta-read":
        conclusion = "success"
        summary = f"Delta read covers the red-tier change since {read_head[:12]}"
    elif status == "delta-clean":
        conclusion = "success"
        summary = f"No red-tier change since the read at {read_head[:12]}"
    else:
        raise PreflightError(f"Unknown review-head status: {status}")
    return {
        "status": status,
        "conclusion": conclusion,
        "summary": summary,
        "read_head": read_head,
    }


def _attach_provenance(
    answer: dict,
    message: str,
    *,
    repo: str,
    number: int,
    head_sha: str,
    run_gh: Callable,
    conn: Any | None,
) -> None:
    """Report-only (task #2398): say why the head moved, never change the answer.

    Every failure is swallowed, because provenance is measurement: the
    ``failure`` conclusion stands whether or not it can be explained.
    """
    try:
        found = review_provenance.observe(
            repo=repo, number=number, head_sha=head_sha, run_gh=run_gh, conn=conn
        )
        sentence = review_provenance.summary_sentence(found)
    except Exception:  # noqa: BLE001 - provenance must never alter the result
        logging.getLogger(__name__).warning("review provenance failed", exc_info=True)
        return
    answer["provenance"] = found
    answer["summary"] = _with_sentence(message, sentence)


def _with_sentence(message: str, sentence: str) -> str:
    """``message`` plus ``sentence`` within ``_CHECK_SUMMARY_MAX``, shortening
    ``message`` at a word boundary when both do not fit."""
    if len(message) + 1 + len(sentence) <= _CHECK_SUMMARY_MAX:
        return f"{message} {sentence}"
    room = _CHECK_SUMMARY_MAX - len(sentence) - 2
    if room < 20:
        return message[:_CHECK_SUMMARY_MAX]
    cut = message[:room].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{cut}\u2026 {sentence}"


def _unanchored_reason(rows: list[dict]) -> str:
    row = rows[0]
    return (
        f"The delta read at {row['head_sha'][:12]} starts from {str(row['from_sha'])[:12]}, "
        "where no full read is recorded, so it is not counted; record a full read."
    )


def _red_tier(paths: list[str]) -> list[str]:
    return sorted(_is_red_tier([{"file": path} for path in paths]))


def _moved_reason(read_head: str, head_sha: str, red: list[str]) -> str:
    shown = ", ".join(red[:5]) + (f" (+{len(red) - 5} more)" if len(red) > 5 else "")
    return (
        f"The head moved from the recorded read at {read_head[:12]} to {head_sha[:12]} "
        f"and changed red-tier {shown} with no delta read recorded for this head."
    )


def _changed_between(repo: str, base_sha: str, head_sha: str, run_gh: Callable) -> list[str] | None:
    """Paths changed between two commits, or ``None`` when ``base_sha`` is not
    an ancestor of ``head_sha``.

    GitHub's compare endpoint is the authority here because this check runs on
    the hub with no checkout. Its file list stops at 300 entries, and a list
    that may be truncated cannot prove a delta clean, so that answer is
    refused rather than read as "nothing red-tier changed".
    """
    try:
        result = run_gh(
            ["gh", "api", f"repos/{repo}/compare/{base_sha}...{head_sha}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise ReviewHeadUnavailable("Commit comparison is unavailable.")
        if len(result.stdout) > _COMPARE_JSON_LIMIT:
            # GitHub answered; the answer is too big to prove anything with.
            raise PreflightError("Commit comparison is unavailable.")
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        raise ReviewHeadUnavailable("Live PR metadata is unavailable.") from None
    if not isinstance(payload, dict):
        raise ReviewHeadUnavailable("Commit comparison is unavailable.")
    if payload.get("status") not in {"ahead", "identical"}:
        return None
    if payload.get("status") == "identical":
        return []
    files = payload.get("files")
    if not isinstance(files, list):
        # An "ahead" answer with no file list proves nothing; reading it as an
        # empty delta would pass a red-tier change as clean.
        raise ReviewHeadUnavailable("Commit comparison is unavailable.")
    if len(files) >= _MAX_COMPARE_FILES:
        raise PreflightError(
            "The change set is too large to prove clean; record a delta read instead."
        )
    # A rename is reported under its new name; the old path counts too, or
    # renaming a red-tier file away would read as a clean delta.
    names = {
        entry[key]
        for entry in files
        if isinstance(entry, dict)
        for key in ("filename", "previous_filename")
        if isinstance(entry.get(key), str)
    }
    return sorted(names)


def preflight(
    *,
    repo: str,
    number: int,
    head_sha: str,
    approval_id: int,
    run_gh: Callable = subprocess.run,
) -> dict:
    if (
        not required_settings.is_owner_repo(repo)
        or type(number) is not int
        or number < 1
        or not isinstance(head_sha, str)
        or _HEAD.fullmatch(head_sha) is None
        or type(approval_id) is not int
        or approval_id < 1
    ):
        raise PreflightError("Supply a repo, positive PR/approval IDs and full lowercase head SHA.")
    _live_head(repo, number, head_sha, run_gh)
    _human_binding(repo, number, head_sha, approval_id)
    review_head_gate(repo=repo, number=number, head_sha=head_sha, run_gh=run_gh)
    # Re-read both authorities after the first round trip. A push, rebase or
    # revocation during the check must invalidate the earlier observation.
    _live_head(repo, number, head_sha, run_gh)
    binding = _human_binding(repo, number, head_sha, approval_id)
    head_gate = review_head_gate(repo=repo, number=number, head_sha=head_sha, run_gh=run_gh)
    return {
        "status": "ready",
        "scope": "exact-head-human-attested-review",
        "repo": repo,
        "number": number,
        "head_sha": head_sha,
        "approval_id": approval_id,
        "approval_status": binding["approval_status"],
        "independent_review": independent_review.public_evidence(binding["independent_review"]),
        "review_head": head_gate,
    }


def _positive_int(value: str) -> int:
    try:
        result = int(value)
        if result > 0:
            return result
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("a positive integer is required")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", type=_positive_int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--approval", type=_positive_int, required=True)
    args = parser.parse_args(argv)
    try:
        result = preflight(
            repo=args.repo, number=args.pr, head_sha=args.head, approval_id=args.approval
        )
    except PreflightError as exc:
        print(json.dumps({"status": "held", "reason": str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
