"""Route table for the web control center.

Mounted onto the hub's Starlette app *before* `HubAuthASGIMiddleware` wraps it (see
`server.wrapped_http_app`), so `/ui` and `/api` inherit exactly the same identity plane
as MCP tool calls rather than growing a second one.

Read routes remain GET-only, while explicitly registered action routes handle the
bounded Command Center mutations. `tests/test_webui_routes.py` asserts both surfaces,
so a new mutation must be deliberate and discoverable.

Every route must also declare who may call it in ``route_scopes.ROUTE_SCOPES``;
``web_routes()`` enforces that table before the handler runs and answers 403 for a
route with no entry (task #1745). The handlers keep their own checks.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import hashlib
import ipaddress
import json
import logging
import math
import sqlite3
import threading
import time
import weakref
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

import anyio
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from .. import (
    agent_access,
    config,
    connector_oauth,
    edition,
    events,
    harness_config,
    llm_broker,
    llm_client,
    node_queue,  # also registers agent_lifecycle's 'node' stop handler (#1723)
    oauth,  # noqa: F401 - registers identity_core's provider lookup for connector_oauth.begin
    object_model,
    platform_http,
    policy,
    profiles,
    registry,
    session_capture,
    slack_gateway,
    storage,
)
from ..audit import evidence_signing

# Imported for its side effect (task #1723): dispatch.coordinator registers
# agent_lifecycle's 'dispatch' stop handler, and node_queue (above) its 'node'
# one. server.py imports this package, so every hub process that can pause or
# terminate an agent (coordination_manage over MCP, /api/agent-lifecycle) has
# both; a process without them refuses the change instead of skipping a cancel.
from ..dispatch import coordinator as _dispatch_coordinator  # noqa: F401
from ..messaging import index as message_index
from ..messaging import wake as message_wake
from ..tool_errors import (
    ConflictError,
    OutcomeUnknownError,
    PermissionDeniedError,
    TypedToolError,
)
from ..tools import approvals as approvals_tool
from ..tools import graph_canvas as graph_canvas_tool
from . import api, auth, route_scopes, sse, workos_signin
from . import spa as web_spa
from .contracts import API_VERSION, parse_dispatch_request, parse_dispatch_stack_request

log = logging.getLogger(__name__)

# Retired in task #993: the legacy vanilla shell (webui/static/) and the vendored
# Excalidraw bundle it lazy-loaded are deleted along with the /ui/legacy mount.
# /ui is a single mount over packages/web/dist; a missing dist is a hard 503.


def _is_parked_approval(payload: object) -> bool:
    """A 202 body that reports a call parked behind a human approval (task #2056).

    It names an approval (``approval_id``, ``global_approval_id``), says the
    call is ``pending``, or carries an ``autonomy_review`` pause. A bare
    ``status: "pending"`` is not enough: a dispatch whose agent has not started
    yet answers that as accepted work, not as an approval.
    """
    if not isinstance(payload, dict):
        return False
    if "approval_id" in payload or "global_approval_id" in payload:
        return True
    if payload.get("global_approval_status") == "pending" or payload.get("pending") is True:
        return True
    return isinstance(payload.get("autonomy_review"), dict)


def _json(
    payload,
    status: int = 200,
    headers: dict[str, str] | None = None,
    *,
    approval_code: bool = True,
) -> JSONResponse:
    # Chokepoint for the typed approval code: every parked-approval 202 names
    # ``approval_required``, including pause dicts a writer returns rather than
    # raises (team boot's autonomy review), so no route can drop it. Only
    # ``/api/approvals/request`` opts out (``approval_code=False``): its 202 is
    # the successful ApprovalRequestResult of an explicit request, not a refusal.
    if approval_code and status == 202 and _is_parked_approval(payload) and "code" not in payload:
        payload = {**payload, "code": approvals_tool.ApprovalPendingError.code}
    return JSONResponse(payload, status_code=status, headers=headers)


def _guard(request: Request) -> Response | None:
    """Shared precondition for every /ui and /api route.

    Returns an error response to short-circuit with, or None to proceed.

    The origin check lives here because `/ui` and `/api` sit *outside* MCP's
    TransportSecurityMiddleware (which only wraps the MCP app), so they would
    otherwise have no cross-origin protection at all. The Host check closes the
    same gap for DNS rebinding. It runs even when ``enforce_auth()`` is off —
    warn mode must not skip it. ``/healthz`` does not call ``_guard`` and stays
    auth-exempt.
    """
    if not auth.origin_allowed(request.headers.get("origin")):
        return _json({"error": "origin not allowed"}, status=403)
    if not auth.host_allowed(request.headers.get("host")):
        return _json({"error": "host not allowed"}, status=403)

    if not config.enforce_auth():
        # Warn mode: the hub itself lets unauthenticated calls through, so the UI
        # must not be stricter than the tool surface it is a window onto.
        # The Host check above already ran; this return does not skip it.
        return None

    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authentication required"}, status=401)
    return None


# The probes are declared here for the full-surface route list, and again in
# ``unified_agent.health`` for a hub that does not load this component (K4b,
# task #2603). Control Center may not import the composition root, so the two
# copies exist; keep them in step, and let the readiness logic stay in
# ``ready.readiness`` rather than here.
async def healthz(request: Request) -> Response:
    """Liveness probe. Deliberately auth-exempt and deliberately content-free.

    It reveals only that a process is listening — no version, no agent names, nothing
    that would help someone who should not be here.

    An unknown ``UA_TOOLSETS`` is the exception: the probe returns ``{"ok": false}``
    with no toolset name, so a hub that reached this route still fails the check.
    """
    from ..toolsets import ToolsetConfigError, require_known_toolsets

    try:
        require_known_toolsets()
    except ToolsetConfigError:
        return _json({"ok": False}, status=503)
    return _json({"ok": True})


async def readyz(request: Request) -> Response:
    """Readiness probe. Auth-exempt. 503 when the live store cannot be written.

    Reports store writability and WAL sanity, plus an advisory WorkOS TCP
    result that never fails the probe. No org id, token, or path.
    """
    from .. import ready

    body = await anyio.to_thread.run_sync(ready.readiness)
    return _json(body, status=200 if body["ok"] else 503)


async def ui_index(request: Request) -> Response:
    """Modern Vite SPA at /ui. 503 with a build hint when packages/web/dist is absent."""
    guard = _guard(request)
    if guard is not None and guard.status_code == 403:
        return guard
    return web_spa.serve_modern("")


async def ui_modern_asset(request: Request) -> Response:
    """Serve Vite dist files under /ui/… with SPA fallback for extensionless paths."""
    guard = _guard(request)
    if guard is not None and guard.status_code == 403:
        return guard
    rel = request.path_params.get("path", "")
    # Retired mounts (#993): the legacy vanilla shell and the vendored Excalidraw
    # bundle are gone — these prefixes must 404, not hit the SPA fallback.
    # Traversal segments 404 the same way rather than rendering the SPA index.
    if rel == "legacy" or rel.startswith(("legacy/", "excalidraw/")) or ".." in rel.split("/"):
        return PlainTextResponse("not found", status_code=404)
    return web_spa.serve_modern(rel)


async def session_create(request: Request) -> Response:
    """Trade a bearer token for an HttpOnly cookie session."""
    if not auth.origin_allowed(request.headers.get("origin")):
        return _json({"error": "origin not allowed"}, status=403)
    if not auth.host_allowed(request.headers.get("host")):
        return _json({"error": "host not allowed"}, status=403)
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        return _json({"error": "invalid JSON"}, status=400)

    token = body.get("token") if isinstance(body, dict) else None
    label = auth.verify_token(token)
    if not label:
        detail = (
            "no tokens configured on this hub" if not config.agent_tokens() else "invalid token"
        )
        return _json({"error": detail}, status=401)

    if profiles.cloud_hosted_identity_default(label):
        return _json({"error": "cloud-assistant cannot create a browser session"}, status=403)

    # A retired agent's bearer (still in UA_AGENT_TOKENS) mints no browser
    # session (review of #2315); the operator's own label is a human sign-in.
    label = config.canonical_identity(label)
    if not config.is_human_identity(label):
        from starlette.concurrency import run_in_threadpool

        from .. import agent_lifecycle

        try:
            await run_in_threadpool(
                agent_lifecycle.refuse_retired_identity, label, site="webui.session_create"
            )
        except agent_lifecycle.AgentRetiredError as exc:
            return _json({"error": str(exc), "code": exc.reason}, status=403)
        except agent_lifecycle.LifecycleUnavailableError as exc:
            return _json({"error": str(exc), "code": exc.reason}, status=503)

    session_id, csrf = auth.create_session(label, request.headers.get("user-agent"))
    # The trusted scheme, not the raw header: a forwarded proto only reaches
    # scope["scheme"] through the listener-scoped proxy middleware (task #1892).
    secure = request.url.scheme == "https"
    response = _json({"agent": label, "csrf": csrf})
    response.headers["set-cookie"] = auth.cookie_header_value(session_id, secure)
    return response


async def session_delete(request: Request) -> Response:
    cookie = request.cookies.get(auth.COOKIE_NAME)
    # The CSRF token must belong to the session this cookie names, before
    # anything is revoked or cleared. A sign-out started under one session
    # can arrive carrying another's cookie (the browser signed in as someone
    # else between the client's CSRF read and its DELETE); without this the
    # stale request would end the other person's session.
    # Read-only, and a failed read is not "no live session": answering as if
    # the session were absent would skip the CSRF check (review round 10).
    try:
        session = auth.find_live_session(cookie)
    except auth.SessionLookupError:
        return _json(
            {
                "error": (
                    "The session could not be checked, so nothing was signed out. "
                    "Try again in a moment."
                ),
                "code": "session_lookup_failed",
            },
            status=503,
        )
    if session is not None and not auth.csrf_ok(
        session["csrf"], request.headers.get(auth.CSRF_HEADER)
    ):
        return _json(
            {
                "error": (
                    "The CSRF token does not belong to the session this cookie names, "
                    "so nothing was signed out. Read /api/whoami and try again."
                ),
                "code": "csrf_session_mismatch",
            },
            status=403,
        )
    revoked = auth.revoke_session(cookie)
    # A WorkOS-established session (task #2114) also drops its stored sealed session.
    # Keep the WorkOS authentication marker if revocation failed: otherwise a
    # copied cookie could lose the membership check while it is still live.
    if revoked:
        workos_signin.forget(cookie)
    response = _json({"ok": True})
    response.headers["set-cookie"] = auth.clear_cookie_value()
    return response


async def slack_events(request: Request) -> Response:
    """Signed Slack ingress; Slack HMAC replaces hub bearer auth for this route."""
    try:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > slack_gateway.MAX_BODY_BYTES:
                    raise slack_gateway.SlackGatewayError("Slack request body exceeds limit")
            except slack_gateway.SlackGatewayError:
                raise
            except ValueError as exc:
                raise slack_gateway.SlackGatewayError("invalid Slack content length") from exc
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > slack_gateway.MAX_BODY_BYTES:
                raise slack_gateway.SlackGatewayError("Slack request body exceeds limit")
            chunks.append(chunk)
        body = b"".join(chunks)
        result = await anyio.to_thread.run_sync(
            lambda: slack_gateway.handle_http_request(body, headers=request.headers)
        )
    except slack_gateway.SlackGatewayOverloadError as exc:
        return _json({"error": str(exc)[:200]}, status=503)
    except slack_gateway.SlackGatewayError as exc:
        return _json({"error": str(exc)[:200]}, status=401 if "signature" in str(exc) else 400)
    except (KeyError, LookupError, OSError, RuntimeError, TypeError, ValueError) as exc:
        # Slack must not receive stack traces or provider/request contents.
        log.exception("Slack ingress failed: %s", type(exc).__name__)
        return _json({"error": "Slack request could not be processed"}, status=500)
    return _json(result)


def _hub_version() -> str:
    """Read the running distribution version, including editable installs."""
    try:
        return metadata.version("unified-agent")
    except metadata.PackageNotFoundError:
        # A source checkout without distribution metadata still supports identity.
        return "unknown"


async def whoami(request: Request) -> Response:
    guard = _guard(request)
    if guard is not None:
        return guard
    session = auth.resolve_session(request.cookies.get(auth.COOKIE_NAME))
    return _json(
        {
            "agent": request.scope.get("ua_identity"),
            "via": request.scope.get("ua_identity_via"),
            "csrf": session["csrf"] if session else None,
            "hub_version": _hub_version(),
            "api_version": API_VERSION,
            "enforce_auth": config.enforce_auth(),
            "tokens_configured": bool(config.agent_tokens()),
            # Keep capability discovery explicit so the UI can render the available
            # action surface without guessing from route presence.
            "control_enabled": True,
            "control_disabled_reason": None,
            # The hub's tenant org (one per hub), from the same gate every write
            # stamps rows with. ``org`` is null exactly when ``org_mode`` is
            # "unpartitioned" (task #2469).
            **_whoami_org(),
        }
    )


def _whoami_org() -> dict[str, str | None]:
    org = edition.tenant_org_id()
    return {"org": org, "org_mode": "partitioned" if org else "unpartitioned"}


async def routine_fire_http(request: Request) -> Response:
    """Fire one API/webhook routine through the hub's bearer identity plane."""
    guard = _guard(request)
    if guard is not None:
        return guard
    # The HTTP trigger must stay authenticated even on a warn-mode hub. A UI
    # cookie or OAuth browser session must not become a webhook credential.
    if request.scope.get("ua_identity_via") != "bearer":
        return _json({"error": "hub bearer token required"}, status=401)
    if not profiles.tool_is_allowed_for(profiles.current(), "routine_fire"):
        return _json({"error": "routine_fire is not allowed for this profile"}, status=403)
    try:
        agent_access.enforce("routine_fire", read_only=False)
    except agent_access.AgentAccessDenied as exc:
        return _json({"error": str(exc)}, status=403)
    try:
        routine_id = int(request.path_params["routine_id"])
        if routine_id < 1:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return _json({"error": "routine id must be a positive integer"}, status=400)
    event_id = request.headers.get("idempotency-key", "")
    if not event_id or len(event_id) > 200 or event_id != event_id.strip():
        return _json(
            {"error": "Idempotency-Key must be 1-200 characters without surrounding whitespace"},
            status=400,
        )
    try:
        # The registered wrapper checks saved receipts and one-shot policy
        # approvals under the same guard used by routine_pause.
        from ..server import REGISTERED_TOOLS

        result = await anyio.to_thread.run_sync(
            lambda: REGISTERED_TOOLS["routine_fire"](
                routine_id=routine_id, idempotency_key=event_id
            )
        )
    except approvals_tool.ApprovalPendingError as exc:
        return _json(
            {"status": "approval_pending", "approval_id": exc.approval_id, "code": exc.code},
            status=202,
        )
    except policy.PolicyViolation as exc:
        return _json({"error": str(exc)}, status=403)
    except ToolError as exc:
        message = str(exc)
        status = {
            "unknown routine": 404,
            "routine is paused": 409,
            "cron routines are fired by the scheduler": 409,
        }.get(message, 503 if message.startswith("routines are disabled") else 400)
        return _json_body_error(exc, status=status, limit=None)
    return _json(result)


def _read_only(handler):
    """Wrap a payload coroutine into a guarded GET handler."""

    async def route(request: Request) -> Response:
        guard = _guard(request)
        if guard is not None:
            return guard
        res = await handler(request)
        if isinstance(res, Response):
            return res
        return _json(res)

    route.__name__ = getattr(handler, "__name__", "route")
    return route


async def _overview(request: Request):
    return await api.overview_payload()


async def _attention(request: Request):
    return await api.attention_payload()


async def _digest(request: Request):
    return await api.digest_payload()


async def _agents(request: Request):
    return await api.agents_payload()


async def _org(request: Request):
    return await api.org_payload()


async def _personas(request: Request):
    return await anyio.to_thread.run_sync(api.personas_payload)


async def _persona_detail(request: Request):
    label = request.path_params.get("label", "")
    from .. import personas

    persona = await anyio.to_thread.run_sync(personas.resolve_effective_persona, label)
    return {"persona": persona}


async def _dispatch_routes(request: Request):
    return await anyio.to_thread.run_sync(api.dispatch_routes_payload)


async def _sessions(request: Request):
    denied = _require_session_reader()
    if denied is not None:
        return denied
    params = request.query_params
    payload = await api.sessions_payload(
        agent=params.get("agent"),
        status=params.get("status"),
        limit=_int_param(params.get("limit"), 100),
    )
    # The Control Center shows Grant/Revoke only to the caller the route admits.
    payload["can_grant_wake"] = _operator_recovery(request)
    return payload


async def _tasks(request: Request):
    """GET /api/tasks — the active queue; ``completed_after`` adds tasks done since then.

    ``completed_after`` is an ISO 8601 date or datetime (task #2514, Home's "Just
    finished"); a value without an offset is read as UTC.
    """
    raw = request.query_params.get("completed_after")
    completed_after = None
    if raw is not None:
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return _json(
                {"error": "completed_after must be an ISO 8601 date or datetime"}, status=400
            )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        completed_after = parsed.astimezone(UTC).isoformat(timespec="seconds")
    return await api.tasks_payload(
        project=request.query_params.get("project"), completed_after=completed_after
    )


async def _next_work_projection(request: Request):
    denied = _require_task_list()
    if denied is not None:
        return denied
    from .. import planning_loop

    try:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                return _json({"error": "next-work manifest exceeds 1 MiB"}, status=413)
        manifest = json.loads(body)
        view = await anyio.to_thread.run_sync(planning_loop.next_work_projection_payload, manifest)
        response = _json(view)
        if len(response.body) > 2 * 1024 * 1024:
            return _json({"error": "next-work view exceeds 2 MiB"}, status=413)
        return response
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return _json({"error": str(exc)[:300]}, status=400)
    except (OSError, sqlite3.Error, storage.LiveStoreError):
        return _json({"error": "next-work projection unavailable on this host"}, status=503)


async def _task_search(request: Request):
    """GET /api/tasks/search?q=... — Control Center query box (task #1419).

    Ranked task search, not the substring `notes_contain` path — that stays
    reachable only through the MCP tool. `_read_only` already applies the
    origin/auth guard shared by every GET pane below, matching `_tasks`.
    """
    denied = _require_task_list()
    if denied is not None:
        return denied
    params = request.query_params
    query = (params.get("q") or params.get("query") or "").strip()
    if not query:
        return _json({"error": "missing search query (q)"}, status=400)
    return await api.task_search_payload(
        query=query,
        project=params.get("project"),
        limit=_int_param(params.get("limit"), 20),
    )


def _typed_error(exc: BaseException) -> TypedToolError | None:
    """The typed refusal behind ``exc``, if any (task #2056).

    A registered tool's ``ToolError`` can arrive re-raised by ``Tool.run`` with
    the typed one as ``__cause__``, as on the MCP side (``tool_errors.install``).
    """
    for candidate in (exc, exc.__cause__):
        if isinstance(candidate, TypedToolError) and candidate.code:
            return candidate
    return None


def _json_body_error(
    exc: BaseException,
    status: int = 400,
    *,
    limit: int | None = 300,
    message: str | None = None,
) -> Response:
    """``{"error": ...}`` plus the typed ``code`` when ``exc`` carries one.

    A ``conflict`` or ``outcome_unknown`` (a write that failed after it began,
    task #2521) is always 409 and a parked approval is always the pending
    202 (``_approval_pending``), whatever the route's default; every other
    code, and an untyped error, keeps the status the route passed. ``limit``
    caps ``str(exc)`` (``None``: uncapped); ``message`` replaces it, so a route
    keeps its own wording (a prefix, a different cap) and still gains the code.
    """
    typed = _typed_error(exc)
    if isinstance(typed, approvals_tool.ApprovalPendingError):
        # A parked call is not a failure: whatever the route's default, it is
        # the same 202 every dedicated approval branch returns (_write_error).
        return _approval_pending(typed)
    text = message if message is not None else str(exc)
    if message is None and limit is not None:
        text = text[:limit]
    body: dict[str, object] = {"error": text}
    if typed is not None:
        body["code"] = typed.code
        if typed.code in (ConflictError.code, OutcomeUnknownError.code):
            status = 409
    return _json(body, status=status)


def _writer_identity(request: Request) -> str | None:
    identity = request.scope.get("ua_identity")
    if isinstance(identity, str) and identity.strip():
        return identity.strip()
    return None


async def _governed_object_write(
    kind: str,
    action: str,
    *,
    actor: str,
    operation: str,
    tool: str,
    **kwargs: object,
) -> object:
    """HTTP writes use the same profile, access, policy, and approval gates as MCP."""
    if not profiles.tool_is_allowed_for(profiles.current(), tool):
        raise ResourceError(f"{tool} is unavailable to this profile")
    return await anyio.to_thread.run_sync(
        lambda: api._registered_platform_write(
            kind,
            action,
            actor=actor,
            operation=operation,
            production=True,
            **kwargs,
        )
    )


def _request_profile(request: Request | None = None) -> str:
    """The profile that bounds one HTTP request, resolved from its verified identity.

    A Control Center request carries no MCP session id, so the ambient exposure
    check (``profiles.tool_is_allowed``, ``platform_readers.deny``) admits every
    tool here. HTTP ceilings use the pure ``profiles.tool_is_allowed_for`` against
    this profile instead: the session's activated profile when the request is bound
    to one, else the identity's default (task #1865).
    """
    identity = _writer_identity(request) if request is not None else None
    if identity is not None and identity != config.verified_agent_identity():
        return profiles.default_for_identity(identity)
    return profiles.current()


def _require_task_list() -> Response | None:
    """Goal and comment HTTP reads need task_list in the caller's profile.

    The same ceiling as the platform readers, judged with the pure check: the
    ambient ``platform_readers.deny`` admits everything without an MCP session.
    """
    from ..platform_readers import PROFILE_DENIED_MESSAGE

    profile = _request_profile()
    if not profiles.tool_is_allowed_for(profile, "task_list"):
        return _json({"error": PROFILE_DENIED_MESSAGE.format(profile=profile)}, status=403)
    return None


def _work_graph_readers() -> tuple[bool, bool, bool, bool, bool] | Response:
    """Require task visibility and report readable optional object families."""
    profile = profiles.current()
    if not profiles.tool_is_allowed_for(profile, "task_list"):
        return _json({"error": "task_list is unavailable to this profile"}, status=403)
    return (
        profiles.tool_is_allowed_for(profile, "dispatch_report_self"),
        profiles.tool_is_allowed_for(profile, "autonomous_cycle_list")
        and profiles.tool_is_allowed_for(profile, "autonomous_cycle_get"),
        profiles.tool_is_allowed_for(profile, "session_list"),
        profiles.consolidated_action_is_allowed_for(profile, "plan_manage", "plan_get"),
        profiles.consolidated_action_is_allowed_for(profile, "observability_manage", "agent_list"),
    )


def _require_session_reader() -> Response | None:
    """Keep direct session routes under the same gate as platform session objects."""
    if not profiles.tool_is_allowed_for(profiles.current(), "session_list"):
        return _json({"error": "session_list is unavailable to this profile"}, status=403)
    return None


def _require_run_reader(request: Request) -> Response | None:
    """The run object's reader ceiling: dispatch_report_self, or the verified operator.

    The same rule as the route's ``operator=True`` scope, kept in the handler as
    defence in depth and for warn mode, where the scope gate has no profile.
    """
    if _writer_identity(request) is None:
        return _json({"error": "authenticated identity required"}, status=401)
    if not _operator_recovery(request) and not profiles.tool_is_allowed_for(
        _request_profile(request), "dispatch_report_self"
    ):
        return _json({"error": "dispatch_report_self is unavailable to this profile"}, status=403)
    return None


async def _runs(request: Request):
    """GET /api/runs — one uniform row per run across the three run stores (#1652)."""
    denied = _require_run_reader(request)
    if denied is not None:
        return denied
    params = request.query_params
    kind = (params.get("kind") or "").strip() or None
    if kind is not None and kind not in ("dispatch", "selfpickup", "session"):
        return _json({"error": "kind must be dispatch, selfpickup or session"}, status=400)
    try:
        task_id = _optional_int_param(params.get("task_id"), "task_id")
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    try:
        return await api.runs_payload(
            kind=kind,
            agent=params.get("agent"),
            task_id=task_id,
            cursor=params.get("cursor"),
            limit=_int_param(params.get("limit"), 100),
        )
    except ValueError as exc:
        return _json({"error": str(exc)[:300]}, status=400)


async def _run_detail(request: Request):
    """GET /api/runs/{run_id} — one uniform run row, or 404."""
    denied = _require_run_reader(request)
    if denied is not None:
        return denied
    run = await api.run_detail_payload(request.path_params.get("run_id", ""))
    if run is None:
        return _json({"error": "unknown run"}, status=404)
    return {"run": run}


def _operator_recovery(request: Request) -> bool:
    """True when the verified operator is calling: the off switch is theirs to flip.

    Same test and auth channels (``agent_access.OPERATOR_RECOVERY_VIA``) as the
    hub middleware's human-recovery exemption. A scope naming the operator
    without one of those channels gets no exemption.
    """
    return agent_access.is_operator_recovery(
        _writer_identity(request), request.scope.get("ua_identity_via")
    )


#: Policy refusals that mean "try again later", answered 429 rather than 403.
_THROTTLED_REASONS = frozenset({"rate_limited", "rate_limit_capacity"})


class _ReadAdmission:
    """An admitted governed read whose route reports how the read ended.

    ``record_now`` calls ``gateway.record_call_result`` the way the registered
    wrapper does after a call body. A refusal never reaches here:
    ``_governed_read`` already recorded it. What is recorded after admission
    differs by route. ``/api/events/stream`` records once through
    ``sse.event_stream(on_open=...)``: an allow row when the stream opens, or the
    error that stopped it first. ``/api/ledger/rows`` records only a failed read
    (``record_failure``); an allowed ledger read writes nothing, as an allowed
    MCP ``event_log_query`` call writes nothing (``_UNRECORDED_ALLOWED_READS`` in
    ``server.py``, task #1811). See ``ledger_rows``.
    """

    def __init__(self, tool: str, invocation: object) -> None:
        self.tool = tool
        self.invocation = invocation

    def record_now(self, error: BaseException | None) -> None:
        from .. import gateway

        gateway.record_call_result(self.tool, self.invocation, error)

    async def record_failure(self, error: BaseException) -> None:
        """Record a failed read off the event loop, even while being cancelled."""
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(self.record_now, error)


class _CapacityDenials:
    """Coalesce repeated stream-capacity denials per caller (PR #981 review).

    A refused stream ends, and the browser's EventSource reconnects on its own,
    so every retry would otherwise append another denial row. The first refusal
    per caller in ``window_s`` is recorded; repeats inside the window are not.
    In-process and bounded: a hub restart forgets it, and at most ``max_callers``
    callers are tracked (expired entries first, then the oldest, are dropped).
    """

    def __init__(self, window_s: float = 60.0, max_callers: int = 256) -> None:
        self.window_s = window_s
        self.max_callers = max_callers
        self.clock = time.monotonic
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def should_record(self, caller: str) -> bool:
        now = self.clock()
        with self._lock:
            last = self._last.get(caller)
            if last is not None and now - last < self.window_s:
                return False
            self._last.pop(caller, None)
            if len(self._last) >= self.max_callers:
                for key in [k for k, t in self._last.items() if now - t >= self.window_s]:
                    del self._last[key]
                while len(self._last) >= self.max_callers:
                    del self._last[next(iter(self._last))]
            self._last[caller] = now
            return True

    def clear(self) -> None:
        with self._lock:
            self._last.clear()


_CAPACITY_DENIALS = _CapacityDenials()


def _stream_on_open(admission: _ReadAdmission, caller: str | None):
    """The stream's ``on_open``: record its decision, coalescing capacity repeats."""

    def on_open(error: BaseException | None) -> None:
        if isinstance(error, sse.StreamCapacityError) and not _CAPACITY_DENIALS.should_record(
            caller or ""
        ):
            return
        admission.record_now(error)

    return on_open


def _read_gate_payload(**arguments: object) -> dict[str, object]:
    """The policy and approval payload for an HTTP event-log read.

    It uses ``event_log_query``'s own argument names and, like the registered
    wrapper, only the arguments actually supplied (``None`` means omitted).
    An approval pass is keyed by the canonical payload, so a pass carries over
    between the HTTP and MCP adapters only when the arguments are equal. The
    ledger always supplies ``after_seq`` and ``limit`` and the stream never
    takes ``limit``, so in practice a pass is specific to the adapter whose
    arguments it names.
    """
    return approvals_tool.supplied_arguments_payload(arguments)


async def _governed_read(
    request: Request, tool: str, payload: dict[str, object]
) -> Response | _ReadAdmission:
    """Admit one HTTP read that bypasses the registered tool (task #1684).

    A route that reads storage directly, instead of calling ``tool`` through
    ``server.REGISTERED_TOOLS``, still owes the gates that registered call
    passes, in the same order: the profile ceiling, the agent-access switch,
    then call permission (policy rules, trust presets and the per-caller rate
    limit, charged to ``tool`` so the HTTP read and the MCP read share one
    budget). The ``UA_APPROVAL_TOOLS`` gate follows unless a policy approval
    already covered this invocation, as ``api._registered_platform_write``
    does. Run it before any row is read or any stream slot is taken.

    The operator keeps the human-recovery exemption from the access switch
    that the hub middleware grants, so flipping it off never hides the
    operator's own recovery reads. Profile, policy, approval and rate limits
    still apply to the operator.

    Returns an error response, or a ``_ReadAdmission`` through which the route
    reports how the read ended.
    """
    from .. import gateway, safety, server

    identity = _writer_identity(request)
    human_recovery = _operator_recovery(request)

    def _admit() -> Response | _ReadAdmission:
        # Policy loads its file and may resolve claims from SQLite: like a
        # registered tool call (``api.call_tool``) it runs off the event loop.
        if not profiles.tool_is_allowed_for(profiles.current(), tool):
            return _json({"error": f"{tool} is unavailable to this profile"}, status=403)
        try:
            # As in the registered wrapper, a refusal leaves the invocation
            # scope as an exception, so a parked approval refunds its rate slot.
            with gateway.invocation(tool) as call:
                try:
                    agent_access.enforce(
                        tool,
                        read_only=True,
                        production=server._production_operation(tool),
                        human_recovery=human_recovery,
                    )
                    with approvals_tool.approval_invocation() as receipts:
                        safety.check_policy_gate(tool, payload, originator=identity)
                        policy_consumed = any(
                            isinstance(receipt, dict) and receipt.get("approval_id") is not None
                            for receipt in receipts.values()
                        )
                        if not policy_consumed:
                            approvals_tool.check_approval_gate(tool, payload, requested_by=identity)
                        approvals_tool.mark_governed_execution()
                except BaseException as exc:
                    # A policy refusal already recorded itself; this records the rest.
                    gateway.record_call_result(tool, call, exc)
                    raise
        except approvals_tool.ApprovalPendingError as exc:
            return _json(
                {"approval_id": exc.approval_id, "code": exc.code, "status": "pending"}, status=202
            )
        except policy.PolicyViolation as exc:
            status = 429 if exc.reason_code in _THROTTLED_REASONS else 403
            return _json({"error": str(exc)[:300]}, status=status)
        except ToolError as exc:  # access, trust-preset and denied-approval refusals
            return _json_body_error(exc, status=403)
        return _ReadAdmission(tool, call)

    return await anyio.to_thread.run_sync(_admit)


def _approval_pending(exc: approvals_tool.ApprovalPendingError) -> Response:
    """The shared parked-approval answer: 202 with the id, ``pending`` and the code."""
    return _json(
        {"approval_id": exc.approval_id, "status": "pending", "code": exc.code},
        status=202,
    )


def _write_error(exc: Exception) -> Response:
    if isinstance(exc, approvals_tool.ApprovalPendingError):
        return _approval_pending(exc)
    if isinstance(exc, agent_access.AgentAccessDenied | ResourceError):
        return _json({"error": str(exc)[:300]}, status=403)
    return _json_body_error(exc)


async def goals_collection(request: Request) -> Response:
    """GET lists the why-tree. POST adds or links through the goal writers. No delete."""
    if request.method == "GET":
        guard = _guard(request)
        if guard is not None:
            return guard
        denied = _require_task_list()
        if denied is not None:
            return denied
        from ..goals import goal_tree

        project = request.query_params.get("project")
        payload = await anyio.to_thread.run_sync(lambda: goal_tree(project=project))
        return _json(payload)
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = _writer_identity(request)
    if identity is None:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    if "author" in body:
        return _json({"error": "author is taken from the authenticated identity"}, status=400)
    action = str(body.get("action") or "")
    try:
        if action == "add":
            result = await _governed_object_write(
                "goal",
                "add",
                actor=identity,
                operation="goal_manage",
                tool="goal_manage",
                title=str(body.get("title") or ""),
                statement=body.get("statement"),
                parent_goal_id=body.get("parent_goal_id"),
                project=body.get("project"),
                author=identity,
            )
        elif action == "link":
            result = await _governed_object_write(
                "goal",
                "link",
                actor=identity,
                operation="goal_manage",
                tool="goal_manage",
                task_id=int(body["task_id"]),
                goal_id=int(body["goal_id"]),
                author=identity,
            )
        else:
            return _json(
                {"error": "action must be add or link; other goal actions use goal_manage"},
                status=400,
            )
    except (
        ToolError,
        KeyError,
        TypeError,
        ValueError,
        agent_access.AgentAccessDenied,
        ResourceError,
        approvals_tool.ApprovalPendingError,
    ) as exc:
        return _write_error(exc)
    return _json(result, status=201)


async def goal_detail(request: Request) -> Response:
    guard = _guard(request)
    if guard is not None:
        return guard
    denied = _require_task_list()
    if denied is not None:
        return denied
    from ..goals import get_goal

    try:
        goal_id = int(request.path_params["goal_id"])
    except (TypeError, ValueError):
        return _json({"error": "invalid goal_id"}, status=400)
    detail = await anyio.to_thread.run_sync(lambda: get_goal(goal_id))
    if detail is None:
        return _json({"error": "unknown goal"}, status=404)
    return _json(detail)


async def task_why_chain(request: Request) -> Response:
    guard = _guard(request)
    if guard is not None:
        return guard
    denied = _require_task_list()
    if denied is not None:
        return denied
    from ..goals import why_chain

    try:
        task_id = int(request.path_params["task_id"])
    except (TypeError, ValueError):
        return _json({"error": "invalid task_id"}, status=400)
    try:
        payload = await anyio.to_thread.run_sync(lambda: why_chain(task_id))
    except ToolError as exc:
        return _json_body_error(exc, status=404)
    return _json(payload)


async def task_comment_thread(request: Request) -> Response:
    """GET lists the thread. POST appends. There is no update or delete method."""
    from mcp.server.mcpserver.exceptions import ToolError

    from ..task_comments import list_comments

    try:
        task_id = int(request.path_params["task_id"])
    except (TypeError, ValueError):
        return _json({"error": "invalid task_id"}, status=400)
    if request.method == "GET":
        guard = _guard(request)
        if guard is not None:
            return guard
        denied = _require_task_list()
        if denied is not None:
            return denied
        try:
            payload = await anyio.to_thread.run_sync(lambda: list_comments(task_id))
        except ToolError as exc:
            return _json_body_error(exc, status=404)
        return _json(payload)
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = _writer_identity(request)
    if identity is None:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    if body.get("action") in {"update", "delete", "edit", "remove"}:
        return _json({"error": "comments are append-only"}, status=405)
    if "author" in body:
        return _json({"error": "author is taken from the authenticated identity"}, status=400)
    try:
        result = await _governed_object_write(
            "comment",
            "append",
            actor=identity,
            operation="task_comment",
            tool="task_comment",
            task_id=task_id,
            body=str(body.get("body") or ""),
            parent_comment_id=body.get("parent_comment_id"),
            mentions=body.get("mentions"),
            refs=body.get("refs"),
            idempotency_key=body.get("idempotency_key"),
            author=identity,
        )
    except (
        ToolError,
        KeyError,
        TypeError,
        ValueError,
        agent_access.AgentAccessDenied,
        ResourceError,
        approvals_tool.ApprovalPendingError,
    ) as exc:
        return _write_error(exc)
    return _json(result, status=201)


def _float_param(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    # NaN/Infinity parse cleanly but serialize as invalid JSON tokens, which
    # breaks JSON.parse in the browser — reject non-finite values.
    return value if math.isfinite(value) else None


async def _board(request: Request):
    params = request.query_params
    include_cancelled = params.get("include_cancelled", "").lower() in {
        "1",
        "true",
        "yes",
    }
    return await api.board_payload(
        project=params.get("project"),
        lane=params.get("lane"),
        agent=params.get("agent"),
        priority=params.get("priority"),
        min_age_days=_float_param(params.get("min_age_days") or params.get("age")),
        swimlane_by=params.get("swimlane_by") or "lane",
        include_cancelled=include_cancelled,
        claimed_by=params.get("claimed_by") or None,
    )


async def _flow_metrics(request: Request):
    return await api.flow_metrics_payload()


async def _queue_health(request: Request):
    return await api.queue_health_payload()


async def _projects(request: Request):
    return await api.projects_payload()


async def _review_approval(request: Request):
    return await api.review_approval_payload()


async def _executor_delegation(request: Request):
    return await api.executor_delegation_payload()


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """RFC 9110 weak comparison of an ``If-None-Match`` list against ``etag``."""
    if not if_none_match:
        return False
    opaque = etag.removeprefix("W/")
    for candidate in if_none_match.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == opaque:
            return True
    return False


def _conditional_json(request: Request, payload) -> Response:
    """JSON with an ETag over the exact body; 304 when the client already has it.

    The payload is still built on every request, so every gate the build runs
    (agent access, profile, policy, perf sampling) still applies to every poll.
    What a match saves is the body on the wire and the client's re-render.
    """
    response = _json(payload)
    etag = 'W/"' + hashlib.sha256(response.body).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return response


async def _approvals(request: Request):
    # Conditional GET (task #1786): the queue changes rarely and is polled often.
    return _conditional_json(request, await api.approvals_payload())


async def approval_detail(request: Request) -> Response:
    """GET /api/approvals/{id} — full single-row detail for the drawer (task #1263).

    Same guard as every other read route; a non-integer id or an id with no
    matching row is a 404, never a 500 (mirrors session_detail below).
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    try:
        approval_id = int(request.path_params["id"])
    except (KeyError, ValueError):
        return _json({"error": "invalid approval id; expected integer"}, status=400)
    detail = await api.approval_detail_payload(approval_id, caller=request.scope.get("ua_identity"))
    if "error" in detail:
        return _json(detail, status=404)
    return _json(detail)


async def _memory(request: Request):
    params = request.query_params
    # Accept both `q` and `query` so the pane form and curl stay interchangeable.
    query = params.get("q") or params.get("query")
    tags = params.getlist("tag")
    return await api.memory_payload(
        query=query,
        tag=tags[0] if tags else None,
        tags=tags[1:],
        limit=_int_param(params.get("limit"), 50),
        as_of=params.get("as_of"),
        author=params.get("author"),
        project=params.get("project"),
    )


async def vault_search(request: Request) -> Response:
    """GET /api/vault/search — regex search over vault notes (task #1123)."""
    guard = _guard(request)
    if guard is not None:
        return guard
    from mcp.server.mcpserver.exceptions import ToolError

    params = request.query_params
    pattern = (params.get("q") or params.get("pattern") or "").strip()
    if not pattern:
        return _json({"error": "missing search query (q)"}, status=400)
    path = (params.get("path") or ".").strip() or "."
    max_results = _int_param(params.get("max_results") or params.get("limit"), 100)
    try:
        return _json(
            await api.vault_search_payload(
                pattern=pattern,
                path=path,
                max_results=max_results,
            )
        )
    except ToolError as exc:
        msg = str(exc)
        if "disabled" in msg.lower() or "UA_VAULT_PATH" in msg:
            return _json(api._vault_not_configured(matches=[], truncated=False))
        return _json_body_error(exc, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)


async def vault_page(request: Request) -> Response:
    """GET /api/vault/page?path=… — note content with backlinks (task #1123)."""
    guard = _guard(request)
    if guard is not None:
        return guard
    from mcp.server.mcpserver.exceptions import ToolError

    path = (request.query_params.get("path") or "").strip()
    if not path:
        return _json({"error": "missing path"}, status=400)
    try:
        return _json(await api.vault_page_payload(path=path))
    except ToolError as exc:
        msg = str(exc)
        status = 404 if "no such note" in msg else 400
        if "disabled" in msg.lower() or "UA_VAULT_PATH" in msg:
            return _json(
                api._vault_not_configured(path=path, content="", outgoing=[], backlinks=[])
            )
        return _json_body_error(exc, status=status)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)


async def vault_ingest_status(request: Request) -> Response:
    """GET /api/vault/ingest-status?path=… — read-only classification (task #1123).

    Query params ``mark`` / ``agent`` are ignored: the Control Center never
    records an ingest from the web surface.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    from mcp.server.mcpserver.exceptions import ToolError

    path = (request.query_params.get("path") or "").strip()
    if not path:
        return _json({"error": "missing path"}, status=400)
    try:
        return _json(await api.vault_ingest_status_payload(path=path))
    except ToolError as exc:
        msg = str(exc)
        if "disabled" in msg.lower() or "UA_VAULT_PATH" in msg:
            return _json(api._vault_not_configured(path=path, status=None, pages=[]))
        status = 404 if "no such note" in msg else 400
        return _json_body_error(exc, status=status)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)


async def vault_briefing(request: Request) -> Response:
    """GET /api/vault/briefing — daily digest + inbox ingest strip (task #1123)."""
    guard = _guard(request)
    if guard is not None:
        return guard
    from mcp.server.mcpserver.exceptions import ToolError

    params = request.query_params
    date = (params.get("date") or "").strip() or None
    include_raw = (params.get("include_inbox") or "1").lower()
    include_inbox = include_raw not in {"0", "false", "no"}
    try:
        return _json(await api.vault_briefing_payload(date=date, include_inbox=include_inbox))
    except ToolError as exc:
        msg = str(exc)
        if "disabled" in msg.lower() or "UA_VAULT_PATH" in msg:
            return _json(
                api._vault_not_configured(
                    date=date,
                    entries=[],
                    inbox=[],
                    inbox_pending_count=0,
                )
            )
        return _json_body_error(exc, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)


async def connectors(request: Request) -> Response:
    """GET /api/connectors — Horizon-2 server + package lifecycle (task #1147).

    Read-only inventory: never reaches register/revoke/authorize. Failures
    surface as a named ``horizon2 unavailable`` payload (HTTP 200), never 500.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    try:
        return _json(await api.connectors_payload())
    except Exception:  # noqa: BLE001 - named error state for the pane
        return _json(api._horizon2_unavailable())


async def connector_oauth_status(request: Request) -> Response:
    guard = _guard(request)
    if guard is not None:
        return guard
    actor = request.scope.get("ua_identity")
    if not actor or config.actor_kind_for_identity(actor) != "human":
        return _json({"error": "connector accounts require a human operator"}, status=403)
    try:
        return _json(
            connector_oauth.status(request.path_params["provider"], org_id=edition.tenant_org_id())
        )
    except connector_oauth.ConnectorOAuthError as exc:
        return _json({"error": str(exc)}, status=400)


async def connector_oauth_begin(request: Request) -> Response:
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    actor = request.scope.get("ua_identity")
    if not actor or config.actor_kind_for_identity(actor) != "human":
        return _json({"error": "connector accounts require a human operator"}, status=403)
    try:
        agent_access.enforce("connector_oauth_begin", read_only=False, production=True)
    except agent_access.AgentAccessDenied as exc:
        return _json({"error": str(exc)}, status=403)
    meta = config.current_auth_metadata() or {}
    try:
        result = await anyio.to_thread.run_sync(
            lambda: connector_oauth.begin(
                request.path_params["provider"],
                actor=actor,
                org_id=edition.tenant_org_id(),
                device_node=meta.get("device_node"),
            )
        )
    except connector_oauth.ConnectorOAuthError as exc:
        return _json({"error": str(exc)}, status=400)
    return _json(result, status=201)


def _netloc_default_port_folded(url: str) -> str:
    """netloc with an explicit default port folded back to the implicit form.

    ``https://hub.example.test:443`` and ``https://hub.example.test`` are the
    same origin; a client that sends ``Host: name:443`` (which tailscaled
    faithfully copies into ``X-Forwarded-Host``, task #1892) must not trip
    the callback host check for it. Only the scheme's own default port
    folds — ``http://name:443`` stays distinct.
    """
    parts = urlsplit(url)
    netloc = parts.netloc.lower()
    if (parts.scheme == "https" and netloc.endswith(":443")) or (
        parts.scheme == "http" and netloc.endswith(":80")
    ):
        return netloc.rsplit(":", 1)[0]
    return netloc


async def connector_oauth_callback(request: Request) -> Response:
    """Provider redirect; one-use state is the sole credential for this route."""
    issuer = config.mcp_oauth_issuer()
    if not issuer or _netloc_default_port_folded(str(request.url)) != _netloc_default_port_folded(
        issuer
    ):
        return PlainTextResponse("Connector callback host is invalid", status_code=403)
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    if request.query_params.get("error"):
        return PlainTextResponse(
            "Connector authorization was declined. Return to Connectors.", status_code=400
        )
    try:
        result = await anyio.to_thread.run_sync(lambda: connector_oauth.complete(state, code))
    except connector_oauth.ConnectorOAuthError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return PlainTextResponse(
        f"{result['provider'].capitalize()} connected. Return to your Control Center tab and refresh Connectors."
    )


async def connector_oauth_disconnect(request: Request) -> Response:
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    actor = request.scope.get("ua_identity")
    if not actor or config.actor_kind_for_identity(actor) != "human":
        return _json({"error": "connector accounts require a human operator"}, status=403)
    try:
        agent_access.enforce("connector_oauth_disconnect", read_only=False, production=True)
    except agent_access.AgentAccessDenied as exc:
        return _json({"error": str(exc)}, status=403)
    try:
        result = await anyio.to_thread.run_sync(
            lambda: connector_oauth.disconnect(
                request.path_params["provider"], actor=actor, org_id=edition.tenant_org_id()
            )
        )
    except connector_oauth.ConnectorOAuthError as exc:
        return _json({"error": str(exc)}, status=400)
    return _json(result)


async def quota(request: Request) -> Response:
    """GET /api/quota — fleet lane availability projection (task #1136).

    Read-only report of ``model_quota_status``: never mutates credential_pool or
    dispatch state. Failures surface as a named ``quota projection unavailable``
    payload (HTTP 200), never 500.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    try:
        return _json(await api.quota_payload())
    except Exception:  # noqa: BLE001 - named error state for the pane
        return _json(api._quota_projection_unavailable())


async def credential_pool(request: Request) -> Response:
    """GET /api/credential_pool — credential metadata inventory (task #1121).

    Read-only: never registers or revokes a credential, and never returns a
    secret value — ``credential_pool_list`` stores metadata only. Failures
    surface as a named ``credential pool unavailable`` payload (HTTP 200),
    never 500.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    try:
        return _json(await api.credential_pool_payload())
    except Exception:  # noqa: BLE001 - named error state for the pane
        return _json(api._credential_pool_unavailable())


async def slack_gateway_status(request: Request) -> Response:
    """GET /api/slack_gateway — Slack ingress health / recent activity (task #1185).

    Status-only: never sends a Slack message and never expands the Slack
    write surface. Failures surface as a named ``slack gateway status
    unavailable`` payload (HTTP 200), never 500.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    try:
        return _json(await api.slack_gateway_payload())
    except Exception:  # noqa: BLE001 - named error state for the pane
        return _json(api._slack_gateway_unavailable())


async def notify_delivery(request: Request) -> Response:
    """GET /api/notify_delivery — outbound notify/iMessage delivery outcomes (task #1183).

    Read-only: never sends a notification itself — ``notify`` / `batch_notify`
    stay MCP/approval-gated, and this route reads their existing durable
    delivery log. Failures surface as a named ``notify delivery status
    unavailable`` payload (HTTP 200), never 500.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    try:
        return _json(await api.notify_delivery_payload())
    except Exception:  # noqa: BLE001 - named error state for the pane
        return _json(api._notify_delivery_unavailable())


async def dispatch_receipts(request: Request) -> Response:
    """GET /api/dispatch_receipts — recent dispatch delivery-receipt summaries (task #1182).

    Read-only projection of the existing ``dispatch_runs`` ledger — never a
    second ledger, never mutates a run, never weakens an evidence gate.
    Failures surface as a named ``dispatch delivery receipts unavailable``
    payload (HTTP 200), never 500.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    raw_task_id = request.query_params.get("task_id")
    try:
        task_id = int(raw_task_id) if raw_task_id else None
    except ValueError:
        return _json({"error": "task_id must be an integer"}, status=400)
    state = request.query_params.get("state") or None
    try:
        return _json(await api.dispatch_receipts_payload(task_id=task_id, state=state))
    except Exception:  # noqa: BLE001 - named error state for the pane
        return _json(api._dispatch_receipts_unavailable())


async def _health(request: Request):
    return await api.health_payload(force=request.query_params.get("force") == "1")


async def _cloud(request: Request):
    return await api.cloud_payload(force=request.query_params.get("force") == "1")


async def _devices(request: Request):
    return await api.devices_payload()


async def _nodes(request: Request):
    """GET /api/nodes — registered ua-node devices + recent dispatches (task #1329)."""
    return await api.nodes_payload()


async def _perf(request: Request):
    return await api.perf_payload(
        since_hours=float(_int_param(request.query_params.get("since_hours"), 24))
    )


async def _delivery_slo(request: Request):
    """GET /api/delivery_slo — Epic C (#1188) exit-test SLO metrics (task #1259).

    Read-only: dispatch success rate, p90 cycle time, unattributed
    completions, CI green rate over `window_days` (default 7).
    """
    return await api.delivery_slo_payload(
        window_days=_float_param(request.query_params.get("window_days")) or 7.0
    )


def _config_revision_authority(identity: str, *, recovery: bool = False) -> Response | None:
    """Same chief-of-staff gate as the MCP tool. HTTP must not be a side door.

    ``recovery`` lets a code-default chief of staff list and roll back after
    an overlay downgrade. Commit is not an HTTP action.
    """
    profile = profiles.default_for_identity(identity)
    if profiles.tool_is_allowed_for(profile, "config_revision_manage"):
        return None
    if recovery:
        from ..config_revisions import rollback_recovery_allowed

        if rollback_recovery_allowed(identity, "rollback"):
            return None
    return _json(
        {"error": "config revisions require the chief-of-staff profile"},
        status=403,
    )


async def _config_revisions(request: Request):
    """GET /api/config-revisions — immutable authority-config history (task #1352)."""
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    denied = _config_revision_authority(identity, recovery=True)
    if denied is not None:
        return denied
    params = request.query_params
    kind = params.get("kind") or "policy"
    from_raw = params.get("from")
    to_raw = params.get("to")

    def _optional_int(value: str | None) -> int | None:
        if value is None or value == "" or value == "live":
            return None
        return int(value)

    try:
        from_revision = _optional_int(from_raw)
        to_revision = _optional_int(to_raw)
    except ValueError:
        return _json({"error": "revision ids must be integers"}, status=400)
    return await api.config_revisions_payload(
        kind, from_revision=from_revision, to_revision=to_revision
    )


async def action_config_revision(request: Request) -> Response:
    """POST /api/action/config-revision — approval-gated rollback (task #1352)."""
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    denied = _config_revision_authority(identity, recovery=True)
    if denied is not None:
        return denied
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    kind = body.get("kind") or "policy"
    try:
        revision = int(body["revision"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "revision must be an integer"}, status=400)
    try:
        result = await api.config_revision_rollback_payload(kind, revision, identity)
    except ToolError as exc:
        return _json_body_error(exc, status=400, limit=None)
    status = 202 if result.get("pending") else 200
    if result.get("status") == 202:
        status = 202
    return _json(result, status=status)


def _operating_authority(identity: str | None) -> Response | None:
    """Profile tool visibility for the Operating pane, and nothing else.

    These are goal_manage actions, so a non-human caller needs goal_manage in
    its profile. The human operator's web profile carries no goal_manage, yet
    operating settings are the operator's to set, so a verified human skips
    this one check. Every other gate still runs for humans on a write
    (agent_access, trust presets, policy rules and rate limits, the approval
    gate): see ``api.operating_action_payload``.
    """
    if identity and config.actor_kind_for_identity(identity) == "human":
        return None
    # Both the request's profile and the identity's own default (which
    # carries a committed profile_defaults downgrade) must allow it, so a
    # downgraded agent is refused even without a bound session profile.
    candidates = {profiles.current(), profiles.default_for_identity(identity)}
    if not all(profiles.tool_is_allowed_for(p, "goal_manage") for p in candidates):
        return _json({"error": "goal_manage is unavailable to this profile"}, status=403)
    return None


def _operating_tool_error(exc: ToolError) -> Response:
    message = str(exc)
    if isinstance(exc, policy.PolicyViolation) or message.startswith("trust preset "):
        return _json({"error": message[:300]}, status=403)
    status = 404 if message.startswith(("unknown task:", "unknown sprint:")) else 400
    return _json_body_error(exc, status=status)


def _optional_int_param(raw: str | None, name: str) -> int | None:
    if raw is None or raw.strip() == "":
        return None
    text = raw.strip()
    if not (text.isascii() and text.isdigit()) or len(text) > 18:
        raise ValueError(f"{name} must be a positive integer")
    return int(text)


async def _operating(request: Request):
    """GET /api/operating — profiles, office bindings and offices (task #1757)."""
    denied = _operating_authority(_writer_identity(request))
    if denied is not None:
        return denied
    return await api.operating_payload()


async def _operating_resolve(request: Request):
    """GET /api/operating/resolve?task_id=&sprint_id= — resolved view with provenance."""
    denied = _operating_authority(_writer_identity(request))
    if denied is not None:
        return denied
    try:
        task_id = _optional_int_param(request.query_params.get("task_id"), "task_id")
        sprint_id = _optional_int_param(request.query_params.get("sprint_id"), "sprint_id")
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    try:
        return await api.operating_resolve_payload(task_id, sprint_id)
    except ToolError as exc:
        return _operating_tool_error(exc)


async def _operating_sprints(request: Request):
    """GET /api/operating/sprints?status= — sprints with linked task ids."""
    denied = _operating_authority(_writer_identity(request))
    if denied is not None:
        return denied
    status = (request.query_params.get("status") or "").strip() or None
    try:
        return await api.operating_sprints_payload(status)
    except ToolError as exc:
        return _operating_tool_error(exc)


async def _operating_action(request: Request, kind: str) -> Response:
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = _writer_identity(request)
    if identity is None:
        return _json({"error": "authenticated identity required"}, status=401)
    denied = _operating_authority(identity)
    if denied is not None:
        return denied
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    try:
        result = await api.operating_action_payload(kind, body, identity)
    except api.OperatingRequestError as exc:
        return _json({"error": str(exc)}, status=400)
    except agent_access.AgentAccessDenied as exc:
        return _json({"error": str(exc)[:300]}, status=403)
    except ToolError as exc:
        return _operating_tool_error(exc)
    return _json(result, status=202 if result.get("pending") else 200)


async def action_operating_sprint(request: Request) -> Response:
    """POST /api/action/operating-sprint — {op: add|update|link|unlink, ...} (task #1757)."""
    return await _operating_action(request, "sprint")


async def action_operating_run(request: Request) -> Response:
    """POST /api/action/operating-run — {op: set|clear, task_id, ...} (task #1757)."""
    return await _operating_action(request, "run")


async def _teams(request: Request):
    """GET /api/teams — committed team specs (task #1736)."""
    if not request.scope.get("ua_identity"):
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        return await api.teams_payload()
    except ToolError as exc:
        return _json_body_error(exc, status=400)


async def _team_plan(request: Request):
    """GET /api/teams/plan?team=<office id> — dry-run boot plan (task #1736)."""
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    team = request.query_params.get("team") or ""
    if not team:
        return _json({"error": "team is required"}, status=400)
    try:
        return await api.team_plan_payload(team, identity)
    except PermissionError as exc:
        return _json_body_error(exc, status=403)
    except ToolError as exc:
        return _json_body_error(exc, status=400)


# --- team snapshot and restore (task #1738) ------------------------------------

_SNAPSHOTS = "/api/teams/snapshots"
_SNAPSHOT_PREVIEW = "/api/teams/snapshots/{snapshot_id}/preview"
_SNAPSHOT_RESTORE = "/api/teams/snapshots/{snapshot_id}/restore"


def _snapshot_gate(request: Request, method: str, path: str) -> Response | None:
    """The route's declared ceiling, enforced by the handler too."""
    if not request.scope.get("ua_identity"):
        return _json({"error": "authenticated identity required"}, status=401)
    return _scope_gate(request, route_scopes.ROUTE_SCOPES[(method, path)])


async def team_snapshots(request: Request) -> Response:
    """GET lists recent team snapshots; POST {team} or {item_ids} takes one."""
    from .. import team_snapshot

    method = request.method.upper()
    denied = _snapshot_gate(request, method, _SNAPSHOTS)
    if denied is not None:
        return denied
    if method == "GET":
        return _json({"snapshots": await anyio.to_thread.run_sync(team_snapshot.list_snapshots)})
    raw, too_big = await _read_capped_body(request, 16_384)
    if too_big is not None:
        return too_big
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict) or set(body) - {"team", "item_ids"}:
        return _json({"error": "body takes team or item_ids"}, status=400)
    item_ids = body.get("item_ids")
    if item_ids is not None and (
        not isinstance(item_ids, list) or not all(isinstance(i, str) for i in item_ids)
    ):
        return _json({"error": "item_ids must be a list of strings"}, status=400)
    team = body.get("team")
    if team is not None and not isinstance(team, str):
        return _json({"error": "team must be a string"}, status=400)
    try:
        result = await anyio.to_thread.run_sync(
            lambda: team_snapshot.snapshot(team=team, item_ids=item_ids)
        )
    except ToolError as exc:
        return _json_body_error(exc, status=400)
    return _json(result, status=201)


async def team_snapshot_preview(request: Request) -> Response:
    """Planned outcome per seat (resumed, rebuilt, fresh, failed, live); read-only."""
    from .. import team_snapshot

    denied = _snapshot_gate(request, "GET", _SNAPSHOT_PREVIEW)
    if denied is not None:
        return denied
    snapshot_id = str(request.path_params.get("snapshot_id") or "")
    operator = bool(_operator_recovery(request))
    try:
        result = await anyio.to_thread.run_sync(
            lambda: team_snapshot.preview_for(snapshot_id, operator=operator)
        )
    except ToolError as exc:
        return _json_body_error(exc, status=404 if "no team snapshot" in str(exc) else 400)
    return _json(result)


async def team_snapshot_restore(request: Request) -> Response:
    """Restore a snapshot: each seat reports its honest outcome."""
    from .. import team_snapshot

    denied = _snapshot_gate(request, "POST", _SNAPSHOT_RESTORE)
    if denied is not None:
        return denied
    snapshot_id = str(request.path_params.get("snapshot_id") or "")
    operator = bool(_operator_recovery(request))
    try:
        result = await anyio.to_thread.run_sync(
            lambda: team_snapshot.restore_for(snapshot_id, operator=operator)
        )
    except ToolError as exc:
        return _json_body_error(exc, status=404 if "no team snapshot" in str(exc) else 400)
    return _json(result)


async def action_team_up(request: Request) -> Response:
    """POST /api/action/team-up — boot a committed team behind one approval (#1736).

    The body names the team and the ``plan_sha256`` the operator reviewed; a
    changed plan is a 409 so nothing boots that was not previewed. A pending
    approval is a 202 with ``approval_id``; re-POST the same body once approved.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    team = body.get("team")
    plan_sha = body.get("plan_sha256")
    if not isinstance(team, str) or not team.strip():
        return _json({"error": "team is required"}, status=400)
    if not isinstance(plan_sha, str) or len(plan_sha) != 64:
        return _json({"error": "plan_sha256 of the reviewed plan is required"}, status=400)
    try:
        result = await api.team_up_payload(team.strip(), plan_sha, identity)
    except PermissionError as exc:
        # The gates wrap their refusals ``from`` the original, so a typed one
        # still maps (conflict: 409); an untyped refusal stays a 403.
        return _json_body_error(exc, status=403)
    except ToolError as exc:
        return _json_body_error(exc, status=400)
    if result.get("pending") or result.get("autonomy_review"):
        return _json({**result, "pending": True}, status=202)
    if result.get("stale_plan") or (result.get("booted") is False and "team_run_id" not in result):
        return _json({**result, "error": result.get("reason")}, status=409)
    return _json(result, status=201)


def _skills_teams_authority(identity: str | None, *, read_only: bool = False) -> Response | None:
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    profile = profiles.default_for_identity(identity)
    if (
        read_only
        and profile == profiles.OPERATOR
        and profiles.tool_is_allowed_for(profile, "crew_view")
    ):
        return None
    if not profiles.tool_is_allowed_for(profile, "skill_registry"):
        return _json(
            {"error": "skills catalog requires skill_registry profile authority"}, status=403
        )
    return None


async def _skills_teams(request: Request):
    identity = request.scope.get("ua_identity")
    denied = _skills_teams_authority(identity, read_only=True)
    if denied is not None:
        return denied
    try:
        result = await api.skills_teams_payload(
            identity, params={"surface_dir": request.query_params.get("surface_dir")}
        )
    except (ToolError, ValueError, OSError) as exc:
        return _json_body_error(exc, status=400)
    # A catalog read parked behind an approval is a 202, as on the POST sibling.
    if isinstance(result, dict) and result.get("pending"):
        return _json(result, status=202)
    return result


async def action_skills_teams(request: Request) -> Response:
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    denied = _skills_teams_authority(identity)
    if denied is not None:
        return denied
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("params", {}), dict):
        return _json({"error": "request body and params must be JSON objects"}, status=400)
    action = body.get("action")
    if not isinstance(action, str) or action not in {
        "publish",
        "install",
        "team_install",
        "starter_preview",
        "starter_install",
    }:
        return _json(
            {
                "error": "action must be publish, install, team_install, starter_preview or starter_install"
            },
            status=400,
        )
    profile = profiles.default_for_identity(identity)
    nested = (
        "fs_write"
        if action == "install"
        else "config_revision_manage"
        if action in {"team_install", "starter_install"}
        else "skill_registry"
    )
    if not profiles.tool_is_allowed_for(profile, nested):
        return _json({"error": f"{nested} profile authority required"}, status=403)
    try:
        result = await api.skills_teams_payload(
            identity, action=action, params=body.get("params", {})
        )
    except (ToolError, ValueError, OSError) as exc:
        return _json_body_error(exc, status=400)
    return _json(result, status=202 if result.get("pending") else 200)


async def _costs(request: Request):
    """GET /api/costs — per-agent cost rollup and budget status (task #1348).

    Read-only. `since`/`until` bound run finish times (default: this UTC month).
    """
    return await api.costs_payload(
        since=request.query_params.get("since") or None,
        until=request.query_params.get("until") or None,
    )


async def _tool_allowance(request: Request):
    """GET /api/tool-allowance — per-tool call allowance (task #2144).

    Tenant-fenced read: a caller sees the identity its calls are charged
    under; ``?agent=`` another identity (or ``*`` for the fleet) needs the
    verified operator on a human auth channel.
    """
    identity = _writer_identity(request)
    requested = (request.query_params.get("agent") or "").strip()
    if requested and requested != (identity or "") and not _operator_recovery(request):
        return _json(
            {
                "error": "tool allowance is per-caller: the verified operator "
                "may read another identity"
            },
            status=403,
        )
    return await api.tool_allowance_payload(agent=requested or None)


async def _scorecards(request: Request):
    """GET /api/scorecards — run cohort by claimed_at in the trailing window.

    Finishes after the window still count for claims opened within it. Open
    claims have no outcome until finished or inferred abandoned.
    """
    return await api.scorecards_payload(
        window_days=_float_param(request.query_params.get("window_days")) or 14.0
    )


# SQLite INTEGER (and so ROWID) is a signed 64-bit value.
_MAX_SEQ = 2**63 - 1
_BAD_SEQ_ERROR = f"after_seq must be an ASCII integer from 0 to {_MAX_SEQ}"


class _BadSeq(ValueError):
    """A malformed ``after_seq``: answered with 400, never a 500 (task #1683)."""


def _seq_param(raw: str | None) -> int | None:
    """The ``after_seq`` cursor: None when absent or empty, else 0..2**63-1.

    ``str.isdigit()`` alone accepts non-ASCII digits such as ``²`` that
    ``int()`` then rejects, and a value past 2**63-1 overflows SQLite, so
    anything else raises ``_BadSeq`` for the caller to turn into a 400.
    """
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if not (text.isascii() and text.isdigit()):
        raise _BadSeq(_BAD_SEQ_ERROR)
    # Leading zeros are harmless; the length check keeps int() off huge inputs.
    significant = text.lstrip("0") or "0"
    if len(significant) > 19 or int(significant) > _MAX_SEQ:
        raise _BadSeq(_BAD_SEQ_ERROR)
    return int(significant)


_BOTH_CURSORS_ERROR = "pass either since (ULID) or after_seq, not both"

_BAD_LAST_EVENT_ID_ERROR = "Last-Event-ID must be an event ULID or seq:<n>"
_SEQ_ID_PREFIX = "seq:"


def _last_event_id_cursor(raw: str | None) -> tuple[str | None, int | None]:
    """Map an SSE ``Last-Event-ID`` header to ``(since, after_seq)`` (task #1811).

    The stream emits a ULID as the id of each event and ``seq:<n>`` as the id of
    its opening cursor frame, so those are the two accepted forms. Absent or
    blank is no cursor. Anything else raises ``_BadSeq``, answered 400 like a bad
    ``after_seq``: silently ignoring a header the client did send would restart
    it at the log tail and drop the events in the gap, the bug this fixes.

    Contract (one rule, no extra query parameter): a valid header is the newest
    position and beats a ``since`` / ``after_seq`` query cursor, which is only
    where that URL's first connect starts; ``events_stream`` ignores a malformed
    header when the query supplies a cursor. Naming both query cursors stays a 400.
    """
    if raw is None or not raw.strip():
        return None, None
    text = raw.strip()
    if text.startswith(_SEQ_ID_PREFIX):
        try:
            seq = _seq_param(text[len(_SEQ_ID_PREFIX) :])
        except _BadSeq:
            seq = None
        if seq is None:
            raise _BadSeq(_BAD_LAST_EVENT_ID_ERROR)
        return None, seq
    if events.is_ulid(text):
        return text, None
    raise _BadSeq(_BAD_LAST_EVENT_ID_ERROR)


def _both_cursors(params) -> bool:
    # Whitespace-only after_seq is absent, as in _seq_param (Devin on #948).
    return bool(params.get("since")) and bool((params.get("after_seq") or "").strip())


async def _events(request: Request):
    params = request.query_params
    if _both_cursors(params):
        return _json({"error": _BOTH_CURSORS_ERROR}, status=400)
    try:
        after_seq = _seq_param(params.get("after_seq"))
    except _BadSeq as exc:
        return _json({"error": str(exc)}, status=400)
    channels = [c for c in params.get("channels", "").split(",") if c.strip()] or None
    channel_prefix = (params.get("channel_prefix") or "").strip() or None
    # The registered event_log_query runs every gate. The operator on a human
    # auth channel keeps the hub's human-recovery exemption from the agent-access
    # switch for this one read, as /api/ledger/rows and /api/events/stream do
    # (task #1811); profile, policy, approval and rate limits still apply.
    try:
        return await api.events_payload(
            channels=channels,
            since=params.get("since") or None,
            limit=_int_param(params.get("limit"), 100),
            after_seq=after_seq,
            operator_recovery=_writer_identity(request) if _operator_recovery(request) else None,
            item_id=(params.get("item_id") or "").strip() or None,
            channel_prefix=channel_prefix,
        )
    except approvals_tool.ApprovalPendingError as exc:
        return _json(
            {"approval_id": exc.approval_id, "code": exc.code, "status": "pending"}, status=202
        )
    except policy.PolicyViolation as exc:
        status = 429 if exc.reason_code in _THROTTLED_REASONS else 403
        return _json({"error": str(exc)[:300]}, status=status)
    except ToolError as exc:  # access, profile, trust-preset and denied-approval refusals
        return _json_body_error(exc, status=403)
    except Exception as exc:  # noqa: BLE001 - a failed read is not a refusal
        return _json_body_error(
            exc, status=500, message=f"event_log_query failed: {str(exc)[:200]}"
        )


async def ledger_rows(request: Request) -> Response:
    """GET /api/ledger/rows?after_seq=&limit= — raw event_log rows for verifiers (#1640).

    Rows come in ROWID (commit/chain) order with the stored payload string as
    ``payload_raw``, so an external verifier can re-hash the exact bytes the
    chain covers. Same gates as ``/api/events``: the read guard, then the
    ``event_log_query`` profile ceiling, agent access, policy and rate limit
    (``_governed_read``, task #1684).
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    if not profiles.tool_is_allowed_for(profiles.current(), "event_log_query"):
        return _json({"error": "event_log_query is unavailable to this profile"}, status=403)
    params = request.query_params
    try:
        after_seq = _seq_param(params.get("after_seq")) or 0
    except _BadSeq as exc:
        return _json({"error": str(exc)}, status=400)
    limit = _int_param(params.get("limit"), 100)
    # A malformed request is refused above without spending a rate-limit slot.
    admission = await _governed_read(
        request, "event_log_query", _read_gate_payload(after_seq=after_seq, limit=limit)
    )
    if isinstance(admission, Response):
        return admission
    # An allowed ledger read is not written to the event log. The route pages
    # that log to its tail, so an allow row per read would make every page,
    # even an empty one at the tail, create the row its successor returns, and
    # a verifier paging until an empty page would never finish. Refusals, parked
    # approvals (recorded by the gate) and failed reads are still recorded.
    try:
        rows = await anyio.to_thread.run_sync(lambda: events.query_ledger_rows(after_seq, limit))
    except BaseException as exc:
        await admission.record_failure(exc)
        raise
    return _json(
        {
            "rows": rows,
            "count": len(rows),
            "next_after_seq": rows[-1]["seq"] if rows else after_seq,
        }
    )


async def _telemetry(request: Request):
    params = request.query_params
    task_id = params.get("task_id")
    return await api.telemetry_payload(
        trace_id=params.get("trace_id") or None,
        task_id=int(task_id) if task_id and task_id.isdigit() else None,
        limit=_int_param(params.get("limit"), 500),
    )


async def _mobile_projection(request: Request):
    if not config.mobile_watch_enabled():
        return _json({"error": "mobile connector is disabled"}, status=404)
    params = request.query_params
    freshness = params.get("freshness", "live")
    error_code = params.get("error_code") or None
    return await api.mobile_projection_payload(
        freshness=freshness,
        error_code=error_code,
    )


async def action_mobile(request: Request) -> Response:
    """POST /api/mobile/v1/action — apply one `mobile-action.v1` request (#779).

    The Watch never calls this; the authenticated iPhone relay does, after a
    fresh hub auth. Mutating: same origin/auth/CSRF guard as every /api/action/*
    route.
    """
    if not config.mobile_watch_enabled():
        return _json({"error": "mobile connector is disabled"}, status=404)
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required for mobile actions"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)

    from ..projection import mobile_action_v1

    result = await api.mobile_action_apply(body, identity)
    if result.get("status") == "rejected":
        status = mobile_action_v1.REJECT_HTTP_STATUS.get(result.get("reason"), 400)
        return _json(result, status=status)
    return _json(result)


async def _langgraph_checkpoints(request: Request):
    params = request.query_params
    run_id = params.get("run_id") or None
    limit = _int_param(params.get("limit"), 20)
    return await api.langgraph_checkpoints_payload(run_id=run_id, limit=limit)


async def _capability_catalog(request: Request):
    return await api.capability_catalog_payload()


async def _evidence(request: Request):
    params = request.query_params
    task_id_raw = params.get("task_id") or params.get("task")
    if task_id_raw is not None and not task_id_raw.isdigit():
        # An explicitly invalid task filter must not silently degrade into a
        # broad unfiltered report.
        return _json({"error": f"invalid task_id: {task_id_raw!r}"}, status=400)
    task_id = int(task_id_raw) if task_id_raw else None
    since = params.get("since")
    until = params.get("until")
    agent = params.get("agent")
    project = params.get("project")
    pr = params.get("pr")
    item = (params.get("item") or "").strip() or None
    if item is not None:
        # Like task_id: a malformed item filter is a 400, never a broad report.
        from ..audit.evidence_report import parse_item_ref

        try:
            parse_item_ref(item)
        except ToolError as exc:
            return _json_body_error(exc, status=400, limit=None)
    limit = _int_param(params.get("limit"), 50)
    fmt = params.get("format", "json").lower()
    cursor_raw = params.get("cursor")
    if cursor_raw is not None and not (cursor_raw.isdigit() and int(cursor_raw) > 0):
        # Task #2516: the keyset cursor is a previous page's next_cursor.
        return _json({"error": f"invalid cursor: {cursor_raw!r}"}, status=400)

    try:
        payload = await api.evidence_payload(
            task_id=task_id,
            since=since,
            until=until,
            agent=agent,
            project=project,
            pr=pr,
            limit=limit,
            item=item,
            before_task_id=int(cursor_raw) if cursor_raw is not None else None,
        )
    except edition.FeatureNotAvailableError as exc:
        return _json({"error": str(exc)}, status=403)
    signing_enabled = evidence_signing.configured()
    if signing_enabled:
        try:
            payload = evidence_signing.with_report_metadata(payload)
        except RuntimeError as exc:
            return _json({"error": str(exc)}, status=503)

    def signed_response(body, media_type: str, headers: dict[str, str] | None = None):
        if not signing_enabled:
            return None
        chain = payload.get("chain_integrity") or {}
        try:
            sidecar = evidence_signing.sign_export(
                evidence_signing.encode_for_signing(body),
                chain_head=str(chain.get("head_hash") or ""),
                generated_at=str(payload.get("generated_at") or ""),
            )
        except RuntimeError as exc:
            return _json({"error": str(exc)}, status=503)
        response_headers = dict(headers or {})
        response_headers.update(evidence_signing.response_headers(sidecar))
        return Response(
            evidence_signing.encode_for_signing(body),
            media_type=media_type,
            headers=response_headers,
        )

    if fmt in ("markdown", "md"):
        from ..audit.evidence_report import format_evidence_report

        md = format_evidence_report(payload, fmt="markdown")
        signed = signed_response(md, "text/markdown; charset=utf-8")
        if signed:
            return signed
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8")
    if fmt == "text":
        from ..audit.evidence_report import format_evidence_report

        txt = format_evidence_report(payload, fmt="text")
        signed = signed_response(txt, "text/plain; charset=utf-8")
        if signed:
            return signed
        return PlainTextResponse(txt, media_type="text/plain; charset=utf-8")
    if fmt == "csv":
        from ..audit.evidence_report import format_evidence_report

        rows = format_evidence_report(payload, fmt="csv")
        signed = signed_response(
            rows,
            "text/csv; charset=utf-8",
            {"content-disposition": 'attachment; filename="evidence-report.csv"'},
        )
        if signed:
            return signed
        return PlainTextResponse(
            rows,
            media_type="text/csv; charset=utf-8",
            headers={"content-disposition": 'attachment; filename="evidence-report.csv"'},
        )
    if fmt == "pdf":
        from ..audit.evidence_report import format_evidence_report

        try:
            pdf_bytes = format_evidence_report(payload, fmt="pdf")
        except ValueError as exc:
            return _json({"error": str(exc)}, status=422)
        except RuntimeError as exc:
            return _json({"error": str(exc)}, status=503)
        signed = signed_response(
            pdf_bytes,
            "application/pdf",
            {"content-disposition": 'attachment; filename="evidence-report.pdf"'},
        )
        if signed:
            return signed
        return Response(
            pdf_bytes,
            media_type="application/pdf",
            headers={"content-disposition": 'attachment; filename="evidence-report.pdf"'},
        )

    if signing_enabled:
        from ..audit.evidence_report import format_evidence_report

        body = format_evidence_report(payload, fmt="json")
        signed = signed_response(body, "application/json")
        if signed:
            return signed
    return payload


async def _platform_manifest(request: Request):
    """Platform object registry as plain JSON, byte-identical to ``object_model.manifest()``."""
    return platform_http.manifest_payload()


async def platform_object(request: Request) -> Response:
    """GET one platform object by registry kind (+ id for id-addressed kinds).

    The same reader the MCP resource path resolves, under the same declared
    profile gate — a profile denied the object over MCP is denied here too
    (``ResourceError`` -> 403). Unknown kind, unserved kind, and unknown id
    are all 4xx, never 500.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    kind = request.path_params["kind"]
    object_id = request.path_params.get("object_id")
    try:
        payload = platform_http.read_object(kind, object_id)
    except platform_http.UnknownObjectKind as exc:
        return _json({"error": str(exc)}, status=404)
    except platform_http.ObjectNotServed as exc:
        return _json({"error": str(exc)}, status=404)
    except platform_http.ObjectRequestError as exc:
        return _json({"error": str(exc)}, status=400)
    except ResourceNotFoundError as exc:
        return _json({"error": str(exc)}, status=404)
    except ResourceError as exc:
        return _json({"error": str(exc)}, status=403)
    except Exception:  # noqa: BLE001 - bounded operator error; never leak internals
        log.exception("platform object read failed: kind=%r id=%r", kind, object_id)
        return _json({"error": "internal error reading platform object"}, status=500)
    return _json(payload)


async def platform_provenance_graph(request: Request) -> Response:
    """GET the provenance graph (task #1287): human->device->agent->action->
    proof as queryable graph objects, alongside ``/api/platform/objects``.

    Query params: ``query`` (``by_human`` | ``by_release``, or omitted for
    the task/project/time-window scope), ``task_id``, ``task_ids`` (comma
    separated), ``project_id``, ``since``, ``until``, ``human``, ``agent``,
    ``pr``, ``limit``. Same gate and error mapping as ``platform_object``.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    params = request.query_params
    try:
        payload = await anyio.to_thread.run_sync(
            lambda: platform_http.provenance_graph_payload(
                query=params.get("query"),
                task_id=params.get("task_id"),
                task_ids=params.get("task_ids"),
                project_id=params.get("project_id"),
                since=params.get("since"),
                until=params.get("until"),
                human=params.get("human"),
                agent=params.get("agent"),
                pr=params.get("pr"),
                limit=params.get("limit"),
            )
        )
    except platform_http.ObjectRequestError as exc:
        return _json({"error": str(exc)}, status=400)
    except ResourceNotFoundError as exc:
        return _json({"error": str(exc)}, status=404)
    except ResourceError as exc:
        return _json({"error": str(exc)}, status=403)
    except edition.FeatureNotAvailableError as exc:
        return _json({"error": str(exc)}, status=403)
    except Exception:  # noqa: BLE001 - bounded operator error; never leak internals
        log.exception("platform provenance graph read failed")
        return _json({"error": "internal error reading provenance graph"}, status=500)
    return _json(payload)


def _int_param(raw: str | None, default: int) -> int:
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


#: ``GET /api/agents/{label}?wait_for=connected`` (task #2523): the longest wait a
#: request may ask for, how often the wait re-reads the registry, and how many
#: waits the process holds at once (a further one answers 429).
AGENT_WAIT_MAX_S = 25
AGENT_WAIT_POLL_S = 1.0
AGENT_WAIT_MAX_WAITERS = 16
_agent_waiters_lock = threading.Lock()
_agent_waiters = 0


def _agent_wait_params(request: Request) -> tuple[float, str | None] | Response | None:
    """``(timeout_s, since)`` for a first-connection wait, None for a plain read, or 400."""
    params = request.query_params
    wait_for = params.get("wait_for")
    if wait_for is None:
        if "timeout" in params or "since" in params:
            return _json({"error": "timeout and since need wait_for=connected"}, status=400)
        return None
    if wait_for != "connected":
        return _json({"error": "wait_for must be connected"}, status=400)
    raw = params.get("timeout", str(AGENT_WAIT_MAX_S))
    try:
        timeout = int(raw)
    except ValueError:
        timeout = -1
    if not 0 <= timeout <= AGENT_WAIT_MAX_S:
        return _json(
            {"error": f"timeout must be an integer from 0 to {AGENT_WAIT_MAX_S} seconds"},
            status=400,
        )
    since = params.get("since")
    if since is not None:
        try:
            parsed = datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
        except ValueError:
            return _json({"error": "since must be an ISO 8601 datetime"}, status=400)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        # storage.now() form, so the comparison with last_seen is like for like.
        since = parsed.astimezone(UTC).isoformat(timespec="seconds")
    return float(timeout), since


@contextlib.contextmanager
def _agent_waiter():
    """Hold one of ``AGENT_WAIT_MAX_WAITERS`` slots; yields False when none is free."""
    global _agent_waiters
    with _agent_waiters_lock:
        admitted = _agent_waiters < AGENT_WAIT_MAX_WAITERS
        if admitted:
            _agent_waiters += 1
    try:
        yield admitted
    finally:
        if admitted:
            with _agent_waiters_lock:
                _agent_waiters -= 1


async def agent_detail(request: Request) -> Response:
    """GET /api/agents/{label} — one agent's detail; optionally wait for its first connection.

    A label the hub's tenant knows but has never seen answers 200 with
    ``connection: never_connected`` (task #2523); any other unknown label is 404.
    ``?wait_for=connected[&timeout=<0-25>][&since=<iso>]`` holds the answer until
    the hub records a sighting (at or after ``since`` when given) or the timeout
    passes, re-reading the registry every ``AGENT_WAIT_POLL_S`` seconds.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    wait = _agent_wait_params(request)
    if isinstance(wait, Response):
        return wait
    from .. import agent_enrollment

    label = request.path_params["label"]
    detail = await api.agent_detail(label)
    if detail is None:
        return _json({"error": "unknown agent"}, status=404)
    if wait is None:
        return _json(detail)
    timeout, since = wait
    started = time.monotonic()
    row = detail["agent"] if detail["connection"] == "connected" else None
    satisfied = agent_enrollment.seen_since(row, since)
    if not satisfied and timeout > 0:
        with _agent_waiter() as admitted:
            if not admitted:
                return _json(
                    {
                        "error": "too many agent reads are waiting; retry shortly",
                        "code": "rate_limited",
                    },
                    status=429,
                )
            deadline = started + timeout
            while not satisfied and time.monotonic() < deadline:
                await anyio.sleep(min(AGENT_WAIT_POLL_S, max(0.0, deadline - time.monotonic())))
                if await request.is_disconnected():
                    return Response(status_code=204)
                row = await anyio.to_thread.run_sync(registry.get_agent, label)
                satisfied = agent_enrollment.seen_since(row, since)
        # One full read at the end, so the answer is the detail as it now stands.
        detail = await api.agent_detail(label) or detail
    detail["wait"] = {
        "for": "connected",
        "since": since,
        "timeout_s": int(timeout),
        "satisfied": satisfied,
        "waited_s": round(time.monotonic() - started, 3),
    }
    return _json(detail)


async def session_detail(request: Request) -> Response:
    guard = _guard(request)
    if guard is not None:
        return guard
    denied = _require_session_reader()
    if denied is not None:
        return denied
    session = await api.session_detail_payload(request.path_params["id"])
    if session is None:
        return _json({"error": "unknown session"}, status=404)
    return _json(session)


async def session_wake_grant(request: Request) -> Response:
    """Operator-only: grant or revoke one session's idle wakes, ``{grant: bool}`` (#2050).

    A session's own ``wake: true`` is only a request, because the hook and the
    agent share the harness bearer. The grant needs a credential the agent does
    not hold: the verified operator (``_operator_recovery``) on a human channel.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    if not _operator_recovery(request):
        return _json({"error": "granting idle wakes is an operator action"}, status=403)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - {"grant"})
    if unknown:
        return _json({"error": "unknown field(s): " + ", ".join(unknown)}, status=400)
    grant = body.get("grant")
    if not isinstance(grant, bool):
        return _json({"error": "grant must be a boolean"}, status=400)
    result = await anyio.to_thread.run_sync(
        lambda: message_wake.set_grant(request.path_params["id"], grant, actor=str(identity))
    )
    if result is None:
        return _json({"error": "unknown session"}, status=404)
    return _json(result)


async def session_crew_live(request: Request) -> Response:
    """GET /api/sessions/{id}/crew — live messages + worktree intervention drop."""
    guard = _guard(request)
    if guard is not None:
        return guard
    denied = _require_session_reader()
    if denied is not None:
        return denied
    from mcp.server.mcpserver.exceptions import ToolError

    limit = _int_param(request.query_params.get("limit"), 50)
    try:
        return _json(await api.crew_live_payload(request.path_params["id"], message_limit=limit))
    except ToolError as exc:
        msg = str(exc)
        status = 404 if "unknown session" in msg else 400
        return _json_body_error(exc, status=status)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)


async def events_stream(request: Request) -> Response:
    guard = _guard(request)
    if guard is not None:
        return guard
    # HTTP has no MCP session: use the named profile ceiling before the raw
    # generator can read replay rows or reserve a live stream slot.
    if not profiles.tool_is_allowed_for(profiles.current(), "event_log_query"):
        return _json({"error": "event_log_query is unavailable to this profile"}, status=403)
    from sse_starlette.sse import EventSourceResponse

    params = request.query_params
    if _both_cursors(params):
        return _json({"error": _BOTH_CURSORS_ERROR}, status=400)
    try:
        after_seq = _seq_param(params.get("after_seq"))
    except _BadSeq as exc:
        return _json({"error": str(exc)}, status=400)
    channels = [c for c in params.get("channels", "").split(",") if c.strip()] or None
    channels = events.merge_channel_prefix(channels, params.get("channel_prefix"))
    since = params.get("since") or None
    # A browser EventSource sends ``Last-Event-ID`` only when it reconnects the
    # same EventSource, with the id of the last frame that stream delivered. That
    # is never older than the cursor in the stream's own URL (a client that
    # re-opened a refused stream puts its resume cursor there), so a valid header
    # takes precedence over the query cursor. A malformed header is ignored when a
    # query cursor is present and is a 400 when it is the only cursor, so it never
    # silently restarts the stream at the tail.
    try:
        header_since, header_seq = _last_event_id_cursor(request.headers.get("last-event-id"))
    except _BadSeq as exc:
        if since is None and after_seq is None:
            return _json({"error": str(exc)}, status=400)
    else:
        if header_since is not None or header_seq is not None:
            since, after_seq = header_since, header_seq
    # Admission runs before the generator exists: a refused caller never reads
    # replay rows or holds a live stream slot (task #1684). The cursor stays in
    # the gate payload on purpose: where a stream starts decides how much history
    # an approval exposes, so a pass for a live-tail stream must not also cover a
    # replay from 0. The cost is that, under ``UA_APPROVAL_TOOLS``, each reconnect
    # from a new ``Last-Event-ID`` is a different payload and parks a new approval.
    admission = await _governed_read(
        request,
        "event_log_query",
        _read_gate_payload(channels=channels, since=since, after_seq=after_seq),
    )
    if isinstance(admission, Response):
        return admission
    on_open = _stream_on_open(admission, _writer_identity(request))
    # The slot is taken here, before a response exists, so a full house is an
    # HTTP 429 and not a 200 whose first frame is an error (task #1811). The
    # decision row is the same ``stream_capacity`` denial the in-stream refusal
    # recorded, still coalesced per caller (``_CapacityDenials``).
    slot = sse.reserve_stream()
    if slot is None:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(
                on_open, sse.StreamCapacityError("too many concurrent streams")
            )
        return _json(
            {"error": "too many concurrent streams"},
            status=429,
            headers={"Retry-After": str(math.ceil(sse.CAPACITY_RETRY_MS / 1000))},
        )
    # A stream has no completed result to wait for. Its one decision is
    # recorded by the generator through ``on_open``: allow once its first replay
    # query succeeded, otherwise the startup, replay or disconnect error that
    # stopped it first. The slot is the stream's from here on; the generator
    # releases it when it ends, and the background task and finalizer cover a
    # response that ends, or is dropped, before the generator ever starts.
    try:
        stream = sse.event_stream(
            channels=channels,
            since=since,
            after_seq=after_seq,
            on_open=on_open,
            slot=slot,
        )
        weakref.finalize(stream, slot.release)
        return EventSourceResponse(stream, background=BackgroundTask(slot.release))
    except BaseException as exc:
        slot.release()
        await admission.record_failure(exc)
        raise


async def _agent_graph(request: Request):
    return await api.agent_graph_payload()


async def _zulip(request: Request):
    return await api.zulip_coordination_payload()


async def _execution_graph(request: Request):
    raw = request.query_params.get("task")
    if raw is None:
        return await api.execution_graph_payload()
    return await api.lineage_payload(raw)


async def work_graph_view(request: Request) -> Response:
    """GET /api/work-graph — goal -> plan -> task -> run/session/evidence (task #1460).

    Query params: ``goal_id`` (omit for a page of root goals only),
    ``cursor`` (keyset, from a prior response's ``next_cursor``), ``page_size``
    (bounded). Requires task visibility; other families appear only when their
    profile reader is available. See ``work_graph.py``
    for the edge vocabulary and the relations this projection does not invent.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    readers = _work_graph_readers()
    if isinstance(readers, Response):
        return readers
    include_dispatch, include_cycles, include_sessions, include_plans, include_agents = readers
    from ..work_graph import work_graph

    params = request.query_params
    goal_id_raw = params.get("goal_id")
    try:
        goal_id = int(goal_id_raw) if goal_id_raw not in (None, "") else None
    except ValueError:
        return _json({"error": "goal_id must be an integer"}, status=400)
    page_size = _int_param(params.get("page_size"), 50)
    try:
        payload = await anyio.to_thread.run_sync(
            lambda: work_graph(
                goal_id=goal_id,
                cursor=params.get("cursor"),
                page_size=page_size,
                include_dispatch_runs=include_dispatch,
                include_self_pickup_runs=include_dispatch,
                include_cycles=include_cycles,
                include_sessions=include_sessions,
                include_plans=include_plans,
                include_agent_metadata=include_agents,
                unavailable_readers=[
                    label
                    for allowed, label in (
                        (include_dispatch, "dispatch_report_self"),
                        (include_cycles, "autonomous_cycle_list/get"),
                        (include_sessions, "session_list"),
                        (include_plans, "plan_get"),
                        (include_agents, "agent_list"),
                    )
                    if not allowed
                ],
            )
        )
    except ToolError as exc:
        message = str(exc)
        status = (
            404
            if "unknown goal" in message
            else 413
            if "goal subtree exceeds maximum scope" in message
            else 400
        )
        return _json_body_error(exc, status=status)
    return _json(payload)


async def _cycles(request: Request):
    state = request.query_params.get("state")
    limit = _int_param(request.query_params.get("limit"), 100)
    return await api.cycles_payload(state=state, limit=limit)


async def _plans(request: Request):
    status = request.query_params.get("status") or "all"
    scope = request.query_params.get("scope")
    limit = _int_param(request.query_params.get("limit"), 50)
    return await api.plans_payload(status=status, scope=scope, limit=limit)


async def _proposals(request: Request):
    """GET /api/proposals — pending learning / skill-cluster proposals (task #1148)."""
    return await api.proposals_payload()


async def _org_proposals(request: Request):
    """GET /api/org-proposals — self-organization proposals (task #1431)."""
    guard = _guard(request)
    if guard is not None:
        return guard
    denied = _require_task_list()
    if denied is not None:
        return denied
    status = request.query_params.get("status")
    scope = request.query_params.get("scope")
    author = request.query_params.get("author")
    limit = _int_param(request.query_params.get("limit"), 50)
    return await api.org_proposals_payload(status=status, scope=scope, author=author, limit=limit)


async def org_proposal_detail(request: Request) -> Response:
    """GET /api/org-proposals/{proposal_id} (task #1431)."""
    guard = _guard(request)
    if guard is not None:
        return guard
    denied = _require_task_list()
    if denied is not None:
        return denied
    proposal_id = str(request.path_params.get("proposal_id") or "").strip()
    if not proposal_id:
        return _json({"error": "missing proposal id"}, status=400)
    data = await api.org_proposal_detail(proposal_id)
    if "error" in data:
        return _json(data, status=404)
    return _json(data)


async def plan_detail(request: Request) -> Response:
    """GET /api/plans/{plan_id} — steps, owners, outputs, status (task #1117)."""
    guard = _guard(request)
    if guard is not None:
        return guard
    from mcp.server.mcpserver.exceptions import ToolError

    plan_id = str(request.path_params.get("plan_id") or "").strip()
    if not plan_id:
        return _json({"error": "missing plan id"}, status=400)
    try:
        return _json(await api.plan_detail(plan_id))
    except ToolError as exc:
        msg = str(exc)
        status = 404 if "no plan with id" in msg or "unknown plan" in msg else 400
        return _json_body_error(exc, status=status)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)


async def cycle_detail(request: Request) -> Response:
    guard = _guard(request)
    if guard is not None:
        return guard
    try:
        return _json(await api.cycle_detail(request.path_params["cycle_id"]))
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=404, limit=200)


async def action_plan(request: Request) -> Response:
    """POST /api/action/plan — CSRF-gated ``plan_manage('review')`` (task #1117).

    Read + review only: the pane resolves the existing human plan-confirmation
    gate; it does not create or edit plans.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    from mcp.server.mcpserver.exceptions import ToolError

    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    intent = body.get("intent")
    if not intent or not isinstance(intent, str) or not intent.strip():
        return _json({"error": "missing or invalid intent"}, status=400)
    task_id_raw = body.get("task_id")
    task_id: int | None = None
    if task_id_raw is not None:
        try:
            task_id = int(task_id_raw)
        except (ValueError, TypeError):
            return _json({"error": "invalid task_id; expected integer"}, status=400)
    try:
        res = await api.review_plan(intent=intent.strip(), task_id=task_id)
    except ToolError as exc:
        return _json_body_error(exc, status=400)
    except (KeyError, ValueError, TypeError) as exc:
        return _json({"error": str(exc)[:200]}, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)
    return _json(res)


async def action_proposal(request: Request) -> Response:
    """POST /api/action/proposal — CSRF-gated file_task | dismiss (task #1148).

    Human review surface only. Never auto-invoked from sleep-pass / cron.
    dismiss marks vault ingest; it does not rewrite proposal content.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    from mcp.server.mcpserver.exceptions import ToolError

    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    action = body.get("action")
    if not isinstance(action, str) or action not in api.PROPOSAL_ACTIONS:
        return _json({"error": "action must be file_task or dismiss"}, status=400)
    proposal_id = body.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id.strip():
        return _json({"error": "missing or invalid proposal_id"}, status=400)
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        return _json({"error": "reason must be a string"}, status=400)
    try:
        res = await api.proposal_action(
            action=action,
            proposal_id=proposal_id.strip(),
            agent=str(identity),
            reason=reason,
        )
    except ToolError as exc:
        return _json_body_error(exc, status=400)
    except (KeyError, ValueError, TypeError) as exc:
        return _json({"error": str(exc)[:200]}, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)
    return _json(res)


async def _cycle_start_keyed(identity: str, key: str, start_args: dict) -> Response:
    """A cycle start under an ``Idempotency-Key`` (task #2516, ``respond_once``)."""
    from .cc_knowledge import respond_once

    async def start() -> Response:
        # A refusal is an answer (nothing started); anything else propagates, so the
        # key records the start's outcome as unknown.
        try:
            result = await api.cycle_start(agent=identity, **start_args)
        except (KeyError, TypeError, ValueError) as exc:
            return _json({"error": f"invalid cycle request: {exc}"}, status=400)
        except ToolError as exc:
            return _json_body_error(exc, status=400, limit=200)
        return _json(result, status=201)

    try:
        return await respond_once(
            identity,
            key,
            "autonomous_cycle_start",
            start_args,
            start,
            check="the cycle list (GET /api/cycles)",
        )
    except Exception as exc:  # noqa: BLE001 - bounded operator error; the key is settled
        return _json_body_error(exc, status=400, limit=200)


async def action_cycle(request: Request) -> Response:
    """POST /api/action/cycle — start, transition, record usage or kill a cycle.

    ``start`` accepts an optional ``Idempotency-Key`` (task #2516); ``transition``
    keeps its own ``idempotency_key`` body field.
    """
    from .cc_knowledge import _idempotency_key

    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        operation = body.get("operation", "transition")
        if operation == "start":
            budgets = body.get("budgets")
            capabilities = body.get("capabilities")
            if budgets is not None and not isinstance(budgets, dict):
                raise ValueError("budgets must be an object")
            if capabilities is not None and not isinstance(capabilities, list):
                raise ValueError("capabilities must be a list")
            start_args = {
                "task_id": body.get("task_id"),
                "plan_id": body.get("plan_id"),
                "delegation_id": body.get("delegation_id"),
                "max_rework": int(body.get("max_rework", 2)),
                "budgets": budgets,
                "capabilities": capabilities,
            }
            key = _idempotency_key(request)
            if isinstance(key, Response):
                return key
            if key is not None:
                return await _cycle_start_keyed(identity, key, start_args)
            result = await api.cycle_start(agent=identity, **start_args)
        elif operation == "transition":
            cycle_id = str(body.get("cycle_id") or "").strip()
            to_state = str(body.get("to_state") or "").strip()
            key = str(body.get("idempotency_key") or "").strip()
            if not cycle_id or not to_state or not key:
                raise ValueError("cycle_id, to_state, and idempotency_key are required")
            result = await api.cycle_transition(
                agent=identity,
                cycle_id=cycle_id,
                to_state=to_state,
                idempotency_key=key,
                expected_version=body.get("expected_version"),
                reason=body.get("reason"),
                pr_url=body.get("pr_url"),
                capability=body.get("capability"),
            )
        elif operation == "usage":
            cycle_id = str(body.get("cycle_id") or "").strip()
            if not cycle_id:
                raise ValueError("cycle_id is required")
            result = await api.cycle_record_usage(
                agent=identity,
                cycle_id=cycle_id,
                elapsed_s=float(body.get("elapsed_s", 0)),
                tokens=int(body.get("tokens", 0)),
                retries=int(body.get("retries", 0)),
                cost_cents=float(body.get("cost_cents", 0)),
                capability=body.get("capability"),
            )
        elif operation == "kill":
            cycle_id = str(body.get("cycle_id") or "").strip()
            reason = str(body.get("reason") or "").strip()
            if not cycle_id or not reason:
                raise ValueError("cycle_id and reason are required to kill a cycle")
            result = await api.cycle_kill(
                agent=identity,
                cycle_id=cycle_id,
                reason=reason,
            )
        else:
            raise ValueError("operation must be start, transition, usage, or kill")
    except (KeyError, TypeError, ValueError) as exc:
        return _json({"error": f"invalid cycle request: {exc}"}, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)
    return _json(result, status=201 if operation == "start" else 200)


def _guard_mutation(request: Request) -> Response | None:
    """Precondition for mutating routes: origin check, auth, and CSRF token validation.

    CSRF is enforced whenever a session cookie is present, regardless of whether
    ``UA_ENFORCE_AUTH`` is on.  In warn mode the hub lets unauthenticated
    requests through (``ua_identity_via`` may be ``None``), but a browser that
    sends a cookie must also send the matching CSRF token — otherwise an
    attacker page can ride the cookie without the user's knowledge.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    # Enforce CSRF when the request carries a session cookie, not just when the
    # middleware resolved it to an identity.  This closes the gap where warn
    # mode leaves ua_identity_via=None even though a cookie is present.
    has_cookie = bool(request.cookies.get(auth.COOKIE_NAME))
    if request.scope.get("ua_identity_via") == "cookie" or has_cookie:
        csrf_header = request.headers.get(auth.CSRF_HEADER) or request.headers.get("x-ua-csrf")
        expected_csrf = request.scope.get("ua_csrf")
        if not expected_csrf:
            session = auth.resolve_session(request.cookies.get(auth.COOKIE_NAME))
            expected_csrf = session["csrf"] if session else None
        if not auth.csrf_ok(expected_csrf, csrf_header):
            return _json({"error": "invalid or missing CSRF token"}, status=403)
    return None


async def action_approval(request: Request) -> Response:
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - {"id", "decision", "reason"})
    if unknown:
        return _json({"error": "unknown approval field(s): " + ", ".join(unknown)}, status=400)
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    if config.actor_kind_for_identity(identity) != "human":
        return _json({"error": "approval resolution requires a human identity"}, status=403)
    approval_id_raw = body.get("id")
    if approval_id_raw is None:
        return _json({"error": "missing approval id"}, status=400)
    try:
        approval_id = int(approval_id_raw)
    except (ValueError, TypeError):
        return _json({"error": "invalid approval id; expected integer"}, status=400)
    decision = body.get("decision", "approve")
    if decision not in ("approve", "deny"):
        return _json({"error": "invalid decision; expected approve or deny"}, status=400)
    try:
        res = await api.resolve_platform_approval(
            approval_id=approval_id,
            decision=decision,
            actor=identity,
            reason=str(body.get("reason", "via HTTP API")),
        )
    except (KeyError, ValueError) as exc:
        return _json({"error": str(exc)}, status=400)
    except agent_access.AgentAccessDenied as exc:
        return _json({"error": str(exc)}, status=403)
    except ResourceError as exc:
        return _json({"error": str(exc)}, status=403)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)
    return _json(res)


async def approval_ledger_rerequest(request: Request) -> Response:
    """POST /api/approvals/ledger-rerequest — open a missed ledger row (task #1854).

    Body: ``{"number": int, "head_sha": str}``. ``github_app_worker`` authorizes
    the caller (a lifecycle operator, or the verified human operator on a
    recovery channel; 403 otherwise), checks the head is still the PR's head and
    queues the approval row. It never resolves the approval.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = _writer_identity(request)
    if identity is None:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - {"number", "head_sha"})
    if unknown:
        return _json({"error": f"unknown field(s): {', '.join(unknown)}"}, status=400)
    number = body.get("number")
    head_sha = body.get("head_sha")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return _json({"error": "number must be a positive integer"}, status=400)
    if not isinstance(head_sha, str) or not head_sha.strip():
        return _json({"error": "head_sha must be a non-empty string"}, status=400)
    from .. import github_app_worker

    grant = (
        github_app_worker.human_operator_rerequest(identity)
        if _operator_recovery(request)
        else contextlib.nullcontext()
    )
    # Through the registered coordination_manage action, as the MCP twin: the
    # same profile ceiling, policy gate (rules on coordination_manage and on
    # approval_ledger_rerequest) and approval gate, for the verified caller.
    try:
        with grant:
            res = await api.call_tool(
                "coordination_manage",
                action="approval_ledger_rerequest",
                params={"number": number, "head_sha": head_sha},
            )
    except approvals_tool.ApprovalPendingError as exc:
        return _json(
            {
                "approval_id": exc.approval_id,
                "code": exc.code,
                "status": "pending",
                "tool": "approval_ledger_rerequest",
            },
            status=202,
        )
    except (github_app_worker.RerequestAuthorityError, policy.PolicyViolation) as exc:
        return _json({"error": str(exc)[:300]}, status=403)
    except ToolError as exc:
        return _json_body_error(exc, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error (GitHub, worker bearer)
        return _json_body_error(exc, status=502, message=f"{type(exc).__name__}: {str(exc)[:200]}")
    return _json(res)


async def approval_ledger_poll(request: Request) -> Response:
    """POST /api/approvals/ledger-poll — run one Approval Ledger poll now (task #1867).

    Body: empty or ``{}``. ``github_app_worker.poll_now`` authorizes the caller
    (a lifecycle operator, or the verified human operator on a recovery
    channel; 403 otherwise) and runs the scheduled poll once. It requests
    approval rows and never resolves one.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = _writer_identity(request)
    if identity is None:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    if body:
        return _json({"error": f"unknown field(s): {', '.join(sorted(body))}"}, status=400)
    from .. import github_app_worker

    grant = (
        github_app_worker.human_operator_grant(identity)
        if _operator_recovery(request)
        else contextlib.nullcontext()
    )
    # Through the registered coordination_manage action, as the MCP twin: the
    # same profile ceiling, policy gate and approval gate for the verified caller.
    try:
        with grant:
            res = await api.call_tool(
                "coordination_manage", action="approval_ledger_poll", params={}
            )
    except approvals_tool.ApprovalPendingError as exc:
        return _json(
            {
                "approval_id": exc.approval_id,
                "code": exc.code,
                "status": "pending",
                "tool": "approval_ledger_poll",
            },
            status=202,
        )
    except (github_app_worker.RerequestAuthorityError, policy.PolicyViolation) as exc:
        return _json({"error": str(exc)[:300]}, status=403)
    except ToolError as exc:
        return _json_body_error(exc, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error (GitHub, worker bearer)
        return _json_body_error(exc, status=502, message=f"{type(exc).__name__}: {str(exc)[:200]}")
    return _json(res)


#: 2000 paths of 500 characters, plus the other fields of one read.
REVIEW_READ_BODY_MAX = 1_100_000
_REVIEW_READ_FIELDS = frozenset(
    {
        "repo",
        "number",
        "head_sha",
        "kind",
        "verdict",
        "from_sha",
        "files",
        "evidence_url",
        "expected_reader",
        "dry_run",
    }
)


def _publish_review_head_after_read(fields: dict) -> dict:
    """Best-effort ``unified-agent/review-head`` for a read that just committed.

    The Approval Ledger poll only publishes open heads, so a read recorded
    after a merge had no publisher and its head kept a false ERROR (task
    #2410). This runs after the row is committed and never rolls it back: a
    failure is reported as ``review_head: {status: error}``.
    """
    from .. import github_app_worker

    try:
        return github_app_worker.publish_review_head(
            repo=fields["repo"], number=fields["number"], head_sha=fields["head_sha"]
        )
    except Exception as exc:  # noqa: BLE001 - the read is durable; this only reports
        log.warning("review-head publish after a recorded read failed: %s", type(exc).__name__)
        return {"status": "error", "error": type(exc).__name__}


async def review_read_record(request: Request) -> Response:
    """POST /api/review/reads — record one explicit review read (task #2407).

    The reader is the verified caller and is never taken from the body;
    ``expected_reader`` only refuses a credential that is not the one the
    client meant to use. ``review_cli.explicit_read_refusal`` applies the same
    rule as the hub-shell CLI, so a head that is not live is refused here too.
    Answers the row id, so the caller confirms the write instead of inferring it.

    The row is published to ``unified-agent/review-head`` once it commits, so a
    read recorded after the merge still clears the check (task #2410). A failed
    publish is reported in ``review_head`` and never undoes the row.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = _writer_identity(request)
    if identity is None:
        return _json({"error": "authenticated identity required"}, status=401)
    raw, too_large = await _read_capped_body(request, REVIEW_READ_BODY_MAX)
    if too_large is not None:
        return too_large
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    if "reader" in body:
        return _json(
            {"error": "the reader is the authenticated identity; do not name one"}, status=400
        )
    unknown = sorted(set(body) - _REVIEW_READ_FIELDS)
    if unknown:
        return _json({"error": f"unknown field(s): {', '.join(unknown)[:200]}"}, status=400)
    expected = body.get("expected_reader")
    if expected is not None and expected != identity:
        return _json(
            {"error": f"this credential is {identity!r}, not {str(expected)[:120]!r}"},
            status=403,
        )
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return _json({"error": "dry_run must be a boolean"}, status=400)
    from .. import review_reads

    kind = body.get("kind", review_reads.STEWARD_READ)
    refused = review_reads.caller_refusal(identity, kind)
    if refused:
        return _json({"error": refused[:300]}, status=403)
    fields = {
        "repo": body.get("repo"),
        "number": body.get("number"),
        "head_sha": body.get("head_sha"),
        "kind": kind,
        "reader": identity,
        "verdict": body.get("verdict", "pass"),
        "from_sha": body.get("from_sha"),
        "files": body.get("files"),
        "evidence_url": body.get("evidence_url"),
    }

    def work() -> tuple[str | None, int | None, dict | None]:
        from .. import review_cli

        reason = review_cli.explicit_read_refusal(**fields)
        if reason or dry_run:
            return reason, None, None
        row_id = review_reads.record(**fields)
        # Only after the row is durable: a publish failure must never lose it.
        return None, row_id, _publish_review_head_after_read(fields)

    try:
        reason, row_id, review_head = await anyio.to_thread.run_sync(work)
    except ValueError as exc:
        return _json({"error": str(exc)[:500]}, status=409)
    except (sqlite3.Error, storage.LiveStoreError) as exc:
        log.warning("review read not recorded: %s", type(exc).__name__)
        return _json({"error": "could not record the read; try again"}, status=503)
    if reason:
        return _json({"error": reason[:500]}, status=409)
    return _json(
        {
            "status": "dry-run" if dry_run else "recorded",
            "id": row_id,
            "repo": fields["repo"],
            "number": fields["number"],
            "head_sha": fields["head_sha"],
            "kind": kind,
            "reader": identity,
            "review_head": review_head,
        }
    )


async def action_approval_request(request: Request) -> Response:
    """Queue an approval request without executing or consuming its one-shot pass."""
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - {"tool", "payload"})
    if unknown:
        return _json(
            {"error": "unknown approval request field(s): " + ", ".join(unknown)}, status=400
        )
    tool = body.get("tool")
    # ApprovalRequest.payload defaults to {} in the SDK contract.
    payload = body.get("payload", {})
    if not isinstance(tool, str) or not tool.strip():
        return _json({"error": "tool is required"}, status=400)
    tool = tool.strip()
    if not isinstance(payload, dict):
        return _json({"error": "payload must be a JSON object"}, status=400)
    from .. import profiles
    from .. import server as server_module
    from ..dispatch_reads import DISPATCH_APPROVAL_TOOL

    if tool not in server_module.REGISTERED_TOOLS and tool != DISPATCH_APPROVAL_TOOL:
        return _json({"error": f"unknown tool: {tool}"}, status=400)
    if not profiles.tool_is_allowed_for(profiles.current(), "approval_pending"):
        return _json(
            {"error": "approval requests are not exposed by the active profile"}, status=403
        )
    try:
        agent_access.enforce("approval_request", read_only=False)
        if tool == DISPATCH_APPROVAL_TOOL:
            from .dispatch_approval import request_existing_dispatch_approval

            result = await anyio.to_thread.run_sync(
                lambda: request_existing_dispatch_approval(payload, requested_by=identity)
            )
        else:
            result = await anyio.to_thread.run_sync(
                lambda: object_model.by_kind("approval").writer("request")(
                    tool,
                    payload,
                    requested_by=identity,
                    request_only=True,
                )
            )
    except agent_access.AgentAccessDenied as exc:
        return _json({"error": str(exc)}, status=403)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)
    if not result.get("gated"):
        return _json({"error": f"tool {tool!r} is not configured to require approval"}, status=409)
    response = {
        "approval_id": result["approval_id"],
        "status": result["status"],
        "tool": tool,
        "payload": payload,
    }
    if result.get("reason"):
        response["reason"] = result["reason"]
    # A denied decision is a valid typed ApprovalRequestResult, not a
    # transport error: return it with 200 like an existing approved one, so
    # SDK callers get the decision instead of a HubTransportError.
    status = 202 if result["status"] == "pending" else 200
    # The SDK's ApprovalRequestResult: the request succeeded; nothing was refused.
    return _json(response, status=status, approval_code=False)


async def task_create(request: Request) -> Response:
    """Create a task through the same registered policy wrapper as MCP."""
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    if not request.scope.get("ua_identity"):
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    allowed = {
        "title",
        "notes",
        "priority",
        "parent_id",
        "untrusted",
        "project",
        "domain",
        "due_at",
        "evidence_ref",
        "idempotency_key",
        "lane",
    }
    unknown = sorted(set(body) - allowed)
    if unknown:
        return _json({"error": "unknown task_add field(s): " + ", ".join(unknown)}, status=400)
    if not isinstance(body.get("title"), str) or not body["title"].strip():
        return _json({"error": "title is required"}, status=400)
    try:
        result = await api.call_tool("task_add", **body)
    except approvals_tool.ApprovalPendingError as exc:
        return _json(
            {
                "approval_id": exc.approval_id,
                "code": exc.code,
                "status": "pending",
                "tool": "task_add",
                "payload": body,
            },
            status=202,
        )
    except agent_access.AgentAccessDenied as exc:
        return _json({"error": str(exc)}, status=403)
    except ResourceError as exc:
        return _json({"error": str(exc)}, status=403)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400)
    return _json(result, status=201)


async def _work_product_write(body: dict, *, success_status: int) -> Response:
    """Run the registered tool, including its profile, policy and approval gates."""
    from .. import profiles

    if not profiles.tool_is_allowed_for(profiles.current(), "work_product_submit"):
        return _json(
            {"error": "work product submission is unavailable to this profile"}, status=403
        )
    try:
        result = await api.call_tool("work_product_submit", **body)
    except approvals_tool.ApprovalPendingError as exc:
        approval = (
            await anyio.to_thread.run_sync(approvals_tool.get_approval, exc.approval_id)
            if exc.approval_id is not None
            else None
        )
        payload = approval.get("payload") if isinstance(approval, dict) else None
        return _json(
            {
                "status": "pending",
                "approval_id": exc.approval_id,
                "code": exc.code,
                "work_product_id": payload.get("work_product_id")
                if isinstance(payload, dict)
                else None,
            },
            status=202,
        )
    except agent_access.AgentAccessDenied as exc:
        return _json({"error": str(exc)}, status=403)
    except ResourceError as exc:
        return _json({"error": str(exc)}, status=403)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400)
    return _json(result, status=success_status)


async def work_product_submit(request: Request) -> Response:
    """Submit a workspace file through the same registered tool used by MCP."""
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    if not request.scope.get("ua_identity"):
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - {"source_path", "title", "template_pack", "tags", "model"})
    if unknown:
        return _json({"error": "unknown work product field(s): " + ", ".join(unknown)}, status=400)
    for field in ("source_path", "title", "template_pack"):
        if not isinstance(body.get(field), str) or not body[field].strip():
            return _json({"error": f"{field} is required"}, status=400)
    tags = body.get("tags", {})
    if not isinstance(tags, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in tags.items()
    ):
        return _json({"error": "tags must map strings to strings"}, status=400)
    if body.get("model") is not None and not isinstance(body["model"], str):
        return _json({"error": "model must be a string or null"}, status=400)
    return await _work_product_write(body, success_status=201)


async def work_product_release(request: Request) -> Response:
    """Retry a reviewed submission using its stored, approval-bound inputs."""
    from .. import profiles
    from ..tools import work_product as work_product_tool

    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    if not profiles.tool_is_allowed_for(profiles.current(), "work_product_submit"):
        return _json(
            {"error": "work product submission is unavailable to this profile"}, status=403
        )
    if (await request.body()).strip() not in (b"", b"{}"):
        return _json({"error": "release request body must be empty"}, status=400)
    try:
        work_product_id = int(request.path_params["id"])
    except (TypeError, ValueError):
        return _json({"error": "invalid work product id"}, status=400)
    if work_product_id <= 0:
        return _json({"error": "invalid work product id"}, status=400)
    row = await anyio.to_thread.run_sync(work_product_tool.get_work_product, work_product_id)
    if row is None:
        return _json({"error": "work product not found"}, status=404)
    if row["producing_agent"] != identity:
        return _json({"error": "only the submitter may retry release"}, status=403)
    return await _work_product_write(
        {
            "resume_work_product_id": work_product_id,
            "source_path": row["source_path"],
            "title": row["title"],
            "template_pack": row["template_pack"],
            "tags": row["tags"],
            "model": row["producing_model"],
        },
        success_status=200,
    )


async def evidence_append(request: Request) -> Response:
    """Append task evidence through the platform object's canonical writer."""
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - {"channel", "payload", "dedupe_key"})
    if unknown:
        return _json({"error": "unknown evidence field(s): " + ", ".join(unknown)}, status=400)
    channel = body.get("channel")
    payload = body.get("payload")
    dedupe_key = body.get("dedupe_key")
    if not isinstance(channel, str) or not channel.startswith("sdk.") or len(channel) > 120:
        return _json({"error": "channel must use the sdk. namespace"}, status=400)
    if not isinstance(payload, dict):
        return _json({"error": "payload must be a JSON object"}, status=400)
    if dedupe_key is not None and (
        not isinstance(dedupe_key, str) or not dedupe_key.strip() or len(dedupe_key) > 256
    ):
        return _json(
            {"error": "dedupe_key must be a non-empty string under 256 characters"}, status=400
        )

    try:
        event_ulid, inserted = await api.append_evidence_event(
            channel=channel,
            payload=payload,
            originator=identity,
            dedupe_key=dedupe_key,
        )
    except approvals_tool.ApprovalPendingError as exc:
        return _json(
            {
                "approval_id": exc.approval_id,
                "code": exc.code,
                "status": "pending",
                "tool": "evidence_event.append",
                "payload": {"channel": channel, "payload": payload, "dedupe_key": dedupe_key},
            },
            status=202,
        )
    except agent_access.AgentAccessDenied as exc:
        return _json({"error": str(exc)}, status=403)
    except ResourceError as exc:
        return _json({"error": str(exc)}, status=403)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400)
    return _json({"event_ulid": event_ulid, "inserted": inserted}, status=201)


async def agent_lifecycle_get(request: Request) -> Response:
    """GET /api/agent-lifecycle — agents whose lifecycle changed (task #1351)."""
    guard = _guard(request)
    if guard is not None:
        return guard
    from .. import agent_lifecycle

    return _json(await anyio.to_thread.run_sync(agent_lifecycle.agent_states))


async def agent_lifecycle_action(request: Request) -> Response:
    """POST /api/agent-lifecycle — pause, resume, terminate, retire or unretire an agent.

    Body: ``{"agent": str, "action": "pause"|"resume"|"terminate"|"retire"|"unretire",
    "reason": str, "owner": str}`` (tasks #1351, #2305; ``owner`` on retire only).
    The caller's session identity is the actor; `agent_lifecycle` authorizes it.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - {"agent", "action", "reason", "owner"})
    if unknown:
        return _json({"error": f"unknown field(s): {', '.join(unknown)}"}, status=400)
    from .. import agent_lifecycle

    identity = request.scope.get("ua_identity")
    token = config.agent_identity_var.set(identity) if identity else None
    try:
        res = await anyio.to_thread.run_sync(
            lambda: agent_lifecycle.set_agent_state(
                body.get("agent") or "",
                body.get("action") or "",
                body.get("reason") or "",
                actor=identity,
                owner=body.get("owner"),
            )
        )
    except ToolError as exc:
        return _json_body_error(exc, status=400, limit=None)
    finally:
        if token is not None:
            config.agent_identity_var.reset(token)
    return _json(res)


async def task_claim_release_action(request: Request) -> Response:
    """POST /api/tasks/{task_id}/claim-release — release another agent's live claim (#1700).

    Body requires ``expected_claimant``, ``reason`` and ``expected_run_id``.
    A null run requires ``expected_claimed_at`` (task #2049), fencing the
    specific unbound claim; a replay cannot release a later claim or run.
    ``claim_release``
    authorizes the caller: a lifecycle operator, or the verified human operator
    on a recovery channel. 403 for anyone else, 409 when the claim (claimant, run,
    claim or status) no longer matches what the caller expected, when the task is
    done or cancelled, or while a dispatch run is live, 429 when a policy rate
    limit refuses the write.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = _writer_identity(request)
    if identity is None:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        task_id = int(request.path_params["task_id"])
    except (TypeError, ValueError):
        return _json({"error": "invalid task id"}, status=400)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(
        set(body) - {"expected_claimant", "reason", "expected_run_id", "expected_claimed_at"}
    )
    if unknown:
        return _json({"error": f"unknown field(s): {', '.join(unknown)}"}, status=400)
    for key in ("expected_claimant", "reason", "expected_run_id", "expected_claimed_at"):
        if key in body and body[key] is not None and not isinstance(body[key], str):
            return _json({"error": f"{key} must be a string"}, status=400)
    if "expected_run_id" not in body or body["expected_run_id"] == "":
        return _json(
            {"error": "expected_run_id is required: pass the task's current_run_id"},
            status=400,
        )
    if body["expected_run_id"] is None and not (body.get("expected_claimed_at") or "").strip():
        return _json(
            {"error": "expected_claimed_at is required when expected_run_id is null"},
            status=400,
        )
    from .. import claim_release

    grant = (
        claim_release.human_operator_release(identity)
        if _operator_recovery(request)
        else contextlib.nullcontext()
    )

    arguments = {
        "task_id": task_id,
        "expected_claimant": body.get("expected_claimant") or "",
        "reason": body.get("reason") or "",
        "expected_run_id": body["expected_run_id"],
    }
    if "expected_claimed_at" in body:
        arguments["expected_claimed_at"] = body["expected_claimed_at"]

    def run() -> dict:
        from .. import safety

        token = config.agent_identity_var.set(identity)
        try:
            # The same policy gates as the MCP twin, coordination_manage(
            # action="task_claim_release"): rules on the consolidated tool and
            # on the action itself (PR #1146 review). task_update's own gates
            # then apply inside the release.
            safety.check_policy_gate(
                "coordination_manage",
                {"action": "task_claim_release", "params": arguments},
                originator=identity,
            )
            safety.check_policy_gate("task_claim_release", arguments, originator=identity)
            with grant:
                return claim_release.task_claim_release(**arguments)
        finally:
            config.agent_identity_var.reset(token)

    try:
        res = await anyio.to_thread.run_sync(run)
    except claim_release.ClaimReleaseAuthorityError as exc:
        return _json({"error": str(exc)[:300]}, status=403)
    except claim_release.ClaimReleaseConflict as exc:
        return _json_body_error(exc, status=409)
    except approvals_tool.ApprovalPendingError as exc:
        return _write_error(exc)
    except policy.PolicyViolation as exc:
        status = 429 if exc.reason_code in _THROTTLED_REASONS else 403
        return _json({"error": str(exc)[:300]}, status=status)
    except (agent_access.AgentAccessDenied, ResourceError) as exc:
        return _json({"error": str(exc)[:300]}, status=403)
    except ToolError as exc:
        return _json_body_error(exc, status=400)
    return _json(res)


def _task_hold_caller(request: Request) -> tuple[str | None, Response | None]:
    """Identity and ceiling for a hold write (task #1747); the guard already ran.

    The same rule as the route's scope: the verified operator passes, anyone else
    needs ``task_update`` and the ``task_hold`` action in the request's profile.
    """
    identity = _writer_identity(request)
    if identity is None:
        return None, _json({"error": "authenticated identity required"}, status=401)
    if _operator_recovery(request):
        return identity, None
    profile = _request_profile(request)
    if not (
        profiles.tool_is_allowed_for(profile, "task_update")
        and profiles.consolidated_action_is_allowed_for(profile, "coordination_manage", "task_hold")
    ):
        return None, _json({"error": "task_update is unavailable to this profile"}, status=403)
    return identity, None


async def _run_task_hold(request: Request, identity: str, params: dict) -> Response:
    from .. import task_holds

    # Only the verified human operator on a recovery channel may release a hold it
    # neither created nor holds as a lifecycle operator (task #1747).
    grant = (
        task_holds.operator_release(identity)
        if _operator_recovery(request)
        else contextlib.nullcontext()
    )
    try:
        with grant:
            res = await api.call_tool("coordination_manage", action="task_hold", params=params)
    except approvals_tool.ApprovalPendingError as exc:
        return _json(
            {
                "approval_id": exc.approval_id,
                "code": exc.code,
                "status": "pending",
                "tool": "task_hold",
            },
            status=202,
        )
    except task_holds.HoldAuthorityError as exc:
        return _json({"error": str(exc)[:300]}, status=403)
    except ToolError as exc:
        return _json_body_error(exc, status=400)
    return _json(res)


async def task_holds_list(request: Request) -> Response:
    """GET /api/tasks/holds — holds, newest first (task #1747).

    ``?status=active|released|all`` (default active) and ``?root_task_id=``.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    denied = _require_task_list()
    if denied is not None:
        return denied
    from .. import task_holds

    raw_root = request.query_params.get("root_task_id")
    try:
        root = int(raw_root) if raw_root not in (None, "") else None
    except ValueError:
        return _json({"error": "invalid root_task_id"}, status=400)
    view = request.query_params.get("status") or "active"
    try:
        payload = await anyio.to_thread.run_sync(
            lambda: task_holds.task_holds(root_task_id=root, status=view)
        )
    except ToolError as exc:
        return _json_body_error(exc, status=400)
    return _json(payload)


async def task_holds_create(request: Request) -> Response:
    """POST /api/tasks/holds — hold a subtree (task #1747).

    Body: ``{"root_task_id": int, "mode": "pause"|"cancel"|"resume"|"restore",
    "reason": str, "release_policy": "manual"|"after_active_runs_finish"}``.
    Runs ``coordination_manage(task_hold)`` with the caller as the actor.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity, denied = _task_hold_caller(request)
    if denied is not None:
        return denied
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - {"root_task_id", "mode", "reason", "release_policy"})
    if unknown:
        return _json({"error": f"unknown field(s): {', '.join(unknown)}"}, status=400)
    root = body.get("root_task_id")
    if not isinstance(root, int) or isinstance(root, bool) or root <= 0:
        return _json({"error": "root_task_id must be a positive integer"}, status=400)
    params = {"root_task_id": root}
    for key in ("mode", "reason", "release_policy"):
        if key in body:
            if not isinstance(body[key], str):
                return _json({"error": f"{key} must be a string"}, status=400)
            params[key] = body[key]
    return await _run_task_hold(request, identity, params)


async def task_hold_release(request: Request) -> Response:
    """POST /api/tasks/holds/{hold_id}/release — resume or restore one hold (task #1747).

    Body (optional): ``{"mode": "resume"|"restore", "reason": str}``; default resume.
    The hold id travels into the action, which refuses unless it is still the root's
    active hold. Only the hold's creator or an operator may release it (403 otherwise).
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity, denied = _task_hold_caller(request)
    if denied is not None:
        return denied
    try:
        hold_id = int(request.path_params["hold_id"])
    except (TypeError, ValueError):
        return _json({"error": "invalid hold id"}, status=400)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - {"mode", "reason"})
    if unknown:
        return _json({"error": f"unknown field(s): {', '.join(unknown)}"}, status=400)
    mode = body.get("mode", "resume")
    reason = body.get("reason", "")
    if mode not in ("resume", "restore") or not isinstance(reason, str):
        return _json({"error": "mode must be resume or restore; reason a string"}, status=400)
    from .. import task_holds

    try:
        hold = await anyio.to_thread.run_sync(lambda: task_holds.task_hold_get(hold_id))
    except ToolError as exc:
        return _json_body_error(exc, status=404)
    if hold["status"] != "active":
        return _json({"error": f"hold #{hold_id} is already released", "hold": hold}, status=409)
    return await _run_task_hold(
        request,
        identity,
        {
            "root_task_id": hold["root_task_id"],
            "mode": mode,
            "reason": reason,
            "hold_id": hold_id,
        },
    )


async def action_task(request: Request) -> Response:
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    task_id_raw = body.get("id")
    if task_id_raw is None:
        return _json({"error": "missing task id"}, status=400)
    try:
        task_id = int(task_id_raw)
    except (ValueError, TypeError):
        return _json({"error": "invalid task id; expected integer"}, status=400)

    _TASK_UPDATE_PARAMS = {
        "status",
        "title",
        "notes",
        "append_note",
        "priority",
        "parent_id",
        "untrusted",
        "worktree",
        "project",
        "expected_project",
        "expected_updated_at",
        "domain",
        "lane",
        "due_at",
        "clear_due_at",
        "evidence_ref",
        "idempotency_key",
        "archived",
    }
    kwargs = {k: v for k, v in body.items() if k != "id" and k in _TASK_UPDATE_PARAMS}
    unknown = sorted(k for k in body if k != "id" and k not in _TASK_UPDATE_PARAMS)
    if unknown:
        # Fail closed like the MCP extra="forbid" path: a caller typo such as
        # "notes_append" used to be dropped by the whitelist above, returning
        # a 200 with the full task and a bumped updated_at while the note was
        # silently discarded (task #1246).
        return _json(
            {
                "error": (
                    "unknown task_update field(s): "
                    + ", ".join(unknown)
                    + "; allowed: "
                    + ", ".join(sorted(_TASK_UPDATE_PARAMS))
                )
            },
            status=400,
        )
    # The registered tool is called without its MCP argument model, so a JSON string
    # such as "false" would reach it as a truthy value: archived="false" passed the
    # parked-task admission while leaving archived_at set (#1507 review), and
    # untrusted, worktree and clear_due_at would do the opposite of what was asked.
    for flag in ("archived", "untrusted", "worktree", "clear_due_at"):
        if flag in kwargs and kwargs[flag] is not None and not isinstance(kwargs[flag], bool):
            return _json({"error": f"{flag} must be true or false"}, status=400)
    if "append_note" in kwargs and (
        not isinstance(kwargs["append_note"], str)
        or not kwargs["append_note"].strip()
        or len(kwargs["append_note"]) > 2_000
    ):
        return _json(
            {"error": "append_note must be a non-empty string under 2000 characters"}, status=400
        )
    if "status" in kwargs and kwargs["status"] not in (
        "open",
        "doing",
        "waiting_review",
        "blocked",
        "done",
        "cancelled",
    ):
        return _json(
            {
                "error": (
                    "invalid task status; expected open, doing, waiting_review, blocked, done, "
                    "or cancelled"
                )
            },
            status=400,
        )

    identity = request.scope.get("ua_identity") or "ui_user"
    try:
        res = await api.update_task(task_id=task_id, actor=identity, **kwargs)
    except approvals_tool.ApprovalPendingError as exc:
        return _json(
            {
                "approval_id": exc.approval_id,
                "code": exc.code,
                "status": "pending",
                "tool": "task_update",
                "payload": {"id": task_id, **kwargs},
            },
            status=202,
        )
    except (KeyError, ValueError) as exc:
        return _json({"error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)
    return _json(res)


async def action_workflow(request: Request) -> Response:
    """POST /api/action/workflow — run a reviewed workflow.

    An optional ``Idempotency-Key`` runs it at most once per caller and key
    (``cc_knowledge.respond_once``, task #2516): the first answer is frozen,
    including a run that stopped at ``pending-approval`` (its earlier steps ran).
    """
    from .cc_knowledge import _idempotency_key, respond_once

    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    name = body.get("name")
    if not name or not isinstance(name, str):
        return _json({"error": "missing or invalid workflow name"}, status=400)
    inputs = body.get("inputs")
    if inputs is not None and not isinstance(inputs, dict):
        return _json({"error": "workflow inputs must be a JSON object"}, status=400)
    identity = request.scope.get("ua_identity") or "ui_user"
    key = _idempotency_key(request)
    if isinstance(key, Response):
        return key
    arguments = {"name": name, "inputs": inputs or {}, "author": identity}
    for field, expected_type in (
        ("instance_id", str),
        ("role_bindings", dict),
        ("closure", dict),
    ):
        if field in body:
            if not isinstance(body[field], expected_type):
                return _json({"error": f"workflow {field} has an invalid type"}, status=400)
            arguments[field] = body[field]

    async def start() -> Response:
        # A refusal (ToolError, bad input) is an answer: nothing ran. Anything else
        # propagates, so a keyed start records its outcome as unknown.
        try:
            res = await api.run_workflow(**arguments)
        except (KeyError, ValueError) as exc:
            return _json({"error": str(exc)}, status=400)
        except ToolError as exc:
            return _json_body_error(exc, status=400, limit=200)
        return _json(res)

    if key is None:
        try:
            return await start()
        except Exception as exc:  # noqa: BLE001 - bounded operator error
            return _json_body_error(exc, status=400, limit=200)
    if request.scope.get("ua_identity") is None:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        return await respond_once(
            identity, key, "workflow_run", arguments, start, check="the workflow's run history"
        )
    except Exception as exc:  # noqa: BLE001 - bounded operator error; the key is settled
        return _json_body_error(exc, status=400, limit=200)


async def action_dispatch(request: Request) -> Response:
    from . import cc_launch  # imports this module

    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required for dispatch"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
        payload = parse_dispatch_request(body)
        if payload.agent == identity:
            return _json({"error": "agent must differ from the requesting identity"}, status=400)
    except (ValueError, TypeError) as exc:
        return _json({"error": str(exc)}, status=400)

    async def run(attempt) -> Response:
        try:
            # Without a key the call is exactly the one agents and the CLI always made.
            keyed = {"attempt": attempt} if attempt is not None else {}
            result = await api.dispatch_prompt(payload, identity, **keyed)
        except (ValueError, TypeError) as exc:
            return _json({"error": str(exc)}, status=400)
        except (cc_launch.LaunchBusy, cc_launch.ReceiptUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001 - route returns a bounded operator error
            return _json_body_error(exc, status=400, message=f"dispatch failed: {str(exc)[:200]}")
        return _json(result, status=202)

    # Task #2501: an Idempotency-Key makes the launch safe to repeat.
    return await cc_launch.keyed_launch(
        request, identity, "dispatch", lambda: dataclasses.asdict(payload), run
    )


async def dispatch_stack(request: Request) -> Response:
    guard = _guard(request)
    if guard is not None:
        return guard
    raw_ids = request.query_params.get("task_ids", "")
    try:
        payload = parse_dispatch_stack_request(
            {
                "task_ids": [item for item in raw_ids.split(",") if item.strip()],
                "project": request.query_params.get("project"),
                "expected_lane": request.query_params.get("expected_lane"),
            }
        )
    except (ValueError, TypeError) as exc:
        return _json({"error": str(exc)}, status=400)
    return _json(await api.dispatch_stack_payload(payload))


async def action_dispatch_stack(request: Request) -> Response:
    from . import cc_launch  # imports this module

    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required for dispatch"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
        payload = parse_dispatch_stack_request(body)
    except (ValueError, TypeError) as exc:
        return _json({"error": str(exc)}, status=400)

    async def run(attempt) -> Response:
        try:
            with profiles.http_dispatch_preview_authority():
                preview = await api.dispatch_stack_payload(payload)
            if preview.get("error"):
                return _json(preview, status=503)
            # A repeat of a keyed launch starts the task the attempt chose, never
            # the next one (the first is claimed by then) (task #2501).
            task_id = (attempt.steps.get("task_id") if attempt else None) or preview.get(
                "next_task_id"
            )
            if task_id is None:
                return _json(
                    {"error": "dispatch stack has no eligible task", **preview}, status=409
                )
            keyed: dict[str, str] = {}
            if attempt is not None:
                if attempt.steps.get("task_id") is not None:
                    await anyio.to_thread.run_sync(lambda: api.require_recorded_task(task_id))
                # The selection must be durable before anything launches: a
                # request that lost its hold, or could not record the task,
                # launches nothing (review #1521 pass 3, item 1).
                await attempt.arequire(task_id=task_id)
                keyed["launch_key"] = attempt.op_key
            actor = api.dispatch_from_agent(identity)
            started = await anyio.to_thread.run_sync(
                lambda: api.start_ui_dispatch(
                    task_id=task_id,
                    actor=actor,
                    expected_project=payload.project,
                    expected_lane=payload.expected_lane,
                    **keyed,
                )
            )
            if attempt is not None and isinstance(started, dict):
                await attempt.asave(run_id=started.get("run_id"))
        except (ValueError, TypeError) as exc:
            return _json({"error": str(exc)}, status=400)
        except (cc_launch.LaunchBusy, cc_launch.ReceiptUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001 - bounded operator error
            return _json_body_error(
                exc, status=400, message=f"stack dispatch failed: {str(exc)[:200]}"
            )
        # The task this request started, not whatever the preview names now.
        return _json({**preview, "next_task_id": task_id, "dispatch": started}, status=202)

    return await cc_launch.keyed_launch(
        request, identity, "stack", lambda: dataclasses.asdict(payload), run
    )


async def action_dependency(request: Request) -> Response:
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        operation = body.get("operation", "add")
        if operation == "add":
            task_id = int(body["task_id"])
            prerequisite_id = int(body["depends_on_task_id"])
            result = await api.add_dependency(task_id, prerequisite_id, identity)
        elif operation == "remove":
            result = await api.remove_dependency(int(body["dependency_id"]), identity)
        else:
            raise ValueError("operation must be add or remove")
    except (KeyError, TypeError, ValueError) as exc:
        return _json({"error": f"invalid dependency request: {exc}"}, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)
    return _json(result, status=201 if operation == "add" else 200)


async def action_handoff(request: Request) -> Response:
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        task_spec = body.get("task_spec")
        to_agent = body.get("to_agent")
        if not isinstance(task_spec, str) or not task_spec.strip():
            raise ValueError("task_spec is required")
        if not isinstance(to_agent, str) or not to_agent.strip():
            raise ValueError("to_agent is required")
        parent_task_id = body.get("parent_task_id")
        if parent_task_id is not None:
            parent_task_id = int(parent_task_id)
        result = await api.create_handoff(
            task_spec=task_spec.strip(),
            to_agent=to_agent.strip(),
            from_agent=identity,
            deadline=body.get("deadline"),
            priority=body.get("priority", "med"),
            parent_task_id=parent_task_id,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return _json({"error": f"invalid handoff request: {exc}"}, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)
    return _json(result, status=202)


async def action_crew_intervene(request: Request) -> Response:
    """POST /api/action/crew-intervene — typed watch+intervene into a live session."""
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    from mcp.server.mcpserver.exceptions import ToolError

    try:
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        session_id = body.get("session_id")
        text = body.get("body")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is required")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("body is required")
        message_id = body.get("message_id")
        if message_id is not None and not isinstance(message_id, str):
            raise ValueError("message_id must be a string when provided")
        result = await api.crew_intervene(
            session_id=session_id.strip(),
            body=text,
            from_agent=identity,
            message_id=message_id,
        )
    except ToolError as exc:
        msg = str(exc)
        status = 404 if "unknown session" in msg else 400
        return _json_body_error(exc, status=status)
    except (KeyError, TypeError, ValueError) as exc:
        return _json({"error": f"invalid crew intervene request: {exc}"}, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)
    return _json(result, status=202)


async def action_node_session(request: Request) -> Response:
    """POST /api/action/node-session — CSRF-gated start/cancel for ua-node work (I3).

    Browser counterpart to ``POST /api/node/dispatch`` (start) and
    ``node_queue.cancel`` (cancel). Start still goes through the prod approval
    gate; cancel is attributed to the authenticated Control Center identity.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    from mcp.server.mcpserver.exceptions import ToolError

    from .. import node_queue
    from . import cc_launch

    try:
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        action = body.get("action")
        if action not in ("start", "cancel", "restart"):
            raise ValueError("action must be 'start', 'cancel', or 'restart'")
        if action == "cancel":
            item_id = body.get("item_id")
            if not isinstance(item_id, str) or not item_id.strip():
                raise ValueError("item_id is required for cancel")
            result = await anyio.to_thread.run_sync(
                lambda: node_queue.cancel(item_id.strip(), actor=identity)
            )
            return _json(result)
        if action == "restart":
            item_id = body.get("item_id")
            if not isinstance(item_id, str) or not item_id.strip():
                raise ValueError("item_id is required for restart")
            item_id = item_id.strip()

            def launch(key):
                return node_queue.restart(item_id, actor=identity, launch_key=key)

            return await cc_launch.keyed_launch(
                request,
                identity,
                "restart",
                {"item_id": item_id},
                lambda attempt: _node_launch(launch, attempt),
            )
        # start
        node = body.get("node")
        if not isinstance(node, str) or not node.strip():
            raise ValueError("node is required")
        node = node.strip()
        fields = {
            k: body.get(k)
            for k in (
                "prompt",
                "cwd",
                "backend",
                "model",
                "permission_mode",
                "timeout_s",
                "task_id",
                "run_id",
                "hermes_profile",
            )
            if k in body
        }

        def launch(key):
            return node_queue.request_dispatch(node, actor=identity, launch_key=key, **fields)

        return await cc_launch.keyed_launch(
            request,
            identity,
            "device",
            {"node": node, **fields},
            lambda attempt: _node_launch(launch, attempt),
        )
    except node_queue.NodeError as exc:
        return _node_error(exc)
    except ToolError as exc:
        return _json_body_error(exc, status=400)
    except (KeyError, TypeError, ValueError) as exc:
        return _json({"error": f"invalid node-session request: {exc}"}, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)


async def _node_launch(launch, attempt) -> Response:
    """Queue one ua-node item (start or restart) and answer as node-session always has.

    ``launch(key)`` queues the item; under a keyed attempt the key is the attempt's
    derived operation key, so the queue itself refuses a second item (task #2501).
    """
    from mcp.server.mcpserver.exceptions import ToolError

    from .. import node_queue
    from . import cc_launch

    key = attempt.op_key if attempt is not None else None
    try:
        result = await anyio.to_thread.run_sync(lambda: launch(key))
        if attempt is not None:
            await attempt.asave(item_id=result.get("item_id"))
    except cc_launch.LaunchBusy:
        raise
    except approvals_tool.ApprovalPendingError as exc:
        return _json(
            {
                "status": "approval_pending",
                "approval_id": exc.approval_id,
                "code": exc.code,
                "tool": node_queue.NODE_DISPATCH_TOOL,
            },
            status=202,
        )
    except node_queue.NodeError as exc:
        return _node_error(exc)
    except ToolError as exc:
        return _json_body_error(exc, status=400)
    except (KeyError, TypeError, ValueError) as exc:
        return _json({"error": f"invalid node-session request: {exc}"}, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)
    return _json(result, status=201)


async def _node_acceptance(request: Request):
    """GET /api/nodes/acceptance — device acceptance state (task #2112).

    ``?node=<name>`` returns that node's live evaluation (computed, not
    recorded); without it, every node's stored summary. Read-only.
    """
    from .. import node_acceptance

    node = request.query_params.get("node")
    try:
        if node:
            return await anyio.to_thread.run_sync(
                lambda: node_acceptance.evaluate(node, persist=False)
            )
        states = await anyio.to_thread.run_sync(node_acceptance.read_states)
    except node_queue.NodeError as exc:
        return _node_error(exc)
    except Exception as exc:  # noqa: BLE001 - reported in-band
        return {"nodes": {}, "error": f"acceptance query failed: {str(exc)[:200]}"}
    return {"nodes": states, "required": config.node_require_acceptance()}


async def action_node_acceptance(request: Request) -> Response:
    """POST /api/action/node-acceptance — start or re-evaluate device acceptance (#2112).

    ``{action: "start" | "evaluate", node}``. The node's own agent or a
    lifecycle operator only. Start queues the harmless acceptance job (no
    model, no harness), so it takes no approval.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)

    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - {"action", "node", "run_id", "evidence_ref"})
    if unknown:
        return _json({"error": "unknown field(s): " + ", ".join(unknown)}, status=400)
    action, node = body.get("action"), body.get("node")
    if action not in ("start", "evaluate", "attest"):
        return _json({"error": "action must be 'start', 'evaluate', or 'attest'"}, status=400)
    if not isinstance(node, str) or not node.strip():
        return _json({"error": "node is required"}, status=400)

    arguments = {"node": node.strip()}
    if action == "attest":
        arguments.update(run_id=body.get("run_id"), evidence_ref=body.get("evidence_ref"))

    def run() -> dict:
        from .. import safety
        from ..tools.graph import coordination_manage

        token = config.agent_identity_var.set(identity)
        try:
            safety.check_policy_gate(
                "coordination_manage",
                {
                    "action": "node_acceptance_" + action,
                    "params": arguments,
                },
                originator=identity,
            )
            return coordination_manage("node_acceptance_" + action, arguments)
        finally:
            config.agent_identity_var.reset(token)

    try:
        result = await anyio.to_thread.run_sync(run)
    except policy.PolicyViolation as exc:
        status = 429 if exc.reason_code in _THROTTLED_REASONS else 403
        return _json({"error": str(exc)[:300]}, status=status)
    except approvals_tool.ApprovalPendingError as exc:
        return _write_error(exc)
    except ToolError as exc:
        if isinstance(exc.__cause__, node_queue.NodeError):
            return _node_error(exc.__cause__)
        return _json_body_error(exc, status=403)
    except node_queue.NodeError as exc:
        return _node_error(exc)
    return _json(result, status=201 if action == "start" else 200)


async def action_node_retire(request: Request) -> Response:
    """POST /api/action/node-retire — retire or un-retire a ua-node row (task #2208).

    ``{action: "retire" | "unretire", node, reason}``. The HTTP twin of the
    ``coordination_manage`` ``node_retire`` / ``node_unretire`` actions (task
    #2181), so the human operator can run them from the Control Center or the
    ``ua`` CLI. ``node_registry`` admits lifecycle operators only (403
    otherwise); retiring a node with queued or claimed work is a 409.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)

    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - {"action", "node", "reason"})
    if unknown:
        return _json({"error": "unknown field(s): " + ", ".join(unknown)}, status=400)
    action, node, reason = body.get("action"), body.get("node"), body.get("reason")
    if action not in ("retire", "unretire"):
        return _json({"error": "action must be 'retire' or 'unretire'"}, status=400)
    if not isinstance(node, str) or not node.strip():
        return _json({"error": "node is required"}, status=400)
    if not isinstance(reason, str) or not reason.strip():
        return _json({"error": "a reason is required"}, status=400)

    tool_action = "node_" + action
    arguments = {"node": node.strip(), "reason": reason}

    def run() -> dict:
        from .. import safety
        from ..tools.graph import coordination_manage

        token = config.agent_identity_var.set(identity)
        try:
            safety.check_policy_gate(
                "coordination_manage",
                {"action": tool_action, "params": arguments},
                originator=identity,
            )
            return coordination_manage(tool_action, arguments)
        finally:
            config.agent_identity_var.reset(token)

    try:
        result = await anyio.to_thread.run_sync(run)
    except policy.PolicyViolation as exc:
        status = 429 if exc.reason_code in _THROTTLED_REASONS else 403
        return _json({"error": str(exc)[:300]}, status=status)
    except approvals_tool.ApprovalPendingError as exc:
        return _write_error(exc)
    except ToolError as exc:
        # A typed refusal keeps its own status: permission_denied is 403 and
        # conflict (queued or claimed work) is 409. An untyped one is a bad
        # request: an unknown node, a bad name or an over-long reason.
        typed = _typed_error(exc)
        if typed is not None and typed.code == PermissionDeniedError.code:
            return _json_body_error(exc, status=403)
        return _json_body_error(exc, status=400)
    return _json(result, status=200)


_WORK_PROPOSAL_DECIDE_FIELDS = frozenset({"proposal_id", "decisions"})


async def action_work_proposal_decide(request: Request) -> Response:
    """POST /api/action/work-proposal-decide — decide a batch work proposal (task #2393).

    ``{proposal_id, decisions: [{index, approve, edits?}]}``. The HTTP twin of
    the ``coordination_manage`` ``work_proposal_decide`` action, so the human
    operator can decide from the Control Center: the operator profile has no
    consolidated managers over MCP. ``work_proposals`` refuses any identity
    that is not human (403); a bad proposal, index or edit is a 400.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - _WORK_PROPOSAL_DECIDE_FIELDS)
    if unknown:
        return _json({"error": "unknown field(s): " + ", ".join(unknown)}, status=400)
    proposal_id, decisions = body.get("proposal_id"), body.get("decisions")
    if not isinstance(proposal_id, str) or not proposal_id.strip():
        return _json({"error": "proposal_id is required"}, status=400)
    if not isinstance(decisions, list) or not decisions:
        return _json({"error": "decisions must be a non-empty list"}, status=400)
    arguments = {"proposal_id": proposal_id.strip(), "decisions": decisions}

    def run() -> dict:
        from .. import safety
        from ..tools.graph import coordination_manage

        token = config.agent_identity_var.set(identity)
        try:
            safety.check_policy_gate(
                "coordination_manage",
                {"action": "work_proposal_decide", "params": arguments},
                originator=identity,
            )
            return coordination_manage("work_proposal_decide", arguments)
        finally:
            config.agent_identity_var.reset(token)

    try:
        result = await anyio.to_thread.run_sync(run)
    except policy.PolicyViolation as exc:
        status = 429 if exc.reason_code in _THROTTLED_REASONS else 403
        return _json({"error": str(exc)[:300]}, status=status)
    except approvals_tool.ApprovalPendingError as exc:
        return _write_error(exc)
    except ToolError as exc:
        typed = _typed_error(exc)
        if typed is not None and typed.code == PermissionDeniedError.code:
            return _json_body_error(exc, status=403)
        return _json_body_error(exc, status=400)
    return _json(result, status=200)


_FOLLOWUP_FIELDS = frozenset({"item_id", "mode", "body", "idempotency_key"})


async def action_node_followup(request: Request) -> Response:
    """POST /api/action/node-followup — queue or steer a live ACP session (task #1740).

    Who is gated (task #1872): an operator-authenticated follow-up
    (``_operator_recovery``: ``UA_OPERATOR_LABEL`` as the authenticated
    identity, on a bearer, OAuth, cookie or WorkOS channel) is queued at once,
    with ``operator_authored: true, approval: "not_required_operator"`` on the
    row and its event. Any other identity, including an agent whose session
    runs the operator profile, passes the ``node_dispatch`` approval gate on
    the follow-up's own text (202 ``approval_pending`` until approved), like
    the dispatch it extends. The decision is made here from the authenticated
    request; the body cannot carry it (unknown fields are refused). It proves
    the operator's credential was used, not that a person typed the text.

    ``mode`` is ``queue`` (sent after the current turn ends) or ``steer``
    (interrupt and redirect now). ``idempotency_key`` is chosen by the client
    once per follow-up and resent on retry: the first accepted row answers
    every retry (200, ``duplicate: true``), so a repeated click never sends
    twice. A new follow-up answers 201: it is queued, not yet delivered.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    # Defence in depth for the route scope: a profile without the session and
    # crew views cannot queue or steer.
    profile = profiles.current()
    for tool in route_scopes.ROUTE_SCOPES[("POST", "/api/action/node-followup")].tools:
        if not profiles.tool_is_allowed_for(profile, tool):
            return _json({"error": f"{tool} is unavailable to this profile"}, status=403)
    from .. import node_followups as followups

    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - _FOLLOWUP_FIELDS)
    if unknown:
        return _json({"error": "unknown field(s): " + ", ".join(unknown)}, status=400)
    # Read in the request context: only a request authenticated as the
    # operator on a recovery channel may follow up on a session another
    # identity started, and only those skip the per-follow-up approval
    # (task #1872).
    # Identity-bound, never profile-bound: an agent on the operator profile is
    # not the operator.
    operator = _operator_recovery(request)
    try:
        result = await anyio.to_thread.run_sync(
            lambda: followups.submit(
                body.get("item_id"),
                mode=body.get("mode"),
                body=body.get("body"),
                idempotency_key=body.get("idempotency_key"),
                actor=identity,
                operator=operator,
            )
        )
    except approvals_tool.ApprovalPendingError as exc:
        # A follow-up is new prompt text for the device: it waits on the same
        # node_dispatch approval the dispatch did. Re-send the same request
        # (same idempotency_key) once approved.
        return _json(
            {
                "status": "approval_pending",
                "approval_id": exc.approval_id,
                "code": exc.code,
                "tool": node_queue.NODE_DISPATCH_TOOL,
            },
            status=202,
        )
    except followups.NodeError as exc:
        return _node_error(exc)
    except ToolError as exc:  # denied approval, revoked or unscoped identity
        return _json_body_error(exc, status=403)
    return _json(result, status=200 if result.get("duplicate") else 201)


def _route_tools_allowed(method: str, path: str) -> Response | None:
    """Re-check a route's declared tool ceiling in the handler (defence in depth)."""
    profile = profiles.current()
    for tool in route_scopes.ROUTE_SCOPES[(method, path)].tools:
        if not profiles.tool_is_allowed_for(profile, tool):
            return _json({"error": f"{tool} is unavailable to this profile"}, status=403)
    return None


async def _native_sessions(request: Request):
    """GET /api/nodes/native-sessions — discovered Claude Code / Codex sessions (#1741).

    Metadata only (harness, session id, cwd, last activity, title), limited
    to each node's worktree roots, with whether the caller may adopt each one.
    """
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    denied = _route_tools_allowed("GET", "/api/nodes/native-sessions")
    if denied is not None:
        return denied
    from .. import node_resume

    try:
        rows = await anyio.to_thread.run_sync(
            lambda: node_resume.list_native_sessions(actor=identity)
        )
    except Exception as exc:  # noqa: BLE001 - reported in-band
        return {"sessions": [], "error": f"native sessions query failed: {str(exc)[:200]}"}
    return {"sessions": rows}


_RESUME_FIELDS = frozenset({"item_id", "prompt"})
_ADOPT_FIELDS = frozenset(
    {"node", "harness", "external_session_id", "prompt", "backend", "timeout_s"}
)


async def _session_takeover(request: Request, path: str, fields: frozenset[str], call):
    """Shared body of the resume and adopt actions: CSRF, identity, ceiling, gate."""
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    denied = _route_tools_allowed("POST", path)
    if denied is not None:
        return denied
    from .. import node_queue

    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - fields)
    if unknown:
        return _json({"error": "unknown field(s): " + ", ".join(unknown)}, status=400)
    try:
        operator = _operator_recovery(request)
        result = await anyio.to_thread.run_sync(lambda: call(body, identity, operator))
    except approvals_tool.ApprovalPendingError as exc:
        return _json(
            {
                "status": "approval_pending",
                "approval_id": exc.approval_id,
                "code": exc.code,
                "tool": node_queue.NODE_DISPATCH_TOOL,
            },
            status=202,
        )
    except node_queue.NodeError as exc:
        return _node_error(exc)
    except ToolError as exc:
        # A denied approval, or a revoked / unscoped identity at the dispatch
        # gate (#1741 review): a refusal, not a server error.
        return _json_body_error(exc, status=403, message=f"dispatch refused: {str(exc)[:300]}")
    # 201: a new item was queued; 200: an adopt repeated while its item is open.
    return _json(
        result, status=200 if result.get("duplicate") and "created_at" not in result else 201
    )


async def action_node_resume(request: Request) -> Response:
    """POST /api/action/node-resume — reopen a finished node session (task #1741).

    ``{item_id, prompt}``: queues a new item that resumes the item's harness
    session (ACP session/resume or session/load, or the native CLI's resume
    flag) with ``prompt`` as the next message. Refused, never downgraded to a
    fresh session, when the agent or node cannot resume. Approval-gated like
    any node dispatch.
    """
    from .. import node_resume

    return await _session_takeover(
        request,
        "/api/action/node-resume",
        _RESUME_FIELDS,
        lambda body, identity, operator: node_resume.resume(
            body.get("item_id"), prompt=body.get("prompt"), actor=identity, operator=operator
        ),
    )


async def action_node_adopt(request: Request) -> Response:
    """POST /api/action/node-adopt — adopt a discovered native session (task #1741).

    ``{node, harness, external_session_id, prompt, backend?, timeout_s?}``:
    records the session under the caller's verified identity (idempotent on
    harness and session id; another label's session is refused) and queues
    its resume through the approval gate.
    """
    from .. import node_resume

    return await _session_takeover(
        request,
        "/api/action/node-adopt",
        _ADOPT_FIELDS,
        lambda body, identity, _operator: node_resume.adopt(
            body.get("node"),
            harness=body.get("harness"),
            external_session_id=body.get("external_session_id"),
            prompt=body.get("prompt"),
            backend=body.get("backend"),
            timeout_s=body.get("timeout_s"),
            actor=identity,
        ),
    )


async def action_fleet(request: Request) -> Response:
    """Approval-gated fleet control (task #799). DoD: restart_service on local host."""
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        action = str(body.get("action") or "").strip()
        host = str(body.get("host") or "").strip()
        unit = body.get("unit")
        if unit is not None:
            unit = str(unit).strip() or None
        if not action or not host:
            raise ValueError("action and host are required")
        result = await api.fleet_control_action(
            action=action,
            host=host,
            unit=unit,
            actor=identity,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return _json({"error": f"invalid fleet request: {exc}"}, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        from ..tools.approvals import ApprovalPendingError

        if isinstance(exc, ApprovalPendingError):
            return _json(
                {
                    "status": "approval_pending",
                    "approval_id": getattr(exc, "approval_id", None),
                    "code": exc.code,
                    "error": str(exc)[:300],
                },
                status=202,
            )
        return _json_body_error(exc, status=400, limit=200)
    return _json(result)


async def action_oauth_revoke(request: Request) -> Response:
    """Approval-gated revoke of a device's OAuth sign-in (task #1321, J2)."""
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        device = str(body.get("device") or "").strip() or None
        session_id = str(body.get("session_id") or "").strip() or None
        if bool(device) == bool(session_id):
            raise ValueError("pass exactly one of device or session_id")
        result = await api.oauth_revoke_action(device=device, session_id=session_id, actor=identity)
    except (KeyError, TypeError, ValueError) as exc:
        return _json({"error": f"invalid revoke request: {exc}"}, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        from ..tools.approvals import ApprovalPendingError

        if isinstance(exc, ApprovalPendingError):
            return _json(
                {
                    "status": "approval_pending",
                    "approval_id": getattr(exc, "approval_id", None),
                    "code": exc.code,
                    "error": str(exc)[:300],
                },
                status=202,
            )
        return _json_body_error(exc, status=400, limit=200)
    return _json(result)


def _passkey_registrar(request: Request) -> tuple[str | None, Response | None]:
    """Who may manage approver passkeys (task #1322): an authenticated *human*
    Control Center identity on a tailnet node owned by a configured approver login.

    Returns ``(login, None)`` or ``(None, error_response)``. Checked even in
    warn-only auth mode: an anonymous caller never registers a passkey.
    """
    from .. import oauth_approver

    identity = request.scope.get("ua_identity")
    if not identity:
        return None, _json({"error": "authenticated identity required"}, status=401)
    if config.actor_kind_for_identity(identity) != "human":
        return None, _json({"error": "only a human operator identity manages passkeys"}, 403)
    if not config.mcp_oauth_approver_logins():
        return None, _json(
            {"error": "set UA_MCP_OAUTH_APPROVER_LOGINS on the hub to enable passkeys"}, 409
        )
    login = oauth_approver._tailnet_approver_login(request)
    if not login:
        return None, _json(
            {"error": "open the Control Center from a tailnet device owned by the approver"},
            403,
        )
    return login, None


_PASSKEY_BODY_LIMIT = 128 * 1024


async def _passkey_json(request: Request) -> tuple[dict | None, Response | None]:
    """Read a passkey mutation with a hard cap, including chunked requests."""
    length = request.headers.get("content-length")
    try:
        if length is not None and (int(length) < 0 or int(length) > _PASSKEY_BODY_LIMIT):
            return None, _json({"error": "passkey request body exceeds limit"}, 413)
    except ValueError:
        return None, _json({"error": "invalid Content-Length"}, 400)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _PASSKEY_BODY_LIMIT:
            return None, _json({"error": "passkey request body exceeds limit"}, 413)
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks) or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None, _json({"error": "invalid JSON"}, 400)
    if not isinstance(body, dict):
        return None, _json({"error": "request body must be a JSON object"}, 400)
    return body, None


async def oauth_passkeys(request: Request) -> Response:
    """List (GET) or register (POST) approver passkeys (task #1322, J3)."""
    from .. import oauth_approver, passkeys

    if request.method == "GET":
        guard = _guard(request)
        if guard is not None:
            return guard
        try:
            rows = passkeys.list_passkeys()
        except Exception as exc:  # noqa: BLE001 - bounded operator error
            return _json_body_error(
                exc, status=503, message=f"passkey store unavailable: {str(exc)[:160]}"
            )
        peer_host = request.client.host if request.client else None
        try:
            peer_is_loopback = ipaddress.ip_address(peer_host).is_loopback
        except (TypeError, ValueError):
            peer_is_loopback = False
        return _json(
            {
                "passkeys": rows,
                "approver_logins": config.mcp_oauth_approver_logins(),
                "rp_id": config.mcp_oauth_passkey_rp_id(),
                "origin": passkeys.expected_origin(),
                "this_device_login": oauth_approver._tailnet_approver_login(request),
                "peer_is_loopback": peer_is_loopback,
                "passphrase_fallback": config.mcp_oauth_passphrase_fallback(),
            }
        )
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    login, refused = _passkey_registrar(request)
    if refused is not None:
        return refused
    body, refused = await _passkey_json(request)
    if refused is not None:
        return refused
    assert body is not None
    try:
        stored = passkeys.register(
            login=login,
            credential_id=str(body.get("credential_id") or ""),
            client_data_json=str(body.get("client_data_json") or ""),
            authenticator_data=str(body.get("authenticator_data") or ""),
            public_key=str(body.get("public_key") or ""),
            public_key_algorithm=body.get("public_key_algorithm"),
            label=str(body.get("label") or "") or None,
            registered_by=request.scope.get("ua_identity"),
        )
    except (ValueError, passkeys.PasskeyError) as exc:
        return _json({"error": str(exc)[:200]}, status=400)
    _record_passkey_event("hub.oauth.passkey_registered", stored, request)
    return _json(stored)


async def oauth_passkey_options(request: Request) -> Response:
    """WebAuthn creation options for registering this approver's passkey."""
    from .. import passkeys

    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    login, refused = _passkey_registrar(request)
    if refused is not None:
        return refused
    try:
        return _json(passkeys.registration_options(login))
    except passkeys.PasskeyError as exc:
        return _json({"error": str(exc)[:200]}, status=409)


async def oauth_passkey_delete(request: Request) -> Response:
    """Remove one approver passkey (same gate as registration)."""
    from .. import passkeys

    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    login, refused = _passkey_registrar(request)
    if refused is not None:
        return refused
    body, refused = await _passkey_json(request)
    if refused is not None:
        return refused
    assert body is not None
    credential_id = str(body.get("credential_id") or "").strip()
    if not credential_id:
        return _json({"error": "credential_id is required"}, status=400)
    # An approver may remove only their own login's passkeys.
    if not passkeys.delete_passkey(credential_id, login=login):
        return _json({"error": "no such passkey"}, status=404)
    _record_passkey_event("hub.oauth.passkey_deleted", {"credential_id": credential_id}, request)
    return _json({"deleted": credential_id})


def _record_passkey_event(channel: str, payload: dict, request: Request) -> None:
    try:
        from .. import events

        identity = request.scope.get("ua_identity")
        events.record_event(
            channel,
            payload={
                "credential": str(payload.get("credential_id") or "")[:16],
                "login": payload.get("login"),
                "rp_id": payload.get("rp_id"),
                "actor": identity,
            },
            originator=identity,
            notify=False,
        )
    except Exception:  # noqa: BLE001 - audit is best-effort
        pass


async def graph_canvas(request: Request) -> Response:
    """Excalidraw planning canvas scene (task #807).

    GET is an approval-free read (same guard plane as every other /api read).
    PUT is CSRF-gated like every mutation and attributes the scene to the
    verified identity. The scene is stored verbatim via the same storage
    accessors the `graph_canvas_read`/`graph_canvas_write` MCP tools use, so
    the browser and the agents can never disagree about what is on the canvas.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    board_raw = request.query_params.get("board", "default")
    board = board_raw.strip().lower()
    if not graph_canvas_tool._BOARD_ID_RE.match(board):
        return _json({"error": "board must match [a-z0-9][a-z0-9._-]{0,63}"}, status=400)

    if request.method == "GET":
        row = await anyio.to_thread.run_sync(lambda: storage.get_graph_canvas_scene(board))
        if row is None:
            return _json(
                {
                    "board_id": board,
                    "exists": False,
                    "updated_by": None,
                    "updated_at": None,
                    "scene": {},
                }
            )
        return _json(
            {
                "board_id": board,
                "exists": True,
                "scene": row["scene"],
                "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
            }
        )

    # PUT — browser save. Cookie sessions must present the CSRF token; bearer
    # callers (agents) have their own write path via graph_canvas_write.
    guard_mutation = _guard_mutation(request)
    if guard_mutation is not None:
        return guard_mutation
    identity = request.scope.get("ua_identity") or "ui_user"
    try:
        scene = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(scene, dict):
        return _json({"error": "scene must be a JSON object"}, status=400)
    elements = scene.get("elements", [])
    if not isinstance(elements, list):
        return _json({"error": "scene.elements must be an array"}, status=400)
    if len(elements) > 1000:
        return _json({"error": "scene exceeds 1000 elements"}, status=400)

    try:
        saved = await anyio.to_thread.run_sync(
            lambda: storage.put_graph_canvas_scene(board, scene, identity)
        )
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)
    return _json(
        {
            "board_id": saved["board_id"],
            "saved": True,
            "updated_by": saved["updated_by"],
            "updated_at": saved["updated_at"],
        }
    )


async def session_capture_ingest(request: Request) -> Response:
    """Passive session-end capture (task #813).

    A Claude Code SessionEnd hook POSTs ``{transcript_path, cwd, project?}``; the
    hub summarizes the transcript tail through ``llm_client`` (subscription
    first, then OpenRouter) and stores a ``session-capture`` memory note. Bare
    ``_guard`` (bearer token, no CSRF) — the caller is a script, not a browser.
    Dedup is handled by ``memory_save``'s idempotency key on the segment hash.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    if not request.scope.get("ua_identity"):
        # The memory note is attributed to the verified token identity; an
        # anonymous capture has no author to stamp.
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    transcript_path = str(body.get("transcript_path") or "").strip()
    cwd = str(body.get("cwd") or "").strip()
    project = body.get("project")
    project = str(project).strip() if project else None
    if not transcript_path or not cwd:
        return _json({"error": "transcript_path and cwd are required"}, status=400)
    if not Path(transcript_path).is_file():
        return _json({"error": f"transcript not found: {transcript_path}"}, status=404)

    try:
        result = await anyio.to_thread.run_sync(
            lambda: session_capture.capture_session(transcript_path, cwd, project)
        )
    except llm_client.LLMConnectionError as exc:
        # Fail loud, not silent: every provider leg was unreachable. The caller
        # (and its logs) get the route so "which leg failed" is answerable.
        return _json({"error": str(exc), "route": exc.route}, status=502)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400)
    return _json(result)


# --- inbox endpoints for hooks (task #1861) -------------------------------------
# A Claude Code hook is a short-lived process that cannot hold an MCP session. These
# two routes let it register its session and read an unread-mail digest over plain
# HTTP with the agent's hub bearer token. Both are bearer-only, like the routine
# trigger: a browser cookie or OAuth session must never act as a hook credential,
# which is also why neither needs a CSRF token (the CSRF guard defends cookies).

INBOX_FIELD_MAX = 512
INBOX_SESSION_KIND = "claude_code"


def _inbox_caller(request: Request) -> tuple[str | None, Response | None]:
    """Guard an inbox route; return ``(agent, None)`` or ``(None, error response)``."""
    guard = _guard(request)
    if guard is not None:
        return None, guard
    identity = request.scope.get("ua_identity")
    if request.scope.get("ua_identity_via") != "bearer" or not identity:
        return None, _json({"error": "hub bearer token required"}, status=401)
    return str(identity), None


def _inbox_field(body: dict, key: str) -> tuple[str | None, str | None]:
    """A trimmed optional string field from the body, or ``(None, error)``."""
    value = body.get(key)
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, f"{key} must be a string"
    value = value.strip()
    if len(value) > INBOX_FIELD_MAX:
        return None, f"{key} must be at most {INBOX_FIELD_MAX} characters"
    return value or None, None


def _register_inbox_session(
    agent: str, external_id: str, host: str | None, cwd: str | None, wake: bool = False
) -> dict | None:
    registered = registry.register_session(
        agent,
        INBOX_SESSION_KIND,
        external_session_id=external_id,
        host=host,
        meta={"cwd": cwd} if cwd else None,
    )
    if registered is None:
        return None
    session_id, created = registered
    # The wake request (task #1873) lives in session_wake, not meta (meta is
    # written on insert only). Every registration restates it, so one that
    # omits it clears it. It never grants: only the operator does (task #2050).
    message_wake.set_opt_in(session_id, wake)
    state = message_wake.wake_states([session_id]).get(session_id) or {}
    if created:
        from ..lifecycle.capture import record_lifecycle_observation

        record_lifecycle_observation(
            kind="session_start",
            session_id=session_id,
            agent=agent,
            payload={"kind": INBOX_SESSION_KIND, "branch": None, "worktree_present": False},
            idempotency_key=f"session_start:{session_id}",
        )
    return {
        "session_id": session_id,
        "agent": agent,
        "wake": wake,
        "wake_granted": bool(state.get("granted")),
    }


async def inbox_session_register(request: Request) -> Response:
    """Register (or refresh) the caller's session:
    ``{external_session_id, host?, cwd?, wake?}``.

    Idempotent on ``(agent, external_session_id)``; every call heartbeats the session.
    The agent is the verified bearer's label, never a body field. ``wake: true`` (a
    JSON boolean, sent by a hook launched with ``UA_WAKE=1``) requests idle wakes
    (task #1873); a registration without it withdraws the request. A request alone
    never wakes: the operator grants each session (``session_wake_grant``, #2050),
    and ``wake_granted`` in the answer says whether that grant stands.
    """
    agent, denied = _inbox_caller(request)
    if denied is not None:
        return denied
    if not profiles.tool_is_allowed_for(profiles.current(), "session_start"):
        return _json({"error": "session_start is not allowed for this profile"}, status=403)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    fields = {}
    for key in ("external_session_id", "host", "cwd"):
        fields[key], error = _inbox_field(body, key)
        if error:
            return _json({"error": error}, status=400)
    if not fields["external_session_id"]:
        return _json({"error": "external_session_id is required"}, status=400)
    wake = body.get("wake", False)
    if not isinstance(wake, bool):
        return _json({"error": "wake must be a boolean"}, status=400)
    result = await anyio.to_thread.run_sync(
        lambda: _register_inbox_session(
            agent, fields["external_session_id"], fields["host"], fields["cwd"], wake
        )
    )
    if result is None:
        return _json({"error": "could not register session"}, status=503)
    return _json(result)


async def inbox_session_owed(request: Request) -> Response:
    """What the caller's session owes at a turn end: ``{session_id}`` (task #2389).

    For the Stop hook. Report only: the answer lists debts and refuses nothing.
    Another agent's session reads as missing.
    """
    agent, denied = _inbox_caller(request)
    if denied is not None:
        return denied
    if not profiles.tool_is_allowed_for(profiles.current(), "session_start"):
        return _json({"error": "session_start is not allowed for this profile"}, status=403)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    session_id, error = _inbox_field(body, "session_id")
    if error or not session_id:
        return _json({"error": error or "session_id is required"}, status=400)
    from ..session_owed import check_turn

    report = await anyio.to_thread.run_sync(lambda: check_turn(agent, session_id))
    if report is None:
        return _json({"error": f"session {session_id!r} is not a session of {agent!r}"}, status=404)
    return _json(report)


def _inbox_summary_sync(
    agent: str, session_id: str | None, limit: int, after_seq: int | None
) -> tuple[dict | None, str | None]:
    """One connection: validate and heartbeat the session, then read the digest."""
    conn = storage.connect()
    try:
        if session_id is not None:
            # Another tenant's session reads as missing (task #1995).
            row = conn.execute(
                "SELECT agent FROM agent_sessions WHERE id=? AND org_id IS ?",
                (session_id, edition.tenant_org_id()),
            ).fetchone()
            if row is None or row["agent"] != agent:
                # One answer for "no such session" and "someone else's", as the MCP tools do.
                return None, f"session {session_id!r} is not a session of {agent!r}"
            registry.touch_session(session_id, conn)
        summary = message_index.inbox_summary(
            conn, agent, session_id, limit=limit, after_seq=after_seq
        )
    finally:
        conn.close()
    return {"agent": agent, "session_id": session_id, **summary}, None


#: Largest value SQLite binds as an integer; anything above it raises OverflowError.
_SQLITE_INT_MAX = 2**63 - 1


def _inbox_int(request: Request, name: str, default: int | None, minimum: int):
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default, None
    try:
        value = int(raw)
    except ValueError:
        return None, f"{name} must be an integer"
    if value < minimum:
        return None, f"{name} must be at least {minimum}"
    if value > _SQLITE_INT_MAX:
        return None, f"{name} must be at most {_SQLITE_INT_MAX}"
    return value, None


async def inbox_summary(request: Request) -> Response:
    """Unread-mail digest for hooks: ``?session_id=&limit=5&after_seq=``. Never marks mail read.

    Covers the caller's label mail plus, with ``session_id``, that session's own mail.
    Newest first; ``unread`` and ``newest_seq`` span all unread mail, not just the page.
    """
    agent, denied = _inbox_caller(request)
    if denied is not None:
        return denied
    if not profiles.tool_is_allowed_for(profiles.current(), "message_list"):
        return _json({"error": "message_list is not allowed for this profile"}, status=403)
    limit, error = _inbox_int(request, "limit", 5, 1)
    after_seq, error2 = (None, None) if error else _inbox_int(request, "after_seq", None, 0)
    if error or error2:
        return _json({"error": error or error2}, status=400)
    session_id = (request.query_params.get("session_id") or "").strip() or None
    if session_id is not None and len(session_id) > INBOX_FIELD_MAX:
        return _json({"error": "session_id is too long"}, status=400)
    result, error = await anyio.to_thread.run_sync(
        lambda: _inbox_summary_sync(
            agent, session_id, min(limit, message_index.SUMMARY_MAX_LIMIT), after_seq
        )
    )
    if error:
        return _json({"error": error}, status=403)
    return _json(result)


# --- Messages pane (task #1864) -------------------------------------------------
# Operator reads and writes over the Messaging A/B layer. Reads follow the
# message_list scope (messaging.conversations): the operator login and the
# chief-of-staff profile see every conversation, any other identity only mail it
# sent or received. The send goes through the registered message_send tool, so its
# access, policy, approval and rate-limit gates run as for an MCP call, and the
# sender is always the verified identity: a body that names a sender is refused.
# All four routes need a verified identity, even in warn mode. The verified human
# operator passes the profile ceiling, as on the action routes (task #1865).

#: Acts the composer offers. done and handoff stay MCP-only.
MESSAGE_COMPOSER_ACTS = ("request", "inform", "query")
MESSAGE_SEND_FIELDS = frozenset(
    {"act", "to_agent", "to_session", "body", "reply_to", "message_id", "expects_reply"}
)
#: Floor for the raw request cap; the cap grows with the configured body cap.
MESSAGE_REQUEST_MIN_CAP = 64 * 1024


def _message_request_max_bytes() -> int:
    """Raw request bodies above this are refused before parsing.

    Room for the largest body the hub accepts (``UA_MESSAGE_MAX_BYTES``) after JSON
    escaping, which spends at most six bytes (``\\uXXXX``) per body byte, plus the
    other fields; never below 64 KiB.
    """
    return max(MESSAGE_REQUEST_MIN_CAP, 6 * config.message_max_body_bytes() + 4096)


MESSAGE_ID_MIN, MESSAGE_ID_MAX = 16, 64
#: send_message's refusal of a message_id that another sender already used (#1869).
_MESSAGE_ID_TAKEN = "already used by another message"
_MESSAGE_ID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def _messages_caller(request: Request, tool: str) -> tuple[str | None, Response | None]:
    """Identity and profile ceiling for a Messages route (the guard already ran).

    The same rule as the route's ``operator=True`` scope: the verified human operator
    passes (``_operator_recovery``), anyone else needs ``tool`` in the request's profile.
    """
    identity = _writer_identity(request)
    if identity is None:
        return None, _json({"error": "authenticated identity required"}, status=401)
    if not _operator_recovery(request) and not profiles.tool_is_allowed_for(
        _request_profile(request), tool
    ):
        return None, _json({"error": f"{tool} is unavailable to this profile"}, status=403)
    return identity, None


def _messages_text_param(request: Request, name: str) -> tuple[str | None, str | None]:
    value = (request.query_params.get(name) or "").strip()
    if len(value) > INBOX_FIELD_MAX:
        return None, f"{name} must be at most {INBOX_FIELD_MAX} characters"
    return value or None, None


async def messages_conversations(request: Request) -> Response:
    """Conversations the caller may see.

    ``?participant=&filter=all|unread|waiting&limit=50&before_seq=`` (the previous
    page's ``next_before_seq``).
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    _, denied = _messages_caller(request, "message_list")
    if denied is not None:
        return denied
    from ..messaging import conversations

    participant, error = _messages_text_param(request, "participant")
    limit, error2 = (None, None) if error else _inbox_int(request, "limit", 50, 1)
    before_seq, error3 = (
        (None, None) if error or error2 else _inbox_int(request, "before_seq", None, 1)
    )
    view = request.query_params.get("filter") or "all"
    if error or error2 or error3:
        return _json({"error": error or error2 or error3}, status=400)
    if view not in conversations.CONVERSATION_FILTERS:
        return _json(
            {"error": f"filter must be one of {', '.join(conversations.CONVERSATION_FILTERS)}"},
            status=400,
        )
    result = await anyio.to_thread.run_sync(
        lambda: conversations.list_conversations(
            participant=participant, filter=view, limit=limit, before_seq=before_seq
        )
    )
    # The composer's byte counter and send button follow the hub's configured cap.
    result["max_body_bytes"] = config.message_max_body_bytes()
    return _json(result)


async def messages_thread(request: Request) -> Response:
    """One conversation's visible messages, oldest first: ``?after_seq=|before_seq=&limit=200``.

    404 covers "no such conversation" and "not yours" alike.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    _, denied = _messages_caller(request, "message_list")
    if denied is not None:
        return denied
    from ..messaging import conversations

    conversation_id = str(request.path_params.get("conversation_id") or "").strip()
    if not conversation_id or len(conversation_id) > INBOX_FIELD_MAX:
        return _json({"error": "invalid conversation_id"}, status=400)
    limit, error = _inbox_int(request, "limit", 200, 1)
    after_seq, error2 = (None, None) if error else _inbox_int(request, "after_seq", None, 0)
    before_seq, error3 = (
        (None, None) if error or error2 else _inbox_int(request, "before_seq", None, 1)
    )
    if error or error2 or error3:
        return _json({"error": error or error2 or error3}, status=400)
    if after_seq is not None and before_seq is not None:
        return _json({"error": "pass after_seq or before_seq, not both"}, status=400)
    try:
        result = await anyio.to_thread.run_sync(
            lambda: conversations.conversation_thread(
                conversation_id, limit=limit, after_seq=after_seq, before_seq=before_seq
            )
        )
    except ToolError as exc:
        return _json_body_error(exc)
    if result is None:
        return _json({"error": "conversation not found"}, status=404)
    return _json(result)


async def messages_live_sessions(request: Request) -> Response:
    """Live sessions a message can target (``to_session``): ``?agent=``. No paths.

    ``/api/sessions`` lists every session with paths and transcripts and pages
    before filtering for liveness; the picker needs only live targets.
    """
    guard = _guard(request)
    if guard is not None:
        return guard
    _, denied = _messages_caller(request, "session_list")
    if denied is not None:
        return denied
    agent, error = _messages_text_param(request, "agent")
    if error:
        return _json({"error": error}, status=400)
    sessions = await anyio.to_thread.run_sync(
        lambda: registry.list_live_sessions(agent=agent, limit=200)
    )
    return _json({"sessions": sessions, "total": len(sessions)})


async def _read_capped_body(request: Request, limit: int) -> tuple[bytes, Response | None]:
    """Read at most ``limit`` bytes of a request body, refusing more without buffering it.

    A declared ``Content-Length`` over the cap is refused before anything is read;
    a chunked or understated body is cut off as soon as the stream passes the cap.
    Same pattern as ``_passkey_json`` and the Slack ingress.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            return b"", _json({"error": "invalid Content-Length"}, status=400)
        if size < 0:
            return b"", _json({"error": "invalid Content-Length"}, status=400)
        if size > limit:
            return b"", _json({"error": "request body too large"}, status=413)
    chunks: list[bytes] = []
    read = 0
    async for chunk in request.stream():
        read += len(chunk)
        if read > limit:
            return b"", _json({"error": "request body too large"}, status=413)
        chunks.append(chunk)
    return b"".join(chunks), None


def _message_send_args(body: dict) -> tuple[dict | None, str | None]:
    """Validate a composed message; return the ``message_send`` kwargs or an error."""
    if "from_agent" in body or "from_session" in body:
        return None, "the sender is the authenticated identity; do not name one"
    unknown = sorted(set(body) - MESSAGE_SEND_FIELDS)
    if unknown:
        return None, "unknown field(s): " + ", ".join(unknown)
    act = body.get("act")
    if act not in MESSAGE_COMPOSER_ACTS:
        return None, f"act must be one of {', '.join(MESSAGE_COMPOSER_ACTS)}"
    text = body.get("body")
    if not isinstance(text, str) or not text.strip():
        return None, "body is required"
    max_bytes = config.message_max_body_bytes()
    if len(text.encode("utf-8")) > max_bytes:
        return None, f"body exceeds {max_bytes} bytes"
    mid = body.get("message_id")
    if (
        not isinstance(mid, str)
        or not MESSAGE_ID_MIN <= len(mid) <= MESSAGE_ID_MAX
        or not set(mid) <= _MESSAGE_ID_CHARS
    ):
        return None, (
            f"message_id is required: {MESSAGE_ID_MIN}-{MESSAGE_ID_MAX} letters, digits, "
            "'-' or '_', generated once per composed message and reused on retry"
        )
    expects_reply = body.get("expects_reply", False)
    if not isinstance(expects_reply, bool):
        return None, "expects_reply must be a boolean"
    args: dict = {"act": act, "body": text, "message_id": mid, "expects_reply": expects_reply}
    for key in ("to_agent", "to_session", "reply_to"):
        value = body.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > INBOX_FIELD_MAX:
            return None, f"{key} must be a string of at most {INBOX_FIELD_MAX} characters"
        if value.strip():
            args[key] = value.strip()
    if "to_agent" not in args and "to_session" not in args:
        return None, "to_agent or to_session is required"
    return args, None


async def messages_send(request: Request) -> Response:
    """Send one message as the caller. Idempotent on ``message_id``.

    Body: ``{act, body, message_id, to_agent?, to_session?, reply_to?, expects_reply?}``.
    201 on a new send, 200 when ``message_id`` was already sent by this caller (a
    retry), 409 when another sender holds that id, 202 when an approval gate parks it.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity, denied = _messages_caller(request, "message_send")
    if denied is not None:
        return denied
    raw, too_big = await _read_capped_body(request, _message_request_max_bytes())
    if too_big is not None:
        return too_big
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    args, error = _message_send_args(body)
    if error:
        return _json({"error": error}, status=400)
    try:
        result = await api.call_tool("message_send", **args)
    except approvals_tool.ApprovalPendingError as exc:
        return _json(
            {"approval_id": exc.approval_id, "code": exc.code, "status": "pending"}, status=202
        )
    except (agent_access.AgentAccessDenied, ResourceError) as exc:
        return _json({"error": str(exc)[:300]}, status=403)
    except policy.PolicyViolation as exc:
        # As _governed_read answers: a throttle is 429 (try later), any other policy
        # refusal 403. Neither is a 400 the pane would invite the operator to retry.
        status = 429 if exc.reason_code in _THROTTLED_REASONS else 403
        return _json(
            {"error": str(exc)[:300], "reason_code": exc.reason_code or "policy_denied"},
            status=status,
        )
    except ToolError as exc:
        if _MESSAGE_ID_TAKEN in str(exc):
            # send_message refuses an id another sender holds (#1869); same answer as below.
            return _json({"error": "message_id is already in use"}, status=409)
        return _json_body_error(exc)
    except Exception as exc:  # noqa: BLE001 - bounded operator error (policy and others)
        return _json_body_error(exc)
    if not isinstance(result, dict):
        return _json({"error": "message_send returned no result"}, status=502)
    if result.get("duplicate"):
        if result.get("from_agent") != identity:
            # Never echo another sender's receipt back.
            return _json({"error": "message_id is already in use"}, status=409)
        return _json(result)
    return _json(result, status=201)


async def messages_handoff(request: Request) -> Response:
    """Hand a visible conversation off to a project task (task #1426).

    Body: ``{task_id}`` to link an existing task, or ``{title, project?, priority?}``
    to create one. The task references the conversation by id; no message is
    copied. 201 when a task was created, 200 when one was linked or the same
    handoff repeats, 202 when the task write parks for approval (it passes the
    gates a direct ``task_add`` / ``task_update`` does), 403 or 429 when policy
    refuses it, 404 for a conversation the caller cannot see, and 409 when an
    identical handoff is still in progress (retry; it returns the same task).
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    # The route's declared ceiling: the operator, or a profile that may both
    # read its mail and add tasks.
    _, denied = _messages_caller(request, "message_list")
    if denied is None:
        _, denied = _messages_caller(request, "task_add")
    if denied is not None:
        return denied
    conversation_id = str(request.path_params.get("conversation_id") or "").strip()
    if not conversation_id or len(conversation_id) > INBOX_FIELD_MAX:
        return _json({"error": "invalid conversation_id"}, status=400)
    raw, too_big = await _read_capped_body(request, 8_192)
    if too_big is not None:
        return too_big
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict) or set(body) - {"task_id", "title", "project", "priority"}:
        return _json({"error": "body takes task_id, or title with project and priority"}, 400)
    task_id = body.get("task_id")
    if task_id is not None and (type(task_id) is not int or task_id < 1):
        return _json({"error": "task_id must be a positive integer"}, status=400)
    for field in ("title", "project", "priority"):
        if body.get(field) is not None and not isinstance(body[field], str):
            return _json({"error": f"{field} must be a string"}, status=400)
    if task_id is not None:
        # Linking writes to an existing task through the registered task_update,
        # which runs under the caller's own profile with no operator override:
        # the operator profile does not expose task_update, so the operator
        # creates a task here rather than linking one.
        if not profiles.tool_is_allowed_for(_request_profile(request), "task_update"):
            return _json(
                {
                    "error": "linking needs task_update, which this profile does not expose; "
                    "create a task instead"
                },
                status=403,
            )
    from ..messaging import handoff

    try:
        result = await anyio.to_thread.run_sync(
            lambda: handoff.hand_off_for_route(
                conversation_id,
                task_id=task_id,
                title=body.get("title"),
                project=body.get("project"),
                priority=body.get("priority") or "med",
            )
        )
    except approvals_tool.ApprovalPendingError as exc:
        return _json(
            {"approval_id": exc.approval_id, "code": exc.code, "status": "pending"}, status=202
        )
    except (agent_access.AgentAccessDenied, ResourceError) as exc:
        return _json({"error": str(exc)[:300]}, status=403)
    except policy.PolicyViolation as exc:
        # As messages_send answers: a throttle is 429 (try later), any other
        # policy refusal 403.
        status = 429 if exc.reason_code in _THROTTLED_REASONS else 403
        return _json(
            {"error": str(exc)[:300], "reason_code": exc.reason_code or "policy_denied"},
            status=status,
        )
    except ToolError as exc:
        if "conversation not found" in str(exc):
            return _json({"error": "conversation not found"}, status=404)
        if handoff.IN_PROGRESS in str(exc):
            return _json(
                {"error": str(exc)[:300], "retry": True},
                status=409,
                headers={"Retry-After": "1"},
            )
        if "is not exposed by the active" in str(exc):
            # The registered tool's own profile check refused the write.
            return _json_body_error(exc, status=403)
        return _json_body_error(exc)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc)
    return _json(result, status=201 if result.get("created") else 200)


async def messages_handoffs(request: Request) -> Response:
    """Tasks a visible conversation was handed off to (task #1426). Read-only."""
    guard = _guard(request)
    if guard is not None:
        return guard
    _, denied = _messages_caller(request, "message_list")
    if denied is not None:
        return denied
    conversation_id = str(request.path_params.get("conversation_id") or "").strip()
    if not conversation_id or len(conversation_id) > INBOX_FIELD_MAX:
        return _json({"error": "invalid conversation_id"}, status=400)
    from ..messaging import handoff

    rows = await anyio.to_thread.run_sync(lambda: handoff.conversation_handoffs(conversation_id))
    return _json({"conversation_id": conversation_id, "handoffs": rows})


# --- ua-node (task #1327, I1) ---------------------------------------------------
# A device running `unified-agent node` calls these with its device token. They are
# POST-only and bearer-authenticated; a node never listens, it only calls out.

NODE_CLAIM_MAX_WAIT_S = 30.0
NODE_CLAIM_POLL_S = 1.0


async def _node_request(
    request: Request, allowed: set[str], *, device_plane: bool = True
) -> tuple[dict | None, str | None, Response | None]:
    guard = _guard_mutation(request)
    if guard is not None:
        return None, None, guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return None, None, _json({"error": "authenticated identity required"}, status=401)
    if device_plane and not config.node_allow_bearer():
        meta = config.current_auth_metadata() or {}
        if request.scope.get("ua_identity_via") != "oauth" or (
            meta.get("auth_method") != "device_code"
        ):
            return (
                None,
                None,
                _json(
                    {
                        "error": "a device sign-in is required (run `unified-agent login`); "
                        "static bearers need UA_NODE_ALLOW_BEARER=1 on the hub"
                    },
                    status=403,
                ),
            )
    raw = await request.body() or b""
    proof_error = await anyio.to_thread.run_sync(lambda: _device_key_proof_response(request, raw))
    if proof_error is not None:
        return None, None, proof_error
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        return None, None, _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return None, None, _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - allowed)
    if unknown:
        return None, None, _json({"error": "unknown field(s): " + ", ".join(unknown)}, status=400)
    return body, identity, None


def _node_error(exc: Exception) -> Response:
    status = getattr(exc, "status", 400)
    body: dict[str, object] = {"error": str(exc)[:300]}
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        # A machine-readable reason the device keys on (``node_retired``, #2181).
        body["code"] = code
    return _json(body, status=status)


def _request_htu(request: Request) -> str:
    """Path the device proof is bound to, including a query when present."""
    path = request.url.path
    return f"{path}?{request.url.query}" if request.url.query else path


def _device_key_proof_response(request: Request, raw: bytes) -> Response | None:
    """403 unless a device-key token proves possession of its private key.

    Tailnet bindings and static bearers are unchanged: only a ``device-key:``
    claim requires the header. The check runs before the queue so a copied
    token cannot register, heartbeat, or claim.
    """
    from .. import device_key

    binding = (config.current_auth_metadata() or {}).get("device_node")
    if not device_key.is_key_binding(binding):
        return None
    header = request.headers.get("authorization") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    try:
        device_key.verify_proof(
            request.headers.get(device_key.PROOF_HEADER, ""),
            method=request.method,
            path=_request_htu(request),
            access_token=token,
            body=raw,
            expected_node_id=str(binding.get("node_id") or ""),
        )
    except device_key.DeviceKeyError as exc:
        return _json({"error": str(exc)[:300]}, status=403)
    return None


def _device_binding_name() -> str | None:
    meta = config.current_auth_metadata() or {}
    binding = meta.get("device_node")
    if isinstance(binding, dict):
        return str(binding.get("node") or binding.get("node_id") or "") or None
    return str(binding) if binding else None


async def node_register(request: Request) -> Response:
    """Register this device's host facts under the caller's identity."""
    body, identity, err = await _node_request(request, {"node", "facts", "version"})
    if err is not None:
        return err
    # Read in the request context, not inside the worker-thread lambda.
    device = _device_binding_name()
    try:
        result = await anyio.to_thread.run_sync(
            lambda: node_queue.register(
                body.get("node"),
                identity=identity,
                facts=body.get("facts"),
                version=body.get("version"),
                device_node=device,
            )
        )
    except node_queue.NodeError as exc:
        return _node_error(exc)
    return _json(result)


async def node_heartbeat(request: Request) -> Response:
    """Liveness + lease renewal; the response relays cancellation for the running item."""
    body, identity, err = await _node_request(request, {"node", "running", "facts", "progress"})
    if err is not None:
        return err
    # Read in the request context, not inside the worker-thread lambda.
    device = _device_binding_name()
    try:
        result = await anyio.to_thread.run_sync(
            lambda: node_queue.heartbeat(
                body.get("node"),
                identity=identity,
                running=body.get("running"),
                facts=body.get("facts"),
                progress=body.get("progress"),
                device_node=device,
            )
        )
    except node_queue.NodeError as exc:
        return _node_error(exc)
    return _json(result)


async def node_claim(request: Request) -> Response:
    """Long-poll: claim the next queued item for this node, waiting up to ``wait_s``.

    With ``claim_op`` (task #2136) the claim belongs to one durable logical
    operation: every poll settles it in the claiming transaction, a
    ``reconcile`` returns its one disposition without selecting work, and the
    response carries that disposition next to ``item``.
    """
    body, identity, err = await _node_request(request, {"node", "wait_s", "claim_op"})
    if err is not None:
        return err
    # Read in the request context, not inside the worker-thread lambda.
    device = _device_binding_name()
    try:
        wait_s = float(body.get("wait_s") or 0)
    except (TypeError, ValueError):
        return _json({"error": "wait_s must be a number"}, status=400)
    wait_s = max(0.0, min(wait_s, NODE_CLAIM_MAX_WAIT_S))
    deadline = anyio.current_time() + wait_s
    if "claim_op" in body:
        return await _node_claim_operation(request, body, identity, device, deadline)
    while True:
        # Never hand an item to a client that already hung up: it would sit
        # claimed until its lease expired.
        if await request.is_disconnected():
            return _json({"item": None, "reason": "client_disconnected"}, status=499)
        try:
            item = await anyio.to_thread.run_sync(
                lambda: node_queue.claim_next(
                    body.get("node"), identity=identity, device_node=device
                )
            )
        except node_queue.NodeError as exc:
            return _node_error(exc)
        remaining = deadline - anyio.current_time()
        if item is not None or remaining <= 0:
            return _json({"item": item})
        await anyio.sleep(min(NODE_CLAIM_POLL_S, remaining))


async def _node_claim_operation(
    request: Request, body: dict, identity: str, device: str | None, deadline: float
) -> Response:
    """The ``claim_op`` long poll: one operation, settled once, never re-selected.

    A disconnect leaves the operation ``pending`` (the worker reconciles it); it
    is never evidence that nothing was claimed.
    """
    raw_op = body.get("claim_op")
    reconcile = isinstance(raw_op, dict) and raw_op.get("mode") == "reconcile"
    while True:
        if await request.is_disconnected():
            return _json({"item": None, "reason": "client_disconnected"}, status=499)
        final = reconcile or deadline - anyio.current_time() <= 0
        try:
            result = await anyio.to_thread.run_sync(
                lambda final=final: node_queue.claim_operation(
                    body.get("node"),
                    raw_op,
                    identity=identity,
                    device_node=device,
                    final=final,
                )
            )
        except node_queue.NodeError as exc:
            return _node_error(exc)
        if result["claim_op"]["state"] != "pending":
            return _json(result)
        remaining = deadline - anyio.current_time()
        await anyio.sleep(max(0.0, min(NODE_CLAIM_POLL_S, remaining)))


async def node_receipt(request: Request) -> Response:
    """Post the receipt for one claimed item. Idempotent: a resend returns the first."""
    body, identity, err = await _node_request(request, {"node", "item_id", "receipt"})
    if err is not None:
        return err
    # Read in the request context, not inside the worker-thread lambda.
    device = _device_binding_name()
    try:
        result = await anyio.to_thread.run_sync(
            lambda: node_queue.post_receipt(
                body.get("node"),
                body.get("item_id"),
                identity=identity,
                receipt=body.get("receipt"),
                device_node=device,
            )
        )
    except node_queue.NodeError as exc:
        return _node_error(exc)
    return _json(result, status=200 if result["duplicate"] else 201)


async def node_wake(request: Request) -> Response:
    """Long-poll: sessions on this node's host that are due an idle wake (task #1873).

    Mirrors ``/api/node/claim``. The answer is ids and counts only
    (``messaging.wake.due_wakes``); the node types the fixed wake line itself.
    """
    body, identity, err = await _node_request(
        request, {"node", "wait_s", "offer_version", "live_sessions"}
    )
    if err is not None:
        return err
    if type(body.get("offer_version")) is not int or body["offer_version"] != 3:
        return _json({"error": "idle wakes require offer_version=3"}, status=400)
    # Read in the request context, not inside the worker-thread lambda.
    device = _device_binding_name()
    try:
        wait_s = float(body.get("wait_s") or 0)
    except (TypeError, ValueError):
        return _json({"error": "wait_s must be a number"}, status=400)
    if not math.isfinite(wait_s):
        return _json({"error": "wait_s must be a number"}, status=400)
    wait_s = max(0.0, min(wait_s, NODE_CLAIM_MAX_WAIT_S))
    live = body.get("live_sessions")
    if live is not None:
        if (
            not isinstance(live, list)
            or len(live) > message_wake.MAX_LIVE_SESSIONS
            or not all(
                isinstance(e, str) and 0 < len(e) <= message_wake.MAX_EXTERNAL_ID_CHARS
                for e in live
            )
        ):
            return _json({"error": "live_sessions must be a short list of ids"}, status=400)
        try:
            await anyio.to_thread.run_sync(
                lambda: message_wake.note_live_sessions(
                    body.get("node"), live, identity=identity, device_node=device
                )
            )
        except node_queue.NodeError as exc:
            return _node_error(exc)
    deadline = anyio.current_time() + wait_s
    while True:
        # A client that hung up must not take offers it will never deliver.
        if await request.is_disconnected():
            return _json({"wakes": [], "reason": "client_disconnected"}, status=499)
        try:
            wakes = await anyio.to_thread.run_sync(
                lambda: message_wake.due_wakes(
                    body.get("node"), identity=identity, device_node=device
                )
            )
        except node_queue.NodeError as exc:
            return _node_error(exc)
        remaining = deadline - anyio.current_time()
        if wakes or remaining <= 0:
            return _json({"wakes": wakes})
        await anyio.sleep(min(NODE_CLAIM_POLL_S, remaining))


async def node_wake_check(request: Request) -> Response:
    """One-shot attempt admission with live consent, generation, expiry and budget (#2068)."""
    body, identity, err = await _node_request(
        request, {"node", "session_id", "seq", "expires_at", "offer_id"}
    )
    if err is not None:
        return err
    device = _device_binding_name()
    try:
        result = await anyio.to_thread.run_sync(
            lambda: message_wake.check_offer(
                body.get("node"),
                body.get("session_id"),
                seq=body.get("seq"),
                expires_at=body.get("expires_at"),
                offer_id=body.get("offer_id"),
                identity=identity,
                device_node=device,
            )
        )
    except node_queue.NodeError as exc:
        return _node_error(exc)
    return _json(result)


async def node_wake_receipt(request: Request) -> Response:
    """Record a wake outcome: ``{session_id, seq, outcome: delivered|failed|skipped, reason?}``.

    ``seq`` is the offer's ``newest_seq``; a receipt for a lapsed offer is a duplicate.
    """
    body, identity, err = await _node_request(
        request,
        {
            "node",
            "session_id",
            "seq",
            "outcome",
            "reason",
            "expires_at",
            "offer_id",
            "delivered_at",
        },
    )
    if err is not None:
        return err
    device = _device_binding_name()
    try:
        result = await anyio.to_thread.run_sync(
            lambda: message_wake.record_receipt(
                body.get("node"),
                body.get("session_id"),
                body.get("outcome"),
                body.get("reason"),
                seq=body.get("seq"),
                expires_at=body.get("expires_at"),
                offer_id=body.get("offer_id"),
                delivered_at=body.get("delivered_at"),
                identity=identity,
                device_node=device,
            )
        )
    except node_queue.NodeError as exc:
        return _node_error(exc)
    return _json(result)


async def node_session_credential(request: Request) -> Response:
    """Mint a session-scoped hub MCP credential for one claimed ACP item (task #1744).

    Device-code OAuth only, checked here as well as in ``_node_request``: a
    static bearer never mints, even with ``UA_NODE_ALLOW_BEARER=1``. The value
    is in this response once and nowhere else; the response is not cached and
    is never logged. Refusals carry a short ``reason`` so the node can record
    why its session fell back to no hub MCP.
    """
    from .. import session_credentials

    body, identity, err = await _node_request(request, {"node", "item_id", "attempt"})
    if err is not None:
        return err
    meta = config.current_auth_metadata() or {}
    if request.scope.get("ua_identity_via") != "oauth" or meta.get("auth_method") != "device_code":
        return _json(
            {
                "error": "a device sign-in is required to mint a session credential",
                "reason": "device_signin_required",
            },
            status=403,
        )
    # Read in the request context, not inside the worker-thread lambda.
    device = _device_binding_name()
    # The device sign-in family the credential stays bound to: it ends with it.
    signin_id = meta.get("signin_id")
    try:
        result = await anyio.to_thread.run_sync(
            lambda: session_credentials.mint(
                body.get("node"),
                body.get("item_id"),
                body.get("attempt"),
                identity=identity,
                device_node=device,
                signin_id=signin_id,
            )
        )
    except session_credentials.SessionCredentialError as exc:
        return _json({"error": str(exc)[:300], "reason": exc.reason}, status=exc.status)
    response = _json(result, status=201)
    response.headers["Cache-Control"] = "no-store"
    return response


async def action_session_credential_revoke(request: Request) -> Response:
    """Operator-only revoke of session credentials by credential, item or node (task #1744).

    Answers what it revoked (ids and their metadata, never a value). An optional
    ``Idempotency-Key`` replays the first answer (task #2516).
    """
    from .. import session_credentials
    from . import cc_knowledge

    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    if not _operator_recovery(request):
        return _json({"error": "revoking session credentials is an operator action"}, status=403)
    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _json({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "request body must be a JSON object"}, status=400)
    unknown = sorted(set(body) - {"credential_id", "item_id", "node"})
    if unknown:
        return _json({"error": "unknown field(s): " + ", ".join(unknown)}, status=400)
    selectors = {k: str(v).strip() for k, v in body.items() if v not in (None, "")}
    if len(selectors) != 1:
        return _json({"error": "pass exactly one of credential_id, item_id or node"}, status=400)

    async def revoke() -> Response:
        def run() -> dict:
            session_credentials.check_selectors(**selectors)
            # Past validation the revoke may commit: a failure from here on is
            # never a refusal (approvals.execution_probe, task #2516).
            approvals_tool.mark_probe_started()
            revoked = session_credentials.revoke(reason="operator", actor=identity, **selectors)
            return {
                "revoked": revoked,
                "count": len(revoked),
                "credentials": session_credentials.credential_metadata(revoked),
            }

        try:
            return _json(await anyio.to_thread.run_sync(run))
        except (ValueError, node_queue.NodeError) as exc:
            return _json({"error": str(exc)[:300]}, status=400)

    key = cc_knowledge._idempotency_key(request)
    if isinstance(key, Response):
        return key
    if key is None:
        return await revoke()
    # Task #2516: the same key replays what the first request revoked.
    return await cc_knowledge.respond_once(
        identity,
        key,
        "session_credential_revoke",
        selectors,
        revoke,
        check="the session credential list (GET /api/session-credentials?state=all)",
    )


async def node_dispatch(request: Request) -> Response:
    """Approval-gated: queue one item for a registered node."""
    from mcp.server.mcpserver.exceptions import ToolError

    body, identity, err = await _node_request(
        request,
        {
            "node",
            "prompt",
            "cwd",
            "backend",
            "model",
            "permission_mode",
            "timeout_s",
            "task_id",
            "run_id",
            "hermes_profile",
        },
        device_plane=False,
    )
    if err is not None:
        return err
    fields = {k: body.get(k) for k in body if k != "node"}
    try:
        result = await anyio.to_thread.run_sync(
            lambda: node_queue.request_dispatch(body.get("node"), actor=identity, **fields)
        )
    except approvals_tool.ApprovalPendingError as exc:
        return _json(
            {
                "status": "approval_pending",
                "approval_id": exc.approval_id,
                "code": exc.code,
                "tool": node_queue.NODE_DISPATCH_TOOL,
            },
            status=202,
        )
    except node_queue.NodeError as exc:
        return _node_error(exc)
    except (TypeError, ValueError, ToolError) as exc:
        return _json_body_error(exc, status=400)
    return _json(result, status=201)


async def node_events(request: Request) -> Response:
    """Batch of normalized ACP events for one claimed item (task #1328, I2).

    Durable in the evidence ledger; per-event and per-call bounds live in
    node_queue.record_node_event.
    """
    body, identity, err = await _node_request(request, {"node", "item_id", "events"})
    if err is not None:
        return err
    # Read in the request context, not inside the worker-thread lambda.
    device = _device_binding_name()
    try:
        result = await anyio.to_thread.run_sync(
            lambda: node_queue.record_node_event(
                body.get("node"),
                body.get("item_id"),
                body.get("events"),
                identity=identity,
                device_node=device,
            )
        )
    except node_queue.NodeError as exc:
        return _node_error(exc)
    return _json(result)


async def node_followups(request: Request) -> Response:
    """Operator follow-ups waiting for this node's claimed ACP item (task #1740).

    The node polls this while a turn runs and once after each turn ends, and
    acknowledges what it received (``ack``) on the next poll; only acknowledged
    rows move to ``claimed``. Delivery is reported on the event stream.
    """
    body, identity, err = await _node_request(request, {"node", "item_id", "ack"})
    if err is not None:
        return err
    # Read in the request context, not inside the worker-thread lambda.
    device = _device_binding_name()
    from .. import node_followups as followups

    try:
        result = await anyio.to_thread.run_sync(
            lambda: followups.claim_for_node(
                body.get("node"),
                body.get("item_id"),
                identity=identity,
                device_node=device,
                ack=body.get("ack"),
            )
        )
    except node_queue.NodeError as exc:
        return _node_error(exc)
    return _json(result)


async def node_permission(request: Request) -> Response:
    """Relay an ACP harness's permission prompt through the hub approval queue.

    Always a 200 status body — ``pending`` / ``approved``(+option_id) /
    ``denied`` — because the poll loop IS the protocol: the node re-POSTs the
    identical body until a human resolves the approval row or it gives up.
    """
    body, identity, err = await _node_request(
        request, {"node", "item_id", "session_id", "tool_call", "options"}
    )
    if err is not None:
        return err
    # Read in the request context, not inside the worker-thread lambda.
    device = _device_binding_name()
    payload = {
        "session_id": body.get("session_id"),
        "tool_call": body.get("tool_call"),
        "options": body.get("options"),
    }
    try:
        result = await anyio.to_thread.run_sync(
            lambda: node_queue.request_node_permission(
                body.get("node"),
                body.get("item_id"),
                payload,
                identity=identity,
                device_node=device,
            )
        )
    except node_queue.NodeError as exc:
        return _node_error(exc)
    return _json(result)


async def node_config(request: Request) -> Response:
    """GET|POST /api/node/config — hub-served harness bundle + drift (task #1330, I4).

    GET ``?node=`` returns the active bundle, rendered surfaces, and apply grants.
    POST ``action=report`` parks proposal approvals (Inbox + Approvals);
    ``action=applied`` consumes a grant after the node writes a surface.
    Writes stay proposal-mode until the operator approves.
    """
    if request.method == "GET":
        # Device plane uses the same auth as other /api/node/* routes, but GET
        # carries ``node`` on the query string (no JSON body).
        guard = _guard(request)
        if guard is not None:
            return guard
        identity = request.scope.get("ua_identity")
        if not identity:
            return _json({"error": "authenticated identity required"}, status=401)
        if not config.node_allow_bearer():
            meta = config.current_auth_metadata() or {}
            if request.scope.get("ua_identity_via") != "oauth" or (
                meta.get("auth_method") != "device_code"
            ):
                return _json(
                    {
                        "error": "a device sign-in is required (run `unified-agent login`); "
                        "static bearers need UA_NODE_ALLOW_BEARER=1 on the hub"
                    },
                    status=403,
                )
        node = request.query_params.get("node")
        if not node:
            return _json({"error": "node query parameter is required"}, status=400)
        proof_error = await anyio.to_thread.run_sync(
            lambda: _device_key_proof_response(request, b"")
        )
        if proof_error is not None:
            return proof_error
        device = _device_binding_name()
        try:
            result = await anyio.to_thread.run_sync(
                lambda: harness_config.node_config_payload(
                    node, identity=identity, device_node=device
                )
            )
        except node_queue.NodeError as exc:
            return _node_error(exc)
        except harness_config.HarnessConfigError as exc:
            return _json({"error": str(exc)[:300]}, status=exc.status)
        return _json(result)

    body, identity, err = await _node_request(
        request, {"node", "action", "proposals", "bundle_version", "surface", "desired_hash"}
    )
    if err is not None:
        return err
    device = _device_binding_name()
    action = body.get("action")
    try:
        if action in (None, "get"):
            result = await anyio.to_thread.run_sync(
                lambda: harness_config.node_config_payload(
                    body.get("node"), identity=identity, device_node=device
                )
            )
            return _json(result)
        if action == "report":
            result = await anyio.to_thread.run_sync(
                lambda: harness_config.report_drift(
                    body.get("node"),
                    body.get("proposals"),
                    identity=identity,
                    device_node=device,
                    bundle_version=body.get("bundle_version"),
                )
            )
            return _json(result)
        if action == "applied":
            result = await anyio.to_thread.run_sync(
                lambda: harness_config.mark_applied(
                    body.get("node"),
                    surface=str(body.get("surface") or ""),
                    desired_hash=str(body.get("desired_hash") or ""),
                    identity=identity,
                    device_node=device,
                )
            )
            return _json(result)
        return _json({"error": "action must be 'get', 'report', or 'applied'"}, status=400)
    except node_queue.NodeError as exc:
        return _node_error(exc)
    except harness_config.HarnessConfigError as exc:
        return _json({"error": str(exc)[:300]}, status=exc.status)
    except approvals_tool.ApprovalPendingError as exc:
        return _json(
            {
                "status": "approval_pending",
                "approval_id": exc.approval_id,
                "code": exc.code,
                "tool": harness_config.APPLY_TOOL,
            },
            status=202,
        )
    except ToolError as exc:
        return _json_body_error(exc, status=400)


async def harness_config_status(request: Request) -> Response:
    """GET /api/harness-config — active bundle + pending proposals (Control Center)."""
    guard = _guard(request)
    if guard is not None:
        return guard
    result = await anyio.to_thread.run_sync(harness_config.hub_status)
    return _json(result)


async def action_harness_config(request: Request) -> Response:
    """POST /api/action/harness-config — CSRF-gated publish (task #1330, I4).

    Publishing is a human Control Center action. Apply stays on the Approvals
    pane via ``harness_config_apply`` one-shot passes.
    """
    guard = _guard_mutation(request)
    if guard is not None:
        return guard
    identity = request.scope.get("ua_identity")
    if not identity:
        return _json({"error": "authenticated identity required"}, status=401)
    try:
        body = json.loads(await request.body() or b"{}")
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        action = body.get("action")
        if action != "publish":
            raise ValueError("action must be 'publish'")
        bundle = body.get("bundle")
        if not isinstance(bundle, dict):
            raise ValueError("bundle must be a JSON object")
        result = await anyio.to_thread.run_sync(
            lambda: harness_config.publish_bundle(bundle, actor=str(identity))
        )
    except harness_config.HarnessConfigError as exc:
        return _json({"error": str(exc)[:300]}, status=exc.status)
    except (KeyError, TypeError, ValueError) as exc:
        return _json({"error": f"invalid harness-config request: {exc}"}, status=400)
    except Exception as exc:  # noqa: BLE001 - bounded operator error
        return _json_body_error(exc, status=400, limit=200)
    return _json(result, status=201)


def _declared_routes() -> list[Route]:
    """Every route, as its handler registers it, before the scope gate is applied."""
    # Control Center v2 adapters (#2469, #2470, #2480, #2501, #2514, #2516)
    from . import (
        cc_agents,
        cc_inbox,
        cc_knowledge,
        cc_launch,
        cc_runs,
        cc_settings,
        cc_work,
    )

    return [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/readyz", readyz, methods=["GET"]),
        Route("/ui", ui_index, methods=["GET"]),
        Route("/ui/{path:path}", ui_modern_asset, methods=["GET"]),
        Route("/api/session", session_create, methods=["POST"]),
        Route("/api/session", session_delete, methods=["DELETE"]),
        Route(workos_signin.OPTIONS_PATH, workos_signin.session_options, methods=["GET"]),
        Route(workos_signin.LOGIN_PATH, workos_signin.workos_login, methods=["GET"]),
        Route(workos_signin.CALLBACK_PATH, workos_signin.workos_callback, methods=["GET"]),
        Route("/api/routines/{routine_id}/fire", routine_fire_http, methods=["POST"]),
        Route("/api/slack/events", slack_events, methods=["POST"]),
        Route("/api/whoami", whoami, methods=["GET"]),
        Route("/api/overview", _read_only(_overview), methods=["GET"]),
        Route("/api/attention", _read_only(_attention), methods=["GET"]),
        Route("/api/digest", _read_only(_digest), methods=["GET"]),
        Route("/api/agents", _read_only(_agents), methods=["GET"]),
        Route("/api/personas", _read_only(_personas), methods=["GET"]),
        Route("/api/personas/{label}", _read_only(_persona_detail), methods=["GET"]),
        Route("/api/org", _read_only(_org), methods=["GET"]),
        Route("/api/dispatch-routes", _read_only(_dispatch_routes), methods=["GET"]),
        Route("/api/agents/{label}", agent_detail, methods=["GET"]),
        Route("/api/sessions", _read_only(_sessions), methods=["GET"]),
        Route("/api/sessions/{id}", session_detail, methods=["GET"]),
        Route("/api/runs", _read_only(_runs), methods=["GET"]),
        Route("/api/runs/{run_id}", _read_only(_run_detail), methods=["GET"]),
        Route("/api/sessions/{id}/crew", session_crew_live, methods=["GET"]),
        Route("/api/sessions/{id}/wake-grant", session_wake_grant, methods=["POST"]),
        Route("/api/tasks", _read_only(_tasks), methods=["GET"]),
        Route("/api/tasks", task_create, methods=["POST"]),
        Route(
            "/api/tasks/next-work-projection", _read_only(_next_work_projection), methods=["POST"]
        ),
        Route("/api/tasks/search", _read_only(_task_search), methods=["GET"]),
        Route("/api/tasks/holds", task_holds_list, methods=["GET"]),
        Route("/api/tasks/holds", task_holds_create, methods=["POST"]),
        Route("/api/tasks/holds/{hold_id}/release", task_hold_release, methods=["POST"]),
        Route("/api/tasks/{task_id}/claim-release", task_claim_release_action, methods=["POST"]),
        Route("/api/goals", goals_collection, methods=["GET", "POST"]),
        Route("/api/goals/{goal_id}", goal_detail, methods=["GET"]),
        Route("/api/tasks/{task_id}/why-chain", task_why_chain, methods=["GET"]),
        Route("/api/tasks/{task_id}/comments", task_comment_thread, methods=["GET", "POST"]),
        Route("/api/work-graph", work_graph_view, methods=["GET"]),
        Route("/api/work-products", work_product_submit, methods=["POST"]),
        Route("/api/work-products/{id}/release", work_product_release, methods=["POST"]),
        Route("/api/board", _read_only(_board), methods=["GET"]),
        Route("/api/flow-metrics", _read_only(_flow_metrics), methods=["GET"]),
        Route("/api/queue-health", _read_only(_queue_health), methods=["GET"]),
        Route("/api/projects", _read_only(_projects), methods=["GET"]),
        Route("/api/review-approval", _read_only(_review_approval), methods=["GET"]),
        Route("/api/executor-delegation", _read_only(_executor_delegation), methods=["GET"]),
        Route("/api/approvals", _read_only(_approvals), methods=["GET"]),
        Route("/api/approvals/request", action_approval_request, methods=["POST"]),
        Route("/api/approvals/ledger-rerequest", approval_ledger_rerequest, methods=["POST"]),
        Route("/api/approvals/ledger-poll", approval_ledger_poll, methods=["POST"]),
        Route("/api/review/reads", review_read_record, methods=["POST"]),
        Route("/api/approvals/{id}", approval_detail, methods=["GET"]),
        Route("/api/memory", _read_only(_memory), methods=["GET"]),
        Route("/api/vault/search", vault_search, methods=["GET"]),
        Route("/api/vault/page", vault_page, methods=["GET"]),
        Route("/api/vault/ingest-status", vault_ingest_status, methods=["GET"]),
        Route("/api/vault/briefing", vault_briefing, methods=["GET"]),
        Route("/api/connectors", connectors, methods=["GET"]),
        Route("/api/connectors/oauth/{provider}", connector_oauth_status, methods=["GET"]),
        Route("/api/connectors/oauth/{provider}/begin", connector_oauth_begin, methods=["POST"]),
        Route(
            "/api/connectors/oauth/{provider}/disconnect",
            connector_oauth_disconnect,
            methods=["POST"],
        ),
        Route("/oauth/connectors/callback", connector_oauth_callback, methods=["GET"]),
        Route("/api/quota", quota, methods=["GET"]),
        Route("/api/credential_pool", credential_pool, methods=["GET"]),
        Route("/api/slack_gateway", slack_gateway_status, methods=["GET"]),
        Route("/api/notify_delivery", notify_delivery, methods=["GET"]),
        Route("/api/dispatch_receipts", dispatch_receipts, methods=["GET"]),
        Route("/api/agent-graph", _read_only(_agent_graph), methods=["GET"]),
        Route("/api/zulip", _read_only(_zulip), methods=["GET"]),
        Route("/api/execution-graph", _read_only(_execution_graph), methods=["GET"]),
        Route("/api/dispatch-stack", dispatch_stack, methods=["GET"]),
        Route("/api/cycles", _read_only(_cycles), methods=["GET"]),
        Route("/api/cycles/{cycle_id}", cycle_detail, methods=["GET"]),
        Route("/api/plans", _read_only(_plans), methods=["GET"]),
        Route("/api/plans/{plan_id}", plan_detail, methods=["GET"]),
        Route("/api/proposals", _read_only(_proposals), methods=["GET"]),
        Route("/api/org-proposals", _read_only(_org_proposals), methods=["GET"]),
        Route("/api/org-proposals/{proposal_id}", org_proposal_detail, methods=["GET"]),
        Route("/api/health", _read_only(_health), methods=["GET"]),
        Route("/api/cloud", _read_only(_cloud), methods=["GET"]),
        Route("/api/devices", _read_only(_devices), methods=["GET"]),
        Route("/api/nodes", _read_only(_nodes), methods=["GET"]),
        Route("/api/nodes/native-sessions", _read_only(_native_sessions), methods=["GET"]),
        Route("/api/nodes/acceptance", _read_only(_node_acceptance), methods=["GET"]),
        Route("/api/mobile/v1/projection", _read_only(_mobile_projection), methods=["GET"]),
        Route("/api/langgraph/checkpoints", _read_only(_langgraph_checkpoints), methods=["GET"]),
        Route("/api/evidence", _read_only(_evidence), methods=["GET"]),
        Route("/api/evidence", evidence_append, methods=["POST"]),
        Route("/api/platform/manifest", _read_only(_platform_manifest), methods=["GET"]),
        Route("/api/platform/objects/{kind}", platform_object, methods=["GET"]),
        Route("/api/platform/objects/{kind}/{object_id}", platform_object, methods=["GET"]),
        Route("/api/platform/provenance-graph", platform_provenance_graph, methods=["GET"]),
        Route("/api/capability-catalog", _read_only(_capability_catalog), methods=["GET"]),
        Route("/api/perf", _read_only(_perf), methods=["GET"]),
        Route("/api/delivery_slo", _read_only(_delivery_slo), methods=["GET"]),
        Route("/api/config-revisions", _read_only(_config_revisions), methods=["GET"]),
        Route("/api/operating", _read_only(_operating), methods=["GET"]),
        Route("/api/operating/resolve", _read_only(_operating_resolve), methods=["GET"]),
        Route("/api/operating/sprints", _read_only(_operating_sprints), methods=["GET"]),
        Route("/api/skills-teams", _read_only(_skills_teams), methods=["GET"]),
        Route("/api/teams", _read_only(_teams), methods=["GET"]),
        Route("/api/teams/plan", _read_only(_team_plan), methods=["GET"]),
        Route("/api/teams/snapshots", team_snapshots, methods=["GET", "POST"]),
        Route(
            "/api/teams/snapshots/{snapshot_id}/preview",
            team_snapshot_preview,
            methods=["GET"],
        ),
        Route(
            "/api/teams/snapshots/{snapshot_id}/restore",
            team_snapshot_restore,
            methods=["POST"],
        ),
        Route("/api/costs", _read_only(_costs), methods=["GET"]),
        Route("/api/tool-allowance", _read_only(_tool_allowance), methods=["GET"]),
        Route("/api/scorecards", _read_only(_scorecards), methods=["GET"]),
        Route("/api/events", _read_only(_events), methods=["GET"]),
        Route("/api/telemetry", _read_only(_telemetry), methods=["GET"]),
        Route("/api/events/stream", events_stream, methods=["GET"]),
        Route("/api/ledger/rows", ledger_rows, methods=["GET"]),
        Route("/api/action/approval", action_approval, methods=["POST"]),
        Route("/api/action/config-revision", action_config_revision, methods=["POST"]),
        Route("/api/action/operating-sprint", action_operating_sprint, methods=["POST"]),
        Route("/api/action/operating-run", action_operating_run, methods=["POST"]),
        Route("/api/action/skills-teams", action_skills_teams, methods=["POST"]),
        Route("/api/action/team-up", action_team_up, methods=["POST"]),
        Route("/api/action/task", action_task, methods=["POST"]),
        Route("/api/agent-lifecycle", agent_lifecycle_get, methods=["GET"]),
        Route("/api/agent-lifecycle", agent_lifecycle_action, methods=["POST"]),
        Route("/api/action/workflow", action_workflow, methods=["POST"]),
        Route("/api/action/dispatch", action_dispatch, methods=["POST"]),
        Route("/api/action/dispatch-stack", action_dispatch_stack, methods=["POST"]),
        Route("/api/action/dependency", action_dependency, methods=["POST"]),
        Route("/api/action/handoff", action_handoff, methods=["POST"]),
        Route("/api/action/crew-intervene", action_crew_intervene, methods=["POST"]),
        Route("/api/action/node-session", action_node_session, methods=["POST"]),
        Route("/api/action/node-followup", action_node_followup, methods=["POST"]),
        Route("/api/action/node-acceptance", action_node_acceptance, methods=["POST"]),
        Route("/api/action/node-retire", action_node_retire, methods=["POST"]),
        Route("/api/action/work-proposal-decide", action_work_proposal_decide, methods=["POST"]),
        Route("/api/action/node-resume", action_node_resume, methods=["POST"]),
        Route("/api/action/node-adopt", action_node_adopt, methods=["POST"]),
        Route("/api/action/cycle", action_cycle, methods=["POST"]),
        Route("/api/action/plan", action_plan, methods=["POST"]),
        Route("/api/action/proposal", action_proposal, methods=["POST"]),
        Route("/api/action/fleet", action_fleet, methods=["POST"]),
        Route("/api/action/oauth-revoke", action_oauth_revoke, methods=["POST"]),
        Route(
            "/api/action/session-credential-revoke",
            action_session_credential_revoke,
            methods=["POST"],
        ),
        Route("/api/action/harness-config", action_harness_config, methods=["POST"]),
        Route("/api/oauth/passkeys", oauth_passkeys, methods=["GET", "POST"]),
        Route("/api/oauth/passkeys/options", oauth_passkey_options, methods=["POST"]),
        Route("/api/oauth/passkeys/delete", oauth_passkey_delete, methods=["POST"]),
        Route("/api/mobile/v1/action", action_mobile, methods=["POST"]),
        Route("/api/graph-canvas", graph_canvas, methods=["GET", "PUT"]),
        Route("/api/session-capture", session_capture_ingest, methods=["POST"]),
        Route("/api/inbox/session", inbox_session_register, methods=["POST"]),
        Route("/api/inbox/session/owed", inbox_session_owed, methods=["POST"]),
        Route("/api/inbox/summary", inbox_summary, methods=["GET"]),
        Route("/api/messages", messages_send, methods=["POST"]),
        Route("/api/messages/conversations", messages_conversations, methods=["GET"]),
        Route("/api/messages/conversations/{conversation_id}", messages_thread, methods=["GET"]),
        Route(
            "/api/messages/conversations/{conversation_id}/handoff",
            messages_handoff,
            methods=["POST"],
        ),
        Route(
            "/api/messages/conversations/{conversation_id}/handoffs",
            messages_handoffs,
            methods=["GET"],
        ),
        Route("/api/messages/sessions", messages_live_sessions, methods=["GET"]),
        Route("/api/node/register", node_register, methods=["POST"]),
        Route("/api/node/heartbeat", node_heartbeat, methods=["POST"]),
        Route("/api/node/claim", node_claim, methods=["POST"]),
        Route("/api/node/receipt", node_receipt, methods=["POST"]),
        Route("/api/node/wake", node_wake, methods=["POST"]),
        Route("/api/node/wake/check", node_wake_check, methods=["POST"]),
        Route("/api/node/wake/receipt", node_wake_receipt, methods=["POST"]),
        Route("/api/node/dispatch", node_dispatch, methods=["POST"]),
        Route("/api/node/events", node_events, methods=["POST"]),
        Route("/api/node/permission", node_permission, methods=["POST"]),
        Route("/api/node/session_credential", node_session_credential, methods=["POST"]),
        Route("/api/node/followups", node_followups, methods=["POST"]),
        Route("/api/node/config", node_config, methods=["GET", "POST"]),
        Route("/api/harness-config", harness_config_status, methods=["GET"]),
        Route("/api/llm/v1/chat/completions", llm_broker.chat_completions, methods=["POST"]),
        *cc_knowledge.ROUTES,
        *cc_work.ROUTES,
        *cc_runs.ROUTES,
        *cc_inbox.ROUTES,
        *cc_launch.ROUTES,
        *cc_agents.ROUTES,
        *cc_settings.ROUTES,
    ]


def _scope_gate(request: Request, scope: route_scopes.Scope) -> Response | None:
    """Enforce one route's declared scope before its handler runs (task #1745).

    The shared guard (with the CSRF check for mutating routes) runs first, then,
    for a caller that has an identity, the profile ceiling: every ``tools`` entry
    and every consolidated ``actions`` entry must be exposed to the request's
    profile (task #1865). A scope marked ``operator`` admits the verified human
    operator past that ceiling. Anything else a handler checks stays in the
    handler (``Scope.handler``).
    """
    if scope.access in (route_scopes.Access.PUBLIC, route_scopes.Access.OWN_AUTH):
        return None
    guard = _guard_mutation(request) if scope.csrf else _guard(request)
    if guard is not None:
        return guard
    if not request.scope.get("ua_identity"):
        # Warn mode only (enforcement refused an anonymous caller above): there is no
        # profile to judge, so the handler's own identity and profile checks answer.
        return None
    if scope.operator and _operator_recovery(request):
        return None
    if scope.operator and not (scope.tools or scope.actions):
        return _json({"error": "this action is reserved for the human operator"}, status=403)
    profile = _request_profile(request)
    for tool in scope.tools:
        if not profiles.tool_is_allowed_for(profile, tool):
            return _json({"error": f"{tool} is unavailable to this profile"}, status=403)
    for manager, action in scope.actions:
        if not profiles.consolidated_action_is_allowed_for(profile, manager, action):
            return _json(
                {"error": f"{manager} {action} is unavailable to this profile"}, status=403
            )
    return None


def _scoped(route: Route) -> Route:
    """Wrap one route so its declared scope is enforced before its handler.

    A method with no entry in ``route_scopes.ROUTE_SCOPES`` answers 403 and never
    reaches the handler: a new route cannot be served until it declares who may
    call it.
    """
    endpoint = route.endpoint
    path = route.path

    @functools.wraps(endpoint)
    async def gated(request: Request) -> Response:
        scope = route_scopes.scope_for(request.method, path)
        if scope is None:
            return _json({"error": "route has no declared authorization scope"}, status=403)
        denied = _scope_gate(request, scope)
        if denied is not None:
            return denied
        # The HTTP table authorizes these exact capabilities, including the
        # operator's browser writes and consolidated legacy actions. Carry that
        # decision into guarded worker calls, never into a different request.
        # Shared read helpers preserve the table's existing authenticated read
        # admission. Never carry them through a mutating method.
        read = request.method in {"GET", "HEAD"}
        if (scope.read_tools or scope.read_actions) and not read:
            return _json({"error": "shared read authority requires GET or HEAD"}, status=403)
        with profiles.http_route_authority(
            request.scope.get("ua_identity"),
            scope.tools,
            scope.actions + scope.read_actions,
            read_tools=scope.read_tools,
            dispatch_preview=request.method == "POST" and path == "/api/action/dispatch-stack",
            human_workflow=scope.operator and _operator_recovery(request),
        ):
            return await endpoint(request)

    return Route(path, gated, methods=sorted(route.methods or ()), name=route.name)


def web_routes() -> list[Route]:
    """Routes to append to the hub's Starlette app, each behind its declared scope."""
    return [_scoped(route) for route in _declared_routes()]
