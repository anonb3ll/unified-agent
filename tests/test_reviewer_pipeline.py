"""Behavioral tests for the validate+hold review contract (reviewer.py).

Covers the contract decided for task 78:

- a task with status doing/blocked + [waiting-for: review] IS reviewable and
  gets a 'validated' verdict when the gates are green (never a merge);
- per-(state, sha) idempotence: a second run at the same branch-head sha is
  'unchanged' — no gate re-run, no note append, no notification;
- red-tier classification still holds work for human approval;
- reconcile: branches already merged into origin/main (ancestry or gh PR
  state, for squash merges) flip the task to done exactly once; missing
  branches with no PR info get one stale note;
- the status='done' local-merge flow keeps working;
- no source file in the pipeline invokes a remote write (push / PR merge).

Git, gh, and the gate runner are monkeypatched at their module seams
(unified_agent.merge.find_task_worktree / run_validation_gates and the
reviewer._* helpers), so nothing touches a real repository or the network.
"""

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from unified_agent import config, review_reads, reviewer, storage
from unified_agent.tools.tasks import task_add, task_update

SHA = "abc123def4567890abc123def4567890abc123de"
SHA2 = "f" * 40
_DEFAULT_REVIEW_REPO = object()


def _get_task(task_id: int):
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()


def _make_review_task(status="doing", notes_extra="", title="validate-me") -> int:
    notes = (
        f"{reviewer.WAITING_FOR_REVIEW}\n{notes_extra}".strip()
        if notes_extra
        else reviewer.WAITING_FOR_REVIEW
    )
    if status == "done":
        notes += "\n[review: approved] pre-authorized fixture outcome"
    t = task_add(title, notes=notes)
    task_update(t["id"], status=status, actor="test-agent")
    return t["id"]


