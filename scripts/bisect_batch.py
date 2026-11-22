#!/usr/bin/env python3
"""Find the pull request that broke a batch, by binary search.

The batch is an ordered list of pull-request numbers (merge order). The runner
is a command that exits 0 when the prefix in ``BISECT_BATCH_PRS`` (comma-
separated) passes and non-zero when it fails. The empty prefix is not run;
the whole batch is. One pull request is assumed to be the first failure.

  python3 scripts/bisect_batch.py 101 102 103 104 -- ./scripts/run_prefix.sh
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable


class BatchPassed(Exception):
    """The whole batch passed, so there is no pull request to blame."""


def bisect(prs: list[str], runner: Callable[[list[str]], bool]) -> str:
    """Return the first pull request whose prefix fails.

    ``runner`` returns True when that prefix passes. Results are cached.
    """
    if not prs:
        raise ValueError("empty batch")
    cache: dict[tuple[str, ...], bool] = {}

    def passes(prefix: list[str]) -> bool:
        key = tuple(prefix)
        if key not in cache:
            cache[key] = runner(prefix)
        return cache[key]

    if passes(list(prs)):
        raise BatchPassed(prs[-1])
    lo, hi = 0, len(prs) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if passes(prs[: mid + 1]):
            lo = mid + 1
        else:
            hi = mid
    return prs[lo]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--" not in args:
        print("usage: bisect_batch.py PR [PR ...] -- COMMAND", file=sys.stderr)
        return 2
    split = args.index("--")
    prs, command = args[:split], args[split + 1 :]
    if not prs or not command:
        print("usage: bisect_batch.py PR [PR ...] -- COMMAND", file=sys.stderr)
        return 2

    def runner(prefix: list[str]) -> bool:
        env = os.environ.copy()
        env["BISECT_BATCH_PRS"] = ",".join(prefix)
        return subprocess.run(command, env=env).returncode == 0

    try:
        bad = bisect(prs, runner)
    except BatchPassed:
        print("bisect: the batch passed", file=sys.stderr)
        return 2
    print(bad)
    return 0


if __name__ == "__main__":
    sys.exit(main())
