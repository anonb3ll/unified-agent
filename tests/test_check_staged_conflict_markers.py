"""Tests for scripts/check_staged_conflict_markers.py (task #1016).

Route 2 of the worktree-contamination incident: a `git stash` / `git stash
pop` collision (the stash stack is gitdir-global, not per-worktree) applied
someone else's unrelated, half-resolved conflict into a fresh worktree,
leaving conflict markers in staged files. This is the load-bearing logic for
the pre-commit guard that `scripts/install-hooks.sh` wires up to catch that
signature before it reaches a commit.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import check_staged_conflict_markers as guard  # noqa: E402


class TestFindConflictMarkersInText:
    def test_clean_text_has_no_hits(self):
        text = "def f():\n    return 1\n"
        assert guard.find_conflict_markers_in_text(text) == []

    def test_detects_all_three_markers(self):
        text = "\n".join(
            [
                "before",
                "<<<<<<< HEAD",
                "ours",
                "=======",
                "theirs",
                ">>>>>>> stash@{0}",
                "after",
            ]
        )
        hits = guard.find_conflict_markers_in_text(text)
        assert hits == [
            (2, "<<<<<<<"),
            (4, "======="),
            (6, ">>>>>>>"),
        ]

    def test_marker_must_be_anchored_at_line_start(self):
        # Indented or mid-line occurrences are not git's conflict-marker
        # convention and must not trip the guard.
        text = "    <<<<<<< not a real marker\nsome text ======= more text\n"
        assert guard.find_conflict_markers_in_text(text) == []

    def test_short_rules_do_not_match(self):
        # A markdown-style horizontal rule or a shorter equals-run must not
        # be mistaken for the seven-character git marker.
        text = "=====\n---\n"
        assert guard.find_conflict_markers_in_text(text) == []

    def test_longer_run_still_matches_via_startswith(self):
        # git itself sometimes emits >7 marker chars with a ref name
        # appended directly (rare); startswith should still catch it.
        text = "<<<<<<<<<< extra\nours\n>>>>>>> theirs\n"
        assert guard.find_conflict_markers_in_text(text)[0] == (1, "<<<<<<<")

    def test_lone_equals_run_is_a_markdown_heading_not_a_conflict(self):
        """A setext H1 underline must not be reported as a conflict marker.

        `Title` followed by a run of `=` at column 0 is how Markdown writes an
        H1, and how reStructuredText underlines a section. Without the
        require-a-complete-block rule this is indistinguishable from a
        conflict's middle marker, and the guard would block a correctly
        written document with a message about merge conflicts.
        """
        text = "Release Notes\n=============\n\nBody text.\n"
        assert guard.find_conflict_markers_in_text(text) == []

    def test_opening_marker_alone_is_not_a_block(self):
        # Half a block is not the signature this guard targets; an
        # unresolved `git stash pop` collision always leaves all three.
        text = "<<<<<<< HEAD\nours\n"
        assert guard.find_conflict_markers_in_text(text) == []

    def test_nested_blockquote_run_is_not_a_conflict(self):
        # Seven levels of markdown blockquote nesting starts a line with
        # seven '>' characters. Unusual, but legal prose, not a conflict.
        text = ">>>>>>> deeply nested quote\n"
        assert guard.find_conflict_markers_in_text(text) == []


class TestCheckStagedBlob:
    def test_clean_content_passes(self):
        assert guard.check_staged_blob("app.py", b"print('hello')\n") == []

    def test_conflict_markers_fail(self):
        content = b"<<<<<<< HEAD\nfoo\n=======\nbar\n>>>>>>> branch\n"
        findings = guard.check_staged_blob("memory.py", content)
        assert len(findings) == 3
        assert findings[0].path == "memory.py"
        assert findings[0].line_number == 1
        assert findings[0].marker == "<<<<<<<"

    def test_binary_content_is_skipped(self):
        content = b"\x00\x01\xff<<<<<<<\x00"
        assert guard.check_staged_blob("image.png", content) == []

    def test_non_utf8_text_is_skipped_not_crashed(self):
        content = b"\xff\xfe<<<<<<<binarylatin1garbage\xe9"
        # Must not raise; undecodable content is treated as out of scope
        # rather than a false positive or a crash.
        assert guard.check_staged_blob("weird.bin", content) == []

    def test_exempt_path_is_skipped_even_with_markers(self, monkeypatch):
        monkeypatch.setattr(
            guard, "EXEMPT_PATHS", frozenset({"tests/fixtures/conflict_sample.txt"})
        )
        content = b"<<<<<<< HEAD\n=======\n>>>>>>> branch\n"
        assert guard.check_staged_blob("tests/fixtures/conflict_sample.txt", content) == []
        # A non-exempt path with identical content still fails.
        assert guard.check_staged_blob("other.txt", content) != []


class TestCheckStagedPaths:
    def test_all_clean_passes(self):
        result = guard.check_staged_paths([("a.py", b"x = 1\n"), ("b.py", b"y = 2\n")])
        assert result.ok
        assert result.findings == []

    def test_one_dirty_file_fails_and_is_reported(self):
        result = guard.check_staged_paths(
            [
                ("clean.py", b"x = 1\n"),
                ("dirty.py", b"<<<<<<< HEAD\n=======\n>>>>>>> x\n"),
            ]
        )
        assert not result.ok
        assert {f.path for f in result.findings} == {"dirty.py"}

    def test_format_report_mentions_each_finding(self):
        result = guard.check_staged_paths(
            [("dirty.py", b"<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> x\n")]
        )
        report = result.format_report()
        assert "dirty.py:1" in report
        assert "commit blocked" in report