def _insert_delegation(task_id: int, status: str = "in_flight") -> int:
    conn = storage.connect()
    try:
        now = storage.now()
        cur = conn.execute(
            "INSERT INTO delegations "
            "(from_agent, to_agent, task_id, task_spec, callback, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "Desktop_Agent5",
                "Desktop_Agent2",
                task_id,
                "delegated test task",
                "none",
                status,
                now,
                now,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


class _GitResult:
    """Minimal stand-in for subprocess.CompletedProcess at the git seam."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_git_result(returncode: int, stdout: str = "", stderr: str = "") -> _GitResult:
    return _GitResult(returncode, stdout, stderr)


@pytest.fixture(autouse=True)
def forbid_real_merge(monkeypatch):
    """The validate+hold path must never reach the real merge orchestration."""

    def boom(*args, **kwargs):
        raise AssertionError("real merge path (orchestrate_merge) was invoked")

    monkeypatch.setattr("unified_agent.merge.orchestrate_merge", boom)


@pytest.fixture(autouse=True)
def whole_suite_local_pytest(monkeypatch):
    """Gate-behaviour tests below predate #1787 and use made-up worktree paths.

    They exercise failure, timeout and red-tier handling of the local test run,
    so they pin the old whole-suite scope. The #1787 tests remove this override
    (``monkeypatch.delenv``) to exercise the targeted default.
    """
    monkeypatch.setenv("UA_REVIEW_LOCAL_PYTEST", "full")


@pytest.fixture
def green_worktree(monkeypatch, tmp_path):
    """Mock the git/gate seams: worktree found, stable sha, gates green, no red files.

    The worktree holds ``tests/test_foo.py`` so the changed ``src/foo.py``
    maps to a targeted test file (task #1787).
    """
    calls = {"gates": 0}
    wt_dir = tmp_path / "wt-x"
    calls["path"] = str(wt_dir)
    (wt_dir / "tests").mkdir(parents=True)
    (wt_dir / "tests" / "test_foo.py").write_text("def test_ok():\n    pass\n")
    wt = {"path": str(wt_dir), "branch": "fix/task-x", "head": SHA}

    def fake_gates(task_id, wt_path, force=False, *, skip_tests=False, test_paths=None):
        calls["gates"] += 1
        calls["skip_tests"] = skip_tests
        calls["test_paths"] = test_paths
        return {
            "success": True,
            "audit_status": "pass",
            "tests_passed": True,
            "tests_skipped": skip_tests,
        }

    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr("unified_agent.merge.run_validation_gates", fake_gates)
    monkeypatch.setattr(reviewer, "_head_sha", lambda wt_path: SHA)
    monkeypatch.setattr(
        reviewer,
        "_changed_files_vs_main",
        lambda wt_path, branch: [{"file": "src/foo.py", "status": "M"}],
    )
    monkeypatch.setattr(
        reviewer,
        "_diff_for_targeted_tests",
        lambda wt_path, branch: [{"file": "src/foo.py", "status": "M"}],
    )
    return calls


@pytest.fixture(autouse=True)
def no_live_pr_lookup(monkeypatch):
    """The policy-read writer asks GitHub which PR head it validated; never here."""
    monkeypatch.setattr(
        reviewer, "_pr_identity_at_head", lambda pr_ref, head_sha, wt_path: "head-moved"
    )


# ── policy-read writer: a validated verdict is a read at that head (#2294) ──


def test_a_validated_verdict_records_a_policy_read_at_the_pr_head(green_worktree, monkeypatch):
    seen = []
    monkeypatch.setattr(
        reviewer,
        "_pr_identity_at_head",
        lambda pr_ref, head_sha, wt_path: seen.append((pr_ref, head_sha)) or ("x/y", 7),
    )
    tid = _make_review_task(notes_extra="PR: https://github.com/x/y/pull/7")
    assert reviewer.review_task(tid, dry_run=True)["verdict"] == "validated"
    assert review_reads.for_pr("x/y", 7) == []  # a dry run writes nothing

    res = reviewer.review_task(tid)
    assert res["verdict"] == "validated"
    assert seen == [("https://github.com/x/y/pull/7", SHA)]
    [row] = review_reads.for_pr("x/y", 7)
    assert (row["head_sha"], row["kind"]) == (SHA, review_reads.POLICY)
    assert (row["reader"], row["verdict"]) == ("review-cron", "validated")
    assert res["review_read"] == {"recorded": row["id"], "error": None}


def test_a_failed_policy_read_write_keeps_the_verdict_and_says_so(green_worktree, monkeypatch):
    def boom(**_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reviewer, "_pr_identity_at_head", lambda *a: ("x/y", 7))
    monkeypatch.setattr(review_reads, "record", boom)
    tid = _make_review_task(notes_extra="PR: https://github.com/x/y/pull/7")
    res = reviewer.review_task(tid)
    assert res["verdict"] == "validated"
    assert "[review: validated @" in _get_task(tid)["notes"]
    assert res["review_read"]["recorded"] is None
    assert "database is locked" in res["review_read"]["error"]


def test_a_missed_policy_read_is_recorded_on_the_next_scan(green_worktree, monkeypatch):
    calls = {"n": 0}
    original = review_reads.record

    def fail_once(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original(**kwargs)

    monkeypatch.setattr(reviewer, "_pr_identity_at_head", lambda *a: ("x/y", 7))
    monkeypatch.setattr(review_reads, "record", fail_once)
    tid = _make_review_task(notes_extra="PR: https://github.com/x/y/pull/7")
    first = reviewer.review_task(tid)
    assert first["verdict"] == "validated"
    assert first["review_read"]["recorded"] is None
    assert review_reads.for_pr("x/y", 7) == []

    second = reviewer.review_task(tid)
    assert second["verdict"] == "unchanged"
    [row] = review_reads.for_pr("x/y", 7)
    assert (row["head_sha"], row["kind"], row["reader"]) == (
        SHA,
        review_reads.POLICY,
        "review-cron",
    )
    assert second["review_read"] == {"recorded": row["id"], "error": None}
    created_at = row["created_at"]

    third = reviewer.review_task(tid)
    assert third["verdict"] == "unchanged"
    assert "review_read" not in third
    [again] = review_reads.for_pr("x/y", 7)
    assert again["created_at"] == created_at


def test_a_skipped_policy_read_says_why(green_worktree, monkeypatch):
    # The autouse stub answers "the PR head is elsewhere"; no branch-only row,
    # and a failed lookup is reported as such, not as a moved head.
    tid = _make_review_task(notes_extra="PR: https://github.com/x/y/pull/7")
    skipped = {"recorded": None, "error": None, "skipped": "head-moved"}
    assert reviewer.review_task(tid)["review_read"] == skipped
    assert "review_read" not in reviewer.review_task(_make_review_task(title="no-pr"))
    monkeypatch.setattr(reviewer, "_pr_identity_at_head", lambda *a: "head-lookup-failed")
    tid = _make_review_task(title="lookup", notes_extra="PR: https://github.com/x/y/pull/7")
    assert reviewer.review_task(tid)["review_read"]["skipped"] == "head-lookup-failed"
    assert review_reads.for_pr("x/y", 7) == []


# ── contract: doing/blocked + annotation → validated, held, never merged ──


def test_doing_task_with_annotation_validates_not_fails(green_worktree):
    tid = _make_review_task(notes_extra="PR: https://github.com/x/y/pull/7")
    res = reviewer.review_task(tid)

    assert res["verdict"] == "validated"
    assert res["head_sha"] == SHA
    assert "ready for your merge" in res["notification"]
    assert "https://github.com/x/y/pull/7" in res["notification"]

    task = _get_task(tid)
    assert task["status"] == "doing"  # held — the pipeline never merges it
    assert "[review: validated @" in task["notes"]
    assert f"gates green at {SHA}" in task["notes"]
    assert "ready for human merge — https://github.com/x/y/pull/7" in task["notes"]


def test_validated_note_falls_back_to_branch_without_pr(green_worktree):
    tid = _make_review_task()
    res = reviewer.review_task(tid)
    assert res["verdict"] == "validated"
    assert "fix/task-x" in res["notification"]
    assert "ready for human merge — fix/task-x" in _get_task(tid)["notes"]


def test_blocked_task_is_also_reviewable(green_worktree):
    tid = _make_review_task(status="blocked")
    assert reviewer.review_task(tid)["verdict"] == "validated"


def test_dry_run_validates_without_writing_notes(green_worktree):
    tid = _make_review_task()
    res = reviewer.review_task(tid, dry_run=True)
    assert res["verdict"] == "validated"
    assert "[review:" not in (_get_task(tid)["notes"] or "")


# ── idempotence: one note line per (state, sha) ──────────────────────────


def test_second_run_at_same_sha_is_unchanged(green_worktree):
    tid = _make_review_task()
    assert reviewer.review_task(tid)["verdict"] == "validated"
    notes_after_first = _get_task(tid)["notes"]

    res2 = reviewer.review_task(tid)
    assert res2["verdict"] == "unchanged"
    assert res2["notification"] is None
    assert _get_task(tid)["notes"] == notes_after_first  # no new note
    assert green_worktree["gates"] == 1  # gates were not re-run


def test_new_sha_revalidates(green_worktree, monkeypatch):
    tid = _make_review_task()
    reviewer.review_task(tid)
    monkeypatch.setattr(reviewer, "_head_sha", lambda wt_path: SHA2)
    res = reviewer.review_task(tid)
    assert res["verdict"] == "validated"
    assert _get_task(tid)["notes"].count("[review: validated") == 2


def test_failed_gates_reject_once_per_sha(monkeypatch):
    tid = _make_review_task()
    wt = {"path": "/tmp/wt-y", "branch": "fix/task-y", "head": SHA}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr(reviewer, "_head_sha", lambda wt_path: SHA)

    def failing_gates(task_id, wt_path, force=False, **kwargs):
        raise RuntimeError("Validation checks failed: Unit tests failed")

    monkeypatch.setattr("unified_agent.merge.run_validation_gates", failing_gates)

    res = reviewer.review_task(tid)
    assert res["verdict"] == "failed"
    assert "Unit tests failed" in res["notification"]
    assert _get_task(tid)["notes"].count("[review: rejected") == 1

    # every-15-minutes cron rerun: no note bloat
    res2 = reviewer.review_task(tid)
    assert res2["verdict"] == "unchanged"
    assert _get_task(tid)["notes"].count("[review: rejected") == 1


def test_pytest_timeout_reports_timeout_not_failed(monkeypatch):
    """Task #1254: a pytest-timeout gate failure must read as verdict 'timeout',
    never 'failed' — the review pipeline's own 600s (now configurable) ceiling
    firing is not a red test, and posting it as one produced false FAILED
    verdicts on 7 review runs 09-20..09-22, two of which later merged green.
    """
    from unified_agent.merge import PytestTimeoutError

    tid = _make_review_task()
    wt = {"path": "/tmp/wt-z", "branch": "fix/task-z", "head": SHA}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr(reviewer, "_head_sha", lambda wt_path: SHA)

    calls = {"gates": 0}

    def timing_out_gates(task_id, wt_path, force=False, **kwargs):
        calls["gates"] += 1
        raise PytestTimeoutError("pytest timed out after 1800 seconds")

    monkeypatch.setattr("unified_agent.merge.run_validation_gates", timing_out_gates)

    res = reviewer.review_task(tid)
    assert res["verdict"] == "timeout"
    assert "FAILED" not in res["notification"]
    assert "timed out" in res["notification"]
    assert _get_task(tid)["notes"].count("[review: timeout") == 1
    assert _get_task(tid)["notes"].count("[review: rejected") == 0

    # Unlike a real failure, a timeout at the same sha is retried on the next
    # scan rather than treated as already-resolved — a transient timeout
    # deserves another chance, and idempotence-by-sha here would silently
    # block every future retry at this exact commit.
    res2 = reviewer.review_task(tid)
    assert res2["verdict"] == "timeout"
    assert calls["gates"] == 2
    assert _get_task(tid)["notes"].count("[review: timeout") == 2


def test_pytest_timeout_retry_is_bounded_and_quiet_after_first(monkeypatch):
    """Task #1254 follow-up (orchestrator review of PR #691): an unbounded
    timeout retry would re-run a (now up to 1800s) pytest on every */15 cron
    tick forever, bloat notes with one line per scan, and page Operator on
    every single scan. This asserts the cap:

    - only the FIRST timeout at a sha sends a notification (no spam);
    - the gates re-run up to UA_REVIEW_TIMEOUT_MAX_RETRIES times (default 2);
    - once the cap is reached, the verdict is 'unchanged' with NO gate rerun,
      NO new note, and NO notification — held until a new commit lands;
    - a new branch-head sha resets the count and runs the gates again.
    """
    from unified_agent.merge import PytestTimeoutError

    tid = _make_review_task()
    wt = {"path": "/tmp/wt-cap", "branch": "fix/task-cap", "head": SHA}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))

    current_sha = {"value": SHA}
    monkeypatch.setattr(reviewer, "_head_sha", lambda wt_path: current_sha["value"])

    calls = {"gates": 0}

    def timing_out_gates(task_id, wt_path, force=False, **kwargs):
        calls["gates"] += 1
        raise PytestTimeoutError("pytest timed out after 1800 seconds")

    monkeypatch.setattr("unified_agent.merge.run_validation_gates", timing_out_gates)

    # Default UA_REVIEW_TIMEOUT_MAX_RETRIES is 2: the gates run for attempts
    # 1 and 2 (existing-count 0 and 1, both < 2), then the 3rd scan is capped.
    res1 = reviewer.review_task(tid)
    assert res1["verdict"] == "timeout"
    assert res1["notification"] is not None
    assert calls["gates"] == 1

    res2 = reviewer.review_task(tid)
    assert res2["verdict"] == "timeout"
    assert res2["notification"] is None  # second timeout at this sha: silent
    assert calls["gates"] == 2
    assert _get_task(tid)["notes"].count("[review: timeout") == 2

    # Cap reached (2 timeout notes already exist at SHA): hold silently.
    res3 = reviewer.review_task(tid)
    assert res3["verdict"] == "unchanged"
    assert res3["notification"] is None
    assert calls["gates"] == 2  # gates NOT re-run
    assert _get_task(tid)["notes"].count("[review: timeout") == 2  # no new note

    # A new commit (new sha) resets the count: gates run again and the first
    # timeout at the new sha notifies again.
    current_sha["value"] = SHA2
    res4 = reviewer.review_task(tid)
    assert res4["verdict"] == "timeout"
    assert res4["notification"] is not None
    assert calls["gates"] == 3
    assert _get_task(tid)["notes"].count(f"at {SHA2}") == 1


def test_bun_timeout_reports_timeout_not_failed(monkeypatch):
    """Task #1295 follow-up to #1254: a bun-test timeout gate failure must
    also read as verdict 'timeout', never 'failed' — the same false-FAILED
    bug #1254 fixed for pytest, extended to the bun-test path via the shared
    merge.TestTimeoutError base."""
    from unified_agent.merge import BunTestTimeoutError

    tid = _make_review_task()
    wt = {"path": "/tmp/wt-bun", "branch": "fix/task-bun", "head": SHA}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr(reviewer, "_head_sha", lambda wt_path: SHA)

    def timing_out_gates(task_id, wt_path, force=False, **kwargs):
        raise BunTestTimeoutError("bun test timed out after 600 seconds")

    monkeypatch.setattr("unified_agent.merge.run_validation_gates", timing_out_gates)

    res = reviewer.review_task(tid)
    assert res["verdict"] == "timeout"
    assert "FAILED" not in res["notification"]
    assert "timed out" in res["notification"]
    assert _get_task(tid)["notes"].count("[review: timeout") == 1
    assert _get_task(tid)["notes"].count("[review: rejected") == 0


def test_test_timeout_cause_walks_multi_level_wrap_chain():
    """Task #1295: _pytest_timeout_cause (task #1254) only unwrapped exactly
    one __cause__ level, which happened to be enough for today's single
    `ToolError(...) from e` wrap in tools/git.py but would silently regress
    to a false FAILED verdict the moment a second wrap was added anywhere
    between the test runner and reviewer.py. _test_timeout_cause must find a
    TestTimeoutError buried arbitrarily deep behind __cause__ and __context__."""
    from unified_agent.merge import BunTestTimeoutError, PytestTimeoutError
    from unified_agent.reviewer import _test_timeout_cause

    original = PytestTimeoutError("pytest timed out after 1800 seconds")
    wrap1 = RuntimeError("outer wrap 1")
    wrap1.__cause__ = original
    wrap2 = ValueError("outer wrap 2 (context, not cause)")
    wrap2.__context__ = wrap1
    outer = RuntimeError("outer wrap 3")
    outer.__cause__ = wrap2

    found = _test_timeout_cause(outer)
    assert found is original

    bun_original = BunTestTimeoutError("bun test timed out after 600 seconds")
    bun_wrap = RuntimeError("single wrap")
    bun_wrap.__cause__ = bun_original
    assert _test_timeout_cause(bun_wrap) is bun_original

    # An explicit cause replaces the ambient context. A timeout retained
    # only in that suppressed context cannot mask a genuine final failure.
    both_links = RuntimeError("wrapper with cause and context")
    both_links.__cause__ = ValueError("explicit non-timeout cause")
    both_links.__context__ = original
    assert _test_timeout_cause(both_links) is None

    suppressed = RuntimeError("failure raised from None")
    suppressed.__context__ = original
    suppressed.__suppress_context__ = True
    assert _test_timeout_cause(suppressed) is None

    # A chain with no TestTimeoutError anywhere must return None, not raise.
    plain = RuntimeError("just a real failure")
    plain.__cause__ = ValueError("nested real failure")
    assert _test_timeout_cause(plain) is None


def test_test_timeout_cause_is_bounded_and_cycle_safe():
    """Task #1295: __context__ can cycle (an exception raised while handling
    itself re-points __context__ back at an ancestor). _test_timeout_cause
    must terminate instead of looping forever, and a chain deeper than its
    bound must not hang either."""
    from unified_agent.reviewer import _test_timeout_cause

    # Two exceptions whose __context__ points at each other.
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__context__ = b
    b.__context__ = a
    assert _test_timeout_cause(a) is None  # must return, not hang

    # A long chain with nothing to find must also terminate promptly.
    head = RuntimeError("head")
    current = head
    for i in range(1000):
        nxt = RuntimeError(f"link {i}")
        current.__cause__ = nxt
        current = nxt
    assert _test_timeout_cause(head) is None


def test_yolo_second_gate_timeout_is_not_permanent_failure(monkeypatch):
    """Devin review, PR #691 (task #1254 follow-up): with +yolo, a green first
    gate run (the direct `run_validation_gates` call in `_validate_and_hold`)
    is followed by a SECOND gate run inside `git_merge_task`
    (`orchestrate_merge` re-runs `run_validation_gates` unconditionally). If
    THAT run times out, it must not become a terminal `yolo-merge-failed`
    note — that marker IS covered by `_REVIEWED_AT_SHA_TEMPLATE` and would
    permanently block every retry at this sha for a condition that was never
    a real test failure, recreating the exact bug task #1254 fixes.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.merge import PytestTimeoutError

    tid = _make_review_task(notes_extra="[delivery: mode=local-only yolo=on]")
    wt = {"path": "/tmp/wt-yolo", "branch": "fix/task-yolo", "head": SHA}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr(reviewer, "_head_sha", lambda wt_path: SHA)
    monkeypatch.setattr(
        reviewer,
        "_changed_files_vs_main",
        lambda wt_path, branch: [{"file": "src/foo.py", "status": "M"}],
    )
    # First gate run (direct call in _validate_and_hold) is green.
    monkeypatch.setattr(
        "unified_agent.merge.run_validation_gates",
        lambda task_id, wt_path, force=False, **kwargs: {
            "success": True,
            "audit_status": "pass",
            "tests_passed": True,
        },
    )

    # Second gate run happens inside git_merge_task (the yolo local-merge
    # call) — simulate its real wrapping: orchestrate_merge's PytestTimeoutError
    # reraised as a bare ToolError(str(e)) from e.
    def fake_git_merge_task(task_id, force=False, dry_run=False, allow_in_flight=False):
        raise ToolError("pytest timed out after 1800 seconds") from PytestTimeoutError(
            "pytest timed out after 1800 seconds"
        )

    monkeypatch.setattr("unified_agent.tools.git.git_merge_task", fake_git_merge_task)

    res = reviewer.review_task(tid)
    assert res["verdict"] == "timeout"
    notes = _get_task(tid)["notes"]
    assert "yolo-merge-failed" not in notes
    assert "[review: timeout" in notes


def test_review_and_notify_skips_notification_for_unchanged(green_worktree, monkeypatch):
    tid = _make_review_task()
    reviewer.review_task(tid)

    calls = {"notify": 0}
    monkeypatch.setattr(
        "unified_agent.tools.notify.notify",
        lambda msg, timeout_s=30: (
            calls.__setitem__("notify", calls["notify"] + 1) or {"delivered": True}
        ),
    )
    res = reviewer.review_and_notify(tid)
    assert res["verdict"] == "unchanged"
    assert calls["notify"] == 0
    assert res["notified"] is False


# ── red tier still holds ─────────────────────────────────────────────────


def test_red_tier_verdict_holds_for_human(green_worktree, monkeypatch):
    tid = _make_review_task()
    monkeypatch.setattr(
        reviewer,
        "_changed_files_vs_main",
        lambda wt_path, branch: [
            {"file": "deploy/review-pipeline-cron.sh", "status": "M"},
            {"file": "src/ok.py", "status": "M"},
        ],
    )
    res = reviewer.review_task(tid)
    assert res["verdict"] == "red-tier"
    assert res["red_tier_files"] == ["deploy/review-pipeline-cron.sh"]
    task = _get_task(tid)
    assert task["status"] == "doing"
    assert "[review: red-tier @" in task["notes"]

    # red-tier is also idempotent per sha
    res2 = reviewer.review_task(tid)
    assert res2["verdict"] == "unchanged"
    assert _get_task(tid)["notes"].count("[review: red-tier") == 1


def test_unreadable_diff_holds_instead_of_yolo_merging(green_worktree, monkeypatch):
    """An empty or unreadable diff is not a clean one: hold, never merge.

    Classification also reads the validated head SHA, not a branch name that
    can advance after the gates."""
    tid = _make_review_task(notes_extra="[delivery: mode=local-only yolo=on]")
    seen = []
    monkeypatch.setattr(
        reviewer, "_changed_files_vs_main", lambda wt_path, target: seen.append(target) or []
    )

    def no_merge(**kwargs):
        raise AssertionError("an unclassified diff must not merge")

    monkeypatch.setattr("unified_agent.tools.git.git_merge_task", no_merge)
    res = reviewer.review_task(tid)
    assert res["verdict"] == "red-tier", res
    assert seen == [SHA]
    assert "unreadable" in _get_task(tid)["notes"]


def test_done_task_unreadable_diff_holds_instead_of_merging(monkeypatch):
    tid = _make_review_task(status="done")
    calls = []

    def fake_merge(task_id, force=False, dry_run=False):
        calls.append(dry_run)
        return {"success": True, "dry_run": dry_run}

    monkeypatch.setattr("unified_agent.tools.git.git_merge_task", fake_merge)
    monkeypatch.setattr("unified_agent.cycle_eligibility._changed_files_vs_main", lambda *a: [])
    monkeypatch.setattr(
        "unified_agent.merge.find_task_worktree",
        lambda task_id: ({"path": "/tmp/wt-done", "branch": "agy/done-x", "head": SHA}, "/m"),
    )
    res = reviewer.review_task(tid)
    assert res["verdict"] == "red-tier", res
    assert calls == [True], "dry-run validation only; no real merge"


# ── missing worktree: genuine failure vs. closed-mid-scan race (#329/#347) ─


def test_missing_worktree_reports_failed_for_still_active_task(monkeypatch):
    """No worktree, task genuinely still in flight — this must stay a real failure."""
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    tid = _make_review_task()

    res = reviewer.review_task(tid)

    assert res["verdict"] == "failed"
    assert f"no git worktree found for task {tid}" in res["notification"]
    task = _get_task(tid)
    assert task["status"] == "doing"
    assert "[review: rejected @" in task["notes"]


def test_missing_worktree_soft_skips_when_task_completed_mid_scan(monkeypatch):
    """Task #327/#345 incident: review_task() reads the task row (still
    'doing') before this module ever looks for a worktree. The race window
    is the gap between that read and the worktree lookup a few lines later
    — if another process completes the task and releases its worktree
    inside that gap, the stale 'doing' snapshot must not produce a false
    FAILED, and must not append a spurious [review: rejected] note."""
    tid = _make_review_task()

    def find_worktree_then_complete(task_id):
        # Simulates another process finishing the task at the exact moment
        # this lookup runs — after review_task()'s own status read, before
        # _validate_and_hold's race-guard re-check. task_update requires a
        # recorded review outcome before it will accept status=done for a
        # task still carrying [waiting-for: review].
        current_notes = _get_task(task_id)["notes"] or ""
        task_update(
            task_id,
            notes=current_notes + "\n[review: approved @ concurrent] finished by another process",
            status="done",
            actor="test-agent",
        )
        return None, None

    monkeypatch.setattr("unified_agent.merge.find_task_worktree", find_worktree_then_complete)

    res = reviewer.review_task(tid)

    assert res["verdict"] == "unchanged"
    assert res["notification"] is None
    task = _get_task(tid)
    assert "[review: rejected @" not in task["notes"]


# ── no-local-worktree + branch-on-origin fallback (UA task #736) ──────────


def test_missing_worktree_with_origin_branch_holds_not_fails(
    monkeypatch, workspace_is_product_repo
):
    """UA task #736 regression (#657 x3, #693, #714, #715, #728): a
    desktop-host task's worktree never registers on the hub row
    (worktree_path/branch NULL), so find_task_worktree misses — but the
    branch was pushed to origin from the dev machine and named in the
    notes. The review must HOLD with an actionable origin-branch verdict,
    not false-reject with 'no git worktree found'."""
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    tid = _make_review_task(notes_extra="branch: task/736-fix-worktree-registration")

    lsremote_calls = []

    def fake_lsremote(branch, repo):
        # This test explicitly declares its workspace as the product checkout;
        # the origin lookup belongs to that owned repo, not a cockpit fallback.
        lsremote_calls.append((branch, repo))
        return True  # branch found on origin

    monkeypatch.setattr(reviewer, "_branch_exists_on_origin", fake_lsremote)

    res = reviewer.review_task(tid)

    assert res["verdict"] == "validated"
    assert "no local worktree" in res["notification"]
    assert "task/736-fix-worktree-registration" in res["notification"]
    task = _get_task(tid)
    assert task["status"] == "doing"  # held, never failed
    assert "no git worktree found" not in task["notes"]
    assert "[review: rejected @" not in task["notes"]
    assert "[review: held-no-checkout @" in task["notes"]
    assert "[review: validated @" not in task["notes"]
    assert "found on origin (ls-remote)" in task["notes"]


def test_missing_worktree_origin_fallback_is_idempotent(monkeypatch, workspace_is_product_repo):
    """The cron re-runs the pipeline every 15 minutes; the fallback verdict
    must not bloat notes the way the #657 rejection loop did — one hold
    note per (fallback, branch), then 'unchanged'."""
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    tid = _make_review_task(notes_extra="branch: task/736-fallback")
    monkeypatch.setattr(reviewer, "_branch_exists_on_origin", lambda branch, repo: True)

    res1 = reviewer.review_task(tid)
    notes_after_first = _get_task(tid)["notes"]
    res2 = reviewer.review_task(tid)

    assert res1["verdict"] == "validated"
    assert res2["verdict"] == "validated"
    assert res2["notification"] is None  # nothing new to notify
    assert _get_task(tid)["notes"] == notes_after_first


def test_missing_worktree_without_branch_still_fails(monkeypatch):
    """No worktree AND no branch reference in the notes: nothing on origin
    to check — the genuine failure verdict must survive (ground truth:
    the fallback never masks a real missing-worktree failure)."""
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    monkeypatch.setattr("unified_agent.config.project_repo_roots", lambda: [])
    tid = _make_review_task()

    res = reviewer.review_task(tid)

    assert res["verdict"] == "failed"
    assert f"no git worktree found for task {tid}" in res["notification"]


def test_product_task_is_not_validated_in_same_named_workspace_worktree(monkeypatch, tmp_path):
    """Task #1844 regression: a default-project [code-task] row was reviewed
    by running the gates in the workspace (cockpit) repo's claim-minted
    `.worktrees/task-<id>`, which holds no product code; pytest there failed
    and the verdict read "Unit tests failed". The real lookup must ignore that
    checkout, so no gate runs and the no-worktree path decides instead."""
    from unified_agent import merge

    tid = _make_review_task(notes_extra="[code-task] product fix")
    product = tmp_path / "dev" / "unified_agent"
    product.mkdir(parents=True)
    cockpit_wt = {
        "path": str(config.workspace_root() / ".worktrees" / f"task-{tid}"),
        "branch": f"task/{tid}-validate-me",
        "head": SHA,
    }
    monkeypatch.setattr(
        merge,
        "git_worktree_list",
        lambda repo_path=None: {"worktrees": [{"path": "/ws", "branch": "master"}, cockpit_wt]},
    )
    monkeypatch.setattr(
        merge,
        "_worktrees_of_project_repo",
        lambda repo: [{"path": str(repo), "branch": "main"}],
    )

    def no_gates(*args, **kwargs):
        raise AssertionError("gates ran in a worktree outside the task's repo")

    monkeypatch.setattr(merge, "run_validation_gates", no_gates)
    monkeypatch.setattr(reviewer, "_branch_exists_on_origin", lambda branch, repo: False)

    res = reviewer.review_task(tid)

    assert res["verdict"] == "failed"
    assert f"no git worktree found for task {tid}" in res["notification"]
    assert "Unit tests failed" not in res["notification"]


def test_origin_branch_absent_still_fails(monkeypatch):
    """Notes name a branch, but ls-remote shows it is NOT on origin (work
    never pushed): fallback must not fabricate a pass — rejection stands."""
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    monkeypatch.setattr("unified_agent.config.project_repo_roots", lambda: [])
    tid = _make_review_task(notes_extra="branch: task/736-never-pushed")
    monkeypatch.setattr(
        reviewer,
        "_branch_exists_on_origin",
        lambda branch, repo: False,  # no matching ref on origin
    )

    res = reviewer.review_task(tid)

    assert res["verdict"] == "failed"
    assert f"no git worktree found for task {tid}" in res["notification"]
    task = _get_task(tid)
    assert "[review: validated @" not in task["notes"]


def test_origin_branch_prefix_does_not_false_match(monkeypatch):
    """ls-remote output matching by ref name must be exact: a notes branch
    'task/1' must not pass because 'refs/heads/task/10' exists."""
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    monkeypatch.setattr("unified_agent.config.project_repo_roots", lambda: [])
    tid = _make_review_task(notes_extra="branch: task/1")

    def lsremote_task10_only(branch, repo):
        # Simulates the real ls-remote parser returning False: only
        # task/10 and task/1x exist on origin, not the notes' task/1.
        return False

    monkeypatch.setattr(reviewer, "_branch_exists_on_origin", lsremote_task10_only)

    res = reviewer.review_task(tid)
    assert res["verdict"] == "failed"


def test_branch_exists_on_origin_exact_match():
    """Unit: the ls-remote parser matches the exact ref only, and any git
    failure reads as 'absent' (never as present)."""
    from unittest.mock import patch

    with patch("unified_agent.merge._run_git_in_repo") as fake_git:
        fake_git.return_value = _run_git_result(0, "abcd1234\trefs/heads/fix/task-1\n")
        assert reviewer._branch_exists_on_origin("fix/task-1", "/any/repo") is True
        fake_git.return_value = _run_git_result(0, "abcd1234\trefs/heads/fix/task-10\n")
        assert reviewer._branch_exists_on_origin("fix/task-1", "/any/repo") is False
        fake_git.return_value = _run_git_result(128, "", stderr="error")
        assert reviewer._branch_exists_on_origin("fix/task-1", "/any/repo") is False


# ── status='done' local-merge flow still works ───────────────────────────


def test_done_task_local_merge_flow_still_merges(monkeypatch):
    tid = _make_review_task(status="done")
    calls = []

    def fake_merge(task_id, force=False, dry_run=False):
        calls.append(dry_run)
        if dry_run:
            return {"success": True, "dry_run": True}
        return {"success": True, "commit_hash": "abc1234", "task_id": task_id, "branch": "agy/x"}

    monkeypatch.setattr("unified_agent.tools.git.git_merge_task", fake_merge)
    monkeypatch.setattr(reviewer, "_red_tier_for_task_checked", lambda task_id: ([], None))

    res = reviewer.review_task(tid)
    assert res["verdict"] == "merged"
    assert calls == [True, False]  # dry-run validation, then the real local merge
    assert "[review: approved @" in _get_task(tid)["notes"]


def test_done_task_dry_run_ok(monkeypatch):
    tid = _make_review_task(status="done")
    monkeypatch.setattr(
        "unified_agent.tools.git.git_merge_task",
        lambda task_id, force=False, dry_run=False: {"success": True, "dry_run": True},
    )
    monkeypatch.setattr(reviewer, "_red_tier_for_task_checked", lambda task_id: ([], None))
    res = reviewer.review_task(tid, dry_run=True)
    assert res["verdict"] == "dry-run-ok"


def test_done_task_timeout_retry_is_bounded(monkeypatch):
    """Task #1254 follow-up: the status='done' local-merge flow (_merge_done_task)
    gets the same bounded timeout retry as _validate_and_hold. git_merge_task
    wraps every orchestrate_merge failure in a bare ToolError(str(e)) from e —
    this confirms _pytest_timeout_cause recovers PytestTimeoutError via
    __cause__ and that the sha-based retry cap applies on this path too."""
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.merge import PytestTimeoutError

    tid = _make_review_task(status="done")
    wt = {"path": "/tmp/wt-done", "branch": "agy/done-x", "head": SHA}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr(reviewer, "_head_sha", lambda wt_path: SHA)

    calls = {"merge": 0}

    def fake_merge(task_id, force=False, dry_run=False):
        calls["merge"] += 1
        raise ToolError("pytest timed out after 1800 seconds") from PytestTimeoutError(
            "pytest timed out after 1800 seconds"
        )

    monkeypatch.setattr("unified_agent.tools.git.git_merge_task", fake_merge)

    res1 = reviewer.review_task(tid)
    assert res1["verdict"] == "timeout"
    assert res1["notification"] is not None
    assert calls["merge"] == 1

    # Second timeout at the same sha: retried (still under the default cap of
    # 2), but silent — no phone spam for an unchanged condition.
    res2 = reviewer.review_task(tid)
    assert res2["verdict"] == "timeout"
    assert res2["notification"] is None
    assert calls["merge"] == 2
    assert _get_task(tid)["notes"].count("[review: timeout") == 2

    # Cap reached: held as 'unchanged', no third merge attempt, no new note.
    res3 = reviewer.review_task(tid)
    assert res3["verdict"] == "unchanged"
    assert calls["merge"] == 2
    assert _get_task(tid)["notes"].count("[review: timeout") == 2


def test_done_task_timeout_retry_is_bounded_without_sha(monkeypatch):
    """Task #1295 follow-up to #1254: _merge_done_task's head_sha resolution
    is best-effort (`find_task_worktree`/`_head_sha` can fail or return
    None). `_timeout_count_at_sha` always returns 0 without a sha (it can't
    tell one commit's timeouts from another's), so before this fix the retry
    cap never engaged on this path and a task whose worktree metadata never
    resolves would re-run the (possibly 1800s) merge gate on every single
    invocation forever. It must fall back to a task-level total-note cap."""
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.merge import PytestTimeoutError

    tid = _make_review_task(status="done")
    wt = {"path": "/tmp/wt-done-nosha", "branch": "agy/done-nosha", "head": None}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    # Neither rev-parse nor the worktree record provides a SHA.
    monkeypatch.setattr(reviewer, "_head_sha", lambda wt_path: None)

    calls = {"merge": 0}

    def fake_merge(task_id, force=False, dry_run=False):
        calls["merge"] += 1
        raise ToolError("pytest timed out after 1800 seconds") from PytestTimeoutError(
            "pytest timed out after 1800 seconds"
        )

    monkeypatch.setattr("unified_agent.tools.git.git_merge_task", fake_merge)

    res1 = reviewer.review_task(tid)
    assert res1["verdict"] == "timeout"
    assert res1["head_sha"] is None
    assert res1["notification"] is not None
    assert calls["merge"] == 1

    res2 = reviewer.review_task(tid)
    assert res2["verdict"] == "timeout"
    assert res2["notification"] is None
    assert calls["merge"] == 2
    assert _get_task(tid)["notes"].count("[review: timeout") == 2

    # Cap reached (default UA_REVIEW_TIMEOUT_MAX_RETRIES=2): held as
    # 'unchanged' with NO third merge attempt — before this fix, a head-less
    # task retried forever since _timeout_count_at_sha(notes, None) == 0
    # never reached the cap.
    res3 = reviewer.review_task(tid)
    assert res3["verdict"] == "unchanged"
    assert calls["merge"] == 2
    assert _get_task(tid)["notes"].count("[review: timeout") == 2


def test_done_task_timeout_uses_worktree_head_when_rev_parse_fails(monkeypatch):
    """Old timeout notes must not consume a new head's retry budget."""
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.merge import PytestTimeoutError

    old_notes = (
        f"[review: timeout @ 2026-09-22T00:00:00Z] at {SHA2}: old timeout\n"
        f"[review: timeout @ 2026-09-22T00:01:00Z] at {SHA2}: old timeout"
    )
    tid = _make_review_task(status="done", notes_extra=old_notes)
    wt = {"path": "/tmp/wt-done-new-head", "branch": "agy/done-new-head", "head": SHA}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr(reviewer, "_head_sha", lambda wt_path: None)
    calls = 0

    def fake_merge(task_id, force=False, dry_run=False):
        nonlocal calls
        calls += 1
        raise ToolError("pytest timed out after 1800 seconds") from PytestTimeoutError(
            "pytest timed out after 1800 seconds"
        )

    monkeypatch.setattr("unified_agent.tools.git.git_merge_task", fake_merge)

    result = reviewer.review_task(tid)
    assert result["verdict"] == "timeout"
    assert result["head_sha"] == SHA
    assert calls == 1


# ── reconcile ────────────────────────────────────────────────────────────


def _quiet_git(monkeypatch, worktrees=None, project_repo=_DEFAULT_REVIEW_REPO):
    """Silence every git/gh seam for the reconcile tests.

    Mocked Git evidence still requires an existing declared product checkout,
    so these fixtures create one under the isolated dev root. `project_repo=None`
    simulates a project with no checked-out repo on this host, which is a
    distinct verdict ('unresolved-repo') from a missing branch.
    """
    if project_repo is _DEFAULT_REVIEW_REPO:
        project_repo = config.dev_root() / config.PROJECT_REPO_DIRNAMES["unified-agent"]
    if project_repo is not None:
        Path(project_repo).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "unified_agent.config.project_repo_path",
        lambda project: Path(project_repo) if project_repo else None,
    )
    monkeypatch.setattr(reviewer, "_list_worktrees", lambda repo_path=None: worktrees or [])
    monkeypatch.setattr(reviewer, "_fetch_origin", lambda repo_path: None)
    monkeypatch.setattr(reviewer, "_branch_exists_locally", lambda branch, repo_path: False)
    monkeypatch.setattr(
        reviewer, "_branch_merged_into_origin_main", lambda branch, repo_path: False
    )
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: None)


