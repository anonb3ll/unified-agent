"""scripts/changelog_assemble.py folds changelog.d/ fragments into CHANGELOG.md (task #1808).

The point of fragments is that two PRs never touch the same file, so the acceptance test
proves that with real git merges.
"""

import difflib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "changelog_assemble", REPO_ROOT / "scripts" / "changelog_assemble.py"
)
ca = importlib.util.module_from_spec(spec)
sys.modules["changelog_assemble"] = ca  # dataclasses resolve annotations through it
spec.loader.exec_module(ca)

BASE = """# Changelog

Intro.

## [Unreleased]

- **Loose note (#1).** Before any subsection.

### Added

- **Existing add (#5).** Old.

### Fixed

- **Existing fix (#6).** Old.

## [1.0.0] - 2026-01-01

### Added

- **Released (#2).** Never touched.
"""


def write(directory: Path, name: str, text: str) -> None:
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(BASE, encoding="utf-8")
    return tmp_path


def run(repo, *args):
    return ca.main(
        [
            "--changelog",
            str(repo / "CHANGELOG.md"),
            "--fragments-dir",
            str(repo / "changelog.d"),
            *args,
        ]
    )


def test_groups_in_keep_a_changelog_order_and_sorts_stably(repo):
    d = repo / "changelog.d"
    write(d, "20.fixed.md", "- fixed twenty\n")
    write(d, "3.security.md", "- security three\n")
    write(d, "100.added.md", "- added hundred\n")
    write(d, "9.added.md", "- added nine\n")
    write(d, "9-2.added.md", "- added nine b\n")
    write(d, "7.removed.md", "- removed seven\n")
    write(d, "4.deprecated.md", "- deprecated four\n")
    write(d, "2.changed.md", "- changed two\n")
    frags, problems = ca.load_fragments(d)
    assert problems == []
    # Types in Keep a Changelog order; within a type newest first (highest id, then name desc).
    assert [f.path.name for f in frags] == [
        "100.added.md",
        "9.added.md",
        "9-2.added.md",
        "2.changed.md",
        "4.deprecated.md",
        "7.removed.md",
        "20.fixed.md",
        "3.security.md",
    ]
    # Same input, same output.
    assert [f.path.name for f in ca.load_fragments(d)[0]] == [f.path.name for f in frags]


def test_non_numeric_ids_sort_after_numeric_newest_first_by_name(repo):
    d = repo / "changelog.d"
    write(d, "zeta.added.md", "- z\n")
    write(d, "alpha.added.md", "- a\n")
    write(d, "12.added.md", "- n\n")
    names = [f.path.name for f in ca.load_fragments(d)[0]]
    assert names == ["12.added.md", "zeta.added.md", "alpha.added.md"]


def test_merges_directly_under_existing_headings_newest_first(repo):
    d = repo / "changelog.d"
    write(d, "10.added.md", "- **New add (#10).** Fresh.\n")
    write(d, "11.added.md", "- **Second add (#11).**\n  Continued line.\n")
    write(d, "12.fixed.md", "- **New fix (#12).** Fresh.\n")
    assert run(repo) == 0
    text = (repo / "CHANGELOG.md").read_text()
    assert text.index("### Added") < text.index("Second add") < text.index("New add")
    assert text.index("New add") < text.index("Existing add") < text.index("### Fixed")
    assert text.index("### Fixed") < text.index("New fix") < text.index("Existing fix")
    assert text.index("Existing fix") < text.index("## [1.0.0]")
    assert text.count("### Added") == 2  # the Unreleased one plus the released one
    assert (
        "### Added\n\n- **Second add (#11).**\n  Continued line.\n\n"
        "- **New add (#10).** Fresh.\n\n- **Existing add (#5).** Old.\n\n### Fixed" in text
    )
    assert not list(d.glob("*.md"))


