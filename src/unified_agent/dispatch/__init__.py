"""Approved-dispatch coordinator package (tasks #548–#551). DEPRECATED.

.. deprecated::
    Dispatch lane A was retired by task #1435 (2026-09-28). Its periodic
    driver, the ``com.unified-agent.dispatch-tick`` LaunchAgent (and the
    ``ua-dispatch-tick`` systemd timer), is disabled and booted out on the hub;
    ``dispatch_runs`` last recorded a run on 2026-08-27. The work model that
    replaced it is agent self-pickup — claims and finishes recorded as runs by
    ``unified_agent.run_records`` (task #1436) — plus the executor daemons
    (lane B, ``unified_agent.executor``). The ``UA_DISPATCH_*`` gates are
    retired with it (docs/configuration.md). The code is kept, not deleted:
    ``run_records`` and the live-state views still read ``dispatch_runs``, and
    manual ``unified-agent-dispatch-scheduler`` invocations still work. Do not
    build new features on this package.

Fail-closed path from operator-approved delegation → durable run record →
named-agent route → optional scheduler tick → fake-worker smoke (#551).
Uses existing task/delegation/approval/event stores; ``dispatch_runs`` is an
index over those, not a second mailbox. Scheduler remains off until
``UA_DISPATCH_SCHEDULER_ENABLED=1``.
"""

from .coordinator import (
    DISPATCH_APPROVAL_TOOL,
    claim_and_dispatch,
    dispatch_approval_payload,
    evaluate_ready,
    get_run,
    get_run_observability,
    list_runs,
    reclaim_expired_leases,
)
from .launch import (
    CliProcessSpawner,
    NoopLaunchBoundary,
    RecordingLaunchBoundary,
    RoutedLaunchBoundary,
    StubProcessSpawner,
)
from .routing import (
    DISPATCHABLE_LABELS,
    AgentRoute,
    all_routes,
    resolve_named_agent,
    resolve_workspace_for_route,
)
from .scheduler import (
    DispatchScheduler,
    dead_letter_exhausted,
    list_ready_candidates,
    reconcile_finished_runs,
    record_run_outcome,
    scheduler_tick,
)
from .worker import (
    FakeWorkerProcessSpawner,
    complete_smoke_run,
    complete_smoke_run_from_dir,
)

__all__ = [
    "CliProcessSpawner",
    "DISPATCH_APPROVAL_TOOL",
    "DISPATCHABLE_LABELS",
    "AgentRoute",
    "DispatchScheduler",
    "FakeWorkerProcessSpawner",
    "NoopLaunchBoundary",
    "RecordingLaunchBoundary",
    "RoutedLaunchBoundary",
    "StubProcessSpawner",
    "all_routes",
    "claim_and_dispatch",
    "complete_smoke_run",
    "complete_smoke_run_from_dir",
    "dead_letter_exhausted",
    "dispatch_approval_payload",
    "evaluate_ready",
    "get_run",
    "get_run_observability",
    "list_ready_candidates",
    "list_runs",
    "reclaim_expired_leases",
    "reconcile_finished_runs",
    "record_run_outcome",
    "resolve_named_agent",
    "resolve_workspace_for_route",
    "scheduler_tick",
]