def test_reconcile_merged_branch_marks_task_done_once(monkeypatch):
    tid = _make_review_task()
    wts = [
        {"path": "/tmp/main", "branch": "main"},
        {"path": f"/tmp/wt-{tid}", "branch": f"fix/task-{tid}", "head": SHA},
    ]
    _quiet_git(monkeypatch, worktrees=wts)
    monkeypatch.setattr(reviewer, "_branch_merged_into_origin_main", lambda branch, repo_path: True)

    out = reviewer.reconcile_tasks()
    assert [r["verdict"] for r in out["results"]] == ["merged-upstream"]

    task = _get_task(tid)
    assert task["status"] == "done"
    assert task["completed_at"] is not None
    assert task["notes"].count("[review: merged-upstream") == 1

    # second pass: task is done now, so it is no longer a reconcile candidate
    assert reviewer.reconcile_tasks()["results"] == []
    assert _get_task(tid)["notes"].count("[review: merged-upstream") == 1


@pytest.mark.parametrize("pr_state", ["OPEN", "CLOSED", None, "MERGED"])
def test_reconcile_explicit_pr_overrides_stale_local_ancestor(
    monkeypatch, workspace_is_product_repo, pr_state
):
    # A native author can have a newer PR head while the hub's task branch
    # remains at an old base. Real git ancestry is not proof that PR merged.
    tid = _make_review_task(notes_extra="[pr: https://github.com/acme/product/pull/9]")
    branch = f"task/{tid}-stale"
    task_update(tid, append_note=f"branch {branch}", actor="test-agent", worktree=False)
    repo = config.workspace_root()
    run = reviewer.subprocess.run

    def git(*args):
        return run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Test Author")
    git("config", "user.email", "test@example.invalid")
    git("commit", "--allow-empty", "-m", "old base")
    git("branch", branch)
    git("commit", "--allow-empty", "-m", "advance main")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("checkout", "-b", "newer-pr-head")
    git("commit", "--allow-empty", "-m", "unmerged work")
    pr_head = git("rev-parse", "HEAD")
    git("checkout", "main")
    assert git("rev-parse", branch) != pr_head
    git("merge-base", "--is-ancestor", branch, "origin/main")

    def github_boundary(args, *positional, **kwargs):
        if Path(args[0]).name != "gh":
            return run(args, *positional, **kwargs)
        assert args[1:3] == ["pr", "view"]
        return SimpleNamespace(
            returncode=1 if pr_state is None else 0,
            stdout=json.dumps(
                {
                    "state": pr_state,
                    "url": "https://github.com/acme/product/pull/9",
                    "headRefOid": pr_head,
                    "mergeCommit": {"oid": pr_head} if pr_state == "MERGED" else None,
                }
            ),
        )

    which = reviewer.shutil.which
    monkeypatch.setattr(
        reviewer.shutil, "which", lambda name: "gh" if name == "gh" else which(name)
    )
    monkeypatch.setattr(reviewer.subprocess, "run", github_boundary)

    out = reviewer.reconcile_tasks()

    if pr_state == "MERGED":
        assert [row["verdict"] for row in out["results"]] == ["merged-upstream"]
        assert _get_task(tid)["status"] == "done"
    else:
        assert out["results"] == []
        assert _get_task(tid)["status"] == "doing"
        assert "[review: merged-upstream" not in _get_task(tid)["notes"]


def test_reconcile_gh_catches_squash_merge_when_branch_missing(monkeypatch):
    tid = _make_review_task(notes_extra="[pr: https://github.com/x/y/pull/9]")
    _quiet_git(monkeypatch)
    seen = {}

    def fake_gh(ref, repo_path):
        seen["ref"] = ref
        return "MERGED"

    monkeypatch.setattr(reviewer, "_gh_pr_state", fake_gh)

    out = reviewer.reconcile_tasks()
    assert out["results"][0]["verdict"] == "merged-upstream"
    assert seen["ref"] == "https://github.com/x/y/pull/9"
    assert _get_task(tid)["status"] == "done"


def test_reconcile_degrades_gracefully_without_gh(monkeypatch):
    """gh missing (e.g. cron PATH): a PR-referencing task is left alone, not staled."""
    tid = _make_review_task(notes_extra="[pr: https://github.com/x/y/pull/9]")
    _quiet_git(monkeypatch)  # _gh_pr_state → None simulates gh being unavailable
    out = reviewer.reconcile_tasks()
    assert out["results"] == []
    assert _get_task(tid)["status"] == "doing"
    assert "[review:" not in _get_task(tid)["notes"]


def test_reconcile_stale_branch_missing_reported_once(monkeypatch):
    # The isolated project dir is not a git repo; without a ceiling, git walks
    # up from it into whatever repo encloses the host's temp root (#2278).
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(config.dev_root().parent))
    tid = _make_review_task()
    _quiet_git(monkeypatch)

    out = reviewer.reconcile_tasks()
    assert out["results"][0]["verdict"] == "stale"
    task = _get_task(tid)
    assert task["status"] == "doing"
    assert task["notes"].count("stale — branch missing") == 1

    # rerun: idempotent, reported once
    assert reviewer.reconcile_tasks()["results"] == []
    assert _get_task(tid)["notes"].count("stale — branch missing") == 1

    # the review pass then skips it instead of re-noting a rejection
    assert reviewer.review_task(tid)["verdict"] == "unchanged"
    assert "[review: rejected" not in _get_task(tid)["notes"]

    # #2278: a worktree found by name that is not a task worktree (outside the
    # workspace and every project's .worktrees/) is no better than none.
    foreign = {"path": f"/nonexistent/elsewhere/task-{tid}", "branch": f"task/{tid}-x"}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (foreign, None))
    assert reviewer.review_task(tid)["verdict"] == "unchanged"
    assert "[review: rejected" not in _get_task(tid)["notes"]


def test_reconcile_open_status_task_is_reconciled(monkeypatch):
    """task #303: a task carrying [waiting-for: review] whose status is 'open'
    (not doing/blocked — e.g. an agent explicitly left it open pending further
    work, as #299 was) must still be reconciled. Before this fix,
    reconcile_tasks/scan_for_reviewable_tasks hardcoded status IN
    ('doing','blocked'), so an 'open' review task was permanently invisible
    to the pipeline: a wrong stale/rejected stamp from an earlier moment could
    never be corrected, no matter how long the branch had been merged."""
    tid = _make_review_task(status="open")
    wts = [
        {"path": "/tmp/main", "branch": "main"},
        {"path": f"/tmp/wt-{tid}", "branch": f"fix/task-{tid}", "head": SHA},
    ]
    _quiet_git(monkeypatch, worktrees=wts)
    monkeypatch.setattr(reviewer, "_branch_merged_into_origin_main", lambda branch, repo_path: True)

    out = reviewer.reconcile_tasks()
    assert [r["verdict"] for r in out["results"]] == ["merged-upstream"]
    assert _get_task(tid)["status"] == "done"


def test_reconcile_open_status_stale_branch_missing_reported_once(monkeypatch):
    """Same gap, the other verdict: an 'open' task with a genuinely missing
    branch still gets (and keeps, idempotently) a real stale verdict instead
    of being silently skipped forever."""
    tid = _make_review_task(status="open")
    _quiet_git(monkeypatch)

    out = reviewer.reconcile_tasks()
    assert out["results"][0]["verdict"] == "stale"
    assert _get_task(tid)["status"] == "open"

    assert reviewer.reconcile_tasks()["results"] == []
    assert _get_task(tid)["notes"].count("stale — branch missing") == 1


def test_scan_reports_open_status_review_task():
    tid = _make_review_task(status="open")
    ids = {t["id"] for t in reviewer.scan_for_reviewable_tasks()["tasks"]}
    assert tid in ids


def test_scan_ignores_open_spec_with_boundary_review_template():
    """Task #709: open spec tasks whose Boundary says '… when done' are not reviewable."""
    t = task_add(
        "UA Task Queue V2.0 — spec only",
        notes="### Boundaries\nRepo work per COLLABORATION.md; "
        f"{reviewer.WAITING_FOR_REVIEW} when done",
    )
    ids = {x["id"] for x in reviewer.scan_for_reviewable_tasks()["tasks"]}
    assert t["id"] not in ids


def test_scan_includes_doing_claimed_task_with_live_review_marker():
    from unified_agent.tools.tasks import task_claim

    t = task_add("real review", notes=f"{reviewer.WAITING_FOR_REVIEW} branch cursor/task-x")
    task_claim(t["id"], agent="Desktop_Agent4")
    ids = {x["id"] for x in reviewer.scan_for_reviewable_tasks()["tasks"]}
    assert t["id"] in ids


def test_scan_includes_case_variant_live_review_marker():
    from unified_agent.tools.tasks import task_claim

    t = task_add("case variant", notes="[WAITING-FOR: REVIEW] branch cursor/task-x")
    task_claim(t["id"], agent="Desktop_Agent4")
    ids = {x["id"] for x in reviewer.scan_for_reviewable_tasks()["tasks"]}
    assert t["id"] in ids


def test_scan_ignores_review_resolved_after_live_marker():
    t = task_add(
        "resolved review",
        notes=f"{reviewer.WAITING_FOR_REVIEW}\n[review: merged-upstream @ 2026-09-21]",
    )
    ids = {x["id"] for x in reviewer.scan_for_reviewable_tasks()["tasks"]}
    assert t["id"] not in ids


def test_scan_keeps_live_marker_quoted_with_no_resolution():
    """Task #1242: a live marker with no resolution stays reviewable even when a
    later reconciliation note quotes the bare marker for reference. Decided:
    quoting alone must not retroactively cancel a genuinely pending review —
    only a real [review: ...] outcome does that. The quoted occurrence is
    stripped before the live-marker scan, so it neither adds nor removes
    liveness here; the original unquoted marker is what keeps this reviewable.
    """
    t = task_add(
        "live then quoted, no resolution",
        notes=(
            f"{reviewer.WAITING_FOR_REVIEW} branch cursor/task-x\n"
            f'Note: reconciliation quotes the marker "{reviewer.WAITING_FOR_REVIEW}" '
            "for reference only."
        ),
    )
    ids = {x["id"] for x in reviewer.scan_for_reviewable_tasks()["tasks"]}
    assert t["id"] in ids


