"""Behavioral tests for the `unified-agent-review` CLI (review_cli.py).

These test the CLI layer only: dispatch, flag/env forwarding, --json shape, and exit
codes. The review pipeline itself (git worktrees, audits, pytest, merges) is exercised
by tests/test_reviewer_tools.py and the reviewer module's own tests; here we monkeypatch
`reviewer.review_and_notify` / `reviewer.review_task` so nothing touches git, iMessage,
or the network.
"""

import json
import os
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from test_shipping_preflight import compare

from unified_agent import (
    config,
    github_app_worker,
    held_pr,
    review_cli,
    review_reads,
    reviewer,
    server,
    shipping_preflight_cli,
    storage,
)
from unified_agent.review_cli import main
from unified_agent.tools.tasks import task_add, task_update
from unified_agent.webui.routes import web_routes

WAITING = reviewer.WAITING_FOR_REVIEW


@pytest.fixture(autouse=True)
def _stub_reconcile(monkeypatch):
    """Neutralize the reconcile pass for CLI-layer tests.

    The scan subcommand runs reviewer.reconcile_tasks first; these tests
    exercise the CLI dispatch/exit-code layer only, so reconcile is stubbed to
    a no-op. Reconcile-specific CLI tests re-patch it with their own fake.
    """
    monkeypatch.setattr(
        reviewer,
        "reconcile_tasks",
        lambda send_notifications=False, dry_run=False: {"results": []},
    )


def _make_candidate(title="review-me") -> int:
    """Create a task annotated [waiting-for: review] and in 'doing' status."""
    t = task_add(title, notes=f"{WAITING} ready")
    task_update(t["id"], status="doing")
    return t["id"]


def _result(task_id: int, verdict: str, notified: bool = True, notification=...) -> dict:
    if notification is ...:
        notification = f"verdict={verdict} for #{task_id}"
    return {
        "task_id": task_id,
        "verdict": verdict,
        "red_tier_files": [],
        "merge_result": None,
        "notification": notification,
        "notified": notified,
    }


# ── scan: no candidates ──────────────────────────────────────────────


def test_scan_no_candidates_exits_zero(isolated_env, capsys):
    code = main(["scan"])
    out = capsys.readouterr().out
    assert code == 0
    assert "no tasks waiting for review" in out


def test_scan_no_candidates_json(isolated_env, capsys):
    code = main(["scan", "--json"])
    out = capsys.readouterr().out
    assert code == 0
    assert json.loads(out) == []


# ── scan: candidates, dispatch + exit codes ──────────────────────────


def test_scan_merged_candidate_exits_zero(isolated_env, monkeypatch, capsys):
    tid = _make_candidate()
    monkeypatch.setattr(
        reviewer, "review_and_notify", lambda task_id, dry_run=False: _result(task_id, "merged")
    )
    code = main(["scan"])
    out = capsys.readouterr().out
    assert code == 0
    assert "reviewed=1 merged=1 red-tier=0 failed=0" in out
    assert f"verdict=merged for #{tid}" in out


def test_scan_failed_candidate_exits_one(isolated_env, monkeypatch, capsys):
    _make_candidate()
    monkeypatch.setattr(
        reviewer, "review_and_notify", lambda task_id, dry_run=False: _result(task_id, "failed")
    )
    code = main(["scan"])
    out = capsys.readouterr().out
    assert code == 1
    assert "failed=1" in out


def test_scan_red_tier_candidate_exits_one(isolated_env, monkeypatch, capsys):
    _make_candidate()
    monkeypatch.setattr(
        reviewer,
        "review_and_notify",
        lambda task_id, dry_run=False: _result(task_id, "red-tier"),
    )
    code = main(["scan"])
    out = capsys.readouterr().out
    assert code == 1
    assert "red-tier=1" in out


def test_scan_timeout_candidate_exits_zero(isolated_env, monkeypatch, capsys):
    """Task #1254: a 'timeout' verdict is inconclusive, not a failure — it must
    not trip exit code 1 (which cron logs read as FAIL), and must be visible in
    the summary line separately from validated/unchanged."""
    _make_candidate()
    monkeypatch.setattr(
        reviewer, "review_and_notify", lambda task_id, dry_run=False: _result(task_id, "timeout")
    )
    code = main(["scan"])
    out = capsys.readouterr().out
    assert code == 0
    assert "timeout=1" in out
    assert "failed=0" in out


def test_scan_dry_run_ok_exits_zero(isolated_env, monkeypatch, capsys):
    _make_candidate()
    monkeypatch.setattr(
        reviewer,
        "review_and_notify",
        lambda task_id, dry_run=False: _result(task_id, "dry-run-ok"),
    )
    code = main(["scan", "--dry-run"])
    assert code == 0


# ── flag / env forwarding ────────────────────────────────────────────