def test_heading_without_blank_line_still_gets_top_insertion(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text("## [Unreleased]\n\n### Added\n- old\n\n## [1.0.0]\n")
    write(tmp_path / "changelog.d", "1.added.md", "- new\n")
    assert run(tmp_path) == 0
    assert (tmp_path / "CHANGELOG.md").read_text() == (
        "## [Unreleased]\n\n### Added\n- new\n\n- old\n\n## [1.0.0]\n"
    )


def test_creates_missing_subsections_in_order(repo):
    d = repo / "changelog.d"
    write(d, "1.changed.md", "- changed\n")
    write(d, "2.security.md", "- sec\n")
    write(d, "3.removed.md", "- removed\n")
    assert run(repo) == 0
    unreleased = ca.unreleased_section((repo / "CHANGELOG.md").read_text())
    order = [ln for ln in unreleased.splitlines() if ln.startswith("### ")]
    assert order == ["### Added", "### Changed", "### Removed", "### Fixed", "### Security"]
    assert "## [1.0.0]" not in unreleased


def test_released_sections_and_existing_text_untouched(repo):
    write(repo / "changelog.d", "10.added.md", "- new\n")
    write(repo / "changelog.d", "11.security.md", "- sec\n")
    assert run(repo) == 0
    new = (repo / "CHANGELOG.md").read_text()
    # Every original line survives in order; only added lines differ.
    diff = list(difflib.ndiff(BASE.splitlines(), new.splitlines()))
    assert not [ln for ln in diff if ln.startswith("- ")]
    assert new.endswith(BASE[BASE.index("## [1.0.0]") :])


def test_no_subsections_and_empty_unreleased(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text("# C\n\n## [Unreleased]\n\n## [1.0.0]\n\nold\n")
    write(tmp_path / "changelog.d", "1.fixed.md", "- f\n")
    assert run(tmp_path) == 0
    assert (tmp_path / "CHANGELOG.md").read_text() == (
        "# C\n\n## [Unreleased]\n\n### Fixed\n\n- f\n\n## [1.0.0]\n\nold\n"
    )


def test_unreleased_is_last_section(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text("# C\n\n## [Unreleased]\n\n### Added\n\n- a\n")
    write(tmp_path / "changelog.d", "1.added.md", "- b\n")
    write(tmp_path / "changelog.d", "2.fixed.md", "- c\n")
    assert run(tmp_path) == 0
    assert (tmp_path / "CHANGELOG.md").read_text() == (
        "# C\n\n## [Unreleased]\n\n### Added\n\n- b\n\n- a\n\n### Fixed\n\n- c\n"
    )


def test_heading_inside_code_fence_is_not_a_subsection(tmp_path):
    changelog = "## [Unreleased]\n\n### Added\n\n- a\n\n```md\n### Fixed\n```\n\n## [1.0.0]\n"
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    write(tmp_path / "changelog.d", "1.fixed.md", "- f\n")
    assert run(tmp_path) == 0
    text = (tmp_path / "CHANGELOG.md").read_text()
    assert text.endswith("```\n\n### Fixed\n\n- f\n\n## [1.0.0]\n")


def test_is_idempotent(repo):
    write(repo / "changelog.d", "10.added.md", "- new\n")
    assert run(repo) == 0
    once = (repo / "CHANGELOG.md").read_text()
    assert run(repo) == 0
    assert (repo / "CHANGELOG.md").read_text() == once


def test_dry_run_prints_section_and_changes_nothing(repo, capsys):
    write(repo / "changelog.d", "10.added.md", "- new\n")
    assert run(repo, "--dry-run") == 0
    out = capsys.readouterr().out
    assert out.startswith("## [Unreleased]") and "- new" in out and "[1.0.0]" not in out
    assert (repo / "CHANGELOG.md").read_text() == BASE
    assert (repo / "changelog.d" / "10.added.md").exists()


@pytest.mark.parametrize(
    ("name", "content", "needle"),
    [
        ("10.bogus.md", "- x\n", "unknown type"),
        ("notes.md", "- x\n", "<id>.<type>.md"),
        ("10.added.txt", "- x\n", "<id>.<type>.md"),
        ("10.added.md", "  \n\n", "is empty"),
        ("10.added.md", "plain prose\n", "bullet"),
        ("10.added.md", "- ok\n\n### Added\n", "heading"),
        ("10.added.md", "- a\n\n  ```bash\n  echo hi\n", "never closed"),
        ("10.added.md", "- a\n\n  ~~~\n  echo hi\n  ```\n", "never closed"),
    ],
)
def test_check_catches_bad_fragments_and_changes_nothing(repo, capsys, name, content, needle):
    write(repo / "changelog.d", name, content)
    write(repo / "changelog.d", "11.fixed.md", "- fine\n")
    assert run(repo, "--check") == 1
    assert needle in capsys.readouterr().err
    # The default mode refuses too, and leaves everything in place.
    assert run(repo) == 1
    assert (repo / "CHANGELOG.md").read_text() == BASE
    assert (repo / "changelog.d" / "11.fixed.md").exists()


def test_check_passes_and_ignores_readme(repo, capsys):
    write(repo / "changelog.d", "README.md", "# Changelog fragments\n")
    write(repo / "changelog.d", "10.added.md", "- ok\n")
    assert run(repo, "--check") == 0
    assert (repo / "changelog.d" / "10.added.md").exists()
    assert run(repo) == 0
    assert (repo / "changelog.d" / "README.md").exists()  # README is never consumed


def test_missing_unreleased_section_fails(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text("# C\n\n## [1.0.0]\n")
    write(tmp_path / "changelog.d", "1.added.md", "- a\n")
    assert run(tmp_path) == 1
    assert (tmp_path / "changelog.d" / "1.added.md").exists()


def test_real_changelog_gains_only_added_lines():
    original = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    frags = [
        ca.Fragment(Path("1.fixed.md"), "1", "fixed", "- f"),
        ca.Fragment(Path("2.security.md"), "2", "security", "- s"),
        ca.Fragment(Path("3.added.md"), "3", "added", "- a"),
    ]
    new = ca.assemble_text(original, frags)
    diff = [ln for ln in difflib.ndiff(original.splitlines(), new.splitlines()) if ln[0] in "+-"]
    assert all(ln.startswith("+ ") for ln in diff)
    added = {ln[2:] for ln in diff if ln[2:].strip()}
    # Only the three entries, plus a type heading where the Unreleased section
    # lacked one; which headings exist depends on the live CHANGELOG.
    assert {"- f", "- s", "- a"} <= added
    assert added - {"- f", "- s", "- a"} <= {"### Fixed", "### Security", "### Added"}


def test_repo_fragments_are_valid():
    frags, problems = ca.load_fragments(REPO_ROOT / "changelog.d")
    assert problems == []
    assert all(f.path.name == f"{f.ident}.{f.type}.md" for f in frags)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
def test_two_branches_adding_fragments_merge_without_conflict(tmp_path):
    def git(*args, check=True):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=tmp_path,
            check=check,
            capture_output=True,
            text=True,
        )

    git("init", "-q", "-b", "main")
    (tmp_path / "CHANGELOG.md").write_text(BASE)
    write(tmp_path / "changelog.d", "README.md", "# fragments\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    for branch, name in (("a", "101.added.md"), ("b", "102.added.md")):
        git("switch", "-q", "-c", branch, "main")
        write(tmp_path / "changelog.d", name, f"- change from {branch}\n")
        git("add", "-A")
        git("commit", "-q", "-m", branch)

    # Control: the old workflow, both branches inserting at the top of Unreleased, conflicts.
    for branch in ("old-a", "old-b"):
        git("switch", "-q", "-c", branch, "main")
        text = (tmp_path / "CHANGELOG.md").read_text()
        (tmp_path / "CHANGELOG.md").write_text(
            text.replace("## [Unreleased]\n\n", f"## [Unreleased]\n\n- entry {branch}\n\n")
        )
        git("commit", "-q", "-am", branch)
    assert git("merge-tree", "--write-tree", "old-a", "old-b", check=False).returncode == 1

    for first, second in (("a", "b"), ("b", "a")):
        merge = git("merge-tree", "--write-tree", first, second, check=False)
        assert merge.returncode == 0, merge.stdout + merge.stderr
        git("switch", "-q", "--detach", first)
        git("merge", "-q", "--no-edit", second)
        assert run(tmp_path) == 0
        text = (tmp_path / "CHANGELOG.md").read_text()
        assert text.index("change from b") < text.index("change from a")  # newest first
        git("reset", "-q", "--hard")


def test_fenced_code_in_fragment_is_allowed(repo):
    write(repo / "changelog.d", "10.added.md", "- a\n\n  ```bash\n  echo hi\n  ```\n")
    assert run(repo, "--check") == 0


def test_dotfiles_are_ignored(repo):
    write(repo / "changelog.d", ".DS_Store", "binary junk")
    write(repo / "changelog.d", ".gitkeep", "")
    write(repo / "changelog.d", "10.added.md", "- ok\n")
    assert run(repo, "--check") == 0
    assert run(repo) == 0
    assert (repo / "changelog.d" / ".DS_Store").exists()


def test_trailing_hard_break_is_kept(repo):
    write(repo / "changelog.d", "10.added.md", "\n- first line  \n  second\n\n  \n")
    assert run(repo) == 0
    assert "- first line  \n  second\n" in (repo / "CHANGELOG.md").read_text()


def test_unreleased_end_is_fence_aware(tmp_path):
    changelog = (
        "## [Unreleased]\n\n### Added\n\n- a\n\n```md\n## Notes\n```\n\n- tail\n\n## [1.0.0]\n"
    )
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    write(tmp_path / "changelog.d", "1.fixed.md", "- f\n")
    assert run(tmp_path) == 0
    assert (
        (tmp_path / "CHANGELOG.md")
        .read_text()
        .endswith("- tail\n\n### Fixed\n\n- f\n\n## [1.0.0]\n")
    )


def test_mixed_line_endings_have_a_clear_error(tmp_path, capsys):
    (tmp_path / "CHANGELOG.md").write_bytes(b"## [Unreleased]\r\n\r\n### Added\n\n- a\r\n")
    write(tmp_path / "changelog.d", "1.added.md", "- b\n")
    assert run(tmp_path) == 1
    assert "mixed line endings in CHANGELOG.md" in capsys.readouterr().err
    assert (tmp_path / "changelog.d" / "1.added.md").exists()


def test_crlf_changelog_is_preserved(tmp_path):
    (tmp_path / "CHANGELOG.md").write_bytes(b"## [Unreleased]\r\n\r\n### Added\r\n\r\n- a\r\n")
    write(tmp_path / "changelog.d", "1.added.md", "- b\n")
    assert run(tmp_path) == 0
    assert (tmp_path / "CHANGELOG.md").read_bytes() == (
        b"## [Unreleased]\r\n\r\n### Added\r\n\r\n- b\r\n\r\n- a\r\n"
    )


def test_write_is_atomic_and_leaves_no_temp_files(repo, monkeypatch):
    write(repo / "changelog.d", "10.added.md", "- new\n")
    assert run(repo) == 0
    assert [p.name for p in repo.iterdir() if p.name.endswith(".tmp")] == []

    # A failed replace leaves the changelog and the fragments exactly as they were.
    write(repo / "changelog.d", "11.added.md", "- newer\n")
    before = (repo / "CHANGELOG.md").read_text()

    def boom(*_):
        raise OSError("disk gone")

    monkeypatch.setattr(ca.os, "replace", boom)
    with pytest.raises(OSError, match="disk gone"):
        run(repo)
    assert (repo / "CHANGELOG.md").read_text() == before
    assert (repo / "changelog.d" / "11.added.md").exists()
    assert [p.name for p in repo.iterdir() if p.name.endswith(".tmp")] == []


def test_failed_delete_is_reported_and_rerun_does_not_duplicate(repo, monkeypatch, capsys):
    d = repo / "changelog.d"
    write(d, "10.added.md", "- **Stuck (#10).** Fresh.\n")
    write(d, "11.fixed.md", "- **Fine (#11).** Fresh.\n")
    real_unlink = Path.unlink

    def flaky(self, *args, **kwargs):
        if self.name == "10.added.md":
            raise PermissionError("nope")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky)
    assert run(repo) == 1
    assert "10.added.md: could not delete" in capsys.readouterr().err
    first = (repo / "CHANGELOG.md").read_text()
    assert first.count("**Stuck (#10).**") == 1 and first.count("**Fine (#11).**") == 1
    assert (d / "10.added.md").exists() and not (d / "11.fixed.md").exists()

    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert run(repo) == 0  # the leftover is recognised as already folded in, then deleted
    assert (repo / "CHANGELOG.md").read_text() == first
    assert not (d / "10.added.md").exists()


@pytest.mark.parametrize(
    "changelog",
    [
        # Fence opens in Unreleased and swallows the next release heading.
        "## [Unreleased]\n\n### Added\n\n- a\n\n```md\n- oops\n\n## [1.0.0]\n\n- old\n",
        # Same, and a later released code block "closes" it, hiding the release heading.
        "## [Unreleased]\n\n- a\n\n```md\n\n## [1.0.0]\n\n```sh\nx\n```\n\n## [0.9.0]\n",
        # Unclosed fence with no release section after it.
        "## [Unreleased]\n\n### Added\n\n- a\n\n~~~\nopen\n",
    ],
)
def test_unclosed_fence_in_unreleased_fails_closed(tmp_path, capsys, changelog):
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    write(tmp_path / "changelog.d", "1.fixed.md", "- f\n")
    assert run(tmp_path) == 1
    assert "code fence" in capsys.readouterr().err
    assert (tmp_path / "CHANGELOG.md").read_text() == changelog  # untouched
    assert (tmp_path / "changelog.d" / "1.fixed.md").exists()


def test_fenced_heading_in_unreleased_and_closed_fence_before_release(tmp_path):
    changelog = (
        "## [Unreleased]\n\n### Added\n\n- a\n\n  ```md\n  ### Fixed\n  ```\n\n"
        "```sh\necho hi\n```\n\n## [1.0.0]\n\n```md\n## [fake]\n```\n"
    )
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    write(tmp_path / "changelog.d", "1.fixed.md", "- f\n")
    assert run(tmp_path) == 0
    text = (tmp_path / "CHANGELOG.md").read_text()
    # The fenced "### Fixed" is not a subsection: a real one is created before the release.
    assert text.count("### Fixed") == 2
    assert text.index("- f") < text.index("## [1.0.0]")
    assert text.endswith("## [1.0.0]\n\n```md\n## [fake]\n```\n")
