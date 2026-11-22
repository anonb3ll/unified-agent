"""Hub identity legs below the adapter layer: the WorkOS leg and the OAuth lookup.

``config`` decides at start-up whether a credential leg is configured, and
``audit.producer_sign`` reuses the WorkOS identity map. Both live in core, so
the parts of the hub's credential legs they read live here too (task #1721):

* The WorkOS hub leg (``WorkOSHubAuth``), its readiness checks (``enabled``,
  ``build_hub_auth``, ``hub_auth``) and the operator's identity map. Moved
  unchanged from ``workos_hub_auth``, which re-exports every moved name and
  forwards a patch of one here; see that module for the leg's contract. Its
  log lines keep the ``unified_agent.workos_hub_auth`` logger name.
* ``oauth_provider``: the hub OAuth provider lookup. The provider itself is
  the ``oauth`` adapter; importing ``oauth`` registers ``oauth.get_provider``
  here, and the hub server imports it at start-up. A process that never
  loaded ``oauth`` has no OAuth provider and gets ``None``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from .pilot_auth import WorkOSClaims, WorkOSSDKProvider

logger = logging.getLogger("unified_agent.workos_hub_auth")

# A sealed session is a Fernet token over the SDK's session payload. Real ones
# are well under this; the bound just stops an unbounded header from reaching
# the crypto layer at all.
MAX_SEALED_SESSION = 8192

# Refresh a held session this long before its access token expires, so a
# request that starts just before expiry does not fail half-way (task #2114).
REFRESH_SKEW_S = 30


class WorkOSHubAuth:
    """Resolves a sealed WorkOS session to a reviewed UA identity label."""

    def __init__(self, provider, *, organization_id: str, identity_map: dict[str, str]):
        if (
            not organization_id
            or not identity_map
            or any(
                not isinstance(k, str) or not isinstance(v, str) or not k or not v or len(v) > 100
                for k, v in identity_map.items()
            )
        ):
            raise ValueError("hub WorkOS leg requires an organization and identity mapping")
        if getattr(provider, "organization_id", None) != organization_id:
            raise ValueError("hub WorkOS provider is bound to a different organization")
        self.provider = provider
        self.organization_id = organization_id
        self.identity_map = dict(identity_map)

    def resolve(self, sealed_session: str | None) -> dict | None:
        """Return non-secret session facts, or None. Never raises."""
        if not sealed_session or len(sealed_session) > MAX_SEALED_SESSION:
            return None
        try:
            claims = self.provider.authenticate_sealed(sealed_session)
            active = self.provider.active(claims)
        except Exception as exc:
            # Provider text may carry a response body or credential, so only
            # the class is recorded (same rule as `PilotAuth.login`).
            logger.warning("hub WorkOS session rejected: %s", type(exc).__name__)
            return None
        if not active:
            return None
        return self._identity_for(claims)

    def resolve_refreshing(self, sealed_session: str | None) -> tuple[dict | None, str | None]:
        """Like `resolve`, but refresh an access token that is expired or about to be.

        Returns ``(session facts or None, replacement sealed session or None)``.
        A caller that holds the sealed session (a cookie, a server-side store)
        must persist the replacement whenever one is returned, even when the
        facts are None: WorkOS rotates the refresh token, so the old sealed
        value cannot be refreshed again (task #2114).

        The `Authorization: WorkOS` header leg does not use this. Its client
        holds the session, and a refresh the client never sees would only burn
        the client's refresh token.
        """
        if not sealed_session or len(sealed_session) > MAX_SEALED_SESSION:
            return None, None
        replacement = None
        try:
            expires_at = self.provider.sealed_expires_at(sealed_session)
            if expires_at is None or expires_at <= time.time() + REFRESH_SKEW_S:
                replacement = self.provider.refresh_sealed(sealed_session)
                if not replacement or len(replacement) > MAX_SEALED_SESSION:
                    return None, None
        except Exception as exc:
            # Same rule as `resolve`: the provider's text may carry a credential.
            logger.warning("hub WorkOS session refresh failed: %s", type(exc).__name__)
            return None, None
        return self.resolve(replacement or sealed_session), replacement

    def _identity_for(self, claims: WorkOSClaims) -> dict | None:
        from . import config

        if (
            claims.organization_id != self.organization_id
            or claims.membership_status != "active"
            or not claims.session_id
            or not isinstance(claims.expires_at, (int, float))
            or isinstance(claims.expires_at, bool)
            or claims.expires_at <= time.time()
        ):
            return None
        # The mapping is the whole authorization decision. An unmapped subject
        # is denied, and a mapped label that is not already a known UA identity
        # is denied too — this leg selects among identities the operator has
        # registered, it never mints one.
        identity = self.identity_map.get(claims.subject)
        if not identity or not config.is_known_agent_label(identity):
            return None
        return {
            "identity": identity,
            "subject": claims.subject,
            "organization_id": claims.organization_id,
            "session_id": claims.session_id,
            "expires_at": int(claims.expires_at),
        }


def _identity_map_from_environment() -> dict[str, str]:
    raw = os.environ.get("UA_HUB_WORKOS_IDENTITY_MAP", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("UA_HUB_WORKOS_IDENTITY_MAP is not valid JSON; hub WorkOS leg stays off")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("UA_HUB_WORKOS_IDENTITY_MAP must be a JSON object; hub WorkOS leg stays off")
        return {}
    return parsed


def enabled() -> bool:
    """Whether the operator has turned this leg on at all."""
    from . import edition

    return bool(
        edition.is_enterprise() and os.environ.get("UA_HUB_WORKOS_ORGANIZATION_ID", "").strip()
    )


def build_hub_auth() -> WorkOSHubAuth | None:
    """Construct the leg from the environment, or None if it is not configured."""
    if not enabled():
        return None
    organization_id = os.environ["UA_HUB_WORKOS_ORGANIZATION_ID"].strip()
    identity_map = _identity_map_from_environment()
    if not identity_map:
        logger.warning(
            "UA_HUB_WORKOS_ORGANIZATION_ID is set but UA_HUB_WORKOS_IDENTITY_MAP is missing "
            "or empty; the hub WorkOS leg stays off"
        )
        return None
    try:
        from workos import WorkOSClient

        issuer = os.environ["WORKOS_ISSUER"]
        provider = WorkOSSDKProvider(
            WorkOSClient(
                api_key=os.environ["WORKOS_API_KEY"],
                client_id=os.environ["WORKOS_CLIENT_ID"],
                jwt_issuer=issuer,
            ),
            # The hub leg never redirects — it only accepts a session the
            # application already issued — but the provider's HTTPS callback
            # guard is shared, so the same registered value is supplied.
            redirect_uri=os.environ["WORKOS_REDIRECT_URI"],
            cookie_password=os.environ["WORKOS_COOKIE_PASSWORD"],
            organization_id=organization_id,
            issuer=issuer,
        )
        leg = WorkOSHubAuth(provider, organization_id=organization_id, identity_map=identity_map)
    except Exception as exc:
        logger.warning(
            "hub WorkOS leg is configured but could not be built (%s); it stays off",
            type(exc).__name__,
        )
        return None
    from . import config

    if not config.enforce_auth():
        # Turning this leg on says "humans authenticate here", but the hub's
        # warn-only default still lets an unauthenticated request through. An
        # operator who wires up an IdP and leaves UA_ENFORCE_AUTH unset gets a
        # login that decides nothing (Devin review, PR #677).
        logger.warning(
            "hub WorkOS leg is enabled but UA_ENFORCE_AUTH is not 1: unauthenticated requests "
            "are still accepted, so the WorkOS session gates nothing. Set UA_ENFORCE_AUTH=1."
        )
    return leg


_cache: tuple[str, WorkOSHubAuth | None] | None = None


def _cache_key() -> str:
    from . import edition

    return "|".join(
        [
            edition.current_edition(),
            os.environ.get("UA_HUB_WORKOS_ORGANIZATION_ID", ""),
            os.environ.get("UA_HUB_WORKOS_IDENTITY_MAP", ""),
            os.environ.get("WORKOS_CLIENT_ID", ""),
            os.environ.get("WORKOS_ISSUER", ""),
        ]
    )


def hub_auth() -> WorkOSHubAuth | None:
    """Process-wide leg, rebuilt when its configuration changes."""
    global _cache
    key = _cache_key()
    if _cache is None or _cache[0] != key:
        _cache = (key, build_hub_auth())
    return _cache[1]


def reset_hub_auth_cache() -> None:
    global _cache
    _cache = None


_oauth_provider_lookup: Callable[[], Any] | None = None


def register_oauth_provider_lookup(lookup: Callable[[], Any]) -> None:
    """Set the hub OAuth provider lookup (called once, by the ``oauth`` adapter)."""
    global _oauth_provider_lookup
    _oauth_provider_lookup = lookup


def oauth_provider() -> Any | None:
    """The hub OAuth provider when OAuth is configured, else None.

    Exactly ``oauth.get_provider()`` once ``oauth`` is loaded; ``None`` in a
    process that never loaded it.
    """
    lookup = _oauth_provider_lookup
    return lookup() if lookup is not None else None
