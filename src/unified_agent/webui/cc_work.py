"""Control Center v2 work routes: plans, task claim and delete, sessions (#2466, #2469).

Thin adapters over registered hub tools, built with the shared helpers in
``cc_knowledge`` so every call goes through the tool's guarded wrapper (profile
ceiling, policy gate, approval gate, rate limit). Identity arguments come from the
verified request identity. Request and response shapes are in
``docs/control-center-api.md``.

Spec routes that are not added here, and why:

* ``POST /api/action/task/comment``: ``POST /api/tasks/{task_id}/comments`` already
  appends a comment through ``task_comment`` with the verified author.
* ``POST /api/goals`` stays the goal create (and legacy link) route. The goal
  edits task #2480 added (``goal_manage`` edit, link, unlink, close, reopen) get
  one fixed path each under ``/api/action/goal/``.
* ``POST /api/action/plan`` stays the plan-review (``intent``) route. Each plan_manage
  write action gets its own fixed path and scope instead
  (``/api/action/plan/<action>``, ``/api/action/plan/joint/<action>``), because a
  scope requires every action it lists.
* Plan structure (``step-add``, ``step-edit``, ``step-remove``, ``reorder``, ``edit``)
  is five more ``plan_manage`` actions with one route each (task #2480).
* ``POST /api/action/session/operator-end`` is the verified human operator's: it
  calls the ``session.operator_end`` platform writer through the governed write
  adapter (``api._registered_platform_write``), never ``session_end`` with a
  borrowed identity (task #2480).
* LangGraph runs and routines live in ``cc_runs`` (task #2480).
"""

from __future__ import annotations

import json

import anyio
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .. import agent_access
from ..tools import approvals as approvals_tool
from . import api, routes
from .cc_knowledge import (
    Field,
    _idempotency_key,
    adapt,
    bind,
    call,
    call_once,
    identity_or_401,
    pending,
    read_body,
    spoofed,
)


def _not_deleted(kind: str, result: object) -> Response | None:
    """404 when a delete tool reports that nothing matched (``deleted: false``)."""
    if isinstance(result, dict) and result.get("deleted") is False:
        return routes._json({"error": f"no such {kind}"}, status=404)
    return None


# --- plans --------------------------------------------------------------------------

_PLAN_ID = Field("plan_id", required=True, limit=128)
_STEP_IDX = Field("step_idx", "int", required=True, limit=10_000)
_REVISION = Field("expected_revision", "int", required=True, limit=2**31 - 1)
_COMMENT = Field("comment_id", "int", required=True, limit=2**31 - 1)
_OUTPUTS = Field("outputs", "object")
_NOTE = Field("note", limit=4_000)
#: The plan's ``revision`` as last read; a stale value answers 409 (task #2480).
_EXPECTED_REVISION = Field("expected_revision", "int", limit=2**62)
_STEP_TEXT = Field("description", required=True, limit=4_000)
#: The step's immutable row id (``steps[].id``) as last read; a step that moved
#: answers 409 (task #2480).
_STEP_ID = Field("expected_step_id", "int", limit=2**62)

