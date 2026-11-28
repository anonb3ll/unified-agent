"""Tests for scripts/check_public_commit_identity.py.

The public repository may carry the operator's own address in commit metadata:
a merge commit GitHub creates is authored with the merging account's identity,
which the operator accepted on 2026-11-27. That is an author/committer allowance and
nothing more - any other Gmail address, and a Co-authored-by trailer claiming
the operator identity, stay findings.

Each test builds a throwaway repository with crafted GIT_AUTHOR_* and
GIT_COMMITTER_* values, the same way tests/test_check_commit_identity.py does.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_public_commit_identity as guard  # noqa: E402

# Assembled independently of the module, so a wrong assembly fails the test
# rather than agreeing with itself.
OPERATOR = "".join(("ant", "mbel", "@", "gm", "ail", ".", "com"))
OTHER = "".join(("someone", "else", "@", "gm", "ail", ".", "com"))
GOOD = ("anonb3ll", "anonb3ll@pm.me")
GITHUB = ("GitHub", "noreply@github.com")


def _env(home: Path) -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GIT_", "UA_COMMIT_", "UA_OPERATOR_")) and k != "UA_AGENT_LABELS"
    } | {
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }


def _commit(
    path: Path,
    message: str,
    *,
    author: tuple[str, str] = GOOD,
    committer: tuple[str, str] = GOOD,
    body: str = "",
) -> str:
    env = _env(path.parent) | {
        "GIT_AUTHOR_NAME": author[0],
        "GIT_AUTHOR_EMAIL": author[1],
        "GIT_COMMITTER_NAME": committer[0],
        "GIT_COMMITTER_EMAIL": committer[1],
    }
    cmd = ["git", "commit", "-q", "--allow-empty", "-m", message]
    if body:
        cmd += ["-m", body]
    subprocess.run(cmd, cwd=path, check=True, env=env)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, env=env, capture_output=True, text=True
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=_env(tmp_path))
    _commit(path, "base")
    subprocess.run(["git", "tag", "base"], cwd=path, check=True, env=_env(tmp_path))
    return path


def _offenses(path: Path, sha: str = "HEAD") -> list[str]:
    return guard.check_range(path, "base", sha)


def test_clean_range_reports_nothing(repo: Path) -> None:
    _commit(repo, "plain commit")
    assert _offenses(repo) == []


def test_operator_author_is_accepted(repo: Path) -> None:
    """A merge commit GitHub creates: the operator authors, GitHub commits."""
    _commit(
        repo,
        "Merge pull request #1 from anonb3ll/topic",
        author=("Ant", OPERATOR),
        committer=GITHUB,
    )
    assert _offenses(repo) == []


def test_operator_committer_is_accepted(repo: Path) -> None:
    _commit(repo, "merged by the account", committer=("Ant", OPERATOR))
    assert _offenses(repo) == []


def test_another_gmail_author_is_still_a_finding(repo: Path) -> None:
    sha = _commit(repo, "planted", author=("Builder", OTHER))
    offenses = _offenses(repo)
    assert offenses == [f"{sha} author-email gmail"]
    assert OTHER not in " ".join(offenses)


def test_operator_look_alike_is_still_a_finding(repo: Path) -> None:
    """The allowance is exact: a +tag variant is a different string."""
    sha = _commit(repo, "tagged", author=("Ant", OPERATOR.replace("@", "+work@")))
    assert _offenses(repo) == [f"{sha} author-email gmail"]


def test_operator_address_with_a_longer_tail_is_still_a_finding(repo: Path) -> None:
    """The allowance is an equality: a startswith test would accept this one."""
    sha = _commit(repo, "tail", author=("Builder", OPERATOR + ".attacker.example"))
    assert _offenses(repo) == [f"{sha} author-email gmail"]


def test_operator_address_inside_a_longer_local_part_is_still_a_finding(repo: Path) -> None:
    """A substring test would accept this one, so the match must be whole-value."""
    sha = _commit(repo, "inner", author=("Builder", "x" + OPERATOR))
    assert _offenses(repo) == [f"{sha} author-email gmail"]


def test_operator_in_a_coauthor_trailer_is_still_a_finding(repo: Path) -> None:
    """A merge never adds a Co-authored-by, so claiming it is not a landing."""
    sha = _commit(repo, "merge", body=f"Co-authored-by: Ant <{OPERATOR}>")
    assert _offenses(repo) == [f"{sha} co-authored-by gmail"]


def test_operator_address_does_not_exempt_the_name_field(repo: Path) -> None:
    """The allowance is the address value, not the whole identity on that commit."""
    patterns = (
        ("gmail", re.compile(r"@gmail[.]com\b", re.IGNORECASE)),
        ("host", re.compile(r"internal-host")),
    )
    sha = _commit(repo, "named", author=("internal-host", OPERATOR))
    assert guard.check_range(repo, "base", sha, patterns) == [f"{sha} author-name host"]


def test_main_exits_nonzero_and_prints_no_address(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(repo, "planted", author=("Builder", OTHER))
    assert guard.main(["--base", "base", "--head", "HEAD", "--root", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "author-email" in out
    assert OTHER not in out
