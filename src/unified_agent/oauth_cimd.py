"""Client ID Metadata Documents for the hub OAuth server (task #1323, Epic J4).

The MCP authorization spec (2025-11-25) prefers CIMD over dynamic client
registration: the ``client_id`` *is* an HTTPS URL, and the document served at
that URL is the client's metadata. The hub fetches it on first use instead of
keeping a registration row.

Fetch rules (``draft-ietf-oauth-client-id-metadata-document``, plus hub policy):

* ``client_id`` is an ``https`` URL with a path, no userinfo, fragment, query,
  or dot segments.
* When ``UA_MCP_OAUTH_CIMD_ALLOW`` lists hosts, only those hosts (or their
  subdomains, via a leading ``.``) are fetched. Empty means any public host.
* Every connection goes through ``safety.SSRFShieldTransport(strict=True)``:
  the host must resolve to globally routable addresses only (the
  ``UA_ALLOW_LOCAL_FETCH`` override does not apply), and the connection is
  pinned to the vetted IP (no DNS rebinding). Redirects are refused outright.
* The body is capped at ``MAX_DOCUMENT_BYTES`` and the fetch at
  ``FETCH_TIMEOUT_S``.
* The document's ``client_id`` must equal the URL. It may not carry a
  ``client_secret`` and must be a public client (``token_endpoint_auth_method``
  ``none``). ``redirect_uris`` is required.

Valid documents are cached for ``CACHE_TTL_S``; failures for
``NEGATIVE_CACHE_TTL_S`` so a bad URL cannot be used to make the hub fetch in a
loop. The displayed client name always carries the document's host, so a remote
document can never present itself as a bare fleet agent label.
"""

from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx
from mcp.shared.auth import OAuthClientInformationFull

from . import config

MAX_DOCUMENT_BYTES = 5 * 1024
FETCH_TIMEOUT_S = 5.0
CACHE_TTL_S = 3600.0
NEGATIVE_CACHE_TTL_S = 60.0
MAX_CACHE_ENTRIES = 512
# Global bound on concurrent document fetches, so anonymous /authorize traffic
# with many distinct URLs cannot tie up the worker pool until timeouts.
MAX_INFLIGHT_FETCHES = 4
_SCOPE = "mcp"
_HOST_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")


class CIMDError(Exception):
    """A client_id URL that cannot be used; the message is safe to log."""


@dataclass
class _Entry:
    client: OAuthClientInformationFull | None
    error: str | None
    expires_at: float


_cache: dict[str, _Entry] = {}
_cache_lock = threading.Lock()
# Per-client_id locks so concurrent lookups of one uncached URL share one fetch.
_key_locks: dict[str, threading.Lock] = {}
_inflight = threading.BoundedSemaphore(MAX_INFLIGHT_FETCHES)


def _clock() -> float:
    return time.monotonic()


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _key_locks.clear()


def looks_like_cimd(client_id: str) -> bool:
    """True when a client_id is URL-shaped (the CIMD form), valid or not."""
    return isinstance(client_id, str) and client_id.startswith("https://")


def _host_allowed(host: str, allow: list[str]) -> bool:
    if not allow:
        return True
    for entry in allow:
        if entry.startswith("."):
            if host.endswith(entry) or host == entry[1:]:
                return True
        elif host == entry:
            return True
    return False


