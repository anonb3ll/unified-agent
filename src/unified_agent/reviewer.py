"""Automated review orchestration for the agent PR pipeline.

**Validate + hold contract** (policy decided 2026-07-16): the automated
pipeline never writes to the remote — it validates gates, records one note
line per (state, branch-head sha), and notifies; a human performs the merge.

The pipeline, as driven by ``review_cli.py`` (cron, no LLM anywhere):

1. **Reconcile**: tasks annotated ``[waiting-for: review]`` or
   ``[waiting-for: human-merge]`` whose branch/PR already landed on
   ``origin/main`` are marked ``done`` (``[review: merged-upstream @ ts]``).
2. **Scan** for reviewable tasks (any status but ``done`` — task #303: a task
   left ``open`` with a live marker is still reviewable, not just
   ``doing``/``blocked``) annotated ``[waiting-for: review]``.
3. For each candidate, run the validation gates in its worktree:
   - security audit (in-process)
   - unit tests (pytest), or exact-head green CI pytest shards when
     ``UA_REVIEW_TRUST_CI`` is on (task #1338 — still runs clean-tree + audit)
   - red-tier diff classification
4. Verdicts: ``validated`` (gates green — held for the human merge),
   ``red-tier``, ``failed``, ``timeout`` (task #1254: the pytest gate hit
   ``UA_REVIEW_PYTEST_TIMEOUT_S`` before finishing — inconclusive, not a red
   test; retried on the next scan rather than held as resolved, up to
   ``UA_REVIEW_TIMEOUT_MAX_RETRIES`` times per sha, after which it holds as
   ``unchanged`` until a new commit lands — only the first timeout at a sha
   notifies), ``unchanged`` (already reviewed at this exact
   branch-head sha; no gates re-run, no note, no notification),
   ``merged-upstream`` / ``stale`` (from reconcile), and ``merged`` (kept for
   tasks already marked ``done`` — the local-merge flow for non-PR worktree
   tasks).

No code path in this module performs a remote write: nothing here pushes a
branch or merges a PR on GitHub (``tests/test_reviewer_pipeline.py`` asserts
this against the source).

The module is designed to be called both as an MCP tool and from a
cron-driven shell script.
"""

import hashlib
import json
import logging
import re
import shlex
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, storage, task_notes
from .cycle_eligibility import (  # noqa: F401 - cycle eligibility and its seams; re-exported here
    _CLASSIFICATION_ERRORS,
    _VALIDATION_GATE_ERRORS,
    RED_TIER_PATTERNS,
    _changed_files_vs_main,
    _gh_pr_head_oid,
    _head_sha,
    _is_red_tier,
    _pr_evidence_binds_worktree,
    _red_tier_for_task,
    _red_tier_for_task_checked,
    _red_tier_re,
    _sha_prefix_matches,
    autonomous_cycle_green_path_eligibility,
    autonomous_cycle_pr_path_eligibility,
)
from .sync import sync_state_to_vault

# Annotations used in task notes to signal workflow state
WAITING_FOR_REVIEW = "[waiting-for: review]"
WAITING_FOR_HUMAN_MERGE = "[waiting-for: human-merge]"
WAITING_FOR_PLAN_APPROVAL = "[waiting-for: plan-approval]"
REVIEW_APPROVED = "[review: approved]"
# Compatibility names share one quoted/order grammar with task guards.
_REVIEW_MARKER_LIVE = task_notes.REVIEW_REQUEST
_REVIEW_RESOLUTION_MARKER = task_notes.REVIEW_RESOLUTION
_strip_quoted_spans = task_notes.strip_quoted_spans


def _notes_mention(notes: str | None, marker: str) -> bool:
    """Case-insensitive substring check for a workflow marker in notes."""
    return marker.lower() in (notes or "").lower()


def _has_live_review_marker(notes: str | None) -> bool:
    """True when the latest review request has not already been resolved.

    Quoted/backticked mentions of the marker (documentation, reconciliation
    notes quoting a prior request) are ignored — see task #1242.
    """
    return task_notes.live_review_request_end(notes) is not None


def _is_open_boundary_review_spec(task: dict, notes: str | None) -> bool:
    """True for unclaimed open tasks whose only review mention is a boundary template."""
    if not _notes_mention(notes, WAITING_FOR_REVIEW):
        return False
    if _has_live_review_marker(notes):
        return False
    if _notes_mention(notes, WAITING_FOR_PLAN_APPROVAL) or _notes_mention(
        notes, WAITING_FOR_HUMAN_MERGE
    ):
        return False
    if task.get("status") != "open" or task.get("claimed_by"):
        return False
    return True


def _is_active_review_handoff(task: dict, notes: str | None) -> bool:
    """Claimed in-flight tasks awaiting review after execution handoff."""
    return (
        task.get("status") in ("doing", "waiting_review", "blocked")
        and bool(task.get("claimed_by"))
        and _has_live_review_marker(notes)
    )


def _scan_task_kind(notes: str | None) -> str:
    """Classify a scanned task as scout / review / plan-approval."""
    from .scout import is_scout

    if is_scout(notes):
        return "scout"
    if _has_live_review_marker(notes):
        return "review"
    if _notes_mention(notes, WAITING_FOR_PLAN_APPROVAL):
        return "plan-approval"
    return "review"


REVIEW_REJECTED = "[review: rejected]"
REVIEW_VALIDATED = "[review: validated"
REVIEW_SCOUT_READY = "[review: scout-ready"
REVIEW_SCOUT_INCOMPLETE = "[review: scout-incomplete"
REVIEW_MERGED_UPSTREAM = "[review: merged-upstream"
REVIEW_STALE = "[review: stale"
REVIEW_UNRESOLVED_REPO = "[review: unresolved-repo"
# A branch was found on origin but no checkout exists here, so no gate ran
# (task #2321). Deliberately not "validated": a SHA in the branch name must
# never read as a validation at that head (`_already_reviewed_at_sha`).
REVIEW_HELD_NO_CHECKOUT = "[review: held-no-checkout"
# The pre-#2321 wording of that hold, written under the validated marker.
_LEGACY_NO_CHECKOUT_HOLD = "no local worktree on this host"

log = logging.getLogger(__name__)


# A task declaring its completion is code-bearing (mirrors tools.tasks) — the
# same marker that routes a task's worktree to a real project repo. Task #494
# reuses it to decide whether a branch was ever expected at all.
_CODE_TASK_RX = re.compile(r"(?<!`)\[code-task\](?!`)")
# Explicit no-code exemption (Codex #208): absence of [code-task] is not proof of
# a branchless task — named projects mint worktrees without that marker.
_NO_CODE_RX = re.compile(r"(?<!`)\[no-code\](?!`)")
# Plan-approval consumer (task #65): a task carrying WAITING_FOR_PLAN_APPROVAL
# references its plan with a 'plan: <id>' (or 'plan #<id>') note line. The
# pipeline surfaces the plan and notifies once per plan-state fingerprint; a
# human approves by editing the annotation (see review_plan_approvals).
PLAN_APPROVAL_NOTIFIED = "[plan-approval: notified"
PLAN_APPROVAL_UNRESOLVABLE = "[plan-approval: unresolvable"


_PR_URL_RE = re.compile(r"\bPR:\s*(https?://\S+)")
_PR_URL_BARE_RE = re.compile(r"https?://github\.com/[\w.\-]+/[\w.\-]+/pull/\d+")
# Bare 'PR #59' / 'PR#59' reference — no URL at all, the common case when an
# agent writes prose like "PR #59 open for review" (2026-07-20/21: tasks
# #115-117 were flagged false-stale by reconcile because neither
# _PR_URL_RE/_PR_URL_BARE_RE nor _BRANCH_NOTE_RE matched that exact prose —
# see _pr_ref_from_notes). `gh pr view` accepts a bare number as a ref, same
# as a URL or branch name, so no further resolution is needed once extracted.
_PR_NUMBER_RE = re.compile(r"\bPR\s*#(\d+)\b", re.IGNORECASE)
_PR_MARKER_PREFIXES = ("[review:", "[pr:")
# evidence_ref holding nothing but a PR number: "42", "#42", "PR #42" (task #2269).
_PR_EVIDENCE_NUMBER_RE = re.compile(r"(?:PR\s*)?#?(\d+)", re.IGNORECASE)
_PR_MARKER_NUMBER_RE = re.compile(r"(?:\[pr:\s*#?|\bPR\s*#?)(\d+)\b", re.IGNORECASE)
_BRANCH_NOTE_RE = re.compile(r"\bbranch[:\s]+`?([A-Za-z0-9][A-Za-z0-9._/\-]*)`?")
# UA task #736: desktop-host task worktrees are created by the dev agent on its
# own machine, so the hub row can carry worktree_path/branch = NULL even though
# the work branch was pushed to origin and named in the task notes. When the
# validate-and-hold path then finds no local worktree, it used to reject with
# "no git worktree found" — a false rejection that repeated on every cron tick
# (instances: #657 x3, #693, #714, #715, #728). Before rejecting, the review
# falls back to `git ls-remote` against the workspace and project repos to see
# whether the notes' branch actually exists on origin; when it does, the task
# is held for the human merge decision instead of failed.
_REVIEW_HOLD_FALLBACK_RE = re.compile(r"\[(?:review:|hygiene:)[^\]]*\]")
# Deliberately excludes "timeout" (task #1254): a pytest-timeout note marks
# the gates as never having finished, not as resolved, so the next scan must
# re-run them at the same sha rather than reading it as "already reviewed".
# That retry is bounded separately by `_timeout_count_at_sha` /
# `config.review_timeout_max_retries()` below, not by this idempotence check —
# an unbounded retry here would re-run a (now up to
# `UA_REVIEW_PYTEST_TIMEOUT_S`-long) pytest suite every `*/15` cron scan.
_REVIEWED_AT_SHA_TEMPLATE = (
    r"\[review: (?:validated|rejected|red-tier|auto-merged|yolo-merge-failed) @ "
    r"[^\]]*\][^\n]*{sha}"
)
# Counts existing pytest-timeout notes at one sha, so the retry above can be
# capped (task #1254 follow-up). Deliberately a *separate* pattern from
# `_REVIEWED_AT_SHA_TEMPLATE` rather than adding "timeout" to it — that
# template's hits are terminal ("already reviewed"); a timeout hit here still
# means "run the gates again", just only while the count is under the cap.
_REVIEW_TIMEOUT_AT_SHA_TEMPLATE = r"\[review: timeout @ [^\]]*\][^\n]*{sha}"
# 'plan: <id>' or 'plan #<id>' — the reference from an approval-gated task to
# its plan. Ignores '[review:/plan-approval:' bracket note lines when scanning.
_PLAN_ID_RE = re.compile(r"\bplan[:\s#]+`?([0-9a-f]{6,32})`?", re.IGNORECASE)


def _ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_status_now(task_id: int) -> str | None:
    """Fresh, uncached read of a task's current status (or None if gone)."""
    conn = storage.connect()
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


