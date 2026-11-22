#!/usr/bin/env python3
"""Refuse a commit whose staged content carries merge-conflict markers.

Task #1016 (Route 2): a `git stash` / `git stash pop` collision left 5
conflicted files sitting in a worktree with `<<<<<<<` / `=======` / `>>>>>>>`
markers still in place. Nothing caught that before it reached `git add`; only
a human reading `git status` did. This module is the load-bearing logic for
the pre-commit guard installed by `scripts/install-hooks.sh` — it inspects
the *staged* blob content (not the working tree) for the exact marker
signature and refuses the commit if it finds one.

Deliberately narrow:
  - Only the three canonical git conflict-marker lines, anchored at column 0
    (`<<<<<<<`, `=======`, `>>>>>>>`), matching git's own convention of seven
    marker characters. A shorter run, or the same characters mid-line
    (a rust `======` doc comment border, a markdown horizontal rule using
    plain `---`, etc.) does not match.
  - A file is only flagged when it carries a complete conflict BLOCK: both an
    opening `<<<<<<<` line and a closing `>>>>>>>` line. This matters because
    a lone `=======` run at column 0 is legitimate prose, not a conflict — it
    is how Markdown underlines a setext H1 and how reStructuredText underlines
    a section title. No file in this repo does that today, but this hook
    outlives that fact, and a guard that blocks a correctly-written heading
    with "merge-conflict markers" would be both wrong and baffling. Requiring
    the pair costs nothing: the failure mode this targets — an unresolved
    `git stash pop` collision — always leaves all three.
  - Only text blobs. A binary staged file (image, archive, model checkpoint)
    is skipped outright — decoding it as text would either crash or produce
    false positives on incidental byte sequences.
  - A per-path exemption list for files that legitimately contain these
    markers on purpose (e.g. a fixture testing a merge-conflict parser). As
    of task #1016 no such file exists in this repo (checked via
    `grep -rlP '^(<{7}|={7}|>{7})'` across the tree), so the list starts
    empty — add a path here, with a comment saying why, if one is ever
    introduced.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field

# Paths exempt from this check because they deliberately contain
# conflict-marker text (e.g. a fixture for testing a merge-conflict parser).
# Empty as of task #1016 — see module docstring.
EXEMPT_PATHS: frozenset[str] = frozenset()

_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


@dataclass
class ConflictMarkerFinding:
    path: str
    line_number: int
    marker: str


@dataclass
class CheckResult:
    findings: list[ConflictMarkerFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def format_report(self) -> str:
        lines = [
            "commit blocked: staged content contains merge-conflict markers",
            "",
        ]
        for f in self.findings:
            lines.append(f"  {f.path}:{f.line_number}: found {f.marker!r}")
        lines += [
            "",
            "This is the exact signature left behind when a `git stash pop`",
            "applies someone else's unresolved conflict (see docs/COLLABORATION.md,",
            "'Overlapping code means worktrees' / shared-stash hazard). Resolve the",
            "markers, re-stage, and commit again.",
        ]
        return "\n".join(lines)


def _is_marker_line(line: str) -> str | None:
    """Return the matched marker text if `line` is a conflict-marker line."""
    for marker in _MARKERS:
        if line.startswith(marker):
            return marker
    return None


def find_conflict_markers_in_text(text: str) -> list[tuple[int, str]]:
    """Return (1-based line number, marker) pairs for a real conflict block.

    Returns [] unless the text contains BOTH an opening `<<<<<<<` and a
    closing `>>>>>>>` line — see the module docstring for why a lone
    `=======` run is not evidence of anything.
    """
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        marker = _is_marker_line(line)
        if marker is not None:
            hits.append((i, marker))
    found = {marker for _, marker in hits}
    if "<<<<<<<" not in found or ">>>>>>>" not in found:
        return []
    return hits


def check_staged_blob(path: str, content: bytes) -> list[ConflictMarkerFinding]:
    """Check one staged file's raw content. Skips binary content and exempt paths."""
    if path in EXEMPT_PATHS:
        return []
    if b"\x00" in content:
        # Binary content - not worth decoding, and not the failure mode this
        # guard targets.
        return []
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return []
    return [
        ConflictMarkerFinding(path=path, line_number=line_no, marker=marker)
        for line_no, marker in find_conflict_markers_in_text(text)
    ]


def check_staged_paths(paths_and_contents: list[tuple[str, bytes]]) -> CheckResult:
    """Pure check over an explicit list of (path, staged-blob-bytes) pairs."""
    result = CheckResult()
    for path, content in paths_and_contents:
        result.findings.extend(check_staged_blob(path, content))
    return result


def _iter_staged_files(repo_root: str | None = None) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in out.stdout.splitlines() if p]


def _read_staged_blob(path: str, repo_root: str | None = None) -> bytes:
    out = subprocess.run(
        ["git", "show", f":{path}"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    )
    return out.stdout


def check_git_staged_content(repo_root: str | None = None) -> CheckResult:
    """Check every currently-staged file in the given git repo (via subprocess)."""
    paths_and_contents = [
        (path, _read_staged_blob(path, repo_root=repo_root))
        for path in _iter_staged_files(repo_root=repo_root)
    ]
    return check_staged_paths(paths_and_contents)


def main() -> int:
    result = check_git_staged_content()
    if result.ok:
        return 0
    print(result.format_report(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