def test_scan_ignores_resolved_review_with_later_quoted_marker():
    """Task #1242 (#1043, #1143): a later reconciliation note that QUOTES the
    bare marker must not re-arm a row whose review already resolved. Without
    stripping the quoted span, the quoted marker reads as a new live
    occurrence positioned after the resolution marker, and the row wrongly
    resurfaces.
    """
    t = task_add(
        "resolved then quoted mention",
        notes=(
            f"{reviewer.WAITING_FOR_REVIEW}\n"
            "[review: merged-upstream @ 2026-09-21]\n"
            f'Reconciliation note: previously requested via "{reviewer.WAITING_FOR_REVIEW}", '
            "now closed."
        ),
    )
    ids = {x["id"] for x in reviewer.scan_for_reviewable_tasks()["tasks"]}
    assert t["id"] not in ids


def test_scan_keeps_live_marker_bracketed_by_apostrophes():
    """Devin Review on PR #674: a single-quote quoted-span alternative treats
    ordinary apostrophes (possessive/contraction) as quote delimiters, so a
    genuine live marker sitting between two unrelated apostrophes on the same
    line got blanked out and silently dropped from the review scan — a false
    negative, and worse than #1242's original symptom (a resolved row
    resurfacing is noisy but visible; a pending review vanishing is silent).
    reviewer.py deliberately has no single-quote alternative in
    _QUOTED_SPAN_RX for this reason; only backtick and double-quote spans are
    stripped. This asserts the marker survives and the task stays
    reviewable.
    """
    t = task_add(
        "apostrophes bracket a live marker",
        notes=f"Owner's task {reviewer.WAITING_FOR_REVIEW} isn't merged",
    )
    ids = {x["id"] for x in reviewer.scan_for_reviewable_tasks()["tasks"]}
    assert t["id"] in ids


def test_scan_keeps_new_review_request_after_prior_resolution():
    t = task_add(
        "new review after old resolution",
        notes=(
            f"{reviewer.WAITING_FOR_REVIEW}\n[review: rejected @ 2026-09-20]\n"
            f"{reviewer.WAITING_FOR_REVIEW} branch cursor/task-x"
        ),
    )
    ids = {x["id"] for x in reviewer.scan_for_reviewable_tasks()["tasks"]}
    assert t["id"] in ids


def test_scan_ignores_claimed_blocked_review_resolved_after_marker():
    from unified_agent.tools.tasks import task_claim

    t = task_add(
        "claimed blocked resolved review",
        notes=f"{reviewer.WAITING_FOR_REVIEW}\n[review: stale @ 2026-09-21]",
    )
    task_claim(t["id"], agent="Desktop_Agent4")
    task_update(t["id"], status="blocked")
    ids = {x["id"] for x in reviewer.scan_for_reviewable_tasks()["tasks"]}
    assert t["id"] not in ids


def test_scan_keeps_plan_approval_when_boundary_review_text_present():
    t = task_add(
        "plan approval",
        notes=f"plan: abc123\n{reviewer.WAITING_FOR_PLAN_APPROVAL}\n"
        f"{reviewer.WAITING_FOR_REVIEW} when done",
    )
    ids = {x["id"] for x in reviewer.scan_for_reviewable_tasks()["tasks"]}
    assert t["id"] in ids
    row = next(x for x in reviewer.scan_for_reviewable_tasks()["tasks"] if x["id"] == t["id"])
    assert row["kind"] == "plan-approval"


def test_reconcile_watches_waiting_for_human_merge(monkeypatch):
    t = task_add("await-human", notes=f"{reviewer.WAITING_FOR_HUMAN_MERGE} branch fix/hold-me")
    task_update(t["id"], status="doing")
    _quiet_git(monkeypatch)
    monkeypatch.setattr(reviewer, "_branch_exists_locally", lambda branch, repo_path: True)
    seen = {}

    def merged(branch, repo_path):
        seen["branch"] = branch
        return True

    monkeypatch.setattr(reviewer, "_branch_merged_into_origin_main", merged)

    out = reviewer.reconcile_tasks()
    assert out["results"][0]["verdict"] == "merged-upstream"
    assert seen["branch"] == "fix/hold-me"
    assert _get_task(t["id"])["status"] == "done"


def test_reconcile_leaves_unmerged_branches_for_the_review_pass(monkeypatch):
    tid = _make_review_task()
    wts = [
        {"path": "/tmp/main", "branch": "main"},
        {"path": f"/tmp/wt-{tid}", "branch": f"fix/task-{tid}", "head": SHA},
    ]
    _quiet_git(monkeypatch, worktrees=wts)
    out = reviewer.reconcile_tasks()
    assert out["results"] == []
    assert _get_task(tid)["status"] == "doing"


def test_reconcile_dry_run_reports_without_mutating(monkeypatch):
    tid = _make_review_task()
    wts = [
        {"path": "/tmp/main", "branch": "main"},
        {"path": f"/tmp/wt-{tid}", "branch": f"fix/task-{tid}", "head": SHA},
    ]
    _quiet_git(monkeypatch, worktrees=wts)
    monkeypatch.setattr(reviewer, "_branch_merged_into_origin_main", lambda branch, repo_path: True)

    out = reviewer.reconcile_tasks(dry_run=True)
    assert out["results"][0]["verdict"] == "merged-upstream"
    task = _get_task(tid)
    assert task["status"] == "doing"
    assert "[review: merged-upstream" not in task["notes"]


# ── assert-no-push: no remote writes anywhere in the pipeline sources ─────


def test_no_remote_write_invocations_in_pipeline_sources():
    """The validate+hold policy: no code path may run a git push or a PR merge.

    Scans the pipeline sources for invocation patterns (subprocess argument
    lists and shell strings), not prose."""
    src_dir = Path(reviewer.__file__).parent
    forbidden = [
        re.compile(r"[\"']push[\"']"),
        re.compile(r"git push"),
        re.compile(r"[\"']pr[\"']\s*,\s*[\"']merge[\"']"),
        re.compile(r"gh pr merge"),
        re.compile(r"[\"']merge[\"']\s*,\s*[\"']--"),
    ]
    for name in ("reviewer.py", "review_cli.py", "merge.py", "tools/git.py"):
        text = (src_dir / name).read_text(encoding="utf-8")
        for pat in forbidden:
            assert not pat.search(text), (
                f"{name} contains forbidden remote-write pattern: {pat.pattern}"
            )


# ── plan-approval consumer (task #65) ─────────────────────────────────────


def _make_plan_task(plan_id, status="doing", extra=""):
    """A task annotated for plan-approval, referencing a plan id."""
    notes = f"{reviewer.WAITING_FOR_PLAN_APPROVAL} plan: {plan_id} {extra}".strip()
    t = task_add("approve-my-plan", notes=notes)
    task_update(t["id"], status=status)
    return t["id"]


class TestOutcomesAwaitingReviewScan:
    """Review-cron consumer for unreviewed terminal delegation outcomes (#1142)."""

    def test_scan_surfaces_unreviewed_terminal_outcomes(self):
        from unified_agent.tools.delegate import delegate, delegate_fail

        r = delegate(
            task_spec="Needs outcome review",
            from_agent="Desktop_Agent5",
            to_agent="server_agent2",
            callback="none",
        )
        delegate_fail(r["delegation_id"], agent="server_agent2", reason="boom")

        out = reviewer.scan_outcomes_awaiting_review()
        ids = {o["delegation_id"] for o in out["outcomes"]}
        assert r["delegation_id"] in ids
        assert out["count"] >= 1


class TestReviewPlanApprovals:
    def _plan(self):
        from unified_agent.tools.plan import plan_create

        return plan_create("ship the thing", ["step a", "step b"], author="Desktop_Agent1")[
            "plan_id"
        ]

    def test_notifies_once_per_fingerprint(self, monkeypatch):
        sent = []
        monkeypatch.setattr(reviewer, "_notify_best_effort", lambda m: sent.append(m) or True)
        pid = self._plan()
        _make_plan_task(pid)

        out = reviewer.review_plan_approvals(send_notifications=True)
        assert [r["verdict"] for r in out["results"]] == ["awaiting-approval"]
        assert out["results"][0]["plan_id"] == pid
        assert len(sent) == 1
        assert "awaits your plan approval" in sent[0]

        # second run, plan unchanged → no re-notify
        out2 = reviewer.review_plan_approvals(send_notifications=True)
        assert out2["results"] == []
        assert len(sent) == 1

    def test_renotifies_when_plan_changes(self, monkeypatch):
        from unified_agent.tools.plan import plan_update

        monkeypatch.setattr(reviewer, "_notify_best_effort", lambda m: True)
        pid = self._plan()
        _make_plan_task(pid)
        reviewer.review_plan_approvals(send_notifications=True)

        # advance a step → fingerprint changes → one fresh notification
        plan_update(pid, step_idx=0, status="done")
        out = reviewer.review_plan_approvals(send_notifications=True)
        assert [r["verdict"] for r in out["results"]] == ["awaiting-approval"]
        # and idempotent again at the new state
        assert reviewer.review_plan_approvals()["results"] == []

    def test_missing_plan_line_is_unresolvable_once(self, monkeypatch):
        monkeypatch.setattr(reviewer, "_notify_best_effort", lambda m: True)
        t = task_add("no plan ref", notes=reviewer.WAITING_FOR_PLAN_APPROVAL)
        task_update(t["id"], status="doing")
        out = reviewer.review_plan_approvals()
        assert out["results"][0]["verdict"] == "unresolvable"
        assert "no 'plan: <id>'" in out["results"][0]["notification"]
        # reported once
        assert reviewer.review_plan_approvals()["results"] == []

    def test_unknown_plan_id_is_unresolvable(self, monkeypatch):
        monkeypatch.setattr(reviewer, "_notify_best_effort", lambda m: True)
        _make_plan_task("deadbeef0000")  # no such plan
        out = reviewer.review_plan_approvals()
        assert out["results"][0]["verdict"] == "unresolvable"
        assert "does not exist" in out["results"][0]["notification"]

    def test_approved_annotation_drops_out_of_scan(self, monkeypatch):
        monkeypatch.setattr(reviewer, "_notify_best_effort", lambda m: True)
        pid = self._plan()
        # task references a plan but is already approved (no waiting annotation)
        t = task_add("done deal", notes=f"[plan: approved @ 2026-07-18T00:00:00Z] plan: {pid}")
        task_update(t["id"], status="doing")
        assert reviewer.review_plan_approvals()["results"] == []

    def test_dry_run_mutates_nothing(self, monkeypatch):
        monkeypatch.setattr(reviewer, "_notify_best_effort", lambda m: True)
        pid = self._plan()
        tid = _make_plan_task(pid)
        out = reviewer.review_plan_approvals(dry_run=True)
        assert out["results"][0]["verdict"] == "awaiting-approval"
        # no note appended → a real run would still see it as fresh
        assert reviewer.PLAN_APPROVAL_NOTIFIED not in (_get_task(tid)["notes"])

    def test_open_status_plan_task_is_reviewed(self, monkeypatch):
        """Same task #303 gap as the code-review path: an 'open' task
        annotated [waiting-for: plan-approval] must not be permanently
        invisible to review_plan_approvals just because it isn't doing/blocked."""
        monkeypatch.setattr(reviewer, "_notify_best_effort", lambda m: True)
        pid = self._plan()
        tid = _make_plan_task(pid, status="open")
        out = reviewer.review_plan_approvals()
        assert [r["verdict"] for r in out["results"]] == ["awaiting-approval"]
        assert out["results"][0]["task_id"] == tid

    def test_scan_reports_kind(self):
        pid = self._plan()
        _make_plan_task(pid)
        _make_review_task()
        by_kind = {t["kind"] for t in reviewer.scan_for_reviewable_tasks()["tasks"]}
        assert by_kind == {"review", "plan-approval"}


def test_pr_url_from_notes_accepts_bare_and_prose_forms():
    """Live task notes write 'PR https://…' in prose, not 'PR: <url>' — both
    must resolve (2026-07-17: tasks #62/#64 were staled instead of reconciled
    because only the colon form matched)."""
    colon = "shipped.\nPR: https://github.com/x/y/pull/31\nheld."
    assert reviewer._pr_url_from_notes(colon) == "https://github.com/x/y/pull/31"
    # colon form with trailing prose punctuation
    colon_punct = "PR: https://github.com/x/y/pull/31, HELD for review"
    assert reviewer._pr_url_from_notes(colon_punct) == "https://github.com/x/y/pull/31"
    # bare prose form, as written by the steward's shipped-at blocks
    bare = "SHIPPED AT PR #31: PR https://github.com/x/y/pull/31, HELD per validate+hold."
    assert reviewer._pr_url_from_notes(bare) == "https://github.com/x/y/pull/31"
    assert reviewer._pr_url_from_notes("no urls here") is None


def test_pr_ref_from_notes_falls_back_to_bare_number():
    """Bare PR numbers resolve only when in pipeline-generated marker lines;
    author prose mentions are ignored."""
    assert reviewer._pr_ref_from_notes("[review: PR #59 open for review]") == "59"
    assert reviewer._pr_ref_from_notes("[pr: Opened PR#61, awaiting merge.]") == "61"
    # a real URL still takes precedence over a coincidental bare number
    mixed = "[review: see PR #31: PR: https://github.com/x/y/pull/31]"
    assert reviewer._pr_ref_from_notes(mixed) == "https://github.com/x/y/pull/31"
    assert reviewer._pr_ref_from_notes("PR #59 open for review") is None
    assert reviewer._pr_ref_from_notes("Opened PR#61, awaiting merge.") is None
    assert reviewer._pr_ref_from_notes("see PR #31: PR: https://github.com/x/y/pull/31") is None
    poisoned = "Context: task #982 merged change PR #93 before this task was filed."
    assert reviewer._pr_ref_from_notes(poisoned) is None
    assert reviewer._pr_ref_from_notes("no PR referenced here") is None


def test_pr_ref_from_notes_ignores_prose_counterexamples_and_requires_marker():
    """Exact incident strings from task #986 counterexample: author-supplied prose
    must return None, while pipeline-generated markers and evidence resolution work."""
    counterexample_1 = (
        "Context: #979 / PR #501 (merged) fixed the COMMIT path on macOS; "
        "this task fixes the GATE path on both hosts."
    )
    counterexample_2 = (
        "Context (verified 2026-09-12 by clean-room probes, escalation forbidden): "
        "on Linux codex is bubblewrap-based and holds the repo git dir READ-ONLY. "
        "On macOS the same mechanism WORKS and is merged (PR #501). "
        "Related filed tasks: #978 (dispatch-side marker guard)."
    )
    assert reviewer._pr_ref_from_notes(counterexample_1) is None
    assert reviewer._pr_ref_from_notes(counterexample_2) is None

    # Positive paths: pipeline-generated marker lines ([review: or [pr:)
    assert reviewer._pr_ref_from_notes("[review: PR #501]") == "501"
    assert (
        reviewer._pr_ref_from_notes(
            "[review: merged-upstream @ 2026-09-12] PR https://github.com/foo/bar/pull/501"
        )
        == "https://github.com/foo/bar/pull/501"
    )
    assert reviewer._pr_ref_from_notes("[pr: #501]") == "501"
    assert reviewer._pr_ref_from_notes("[pr: 501]") == "501"
    assert (
        reviewer._pr_ref_from_notes("[pr: https://github.com/foo/bar/pull/501]")
        == "https://github.com/foo/bar/pull/501"
    )

    # Evidence ref resolution remains unaffected
    assert (
        reviewer._pr_ref_from_evidence("https://github.com/foo/bar/pull/501")
        == "https://github.com/foo/bar/pull/501"
    )
    assert reviewer._pr_ref_from_evidence("#501") == "501"
    assert reviewer._pr_ref_from_evidence("501") == "501"


def test_reconcile_bare_pr_number_catches_squash_merge(monkeypatch):
    """End-to-end reproduction of the tasks #115-117 squash-merge check: notes
    reference the PR in a pipeline marker ('[pr: #59]'), the worktree
    is already gone (cleaned up post-merge), and no 'branch <name>' line
    exists either — reconcile must still resolve via gh instead of flagging
    stale."""
    tid = _make_review_task(notes_extra="[pr: #59]")
    _quiet_git(monkeypatch)
    seen = {}

    def fake_gh(ref, repo_path):
        seen["ref"] = ref
        return "MERGED"

    monkeypatch.setattr(reviewer, "_gh_pr_state", fake_gh)

    out = reviewer.reconcile_tasks()
    assert out["results"][0]["verdict"] == "merged-upstream"
    assert seen["ref"] == "59"
    assert _get_task(tid)["status"] == "done"


def test_reconcile_bare_pr_number_not_flagged_stale_when_unresolved(monkeypatch):
    """Same notes, but gh is unavailable (e.g. cron PATH) — must be left
    alone, not staled, since a PR reference does exist."""
    tid = _make_review_task(notes_extra="[pr: #59]")
    _quiet_git(monkeypatch)  # _gh_pr_state → None simulates gh being unavailable
    out = reviewer.reconcile_tasks()
    assert out["results"] == []
    assert _get_task(tid)["status"] == "doing"
    assert "[review:" not in _get_task(tid)["notes"]