def _append_note(task_id: int, line: str) -> None:
    """Append a timestamped line to a task's notes."""
    from . import run_records

    if run_records.scoped_direct_completion(task_id) is not None:
        if not run_records.append_scoped_direct_note(task_id, line):
            raise run_records.DirectCompletionChanged("review note refused: task was superseded")
        sync_state_to_vault()
        return
    conn = storage.connect()
    try:
        storage.begin_task_write(conn)  # fence the revision read with its write
        row = conn.execute("SELECT notes, updated_at FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return
        notes = (row["notes"] or "") + f"\n{line}"
        conn.execute(
            "UPDATE tasks SET notes=?, updated_at=? WHERE id=?",
            (notes, storage.next_revision(row["updated_at"]), task_id),
        )
        conn.commit()
    finally:
        conn.close()
    sync_state_to_vault()


def _delegation_reconcile_blocker(conn, row) -> str | None:
    """Why reconcile must not close this task: live delegation work or a deployment wait."""
    from . import task_liveness

    deployment = task_liveness.deployment_wait_blocker(conn, int(row["id"]))
    if deployment:
        return deployment
    delegation = conn.execute(
        "SELECT id, status FROM delegations "
        "WHERE task_id=? AND status IN ('pending', 'in_flight') LIMIT 1",
        (row["id"],),
    ).fetchone()
    if delegation:
        return f"{delegation['status']} delegation {delegation['id']}"
    return None


def _active_reconcile_blocker(conn, row) -> str | None:
    """Why the AUTOMATIC reconcile loop must not close this task, else None.

    Delegation state is a hard signal and is checked on every close path. The
    claim-lease check is deliberately scoped to the reconcile loop, which is
    what reads PR state and can therefore be misled by a falsely-MERGED read
    into closing a task an executor currently owns (task #1109).

    The explicit reviewer action (``_mark_done_with_note``, called with an
    already-established merged outcome) must still reconcile a task that
    carries a claimant: that attribution-preserving behavior is asserted by
    ``test_reviewer_mark_done_stamps_service_identity`` and is what keeps
    legitimately-merged claimed work from sticking in review forever. The loop
    pre-checks this function, so scoping the claim guard here loses nothing.
    """
    blocked = _delegation_reconcile_blocker(conn, row)
    if blocked:
        return blocked

    # ``task_update(actor=...)`` records completion attribution without a
    # claim lease. Only task_claim sets claimed_at/lock_expires_at, so preserve
    # reconciliations of already-landed attributed work while never closing a
    # task an executor currently owns.
    if row["claimed_by"] and (row["claimed_at"] or row["lock_expires_at"]):
        lock_expires_at = row["lock_expires_at"]
        if not lock_expires_at or lock_expires_at > storage.now():
            return f"active claim by {row['claimed_by']}"
    return None


def _mark_done_with_note(task_id: int, line: str, *, expected=None) -> bool:
    """Atomically complete the orchestration's original task and run."""
    from . import run_records

    if expected is None:
        expected = run_records.capture_direct_completion(task_id)
    if not run_records.complete_direct(
        expected,
        actor="review-cron",
        note=line,
        blocker=_delegation_reconcile_blocker,
    ):
        return False
    sync_state_to_vault()
    return True


# ── git / gh seams (module-level so tests can monkeypatch them) ──────────────


def _branch_exists_on_origin(branch: str, repo: str) -> bool:
    """Does `branch` exist on origin? Checked via `git ls-remote --heads`.

    UA task #736 fallback: a desktop-host task's worktree never registers on
    the hub row (worktree_path/branch stay NULL), but the work branch was
    pushed to origin from the dev machine. Reading origin — rather than the
    local filesystem — is the only way the hub-hosted review can see that the
    work exists. Read-only, no fetch, ~1s; returns False on any error (a
    broken remote must not convert a missing-worktree failure into a pass).
    """
    if not branch or not repo:
        return False
    from . import merge

    try:
        res = merge._run_git_in_repo(["ls-remote", "--heads", "origin", branch], repo)
    except Exception:
        return False
    if res.returncode != 0:
        return False
    # ls-remote prints "<sha>\trefs/heads/<branch>" per match. Exact-match the
    # ref name so a notes branch like "fix/task-1" cannot false-match
    # "refs/heads/fix/task-10" via substring or prefix.
    wanted = f"refs/heads/{branch}"
    return any(line.split("\t", 1)[-1].strip() == wanted for line in res.stdout.splitlines())


# A branch name taken from the task row or a PR head before it reaches
# `git ls-remote`. A leading "-" is refused here; every other rule is git's.
_GITHUB_HOST = "github.com"
_GITHUB_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]+")
# scp-like remote: [user@]host:owner/name[.git][/]
_SCP_REMOTE_RE = re.compile(r"(?:[^@/:\s]+@)?([A-Za-z0-9.\-]+):(?!/)([^\s]+)")
_DEFAULT_PORTS = {"https": 443, "http": 80, "ssh": 22, "git": 9418}