def validate_client_id_url(client_id: str) -> str:
    """Return the lower-cased host of a well-formed CIMD client_id, or raise.

    Strict: the URL must already be in canonical form (it round-trips through
    ``urlunsplit`` unchanged), so what is validated is exactly what is fetched.
    The host must be a lower-case DNS name (no IP literal, no port), with
    no percent-encoding, control characters, backslashes, or non-ASCII.
    """
    if not isinstance(client_id, str) or len(client_id) > 2048:
        raise CIMDError("client_id URL is too long")
    if not client_id.isascii() or any(c.isspace() or ord(c) < 0x21 for c in client_id):
        raise CIMDError("client_id URL must be printable ASCII with no whitespace")
    if "\\" in client_id:
        raise CIMDError("client_id URL may not contain backslashes")
    try:
        parts = urlsplit(client_id)
        port = parts.port
        host = parts.hostname or ""
    except ValueError as exc:
        raise CIMDError(f"client_id URL is malformed: {exc}") from exc
    if parts.scheme != "https":
        raise CIMDError("client_id URL must use https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise CIMDError("client_id URL may not carry credentials")
    if port is not None:
        raise CIMDError("client_id URL may not carry a port")
    if parts.fragment or parts.query or "#" in client_id or "?" in client_id:
        raise CIMDError("client_id URL may not carry a query or fragment")
    if not host or "%" in parts.netloc:
        raise CIMDError("client_id URL has no valid host")
    if parts.netloc != host:
        raise CIMDError("client_id URL host must be lower-case with no port")
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise CIMDError("client_id URL host must be a DNS name, not an IP address")
    except ValueError:
        pass
    labels = host.rstrip(".").split(".")
    if len(labels) < 2 or not all(_HOST_LABEL.fullmatch(label) for label in labels):
        raise CIMDError("client_id URL host is not a valid DNS name")
    path = parts.path
    if not path or path == "/":
        raise CIMDError("client_id URL must have a path")
    if any(seg in (".", "..") for seg in unquote(path).split("/")):
        raise CIMDError("client_id URL may not contain dot segments")
    if urlunsplit((parts.scheme, parts.netloc, path, "", "")) != client_id:
        raise CIMDError("client_id URL is not in canonical form")
    if not _host_allowed(host, config.mcp_oauth_cimd_allow()):
        raise CIMDError(f"client_id host {host!r} is not in UA_MCP_OAUTH_CIMD_ALLOW")
    return host


def _transport() -> httpx.BaseTransport:
    """Network seam (tests substitute a mock transport).

    The shared SSRF shield in strict mode: every resolved address must be
    globally routable, the connection is pinned to it (no DNS rebinding), and
    ``UA_ALLOW_LOCAL_FETCH`` is ignored — that override is for agent web
    fetches, never for anonymous /authorize traffic.
    """
    from .safety import SSRFShieldTransport

    return SSRFShieldTransport(strict=True)


def _fetch(client_id: str) -> bytes:
    try:
        with httpx.Client(
            transport=_transport(),
            timeout=FETCH_TIMEOUT_S,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        ) as http:
            with http.stream("GET", client_id) as resp:
                if resp.is_redirect:
                    raise CIMDError("client metadata document redirected; redirects are refused")
                if resp.status_code != 200:
                    raise CIMDError(f"client metadata document returned HTTP {resp.status_code}")
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > MAX_DOCUMENT_BYTES:
                        raise CIMDError("client metadata document is too large")
                return body
    except CIMDError:
        raise
    except Exception as exc:  # noqa: BLE001 - network/SSRF errors become one class
        raise CIMDError(f"client metadata document fetch failed: {str(exc)[:160]}") from exc


def parse_document(client_id: str, host: str, body: bytes) -> OAuthClientInformationFull:
    try:
        doc: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CIMDError("client metadata document is not JSON") from exc
    if not isinstance(doc, dict):
        raise CIMDError("client metadata document must be a JSON object")
    if doc.get("client_id") != client_id:
        raise CIMDError("client metadata document client_id does not match its URL")
    if "client_secret" in doc or "client_secret_expires_at" in doc:
        raise CIMDError("client metadata document may not carry a client secret")
    method = doc.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise CIMDError("client metadata document must describe a public client (auth method none)")
    redirect_uris = doc.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise CIMDError("client metadata document must list redirect_uris")
    grant_types = doc.get("grant_types") or ["authorization_code", "refresh_token"]
    if (
        not isinstance(grant_types, list)
        or not all(isinstance(g, str) for g in grant_types)
        or not all(isinstance(u, str) for u in redirect_uris)
    ):
        raise CIMDError("client metadata document has non-string grant_types or redirect_uris")
    if not set(grant_types) <= {
        "authorization_code",
        "refresh_token",
    }:
        raise CIMDError("client metadata document requests an unsupported grant type")
    name = str(doc.get("client_name") or "").strip()[:80]
    try:
        return OAuthClientInformationFull(
            client_id=client_id,
            client_name=f"{name} ({host})" if name else host,
            client_uri=doc.get("client_uri"),
            logo_uri=doc.get("logo_uri"),
            redirect_uris=redirect_uris,
            grant_types=grant_types,
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=_SCOPE,
        )
    except Exception as exc:  # noqa: BLE001 - pydantic validation detail
        raise CIMDError(f"client metadata document is invalid: {str(exc)[:160]}") from exc


def resolve(client_id: str) -> OAuthClientInformationFull:
    """Fetch (or serve from cache) the client for a CIMD client_id, or raise.

    The URL (including the host allowlist) is validated on every call, so a
    tightened ``UA_MCP_OAUTH_CIMD_ALLOW`` takes effect on cached entries too.
    Concurrent lookups of one uncached URL share a single fetch, and fetches
    overall are capped at ``MAX_INFLIGHT_FETCHES``.
    """
    host = validate_client_id_url(client_id)
    cached = _cached(client_id)
    if cached is not None:
        return cached
    with _cache_lock:
        key_lock = _key_locks.setdefault(client_id, threading.Lock())
    with key_lock:
        cached = _cached(client_id)  # another thread may have fetched it
        if cached is not None:
            return cached
        if not _inflight.acquire(blocking=False):
            # Not negatively cached: the client did nothing wrong.
            raise CIMDError("too many client metadata fetches in flight; retry shortly")
        try:
            client = parse_document(client_id, host, _fetch(client_id))
        except CIMDError as exc:
            # TTLs start when the fetch completes, not when it began.
            _store(client_id, _Entry(None, str(exc), _clock() + NEGATIVE_CACHE_TTL_S))
            raise
        finally:
            _inflight.release()
        _store(client_id, _Entry(client, None, _clock() + CACHE_TTL_S))
        return client


def _cached(client_id: str) -> OAuthClientInformationFull | None:
    """A live cache hit, raising for a cached failure; None on a miss."""
    with _cache_lock:
        entry = _cache.get(client_id)
    if entry is None or entry.expires_at <= _clock():
        return None
    if entry.client is None:
        raise CIMDError(entry.error or "client metadata document unavailable")
    return entry.client


def _store(client_id: str, entry: _Entry) -> None:
    with _cache_lock:
        if client_id not in _cache and len(_cache) >= MAX_CACHE_ENTRIES:
            # Evict the entry closest to expiry; the cache is a bound, not an index.
            oldest = min(_cache, key=lambda k: _cache[k].expires_at)
            _cache.pop(oldest, None)
            lock = _key_locks.get(oldest)
            if lock is not None and not lock.locked():
                _key_locks.pop(oldest, None)
        _cache[client_id] = entry
