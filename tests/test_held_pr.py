"""Held-despite-green PR detection (task #1260, Epic C #1188, company OS step 5).

`held_despite_green()` / `is_ci_green()` are pure functions — no I/O, no
subprocess, no database — so those get synthetic-data tests only
(`TestIsCiGreen` / `TestHeldDespiteGreen`). The `gh` I/O wrappers
(`gh_pr_check_conclusions`, `label_held_pr`) and the task-store-backed
`resolve_pr_ref_for_task` / `label_held_task` are exercised below with
monkeypatched `subprocess.run`/`shutil.which` — the same seam-patching
convention this repo's other `gh`-calling modules use — plus the CLI
dispatch layer in `tests/test_review_cli.py::TestLabelHeld`.
"""

from __future__ import annotations

from unified_agent import held_pr, reviewer
from unified_agent.held_pr import HeldVerdict, held_despite_green, is_ci_green
from unified_agent.tools.tasks import task_add


class TestIsCiGreen:
    def test_no_data_is_none(self):
        assert is_ci_green(None) is None
        assert is_ci_green([]) is None
        assert is_ci_green({}) is None

    def test_all_success_is_green(self):
        assert is_ci_green(["success", "success"]) is True

    def test_mapping_values_are_read(self):
        assert is_ci_green({"test": "success", "lint": "success"}) is True

    def test_one_failure_is_red(self):
        assert is_ci_green(["success", "failure"]) is False

    def test_skipped_and_neutral_count_as_green(self):
        # This repo's own changes-gated jobs (protocol/client/web) report
        # "skipped" on a Python-only PR — that must not read as "not green".
        assert is_ci_green(["success", "skipped", "neutral"]) is True

    def test_cancelled_and_timed_out_are_red(self):
        assert is_ci_green(["cancelled"]) is False
        assert is_ci_green(["timed_out"]) is False

    def test_case_insensitive(self):
        assert is_ci_green(["SUCCESS", "Skipped"]) is True


class TestHeldDespiteGreen:
    def test_no_rework_signal_is_not_held(self):
        notes = "[review: validated @ 2026-09-20T00:00:00Z] gates green at abc123"
        v = held_despite_green(notes, ci_conclusions=["success"])
        assert v.held is False
        assert "no rework signal" in v.reason

    def test_rework_note_with_green_ci_is_held(self):
        notes = (
            "[review: validated @ 2026-09-20T00:00:00Z] gates green at abc123; "
            "ready for human merge — https://github.com/anonb3ll/unified-agent/pull/42\n"
            "[review: rework — needs a rename before merge]"
        )
        v = held_despite_green(notes, ci_conclusions=["success", "success"])
        assert v.held is True
        assert isinstance(v, HeldVerdict)
        assert v.rework_note is not None
        assert "rework" in v.rework_note.lower()
        assert v.validated_note is not None

    def test_rework_note_with_red_ci_is_not_held(self):
        # Held for a real reason (CI itself was red) — not the "despite
        # green" signature this rule targets.
        notes = "[review: rework — fix the failing test]"
        v = held_despite_green(notes, ci_conclusions=["failure"])
        assert v.held is False
        assert "not green" in v.reason

    def test_rework_note_with_no_ci_data_is_not_held(self):
        # Absent CI data must never be treated as "green" — that would
        # false-positive on every PR the check couldn't reach the Checks API
        # for.
        notes = "[review: rework — needs another pass]"
        v = held_despite_green(notes, ci_conclusions=None)
        assert v.held is False
        assert "no CI conclusions" in v.reason

    def test_explicit_verdict_rework_counts_as_signal(self):
        v = held_despite_green("", verdict="rework", ci_conclusions=["success"])
        assert v.held is True

    def test_explicit_verdict_is_case_insensitive_and_trims(self):
        v = held_despite_green("", verdict="  REWORK  ", ci_conclusions=["success"])
        assert v.held is True

    def test_only_the_documented_rework_verdict_counts(self):
        # The documented marker is `[review: rework ...]`; incidental words
        # elsewhere in an annotation are not a hold.
        assert not held_despite_green("[review: needs fix]", ci_conclusions=["success"]).held
        assert not held_despite_green(
            "[review: changes requested]", ci_conclusions=["success"]
        ).held
        assert held_despite_green(
            "[review: rework — flaky retry path]", ci_conclusions=["success"]
        ).held

    def test_unfinished_checks_are_not_green(self):
        assert (
            held_despite_green(
                "[review: rework]", ci_conclusions={"test": "IN_PROGRESS", "lint": "SUCCESS"}
            ).held
            is False
        )

    def test_merged_note_alone_is_not_a_rework_signal(self):
        v = held_despite_green("[review: approved @ ts] merged by automated review")
        assert v.held is False

    def test_uses_the_last_rework_note_not_the_first(self):
        # Notes are append-only; an earlier round's rework note must not be
        # the one reported once a later round supersedes it.
        notes = (
            "[review: rework — round 1, typo in docstring]\n"
            "[review: approved @ ts] round 1 merged\n"
            "[review: rework — round 2, missing null check]"
        )
        v = held_despite_green(notes, ci_conclusions=["success"])
        assert v.held is True
        assert "round 2" in v.rework_note

    def test_approval_closes_the_outstanding_rework_round(self):
        notes = "[review: rework — fix retry]\n[review: approved @ ts]"
        assert not held_despite_green(notes, ci_conclusions=["success"]).held
        assert held_despite_green(
            notes + "\n[review: rework — new issue]", ci_conclusions=["success"]
        ).held

    def test_empty_notes_and_no_verdict_is_not_held(self):
        v = held_despite_green("", ci_conclusions=["success"])
        assert v.held is False

    def test_ci_conclusions_default_to_none_when_omitted(self):
        v = held_despite_green("[review: rework]")
        assert v.held is False
        assert "no CI conclusions" in v.reason


