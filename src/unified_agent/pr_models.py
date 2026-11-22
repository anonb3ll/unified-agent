"""Pull-request snapshot and event types shared by the PR watcher and the GitHub App.

``pr_watch`` builds them from ``gh`` output and ``github_app`` normalizes them
into connector events. Keeping them here lets the GitHub integration use them
without importing the watcher that sits above it.
"""

from __future__ import annotations

from dataclasses import dataclass

# Snapshot fields the diff compares. Anything else (title edits, label churn,
# updatedAt-only churn from CI re-runs) is deliberately NOT an event — only
# state transitions, new review activity, and a moved head are meaningful.
_DIFF_FIELDS = (
    "state",
    "head_sha",
    "last_review_at",
    "last_review_state",
    "merged_at",
    "closed_at",
    "draft",
    "author_login",
    "labels",
    "base_branch",
    "head_branch",
)


@dataclass
class PRSnapshot:
    """One PR's state, reduced to the fields the event diff needs."""

    number: int
    title: str
    url: str
    state: str
    head_sha: str = ""
    merged_at: str | None = None
    closed_at: str | None = None
    last_review_at: str | None = None
    last_review_state: str | None = None
    last_review_body: str = ""
    body: str = ""
    draft: bool = False
    author_login: str = ""
    labels: tuple[str, ...] = ()
    base_branch: str = ""
    head_branch: str = ""
    # The commit the PR landed as on the base branch (task #1848). Deliberately
    # not a diff field: learning a merge SHA later is not a PR event. It rides
    # in the cursor so the queue-closure reader can cite it without a network
    # call; empty until the PR is merged and the poller has seen the field.
    merge_sha: str = ""

    def diff_key(self) -> tuple:
        return tuple(getattr(self, f) for f in _DIFF_FIELDS)

    def to_cursor(self) -> dict:
        cursor = {f: getattr(self, f) for f in _DIFF_FIELDS}
        if self.merge_sha:
            cursor["merge_sha"] = self.merge_sha
        return cursor


@dataclass
class PREvent:
    """One meaningful PR event to surface into memory + journal."""

    repo: str
    kind: str  # opened | synchronize | ready_for_review | reviewed | merged | closed
    snapshot: PRSnapshot
    rationale: str = ""
    rationale_author: str = ""


#: The hub-published check that says whether a pull request's head was read
#: (task #2320, ``review_head_status``). A verdict about review, not about CI.
REVIEW_HEAD_CHECK_NAME = "unified-agent/review-head"


def rollup_check_name(check: dict, index: int = 0) -> str:
    """One stable "<workflow> / <job>" name for a ``statusCheckRollup`` entry.

    The two CI providers spell the same check differently in
    ``gh pr view --json statusCheckRollup`` (task #1787, verified against PR
    heads on both): Depot CI's app reports ``name="CI / test (shard=0)"`` with
    no ``workflowName``, while GitHub Actions reports ``name="test (shard=0)"``
    with ``workflowName="CI"``. Prefixing the workflow when the name lacks it
    makes both read ``CI / test (shard=0)``, and keeps same-named jobs in
    different workflows (``Docker / build``) apart. Commit-status contexts
    (Devin Review) carry ``context`` and pass through unchanged.
    """
    name = str(check.get("name") or check.get("context") or f"check-{index}")
    workflow = str(check.get("workflowName") or "").strip()
    if workflow and not name.startswith(f"{workflow} / "):
        return f"{workflow} / {name}"
    return name


def prefer_check_state(existing: str | None, new: str) -> str:
    """Pick the state to keep when two rollup entries share one check name.

    A PR event that does not start CI (task #1787: any label other than
    ``ci:full``) still creates a run whose jobs are all skipped, with the same
    check names as the real run. Keeping the last entry let that SKIPPED mask
    an earlier FAILURE, and skipped reads as green. A skipped entry therefore
    never replaces a non-skipped one; otherwise the later entry wins, so a
    re-run that passed still supersedes its failure.
    """
    if existing is not None and new.upper() == "SKIPPED" and existing.upper() != "SKIPPED":
        return existing
    return new