def test_dry_run_flag_forwarded(isolated_env, monkeypatch):
    tid = _make_candidate()
    seen = {}

    def fake_review_and_notify(task_id, dry_run=False):
        seen["dry_run"] = dry_run
        return _result(task_id, "dry-run-ok")

    monkeypatch.setattr(reviewer, "review_and_notify", fake_review_and_notify)
    main(["task", str(tid), "--dry-run"])
    assert seen["dry_run"] is True


def test_env_var_dry_run_equivalent_to_flag(isolated_env, monkeypatch):
    tid = _make_candidate()
    seen = {}

    def fake_review_and_notify(task_id, dry_run=False):
        seen["dry_run"] = dry_run
        return _result(task_id, "dry-run-ok")

    monkeypatch.setattr(reviewer, "review_and_notify", fake_review_and_notify)
    monkeypatch.setenv("UA_REVIEW_DRY_RUN", "1")
    main(["task", str(tid)])
    assert seen["dry_run"] is True


def test_without_dry_run_defaults_false(isolated_env, monkeypatch):
    tid = _make_candidate()
    seen = {}

    def fake_review_and_notify(task_id, dry_run=False):
        seen["dry_run"] = dry_run
        return _result(task_id, "merged")

    monkeypatch.setattr(reviewer, "review_and_notify", fake_review_and_notify)
    main(["task", str(tid)])
    assert seen["dry_run"] is False


def test_no_notify_uses_review_task_not_review_and_notify(isolated_env, monkeypatch):
    tid = _make_candidate()
    calls = {"review_task": 0, "review_and_notify": 0}

    def fake_review_task(task_id, dry_run=False):
        calls["review_task"] += 1
        return _result(task_id, "merged")

    def fake_review_and_notify(task_id, dry_run=False):
        calls["review_and_notify"] += 1
        return _result(task_id, "merged")

    monkeypatch.setattr(reviewer, "review_task", fake_review_task)
    monkeypatch.setattr(reviewer, "review_and_notify", fake_review_and_notify)

    main(["task", str(tid), "--no-notify"])

    assert calls["review_task"] == 1
    assert calls["review_and_notify"] == 0


def test_notify_default_uses_review_and_notify(isolated_env, monkeypatch):
    tid = _make_candidate()
    calls = {"review_task": 0, "review_and_notify": 0}

    monkeypatch.setattr(
        reviewer,
        "review_task",
        lambda task_id, dry_run=False: (
            calls.__setitem__("review_task", calls["review_task"] + 1) or _result(task_id, "merged")
        ),
    )
    monkeypatch.setattr(
        reviewer,
        "review_and_notify",
        lambda task_id, dry_run=False: (
            calls.__setitem__("review_and_notify", calls["review_and_notify"] + 1)
            or _result(task_id, "merged")
        ),
    )

    main(["task", str(tid)])

    assert calls["review_and_notify"] == 1
    assert calls["review_task"] == 0


# ── --json shape ──────────────────────────────────────────────────────


