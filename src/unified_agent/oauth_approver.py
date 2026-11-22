"""Who approved an OAuth sign-in: a verified human, or the passphrase fallback (task #1322, J3).

Before J3 the only consent gate on ``/oauth/consent`` and ``/oauth/device`` was
the shared ``UA_MCP_OAUTH_PASSPHRASE``, so anyone holding it could mint a token
for any fleet label. This module is the one seam both approval handlers call.
It tries, in order, and fails closed:

1. ``workos`` (enterprise edition): a sealed WorkOS AuthKit session, verified by
   the hub's own leg (``workos_hub_auth`` → ``WorkOSSDKProvider``: JWKS, issuer,
   the one organization, live membership and session re-check). It arrives as
   ``Authorization: WorkOS <sealed>`` or as the ``ua_oauth_workos`` cookie that
   ``/oauth/approver/workos/callback`` sets after an AuthKit sign-in.
2. ``tailnet+passkey`` (private edition): the *direct* peer resolves, through
   the local tailscaled, to a node owned by one of ``UA_MCP_OAUTH_APPROVER_LOGINS``,
   **and** the browser presents a passkey assertion from a passkey that login
   registered in the Control Center (``passkeys.py``). Forwarded headers
   (``X-Forwarded-For``, ``Tailscale-User-Login``) are never read.
3. ``passphrase``: only when ``UA_MCP_OAUTH_PASSPHRASE_FALLBACK=1``. Recorded as
   ``human.verified=false``.

The result is recorded on the consent / device-approval audit event and carried
in the issued token's claims as ``human_approval`` so every later row that
cites the token can say who approved it and how.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import config

_log = logging.getLogger(__name__)

VIA_WORKOS = "workos"
VIA_TAILNET_PASSKEY = "tailnet+passkey"
VIA_PASSPHRASE = "passphrase"
HUMAN_VERIFIED_VIA = frozenset({VIA_WORKOS, VIA_TAILNET_PASSKEY})

WORKOS_COOKIE = "ua_oauth_workos"
_WORKOS_STATE_COOKIE = "ua_oauth_workos_state"
_COOKIE_PATH = "/oauth"
_STATE_TTL_S = 600
# The cookie lives an hour; the access token inside it is refreshed on use.
_SESSION_COOKIE_MAX_AGE_S = 3600
_MAX_STATES = 256


def _state_cookie_name(state: str) -> str:
    """Give each sign-in its own bounded cookie, including parallel browser tabs."""
    return f"{_WORKOS_STATE_COOKIE}_{hashlib.sha256(state.encode()).hexdigest()[:24]}"


@dataclass
class Approval:
    """A decided approval. ``human_verified`` is True only for a verified human."""

    verified_via: str
    approver: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def human_verified(self) -> bool:
        return self.verified_via in HUMAN_VERIFIED_VIA

    def claims(self) -> dict[str, Any]:
        """Non-secret claim block stored on the token and the audit events."""
        return {
            "verified": self.human_verified,
            "verified_via": self.verified_via,
            "approver": self.approver,
            **self.details,
        }


class ApprovalRefused(Exception):
    """No acceptable approver. ``str(exc)`` is safe to show on the page."""


# -- page context -------------------------------------------------------------


def page_context(request: Request, *, binding: str) -> dict[str, Any]:
    """What the approval page should offer this browser (no secrets)."""
    ctx: dict[str, Any] = {
        "workos": False,
        "workos_signed_in": False,
        "passkey_options": None,
        "tailnet_login": None,
        "passphrase": config.mcp_oauth_passphrase_fallback()
        and bool(config.mcp_oauth_passphrase()),
    }
    try:
        from . import workos_hub_auth

        # Offer WorkOS only when the leg actually builds (org + identity map +
        # provider settings); an org id alone would link to a 404 login.
        ctx["workos"] = workos_hub_auth.enabled() and workos_hub_auth.hub_auth() is not None
    except Exception:  # noqa: BLE001 - optional leg
        ctx["workos"] = False
    if ctx["workos"]:
        try:
            ctx["workos_signed_in"] = _workos_approval(request) is not None
        except Exception:  # noqa: BLE001 - stale session may still use a passkey
            ctx["workos_signed_in"] = False
    login = _tailnet_approver_login(request)
    if login:
        ctx["tailnet_login"] = login
        try:
            from . import passkeys

            ctx["passkey_options"] = passkeys.assertion_options(login, binding=binding)
        except Exception as exc:  # noqa: BLE001 - page still renders; approval fails closed
            _log.warning("passkey options unavailable: %s", type(exc).__name__)
    return ctx


# -- verification ---------------------------------------------------------------


def _peer_ip(request: Request) -> str | None:
    # Direct peer only — never a forwarded header (see oauth._peer_ip).
    return request.client.host if request.client else None


def _resolve_peer_device(peer_ip: str | None) -> dict[str, Any] | None:
    try:
        from . import oauth

        return oauth._resolve_peer_device(peer_ip)
    except Exception:  # noqa: BLE001 - fail closed
        return None


def _tailnet_approver_login(request: Request) -> str | None:
    """The configured human login owning the direct peer's tailnet node, else None."""
    logins = config.mcp_oauth_approver_logins()
    if not logins:
        return None
    device = _resolve_peer_device(_peer_ip(request))
    user = str((device or {}).get("user") or "").strip().lower()
    return user if user and user in logins else None


