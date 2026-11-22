"""Opt-in, separate-process pilot. Never mount these routes in the private hub."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from . import post_init
from .pilot_auth import PilotAuth, WorkOSSDKProvider
from .webui.auth import csrf_ok

WORKFLOW = "pilot_record_verification"


def _check_boundary(auth: PilotAuth) -> None:
    from . import config

    root = auth.db_path.parent.resolve()
    private = (
        Path(os.environ.get("UA_PILOT_PRIVATE_HOME", "~/.unified_agent")).expanduser().resolve()
    )
    if root.is_relative_to(private) or private.is_relative_to(root):
        raise ValueError("pilot and private data roots must not overlap")
    if config.data_home().resolve() != root:
        raise ValueError("start a separate process with UA_HOME set to the dedicated pilot root")
    if os.environ.get("UA_STATE_SYNC") != "0":
        raise ValueError("pilot requires UA_STATE_SYNC=0; it must not write the private vault")
    if os.environ.get("UA_EVENT_BUS") != "0":
        raise ValueError(
            "pilot requires UA_EVENT_BUS=0; it must not publish to the private event bus"
        )
    if config.db_path().exists() and (
        config.db_path().is_symlink() or config.db_path().stat().st_nlink != 1
    ):
        raise ValueError("pilot platform database must not be linked")


def create_pilot_app(auth: PilotAuth, *, origin: str) -> Starlette:
    _check_boundary(auth)
    parsed = urlparse(origin)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError("pilot origin must be an exact HTTPS origin")
    from . import storage

    post_init.register_default_hooks()
    storage.init_db()

    def response(payload: dict, status: int = 200):
        return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})

    async def bounded_body(request: Request, limit: int) -> bytes:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise ValueError("request body too large")
            body.extend(chunk)
        return bytes(body)

    async def session_for(request: Request):
        _check_boundary(auth)
        return await run_in_threadpool(auth.resolve, request.cookies.get("pilot_session"))

    def mutation_allowed(request: Request, session: dict) -> bool:
        return request.headers.get("origin") in (None, origin) and csrf_ok(
            session["csrf"], request.headers.get("x-ua-csrf")
        )

    async def login(request: Request):
        state = await run_in_threadpool(auth.begin_login)
        result = RedirectResponse(auth.provider.authorization_url(state))
        result.set_cookie(
            "pilot_state",
            state,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/pilot",
            max_age=600,
        )
        return result

    async def callback(request: Request):
        state = request.query_params.get("state", "")
        result = response({"error": "pilot authentication denied"}, 401)
        if csrf_ok(request.cookies.get("pilot_state"), state):
            try:
                session = await run_in_threadpool(
                    auth.login, request.query_params.get("code", ""), state
                )
            except Exception:
                # Provider errors may carry tokens or response bodies; never echo them.
                pass
            else:
                result = RedirectResponse("/pilot/discovery", status_code=303)
                # Lax, not Strict: sign-in arrives on a redirect chain that starts
                # at the identity provider, and browsers withhold Strict cookies
                # for that whole chain, so the first page would answer 401.
                # Mutations still need x-ua-csrf plus a same-origin Origin, and
                # Lax keeps the cookie off cross-site POSTs.
                result.set_cookie(
                    "pilot_session",
                    session["id"],
                    httponly=True,
                    secure=True,
                    samesite="lax",
                    path="/pilot",
                )
                result.set_cookie(
                    "pilot_csrf", session["csrf"], secure=True, samesite="lax", path="/pilot"
                )
        result.delete_cookie(
            "pilot_state", path="/pilot", secure=True, httponly=True, samesite="lax"
        )
        return result

    async def logout(request: Request):
        _check_boundary(auth)
        session = await run_in_threadpool(auth.local_session, request.cookies.get("pilot_session"))
        if not session:
            return response({"error": "authentication required"}, 401)
        if not mutation_allowed(request, session):
            return response({"error": "CSRF denied"}, 403)
        await run_in_threadpool(auth.revoke, request.cookies.get("pilot_session"))
        try:
            result = RedirectResponse(
                auth.provider.logout_url(session["workos_session"]), status_code=303
            )
        except Exception:
            result = response({"logged_out": True, "provider_logout": "unavailable"})
        result.delete_cookie("pilot_session", path="/pilot", secure=True, httponly=True)
        result.delete_cookie("pilot_csrf", path="/pilot", secure=True)
        return result

    async def discovery(request: Request):
        session = await session_for(request)
        if not session:
            return response({"error": "authentication required"}, 401)
        return response(
            {
                "identity": session["ua_identity"],
                "profile": "pilot-reader",
                "organization_id": auth.organization_id,
                "capabilities": ["discovery", "request_verification"],
                "workflow": WORKFLOW,
                "approval_required": True,
            }
        )

    async def workflow(request: Request):
        session = await session_for(request)
        if not session:
            return response({"error": "authentication required"}, 401)
        if not mutation_allowed(request, session):
            return response({"error": "CSRF denied"}, 403)
        try:
            data = json.loads(await bounded_body(request, 1024))
            if (
                not isinstance(data, dict)
                or set(data) != {"request_id"}
                or not isinstance(data["request_id"], str)
            ):
                raise ValueError
            request_id = str(uuid.UUID(data["request_id"]))
        except (ValueError, TypeError, KeyError):
            return response({"error": "one UUID request_id is required"}, 400)
        from mcp.server.mcpserver.exceptions import ToolError

        from . import events
        from .tools.approvals import ApprovalPendingError, check_approval_gate

        payload = {
            "organization_id": auth.organization_id,
            "subject": session["subject"],
            "identity": session["ua_identity"],
            "request_id": request_id,
        }

        def execute():
            _check_boundary(auth)
            # No callable or client claim can grant approval. This fixed action
            # only records sanitized proof in this process's dedicated ledger.
            approval = check_approval_gate(
                WORKFLOW, payload, requested_by=session["ua_identity"], force=True
            )
            event_id = events.record_event(
                "pilot.verification",
                payload,
                originator=session["ua_identity"],
                notify=False,
                dedupe_key=f"pilot:{auth.organization_id}:{request_id}",
            )
            return {
                "event_id": event_id,
                "approval_id": approval["approval_id"],
                "organization_id": auth.organization_id,
            }

        try:
            return response(await run_in_threadpool(execute))
        except ApprovalPendingError as exc:
            return response(
                {"error": "human approval required", "approval_id": exc.approval_id}, 409
            )
        except ToolError:
            return response({"error": "workflow approval denied"}, 403)
        except Exception:
            return response({"error": "workflow unavailable"}, 503)

    async def lifecycle(request: Request):
        try:
            body = await bounded_body(request, 65536)
            event = await run_in_threadpool(
                auth.provider.lifecycle, body, request.headers.get("workos-signature", "")
            )
            if event:
                await run_in_threadpool(auth.apply_lifecycle, **event)
        except Exception:
            return response({"error": "lifecycle verification denied"}, 400)
        return response({"received": True})

    return Starlette(
        routes=[
            Route("/pilot/login", login),
            Route("/pilot/callback", callback),
            Route("/pilot/discovery", discovery),
            Route("/pilot/logout", logout, methods=["POST"]),
            Route("/pilot/workflow", workflow, methods=["POST"]),
            Route("/pilot/lifecycle", lifecycle, methods=["POST"]),
        ]
    )


def app_from_environment() -> Starlette:
    """uvicorn factory; requires a fresh, separately configured operator process."""
    root = Path(os.environ["UA_HOME"]).expanduser().resolve()
    provider = WorkOSSDKProvider.from_environment()
    auth = PilotAuth(
        root / "pilot-auth.sqlite",
        os.environ["WORKOS_ORGANIZATION_ID"],
        json.loads(os.environ["UA_PILOT_IDENTITY_MAP"]),
        provider,
    )
    return create_pilot_app(auth, origin=os.environ["UA_PILOT_ORIGIN"])
