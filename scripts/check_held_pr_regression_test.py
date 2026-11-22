#!/usr/bin/env python3
"""CI gate: a held-despite-green PR must carry a regression test (task #1260).

Company operating rule (`docs/company/OPERATING-SYSTEM.md` step 5, Epic C
#1188): "A PR held back despite green CI gets a regression test the same
day." This script is that rule's CI enforcement — a step in the `gates` job
of `.github/workflows/ci.yml` (the dispatch-only Depot copy carries it too)
that runs on every `pull_request` CI run, i.e. on PRs labelled `ci:full`.

**How it learns "held".** The review pipeline (`src/unified_agent/held_pr.py`)
never labels PRs automatically — see that module's docstring for why. The
`held` GitHub label is applied by a human/reviewing agent running
`unified-agent-review label-held <task_id>` right after writing a
`[review: rework]` note on a PR with green CI. This script reads that label
straight from the `pull_request` webhook payload (`GITHUB_EVENT_PATH`) — no
`gh` call needed for that part. When the label is absent, the PR was never
held and the check passes trivially (`skipped=True`).

**What counts as a regression test.** Deliberately pragmatic, not exact: the
PR's full changed-file set (base...head, i.e. every commit in the PR) must
add or modify at least one file under `tests/` (or
`packages/*/src/**/*.test.*` / `__tests__/`), OR the PR body or any commit
message must carry a `Regression-Test: <path>::<name>` trailer whose `<path>`
exists in the checkout. The CI job reads the `held` label event time and
reports tests committed more than 24 hours later as late.

**Offline testability.** Every input this script needs — held/not-held,
changed files, PR body, commit messages — can be supplied as an explicit CLI
arg (see `--help`), so `tests/test_check_held_pr_regression_test.py` drives
`evaluate()` and `main()` directly with fixtures, no `git`/`gh`/network
required. The live-CI path (`--event-path` + `--base-sha`) is exercised only
by the real workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# "at least one file under tests/" (any extension — fixtures count too), a
# packages/*/src/**/*.test.* file (JS/TS convention), or anything under an
# __tests__/ directory.
TEST_PATH_RE = re.compile(
    r"(^|/)tests/"
    r"|(^|/)__tests__/"
    r"|^packages/[^/]+/src/.*\.test\.[A-Za-z0-9]+$"
)

# `Regression-Test: path/to/test_file.py::test_name` — the `::test_name` part
# is optional and, if present, is not itself verified (pytest/vitest node-id
# syntax varies too much to validate generically); only the file path before
# `::` must exist.
REGRESSION_TRAILER_RE = re.compile(r"^Regression-Test:\s*(\S+)\s*$", re.MULTILINE)

LATE_AFTER = timedelta(hours=24)

HELD_LABEL = "held"

RULE_CITATION = (
    "Company operating rule (docs/company/OPERATING-SYSTEM.md step 5): "
    '"A PR held back despite green CI gets a regression test the same day."'
)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    skipped: bool
    message: str


def touches_test_file(changed_files: list[str]) -> str | None:
    """The first changed path that looks like a test file, or None."""
    for f in changed_files:
        if TEST_PATH_RE.search(f):
            return f
    return None


def find_regression_trailers(texts: list[str]) -> list[str]:
    """Every `Regression-Test: <value>` trailer across `texts`, in order."""
    found: list[str] = []
    for text in texts:
        found.extend(REGRESSION_TRAILER_RE.findall(text or ""))
    return found


def find_regression_trailer(texts: list[str]) -> str | None:
    """The first `Regression-Test: <value>` trailer found across `texts`."""
    trailers = find_regression_trailers(texts)
    return trailers[0] if trailers else None


def regression_trailer_target_exists(value: str, repo_root: Path) -> bool:
    """A trailer counts only if it names a real test file (and test, when given).

    The path must be a regular file that looks like a test (`TEST_PATH_RE`),
    and a `::name` suffix must appear in that file — so a trailer can't be
    satisfied by pointing at README.md, a directory, or a test that was
    never written.
    """
    path_part, _, name = value.partition("::")
    target = (repo_root / path_part).resolve()
    # The trailer is PR-controlled: it must stay inside this checkout.
    if not target.is_relative_to(repo_root.resolve()):
        return False
    if not TEST_PATH_RE.search(path_part) or not target.is_file():
        return False
    if name:
        leaf = name.split("::")[-1]
        try:
            return leaf in target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
    return True


def late_warning(held_at: datetime | None, tested_at: datetime | None) -> str | None:
    """A warning when coverage landed more than a day after the hold, else None.

    The rule is "the same day". A late test still unblocks the PR — failing
    it forever after the fix lands would punish the fix — but the lateness
    is reported so it shows up in the job summary and delivery metrics.
    """
    if held_at is None or tested_at is None:
        return None
    if tested_at - held_at > LATE_AFTER:
        hours = (tested_at - held_at).total_seconds() / 3600
        return (
            f"LATE: regression test landed {hours:.0f}h after the PR was held "
            f"(rule: same day). {RULE_CITATION}"
        )
    return None


def evaluate(
    held: bool,
    changed_files: list[str],
    texts: list[str],
    repo_root: Path | None = None,
) -> CheckResult:
    """Pure decision: was this held PR given a regression test?

    `repo_root` is only used to validate a `Regression-Test:` trailer's path
    actually exists; pass None to skip that existence check (e.g. when only
    the trailer's presence matters, as in some offline tests).
    """
    if not held:
        return CheckResult(ok=True, skipped=True, message="PR was never held — check skipped.")

    test_file = touches_test_file(changed_files)
    if test_file:
        return CheckResult(
            ok=True,
            skipped=False,
            message=f"held PR adds/modifies a test file ({test_file}) — regression coverage present.",
        )

    trailers = find_regression_trailers(texts)
    for trailer in trailers:
        if repo_root is None or regression_trailer_target_exists(trailer, repo_root):
            return CheckResult(
                ok=True,
                skipped=False,
                message=f"held PR references an existing test via 'Regression-Test: {trailer}'.",
            )
    if trailers:
        listed = ", ".join(f"'{t}'" for t in trailers)
        return CheckResult(
            ok=False,
            skipped=False,
            message=(
                f"no Regression-Test trailer ({listed}) points at an existing test file "
                f"(and named test) in this checkout. {RULE_CITATION}"
            ),
        )

    return CheckResult(
        ok=False,
        skipped=False,
        message=(
            "This PR is held despite green CI (label 'held') but adds no test under tests/ "
            "(or packages/*/src/**/*.test.* / __tests__) and carries no "
            "'Regression-Test: <path>::<name>' trailer in its body or commit messages. "
            f"{RULE_CITATION} Add a test reproducing the held issue, or add a "
            "'Regression-Test: <path>::<name>' trailer pointing at an existing test that covers it."
        ),
    )


# ── live-CI gathering (git/event-payload I/O; not exercised by offline tests) ──


def _read_event_payload(event_path: str) -> dict:
    try:
        return json.loads(Path(event_path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _held_from_event(event: dict) -> bool:
    labels = (event.get("pull_request") or {}).get("labels") or []
    return any((label or {}).get("name") == HELD_LABEL for label in labels)


def _pr_body_from_event(event: dict) -> str:
    return (event.get("pull_request") or {}).get("body") or ""


def _git_changed_files(base_sha: str, head_sha: str, repo_root: Path) -> list[str]:
    res = subprocess.run(
        # Added/copied/modified/renamed only: deleting a test is never coverage.
        ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base_sha}...{head_sha}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if res.returncode != 0:
        return []
    return [line for line in res.stdout.splitlines() if line]


def _git_commit_messages(base_sha: str, head_sha: str, repo_root: Path) -> list[str]:
    res = subprocess.run(
        ["git", "log", f"{base_sha}..{head_sha}", "--format=%B%x00"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if res.returncode != 0:
        return []
    return [m for m in res.stdout.split("\x00") if m.strip()]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _git_last_touch(path: str, head_sha: str, repo_root: Path) -> datetime | None:
    res = subprocess.run(
        ["git", "log", "-1", "--format=%cI", head_sha, "--", path],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return _parse_iso(res.stdout.strip()) if res.returncode == 0 else None


def _load_list_arg(inline: str | None, path_str: str | None) -> list[str] | None:
    """CSV/JSON inline value, a JSON file, or None when neither is given."""
    if path_str:
        data = json.loads(Path(path_str).read_text(encoding="utf-8"))
        return list(data)
    if inline is None:
        return None
    stripped = inline.strip()
    if stripped.startswith("["):
        return list(json.loads(stripped))
    return [p for p in stripped.split(",") if p]


def _load_text_arg(inline: str | None, path_str: str | None) -> str | None:
    if path_str:
        return Path(path_str).read_text(encoding="utf-8")
    return inline


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "CI gate: a PR held despite green CI (GitHub label 'held') must add or "
            "reference a regression test before it can go green again. See module "
            "docstring for the full rule."
        )
    )
    held_group = parser.add_mutually_exclusive_group()
    held_group.add_argument(
        "--held",
        choices=("true", "false"),
        default=None,
        help="Explicit held/not-held override (primarily for offline tests).",
    )
    held_group.add_argument(
        "--event-path",
        default=None,
        help="Path to a GitHub Actions pull_request event JSON payload "
        "(defaults to $GITHUB_EVENT_PATH). Labels + PR body are read from it.",
    )
    parser.add_argument(
        "--changed-files",
        default=None,
        help="Changed file paths, comma-separated or a JSON array string.",
    )
    parser.add_argument(
        "--changed-files-json",
        default=None,
        help="Path to a JSON file containing a list of changed file paths.",
    )
    parser.add_argument("--pr-body", default=None, help="Raw PR body text.")
    parser.add_argument("--pr-body-file", default=None, help="Path to a file with the PR body.")
    parser.add_argument(
        "--commit-messages-json",
        default=None,
        help="Path to a JSON file containing a list of commit message strings.",
    )
    parser.add_argument(
        "--base-sha",
        default=None,
        help="Base commit sha — with --head-sha, changed files/commit messages are "
        "computed via `git diff`/`git log` when not given explicitly above.",
    )
    parser.add_argument("--head-sha", default="HEAD", help="Head commit sha (default HEAD).")
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository checkout root, for both git subprocess calls and "
        "Regression-Test trailer path existence checks (default: cwd).",
    )
    parser.add_argument(
        "--held-at",
        default=None,
        help="ISO-8601 time the 'held' label was applied. When given, a test that "
        "landed more than 24h later is reported as LATE (the check still passes).",
    )
    parser.add_argument("--json", action="store_true", help="Emit the result as JSON.")
    return parser


def _resolve_held(args: argparse.Namespace, event: dict) -> bool:
    if args.held is not None:
        return args.held == "true"
    if event:
        return _held_from_event(event)
    # No held-signal source at all: fail-safe to "never held" (skip cleanly)
    # rather than either false-failing every PR or silently no-op'ing a
    # genuinely held one without saying so.
    print(
        "warning: no --held/--event-path (and $GITHUB_EVENT_PATH unset) — "
        "assuming this PR was never held.",
        file=sys.stderr,
    )
    return False


def _resolve_pr_body(args: argparse.Namespace, event: dict) -> str:
    explicit = _load_text_arg(args.pr_body, args.pr_body_file)
    if explicit is not None:
        return explicit
    return _pr_body_from_event(event)


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve()

    event_path = args.event_path or os.environ.get("GITHUB_EVENT_PATH")
    event: dict = {}
    if event_path and args.held is None:
        # A named event file that is missing or unreadable means held status
        # is unknown — never "not held", which would skip the gate.
        event = _read_event_payload(event_path) if Path(event_path).exists() else {}
        if not event.get("pull_request"):
            print(
                f"error: event payload {event_path!r} is missing or has no pull_request — "
                "cannot tell whether this PR is held.",
                file=sys.stderr,
            )
            return 2

    held = _resolve_held(args, event)

    changed_files = _load_list_arg(args.changed_files, args.changed_files_json)
    commit_messages = _load_list_arg(None, args.commit_messages_json) or []

    if held and changed_files is None:
        if args.base_sha:
            changed_files = _git_changed_files(args.base_sha, args.head_sha, repo_root)
            if not commit_messages:
                commit_messages = _git_commit_messages(args.base_sha, args.head_sha, repo_root)
        else:
            print(
                "error: PR is held but no changed-file source was given "
                "(--changed-files / --changed-files-json / --base-sha).",
                file=sys.stderr,
            )
            return 2
    changed_files = changed_files or []

    pr_body = _resolve_pr_body(args, event)
    texts = [pr_body, *commit_messages]

    result = evaluate(held, changed_files, texts, repo_root=repo_root)

    warning = None
    test_file = touches_test_file(changed_files) if result.ok and not result.skipped else None
    if result.ok and not result.skipped and not test_file:
        # Trailer-backed coverage still has a test commit date. Otherwise a
        # test added after the 24-hour window produces no lateness warning.
        test_file = next(
            (
                trailer.partition("::")[0]
                for trailer in find_regression_trailers(texts)
                if regression_trailer_target_exists(trailer, repo_root)
            ),
            None,
        )
    if test_file and args.held_at and args.base_sha:
        warning = late_warning(
            _parse_iso(args.held_at), _git_last_touch(test_file, args.head_sha, repo_root)
        )
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(f"- {warning}\n")

    if args.json:
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "skipped": result.skipped,
                    "message": result.message,
                    "late_warning": warning,
                }
            )
        )
    else:
        print(result.message)

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
