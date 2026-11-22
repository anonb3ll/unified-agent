#!/usr/bin/env python3
"""Read-only CI coverage proof for a pinned PR and tested commit (task #2254).

Uses the hosted runner's ``CI / ...`` commit-status contract. Exit 0 means
coverage was observed, 1 means coverage was refused, and 2 means evidence
could not be read. This does not approve a release or change GitHub state.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

REQUIRED = ("CI / gates", *(f"CI / test (shard={i})" for i in range(4)), "CI / full-verification")
OPTIONAL = ("CI / js", "CI / desktop-core")
MAX_PAGES = 20
SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
Get = Callable[[str], object]


class Unavailable(Exception):
    """Incomplete or unreadable evidence, never a passing result."""


class Refused(Exception):
    """Readable evidence does not satisfy the coverage contract."""


def gh_get(path: str) -> object:
    """Explicit GET only; gh owns credentials, which never enter our output."""
    try:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "--method", "GET", path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise Unavailable("GitHub GET failed; check authentication and API availability")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        # Neither subprocess output nor remote error text is safe diagnostic data.
        raise Unavailable(f"GitHub GET unavailable ({type(exc).__name__})") from None


def obj(value: object) -> dict:
    if not isinstance(value, dict):
        raise Unavailable("malformed GitHub object")
    return value


def sha(value: object) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise Unavailable("missing or malformed full commit/tree SHA")
    return value


def snapshot(get: Get, root: str, number: int, head: str, merged: bool) -> tuple[str, str]:
    pr = obj(get(f"{root}/pulls/{number}"))
    if pr.get("number") != number or sha(obj(pr.get("head")).get("sha")) != head:
        raise Refused("PR head changed or PR identity mismatched")
    base = obj(pr.get("base"))
    if obj(base.get("repo")).get("full_name") != root.removeprefix("repos/"):
        raise Refused("PR base repository mismatched")
    ref = base.get("ref")
    if not isinstance(ref, str) or not ref:
        raise Unavailable("PR base branch absent")
    if (merged and (pr.get("state") != "closed" or pr.get("merged") is not True)) or (
        not merged and pr.get("state") != "open"
    ):
        raise Refused("PR is not in the requested pre-merge or merged state")
    branch = obj(get(f"{root}/git/ref/heads/{quote(ref, safe='')}"))
    return ref, sha(obj(branch.get("object")).get("sha"))


def ancestor(get: Get, root: str, older: str, newer: str) -> None:
    if older == newer:
        return
    compare = obj(get(f"{root}/compare/{older}...{newer}?per_page=1"))
    if (
        compare.get("status") not in ("ahead", "identical")
        or sha(obj(compare.get("base_commit")).get("sha")) != older
        or sha(obj(compare.get("merge_base_commit")).get("sha")) != older
    ):
        raise Refused("tested commit does not contain the pinned PR head and integration base")


def statuses(get: Get, root: str, tested: str) -> dict[str, dict]:
    """Read the complete status history and select newest ids, not list order."""
    latest: dict[str, dict] = {}
    seen: set[int] = set()
    for page in range(1, MAX_PAGES + 1):
        rows = get(f"{root}/commits/{tested}/statuses?per_page=100&page={page}")
        if not isinstance(rows, list) or len(rows) > 100:
            raise Unavailable("malformed status page")
        for value in rows:
            row = obj(value)
            ident, context, state = row.get("id"), row.get("context"), row.get("state")
            if (
                type(ident) is not int
                or ident <= 0
                or ident in seen
                or not isinstance(context, str)
                or not context.strip()
                or state not in ("success", "failure", "error", "pending")
            ):
                raise Unavailable("incomplete, duplicate or malformed status history")
            seen.add(ident)
            if row.get("url") != f"https://api.github.com/{root}/statuses/{tested}":
                raise Unavailable("status belongs to another commit")
            name = context.strip().casefold()
            if name not in latest or ident > latest[name]["id"]:
                latest[name] = row
        if len(rows) < 100:
            return latest
    raise Unavailable("status history exceeds the bounded page budget")


def ci_proof(rows: dict[str, dict], required: tuple[str, ...]) -> dict:
    checked = {}
    for name in (*required, *OPTIONAL):
        row = rows.get(name.casefold())
        if row is None:
            if name in required:
                raise Refused(f"required status absent: {name}")
            continue
        if row["state"] != "success":
            raise Refused(f"CI status did not succeed: {name}={row['state']}")
        checked[name] = row
    if any(not isinstance(row.get("target_url"), str) for row in checked.values()):
        raise Unavailable("CI run link absent or malformed")
    urls = {row["target_url"] for row in checked.values()}
    if len(urls) != 1:
        raise Refused("CI statuses refer to different runs")
    url = next(iter(urls))
    if not isinstance(url, str):
        raise Unavailable("CI run link absent")
    try:
        parsed = urlsplit(url)
        # urlsplit leaves a bad port in netloc and raises only when .port is read.
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise Unavailable("CI run link is not a valid HTTPS evidence reference") from None
    return {
        "successful_status_ids": {name: row["id"] for name, row in checked.items()},
        "not_observed": [name for name in OPTIONAL if name not in checked],
    }


def verify(
    get: Get,
    repo: str,
    number: int,
    head: str,
    base: str,
    tested: str,
    merged: str | None = None,
    require: tuple[str, ...] = (),
) -> dict:
    """Prove inclusion and status coverage, rechecking live pins before return."""
    if not REPO.fullmatch(repo) or number <= 0:
        raise Unavailable("repository or PR number invalid")
    for pin in (head, base, tested, *([merged] if merged is not None else [])):
        sha(pin)
    if any(name not in OPTIONAL for name in require):
        raise Unavailable("only the known optional CI contexts may be additionally required")
    required = tuple(dict.fromkeys((*REQUIRED, *require)))
    root = f"repos/{repo}"
    initial = snapshot(get, root, number, head, merged is not None)
    expected_branch = merged or base
    if initial[1] != expected_branch:
        raise Refused("base branch moved from the explicitly pinned revision")
    ancestor(get, root, head, tested)
    ancestor(get, root, base, tested)
    tree = None
    if merged is not None:
        trees = []
        for revision in (tested, merged):
            commit = obj(get(f"{root}/commits/{revision}"))
            if sha(commit.get("sha")) != revision:
                raise Unavailable("commit response does not match its requested revision")
            trees.append(sha(obj(obj(commit.get("commit")).get("tree")).get("sha")))
        if trees[0] != trees[1]:
            raise Refused("merged tree differs from the tested tree")
        tree = trees[0]
    proof = ci_proof(statuses(get, root, tested), required)
    if snapshot(get, root, number, head, merged is not None) != initial:
        raise Refused("PR base branch changed during verification")
    if ci_proof(statuses(get, root, tested), required) != proof:
        raise Refused("CI statuses changed during verification")
    return {
        "schema": "unified-agent.ci-coverage.v1",
        "coverage": "observed",
        "repo": repo,
        "pr": number,
        "head_sha": head,
        "base_sha": base,
        "tested_sha": tested,
        "base_branch": initial[0],
        "merged_sha": merged,
        "verified_tree": tree,
        "mode": "exact-head" if tested == head else "batch",
        **proof,
        "observed_at": datetime.now(UTC).isoformat(),
        "limits": [
            "Commit-status contract only; check-run-only CI is not certified.",
            "Absent optional statuses are unobserved, not proof of classifier skipping.",
            "Statuses are GitHub assertions, not independently verified build provenance.",
            "Snapshot only; recheck pins at merge. Reviews and human approval are separate.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="GitHub owner/repository")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head", required=True, help="expected full PR head SHA")
    parser.add_argument("--base", required=True, help="integration base SHA")
    parser.add_argument("--tested", required=True, help="exact commit carrying CI statuses")
    parser.add_argument("--merged", help="expected current base-branch SHA after merging the batch")
    parser.add_argument("--require", action="append", choices=OPTIONAL, default=[])
    args = parser.parse_args(argv)
    try:
        report = verify(
            gh_get,
            args.repo,
            args.pr,
            args.head,
            args.base,
            args.tested,
            args.merged,
            tuple(args.require),
        )
    except (Refused, Unavailable) as exc:
        print(json.dumps({"coverage": "refused", "reason": str(exc)}))
        return 1 if isinstance(exc, Refused) else 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
