#!/usr/bin/env python3
"""Print the test files a change can affect, one per line (task #2231). Stdlib only.

The selection comes from the static import graph in
``src/unified_agent/affected_tests.py`` (read from this checkout, never imported
from an installed copy), so it covers tests that import a changed module through
other modules and test helpers, not only ``tests/test_<stem>.py``. Every
selection also carries the cross-cutting inventory tests (``ALWAYS_RUN``) and,
for a documentation-only change, the docs scope of ``scripts/ci_changes.py``.

Input (default: the branch diff ``git diff --name-status <base>...HEAD`` plus
staged, unstaged and untracked changes):

  --base REF        diff against REF instead of origin/main
  --files A B ...   these paths, read as modified
  --from-stdin      ``git diff --name-status`` lines or bare paths on stdin

Output: the selected test files, sorted, one per line. When the change cannot be
mapped to tests (pyproject.toml, uv.lock, tests/conftest.py, the package root,
or a non-markdown file nothing names), the only line printed is the sentinel
``ALL``: run the full suite, which on the desktop means CI (label the PR
ci:full or let the merge batch run it). A documentation-only change (markdown
outside ``src/``, ``tests/`` and ``packages/``) prints the docs test scope from
``ci_changes.candidates("docs")`` along with the inventory tests, including a
page no test names. An empty output means nothing to run. Exit status is 0 in
all three cases and 2 when the diff cannot be read.

  --depth DEPTH     transitive (default): module-level import chains, lazy
                    imports one step, and the component map's declared edges;
                    direct: only tests that import a changed file without going
                    through other src modules (the review pipeline's tier);
                    full: every lazy edge too. See
                    src/unified_agent/affected_tests.py for each.
  --no-inventory    leave out the always-run inventory tests
  --explain         a summary on stderr: counts, depth, why ALL was chosen, and
                    the dynamic imports the graph could not follow

Whatever the depth, the selection is a targeted subset, not a certification:
batch CI (the full suite) is still required before merge.

Examples:

  uv run python scripts/affected_tests.py
  uv run python scripts/affected_tests.py --files src/unified_agent/reviewer.py
  git diff --name-status main~3 main | uv run python scripts/affected_tests.py --from-stdin
  ./scripts/local-gate.sh --affected
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for entry in (str(REPO_ROOT / "src"), str(SCRIPTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import ci_changes  # noqa: E402

from unified_agent import affected_tests  # noqa: E402

FULL_SENTINEL = "ALL"


class DiffError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if res.returncode != 0:
        raise DiffError(f"git {' '.join(args)} failed: {res.stderr.strip() or res.returncode}")
    return res.stdout


def branch_changes(root: Path, base: str) -> list[affected_tests.Change]:
    """The branch diff against ``base`` plus everything not yet committed."""
    lines = _git(root, "diff", "--name-status", "-M", f"{base}...HEAD").splitlines()
    lines += _git(root, "diff", "--name-status", "-M", "HEAD").splitlines()
    untracked = _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    lines += [f"A\t{path}" for path in untracked if path.strip()]
    return affected_tests.parse_name_status(lines)


def docs_only(changes: list[affected_tests.Change]) -> bool:
    paths = [p for c in changes for p in (c.path, c.old_path) if p]
    return bool(paths) and all(affected_tests.is_markdown_doc(p) for p in paths)


def selected_paths(
    root: Path,
    changes: list[affected_tests.Change],
    *,
    depth: str = "transitive",
    inventory: bool = True,
) -> tuple[list[str] | None, affected_tests.Selection]:
    """The test files to run, or None when the full suite is needed."""
    selection = affected_tests.select(root, changes, depth=depth)
    if selection.full:
        return None, selection
    paths = set(selection.paths(inventory=inventory))
    if docs_only(changes):
        # ci_changes.classify's docs scope: only when nothing but documentation
        # changed. Nearly every PR carries a changelog.d fragment, so adding the
        # scope to code changes would add ~180 files to every selection; a code
        # change keeps the tests that name its docs by path instead.
        paths |= set(ci_changes.candidates("docs", root))
    return sorted(paths), selection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--base", default="origin/main", help="diff base (default origin/main)")
    source.add_argument("--files", nargs="+", metavar="PATH", help="changed paths")
    source.add_argument("--from-stdin", action="store_true", help="read changes from stdin")
    parser.add_argument("--depth", choices=affected_tests.DEPTHS, default="transitive")
    parser.add_argument("--no-inventory", action="store_true", help="omit ALWAYS_RUN tests")
    parser.add_argument("--explain", action="store_true", help="summary on stderr")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    root = args.root.resolve()
    try:
        if args.files:
            changes = affected_tests.parse_name_status(args.files)
        elif args.from_stdin:
            changes = affected_tests.parse_name_status(sys.stdin.read().splitlines())
        else:
            changes = branch_changes(root, args.base)
    except DiffError as exc:
        print(f"affected_tests: {exc}", file=sys.stderr)
        return 2

    paths, selection = selected_paths(
        root, changes, depth=args.depth, inventory=not args.no_inventory
    )
    if paths is None:
        print(FULL_SENTINEL)
    elif paths:
        print("\n".join(paths))
    if args.explain:
        total = len(ci_changes.candidates("full", root))
        if paths is None:
            summary = "full suite: " + "; ".join(selection.full_reasons)
        else:
            summary = (
                f"{len(paths)} of {total} test files ({len(selection.tests)} mapped, "
                f"{len(selection.inventory)} inventory"
                f"{', plus the docs scope' if docs_only(changes) else ''})"
            )
        print(
            f"affected_tests: {len(changes)} changed path(s), depth {args.depth}: {summary}",
            file=sys.stderr,
        )
        if paths is not None:
            print(f"affected_tests: {affected_tests.SUBSET_NOTE}", file=sys.stderr)
            if selection.unresolved_tests:
                print(
                    "affected_tests: test-side analysis is conservative: "
                    f"{len(selection.unresolved_tests)} of these run on every Python change "
                    "because their imports cannot be read statically",
                    file=sys.stderr,
                )
            if selection.caveats:
                print(
                    f"affected_tests: {len(selection.caveats)} src module(s) with dynamic "
                    "imports that cannot be read are reported, not followed:",
                    file=sys.stderr,
                )
            for caveat in selection.caveats:
                print(f"affected_tests: not followed: {caveat}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