def _sealed_session(request: Request) -> str | None:
    from . import workos_hub_auth

    header = workos_hub_auth.sealed_session_from_header(request.headers.get("authorization"))
    return header or request.cookies.get(WORKOS_COOKIE) or None


def _workos_approval(request: Request) -> Approval | None:
    from . import workos_hub_auth

    if not workos_hub_auth.enabled():
        return None
    sealed = _sealed_session(request)
    if not sealed:
        return None
    leg = workos_hub_auth.hub_auth()
    if leg is None:
        return None
    # The cookie outlives the ~5 minute AuthKit access token, so refresh it
    # rather than fail every approval after the first few minutes (task
    # #2114). A replacement is kept on the request; `with_refreshed_cookie`
    # writes it back so the rotated refresh token is not lost. A session in
    # the Authorization header is the client's to manage and is not
    # refreshed here: a refresh the client never sees would spend its
    # refresh token.
    from_cookie = sealed == request.cookies.get(WORKOS_COOKIE) and not (
        workos_hub_auth.sealed_session_from_header(request.headers.get("authorization"))
    )
    if from_cookie:
        session, replacement = leg.resolve_refreshing(sealed)
        if replacement:
            request.scope.setdefault("state", {})[_REFRESHED_STATE_KEY] = replacement
    else:
        session = leg.resolve(sealed)
    if not session or not config.is_known_agent_label(session.get("identity") or ""):
        return None
    return Approval(
        VIA_WORKOS,
        session["identity"],
        {
            "workos_subject": session.get("subject"),
            "workos_organization_id": session.get("organization_id"),
            "workos_session_id": session.get("session_id"),
        },
    )


_REFRESHED_STATE_KEY = "ua_workos_refreshed"


def apply_refreshed_cookie(request: Request, response: Response) -> Response:
    """Write a session refreshed while handling ``request`` back to the browser."""
    sealed = request.scope.get("state", {}).get(_REFRESHED_STATE_KEY)
    if sealed:
        _set_session_cookie(response, sealed)
    return response


def with_refreshed_cookie(handler):
    """Wrap an approval page so a refresh made while serving it reaches the cookie."""

    async def wrapped(request: Request) -> Response:
        return apply_refreshed_cookie(request, await handler(request))

    wrapped.__name__ = getattr(handler, "__name__", "wrapped")
    wrapped.__doc__ = handler.__doc__
    return wrapped


def _set_session_cookie(response: Response, sealed: str) -> None:
    response.set_cookie(
        WORKOS_COOKIE,
        sealed,
        max_age=_SESSION_COOKIE_MAX_AGE_S,
        path=_COOKIE_PATH,
        secure=_secure_cookie(),
        httponly=True,
        samesite="lax",
    )


def _passkey_fields(form) -> dict[str, str] | None:
    fields = {
        "credential_id": str(form.get("passkey_credential_id") or ""),
        "client_data_json": str(form.get("passkey_client_data") or ""),
        "authenticator_data": str(form.get("passkey_authenticator_data") or ""),
        "signature": str(form.get("passkey_signature") or ""),
    }
    if not any(fields.values()):
        return None
    return fields


