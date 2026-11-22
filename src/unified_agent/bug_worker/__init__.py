"""Bounded SYSTEM bug-to-patch worker (task #488).

Discovers eligible SYSTEM bug tickets, claims atomically, patches in an isolated
worktree, runs deterministic verification gates, and hands off to human review —
never auto-merge or auto-deploy.
"""

from .eligibility import discover_eligible_tasks, is_system_bug_eligible
from .state import WORKER_STATES, transition_worker_state, validate_transition
from .worker import BugWorker, PatcherProtocol, PatchResult

__all__ = [
    "BugWorker",
    "PatchResult",
    "PatcherProtocol",
    "WORKER_STATES",
    "discover_eligible_tasks",
    "is_system_bug_eligible",
    "transition_worker_state",
    "validate_transition",
]
