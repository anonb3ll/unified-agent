"""Recovered Hermes regressions for atomic direct task/run completion."""

from types import SimpleNamespace

import pytest
from test_merge_autonomy_boundary import boundary as boundary

from unified_agent import storage
from unified_agent.merge import _run_post_merge_verify as real_post_merge_verify


def sql(statement, params=()):
    conn = storage.connect()
    try:
        conn.execute(statement, params)
        conn.commit()
    finally:
        conn.close()


def claimed_task():
    from test_run_records import _as, _new_task

    from unified_agent.tools.tasks import task_claim

    tid = _new_task()
    with _as("Desktop_Agent1"):
        run = task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")["current_run_id"]
    return tid, run


def same_agent_successor(tid):
    from test_run_records import _as

    from unified_agent.tools.tasks import task_claim, task_update

    with _as("Desktop_Agent1"):
        task_update(tid, status="open", release_claim=True, worktree=False)
        return task_claim(tid, "Desktop_Agent1", expected_lane="dispatch-ready")["current_run_id"]


def test_merge_inflight_completion_cannot_close_same_agent_successor(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from unified_agent import merge, run_records

    tid, original = claimed_task()
    successor = []
    removed = []
    monkeypatch.setattr(
        merge,
        "find_task_worktree",
        lambda task_id: (
            {"path": str(tmp_path / "workspace"), "branch": "task/1"},
            str(tmp_path / "workspace"),
        ),
    )
    monkeypatch.setattr(
        merge,
        "run_validation_gates",
        lambda *args, **kwargs: successor.append(same_agent_successor(tid)),
    )
    monkeypatch.setattr(merge, "_known_repo_or_raise", lambda repo: repo)
    monkeypatch.setattr(
        merge,
        "_run_git_in_repo",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="sha", stderr=""),
    )
    monkeypatch.setattr(
        merge, "_worktree_remove_in_repo", lambda *args, **kwargs: removed.append(args)
    )
    monkeypatch.setattr(merge, "_run_post_merge_verify", lambda *args: {})
    monkeypatch.setattr(merge, "_close_delegation_ledger", lambda *args: {})
    monkeypatch.setattr(merge, "sync_state_to_vault", lambda: None)
    try:
        merge.orchestrate_merge(tid, allow_in_flight=True)
    except RuntimeError:
        pass
    assert successor and successor[0] != original
    assert removed == []
    assert run_records.get_run(successor[0])["state"] == "open"
    conn = storage.connect()
    try:
        assert (
            conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]
            == "doing"
        )
    finally:
        conn.close()


def test_reconcile_inflight_completion_cannot_close_same_agent_successor(monkeypatch):
    from unified_agent import reviewer, run_records

    tid, original = claimed_task()
    sql(
        "UPDATE tasks SET notes=?, project='missing', evidence_ref='https://github.com/org/repo/pull/1', lock_expires_at='2000-01-01' WHERE id=?",
        ("[waiting-for: review]\nPR: https://example.com/org/repo/pull/1", tid),
    )
    successor = []

    def merged(*args):
        successor.append(same_agent_successor(tid))
        return "MERGED"

    monkeypatch.setattr(reviewer, "_gh_pr_state", merged)
    monkeypatch.setattr(reviewer, "_gh_pr_merge_details", lambda *args: {})
    monkeypatch.setattr(reviewer, "sync_state_to_vault", lambda: None)
    result = reviewer.reconcile_tasks()
    assert successor and successor[0] != original
    assert run_records.get_run(successor[0])["state"] == "open"
    assert result["results"] == []