def test_json_output_is_list_of_result_dicts(isolated_env, monkeypatch, capsys):
    tid = _make_candidate()
    monkeypatch.setattr(
        reviewer, "review_and_notify", lambda task_id, dry_run=False: _result(task_id, "merged")
    )
    code = main(["scan", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 0
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["task_id"] == tid
    assert payload[0]["verdict"] == "merged"


# ── task subcommand: unknown / unannotated task -> exit 2 ─────────────


def test_task_unknown_id_exits_two(isolated_env, capsys):
    code = main(["task", "999999"])
    err = capsys.readouterr().err
    assert code == 2
    assert "999999" in err


def test_task_not_annotated_exits_two(isolated_env, capsys):
    t = task_add("not-annotated")
    code = main(["task", str(t["id"])])
    err = capsys.readouterr().err
    assert code == 2
    assert WAITING in err or "annotated" in err


def test_task_notify_error_never_changes_verdict_or_crashes(isolated_env, monkeypatch, capsys):
    """review_and_notify already swallows notify errors internally; the CLI must
    surface the verdict unchanged even if notification failed."""
    tid = _make_candidate()

    def fake_review_and_notify(task_id, dry_run=False):
        r = _result(task_id, "merged", notified=False)
        r["notify_error"] = "bridge unavailable"
        return r

    monkeypatch.setattr(reviewer, "review_and_notify", fake_review_and_notify)
    code = main(["task", str(tid)])
    assert code == 0


# ── scan with a mix of verdicts ────────────────────────────────────────


def test_scan_mixed_verdicts_exit_reflects_worst(isolated_env, monkeypatch, capsys):
    t1 = _make_candidate("a")
    _make_candidate("b")

    def fake(task_id, dry_run=False):
        return _result(task_id, "merged" if task_id == t1 else "failed")

    monkeypatch.setattr(reviewer, "review_and_notify", fake)
    code = main(["scan"])
    out = capsys.readouterr().out
    assert code == 1
    assert "reviewed=2 merged=1 red-tier=0 failed=1" in out


# ── validate+hold verdicts must not count as failures ──────────────────


def test_scan_validated_candidate_exits_zero(isolated_env, monkeypatch, capsys):
    _make_candidate()
    monkeypatch.setattr(
        reviewer,
        "review_and_notify",
        lambda task_id, dry_run=False: _result(task_id, "validated"),
    )
    code = main(["scan"])
    out = capsys.readouterr().out
    assert code == 0
    assert "validated=1" in out


def test_scan_unchanged_candidate_exits_zero_and_prints_no_line(isolated_env, monkeypatch, capsys):
    tid = _make_candidate()
    monkeypatch.setattr(
        reviewer,
        "review_and_notify",
        lambda task_id, dry_run=False: _result(task_id, "unchanged", notification=None),
    )
    code = main(["scan"])
    out = capsys.readouterr().out
    assert code == 0
    assert "unchanged=1" in out
    # unchanged verdicts carry no notification line, only the summary
    assert f"#{tid}" not in out


# ── reconcile wiring in the scan subcommand ─────────────────────────────


def test_scan_runs_reconcile_and_includes_its_results(isolated_env, monkeypatch, capsys):
    seen = {}

    def fake_reconcile(send_notifications=False, dry_run=False):
        seen["called"] = True
        seen["send_notifications"] = send_notifications
        return {
            "results": [
                {
                    "task_id": 42,
                    "verdict": "merged-upstream",
                    "red_tier_files": [],
                    "merge_result": None,
                    "notification": "✅ Task #42 (x) branch already merged into origin/main — marked done.",
                }
            ]
        }

    monkeypatch.setattr(reviewer, "reconcile_tasks", fake_reconcile)
    # No review candidates: reconcile results alone must still be reported.
    code = main(["scan"])
    out = capsys.readouterr().out
    assert code == 0
    assert seen["called"] is True
    assert seen["send_notifications"] is True
    assert "merged into origin/main" in out
    assert "merged-upstream=1" in out


def test_task_subcommand_does_not_reconcile(isolated_env, monkeypatch):
    tid = _make_candidate()
    called = {"reconcile": 0}

    def fake_reconcile(send_notifications=False, dry_run=False):
        called["reconcile"] += 1
        return {"results": []}

    monkeypatch.setattr(reviewer, "reconcile_tasks", fake_reconcile)
    monkeypatch.setattr(
        reviewer, "review_and_notify", lambda task_id, dry_run=False: _result(task_id, "validated")
    )
    code = main(["task", str(tid)])
    assert code == 0
    assert called["reconcile"] == 0


def test_scan_reconcile_failure_never_blocks_review_pass(isolated_env, monkeypatch, capsys):
    tid = _make_candidate()

    def broken_reconcile(send_notifications=False, dry_run=False):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(reviewer, "reconcile_tasks", broken_reconcile)
    monkeypatch.setattr(
        reviewer, "review_and_notify", lambda task_id, dry_run=False: _result(task_id, "validated")
    )
    code = main(["scan"])
    captured = capsys.readouterr()
    assert code == 0
    assert "reconcile step failed" in captured.err
    assert f"#{tid}" in captured.out


# ── hygiene wiring in the scan subcommand (task #729) ──────────────────


def test_scan_runs_hygiene_pass_before_review(isolated_env, monkeypatch, capsys):

    # A stale claim exists purely to be released by the hygiene pass.
    stale = task_add("Hygiene stale claim")
    task_update(stale["id"], status="doing", worktree=False)
    from unified_agent import storage

    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET claimed_by='Desktop_Agent1', cancel_requested=1, "
            "lock_expires_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (stale["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    tid = _make_candidate()
    monkeypatch.setattr(
        reviewer, "review_and_notify", lambda task_id, dry_run=False: _result(task_id, "validated")
    )
    code = main(["scan"])
    out = capsys.readouterr().out
    assert code == 0
    # The hygiene line is printed and the claim was released in the same pass.
    assert "hygiene: released=1 demoted=0 archived=0" in out
    row = (
        storage.connect().execute("SELECT status FROM tasks WHERE id=?", (stale["id"],)).fetchone()
    )
    assert row["status"] == "open"
    # The review candidate was still processed after hygiene ran.
    assert f"#{tid}" in out


def test_scan_hygiene_failure_never_blocks_review_pass(isolated_env, monkeypatch, capsys):
    from unified_agent import queue_hygiene

    def broken_hygiene(**kwargs):
        raise RuntimeError("hygiene exploded")

    monkeypatch.setattr(queue_hygiene, "run_hygiene_pass", broken_hygiene)
    tid = _make_candidate()
    monkeypatch.setattr(
        reviewer, "review_and_notify", lambda task_id, dry_run=False: _result(task_id, "validated")
    )
    code = main(["scan"])
    captured = capsys.readouterr()
    assert code == 0
    assert "hygiene pass failed" in captured.err
    assert f"#{tid}" in captured.out


def test_task_subcommand_does_not_run_hygiene(isolated_env, monkeypatch):
    from unified_agent import queue_hygiene

    tid = _make_candidate()
    called = {"hygiene": 0}
    monkeypatch.setattr(
        queue_hygiene, "run_hygiene_pass", lambda **kwargs: called.__setitem__("hygiene", 1)
    )
    monkeypatch.setattr(
        reviewer, "review_and_notify", lambda task_id, dry_run=False: _result(task_id, "validated")
    )
    code = main(["task", str(tid)])
    assert code == 0
    assert called["hygiene"] == 0


# ── label-held subcommand (task #1260) ──────────────────────────────────


class TestLabelHeld:
    """`unified-agent-review label-held <id>` — the one CLI path that writes
    to GitHub (a PR label), so it is tested as a fully separate subcommand
    from scan/task above. `held_pr.label_held_task` itself is monkeypatched
    here; its own logic (held_despite_green, the gh I/O wrappers) is covered
    by tests/test_held_pr.py.
    """

    def test_not_held_exits_zero(self, isolated_env, monkeypatch, capsys):
        tid = _make_candidate()
        monkeypatch.setattr(
            held_pr,
            "label_held_task",
            lambda task_id, dry_run=False: {
                "task_id": task_id,
                "pr_ref": "123",
                "held": False,
                "reason": "no rework signal in verdict or notes",
                "ok": True,
                "message": "not held-despite-green: no rework signal in verdict or notes",
            },
        )
        code = main(["label-held", str(tid)])
        out = capsys.readouterr().out
        assert code == 0
        assert "not held-despite-green" in out

    def test_held_and_labeled_exits_zero(self, isolated_env, monkeypatch, capsys):
        tid = _make_candidate()
        monkeypatch.setattr(
            held_pr,
            "label_held_task",
            lambda task_id, dry_run=False: {
                "task_id": task_id,
                "pr_ref": "123",
                "held": True,
                "reason": "rework signal present and exact-head CI was green",
                "ok": True,
                "labeled": True,
                "message": "labeled 123 'held'",
            },
        )
        code = main(["label-held", str(tid)])
        out = capsys.readouterr().out
        assert code == 0
        assert "labeled 123 'held'" in out

    def test_held_but_label_write_fails_exits_one(self, isolated_env, monkeypatch, capsys):
        tid = _make_candidate()
        monkeypatch.setattr(
            held_pr,
            "label_held_task",
            lambda task_id, dry_run=False: {
                "task_id": task_id,
                "pr_ref": "123",
                "held": True,
                "reason": "rework signal present and exact-head CI was green",
                "ok": False,
                "labeled": False,
                "message": "held-despite-green but labeling 123 failed",
            },
        )
        code = main(["label-held", str(tid)])
        err_or_out = capsys.readouterr()
        assert code == 1
        assert "labeling 123 failed" in err_or_out.out

    def test_unresolvable_pr_ref_exits_two(self, isolated_env, monkeypatch, capsys):
        tid = _make_candidate()
        monkeypatch.setattr(
            held_pr,
            "label_held_task",
            lambda task_id, dry_run=False: {
                "task_id": task_id,
                "ok": False,
                "held": False,
                "message": "no resolvable PR reference in task notes/evidence_ref",
            },
        )
        code = main(["label-held", str(tid)])
        out = capsys.readouterr().out
        assert code == 2
        assert "no resolvable PR reference" in out

    def test_dry_run_forwarded(self, isolated_env, monkeypatch):
        tid = _make_candidate()
        seen = {}

        def fake_label_held_task(task_id, dry_run=False):
            seen["dry_run"] = dry_run
            return {
                "task_id": task_id,
                "pr_ref": "123",
                "held": True,
                "reason": "x",
                "ok": True,
                "labeled": False,
                "message": "held-despite-green (dry-run — not labeling): 123",
            }

        monkeypatch.setattr(held_pr, "label_held_task", fake_label_held_task)
        code = main(["label-held", str(tid), "--dry-run"])
        assert code == 0
        assert seen["dry_run"] is True

    def test_json_output(self, isolated_env, monkeypatch, capsys):
        tid = _make_candidate()
        payload = {
            "task_id": tid,
            "pr_ref": "123",
            "held": False,
            "reason": "no rework signal in verdict or notes",
            "ok": True,
            "message": "not held-despite-green: no rework signal in verdict or notes",
        }
        monkeypatch.setattr(held_pr, "label_held_task", lambda task_id, dry_run=False: payload)
        code = main(["label-held", str(tid), "--json"])
        out = capsys.readouterr().out
        assert code == 0
        assert json.loads(out) == payload

    def test_does_not_run_reconcile_or_hygiene(self, isolated_env, monkeypatch):
        from unified_agent import queue_hygiene

        tid = _make_candidate()
        called = {"reconcile": 0, "hygiene": 0}
        monkeypatch.setattr(
            reviewer,
            "reconcile_tasks",
            lambda **kwargs: (
                called.__setitem__("reconcile", called["reconcile"] + 1) or {"results": []}
            ),
        )
        monkeypatch.setattr(
            queue_hygiene,
            "run_hygiene_pass",
            lambda **kwargs: called.__setitem__("hygiene", called["hygiene"] + 1),
        )
        monkeypatch.setattr(
            held_pr,
            "label_held_task",
            lambda task_id, dry_run=False: {
                "task_id": task_id,
                "pr_ref": "123",
                "held": False,
                "reason": "x",
                "ok": True,
                "message": "not held-despite-green: x",
            },
        )
        code = main(["label-held", str(tid)])
        assert code == 0
        assert called["reconcile"] == 0
        assert called["hygiene"] == 0


# ── record-read: the steward's own read (task #2294) ─────────────────────

READ_REPO = "example/project"
H1 = "1" * 40
H2 = "2" * 40


def _record_read(monkeypatch, *argv, live_head=H1, delta=()):
    """``delta`` is what GitHub's compare answers for ``--from..--head``: a path
    list, ``None`` (not an ancestor), or an exception to raise."""

    def delta_paths(repo, base, head):
        if isinstance(delta, Exception):
            raise delta
        return delta if delta is None else list(delta)

    monkeypatch.setattr(review_cli, "_live_pr_head", lambda repo, number: live_head)
    monkeypatch.setattr(review_cli, "_delta_paths", delta_paths)
    return main(["record-read", "--repo", READ_REPO, "--pr", "123", "--reader", "steward", *argv])


def _gate(head_sha, pairs=None):
    def run_gh(args, **kwargs):
        if pairs is None:
            pytest.fail("the gate needed a comparison it should not have needed")
        return compare(pairs)(args, **kwargs)

    return shipping_preflight_cli.review_head_gate(
        repo=READ_REPO, number=123, head_sha=head_sha, run_gh=run_gh
    )


def test_a_recorded_read_passes_its_head_and_a_red_tier_push_needs_a_delta_read(
    isolated_env, monkeypatch, capsys
):
    assert _gate(H1)["status"] == "no-read-recorded"
    assert _record_read(monkeypatch, "--head", H1) == 0
    assert _record_read(monkeypatch, "--head", H1) == 0  # a re-read is one row
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "recorded"
    [row] = review_reads.for_pr(READ_REPO, 123)
    assert (row["kind"], row["reader"]) == (review_reads.STEWARD_READ, "steward")
    assert _gate(H1)["status"] == "read-at-head"

    pairs = {(H1, H2): {"status": "ahead", "files": [{"filename": "deploy/x.sh"}]}}
    with pytest.raises(shipping_preflight_cli.PreflightError, match="deploy/x.sh"):
        _gate(H2, pairs)

    code = _record_read(
        monkeypatch,
        *("--head", H2, "--kind", "delta-read", "--from", H1, "--files", "deploy/x.sh"),
        *("--evidence-url", "https://example.invalid/pull/123#c1"),
        live_head=H2,
        delta=["deploy/x.sh", "src/a.py"],
    )
    assert code == 0
    out = _gate(H2, pairs)
    assert out["status"] == "delta-read" and out["red_tier_delta"] == ["deploy/x.sh"]


@pytest.mark.parametrize(
    "argv, live_head, message",
    [
        # The writer half of the delta-read rule: no full read at the base.
        (
            ("--head", H2, "--kind", "delta-read", "--from", H1, "--evidence-url", "u"),
            H2,
            "no full read is recorded",
        ),
        (("--head", H2, "--kind", "delta-read", "--from", H1), H2, "--evidence-url"),
        (("--head", H2, "--kind", "delta-read", "--evidence-url", "u"), H2, "from_sha"),
        (("--head", H1, "--from", H2), H1, "only a delta-read row carries from_sha"),
        (("--head", H1), H2, "is not the pull request's current head"),
        (("--head", H1), None, "is not the pull request's current head"),
        (("--head", H1, "--reader", "not a label!"), H1, "not a known identity"),
    ],
)
def test_record_read_refusals_write_nothing(
    isolated_env, monkeypatch, capsys, argv, live_head, message
):
    assert _record_read(monkeypatch, *argv, live_head=live_head) == 2
    assert message in capsys.readouterr().err
    assert review_reads.for_pr(READ_REPO, 123) == []


@pytest.mark.parametrize(
    "delta, message",
    [
        (["src/a.py", "deploy/x.sh"], "does not name red-tier deploy/x.sh"),
        (None, "is not an ancestor"),
        (shipping_preflight_cli.PreflightError("too large to prove clean"), "too large"),
    ],
)
def test_a_delta_read_that_would_fail_the_gate_is_refused_when_written(
    isolated_env, monkeypatch, capsys, delta, message
):
    assert _record_read(monkeypatch, "--head", H1) == 0
    argv = ("--head", H2, "--kind", "delta-read", "--from", H1, "--files", "src/a.py")
    code = _record_read(monkeypatch, *argv, "--evidence-url", "u", live_head=H2, delta=delta)
    assert code == 2 and message in capsys.readouterr().err
    assert [r["head_sha"] for r in review_reads.for_pr(READ_REPO, 123)] == [H1]


def test_a_human_read_is_explicit_and_needs_a_human_reader_and_evidence(
    isolated_env, monkeypatch, capsys
):
    from unified_agent import config

    human = config.operator_label()
    assert _record_read(monkeypatch, "--head", H1, "--kind", "human", "--evidence-url", "u") == 2
    assert "not a human identity" in capsys.readouterr().err
    assert _record_read(monkeypatch, "--head", H1, "--kind", "human", "--reader", human) == 2
    assert "--evidence-url" in capsys.readouterr().err
    argv = ("--head", H1, "--kind", "human", "--reader", human, "--evidence-url", "u")
    assert _record_read(monkeypatch, *argv) == 0
    [row] = review_reads.for_pr(READ_REPO, 123)
    assert (row["kind"], row["reader"]) == (review_reads.HUMAN, human)


def test_record_read_dry_run_writes_nothing(isolated_env, monkeypatch, capsys):
    monkeypatch.setenv("UA_REVIEW_DRY_RUN", "1")
    assert _record_read(monkeypatch, "--head", H1) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry-run"
    assert review_reads.for_pr(READ_REPO, 123) == []


def test_record_read_database_lock_is_a_refusal(isolated_env, monkeypatch, capsys):
    def locked(**_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(review_reads, "record", locked)
    assert _record_read(monkeypatch, "--head", H1) == 2
    assert "could not record the read: database is locked" in capsys.readouterr().err
    assert review_reads.for_pr(READ_REPO, 123) == []


def test_record_read_publishes_under_the_service_env_and_leaves_no_trace(
    isolated_env, monkeypatch, capsys
):
    """#2418: an interactive hub shell lacks service.env, so the documented
    command reported publishing as off. The shell's own value still wins."""
    monkeypatch.delenv("UA_GITHUB_APP_ENABLED", raising=False)
    monkeypatch.setenv("UA_REVIEW_HEAD_CHECK", "shell-value")
    home = config.data_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "service.env").write_text("UA_GITHUB_APP_ENABLED=1\nUA_REVIEW_HEAD_CHECK=file-value\n")
    seen = {}

    def publish(**_kwargs):
        seen.update(
            enabled=os.environ.get("UA_GITHUB_APP_ENABLED"),
            check=os.environ.get("UA_REVIEW_HEAD_CHECK"),
        )
        return {"status": "review_head_posted"}

    monkeypatch.setattr(github_app_worker, "publish_review_head", publish)

    assert _record_read(monkeypatch, "--head", H1) == 0

    assert seen == {"enabled": "1", "check": "shell-value"}
    assert "UA_GITHUB_APP_ENABLED" not in os.environ
    assert json.loads(capsys.readouterr().out)["review_head"] == {"status": "review_head_posted"}


# ── record-read over the hub (task #2407) ────────────────────────────────

READ_URL = "/api/review/reads"
READ_OPERATOR = "Operator"
READ_TOKENS = {
    "Desktop_Agent1": "fixture-reader-token",
    "Desktop_Agent2": "fixture-peer-token",
    READ_OPERATOR: "fixture-operator-token",
}


def _as_reader(label="Desktop_Agent1"):
    return {"Authorization": "Bearer " + READ_TOKENS[label]}


def _read_body(**over):
    return {"repo": READ_REPO, "number": 123, "head_sha": H1, **over}


@pytest.fixture
def read_hub(isolated_env, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_OPERATOR_LABEL", READ_OPERATOR)
    monkeypatch.setenv("UA_REVIEW_READERS", "Desktop_Agent1")
    monkeypatch.setenv("UA_AGENT_TOKENS", ",".join(f"{k}:{v}" for k, v in READ_TOKENS.items()))
    monkeypatch.setattr(config, "agent_tokens", lambda: {v: k for k, v in READ_TOKENS.items()})
    for name in ("UA_AGENT_ACCESS_ENABLED", "UA_APPROVAL_TOOLS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(review_cli, "_live_pr_head", lambda repo, number: H1)
    storage.init_db()
    return TestClient(
        server.HubAuthASGIMiddleware(Starlette(routes=web_routes())),
        base_url="http://127.0.0.1",
    )


def test_hub_records_a_read_under_the_verified_caller_and_returns_the_row(read_hub):
    res = read_hub.post(READ_URL, headers=_as_reader(), json=_read_body())
    assert res.status_code == 200, res.text
    [row] = review_reads.for_pr(READ_REPO, 123)
    assert res.json()["id"] == row["id"]
    assert res.json()["reader"] == "Desktop_Agent1"
    assert (row["kind"], row["reader"], row["verdict"]) == (
        review_reads.STEWARD_READ,
        "Desktop_Agent1",
        "pass",
    )
    assert _gate(H1)["status"] == "read-at-head"


@pytest.mark.parametrize(
    "body, status, message",
    [
        # The identity is the credential's, never the body's.
        (_read_body(reader="Desktop_Agent1"), 400, "authenticated identity"),
        (_read_body(expected_reader="Desktop_Agent5"), 403, "Desktop_Agent5"),
        (_read_body(approved=True), 400, "unknown field"),
        (_read_body(kind=review_reads.POLICY), 409, "kind must be one of"),
        (_read_body(kind=review_reads.HUMAN, evidence_url="u"), 409, "not a human identity"),
        (_read_body(kind=review_reads.DELTA_READ, from_sha=H2), 409, "evidence"),
        (_read_body(head_sha="abc"), 409, "40-character"),
        (_read_body(number=True), 409, "positive integer"),
        (_read_body(repo="not a repo"), 409, "owner/name"),
        (_read_body(files=[["nested"]]), 409, "files entry"),
        (_read_body(verdict=""), 409, "verdict"),
        (_read_body(dry_run="yes"), 400, "dry_run"),
    ],
)
def test_hub_refuses_a_malformed_or_misattributed_read(read_hub, body, status, message):
    res = read_hub.post(READ_URL, headers=_as_reader(), json=body)
    assert res.status_code == status, res.text
    assert message in res.json()["error"]
    assert review_reads.for_pr(READ_REPO, 123) == []


def test_hub_refuses_a_read_at_a_head_that_is_not_live(read_hub, monkeypatch):
    monkeypatch.setattr(review_cli, "_live_pr_head", lambda repo, number: H2)
    res = read_hub.post(READ_URL, headers=_as_reader(), json=_read_body())
    assert res.status_code == 409, res.text
    assert "is not the pull request's current head" in res.json()["error"]
    assert review_reads.for_pr(READ_REPO, 123) == []


def test_hub_admits_only_named_readers_and_fails_closed(read_hub, monkeypatch):
    peer = read_hub.post(READ_URL, headers=_as_reader("Desktop_Agent2"), json=_read_body())
    assert peer.status_code == 403, peer.text
    assert "UA_REVIEW_READERS" in peer.json()["error"]
    monkeypatch.delenv("UA_REVIEW_READERS")
    unset = read_hub.post(READ_URL, headers=_as_reader(), json=_read_body())
    assert unset.status_code == 403, unset.text
    assert read_hub.post(READ_URL, json=_read_body()).status_code == 401
    assert review_reads.for_pr(READ_REPO, 123) == []


def test_hub_lets_the_operator_record_a_human_read_without_being_listed(read_hub):
    res = read_hub.post(
        READ_URL,
        headers=_as_reader(READ_OPERATOR),
        json=_read_body(kind=review_reads.HUMAN, evidence_url="https://example.invalid/r/1"),
    )
    assert res.status_code == 200, res.text
    [row] = review_reads.for_pr(READ_REPO, 123)
    assert (row["kind"], row["reader"]) == (review_reads.HUMAN, READ_OPERATOR)


def test_hub_applies_the_delta_read_rules(read_hub, monkeypatch):
    monkeypatch.setattr(review_cli, "_live_pr_head", lambda repo, number: H2)
    monkeypatch.setattr(review_cli, "_delta_paths", lambda repo, base, head: ["deploy/x.sh"])
    body = _read_body(
        head_sha=H2,
        kind=review_reads.DELTA_READ,
        from_sha=H1,
        evidence_url="u",
        files=["deploy/x.sh"],
    )
    no_base = read_hub.post(READ_URL, headers=_as_reader(), json=body)
    assert no_base.status_code == 409 and "no full read is recorded" in no_base.json()["error"]
    monkeypatch.setattr(review_cli, "_live_pr_head", lambda repo, number: H1)
    assert read_hub.post(READ_URL, headers=_as_reader(), json=_read_body()).status_code == 200
    monkeypatch.setattr(review_cli, "_live_pr_head", lambda repo, number: H2)
    unnamed = read_hub.post(READ_URL, headers=_as_reader(), json={**body, "files": ["src/a.py"]})
    assert unnamed.status_code == 409 and "does not name red-tier" in unnamed.json()["error"]
    assert read_hub.post(READ_URL, headers=_as_reader(), json=body).status_code == 200
    assert {r["kind"] for r in review_reads.for_pr(READ_REPO, 123)} == {
        review_reads.STEWARD_READ,
        review_reads.DELTA_READ,
    }


def test_hub_dry_run_applies_the_refusals_and_writes_nothing(read_hub, monkeypatch):
    res = read_hub.post(READ_URL, headers=_as_reader(), json=_read_body(dry_run=True))
    assert res.status_code == 200 and res.json()["status"] == "dry-run"
    assert review_reads.for_pr(READ_REPO, 123) == []
    monkeypatch.setattr(review_cli, "_live_pr_head", lambda repo, number: None)
    stale = read_hub.post(READ_URL, headers=_as_reader(), json=_read_body(dry_run=True))
    assert stale.status_code == 409


def test_hub_answers_a_locked_store_and_an_oversized_body(read_hub, monkeypatch):
    def locked(**_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(review_reads, "record", locked)
    busy = read_hub.post(READ_URL, headers=_as_reader(), json=_read_body())
    assert busy.status_code == 503, busy.text
    assert "locked" not in busy.json()["error"]
    big = read_hub.post(READ_URL, headers=_as_reader(), content=b" " * 2_000_000)
    assert big.status_code == 413, big.text


def test_hub_publishes_the_recorded_head_once_and_not_on_a_dry_run(read_hub, monkeypatch):
    """#2410: the merged head has no open PR for the poll to reach, so the write
    publishes it itself — once, after the row commits, and never on a dry run."""
    calls = []

    def publish(**kwargs):
        calls.append(kwargs)
        return {"status": "review_head_posted", "conclusion": "success"}

    monkeypatch.setattr(github_app_worker, "publish_review_head", publish)
    dry = read_hub.post(READ_URL, headers=_as_reader(), json=_read_body(dry_run=True))
    assert dry.status_code == 200 and dry.json()["review_head"] is None
    assert calls == []

    res = read_hub.post(READ_URL, headers=_as_reader(), json=_read_body())
    assert res.status_code == 200, res.text
    assert calls == [{"repo": READ_REPO, "number": 123, "head_sha": H1}]
    assert res.json()["review_head"] == {"status": "review_head_posted", "conclusion": "success"}
    [row] = review_reads.for_pr(READ_REPO, 123)
    assert res.json()["id"] == row["id"]


def test_hub_keeps_the_read_when_the_publish_fails(read_hub, monkeypatch):
    """A publish failure is reported, never rolled back over a durable row."""

    def boom(**_kwargs):
        raise RuntimeError("github is down")

    monkeypatch.setattr(github_app_worker, "publish_review_head", boom)
    res = read_hub.post(READ_URL, headers=_as_reader(), json=_read_body())
    assert res.status_code == 200, res.text
    assert res.json()["review_head"] == {"status": "error", "error": "RuntimeError"}
    [row] = review_reads.for_pr(READ_REPO, 123)
    assert res.json()["id"] == row["id"]
    assert _gate(H1)["status"] == "read-at-head"


@pytest.fixture
def read_client(isolated_env, monkeypatch):
    """A hub client: the retired-store marker present, the hub answered by a mock."""
    storage.no_local_memory_db_marker().write_text("", encoding="utf-8")
    sent: list[httpx.Request] = []
    reply = SimpleNamespace(status=200, body={"status": "recorded", "id": 7})

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(reply.status, json=reply.body)

    real_client = httpx.Client

    def client(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(review_cli.httpx, "Client", client)
    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://hub.example:8765/mcp")
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", "fixture-reader-token")
    monkeypatch.setattr(
        review_cli, "_live_pr_head", lambda repo, number: pytest.fail("the hub checks the head")
    )
    return SimpleNamespace(sent=sent, reply=reply)


_CLIENT_ARGV = ("record-read", "--repo", READ_REPO, "--pr", "123", "--head", H1)


def test_cli_on_a_hub_client_records_through_the_hub(read_client, capsys):
    argv = (*_CLIENT_ARGV, "--reader", "Desktop_Agent1", "--files", "src/a.py")
    assert main(list(argv)) == 0
    [request] = read_client.sent
    assert str(request.url) == "http://hub.example:8765/api/review/reads"
    assert request.headers["Authorization"] == "Bearer fixture-reader-token"
    assert json.loads(request.content) == {
        "repo": READ_REPO,
        "number": 123,
        "head_sha": H1,
        "kind": "steward-read",
        "verdict": "pass",
        "files": ["src/a.py"],
        "expected_reader": "Desktop_Agent1",
        "dry_run": False,
    }
    assert json.loads(capsys.readouterr().out)["id"] == 7


def test_cli_on_a_hub_client_reports_a_refusal(read_client, capsys):
    read_client.reply.status = 409
    read_client.reply.body = {"error": "head is not the pull request's current head"}
    assert main([*_CLIENT_ARGV, "--reader", "Desktop_Agent1"]) == 2
    assert "current head" in capsys.readouterr().err


def test_cli_on_a_hub_client_forwards_a_dry_run(read_client, monkeypatch):
    monkeypatch.setenv("UA_REVIEW_DRY_RUN", "1")
    read_client.reply.body = {"status": "dry-run"}
    assert main([*_CLIENT_ARGV, "--reader", "Desktop_Agent1"]) == 0
    assert json.loads(read_client.sent[0].content)["dry_run"] is True


def test_cli_on_a_hub_client_needs_a_hub_url(read_client, monkeypatch, capsys):
    monkeypatch.delenv("UA_HTTP_HUB_URL")
    assert main([*_CLIENT_ARGV, "--reader", "Desktop_Agent1"]) == 2
    assert "UA_HTTP_HUB_URL" in capsys.readouterr().err
    assert read_client.sent == []
