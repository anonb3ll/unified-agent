#!/usr/bin/env python3
"""Report how a merge should be verified, from fingerprinted receipts (task #2412).

Report mode: this changes no gate and dispatches nothing. Stdlib only, plus
``git`` and ``gh``.

  gate_route.py route --repo OWNER/NAME --head REF [--base REF] [--env ci|local]
      Print reuse, targeted or full, with the reason and the run that proves it.
      Exit 0 whenever a route was printed, 2 when REF cannot be read. Run it
      from a checkout of REF so a targeted selection reads the right tree.
  gate_route.py record --check NAME --conclusion C --scope S --head SHA
      Add a local receipt for SHA's tree. Records nothing unless SHA is HEAD
      and the worktree is clean, because a receipt names a tree.

``reuse`` does not replace DEPLOY-STEWARD step 3: after the last member lands,
origin/main's tree must still equal the tree the receipt names.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
for entry in (str(REPO_ROOT / "src"), str(SCRIPTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from unified_agent import check_receipts as receipts  # noqa: E402


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _tree_of(ref: str) -> str:
    try:
        return _git("rev-parse", "--verify", "--quiet", f"{ref}^{{tree}}")
    except subprocess.CalledProcessError:
        return ""


def _store() -> Path:
    common = Path(_git("rev-parse", "--git-common-dir"))
    return common.resolve() / receipts.STORE_NAME


def _gh_json(args: list[str]) -> Any:
    done = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return json.loads(done.stdout)


def _target_ci_env(repo: str) -> str:
    """The environment a new CI run would get. An unreadable variable is unknown."""
    done = subprocess.run(
        ["gh", "variable", "get", "UA_CI_RUNNER", "--repo", repo], capture_output=True, text=True
    )
    if done.returncode == 0:
        runner = done.stdout.strip()
    elif "not found" in done.stderr.lower():
        runner = ""
    else:
        return ""
    return receipts.ci_env([runner or receipts.DEFAULT_RUNNER])


def _selection(base: str, head: str) -> list[str] | None:
    """CI's certified subset for this diff, or None for the full suite."""
    import ci_changes

    return ci_changes.affected_selection(
        _git("diff", "--name-status", "-M", base, head).split("\n")
    )


def _route(args: argparse.Namespace) -> int:
    tree = _tree_of(args.head)
    if not tree:
        print(f"error: cannot read a tree for {args.head!r}", file=sys.stderr)
        return 2
    try:
        if args.env == "local":
            env = receipts.local_env()
            found = receipts.read_receipts(_store())
        else:
            env = _target_ci_env(args.repo)
            found = receipts.actions_receipts(
                args.repo, tree, tree_of=_tree_of, gh_json=_gh_json, limit=args.limit
            )
        selection = _selection(args.base, args.head)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        route = receipts.Route(receipts.FULL, f"evidence could not be read ({type(exc).__name__})")
        env = ""
    else:
        route = receipts.classify(
            tree, env, found, selection=selection, max_age_days=args.max_age_days
        )
    if args.json:
        print(
            json.dumps(
                {
                    "route": route.route,
                    "reason": route.reason,
                    "proof": route.proof,
                    "tests": list(route.tests),
                    "ignored": route.ignored,
                    "tree": tree,
                    "env": env,
                    "report_mode": True,
                },
                sort_keys=True,
            )
        )
        return 0
    print(f"route:   {route.route}")
    print(f"tree:    {tree}")
    print(f"env:     {env or 'unknown'}")
    print(f"reason:  {route.reason}")
    if route.proof:
        print(f"proof:   {route.proof}")
    if route.tests:
        print(f"tests:   {' '.join(route.tests)}")
    if route.ignored:
        print(f"ignored: {route.ignored} cancelled or unfinished full run(s)")
    print("note:    report mode; after landing, main's tree must still equal this tree")
    return 0


def _record(args: argparse.Namespace) -> int:
    if _git("rev-parse", "HEAD") != args.head or _git("status", "--porcelain"):
        print("not recorded: the worktree is not a clean checkout of --head", file=sys.stderr)
        return 0
    receipts.append_receipt(
        _store(),
        receipts.Receipt(
            check=args.check,
            conclusion=args.conclusion,
            tree=_tree_of(args.head),
            env=receipts.local_env(),
            scope=args.scope,
            ref=f"local:{args.head}",
            finished_at=datetime.now(UTC).isoformat(),
        ),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    route = commands.add_parser("route")
    route.add_argument("--repo", required=True)
    route.add_argument("--head", required=True)
    route.add_argument("--base", default="origin/main")
    route.add_argument("--env", choices=("ci", "local"), default="ci")
    route.add_argument("--limit", type=int, default=100)
    route.add_argument("--max-age-days", type=int, default=receipts.DEFAULT_MAX_AGE_DAYS)
    route.add_argument("--json", action="store_true")
    route.set_defaults(handler=_route)
    record = commands.add_parser("record")
    record.add_argument("--check", required=True)
    record.add_argument("--conclusion", required=True)
    record.add_argument("--scope", required=True)
    record.add_argument("--head", required=True)
    record.set_defaults(handler=_record)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