#: One route per plan_manage action: its accepted fields and the argument filled
#: from the verified identity (``None`` when the action takes none). Each route's
#: scope declares exactly that action's ``plan_*`` function in ``Scope.actions``.
PLAN_ACTIONS: dict[str, tuple[tuple[Field, ...], str | None]] = {
    "create": (
        (
            Field("goal", required=True, limit=4_000),
            Field("steps", "list", required=True, limit=100),
            Field("scope", limit=100),
        ),
        "author",
    ),
    "update": (
        (
            _PLAN_ID,
            _STEP_IDX,
            Field("status", limit=16, choices=("open", "doing", "done", "failed", "blocked")),
            _NOTE,
            Field("owner", limit=200),
            _STEP_ID,
        ),
        None,
    ),
    "checkpoint": (
        (
            _PLAN_ID,
            Field("memory_ids", "strlist", limit=100, item_limit=200),
            Field("file_paths", "strlist", limit=100, item_limit=1_000),
            _OUTPUTS,
            _NOTE,
        ),
        None,
    ),
    "resume": ((_PLAN_ID, Field("from_checkpoint", "int", limit=1_000_000)), None),
    "retry": (
        (_PLAN_ID, _STEP_IDX, Field("reason", required=True, limit=4_000), _STEP_ID),
        None,
    ),
    "claim": ((_PLAN_ID, _STEP_IDX, _STEP_ID), "agent"),
    "handoff": (
        (_PLAN_ID, _STEP_IDX, _OUTPUTS, _NOTE, Field("notify_owner", "bool"), _STEP_ID),
        "agent",
    ),
    "close": ((_PLAN_ID, Field("reason", required=True, limit=4_000), _OUTPUTS), None),
    "delete": ((_PLAN_ID,), None),
    "step_add": (
        (
            _PLAN_ID,
            _STEP_TEXT,
            Field("position", "int", limit=10_000),
            Field("owner", limit=200),
            Field("depends_on", "list", limit=100),
            _EXPECTED_REVISION,
        ),
        None,
    ),
    "step_edit": ((_PLAN_ID, _STEP_IDX, _STEP_TEXT, _EXPECTED_REVISION, _STEP_ID), None),
    "step_remove": ((_PLAN_ID, _STEP_IDX, _EXPECTED_REVISION, _STEP_ID), None),
    "step_reorder": (
        (_PLAN_ID, Field("order", "list", required=True, limit=1_000), _EXPECTED_REVISION),
        None,
    ),
    "edit": (
        (
            _PLAN_ID,
            Field("title", limit=200),
            Field("description", limit=4_000),
            _EXPECTED_REVISION,
        ),
        None,
    ),
    "joint_bind": (
        (
            _PLAN_ID,
            Field("goal_id", "int", required=True, limit=2**31 - 1),
            Field("org_unit_id", required=True, limit=200),
            Field("comment_id", "int", limit=2**31 - 1),
        ),
        "author",
    ),
    "joint_propose": (
        (_PLAN_ID, _REVISION, Field("steps", "list", required=True, limit=100), _COMMENT),
        "author",
    ),
    "joint_review": (
        (_PLAN_ID, _REVISION, Field("decision", required=True, limit=32), _COMMENT),
        "author",
    ),
    "joint_decide": (
        (_PLAN_ID, _REVISION, Field("text", required=True, limit=8_000), _COMMENT),
        "author",
    ),
    "joint_recover": ((_PLAN_ID,), "author"),
}


#: Plan actions that accept an ``Idempotency-Key`` header (task #2516): a create
#: repeated after a lost answer must not make a second plan.
IDEMPOTENT_PLAN_ACTIONS = frozenset({"create"})


def _plan_handler(action: str):
    """A CSRF-guarded POST handler for one plan_manage action (``PLAN_ACTIONS``)."""
    fields, identity_arg = PLAN_ACTIONS[action]
    keyed = action in IDEMPOTENT_PLAN_ACTIONS
    identity_args = (identity_arg,) if identity_arg else ()

    async def handler(request: Request) -> Response:
        guard = routes._guard_mutation(request)
        if guard is not None:
            return guard
        identity = identity_or_401(request)
        if isinstance(identity, Response):
            return identity
        key = _idempotency_key(request) if keyed else None
        if isinstance(key, Response):
            return key
        body = await read_body(request)
        if isinstance(body, Response):
            return body
        refused = spoofed(body, identity, identity_args)
        if refused is not None:
            return refused
        params = bind(body, fields, extra=frozenset(identity_args))
        if isinstance(params, str):
            return routes._json({"error": params}, status=400)
        params.update({arg: identity for arg in identity_args})
        if key is not None:
            response = await call_once(identity, key, "plan_manage", action=action, params=params)
        else:
            response = await call("plan_manage", action=action, params=params)
        if action == "delete" and response.status_code == 200:
            return _not_deleted("plan", json.loads(bytes(response.body))) or response
        return response

    handler.__name__ = f"cc_plan_{action}"
    handler.identity_args = identity_args  # type: ignore[attr-defined]
    return handler


#: Path segment per action where it differs from the action name (task #2480).
_PLAN_PATHS = {
    "step_add": "step-add",
    "step_edit": "step-edit",
    "step_remove": "step-remove",
    "step_reorder": "reorder",
}


def _plan_path(action: str) -> str:
    if action in _PLAN_PATHS:
        return "/api/action/plan/" + _PLAN_PATHS[action]
    if action.startswith("joint_"):
        return "/api/action/plan/joint/" + action.removeprefix("joint_")
    return "/api/action/plan/" + action


