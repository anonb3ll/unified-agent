"""Isolated single-organization WorkOS pilot boundary."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from os import environ
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

logger = logging.getLogger("unified_agent.pilot_auth")

# Exception classes raised by *our* call into the SDK rather than by the
# provider itself. Their messages are Python-generated (a bad keyword, a
# missing attribute) and carry no provider payload, so they are safe to log.
# Anything else — an SDK or transport error — may embed a response body or
# credential, so only its class name is recorded (Devin review, PR #671).
_LOGGABLE_CAUSES = (TypeError, AttributeError, KeyError, IndexError, NotImplementedError)


class PilotAuthError(ValueError):
    pass


@dataclass(frozen=True)
class WorkOSClaims:
    subject: str
    organization_id: str | None
    membership_status: str = "active"
    session_id: str | None = None
    expires_at: int | None = None


class WorkOSProvider(Protocol):
    def authorization_url(self, state: str) -> str: ...
    def authenticate(self, code: str) -> WorkOSClaims: ...
    def logout_url(self, session_id: str) -> str: ...

    def active(self, claims: WorkOSClaims) -> bool: ...


class WorkOSSDKProvider:
    def __init__(
        self,
        client,
        *,
        redirect_uri: str,
        cookie_password: str,
        issuer: str = "https://api.workos.com/",
        organization_id: str,
        webhook_secret: str = "",
    ):
        if (
            not organization_id
            or len(cookie_password) < 32
            or urlparse(redirect_uri).scheme != "https"
        ):
            raise ValueError(
                "pilot requires an organization, HTTPS callback and 32-character cookie key"
            )
        from cryptography.fernet import Fernet

        Fernet(cookie_password.encode())  # SDK sealed sessions require a Fernet key.
        if urlparse(issuer).scheme != "https":
            raise ValueError("pilot issuer must be an operator-configured HTTPS URL")
        self.organization_id = organization_id
        self.webhook_secret = webhook_secret
        self.client, self.redirect_uri, self.cookie_password, self.issuer = (
            client,
            redirect_uri,
            cookie_password,
            issuer,
        )

    @classmethod
    def from_environment(cls) -> WorkOSSDKProvider:
        from workos import WorkOSClient

        issuer = environ["WORKOS_ISSUER"]
        return cls(
            WorkOSClient(
                api_key=environ["WORKOS_API_KEY"],
                client_id=environ["WORKOS_CLIENT_ID"],
                jwt_issuer=issuer,
            ),
            redirect_uri=environ["WORKOS_REDIRECT_URI"],
            cookie_password=environ["WORKOS_COOKIE_PASSWORD"],
            organization_id=environ["WORKOS_ORGANIZATION_ID"],
            webhook_secret=environ["WORKOS_WEBHOOK_SECRET"],
            issuer=issuer,
        )

    def authorization_url(self, state: str) -> str:
        return self.client.user_management.get_authorization_url(
            provider="authkit",
            state=state,
            redirect_uri=self.redirect_uri,
            organization_id=self.organization_id,
        )

    def authenticate(self, code: str) -> WorkOSClaims:
        from workos.session import seal_data

        result = self.client.user_management.authenticate_with_code(code=code)
        return self.authenticate_sealed(seal_data(result.to_dict(), self.cookie_password))

    def authenticate_sealed(self, sealed_session: str) -> WorkOSClaims:
        """Verify an already-sealed session and return its claims.

        Split out of `authenticate` so the optional hub auth leg
        (`workos_hub_auth`, task #1202) verifies through this exact code rather
        than a second implementation. The checks below are the contract both
        callers depend on; do not relax one for a caller that finds it
        inconvenient.
        """
        from workos.session import unseal_data

        session = self.client.user_management.load_sealed_session(
            session_data=sealed_session, cookie_password=self.cookie_password
        )
        checked = session.authenticate()
        if not checked.authenticated:
            raise PilotAuthError("invalid WorkOS session")
        raw = unseal_data(sealed_session, self.cookie_password)
        import jwt

        token = raw["access_token"]
        key = session.jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=self.issuer,
            options={"verify_aud": False, "require": ["exp", "iat", "sub", "sid", "org_id"]},
        )
        audience = claims.get("aud")
        audience_matches = audience == self.client.client_id or (
            isinstance(audience, list) and self.client.client_id in audience
        )
        user_id = checked.user["id"] if isinstance(checked.user, dict) else checked.user.id
        if (
            claims["sub"] != user_id
            or claims["sid"] != checked.session_id
            or claims["org_id"] != self.organization_id
            or checked.organization_id != self.organization_id
            or ("aud" in claims and not audience_matches)
        ):
            raise PilotAuthError("WorkOS session claims mismatch")
        # WorkOS v10 returns `user` as a dict (optional); v9 returned an
        # object. Use the `user_id` normalized above rather than touching
        # `.id` here — a dict `user` has no attribute access, and every valid
        # v10 login raised AttributeError before this (Devin review, PR #616).
        return WorkOSClaims(
            user_id,
            checked.organization_id,
            session_id=checked.session_id,
            expires_at=claims["exp"],
        )

    def sealed_expires_at(self, sealed_session: str) -> int | None:
        """The access token's ``exp`` inside a sealed session, or None.

        Read without verifying the signature. It only decides *whether to
        refresh* (task #2114); `authenticate_sealed` still verifies whatever
        token is finally accepted.
        """
        from workos.session import unseal_data

        try:
            token = unseal_data(sealed_session, self.cookie_password).get("access_token")
            if not isinstance(token, str):
                return None
            import jwt

            exp = jwt.decode(token, options={"verify_signature": False}).get("exp")
        except Exception:  # noqa: BLE001 - an unreadable session is simply "no expiry"
            return None
        return int(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None

    def refresh_sealed(self, sealed_session: str) -> str:
        """Trade the sealed refresh token for a new sealed session (task #2114).

        AuthKit access tokens live about five minutes, so a sealed session held
        for longer (the approver cookie, a Control Center sign-in) must be
        refreshed rather than re-checked against an expiry it has already
        passed. The SDK posts the refresh token, verifies the new access token
        against the application JWKS and re-seals it with the same cookie key.
        Scoped to the one configured organization. Raises on any denial.
        """
        session = self.client.user_management.load_sealed_session(
            session_data=sealed_session, cookie_password=self.cookie_password
        )
        result = session.refresh(organization_id=self.organization_id)
        sealed = getattr(result, "sealed_session", None)
        if not getattr(result, "authenticated", False) or not isinstance(sealed, str) or not sealed:
            raise PilotAuthError("WorkOS session refresh denied")
        return sealed

    def logout_url(self, session_id: str) -> str:
        return self.client.user_management.get_logout_url(session_id=session_id)

    def active(self, claims: WorkOSClaims) -> bool:
        if claims.organization_id != self.organization_id:
            return False
        memberships = self.client.organization_membership.list_organization_memberships(
            user_id=claims.subject, organization_id=self.organization_id, statuses=["active"]
        )
        if not any(
            m.user_id == claims.subject
            and m.organization_id == self.organization_id
            and m.status == "active"
            for m in memberships.auto_paging_iter()
        ):
            return False
        # SDK-drift, same class as the v9->v10 AttributeError above (PR #616):
        # 10.4.0 names the user id `id`, and `expires_at` is already a
        # `datetime`, not an ISO string. Both raised TypeError inside the
        # `except Exception` in `PilotAuth.login`, so every live login failed
        # as a generic denial (task #1243).
        for session in self.client.user_management.list_sessions(
            id=claims.subject
        ).auto_paging_iter():
            if session.id == claims.session_id:
                return (
                    session.user_id == claims.subject
                    and session.organization_id == self.organization_id
                    and session.status == "active"
                    and session.expires_at.timestamp() > time.time()
                )
        return False

    def lifecycle(self, body: bytes, signature: str) -> dict | None:
        if not self.webhook_secret or len(body) > 65536:
            raise PilotAuthError("lifecycle verification denied")
        # SDK verifies the MAC; also reject future timestamps before processing.
        timestamp = int(signature.split(", ")[0].removeprefix("t=")) / 1000
        if abs(time.time() - timestamp) > 180:
            raise PilotAuthError("lifecycle timestamp denied")
        self.client.webhooks.verify_header(
            event_body=body, event_signature=signature, secret=self.webhook_secret, tolerance=180
        )
        event = json.loads(body)
        if event.get("event") not in {
            "organization_membership.updated",
            "organization_membership.deleted",
            "session.revoked",
        }:
            return None
        data = event["data"]
        if data.get("organization_id") != self.organization_id:
            raise PilotAuthError("lifecycle organization denied")
        return {"event_id": event["id"], "subject": data["user_id"]}


class PilotAuth:
    def __init__(
        self,
        db_path: str | Path,
        organization_id: str,
        identity_map: dict[str, str],
        provider: WorkOSProvider,
    ):
        if (
            not organization_id
            or not identity_map
            or any(
                not isinstance(k, str) or not isinstance(v, str) or not k or not v or len(v) > 100
                for k, v in identity_map.items()
            )
        ):
            raise ValueError("pilot organization and identity mapping are required")
        self.db_path = Path(db_path)
        self.db_path = self.db_path.expanduser().resolve()
        private = (
            Path(environ.get("UA_PILOT_PRIVATE_HOME", "~/.unified_agent")).expanduser().resolve()
        )
        if self.db_path.is_relative_to(private) or self.db_path.name == "memory.db":
            raise ValueError("pilot datastore must be separate from the private hub store")
        if self.db_path.exists() and self.db_path.stat().st_nlink != 1:
            raise ValueError("pilot datastore must not be hard-linked")
        self.organization_id, self.identity_map, self.provider = (
            organization_id,
            dict(identity_map),
            provider,
        )
        self._init_db()

    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.db_path.exists():
            fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        if self.db_path.stat().st_mode & 0o077:
            raise PilotAuthError("pilot datastore must be owner-only")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS pilot_owner (id INTEGER PRIMARY KEY CHECK(id=1), organization TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pilot_sessions (
                    id TEXT PRIMARY KEY, subject TEXT NOT NULL, ua_identity TEXT NOT NULL,
                    csrf TEXT NOT NULL, workos_session TEXT NOT NULL, expires_at INTEGER NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pilot_states (state TEXT PRIMARY KEY, expires_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pilot_lifecycle (event_id TEXT PRIMARY KEY, subject TEXT NOT NULL);
            """)
            conn.execute("INSERT OR IGNORE INTO pilot_owner VALUES (1, ?)", (self.organization_id,))
            if (
                conn.execute("SELECT organization FROM pilot_owner WHERE id=1").fetchone()[0]
                != self.organization_id
            ):
                raise PilotAuthError("pilot datastore belongs to another organization")

    def begin_login(self) -> str:
        state = secrets.token_urlsafe(32)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO pilot_states VALUES (?, datetime('now', '+10 minutes'))", (state,)
            )
        return state

    def login(self, code: str, state: str) -> dict:
        with self._connect() as conn:
            valid = conn.execute(
                "DELETE FROM pilot_states WHERE state = ? AND expires_at > datetime('now') RETURNING state",
                (state,),
            ).fetchone()
        if not valid:
            raise PilotAuthError("invalid or expired login state")
        try:
            claims = self.provider.authenticate(code)
            active = self.provider.active(claims)
        except Exception as exc:
            # The client-facing denial stays generic, but discarding the cause
            # entirely hid two SDK-drift TypeErrors for the whole life of this
            # adapter (task #1243). Record the class always, and the message
            # only for the drift classes above — never the code, the token or
            # any provider-supplied text.
            detail = (
                str(exc)[:200]
                if isinstance(exc, _LOGGABLE_CAUSES)
                else "<provider detail withheld>"
            )
            logger.warning("pilot WorkOS validation failed: %s: %s", type(exc).__name__, detail)
            raise PilotAuthError("WorkOS validation unavailable or denied") from None
        if (
            not active
            or not claims.session_id
            or not isinstance(claims.expires_at, (int, float))
            or claims.expires_at <= time.time()
        ):
            raise PilotAuthError("invalid or expired WorkOS session")
        if claims.organization_id != self.organization_id or claims.membership_status != "active":
            raise PilotAuthError("organization membership denied")
        identity = self.identity_map.get(claims.subject)
        if not identity:
            raise PilotAuthError("identity mapping denied")
        with self._connect() as conn:
            sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO pilot_sessions VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    sid,
                    claims.subject,
                    identity,
                    csrf,
                    claims.session_id,
                    claims.expires_at,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return {
            "id": sid,
            "csrf": csrf,
            "identity": identity,
            "logout_url": self.provider.logout_url(claims.session_id)
            if claims.session_id
            else None,
        }

    def local_session(self, session_id: str | None) -> dict | None:
        if not session_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pilot_sessions WHERE id = ? AND revoked = 0", (session_id,)
            ).fetchone()
            if not row or (
                row["expires_at"] is not None
                and row["expires_at"] <= int(datetime.now(UTC).timestamp())
            ):
                return None
            if self.identity_map.get(row["subject"]) != row["ua_identity"]:
                return None
            return dict(row)

    def resolve(self, session_id: str | None) -> dict | None:
        row = self.local_session(session_id)
        if not row:
            return None
        claims = WorkOSClaims(
            row["subject"],
            self.organization_id,
            session_id=row["workos_session"],
            expires_at=row["expires_at"],
        )
        try:
            return dict(row) if self.provider.active(claims) else None
        except Exception:
            return None

    def revoke(self, session_id: str | None) -> None:
        if session_id:
            with self._connect() as conn:
                conn.execute("UPDATE pilot_sessions SET revoked = 1 WHERE id = ?", (session_id,))

    def apply_lifecycle(self, *, event_id: str, subject: str) -> None:
        if not event_id or not subject:
            raise PilotAuthError("invalid lifecycle event")
        with self._connect() as conn:
            old = conn.execute(
                "SELECT subject FROM pilot_lifecycle WHERE event_id=?", (event_id,)
            ).fetchone()
            if old and old["subject"] == subject:
                return
            # Notifications only invalidate. Delayed/reordered events cannot grant access;
            # a new login must revalidate current provider membership and session state.
            conn.execute("UPDATE pilot_sessions SET revoked=1 WHERE subject=?", (subject,))
            conn.execute("INSERT OR IGNORE INTO pilot_lifecycle VALUES (?, ?)", (event_id, subject))