# ── gh I/O wrappers (subprocess/shutil monkeypatched) ──────────────────────


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestGhPrCheckConclusions:
    def test_gh_missing_returns_none(self, monkeypatch):
        monkeypatch.setattr(held_pr.shutil, "which", lambda name: None)
        assert held_pr.gh_pr_check_conclusions("123") is None

    def test_parses_conclusion_and_state_fields(self, monkeypatch):
        monkeypatch.setattr(held_pr.shutil, "which", lambda name: "/usr/bin/gh")
        payload = {
            "statusCheckRollup": [
                {"name": "test", "conclusion": "SUCCESS"},
                {"name": "lint", "conclusion": "SUCCESS"},
                {"context": "legacy-status", "state": "FAILURE"},
            ]
        }
        import json as _json

        monkeypatch.setattr(
            held_pr.subprocess,
            "run",
            lambda *a, **k: _FakeCompleted(0, _json.dumps(payload)),
        )
        result = held_pr.gh_pr_check_conclusions("123")
        assert result == {"test": "SUCCESS", "lint": "SUCCESS", "legacy-status": "FAILURE"}

    def test_coderabbit_check_run_does_not_count_as_ci(self, monkeypatch):
        """A CodeRabbit CheckRun cannot pass or fail the CI conclusion map."""
        monkeypatch.setattr(held_pr.shutil, "which", lambda name: "/usr/bin/gh")
        payload = {
            "statusCheckRollup": [
                {"name": "test", "workflowName": "CI", "conclusion": "SUCCESS"},
                {
                    "__typename": "CheckRun",
                    "name": "CodeRabbit",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                },
                {"__typename": "StatusContext", "context": "Devin Review", "state": "SUCCESS"},
                {"__typename": "StatusContext", "context": "Approval Ledger", "state": "SUCCESS"},
            ]
        }
        import json as _json

        monkeypatch.setattr(
            held_pr.subprocess,
            "run",
            lambda *a, **k: _FakeCompleted(0, _json.dumps(payload)),
        )
        result = held_pr.gh_pr_check_conclusions("123")
        assert "CodeRabbit" not in result
        assert result["CI / test"] == "SUCCESS"
        assert result["Devin Review"] == "SUCCESS"
        assert result["Approval Ledger"] == "SUCCESS"
        assert held_pr.is_ci_green(result) is True

        only = {
            "statusCheckRollup": [
                {
                    "__typename": "CheckRun",
                    "name": "CodeRabbit",
                    "status": "IN_PROGRESS",
                    "conclusion": "",
                }
            ]
        }
        monkeypatch.setattr(
            held_pr.subprocess,
            "run",
            lambda *a, **k: _FakeCompleted(0, _json.dumps(only)),
        )
        assert held_pr.gh_pr_check_conclusions("123") is None

    def test_github_actions_names_are_prefixed_with_their_workflow(self, monkeypatch):
        """#1787: bare GitHub Actions job names from two workflows must not collide."""
        monkeypatch.setattr(held_pr.shutil, "which", lambda name: "/usr/bin/gh")
        payload = {
            "statusCheckRollup": [
                {"name": "build", "workflowName": "CI", "conclusion": "SUCCESS"},
                {"name": "build", "workflowName": "Docker", "conclusion": "FAILURE"},
                {"name": "CI / gates", "workflowName": "", "conclusion": "SUCCESS"},
            ]
        }
        import json as _json

        monkeypatch.setattr(
            held_pr.subprocess,
            "run",
            lambda *a, **k: _FakeCompleted(0, _json.dumps(payload)),
        )
        result = held_pr.gh_pr_check_conclusions("123")
        assert result == {
            "CI / build": "SUCCESS",
            "Docker / build": "FAILURE",
            "CI / gates": "SUCCESS",
        }
        assert held_pr.is_ci_green(result) is False

    def test_skipped_duplicate_cannot_mask_a_failure(self, monkeypatch):
        """#1787: a later non-ci:full label event re-creates CI checks as skipped."""
        monkeypatch.setattr(held_pr.shutil, "which", lambda name: "/usr/bin/gh")
        payload = {
            "statusCheckRollup": [
                {"name": "gates", "workflowName": "CI", "conclusion": "FAILURE"},
                {"name": "gates", "workflowName": "CI", "conclusion": "SKIPPED"},
                {"name": "js", "workflowName": "CI", "conclusion": "SKIPPED"},
            ]
        }
        import json as _json

        monkeypatch.setattr(
            held_pr.subprocess,
            "run",
            lambda *a, **k: _FakeCompleted(0, _json.dumps(payload)),
        )
        result = held_pr.gh_pr_check_conclusions("123")
        assert result == {"CI / gates": "FAILURE", "CI / js": "SKIPPED"}
        assert held_pr.is_ci_green(result) is False

    def test_empty_rollup_returns_none(self, monkeypatch):
        monkeypatch.setattr(held_pr.shutil, "which", lambda name: "/usr/bin/gh")
        monkeypatch.setattr(
            held_pr.subprocess,
            "run",
            lambda *a, **k: _FakeCompleted(0, '{"statusCheckRollup": []}'),
        )
        assert held_pr.gh_pr_check_conclusions("123") is None

    def test_nonzero_exit_returns_none(self, monkeypatch):
        monkeypatch.setattr(held_pr.shutil, "which", lambda name: "/usr/bin/gh")
        monkeypatch.setattr(
            held_pr.subprocess, "run", lambda *a, **k: _FakeCompleted(1, "", "not found")
        )
        assert held_pr.gh_pr_check_conclusions("999") is None

    def test_exception_degrades_to_none(self, monkeypatch):
        monkeypatch.setattr(held_pr.shutil, "which", lambda name: "/usr/bin/gh")

        def boom(*a, **k):
            raise TimeoutError("gh timed out")

        monkeypatch.setattr(held_pr.subprocess, "run", boom)
        assert held_pr.gh_pr_check_conclusions("123") is None


