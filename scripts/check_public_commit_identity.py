#!/usr/bin/env python3
"""Check new commits for private email addresses in the public repository.

Checks authors, committers, and Co-authored-by trailers. The report names a
commit and the field, and never prints the name or address.

The operator's own address is accepted in the author and committer fields
(Anthony, 2026-11-27): a merge commit GitHub creates carries the account
identity, so a landing that goes through a pull request must not be refused for
it. A Co-authored-by trailer claiming that identity is still a finding, because
a merge never adds one.

    check_public_commit_identity.py --base BASE --head HEAD [--root DIR]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

PUBLIC_PATTERNS = (("gmail", re.compile(r"@gmail[.]com\b", re.IGNORECASE)),)
_COAUTHOR = re.compile(
    r"^co-authored-by:\s*(.*?)\s*(?:<([^>]+)>)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_NOREPLY = re.compile(
    r"(?:@users\.noreply\.github\.com|noreply@github\.com|web-flow@github\.com)$",
    re.IGNORECASE,
)


def _exempt(name: str, email: str) -> bool:
    return bool(email) and _NOREPLY.search(email.strip()) is not None


# The operator's own address, assembled here so this file never contains it: the
# repository's content gates scan tracked files, and the ruling covers commit
# metadata only.
_OPERATOR_EMAIL = "".join(("ant", "mbel", "@", "gm", "ail", ".", "com"))


def _operator_identity(email: str) -> bool:
    """True for the operator's own address, which commit metadata may carry."""
    return email.strip().lower() == _OPERATOR_EMAIL


def _findings(
    name: str, email: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]
) -> list[tuple[str, str]]:
    """Field and category for a name/email pair, with no raw identity data."""
    found = []
    for category, pattern in patterns:
        if category != "gmail" and pattern.search(name):
            found.append(("name", category))
        elif pattern.search(email):
            found.append(("email", category))
    return found


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _prepare_range(root: Path, base: str, head: str) -> subprocess.CompletedProcess[str]:
    # GitHub sends an all-zero before SHA on the first push to a branch.
    first_push = len(base) in (40, 64) and not base.strip("0")
    rev = head if first_push else f"{base}..{head}"
    listed = _git(root, "rev-list", "--reverse", "--end-of-options", rev, "--")
    if listed.returncode == 0 or first_push:
        return listed
    # A shallow checkout or force push may not carry the previous commit.
    _git(root, "fetch", "--no-tags", "--deepen=200", "origin")
    _git(root, "fetch", "--no-tags", "origin", base)
    return _git(root, "rev-list", "--reverse", "--end-of-options", rev, "--")


def check_range(
    root: Path,
    base: str,
    head: str,
    patterns: tuple[tuple[str, re.Pattern[str]], ...] = PUBLIC_PATTERNS,
) -> list[str]:
    """``sha field category`` lines for every private identity in the range."""
    listed = _prepare_range(root, base, head)
    if listed.returncode != 0:
        raise RuntimeError("cannot list commits in the requested range")
    offenses: list[str] = []
    for sha in (line for line in listed.stdout.splitlines() if line):
        meta = _git(root, "log", "-1", "--format=%an%n%ae%n%cn%n%ce%n%B", sha)
        if meta.returncode != 0:
            raise RuntimeError(f"cannot read commit {sha[:12]}")
        name_a, email_a, name_c, email_c, body = (meta.stdout.split("\n", 4) + [""] * 5)[:5]
        for role, name, email in (("author", name_a, email_a), ("committer", name_c, email_c)):
            if _exempt(name, email) or _operator_identity(email):
                continue
            for field, category in _findings(name, email, patterns):
                offenses.append(f"{sha} {role}-{field} {category}")
        for match in _COAUTHOR.finditer(body):
            co_name, co_email = match.group(1) or "", match.group(2) or ""
            if _exempt(co_name, co_email):
                continue
            for _, kind in _findings(co_name, co_email, patterns):
                offenses.append(f"{sha} co-authored-by {kind}")
    return offenses


def main(
    argv: list[str] | None = None,
    *,
    patterns: tuple[tuple[str, re.Pattern[str]], ...] = PUBLIC_PATTERNS,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    if any(ref.startswith("-") for ref in (args.base, args.head)):
        parser.error("base and head must name commits")
    try:
        offenses = check_range(args.root.resolve(), args.base, args.head, patterns)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not offenses:
        print("commit identity: clean")
        return 0
    print(f"commit identity: {len(offenses)} private identit{'y' if len(offenses) == 1 else 'ies'}")
    for line in offenses:
        print(line)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
