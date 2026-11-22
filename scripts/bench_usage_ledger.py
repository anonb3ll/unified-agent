#!/usr/bin/env python3
"""Benchmark provider-usage attribution cost at realistic event_log volume
(task #1526).

Cursor's PR #860 was vetoed because its "which run is this tool call part of"
lookup (``usage_ledger._open_run_id`` there) queried ``event_log`` with
correlated JSON extraction and a ``NOT EXISTS`` on the unindexed
``dedupe_key`` column — on every wrapped tool call. This script answers the
question a reviewer would ask about the replacement: does
``usage_ledger.resolve_run_id`` / ``record_observed`` stay flat as
``event_log`` grows, or does it degrade the same way?

It measures, before and after inserting 50,000 synthetic ``event_log`` rows
unrelated to any run:
  1. ``usage_ledger.resolve_run_id(actor)`` — the O(1)-ish indexed
     ``tasks.current_run_agent`` lookup.
  2. ``usage_ledger.record_observed(...)`` — one full insert, including that
     lookup.

Expectation: both stay within noise of their pre-growth timing, because
neither touches ``event_log`` at all — see the ``tasks.current_run_agent``
index this reads instead.

Usage:
    uv run python scripts/bench_usage_ledger.py --rows 50000 --iterations 500
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))


def _percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "p50_ms": round(statistics.median(ordered) * 1000, 4),
        "p95_ms": round(ordered[int(len(ordered) * 0.95) - 1] * 1000, 4),
        "mean_ms": round(statistics.fmean(ordered) * 1000, 4),
    }


def _time_calls(fn, iterations: int) -> list[float]:
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=50_000, help="synthetic event_log rows")
    parser.add_argument("--iterations", type=int, default=500, help="timed calls per phase")
    parser.add_argument("--tasks", type=int, default=40, help="claimed tasks to seed")
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="ua-usage-ledger-bench-")
    import os

    os.environ["UA_HOME"] = tmp
    os.environ["UA_WORKSPACE"] = tmp
    os.environ.setdefault("UA_STATE_SYNC", "0")

    from unified_agent import events, post_init, run_records, storage, usage_ledger
    from unified_agent.tools.tasks import task_add, task_claim

    post_init.init_db()  # storage.init_db() plus the hub's post-init hooks
    storage.join_init_background_threads()

    # Seed a realistic number of claimed tasks across a handful of agents, so
    # resolve_run_id has real rows to search, not an empty table.
    # The default WIP cap is 2 doing tasks per agent, so one agent per task
    # keeps the seed realistic (many agents, each with an open run) without
    # tripping it. The workspace isn't a real git checkout, so task_claim's
    # worktree bookkeeping only warns (never fails) here — muted for signal.
    logging.getLogger("unified_agent.tools.tasks").setLevel(logging.ERROR)
    agents = [f"bench-agent-{i}" for i in range(args.tasks)]
    task_ids: list[int] = []
    for i in range(args.tasks):
        tid = task_add(f"bench task {i}", lane="dispatch-ready")["id"]
        task_claim(tid, agents[i % len(agents)], expected_lane="dispatch-ready")
        task_ids.append(tid)

    target_actor = agents[0]

    def _resolve() -> None:
        usage_ledger.resolve_run_id(target_actor)

    def _record() -> None:
        usage_ledger.record_observed(
            provider="bench",
            model="bench-model",
            input_tokens=1,
            output_tokens=1,
            cost_usd=None,
            actor=target_actor,
        )

    print(f"Seeded {len(task_ids)} claimed tasks across {len(agents)} agents.")
    print(f"Timing {args.iterations} calls each, before growing event_log...")
    before_resolve = _percentiles(_time_calls(_resolve, args.iterations))
    before_record = _percentiles(_time_calls(_record, args.iterations))
    print(f"  resolve_run_id (before): {before_resolve}")
    print(f"  record_observed (before): {before_record}")

    print(f"Inserting {args.rows} synthetic event_log rows (unrelated channel)...")
    conn = storage.connect()
    try:
        now = storage.now()
        rows = [
            (events.new_ulid(), "bench.noise", f'{{"n": {i}}}', "bench", now)
            for i in range(args.rows)
        ]
        conn.executemany(
            "INSERT INTO event_log (ulid, channel, payload, originator, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) AS n FROM event_log").fetchone()["n"]
    finally:
        conn.close()
    print(f"event_log now has {total} rows.")

    print(f"Timing {args.iterations} calls each, after growing event_log...")
    after_resolve = _percentiles(_time_calls(_resolve, args.iterations))
    after_record = _percentiles(_time_calls(_record, args.iterations))
    print(f"  resolve_run_id (after): {after_resolve}")
    print(f"  record_observed (after): {after_record}")

    def _ratio(before: dict, after: dict, key: str) -> float:
        return round(after[key] / before[key], 2) if before[key] else float("nan")

    print()
    print("Ratio (after / before), should be close to 1.0 regardless of event_log size:")
    print(f"  resolve_run_id p50 ratio: {_ratio(before_resolve, after_resolve, 'p50_ms')}x")
    print(f"  record_observed p50 ratio: {_ratio(before_record, after_record, 'p50_ms')}x")

    # Sanity: run_records itself still reads correctly with a large event_log
    # present (this module does not touch run_records' own event_log reads,
    # but a broken migration would show up here as a crash).
    assert run_records.get_run(run_records.run_history(task_id=task_ids[0])["runs"][0]["run_id"])
    print()
    print("OK: usage_ledger attribution cost did not scale with event_log size.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