class TestLabelHeldPr:
    def test_gh_missing_returns_false(self, monkeypatch):
        monkeypatch.setattr(held_pr.shutil, "which", lambda name: None)
        assert held_pr.label_held_pr("123") is False

    def test_success_returns_true_and_uses_add_label(self, monkeypatch):
        monkeypatch.setattr(held_pr.shutil, "which", lambda name: "/usr/bin/gh")
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return _FakeCompleted(0)

        monkeypatch.setattr(held_pr.subprocess, "run", fake_run)
        assert held_pr.label_held_pr("123", repo_path="/repo") is True
        assert seen["cmd"][1:4] == ["pr", "edit", "123"]
        assert "--add-label" in seen["cmd"]
        assert held_pr.HELD_LABEL in seen["cmd"]

    def test_failure_returns_false(self, monkeypatch):
        monkeypatch.setattr(held_pr.shutil, "which", lambda name: "/usr/bin/gh")
        monkeypatch.setattr(
            held_pr.subprocess, "run", lambda *a, **k: _FakeCompleted(1, "", "no such label")
        )
        assert held_pr.label_held_pr("123") is False

    def test_exception_degrades_to_false(self, monkeypatch):
        monkeypatch.setattr(held_pr.shutil, "which", lambda name: "/usr/bin/gh")

        def boom(*a, **k):
            raise TimeoutError("gh timed out")

        monkeypatch.setattr(held_pr.subprocess, "run", boom)
        assert held_pr.label_held_pr("123") is False