def _tailnet_passkey_approval(request: Request, form, binding: str) -> Approval | None:
    """``None`` when this path does not apply; raises when an assertion is bad."""
    fields = _passkey_fields(form)
    logins = config.mcp_oauth_approver_logins()
    if not logins:
        if fields:
            raise ApprovalRefused("Passkey approval is not configured on this hub.")
        return None
    device = _resolve_peer_device(_peer_ip(request))
    user = str((device or {}).get("user") or "").strip().lower()
    if not user or user not in logins:
        if fields:
            # A passkey from a browser that is not on the approver's own node is
            # refused outright, not downgraded to the passphrase.
            raise ApprovalRefused(
                "Passkey approval must come from a tailnet device owned by the approver."
            )
        return None
    if fields is None:
        return None
    from . import passkeys

    try:
        used = passkeys.verify_assertion(login=user, binding=binding, **fields)
    except passkeys.PasskeyError as exc:
        raise ApprovalRefused(f"Passkey refused: {exc}.") from exc
    return Approval(
        VIA_TAILNET_PASSKEY,
        user,
        {
            "tailnet_login": user,
            "tailnet_node": (device or {}).get("node"),
            "tailnet_node_id": (device or {}).get("node_id"),
            "passkey_credential": used["credential_id"][:16],
        },
    )


def passphrase_ok(got: str) -> bool:
    expected = config.mcp_oauth_passphrase()
    if not expected:
        return False
    digest = hashlib.sha256
    return hmac.compare_digest(digest(got.encode()).digest(), digest(expected.encode()).digest())


# Failed passphrase guesses, in-memory for this hub process.
# 5 per client IP per 10 minutes, and 50 globally per 10 minutes.
# A success clears that IP only. WorkOS and passkey approval never consult
# this counter, so another client's failures cannot lock those paths out.
# The global ceiling can still refuse further passphrase guesses.
PASSPHRASE_IP_LIMIT = 5
PASSPHRASE_GLOBAL_LIMIT = 50
PASSPHRASE_WINDOW_S = 10 * 60

_passphrase_lock = threading.Lock()
_passphrase_failures: dict[str, list[float]] = {}
_passphrase_global: list[float] = []


def _passphrase_key(ip: str | None) -> str:
    return ip or ""


def _prune_passphrase_failures(now: float) -> None:
    cutoff = now - PASSPHRASE_WINDOW_S
    _passphrase_global[:] = [stamp for stamp in _passphrase_global if stamp > cutoff]
    for key, stamps in list(_passphrase_failures.items()):
        fresh = [stamp for stamp in stamps if stamp > cutoff]
        if fresh:
            _passphrase_failures[key] = fresh
        else:
            _passphrase_failures.pop(key, None)


def passphrase_attempt_blocked(ip: str | None) -> bool:
    """True when this IP, or the process, is already over the failure ceiling."""
    now = time.monotonic()
    key = _passphrase_key(ip)
    with _passphrase_lock:
        _prune_passphrase_failures(now)
        ip_hits = _passphrase_failures.get(key, [])
        return (
            len(ip_hits) >= PASSPHRASE_IP_LIMIT
            or len(_passphrase_global) >= PASSPHRASE_GLOBAL_LIMIT
        )


def record_passphrase_failure(ip: str | None) -> None:
    now = time.monotonic()
    key = _passphrase_key(ip)
    with _passphrase_lock:
        _prune_passphrase_failures(now)
        _passphrase_failures.setdefault(key, []).append(now)
        _passphrase_global.append(now)


def clear_passphrase_failures(ip: str | None) -> None:
    """Drop this IP's counter after a correct passphrase. The global ceiling stays."""
    key = _passphrase_key(ip)
    with _passphrase_lock:
        _passphrase_failures.pop(key, None)


def reset_passphrase_limits() -> None:
    """Clear both counters. Used by tests; not an approval bypass on the hub."""
    with _passphrase_lock:
        _passphrase_failures.clear()
        _passphrase_global.clear()


def verify(request: Request, form, *, binding: str) -> Approval:
    """Decide who is approving ``binding``. Raises ``ApprovalRefused`` (fail closed).

    Passphrase guesses are limited to 5 failures per client IP per 10 minutes
    and 50 failures globally per 10 minutes. Once either ceiling is hit, the
    next guess is refused before the constant-time compare. A correct
    passphrase clears that IP's counter. WorkOS and passkey approval do not
    use this counter.
    """
    try:
        approval = _workos_approval(request)
    except Exception as exc:  # noqa: BLE001 - a broken leg must not open the gate
        _log.warning("WorkOS approver check failed: %s", type(exc).__name__)
        approval = None
    if approval is not None:
        return approval
    approval = _tailnet_passkey_approval(request, form, binding)
    if approval is not None:
        return approval
    passphrase = str(form.get("passphrase") or "")
    if passphrase:
        if not config.mcp_oauth_passphrase_fallback():
            raise ApprovalRefused(
                "Passphrase approval is disabled on this hub. Approve with a verified "
                "identity (WorkOS sign-in, or a passkey from your own tailnet device)."
            )
        ip = _peer_ip(request)
        if passphrase_attempt_blocked(ip):
            raise ApprovalRefused("Passphrase refused.")
        if passphrase_ok(passphrase):
            clear_passphrase_failures(ip)
            return Approval(VIA_PASSPHRASE, "passphrase-holder")
        record_passphrase_failure(ip)
        raise ApprovalRefused("Passphrase refused.")
    raise ApprovalRefused("No verified approver. Sign in or use your passkey to approve.")


