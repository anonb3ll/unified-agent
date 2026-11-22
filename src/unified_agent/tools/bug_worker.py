"""MCP dispatch for bug worker operations."""

from __future__ import annotations

import inspect

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, profiles, safety
from ..bug_worker.eligibility import is_system_bug_eligible
from ..bug_worker.worker import BugWorker


def bug_worker_discover(*, agent: str, limit: int = 20) -> dict:
    """List frontier SYSTEM bug tickets eligible for automated patching."""
    _agent = _require_agent(agent)
    worker = BugWorker(_agent)
    return {"enabled": worker.enabled(), "tasks": worker.discover(limit=limit)}


def bug_worker_run_once(
    *,
    agent: str,
    task_id: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Run one bounded bug-worker cycle (requires an injected patcher in-process)."""
    _agent = _require_agent(agent)
    worker = BugWorker(_agent)
    return worker.run_once(task_id=task_id, dry_run=dry_run)


def bug_worker_status(*, agent: str) -> dict:
    """Report kill-switch and configuration state."""
    _require_agent(agent)
    return {
        "enabled": config.bug_worker_enabled(),
        "max_attempts": config.bug_worker_max_attempts(),
        "agent_label": agent,
    }


def bug_worker_check_eligibility(*, task_id: int) -> dict:
    """Evaluate eligibility markers for one task without claiming."""
    from ..bug_worker.worker import _find_task

    task = _find_task(task_id)
    if task is None:
        raise ToolError(f"no task with id {task_id}")
    ok, reason = is_system_bug_eligible(task)
    return {"task_id": task_id, "eligible": ok, "reason": reason}


def _require_agent(agent: str) -> str:
    label = (agent or "").strip()
    if not label or not config.is_known_agent_label(label):
        raise ToolError("a known agent label is required")
    verified = config.verified_agent_identity()
    if verified and verified != label:
        raise ToolError(f"auth identity mismatch: requested {label!r} but verified as {verified!r}")
    return label


_ACTIONS = {
    "discover": bug_worker_discover,
    "run_once": bug_worker_run_once,
    "status": bug_worker_status,
    "check_eligibility": bug_worker_check_eligibility,
}


def bug_worker_manage(action: str, params: dict | None = None) -> dict:
    """Dispatch bug-worker lifecycle operations through one API."""
    action = (action or "").strip().lower()
    fn = _ACTIONS.get(action)
    if fn is None:
        raise ToolError(f"action must be one of {tuple(_ACTIONS)}")
    if not profiles.consolidated_action_is_allowed("bug_worker_manage", fn.__name__):
        raise ToolError(f"bug worker action {action!r} is not exposed by the active profile")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ToolError("params must be an object")
    try:
        bound = inspect.signature(fn).bind(**params)
    except TypeError as exc:
        raise ToolError(f"invalid params for bug worker action {action!r}: {exc}") from exc
    safety.check_policy_gate(fn.__name__, bound.arguments)
    return fn(*bound.args, **bound.kwargs)