def test_reconcile_ignores_unrelated_merged_change_mentioned_in_delegated_spec(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    repo.mkdir()
    notes = "\n".join(
        [
            "[delegated-by: Desktop_Agent5] [delegated-to: Desktop_Agent2]",
            "[code-task]",
            "Spec context: task #982 merged change PR #93 before this task was filed.",
        ]
    )
    t = task_add("delegated poison spec", notes=notes)
    task_update(t["id"], status="waiting_review")
    _quiet_git(monkeypatch, project_repo=str(repo))
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_state",
        lambda ref, repo_path: pytest.fail(f"unexpected pull-request lookup for {ref}"),
    )
    monkeypatch.setattr(reviewer, "_gh_pr_for_task", lambda task_id, repo_path: None)

    out = reviewer.reconcile_tasks()

    assert out["results"] == []
    task = _get_task(t["id"])
    assert task["status"] == "waiting_review"
    assert "[review: merged-upstream" not in task["notes"]


def test_reconcile_ignores_tonights_poisoned_delegated_spec_notes(monkeypatch, tmp_path):
    """Spec prose about another task's merge is never this task's PR evidence."""
    repo = tmp_path / "repo"
    repo.mkdir()
    notes = "\n".join(
        [
            "[delegated-by: Desktop_Agent5] [delegated-to: Desktop_Agent2]",
            "[code-task]",
            "Execute task #1082 after this delegated slice is claimed.",
            "The parent slice (#1053) shipped and is live-merged at b14c0b0.",
            "PR #584 merged before this child task began.",
        ]
    )
    t = task_add("delegated poison spec", notes=notes)
    task_update(t["id"], status="waiting_review")
    _quiet_git(monkeypatch, project_repo=str(repo))
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_state",
        lambda ref, repo_path: pytest.fail(f"unexpected pull-request lookup for {ref}"),
    )
    monkeypatch.setattr(reviewer, "_gh_pr_for_task", lambda task_id, repo_path: None)

    out = reviewer.reconcile_tasks()

    assert out["results"] == []
    task = _get_task(t["id"])
    assert task["status"] == "waiting_review"
    assert "[review: merged-upstream" not in task["notes"]


def test_reconcile_bare_pr_url_catches_squash_merge(monkeypatch):
    """End-to-end: a task whose notes carry only a bare PR URL (no colon, no
    live branch) reconciles as merged-upstream instead of stale."""
    tid = _make_review_task(
        notes_extra="[review: PR https://github.com/x/y/pull/31, HELD for Operator.]"
    )
    _quiet_git(monkeypatch)
    seen = {}

    def fake_gh(ref, repo_path):
        seen["ref"] = ref
        return "MERGED"

    monkeypatch.setattr(reviewer, "_gh_pr_state", fake_gh)

    out = reviewer.reconcile_tasks()
    assert out["results"][0]["verdict"] == "merged-upstream"
    assert seen["ref"] == "https://github.com/x/y/pull/31"
    assert _get_task(tid)["status"] == "done"


def test_pr_ref_from_evidence_accepts_url_and_bare_number():
    """UA #851: the evidence_ref column is a resolvable PR ref of last resort."""
    assert (
        reviewer._pr_ref_from_evidence("https://github.com/x/y/pull/42")
        == "https://github.com/x/y/pull/42"
    )
    assert reviewer._pr_ref_from_evidence("PR merged: https://github.com/x/y/pull/42 ✅") == (
        "https://github.com/x/y/pull/42"
    )
    assert reviewer._pr_ref_from_evidence("#42") == "42"
    assert reviewer._pr_ref_from_evidence("42") == "42"
    assert reviewer._pr_ref_from_evidence("PR #42") == "42"
    assert reviewer._pr_ref_from_evidence("abc123def") is None
    # #2269: trailing digits of a SHA, timestamp or prose are not a PR number.
    assert reviewer._pr_ref_from_evidence("baf94d84") is None
    assert reviewer._pr_ref_from_evidence("deployed 2026-10-29T15:59:41") is None
    assert (
        reviewer._pr_ref_from_evidence(
            "system_doctor 2026-10-29T15:59:41Z fleet_node_versions LIVE at 65e19b43fe25"
        )
        is None
    )
    assert reviewer._pr_ref_from_evidence(None) is None
    assert reviewer._pr_ref_from_evidence("") is None


def test_gh_pr_for_task_uses_explicit_title_query_and_rejects_ambiguity(monkeypatch):
    calls = []
    monkeypatch.setattr(reviewer.shutil, "which", lambda name: "/usr/bin/gh")

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout='[{"number": 1, "title": "UA #868 reconcile"}, '
            '{"number": 2, "title": "fix task-868 duplicate"}]',
        )

    monkeypatch.setattr(reviewer.subprocess, "run", run)
    assert reviewer._gh_pr_for_task(868, "/repo") is None
    argv, kwargs = calls[0]
    assert argv[argv.index("--search") + 1] == "UA #868"
    assert "number,title,url,state,mergeCommit" in argv
    assert kwargs["cwd"] == "/repo"


def test_gh_pr_merge_details_returns_url_and_merge_sha(monkeypatch):
    monkeypatch.setattr(reviewer.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(
        reviewer.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"url":"https://github.com/x/y/pull/868",'
            '"mergeCommit":{"oid":"deadbeef1234567"}}',
        ),
    )
    assert reviewer._gh_pr_merge_details("https://github.com/x/y/pull/868", "/repo") == {
        "url": "https://github.com/x/y/pull/868",
        "mergeCommit": {"oid": "deadbeef1234567"},
    }


def test_reconcile_uses_evidence_ref_when_notes_carry_no_pr(monkeypatch):
    """UA #851: Cursor's flow merges on GitHub and records the PR on
    ``evidence_ref`` but not always in the notes. The worktree is gone, the task
    is still ``waiting_review`` — reconcile must resolve the merge via
    evidence_ref instead of leaving it for the review pass to false-FAIL with
    'no git worktree found'."""
    tid = _make_review_task()
    task_update(
        tid,
        evidence_ref="https://github.com/x/y/pull/77",
        actor="test-agent",
    )
    _quiet_git(monkeypatch)
    seen = {}

    def fake_gh(ref, repo_path):
        seen["ref"] = ref
        return "MERGED"

    monkeypatch.setattr(reviewer, "_gh_pr_state", fake_gh)

    out = reviewer.reconcile_tasks()
    assert out["results"][0]["verdict"] == "merged-upstream"
    assert seen["ref"] == "https://github.com/x/y/pull/77"
    assert _get_task(tid)["status"] == "done"


def test_reconcile_evidence_ref_not_staled_when_gh_unavailable(monkeypatch):
    """Same shape, gh unavailable — a PR reference exists, so leave it alone."""
    tid = _make_review_task()
    task_update(tid, evidence_ref="https://github.com/x/y/pull/77", actor="test-agent")
    _quiet_git(monkeypatch)  # _gh_pr_state → None

    out = reviewer.reconcile_tasks()
    assert out["results"] == []
    assert _get_task(tid)["status"] == "doing"
    assert "[review: stale" not in _get_task(tid)["notes"]


def test_reconcile_markerless_waiting_review_with_merged_evidence_ref(monkeypatch):
    """UA #851: a task in ``waiting_review`` with a merged PR on evidence_ref and
    NO ``[waiting-for: review]`` marker anywhere still reconciles to done."""
    t = task_add("shipped-no-marker", notes="Implemented and merged.")
    task_update(
        t["id"],
        status="waiting_review",
        evidence_ref="https://github.com/x/y/pull/91",
        actor="test-agent",
    )
    _quiet_git(monkeypatch)
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "MERGED")

    out = reviewer.reconcile_tasks()
    assert [r["verdict"] for r in out["results"]] == ["merged-upstream"]
    assert _get_task(t["id"])["status"] == "done"


def test_reconcile_markerless_waiting_review_unmerged_is_left_alone(monkeypatch):
    """A markerless waiting_review task whose PR is NOT merged must not be
    staled or unresolved-repo'd — those are review-flow verdicts."""
    t = task_add("in-flight-no-marker", notes="Still open.")
    task_update(
        t["id"],
        status="waiting_review",
        evidence_ref="https://github.com/x/y/pull/92",
        actor="test-agent",
    )
    _quiet_git(monkeypatch)
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "OPEN")

    out = reviewer.reconcile_tasks()
    assert out["results"] == []
    assert _get_task(t["id"])["status"] == "waiting_review"
    assert "[review:" not in _get_task(t["id"])["notes"]


def test_reconcile_active_claim_is_never_reconciled_done(monkeypatch):
    """A task held by an unexpired claim remains in progress despite a merged PR."""
    from unified_agent.tools.tasks import task_claim

    t = task_add("just-doing", notes="working on it")
    task_update(t["id"], evidence_ref="https://github.com/x/y/pull/93", actor="test-agent")
    task_claim(t["id"], agent="Desktop_Agent2")
    _quiet_git(monkeypatch)
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "MERGED")

    out = reviewer.reconcile_tasks()

    assert out["results"] == []
    task = _get_task(t["id"])
    assert task["status"] == "doing"
    assert task["claimed_by"] == "Desktop_Agent2"
    assert "[review: merged-upstream" not in task["notes"]


def test_reconcile_pending_delegation_is_never_reconciled_done(monkeypatch):
    """A delegated task remains open until its pending handoff resolves."""
    t = _make_review_task()
    task_update(t, evidence_ref="https://github.com/x/y/pull/96", actor="test-agent")
    _insert_delegation(t, status="pending")
    _quiet_git(monkeypatch)
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "MERGED")

    out = reviewer.reconcile_tasks()

    assert out["results"] == []
    task = _get_task(t)
    assert task["status"] == "doing"
    assert "[review: merged-upstream" not in task["notes"]


def test_reconcile_claimed_task_with_in_flight_delegation_is_refused(monkeypatch):
    """A task with claimed_by set AND an in-flight delegation row is NOT reconciled."""
    from unified_agent.tools.tasks import task_claim

    t = task_add("claimed-with-delegation", notes="implemented")
    task_update(
        t["id"],
        evidence_ref="https://github.com/x/y/pull/94",
        actor="test-agent",
    )
    task_claim(t["id"], agent="Desktop_Agent2")
    _insert_delegation(t["id"], status="in_flight")
    _quiet_git(monkeypatch)
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "MERGED")

    out = reviewer.reconcile_tasks()

    assert out["results"] == []
    task = _get_task(t["id"])
    assert task["status"] == "doing"
    assert task["claimed_by"] == "Desktop_Agent2"
    assert "[review: merged-upstream" not in task["notes"]
    assert reviewer._mark_done_with_note(t["id"], "[review: merged-upstream]") is False
    assert _get_task(t["id"])["status"] == "doing"


def test_reconcile_task_with_in_flight_delegation_is_untouched(monkeypatch):
    t = task_add("delegation-running", notes="implemented")
    task_update(
        t["id"],
        status="waiting_review",
        evidence_ref="https://github.com/x/y/pull/94",
        actor="test-agent",
    )
    _insert_delegation(t["id"])
    _quiet_git(monkeypatch)
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "MERGED")

    out = reviewer.reconcile_tasks()

    assert out["results"] == []
    task = _get_task(t["id"])
    assert task["status"] == "waiting_review"
    assert "[review: merged-upstream" not in task["notes"]


def test_reconcile_markerless_doing_open_pr_is_untouched(monkeypatch):
    """An unmerged markerless PR must remain in-flight."""
    t = task_add("still-doing", notes="working on it")
    task_update(t["id"], status="doing", evidence_ref="#94", actor="test-agent")
    _quiet_git(monkeypatch)
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "OPEN")

    out = reviewer.reconcile_tasks()
    assert out["results"] == []
    assert _get_task(t["id"])["status"] == "doing"


def test_reconcile_markerless_doing_ancestor_without_pr_is_untouched(monkeypatch):
    """A fresh task branch already in origin/main is not merge evidence."""
    t = task_add("fresh-doing", notes="working on it")
    task_update(t["id"], status="doing", actor="test-agent")
    _quiet_git(monkeypatch)
    monkeypatch.setattr(reviewer, "_branch_merged_into_origin_main", lambda branch, repo: True)
    monkeypatch.setattr(reviewer, "_gh_pr_for_task", lambda task_id, repo: None)

    out = reviewer.reconcile_tasks()
    assert out["results"] == []
    assert _get_task(t["id"])["status"] == "doing"


def test_reconcile_markerless_doing_matches_merged_pr_title(monkeypatch):
    """A title-only task reference resolves after the worktree is gone."""
    t = task_add("title-only", notes="implemented")
    task_update(t["id"], status="doing", actor="test-agent")
    _quiet_git(monkeypatch)
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_for_task",
        lambda task_id, repo_path: {
            "url": "https://github.com/x/y/pull/95",
            "title": f"fix: task #{task_id}",
            "mergeCommit": {"oid": "abc1234"},
        },
    )
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "MERGED")

    out = reviewer.reconcile_tasks()
    assert out["results"][0]["verdict"] == "merged-upstream"
    notes = _get_task(t["id"])["notes"]
    assert "https://github.com/x/y/pull/95" in notes
    assert "merge_sha=abc1234" in notes


# ── task #494: repo scope, unresolvable repos, and no-code tasks ──────────
#
# The reconciler searched config.workspace_root() — the cockpit at ~/agent-workspace,
# which holds no product code and has no git remote — for every task's branch,
# so merged work was flagged "stale — branch missing" every 15 minutes. Third
# recurrence (#115-117, #299/#300, #484/#473); the two prior fixes addressed the
# PR-ref fallback and the status filter, never the repo scope.


def _quiet_git_per_repo(monkeypatch, worktrees_by_repo=None, fetch_log=None):
    """Like _quiet_git, but records which repo each lookup was aimed at."""
    by_repo = worktrees_by_repo or {}
    monkeypatch.setattr(
        reviewer, "_list_worktrees", lambda repo_path=None: by_repo.get(str(repo_path), [])
    )
    monkeypatch.setattr(
        reviewer,
        "_fetch_origin",
        lambda repo_path: fetch_log.append(str(repo_path)) if fetch_log is not None else None,
    )
    monkeypatch.setattr(reviewer, "_branch_exists_locally", lambda branch, repo_path: False)
    monkeypatch.setattr(
        reviewer, "_branch_merged_into_origin_main", lambda branch, repo_path: False
    )
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: None)


def test_reconcile_searches_the_project_repo_not_the_cockpit(monkeypatch, tmp_path):
    """Defect A: a unified-agent task's branch must be looked for in the
    project's own checkout, not in the (remote-less, code-less) cockpit."""
    repo = tmp_path / "unified_agent"
    repo.mkdir()
    monkeypatch.setattr(
        "unified_agent.config.project_repo_path", lambda project: repo if project else None
    )
    tid = _make_review_task(notes_extra="[code-task]")
    fetches: list[str] = []
    _quiet_git_per_repo(
        monkeypatch,
        worktrees_by_repo={
            str(repo): [
                {"path": "/tmp/main", "branch": "main"},
                {"path": f"/tmp/wt-{tid}", "branch": f"task/{tid}-fix", "head": SHA},
            ]
        },
        fetch_log=fetches,
    )
    monkeypatch.setattr(reviewer, "_branch_merged_into_origin_main", lambda branch, repo_path: True)

    out = reviewer.reconcile_tasks()

    assert [r["verdict"] for r in out["results"]] == ["merged-upstream"]
    assert _get_task(tid)["status"] == "done"
    assert fetches == [str(repo)], "fetch must target the project repo, once"


def test_reconcile_reports_unresolvable_repo_instead_of_staling(monkeypatch):
    """Defect A, loud-failure half: when no product repo can be resolved, say so.
    Both prior recurrences survived because this failure was silent — a task with
    an unknown repo was indistinguishable from one with an abandoned branch."""
    monkeypatch.setattr("unified_agent.config.project_repo_path", lambda project: None)
    tid = _make_review_task(notes_extra="[code-task]")
    _quiet_git_per_repo(monkeypatch)

    out = reviewer.reconcile_tasks()

    assert [r["verdict"] for r in out["results"]] == ["unresolved-repo"]
    notes = _get_task(tid)["notes"]
    assert "[review: unresolved-repo @" in notes
    assert "stale — branch missing" not in notes
    # idempotent: reported once, like every other reconcile verdict
    assert reviewer.reconcile_tasks()["results"] == []
    assert _get_task(tid)["notes"].count("[review: unresolved-repo") == 1


def test_reconcile_skips_branch_check_for_no_code_task_with_evidence(monkeypatch, tmp_path):
    """Defect C: #473 was reporting-only ("No code touched"), so no branch ever
    existed to find. Explicit ``[no-code]`` opts out of branch reconciliation
    (and of the subsequent review scan)."""
    repo = tmp_path / "unified_agent"
    repo.mkdir()
    monkeypatch.setattr("unified_agent.config.project_repo_path", lambda project: repo)
    tid = _make_review_task(notes_extra="[no-code]")
    task_update(tid, evidence_ref="observability_manage(metrics) queried 2026-08-20T00:22Z")
    _quiet_git_per_repo(monkeypatch)

    out = reviewer.reconcile_tasks()

    assert out["results"] == []
    assert "[review: stale" not in _get_task(tid)["notes"]
    scanned = reviewer.scan_for_reviewable_tasks()["tasks"]
    assert tid not in {t["id"] for t in scanned}