# ── task-store-backed helpers ────────────────────────────────────────────


def _make_reviewed_task(notes: str) -> int:
    t = task_add("held-pr-fixture", notes=notes)
    return t["id"]


class TestResolvePrRefForTask:
    def test_unknown_task_returns_none_none(self, isolated_env):
        assert held_pr.resolve_pr_ref_for_task(999999) == (None, None)

    def test_no_pr_marker_returns_none_none(self, isolated_env):
        tid = _make_reviewed_task("just some prose, no [review:...] marker")
        assert held_pr.resolve_pr_ref_for_task(tid) == (None, None)

    def test_pr_url_from_validated_note_is_resolved(self, isolated_env, monkeypatch):
        tid = _make_reviewed_task(
            f"{reviewer.WAITING_FOR_REVIEW}\n"
            "[review: validated @ 2026-09-20T00:00:00Z] gates green at abc123; "
            "ready for human merge — https://github.com/anonb3ll/unified-agent/pull/42"
        )
        # find_task_worktree touches git; degrade gracefully by monkeypatching
        # the merge module's function directly rather than faking git state.
        from unified_agent import merge as merge_mod

        monkeypatch.setattr(merge_mod, "find_task_worktree", lambda task_id: (None, None))
        pr_ref, repo_path = held_pr.resolve_pr_ref_for_task(tid)
        assert pr_ref == "https://github.com/anonb3ll/unified-agent/pull/42"
        assert repo_path is None

    def test_bare_pr_number_without_a_checkout_is_unresolvable(self, isolated_env, monkeypatch):
        from unified_agent import merge as merge_mod

        tid = _make_reviewed_task("x")
        monkeypatch.setattr(reviewer, "_pr_ref_from_notes", lambda notes, **_kw: "42")
        monkeypatch.setattr(merge_mod, "find_task_worktree", lambda task_id: (None, None))
        # Without a repo checkout, "42" could name a PR in any repo: refuse.
        assert held_pr.resolve_pr_ref_for_task(tid) == (None, None)


