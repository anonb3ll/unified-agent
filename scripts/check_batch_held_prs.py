#!/usr/bin/env python3
"""Held-PR regression rule for a merge batch (task #1787). Stdlib only.

CI runs on merge-batch branches by dispatch, where there is no pull_request
event to read the `held` label from. This finds every open PR labelled `held`
whose head commit is contained in the batch head, and applies the same rule as
`check_held_pr_regression_test.py` to each one, from the GitHub API (no git
history needed, so it works on a shallow checkout):

    a held PR must add or modify a test file, or name an existing test in a
    `Regression-Test:` trailer in its body or commit messages.

Usage, in CI (`gates`, on workflow_dispatch) or by the steward locally:

    python3 scripts/check_batch_held_prs.py --repo <owner>/<repo> --sha <batch-head>
    python3 scripts/check_batch_held_prs.py --repo <owner>/<repo> --pr 123 --pr 456

The token comes from GH_TOKEN, else from `gh auth token`; it is never printed.
Exit 0 when every held member passes (or none is held), 1 when one fails,
2 on an API or usage error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HELD_LABEL = "held"


def _load_rule():
    spec = importlib.util.spec_from_file_location(
        "check_held_pr_regression_test",
        Path(__file__).with_name("check_held_pr_regression_test.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


def _token() -> str:
    token = os.environ.get("GH_TOKEN", "").strip()
    if token:
        return token
    try:
        res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def _api(path: str, token: str) -> object:
    req = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _paged(path: str, token: str) -> list:
    out: list = []
    page = 1
    sep = "&" if "?" in path else "?"
    while True:
        batch = _api(f"{path}{sep}per_page=100&page={page}", token)
        out.extend(batch)
        if len(batch) < 100:
            return out
        page += 1


def held_members(repo: str, sha: str, token: str) -> list[dict]:
    """Open PRs labelled `held` whose head is contained in ``sha``."""
    members = []
    for pr in _paged(f"repos/{repo}/pulls?state=open", token):
        if HELD_LABEL not in {label.get("name") for label in pr.get("labels") or []}:
            continue
        head = pr["head"]["sha"]
        compare = _api(f"repos/{repo}/compare/{head}...{sha}", token)
        # "ahead"/"identical": the PR head is an ancestor of (or is) the batch head.
        if compare.get("status") in ("ahead", "identical"):
            members.append(pr)
    return members


def check_pr(rule, repo: str, pr: dict, token: str, repo_root: Path) -> tuple[bool, str]:
    number = pr["number"]
    # Deleting a test is never coverage: skip removed files, as the PR event
    # gate's `git diff --diff-filter=ACMR` does.
    files = [
        f["filename"]
        for f in _paged(f"repos/{repo}/pulls/{number}/files", token)
        if f.get("status") != "removed"
    ]
    commits = _paged(f"repos/{repo}/pulls/{number}/commits", token)
    texts = [pr.get("body") or "", *[(c.get("commit") or {}).get("message", "") for c in commits]]
    result = rule.evaluate(True, files, texts, repo_root=repo_root)
    return result.ok, f"PR #{number}: {result.message}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--sha", help="batch head; checks every held PR contained in it")
    parser.add_argument("--pr", type=int, action="append", help="check this PR (repeatable)")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    args = parser.parse_args(argv)
    if not args.repo or not (args.sha or args.pr):
        parser.error("--repo and one of --sha / --pr are required")
    token = _token()
    if not token:
        print("check_batch_held_prs: no GH_TOKEN and no `gh auth token`", file=sys.stderr)
        return 2
    rule = _load_rule()
    try:
        if args.pr:
            prs = [_api(f"repos/{args.repo}/pulls/{n}", token) for n in args.pr]
            prs = [p for p in prs if HELD_LABEL in {x.get("name") for x in p.get("labels") or []}]
        else:
            prs = held_members(args.repo, args.sha, token)
        results = [check_pr(rule, args.repo, pr, token, Path(args.repo_root)) for pr in prs]
    except Exception as exc:  # noqa: BLE001 - any API doubt is an error, not a pass
        print(f"check_batch_held_prs: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not results:
        print("No held PR in this batch — check passes trivially.")
        return 0
    for ok, message in results:
        print(("PASS " if ok else "FAIL ") + message)
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