def test_reconcile_does_not_infer_no_code_from_missing_code_task_marker(monkeypatch, tmp_path):
    """Evidence without [code-task] must not skip reconcile for a named project;
    those tasks get project worktrees without needing the marker."""
    repo = tmp_path / "browser"
    repo.mkdir()
    monkeypatch.setattr(
        "unified_agent.config.project_repo_path",
        lambda project: repo if project == "browser" else None,
    )
    t = task_add(
        "named-project-review",
        notes=f"{reviewer.WAITING_FOR_REVIEW}\n[pr: https://github.com/anonb3ll/browser/pull/1]",
        project="browser",
    )
    tid = t["id"]
    task_update(tid, status="doing", evidence_ref="observability note")
    _quiet_git_per_repo(monkeypatch)
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_state",
        lambda ref, repo_path: "MERGED" if "pull/1" in str(ref) else None,
    )

    out = reviewer.reconcile_tasks()
    assert [r["verdict"] for r in out["results"]] == ["merged-upstream"]


def test_reconcile_unmarked_default_project_falls_back_to_workspace(monkeypatch, tmp_path):
    """Unmarked DEFAULT_PROJECT tasks are cockpit-sourced at claim time. Task
    #1844: reconcile looks in the product checkout first, then falls back to
    the workspace worktree claim time minted."""
    workspace = tmp_path / "agent"
    workspace.mkdir()
    product = tmp_path / "unified_agent"
    product.mkdir()
    monkeypatch.setattr("unified_agent.config.workspace_root", lambda: workspace)
    monkeypatch.setattr(
        "unified_agent.config.project_repo_path", lambda project: product if project else None
    )
    tid = _make_review_task()
    with storage.connect() as conn:
        conn.execute("UPDATE tasks SET worktree_path=? WHERE id=?", (f".worktrees/task-{tid}", tid))
        conn.commit()
    fetches: list[str] = []
    _quiet_git_per_repo(
        monkeypatch,
        worktrees_by_repo={
            str(workspace): [
                {"path": str(workspace / "main"), "branch": "main"},
                {"path": str(workspace / f"wt-{tid}"), "branch": f"task/{tid}-fix"},
            ]
        },
        fetch_log=fetches,
    )
    checked: list[str] = []

    def merged(branch, repo_path):
        checked.append(str(repo_path))
        return True

    monkeypatch.setattr(reviewer, "_branch_merged_into_origin_main", merged)

    out = reviewer.reconcile_tasks()
    assert [r["verdict"] for r in out["results"]] == ["merged-upstream"]
    assert fetches == [str(product), str(workspace)]
    assert checked == [str(workspace)]


def test_reconcile_unmarked_default_project_prefers_product_worktree(monkeypatch, tmp_path):
    """Task #1844: an unmarked default-project task whose work lives in the
    product checkout reconciles there, even with a same-named workspace
    worktree; the workspace is not consulted at all."""
    workspace = tmp_path / "agent"
    workspace.mkdir()
    product = tmp_path / "unified_agent"
    product.mkdir()
    monkeypatch.setattr("unified_agent.config.workspace_root", lambda: workspace)
    monkeypatch.setattr(
        "unified_agent.config.project_repo_path", lambda project: product if project else None
    )
    tid = _make_review_task()
    fetches: list[str] = []
    _quiet_git_per_repo(
        monkeypatch,
        worktrees_by_repo={
            str(product): [
                {"path": str(product), "branch": "main"},
                {"path": str(product / ".worktrees" / f"task-{tid}"), "branch": "t"},
            ],
            str(workspace): [
                {"path": str(workspace / ".worktrees" / f"task-{tid}"), "branch": "w"},
            ],
        },
        fetch_log=fetches,
    )
    checked: list[tuple[str, str]] = []

    def merged(branch, repo_path):
        checked.append((branch, str(repo_path)))
        return True

    monkeypatch.setattr(reviewer, "_branch_merged_into_origin_main", merged)

    out = reviewer.reconcile_tasks()
    assert [r["verdict"] for r in out["results"]] == ["merged-upstream"]
    assert fetches == [str(product)]
    assert checked == [("t", str(product))]


def test_reconcile_ignores_pr_numbered_worktree(monkeypatch, tmp_path):
    """Task #1844: `pr-<N>` names PR N, not task N. A product-sourced task
    whose id equals that PR number must not adopt the PR worktree's branch."""
    product = tmp_path / "unified_agent"
    product.mkdir()
    monkeypatch.setattr(
        "unified_agent.config.project_repo_path", lambda project: product if project else None
    )
    tid = _make_review_task(notes_extra="[code-task]")
    _quiet_git_per_repo(
        monkeypatch,
        worktrees_by_repo={
            str(product): [
                {"path": str(product), "branch": "main"},
                {"path": str(product / ".worktrees" / f"pr-{tid}"), "branch": f"pr-{tid}"},
            ]
        },
    )
    checked: list[str] = []

    def merged(branch, repo_path):
        checked.append(branch)
        return True

    monkeypatch.setattr(reviewer, "_branch_merged_into_origin_main", merged)

    out = reviewer.reconcile_tasks()
    assert checked == []
    assert [r["verdict"] for r in out["results"]] != ["merged-upstream"]
    assert _get_task(tid)["status"] == "doing"


def test_reconcile_absolute_pr_url_without_local_repo(monkeypatch):
    """When the project checkout is missing, an absolute PR URL is still
    queryable via ``gh pr view <url>``."""
    monkeypatch.setattr("unified_agent.config.project_repo_path", lambda project: None)
    tid = _make_review_task(
        notes_extra="[code-task]\n[pr: https://github.com/anonb3ll/unified-agent/pull/99]"
    )
    _quiet_git_per_repo(monkeypatch)
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_state",
        lambda ref, repo_path: "MERGED" if str(ref).startswith("http") else None,
    )

    out = reviewer.reconcile_tasks()
    assert [r["verdict"] for r in out["results"]] == ["merged-upstream"]
    assert _get_task(tid)["status"] == "done"


def test_reconcile_still_stales_a_code_task_with_no_branch(monkeypatch, tmp_path):
    """The gate must not go soft: a [code-task] with evidence but no branch and
    no PR is still flagged, because code-bearing work has something to point at."""
    repo = tmp_path / "unified_agent"
    repo.mkdir()
    monkeypatch.setattr("unified_agent.config.project_repo_path", lambda project: repo)
    tid = _make_review_task(notes_extra="[code-task]")
    task_update(tid, evidence_ref="some/branchless/claim")
    _quiet_git_per_repo(monkeypatch)

    out = reviewer.reconcile_tasks()

    assert [r["verdict"] for r in out["results"]] == ["stale"]
    assert "stale — branch missing" in _get_task(tid)["notes"]


# ── task #1338: trust exact-head CI as the pytest gate ────────────────────


def _ci_rollup_payload(head: str, shard_conclusions: list[str]) -> str:
    checks = [
        {"name": f"CI / test (shard={i})", "conclusion": c, "status": "COMPLETED"}
        for i, c in enumerate(shard_conclusions)
    ]
    return json.dumps({"headRefOid": head, "statusCheckRollup": checks})


