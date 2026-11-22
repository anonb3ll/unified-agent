"""CLI entry point: unified-agent-dispatch-scheduler (tasks #550–#551).

Opt-in poller over the approved-dispatch ready set. Does **not** install or
enable systemd/Hermes cron timers — operators must wire enablement explicitly.
"""

from __future__ import annotations

import argparse
import json
import sys

from .coordinator import get_run_observability, list_runs
from .report import NotDispatchedError, report_outcome
from .scheduler import DispatchScheduler, list_ready_candidates, scheduler_tick


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unified-agent-dispatch-scheduler",
        description=(
            "Durable approved-dispatch scheduler tick (task #550). "
            "Disabled by default; set UA_DISPATCH_SCHEDULER_ENABLED=1 or pass --force."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status_p = sub.add_parser("status", help="report enablement and tuning")
    status_p.add_argument("--json", action="store_true")

    ready_p = sub.add_parser("ready", help="list evaluate_ready candidates (read-only)")
    ready_p.add_argument("--limit", type=int, default=20)
    ready_p.add_argument("--json", action="store_true")

    tick_p = sub.add_parser("tick", help="one reclaim + claim poll")
    tick_p.add_argument(
        "--force",
        action="store_true",
        help="run one tick even when UA_DISPATCH_SCHEDULER_ENABLED=0",
    )
    tick_p.add_argument("--json", action="store_true")

    observe_p = sub.add_parser(
        "observe",
        help="operator view for a run_id (task #551 telemetry)",
    )
    observe_p.add_argument("run_id", help="dispatch run_id")
    observe_p.add_argument("--json", action="store_true")

    runs_p = sub.add_parser("runs", help="list recent dispatch_runs")
    runs_p.add_argument("--task-id", type=int, default=None)
    runs_p.add_argument("--state", default=None)
    runs_p.add_argument("--limit", type=int, default=20)
    runs_p.add_argument("--json", action="store_true")

    report_p = sub.add_parser(
        "report",
        help="record a terminal outcome for a dispatched run (task #601)",
        description=(
            "Report the outcome of THIS dispatched session. Takes no run_id "
            "when run inside a spawned session: UA_DISPATCH_RUN_ID is already "
            "in the environment. Refuses rather than guessing when it is not."
        ),
    )
    report_p.add_argument("state", choices=("finished", "failed", "retryable", "blocked"))
    report_p.add_argument("--run-id", default=None, help="override UA_DISPATCH_RUN_ID")
    report_p.add_argument("--reason", default=None, help="failure_reason for a non-finished state")
    report_p.add_argument(
        "--evidence",
        default=None,
        help="evidence_ref an exit code cannot carry — branch, PR URL, or commit sha",
    )
    report_p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "status":
        payload = DispatchScheduler().status()
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            print(
                f"enabled={payload['enabled']} "
                f"max_attempts={payload['max_attempts']} "
                f"batch_limit={payload['batch_limit']} "
                f"lease_ttl_s={payload['lease_ttl_s']}"
            )
        return 0

    if args.command == "ready":
        items = list_ready_candidates(limit=args.limit)
        if args.json:
            print(json.dumps({"ready": items}, indent=2, default=str))
        else:
            if not items:
                print("no ready approved-dispatch candidates")
            for item in items:
                print(
                    f"task=#{item['task_id']} delegation=#{item['delegation_id']} "
                    f"agent={item['agent_label']}"
                )
        return 0

    if args.command == "tick":
        result = scheduler_tick(force=args.force)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(
                f"status={result.get('status')} "
                f"launched={len(result.get('launched') or [])} "
                f"reclaimed={result.get('reclaimed_count', 0)} "
                f"dead_lettered={result.get('dead_lettered_count', 0)}"
            )
            if result.get("status") == "disabled":
                print(
                    "scheduler disabled — set UA_DISPATCH_SCHEDULER_ENABLED=1 "
                    "or re-run with --force",
                    file=sys.stderr,
                )
                return 0
        return 0

    if args.command == "report":
        try:
            result = report_outcome(
                args.state,
                run_id=args.run_id,
                failure_reason=args.reason,
                evidence_ref=args.evidence,
            )
        except NotDispatchedError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            if result.get("ok"):
                print(f"run={result.get('run_id')} state={args.state} recorded")
            else:
                print(
                    f"run={result.get('run_id')} not recorded: {result.get('reason')} "
                    f"(current state={result.get('state')!r})",
                    file=sys.stderr,
                )
        # A refusal is information, not a crash: 'already_terminal' normally
        # means the supervisor and the agent both reported, which is the design
        # working. Exit 1 so a script can tell, without implying breakage.
        return 0 if result.get("ok") else 1

    if args.command == "observe":
        payload = get_run_observability(args.run_id)
        if payload is None:
            print(f"unknown run_id={args.run_id}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            print(
                f"task_id={payload['task_id']} agent={payload['agent_label']} "
                f"run_id={payload['run_id']} state={payload['state']} "
                f"retry_count={payload['retry_count']} "
                f"last_error={payload['last_error']!r} "
                f"evidence={payload['evidence_location']!r}"
            )
        return 0

    if args.command == "runs":
        items = list_runs(task_id=args.task_id, state=args.state, limit=args.limit)
        if args.json:
            print(json.dumps({"runs": items}, indent=2, default=str))
        else:
            if not items:
                print("no dispatch_runs")
            for item in items:
                print(
                    f"run={item['run_id']} task=#{item['task_id']} "
                    f"agent={item['agent_label']} state={item['state']} "
                    f"error={item.get('failure_reason')!r}"
                )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
