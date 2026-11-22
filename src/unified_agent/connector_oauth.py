"""Hub-only OAuth client for reviewed downstream connector grants.

The public surface returns authorize URLs and connection metadata. Provider
tokens stay in tenant-scoped encrypted managed secrets and are usable only by
in-process connector consumers.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlencode

import httpx
from cryptography.fernet import InvalidToken

from . import agent_access, config, connectors, events, managed_secrets, storage

_SPECS = {"github": connectors.GITHUB_OAUTH, "google": connectors.GOOGLE_OAUTH}
_PENDING_TTL = 600
_MAX_PENDING = 256
_REFRESH_MARGIN = 300
_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_PURPOSE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class ConnectorOAuthError(ValueError):
    """Safe public error; never includes a provider response or credential."""


def _spec(provider: str) -> connectors.OAuthClientSpec:
    try:
        return _SPECS[provider]
    except KeyError as exc:
        raise ConnectorOAuthError("unsupported connector OAuth provider") from exc


def _require_enabled(provider: str) -> None:
    if provider == "google" and os.environ.get("UA_GOOGLE_CONNECTOR_OAUTH_ENABLED") != "1":
        raise ConnectorOAuthError("Google connector OAuth awaits consumer and scope review")


def _scope(org_id: str | None) -> str:
    return org_id or ""


def _secret_name(provider: str, part: str) -> str:
    _spec(provider)
    return f"oauth:{provider}:{part}"


def _client_credentials(provider: str, org_id: str | None) -> tuple[str, str]:
    try:
        client_id = managed_secrets._load_for_consumer(
            _secret_name(provider, "client_id"), "connector", org_id=org_id
        )
        client_secret = managed_secrets._load_for_consumer(
            _secret_name(provider, "client_secret"), "connector", org_id=org_id
        )
    except Exception as exc:
        raise ConnectorOAuthError("connector OAuth client credentials are not configured") from exc
    return client_id, client_secret


def _redirect_uri() -> str:
    issuer = config.mcp_oauth_issuer()
    if not issuer or not issuer.startswith("https://"):
        raise ConnectorOAuthError("connector OAuth requires a configured HTTPS hub issuer")
    return f"{issuer.rstrip('/')}/oauth/connectors/callback"


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _verifier_cipher():
    cipher = managed_secrets._cipher()
    if cipher is None:
        raise ConnectorOAuthError("managed-secret encryption is required for connector OAuth")
    return cipher


def begin(
    provider: str, *, actor: str, org_id: str | None = None, device_node: str | None = None
) -> dict[str, str]:
    """Create one expiring state and a PKCE S256 authorize URL."""
    spec = _spec(provider)
    _require_enabled(provider)
    if not actor:
        raise ConnectorOAuthError("authenticated identity required")
    from . import identity_core

    if identity_core.oauth_provider() is None:
        raise ConnectorOAuthError("hub OAuth must be enabled for connector callbacks")
    if not agent_access.managed_secret_mode():
        raise ConnectorOAuthError("managed-secret mode is required for connector OAuth")
    if not managed_secrets.encryption_enabled():
        raise ConnectorOAuthError("managed-secret encryption is required for connector OAuth")
    client_id, _ = _client_credentials(provider, org_id)
    redirect_uri = _redirect_uri()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    with _lock(provider, org_id), storage.connect() as conn:
        conn.execute(
            "DELETE FROM connector_oauth_pending WHERE expires_at < ?", (int(time.time()),)
        )
        pending = conn.execute("SELECT count(*) FROM connector_oauth_pending").fetchone()[0]
        if pending >= _MAX_PENDING:
            raise ConnectorOAuthError("connector OAuth is busy; retry later")
        conn.execute(
            "INSERT INTO connector_oauth_pending "
            "(state_hash, provider, org_id, actor, device_node, code_verifier, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _state_hash(state),
                provider,
                _scope(org_id),
                actor,
                device_node,
                _verifier_cipher().encrypt(verifier.encode()).decode(),
                int(time.time()) + _PENDING_TTL,
            ),
        )
        conn.commit()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(spec.scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **spec.extra_authorize_params,
    }
    return {"provider": provider, "authorize_url": f"{spec.authorize_url}?{urlencode(params)}"}


def _consume_state(state: str) -> dict[str, object]:
    if len(state) < 32 or len(state) > 256:
        raise ConnectorOAuthError("invalid or expired connector OAuth state")
    with storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT provider, org_id, actor, device_node, code_verifier, expires_at "
            "FROM connector_oauth_pending WHERE state_hash = ?",
            (_state_hash(state),),
        ).fetchone()
        if row is None:
            raise ConnectorOAuthError("invalid or expired connector OAuth state")
        conn.execute(
            "DELETE FROM connector_oauth_pending WHERE state_hash = ?", (_state_hash(state),)
        )
        conn.commit()
    if int(row["expires_at"]) < int(time.time()):
        raise ConnectorOAuthError("invalid or expired connector OAuth state")
    try:
        verifier = _verifier_cipher().decrypt(str(row["code_verifier"]).encode()).decode()
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise ConnectorOAuthError("connector OAuth state cannot be decrypted") from exc
    return {**dict(row), "code_verifier": verifier}


def _pending_scope(state: str) -> tuple[str, str | None]:
    if len(state) < 32 or len(state) > 256:
        raise ConnectorOAuthError("invalid or expired connector OAuth state")
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT provider, org_id FROM connector_oauth_pending WHERE state_hash=?",
            (_state_hash(state),),
        ).fetchone()
    if row is None:
        raise ConnectorOAuthError("invalid or expired connector OAuth state")
    return str(row["provider"]), str(row["org_id"]) or None


def _token_response(response: httpx.Response) -> dict[str, object]:
    if response.status_code != 200:
        raise ConnectorOAuthError("provider token exchange failed")
    if len(response.content) > 65536:
        raise ConnectorOAuthError("provider token response is invalid")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ConnectorOAuthError("provider token response is invalid") from exc
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("access_token"), str)
        or not payload["access_token"]
    ):
        raise ConnectorOAuthError("provider token response is invalid")
    return payload


def _expiry(payload: dict[str, object], *, required: bool) -> int | None:
    raw = payload.get("expires_in")
    if raw is None and not required:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 1 <= raw <= 604800:
        raise ConnectorOAuthError("provider token expiry is invalid")
    return int(time.time()) + int(raw)


def _write_grant(
    provider: str,
    payload: dict[str, object],
    *,
    actor: str,
    org_id: str | None,
    device_node: str | None,
) -> dict[str, object]:
    spec = _spec(provider)
    raw_scopes = payload.get("scope")
    if not isinstance(raw_scopes, str):
        raise ConnectorOAuthError("provider did not report granted scopes")
    granted_scopes = set(raw_scopes.split("," if provider == "github" else " "))
    if not set(spec.scopes) <= granted_scopes:
        raise ConnectorOAuthError("provider granted fewer scopes than requested")
    refresh = payload.get("refresh_token")
    if spec.refresh_supported and (not isinstance(refresh, str) or not refresh):
        raise ConnectorOAuthError("provider did not grant offline refresh access")
    expires_at = _expiry(payload, required=spec.refresh_supported)
    grant = {
        "access_token": payload["access_token"],
        "refresh_token": refresh if isinstance(refresh, str) else None,
        "expires_at": expires_at,
        "scopes": sorted(granted_scopes),
    }
    # A failed file write or ledger append must leave provider use disabled.
    _mark_reconnect(provider, org_id)
    managed_secrets.set_secret(
        _secret_name(provider, "grant"),
        json.dumps(grant),
        requested_by=actor,
        consumer="connector",
        org_id=org_id,
    )
    with storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO connector_oauth_connections "
            "(provider, org_id, status, scopes_json, updated_at) VALUES (?, ?, 'connected', ?, ?) "
            "ON CONFLICT(provider, org_id) DO UPDATE SET status='connected', "
            "scopes_json=excluded.scopes_json, updated_at=excluded.updated_at",
            (provider, _scope(org_id), json.dumps(sorted(granted_scopes)), storage.now()),
        )
        _, _, publish_payload = events.record_event_once_on_connection(
            conn,
            "connector.oauth.connected",
            payload=_event_payload(
                "connector.oauth.connected",
                provider,
                actor,
                org_id,
                device_node,
                "connect",
                scopes=sorted(granted_scopes),
            ),
            originator=actor,
        )
        conn.commit()
    if publish_payload is not None:
        events.publish(publish_payload)
    return status(provider, org_id=org_id)


def complete(state: str, code: str) -> dict[str, object]:
    """Consume state before exchange, so a callback can never replay."""
    if not code or len(code) > 4096:
        raise ConnectorOAuthError("invalid provider authorization code")
    provider, org_id = _pending_scope(state)
    _require_enabled(provider)
    with _lock(provider, org_id):
        pending = _consume_state(state)
        client_id, client_secret = _client_credentials(provider, org_id)
        try:
            with httpx.Client(timeout=15, trust_env=False) as client:
                response = client.post(
                    _spec(provider).token_url,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": _redirect_uri(),
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "code_verifier": str(pending["code_verifier"]),
                    },
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise ConnectorOAuthError("provider token exchange unavailable") from exc
        return _write_grant(
            provider,
            _token_response(response),
            actor=str(pending["actor"]),
            org_id=org_id,
            device_node=pending["device_node"],
        )


def _event_payload(
    channel: str,
    provider: str,
    actor: str,
    org_id: str | None,
    device_node: str | None,
    purpose: str,
    *,
    scopes: list[str] | None = None,
) -> dict[str, object]:
    digest = None
    if channel not in {"connector.oauth.disconnected", "connector.oauth.disconnect_requested"}:
        digest = managed_secrets.metadata(_secret_name(provider, "grant"), org_id=org_id)["digest"]
    return {
        "provider": provider,
        "org_id": org_id,
        "actor": actor,
        "device_node": device_node,
        "scopes": scopes if scopes is not None else status(provider, org_id=org_id)["scopes"],
        "purpose": purpose,
        "grant_digest": digest,
    }


def _event(
    channel: str,
    provider: str,
    actor: str,
    org_id: str | None,
    device_node: str | None,
    purpose: str,
) -> None:
    events.record_event(
        channel,
        _event_payload(channel, provider, actor, org_id, device_node, purpose),
        originator=actor,
    )


def status(provider: str, *, org_id: str | None = None) -> dict[str, object]:
    _spec(provider)
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT status, scopes_json, updated_at FROM connector_oauth_connections "
            "WHERE provider = ? AND org_id = ?",
            (provider, _scope(org_id)),
        ).fetchone()
    return {
        "provider": provider,
        "status": row["status"] if row else "disconnected",
        "scopes": json.loads(row["scopes_json"]) if row else [],
        "updated_at": row["updated_at"] if row else None,
    }


def grant_ready(provider: str, *, org_id: str | None = None) -> bool:
    """Internal readiness probe; validates decryptability without exposing a value."""
    if status(provider, org_id=org_id)["status"] != "connected":
        return False
    try:
        grant = json.loads(
            managed_secrets._load_for_consumer(
                _secret_name(provider, "grant"), "connector", org_id=org_id
            )
        )
    except Exception:
        return False
    return (
        isinstance(grant, dict)
        and isinstance(grant.get("access_token"), str)
        and bool(grant["access_token"])
    )


@contextmanager
def _lock(provider: str, org_id: str | None):
    """Serialize grant changes across hub workers and processes."""
    key = (provider, _scope(org_id))
    with _LOCKS_GUARD:
        thread_lock = _LOCKS.setdefault(key, threading.Lock())
    lock_dir = config.data_home() / "connector-oauth-locks"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = hashlib.sha256(f"{provider}:{_scope(org_id)}".encode()).hexdigest()
    with thread_lock:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(lock_dir / filename, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_mode & 0o777 != 0o600
            ):
                raise ConnectorOAuthError("connector OAuth lock file is insecure")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _mark_reconnect(provider: str, org_id: str | None) -> None:
    with storage.connect() as conn:
        conn.execute(
            "UPDATE connector_oauth_connections SET status='needs_reconnect', updated_at=? "
            "WHERE provider=? AND org_id=?",
            (storage.now(), provider, _scope(org_id)),
        )
        conn.commit()


def _transition_with_event(
    provider: str,
    org_id: str | None,
    *,
    status_value: str,
    channel: str,
    actor: str,
    device_node: str | None,
    purpose: str,
    scopes: list[str] | None = None,
    clear_pending: bool = False,
) -> None:
    payload = _event_payload(channel, provider, actor, org_id, device_node, purpose, scopes=scopes)
    with storage.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if clear_pending:
            conn.execute(
                "DELETE FROM connector_oauth_pending WHERE provider=? AND org_id=?",
                (provider, _scope(org_id)),
            )
        conn.execute(
            "INSERT INTO connector_oauth_connections "
            "(provider, org_id, status, scopes_json, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(provider, org_id) DO UPDATE SET status=excluded.status, "
            "scopes_json=CASE WHEN excluded.status='disconnected' THEN '[]' "
            "ELSE connector_oauth_connections.scopes_json END, updated_at=excluded.updated_at",
            (
                provider,
                _scope(org_id),
                status_value,
                "[]" if scopes is None else json.dumps(scopes),
                storage.now(),
            ),
        )
        _, _, publish_payload = events.record_event_once_on_connection(
            conn, channel, payload=payload, originator=actor
        )
        conn.commit()
    if publish_payload is not None:
        events.publish(publish_payload)


def mark_invalid_credential(
    provider: str,
    *,
    actor: str,
    purpose: str,
    org_id: str | None = None,
    device_node: str | None = None,
    rejected_token: str,
) -> None:
    """Provider API 401: stop use until a human reconnects; never retry it."""
    _spec(provider)
    if not actor or not _PURPOSE_RE.fullmatch(purpose):
        raise ConnectorOAuthError("actor and purpose are required")
    with _lock(provider, org_id):
        try:
            grant = json.loads(
                managed_secrets._load_for_consumer(
                    _secret_name(provider, "grant"), "connector", org_id=org_id
                )
            )
        except Exception:
            return
        if not secrets.compare_digest(str(grant.get("access_token", "")), rejected_token):
            return
        _transition_with_event(
            provider,
            org_id,
            status_value="needs_reconnect",
            channel="connector.oauth.needs_reconnect",
            actor=actor,
            device_node=device_node,
            purpose=purpose,
        )


def access_token_for(
    provider: str,
    *,
    actor: str,
    purpose: str,
    org_id: str | None = None,
    device_node: str | None = None,
) -> str:
    """Private provider-consumer path. Never register this as a hub tool or route."""
    spec = _spec(provider)
    _require_enabled(provider)
    if not actor or not _PURPOSE_RE.fullmatch(purpose):
        raise ConnectorOAuthError("actor and purpose are required")
    with _lock(provider, org_id):
        if status(provider, org_id=org_id)["status"] != "connected":
            raise ConnectorOAuthError("connector needs reconnect")
        try:
            grant = json.loads(
                managed_secrets._load_for_consumer(
                    _secret_name(provider, "grant"), "connector", org_id=org_id
                )
            )
        except Exception as exc:
            raise ConnectorOAuthError("connector grant is unavailable") from exc
        expiry = grant.get("expires_at")
        needs_refresh = spec.refresh_supported and (
            not isinstance(expiry, int) or expiry <= time.time() + _REFRESH_MARGIN
        )
        if needs_refresh:
            client_id, client_secret = _client_credentials(provider, org_id)
            try:
                with httpx.Client(timeout=15, trust_env=False) as client:
                    response = client.post(
                        spec.token_url,
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": grant["refresh_token"],
                            "client_id": client_id,
                            "client_secret": client_secret,
                        },
                        headers={"Accept": "application/json"},
                    )
            except httpx.HTTPError as exc:
                raise ConnectorOAuthError("provider refresh unavailable") from exc
            if response.status_code == 400:
                try:
                    invalid_grant = response.json().get("error") == "invalid_grant"
                except ValueError:
                    invalid_grant = False
                if invalid_grant:
                    _transition_with_event(
                        provider,
                        org_id,
                        status_value="needs_reconnect",
                        channel="connector.oauth.needs_reconnect",
                        actor=actor,
                        device_node=device_node,
                        purpose=purpose,
                    )
                    raise ConnectorOAuthError("connector needs reconnect")
            refreshed = _token_response(response)
            grant["access_token"] = refreshed["access_token"]
            grant["refresh_token"] = refreshed.get("refresh_token") or grant["refresh_token"]
            grant["expires_at"] = _expiry(refreshed, required=True)
            # A rotated grant cannot be used if its audit event fails.
            _mark_reconnect(provider, org_id)
            managed_secrets.set_secret(
                _secret_name(provider, "grant"),
                json.dumps(grant),
                requested_by=actor,
                consumer="connector",
                org_id=org_id,
            )
        _event("connector.oauth.used", provider, actor, org_id, device_node, purpose)
        if needs_refresh:
            with storage.connect() as conn:
                conn.execute(
                    "UPDATE connector_oauth_connections SET status='connected', updated_at=? "
                    "WHERE provider=? AND org_id=? AND status='needs_reconnect'",
                    (storage.now(), provider, _scope(org_id)),
                )
                conn.commit()
        return str(grant["access_token"])


def disconnect(provider: str, *, actor: str, org_id: str | None = None) -> dict[str, object]:
    _spec(provider)
    if not actor:
        raise ConnectorOAuthError("authenticated identity required")
    with _lock(provider, org_id):
        # Durable intent and unusable status precede irreversible file removal.
        # A failed final event leaves a retryable needs_reconnect row and intent.
        _transition_with_event(
            provider,
            org_id,
            status_value="needs_reconnect",
            channel="connector.oauth.disconnect_requested",
            actor=actor,
            device_node=None,
            purpose="disconnect",
            clear_pending=True,
        )
        managed_secrets.delete_secret(_secret_name(provider, "grant"), org_id=org_id)
        _transition_with_event(
            provider,
            org_id,
            status_value="disconnected",
            channel="connector.oauth.disconnected",
            actor=actor,
            device_node=None,
            purpose="disconnect",
            scopes=[],
        )
    return status(provider, org_id=org_id)
