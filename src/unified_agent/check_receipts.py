"""Fingerprinted check receipts and the merge route they imply (task #2412).

A receipt says one check finished on one git tree in one environment. The
router reads receipts and answers how a merge should be verified:

* ``reuse``: the newest terminal full run on this exact tree and environment
  passed, and it is recent enough.
* ``targeted``: no such run, and CI's own classifier can certify this change
  with a subset.
* ``full``: anything else. An unknown tree, environment or selection is full.

Report mode only. Nothing here changes a gate, and a ``reuse`` answer does not
replace DEPLOY-STEWARD step 3: after the last member lands, ``origin/main``'s
tree must still equal the tree the receipt names.

Stdlib only, and it imports nothing from this package, so
``scripts/gate_route.py`` can load it without the project's dependencies.
"""

from __future__ import annotations

import hashlib
import json
import platform
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REUSE = "reuse"
TARGETED = "targeted"
FULL = "full"

SCOPE_FULL = "full"
SCOPE_PARTIAL = "partial"
SUCCESS = "success"
FAILURE = "failure"
DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_RUNNER = "ubuntu-latest"
STORE_NAME = "ua-check-receipts.jsonl"

# Only these events run the whole suite (docs/ci-batch-verify.md). A pull
# request's cheap gates and a merge queue's affected subset can both conclude
# success without running it.
FULL_EVENTS = frozenset({"workflow_dispatch", "schedule"})
VERIFICATION_JOB = "full-verification"
# Pinned to ci_changes.FULL_SHARDS by tests/test_check_receipts.py.
SHARD_JOBS = tuple(f"test (shard={shard})" for shard in range(4))


@dataclass(frozen=True)
class Receipt:
    """One finished check. ``tree`` and ``env`` are what it can be reused for."""

    check: str
    conclusion: str
    tree: str
    env: str
    scope: str
    ref: str
    finished_at: str


@dataclass(frozen=True)
class Route:
    """How to verify a merge, and why."""

    route: str
    reason: str
    proof: str | None = None
    tests: tuple[str, ...] = field(default=())
    ignored: int = 0


def env_fingerprint(facts: Mapping[str, str]) -> str:
    """A digest of ``facts``, or empty when any fact is missing (empty is unknown)."""
    if not facts or not all(facts.values()):
        return ""
    canonical = json.dumps(dict(facts), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def ci_env(runner_labels: Sequence[str]) -> str:
    """The fingerprint of a GitHub Actions environment, from its runner labels."""
    labels = sorted({label for label in runner_labels if label})
    return env_fingerprint({"backend": "actions", "runner": ",".join(labels)})


def local_env() -> str:
    """The fingerprint of this machine. Locked dependencies are part of the tree."""
    return env_fingerprint(
        {
            "backend": "local",
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
        }
    )


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def classify(
    tree: str,
    env: str,
    receipts: Iterable[Receipt],
    *,
    selection: Sequence[str] | None,
    now: datetime | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> Route:
    """Route one merge. ``selection`` is CI's certified test list, None for the full suite."""
    if not tree or not env:
        return Route(FULL, "the tree or the environment is unknown")
    full_runs = [r for r in receipts if r.tree == tree and r.env == env and r.scope == SCOPE_FULL]
    decided = [r for r in full_runs if r.conclusion in (SUCCESS, FAILURE)]
    ignored = len(full_runs) - len(decided)
    dated = [(_parse_time(r.finished_at), r) for r in decided]
    if any(when is None for when, _ in dated):
        return Route(FULL, "a full run on this tree has no readable finish time", ignored=ignored)
    stale = ""
    if dated:
        # On a tie the failure is the newer fact, so a flake is never hidden.
        when, newest = max(dated, key=lambda item: (item[0], item[1].conclusion == FAILURE))
        if newest.conclusion == FAILURE:
            return Route(
                FULL,
                f"the newest full run on this tree failed ({newest.ref})",
                proof=newest.ref,
                ignored=ignored,
            )
        reference = now or datetime.now(UTC)
        if reference - when <= timedelta(days=max_age_days):
            return Route(
                REUSE,
                f"a full run passed on this exact tree and environment ({newest.ref})",
                proof=newest.ref,
                ignored=ignored,
            )
        stale = f"the passing full run is older than {max_age_days} days; "
    if selection is None:
        return Route(FULL, f"{stale}this change needs the full suite", ignored=ignored)
    if not selection:
        return Route(FULL, f"{stale}no test selection could be made", ignored=ignored)
    return Route(
        TARGETED,
        f"{stale}CI's classifier can certify this change with {len(selection)} test file(s)",
        tests=tuple(selection),
        ignored=ignored,
    )


def receipt_from_run(
    run: Mapping[str, Any], jobs: Sequence[Mapping[str, Any]], tree: str, env: str
) -> Receipt:
    """The receipt for one completed Actions run.

    A run counts as full only when it could have run everything and its
    verification job and every shard passed. A failed or cancelled run keeps
    full scope when its event was a full one, so it can still veto a reuse.
    """
    results = {str(job.get("name")): job.get("conclusion") for job in jobs}
    conclusion = str(run.get("conclusion") or "unknown")
    full_event = run.get("event") in FULL_EVENTS
    proved = full_event and all(
        results.get(name) == SUCCESS for name in (VERIFICATION_JOB, *SHARD_JOBS)
    )
    keeps_scope = proved or (full_event and conclusion != SUCCESS)
    return Receipt(
        check=f"ci:{VERIFICATION_JOB}",
        conclusion=conclusion,
        tree=tree,
        env=env,
        scope=SCOPE_FULL if keeps_scope else SCOPE_PARTIAL,
        ref=str(run.get("url") or run.get("databaseId") or ""),
        finished_at=str(run.get("updatedAt") or ""),
    )


def actions_receipts(
    repo: str,
    target_tree: str,
    *,
    tree_of: Callable[[str], str],
    gh_json: Callable[[list[str]], Any],
    limit: int = 100,
) -> list[Receipt]:
    """Receipts for the completed CI runs whose head has ``target_tree``.

    A run whose head commit is not in this clone cannot be matched and is left
    out, which can only send the route toward full.
    """
    runs = gh_json(
        [
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            "ci.yml",
            "--limit",
            str(limit),
            "--json",
            "databaseId,headSha,event,status,conclusion,updatedAt,url",
        ]
    )
    receipts: list[Receipt] = []
    for run in runs:
        if run.get("status") != "completed" or tree_of(str(run.get("headSha"))) != target_tree:
            continue
        listing = gh_json(
            ["api", f"repos/{repo}/actions/runs/{run['databaseId']}/jobs?per_page=100"]
        )
        jobs = listing.get("jobs", [])
        labels = [
            str(label) for job in jobs if job.get("name") in SHARD_JOBS for label in job["labels"]
        ]
        receipts.append(receipt_from_run(run, jobs, target_tree, ci_env(labels)))
    return receipts


def append_receipt(path: Path, receipt: Receipt) -> None:
    """Add one receipt. A single short append, so concurrent gates do not interleave."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(receipt), sort_keys=True) + "\n")


def read_receipts(path: Path) -> list[Receipt]:
    """Every readable receipt in ``path``. A damaged line is evidence of nothing."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    receipts: list[Receipt] = []
    for line in lines:
        try:
            receipts.append(Receipt(**json.loads(line)))
        except (TypeError, ValueError):
            continue
    return receipts