async def plan_joint(request: Request) -> Response:
    """GET /api/plans/{plan_id}/joint — the joint contract through ``joint_get``.

    The tool refuses a caller outside the plan's office. ``caller`` repeats the
    caller's entry from ``team`` (``role`` is ``lead`` or ``member``); whether a
    given write is allowed is decided by the tool when it is made.
    """
    guard = routes._guard(request)
    if guard is not None:
        return guard
    identity = identity_or_401(request)
    if isinstance(identity, Response):
        return identity
    if request.query_params:
        return routes._json({"error": "this route takes no query parameters"}, status=400)
    plan_id = request.path_params["plan_id"]
    if len(plan_id) > 128:
        return routes._json({"error": "plan_id is longer than 128 characters"}, status=400)
    response = await call(
        "plan_manage", action="joint_get", params={"plan_id": plan_id, "author": identity}
    )
    if response.status_code != 200:
        return response
    view = json.loads(bytes(response.body))
    team = view.get("team") if isinstance(view, dict) else None
    caller = next(
        (m for m in team or [] if isinstance(m, dict) and m.get("identity") == identity), None
    )
    return routes._json({**view, "caller": caller})


# --- goals (task #2480) -------------------------------------------------------------

_GOAL_ID = Field("goal_id", "int", required=True, limit=2**31 - 1)
_LINK_TASK_ID = Field("task_id", "int", required=True, limit=2**31 - 1)
_GOAL_EXPECTED = Field("expected_revision", "int", limit=2**62)
_REASON = Field("reason", limit=4_000)

#: One route per goal_manage action: its accepted fields. Every route fills
#: ``author`` from the verified identity.
GOAL_ACTIONS: dict[str, tuple[Field, ...]] = {
    "edit": (
        _GOAL_ID,
        Field("title", limit=200),
        Field("statement", limit=4_000),
        _GOAL_EXPECTED,
    ),
    "link": (_LINK_TASK_ID, _GOAL_ID),
    "unlink": (_LINK_TASK_ID, _GOAL_ID),
    "close": (
        _GOAL_ID,
        Field("status", limit=16, choices=("achieved", "abandoned")),
        _REASON,
        _GOAL_EXPECTED,
    ),
    "reopen": (_GOAL_ID, _REASON, _GOAL_EXPECTED),
}

_GOAL_NOT_FOUND = ("unknown goal", "unknown task")


def not_found_as_404(response: Response, prefixes: tuple[str, ...]) -> Response:
    """Turn a tool's 400 that names a missing object into 404 (closed prefix list)."""
    if response.status_code != 400:
        return response
    body = json.loads(bytes(response.body))
    error = str(body.get("error", "")).strip().lower() if isinstance(body, dict) else ""
    if "code" not in body and error.startswith(prefixes):
        return routes._json(body, status=404)
    return response


def _goal_handler(action: str):
    """A CSRF-guarded POST handler for one goal_manage action (``GOAL_ACTIONS``)."""
    fields = GOAL_ACTIONS[action]
    identity_args = ("author",)

    async def handler(request: Request) -> Response:
        guard = routes._guard_mutation(request)
        if guard is not None:
            return guard
        identity = identity_or_401(request)
        if isinstance(identity, Response):
            return identity
        body = await read_body(request)
        if isinstance(body, Response):
            return body
        refused = spoofed(body, identity, identity_args)
        if refused is not None:
            return refused
        params = bind(body, fields, extra=frozenset(identity_args))
        if isinstance(params, str):
            return routes._json({"error": params}, status=400)
        params["author"] = identity
        response = await call("goal_manage", action=action, params=params)
        return not_found_as_404(response, _GOAL_NOT_FOUND)

    handler.__name__ = f"cc_goal_{action}"
    handler.identity_args = identity_args  # type: ignore[attr-defined]
    return handler


# --- tasks --------------------------------------------------------------------------

_TASK_ID = Field("id", "int", required=True, limit=2**31 - 1)

task_claim = adapt(
    "task_claim",
    (
        _TASK_ID,
        Field("timeout_seconds", "int", limit=7 * 24 * 3600),
        Field("expected_project", limit=200),
        Field("expected_lane", limit=100),
    ),
    identity_args=("agent",),
)


task_delete = adapt("task_delete", (_TASK_ID,), after=lambda result: _not_deleted("task", result))


# --- runs and sessions --------------------------------------------------------------