class TestLabelHeldTask:
    def test_unknown_task(self, isolated_env):
        result = held_pr.label_held_task(999999)
        assert result["ok"] is False
        assert "not found" in result["message"]

    def test_unreadable_checks_are_inconclusive_not_clean(self, isolated_env, monkeypatch):
        from unified_agent import review_cli

        tid = _make_reviewed_task("[review: rework]")
        monkeypatch.setattr(
            held_pr, "resolve_pr_ref_for_task", lambda task_id: ("https://x/pull/1", None)
        )
        monkeypatch.setattr(held_pr, "gh_pr_check_conclusions", lambda *a, **k: None)
        result = held_pr.label_held_task(tid)
        assert result["ok"] is False
        assert result["inconclusive"] is True
        assert review_cli._label_held(tid, dry_run=False, emit_json=False) == 1

    def test_no_pr_ref(self, isolated_env):
        tid = _make_reviewed_task("no marker here")
        result = held_pr.label_held_task(tid)
        assert result["ok"] is False
        assert result["held"] is False
        assert "no resolvable PR reference" in result["message"]

    def test_not_held_when_ci_not_green(self, isolated_env, monkeypatch):
        tid = _make_reviewed_task(
            "[review: validated @ ts] gates green at abc; "
            "ready for human merge — https://github.com/anonb3ll/unified-agent/pull/7\n"
            "[review: rework — flaky]"
        )
        monkeypatch.setattr(held_pr, "resolve_pr_ref_for_task", lambda t: ("7", "/repo"))
        monkeypatch.setattr(
            held_pr, "gh_pr_check_conclusions", lambda ref, repo_path=None: {"test": "failure"}
        )
        result = held_pr.label_held_task(tid)
        assert result["held"] is False
        assert result["ok"] is True

    def test_held_dry_run_never_calls_label_write(self, isolated_env, monkeypatch):
        tid = _make_reviewed_task(
            "[review: validated @ ts] gates green at abc; "
            "ready for human merge — https://github.com/anonb3ll/unified-agent/pull/7\n"
            "[review: rework — needs a rename]"
        )
        monkeypatch.setattr(held_pr, "resolve_pr_ref_for_task", lambda t: ("7", "/repo"))
        monkeypatch.setattr(
            held_pr, "gh_pr_check_conclusions", lambda ref, repo_path=None: {"test": "success"}
        )
        called = {"n": 0}
        monkeypatch.setattr(
            held_pr,
            "label_held_pr",
            lambda ref, repo_path=None: called.__setitem__("n", called["n"] + 1) or True,
        )
        result = held_pr.label_held_task(tid, dry_run=True)
        assert result["held"] is True
        assert result["labeled"] is False
        assert called["n"] == 0

    def test_held_labels_the_pr(self, isolated_env, monkeypatch):
        tid = _make_reviewed_task(
            "[review: validated @ ts] gates green at abc; "
            "ready for human merge — https://github.com/anonb3ll/unified-agent/pull/7\n"
            "[review: rework — needs a rename]"
        )
        monkeypatch.setattr(held_pr, "resolve_pr_ref_for_task", lambda t: ("7", "/repo"))
        monkeypatch.setattr(
            held_pr, "gh_pr_check_conclusions", lambda ref, repo_path=None: {"test": "success"}
        )
        monkeypatch.setattr(held_pr, "label_held_pr", lambda ref, repo_path=None: True)
        result = held_pr.label_held_task(tid)
        assert result["held"] is True
        assert result["labeled"] is True
        assert result["ok"] is True

    def test_held_but_label_write_fails(self, isolated_env, monkeypatch):
        tid = _make_reviewed_task(
            "[review: validated @ ts] gates green at abc; "
            "ready for human merge — https://github.com/anonb3ll/unified-agent/pull/7\n"
            "[review: rework — needs a rename]"
        )
        monkeypatch.setattr(held_pr, "resolve_pr_ref_for_task", lambda t: ("7", "/repo"))
        monkeypatch.setattr(
            held_pr, "gh_pr_check_conclusions", lambda ref, repo_path=None: {"test": "success"}
        )
        monkeypatch.setattr(held_pr, "label_held_pr", lambda ref, repo_path=None: False)
        result = held_pr.label_held_task(tid)
        assert result["held"] is True
        assert result["labeled"] is False
        assert result["ok"] is False
