"""Autonomous-cycle eligibility checks and the red-tier / worktree-HEAD seams they use.

Moved out of ``reviewer.py`` (task #1724) so ``tools.cycles`` (services) can
ask for a green-path or auto-PR verdict without importing the review pipeline
(runtime). ``reviewer`` re-exports every name here, so existing callers and
``reviewer.<name>`` imports keep working.

Patch the seams (``_head_sha``, ``_changed_files_vs_main``, ``_is_red_tier``,
``_pr_evidence_binds_worktree``) on THIS module when exercising the
eligibility checks: the functions below look them up here. The review
pipeline in ``reviewer`` still looks up its own re-exported names, so its tests
keep patching ``reviewer.<name>``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

from mcp.server.mcpserver.exceptions import ToolError

# Operational failures from merge.run_validation_gates / resolve_task_worktree.
# Deliberately NOT Exception — implementation bugs must stay observable.
_VALIDATION_GATE_ERRORS = (ValueError, RuntimeError, ToolError)


# Operational failures from worktree/git/red-tier inspection in eligibility checks.
_CLASSIFICATION_ERRORS = (OSError, ValueError, RuntimeError, ToolError)


# Files/directories that require human approval before merging
RED_TIER_PATTERNS = [
    r"config\.ya?ml$",
    r"\.env",
    r"SOUL\.md",
    r"SKILL\.md",
    r"AGENTS\.md",
    r"CLAUDE\.md",
    r"\.cursorrules$",
    # Security surfaces: code that implements or guards a boundary. The bare
    # substring this replaced also caught prose (a changelog fragment named
    # ``*.security.md``, ``docs/security.md``), which spent the operator's
    # approval on documentation while real gate code escaped (#2275).
    r"(^|/)SECURITY\.md$",  # the security policy statement itself
    r"(^|/)security/",  # a security package
    r"(^|/)security\.py$",  # a security module
    r"security[^/]*\.py$",  # a module or test whose NAME says security
    r"safety\.py$",
    r"server\.py$",
    r"deploy/",
    r"hooks?/",
    r"\.mcp\.json$",
    r"mcp_config\.json$",
    # CI definitions and the classifier that decides which tests CI runs: a
    # PR that edits them could produce green shards that prove nothing, and
    # green shards stand in for the review's pytest (task #1787).
    r"(^|/)\.github/workflows/",
    r"(^|/)\.depot/",
    r"(^|/)scripts/ci_changes\.py$",
    # The review pipeline's own test selection (task #2274): reviewer.py asks
    # affected_tests.select() which tests to run against a PR's diff, and
    # scripts/local-gate.sh --affected reads the same module. A PR that
    # narrows it would choose the evidence for its own review.
    r"(^|/)affected_tests\.py$",
    # This classifier decides what escalates at all (task #2274): editing it
    # without escalation would let a PR widen its own way through the gate.
    r"(^|/)cycle_eligibility\.py$",
    # Authority surfaces (task #2277). policy.py is the policy gate itself, and
    # audit/rules.py defines what the security audit reports as a finding, so
    # narrowing a rule there would silence findings fleet-wide. Both are low
    # churn, which is why they are path patterns. profiles.py is deliberately
    # NOT here: it takes ~50 commits in two months, and the change that matters
    # there is widening a tool surface, not touching the file — that needs a
    # diff-content check, not a path match.
    r"(^|/)policy\.py$",
    r"(^|/)audit/rules\.py$",
    # The injection phrases audit/rules.py re-exports and the read-path scanner
    # counts (task #2450). They moved out of audit/rules.py so the kernel can
    # read them; narrowing them here would silence the same findings.
    r"(^|/)injection_patterns\.py$",
]


_red_tier_re = re.compile("|".join(RED_TIER_PATTERNS))


def _is_red_tier(changed_files: list[dict]) -> list[str]:
    """Return list of filenames that match red-tier patterns."""
    hits = []
    for f in changed_files:
        path = f.get("file", "")
        if _red_tier_re.search(path):
            hits.append(path)
    return hits


def _head_sha(wt_path: str) -> str | None:
    """Current HEAD sha of a worktree, or None if it cannot be resolved.

    Task #531: product-repo worktrees live outside ``UA_WORKSPACE``. Use the
    pipeline-only ``resolve_task_worktree`` + ``merge._run_git_in_repo`` escape
    (same as the merge gates) — never the sandboxed ``tools.git._run_git``.
    """
    from . import merge
    from .safety import resolve_task_worktree

    try:
        cwd = str(resolve_task_worktree(wt_path))
        res = merge._run_git_in_repo(["rev-parse", "HEAD"], cwd)
    except Exception:
        return None
    sha = res.stdout.strip() if res.returncode == 0 else ""
    return sha or None


def _changed_files_vs_main(wt_path: str, branch: str) -> list[dict]:
    """List files changed on a branch relative to (origin/)main.

    Returns [{file, status}] from ``git diff --name-status <base>...<branch>``,
    trying ``main`` then ``origin/main`` as the base. Empty list on failure.

    Task #531: must not silent-empty when the worktree is under a project
    checkout (sandboxed ``tools.git._run_git`` would refuse and hide red-tier).
    """
    from . import merge
    from .safety import resolve_task_worktree

    target = branch or "HEAD"
    try:
        cwd = str(resolve_task_worktree(wt_path))
    except Exception:
        return []
    for base in ("main", "origin/main"):
        try:
            res = merge._run_git_in_repo(["diff", "--name-status", f"{base}...{target}"], cwd)
        except Exception:
            return []
        if res.returncode == 0:
            changed = []
            for line in res.stdout.splitlines():
                # A rename/copy line is "R100\told\tnew": classify both paths,
                # or renaming safety.py away would read as a clean diff.
                status, *paths = line.split("\t")
                changed.extend({"file": path, "status": status} for path in paths if path)
            return changed
    return []


def _gh_pr_head_oid(ref: str, repo_path: str | None) -> str | None:
    """HEAD commit OID of a PR via the gh CLI, if available.

    Read-only companion to ``_gh_pr_state`` — used to bind a supplied PR URL to
    the worktree HEAD before an auto-PR bypass clears the human gate.
    """
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        res = subprocess.run(
            [gh, "pr", "view", ref, "--json", "headRefOid"],
            cwd=repo_path or None,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0:
            return None
        return json.loads(res.stdout).get("headRefOid")
    except Exception:
        return None


def _sha_prefix_matches(left: str, right: str) -> bool:
    """Return True when two SHAs share a prefix of at least 7 hex chars."""
    left = left.lower()
    right = right.lower()
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return len(shorter) >= 7 and longer.startswith(shorter)


def _pr_evidence_binds_worktree(pr_url: str, wt: dict) -> str | None:
    """Return None when ``pr_url`` is bound to the worktree HEAD; else a reason.

    Auto-PR bypass must not record unrelated evidence: a SHA must match the
    validated worktree HEAD, and a GitHub PR URL must resolve (via ``gh``) to
    that same HEAD. Any other case fails closed to the human gate.
    """
    from .tools.cycles import _EVIDENCE_SHA_RE, _EVIDENCE_URL_RE

    stripped = (pr_url or "").strip()
    if not stripped:
        return "pr_url is required for evidence binding"

    wt_path = wt.get("path") or ""
    head = (_head_sha(wt_path) if wt_path else None) or (wt.get("head") or "")
    head = head.strip().lower()
    if not head:
        return "worktree HEAD could not be resolved for evidence binding"

    if _EVIDENCE_SHA_RE.match(stripped):
        if _sha_prefix_matches(stripped, head):
            return None
        return f"pr_url SHA {stripped!r} does not match worktree HEAD {head[:12]}"

    if _EVIDENCE_URL_RE.match(stripped):
        pr_head = _gh_pr_head_oid(stripped, wt_path or None)
        if pr_head and _sha_prefix_matches(pr_head, head):
            return None
        return "pr_url could not be bound to worktree HEAD (gh unavailable or mismatch)"

    return "pr_url is not verifiable evidence"


def _red_tier_for_task_checked(task_id: int) -> tuple[list[str], str | None]:
    """Red-tier files in the task branch's diff, plus why the diff could not be
    classified (or None when it was).

    Returns (hits, unclassified_reason). A non-None reason means the diff could
    not be inspected at all — no worktree, no usable main/origin-main base,
    `git diff` failing. That case is INDISTINGUISHABLE from a clean diff if you
    only look at the returned list, because both produce `[]`.

    That ambiguity is the point of this function. Any caller deciding whether
    to SKIP a human approval must treat "could not classify" as disqualifying,
    not as "nothing red-tier here" — otherwise a branch touching safety.py can
    bypass the gate purely because the diff failed to read. Callers that merely
    *report* red-tier files (rather than gating on their absence) can keep
    using the plain `_red_tier_for_task` wrapper below.

    The reason is carried rather than reduced to a bool so the operator sees
    the actionable cause ("no git worktree found for this task") instead of a
    generic "unavailable".
    """
    try:
        from .merge import find_task_worktree

        wt, _main_repo = find_task_worktree(task_id)
        if not wt:
            return [], "no git worktree found for this task"
        changed = _changed_files_vs_main(wt["path"], wt.get("branch", ""))
        if not changed:
            # An empty change list is itself ambiguous: a branch with no diff
            # versus a diff command that produced nothing usable. Refuse to
            # certify either as "provably clean".
            return [], "diff against main is empty or unreadable"
        return _is_red_tier(changed), None
    except _CLASSIFICATION_ERRORS as e:
        return [], f"red-tier classification failed: {str(e)[:200]}"


def _red_tier_for_task(task_id: int) -> list[str]:
    """Red-tier files in the task branch's diff against main (best-effort).

    Reporting-only view. Do NOT use this to decide that a branch is clean
    enough to skip an approval gate — see `_red_tier_for_task_checked`.
    """
    hits, _unclassified = _red_tier_for_task_checked(task_id)
    return hits


def autonomous_cycle_green_path_eligibility(task_id: int, cycle: dict) -> dict:
    """Green-path auto-merge CRITERIA (task #373 Tier 2, 3.4) — a decision,
    not an action.

    Returns {"eligible": bool, "reasons": [str, ...]}. `reasons` is always
    populated: for an ineligible cycle it stops at the first failing check
    and explains why; for an eligible one it lists everything that was
    checked and passed, so a human reading an "eligible" verdict sees the
    reasoning, not just a boolean.

    Checks, in order (first failure wins):
    1. cycle.state is 'pr' or 'completed' — anything earlier hasn't produced
       a mergeable artifact yet.
    2. cycle.pr_url is present — should already be guaranteed by the
       evidence contract (task #373 3.2: pr_url is required and format-
       checked before a cycle can enter 'pr'), checked again here rather
       than trusted blindly.
    3. cycle.stop_reason is empty — a cycle that ever recorded a stop reason
       (budget block, capability denial, rework-budget exhaustion) carried
       friction; that is not what "green path" means, even if it later
       reached 'pr' some other way.
    4. no changed file matches RED_TIER_PATTERNS (reusing `_is_red_tier` —
       the SAME canonical list `_validate_and_hold`/`_merge_done_task` use,
       not a reimplementation that could drift from it), and the diff could
       be read at all: like those two, an empty or unreadable diff holds.
    5. `run_validation_gates` passes (clean tree, security audit, pytest —
       the same gates the existing merge paths require), against a worktree
       HEAD pinned before the gates run and re-checked after (task #598): the
       gates take minutes, and nothing else stopped a concurrent commit,
       rebase, or executor fixup from moving HEAD mid-run — an eligible
       verdict must describe the commit it actually validated.

    IMPORTANT — this function itself still makes no merge, approval, or
    state-transition call; it only returns a verdict. Wiring the verdict to
    actually skip approval landed separately (task #384, explicit go/no-go
    given by the operator 2026-08-12, per the separate-PR requirement in
    docs/s5-bounded-autonomy-acceptance-gates.md MUST 8): tools/cycles.py's
    autonomous_cycle_transition calls this function when approval_action is
    "merge" (to_state="completed") and skips check_approval_gate only when
    `eligible` is True. Every other approval_action ('pr'/external_mutation,
    'blocked'/'failed'/escalation) is untouched — MUST 9's "default stays
    human-merge for anything not provably green" applies exactly as before
    to everything this function does not explicitly clear.
    """
    reasons: list[str] = []

    state = cycle.get("state")
    if state not in ("pr", "completed"):
        return {
            "eligible": False,
            "reasons": [f"cycle state is {state!r}, expected 'pr' or 'completed'"],
        }
    reasons.append(f"cycle state is {state!r}")

    pr_url = (cycle.get("pr_url") or "").strip()
    if not pr_url:
        reasons.append("no pr_url recorded")
        return {"eligible": False, "reasons": reasons}
    reasons.append(f"evidence recorded: {pr_url}")

    if cycle.get("stop_reason"):
        reasons.append(f"cycle carries a stop_reason: {cycle['stop_reason']!r}")
        return {"eligible": False, "reasons": reasons}
    reasons.append("no stop_reason recorded")

    red_hits, unclassified = _red_tier_for_task_checked(task_id)
    if red_hits:
        reasons.append(f"touches red-tier file(s): {', '.join(red_hits)}")
        return {"eligible": False, "reasons": reasons}
    if unclassified:
        reasons.append(unclassified)
        return {"eligible": False, "reasons": reasons}
    reasons.append("no red-tier files touched")

    try:
        from .merge import find_task_worktree, run_validation_gates

        wt, _main_repo = find_task_worktree(task_id)
        if wt is None:
            reasons.append("no git worktree found for this task")
            return {"eligible": False, "reasons": reasons}
        head_before = _head_sha(wt["path"])
        if head_before is None:
            reasons.append("worktree HEAD could not be resolved before validation")
            return {"eligible": False, "reasons": reasons}
        run_validation_gates(task_id, wt["path"])
        head_after = _head_sha(wt["path"])
        if head_after != head_before:
            reasons.append(
                "worktree HEAD moved during validation (validated "
                f"{head_before[:12]}, now {(head_after or 'unresolved')[:12]}) — "
                "the gates no longer describe what is being approved"
            )
            return {"eligible": False, "reasons": reasons}
    except _VALIDATION_GATE_ERRORS as e:
        reasons.append(f"validation gates failed: {str(e)[:200]}")
        return {"eligible": False, "reasons": reasons}
    reasons.append(
        f"validation gates passed at pinned commit {head_before[:12]} "
        "(clean tree, security audit, pytest)"
    )

    return {"eligible": True, "reasons": reasons}


def autonomous_cycle_pr_path_eligibility(
    task_id: int, cycle: dict, pr_url: str | None = None
) -> dict:
    """Auto-PR CRITERIA for the 'reviewing' -> 'pr' transition — a decision,
    not an action.

    Same shape and same rigor as `autonomous_cycle_green_path_eligibility`,
    which it deliberately mirrors rather than reimplements: identical
    stop_reason check, the identical canonical `_red_tier_for_task` /
    `_is_red_tier` red-tier check (MUST 7 — one list, no drift), the
    identical `run_validation_gates` (clean tree, security audit, pytest),
    and the identical worktree-HEAD pin-and-recheck around those gates
    (task #598) — a HEAD that moves mid-validation returns ineligible here
    exactly as it does in the merge check.

    Two differences from the merge criteria, both because this runs one step
    EARLIER in the cycle:
    1. Expected state is 'reviewing' — the only state TRANSITIONS allows into
       'pr'. The merge check expects 'pr'/'completed'.
    2. No `pr_url` requirement. The merge check demands one because by then it
       must already exist; here the transition is what records it.

    Why opening a PR is a weaker action than merging, and safe to automate on
    these criteria: a PR mutates a branch and a GitHub object, never `main`.
    It is reversible by closing it, it is the artifact a human reviews, and
    the operator's standing autonomy rule already pre-authorizes PR creation
    explicitly. Gating it forced a human stop *before* the reviewable artifact
    existed, which inverted the intent — the human was interrupted to permit
    the creation of the very thing they were meant to review.

    Scope boundary, unchanged from MUST 9: anything not provably green still
    goes to the human approval gate. A red-tier diff, a recorded stop_reason,
    a failing validation gate, a missing worktree, or an unclassifiable diff
    all return ineligible, and the caller falls through to `check_approval_gate`
    exactly as before. Unexpected implementation faults propagate — they are
    not converted into a normal ineligible verdict. This function itself makes
    no transition, approval, or PR call — it only returns a verdict.

    Returns {"eligible": bool, "reasons": [str, ...]}, `reasons` always
    populated: first failing check for an ineligible cycle, every passed check
    for an eligible one.
    """
    reasons: list[str] = []

    state = cycle.get("state")
    if state != "reviewing":
        return {
            "eligible": False,
            "reasons": [f"cycle state is {state!r}, expected 'reviewing'"],
        }
    reasons.append("cycle state is 'reviewing'")

    if cycle.get("stop_reason"):
        reasons.append(f"cycle carries a stop_reason: {cycle['stop_reason']!r}")
        return {"eligible": False, "reasons": reasons}
    reasons.append("no stop_reason recorded")

    red_hits, unclassified = _red_tier_for_task_checked(task_id)
    if red_hits:
        reasons.append(f"touches red-tier file(s): {', '.join(red_hits)}")
        return {"eligible": False, "reasons": reasons}
    if unclassified:
        reasons.append(unclassified)
        return {"eligible": False, "reasons": reasons}
    reasons.append("no red-tier files touched")

    try:
        from .merge import find_task_worktree, run_validation_gates

        wt, _main_repo = find_task_worktree(task_id)
        if wt is None:
            reasons.append("no git worktree found for this task")
            return {"eligible": False, "reasons": reasons}
        if pr_url is not None:
            unbound = _pr_evidence_binds_worktree(pr_url, wt)
            if unbound:
                reasons.append(unbound)
                return {"eligible": False, "reasons": reasons}
            reasons.append("pr_url bound to worktree HEAD")
        head_before = _head_sha(wt["path"])
        if head_before is None:
            reasons.append("worktree HEAD could not be resolved before validation")
            return {"eligible": False, "reasons": reasons}
        run_validation_gates(task_id, wt["path"])
        head_after = _head_sha(wt["path"])
        if head_after != head_before:
            reasons.append(
                "worktree HEAD moved during validation (validated "
                f"{head_before[:12]}, now {(head_after or 'unresolved')[:12]}) — "
                "the gates no longer describe what is being approved"
            )
            return {"eligible": False, "reasons": reasons}
    except _VALIDATION_GATE_ERRORS as e:
        reasons.append(f"validation gates failed: {str(e)[:200]}")
        return {"eligible": False, "reasons": reasons}
    reasons.append(
        f"validation gates passed at pinned commit {head_before[:12]} "
        "(clean tree, security audit, pytest)"
    )

    return {"eligible": True, "reasons": reasons}