def test_exact_head_ci_pytest_ok_requires_all_shards_success(monkeypatch):
    monkeypatch.setattr(reviewer.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run(cmd, **kwargs):
        assert "pr" in cmd and "view" in cmd
        return _GitResult(0, _ci_rollup_payload(SHA, ["SUCCESS"] * 4))

    monkeypatch.setattr(reviewer.subprocess, "run", fake_run)
    ok, detail = reviewer._exact_head_ci_pytest_ok(
        "https://github.com/anonb3ll/unified-agent/pull/7", SHA, "/tmp/wt"
    )
    assert ok is True
    assert "SUCCESS" in detail


def test_exact_head_ci_pytest_ok_accepts_github_actions_check_names(monkeypatch):
    """#1787: GitHub Actions reports the bare job name plus workflowName.

    Shape verified against `gh pr view --json statusCheckRollup` on a GitHub
    Actions-era PR head (name="test", workflowName="CI"); Depot reports
    name="CI / test (shard=0)" with no workflowName. Both satisfy the default
    "CI / test (shard=N)" requirement.
    """
    monkeypatch.setattr(reviewer.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.delenv("UA_REVIEW_CI_REQUIRED_CHECKS", raising=False)
    checks = [
        {
            "__typename": "CheckRun",
            "name": f"test (shard={i})",
            "workflowName": "CI",
            "conclusion": "SUCCESS",
            "status": "COMPLETED",
        }
        for i in range(4)
    ]
    # A same-named job in another workflow must not stand in for a CI shard.
    checks[2] = {**checks[2], "workflowName": "Nightly"}
    payload = {"headRefOid": SHA, "statusCheckRollup": checks}
    monkeypatch.setattr(
        reviewer.subprocess, "run", lambda cmd, **kw: _GitResult(0, json.dumps(payload))
    )
    ok, detail = reviewer._exact_head_ci_pytest_ok("7", SHA, None)
    assert ok is False and "CI / test (shard=2)=absent" in detail

    checks[2] = {**checks[2], "workflowName": "CI"}
    ok, detail = reviewer._exact_head_ci_pytest_ok("7", SHA, None)
    assert ok is True, detail
    assert "CI pytest shards SUCCESS" in detail


def test_exact_head_ci_pytest_ok_rejects_skipped_or_pending(monkeypatch):
    monkeypatch.setattr(reviewer.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run(cmd, **kwargs):
        return _GitResult(0, _ci_rollup_payload(SHA, ["SUCCESS", "SUCCESS", "SKIPPED", "SUCCESS"]))

    monkeypatch.setattr(reviewer.subprocess, "run", fake_run)
    ok, detail = reviewer._exact_head_ci_pytest_ok("7", SHA, None)
    assert ok is False
    assert "SKIPPED" in detail or "not SUCCESS" in detail


def test_exact_head_ci_pytest_ok_rejects_head_mismatch(monkeypatch):
    monkeypatch.setattr(reviewer.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run(cmd, **kwargs):
        return _GitResult(
            0, _ci_rollup_payload("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", ["SUCCESS"] * 4)
        )

    monkeypatch.setattr(reviewer.subprocess, "run", fake_run)
    ok, detail = reviewer._exact_head_ci_pytest_ok("7", SHA, None)
    assert ok is False
    assert "worktree" in detail


def test_validate_and_hold_skips_local_pytest_when_ci_green(green_worktree, monkeypatch):
    monkeypatch.setattr(
        reviewer,
        "_exact_head_ci_pytest_ok",
        lambda pr_ref, sha, path: (True, f"Depot pytest shards SUCCESS at {sha[:12]}"),
    )
    tid = _make_review_task(notes_extra="PR: https://github.com/anonb3ll/unified-agent/pull/7")
    res = reviewer.review_task(tid)
    assert res["verdict"] == "validated"
    assert green_worktree["skip_tests"] is True
    notes = _get_task(tid)["notes"]
    assert "[review: ci-gate @" in notes
    assert f"gates green at {SHA}" in notes


def test_validate_and_hold_runs_local_pytest_when_ci_not_green(green_worktree, monkeypatch):
    monkeypatch.delenv("UA_REVIEW_LOCAL_PYTEST")
    monkeypatch.setattr(
        reviewer,
        "_exact_head_ci_pytest_ok",
        lambda pr_ref, sha, path: (False, "required checks not SUCCESS"),
    )
    tid = _make_review_task(notes_extra="PR: https://github.com/anonb3ll/unified-agent/pull/7")
    res = reviewer.review_task(tid)
    assert res["verdict"] == "validated"
    assert green_worktree["skip_tests"] is False
    # #1787: the local fallback is targeted, never the whole suite.
    assert green_worktree["test_paths"] == ["tests/test_foo.py"]
    notes = _get_task(tid)["notes"]
    assert "[review: ci-gate @" not in notes
    assert "[review: targeted-tests @" in notes and "tests/test_foo.py" in notes
    assert "targeted subset" in notes and "batch CI (the full suite) is still required" in notes


# ── task #1787: batch CI means most PR heads have no shards ───────────────


def test_missing_ci_shards_never_run_the_full_suite_on_the_hub(green_worktree, monkeypatch):
    """No shards and no mapped tests: hold as awaiting-ci, run no pytest at all."""
    monkeypatch.delenv("UA_REVIEW_LOCAL_PYTEST", raising=False)
    monkeypatch.setattr(
        reviewer,
        "_exact_head_ci_pytest_ok",
        lambda pr_ref, sha, path: (
            False,
            "required checks not SUCCESS: CI / test (shard=0)=absent",
        ),
    )
    monkeypatch.setattr(
        reviewer,
        "_diff_for_targeted_tests",
        lambda wt_path, branch: [{"file": "docs/notes.md", "status": "M"}],
    )
    tid = _make_review_task(notes_extra="PR: https://github.com/anonb3ll/unified-agent/pull/7")
    res = reviewer.review_task(tid)
    assert res["verdict"] == "awaiting-ci"
    assert green_worktree["gates"] == 0, "no gate (and so no pytest) may run"
    notes = _get_task(tid)["notes"]
    assert "[review: awaiting-ci @" in notes and SHA in notes
    assert "ci:full" in notes
    # The next scan at the same sha is silent: no second note, no notification.
    again = reviewer.review_task(tid)
    assert again["verdict"] == "unchanged" and again["notification"] is None
    assert _get_task(tid)["notes"].count("[review: awaiting-ci @") == 1


def test_targeted_cap_holds_instead_of_running_a_near_full_suite(green_worktree, monkeypatch):
    monkeypatch.delenv("UA_REVIEW_LOCAL_PYTEST")
    monkeypatch.setenv("UA_REVIEW_TARGETED_MAX_FILES", "1")
    monkeypatch.setattr(
        reviewer, "_exact_head_ci_pytest_ok", lambda pr_ref, sha, path: (False, "absent")
    )
    wt_tests = Path(green_worktree["path"]) / "tests"
    (wt_tests / "test_bar.py").write_text("def test_ok():\n    pass\n")
    monkeypatch.setattr(
        reviewer,
        "_diff_for_targeted_tests",
        lambda wt_path, branch: [
            {"file": "src/foo.py", "status": "M"},
            {"file": "tests/test_bar.py", "status": "A"},
        ],
    )
    tid = _make_review_task(notes_extra="PR: https://github.com/anonb3ll/unified-agent/pull/7")
    res = reviewer.review_task(tid)
    assert res["verdict"] == "awaiting-ci"
    assert green_worktree["gates"] == 0
    assert "exceed the targeted cap of 1" in _get_task(tid)["notes"]


@pytest.mark.parametrize("changed", ["docs/notes.md", "src/foo.py"])
def test_task_without_a_pr_keeps_the_whole_suite(green_worktree, monkeypatch, changed):
    """No PR means no CI or batch will ever cover it: keep full coverage (#1787 review)."""
    monkeypatch.delenv("UA_REVIEW_LOCAL_PYTEST")
    monkeypatch.setattr(
        reviewer,
        "_diff_for_targeted_tests",
        lambda wt_path, branch: [{"file": changed, "status": "M"}],
    )
    tid = _make_review_task()
    res = reviewer.review_task(tid)
    assert res["verdict"] == "validated"
    assert green_worktree["test_paths"] is None


def test_targeted_test_paths_follow_renames(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_new_name.py").write_text("")
    (tmp_path / "tests" / "test_widget.py").write_text("")
    changed = [
        {"file": "tests/test_old_name.py\ttests/test_new_name.py", "status": "R087"},
        {"file": "src/unified_agent/gadget.py\tsrc/unified_agent/widget.py", "status": "R100"},
    ]
    assert reviewer._targeted_test_paths(str(tmp_path), changed) == [
        "tests/test_new_name.py",
        "tests/test_widget.py",
    ]


def test_skipped_duplicate_check_cannot_revoke_or_fake_ci_green(monkeypatch):
    """A later non-ci:full PR event adds skipped checks with the same names (#1787)."""
    monkeypatch.setattr(reviewer.shutil, "which", lambda name: "/usr/bin/gh")
    checks = [
        {"name": f"test (shard={i})", "workflowName": "CI", "conclusion": "SUCCESS"}
        for i in range(4)
    ] + [
        {"name": f"test (shard={i})", "workflowName": "CI", "conclusion": "SKIPPED"}
        for i in range(4)
    ]
    payload = {"headRefOid": SHA, "statusCheckRollup": checks}
    monkeypatch.setattr(
        reviewer.subprocess, "run", lambda cmd, **kw: _GitResult(0, json.dumps(payload))
    )
    assert reviewer._exact_head_ci_pytest_ok("7", SHA, None)[0] is True
    checks[1] = {**checks[1], "conclusion": "FAILURE"}
    assert reviewer._exact_head_ci_pytest_ok("7", SHA, None)[0] is False


def test_ci_definitions_are_red_tier():
    from unified_agent.cycle_eligibility import _is_red_tier

    files = [
        ".github/workflows/ci.yml",
        ".depot/workflows/ci.yml",
        "scripts/ci_changes.py",
        "docs/ci-batch-verify.md",
        "src/unified_agent/reviewer.py",
    ]
    assert _is_red_tier([{"file": f} for f in files]) == files[:3]


def test_test_selection_classifier_is_red_tier():
    """Task #2274: the module that decides which tests run against a PR's diff
    must not be editable without escalation, whichever path it is reached by."""
    from unified_agent.cycle_eligibility import _is_red_tier

    files = [
        "src/unified_agent/affected_tests.py",
        "scripts/affected_tests.py",
        "src/unified_agent/cycle_eligibility.py",
        "tests/test_affected_tests.py",
    ]
    assert _is_red_tier([{"file": f} for f in files]) == files[:3]


def test_security_code_is_red_tier_but_security_prose_is_not():
    """Task #2275: red tier follows the boundary, not the word. A changelog
    fragment or a docs page named for security is prose; #1343 carried one and
    was escalated for it while the gate code below was not."""
    from unified_agent.cycle_eligibility import _is_red_tier

    red = [
        "SECURITY.md",
        "src/unified_agent/security/keys.py",
        "src/unified_agent/security.py",
        "src/unified_agent/redaction_security.py",
        "tests/test_security_posture.py",
        "src/unified_agent/safety.py",
    ]
    prose = [
        "changelog.d/2255.security.md",
        "docs/security.md",
        "docs/security-posture.md",
        "docs/diagrams/control-security-layers.html",
    ]
    assert _is_red_tier([{"file": f} for f in red]) == red
    assert _is_red_tier([{"file": f} for f in prose]) == []


def test_authority_surfaces_are_red_tier_except_the_profile_surface():
    """Task #2277: the policy gate and the audit's rule set escalate. profiles.py
    does not, deliberately: it takes ~50 commits in two months, and a path
    pattern would put an operator approval on nearly every PR that touches the
    tool surface. The change that matters there is widening a profile, which is
    a diff-content check, not a path match. If this test fails because someone
    added profiles.py to the patterns, read #2277 first."""
    from unified_agent.cycle_eligibility import _is_red_tier

    assert _is_red_tier(
        [{"file": f} for f in ("src/unified_agent/policy.py", "src/unified_agent/audit/rules.py")]
    ) == ["src/unified_agent/policy.py", "src/unified_agent/audit/rules.py"]
    # The injection phrases moved out of audit/rules.py into the kernel (#2450);
    # the file that now holds them escalates the same way.
    assert _is_red_tier([{"file": "src/unified_agent/injection_patterns.py"}]) == [
        "src/unified_agent/injection_patterns.py"
    ]
    # Not the neighbours, and not the profile surface.
    assert (
        _is_red_tier(
            [
                {"file": f}
                for f in (
                    "src/unified_agent/profiles.py",
                    "src/unified_agent/audit/engine.py",
                    "tests/test_policy.py",
                )
            ]
        )
        == []
    )


def test_full_scope_opt_in_keeps_the_old_whole_suite_run(green_worktree, monkeypatch):
    monkeypatch.setenv("UA_REVIEW_LOCAL_PYTEST", "full")
    monkeypatch.setattr(
        reviewer, "_exact_head_ci_pytest_ok", lambda pr_ref, sha, path: (False, "absent")
    )
    tid = _make_review_task(notes_extra="PR: https://github.com/anonb3ll/unified-agent/pull/7")
    res = reviewer.review_task(tid)
    assert res["verdict"] == "validated"
    assert green_worktree["test_paths"] is None


def test_targeted_test_paths_map_sources_and_keep_changed_tests(tmp_path):
    (tmp_path / "tests" / "connectors").mkdir(parents=True)
    for rel in (
        "tests/test_reviewer.py",
        "tests/test_merge.py",
        "tests/connectors/test_slack_chat.py",
        "tests/test_ci_changes.py",
        "tests/test_tools.py",
    ):
        (tmp_path / rel).write_text("")
    changed = [
        {"file": "src/unified_agent/reviewer.py", "status": "M"},
        {"file": "src/unified_agent/slack_chat.py", "status": "M"},
        {"file": "src/unified_agent/tools/__init__.py", "status": "M"},
        {"file": "scripts/ci_changes.py", "status": "M"},
        {"file": "tests/test_merge.py", "status": "M"},
        {"file": "tests/test_gone.py", "status": "D"},
        {"file": "src/unified_agent/nothing_maps.py", "status": "A"},
        {"file": "docs/development.md", "status": "M"},
    ]
    assert reviewer._targeted_test_paths(str(tmp_path), changed) == [
        "tests/connectors/test_slack_chat.py",
        "tests/test_ci_changes.py",
        "tests/test_merge.py",
        "tests/test_reviewer.py",
        "tests/test_tools.py",
    ]


def test_targeted_test_paths_follow_the_import_graph(tmp_path):
    """Task #2231: importers are selected, not only tests/test_<stem>.py."""
    files = {
        "src/unified_agent/__init__.py": "",
        "src/unified_agent/widget.py": "VALUE = 1\n",
        "tests/test_uses_widget.py": "from unified_agent.widget import VALUE\n",
        "tests/test_unrelated.py": "import json\n",
        "tests/test_profiles.py": "",  # an always-run inventory test
    }
    for rel, body in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body)
    changed = [{"file": "src/unified_agent/widget.py", "status": "M"}]
    assert reviewer._targeted_test_paths(str(tmp_path), changed) == [
        "tests/test_profiles.py",
        "tests/test_uses_widget.py",
    ]
    # Inventory tests ride along only when something maps: a diff with no tests
    # of its own still waits for batch CI.
    docs_only = [{"file": "docs/notes.md", "status": "M"}]
    assert reviewer._targeted_test_paths(str(tmp_path), docs_only) == []


def test_targeted_test_plan_reports_what_it_cannot_read(tmp_path):
    """Review round 3 (#2231): unresolved imports reach the plan, not just a
    control test with no reason."""
    files = {
        "src/unified_agent/__init__.py": "",
        "src/unified_agent/widget.py": "",
        "src/unified_agent/plugins.py": (
            "import importlib\ndef load(n):\n    return importlib.import_module(n)\n"
        ),
        "tests/test_uses_widget.py": "from unified_agent import widget\n",
        "tests/test_computed.py": (
            "import importlib\ndef test_x(n):\n    importlib.import_module(name=n)\n"
        ),
        # Scans every src file as text: it runs on a src change but maps nothing.
        "tests/test_tree_scan.py": (
            "from pathlib import Path\ndef test_s():\n    list(Path('src').rglob('*.py'))\n"
        ),
    }
    for rel, body in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body)
    changed = [{"file": "src/unified_agent/widget.py", "status": "M"}]
    paths, full_reasons, blind_spots = reviewer._targeted_test_plan(str(tmp_path), changed)
    assert paths == [
        "tests/test_computed.py",
        "tests/test_tree_scan.py",
        "tests/test_uses_widget.py",
    ]
    assert full_reasons == []
    assert any("tests/test_computed.py" in spot for spot in blind_spots)
    assert any(spot.startswith("src/unified_agent/plugins.py") for spot in blind_spots)
    # A test selected only because its imports cannot be read is not a mapping:
    # a diff with nothing of its own still waits for batch CI, with the reason.
    other = [{"file": "src/unified_agent/unused.py", "status": "A"}]
    paths, full_reasons, blind_spots = reviewer._targeted_test_plan(str(tmp_path), other)
    assert paths == [] and full_reasons == [] and blind_spots


def test_diff_needing_the_full_suite_holds_as_awaiting_ci(green_worktree, monkeypatch):
    monkeypatch.delenv("UA_REVIEW_LOCAL_PYTEST", raising=False)
    monkeypatch.setattr(
        reviewer, "_exact_head_ci_pytest_ok", lambda pr_ref, sha, path: (False, "absent")
    )
    monkeypatch.setattr(
        reviewer,
        "_diff_for_targeted_tests",
        lambda wt_path, branch: [
            {"file": "src/foo.py", "status": "M"},
            {"file": "uv.lock", "status": "M"},
        ],
    )
    tid = _make_review_task(notes_extra="PR: https://github.com/example-org/example-repo/pull/7")
    res = reviewer.review_task(tid)
    assert res["verdict"] == "awaiting-ci"
    assert green_worktree["gates"] == 0
    notes = _get_task(tid)["notes"]
    assert "needs the full suite (uv.lock" in notes
    # Review finding (#2231): a targeted selection never reads as a certification.
    assert "not a certification; batch CI (the full suite) is still required" in notes


def test_validate_and_hold_runs_local_pytest_without_pr(green_worktree, monkeypatch):
    called = {"ci": 0}

    def boom(*a, **k):
        called["ci"] += 1
        raise AssertionError("CI gate should not run without a PR ref")

    monkeypatch.setattr(reviewer, "_exact_head_ci_pytest_ok", boom)
    tid = _make_review_task()
    res = reviewer.review_task(tid)
    assert res["verdict"] == "validated"
    assert green_worktree["skip_tests"] is False
    assert called["ci"] == 0


def test_validate_and_hold_runs_local_pytest_for_bun_worktree(
    green_worktree, monkeypatch, tmp_path
):
    """Devin #1338: Depot pytest shards must not skip Bun suites."""
    bun_wt = tmp_path / "bun-wt"
    bun_wt.mkdir()
    (bun_wt / "package.json").write_text(
        json.dumps({"scripts": {"test": "bun test tests/"}}), encoding="utf-8"
    )
    wt = {"path": str(bun_wt), "branch": "fix/bun", "head": SHA}
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr(reviewer, "_head_sha", lambda wt_path: SHA)
    monkeypatch.setattr(
        reviewer,
        "_exact_head_ci_pytest_ok",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("CI must not be consulted for Bun")),
    )
    tid = _make_review_task(notes_extra="PR: https://github.com/anonb3ll/unified-agent/pull/7")
    res = reviewer.review_task(tid)
    assert res["verdict"] == "validated"
    assert green_worktree["skip_tests"] is False


def test_validate_and_hold_refuses_ci_skip_without_bun_for_vitest(monkeypatch, tmp_path):
    """Devin #1338: missing Bun + nonempty package test script must not CI-skip.

    Mirrors merge._run_tests exit-127 path: local gates fail closed; do not
    paper over that with a green_worktree mock that always validates.
    """
    wt_dir = tmp_path / "vitest-wt"
    wt_dir.mkdir()
    (wt_dir / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    wt = {"path": str(wt_dir), "branch": "fix/vitest", "head": SHA}
    calls: dict = {"gates": 0, "skip_tests": None}

    def failing_gates(task_id, wt_path, force=False, *, skip_tests=False):
        calls["gates"] += 1
        calls["skip_tests"] = skip_tests
        # Same failure merge._run_tests raises when Bun is missing for a
        # nonempty package.json test script (CompletedProcess returncode 127).
        raise RuntimeError(
            "Validation checks failed: Unit tests failed: Bun is required by "
            "package.json test script"
        )

    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (wt, "/tmp/main"))
    monkeypatch.setattr("unified_agent.merge.run_validation_gates", failing_gates)
    monkeypatch.setattr(reviewer, "_head_sha", lambda wt_path: SHA)
    monkeypatch.setattr(reviewer.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        reviewer,
        "_exact_head_ci_pytest_ok",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("CI must not skip missing-Bun")),
    )
    tid = _make_review_task(notes_extra="PR: https://github.com/anonb3ll/unified-agent/pull/7")
    res = reviewer.review_task(tid)
    assert res["verdict"] == "failed"
    assert "Bun is required" in (res["notification"] or "")
    assert calls["gates"] == 1
    assert calls["skip_tests"] is False


def test_validate_and_hold_ci_rescues_timeout_cap(green_worktree, monkeypatch):
    """Devin #1338: exact-head green CI must validate even after local timeout retries."""
    monkeypatch.setattr(config, "review_timeout_max_retries", lambda: 2)
    notes_extra = (
        "PR: https://github.com/anonb3ll/unified-agent/pull/7\n"
        f"[review: timeout @ 2026-09-23T00:00:00Z] at {SHA}: pytest timed out — will retry\n"
        f"[review: timeout @ 2026-09-23T00:15:00Z] at {SHA}: pytest timed out — will retry"
    )
    monkeypatch.setattr(
        reviewer,
        "_exact_head_ci_pytest_ok",
        lambda pr_ref, sha, path: (True, f"Depot pytest shards SUCCESS at {sha[:12]}"),
    )
    tid = _make_review_task(notes_extra=notes_extra)
    res = reviewer.review_task(tid)
    assert res["verdict"] == "validated"
    assert green_worktree["skip_tests"] is True


def test_ci_pytest_skip_eligible_matches_run_tests(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    assert reviewer._ci_pytest_skip_eligible(str(root)) is True
    (root / "package.json").write_text(
        json.dumps({"scripts": {"test": "bun test"}}), encoding="utf-8"
    )
    assert reviewer._ci_pytest_skip_eligible(str(root)) is False
    (root / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    monkeypatch.setattr(reviewer.shutil, "which", lambda name: None)
    assert reviewer._ci_pytest_skip_eligible(str(root)) is False
    monkeypatch.setattr(reviewer.shutil, "which", lambda name: "/usr/bin/bun")
    assert reviewer._ci_pytest_skip_eligible(str(root)) is True


def test_run_validation_gates_skip_tests_still_audits(monkeypatch, tmp_path):
    from unified_agent import merge

    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setattr(merge, "resolve_task_worktree", lambda p: wt)
    monkeypatch.setattr(
        merge,
        "_run_git_in_repo",
        lambda args, cwd: _GitResult(0, ""),
    )
    monkeypatch.setattr(merge, "_run_audit", lambda p: {"status": "pass", "findings": []})

    def boom(_cwd):
        raise AssertionError("local pytest must not run when skip_tests=True")

    monkeypatch.setattr(merge, "_run_tests", boom)
    out = merge.run_validation_gates(1, str(wt), skip_tests=True)
    assert out["success"] is True
    assert out["tests_skipped"] is True
    assert out["tests_passed"] is True


def test_review_trust_ci_config_defaults(monkeypatch):
    monkeypatch.delenv("UA_REVIEW_TRUST_CI", raising=False)
    monkeypatch.delenv("UA_REVIEW_CI_REQUIRED_CHECKS", raising=False)
    assert config.review_trust_ci() is True
    assert config.review_ci_required_checks() == [
        "CI / test (shard=0)",
        "CI / test (shard=1)",
        "CI / test (shard=2)",
        "CI / test (shard=3)",
    ]
    monkeypatch.setenv("UA_REVIEW_TRUST_CI", "0")
    assert config.review_trust_ci() is False
    monkeypatch.setenv("UA_REVIEW_CI_REQUIRED_CHECKS", "CI / test (shard=0):CI / test (shard=1)")
    assert config.review_ci_required_checks() == [
        "CI / test (shard=0)",
        "CI / test (shard=1)",
    ]


# ── task #1333: NoneType mergeCommit + merged-PR worktree false-reject ────


def test_merge_commit_oid_tolerates_null_and_none_metadata():
    """gh returns mergeCommit:null; chained .get used to raise NoneType."""
    assert reviewer._merge_commit_oid(None) is None
    assert reviewer._merge_commit_oid({"mergeCommit": None}) is None
    assert reviewer._merge_commit_oid({"mergeCommit": {}}) is None
    assert reviewer._merge_commit_oid({"mergeCommit": {"oid": None}}) is None
    assert reviewer._merge_commit_oid({"mergeCommit": {"oid": "deadbeef"}}) == "deadbeef"


def test_reconcile_tolerates_null_merge_commit_metadata(monkeypatch):
    """task #1333: null mergeCommit must mark merged-upstream, not abort."""
    tid = _make_review_task()
    task_update(
        tid,
        evidence_ref="https://github.com/x/y/pull/715",
        actor="test-agent",
    )
    _quiet_git(monkeypatch)
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "MERGED")
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_merge_details",
        lambda ref, repo_path: {
            "url": "https://github.com/x/y/pull/715",
            "mergeCommit": None,
        },
    )

    out = reviewer.reconcile_tasks()

    assert out["results"][0]["verdict"] == "merged-upstream"
    assert _get_task(tid)["status"] == "done"
    assert "[review: merged-upstream" in _get_task(tid)["notes"]


def test_reconcile_tolerates_none_merge_details_lookup(monkeypatch):
    """task #1333: a None PR-details lookup must coerce to {} and continue."""
    tid = _make_review_task()
    task_update(
        tid,
        evidence_ref="https://github.com/x/y/pull/715",
        actor="test-agent",
    )
    _quiet_git(monkeypatch)
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "MERGED")
    monkeypatch.setattr(reviewer, "_gh_pr_merge_details", lambda ref, repo_path: None)

    out = reviewer.reconcile_tasks()

    assert out["results"][0]["verdict"] == "merged-upstream"
    assert _get_task(tid)["status"] == "done"


def test_reconcile_skips_row_on_lookup_error_continues_others(monkeypatch):
    """task #1333: one row's NoneType crash must not abort the whole step."""
    bad = _make_review_task(title="bad-row")
    good = _make_review_task(title="good-row")
    task_update(
        good,
        evidence_ref="https://github.com/x/y/pull/99",
        actor="test-agent",
    )
    _quiet_git(monkeypatch)
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "MERGED")

    real_one = reviewer._reconcile_one_task

    def flaky(row, **kwargs):
        if row["id"] == bad:
            raise TypeError("'NoneType' object has no attribute 'get'")
        return real_one(row, **kwargs)

    monkeypatch.setattr(reviewer, "_reconcile_one_task", flaky)

    out = reviewer.reconcile_tasks()

    assert all(r["task_id"] != bad for r in out["results"])
    assert any(r["task_id"] == good and r["verdict"] == "merged-upstream" for r in out["results"])
    assert _get_task(good)["status"] == "done"
    assert _get_task(bad)["status"] == "doing"


