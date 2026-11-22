"""Browser credentials for the web control center.

The hub's own credential is a bearer token (`HubAuthASGIMiddleware`). A static page
cannot hold one safely: anything reachable from JavaScript is reachable from an XSS,
and what would leak is a permanent, tailnet-wide MCP tool credential. So the browser
trades a token once for an HttpOnly cookie session that is scoped to `/ui` and `/api`
and can be revoked.

The rule that makes this safe: **`/mcp` never accepts the cookie.** Tool calls remain
bearer-only, so even a cross-site request riding the cookie cannot reach one.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

from .. import config, storage

COOKIE_NAME = "ua_ui"
CSRF_HEADER = "x-ua-csrf"

# Paths the cookie is allowed to authenticate. /mcp is deliberately absent.
COOKIE_PATH_PREFIXES = ("/ui", "/api")


def cookie_paths_cover(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in COOKIE_PATH_PREFIXES)


def _now() -> datetime:
    return datetime.now(UTC)


def _session_digest(session_id: str) -> str:
    """sha256 hex of the cookie value. This is what ``ui_sessions.id`` stores.

    The raw id is the cookie and is not logged. Cookies issued before hashing
    landed no longer match a row; the operator signs in again.
    """
    return hashlib.sha256(session_id.encode()).hexdigest()


def create_session(agent: str, user_agent: str | None = None) -> tuple[str, str]:
    """Mint a UI session. Returns (session_id, csrf_token).

    ``session_id`` is the raw cookie value. The database stores
    ``sha256(session_id)`` so a local reader of ``memory.db`` cannot replay the
    cookie. Existing cookies from before this change stop matching.
    """
    session_id = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(hours=config.ui_session_ttl_hours())
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO ui_sessions (id, agent, csrf, created_at, expires_at, last_seen,"
            " user_agent, revoked) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (
                _session_digest(session_id),
                agent,
                csrf,
                now.isoformat(timespec="seconds"),
                expires.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
                (user_agent or "")[:300] or None,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return session_id, csrf


def resolve_session(session_id: str | None) -> dict | None:
    """Return {agent, csrf} for a live session, or None.

    Expiry is evaluated on read rather than by a sweep: a sweep that stops running
    would silently extend every session, which is the failure mode you least want in
    an auth path.
    """
    if not session_id:
        return None
    digest = _session_digest(session_id)
    try:
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT id, agent, csrf, expires_at, revoked FROM ui_sessions WHERE id = ?",
                (digest,),
            ).fetchone()
            if row is None or row["revoked"]:
                return None
            try:
                expires = datetime.fromisoformat(row["expires_at"])
            except ValueError:
                return None
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= _now():
                return None
            conn.execute(
                "UPDATE ui_sessions SET last_seen = ? WHERE id = ?",
                (_now().isoformat(timespec="seconds"), digest),
            )
            conn.commit()
            return {"agent": row["agent"], "csrf": row["csrf"]}
        finally:
            conn.close()
    except sqlite3.Error:
        return None


class SessionLookupError(Exception):
    """The session store could not be read: whether the session is live is unknown."""


def find_live_session(session_id: str | None) -> dict | None:
    """Return {agent, csrf} for a live session, None only when there is none.

    Unlike :func:`resolve_session` it never reads a failure as absence: any
    SQLite error (a lock timeout included) raises :class:`SessionLookupError`.
    It writes nothing (no ``last_seen`` update), so it cannot wait on a write
    lock. Sign-out uses it: only a confirmed absence may skip the CSRF check.
    """
    if not session_id:
        return None
    digest = _session_digest(session_id)
    try:
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT agent, csrf, expires_at, revoked FROM ui_sessions WHERE id = ?",
                (digest,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise SessionLookupError(str(exc)) from exc
    if row is None or row["revoked"]:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= _now():
        return None
    return {"agent": row["agent"], "csrf": row["csrf"]}


def revoke_session(session_id: str | None) -> bool:
    if not session_id:
        return False
    digest = _session_digest(session_id)
    try:
        conn = storage.connect()
        try:
            cur = conn.execute("UPDATE ui_sessions SET revoked = 1 WHERE id = ?", (digest,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def verify_token(token: str | None) -> str | None:
    """Resolve a bearer token to its agent label, in constant time.

    Returns None when auth is not configured at all — the caller decides whether that
    means "let anyone in" (warn mode) or "nobody can log in".
    """
    if not token:
        return None
    tokens = config.agent_tokens()
    if not tokens:
        return None
    # compare_digest against every candidate so a wrong token costs the same as a
    # right one regardless of which entry it nearly matched.
    match = None
    for candidate, label in tokens.items():
        if hmac.compare_digest(candidate, token):
            match = label
    return match


def csrf_ok(expected: str | None, provided: str | None) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected, provided)


def origin_allowed(origin: str | None) -> bool:
    """Same-origin policy for /ui and /api.

    An absent Origin passes: same-origin navigations and curl send none, and MCP's own
    TransportSecurityMiddleware makes the same choice. A *present* Origin must match
    the allowlist, which is what blocks a hostile page from driving this API with the
    user's cookie.

    `UA_HTTP_ALLOWED_HOSTS` entries may use the 'name:*' any-port form; an allowlist
    entry ending in ':*' matches any port on that host.
    """
    if not origin:
        return True
    allowed = config.http_allowed_origins()
    for entry in allowed:
        if entry == origin:
            return True
        if entry.endswith(":*"):
            prefix = entry[:-1]  # keep the trailing ':'
            if origin.startswith(prefix) and origin[len(prefix) :].isdigit():
                return True
            # 'http://host:*' should also admit the default-port form 'http://host'
            if origin == entry[:-2]:
                return True
    return False


def host_allowed(host: str | None) -> bool:
    """Host allowlist for ``/ui`` and ``/api``.

    ``localhost`` and ``127.0.0.1`` are always accepted, on any port.
    ``UA_HTTP_ALLOWED_HOSTS`` entries are too. An entry ending in ``:*``
    matches any port the same way ``origin_allowed`` treats an origin entry
    ending in ``:*`` (and the bare host with the port omitted).
    """
    if not host or not host.strip():
        return False
    presented = host.strip().lower()
    entries = ["localhost:*", "127.0.0.1:*", *config.http_allowed_hosts()]
    for entry in entries:
        candidate = entry.strip().lower()
        if not candidate:
            continue
        if candidate.endswith(":*"):
            prefix = candidate[:-1]  # keep the trailing ':'
            if presented.startswith(prefix) and presented[len(prefix) :].isdigit():
                return True
            if presented == candidate[:-2]:
                return True
            continue
        if presented == candidate:
            return True
    return False


def cookie_header_value(session_id: str, secure: bool) -> str:
    """Build the Set-Cookie value.

    `Secure` is conditional: `tailscale serve` terminates TLS and forwards plaintext
    to loopback, so the flag is driven by the request's scheme — which a trusted
    proxy sets via X-Forwarded-Proto on the forwarded-trust listener only
    (task #1892). Setting it unconditionally would break plain-http localhost
    use, which is the primary path.
    """
    max_age = int(config.ui_session_ttl_hours() * 3600)
    parts = [
        f"{COOKIE_NAME}={session_id}",
        "HttpOnly",
        "SameSite=Strict",
        "Path=/",
        f"Max-Age={max_age}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_value() -> str:
    return f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
