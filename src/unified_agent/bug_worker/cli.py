"""CLI entry point: unified-agent-bug-worker."""

from __future__ import annotations

import argparse
import json
import sys

from mcp.server.mcpserver.exceptions import ToolError

from .worker import BugWorker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unified-agent-bug-worker",
        description="Bounded SYSTEM bug-to-patch worker (task #488).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="report whether the worker is enabled")
    discover_p = sub.add_parser("discover", help="list eligible SYSTEM bug tickets")
    discover_p.add_argument("--limit", type=int, default=20)
    discover_p.add_argument("--json", action="store_true")

    run_p = sub.add_parser("run-once", help="claim and process one eligible ticket")
    run_p.add_argument("--task-id", type=int, default=None)
    run_p.add_argument("--agent", default="Desktop_agent3")
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    agent = args.agent if hasattr(args, "agent") else "Desktop_agent3"

    if args.command == "status":
        enabled = BugWorker.enabled()
        print(
            json.dumps({"enabled": enabled}) if "--json" in sys.argv[1:] else f"enabled={enabled}"
        )
        return 0

    worker = BugWorker(agent)

    if args.command == "discover":
        tasks = worker.discover(limit=args.limit)
        if args.json:
            print(json.dumps({"tasks": tasks}, indent=2))
        else:
            if not tasks:
                print("no eligible SYSTEM bug tickets")
            for t in tasks:
                print(f"#{t['id']} {t['title']}")
        return 0

    if args.command == "run-once":
        try:
            result = worker.run_once(task_id=args.task_id, dry_run=args.dry_run)
        except ToolError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(result.get("status", "unknown"), result)
        if result.get("status") in ("blocked", "escalated"):
            return 1
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