# -- WorkOS sign-in round trip for the approval pages ---------------------------

_states: dict[str, tuple[str, float]] = {}
_states_lock = threading.Lock()


def _safe_next(raw: str) -> str:
    """Only return to an approval page on this hub."""
    parts = urlsplit(raw or "")
    if parts.scheme or parts.netloc or parts.path not in ("/oauth/consent", "/oauth/device"):
        return "/oauth/device"
    return parts.path + (f"?{parts.query}" if parts.query else "")


def workos_redirect_uri() -> str:
    return f"{config.mcp_oauth_issuer() or ''}/oauth/approver/workos/callback"


def _secure_cookie() -> bool:
    return (config.mcp_oauth_issuer() or "").startswith("https://")


async def workos_login(request: Request) -> Response:
    """Start an AuthKit sign-in and come back to the approval page."""
    from . import workos_hub_auth

    leg = workos_hub_auth.hub_auth() if workos_hub_auth.enabled() else None
    if leg is None:
        return HTMLResponse("<p>WorkOS sign-in is not configured on this hub.</p>", 404)
    target = _safe_next(request.query_params.get("next", ""))
    state = secrets.token_urlsafe(24)
    now = time.time()
    with _states_lock:
        for key in [k for k, (_, exp) in _states.items() if exp < now]:
            _states.pop(key, None)
        if len(_states) >= _MAX_STATES:
            return HTMLResponse("<p>Too many sign-ins in flight; retry shortly.</p>", 503)
        _states[state] = (target, now + _STATE_TTL_S)
    url = leg.provider.client.user_management.get_authorization_url(
        provider="authkit",
        state=state,
        redirect_uri=workos_redirect_uri(),
        organization_id=leg.organization_id,
    )
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        _state_cookie_name(state),
        state,
        max_age=_STATE_TTL_S,
        path=_COOKIE_PATH,
        secure=_secure_cookie(),
        httponly=True,
        samesite="lax",
    )
    return response


async def workos_callback(request: Request) -> Response:
    """Finish the AuthKit sign-in: verify, set the sealed-session cookie, go back."""
    from starlette.concurrency import run_in_threadpool

    from . import workos_hub_auth

    leg = workos_hub_auth.hub_auth() if workos_hub_auth.enabled() else None
    if leg is None:
        return HTMLResponse("<p>WorkOS sign-in is not configured on this hub.</p>", 404)
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    if not state or len(state) > 128:
        return HTMLResponse("<p>Sign-in state expired or did not match. Retry.</p>", 400)
    cookie_name = _state_cookie_name(state)
    cookie_state = request.cookies.get(cookie_name, "")
    with _states_lock:
        entry = _states.get(state)
    if (
        entry is None
        or entry[1] < time.time()
        or not code
        or not cookie_state
        or not hmac.compare_digest(cookie_state, state)
    ):
        return HTMLResponse("<p>Sign-in state expired or did not match. Retry.</p>", 400)
    with _states_lock:
        # A matching state is single use; a wrong browser must not consume it.
        entry = _states.pop(state, None)
    if entry is None:
        return HTMLResponse("<p>Sign-in state expired or did not match. Retry.</p>", 400)

    try:
        sealed = await run_in_threadpool(_seal_code, leg, code)
    except Exception as exc:  # noqa: BLE001 - provider text may carry secrets
        _log.warning("WorkOS approver sign-in failed: %s", type(exc).__name__)
        sealed = None
    if not sealed:
        return HTMLResponse(
            "<p>That WorkOS account is not mapped to a hub identity. Close this tab.</p>", 403
        )
    response = RedirectResponse(entry[0], status_code=302)
    _set_session_cookie(response, sealed)
    response.delete_cookie(cookie_name, path=_COOKIE_PATH)
    return response


def _seal_code(leg, code: str) -> str | None:
    """Exchange an AuthKit code for a sealed session the hub leg accepts, else None."""
    from workos.session import seal_data

    result = leg.provider.client.user_management.authenticate_with_code(code=code)
    sealed = seal_data(result.to_dict(), leg.provider.cookie_password)
    return sealed if leg.resolve(sealed) else None


def workos_login_href(next_path: str) -> str:
    return "/oauth/approver/workos/login?next=" + quote(next_path, safe="")