def _is_safe_branch(branch: object) -> bool:
    """A branch name git accepts, that cannot be read as a git option.

    A leading ``-`` is refused before git sees the name. Every other rule is
    ``git check-ref-format --branch``, which allows ``+`` and ``@``.
    """
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        return False
    try:
        res = subprocess.run(
            ["git", "check-ref-format", "--branch", branch],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0 and res.stdout.strip() == branch


def _github_identity(owner: str, name: str) -> str | None:
    if name.lower().endswith(".git"):
        name = name[:-4]
    if not (_GITHUB_NAME_RE.fullmatch(owner) and _GITHUB_NAME_RE.fullmatch(name)):
        return None
    return f"{owner}/{name}".lower()


def _github_remote_identity(url: str) -> str | None:
    """Canonical ``owner/name`` of a git remote URL on GitHub, else None.

    Accepts the https, ssh and scp-like (``git@github.com:owner/name.git``)
    forms. The host must be exactly github.com (a lookalike such as
    ``othergithub.com`` or ``github.com.evil.example`` is refused), any port
    must be the scheme's default, and the path must be exactly
    ``owner/name`` with an optional ``.git`` and trailing slash. Owner and
    name compare case-insensitively, as GitHub treats them.
    """
    url = (url or "").strip()
    if "://" in url:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return None
        if parts.scheme not in _DEFAULT_PORTS or parts.query or parts.fragment:
            return None
        if port is not None and port != _DEFAULT_PORTS[parts.scheme]:
            return None
        host, path = (parts.hostname or ""), parts.path
        if not path.startswith("/"):
            return None
        path = path[1:]
    else:
        m = _SCP_REMOTE_RE.fullmatch(url)
        if not m:
            return None
        host, path = m.group(1), m.group(2)
    if host.lower() != _GITHUB_HOST:
        return None
    if path.endswith("/"):
        path = path[:-1]
    segments = path.split("/")
    return _github_identity(*segments) if len(segments) == 2 else None


def _github_pr_identity(url: str) -> str | None:
    """Canonical ``owner/name`` of a ``https://github.com/<o>/<n>/pull/<N>`` URL."""
    m = _PR_URL_PARTS_RE.fullmatch((url or "").strip())
    return _github_identity(*m.group(1).split("/", 1)) if m else None


def _origin_repo_slug(repo: str) -> str | None:
    """GitHub ``owner/name`` of ``repo``'s origin remote; None on any error."""
    from . import merge

    try:
        res = merge._run_git_in_repo(["remote", "get-url", "origin"], repo)
    except Exception:
        return None
    if res.returncode != 0:
        return None
    return _github_remote_identity(res.stdout.strip())


def _open_pr_head(ref: str, repo_path: str | None) -> dict | None:
    """State, head and repository of a PR via the gh CLI (task #2321).

    Read-only ``gh pr view``, same pattern as
    ``cycle_eligibility._gh_pr_head_oid``. The PR's base repository comes from
    its ``url``, which every gh version reports. Returns the parsed JSON object,
    or None when gh is missing, errors, times out or returns something else.
    """
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        res = subprocess.run(
            [
                gh,
                "pr",
                "view",
                ref,
                "--json",
                "state,headRefName,headRefOid,isCrossRepository,url",
            ],
            cwd=repo_path or None,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0:
            return None
        data = json.loads(res.stdout)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _open_pr_branch(evidence_ref: str | None, repo: str | None) -> tuple[str, str] | None:
    """Head branch and URL of the open PR ``evidence_ref`` names (task #2321).

    A native-checkout claim leaves the row's branch and worktree NULL, so the
    PR the agent opened can be the only record of its branch. The head is
    trusted only when gh reports the PR OPEN, not from a fork, and based on
    ``repo``'s own GitHub origin; anything else returns None.
    """
    pr_ref = _pr_ref_from_evidence(evidence_ref)
    if not pr_ref or not repo:
        return None
    view = _open_pr_head(pr_ref, repo)
    if not view or view.get("state") != "OPEN" or view.get("isCrossRepository") is not False:
        return None
    pr_url = str(view.get("url") or "")
    pr_slug = _github_pr_identity(pr_url)
    if pr_slug is None or pr_slug != _origin_repo_slug(repo):
        return None
    branch = view.get("headRefName")
    if not _is_safe_branch(branch):
        return None
    return branch, pr_url


def _origin_branch_fallback(
    task_id: int,
    notes: str,
    *,
    project: str | None = None,
    task: dict | None = None,
    evidence_ref: str | None = None,
) -> dict | None:
    """Origin-branch fallback for a task with no local worktree (UA #736).

    Returns a hold-verdict result dict when the task's branch exists on
    origin, else None so the caller proceeds to its existing rejection. The
    branch comes from, in order: a ``branch <name>`` phrase in the notes, the
    task row's ``branch`` column, then the head of an open same-repo PR named
    in ``evidence_ref`` (task #2321: a native-checkout claim records neither of
    the first two). Deliberately never validates gates: without a local
    checkout the audit/pytest gates cannot run, so this is a HOLD for the human
    merge decision (with the reason made explicit), not a green-path
    validation.
    """
    from . import merge

    # Task #1844: a product-sourced task's branch can only be on its own
    # repo's origin; a same-named cockpit branch must not validate it.
    routed, exclusive = merge._task_repo_routing(
        task if task is not None else {"project": project, "notes": notes}
    )
    if exclusive:
        repos: list[str] = [str(routed)] if routed is not None else []
    else:
        repos = [str(config.workspace_root())]
        try:
            repos.extend(str(p) for p in config.project_repo_roots())
        except Exception:
            pass

    def candidates():
        from_notes = _branch_from_notes(notes)
        if from_notes:
            yield from_notes, "named in notes"
        row_branch = (task or {}).get("branch")
        if _is_safe_branch(row_branch):
            yield row_branch, "task row"
        # The PR resolves only in the task's own repo: its routed checkout,
        # or the workspace when that is the project's checkout.
        pr_repo = (str(routed) if routed is not None else None) if exclusive else repos[0]
        from_pr = _open_pr_branch(evidence_ref, pr_repo)
        if from_pr:
            yield from_pr[0], f"open PR {from_pr[1]}"

    seen: set[str] = set()
    for branch, source in candidates():
        if branch in seen:
            continue
        seen.add(branch)
        for repo in repos:
            if _branch_exists_on_origin(branch, repo):
                return {
                    "verdict": "validated",
                    "notification": (
                        f"✅ Task #{task_id} review: no local worktree on this host, but "
                        f"branch {branch} exists on origin — held for your merge "
                        f"decision (gates not run: no local checkout)."
                    ),
                    "note": (
                        f"{REVIEW_HELD_NO_CHECKOUT} @ {_ts()}] no local worktree on this host; "
                        f"branch {branch} ({source}) found on origin (ls-remote) — held "
                        "for human merge decision; gates not run (no local checkout)"
                    ),
                    "branch": branch,
                }
    return None


def _merged_pr_fallback(
    task_id: int,
    title: str,
    notes: str,
    evidence_ref: str | None,
    *,
    project: str | None = None,
    task: dict | None = None,
) -> dict | None:
    """Merged-PR fallback when no local worktree exists (task #1333 / #1297).

    If notes or ``evidence_ref`` resolve to a PR that ``gh`` reports as
    ``MERGED``, return a merged-upstream verdict payload so the review path
    never false-rejects with ``no git worktree found``. Defense in depth for
    the case where reconcile aborted or missed the row.

    Bare ``PR #N`` refs need a project checkout as ``gh`` cwd (or an absolute
    URL); without one this returns None rather than guessing from the process
    working directory (Devin review on #756).
    """
    pr_ref = _pr_ref_for_review(notes, evidence_ref)
    if not pr_ref:
        return None
    from . import merge
    from .tools.tasks import DEFAULT_PROJECT

    repo_path = None
    # Task #1844: a product-sourced task resolves its PR only in its own
    # checkout. An unmarked default-project task keeps the workspace: a bare
    # PR number there must not close it against an unrelated product PR with
    # the same number (PR #1000 review).
    routed, exclusive = merge._task_repo_routing(
        task if task is not None else {"project": project, "notes": notes}
    )
    if exclusive:
        repo_path = str(routed) if routed is not None else None
    elif project == DEFAULT_PROJECT and not _CODE_TASK_RX.search(notes or ""):
        # An unmarked task may be cockpit or product work. The product repo is
        # trusted only when the PR there names this task (its branch or title),
        # so a same-numbered product PR cannot close a cockpit task (PR #1000).
        if routed is not None and _gh_pr_names_task(pr_ref, str(routed), task_id):
            repo_path = str(routed)
        else:
            workspace = config.workspace_root()
            if Path(workspace).is_dir():
                repo_path = str(workspace)
    ref_is_url = str(pr_ref).startswith("http://") or str(pr_ref).startswith("https://")
    if not ref_is_url and repo_path is None:
        return None
    if _gh_pr_state(pr_ref, repo_path) != "MERGED":
        return None
    metadata = _coerce_pr_metadata(_gh_pr_merge_details(pr_ref, repo_path))
    pr_url = metadata.get("url") or (pr_ref if ref_is_url else None)
    details = f" PR {pr_url}" if pr_url else f" PR {pr_ref}"
    sha = _merge_commit_oid(metadata)
    if sha:
        details += f" merge_sha={sha}"
    return {
        "verdict": "merged-upstream",
        "notification": (
            f"✅ Task #{task_id} ({title}) PR already merged upstream — marked done "
            "(no local worktree; resolved from notes/evidence_ref)."
        ),
        "note": f"[review: merged-upstream @ {_ts()}]{details}",
        "pr_ref": pr_ref,
    }


def _list_worktrees(repo_path: str | None = None) -> list[dict]:
    """All git worktrees in ``repo_path``; the workspace repo when omitted.

    Task #494: callers that need a *product* branch must pass that project's
    checkout. The configured workspace may be the cockpit,
    which holds no product code and has no git remote, so searching it for a
    product branch can only ever fail — silently, since every git helper here
    degrades to False/[] on error.

    Task #531: when ``repo_path`` is a known project root, list via
    ``merge._worktrees_of_project_repo`` (pipeline escape). Agent-facing
    ``git_worktree_list`` remains workspace-sandboxed.
    """

    from . import merge

    try:
        target = repo_path or str(config.workspace_root())
        resolved = Path(target).resolve()
        project_roots = {p.resolve() for p in config.project_repo_roots()}
        if resolved in project_roots:
            return merge._worktrees_of_project_repo(resolved)
        from .tools.git import git_worktree_list

        return git_worktree_list(repo_path=target).get("worktrees", [])
    except Exception:
        return []


def _fetch_origin(repo_path: str) -> None:
    """Best-effort ``git fetch origin`` so ancestry checks see the latest main.

    Still best-effort — a reconcile pass must survive an offline host — but the
    failure is logged rather than swallowed (task #494). Two earlier recurrences
    of the false-stale bug (#115-117, #299/#300) survived precisely because a
    repo that could not be fetched looked identical to one with nothing to fetch.

    Task #531: product repos use ``merge._run_git_in_repo`` after
    ``_known_repo_or_raise``.
    """
    from . import merge

    try:
        repo = merge._known_repo_or_raise(repo_path)
        res = merge._run_git_in_repo(["fetch", "origin", "--quiet"], repo)
        if getattr(res, "returncode", 0) != 0:
            log.warning("reconcile: git fetch origin failed in %s", repo_path)
    except Exception as exc:  # pragma: no cover - environment dependent
        log.warning("reconcile: git fetch origin unavailable in %s (%s)", repo_path, exc)


def _branch_exists_locally(branch: str, repo_path: str) -> bool:
    from . import merge

    try:
        repo = merge._known_repo_or_raise(repo_path)
        res = merge._run_git_in_repo(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo
        )
        return res.returncode == 0
    except Exception:
        return False


def _branch_merged_into_origin_main(branch: str, repo_path: str) -> bool:
    """True if the branch tip is an ancestor of origin/main (regular merge)."""
    from . import merge

    try:
        repo = merge._known_repo_or_raise(repo_path)
        tip_res = merge._run_git_in_repo(["rev-parse", branch], repo)
        main_res = merge._run_git_in_repo(["rev-parse", "origin/main"], repo)
        if tip_res.returncode == 0 and main_res.returncode == 0:
            if tip_res.stdout.strip() == main_res.stdout.strip():
                return False

        res = merge._run_git_in_repo(["merge-base", "--is-ancestor", branch, "origin/main"], repo)
        return res.returncode == 0
    except Exception:
        return False


def _gh_pr_state(ref: str, repo_path: str | None) -> str | None:
    """PR state ('MERGED', 'OPEN', 'CLOSED') via the gh CLI, if available.

    ``ref`` may be a branch name or a PR URL. Returns None when gh is missing
    (e.g. cron PATH), errors, or times out — callers degrade gracefully. This
    catches squash/rebase merges where branch ancestry never holds. Read-only:
    the only gh subcommand used anywhere in the pipeline is ``pr view``.
    """
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        res = subprocess.run(
            [gh, "pr", "view", ref, "--json", "state"],
            cwd=repo_path or None,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0:
            return None
        return json.loads(res.stdout).get("state")
    except Exception:
        return None


def _gh_pr_for_task(task_id: int, repo_path: str | None) -> dict | None:
    """Find a PR whose title names this task, without trusting body prose."""
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        res = subprocess.run(
            [
                gh,
                "pr",
                "list",
                "--state",
                "all",
                "--search",
                f"UA #{task_id}",
                "--limit",
                "20",
                "--json",
                "number,title,url,state,mergeCommit",
            ],
            cwd=repo_path or None,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0:
            return None
        token = re.compile(rf"(?:UA\s*#|task[-_]){task_id}(?!\d)", re.IGNORECASE)
        matches = [pr for pr in json.loads(res.stdout) if token.search(pr.get("title") or "")]
        return matches[0] if len(matches) == 1 else None
    except Exception:
        return None
    return None


def _gh_pr_names_task(ref: str, repo_path: str, task_id: int) -> bool:
    """True when PR ``ref`` in ``repo_path`` names ``task_id`` in its head
    branch (``merge.names_task_id``) or its title (``#<id>``)."""
    from . import merge

    gh = shutil.which("gh")
    if not gh:
        return False
    try:
        res = subprocess.run(
            [gh, "pr", "view", str(ref), "--json", "headRefName,title"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0:
            return False
        payload = json.loads(res.stdout)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    head = str(payload.get("headRefName") or "")
    title = str(payload.get("title") or "")
    return merge.names_task_id(head, task_id) or bool(re.search(rf"#{task_id}(?!\d)", title))


def _gh_pr_merge_details(ref: str, repo_path: str | None) -> dict:
    """Return merged PR URL/SHA metadata, or an empty mapping on lookup failure."""
    gh = shutil.which("gh")
    if not gh:
        return {}
    try:
        res = subprocess.run(
            [gh, "pr", "view", ref, "--json", "url,mergeCommit"],
            cwd=repo_path or None,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode == 0:
            payload = json.loads(res.stdout)
            return payload if isinstance(payload, dict) else {}
    except Exception:
        pass
    return {}


def _merge_commit_oid(metadata: dict | None) -> str | None:
    """Extract mergeCommit.oid without crashing on None lookups (task #1333).

    ``gh pr view --json mergeCommit`` returns ``"mergeCommit": null`` for
    unmerged PRs (and occasionally for just-merged ones before the field is
    populated). ``dict.get("mergeCommit", {})`` still returns ``None`` when the
    key is present with a null value, so a chained ``.get("oid")`` raised
    ``'NoneType' object has no attribute 'get'`` and aborted the whole
    reconcile step (2026-09-23 scan, task #1297).
    """
    if not isinstance(metadata, dict):
        return None
    commit = metadata.get("mergeCommit")
    if not isinstance(commit, dict):
        return None
    oid = commit.get("oid")
    return oid if isinstance(oid, str) and oid else None


def _coerce_pr_metadata(metadata: dict | None) -> dict:
    """Normalize a PR lookup result to a dict (never None)."""
    return metadata if isinstance(metadata, dict) else {}


def _ci_pytest_skip_eligible(wt_path: str) -> bool:
    """True when local gates would select pytest — safe to trust CI pytest shards.

    Mirrors ``merge._run_tests`` selection (task #1338 / Devin):
    - no root ``package.json`` / no ``test`` script → pytest
    - ``test`` script is ``bun test …`` → Bun (never CI-skip)
    - any other ``test`` script with Bun missing → local gate fails closed
      (exit 127); never CI-skip
    - other ``test`` script with Bun present → falls through to pytest
    """
    package_json = Path(wt_path) / "package.json"
    if not package_json.is_file():
        return True
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return True
    if not isinstance(package, dict) or not isinstance(package.get("scripts"), dict):
        return True
    script = package["scripts"].get("test")
    if not script:
        return True
    try:
        args = shlex.split(script)
    except ValueError:
        return False
    if args[:2] == ["bun", "test"]:
        return False
    if not shutil.which("bun"):
        return False
    return True


def _exact_head_ci_pytest_ok(
    pr_ref: str,
    expected_sha: str,
    repo_path: str | None,
) -> tuple[bool, str]:
    """True when CI test shards are SUCCESS at the same head as ``expected_sha``.

    Either CI provider counts (task #1787): check names are normalized with
    ``pr_models.rollup_check_name``, so GitHub Actions' ``test (shard=0)`` in
    workflow ``CI`` and Depot's ``CI / test (shard=0)`` both satisfy the
    ``CI / test (shard=0)`` requirement.

    Task #1338: substitute for local pytest in the review pipeline when
    ``UA_REVIEW_TRUST_CI`` is on (default). Fail closed on missing ``gh``,
    head mismatch, pending/failed/skipped required checks, or any I/O error —
    the caller then runs local pytest as before.

    Returns ``(ok, detail)`` where ``detail`` is a short reason for notes/logs.
    """
    if not config.review_trust_ci():
        return False, "UA_REVIEW_TRUST_CI disabled"
    ref = (pr_ref or "").strip()
    sha = (expected_sha or "").strip()
    if not ref or not sha or sha == "unknown":
        return False, "missing PR ref or worktree SHA"
    gh = shutil.which("gh")
    if not gh:
        return False, "gh unavailable"
    try:
        res = subprocess.run(
            [gh, "pr", "view", ref, "--json", "headRefOid,statusCheckRollup"],
            cwd=repo_path or None,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0:
            return False, f"gh pr view failed ({res.returncode})"
        payload = json.loads(res.stdout)
    except Exception as e:
        return False, f"gh pr view error: {type(e).__name__}"

    pr_head = (payload.get("headRefOid") or "").strip()
    if not pr_head or not _sha_prefix_matches(pr_head, sha):
        return False, f"PR head {pr_head[:12] or '?'} != worktree {sha[:12]}"

    from .pr_models import prefer_check_state, rollup_check_name

    conclusions: dict[str, str] = {}
    for i, check in enumerate(payload.get("statusCheckRollup") or []):
        if not isinstance(check, dict):
            continue
        name = rollup_check_name(check, i)
        state = check.get("conclusion") or check.get("state") or check.get("status")
        if name and state:
            conclusions[name] = prefer_check_state(conclusions.get(name), str(state))

    required = config.review_ci_required_checks()
    missing: list[str] = []
    for name in required:
        state = conclusions.get(name)
        if state is None or state.upper() != "SUCCESS":
            missing.append(f"{name}={state or 'absent'}")
    if missing:
        return False, "required checks not SUCCESS: " + "; ".join(missing[:4])
    return True, f"CI pytest shards SUCCESS at {pr_head[:12]}"


_TEST_FILE_RE = re.compile(r"^tests/(?:.+/)?(?:test_[^/]+|[^/]+_test)\.py$")
_AWAITING_CI_AT_SHA_TEMPLATE = r"\[review: awaiting-ci @ [^\]]*\][^\n]*{sha}"


def _targeted_test_plan(
    wt_path: str, changed: list[dict]
) -> tuple[list[str], list[str], list[str]]:
    """Test files for a review run without the full suite, and why it needs one.

    Task #1787: CI runs on batches, not on every PR head, so the review
    pipeline usually has no exact-head CI to trust. Rather than run the whole
    suite on the Mac hub (#1338, #1786), it runs the ``direct`` tier of the
    import-graph selection (``affected_tests``, task #2231):

    - changed test files (``tests/**/test_*.py`` / ``*_test.py``) that still exist,
      including the new path of a renamed file;
    - ``tests/**/test_<stem>.py`` for each changed ``src/**/<stem>.py`` or
      ``scripts/<stem>.py`` (``__init__.py`` maps to its package name);
    - every test that imports a changed module, names it in a patch target, or
      reads a changed file by path, directly or through test helpers;
    - the cross-cutting inventory tests (``affected_tests.ALWAYS_RUN``), but only
      when something else maps, so a diff with no tests of its own still waits
      for batch CI.

    Returns ``(paths, full_reasons, blind_spots)``. ``full_reasons`` is
    non-empty when the diff touches something every test depends on
    (``pyproject.toml``, ``tests/conftest.py``, ...) or a file nothing names;
    only the full suite can verify such a diff. ``blind_spots`` says what the
    static analysis could not read: tests that run on every change because
    their imports are computed, and src modules whose dynamic imports are not
    followed. Tests selected only for that reason, or only because they scan
    the whole source tree as text, do not count as the diff mapping to tests.
    Paths are worktree-relative and sorted.
    """
    from . import affected_tests

    selection = affected_tests.select(
        wt_path, affected_tests.changes_from_entries(changed), depth="direct"
    )
    blind_spots = list(selection.caveats)
    if selection.unresolved_tests:
        blind_spots.append(
            f"{len(selection.unresolved_tests)} test file(s) run on every change because "
            f"their imports cannot be read statically (e.g. {selection.unresolved_tests[0]})"
        )
    if selection.full:
        return [], selection.full_reasons, blind_spots
    if not set(selection.tests) - set(selection.unresolved_tests) - set(selection.scanner_tests):
        return [], [], blind_spots
    return selection.paths(), [], blind_spots


def _targeted_test_paths(wt_path: str, changed: list[dict]) -> list[str]:
    """The test files of :func:`_targeted_test_plan` (empty when it needs the full suite)."""
    return _targeted_test_plan(wt_path, changed)[0]


def _diff_for_targeted_tests(wt_path: str, branch: str) -> list[dict]:
    """The branch diff for test selection; its own seam, apart from red-tier classification."""
    from . import cycle_eligibility

    return cycle_eligibility._changed_files_vs_main(wt_path, branch)


def _awaiting_ci_at_sha(notes: str, sha: str) -> bool:
    if not sha:
        return False
    return bool(re.search(_AWAITING_CI_AT_SHA_TEMPLATE.format(sha=re.escape(sha)), notes))


def _notify_best_effort(message: str) -> bool:
    try:
        from .tools.notify import internal_route, notify

        with internal_route("review_pipeline"):
            return bool(notify(message).get("delivered", False))
    except Exception:
        return False


# ── notes parsing helpers ─────────────────────────────────────────────────────


def _pr_url_from_notes(notes: str) -> str | None:
    """Extract a PR URL from task notes.

    Prefers an explicit ``PR: <url>`` line, but falls back to the first bare
    GitHub pull-request URL anywhere in the notes — live task notes are prose
    and rarely use the colon form (observed 2026-07-17: tasks #62/#64 wrote
    'PR https://…', reconcile missed the merge and marked them stale because
    the squash-merged branch's remote ref was already deleted, so the
    branch-name fallback had nothing to resolve)."""
    m = _PR_URL_RE.search(notes)
    if m:
        # Trim prose punctuation that \S+ drags along ("…/pull/31," or "…/pull/31]").
        return m.group(1).rstrip(".,;:)]")
    m = _PR_URL_BARE_RE.search(notes)
    return m.group(0) if m else None


def _pr_ref_from_notes(
    notes: str,
    *,
    skip_merged_upstream: bool = False,
    after_pos: int | None = None,
) -> str | None:
    """A resolvable PR reference from task notes.

    Only pipeline-generated marker lines (e.g. starting with '[review:' or
    '[pr:') are trusted; author-supplied prose is ignored so unrelated PR
    mentions cannot trigger false reconcile completions.

    When ``skip_merged_upstream`` is set, historical ``[review: merged-upstream]``
    lines are ignored. When ``after_pos`` is set, only marker lines whose
    absolute offset in ``notes`` starts at or after that position are
    considered (live re-review: markers belonging to the current cycle).
    With either filter active, the *last* matching marker wins.
    """
    take_last = skip_merged_upstream or after_pos is not None
    found: str | None = None
    offset = 0
    for line in (notes or "").splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        stripped = line.strip()
        if not stripped.lower().startswith(_PR_MARKER_PREFIXES):
            continue
        # Anchor on the first non-whitespace char so leading indent still
        # places the marker after a preceding waiting-for line.
        marker_pos = line_start + (len(line) - len(line.lstrip(" \t")))
        if after_pos is not None and marker_pos < after_pos:
            continue
        if skip_merged_upstream and stripped.lower().startswith(REVIEW_MERGED_UPSTREAM.lower()):
            continue
        candidate: str | None = None
        url = _pr_url_from_notes(line)
        if url:
            candidate = url
        else:
            m = _PR_MARKER_NUMBER_RE.search(line)
            if m:
                candidate = m.group(1)
        if candidate is None:
            continue
        if take_last:
            found = candidate
            continue
        return candidate
    return found


def _live_review_marker_pos(notes: str | None) -> int | None:
    """Start offset of the latest unresolved ``[waiting-for: review]``, or None."""
    return task_notes.live_review_request_end(notes)


def _pr_ref_for_review(notes: str, evidence_ref: str | None) -> str | None:
    """PR for the current review / reconcile cycle.

    On a live ``[waiting-for: review]`` (Devin #756):

    - only trust PR markers that appear *after* that live marker (current
      cycle), skipping historical ``[review: merged-upstream]`` lines
    - otherwise fall back to ``evidence_ref`` (reopened work often updates
      the column without rewriting an older ``[pr:]`` note)

    Without a live marker, notes (including merged-upstream) still win over
    evidence — existing first-cycle / already-reconciled behavior.
    """
    live_pos = _live_review_marker_pos(notes)
    if live_pos is not None:
        from_notes = _pr_ref_from_notes(notes, skip_merged_upstream=True, after_pos=live_pos)
        from_evidence = _pr_ref_from_evidence(evidence_ref)
        return from_notes or from_evidence
    from_notes = _pr_ref_from_notes(notes)
    from_evidence = _pr_ref_from_evidence(evidence_ref)
    return from_notes or from_evidence


def _pr_ref_from_evidence(evidence_ref: str | None) -> str | None:
    """A resolvable PR reference from the task's ``evidence_ref`` column.

    Cursor's PR flow merges on GitHub and records the PR on the row's
    ``evidence_ref`` without always writing it back into the notes prose — so
    reconcile could not see the merge, the task stayed ``waiting_review`` with
    its worktree already cleaned, and the review pass then emitted a false
    ``no git worktree found`` FAILED (UA #851; the "no git worktree" race has
    recurred on this exact shape since 2026-07). Accepts a bare PR URL, a
    ``…/pull/N`` URL embedded in a longer string, or a value that is only a PR
    number (``N``, ``#N``, ``PR #N``). ``gh pr view`` resolves any of these.

    A number must be the whole value (task #2269). Trailing digits of other
    evidence are not a PR: a commit SHA, timestamp or prose such as
    ``"... LIVE at 65e19b43fe25"`` once resolved to PR #25, an unrelated merged
    PR, and closed the task with false merged-upstream evidence."""
    if not evidence_ref:
        return None
    ev = evidence_ref.strip()
    m = _PR_URL_BARE_RE.search(ev)
    if m:
        return m.group(0)
    m = _PR_EVIDENCE_NUMBER_RE.fullmatch(ev)
    return m.group(1) if m else None


def _branch_from_notes(notes: str) -> str | None:
    """Extract a 'branch <name>' reference, ignoring [review: …] note lines
    (which may contain the phrase 'branch missing')."""
    cleaned = "\n".join(
        line for line in notes.splitlines() if not line.strip().startswith("[review:")
    )
    m = _BRANCH_NOTE_RE.search(cleaned)
    return m.group(1) if m else None


def _plan_id_from_notes(notes: str) -> str | None:
    """Extract the 'plan: <id>' reference from a task's notes.

    The regex requires the literal ``plan`` token followed by ``:``/space/``#``,
    so the pipeline's own ``[waiting-for: plan-approval]`` / ``[plan-approval:
    …]`` note lines (where ``plan`` is followed by ``-``) never false-match, and
    the reference may share a line with the annotation.
    """
    m = _PLAN_ID_RE.search(notes)
    return m.group(1) if m else None


def _plan_fingerprint(plan_data: dict) -> str:
    """Short, stable digest of a plan's reviewable state.

    Changes whenever the plan's updated_at, status, or any step's status/owner
    moves — so an edited plan re-notifies exactly once, an unchanged one never
    re-pages. Pure function of the plan payload; no clock, no randomness.
    """
    plan = plan_data.get("plan", {})
    parts = [str(plan.get("updated_at", "")), str(plan.get("status", ""))]
    for s in plan_data.get("steps", []):
        parts.append(f"{s.get('step_idx')}:{s.get('status')}:{s.get('owner')}")
    digest = hashlib.blake2s("|".join(parts).encode(), digest_size=6).hexdigest()
    return digest


def _plan_summary(plan_data: dict) -> str:
    """A compact human summary of a plan for the approval notification."""
    plan = plan_data.get("plan", {})
    steps = plan_data.get("steps", [])
    checkpoints = plan_data.get("checkpoints", [])
    lines = [
        f"Plan {plan.get('id', '?')} — {plan.get('goal', '(no goal)')}",
        f"scope={plan.get('scope', 'default')} status={plan.get('status', '?')}"
        + (" [needs human]" if plan.get("needs_human") else ""),
        f"{len(steps)} step(s):",
    ]
    for s in steps:
        owner = s.get("owner") or "unassigned"
        lines.append(f"  #{s.get('step_idx')} [{s.get('status')}] {owner}: {s.get('description')}")
    if checkpoints:
        last = checkpoints[-1]
        lines.append(f"latest checkpoint (seq {last.get('seq')}): {last.get('note') or '—'}")
    return "\n".join(lines)


def _already_reviewed_at_sha(notes: str, sha: str) -> bool:
    """True if a validated/rejected/red-tier note already exists for this sha.

    A pre-#2321 no-checkout hold was written as ``[review: validated …] no
    local worktree on this host; branch <name> …``. No gate ran for it, and a
    branch named after a commit put that SHA in the line, so such a line never
    counts as a review at this head.
    """
    if not sha:
        return False
    pat = re.compile(_REVIEWED_AT_SHA_TEMPLATE.format(sha=re.escape(sha)))
    return any(_LEGACY_NO_CHECKOUT_HOLD not in m.group(0) for m in pat.finditer(notes))


# The note only the gates path writes on a green run (`_validate_and_hold`):
# "[review: validated @ <ts>] <delivery> gates green at <sha>; ready …". The
# phrase has spaces, so it cannot come from a branch name, and the SHA must end
# at the ";" so a longer hex string never matches a prefix of it.
_GATES_GREEN_AT_SHA_TEMPLATE = r"^\[review: validated @ [^\]\n]*\][^\n]* gates green at {sha};"


def _validated_note_at_sha(notes: str, sha: str) -> bool:
    """True when the gates ran green at this sha (task #2321 review).

    Gates the policy-read retry, so it needs the gate run's own wording, not
    just any validated line that mentions the sha.
    """
    if not sha:
        return False
    pat = re.compile(_GATES_GREEN_AT_SHA_TEMPLATE.format(sha=re.escape(sha)), re.MULTILINE)
    return bool(pat.search(notes))


def _timeout_count_at_sha(notes: str, sha: str | None) -> int:
    """Number of pytest/bun-test timeout notes already recorded for this exact sha.

    Task #1254 follow-up: bounds the timeout-retry loop. Without a sha (e.g.
    `_merge_done_task` could not resolve one) this can't tell one commit's
    timeouts from another's, so it returns 0 rather than guessing — the
    caller falls back to `_timeout_count_total` instead (task #1295
    follow-up) so that path stays bounded too, just more coarsely.
    """
    if not sha:
        return 0
    pat = re.compile(_REVIEW_TIMEOUT_AT_SHA_TEMPLATE.format(sha=re.escape(sha)))
    return len(pat.findall(notes))


_REVIEW_TIMEOUT_ANY_RX = re.compile(r"\[review: timeout @ [^\]]*\]")


def _timeout_count_total(notes: str) -> int:
    """Number of timeout notes recorded for this task, regardless of sha.

    Fallback for `_merge_done_task` when no head sha could be resolved (task
    #1295 follow-up to #1254): `_timeout_count_at_sha` can't attribute a
    timeout to one commit without a sha, which used to leave that path's
    retries unbounded — a suite that never finishes would re-run on every
    invocation forever. This is coarser than the at-sha count (it doesn't
    reset when a new commit lands), but bounded beats unbounded.
    """
    return len(_REVIEW_TIMEOUT_ANY_RX.findall(notes))


# ── scan / reconcile ──────────────────────────────────────────────────────────


def _is_no_code_task(notes: str, evidence_ref: str | None = None) -> bool:
    """True when the task explicitly opts out of branch/code review.

    Reporting-only work must carry ``[no-code]``. Inferring no-code from a missing
    ``[code-task]`` marker wrongly skipped named-project code tasks that never
    needed the marker to get a project-sourced worktree.
    """
    if not _NO_CODE_RX.search(notes or ""):
        return False
    # Evidence is optional but typical for reporting-only completions.
    return True


def scan_for_reviewable_tasks() -> dict:
    """Find all tasks annotated with [waiting-for: review] or [waiting-for: plan-approval].

    Each returned task carries an additive `kind` field: 'review' for the code
    review path, 'scout' for FirstMate-style investigation reports (task #1027),
    'plan-approval' for the plan-approval path (a task annotated with both
    review and plan-approval is reported as 'review' unless it is a scout).
    Existing consumers that ignore `kind` are unaffected. Explicit ``[no-code]``
    tasks are omitted so they never hit the missing-worktree validate path —
    except scouts, which are report-only and still need captain review.
    Returns {tasks: [{id, title, status, priority, notes, kind, …}]}.
    """
    try:
        storage.require_live_task_store()
    except storage.LiveStoreError as e:
        raise ToolError(str(e)) from e
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE (notes LIKE ? OR notes LIKE ?) "
            "AND status IN ('open', 'doing', 'waiting_review', 'blocked')",
            (f"%{WAITING_FOR_REVIEW}%", f"%{WAITING_FOR_PLAN_APPROVAL}%"),
        ).fetchall()
        tasks = []
        for r in rows:
            d = {k: r[k] for k in r.keys()}
            notes = d.get("notes") or ""
            from .scout import is_scout

            if _is_no_code_task(notes, d.get("evidence_ref")) and not is_scout(notes):
                continue
            if _is_open_boundary_review_spec(d, notes):
                continue
            if not (
                _has_live_review_marker(notes)
                or _notes_mention(notes, WAITING_FOR_PLAN_APPROVAL)
                or _notes_mention(notes, WAITING_FOR_HUMAN_MERGE)
                or _is_active_review_handoff(d, notes)
            ):
                continue
            d["kind"] = _scan_task_kind(notes)
            tasks.append(d)
        return {"tasks": tasks}
    finally:
        conn.close()


def scan_outcomes_awaiting_review(limit: int = 100) -> dict:
    """Surface unreviewed terminal delegation outcomes for the review cron (#1142).

    Read-only projection over `delegations` + review annotations. Never mutates
    tasks or blocks completion — it makes the unreviewed outcome set visible.
    """
    try:
        storage.require_live_task_store()
    except storage.LiveStoreError as e:
        raise ToolError(str(e)) from e
    from .tools.delegate import outcomes_awaiting_review

    return outcomes_awaiting_review(limit=limit)


def _reconcile_one_task(
    row,
    *,
    results: list[dict],
    worktrees_by_repo: dict[str, list[dict]],
    fetched: set[str],
    dry_run: bool,
) -> None:
    """Reconcile a single in-flight review task; append verdicts to ``results``.

    Raises on unexpected lookup failures so the caller can log + skip the row
    without aborting the rest of the reconcile pass (task #1333).
    """
    from . import run_records

    task_id = row["id"]
    completion = run_records.capture_direct_completion(task_id, row)
    if completion is None:
        return
    title = row["title"]
    notes = row["notes"] or ""

    if _is_open_boundary_review_spec(dict(row), notes):
        return
    conn = storage.connect()
    try:
        if _active_reconcile_blocker(conn, row):
            return
    finally:
        conn.close()
    # A markerless task in ``waiting_review`` (UA #851): a handoff can land a
    # task there with a merged PR on its ``evidence_ref`` and never write a
    # ``[waiting-for: review]`` marker. Reconcile it if — and only if — the
    # PR is actually merged; the ``stale`` / ``unresolved-repo`` verdicts
    # below are review-flow signals and must not fire on it.
    merge_only = not (
        _has_live_review_marker(notes)
        or _notes_mention(notes, WAITING_FOR_HUMAN_MERGE)
        or _is_active_review_handoff(dict(row), notes)
    )
    # Markerless doing/waiting_review rows are eligible only for a confirmed
    # merged PR; they must never receive stale/unresolved review verdicts.

    # Explicit [no-code] reporting-only tasks have no branch to reconcile.
    # Do not infer this from a missing [code-task] marker — named projects
    # mint worktrees without that marker (see tools.tasks._worktree_source_repo).
    if _is_no_code_task(notes, row["evidence_ref"]):
        return

    project = row["project"]
    # Same full-row routing as merge.find_task_worktree: product provenance is
    # exclusive even when its checkout is absent on this host. Only a valid
    # legacy workspace-relative checkout keeps the cockpit fallback (#2077).
    from . import merge
    from .tools.tasks import DEFAULT_PROJECT, _worktree_source_repo

    routed, exclusive = merge._task_repo_routing(row)
    fallback_repo: str | None = None
    if routed is not None:
        repo = routed
        if not exclusive:
            fallback_repo = str(config.workspace_root())
    elif exclusive:
        repo = None
    else:
        # An unmarked default-project row may predate #2041 and live in the
        # workspace, so only the others resolve to their product checkout here.
        unmarked_default = project == DEFAULT_PROJECT and not _CODE_TASK_RX.search(notes)
        source = None if unmarked_default else _worktree_source_repo(project)
        if source is not None:
            repo = source
        elif unmarked_default:
            repo = config.workspace_root()
        else:
            repo = None
    pr_ref_early = _pr_ref_for_review(notes, row["evidence_ref"])
    task_pr = None
    if merge_only and not pr_ref_early and repo is not None:
        task_pr = _gh_pr_for_task(task_id, repo)
        pr_ref_early = str(task_pr["url"]) if task_pr and task_pr.get("url") else None
    if repo is not None and not Path(repo).is_dir():
        repo = None
    if repo is None:
        # Absolute PR URLs are still resolvable via `gh pr view <url>` with
        # no local checkout. Prefer that over a false unresolved-repo note.
        if pr_ref_early and (
            pr_ref_early.startswith("http://") or pr_ref_early.startswith("https://")
        ):
            if _gh_pr_state(pr_ref_early, None) == "MERGED":
                # Same live-marker rule as the in-repo path (Devin #756).
                if REVIEW_MERGED_UPSTREAM in notes and not _has_live_review_marker(notes):
                    return
                if not dry_run:
                    metadata = _coerce_pr_metadata(
                        task_pr or _gh_pr_merge_details(pr_ref_early, None)
                    )
                    pr_url = metadata.get("url") or pr_ref_early
                    details = f" PR {pr_url}"
                    sha = _merge_commit_oid(metadata)
                    if sha:
                        details += f" merge_sha={sha}"
                    if not _mark_done_with_note(
                        task_id,
                        f"[review: merged-upstream @ {_ts()}]{details}",
                        expected=completion,
                    ):
                        return
                results.append(
                    {
                        "task_id": task_id,
                        "verdict": "merged-upstream",
                        "red_tier_files": [],
                        "merge_result": None,
                        "notification": (
                            f"✅ Task #{task_id} ({title}) PR already merged upstream "
                            "(resolved from absolute URL without a local checkout)."
                        ),
                    }
                )
                return
        if merge_only:
            return  # markerless waiting_review: only a merged PR reconciles it
        if REVIEW_UNRESOLVED_REPO in notes:
            return
        if not dry_run:
            _append_note(
                task_id,
                f"[review: unresolved-repo @ {_ts()}] no checked-out repo for "
                f"project {project!r}; branch state not verifiable",
            )
        results.append(
            {
                "task_id": task_id,
                "verdict": "unresolved-repo",
                "red_tier_files": [],
                "merge_result": None,
                "notification": (
                    f"⚠️ Task #{task_id} ({title}) is waiting for review but no "
                    f"checked-out repo resolves for project {project!r} — its "
                    "branch state cannot be verified."
                ),
            }
        )
        return

    main_repo = str(repo)
    if main_repo not in fetched:
        _fetch_origin(main_repo)
        fetched.add(main_repo)
    if main_repo not in worktrees_by_repo:
        worktrees_by_repo[main_repo] = _list_worktrees(main_repo)
    worktrees = worktrees_by_repo[main_repo]

    # Resolve the branch: worktree match first, then a 'branch <name>' note.
    # The id must be a task-id token (merge.names_task_id): `pr-<N>`-style
    # worktrees name a PR number, never task N (task #1844).
    def _worktree_branch(candidates: list[dict]) -> str | None:
        for wt in candidates:
            if merge.names_task_id(wt.get("branch") or "", task_id, slug=False) or (
                merge.names_task_id(wt.get("path") or "", task_id, slug=False)
            ):
                return wt.get("branch") or None
        return None

    def _use_fallback_repo() -> str:
        if fallback_repo not in fetched:
            _fetch_origin(fallback_repo)
            fetched.add(fallback_repo)
        return fallback_repo

    if fallback_repo is not None and not Path(fallback_repo).is_dir():
        fallback_repo = None
    branch = _worktree_branch(worktrees)
    if not branch and fallback_repo is not None:
        if fallback_repo not in worktrees_by_repo:
            worktrees_by_repo[fallback_repo] = _list_worktrees(fallback_repo)
        branch = _worktree_branch(worktrees_by_repo[fallback_repo])
        if branch:
            main_repo = _use_fallback_repo()
    branch_local = bool(branch)
    if not branch:
        branch = _branch_from_notes(notes)
        if branch:
            branch_local = _branch_exists_locally(branch, main_repo)
            if (
                not branch_local
                and fallback_repo is not None
                and _branch_exists_locally(branch, fallback_repo)
            ):
                main_repo = _use_fallback_repo()
                branch_local = True
    pr_ref = _pr_ref_for_review(notes, row["evidence_ref"])

    ref = pr_ref or branch
    # An explicit PR is the delivery artifact. A hub-local task branch can
    # still point at an old base while a native author has newer work on the
    # PR's remote head; local ancestry must not override OPEN/CLOSED/unknown.
    merged = bool(
        not pr_ref
        and not merge_only
        and branch
        and branch_local
        and _branch_merged_into_origin_main(branch, main_repo)
    )
    if not merged:
        if merge_only and not ref:
            task_pr = _gh_pr_for_task(task_id, main_repo)
            ref = str(task_pr["url"]) if task_pr and task_pr.get("url") else None
        if ref and _gh_pr_state(ref, main_repo) == "MERGED":
            merged = True

    if merged:
        # Same live-marker rule as _validate_and_hold (Devin #756).
        if REVIEW_MERGED_UPSTREAM in notes and not _has_live_review_marker(notes):
            return  # already reconciled — never re-note or re-notify
        if not dry_run:
            metadata = _coerce_pr_metadata(
                task_pr or (_gh_pr_merge_details(ref, main_repo) if ref else {})
            )
            pr_url = metadata.get("url") or (ref if str(ref).startswith("http") else None)
            details = f" PR {pr_url}" if pr_url else ""
            sha = _merge_commit_oid(metadata)
            if sha:
                details += f" merge_sha={sha}"
            if not _mark_done_with_note(
                task_id,
                f"[review: merged-upstream @ {_ts()}]{details}",
                expected=completion,
            ):
                return
        results.append(
            {
                "task_id": task_id,
                "verdict": "merged-upstream",
                "red_tier_files": [],
                "merge_result": None,
                "notification": (
                    f"✅ Task #{task_id} ({title}) branch already merged into "
                    "origin/main — marked done."
                ),
            }
        )
    elif merge_only:
        return  # markerless rows only reconcile on a confirmed merged PR
    elif not branch_local and not pr_ref:
        if REVIEW_STALE in notes:
            return  # already flagged — report once
        if not dry_run:
            _append_note(task_id, f"[review: stale @ {_ts()}] stale — branch missing")
        results.append(
            {
                "task_id": task_id,
                "verdict": "stale",
                "red_tier_files": [],
                "merge_result": None,
                "notification": (
                    f"⚠️ Task #{task_id} ({title}) is waiting for review but its "
                    "branch is missing and no PR is referenced."
                ),
            }
        )
    # else: branch exists and is not merged yet — leave it for the review pass


def reconcile_tasks(send_notifications: bool = False, dry_run: bool = False) -> dict:
    """Reconcile in-flight review tasks with origin/main before scanning.

    For every non-``done`` task (``open``/``doing``/``waiting_review``/``blocked`` —
    task #303) annotated ``[waiting-for: review]``
    or ``[waiting-for: human-merge]``:

    - branch already merged into origin/main (ancestry after a fetch, or gh
      PR state MERGED — catches squash merges) → status ``done`` plus one
      ``[review: merged-upstream @ ts]`` note
    - branch missing locally and no PR referenced (URL *or* a bare ``PR #N``
      number — see ``_pr_ref_from_notes``) → one idempotent
      ``stale — branch missing`` note; reported once

    Both notes are idempotent: each state is recorded and notified exactly
    once. Returns {results: [{task_id, verdict, notification, notified?}]}.
    """
    try:
        storage.require_live_task_store()
    except storage.LiveStoreError as e:
        raise ToolError(str(e)) from e
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status IN ('open', 'doing', 'waiting_review', 'blocked') "
            "AND ((notes LIKE ? OR notes LIKE ?) OR status IN ('doing', 'waiting_review'))",
            (f"%{WAITING_FOR_REVIEW}%", f"%{WAITING_FOR_HUMAN_MERGE}%"),
        ).fetchall()
    finally:
        conn.close()

    results: list[dict] = []
    if not rows:
        return {"results": results}

    # Task #494: the repo is resolved per task, from the task's own project,
    # instead of once from config.workspace_root(). Worktree listings and
    # fetches are cached per repo so a queue full of same-project tasks still
    # costs one fetch.
    worktrees_by_repo: dict[str, list[dict]] = {}
    fetched: set[str] = set()

    for row in rows:
        task_id = row["id"]

        # Per-row isolation (task #1333): one None lookup / gh quirk must log
        # and skip that row — never abort the whole reconcile step (2026-09-23
        # scan aborted early with ``'NoneType' object has no attribute 'get'``,
        # leaving already-merged tasks for the review pass to false-reject).
        try:
            _reconcile_one_task(
                row,
                results=results,
                worktrees_by_repo=worktrees_by_repo,
                fetched=fetched,
                dry_run=dry_run,
            )
        except (TypeError, AttributeError, KeyError, json.JSONDecodeError) as exc:
            # Recoverable PR/metadata lookup quirks only (null mergeCommit,
            # non-dict gh payloads). Deliberately excludes ValueError and
            # broader Exception so task-store / note-write failures cannot
            # look like a clean per-row skip (Devin analysis on #756).
            log.warning(
                "reconcile: skipping task #%s after lookup error: %s",
                task_id,
                exc,
            )
            continue
        except Exception:
            log.exception("reconcile: task-store or unexpected error on task #%s", task_id)
            raise

    if send_notifications and not dry_run:
        for r in results:
            r["notified"] = _notify_best_effort(r["notification"])

    return {"results": results}


# ── plan-approval consumer (task #65) ─────────────────────────────────────────


def _plan_approval_note(row, line: str, *, dry_run: bool) -> bool:
    """Append only to the same visible task revision the plan scan read."""
    conn = storage.connect()
    try:
        args = (row["id"], row["org_id"], row["updated_at"], row["notes"], row["status"])
        where = "id=? AND org_id IS ? AND updated_at=? AND notes IS ? AND status=?"
        if dry_run:
            return conn.execute(f"SELECT 1 FROM tasks WHERE {where}", args).fetchone() is not None
        cur = conn.execute(
            f"UPDATE tasks SET notes=COALESCE(notes,'') || ?, updated_at=? WHERE {where}",
            (f"\n{line}", storage.next_revision(row["updated_at"]), *args),
        )
        conn.commit()
        changed = cur.rowcount == 1
    finally:
        conn.close()
    if changed:
        sync_state_to_vault()
    return changed


def review_plan_approvals(send_notifications: bool = False, dry_run: bool = False) -> dict:
    """Surface plans awaiting human approval; notify once per plan-state change.

    For every non-``done`` task (``open``/``doing``/``blocked`` — task #303)
    annotated ``[waiting-for: plan-approval]``:

    - It references its plan with a ``plan: <id>`` note line. The plan is loaded
      read-only and summarized; the operator is notified **once per plan-state
      fingerprint** (updated_at + each step's status/owner). An unchanged plan is
      never re-paged; an edited plan re-notifies exactly once. Idempotency is
      recorded as a ``[plan-approval: notified @ ts] <fingerprint>`` note line,
      mirroring the per-sha review notes.
    - A missing/malformed ``plan:`` line, or a plan id that does not exist, yields
      one idempotent ``[plan-approval: unresolvable @ ts]`` note + notification
      (mirrors the reconcile ``stale`` path).

    The pipeline never approves: a human approves by replacing the annotation with
    ``[plan: approved @ ts]`` (or removing it), which drops the task from this
    scan on the next run. Read-only — no plan mutation, no remote write, no LLM.
    Returns {results: [{task_id, verdict, notification, notified?}]}.
    """
    from .tools.plan import plan_get

    try:
        storage.require_live_task_store()
    except storage.LiveStoreError as e:
        raise ToolError(str(e)) from e

    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE notes LIKE ? AND org_id IS ? "
            "AND status IN ('open', 'doing', 'waiting_review', 'blocked')",
            (f"%{WAITING_FOR_PLAN_APPROVAL}%", edition.tenant_org_id()),
        ).fetchall()
    finally:
        conn.close()

    results: list[dict] = []
    for row in rows:
        task_id = row["id"]
        title = row["title"]
        notes = row["notes"] or ""

        plan_id = _plan_id_from_notes(notes)
        plan_data = None
        if plan_id is not None:
            try:
                plan_data = plan_get(plan_id)
            except Exception:
                plan_data = None  # unknown/deleted plan → unresolvable below

        if plan_id is None or plan_data is None:
            if PLAN_APPROVAL_UNRESOLVABLE in notes:
                continue  # already flagged — report once
            detail = (
                "no 'plan: <id>' reference in notes"
                if plan_id is None
                else f"plan {plan_id!r} does not exist"
            )
            if not _plan_approval_note(
                row, f"{PLAN_APPROVAL_UNRESOLVABLE} @ {_ts()}] {detail}", dry_run=dry_run
            ):
                continue
            results.append(
                {
                    "task_id": task_id,
                    "verdict": "unresolvable",
                    "plan_id": plan_id,
                    "notification": (
                        f"⚠️ Task #{task_id} ({title}) waits for plan-approval but "
                        f"{detail}. Add a 'plan: <id>' line or clear the annotation."
                    ),
                }
            )
            continue

        fingerprint = _plan_fingerprint(plan_data)
        # Idempotency: skip if we already notified at this exact fingerprint.
        if f"{PLAN_APPROVAL_NOTIFIED}" in notes and fingerprint in notes:
            continue

        if not _plan_approval_note(
            row, f"{PLAN_APPROVAL_NOTIFIED} @ {_ts()}] {fingerprint}", dry_run=dry_run
        ):
            continue
        results.append(
            {
                "task_id": task_id,
                "verdict": "awaiting-approval",
                "plan_id": plan_id,
                "fingerprint": fingerprint,
                "notification": (
                    f"📋 Task #{task_id} ({title}) awaits your plan approval.\n\n"
                    f"{_plan_summary(plan_data)}\n\n"
                    "Approve by replacing [waiting-for: plan-approval] with "
                    "[plan: approved @ <ts>] in the task notes (or removing it)."
                ),
            }
        )

    if send_notifications and not dry_run:
        for r in results:
            r["notified"] = _notify_best_effort(r["notification"])

    return {"results": results}


# ── review ────────────────────────────────────────────────────────────────────


def review_task(task_id: int, dry_run: bool = False) -> dict:
    """Run the review pipeline on a task.

    Contract:

    - status ``doing``/``blocked`` + ``[waiting-for: review]`` → **validate
      and hold**: run the gates (security audit + pytest + red-tier diff
      classification), record one note per (state, branch-head sha), never
      merge. Verdicts: ``validated`` | ``red-tier`` | ``failed`` | ``timeout`` |
      ``unchanged``.
    - status ``done`` → the original local-merge flow (still valid for non-PR
      worktree tasks). Verdicts: ``merged`` | ``red-tier`` | ``failed`` |
      ``timeout`` | ``dry-run-ok``.

    Args:
        task_id: The task ID to review.
        dry_run: Run all validations but write no notes and never merge.

    Returns:
        {task_id, verdict, red_tier_files, merge_result, notification,
         head_sha}
    """
    # 1. Load the task
    conn = storage.connect()
    try:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()

    if not task:
        raise ToolError(f"Task {task_id} does not exist.")

    from . import run_records

    completion = run_records.capture_direct_completion(task_id, task)
    if completion is None:
        return _result(task_id, "unchanged", None)
    notes = task["notes"] or ""
    title = task["title"]
    project = task["project"] if "project" in task.keys() else None

    if WAITING_FOR_REVIEW not in notes:
        raise ToolError(
            f"Task {task_id} is not annotated with {WAITING_FOR_REVIEW}. "
            "The dev must annotate the task before requesting review."
        )

    from .scout import is_scout

    if is_scout(notes):
        return _review_scout_task(
            task_id,
            title,
            notes,
            evidence_ref=task["evidence_ref"],
            dry_run=dry_run,
        )

    with run_records.direct_completion_scope(completion):
        try:
            if task["status"] == "done":
                return _merge_done_task(task_id, title, notes, dry_run)
            return _validate_and_hold(
                task_id,
                title,
                notes,
                dry_run,
                project=project,
                evidence_ref=task["evidence_ref"],
                task=dict(task),
            )
        except run_records.DirectCompletionChanged:
            return _result(task_id, "unchanged", None)


def _review_scout_task(
    task_id: int,
    title: str,
    notes: str,
    *,
    evidence_ref: str | None,
    dry_run: bool,
) -> dict:
    """Validate a scout report artifact; never merge, never require a PR branch.

    Verdicts: ``scout-ready`` | ``scout-incomplete`` | ``unchanged``.
    """
    from .scout import (
        report_path_from_evidence,
        report_path_from_notes,
        validate_scout_report,
    )

    current_notes = task_notes.review_cycle_notes(notes, keep_quoted=True)
    visible_notes = task_notes.strip_quoted_spans(current_notes)

    def body_fingerprint(body: str) -> str:
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def verdict_note(marker: str, body: str) -> str:
        return f"{marker} @ {_ts()} body-sha256={body_fingerprint(body)}] {body}"

    def already_reviewed(marker: str, body: str) -> bool:
        # Admit only an unquoted verdict header in this review cycle. The
        # exact-body digest preserves quotes/newlines and cannot match a
        # shorter error or borrow bytes from another appended note. Legacy
        # unkeyed verdicts receive one validation, then use this exact key.
        headers = re.finditer(
            r"(?m)^[ \t]*" + re.escape(marker) + r" @ [^\]\r\n]*\] ", visible_notes
        )
        key = f" body-sha256={body_fingerprint(body)}] "
        return any(header.group(0).endswith(key) for header in headers)

    report_path = report_path_from_notes(notes) or report_path_from_evidence(evidence_ref)
    if not report_path:
        reason = "scout report path missing from notes and evidence_ref"
        note = verdict_note(REVIEW_SCOUT_INCOMPLETE, reason)
        if already_reviewed(REVIEW_SCOUT_INCOMPLETE, reason):
            return _result(task_id, "unchanged", None)
        if not dry_run:
            _append_note(task_id, note)
        notification = f"Scout #{task_id} incomplete — {title}: {reason}"
        return _result(task_id, "scout-incomplete", notification)

    # Idempotency is scoped to the latest request, not a prior report cycle.
    ready_body = f"report={report_path} decision inventory ready for captain — no PR/merge"
    if already_reviewed(REVIEW_SCOUT_READY, ready_body):
        return _result(task_id, "unchanged", None)

    markdown = ""
    read_error: str | None = None
    try:
        from .tools.vault import vault_read

        payload = vault_read(report_path, offset=0, limit=5000)
        markdown = payload.get("content") or ""
        if payload.get("truncated"):
            # Cap is generous; treat truncation as incomplete so we never
            # falsely certify a cut-off decision inventory.
            read_error = f"report truncated at {payload.get('total_lines')} lines"
    except Exception as exc:
        read_error = str(exc)[:200]

    if read_error is not None:
        reason = f"could not read scout report {report_path!r}: {read_error}"
        note = verdict_note(REVIEW_SCOUT_INCOMPLETE, reason)
        if already_reviewed(REVIEW_SCOUT_INCOMPLETE, reason):
            return _result(task_id, "unchanged", None)
        if not dry_run:
            _append_note(task_id, note)
        return _result(
            task_id,
            "scout-incomplete",
            f"Scout #{task_id} incomplete — {title}: {reason}",
        )

    problems = validate_scout_report(markdown)
    if problems:
        reason = f"report {report_path}: " + "; ".join(problems)
        note = verdict_note(REVIEW_SCOUT_INCOMPLETE, reason)
        if already_reviewed(REVIEW_SCOUT_INCOMPLETE, reason):
            return _result(task_id, "unchanged", None)
        if not dry_run:
            _append_note(task_id, note)
        return _result(
            task_id,
            "scout-incomplete",
            f"Scout #{task_id} incomplete — {title}: {reason}",
        )

    note = verdict_note(REVIEW_SCOUT_READY, ready_body)
    if not dry_run:
        _append_note(task_id, note)
        # Surface as waiting_review so Control Center / humans see it.
        try:
            conn = storage.connect()
            try:
                conn.execute(
                    "UPDATE tasks SET status=?, updated_at=? WHERE id=? AND status!=?",
                    (
                        "waiting_review",
                        storage.next_task_revision(conn, task_id),
                        task_id,
                        "done",
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            log.warning("scout review could not flip task #%s to waiting_review", task_id)

    notification = (
        f"Scout #{task_id} ready — {title}\n"
        f"Report: {report_path}\n"
        "Decision inventory awaiting captain action (no PR)."
    )
    return _result(task_id, "scout-ready", notification)


def _result(
    task_id: int,
    verdict: str,
    notification: str | None,
    red_tier_files: list[str] | None = None,
    merge_result: dict | None = None,
    head_sha: str | None = None,
) -> dict:
    return {
        "task_id": task_id,
        "verdict": verdict,
        "red_tier_files": red_tier_files or [],
        "merge_result": merge_result,
        "notification": notification,
        "head_sha": head_sha,
    }


def _validate_and_hold(
    task_id: int,
    title: str,
    notes: str,
    dry_run: bool,
    *,
    project: str | None = None,
    evidence_ref: str | None = None,
    task: dict | None = None,
) -> dict:
    """Validate an in-flight task's branch and hold it for a human merge.

    Never calls the merge path unless the project's delivery policy has
    ``yolo`` on (task #1026) — then a green, non-red-tier local worktree is
    merged via ``git_merge_task``. Idempotent per branch-head sha: if a
    validated/rejected/red-tier/auto-merged note already exists for the
    current sha the verdict is ``unchanged`` — no gates run, no note append,
    no notification.
    """
    from . import run_records
    from .delivery import resolve_delivery

    completion = run_records.scoped_direct_completion(task_id)
    if completion is None:
        completion = run_records.capture_direct_completion(task_id)
    if completion is None:
        return _result(task_id, "unchanged", None)
    from .operating_profiles import task_run_snapshot

    # Review acts on work a run already handed off: its newest run's frozen
    # operating snapshot governs delivery (task #1756).
    policy = resolve_delivery(
        project, notes=notes, snapshot=task_run_snapshot(task_id, finished=True)
    )

    try:
        from .merge import find_task_worktree

        wt, _main_repo = find_task_worktree(task_id)
    except Exception:
        wt = None

    pr_url = _pr_url_from_notes(notes)

    if wt is None:
        # Race guard (task #329/#347): the caller's task row was read once,
        # up in review_task(), before this worktree lookup. If the task
        # completes concurrently — status flips to done and task_update
        # releases the worktree — that snapshot goes stale in the gap
        # between the two reads, and a task that finished cleanly gets
        # reported as a worktree-missing failure. Re-read status fresh,
        # right here, immediately before deciding: if the task is done now,
        # this is a closed-mid-scan race, not a real failure.
        if _task_status_now(task_id) == "done":
            return _result(task_id, "unchanged", None)

        # Task #1333 / #1297: notes or evidence_ref already point at a
        # merged PR — reconcile should have closed this, but when that step
        # aborted (NoneType .get) the review pass used to false-reject with
        # "no git worktree found". Check the PR before requiring a worktree.
        # An older merged-upstream stamp must not hide a newer live review
        # request (Devin #756: reopened task keeps the old merge note).
        if REVIEW_MERGED_UPSTREAM in notes and not _has_live_review_marker(notes):
            return _result(task_id, "unchanged", None)
        merged_fb = _merged_pr_fallback(
            task_id, title, notes, evidence_ref, project=project, task=task
        )
        if merged_fb is not None:
            # Mirror reconcile's claim-lease guard for dry-run and real reviews
            # (Devin #756): never report merged-upstream for a live claim.
            conn = storage.connect()
            try:
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                if row is not None and _active_reconcile_blocker(conn, row):
                    return _result(task_id, "unchanged", None)
            finally:
                conn.close()
            if not dry_run:
                if not _mark_done_with_note(task_id, merged_fb["note"], expected=completion):
                    return _result(task_id, "unchanged", None)
            return _result(
                task_id,
                merged_fb["verdict"],
                merged_fb["notification"],
            )

        reason = f"no git worktree found for task {task_id}"
        # UA task #736 fallback: desktop-host work — the worktree lives on the
        # dev machine, so the hub row's worktree_path/branch are NULL — but the
        # branch was pushed to origin and named in the notes. Before rejecting
        # (the #657-style 3x-rejection loop), check origin: a branch that is
        # really there gets HELD for the human merge decision, not failed.
        # This runs even after an earlier "no git worktree found" rejection:
        # a later open PR on evidence_ref must be able to recover the task.
        fallback = _origin_branch_fallback(
            task_id, notes, project=project, task=task, evidence_ref=evidence_ref
        )
        if fallback is not None:
            fb_note = fallback["note"]
            already_held = any(
                "no local worktree" in line and fallback["branch"] in line
                for line in notes.splitlines()
                if line.strip().startswith("[review:")
            )
            if not already_held and not dry_run:
                _append_note(task_id, fb_note)
            return _result(
                task_id,
                fallback["verdict"],
                fallback["notification"] if not already_held else None,
            )
        # Report once: reconcile may already have flagged the branch as stale.
        # Checked after the fallback so an earlier false rejection is repaired
        # once the branch can be found (task #2321).
        if reason in notes or REVIEW_STALE in notes:
            return _result(task_id, "unchanged", None)
        if not dry_run:
            _append_note(task_id, f"[review: rejected @ {_ts()}] {reason}")
        return _result(task_id, "failed", f"❌ Task #{task_id} ({title}) review FAILED: {reason}")

    wt_path = wt["path"]
    # Task #2278: a worktree matched only by name can sit outside the workspace
    # and every project's .worktrees/, so the gates refuse it. When reconcile or
    # an earlier pass already reported this task as having no worktree, that
    # refusal is the same condition: report it once, not as a new rejection.
    if REVIEW_STALE in notes or f"no git worktree found for task {task_id}" in notes:
        from .safety import resolve_task_worktree

        try:
            resolve_task_worktree(wt_path)
        except ToolError:
            return _result(task_id, "unchanged", None)
    branch = wt.get("branch", "")
    head_sha = _head_sha(wt_path) or wt.get("head") or "unknown"

    # Idempotence: one note line per (state, sha). A validated note whose policy
    # read never landed is not finished: the gates stay skipped, and the missing
    # row is written on a later scan.
    if _already_reviewed_at_sha(notes, head_sha):
        result = _result(task_id, "unchanged", None, head_sha=head_sha)
        if not dry_run and _validated_note_at_sha(notes, head_sha):
            ref = _pr_ref_for_review(notes, evidence_ref) or pr_url
            if ref:
                review_read = _record_policy_read(ref, head_sha, wt_path, only_if_missing=True)
                if review_read is not None:
                    result["review_read"] = review_read
        return result

    # Gates: clean tree + security audit + pytest — the same gate logic the
    # real merge path uses, shared via merge.run_validation_gates, but with no
    # status='done' precondition and no merge afterwards.
    #
    # Task #1338: when the candidate has a PR whose exact-head CI test
    # shards are already SUCCESS, skip local pytest (still run clean-tree +
    # audit + red-tier). Fall back to local pytest when there is no PR, CI
    # is pending/failed/skipped, heads differ, the worktree uses Bun tests,
    # or UA_REVIEW_TRUST_CI=0; since #1787 that local run is targeted (see
    # config.review_local_pytest_scope), never the whole suite by default. Consult CI *before* the local-timeout cap so
    # a later-green CI run can still validate a SHA that exhausted local
    # pytest retries.
    from .merge import TestTimeoutError, run_validation_gates

    timeout_count = _timeout_count_at_sha(notes, head_sha)
    max_retries = config.review_timeout_max_retries()

    skip_tests = False
    ci_detail = ""
    test_paths: list[str] | None = None
    blind_note = ""  # what the targeted selection could not follow (#2231)
    pytest_selected = _ci_pytest_skip_eligible(wt_path)
    pr_ref = _pr_ref_for_review(notes, evidence_ref) or pr_url
    if pr_ref and pytest_selected:
        ci_ok, ci_detail = _exact_head_ci_pytest_ok(pr_ref, head_sha, wt_path)
        if ci_ok:
            skip_tests = True

    # Task #1787: CI runs on merge batches (and PRs labelled ci:full), so most
    # PR heads have no exact-head shards. Never fall back to the whole suite
    # on the hub for a PR (#1338, #1786): run the test files this diff touches
    # or names, or hold the task as awaiting batch CI when there are none or
    # too many. A task with no PR has no CI to wait for and never reaches a
    # batch, so it keeps the old whole-suite run (the local-merge flow).
    if (
        not skip_tests
        and pytest_selected
        and pr_ref
        and config.review_local_pytest_scope() == "targeted"
    ):
        from . import affected_tests

        mapped, full_reasons, blind_spots = _targeted_test_plan(
            wt_path, _diff_for_targeted_tests(wt_path, branch)
        )
        blind_note = (
            " Not followed statically: " + "; ".join(blind_spots[:3]) + "." if blind_spots else ""
        )
        cap = config.review_targeted_max_files()
        if mapped and len(mapped) <= cap:
            test_paths = mapped
        else:
            if full_reasons:
                why = "the diff needs the full suite (" + "; ".join(full_reasons[:3]) + ")"
            elif not mapped:
                why = "no test files map to this diff"
            else:
                why = f"{len(mapped)} test files exceed the targeted cap of {cap}"
            if _awaiting_ci_at_sha(notes, head_sha):
                return _result(task_id, "unchanged", None, head_sha=head_sha)
            if not dry_run:
                _append_note(
                    task_id,
                    f"[review: awaiting-ci @ {_ts()}] at {head_sha}: {why}; "
                    f"exact-head CI: {ci_detail or 'not read'}. The hub does not run "
                    "the full suite: label the PR ci:full or verify it in a batch CI run. "
                    f"A targeted test selection is {affected_tests.SUBSET_NOTE}.{blind_note}",
                )
            return _result(
                task_id,
                "awaiting-ci",
                f"⏳ Task #{task_id} ({title}) awaiting batch CI at {head_sha[:12]}: {why}.",
                head_sha=head_sha,
            )

    if not skip_tests and timeout_count >= max_retries:
        # Local pytest already timed out max_retries times at this sha and CI
        # cannot substitute — hold silently until a new commit lands.
        return _result(task_id, "unchanged", None, head_sha=head_sha)

    try:
        if test_paths is None:
            run_validation_gates(task_id, wt_path, skip_tests=skip_tests)
        else:
            run_validation_gates(task_id, wt_path, skip_tests=skip_tests, test_paths=test_paths)
    except TestTimeoutError as e:
        # Task #1254 (pytest), extended to bun-test task #1295: the test
        # runner's own ceiling firing means the gates never finished — not
        # that the code under review is broken. The old blanket `except
        # Exception` below read this exactly like a red test and posted a
        # false "review FAILED" verdict (7 instances 09-20..09-22 on the Mac,
        # two of which later merged green once the suite got a clean run).
        # Verdict "timeout" keeps that distinct, for either runner.
        # Deliberately NOT a "[review: rejected @ ...] ... {head_sha}" note:
        # that phrase matches `_REVIEWED_AT_SHA_TEMPLATE` and would mark this
        # sha as already reviewed, silently blocking every retry at this
        # same commit — a timeout is transient and should be retried on the
        # next scan (up to `max_retries`), not treated as a terminal verdict.
        msg = str(e)[:200]
        if not dry_run:
            _append_note(
                task_id,
                f"[review: timeout @ {_ts()}] at {head_sha}: {msg} — will retry next scan",
            )
        # Only the FIRST timeout at this sha pages the operator's phone — a retry
        # that also times out appends the note (so the count/cap above stays
        # accurate) but stays silent, or every `*/15` retry would spam a
        # notification for a condition that hasn't changed.
        notification = None
        if timeout_count == 0:
            notification = (
                f"⏱️ Task #{task_id} ({title}) review INCONCLUSIVE at {head_sha[:12]}: "
                f"{msg}. Not a test failure — will retry."
            )
        return _result(task_id, "timeout", notification, head_sha=head_sha)
    except Exception as e:
        msg = str(e)[:200]
        if not dry_run:
            _append_note(
                task_id,
                f"[review: rejected @ {_ts()}] at {head_sha}: validation failed: {msg}",
            )
        return _result(
            task_id,
            "failed",
            f"❌ Task #{task_id} ({title}) review FAILED at {head_sha[:12]}: {msg}",
            head_sha=head_sha,
        )

    # Pin HEAD after gates: a concurrent commit must not inherit results from
    # an earlier SHA (local or CI-trusted) — Devin #1338.
    post_sha = _head_sha(wt_path) or wt.get("head") or "unknown"
    if post_sha != head_sha and not _sha_prefix_matches(post_sha, head_sha):
        if not dry_run:
            _append_note(
                task_id,
                f"[review: timeout @ {_ts()}] at {head_sha}: worktree HEAD moved to "
                f"{post_sha[:12]} during validation — will retry next scan",
            )
        return _result(
            task_id,
            "timeout",
            (
                f"⏱️ Task #{task_id} ({title}) review INCONCLUSIVE: HEAD moved during "
                f"validation ({head_sha[:12]} → {post_sha[:12]})."
            ),
            head_sha=post_sha,
        )
    head_sha = post_sha

    # Record CI substitution only after clean-tree/audit + HEAD pin succeed,
    # so a dirty-tree / audit fail does not leave a green ci-gate note.
    if skip_tests and not dry_run:
        from . import run_records

        if not run_records.append_scoped_direct_note(
            task_id, f"[review: ci-gate @ {_ts()}] {head_sha} — {ci_detail}"
        ):
            return _result(task_id, "unchanged", None, head_sha=head_sha)
        sync_state_to_vault()
    elif test_paths is not None and not dry_run:
        from . import affected_tests, run_records

        shown = ", ".join(test_paths[:6]) + (" …" if len(test_paths) > 6 else "")
        if not run_records.append_scoped_direct_note(
            task_id,
            f"[review: targeted-tests @ {_ts()}] {head_sha} — {len(test_paths)} test "
            f"file(s) passed ({shown}); this is {affected_tests.SUBSET_NOTE}.{blind_note}",
        ):
            return _result(task_id, "unchanged", None, head_sha=head_sha)

    # Red-tier classification of the pinned head, not a branch name that can
    # advance after the gates. An empty or unreadable diff is not a clean one.
    changed = _changed_files_vs_main(wt_path, head_sha)
    red_files = _is_red_tier(changed)
    if red_files or not changed:
        held = ", ".join(red_files) or "diff against main is empty or unreadable"
        if not dry_run:
            _append_note(
                task_id,
                f"[review: red-tier @ {_ts()}] at {head_sha} — "
                f"files requiring human approval: {held}",
            )
        return _result(
            task_id,
            "red-tier",
            (
                f"🔴 Task #{task_id} ({title}) passed gates but touches red-tier files: "
                f"{held}. Needs your approval to merge."
            ),
            red_tier_files=red_files,
            head_sha=head_sha,
        )

    # Gates green — record once. With +yolo, merge locally; otherwise hold.
    target = pr_url or branch or wt_path
    delivery_note = policy.marker()
    if policy.mode == "no-mistakes" and not pr_url:
        # Prefer a PR for no-mistakes, but still validate the local branch.
        target = f"{target} (no-mistakes: prefer PR)"

    if policy.yolo and not dry_run:
        from .tools.git import git_merge_task

        try:
            # allow_in_flight, not force: this task is still doing/blocked —
            # review runs before it is marked done — so the status guard has to
            # give, but the clean-worktree, audit and pytest gates must not.
            from .merge import reviewed_gate_scope

            # The merge re-runs the gates; reuse this review's test scope for
            # this head instead of a whole-suite run on the hub (#1787).
            with reviewed_gate_scope(
                task_id, head_sha, skip_tests=skip_tests, test_paths=test_paths
            ):
                merge_result = git_merge_task(
                    task_id=task_id, force=False, dry_run=False, allow_in_flight=True
                )
        except Exception as e:
            if _direct_completion_changed_cause(e):
                # A takeover or revision change is transient and cannot suppress
                # a later review at the same HEAD, or annotate a successor.
                return _result(task_id, "unchanged", None, head_sha=head_sha)
            timeout_cause = _test_timeout_cause(e)
            if timeout_cause is not None:
                # Devin review, PR #691: `git_merge_task` re-runs
                # `run_validation_gates` internally (see `orchestrate_merge`),
                # so a timeout here is a SECOND gate run timing out even
                # though the first one (above) already passed. It is still
                # "gates never finished", not a real merge failure, and must
                # not become a `yolo-merge-failed` note — that marker IS in
                # `_REVIEWED_AT_SHA_TEMPLATE` and is terminal, which would
                # have turned this transient timeout into a permanent
                # rejection at this sha (exactly the bug task #1254 fixes).
                # This whole block only runs when `not dry_run` (the outer
                # `if policy.yolo and not dry_run:` above), so the note
                # append here doesn't need its own guard.
                msg = str(timeout_cause)[:200]
                _append_note(
                    task_id,
                    f"[review: timeout @ {_ts()}] at {head_sha}: {msg} — will retry next scan",
                )
                notification = None
                if timeout_count == 0:
                    notification = (
                        f"⏱️ Task #{task_id} ({title}) review INCONCLUSIVE at {head_sha[:12]}: "
                        f"{msg}. Not a test failure — will retry."
                    )
                return _result(task_id, "timeout", notification, head_sha=head_sha)
            _append_note(
                task_id,
                f"[review: yolo-merge-failed @ {_ts()}] {delivery_note} "
                f"at {head_sha}: {str(e)[:200]}",
            )
            return _result(
                task_id,
                "failed",
                (
                    f"❌ Task #{task_id} ({title}) yolo merge failed at "
                    f"{head_sha[:12]}: {str(e)[:200]}"
                ),
                head_sha=head_sha,
            )
        _append_note(
            task_id,
            f"[review: auto-merged @ {_ts()}] {delivery_note} "
            f"yolo local merge at {head_sha} — {target}",
        )
        return _result(
            task_id,
            "auto-merged",
            (
                f"🚀 Task #{task_id} ({title}) auto-merged via {delivery_note} "
                f"at {head_sha[:12]}: {target}"
            ),
            merge_result=merge_result,
            head_sha=head_sha,
        )

    review_read = None
    if not dry_run:
        _append_note(
            task_id,
            f"[review: validated @ {_ts()}] {delivery_note} gates green at "
            f"{head_sha}; ready for human merge — {target}",
        )
        review_read = _record_policy_read(pr_ref, head_sha, wt_path)
    result = _result(
        task_id,
        "validated",
        f"✅ Task #{task_id} ({title}) validated — ready for your merge: {target}",
        head_sha=head_sha,
    )
    if review_read is not None:
        result["review_read"] = review_read
    return result


#: The identity the review pipeline records its own reads under; the same
#: actor its completions are attributed to.
REVIEW_PIPELINE_ACTOR = "review-cron"


def _pr_identity_at_head(pr_ref: str, head_sha: str, wt_path: str) -> tuple[str, int] | str:
    """``(owner/name, number)`` of ``pr_ref`` when its live head is ``head_sha``.

    Otherwise a skip reason: ``head-moved`` when the pull request's head is
    another commit (a read at a head it no longer has says nothing about what
    would merge), or ``head-lookup-failed`` when gh is missing, fails or
    answers something unreadable. A read recorded at a head GitHub does not
    know would make the release gate's comparison fail, so neither is written.
    """
    gh = shutil.which("gh")
    if not gh:
        return "head-lookup-failed"
    try:
        res = subprocess.run(
            [gh, "pr", "view", pr_ref, "--json", "url,headRefOid"],
            cwd=wt_path or None,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0:
            return "head-lookup-failed"
        payload = json.loads(res.stdout)
    except Exception:
        return "head-lookup-failed"
    if not isinstance(payload, dict):
        return "head-lookup-failed"
    if payload.get("headRefOid") != head_sha:
        return "head-moved"
    m = _PR_URL_PARTS_RE.fullmatch(str(payload.get("url") or ""))
    return (m.group(1), int(m.group(2))) if m else "head-lookup-failed"


_PR_URL_PARTS_RE = re.compile(r"https://github\.com/([\w.\-]+/[\w.\-]+)/pull/(\d+)")


def _record_policy_read(
    pr_ref: str | None, head_sha: str, wt_path: str, *, only_if_missing: bool = False
) -> dict | None:
    """Record the validated verdict as a ``policy`` read (task #2294).

    Runs after the validated note is written and never changes the verdict: a
    failed write is logged and returned for the caller to surface. Returns
    ``None`` when the task names no pull request, and a ``skipped`` reason when
    its live head could not be confirmed as this head. ``only_if_missing`` is
    the next scan at the same SHA: the gates are not re-run, a row already
    stored is left untouched, and a write that failed last time is tried again.
    """
    if not pr_ref:
        return None
    identity = _pr_identity_at_head(pr_ref, head_sha, wt_path)
    if isinstance(identity, str):
        if only_if_missing:
            return None
        return {"recorded": None, "error": None, "skipped": identity}
    from . import review_reads

    repo, number = identity
    if only_if_missing and any(
        row["kind"] == review_reads.POLICY for row in review_reads.for_head(repo, number, head_sha)
    ):
        return None
    return review_reads.record_best_effort(
        repo=repo,
        number=number,
        head_sha=head_sha,
        kind=review_reads.POLICY,
        reader=REVIEW_PIPELINE_ACTOR,
        verdict="validated",
    )


def _direct_completion_changed_cause(exc: BaseException) -> bool:
    from .run_records import DirectCompletionChanged

    seen = set()
    pending = [exc]
    while pending and len(seen) < 100:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, DirectCompletionChanged):
            return True
        pending.extend(cause for cause in (current.__cause__, current.__context__) if cause)
    return False


def _test_timeout_cause(exc: BaseException) -> Exception | None:
    """Recover a `merge.TestTimeoutError` (pytest or bun-test) hidden behind
    one or more generic wraps, e.g. `git_merge_task`'s `ToolError(str(e))
    from e` (task #1254).

    `git_merge_task` (`tools/git.py`) reraises every `orchestrate_merge`
    failure — including a test-runner timeout — as a bare `ToolError`, which
    loses the exception type the caller would otherwise branch on. The
    original exception survives on `__cause__` because that wrap uses `from
    e`, so unwrap it there instead of string-matching the message.

    Walks the full visible `__cause__`/`__context__` chain rather than a
    single `__cause__` hop (task #1295 follow-up): unwrapping exactly one level
    happened to be enough for today's single `ToolError(...) from e` wrap,
    but was silently wrong the moment a second wrap was added anywhere
    between the test runner and this call site — that would have turned a
    timeout back into a false "failed"/"rejected" verdict with no test
    covering it. Bounded and cycle-safe: `__context__` chains can cycle (an
    exception raised while handling itself re-points `__context__` back at
    an ancestor), so this caps the walk and tracks visited exception ids
    rather than trusting the chain to terminate on its own.
    """
    from .merge import TestTimeoutError

    max_depth = 20
    max_nodes = 100
    seen: set[int] = set()
    pending: list[tuple[BaseException, int]] = [(exc, 0)]
    while pending and len(seen) < max_nodes:
        current, depth = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, TestTimeoutError):
            return current
        if depth >= max_depth:
            continue
        # Follow Python's displayed exception chain. An explicit cause
        # replaces context; "raise ... from None" suppresses context.
        if current.__cause__ is not None:
            pending.append((current.__cause__, depth + 1))
        elif not current.__suppress_context__ and current.__context__ is not None:
            pending.append((current.__context__, depth + 1))
    return None


def _merge_done_task(task_id: int, title: str, notes: str, dry_run: bool) -> dict:
    """Original local-merge flow, kept for tasks already marked ``done``
    (non-PR worktree tasks merged into the local workspace main)."""
    from .merge import find_task_worktree
    from .tools.git import git_merge_task

    # Best-effort sha resolution so a timeout note here can be capped per
    # (task, sha) the same way `_validate_and_hold`'s is (task #1254
    # follow-up). `git_merge_task` resolves the worktree internally too; this
    # separate, cheaper lookup only feeds the retry-count check below and is
    # never used to skip or alter the merge itself. If rev-parse fails, use
    # the SHA reported by worktree discovery. Only when neither provides a
    # SHA do retries fall back to a task-level total-note cap (task #1295
    # follow-up), which cannot reset on a new commit.
    head_sha: str | None = None
    try:
        wt, _main_repo = find_task_worktree(task_id)
        if wt is not None:
            head_sha = _head_sha(wt["path"]) or wt.get("head")
    except Exception:
        head_sha = None

    timeout_count = (
        _timeout_count_at_sha(notes, head_sha) if head_sha else _timeout_count_total(notes)
    )
    max_retries = config.review_timeout_max_retries()
    if timeout_count >= max_retries:
        # Same bound as `_validate_and_hold`: hold silently until a new
        # commit lands (or, without a resolvable sha, until something else
        # clears the notes) rather than re-running a (possibly 1800s) pytest
        # and paging the operator on every `*/15` cron tick forever.
        return _result(task_id, "unchanged", None, head_sha=head_sha)

    # 1. Dry-run merge to validate audit + tests
    try:
        dry_result = git_merge_task(task_id=task_id, force=False, dry_run=True)
    except Exception as e:
        if _direct_completion_changed_cause(e):
            return _result(task_id, "unchanged", None, head_sha=head_sha)
        timeout_cause = _test_timeout_cause(e)
        if timeout_cause is not None:
            # Same rationale as `_validate_and_hold`: a timeout means the
            # gates never finished, not that the code failed — don't post it
            # as "rejected"/"failed" or a retry gets blocked at this sha.
            #
            # Devin review, PR #691, two fixes vs. the in-flight path above:
            # (1) `--dry-run` promises no task mutation (review_cli.py:
            # "write no notes, change no task state, and never merge") —
            # `_validate_and_hold` already guards its note append with `not
            # dry_run`; this one didn't.
            # (2) This flow only runs for status='done' tasks, which
            # `scan_for_reviewable_tasks` deliberately excludes
            # (`status IN ('open', 'doing', 'waiting_review', 'blocked')`) —
            # unlike the in-flight path, there is no `*/15` cron scan that
            # will ever pick this task back up, so "will retry next scan"
            # would be a false promise. It retries only if something
            # explicitly reruns `unified-agent-review task <id>` on it.
            msg = str(timeout_cause)[:200]
            sha_suffix = f" at {head_sha}:" if head_sha else ""
            if not dry_run:
                _append_note(
                    task_id,
                    f"[review: timeout @ {_ts()}]{sha_suffix} {msg} — "
                    "retry with `unified-agent-review task <id>` (not covered by the periodic scan)",
                )
            notification = None
            if timeout_count == 0:
                notification = (
                    f"⏱️ Task #{task_id} ({title}) review INCONCLUSIVE: {msg}. "
                    "Not a test failure — rerun to retry (status=done tasks aren't auto-rescanned)."
                )
            return _result(task_id, "timeout", notification, head_sha=head_sha)
        # Validation failed — annotate and report
        error_msg = str(e)
        _append_note(
            task_id,
            f"[review: rejected @ {_ts()}] validation failed: {error_msg[:200]}",
        )
        return _result(
            task_id,
            "failed",
            f"❌ Task #{task_id} ({title}) review FAILED: {error_msg[:200]}",
        )

    # 2. Check for red-tier files in the branch's diff against main. A diff
    # that could not be classified is held like a red-tier one, never merged.
    red_files, unclassified = _red_tier_for_task_checked(task_id)

    # 3. Red-tier gate: require human approval
    if red_files or unclassified:
        held = ", ".join(red_files) or unclassified
        _append_note(
            task_id,
            f"[review: red-tier @ {_ts()}] files requiring human approval: {held}",
        )
        return _result(
            task_id,
            "red-tier",
            (
                f"🔴 Task #{task_id} ({title}) passed tests but touches red-tier files: "
                f"{held}. Needs your approval to merge."
            ),
            red_tier_files=red_files,
        )

    # 4. All green, not red-tier — merge (or dry-run)
    if dry_run:
        return _result(
            task_id,
            "dry-run-ok",
            f"✅ Task #{task_id} ({title}) dry-run passed. Ready to merge.",
            merge_result=dry_result,
        )

    try:
        merge_result = git_merge_task(task_id=task_id, force=False, dry_run=False)
    except Exception as e:
        if _direct_completion_changed_cause(e):
            return _result(task_id, "unchanged", None, head_sha=head_sha)
        timeout_cause = _test_timeout_cause(e)
        if timeout_cause is not None:
            # This branch only runs when `not dry_run` (the `if dry_run:
            # return "dry-run-ok"` above), so the note append needs no guard
            # here — see the dry-run-step timeout branch above for why the
            # wording differs from `_validate_and_hold`'s.
            msg = str(timeout_cause)[:200]
            sha_suffix = f" at {head_sha}:" if head_sha else ""
            _append_note(
                task_id,
                f"[review: timeout @ {_ts()}]{sha_suffix} {msg} — "
                "retry with `unified-agent-review task <id>` (not covered by the periodic scan)",
            )
            notification = None
            if timeout_count == 0:
                notification = (
                    f"⏱️ Task #{task_id} ({title}) review INCONCLUSIVE: {msg}. "
                    "Not a test failure — rerun to retry (status=done tasks aren't auto-rescanned)."
                )
            return _result(task_id, "timeout", notification, head_sha=head_sha)
        _append_note(
            task_id,
            f"[review: merge-failed @ {_ts()}] {str(e)[:200]}",
        )
        return _result(
            task_id,
            "failed",
            f"❌ Task #{task_id} ({title}) merge failed: {str(e)[:200]}",
        )

    _append_note(
        task_id,
        f"[review: approved @ {_ts()}] merged by automated review. "
        f"commit: {merge_result.get('commit_hash', 'unknown')}",
    )

    return _result(
        task_id,
        "merged",
        (
            f"✅ Task #{task_id} ({title}) merged successfully. "
            f"Commit: {merge_result.get('commit_hash', 'unknown')}."
        ),
        merge_result=merge_result,
    )


def review_and_notify(task_id: int, dry_run: bool = False) -> dict:
    """Review a task and send an iMessage notification with the result.

    This is the top-level entry point for the review pipeline.
    It combines ``review_task`` with ``notify`` to deliver the verdict
    to the operator's phone. ``unchanged`` verdicts (already reviewed at the
    current branch-head sha) produce no notification.

    Returns the review result dict with an added ``notified`` field.
    """
    result = review_task(task_id, dry_run=dry_run)

    # Send notification (skipped when there is nothing new to report)
    notified = False
    if result.get("notification"):
        try:
            from .tools.notify import internal_route, notify

            with internal_route("review_pipeline"):
                notify_result = notify(result["notification"])
            notified = notify_result.get("delivered", False)
        except Exception as e:
            result["notify_error"] = str(e)[:200]

    result["notified"] = notified
    return result
