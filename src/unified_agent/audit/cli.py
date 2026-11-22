"""CLI entry point (`unified-agent-audit`) so the audit can gate commits and CI.

Exit codes: 0 = clean at the chosen gate, 1 = findings at/above --fail-on, 2 = bad usage.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from . import baseline as audit_baseline
from .engine import format_text as _format_text
from .engine import run_audit
from .rules import SEVERITIES

_SEV_RANK = {sev: i for i, sev in enumerate(SEVERITIES)}
ACCEPTABLE = ("high", "medium")  # severities `baseline accept` records


def _selected(path: str, wanted: list[str]) -> bool:
    if not wanted:
        return True
    return any(path == w or path.startswith(w.rstrip("/") + "/") for w in wanted)


def _confirmed(args, count: int) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("baseline: nothing written; re-run with --yes to accept these", file=sys.stderr)
        return False
    try:
        answer = input(f"Accept these {count} finding(s) into the baseline? [y/N] ")
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


def _baseline_accept(args) -> int:
    """Preview the current high/medium SECRETS-KEY findings under the root,
    then, on --yes or an interactive yes, record exactly that snapshot: the
    audit runs once, so a value that appears after the preview is not taken."""
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"baseline: not a directory: {args.root}", file=sys.stderr)
        return 2
    if not (args.reason or "").strip():
        print("baseline: a non-empty --reason is required", file=sys.stderr)
        return 2
    wanted = [os.path.normpath(p).lstrip("/") for p in args.path]
    collected: list = []
    run_audit(root, excludes=args.exclude, collect_values=collected)
    items, in_git = [], 0
    for item in collected:
        if item["severity"] not in ACCEPTABLE or not item["values"]:
            continue
        if not _selected(item["path"], wanted):
            continue
        if item["in_git"]:
            in_git += 1  # repo source is never accepted
            continue
        items.append(item)
    if in_git:
        print(f"baseline: skipping {in_git} finding(s) inside a git work tree (repo source)")
    if not items:
        print(f"baseline: no high or medium secret findings to accept under {root}")
        return 0
    print(f"baseline: {len(items)} finding(s) under {root} (values are never shown):")
    for item in items:
        rules = sorted(
            {rule.removeprefix(audit_baseline.RULE_PREFIX) for rule, _ in item["values"]}
        )
        where = f"{item['path']}:{item['line']}" if item.get("line") else item["path"]
        print(f"  [{item['severity'].upper():6}] {where}  ({', '.join(rules)})")
    if not _confirmed(args, len(items)):
        return 1
    try:
        added, total = audit_baseline.append(items, root=root, reason=args.reason)
    except audit_baseline.BaselineError as exc:
        print(f"baseline: refused: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"baseline: cannot write ({type(exc).__name__})", file=sys.stderr)
        return 1
    plural = "" if len(items) == 1 else "s"
    print(
        f"baseline: accepted {len(items)} finding{plural} under {root} "
        f"({added} new value hashes); {total} entries in {audit_baseline.default_path()}"
    )
    return 0


def _baseline_list(args) -> int:
    loaded = audit_baseline.load()
    if loaded.warning:
        print(loaded.warning, file=sys.stderr)
        return 1
    rows = audit_baseline.listing(loaded)
    by_rule: dict[str, int] = {}
    for row in rows:
        by_rule[row["rule"]] = by_rule.get(row["rule"], 0) + 1
    if args.json:
        print(json.dumps({"path": str(loaded.path), "entries": rows, "by_rule": by_rule}, indent=2))
        return 0
    for row in rows:
        print(
            f"{row['at'][:10]}  {row['who']:16} {row['rule']:34} {row['sha256']}…  "
            f"{row['root']}/{row['path']}  — {row['reason']}"
        )
    counts = ", ".join(f"{rule} {n}" for rule, n in sorted(by_rule.items())) or "none"
    noun = "entry" if len(rows) == 1 else "entries"
    print(f"{len(rows)} {noun} in {loaded.path} ({counts})")
    return 0


def run_baseline_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="unified-agent-audit baseline",
        description=(
            "Operator baseline: preview and accept the current HIGH/MEDIUM secret findings "
            "under an agent-home root by value hash (never the value), or list entries. "
            "Repo source (any file inside a git work tree) is never accepted."
        ),
    )
    sub = parser.add_subparsers(dest="action", required=True)
    accept = sub.add_parser("accept", help="record the current secret findings under ROOT")
    accept.add_argument("root", help="directory to audit (as for unified-agent-audit)")
    accept.add_argument("--reason", required=True, help="why these findings are not leaks")
    accept.add_argument("--exclude", action="append", default=[], help="glob to exclude")
    accept.add_argument(
        "--path",
        action="append",
        default=[],
        help="accept only this file or directory, relative to ROOT (repeatable)",
    )
    accept.add_argument(
        "--yes", action="store_true", help="accept the previewed findings without prompting"
    )
    show = sub.add_parser("list", help="show accepted entries (hashes truncated)")
    show.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    return _baseline_accept(args) if args.action == "accept" else _baseline_list(args)


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    if args_list and args_list[0] == "verify":
        from .ledger_cli import run_verify_cli

        return run_verify_cli(args_list[1:])
    if args_list and args_list[0] == "re-anchor":
        from .ledger_cli import run_reanchor_cli

        return run_reanchor_cli(args_list[1:])
    if args_list and args_list[0] == "evidence":
        from .evidence_cli import run_evidence_cli

        return run_evidence_cli(args_list[1:])
    if args_list and args_list[0] == "baseline":
        return run_baseline_cli(args_list[1:])
    if args_list and args_list[0] == "surfaces":
        from .surfaces import run_surfaces_cli

        return run_surfaces_cli(args_list[1:])

    parser = argparse.ArgumentParser(
        prog="unified-agent-audit",
        description="Audit a project for AI-assisted-development security best practices.",
    )
    parser.add_argument("path", nargs="?", default=".", help="project root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
    parser.add_argument(
        "--fail-on",
        choices=[*SEVERITIES, "none"],
        default="high",
        help="exit 1 if any finding is at least this severe (default: high)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="glob pattern of paths to exclude from auditing (can specify multiple times)",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="apply the operator baseline ($UA_HOME/audit-baseline.json); never applied "
        "by default, and never to a file inside a git work tree",
    )
    args = parser.parse_args(argv)

    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"not a directory: {args.path}")

    # Verification gates (CI, local-gate, hooks, bug worker) run this command:
    # the baseline applies only on an explicit request (#2323 review).
    baseline = audit_baseline.load() if args.baseline else None
    if baseline is not None and baseline.warning:
        print(baseline.warning, file=sys.stderr)
    report = run_audit(root, excludes=args.exclude, baseline=baseline)
    print(json.dumps(report, indent=2) if args.json else _format_text(report))

    if args.fail_on == "none":
        return 0
    gate = _SEV_RANK[args.fail_on]
    gated = any(_SEV_RANK[fd["severity"]] <= gate for fd in report["findings"])
    return 1 if gated else 0


if __name__ == "__main__":
    sys.exit(main())
