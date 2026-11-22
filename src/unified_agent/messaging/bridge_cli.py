"""``unified-agent-bridge``: run the messaging bridges once (task #1863).

    unified-agent-bridge run-once [--brain] [--a2a] [--dry-run] [--baseline] [--limit N]

With neither ``--brain`` nor ``--a2a`` both bridges run. It runs in process on the
hub host (it opens the hub database through ``storage.connect()``) and prints a
JSON report. Exit status 0 when nothing failed, 1 when any item failed, 2 when the
bridge could not run (missing brain repo, unreadable state, another run active).

``--baseline`` records everything already present as handled without sending it:
run it once before the first real run so history is not replayed. No timer or
service ships with this command; scheduling it is the operator's decision.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import bridge_a2a, bridge_brain, bridge_state


def run_once(
    *,
    brain: bool,
    a2a: bool,
    dry_run: bool = False,
    baseline: bool = False,
    limit: int = bridge_brain.DEFAULT_SEND_LIMIT,
) -> dict[str, Any]:
    bridge_state.check_capacity()
    with bridge_state.locked():
        state = bridge_state.load()
        out: dict[str, Any] = {"dry_run": dry_run, "baseline": baseline}
        try:
            if brain:
                out["brain"] = bridge_brain.run(
                    state, dry_run=dry_run, baseline=baseline, limit=limit
                )
            if a2a:
                out["a2a"] = bridge_a2a.run(state, dry_run=dry_run, baseline=baseline)
        finally:
            # Save what was carried even if a later step raised, so it is not resent.
            if not dry_run:
                bridge_state.save(state)
        return out


def _failed(report: dict[str, Any]) -> bool:
    return any(part.get("errors") for part in report.values() if isinstance(part, dict))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unified-agent-bridge",
        description="Carry messages between unified_brain threads, Hermes A2A gateways "
        "and the hub mailbox (task #1863).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    once = sub.add_parser("run-once", help="one pass of the selected bridges")
    once.add_argument("--brain", action="store_true", help="unified_brain threads <-> hub")
    once.add_argument("--a2a", action="store_true", help="hub -> Hermes A2A gateways and back")
    once.add_argument("--dry-run", action="store_true", help="report only; send and write nothing")
    once.add_argument(
        "--baseline",
        action="store_true",
        help="mark everything present now as handled without sending it",
    )
    once.add_argument(
        "--limit",
        type=int,
        default=bridge_brain.DEFAULT_SEND_LIMIT,
        help="most thread entries sent to the hub in one run",
    )
    args = parser.parse_args(argv)
    both = not (args.brain or args.a2a)
    try:
        report = run_once(
            brain=args.brain or both,
            a2a=args.a2a or both,
            dry_run=args.dry_run,
            baseline=args.baseline,
            limit=args.limit,
        )
    except (bridge_state.BridgeBusy, bridge_brain.BrainRepoError, RuntimeError, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, default=str))
    return 1 if _failed(report) else 0


if __name__ == "__main__":
    sys.exit(main())
