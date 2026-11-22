"""Review of a task that has no local worktree on the reviewing host.

A claim the hub could not provision (``provisioning: native-required``) leaves
the task row's ``branch`` and ``worktree_path`` empty while the agent builds in
its own checkout and opens a PR (task #2321). These tests cover how the review
finds that branch on origin, which PR heads it trusts, and that the resulting
hold is never mistaken for a review that ran the gates.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from unified_agent import review_reads, reviewer, storage
from unified_agent.tools.tasks import task_add, task_update

SHA = "abc123def4567890abc123def4567890abc123de"
_PRODUCT_SLUG = "acme/product"
_PR_URL = f"https://github.com/{_PRODUCT_SLUG}/pull/77"
_OPEN_PR = {
    "state": "OPEN",
    "headRefName": "task/2321-native",
    "headRefOid": SHA,
    "isCrossRepository": False,
    "url": _PR_URL,
}


def _get_task(task_id: int):
    with storage.connect() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def _native_review_task(*, branch=None, evidence_ref=None, notes_extra="") -> int:
    """A waiting-review task claimed natively: no worktree, notes name no branch."""
    notes = f"{reviewer.WAITING_FOR_REVIEW}\nprovisioning: native-required\n{notes_extra}"
    tid = task_add("native-checkout", notes=notes.strip())["id"]
    task_update(tid, status="doing", actor="test-agent")
    with storage.connect() as conn:
        conn.execute(
            "UPDATE tasks SET branch=?, evidence_ref=? WHERE id=?", (branch, evidence_ref, tid)
        )
    return tid


@pytest.fixture(autouse=True)
def no_real_merge_or_pr_lookup(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("real merge path (orchestrate_merge) was invoked")

    monkeypatch.setattr("unified_agent.merge.orchestrate_merge", boom)
    monkeypatch.setattr(reviewer, "_pr_identity_at_head", lambda *a: ("acme/product", 77))


@pytest.fixture
def native_checkout(monkeypatch, workspace_is_product_repo):
    """No local worktree, no merged PR, the product repo's origin is acme/product."""
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "OPEN")

    def git(args, repo):
        assert args == ["remote", "get-url", "origin"]
        return SimpleNamespace(returncode=0, stdout="git@github.com:Acme/Product.git\n")

    monkeypatch.setattr("unified_agent.merge._run_git_in_repo", git)
    seen: list[str] = []

    def on_origin(branch, repo):
        seen.append(branch)
        return branch.startswith("task/2321-")

    monkeypatch.setattr(reviewer, "_branch_exists_on_origin", on_origin)
    return seen


def test_branch_on_task_row_holds(monkeypatch, native_checkout):
    """The row's branch column is a source when the notes name none."""
    monkeypatch.setattr(
        reviewer, "_open_pr_head", lambda ref, repo: pytest.fail("gh asked needlessly")
    )
    tid = _native_review_task(branch="task/2321-native")

    res = reviewer.review_task(tid)

    assert res["verdict"] == "validated"
    notes = _get_task(tid)["notes"]
    assert "[review: rejected @" not in notes
    assert "task/2321-native (task row) found on origin (ls-remote)" in notes


def test_open_same_repo_pr_holds_once(monkeypatch, native_checkout):
    """#2319 / PR #1379: the open PR in evidence_ref is the only record of the
    branch. It resolves the branch, the task is held, a second scan appends
    nothing, and no policy read is recorded for a review that ran no gates."""
    asked: list[str] = []
    monkeypatch.setattr(
        reviewer, "_open_pr_head", lambda ref, repo: asked.append(ref) or dict(_OPEN_PR)
    )
    tid = _native_review_task(evidence_ref=_PR_URL)

    res1 = reviewer.review_task(tid)
    notes_after_first = _get_task(tid)["notes"]
    res2 = reviewer.review_task(tid)

    assert res1["verdict"] == "validated"
    assert asked[0] == _PR_URL
    assert native_checkout[-1] == "task/2321-native"
    assert "[review: held-no-checkout @" in notes_after_first
    assert f"(open PR {_PR_URL}) found on origin" in notes_after_first
    assert "[review: validated @" not in notes_after_first
    assert (res2["verdict"], res2["notification"]) == ("validated", None)
    assert _get_task(tid)["notes"] == notes_after_first
    assert review_reads.for_pr("acme/product", 77) == []


@pytest.mark.parametrize("branch", ["fix/feature+api", "fix/feature@api"])
def test_open_pr_accepts_plus_and_at_branches(monkeypatch, native_checkout, branch):
    """Git accepts ``+`` and ``@`` in a branch. The PR head must still hold."""

    def on_origin(name, repo):
        native_checkout.append(name)
        return name == branch

    monkeypatch.setattr(reviewer, "_branch_exists_on_origin", on_origin)
    monkeypatch.setattr(
        reviewer,
        "_open_pr_head",
        lambda ref, repo: {**_OPEN_PR, "headRefName": branch},
    )
    tid = _native_review_task(evidence_ref=_PR_URL)

    res = reviewer.review_task(tid)

    assert res["verdict"] == "validated", res
    assert branch in (res["notification"] or "")
    assert f"{branch} (open PR {_PR_URL}) found on origin" in _get_task(tid)["notes"]


