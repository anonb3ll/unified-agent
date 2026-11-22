"""In-container backup schedule for a hosted hub (task #2585).

A hosted hub (Railway, or a VM running the image) has no systemd timer or
launchd agent, and a Railway cron service cannot mount the hub's volume. When
``UA_BACKUP_INTERVAL_S`` is set, the container entrypoint starts this loop in
the background, as the same unprivileged user as the hub. It waits
``UA_BACKUP_FIRST_DELAY_S`` (default 300 s, so startup migrations finish
first), then runs the scheduled backup (``backup_runner.main``) every
interval. Each run prints the runner's one JSON line to stdout, which the
platform log keeps.

A failed run is logged and the loop carries on; nothing here can stop the
hub. The interval is at least an hour, so a typo cannot snapshot the
database in a tight loop.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable

from . import backup_runner

MIN_INTERVAL_S = 3600
DEFAULT_FIRST_DELAY_S = 300


def _seconds(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def interval_from_env() -> int | None:
    """``UA_BACKUP_INTERVAL_S`` raised to the one-hour floor, or None when unset."""
    value = _seconds("UA_BACKUP_INTERVAL_S")
    if value is None or value <= 0:
        return None
    return max(MIN_INTERVAL_S, value)


def first_delay_from_env() -> int:
    value = _seconds("UA_BACKUP_FIRST_DELAY_S")
    return DEFAULT_FIRST_DELAY_S if value is None or value < 0 else value


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"backup_loop": event, **fields}, sort_keys=True), flush=True)


def run(
    *,
    interval: int,
    first_delay: int,
    runner: Callable[[], int] = backup_runner.main,
    sleep: Callable[[float], None] = time.sleep,
    max_runs: int | None = None,
) -> int:
    """Run ``runner`` after ``first_delay`` and then every ``interval`` seconds."""
    _log("started", interval_s=interval, first_delay_s=first_delay)
    sleep(first_delay)
    runs = 0
    while max_runs is None or runs < max_runs:
        try:
            code = runner()
            if code:
                _log("run_failed", exit_code=code)
        except Exception as exc:  # a failed snapshot must not end the schedule
            # The type only: an exception from an upload path can quote a
            # signed request or a provider body (#2593, #2598).
            _log("run_failed", error=type(exc).__name__)
        sys.stdout.flush()
        runs += 1
        if max_runs is not None and runs >= max_runs:
            break
        sleep(interval)
    return 0


def main() -> int:
    interval = interval_from_env()
    if interval is None:
        _log("disabled", reason="UA_BACKUP_INTERVAL_S unset or not a positive integer")
        return 0
    return run(interval=interval, first_delay=first_delay_from_env())


if __name__ == "__main__":
    raise SystemExit(main())
