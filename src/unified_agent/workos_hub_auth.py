"""Optional hub auth leg: a WorkOS AuthKit session mapped to a UA identity.

The separate-process pilot (`pilot_app`/`pilot_auth`, docs/workos-pilot.md)
stays exactly as it is and remains the fallback. This module adds a *third*
credential leg to the private hub itself, alongside the bearer token and the
OAuth access token, for operators who want a human to reach the hub through
their own identity provider rather than through a shared agent token.

Three properties hold by construction:

* It is off unless the operator turns it on — `UA_EDITION=enterprise` **and**
  `UA_HUB_WORKOS_ORGANIZATION_ID` set. Absent either, `hub_auth()` is `None`
  and the middleware never grows a new code path.
* It verifies exactly what the pilot verifies. Both call
  `WorkOSSDKProvider.authenticate_sealed` (JWKS signature, exact configured
  issuer, required `exp`/`iat`/`sub`/`sid`/`org_id`, audience, `sub`/`sid`
  agreement, the one configured organization) followed by
  `WorkOSSDKProvider.active` (live membership re-check and live session state).
  `active` runs on *every* request, so a revoked session or a removed
  membership stops working on the next call rather than at token expiry.
* WorkOS never grants UA capability. The only thing read out of a verified
  session is the subject id, which a reviewed operator-supplied mapping turns
  into an existing UA agent label. Roles, permissions, groups and email
  domains from the provider are not consulted for anything, ever.

The leg itself (``WorkOSHubAuth``, its readiness checks ``enabled``,
``build_hub_auth`` and ``hub_auth``, the cache and the identity map) lives in
``identity_core`` since task #1721, so ``config`` and ``audit.producer_sign``
read it without importing this adapter. Every moved name still resolves here,
and a patch of one made here (``monkeypatch.setattr(workos_hub_auth, "enabled",
...)``) lands in ``identity_core``, so every caller sees it.
"""

from __future__ import annotations

import sys
import types

from . import identity_core

AUTH_SCHEME = "workos"

#: Names that moved to ``identity_core`` (task #1721). Read through this module
#: they resolve there; set or deleted through it they change it there.
MOVED_TO_IDENTITY_CORE = (
    "MAX_SEALED_SESSION",
    "WorkOSClaims",
    "WorkOSHubAuth",
    "WorkOSSDKProvider",
    "_cache",
    "_cache_key",
    "_identity_map_from_environment",
    "build_hub_auth",
    "enabled",
    "hub_auth",
    "logger",
    "reset_hub_auth_cache",
)


def sealed_session_from_header(auth_header: str | None) -> str | None:
    """The sealed session from an `Authorization: WorkOS <session>` header."""
    if not auth_header:
        return None
    scheme, _, rest = auth_header.partition(" ")
    if scheme.lower() != AUTH_SCHEME:
        return None
    return rest.strip() or None


class _IdentityCoreForwardingModule(types.ModuleType):
    """This module, with the moved names kept in ``identity_core`` only.

    There is one copy of each moved name, in ``identity_core``: a read here
    returns it, and a patch here (or its undo) rebinds it there. So a patch
    through either module is seen by ``config``'s start-up guard, by
    ``identity_core.hub_auth``'s call to ``build_hub_auth``, and by the hub
    middleware, which reads ``workos_hub_auth.<name>``.
    """

    def __getattr__(self, name: str):
        if name in MOVED_TO_IDENTITY_CORE:
            return getattr(identity_core, name)
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value) -> None:
        if name in MOVED_TO_IDENTITY_CORE:
            setattr(identity_core, name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in MOVED_TO_IDENTITY_CORE:
            delattr(identity_core, name)
        else:
            super().__delattr__(name)


sys.modules[__name__].__class__ = _IdentityCoreForwardingModule
