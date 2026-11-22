"""Held-despite-green PR detection, and the `held` GitHub label (task #1260).

Company operating rule (`docs/company/OPERATING-SYSTEM.md` step 5, Epic C
#1188): "A PR held back despite green CI gets a regression test the same
day." This module gives that rule a precise, testable meaning:

- `held_despite_green()` — a pure function deciding whether a task/PR was
  held despite green CI, from its note history plus a caller-supplied set of
  CI conclusions for the exact head commit. No I/O, no subprocess — the same
  "pure decision, impure gathering" split `delivery_slo.py` uses, so this is
  unit-testable on synthetic data and reusable by both
  `scripts/check_held_pr_regression_test.py` (the CI gate) and
  `review_cli.py label-held` (the operator command below).
- `label_held_pr()` — applies the GitHub `held` label to a PR via `gh pr
  edit --add-label`.

**Why the label write lives here and not in `reviewer.py`.** `reviewer.py`'s
validate+hold contract (module docstring; `tests/test_reviewer_pipeline.py::
test_no_remote_write_invocations_in_pipeline_sources`) keeps its unattended
cron pipeline strictly read-only against GitHub — every `gh` call it makes is
`pr view`, nothing ever pushes or mutates a PR — because that pipeline runs
without a human in the loop and must never act on the operator's behalf. A `held`
label is not a push or a merge, but it IS a GitHub write, so it does not
belong in that unattended path either.

The automated pipeline also never records a `rework` verdict in the first
place: `[review: rework ...]` is exclusively a *human* closeout note (see
`tools/delegate.py`'s `_REVIEW_ANNOTATION_RE` comment, "the human closeout" —
confirmed by grepping every non-test, non-doc source file in this repo: none
of them writes that string). So there is today no automated moment for a
label-write to hook into. `label_held_pr()` is therefore wired to one
explicit, opt-in operator surface instead: `unified-agent-review label-held
<task_id>` (`review_cli.py`), run by whoever just typed the `[review:
rework]` note on a PR that had green CI. Until that command runs, applying
the label is a manual step — see AGENTS.md and docs/automation.md.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

HELD_LABEL = "held"

# The pipeline's own "gates green, held for human merge" verdict
# (reviewer.REVIEW_VALIDATED / docs/automation.md's marker table). Only used
# to annotate a positive `HeldVerdict.validated_note` for diagnostics; it is
# not required for the held decision itself — a caller-supplied
# `ci_conclusions` is the source of truth for "was CI green", since that is
# what step 5 of the company OS actually means (exact-head *CI*, not this
# pipeline's own internal gate run).
_VALIDATED_RE = re.compile(r"\[review:\s*validated\b[^\]]*\]", re.IGNORECASE)

# Same bracket shape `tools.delegate._REVIEW_ANNOTATION_RE` parses. Only the
# documented verdict counts: the annotation must *start* with `rework`
# (`[review: rework]`, `[review: rework — reason]`). Substrings such as "fix"
# or "changes" elsewhere in an annotation are not a hold.
_REVIEW_NOTE_RE = re.compile(r"\[review:\s*([^\]]+)\]", re.IGNORECASE)
_REWORK_VERDICT_RE = re.compile(r"^rework\b", re.IGNORECASE)

# GitHub Checks API `conclusion` values that are not a failure. "skipped"
# covers this repo's own changes-gated jobs (protocol/client/web only run
# when their sources moved — see .github/workflows/ci.yml's `changes` job);
# treating a skip as red would make every Python-only PR permanently
# "not green". "neutral" is a valid non-blocking outcome some third-party
# checks report. Anything else (failure, cancelled, timed_out,
# action_required, stale, or a plain commit-status "error"/"pending") is red.
_GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


@dataclass(frozen=True)
class HeldVerdict:
    """Result of `held_despite_green()`. `held` is the only field CI needs."""

    held: bool
    reason: str
    validated_note: str | None = None
    rework_note: str | None = None


def _last_rework_note(notes: str) -> str | None:
    """The outstanding `[review: rework]` note, or None after approval.

    Task notes are append-only (`reviewer._append_note`), so later position
    in the string is later in time. A later approval closes the rework round;
    another later rework opens a new one.
    """
    last: str | None = None
    for m in _REVIEW_NOTE_RE.finditer(notes or ""):
        if _REWORK_VERDICT_RE.match(m.group(1).strip()):
            last = m.group(0)
        elif re.match(r"^approved\b", m.group(1).strip(), re.IGNORECASE):
            last = None
    return last


def is_ci_green(conclusions: Any) -> bool | None:
    """Classify CI conclusions for one commit sha.

    Accepts an iterable of conclusion strings, or a mapping of
    check-name -> conclusion (only the values are used). Returns:
      - True  — at least one conclusion, and every one is green.
      - False — at least one conclusion is red.
      - None  — no data at all (empty/None). Absent data is "unknown", never
        "green" — a caller that could not reach the Checks API must not have
        that silently read as CI having passed.
    """
    if conclusions is None:
        values: list[Any] = []
    elif isinstance(conclusions, dict):
        values = list(conclusions.values())
    else:
        values = list(conclusions)
    if not values:
        return None
    return all(str(v).lower() in _GREEN_CONCLUSIONS for v in values)


def held_despite_green(
    notes: str = "",
    *,
    verdict: str | None = None,
    ci_conclusions: Any = None,
) -> HeldVerdict:
    """Decide whether a PR/task is "held despite green" (company OS step 5).

    Held requires BOTH:
      1. A rework signal — `verdict == "rework"` (a structured verdict a
         caller may already have, e.g. a PR review decision) OR a
         `[review: rework]`-style task note (`_last_rework_note`); and
      2. `ci_conclusions` is non-empty and every conclusion is green
         (`is_ci_green` returns True) for the exact head commit — i.e. CI
         was NOT the reason it was held.

    No CI data (`ci_conclusions` empty/None) never counts as held: this
    never false-positives just because a caller couldn't reach the Checks
    API. A rework signal with red or unknown CI is correctly *not* "held
    despite green" — it was held because of red CI, or for an unrelated
    reason unconnected to a green build.
    """
    rework_note = _last_rework_note(notes)
    has_rework_signal = bool(rework_note) or (verdict or "").strip().lower() == "rework"
    if not has_rework_signal:
        return HeldVerdict(held=False, reason="no rework signal in verdict or notes")

    green = is_ci_green(ci_conclusions)
    if green is None:
        return HeldVerdict(
            held=False,
            reason="rework signal present but no CI conclusions supplied — cannot confirm green",
            rework_note=rework_note,
        )
    if not green:
        return HeldVerdict(
            held=False,
            reason="rework signal present but CI was not green at the head commit",
            rework_note=rework_note,
        )

    validated_match = _VALIDATED_RE.search(notes or "")
    return HeldVerdict(
        held=True,
        reason="rework signal present and exact-head CI was green",
        validated_note=validated_match.group(0) if validated_match else None,
        rework_note=rework_note,
    )


# ── GitHub I/O: read the PR's checks, write the `held` label ──────────────
#
# Both functions below degrade to a safe default (None / False) on any
# failure — missing `gh`, network error, timeout, bad JSON — exactly like
# every other `gh` helper in this codebase (see reviewer.py's `_gh_pr_state`
# et al.). Neither raises.


def gh_pr_check_conclusions(pr_ref: str, repo_path: str | None = None) -> dict[str, str] | None:
    """Check-name -> conclusion/state mapping for a PR's current head, via `gh`.

    Read-only: `gh pr view --json statusCheckRollup`, the same `pr view`
    subcommand reviewer.py already uses elsewhere — this reuses that
    invariant rather than introducing a new gh read pattern. Returns None
    when `gh` is unavailable, the call fails, or the PR has no checks yet.
    """
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        res = subprocess.run(
            [gh, "pr", "view", pr_ref, "--json", "statusCheckRollup"],
            cwd=repo_path or None,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0:
            return None
        from .pr_models import REVIEW_HEAD_CHECK_NAME, prefer_check_state, rollup_check_name

        rollup = json.loads(res.stdout).get("statusCheckRollup") or []
        out: dict[str, str] = {}
        for i, check in enumerate(rollup):
            # "<workflow> / <job>": GitHub Actions reports bare job names, so
            # two workflows' same-named jobs would otherwise overwrite each
            # other and could hide a red check behind a green one (#1787).
            name = rollup_check_name(check, i)
            if name.casefold() == "coderabbit":
                continue
            # Whether a head was read is not CI (task #2320): a PR nobody has
            # read yet must still read as "held despite green".
            if name == REVIEW_HEAD_CHECK_NAME:
                continue
            # A CheckRun that hasn't finished has an empty conclusion and
            # carries its lifecycle in `status` (QUEUED/IN_PROGRESS); keep it
            # so is_ci_green sees "not finished" rather than dropping it.
            state = check.get("conclusion") or check.get("state") or check.get("status")
            if state:
                # A skipped duplicate (a later non-CI PR event) must not mask a
                # real failure under the same name (#1787).
                out[name] = prefer_check_state(out.get(name), state)
        return out or None
    except Exception:
        return None


def label_held_pr(pr_ref: str, repo_path: str | None = None) -> bool:
    """Apply the `held` GitHub label to a PR. Returns True on success.

    The one deliberate GitHub *write* in the review workflow — see the
    module docstring for why it lives here rather than in reviewer.py, and
    why it is only ever invoked explicitly (never from the unattended scan).
    Requires the `held` label to already exist on the repo
    (`gh label create held` is a one-time repo setup step, not done here).
    """
    gh = shutil.which("gh")
    if not gh:
        return False
    try:
        res = subprocess.run(
            [gh, "pr", "edit", pr_ref, "--add-label", HELD_LABEL],
            cwd=repo_path or None,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return res.returncode == 0
    except Exception:
        return False


def resolve_pr_ref_for_task(task_id: int) -> tuple[str | None, str | None]:
    """(pr_ref, repo_path) for a task, or (None, None) when unresolvable.

    Reuses `reviewer.py`'s own PR-reference extraction
    (`_pr_ref_from_notes` / `_pr_ref_from_evidence`) so this module's notion
    of "which PR" never drifts from the one the automated pipeline already
    uses to reconcile merges (docs/automation.md's marker table).
    """
    from . import merge, reviewer, storage

    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT notes, evidence_ref FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None, None

    notes = row["notes"] or ""
    pr_ref = reviewer._pr_ref_from_notes(notes) or reviewer._pr_ref_from_evidence(
        row["evidence_ref"]
    )
    if not pr_ref:
        return None, None

    try:
        wt, main_repo = merge.find_task_worktree(task_id)
    except Exception:
        wt, main_repo = None, None
    repo_path = (wt or {}).get("path") or main_repo
    # A bare PR number only means something inside its repo's checkout. With
    # no checkout, `gh` would resolve it against whatever repo the process
    # happens to be in — so refuse rather than label the wrong PR. A full PR
    # URL carries its own repo and is always safe.
    if str(pr_ref).strip().lstrip("#").isdigit() and not repo_path:
        return None, None
    return pr_ref, repo_path


def label_held_task(task_id: int, *, dry_run: bool = False) -> dict:
    """Operator entry point: label a task's PR `held` if it is held-despite-green.

    Loads the task's notes, resolves its PR reference and repo checkout,
    reads the PR's current check conclusions, and — only when
    `held_despite_green()` says True — applies the `held` label (unless
    `dry_run`). Always returns a dict describing what it found/did; never
    raises for an ordinary "not held" / "gh unavailable" outcome.
    """
    from . import storage

    conn = storage.connect()
    try:
        row = conn.execute("SELECT notes FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"task_id": task_id, "ok": False, "message": f"task #{task_id} not found"}

    notes = row["notes"] or ""
    pr_ref, repo_path = resolve_pr_ref_for_task(task_id)
    if not pr_ref:
        return {
            "task_id": task_id,
            "ok": False,
            "held": False,
            "message": "no resolvable PR reference in task notes/evidence_ref",
        }

    conclusions = gh_pr_check_conclusions(pr_ref, repo_path)
    if conclusions is None:
        # Couldn't read the checks (no gh, auth, network): inconclusive, not
        # "not held" — the operator must know the label decision didn't run.
        return {
            "task_id": task_id,
            "pr_ref": pr_ref,
            "ok": False,
            "held": False,
            "inconclusive": True,
            "message": f"could not read CI checks for {pr_ref} (gh unavailable, "
            "unauthenticated, or no checks yet) — held status unknown, nothing labeled",
        }
    verdict = held_despite_green(notes, ci_conclusions=conclusions)
    result = {
        "task_id": task_id,
        "pr_ref": pr_ref,
        "held": verdict.held,
        "reason": verdict.reason,
    }
    if not verdict.held:
        result["ok"] = True
        result["message"] = f"not held-despite-green: {verdict.reason}"
        return result

    if dry_run:
        result["ok"] = True
        result["labeled"] = False
        result["message"] = f"held-despite-green (dry-run — not labeling): {pr_ref}"
        return result

    labeled = label_held_pr(pr_ref, repo_path)
    result["ok"] = labeled
    result["labeled"] = labeled
    result["message"] = (
        f"labeled {pr_ref} '{HELD_LABEL}'"
        if labeled
        else f"held-despite-green but labeling {pr_ref} failed (gh unavailable, "
        f"label 'held' missing on the repo, or PR unwritable)"
    )
    return result