def test_already_done_dangling_run_repair_has_unknown_duration(monkeypatch):
    from unified_agent import reviewer, run_records

    tid, run = claimed_task()
    sql("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    monkeypatch.setattr(reviewer, "sync_state_to_vault", lambda: None)
    assert reviewer._mark_done_with_note(tid, "already merged") is True
    repaired = run_records.get_run(run)
    assert repaired["state"] == "finished"
    assert repaired["outcome"] == "abandoned"
    assert repaired["wall_s"] is None


def test_direct_finish_record_failure_rolls_back_task(monkeypatch):
    from unified_agent import run_records

    tid, run = claimed_task()
    expected = run_records.capture_direct_completion(tid)

    def broken(*args, **kwargs):
        raise RuntimeError("injected run record failure")

    monkeypatch.setattr(run_records, "_append", broken)
    with pytest.raises(RuntimeError, match="injected"):
        run_records.complete_direct(expected, actor="review-cron", note="must roll back")
    assert run_records.get_run(run)["state"] == "open"
    conn = storage.connect()
    try:
        row = conn.execute("SELECT status, notes FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "doing"
        assert "must roll back" not in (row["notes"] or "")
    finally:
        conn.close()


def test_direct_finish_serializes_record_and_task_write(monkeypatch):
    import sqlite3

    from unified_agent import config, run_records

    tid, run = claimed_task()
    expected = run_records.capture_direct_completion(tid)
    original = run_records._append
    blocked = []

    def append(*args, **kwargs):
        contender = sqlite3.connect(config.db_path(), timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute(
                    "UPDATE tasks SET current_run_id='sp-successor' WHERE id=?", (tid,)
                )
            blocked.append(True)
        finally:
            contender.close()
        return original(*args, **kwargs)

    monkeypatch.setattr(run_records, "_append", append)
    assert run_records.complete_direct(expected, actor="review-cron") is True
    assert blocked == [True]
    assert run_records.get_run(run)["outcome"] == "completed"


def test_review_task_inflight_merged_pr_refuses_same_agent_successor(monkeypatch):
    from unified_agent import merge, reviewer, run_records

    tid, original = claimed_task()
    sql(
        "UPDATE tasks SET notes=?, project='missing', lock_expires_at='2000-01-01' WHERE id=?",
        ("[waiting-for: review]", tid),
    )
    successor = []

    def fallback(*args, **kwargs):
        successor.append(same_agent_successor(tid))
        # Keep the claim expired to ensure the lifecycle fence, rather than the lease
        # precheck, refuses this same-agent successor.
        sql("UPDATE tasks SET lock_expires_at='2000-01-01' WHERE id=?", (tid,))
        return {
            "note": "merged old work",
            "verdict": "merged-upstream",
            "notification": "merged",
        }

    monkeypatch.setattr(merge, "find_task_worktree", lambda *args: (None, None))
    monkeypatch.setattr(reviewer, "_merged_pr_fallback", fallback)
    monkeypatch.setattr(reviewer, "sync_state_to_vault", lambda: None)
    result = reviewer.review_task(tid)
    assert successor and successor[0] != original
    assert result["verdict"] == "unchanged"
    assert run_records.get_run(successor[0])["state"] == "open"


def test_direct_finish_declined_record_rolls_back_task(monkeypatch):
    from unified_agent import run_records

    tid, run = claimed_task()
    expected = run_records.capture_direct_completion(tid)
    monkeypatch.setattr(run_records, "_append", lambda *args, **kwargs: False)
    completed = False
    try:
        completed = run_records.complete_direct(expected, actor="review-cron")
    except RuntimeError:
        pass
    assert completed is False
    assert run_records.get_run(run)["state"] == "open"
    conn = storage.connect()
    try:
        assert (
            conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]
            == "doing"
        )
    finally:
        conn.close()


@pytest.mark.parametrize("takeover", [False, True])
def test_ci_green_yolo_completes_and_transient_takeover_can_retry(monkeypatch, tmp_path, takeover):
    from unified_agent import merge, reviewer, run_records

    tid, original_run = claimed_task()
    sql(
        "UPDATE tasks SET status='doing', notes=?, updated_at='2000-01-01T00:00:00Z', lock_expires_at='2000-01-01' WHERE id=?",
        (
            "[waiting-for: review]\\n[delivery: mode=local-only yolo=on]\\nPR: https://github.com/org/repo/pull/1",
            tid,
        ),
    )
    wt = {
        "path": str(tmp_path / "intercepted-wt"),
        "branch": "task/owned",
        "head": "a" * 40,
    }
    monkeypatch.setattr(merge, "find_task_worktree", lambda *a: (wt, str(tmp_path)))
    validations = []
    successor = []

    def gates(*args, **kwargs):
        validations.append(True)
        if takeover and len(validations) == 2:
            successor.append(same_agent_successor(tid))
            sql("UPDATE tasks SET lock_expires_at='2000-01-01' WHERE id=?", (tid,))

    monkeypatch.setattr(merge, "run_validation_gates", gates)
    monkeypatch.setattr(merge, "_known_repo_or_raise", lambda repo: repo)
    monkeypatch.setattr(merge, "_worktree_remove_in_repo", lambda *a, **kw: None)
    monkeypatch.setattr(merge, "_run_post_merge_verify", lambda *a: {})
    monkeypatch.setattr(merge, "_close_delegation_ledger", lambda *a: {})
    monkeypatch.setattr(merge, "sync_state_to_vault", lambda: None)
    monkeypatch.setattr(reviewer, "_head_sha", lambda *a: "a" * 40)
    monkeypatch.setattr(reviewer, "_ci_pytest_skip_eligible", lambda *a: True)
    monkeypatch.setattr(reviewer, "_exact_head_ci_pytest_ok", lambda *a: (True, "CI SUCCESS"))
    monkeypatch.setattr(reviewer, "_changed_files_vs_main", lambda *a: [{"file": "src/f.py"}])
    effects = []
    monkeypatch.setattr(
        merge,
        "_run_git_in_repo",
        lambda *a, **kw: (
            effects.append(a) or SimpleNamespace(returncode=0, stdout="intercepted-sha", stderr="")
        ),
    )
    monkeypatch.setattr(reviewer, "sync_state_to_vault", lambda: None)
    result = reviewer.review_task(tid)
    conn = storage.connect()
    try:
        notes = conn.execute("SELECT notes FROM tasks WHERE id=?", (tid,)).fetchone()["notes"]
    finally:
        conn.close()
    if takeover:
        assert result["verdict"] == "unchanged"
        assert "[review: yolo-merge-failed @" not in notes
        assert successor and successor[0] != original_run
        assert run_records.get_run(successor[0])["state"] == "open"
        # A later review owns its new snapshot and may finish this legitimate run.
        # Successor provisioning now clears the old attempt's review markers.
        from test_run_records import _as

        from unified_agent.tools.tasks import task_update

        with _as("Desktop_Agent1"):
            task_update(tid, append_note="[waiting-for: review]", worktree=False)
        assert reviewer.review_task(tid)["verdict"] == "auto-merged"
        assert run_records.get_run(successor[0])["outcome"] == "completed"
    else:
        assert result["verdict"] == "auto-merged"
        assert run_records.get_run(original_run)["outcome"] == "completed"
        assert any("merge" in call[0] for call in effects)
        assert "[review: ci-gate @" in notes


def test_ci_annotation_cannot_advance_successor_snapshot(monkeypatch, tmp_path):
    from unified_agent import merge, reviewer, run_records

    tid, original = claimed_task()
    sql(
        "UPDATE tasks SET notes=?, lock_expires_at='2000-01-01' WHERE id=?",
        (
            "[waiting-for: review]\n[delivery: mode=local-only yolo=on]\nPR: https://github.com/org/repo/pull/1",
            tid,
        ),
    )
    monkeypatch.setattr(
        merge,
        "find_task_worktree",
        lambda *a: ({"path": str(tmp_path), "branch": "task/owned"}, str(tmp_path)),
    )
    monkeypatch.setattr(merge, "run_validation_gates", lambda *a, **kw: None)
    monkeypatch.setattr(reviewer, "_head_sha", lambda *a: "a" * 40)
    monkeypatch.setattr(reviewer, "_ci_pytest_skip_eligible", lambda *a: True)
    successor = []

    def ci(*args):
        successor.append(same_agent_successor(tid))
        return True, "CI SUCCESS"

    monkeypatch.setattr(reviewer, "_exact_head_ci_pytest_ok", ci)
    monkeypatch.setattr(reviewer, "sync_state_to_vault", lambda: None)
    effects = []
    monkeypatch.setattr(merge, "_run_git_in_repo", lambda *a, **kw: effects.append(a))
    assert reviewer.review_task(tid)["verdict"] == "unchanged"
    assert effects == [] and successor[0] != original
    assert run_records.get_run(successor[0])["state"] == "open"
    conn = storage.connect()
    try:
        notes = conn.execute("SELECT notes FROM tasks WHERE id=?", (tid,)).fetchone()["notes"]
        assert "[review: ci-gate @" not in notes and "[review: yolo-merge-failed @" not in notes
    finally:
        conn.close()


@pytest.mark.parametrize("red_tier", [False, True])
def test_classification_verdict_does_not_mark_successor(monkeypatch, tmp_path, red_tier):
    from unified_agent import merge, reviewer, run_records

    tid, _ = claimed_task()
    sql(
        "UPDATE tasks SET notes=?, lock_expires_at='2000-01-01' WHERE id=?",
        ("[waiting-for: review]", tid),
    )
    monkeypatch.setattr(
        merge,
        "find_task_worktree",
        lambda *a: ({"path": str(tmp_path), "branch": "task/owned"}, str(tmp_path)),
    )
    monkeypatch.setattr(merge, "run_validation_gates", lambda *a, **kw: None)
    monkeypatch.setattr(reviewer, "_head_sha", lambda *a: "a" * 40)
    monkeypatch.setattr(reviewer, "_ci_pytest_skip_eligible", lambda *a: True)
    monkeypatch.setattr(reviewer, "_exact_head_ci_pytest_ok", lambda *a: (True, "CI SUCCESS"))
    monkeypatch.setattr(reviewer, "sync_state_to_vault", lambda: None)
    successor = []

    def classify(*args):
        successor.append(same_agent_successor(tid))
        return [{"file": "policy.yaml" if red_tier else "src/f.py"}]

    monkeypatch.setattr(reviewer, "_changed_files_vs_main", classify)
    result = reviewer.review_task(tid)
    assert result["verdict"] == "unchanged" and result["notification"] is None
    assert run_records.get_run(successor[0])["state"] == "open"
    conn = storage.connect()
    try:
        notes = conn.execute("SELECT notes FROM tasks WHERE id=?", (tid,)).fetchone()["notes"]
        assert "[review: validated" not in notes and "[review: red-tier" not in notes
    finally:
        conn.close()


@pytest.mark.parametrize("dry_stage", [True, False])
def test_done_task_identity_refusal_remains_retryable(monkeypatch, dry_stage):
    from unified_agent import reviewer, run_records
    from unified_agent.tools import git

    tid, _ = claimed_task()
    sql("UPDATE tasks SET status='done', notes='[waiting-for: review]' WHERE id=?", (tid,))
    monkeypatch.setattr(reviewer, "_head_sha", lambda *a: "a" * 40)
    monkeypatch.setattr(reviewer, "_red_tier_for_task_checked", lambda *a: ([], None))
    monkeypatch.setattr(reviewer, "sync_state_to_vault", lambda: None)
    successor = []

    def merge_call(*args, **kwargs):
        if kwargs["dry_run"] == dry_stage:
            successor.append(same_agent_successor(tid))
            try:
                raise run_records.DirectCompletionChanged("superseded")
            except run_records.DirectCompletionChanged as exc:
                raise RuntimeError("wrapped tool refusal") from exc
        return {"success": True}

    monkeypatch.setattr(git, "git_merge_task", merge_call)
    result = reviewer.review_task(tid)
    assert result["verdict"] == "unchanged" and result["notification"] is None
    assert run_records.get_run(successor[0])["state"] == "open"
    conn = storage.connect()
    try:
        notes = conn.execute("SELECT notes FROM tasks WHERE id=?", (tid,)).fetchone()["notes"]
        assert "[review: merge-failed" not in notes and "[review: rejected" not in notes
    finally:
        conn.close()


def test_distinct_unclaimed_completions_in_same_second(monkeypatch):
    from test_run_records import _new_task

    from unified_agent import run_records

    tid = _new_task()
    monkeypatch.setattr(storage, "now", lambda: "2026-09-30T13:04:24+00:00")
    for _ in range(3):
        expected = run_records.capture_direct_completion(tid)
        assert run_records.complete_direct(expected, actor="merge-orchestrator")
        assert run_records.complete_direct(expected, actor="merge-orchestrator") is False
        sql("UPDATE tasks SET status='open' WHERE id=?", (tid,))
    conn = storage.connect()
    try:
        finishes = conn.execute(
            "SELECT dedupe_key FROM event_log WHERE channel=? "
            "AND json_extract(payload, '$.kind')=?",
            (run_records.CHANNEL, run_records.KIND_FINISHED),
        ).fetchall()
        assert len(finishes) == 3
        assert len({row["dedupe_key"] for row in finishes}) == 3
    finally:
        conn.close()


@pytest.mark.parametrize("verify_warning", [False, True])
def test_mission_done_review_reports_its_own_success(boundary, monkeypatch, verify_warning):
    from test_merge_autonomy_boundary import AGENT, approve_production, as_actor, mode

    from unified_agent import reviewer

    sql("UPDATE tasks SET notes='[waiting-for: review]' WHERE id=17")
    mode(boundary, "observe")
    approve_production()
    monkeypatch.setattr(reviewer, "_red_tier_for_task_checked", lambda *a: ([], None))
    monkeypatch.setattr(reviewer, "sync_state_to_vault", lambda: None)
    if verify_warning:
        from unified_agent import merge
        from unified_agent.tools import workflow

        monkeypatch.setenv("UA_POST_MERGE_VERIFY", "1")
        monkeypatch.setattr(merge, "_run_post_merge_verify", real_post_merge_verify)
        monkeypatch.setattr(
            workflow, "workflow_run", lambda **kw: {"status": "failed", "error": "probe"}
        )
    with as_actor(AGENT):
        result = reviewer.review_task(17)
    assert result["verdict"] == "merged"
    assert result["merge_result"]["task_completed"] is True
    if verify_warning:
        conn = storage.connect()
        try:
            notes = conn.execute("SELECT notes FROM tasks WHERE id=17").fetchone()["notes"]
            assert "[warning: post-merge verification failed" in notes
            assert "[review: approved" in notes
        finally:
            conn.close()


def test_post_verify_warning_never_adopts_successor(monkeypatch):
    from unified_agent import run_records
    from unified_agent.tools import workflow

    tid, _ = claimed_task()
    expected = run_records.capture_direct_completion(tid)
    monkeypatch.setenv("UA_POST_MERGE_VERIFY", "1")
    successor = []

    def verify(**kwargs):
        successor.append(same_agent_successor(tid))
        return {"status": "failed", "error": "probe"}

    monkeypatch.setattr(workflow, "workflow_run", verify)
    with run_records.direct_completion_scope(expected):
        assert run_records.complete_direct(expected, actor="review-cron")
        assert real_post_merge_verify(tid)["status"] == "failed"
    assert run_records.get_run(successor[0])["state"] == "open"
    conn = storage.connect()
    try:
        notes = conn.execute("SELECT notes FROM tasks WHERE id=?", (tid,)).fetchone()["notes"]
        assert "[warning: post-merge verification failed" not in notes
    finally:
        conn.close()


@pytest.mark.parametrize("takeover", [False, True])
def test_legacy_ledger_note_advances_only_own_completion(monkeypatch, takeover):
    from test_run_records import _as

    from unified_agent import merge, run_records
    from unified_agent.tools import delegate
    from unified_agent.tools.tasks import task_claim, task_update

    delegated = delegate.delegate(
        task_spec="ledger note", from_agent="Desktop_Agent1", to_agent="Desktop_agent3"
    )
    tid = delegated["task_id"]
    expected = run_records.capture_direct_completion(tid)
    with run_records.direct_completion_scope(expected):
        assert run_records.complete_direct(expected, actor="Desktop_agent3")
        if takeover:
            with _as("Desktop_agent3"):
                task_update(tid, status="open", release_claim=True, worktree=False)
                task_claim(tid, "Desktop_agent3")
        result = merge._close_delegation_ledger(tid)
        row = delegate.delegate_get(delegated["delegation_id"])["delegation"]
        if takeover:
            assert result["delegations_closed"] == 0
            assert row["status"] == "pending"
        else:
            assert result["delegations_closed"] == 1
            assert row["status"] == "completed"
            assert run_records.scoped_direct_completion(
                tid
            ) == run_records.capture_direct_completion(tid)