session_end = adapt(
    "session_end",
    (
        Field("session_id", required=True, limit=128),
        Field("status", limit=16, choices=("completed", "failed", "abandoned")),
        Field("exit_status", limit=200),
        Field("summary", limit=8_000),
    ),
)


_OPERATOR_END_FIELDS = (
    Field("session_id", required=True, limit=128),
    Field("reason", required=True, limit=2_000),
    Field("status", limit=16, choices=("completed", "failed", "abandoned")),
)


async def session_operator_end(request: Request) -> Response:
    """POST /api/action/session/operator-end — the operator ends another identity's session.

    The verified human operator only (``routes._operator_recovery``; the scope is
    ``operator=True`` with no tools). The ``session.operator_end`` platform writer runs
    behind the governed write adapter: object profile, agent-access switch (with the
    operator's recovery exemption), policy gate and approval gate, then the writer
    re-checks the operator identity, ends the row and appends the evidence event.
    """
    guard = routes._guard_mutation(request)
    if guard is not None:
        return guard
    identity = identity_or_401(request)
    if isinstance(identity, Response):
        return identity
    if not routes._operator_recovery(request):
        return routes._json({"error": "this action is reserved for the human operator"}, status=403)
    body = await read_body(request)
    if isinstance(body, Response):
        return body
    refused = spoofed(body, identity, ("operator",))
    if refused is not None:
        return refused
    kwargs = bind(body, _OPERATOR_END_FIELDS, extra=frozenset({"operator"}))
    if isinstance(kwargs, str):
        return routes._json({"error": kwargs}, status=400)
    kwargs["operator"] = identity
    operation = "session.operator_end"
    try:
        result = await anyio.to_thread.run_sync(
            lambda: api._registered_platform_write(
                "session",
                "operator_end",
                actor=identity,
                operation=operation,
                production=True,
                human_recovery=True,
                **kwargs,
            )
        )
    except approvals_tool.ApprovalPendingError as exc:
        return pending(operation, exc)
    except (agent_access.AgentAccessDenied, ResourceError) as exc:
        return routes._json({"error": str(exc)[:300]}, status=403)
    except ToolError as exc:
        typed = routes._typed_error(exc)
        if isinstance(typed, approvals_tool.ApprovalPendingError):
            return pending(operation, typed)
        if typed is None and str(exc).startswith("no such session"):
            return routes._json({"error": str(exc)[:300]}, status=404)
        return routes._json_body_error(exc, status=400)
    return routes._json(result)


session_operator_end.identity_args = ("operator",)  # type: ignore[attr-defined]


async def task_subtree(request: Request) -> Response:
    """GET /api/tasks/{task_id}/subtree — every descendant, any depth and status (#2516).

    The board's tree reads it instead of a capped ``task_list`` page, so old done
    descendants are not omitted. Gated like ``why-chain``: ``task_list`` in the
    caller's profile.
    """
    from ..tools.tasks import SubtreeRefused
    from ..tools.tasks import task_subtree as read_subtree

    guard = routes._guard(request)
    if guard is not None:
        return guard
    denied = routes._require_task_list()
    if denied is not None:
        return denied
    try:
        task_id = int(request.path_params["task_id"])
    except (TypeError, ValueError):
        return routes._json({"error": "invalid task_id"}, status=400)
    try:
        payload = await anyio.to_thread.run_sync(lambda: read_subtree(task_id))
    except SubtreeRefused as exc:
        return routes._json({"error": str(exc)[:300], "code": exc.code}, status=422)
    except ToolError as exc:
        return routes._json({"error": str(exc)[:300]}, status=404)
    return routes._json(payload)


ROUTES: list[Route] = [
    Route("/api/tasks/{task_id}/subtree", task_subtree, methods=["GET"]),
    *(
        Route(_plan_path(action), _plan_handler(action), methods=["POST"])
        for action in PLAN_ACTIONS
    ),
    Route("/api/plans/{plan_id}/joint", plan_joint, methods=["GET"]),
    *(
        Route(f"/api/action/goal/{action}", _goal_handler(action), methods=["POST"])
        for action in GOAL_ACTIONS
    ),
    Route("/api/action/task/claim", task_claim, methods=["POST"]),
    Route("/api/action/task/delete", task_delete, methods=["POST"]),
    Route("/api/action/session/end", session_end, methods=["POST"]),
    Route("/api/action/session/operator-end", session_operator_end, methods=["POST"]),
]