def test_prior_worktree_rejection_recovers_when_an_open_pr_arrives(monkeypatch, native_checkout):
    """A task already rejected for a missing worktree recovers when evidence_ref
    later names an open same-repo PR."""
    monkeypatch.setattr(reviewer, "_open_pr_head", lambda ref, repo: None)
    tid = _native_review_task()

    first = reviewer.review_task(tid)

    assert first["verdict"] == "failed"
    assert f"no git worktree found for task {tid}" in _get_task(tid)["notes"]
    task_update(tid, evidence_ref=_PR_URL, actor="test-agent")
    monkeypatch.setattr(reviewer, "_open_pr_head", lambda ref, repo: dict(_OPEN_PR))

    second = reviewer.review_task(tid)

    assert second["verdict"] == "validated", second
    notes = _get_task(tid)["notes"]
    assert "task/2321-native (open PR" in notes
    assert "[review: rejected @" in notes


def test_earlier_false_rejection_is_repaired_once(monkeypatch, native_checkout):
    """A task that already carries the false no-worktree rejection still gets
    the fallback: it is held, and the hold note is written only once."""
    monkeypatch.setattr(reviewer, "_open_pr_head", lambda ref, repo: dict(_OPEN_PR))
    tid = _native_review_task(evidence_ref=_PR_URL)
    reviewer._append_note(tid, f"[review: rejected @ t0] no git worktree found for task {tid}")

    res1 = reviewer.review_task(tid)
    res2 = reviewer.review_task(tid)

    assert res1["verdict"] == "validated"
    assert res2["notification"] is None
    assert _get_task(tid)["notes"].count("[review: held-no-checkout @") == 1


@pytest.mark.parametrize(
    "gh_view",
    [
        {**_OPEN_PR, "state": "CLOSED"},
        {**_OPEN_PR, "isCrossRepository": True},
        {**_OPEN_PR, "url": "https://github.com/other/fork/pull/77"},
        {**_OPEN_PR, "url": "https://othergithub.com/acme/product/pull/77"},
        {**_OPEN_PR, "headRefName": "--upload-pack=x"},
        None,
    ],
    ids=["closed-unmerged", "fork", "other-repo", "lookalike-host", "option-branch", "gh-error"],
)
def test_untrusted_pr_head_still_rejects(monkeypatch, native_checkout, gh_view):
    """Only an OPEN, same-repo PR on the routed repo is trusted; anything
    else, including a gh failure, fails closed to the rejection."""
    monkeypatch.setattr(reviewer, "_open_pr_head", lambda ref, repo: gh_view)
    tid = _native_review_task(evidence_ref=_PR_URL)

    res = reviewer.review_task(tid)

    assert res["verdict"] == "failed"
    assert f"no git worktree found for task {tid}" in res["notification"]
    assert "[review: held-no-checkout @" not in _get_task(tid)["notes"]
    assert native_checkout == []


def test_hold_on_a_sha_named_branch_never_stands_in_for_gates(monkeypatch, native_checkout):
    """GPT review of PR #1383 (P1): a branch named after the full head SHA put
    that SHA in the hold note. Once a worktree appeared, the note read as a
    validation at that head, the gates were skipped and a policy read was
    recorded. The hold must not count: the gates run, and the policy read is
    written only after they pass."""
    sha_branch = f"task/2321-{SHA}"
    monkeypatch.setattr(
        reviewer, "_open_pr_head", lambda ref, repo: {**_OPEN_PR, "headRefName": sha_branch}
    )
    tid = _native_review_task(evidence_ref=_PR_URL)
    assert reviewer.review_task(tid)["verdict"] == "validated"
    # A pre-#2321 hold wrote the same text under the validated marker.
    reviewer._append_note(
        tid, f"[review: validated @ t1] no local worktree on this host; branch {sha_branch} x"
    )
    assert sha_branch in _get_task(tid)["notes"]

    wt = {"path": "/wt/task-2321", "branch": sha_branch, "head": SHA}
    gates: list[list] = []

    def fake_gates(task_id, wt_path, force=False, *, skip_tests=False, test_paths=None):
        gates.append(review_reads.for_pr("acme/product", 77))
        return {"success": True, "audit_status": "pass", "tests_passed": True}

    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/main"))
    monkeypatch.setattr("unified_agent.merge.run_validation_gates", fake_gates)
    monkeypatch.setattr("unified_agent.safety.resolve_task_worktree", lambda path: path)
    monkeypatch.setattr(reviewer, "_head_sha", lambda wt_path: SHA)
    monkeypatch.setattr(reviewer, "_exact_head_ci_pytest_ok", lambda *a: (False, "stub"))
    monkeypatch.setattr(reviewer, "_changed_files_vs_main", lambda *a: [{"file": "src/f.py"}])
    monkeypatch.setenv("UA_REVIEW_LOCAL_PYTEST", "full")

    res = reviewer.review_task(tid)

    assert res["verdict"] == "validated"
    assert gates == [[]]  # the gates ran, and no policy read existed before they passed
    [row] = review_reads.for_pr("acme/product", 77)
    assert (row["head_sha"], row["kind"]) == (SHA, review_reads.POLICY)
    assert res["review_read"] == {"recorded": row["id"], "error": None}
    assert f"gates green at {SHA}" in _get_task(tid)["notes"]


@pytest.mark.parametrize(
    ("url", "identity"),
    [
        ("https://github.com/Acme/Product.git", "acme/product"),
        ("https://token@github.com/acme/product/", "acme/product"),
        ("git@github.com:acme/product.git", "acme/product"),
        ("ssh://git@github.com/acme/product.git", "acme/product"),
        ("https://github.com:443/acme/product", "acme/product"),
        ("git@othergithub.com:acme/product.git", None),
        ("https://github.com.evil.example/acme/product", None),
        ("https://github.com@evil.example/acme/product", None),
        ("https://github.com:8443/acme/product", None),
        ("https://github.com/acme/product/extra", None),
        ("/srv/git/cockpit.git", None),
    ],
)
def test_github_remote_identity(url, identity):
    assert reviewer._github_remote_identity(url) == identity
