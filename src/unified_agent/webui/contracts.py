"""Validated request and response shapes for Command Center actions."""

from __future__ import annotations

from dataclasses import dataclass

from .. import config
from ..tools.tasks import PROJECTS

# Bump for breaking HTTP API field, type, or behavior changes. Additive fields
# and endpoints keep the current version; package releases have their own version.
API_VERSION = 1

MAX_PROMPT_CHARS = 1000
MAX_DISPATCH_STACK_TASKS = 50
ALLOWED_CONTEXT = frozenset({"task_notes", "recent_validation", "related_pr"})
DISPATCH_STATES = ("pending", "running", "completed", "blocked", "failed")


@dataclass(frozen=True)
class DispatchRequest:
    """The browser-facing dispatch payload after validation."""

    agent: str
    prompt: str
    task_id: int | None
    context: tuple[str, ...]
    project: str
    model: str | None = None


@dataclass(frozen=True)
class DispatchStackRequest:
    """The browser-facing stack selection after validation."""

    task_ids: tuple[int, ...]
    project: str | None
    expected_lane: str | None


def parse_dispatch_stack_request(body: object) -> DispatchStackRequest:
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    raw_ids = body.get("task_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ValueError("task_ids must be a non-empty list")
    if len(raw_ids) > MAX_DISPATCH_STACK_TASKS:
        raise ValueError(f"task_ids must contain {MAX_DISPATCH_STACK_TASKS} or fewer items")
    task_ids: list[int] = []
    for raw_id in raw_ids:
        if isinstance(raw_id, bool):
            raise ValueError("task_ids must contain positive integers")
        try:
            task_id = int(raw_id)
        except (TypeError, ValueError):
            raise ValueError("task_ids must contain positive integers") from None
        if task_id < 1 or task_id in task_ids:
            raise ValueError("task_ids must contain unique positive integers")
        task_ids.append(task_id)

    project = body.get("project")
    if project is not None:
        if not isinstance(project, str) or not project.strip():
            raise ValueError("project must be a non-empty string")
        project = project.strip()
        if project not in PROJECTS:
            raise ValueError(f"unknown project: {project}")
    expected_lane = body.get("expected_lane")
    if expected_lane is not None:
        if not isinstance(expected_lane, str) or not expected_lane.strip():
            raise ValueError("expected_lane must be a non-empty string")
        expected_lane = expected_lane.strip()
    return DispatchStackRequest(tuple(task_ids), project, expected_lane)


def parse_dispatch_request(body: object) -> DispatchRequest:
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")

    agent = body.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError("agent is required")
    agent = agent.strip()
    if not config.is_known_agent_label(agent):
        raise ValueError(f"unknown agent: {agent}")

    project = body.get("project")
    if not isinstance(project, str) or not project.strip():
        raise ValueError("project is required")
    project = project.strip()
    if project not in PROJECTS:
        raise ValueError(f"unknown project: {project}")

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required")
    prompt = prompt.strip()
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt must be {MAX_PROMPT_CHARS} characters or fewer")

    raw_task_id = body.get("task_id")
    task_id = None
    if raw_task_id not in (None, ""):
        if isinstance(raw_task_id, bool):
            raise ValueError("task_id must be a positive integer")
        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError):
            raise ValueError("task_id must be a positive integer") from None
        if task_id < 1:
            raise ValueError("task_id must be a positive integer")

    raw_context = body.get("context", [])
    if not isinstance(raw_context, list) or any(
        not isinstance(item, str) or item not in ALLOWED_CONTEXT for item in raw_context
    ):
        raise ValueError("context contains an unsupported value")
    context = tuple(dict.fromkeys(raw_context))

    model = None
    raw_model = body.get("model")
    if raw_model not in (None, ""):
        if not isinstance(raw_model, str):
            raise ValueError("model must be a string")
        model = config.sanitize_model_id(raw_model.strip(), source="dispatch model")
        if model is None:
            raise ValueError("model is not a valid model id")

    return DispatchRequest(
        agent=agent,
        prompt=prompt,
        task_id=task_id,
        context=context,
        project=project,
        model=model,
    )


def dispatch_spec(request: DispatchRequest) -> str:
    """Turn a UI payload into the existing delegation-spec format."""
    context = ", ".join(request.context) if request.context else "none"
    parent = f"Parent task: #{request.task_id}" if request.task_id else "No parent task"
    model_line = f"\n[model: {request.model}]" if request.model else ""
    return f"""### Objective
Execute the bounded user request below through the existing executor and return a reviewable result.

### Scope
- Project stream: {request.project}.
- Work only in the task-scoped workspace/worktree.
- {parent}.
- Requested context: {context}.{model_line}

### Definition of Done
- Verify the requested behavior with an observable result, not only a build or test status.
- Return a concise execution summary and identify changed files or the blocking reason.

### TDD Constraints
- Preserve existing tests and safety gates.
- Stub only external services when the existing test suite requires it; keep local behavior real.

### Boundaries
- Do not expose raw prompts, credentials, or execution logs through the web response.
- Do not broaden permissions, bypass approvals, or operate outside the task-scoped workspace.

USER REQUEST
{request.prompt}
"""