def test_review_missing_worktree_merged_evidence_ref_marks_done(monkeypatch):
    """task #1333 / #1297: merged PR on evidence_ref must not false-reject.

    Repro shape from the 2026-09-23 scan: reconcile aborted, worktree gone,
    review pass emitted ``no git worktree found for task 1297`` even though
    the PR was already merged on GitHub.
    """
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    tid = _make_review_task()
    task_update(
        tid,
        evidence_ref="https://github.com/x/y/pull/715",
        actor="test-agent",
    )
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "MERGED")
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_merge_details",
        lambda ref, repo_path: {
            "url": "https://github.com/x/y/pull/715",
            "mergeCommit": {"oid": "abc123def456"},
        },
    )

    res = reviewer.review_task(tid)

    assert res["verdict"] == "merged-upstream"
    assert "no git worktree found" not in (res["notification"] or "")
    task = _get_task(tid)
    assert task["status"] == "done"
    assert "[review: merged-upstream" in task["notes"]
    assert "pull/715" in task["notes"]


def test_review_missing_worktree_merged_pr_marker_in_notes_marks_done(monkeypatch):
    """Same defense when the PR lives in a trusted ``[pr: …]`` note line."""
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    tid = _make_review_task(notes_extra="[pr: https://github.com/x/y/pull/88]")
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "MERGED")
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_merge_details",
        lambda ref, repo_path: {"url": "https://github.com/x/y/pull/88", "mergeCommit": None},
    )

    res = reviewer.review_task(tid)

    assert res["verdict"] == "merged-upstream"
    assert _get_task(tid)["status"] == "done"
    assert "no git worktree found" not in (_get_task(tid)["notes"] or "")


def test_review_merged_pr_fallback_skips_active_claim(monkeypatch):
    """Devin #756: live claim must not be closed by the no-worktree fallback."""
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    tid = _make_review_task()
    task_update(
        tid,
        evidence_ref="https://github.com/x/y/pull/715",
        actor="test-agent",
    )
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET claimed_by=?, claimed_at=?, lock_expires_at=? WHERE id=?",
            ("Desktop_Agent4", storage.now(), future, tid),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "MERGED")
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_merge_details",
        lambda ref, repo_path: {"url": "https://github.com/x/y/pull/715", "mergeCommit": None},
    )

    res = reviewer.review_task(tid)

    assert res["verdict"] == "unchanged"
    assert _get_task(tid)["status"] == "doing"
    assert "merged-upstream" not in (_get_task(tid)["notes"] or "")


def test_merged_pr_fallback_needs_repo_for_bare_number(monkeypatch, tmp_path):
    """Bare PR #N without a project checkout must not query via process cwd."""
    repo = tmp_path / "unified_agent"
    repo.mkdir()
    monkeypatch.setattr("unified_agent.config.project_repo_path", lambda project: repo)
    seen: list[object] = []

    def capture_state(ref, repo_path):
        seen.append(repo_path)
        return "MERGED"

    monkeypatch.setattr(reviewer, "_gh_pr_state", capture_state)
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_merge_details",
        lambda ref, repo_path: {"url": "https://github.com/x/y/pull/715", "mergeCommit": None},
    )

    assert reviewer._merged_pr_fallback(1, "t", "[pr: #715]", None, project=None) is None
    assert seen == []
    out = reviewer._merged_pr_fallback(
        1, "t", "[code-task]\n[pr: #715]", None, project="unified-agent"
    )
    assert out is not None
    assert seen == [str(repo)]


def test_merged_pr_fallback_uses_workspace_for_unmarked_default(monkeypatch, tmp_path):
    """Devin #756: unmarked default-project bare PR uses workspace checkout."""
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    monkeypatch.setattr("unified_agent.config.workspace_root", lambda: ws)
    monkeypatch.setattr("unified_agent.config.project_repo_path", lambda project: None)
    seen: list[object] = []

    def capture_state(ref, repo_path):
        seen.append(repo_path)
        return "MERGED"

    monkeypatch.setattr(reviewer, "_gh_pr_state", capture_state)
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_merge_details",
        lambda ref, repo_path: {"url": "https://github.com/x/y/pull/59", "mergeCommit": None},
    )

    out = reviewer._merged_pr_fallback(
        1, "t", "[waiting-for: review]\n[pr: #59]", None, project="unified-agent"
    )
    assert out is not None
    assert seen == [str(ws)]


def test_merged_pr_fallback_unmarked_default_keeps_workspace_beside_product(monkeypatch, tmp_path):
    """PR #1000 review: an unmarked default-project task may be a cockpit task;
    a bare PR number must not close it against a same-numbered product PR."""
    monkeypatch.setattr(reviewer, "_gh_pr_names_task", lambda ref, repo, task_id: False)
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    product = tmp_path / "product"
    product.mkdir()
    monkeypatch.setattr("unified_agent.config.workspace_root", lambda: ws)
    monkeypatch.setattr("unified_agent.config.project_repo_path", lambda project: product)
    seen: list[object] = []

    def capture_state(ref, repo_path):
        seen.append(repo_path)
        return "MERGED"

    monkeypatch.setattr(reviewer, "_gh_pr_state", capture_state)
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_merge_details",
        lambda ref, repo_path: {"url": "https://github.com/x/y/pull/59", "mergeCommit": None},
    )

    out = reviewer._merged_pr_fallback(
        1, "t", "[waiting-for: review]\n[pr: #59]", None, project="unified-agent"
    )
    assert out is not None
    assert seen == [str(ws)]


def test_merged_pr_fallback_unmarked_task_uses_product_when_pr_names_it(monkeypatch, tmp_path):
    """PR #1000 review: an unmarked task's bare PR resolves in the product repo
    when that PR names the task (branch or title); the workspace otherwise."""
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    product = tmp_path / "product"
    product.mkdir()
    monkeypatch.setattr("unified_agent.config.workspace_root", lambda: ws)
    monkeypatch.setattr("unified_agent.config.project_repo_path", lambda project: product)
    monkeypatch.setattr(
        reviewer, "_gh_pr_names_task", lambda ref, repo, task_id: repo == str(product.resolve())
    )
    seen: list[object] = []

    def capture_state(ref, repo_path):
        seen.append(repo_path)
        return "MERGED"

    monkeypatch.setattr(reviewer, "_gh_pr_state", capture_state)
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_merge_details",
        lambda ref, repo_path: {"url": "https://github.com/x/y/pull/59", "mergeCommit": None},
    )
    out = reviewer._merged_pr_fallback(1, "t", "[pr: #59]", None, project="unified-agent")
    assert out is not None
    assert seen == [str(product.resolve())]


@pytest.mark.parametrize(
    ("head", "title", "expected"),
    [
        ("task/77-fix", "fix a thing", True),
        ("codex/77-fix", "fix a thing", True),
        ("feature-x", "fix a thing (#77)", True),
        ("feature-x", "fix a thing (#778)", False),
        ("pr-77", "unrelated", False),
    ],
)
def test_gh_pr_names_task(monkeypatch, head, title, expected):
    import json as _json
    import subprocess as _sp

    monkeypatch.setattr(reviewer.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(
        reviewer.subprocess,
        "run",
        lambda *a, **k: _sp.CompletedProcess(
            a, 0, stdout=_json.dumps({"headRefName": head, "title": title}), stderr=""
        ),
    )
    assert reviewer._gh_pr_names_task("77", "/repo", 77) is expected


def test_merged_pr_fallback_code_task_uses_product_checkout(monkeypatch, tmp_path):
    """Task #1844: a product-sourced task resolves its bare PR in its own repo."""
    product = tmp_path / "product"
    product.mkdir()
    monkeypatch.setattr("unified_agent.tools.tasks._worktree_source_repo", lambda project: product)
    seen: list[object] = []

    def capture_state(ref, repo_path):
        seen.append(repo_path)
        return "MERGED"

    monkeypatch.setattr(reviewer, "_gh_pr_state", capture_state)
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_merge_details",
        lambda ref, repo_path: {"url": "https://github.com/x/y/pull/59", "mergeCommit": None},
    )

    out = reviewer._merged_pr_fallback(
        1, "t", "[code-task]\n[pr: #59]", None, project="unified-agent"
    )
    assert out is not None
    assert seen == [str(product.resolve())]


def test_origin_fallback_product_task_ignores_workspace_branch(monkeypatch, tmp_path):
    """PR #1000 review: a same-named branch on the cockpit's origin must not
    validate a product-sourced task; only its own repo's origin counts."""
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    product = tmp_path / "product"
    product.mkdir()
    monkeypatch.setattr("unified_agent.config.workspace_root", lambda: ws)
    monkeypatch.setattr("unified_agent.tools.tasks._worktree_source_repo", lambda project: product)
    asked: list[str] = []

    def on_origin(branch, repo):
        asked.append(repo)
        return repo == str(ws)

    monkeypatch.setattr(reviewer, "_branch_exists_on_origin", on_origin)
    notes = "[code-task] branch task/1640-x"
    assert reviewer._origin_branch_fallback(1640, notes, project="unified-agent") is None
    assert asked == [str(product.resolve())]


def test_review_merged_pr_fallback_dry_run_respects_active_claim(monkeypatch):
    """Devin #756: dry-run must not report merged-upstream under a live claim."""
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    tid = _make_review_task()
    task_update(
        tid,
        evidence_ref="https://github.com/x/y/pull/715",
        actor="test-agent",
    )
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET claimed_by=?, claimed_at=?, lock_expires_at=? WHERE id=?",
            ("Desktop_Agent4", storage.now(), future, tid),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(reviewer, "_gh_pr_state", lambda ref, repo_path: "MERGED")
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_merge_details",
        lambda ref, repo_path: {"url": "https://github.com/x/y/pull/715", "mergeCommit": None},
    )

    res = reviewer.review_task(tid, dry_run=True)

    assert res["verdict"] == "unchanged"
    assert _get_task(tid)["status"] == "doing"


def test_pr_ref_for_review_scopes_markers_to_live_cycle():
    """Devin #756: live cycle prefers post-waiting markers / evidence, not history."""
    evidence = "https://github.com/x/y/pull/99"
    # No marker after waiting-for → evidence_ref (even if older [pr:] precedes it).
    notes_old_pr = "[pr: https://github.com/x/y/pull/10]\n[waiting-for: review]"
    assert reviewer._pr_ref_for_review(notes_old_pr, evidence) == evidence
    # Merged-upstream only before waiting-for → evidence_ref.
    notes_mu = (
        "[review: merged-upstream @ 2026-09-20T00:00:00Z] PR "
        "https://github.com/x/y/pull/10\n"
        "[waiting-for: review]"
    )
    assert reviewer._pr_ref_for_review(notes_mu, evidence) == evidence
    # Newer [pr:] after waiting-for beats stale evidence_ref.
    notes_with_pr = (
        "[pr: https://github.com/x/y/pull/10]\n"
        "[waiting-for: review]\n"
        "[pr: https://github.com/x/y/pull/99]"
    )
    assert reviewer._pr_ref_for_review(notes_with_pr, "https://github.com/x/y/pull/10") == (
        "https://github.com/x/y/pull/99"
    )
    # Without a live marker, the merged-upstream note remains authoritative.
    prior = "[review: merged-upstream @ 2026-09-20T00:00:00Z] PR https://github.com/x/y/pull/10"
    assert reviewer._pr_ref_for_review(prior, evidence) == "https://github.com/x/y/pull/10"


def test_review_reopened_after_merged_upstream_checks_new_pr(monkeypatch):
    """Devin #756: old merged PR must not close a reopened task whose new PR is open."""
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    tid = _make_review_task(
        notes_extra=(
            "[review: merged-upstream @ 2026-09-20T00:00:00Z] PR "
            "https://github.com/x/y/pull/10\n"
            "[waiting-for: review]"
        )
    )
    task_update(
        tid,
        evidence_ref="https://github.com/x/y/pull/99",
        actor="test-agent",
    )

    def _state(ref, repo_path):
        ref_s = str(ref)
        if "pull/10" in ref_s or ref_s.endswith("/10") or ref_s == "10":
            return "MERGED"
        if "pull/99" in ref_s or ref_s.endswith("/99") or ref_s == "99":
            return "OPEN"
        return None

    seen: list[str] = []

    def _state_tracking(ref, repo_path):
        seen.append(str(ref))
        return _state(ref, repo_path)

    monkeypatch.setattr(reviewer, "_gh_pr_state", _state_tracking)
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_merge_details",
        lambda ref, repo_path: {
            "url": str(ref)
            if str(ref).startswith("http")
            else f"https://github.com/x/y/pull/{ref}",
            "mergeCommit": {"oid": "abc"},
        },
    )

    res = reviewer.review_task(tid)

    # New PR is still open — must not mark done via the old merged PR.
    assert res["verdict"] != "merged-upstream"
    assert _get_task(tid)["status"] != "done"
    assert any("99" in s for s in seen)
    assert not any(s.endswith("/10") or s.endswith("pull/10") for s in seen)


def test_review_reopened_ignores_pr_marker_before_live_waiting(monkeypatch):
    """Devin #756: [pr:] before the live waiting-for must not beat updated evidence_ref."""
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    tid = _make_review_task(
        notes_extra=("[pr: https://github.com/x/y/pull/10]\n[waiting-for: review]")
    )
    task_update(
        tid,
        evidence_ref="https://github.com/x/y/pull/99",
        actor="test-agent",
    )
    seen: list[str] = []

    def _state(ref, repo_path):
        seen.append(str(ref))
        ref_s = str(ref)
        if "pull/10" in ref_s:
            return "MERGED"
        if "pull/99" in ref_s:
            return "OPEN"
        return None

    monkeypatch.setattr(reviewer, "_gh_pr_state", _state)
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_merge_details",
        lambda ref, repo_path: {"url": str(ref), "mergeCommit": {"oid": "abc"}},
    )
    res = reviewer.review_task(tid)
    assert res["verdict"] != "merged-upstream"
    assert _get_task(tid)["status"] != "done"
    assert any("99" in s for s in seen)
    assert not any("pull/10" in s for s in seen)


def test_review_reopened_prefers_pr_marker_over_stale_evidence(monkeypatch):
    """Devin #756: stale evidence_ref must not close when a newer [pr:] is open."""
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    tid = _make_review_task(
        notes_extra=(
            "[review: merged-upstream @ 2026-09-20T00:00:00Z] PR "
            "https://github.com/x/y/pull/10\n"
            "[waiting-for: review]\n"
            "[pr: https://github.com/x/y/pull/99]"
        )
    )
    task_update(
        tid,
        evidence_ref="https://github.com/x/y/pull/10",
        actor="test-agent",
    )

    seen: list[str] = []

    def _state(ref, repo_path):
        seen.append(str(ref))
        ref_s = str(ref)
        if "pull/10" in ref_s:
            return "MERGED"
        if "pull/99" in ref_s:
            return "OPEN"
        return None

    monkeypatch.setattr(reviewer, "_gh_pr_state", _state)
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_merge_details",
        lambda ref, repo_path: {
            "url": str(ref),
            "mergeCommit": {"oid": "abc"},
        },
    )

    res = reviewer.review_task(tid)

    assert res["verdict"] != "merged-upstream"
    assert _get_task(tid)["status"] != "done"
    assert any("99" in s for s in seen)
    assert not any("pull/10" in s for s in seen)


def test_review_reopened_closes_when_new_pr_merged(monkeypatch):
    """Reopened task closes only when the *current* evidence_ref PR is MERGED."""
    monkeypatch.setattr("unified_agent.merge.find_task_worktree", lambda task_id: (None, None))
    tid = _make_review_task(
        notes_extra=(
            "[review: merged-upstream @ 2026-09-20T00:00:00Z] PR "
            "https://github.com/x/y/pull/10\n"
            "[waiting-for: review]"
        )
    )
    task_update(
        tid,
        evidence_ref="https://github.com/x/y/pull/99",
        actor="test-agent",
    )

    def _state(ref, repo_path):
        ref_s = str(ref)
        if "pull/10" in ref_s:
            return "MERGED"
        if "pull/99" in ref_s:
            return "MERGED"
        return None

    monkeypatch.setattr(reviewer, "_gh_pr_state", _state)
    monkeypatch.setattr(
        reviewer,
        "_gh_pr_merge_details",
        lambda ref, repo_path: {
            "url": "https://github.com/x/y/pull/99",
            "mergeCommit": {"oid": "abc"},
        },
    )

    res = reviewer.review_task(tid)

    assert res["verdict"] == "merged-upstream"
    assert _get_task(tid)["status"] == "done"
    assert "pull/99" in (_get_task(tid)["notes"] or "")


@pytest.mark.parametrize(
    "expiry,active",
    [
        ("2026-09-30T04:00:01+00:00", True),
        ("2026-09-30T04:00:00+00:00", False),
        ("2026-09-30T03:59:59+00:00", False),
        (None, True),
    ],
)
def test_reconcile_claim_blocker_matches_expiry_boundary(monkeypatch, expiry, active):
    now = "2026-09-30T04:00:00+00:00"
    monkeypatch.setattr(storage, "now", lambda: now)
    task_id = task_add("Reconcile expiry")["id"]
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status='doing',claimed_by='Desktop_Agent2',claimed_at=?,lock_expires_at=? WHERE id=?",
            ("2026-09-30T03:00:00+00:00", expiry, task_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        assert bool(reviewer._active_reconcile_blocker(conn, row)) is active
    finally:
        conn.close()
