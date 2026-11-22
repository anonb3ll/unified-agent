"""Tests for scripts/check_held_pr_regression_test.py (task #1260, Epic C #1188).

Company operating rule (docs/company/OPERATING-SYSTEM.md step 5): "A PR held
back despite green CI gets a regression test the same day." This is the CI
enforcement for that rule. All tests drive `evaluate()` (pure) and `main()`
(CLI dispatch) with explicit args/fixture files — no git, no gh, no network,
same convention as `test_check_staged_conflict_markers.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import check_held_pr_regression_test as gate  # noqa: E402


class TestTouchesTestFile:
    def test_no_test_files_returns_none(self):
        assert gate.touches_test_file(["src/a.py", "docs/x.md"]) is None

    def test_tests_dir_file_matches(self):
        assert gate.touches_test_file(["src/a.py", "tests/test_a.py"]) == "tests/test_a.py"

    def test_tests_dir_nested_matches(self):
        assert gate.touches_test_file(["tests/connectors/test_slack.py"]) is not None

    def test_dunder_tests_dir_matches(self):
        assert gate.touches_test_file(["packages/web/src/__tests__/Foo.test.tsx"]) is not None

    def test_packages_src_dot_test_matches(self):
        assert gate.touches_test_file(["packages/web/src/quota/DeliverySlo.test.tsx"]) is not None

    def test_unrelated_test_word_in_path_does_not_match(self):
        # "testing_utils.py" at the repo root is not a tests/ path.
        assert gate.touches_test_file(["testing_utils.py"]) is None

    def test_empty_list_returns_none(self):
        assert gate.touches_test_file([]) is None


class TestFindRegressionTrailer:
    def test_no_trailer_returns_none(self):
        assert gate.find_regression_trailer(["just a normal PR body"]) is None

    def test_trailer_in_pr_body_is_found(self):
        body = "Fixes the thing.\n\nRegression-Test: tests/test_x.py::test_case"
        assert gate.find_regression_trailer([body]) == "tests/test_x.py::test_case"

    def test_trailer_in_commit_message_is_found(self):
        assert (
            gate.find_regression_trailer(["subject line", "Regression-Test: tests/test_y.py"])
            == "tests/test_y.py"
        )

    def test_trailer_without_test_name_suffix(self):
        assert gate.find_regression_trailer(["Regression-Test: tests/test_z.py"]) == (
            "tests/test_z.py"
        )

    def test_trailer_must_be_its_own_line(self):
        assert gate.find_regression_trailer(["prose mentioning Regression-Test: inline"]) is None


class TestEvaluate:
    def test_never_held_skips_regardless_of_changes(self):
        result = gate.evaluate(held=False, changed_files=[], texts=[])
        assert result.ok is True
        assert result.skipped is True
        assert "never held" in result.message

    def test_held_with_test_file_change_passes(self):
        result = gate.evaluate(held=True, changed_files=["src/a.py", "tests/test_a.py"], texts=[""])
        assert result.ok is True
        assert result.skipped is False
        assert "tests/test_a.py" in result.message

    def test_held_with_no_test_and_no_trailer_fails(self):
        result = gate.evaluate(held=True, changed_files=["src/a.py"], texts=["just a PR body"])
        assert result.ok is False
        assert result.skipped is False
        assert "held despite green CI" in result.message
        assert "OPERATING-SYSTEM.md step 5" in result.message

    def test_held_with_valid_trailer_passes(self, tmp_path):
        test_file = tmp_path / "tests" / "test_regression.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("def test_x(): pass\n")
        result = gate.evaluate(
            held=True,
            changed_files=["src/a.py"],
            texts=["Regression-Test: tests/test_regression.py::test_x"],
            repo_root=tmp_path,
        )
        assert result.ok is True
        assert "Regression-Test" in result.message

    def test_held_with_trailer_pointing_at_missing_file_fails(self, tmp_path):
        result = gate.evaluate(
            held=True,
            changed_files=["src/a.py"],
            texts=["Regression-Test: tests/does_not_exist.py::test_x"],
            repo_root=tmp_path,
        )
        assert result.ok is False
        assert "points at an existing test file" in result.message

    def test_a_stale_first_trailer_does_not_mask_a_valid_later_one(self, tmp_path):
        real = tmp_path / "tests" / "test_real.py"
        real.parent.mkdir(parents=True)
        real.write_text("def test_it():\n    pass\n")
        result = gate.evaluate(
            held=True,
            changed_files=["src/a.py"],
            texts=[
                "Regression-Test: tests/gone.py::test_x",
                "Regression-Test: tests/test_real.py::test_it",
            ],
            repo_root=tmp_path,
        )
        assert result.ok is True
        assert "tests/test_real.py::test_it" in result.message

    def test_held_with_trailer_and_no_repo_root_skips_existence_check(self):
        # repo_root=None: only presence of the trailer matters.
        result = gate.evaluate(
            held=True,
            changed_files=[],
            texts=["Regression-Test: tests/whatever.py::test_x"],
            repo_root=None,
        )
        assert result.ok is True

    def test_test_file_change_takes_priority_over_missing_trailer_check(self):
        # A real test-file change should never be penalized just because no
        # trailer was also supplied.
        result = gate.evaluate(
            held=True, changed_files=["tests/test_a.py"], texts=["no trailer here"]
        )
        assert result.ok is True


class TestRegressionTrailerTargetExists:
    def test_strips_double_colon_node_id(self, tmp_path):
        f = tmp_path / "tests" / "test_x.py"
        f.parent.mkdir(parents=True)
        f.write_text("def test_case():\n    pass\n")
        assert gate.regression_trailer_target_exists("tests/test_x.py::test_case", tmp_path)
        assert not gate.regression_trailer_target_exists("tests/test_x.py::test_other", tmp_path)

    def test_non_test_files_and_directories_do_not_count(self, tmp_path):
        (tmp_path / "README.md").write_text("docs\n")
        (tmp_path / "tests").mkdir()
        assert not gate.regression_trailer_target_exists("README.md", tmp_path)
        assert not gate.regression_trailer_target_exists("tests", tmp_path)

    def test_missing_file_is_false(self, tmp_path):
        assert not gate.regression_trailer_target_exists("tests/nope.py", tmp_path)


class TestLoadListArg:
    def test_csv_inline(self):
        assert gate._load_list_arg("a.py,b.py", None) == ["a.py", "b.py"]

    def test_json_array_inline(self):
        assert gate._load_list_arg('["a.py", "b.py"]', None) == ["a.py", "b.py"]

    def test_json_file(self, tmp_path):
        p = tmp_path / "files.json"
        p.write_text(json.dumps(["x.py", "y.py"]))
        assert gate._load_list_arg(None, str(p)) == ["x.py", "y.py"]

    def test_none_when_neither_given(self):
        assert gate._load_list_arg(None, None) is None

    def test_file_takes_priority_over_inline(self, tmp_path):
        p = tmp_path / "files.json"
        p.write_text(json.dumps(["from-file.py"]))
        assert gate._load_list_arg("from-inline.py", str(p)) == ["from-file.py"]


class TestEventPayloadParsing:
    def test_held_label_present(self):
        event = {"pull_request": {"labels": [{"name": "held"}, {"name": "other"}]}}
        assert gate._held_from_event(event) is True

    def test_held_label_absent(self):
        event = {"pull_request": {"labels": [{"name": "other"}]}}
        assert gate._held_from_event(event) is False

    def test_no_labels_key(self):
        assert gate._held_from_event({"pull_request": {}}) is False

    def test_empty_event(self):
        assert gate._held_from_event({}) is False

    def test_pr_body_extraction(self):
        event = {"pull_request": {"body": "the body text"}}
        assert gate._pr_body_from_event(event) == "the body text"

    def test_pr_body_missing_defaults_empty(self):
        assert gate._pr_body_from_event({}) == ""


# ── main() CLI dispatch ─────────────────────────────────────────────────


class TestMainCli:
    def test_never_held_exits_zero(self, capsys):
        code = gate.main(["--held", "false"])
        out = capsys.readouterr().out
        assert code == 0
        assert "never held" in out

    def test_held_no_test_no_trailer_exits_one(self, capsys):
        code = gate.main(["--held", "true", "--changed-files", "src/a.py"])
        out = capsys.readouterr().out
        assert code == 1
        assert "held despite green CI" in out

    def test_held_with_test_change_exits_zero(self, capsys):
        code = gate.main(["--held", "true", "--changed-files", "src/a.py,tests/test_a.py"])
        out = capsys.readouterr().out
        assert code == 0
        assert "tests/test_a.py" in out

    def test_held_with_trailer_in_pr_body_file(self, tmp_path, capsys):
        test_file = tmp_path / "tests" / "test_regression.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("def test_x(): pass\n")
        body_file = tmp_path / "body.txt"
        body_file.write_text("Fixes it.\n\nRegression-Test: tests/test_regression.py::test_x\n")

        code = gate.main(
            [
                "--held",
                "true",
                "--changed-files",
                "src/a.py",
                "--pr-body-file",
                str(body_file),
                "--repo-root",
                str(tmp_path),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Regression-Test" in out

    def test_held_with_changed_files_json_file(self, tmp_path, capsys):
        files_json = tmp_path / "changed.json"
        files_json.write_text(json.dumps(["tests/test_new_thing.py"]))
        code = gate.main(["--held", "true", "--changed-files-json", str(files_json)])
        assert code == 0

    def test_held_with_no_changed_file_source_and_no_base_sha_exits_two(self, capsys):
        code = gate.main(["--held", "true"])
        err = capsys.readouterr().err
        assert code == 2
        assert "no changed-file source" in err

    def test_json_output_flag(self, capsys):
        code = gate.main(["--held", "false", "--json"])
        out = capsys.readouterr().out
        assert code == 0
        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["skipped"] is True

    def test_held_and_event_path_are_mutually_exclusive(self):
        import pytest

        with pytest.raises(SystemExit):
            gate.main(["--held", "true", "--event-path", "/nonexistent.json"])

    def test_event_path_drives_held_detection(self, tmp_path, capsys):
        event = {
            "pull_request": {
                "labels": [{"name": "held"}],
                "body": "Regression-Test: tests/test_e.py::test_x",
            }
        }
        event_file = tmp_path / "event.json"
        event_file.write_text(json.dumps(event))
        test_file = tmp_path / "tests" / "test_e.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("def test_x(): pass\n")

        code = gate.main(
            [
                "--event-path",
                str(event_file),
                "--changed-files",
                "src/a.py",
                "--repo-root",
                str(tmp_path),
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "Regression-Test" in out

    def test_event_path_without_held_label_skips(self, tmp_path, capsys):
        event = {"pull_request": {"labels": [{"name": "some-other-label"}]}}
        event_file = tmp_path / "event.json"
        event_file.write_text(json.dumps(event))

        code = gate.main(["--event-path", str(event_file)])
        out = capsys.readouterr().out
        assert code == 0
        assert "never held" in out

    def test_unreadable_event_is_an_error_not_a_skip(self, tmp_path, capsys):
        bad = tmp_path / "event.json"
        bad.write_text("{not json")
        assert gate.main(["--event-path", str(bad)]) == 2
        assert gate.main(["--event-path", str(tmp_path / "missing.json")]) == 2
        assert "cannot tell whether this PR is held" in capsys.readouterr().err

    def test_trailer_outside_the_checkout_never_counts(self, tmp_path):
        outside = tmp_path / "elsewhere" / "tests" / "test_evil.py"
        outside.parent.mkdir(parents=True)
        outside.write_text("def test_x():\n    pass\n")
        repo = tmp_path / "repo"
        repo.mkdir()
        assert not gate.regression_trailer_target_exists(
            "../elsewhere/tests/test_evil.py::test_x", repo
        )
        assert not gate.regression_trailer_target_exists(f"{outside}::test_x", repo)

    def test_no_held_signal_source_at_all_warns_and_skips(self, monkeypatch, capsys):
        monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
        code = gate.main([])
        captured = capsys.readouterr()
        assert code == 0
        assert "assuming this PR was never held" in captured.err
        assert "never held" in captured.out


class TestDeletedTestsAndLateness:
    def test_deleting_a_test_is_not_coverage(self, tmp_path):
        import subprocess

        def git(*args):
            subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_old.py").write_text("def test_old():\n    pass\n")
        (tmp_path / "src.py").write_text("x = 1\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
        ).stdout.strip()
        (tmp_path / "tests" / "test_old.py").unlink()
        (tmp_path / "src.py").write_text("x = 2\n")
        git("add", "-A")
        git("commit", "-qm", "delete test")

        changed = gate._git_changed_files(base, "HEAD", tmp_path)
        assert changed == ["src.py"]
        assert gate.evaluate(held=True, changed_files=changed, texts=[]).ok is False

    def test_late_warning_after_a_day_but_not_before(self):
        from datetime import UTC, datetime, timedelta

        held = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
        assert gate.late_warning(held, held + timedelta(hours=5)) is None
        warning = gate.late_warning(held, held + timedelta(hours=30))
        assert warning is not None and warning.startswith("LATE")
        assert gate.late_warning(None, held) is None

    def test_trailer_coverage_reports_late_test_delivery(self, tmp_path, monkeypatch, capsys):
        from datetime import UTC, datetime, timedelta

        test_path = tmp_path / "tests" / "test_regression.py"
        test_path.parent.mkdir()
        test_path.write_text("def test_retry(): pass\n")
        held = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
        monkeypatch.setattr(
            gate, "_git_last_touch", lambda path, head, root: held + timedelta(hours=30)
        )

        code = gate.main(
            [
                "--held",
                "true",
                "--changed-files",
                "[]",
                "--pr-body",
                "Regression-Test: tests/test_regression.py::test_retry",
                "--held-at",
                held.isoformat(),
                "--base-sha",
                "base",
                "--repo-root",
                str(tmp_path),
                "--json",
            ]
        )
        assert code == 0
        assert "LATE: regression test landed 30h" in capsys.readouterr().out
