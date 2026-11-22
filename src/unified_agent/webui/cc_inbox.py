"""Control Center v2 Home and Inbox routes (tasks #2466, #2514).

Request and response shapes are in ``docs/control-center-api.md``.

* ``GET /api/work-proposals`` and ``GET /api/work-proposals/{proposal_id}`` read
  through the registered ``coordination_manage`` tool (``work_proposal_list``,
  ``work_proposal_get``), so its profile ceiling, policy gate and tenant fence
  apply. Deciding stays on ``POST /api/action/work-proposal-decide``.
* ``POST /api/sessions/{id}/wake-decline`` is the verified human operator's
  refusal of a session's wake request (``messaging.wake.set_decline``), the
  counterpart of the operator-only wake-grant route. There is no MCP twin: the
  session's request comes from the bearer the agent also holds.
* ``POST /api/action/review-hold`` records the operator's ``release`` or ``keep``
  of a red-tier or held pull-request hold (``review_holds``). A release of a
  red-tier hold is refused with ``red_tier_approval_required`` and the approval
  path, because releasing it would approve red-tier scope.

Approval completeness, pass state and "may I approve" are fields of the existing
``GET /api/approvals/{id}`` (``api.approval_detail_payload``); tasks completed in
a window are ``GET /api/tasks?completed_after=`` (``api.tasks_payload``).
"""

from __future__ import annotations

import anyio
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .. import review_holds, work_proposals
from ..messaging import wake as message_wake
from . import routes
from .cc_knowledge import Field, bind, bind_query, call, identity_or_401, read_body
from .cc_work import not_found_as_404

# --- work proposals -----------------------------------------------------------------

_PROPOSAL_LIST_FIELDS = (
    Field("status", limit=16, choices=work_proposals.STATUSES),
    Field("proposer", limit=200),
    Field("limit", "int", limit=work_proposals.MAX_LIST),
)
_PROPOSAL_NOT_FOUND = ("unknown work proposal",)


async def work_proposal_list(request: Request) -> Response:
    """GET /api/work-proposals — newest first, with per-status item counts."""
    guard = routes._guard(request)
    if guard is not None:
        return guard
    identity = identity_or_401(request)
    if isinstance(identity, Response):
        return identity
    kwargs = bind_query(request, _PROPOSAL_LIST_FIELDS)
    if isinstance(kwargs, str):
        return routes._json({"error": kwargs}, status=400)
    if kwargs.get("limit") == 0:
        return routes._json({"error": "limit must be at least 1"}, status=400)
    return await call("coordination_manage", action="work_proposal_list", params=kwargs)


async def work_proposal_detail(request: Request) -> Response:
    """GET /api/work-proposals/{proposal_id} — one proposal with its items."""
    guard = routes._guard(request)
    if guard is not None:
        return guard
    identity = identity_or_401(request)
    if isinstance(identity, Response):
        return identity
    if request.query_params:
        return routes._json({"error": "this route takes no query parameters"}, status=400)
    proposal_id = request.path_params["proposal_id"]
    if len(proposal_id) > 64:
        return routes._json({"error": "proposal_id is longer than 64 characters"}, status=400)
    response = await call(
        "coordination_manage", action="work_proposal_get", params={"proposal_id": proposal_id}
    )
    return not_found_as_404(response, _PROPOSAL_NOT_FOUND)


# --- wake decline -------------------------------------------------------------------

_DECLINE_FIELDS = (Field("reason", limit=message_wake.MAX_DECLINE_REASON),)


async def session_wake_decline(request: Request) -> Response:
    """POST /api/sessions/{id}/wake-decline — the operator refuses a wake request.

    ``{reason?}``. Records who, when and why; revokes any grant; the session asking
    again does not clear it, only a later grant does.
    """
    guard = routes._guard_mutation(request)
    if guard is not None:
        return guard
    identity = identity_or_401(request)
    if isinstance(identity, Response):
        return identity
    if not routes._operator_recovery(request):
        return routes._json({"error": "declining idle wakes is an operator action"}, status=403)
    body = await read_body(request)
    if isinstance(body, Response):
        return body
    kwargs = bind(body, _DECLINE_FIELDS)
    if isinstance(kwargs, str):
        return routes._json({"error": kwargs}, status=400)
    session_id = request.path_params["id"]
    result = await anyio.to_thread.run_sync(
        lambda: message_wake.set_decline(session_id, actor=identity, reason=kwargs.get("reason"))
    )
    if result is None:
        return routes._json({"error": "unknown session"}, status=404)
    return routes._json(result)


# --- review holds -------------------------------------------------------------------

_HOLD_FIELDS = (
    Field("task_id", "int", required=True, limit=2**31 - 1),
    Field("decision", required=True, limit=16, choices=review_holds.DECISIONS),
    Field("reason", limit=review_holds.MAX_REASON_CHARS),
)


async def review_hold_decide(request: Request) -> Response:
    """POST /api/action/review-hold — the operator releases or keeps a review hold.

    ``{task_id, decision: release|keep, reason?}``. 409 ``red_tier_approval_required``
    for a release of a red-tier hold (approve the pull request's Ledger request
    instead); 409 ``no_hold`` when the task is not held; 404 for an unknown task.
    """
    guard = routes._guard_mutation(request)
    if guard is not None:
        return guard
    identity = identity_or_401(request)
    if isinstance(identity, Response):
        return identity
    if not routes._operator_recovery(request):
        return routes._json({"error": "deciding a review hold is an operator action"}, status=403)
    body = await read_body(request)
    if isinstance(body, Response):
        return body
    kwargs = bind(body, _HOLD_FIELDS)
    if isinstance(kwargs, str):
        return routes._json({"error": kwargs}, status=400)
    try:
        result = await anyio.to_thread.run_sync(
            lambda: review_holds.record(
                kwargs["task_id"],
                kwargs["decision"],
                actor=identity,
                reason=kwargs.get("reason"),
            )
        )
    except review_holds.HoldError as exc:
        payload = {"error": str(exc)[:500]}
        if exc.code:
            payload["code"] = exc.code
        return routes._json(payload, status=exc.status)
    return routes._json(result)


ROUTES: list[Route] = [
    Route("/api/work-proposals", work_proposal_list, methods=["GET"]),
    Route("/api/work-proposals/{proposal_id}", work_proposal_detail, methods=["GET"]),
    Route("/api/sessions/{id}/wake-decline", session_wake_decline, methods=["POST"]),
    Route("/api/action/review-hold", review_hold_decide, methods=["POST"]),
]
