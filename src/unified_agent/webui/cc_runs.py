"""Control Center v2 runs routes: routines and LangGraph runs (tasks #2466, #2480).

Thin adapters over registered hub tools, built with the shared helpers in
``cc_knowledge`` so every call goes through the tool's guarded wrapper (profile
ceiling, policy gate, approval gate, rate limit). Request and response shapes
are in ``docs/control-center-api.md``.

* Routines adapt ``routine_list``, ``routine_add``, ``routine_pause`` and
  ``routine_fire``. The bearer-only webhook route ``POST
  /api/routines/{routine_id}/fire`` is unchanged; "Run now" is a separate,
  CSRF-guarded route. Only the verified human operator's Run now may fire a
  scheduled (cron) routine, through ``routines.operator_run_now``; anyone else
  keeps ``routine_fire``'s existing rule.
* ``POST /api/action/langgraph/run`` runs ``langgraph_workflow_run``. The tool
  refuses a call without a session id, and a browser request has none, so the
  handler registers a real ``agent_sessions`` row for the verified caller, binds
  it for the one call (``profiles.http_route_invocation_session``) and ends it
  afterwards. ``engine: "research"`` continues a paused ``langgraph_run`` research
  run by ``run_id`` (task #2522); starting a research run stays an MCP call.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

import anyio
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from .. import events, profiles, registry, routines, storage
from . import routes
from .cc_knowledge import (
    Field,
    _idempotency_key,
    adapt,
    bind,
    bind_query,
    call,
    identity_or_401,
    read_body,
    respond_once,
)
from .cc_work import not_found_as_404

logger = logging.getLogger(__name__)

_ROUTINE_NOT_FOUND = ("unknown routine",)
_ROUTINE_ID = Field("routine_id", "int", required=True, limit=2**31 - 1)

# --- routines -----------------------------------------------------------------------

_LIST_FIELDS = (
    Field("include_paused", "bool"),
    Field("recent_runs", "int", limit=20),
)


def _next_run_at(routine: dict[str, Any]) -> str | None:
    """The scheduler's next slot for an unpaused cron routine, else None.

    The scheduler fires the first slot after ``cursor_at``; a value in the past
    means the routine is due and fires on the next tick (subject to ``catch_up``).
    """
    if routine.get("trigger") != "cron" or routine.get("paused") or not routine.get("schedule"):
        return None
    try:
        after = datetime.fromisoformat(str(routine["cursor_at"]))
        if after.tzinfo is None:
            after = after.replace(tzinfo=UTC)
        return routines.cron_next(str(routine["schedule"]), after).isoformat()
    except Exception:  # noqa: BLE001 - a malformed row reads as "no next run", never a 500
        return None


async def routine_list(request: Request) -> Response:
    """GET /api/routines — every routine with ``next_run_at`` and its recent runs."""
    guard = routes._guard(request)
    if guard is not None:
        return guard
    kwargs = bind_query(request, _LIST_FIELDS)
    if isinstance(kwargs, str):
        return routes._json({"error": kwargs}, status=400)
    kwargs.setdefault("include_paused", True)
    kwargs.setdefault("recent_runs", 5)
    response = await call("routine_list", **kwargs)
    if response.status_code != 200:
        return response
    listed = json.loads(bytes(response.body))
    for routine in listed.get("routines", []):
        routine["next_run_at"] = _next_run_at(routine)
    listed["routines_enabled"] = os.environ.get("UA_ROUTINES_ENABLED") == "1"
    return routes._json(listed)


def _with_404(handler):
    async def wrapped(request: Request) -> Response:
        return not_found_as_404(await handler(request), _ROUTINE_NOT_FOUND)

    wrapped.__name__ = handler.__name__
    wrapped.identity_args = handler.identity_args  # type: ignore[attr-defined]
    return wrapped


routine_add = adapt(
    "routine_add",
    (
        Field("title", required=True, limit=200),
        Field("trigger", limit=16, choices=("cron", "api", "webhook")),
        Field("schedule", limit=100),
        Field("notes", limit=8_000),
        Field("lane", limit=100),
        Field("project", limit=100),
        Field("domain", limit=100),
        Field("concurrency", limit=16, choices=("skip", "queue")),
        Field("catch_up", limit=16, choices=("none", "last", "all")),
        Field("start_at", limit=64),
        Field("activity_gate", limit=32, choices=routines.ACTIVITY_GATES),
        Field("activity_gate_scope", limit=16, choices=routines.ACTIVITY_GATE_SCOPES),
    ),
    idempotent=True,
)
routine_pause = _with_404(adapt("routine_pause", (_ROUTINE_ID,), fixed={"paused": True}))
routine_resume = _with_404(adapt("routine_pause", (_ROUTINE_ID,), fixed={"paused": False}))

#: ToolError messages of routine_fire and the status each answers.
_RUN_STATUS = {
    "unknown routine": 404,
    "routine is paused": 409,
    "cron routines are fired by the scheduler": 409,
}


async def routine_run(request: Request) -> Response:
    """POST /api/action/routine/run — Run now, with the ``Idempotency-Key`` header.

    The key, scoped to tenant and identity and bound to the request, names the fire:
    repeating it returns the stored first answer and creates nothing; a duplicate
    while the first is still running answers 409 ``in_flight``. The verified human operator may run a scheduled routine;
    everyone else fires api and webhook routines only, as ``routine_fire`` does.
    """
    guard = routes._guard_mutation(request)
    if guard is not None:
        return guard
    identity = identity_or_401(request)
    if isinstance(identity, Response):
        return identity
    key = request.headers.get("idempotency-key", "")
    if (
        not key
        or len(key) > 200
        or key != key.strip()
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in key)
    ):
        return routes._json(
            {
                "error": "Idempotency-Key must be 1-200 characters without surrounding "
                "whitespace or control characters"
            },
            status=400,
        )
    body = await read_body(request)
    if isinstance(body, Response):
        return body
    kwargs = bind(body, (_ROUTINE_ID,))
    if isinstance(kwargs, str):
        return routes._json({"error": kwargs}, status=400)
    digest = routines.run_now_digest(kwargs)
    try:
        owner, stored = await anyio.to_thread.run_sync(
            lambda: routines.run_now_begin(identity, key, digest)
        )
    except routines.RunNowInFlight as exc:
        return routes._json({"error": str(exc), "code": "in_flight"}, status=409)
    except storage.AmbiguousIdempotencyReceipt as exc:
        return routes._json({"error": str(exc), "code": "ambiguous_receipt"}, status=409)
    except ToolError as exc:  # ConflictError: the key belongs to another request
        return routes._json_body_error(exc, status=409)
    if stored is not None:
        return routes._json(stored["body"], status=stored["status_code"])
    fire_key = routines.run_now_fire_key(identity, key)
    response: Response | None = None
    try:
        with routines.run_now_namespace():
            if routes._operator_recovery(request):
                with routines.operator_run_now():
                    response = await call("routine_fire", idempotency_key=fire_key, **kwargs)
            else:
                response = await call("routine_fire", idempotency_key=fire_key, **kwargs)
        if response.status_code in (400, 404):
            error = json.loads(bytes(response.body)).get("error", "")
            status = _RUN_STATUS.get(error)
            if status is None and str(error).startswith("routines are disabled"):
                status = 503
            if status is not None:
                response = routes._json({"error": error}, status=status)
    finally:
        first = None
        if response is not None and response.status_code == 200:
            first = {"status_code": 200, "body": json.loads(bytes(response.body))}
        with anyio.CancelScope(shield=True):
            still_owner, canonical = await anyio.to_thread.run_sync(
                lambda: routines.run_now_finish(identity, key, digest, owner, first)
            )
    if canonical is not None:
        return routes._json(canonical["body"], status=canonical["status_code"])
    if not still_owner:
        # Superseded: another request took this reservation over and has not
        # settled it yet. Never answer with this request's own, unfrozen result.
        return routes._json(
            {
                "error": "this Run now request was taken over by a retry that has not "
                "finished; repeat it shortly to read its answer",
                "code": "in_flight",
            },
            status=409,
        )
    return response


routine_run.identity_args = ()  # type: ignore[attr-defined]


# --- LangGraph ----------------------------------------------------------------------

_LANGGRAPH_FIELDS = (
    Field("task_id", "int", required=True, limit=2**31 - 1),
    Field("query", required=True, limit=4_000),
    Field("run_id", limit=26),
    Field("engine", limit=16, choices=("workflow", "research")),
)
#: The research engine only continues a paused run, so ``run_id`` is required and
#: ``task_id`` and ``query`` are optional checks against the checkpointed run.
_RESEARCH_FIELDS = (
    Field("task_id", "int", limit=2**31 - 1),
    Field("query", limit=4_000),
    Field("run_id", required=True, limit=26),
    Field("engine", required=True, limit=16, choices=("research",)),
)
#: HTTP status of a research resume the tool refused before executing (task #2522).
_RESEARCH_REFUSALS = {
    "unauthenticated": 403,
    "not_run_owner": 403,
    "task_not_accessible": 403,
    "stale_authority": 403,
    "checkpoint_missing": 409,
    "checkpoint_corrupt": 409,
    "checkpoint_incompatible": 409,
    "resume_in_flight": 409,
    "lease_lost": 409,
}
INVOCATION_SESSION_KIND = "control_center"


#: Bound on closing the invocation session after the call, cancellation or not.
SESSION_CLOSE_TIMEOUT_S = 10.0


async def run_in_invocation_session(
    identity: str, kwargs: dict[str, Any], tool: str = "langgraph_workflow_run"
) -> tuple[str, Response] | Response:
    """Run a LangGraph tool (``tool``) under a registered session of the caller.

    Registers the session, binds it for the one call and always ends it: the close
    runs in a shielded, time-bounded scope, so a cancelled request (client gone,
    server shutdown) still closes the row, and a failed close is logged and
    recorded as an ``control_center.invocation_session_unclosed`` event rather than
    ignored (review #1516).
    """
    registered = await anyio.to_thread.run_sync(
        lambda: registry.register_session(
            identity,
            INVOCATION_SESSION_KIND,
            task_id=kwargs.get("task_id"),
            external_session_id=f"cc-langgraph:{events.new_ulid()}",
            meta={"route": "/api/action/langgraph/run", "run_id": kwargs.get("run_id")},
        )
    )
    if registered is None:
        return routes._json({"error": "could not register an invocation session"}, status=503)
    session_id = registered[0]
    response: Response | None = None
    try:
        with profiles.http_route_invocation_session(session_id):
            response = await call(tool, **kwargs)
    finally:
        ok = response is not None and response.status_code in (200, 202)
        await _close_invocation_session(
            session_id,
            identity,
            status="completed" if ok else "failed",
            summary=f"{tool} task {kwargs.get('task_id')}"
            + ("" if response is not None else " (request cancelled or failed)"),
        )
    return session_id, response


def _close_and_record(session_id: str, identity: str, status: str, summary: str) -> bool:
    """End the session and record a failed close, exactly once (runs in a worker)."""
    try:
        closed = registry.end_session(session_id, status=status, summary=summary)
    except Exception:  # noqa: BLE001 - reported below; never masks the call's outcome
        closed = False
    if not closed:
        logger.warning("control center: invocation session %s did not close", session_id)
        try:
            events.record_event(
                "control_center.invocation_session_unclosed",
                payload={"session_id": session_id, "status": status},
                originator=identity,
                notify=False,
            )
        except Exception:  # noqa: BLE001 - the warning above is the floor
            logger.warning("control center: could not record unclosed session %s", session_id)
    return closed


async def _close_invocation_session(
    session_id: str, identity: str, *, status: str, summary: str
) -> bool | None:
    """Close the invocation session without holding the response past the bound.

    The close runs in its own thread that ends the row and records a failed close
    itself (``_close_and_record``), so its outcome is recorded once whether or not
    this coroutine is still waiting. Only the wait is shielded from the request's
    cancellation and bounded by ``SESSION_CLOSE_TIMEOUT_S``; at the bound this stops
    waiting and returns None while the thread still closes the row (#2503).
    Otherwise it returns whether the row closed.
    """
    # The close gets its own thread, so it runs even when the wait below is abandoned
    # before a worker picks it up; only the wait is bounded.
    done = threading.Event()
    outcome: dict[str, bool] = {}

    def close() -> None:
        try:
            outcome["closed"] = _close_and_record(session_id, identity, status, summary)
        finally:
            done.set()

    threading.Thread(target=close, name=f"cc-session-close-{session_id}", daemon=True).start()
    with anyio.CancelScope(shield=True), anyio.move_on_after(SESSION_CLOSE_TIMEOUT_S):
        await anyio.to_thread.run_sync(done.wait, abandon_on_cancel=True)
        return outcome.get("closed", False)
    logger.info("control center: invocation session %s is closing in the background", session_id)
    return None


def _graph_answer_frozen(response: Response) -> bool:
    """A keyed graph start freezes every 2xx that started a run.

    A native approval (202 with a ``run_id``) started the run, so it is the answer
    a repeat gets back; the wrapper gate's 202 has no ``run_id``: nothing ran.
    """
    if not 200 <= response.status_code < 300:
        return False
    if response.status_code != 202:
        return True
    return "run_id" in json.loads(bytes(response.body))


async def langgraph_run(request: Request) -> Response:
    """POST /api/action/langgraph/run — the governed workflow, or a research resume.

    A native approval interruption answers 202 with ``code: "approval_required"``;
    the caller continues with the returned ``run_id`` after deciding it, never by
    repeating the first request (which would start a new run). An optional
    ``Idempotency-Key`` makes the start run at most once (``respond_once``);
    ``engine: "research"`` continues a paused research run by ``run_id``, and
    two concurrent continuations of one run never both execute
    (``langgraph_resume``).
    """
    guard = routes._guard_mutation(request)
    if guard is not None:
        return guard
    identity = identity_or_401(request)
    if isinstance(identity, Response):
        return identity
    key = _idempotency_key(request)
    if isinstance(key, Response):
        return key
    body = await read_body(request)
    if isinstance(body, Response):
        return body
    if body.get("engine") == "research":
        return await _research_resume(identity, body)
    kwargs = bind(body, _LANGGRAPH_FIELDS)
    if isinstance(kwargs, str):
        return routes._json({"error": kwargs}, status=400)
    kwargs.pop("engine", None)

    async def start() -> Response:
        outcome = await run_in_invocation_session(identity, kwargs)
        if isinstance(outcome, Response):
            return outcome
        session_id, response = outcome
        if response.status_code != 200:
            return response
        result = json.loads(bytes(response.body))
        if isinstance(result, dict) and result.get("status") == "interrupted":
            interrupted = result.get("interrupted_by") or {}
            return routes._json(
                {
                    **result,
                    "status": "approval_pending",
                    "run_status": "interrupted",
                    "code": "approval_required",
                    "approval_id": interrupted.get("approval_id"),
                    "tool": "langgraph_workflow_run",
                    "session_id": session_id,
                },
                status=202,
            )
        return routes._json({**result, "session_id": session_id})

    if key is None:
        return await start()
    return await respond_once(
        identity,
        key,
        "langgraph_workflow_run",
        kwargs,
        start,
        freeze=_graph_answer_frozen,
        check=f"task {kwargs['task_id']}'s notes and plans",
    )


langgraph_run.identity_args = ()  # type: ignore[attr-defined]


_RUN_ID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


def _graph_progress(run_id: str, identity: str, operator: bool) -> dict[str, Any] | None:
    """The run's progress for its own caller or the verified operator, else None.

    The run belongs to the identity that started it (``claimed_by`` in its
    origin authority, the one identity the tool lets drive it). Anyone else, in
    the tenant or not, reads it exactly as a missing run (review #1538).
    """
    from ..langgraph_workflow import run_progress
    from ..tools.tasks import _visible_task

    progress = run_progress(run_id)
    if progress is None:
        return None
    if not operator and progress.get("claimed_by") != identity:
        return None
    with closing(storage.connect()) as conn:
        if _visible_task(conn, int(progress["task_id"])) is None:
            return None
    return progress


async def langgraph_run_progress(request: Request) -> Response:
    """GET /api/langgraph/runs/{run_id} — a governed graph run's progress (task #2516).

    Read from the run's LangGraph checkpoints and its bound plan, so the canvas
    shows where a run is (each hop, the approval it waits on, the last
    checkpoint) whatever the last POST answered. Read-only: nothing runs. Only
    the run's own caller and the verified human operator may read it; for anyone
    else it is 404, the same answer as no run.
    """
    guard = routes._guard(request)
    if guard is not None:
        return guard
    identity = identity_or_401(request)
    if isinstance(identity, Response):
        return identity
    run_id = request.path_params["run_id"]
    if not _RUN_ID_RE.fullmatch(run_id):
        return routes._json({"error": "run_id must be a 26-character ULID"}, status=400)
    operator = routes._operator_recovery(request)
    try:
        progress = await anyio.to_thread.run_sync(
            lambda: _graph_progress(run_id, identity, operator)
        )
    except ImportError:
        return routes._json({"error": "the LangGraph extra is not installed"}, status=503)
    if progress is None:
        return routes._json({"error": "no governed graph run with this id"}, status=404)
    return routes._json(progress)


async def _research_resume(identity: str, body: dict[str, Any]) -> Response:
    """Continue a paused ``langgraph_run`` research run (task #2522).

    The tool rebuilds the run from its validated checkpoint and checks, again, that
    the caller owns it or is the verified operator, that the task, claim, profile
    ceiling and budget still hold, and the policy and approval gates. A refusal
    before execution answers 403 (authority) or 409 (checkpoint, or a resume
    already in flight) with the typed result; graph outcomes answer 200.
    """
    kwargs = bind(body, _RESEARCH_FIELDS)
    if isinstance(kwargs, str):
        return routes._json({"error": kwargs}, status=400)
    kwargs.pop("engine")
    kwargs["resume"] = True
    outcome = await run_in_invocation_session(identity, kwargs, tool="langgraph_run")
    if isinstance(outcome, Response):
        return outcome
    session_id, response = outcome
    if response.status_code != 200:
        return response
    result = json.loads(bytes(response.body))
    if not isinstance(result, dict):
        return response
    if result.get("outcome") == "approval_interrupted":
        interrupted = result.get("interrupted_by") or {}
        return routes._json(
            {
                **result,
                "status": "approval_pending",
                "run_status": "interrupted",
                "code": "approval_required",
                "approval_id": interrupted.get("approval_id"),
                "tool": "langgraph_run",
                "session_id": session_id,
            },
            status=202,
        )
    status = _RESEARCH_REFUSALS.get(str(result.get("code")), 200)
    return routes._json({**result, "session_id": session_id}, status=status)


ROUTES: list[Route] = [
    Route("/api/langgraph/runs/{run_id}", langgraph_run_progress, methods=["GET"]),
    Route("/api/routines", routine_list, methods=["GET"]),
    Route("/api/action/routine/add", routine_add, methods=["POST"]),
    Route("/api/action/routine/pause", routine_pause, methods=["POST"]),
    Route("/api/action/routine/resume", routine_resume, methods=["POST"]),
    Route("/api/action/routine/run", routine_run, methods=["POST"]),
    Route("/api/action/langgraph/run", langgraph_run, methods=["POST"]),
]
