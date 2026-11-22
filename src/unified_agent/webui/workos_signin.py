"""Control Center sign-in through WorkOS AuthKit (task #2114, epic #2107).

The bearer-token sign-in (``POST /api/session``) is unchanged. This adds a
second way to mint the same ``ua_ui`` cookie session, for a human who signs in
with the hub's WorkOS organization instead of pasting a token:

``GET /api/session/options``
    Public. ``{"workos": true}`` when the sign-in below is usable, so the
    sign-in screen can offer the button. Nothing else is disclosed.
``GET /api/session/workos/login``
    Redirects to AuthKit, scoped to the hub's one organization, with a signed
    state and a PKCE (S256) challenge.
``GET /api/session/workos/callback``
    Checks the state, exchanges the code with the PKCE verifier, verifies the
    session through the hub's WorkOS leg (JWKS, exact issuer, the one
    organization, live membership and session state, the reviewed identity
    map), then mints the normal ``ua_ui`` session and redirects to ``/ui``.

Design choices:

* **The redirect origin is configuration**, ``config.public_origin()``
  (``UA_MCP_OAUTH_ISSUER``, else ``UA_PUBLIC_URL``), never a request Host
  header.
* **The login state is signed, not stored.** ``<nonce>.<expiry>.<hmac>``, keyed
  by a derivation of ``WORKOS_COOKIE_PASSWORD``, so a login started before a
  hub restart still completes. It is bound to the browser by an HttpOnly
  cookie carrying the nonce and the PKCE verifier (the verifier never travels
  through AuthKit), and it is single use within the process. After a restart a
  replay still fails: the browser cookie is deleted on use and WorkOS
  authorization codes are single use.
* **The WorkOS session stays on the server.** The sealed session (Fernet, the
  same ``WORKOS_COOKIE_PASSWORD``) is stored in ``ui_session_workos`` against
  the ``ua_ui`` session it created; the browser holds only the ``ua_ui``
  cookie. That row is the record that the session was WorkOS-established,
  with the WorkOS user, organization and session ids.
* **Each request is resolved per request**, by ``verify_ui_session`` from the
  hub middleware. A mutating request (anything but GET/HEAD/OPTIONS, so every
  approval) always re-checks membership and session state live; a read
  re-checks at most every ``RECHECK_S`` seconds. An access token near expiry
  is refreshed through the SDK and the new sealed session is stored. A
  verified session is bound with ``config.bind_workos_request_auth``, exactly
  like the ``Authorization: WorkOS`` header leg, so ``approval_resolve``
  stamps ``workos-idp-v1`` and evidence reports ``verified_via: idp``. A failed
  check denies the request; the session never falls back to a plain cookie.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import config, storage
from . import auth

log = logging.getLogger(__name__)

OPTIONS_PATH = "/api/session/options"
LOGIN_PATH = "/api/session/workos/login"
CALLBACK_PATH = "/api/session/workos/callback"
PUBLIC_PATHS = frozenset({OPTIONS_PATH, LOGIN_PATH, CALLBACK_PATH})

STATE_COOKIE = "ua_ui_workos_state"
_STATE_COOKIE_PATH = "/api/session/workos"
STATE_TTL_S = 600
_MAX_STATE_LEN = 256
_STATE_KEY_LABEL = b"ua-ui-workos-state-v1"

#: A read request trusts the last live check for this long; a write never does.
RECHECK_S = 60
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

NOT_WORKOS = "not_workos"
VERIFIED = "verified"
DENIED = "denied"

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

# One-use record of state nonces this process has accepted, until they expire.
_consumed: dict[str, float] = {}
_consumed_lock = threading.Lock()
_MAX_CONSUMED = 4096

# Striped locks so two requests on one session do not refresh concurrently:
# WorkOS rotates the refresh token, and the loser of that race would be denied.
_refresh_locks = tuple(threading.Lock() for _ in range(64))


# -- configuration ---------------------------------------------------------------


def _leg():
    from .. import workos_hub_auth

    if not workos_hub_auth.enabled():
        return None
    return workos_hub_auth.hub_auth()


def redirect_uri() -> str | None:
    origin = config.public_origin()
    return f"{origin}{CALLBACK_PATH}" if origin else None


def available() -> bool:
    """Whether WorkOS sign-in can run: the hub leg builds and the origin is known."""
    try:
        return _leg() is not None and redirect_uri() is not None
    except Exception:  # noqa: BLE001 - an optional leg never breaks the sign-in screen
        return False


# -- signed login state ----------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _state_key(cookie_password: str) -> bytes:
    # A derived key, so the state MAC can never be confused with the sealed
    # sessions or the IdP producer stamps that use the same secret.
    return hmac.new(cookie_password.encode(), _STATE_KEY_LABEL, hashlib.sha256).digest()


def _state_mac(key: bytes, nonce: str, expires: int, verifier: str) -> str:
    # The MAC covers the PKCE verifier (by hash), which only the initiating
    # browser holds. A copied callback URL therefore cannot pass, or spend, the
    # state from another browser even though the nonce is visible in it.
    bound = hashlib.sha256(verifier.encode()).hexdigest()
    return _b64(hmac.new(key, f"{nonce}.{expires}.{bound}".encode(), hashlib.sha256).digest())


def issue_state(cookie_password: str, *, now: float | None = None) -> tuple[str, str, str]:
    """Return ``(state, nonce, code_verifier)`` for one sign-in."""
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)  # 64 chars of the RFC 7636 alphabet
    expires = int((time.time() if now is None else now) + STATE_TTL_S)
    state = f"{nonce}.{expires}.{_state_mac(_state_key(cookie_password), nonce, expires, verifier)}"
    return state, nonce, verifier


def state_cookie_name(nonce: str) -> str:
    """One cookie per sign-in, so parallel tabs do not overwrite each other."""
    return f"{STATE_COOKIE}_{hashlib.sha256(nonce.encode()).hexdigest()[:24]}"


def state_nonce(state: str) -> str | None:
    if not state or len(state) > _MAX_STATE_LEN or state.count(".") != 2:
        return None
    return state.split(".", 1)[0] or None


def consume_state(
    cookie_password: str, state: str, cookie_value: str | None, *, now: float | None = None
) -> str | None:
    """Check a returned state against its browser cookie; the PKCE verifier, or None.

    Fails on a bad MAC, an expired state, a cookie from a different sign-in or
    browser (the MAC covers the cookie's PKCE verifier), or a nonce this
    process has already accepted. A state is consumed only after every other
    check passes, so a request from the wrong browser cannot burn the real one.
    """
    clock = time.time() if now is None else now
    nonce = state_nonce(state)
    if (
        nonce is None
        or not state.isascii()
        or not cookie_value
        or len(cookie_value) > _MAX_STATE_LEN
        or not cookie_value.isascii()
        or "." not in cookie_value
    ):
        return None
    _, expires_raw, mac = state.split(".")
    cookie_nonce, _, verifier = cookie_value.partition(".")
    try:
        expires = int(expires_raw)
    except ValueError:
        return None
    if not hmac.compare_digest(cookie_nonce, nonce) or not 43 <= len(verifier) <= 128:
        return None
    expected = _state_mac(_state_key(cookie_password), nonce, expires, verifier)
    if not hmac.compare_digest(expected, mac) or expires < clock:
        return None
    with _consumed_lock:
        for key in [k for k, exp in _consumed.items() if exp < clock]:
            _consumed.pop(key, None)
        if nonce in _consumed:
            return None
        if len(_consumed) >= _MAX_CONSUMED:
            _consumed.pop(min(_consumed, key=_consumed.__getitem__), None)
        _consumed[nonce] = float(expires)
    return verifier


def reset_state() -> None:
    """Forget consumed nonces. Tests only."""
    with _consumed_lock:
        _consumed.clear()


def _pkce_challenge(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode("ascii")).digest())


# -- the WorkOS-established session store ------------------------------------------


def _store(session_id: str, sealed: str, facts: dict[str, Any]) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ui_session_workos (session_id, sealed, workos_user_id,"
            " workos_organization_id, workos_session_id, access_expires_at, checked_at,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                auth._session_digest(session_id),
                sealed,
                facts["subject"],
                facts["organization_id"],
                facts["session_id"],
                int(facts["expires_at"]),
                time.time(),
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _load(digest: str) -> sqlite3.Row | None:
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT * FROM ui_session_workos WHERE session_id = ?", (digest,)
        ).fetchone()
    finally:
        conn.close()


def _save_check(
    digest: str,
    *,
    expected_sealed: str,
    sealed: str | None,
    facts: dict[str, Any] | None,
    checked: bool,
) -> bool:
    """Persist verification only while the same active WorkOS marker still exists.

    A logout may clear it during a provider call. Comparing the sealed value
    atomically with the write prevents a rotated session from reviving that
    tombstone, and tells the caller not to authenticate that request.
    """
    assignments = []
    values: list[Any] = []
    if sealed:
        assignments.append("sealed = ?")
        values.append(sealed)
    if checked and facts:
        assignments.extend(("checked_at = ?", "access_expires_at = ?", "workos_session_id = ?"))
        values.extend((time.time(), int(facts["expires_at"]), facts["session_id"]))
    if not assignments or not expected_sealed:
        return False
    conn = storage.connect()
    try:
        updated = conn.execute(
            "UPDATE ui_session_workos SET "
            + ", ".join(assignments)
            + " WHERE session_id = ? AND sealed = ? AND sealed <> ''",
            (*values, digest, expected_sealed),
        ).rowcount
        conn.commit()
        return updated == 1
    finally:
        conn.close()


def forget(session_id: str | None) -> None:
    """Erase the credential while retaining its WorkOS authentication-kind marker.

    An in-flight request may already have resolved the ordinary cookie row.
    Deleting this marker would make that request fall back to bearer-style
    authentication. Keep a non-secret tombstone instead; it must outlive any
    reader of the base session row, including future base-session collection.
    """
    if not session_id:
        return
    try:
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE ui_session_workos SET sealed = '', access_expires_at = 0, checked_at = 0 "
                "WHERE session_id = ?",
                (auth._session_digest(session_id),),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def _facts(row: sqlite3.Row, identity: str) -> dict[str, Any]:
    return {
        "identity": identity,
        "subject": row["workos_user_id"],
        "organization_id": row["workos_organization_id"],
        "session_id": row["workos_session_id"],
        "expires_at": int(row["access_expires_at"]),
    }


def _fresh(row: sqlite3.Row, method: str, now: float) -> bool:
    from ..identity_core import REFRESH_SKEW_S

    return (
        method in _SAFE_METHODS
        and now - float(row["checked_at"]) < RECHECK_S
        and int(row["access_expires_at"]) > now + REFRESH_SKEW_S
    )


def verify_ui_session(
    session_id: str, *, identity: str, method: str
) -> tuple[str, dict[str, Any] | None]:
    """Resolve the WorkOS side of a live ``ua_ui`` session. Never raises.

    ``(NOT_WORKOS, None)``: a bearer-established session, untouched.
    ``(VERIFIED, facts)``: bind ``facts`` as a WorkOS request.
    ``(DENIED, None)``: a WorkOS-established session that does not verify now;
    the request must be treated as unauthenticated.
    """
    digest = auth._session_digest(session_id)
    try:
        row = _load(digest)
    except sqlite3.Error:
        # Cannot tell whether this is a WorkOS session; deny rather than guess.
        return DENIED, None
    if row is None:
        return NOT_WORKOS, None
    if not row["sealed"]:
        return DENIED, None
    try:
        leg = _leg()
        if (
            leg is None
            or row["workos_organization_id"] != leg.organization_id
            or leg.identity_map.get(row["workos_user_id"]) != identity
        ):
            # The leg was turned off, or the reviewed mapping no longer gives
            # this person this label. Either way the session is over.
            # The side row is also the authentication-kind marker. If a
            # transient database failure prevents cookie revocation, keep it:
            # removing it would downgrade the live cookie to bearer-style
            # authentication on its next request.
            if auth.revoke_session(session_id):
                forget(session_id)
            return DENIED, None
        if _fresh(row, method, time.time()):
            return VERIFIED, _facts(row, identity)
        with _refresh_locks[int(digest[:8], 16) % len(_refresh_locks)]:
            # Re-read under the lock: a concurrent request may have refreshed.
            row = _load(digest)
            if row is None or not row["sealed"]:
                return DENIED, None
            if _fresh(row, method, time.time()):
                return VERIFIED, _facts(row, identity)
            facts, replacement = leg.resolve_refreshing(row["sealed"])
            ok = bool(
                facts
                and facts.get("identity") == identity
                and facts.get("subject") == row["workos_user_id"]
                and facts.get("organization_id") == row["workos_organization_id"]
            )
            # Store a rotated session even when the check failed: the old
            # refresh token is spent.
            saved = _save_check(
                digest,
                expected_sealed=row["sealed"],
                sealed=replacement,
                facts=facts,
                checked=ok,
            )
            if not saved:
                return DENIED, None
    except Exception as exc:  # noqa: BLE001 - fail closed; provider text may carry secrets
        log.warning("Control Center WorkOS session check failed: %s", type(exc).__name__)
        return DENIED, None
    if not ok:
        return DENIED, None
    return VERIFIED, {**facts, "identity": identity}


# -- routes --------------------------------------------------------------------------


def _page(message: str, status: int) -> HTMLResponse:
    from html import escape

    return HTMLResponse(
        "<!DOCTYPE html><html><body><p>"
        + escape(message)
        + ' <a href="/ui">Back to sign in</a></p></body></html>',
        status_code=status,
        headers=_NO_STORE,
    )


async def session_options(request: Request) -> Response:
    """Public: which sign-in methods the hub offers. Only a boolean, no settings."""
    if not auth.host_allowed(request.headers.get("host")):
        return JSONResponse({"error": "host not allowed"}, status_code=403)
    workos = await run_in_threadpool(available)
    return JSONResponse({"workos": workos}, headers=_NO_STORE)


async def workos_login(request: Request) -> Response:
    if not auth.host_allowed(request.headers.get("host")):
        return JSONResponse({"error": "host not allowed"}, status_code=403)
    leg = await run_in_threadpool(_leg)
    callback = redirect_uri()
    if leg is None or callback is None:
        return _page("WorkOS sign-in is not configured on this hub.", 404)
    state, nonce, verifier = issue_state(leg.provider.cookie_password)
    url = leg.provider.client.user_management.get_authorization_url(
        provider="authkit",
        state=state,
        redirect_uri=callback,
        organization_id=leg.organization_id,
        code_challenge=_pkce_challenge(verifier),
        code_challenge_method="S256",
    )
    response = RedirectResponse(url, status_code=302, headers=_NO_STORE)
    response.set_cookie(
        state_cookie_name(nonce),
        f"{nonce}.{verifier}",
        max_age=STATE_TTL_S,
        path=_STATE_COOKIE_PATH,
        secure=callback.startswith("https://"),
        httponly=True,
        # AuthKit returns with a top-level cross-site GET, which Lax allows.
        samesite="lax",
    )
    return response


def _exchange(leg, code: str, verifier: str) -> str:
    """Trade the code (with its PKCE verifier) for a sealed session."""
    from workos.session import seal_data

    result = leg.provider.client.user_management.authenticate_with_code(
        code=code, code_verifier=verifier
    )
    return seal_data(result.to_dict(), leg.provider.cookie_password)


def _record_signin(facts: dict[str, Any]) -> None:
    try:
        from .. import events

        events.record_event(
            "hub.ui.signin",
            {
                "via": "workos",
                "identity": facts["identity"],
                "workos_user_id": facts["subject"],
                "workos_organization_id": facts["organization_id"],
                "workos_session_id": facts["session_id"],
            },
            originator=facts["identity"],
        )
    except Exception as exc:  # noqa: BLE001 - the audit row never blocks a sign-in
        log.warning("could not record the Control Center sign-in: %s", type(exc).__name__)


async def workos_callback(request: Request) -> Response:
    if not auth.host_allowed(request.headers.get("host")):
        return JSONResponse({"error": "host not allowed"}, status_code=403)
    leg = await run_in_threadpool(_leg)
    callback = redirect_uri()
    if leg is None or callback is None:
        return _page("WorkOS sign-in is not configured on this hub.", 404)
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    nonce = state_nonce(state)
    cookie_name = state_cookie_name(nonce) if nonce else None
    if request.query_params.get("error"):
        # The user cancelled, or AuthKit refused. Its text is not echoed.
        response = _page("Sign-in was cancelled or refused by WorkOS.", 400)
    elif not code or len(code) > 1024 or cookie_name is None:
        response = _page("Sign-in state expired or did not match. Try again.", 400)
    else:
        verifier = consume_state(
            leg.provider.cookie_password, state, request.cookies.get(cookie_name)
        )
        if verifier is None:
            response = _page("Sign-in state expired or did not match. Try again.", 400)
        else:
            response = await _finish(request, leg, code, verifier, callback)
    if cookie_name:
        response.delete_cookie(cookie_name, path=_STATE_COOKIE_PATH)
    return response


async def _finish(request: Request, leg, code: str, verifier: str, callback: str) -> Response:
    from .. import profiles

    try:
        sealed = await run_in_threadpool(_exchange, leg, code, verifier)
        facts = await run_in_threadpool(leg.resolve, sealed)
    except Exception as exc:  # noqa: BLE001 - provider text may carry secrets
        log.warning("Control Center WorkOS sign-in failed: %s", type(exc).__name__)
        facts = None
    if not facts:
        # One message for every refusal (not a member of the organization, not
        # mapped to a hub identity, a revoked session): which one is not the
        # caller's business, and the hub log has the class.
        return _page(
            "This WorkOS account cannot sign in to this hub. Ask the operator to add it "
            "to the organization and map it to a hub identity.",
            403,
        )
    label = facts["identity"]
    if profiles.cloud_hosted_identity_default(label):
        return _page("This identity cannot open a Control Center session.", 403)
    session_id, _csrf = auth.create_session(label, request.headers.get("user-agent"))
    try:
        await run_in_threadpool(_store, session_id, sealed, facts)
    except Exception as exc:  # noqa: BLE001 - never leave a half-made WorkOS session
        auth.revoke_session(session_id)
        log.warning("could not store the Control Center WorkOS session: %s", type(exc).__name__)
        return _page("Sign-in could not be completed. Try again.", 503)
    await run_in_threadpool(_record_signin, facts)
    response = RedirectResponse("/ui", status_code=302, headers=_NO_STORE)
    response.headers.append(
        "set-cookie", auth.cookie_header_value(session_id, callback.startswith("https://"))
    )
    return response
