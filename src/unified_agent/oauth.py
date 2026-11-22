"""Opt-in OAuth 2.1 authorization server for remote MCP clients and agents.

Remote MCP clients speak OAuth 2.1 with dynamic client registration (RFC 7591) and
PKCE. This module is the hub-side authorization server: RFC 8414 metadata, RFC 7591
registration, authorization-code + PKCE, verified-human consent, and RFC 9728
protected-resource metadata.

Off by default. Enable only with ``UA_MCP_OAUTH=1``, a public HTTPS issuer
(``UA_MCP_OAUTH_ISSUER``), and an approver: a verified human (task #1322, J3 —
WorkOS session, or tailnet login ``UA_MCP_OAUTH_APPROVER_LOGINS`` plus a passkey;
see ``oauth_approver``) or the legacy passphrase (``UA_MCP_OAUTH_PASSPHRASE``),
which approves only with ``UA_MCP_OAUTH_PASSPHRASE_FALLBACK=1``.
Issued access tokens authenticate as the approved agent identity (chosen or confirmed
at the consent screen, defaulting to ``UA_MCP_OAUTH_AGENT`` or ``Remote_Agent``).
Config-pinned confidential clients may use the non-interactive ``client_credentials``
grant; those tokens are bound to their configured agent and are not refreshable.

Task #870 hardenings (private/tailnet first; no public-exposure enablement here):
* Public-Host consent pins identity to ``UA_MCP_OAUTH_AGENT`` — a passphrase
  holder cannot pick another fleet label on a public front door (#812 residual).
* Non-interactive client-credentials at ``/oauth/client-credentials`` for cloud
  bots, mapped to existing profiles only (``UA_MCP_OAUTH_CLIENT_CREDENTIALS``).
* Refresh-token rotation with identity preservation and linked revoke/audit.
* ``GET /oauth/whoami`` (+ ``unified-agent doctor --whoami``) verification surface.

Durable store (task #1321, J2): registered clients, refresh tokens, and access tokens
persist in the hub DB (``oauth_store``; tokens and client secrets as SHA-256 digests
only), so a hub restart logs no device out. Only pending device grants (ten-minute
TTL) and authorization codes stay in memory.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import html
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, unquote

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route, request_response

from . import config, device_key, events, identity_core, oauth_cimd, oauth_store

_log = logging.getLogger(__name__)

# The client secret presented on the current /token or /revoke request. A client
# restored from the durable store has only a secret digest; ``get_client`` hands
# the SDK authenticator the presented secret only when it matches that digest.
_presented_client_secret: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ua_oauth_presented_client_secret", default=None
)
_SECRET_BIND_MAX_BODY = 64 * 1024
# Returned by a durable-store read that failed, as distinct from "no such row".
_STORE_DOWN = object()

_SCOPE = "mcp"
_CLIENT_CREDENTIALS_GRANT = "client_credentials"
_CODE_TTL_S = 300.0
_CONSENT_TTL_S = 600.0
_REFRESH_TTL_S = 30 * 24 * 3600

# RFC 8628 device authorization grant (task #1320, Epic J). A headless agent asks
# for a short user code, a human approves it in any browser, the device polls.
_DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_DEVICE_TTL_S = 600
_DEVICE_MAX_PENDING = 256
_DEVICE_INTERVAL_S = 5
_DEVICE_SLOW_DOWN_S = 5
# RFC 8628 §6.1: consonants only — no vowels (no accidental words), no 0/O, 1/I.
_USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"
# First-party public client for `unified-agent login` and node daemons. It has no
# secret (RFC 8628 devices cannot keep one); the human approval is the credential.
CLI_CLIENT_ID = "unified-agent-cli"

_OAUTH_PUBLIC_PATHS = frozenset(
    {
        "/authorize",
        "/token",
        "/register",
        "/revoke",
        "/oauth/consent",
        "/oauth/client-credentials",
        "/oauth/device_authorization",
        "/oauth/device",
        "/oauth/connectors/callback",
        "/.well-known/oauth-authorization-server",
    }
)
_OAUTH_PUBLIC_PREFIXES = (
    "/.well-known/oauth-protected-resource",
    "/oauth/consent",
    "/oauth/approver",
)
# Authenticated but still part of the public-Host slice (Funnel / PUBLIC_HOSTS).
_OAUTH_SLICE_AUTH_PATHS = frozenset({"/oauth/whoami"})


def _now() -> float:
    """Clock seam for the device grant (tests replace it)."""
    return time.time()


def _resolve_peer_device(peer_ip: str | None) -> dict[str, Any] | None:
    """Tailnet node for a peer IP, or None (fail-closed; see tailnet.py)."""
    try:
        from . import tailnet

        return tailnet.resolve_peer_device(peer_ip)
    except Exception:
        return None


def _device_sign_in_banner(client_id: str, device_node: dict[str, str] | None) -> str:
    """Approval-page sentence. A device-key binding shows its thumbprint."""
    node = (device_node or {}).get("node") or ""
    if node.startswith(device_key.PREFIX):
        thumb = html.escape(node[len(device_key.PREFIX) :])
        who = html.escape(client_id)
        return (
            f"<strong>{who}</strong> is asking to sign in from this device. "
            "Confirm the device-key thumbprint matches what "
            f"<code>unified-agent login</code> printed: <code>{thumb}</code>."
        )
    where = node or "an unidentified device"
    return (
        f"<strong>{html.escape(client_id)}</strong> on "
        f"<strong>{html.escape(where)}</strong> is asking to sign in to this hub."
    )


def _device_binding(node: dict[str, Any] | None) -> dict[str, str] | None:
    """The stable, non-secret part of a tailnet node that a token is bound to."""
    if not node:
        return None
    name = node.get("node") or node.get("name")
    node_id = node.get("node_id") or node.get("id")
    if not name and not node_id:
        return None
    return {"node": str(name or ""), "node_id": str(node_id or "")}


def _normalize_user_code(raw: str) -> str:
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


# Per-process key for the device page's anti-CSRF token. The device store is
# in-memory too, so a restart invalidating open approval pages loses nothing.
_DEVICE_CSRF_KEY = secrets.token_bytes(32)


def _device_csrf_token(user_code: str) -> str:
    """Form token bound to one user code. A cross-site page cannot mint it, so
    an attacker cannot trick an approver into approving the attacker's device."""
    msg = b"ua-device-approve:" + _normalize_user_code(user_code).encode()
    return hmac.new(_DEVICE_CSRF_KEY, msg, hashlib.sha256).hexdigest()


def _device_csrf_ok(user_code: str, got: str) -> bool:
    return bool(got) and hmac.compare_digest(_device_csrf_token(user_code), got)


def _consent_csrf_token(rid: str) -> str:
    """Form token bound to one consent request (task #1322). The passphrase used
    to double as CSRF protection; a cookie-borne WorkOS approval needs its own."""
    msg = b"ua-consent-approve:" + rid.encode()
    return hmac.new(_DEVICE_CSRF_KEY, msg, hashlib.sha256).hexdigest()


def _consent_csrf_ok(rid: str, got: str) -> bool:
    return bool(got) and hmac.compare_digest(_consent_csrf_token(rid), got)


def _new_user_code() -> str:
    chars = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"


@dataclass
class DeviceGrant:
    """One pending RFC 8628 device authorization. Never logged or serialized."""

    device_code: str
    user_code: str
    client_id: str
    scopes: list[str]
    resource: str | None
    expires_at: float
    interval: int
    device_node: dict[str, str] | None
    agent_hint: str | None = None
    status: str = "pending"  # pending | approved | denied
    subject: str | None = None
    last_poll: float | None = None
    human_approval: dict[str, Any] | None = None


def _retired_subject(subject: str | None) -> str | None:
    """Why ``subject`` may not receive a sign-in, or None (review of #2315).

    A retired label is refused; so is any label whose lifecycle state cannot
    be read right now (fail closed: a sign-in is not issued on a guess).
    """
    if not subject:
        return None
    from . import agent_lifecycle

    try:
        current = agent_lifecycle.retired_state(subject)
    except Exception:  # noqa: BLE001 - fail closed
        return "the hub cannot read this agent's lifecycle state right now; retry shortly"
    if current is None:
        return None
    return f"agent {config.canonical_identity(subject)!r} is retired; it cannot sign in"


class DeviceGrantError(Exception):
    """RFC 8628 §3.5 token-endpoint error for a device_code poll."""

    def __init__(self, error: str, description: str, status_code: int = 400) -> None:
        super().__init__(error)
        self.error = error
        self.description = description
        self.status_code = status_code


_CONSENT_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Unified-Agent OAuth Authorization</title>
<style>
body{{font:16px/1.4 system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;color:#1e293b;background:#f8fafc}}
.card{{background:#ffffff;padding:1.5rem;border-radius:8px;border:1px solid #e2e8f0;box-shadow:0 1px 3px rgba(0,0,0,0.05)}}
h1{{font-size:1.4rem;margin:0 0 1rem 0;color:#0f172a}}
p{{margin:0.5rem 0;font-size:0.95rem;color:#475569}}
label,input,select,button{{display:block;width:100%;margin:0.4rem 0;box-sizing:border-box}}
input,select,button{{padding:0.6rem 0.75rem;font:inherit;border:1px solid #cbd5e1;border-radius:6px;background:#ffffff}}
input:focus,select:focus{{outline:2px solid #2563eb;outline-offset:1px;border-color:#2563eb}}
button{{background:#2563eb;color:#ffffff;border:none;cursor:pointer;font-weight:600;margin-top:1.25rem;transition:background 0.15s}}
button:hover{{background:#1d4ed8}}
.hint{{color:#64748b;font-size:0.85rem;margin-top:0.25rem}}
.banner{{background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;padding:0.75rem 1rem;margin-bottom:1.25rem}}
.pin{{background:#f1f5f9;border:1px solid #cbd5e1;border-radius:6px;padding:0.75rem 1rem;margin:0.5rem 0 1rem}}
</style></head><body>
<div class="card">
<h1>Authorize {client_name}</h1>
<div class="banner">
<p><strong>{client_name}</strong> is requesting access to Unified-Agent tools, memory, and tasks on this hub.</p>
</div>
<form method="post" action="/oauth/consent" id="ua-approve-form">
<input type="hidden" name="rid" value="{rid}">
<input type="hidden" name="csrf" value="{csrf}">

{agent_block}

{approver_block}

<button type="submit" name="action" value="approve">Approve Access</button>
</form>
<p class="hint" style="margin-top:1.5rem;text-align:center">Refuse access by closing this browser tab.</p>
</div>
{approver_script}
</body></html>
"""

_AGENT_BLOCK_SELECTABLE = """\
<label for="agent"><strong>Agent Identity</strong></label>
<input id="agent" name="agent" list="agent-list" value="{suggested_agent}" autocomplete="off" required>
<datalist id="agent-list">
{agent_options}
</datalist>
<p class="hint">Hub actions performed with this connection will be attributed to this agent identity.</p>
"""

_AGENT_BLOCK_PINNED = """\
<label><strong>Agent Identity (pinned)</strong></label>
<div class="pin"><code>{suggested_agent}</code></div>
<input type="hidden" name="agent" value="{suggested_agent}">
<p class="hint">Public-Host consent is pinned to this identity. Choosing another label is refused.</p>
"""

# Passkey ceremony for the tailnet+passkey approver (task #1322). Kept out of the
# str.format templates so its braces need no escaping; options arrive as JSON.
_PASSKEY_SCRIPT = """<script>
(function () {
  var el = document.getElementById("ua-passkey-options");
  var form = document.getElementById("ua-approve-form");
  if (!el || !form || !window.PublicKeyCredential) return;
  var opts = JSON.parse(el.textContent);
  function dec(s) {
    s = s.replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    var b = atob(s), u = new Uint8Array(b.length);
    for (var i = 0; i < b.length; i++) u[i] = b.charCodeAt(i);
    return u.buffer;
  }
  function enc(buf) {
    var u = new Uint8Array(buf), s = "";
    for (var i = 0; i < u.length; i++) s += String.fromCharCode(u[i]);
    return btoa(s).replace(/\\+/g, "-").replace(/\\//g, "_").replace(/=+$/, "");
  }
  var done = false;
  form.addEventListener("submit", function (ev) {
    if (done) return;
    var sub = ev.submitter;
    if (sub && sub.value === "deny") return;
    if (form.passphrase && form.passphrase.value) return;
    ev.preventDefault();
    navigator.credentials.get({publicKey: {
      challenge: dec(opts.challenge), rpId: opts.rpId, timeout: opts.timeout,
      userVerification: "required",
      allowCredentials: opts.allowCredentials.map(function (c) {
        return {type: "public-key", id: dec(c.id)};
      })
    }}).then(function (cred) {
      form.passkey_credential_id.value = enc(cred.rawId);
      form.passkey_client_data.value = enc(cred.response.clientDataJSON);
      form.passkey_authenticator_data.value = enc(cred.response.authenticatorData);
      form.passkey_signature.value = enc(cred.response.signature);
      done = true;
      form.submit();
    }).catch(function (err) {
      var m = document.getElementById("ua-passkey-status");
      if (m) m.textContent = "Passkey was not used (" + err.name + "). Try again.";
    });
  });
})();
</script>"""


async def _render_approver(request: Request, *, binding: str, next_path: str) -> tuple[str, str]:
    """The approval page's approver section and script for this browser (task #1322)."""
    from . import oauth_approver

    # Live WorkOS membership/session checks are synchronous network calls.
    ctx = await run_in_threadpool(oauth_approver.page_context, request, binding=binding)
    parts: list[str] = ['<div style="margin-top:1.25rem"><strong>Approver</strong></div>']
    script = ""
    if ctx["workos"]:
        if ctx["workos_signed_in"]:
            parts.append(
                '<p class="hint">Signed in with WorkOS. Approving records your verified identity.</p>'
            )
        else:
            href = html.escape(oauth_approver.workos_login_href(next_path), quote=True)
            parts.append(f'<p><a href="{href}">Sign in with WorkOS to approve</a></p>')
    if ctx["passkey_options"] and not ctx["workos_signed_in"]:
        # A WorkOS session already approves (it is checked first); a passkey
        # prompt here would only block that submission if cancelled.
        options = json.dumps(ctx["passkey_options"]).replace("<", "\\u003c")
        login = html.escape(ctx["tailnet_login"] or "")
        parts.append(
            f'<p class="hint">Approving as <strong>{login}</strong> from your own tailnet '
            "device. Your passkey will be requested.</p>"
            '<p class="hint" id="ua-passkey-status" role="status"></p>'
            '<input type="hidden" name="passkey_credential_id">'
            '<input type="hidden" name="passkey_client_data">'
            '<input type="hidden" name="passkey_authenticator_data">'
            '<input type="hidden" name="passkey_signature">'
            f'<script type="application/json" id="ua-passkey-options">{options}</script>'
        )
        script = _PASSKEY_SCRIPT
    elif ctx["tailnet_login"] and not ctx["workos_signed_in"]:
        parts.append(
            f'<p class="hint">No passkey is registered for {html.escape(ctx["tailnet_login"])}. '
            "Register one in the Control Center (Devices) first.</p>"
        )
    if ctx["passphrase"]:
        parts.append(
            '<label for="passphrase"><strong>Hub Passphrase (fallback)</strong></label>'
            '<input id="passphrase" name="passphrase" type="password" '
            'autocomplete="current-password">'
            '<p class="hint">Passphrase approvals are recorded as not human-verified.</p>'
        )
    if len(parts) == 1:
        parts.append(
            '<p class="hint">No verified approval method is available from this browser. '
            "Approve from a device on the approver's tailnet with a registered passkey, "
            "or sign in with the hub's identity provider.</p>"
        )
    return "\n".join(parts), script


_DEVICE_PAGE = (
    _CONSENT_PAGE.split('<div class="card">', 1)[0]
    + """<div class="card">
<h1>Sign in a device</h1>
<div class="banner">
<p>{banner}</p>
</div>
<form method="post" action="/oauth/device" id="ua-approve-form">
<input type="hidden" name="csrf" value="{csrf}">
<label for="user_code"><strong>Code shown on the device</strong></label>
<input id="user_code" name="user_code" value="{user_code}" autocomplete="off"
 autocapitalize="characters" spellcheck="false" required>

{agent_block}

{approver_block}
<p class="hint">Approving signs this device in as the agent above. Only approve a code you started yourself.</p>

<button type="submit" name="action" value="approve">Approve device</button>
<button type="submit" name="action" value="deny" formnovalidate
 style="background:#ffffff;color:#b91c1c;border:1px solid #fecaca">Deny</button>
</form>
</div>
{approver_script}
</body></html>
"""
)


_DEVICE_LOOKUP_PAGE = (
    _CONSENT_PAGE.split('<div class="card">', 1)[0]
    + """<div class="card">
<h1>Sign in a device</h1>
<div class="banner"><p>{note}Enter the code shown on the device you are signing in.</p></div>
<form method="get" action="/oauth/device">
<label for="user_code"><strong>Code</strong></label>
<input id="user_code" name="user_code" value="{user_code}" autocomplete="off"
 autocapitalize="characters" spellcheck="false" required>
<button type="submit">Continue</button>
</form>
</div>
</body></html>
"""
)


def _device_result_page(message: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!DOCTYPE html><html><body><p>" + html.escape(message) + "</p></body></html>",
        status_code=status_code,
    )


def _resolve_suggested_agent(
    client: OAuthClientInformationFull | None,
    params: AuthorizationParams | None = None,
) -> str:
    """Best-effort candidate for the agent identity to present in the consent prompt."""
    if client and client.client_name:
        name = client.client_name.strip()
        canonical = config.canonical_agent_label(name)
        if canonical and config.is_known_agent_label(canonical):
            return canonical
        roster = config.agent_labels()
        if roster and name in roster:
            return name
    fallback = config.mcp_oauth_agent()
    if fallback and config.is_known_agent_label(fallback):
        return fallback
    return config.DEFAULT_MCP_OAUTH_AGENT


def _render_agent_options(selected_agent: str) -> str:
    """Render <option> tags for the agent identity datalist in the consent form."""
    candidates = set(config.fleet_agent_labels())
    custom_roster = config.agent_labels()
    if custom_roster:
        candidates.update(custom_roster)
    if selected_agent:
        candidates.add(selected_agent)
    candidates.add(config.mcp_oauth_agent())
    return "\n".join(
        f'<option value="{html.escape(c, quote=True)}">' for c in sorted(candidates) if c
    )


def _request_host(request: Request) -> str:
    return (request.headers.get("host") or "").lower().split(":", 1)[0]


def _request_is_public_host(request: Request) -> bool:
    # tailscaled's Funnel marker is authoritative whatever Host claims: a
    # public client can send "Host: localhost" because Funnel's TLS selects
    # on SNI (task #1892, round 3). Honoured on every listener — it can
    # only pin, never widen.
    from . import tailnet

    if tailnet.is_funnel_request(request.headers):
        return True
    host = _request_host(request)
    return bool(host) and host in config.http_public_hosts()


def _audit_oauth(channel: str, payload: dict, originator: str | None = None) -> None:
    """Best-effort durable audit; never fail an OAuth grant/revoke on log errors."""
    try:
        from . import events

        events.record_event(channel, payload=payload, originator=originator, notify=False)
    except Exception:
        pass


def _human_fields(human: dict[str, Any] | None) -> dict[str, Any]:
    """Audit-event fields for who approved a sign-in (task #1322).

    ``human.verified`` is true only for a WorkOS or tailnet+passkey approval; a
    passphrase approval, or a code minted with no recorded approver, is false.
    """
    if not human:
        return {"human": {"verified": False, "verified_via": None}}
    return {
        "human": {
            "verified": bool(human.get("verified")),
            "verified_via": human.get("verified_via"),
            "approver": human.get("approver"),
        },
        "approver": human,
    }


_lock = threading.Lock()
_provider: HubOAuthProvider | None = None


class HubOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """OAuth store: clients, refresh tokens, and access tokens are durable
    (``oauth_store``, digests only); authorization codes and pending device
    grants are process-local."""

    def __init__(self) -> None:
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.pending: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams, float]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        # Who approved each pending authorization code (task #1322).
        self.code_approvals: dict[str, dict[str, Any]] = {}
        self.access: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}
        self.access_issued_at: dict[str, int] = {}
        self.client_agents: dict[str, str] = {}
        self.access_refresh: dict[str, str] = {}
        self.refresh_access: dict[str, str] = {}
        # Claims that must survive refresh rotation (device binding, auth method).
        self.refresh_claims: dict[str, dict[str, Any]] = {}
        self.device_grants: dict[str, DeviceGrant] = {}
        self.user_codes: dict[str, str] = {}
        # client_id -> sha256 of the secret, for clients restored from the store.
        self.client_secret_digests: dict[str, str] = {}
        # Refresh tokens whose durable write failed: the only ones rotation may
        # honour from memory alone. Every other rotation needs its durable row.
        self.memory_only_refresh: set[str] = set()
        # access token -> sha256 of the refresh token that minted it (durable link).
        self.access_refresh_digest: dict[str, str] = {}
        # Digests of tokens revoked while the durable delete failed: refused on
        # every durable load so a store hiccup can never undo a revocation.
        self.revoked_digests: set[str] = set()
        # Serializes refresh rotation (consume + reissue) against device revoke,
        # so a revoke can never land between the two and miss the new token.
        self._rotation_lock = threading.RLock()
        self._seed_client_credentials()
        self._seed_cli_client()
        # Config / first-party clients: never persisted, never pruned.
        self.seeded_client_ids: set[str] = set(self.clients)
        self._restore_persisted_clients()

    def _restore_persisted_clients(self) -> None:
        """Reload dynamically registered clients from the durable store."""
        try:
            oauth_store.prune_expired()
            persisted = oauth_store.load_clients()
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            _log.warning("oauth store unavailable; clients not restored: %s", exc)
            return
        for body, secret_digest in persisted:
            client_id = str(body.get("client_id") or "")
            if not client_id or client_id in self.clients:
                # Seeded (config / first-party) clients always win.
                continue
            try:
                client = OAuthClientInformationFull.model_validate(body)
            except Exception:  # noqa: BLE001 - skip a row this build cannot parse
                continue
            self.clients[client_id] = client
            if secret_digest:
                self.client_secret_digests[client_id] = secret_digest

    async def _resolve_cimd_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """A URL client_id: fetch its metadata document (task #1323, J4).

        Never cached into ``self.clients`` (the document is the registration;
        ``oauth_cimd`` holds the TTL cache), never persisted, and always a public
        client. Any failure makes the client unknown, which the SDK reports as
        ``invalid_client`` / ``invalid_request``.
        """
        if not config.mcp_oauth_cimd_enabled():
            return None
        import anyio

        try:
            return await anyio.to_thread.run_sync(oauth_cimd.resolve, client_id)
        except oauth_cimd.CIMDError as exc:
            _audit_oauth(
                "hub.oauth.cimd_rejected",
                {"client_id": client_id[:256], "reason": str(exc)[:200]},
            )
            return None

    def _reload_persisted_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Lazily load one client the startup restore missed (e.g. a locked DB)."""
        try:
            record = oauth_store.load_client(client_id)
        except Exception as exc:  # noqa: BLE001 - the client just stays unknown
            _log.warning("oauth store: client %s reload failed: %s", client_id, exc)
            return None
        if record is None:
            return None
        body, secret_digest = record
        try:
            client = OAuthClientInformationFull.model_validate(body)
        except Exception:  # noqa: BLE001 - a row this build cannot parse
            return None
        with _lock:
            # Seeded clients always win; a concurrent reload keeps the first.
            existing = self.clients.setdefault(client_id, client)
            if existing is client and secret_digest:
                self.client_secret_digests[client_id] = secret_digest
        return existing

    def client_secret_matches(self, client_id: str, presented: str | None) -> bool:
        """Constant-time check of a presented secret against memory or digest."""
        if not presented:
            return False
        with _lock:
            client = self.clients.get(client_id)
            stored_digest = self.client_secret_digests.get(client_id)
        if client is not None and client.client_secret:
            return hmac.compare_digest(client.client_secret.encode(), presented.encode())
        if stored_digest:
            return hmac.compare_digest(stored_digest, oauth_store.digest(presented))
        return False

    def _seed_cli_client(self) -> None:
        if CLI_CLIENT_ID in self.clients:
            # An operator-configured client already owns this id; leave it alone
            # rather than silently replacing its credentials. Device sign-in is
            # then unavailable until the configured client is renamed.
            return
        self.clients[CLI_CLIENT_ID] = OAuthClientInformationFull(
            client_id=CLI_CLIENT_ID,
            client_name="unified-agent CLI",
            client_id_issued_at=int(time.time()),
            redirect_uris=[],
            grant_types=[_DEVICE_CODE_GRANT, "refresh_token"],
            response_types=[],
            token_endpoint_auth_method="none",
            scope=_SCOPE,
        )

    def _seed_client_credentials(self) -> None:
        now = int(time.time())
        for client_id, secret, agent in config.mcp_oauth_client_credentials():
            self.clients[client_id] = OAuthClientInformationFull(
                client_id=client_id,
                client_secret=secret,
                client_id_issued_at=now,
                client_secret_expires_at=0,
                client_name=agent,
                redirect_uris=[],
                grant_types=["client_credentials", "refresh_token"],
                response_types=[],
                token_endpoint_auth_method="client_secret_post",
                scope=_SCOPE,
            )
            self.client_agents[client_id] = agent

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with _lock:
            client = self.clients.get(client_id)
        if client is None:
            # A stored registration wins over CIMD, even for a URL-shaped id.
            client = self._reload_persisted_client(client_id)
        if client is None and oauth_cimd.looks_like_cimd(client_id):
            return await self._resolve_cimd_client(client_id)
        with _lock:
            restored = client is not None and not client.client_secret
            restored = restored and client_id in self.client_secret_digests
        if client is None or not restored:
            return client
        # Restored confidential client: only a verified presented secret is
        # handed to the SDK authenticator. Anything else gets an unguessable
        # stand-in, so the authenticator's own comparison fails whatever its
        # handling of a missing secret (never secretless = public).
        presented = _presented_client_secret.get()
        if self.client_secret_matches(client_id, presented):
            return client.model_copy(update={"client_secret": presented})
        return client.model_copy(update={"client_secret": secrets.token_urlsafe(32)})

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id == CLI_CLIENT_ID or client_info.client_id in self.client_agents:
            from mcp.server.auth.provider import RegistrationError

            raise RegistrationError("invalid_client_metadata", "client_id is reserved")
        with _lock:
            self.clients[client_info.client_id] = client_info
        try:
            stored = oauth_store.save_client(
                client_info.client_id,
                client_info.model_dump(mode="json", exclude_none=True),
                client_info.client_secret,
            )
        except Exception as exc:  # noqa: BLE001 - reported as a registration error
            _log.warning("oauth store: client %s not persisted: %s", client_info.client_id, exc)
            oauth_store.note_write_failure("client")
            stored = False
        if not stored:
            # A registration that would vanish on restart is refused, not
            # handed out: the client retries once the registry is healthy.
            with _lock:
                self.clients.pop(client_info.client_id, None)
            _audit_oauth(
                "hub.oauth.store_skipped",
                {"client_id": client_info.client_id, "reason": "client not persisted"},
            )
            from mcp.server.auth.provider import RegistrationError

            raise RegistrationError(
                "invalid_client_metadata", "the hub client registry is unavailable; retry later"
            )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        rid = secrets.token_urlsafe(24)
        with _lock:
            self.pending[rid] = (client, params, time.time() + _CONSENT_TTL_S)
        return f"{config.mcp_oauth_issuer()}/oauth/consent?rid={rid}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with _lock:
            code = self.codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        if code.expires_at < time.time():
            with _lock:
                self.codes.pop(authorization_code, None)
                self.code_approvals.pop(authorization_code, None)
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with _lock:
            stored = self.codes.pop(authorization_code.code, None)
            approval = self.code_approvals.pop(authorization_code.code, None)
        if stored is None or stored.client_id != client.client_id:
            raise TokenError("invalid_grant", "authorization code is not valid")
        return self._issue(
            client,
            stored.scopes,
            stored.resource,
            subject=stored.subject,
            auth_method="authorization_code",
            extra_claims={"human_approval": approval} if approval else None,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        with _lock:
            token = self.refresh.get(refresh_token)
        if token is None:
            token = self._load_persisted_refresh(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        if token.expires_at is not None and token.expires_at < int(time.time()):
            return None
        return token

    def _load_persisted_refresh(self, refresh_token: str) -> RefreshToken | None:
        """A refresh token issued before this process started, from the store."""
        if oauth_store.digest(refresh_token) in self.revoked_digests:
            return None
        try:
            record = oauth_store.load_refresh(refresh_token)
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            _log.warning("oauth store: refresh lookup failed: %s", exc)
            return None
        if record is None:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record["client_id"],
            scopes=list(record["scopes"]),
            expires_at=record["expires_at"],
            resource=record["resource"],
            subject=record["subject"],
        )

    def _consume_persisted_refresh(
        self, refresh_token: str, client_id: str
    ) -> dict[str, Any] | None | object:
        """The consumed durable row, None when absent (or not this client's),
        or ``_STORE_DOWN``."""
        try:
            return oauth_store.consume_refresh(refresh_token, client_id=client_id)
        except Exception as exc:  # noqa: BLE001 - caller decides on availability
            _log.warning("oauth store: refresh consume failed: %s", exc)
            return _STORE_DOWN

    def _rotate_persisted_refresh(
        self, refresh_token: str, client_id: str, new_token: str, scopes: list[str]
    ) -> dict[str, Any] | None | object:
        """Consume the durable row and store its successor in one transaction.

        The consumed record (with ``rotated``), None when absent (or not this
        client's), or ``_STORE_DOWN``. One write, so the sign-in family is never
        without a row (task #1744 review).
        """
        try:
            return oauth_store.rotate_refresh(
                refresh_token,
                client_id=client_id,
                new_token=new_token,
                scopes=scopes or None,
                expires_at=int(time.time()) + _REFRESH_TTL_S,
            )
        except Exception as exc:  # noqa: BLE001 - caller decides on availability
            _log.warning("oauth store: refresh rotation failed: %s", exc)
            return _STORE_DOWN

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        with self._rotation_lock:
            with _lock:
                held = self.refresh.get(refresh_token.token)
                if held is not None and held.client_id != client.client_id:
                    # Another client presenting this token must not destroy the
                    # owner's session: refuse without consuming anything.
                    raise TokenError("invalid_grant", "refresh token is not valid")
                stored = self.refresh.pop(refresh_token.token, None)
                carried = self.refresh_claims.pop(refresh_token.token, None) or {}
                old_access = self.refresh_access.pop(refresh_token.token, None)
                if old_access:
                    self.access.pop(old_access, None)
                    self.access_issued_at.pop(old_access, None)
                    self.access_refresh.pop(old_access, None)
                memory_only = refresh_token.token in self.memory_only_refresh
                self.memory_only_refresh.discard(refresh_token.token)
            # The durable row is authoritative: a token revoked or rotated
            # elsewhere is gone from the store even if this process still holds
            # it. Only a token whose durable write failed may rotate from memory,
            # and a store that is down right now falls back to memory.
            successor = secrets.token_urlsafe(32)
            if oauth_store.digest(refresh_token.token) in self.revoked_digests:
                # Revoked here: consume whatever is left, never write a successor.
                self._consume_persisted_refresh(refresh_token.token, client.client_id)
                persisted = None
            else:
                persisted = self._rotate_persisted_refresh(
                    refresh_token.token, client.client_id, successor, scopes
                )
            self._drop_memory_access_for_refresh(oauth_store.digest(refresh_token.token))
            if oauth_store.digest(refresh_token.token) in self.revoked_digests:
                if isinstance(persisted, dict) and persisted.get("rotated"):
                    # Revoked while rotating: the successor must not outlive it.
                    oauth_store.delete_refresh(successor)
                stored, persisted = None, None
                memory_only = False
            if isinstance(persisted, dict):
                stored = RefreshToken(
                    token=refresh_token.token,
                    client_id=persisted["client_id"],
                    scopes=list(persisted["scopes"]),
                    expires_at=persisted["expires_at"],
                    resource=persisted["resource"],
                    subject=persisted["subject"],
                )
                carried = dict(persisted["claims"] or {})
            elif persisted is None and not memory_only:
                stored = None
            rotated = isinstance(persisted, dict) and bool(persisted.get("rotated"))
            try:
                if stored is None or stored.client_id != client.client_id:
                    raise TokenError("invalid_grant", "refresh token is not valid")
                # A committed rotation already found the row live; checking again
                # here could only refuse it because the clock ticked (task #1893).
                if (
                    not rotated
                    and stored.expires_at is not None
                    and stored.expires_at < int(time.time())
                ):
                    raise TokenError("invalid_grant", "refresh token expired")
                granted = scopes or stored.scopes
                return self._issue(
                    client,
                    granted,
                    stored.resource,
                    subject=stored.subject,
                    auth_method=carried.get("auth_method") or "refresh_token",
                    extra_claims={k: v for k, v in carried.items() if k != "auth_method"},
                    # The durable successor is already written (atomic rotation);
                    # a memory-only or store-down rotation writes a fresh one.
                    refresh_value=successor if rotated else None,
                )
            except BaseException:
                if rotated:
                    # The client never receives the successor: drop its row so
                    # the family is not left with an orphan (task #1893).
                    try:
                        oauth_store.delete_refresh(successor)
                    except Exception as exc:  # noqa: BLE001 - the original error wins
                        _log.warning("oauth store: orphan successor not deleted: %s", exc)
                raise

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self._lookup_access(token)

    def _lookup_access(self, token: str) -> AccessToken | None:
        """A live access token from memory, else from the durable store.

        Access tokens survive a hub restart like refresh tokens do: a client
        holding one keeps working instead of getting 401 until it expires.
        """
        with _lock:
            access = self.access.get(token)
        if access is None:
            access = self._load_persisted_access(token)
        if access is None:
            return None
        if access.expires_at is not None and access.expires_at < int(time.time()):
            with _lock:
                self.access.pop(token, None)
                self.access_issued_at.pop(token, None)
                linked = self.access_refresh.pop(token, None)
                if linked:
                    self.refresh_access.pop(linked, None)
            try:
                oauth_store.delete_access(token)
            except Exception:  # noqa: BLE001 - expired either way
                pass
            return None
        return access

    def _load_persisted_access(self, token: str) -> AccessToken | None:
        # Read and cache under the rotation lock that revoke also holds, so a
        # revoke can never land between the durable read and the cache insert
        # and leave a revoked token cached.
        with self._rotation_lock:
            return self._load_persisted_access_locked(token)

    def _load_persisted_access_locked(self, token: str) -> AccessToken | None:
        if oauth_store.digest(token) in self.revoked_digests:
            return None
        try:
            record = oauth_store.load_access(token)
        except Exception as exc:  # noqa: BLE001 - treated as unknown token
            _log.warning("oauth store: access lookup failed: %s", exc)
            return None
        if record is None or record.get("refresh_sha256") in self.revoked_digests:
            return None
        access = AccessToken(
            token=token,
            client_id=record["client_id"],
            scopes=list(record["scopes"]),
            expires_at=record["expires_at"],
            resource=record["resource"],
            subject=record["subject"],
            claims=record["claims"] or {},
        )
        with _lock:
            self.access.setdefault(token, access)
            self.access_issued_at.setdefault(token, int(record["issued_at"]))
            if record.get("refresh_sha256"):
                self.access_refresh_digest.setdefault(token, record["refresh_sha256"])
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        token_value = getattr(token, "token", "")
        refresh_value: str | None = None
        with _lock:
            if isinstance(token, AccessToken):
                refresh_value = self.access_refresh.pop(token_value, None)
                self.access.pop(token_value, None)
                self.access_issued_at.pop(token_value, None)
                if refresh_value:
                    self.refresh.pop(refresh_value, None)
                    self.refresh_access.pop(refresh_value, None)
                    self.refresh_claims.pop(refresh_value, None)
            else:
                refresh_value = token_value
                access_value = self.refresh_access.pop(token_value, None)
                self.refresh.pop(token_value, None)
                self.refresh_claims.pop(token_value, None)
                if access_value:
                    self.access.pop(access_value, None)
                    self.access_issued_at.pop(access_value, None)
                    self.access_refresh.pop(access_value, None)
        durable = self._delete_durably(token, token_value, refresh_value)
        if isinstance(token, RefreshToken):
            self._drop_memory_access_for_refresh(oauth_store.digest(token_value))
        try:
            events.record_event(
                "oauth.token_revoked",
                payload={
                    "client_id": token.client_id,
                    "subject": token.subject,
                    "token_type": type(token).__name__,
                    "durable": durable,
                },
                originator=token.subject,
            )
        except Exception:
            # Revocation is already complete; an unavailable audit sink must not
            # make the caller retry a destructive credential operation.
            pass

    def _delete_durably(
        self, token: AccessToken | RefreshToken, token_value: str, refresh_value: str | None
    ) -> bool:
        """Delete a revoked token's durable rows; fail closed if the store errs.

        Tries twice. If the store still fails, the token (and its linked
        refresh) go on this process's deny list, which every durable load
        consults, and the doctor reads DEGRADED. Returns whether it was durable.
        """
        for attempt in (1, 2):
            try:
                if isinstance(token, AccessToken):
                    # The durable row knows its refresh even when this process
                    # does not (a token restored after a restart): cascade by digest.
                    refresh_digest = oauth_store.delete_access(token_value)
                    if refresh_value:
                        oauth_store.delete_refresh(refresh_value)
                    elif refresh_digest:
                        oauth_store.delete_refresh_digest(refresh_digest)
                elif refresh_value:
                    oauth_store.delete_refresh(refresh_value)
                return True
            except Exception as exc:  # noqa: BLE001 - handled below
                _log.warning("oauth store: revoke delete failed (attempt %d): %s", attempt, exc)
        oauth_store.note_write_failure("revoke")
        with _lock:
            self.revoked_digests.add(oauth_store.digest(token_value))
            if refresh_value:
                self.revoked_digests.add(oauth_store.digest(refresh_value))
            linked = self.access_refresh_digest.get(token_value)
            if linked:
                self.revoked_digests.add(linked)
        return False

    def _drop_memory_access_for_refresh(self, refresh_digest: str) -> None:
        """Evict cached access tokens minted from the refresh with this digest.

        Covers tokens restored from the store after a restart, which have no
        in-memory link to their (plaintext) refresh token.
        """
        with _lock:
            for access_value, linked in list(self.access_refresh_digest.items()):
                if linked == refresh_digest:
                    self.access.pop(access_value, None)
                    self.access_issued_at.pop(access_value, None)
                    self.access_refresh.pop(access_value, None)
                    self.access_refresh_digest.pop(access_value, None)

    def complete_consent(
        self,
        rid: str,
        agent: str | None = None,
        *,
        pinned_agent: str | None = None,
        approval: Any = None,
    ) -> str:
        """Mint an authorization code after the approver check; return client redirect.

        When ``pinned_agent`` is set (public-Host path), the issued subject is
        forced to that identity. A non-matching ``agent`` is refused, and unknown
        agent identities fail closed. ``approval`` (an ``oauth_approver.Approval``)
        is recorded on the consent event and rides into the token's claims.
        """
        now = time.time()
        with _lock:
            pending = self.pending.pop(rid, None)
        if pending is None:
            raise AuthorizeError("invalid_request", "unknown or expired consent")
        client, params, expires_at = pending
        if expires_at < now:
            raise AuthorizeError("access_denied", "consent expired")

        if pinned_agent is not None and agent not in (None, pinned_agent):
            raise AuthorizeError(
                "access_denied", "this OAuth endpoint is pinned to its configured agent"
            )
        target_agent = pinned_agent or agent
        if not target_agent:
            target_agent = _resolve_suggested_agent(client, params)
        if not config.is_known_agent_label(target_agent):
            raise AuthorizeError("invalid_request", "agent identity is not registered")
        target_agent = config.canonical_identity(target_agent)
        refused = _retired_subject(target_agent)
        if refused is not None:
            raise AuthorizeError("access_denied", refused)

        code = secrets.token_urlsafe(32)
        record = AuthorizationCode(
            code=code,
            scopes=params.scopes or [_SCOPE],
            expires_at=now + _CODE_TTL_S,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=target_agent,
        )
        human = approval.claims() if approval is not None else None
        with _lock:
            self.codes[code] = record
            if human is not None:
                self.code_approvals[code] = human
        _audit_oauth(
            "hub.oauth.consent",
            {
                "client_id": client.client_id,
                "subject": target_agent,
                "pinned": pinned_agent is not None,
                **_human_fields(human),
            },
            originator=target_agent,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    # -- RFC 8628 device authorization grant (task #1320) ---------------------

    def start_device_authorization(
        self,
        client_id: str,
        scopes: list[str] | None,
        resource: str | None,
        device_node: dict[str, Any] | None,
        agent_hint: str | None = None,
    ) -> dict[str, Any]:
        """Open a pending device grant. The requesting peer's node is bound here."""
        with _lock:
            client = self.clients.get(client_id)
        if client is None or _DEVICE_CODE_GRANT not in (client.grant_types or []):
            raise DeviceGrantError("invalid_client", "client may not use the device grant", 401)
        granted = scopes or [_SCOPE]
        if granted != [_SCOPE]:
            raise DeviceGrantError("invalid_scope", "requested scope is not allowed")
        now = _now()
        binding = _device_binding(device_node)
        with _lock:
            self._expire_device_grants(now)
            pending = sum(1 for g in self.device_grants.values() if g.status == "pending")
            if pending >= _DEVICE_MAX_PENDING:
                raise DeviceGrantError(
                    "temporarily_unavailable",
                    "too many pending device grants; try again later",
                    503,
                )
            user_code = _new_user_code()
            while _normalize_user_code(user_code) in self.user_codes:
                user_code = _new_user_code()
            device_code = secrets.token_urlsafe(32)
            self.device_grants[device_code] = DeviceGrant(
                device_code=device_code,
                user_code=user_code,
                client_id=client_id,
                scopes=granted,
                resource=resource,
                expires_at=now + _DEVICE_TTL_S,
                interval=_DEVICE_INTERVAL_S,
                device_node=binding,
                agent_hint=agent_hint,
            )
            self.user_codes[_normalize_user_code(user_code)] = device_code
        _audit_oauth(
            "hub.oauth.device_authorization",
            {"client_id": client_id, "device_node": binding},
        )
        issuer = config.mcp_oauth_issuer() or ""
        verification_uri = f"{issuer}/oauth/device"
        return {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "verification_uri_complete": f"{verification_uri}?user_code={quote(user_code)}",
            "expires_in": _DEVICE_TTL_S,
            "interval": _DEVICE_INTERVAL_S,
        }

    def _expire_device_grants(self, now: float) -> None:
        """Drop grants that expired well past their TTL (caller holds the lock).

        Recently expired grants are kept for one more TTL so a late poll still
        gets ``expired_token`` instead of an ambiguous ``invalid_grant``.
        """
        stale = [
            code for code, g in self.device_grants.items() if g.expires_at + _DEVICE_TTL_S < now
        ]
        for code in stale:
            grant = self.device_grants.pop(code)
            self.user_codes.pop(_normalize_user_code(grant.user_code), None)

    def pending_device_grant(self, user_code: str) -> DeviceGrant | None:
        """The live pending grant for a user code, or None."""
        with _lock:
            device_code = self.user_codes.get(_normalize_user_code(user_code))
            grant = self.device_grants.get(device_code) if device_code else None
        if grant is None or grant.status != "pending" or grant.expires_at < _now():
            return None
        return grant

    def approve_device(
        self,
        user_code: str,
        agent: str | None = None,
        *,
        pinned_agent: str | None = None,
        approver_node: dict[str, Any] | None = None,
        approval: Any = None,
    ) -> str:
        """Approve a pending device grant as ``agent``; return the issued subject.

        ``approval`` (an ``oauth_approver.Approval``) is recorded on the event and
        carried into the issued token's claims as ``human_approval``.
        """
        grant = self.pending_device_grant(user_code)
        if grant is None:
            raise AuthorizeError("invalid_request", "unknown or expired device code")
        if pinned_agent is not None and agent not in (None, pinned_agent):
            raise AuthorizeError(
                "access_denied", "this OAuth endpoint is pinned to its configured agent"
            )
        target_agent = pinned_agent or agent or config.mcp_oauth_agent()
        if not config.is_known_agent_label(target_agent):
            raise AuthorizeError("invalid_request", "agent identity is not registered")
        target_agent = config.canonical_identity(target_agent)
        refused = _retired_subject(target_agent)
        if refused is not None:
            raise AuthorizeError("access_denied", refused)
        with _lock:
            if grant.status != "pending":
                raise AuthorizeError("invalid_request", "device code already resolved")
            grant.status = "approved"
            grant.subject = target_agent
            grant.human_approval = approval.claims() if approval is not None else None
        _audit_oauth(
            "hub.oauth.device_approved",
            {
                "client_id": grant.client_id,
                "subject": target_agent,
                "pinned": pinned_agent is not None,
                "device_node": grant.device_node,
                "approver_node": _device_binding(approver_node),
                **_human_fields(grant.human_approval),
            },
            originator=target_agent,
        )
        return target_agent

    def deny_device(self, user_code: str, *, approver_node: dict[str, Any] | None = None) -> bool:
        grant = self.pending_device_grant(user_code)
        if grant is None:
            return False
        with _lock:
            if grant.status != "pending":
                # Approval (or another denial) already won the race.
                return False
            grant.status = "denied"
        _audit_oauth(
            "hub.oauth.device_denied",
            {
                "client_id": grant.client_id,
                "device_node": grant.device_node,
                "approver_node": _device_binding(approver_node),
            },
        )
        return True

    def poll_device(self, client_id: str, device_code: str) -> OAuthToken:
        """RFC 8628 §3.4/3.5 token request for a device code."""
        now = _now()
        with _lock:
            grant = self.device_grants.get(device_code)
            if grant is None:
                raise DeviceGrantError("invalid_grant", "device code is not valid")
            if grant.client_id != client_id:
                raise DeviceGrantError("invalid_grant", "device code was issued to another client")
            if grant.expires_at < now:
                raise DeviceGrantError("expired_token", "device code expired")
            if grant.status == "denied":
                self.device_grants.pop(device_code, None)
                self.user_codes.pop(_normalize_user_code(grant.user_code), None)
                raise DeviceGrantError("access_denied", "the device request was denied")
            if grant.status == "pending":
                too_fast = grant.last_poll is not None and now - grant.last_poll < grant.interval
                grant.last_poll = now
                if too_fast:
                    grant.interval += _DEVICE_SLOW_DOWN_S
                    raise DeviceGrantError("slow_down", "polling too fast")
                raise DeviceGrantError("authorization_pending", "waiting for approval")
            # approved: single use
            self.device_grants.pop(device_code, None)
            self.user_codes.pop(_normalize_user_code(grant.user_code), None)
            client = self.clients.get(client_id)
        if client is None:
            raise DeviceGrantError("invalid_client", "unknown client", 401)
        refused = _retired_subject(grant.subject)
        if refused is not None:
            # The grant is already spent above: an approval made before the
            # retirement yields nothing, now or after an unretire.
            raise DeviceGrantError("access_denied", refused)
        extra: dict[str, Any] = {"device_node": grant.device_node} if grant.device_node else {}
        # One id per device sign-in, carried through every refresh rotation:
        # session-scoped MCP credentials bind to it (task #1744).
        extra["signin_id"] = secrets.token_hex(16)
        if grant.human_approval:
            extra["human_approval"] = grant.human_approval
        return self._issue(
            client,
            grant.scopes,
            grant.resource,
            subject=grant.subject,
            auth_method="device_code",
            extra_claims=extra,
        )

    def exchange_client_credentials(
        self, client_id: str, client_secret: str, scopes: list[str] | None = None
    ) -> OAuthToken:
        """Non-interactive grant for pre-provisioned confidential cloud-bot clients."""
        with _lock:
            client = self.clients.get(client_id)
            agent = self.client_agents.get(client_id)
        if client is None or agent is None or not client.client_secret:
            raise TokenError("invalid_client", "unknown client")
        if not hmac.compare_digest(client.client_secret, client_secret):
            raise TokenError("invalid_client", "client authentication failed")
        if "client_credentials" not in (client.grant_types or []):
            raise TokenError("unauthorized_client", "client_credentials not allowed")
        granted = scopes or ([_SCOPE] if not client.scope else client.scope.split())
        return self._issue(client, granted, None, subject=agent, grant="client_credentials")

    def whoami(self, access_token: str) -> dict | None:
        """Resolve a live access token into a verification payload."""
        access = self._lookup_access(access_token)
        with _lock:
            issued_at = self.access_issued_at.get(access_token)
        if access is None:
            return None
        now = int(time.time())
        if access.expires_at is not None and access.expires_at < now:
            return None
        subject = access.subject or config.mcp_oauth_agent()
        from . import profiles

        ttl = config.mcp_oauth_ttl_seconds()
        age = (
            (now - issued_at)
            if issued_at is not None
            else max(0, ttl - max(0, (access.expires_at or now) - now))
        )
        expires_in = max(0, (access.expires_at or now) - now) if access.expires_at else None
        claims = access.claims or {}
        return {
            "agent": subject,
            "profile": profiles.default_for_identity(subject),
            "scopes": list(access.scopes or []),
            "token_age_s": age,
            "expires_in": expires_in,
            "via": "oauth",
            "client_id": access.client_id,
            "auth_method": claims.get("auth_method"),
            "device_node": claims.get("device_node"),
            "human_approval": claims.get("human_approval"),
        }

    def _issue(
        self,
        client: OAuthClientInformationFull,
        scopes: list[str],
        resource: str | None,
        subject: str | None = None,
        *,
        grant: str = "authorization_code",
        auth_method: str | None = None,
        include_refresh: bool = True,
        extra_claims: dict[str, Any] | None = None,
        refresh_value: str | None = None,
    ) -> OAuthToken:
        """``refresh_value``: a refresh token whose durable row is already
        written (``oauth_store.rotate_refresh``); it is not saved again."""
        now = int(time.time())
        preset_refresh = refresh_value
        ttl = config.mcp_oauth_ttl_seconds()
        access_value = secrets.token_urlsafe(32)
        target_subject = subject or config.mcp_oauth_agent()
        # Every grant (code exchange, refresh rotation, client credentials,
        # device poll) issues through here: a retired label gets nothing.
        refused = _retired_subject(target_subject)
        if refused is not None:
            raise TokenError("invalid_grant", refused)
        access = AccessToken(
            token=access_value,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=now + ttl,
            resource=resource,
            subject=target_subject,
            claims={**(extra_claims or {}), "auth_method": auth_method or grant, "iat": now},
        )
        effective_auth_method = auth_method or grant
        # client_credentials may still receive a refresh token so bots can rotate
        # without re-presenting the long-lived secret every TTL; revoke cascades.
        with _lock:
            self.access[access_value] = access
            refresh_value = None
            if include_refresh:
                refresh_value = preset_refresh or secrets.token_urlsafe(32)
                self.refresh[refresh_value] = RefreshToken(
                    token=refresh_value,
                    client_id=client.client_id,
                    scopes=scopes,
                    expires_at=now + _REFRESH_TTL_S,
                    resource=resource,
                    subject=target_subject,
                )
                self.access_refresh[access_value] = refresh_value
                self.refresh_access[refresh_value] = access_value
                self.refresh_claims[refresh_value] = {
                    **(extra_claims or {}),
                    "auth_method": effective_auth_method,
                }
            self.access_issued_at[access_value] = now
        if refresh_value and refresh_value != preset_refresh:
            try:
                oauth_store.save_refresh(
                    refresh_value,
                    client_id=client.client_id,
                    subject=target_subject,
                    scopes=list(scopes),
                    resource=resource,
                    expires_at=now + _REFRESH_TTL_S,
                    claims={**(extra_claims or {}), "auth_method": effective_auth_method},
                )
            except Exception as exc:  # noqa: BLE001 - token still valid in memory
                _log.warning("oauth store: refresh token not persisted: %s", exc)
                oauth_store.note_write_failure("refresh_token")
                with _lock:
                    self.memory_only_refresh.add(refresh_value)
        if refresh_value:
            with _lock:
                self.access_refresh_digest[access_value] = oauth_store.digest(refresh_value)
                refresh_durable = refresh_value not in self.memory_only_refresh
        else:
            refresh_durable = True
        self._ensure_client_persisted(client)
        # An access token is persisted only alongside a durable session: one
        # whose refresh write failed stays in memory, so it can never outlive a
        # restart with no session row to list or revoke.
        if refresh_durable:
            try:
                oauth_store.save_access(
                    access_value,
                    refresh_token=refresh_value,
                    client_id=client.client_id,
                    subject=target_subject,
                    scopes=list(scopes),
                    resource=resource,
                    expires_at=now + ttl,
                    claims=access.claims or {},
                    issued_at=now,
                )
            except Exception as exc:  # noqa: BLE001 - token still valid in memory
                _log.warning("oauth store: access token not persisted: %s", exc)
                oauth_store.note_write_failure("access_token")
        _audit_oauth(
            "hub.oauth.token_issue",
            {
                "client_id": client.client_id,
                "subject": target_subject,
                "grant": effective_auth_method,
                "scopes": list(scopes),
                **_human_fields((extra_claims or {}).get("human_approval")),
            },
            originator=target_subject,
        )
        return OAuthToken(
            access_token=access_value,
            token_type="Bearer",
            expires_in=ttl,
            scope=" ".join(scopes),
            refresh_token=refresh_value,
        )

    def _ensure_client_persisted(self, client: OAuthClientInformationFull) -> None:
        """Re-save a dynamic client pruned from the store while still in memory.

        Pruning (startup, or at the registration cap) can remove an idle client
        this process still holds; a token issued to it now must not become
        unusable after the next restart.
        """
        client_id = client.client_id
        with _lock:
            dynamic = client_id in self.clients and client_id not in self.seeded_client_ids
            held = self.clients.get(client_id)
            digest = self.client_secret_digests.get(client_id)
        if not dynamic or held is None:
            return
        try:
            oauth_store.ensure_client(
                client_id,
                held.model_dump(mode="json", exclude_none=True),
                secret=held.client_secret,
                secret_digest=digest,
            )
        except Exception as exc:  # noqa: BLE001 - reported via the doctor
            _log.warning("oauth store: client %s not re-saved: %s", client_id, exc)
            oauth_store.note_write_failure("client")

    def revoke_sessions(
        self,
        *,
        device: str | None = None,
        session_id: str | None = None,
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Revoke every refresh token bound to a device, or one session (J2).

        Drops the durable rows, the matching in-memory refresh tokens, and every
        live access token minted from them or bound to the device, then writes
        one ``hub.oauth.sessions_revoked`` event. Returns the revoked sessions
        (no token values).
        """
        with self._rotation_lock:
            return self._revoke_sessions_locked(device, session_id, actor)

    def _revoke_sessions_locked(
        self, device: str | None, session_id: str | None, actor: str | None
    ) -> list[dict[str, Any]]:
        ids, names = oauth_store.device_scope(device) if device else (set(), set())
        revoked = oauth_store.revoke_sessions(device=device, session_id=session_id)
        digests = {row["token_sha256"] for row in revoked}

        def _on_device(claims: dict[str, Any] | None) -> bool:
            return bool(device) and oauth_store.on_device(
                ids, names, (claims or {}).get("device_node")
            )

        with _lock:
            for access_value, linked in list(self.access_refresh_digest.items()):
                if linked in digests:
                    self.access.pop(access_value, None)
                    self.access_issued_at.pop(access_value, None)
                    self.access_refresh.pop(access_value, None)
                    self.access_refresh_digest.pop(access_value, None)
            doomed = [
                r
                for r in self.refresh
                if oauth_store.digest(r) in digests or _on_device(self.refresh_claims.get(r))
            ]
            for refresh_value in doomed:
                self.refresh.pop(refresh_value, None)
                self.refresh_claims.pop(refresh_value, None)
                self.memory_only_refresh.discard(refresh_value)
                access_value = self.refresh_access.pop(refresh_value, None)
                if access_value:
                    self.access.pop(access_value, None)
                    self.access_issued_at.pop(access_value, None)
                    self.access_refresh.pop(access_value, None)
            if device:
                for access_value, access in list(self.access.items()):
                    if _on_device(access.claims):
                        self.access.pop(access_value, None)
                        self.access_issued_at.pop(access_value, None)
                        linked = self.access_refresh.pop(access_value, None)
                        if linked:
                            self.refresh_access.pop(linked, None)
        public = [{k: v for k, v in row.items() if k != "token_sha256"} for row in revoked]
        _audit_oauth(
            "hub.oauth.sessions_revoked",
            {
                "device": device,
                "session_id": session_id,
                "revoked": len(public),
                "agents": sorted({str(r["agent"]) for r in public if r.get("agent")}),
                "actor": actor,
            },
            originator=actor,
        )
        return public

    def forget_subject(self, subject: str, digests: set[str]) -> int:
        """Drop every in-memory token, device grant and code of ``subject`` after a retire.

        ``digests`` are the deleted rows' token digests; they join
        ``revoked_digests`` so a durable reload can never bring one back. The
        caller holds ``_rotation_lock`` (task #2315). Returns the number of
        in-memory tokens dropped.
        """
        dropped = 0
        folded = config.identity_fold(subject)

        def mine(value: str | None) -> bool:
            return bool(value) and config.identity_fold(value) == folded

        with self._rotation_lock, _lock:
            self.revoked_digests |= set(digests)
            # Outstanding approvals: an approved device code or an unexchanged
            # authorization code for this label yields no token afterwards.
            for device_code, grant in list(self.device_grants.items()):
                if mine(grant.subject) or (grant.status == "pending" and mine(grant.agent_hint)):
                    self.device_grants.pop(device_code, None)
                    self.user_codes.pop(_normalize_user_code(grant.user_code), None)
                    dropped += 1
            for code, record in list(self.codes.items()):
                if mine(record.subject):
                    self.codes.pop(code, None)
                    self.code_approvals.pop(code, None)
                    dropped += 1
            for access_value, access in list(self.access.items()):
                if mine(access.subject):
                    self.access.pop(access_value, None)
                    self.access_issued_at.pop(access_value, None)
                    self.access_refresh_digest.pop(access_value, None)
                    linked = self.access_refresh.pop(access_value, None)
                    if linked:
                        self.refresh_access.pop(linked, None)
                    dropped += 1
            for refresh_value, refresh in list(self.refresh.items()):
                if mine(refresh.subject):
                    self.refresh.pop(refresh_value, None)
                    self.refresh_claims.pop(refresh_value, None)
                    self.memory_only_refresh.discard(refresh_value)
                    access_value = self.refresh_access.pop(refresh_value, None)
                    if access_value:
                        self.access.pop(access_value, None)
                        self.access_issued_at.pop(access_value, None)
                        self.access_refresh.pop(access_value, None)
                        self.access_refresh_digest.pop(access_value, None)
                    dropped += 1
        return dropped

    def issue_client_credentials(
        self, client_id: str, agent: str, resource: str | None = None
    ) -> OAuthToken:
        """Issue a non-refreshable token for a config-pinned confidential client."""
        client = OAuthClientInformationFull(
            client_id=client_id,
            client_name=agent,
            client_secret="configured",
            token_endpoint_auth_method="client_secret_basic",
            grant_types=[_CLIENT_CREDENTIALS_GRANT],
            response_types=[],
        )
        return self._issue(
            client,
            [_SCOPE],
            resource,
            subject=agent,
            auth_method=_CLIENT_CREDENTIALS_GRANT,
            include_refresh=False,
        )


def get_provider() -> HubOAuthProvider | None:
    """Return the process provider when OAuth is fully configured; else None."""
    global _provider
    if not config.mcp_oauth_enabled():
        return None
    if _provider is None:
        _provider = HubOAuthProvider()
    return _provider


def reset_provider() -> None:
    """Test hook: drop the process-wide store."""
    global _provider
    with _lock:
        _provider = None


# Code below the adapter layer (connector_oauth, tools.oauth_devices) asks
# identity_core for the provider (task #1721). The lambda reads get_provider at
# call time, so a patch of oauth.get_provider reaches those callers too.
identity_core.register_oauth_provider_lookup(lambda: get_provider())


def is_oauth_public_path(path: str) -> bool:
    """True for authorization-server endpoints that must stay anonymous."""
    if path in _OAUTH_PUBLIC_PATHS:
        return True
    return any(path == p or path.startswith(p + "/") for p in _OAUTH_PUBLIC_PREFIXES)


def is_oauth_slice_path(path: str) -> bool:
    """True for MCP/OAuth paths a ``UA_HTTP_PUBLIC_HOSTS`` Host may reach."""
    if is_oauth_public_path(path):
        return True
    return path in _OAUTH_SLICE_AUTH_PATHS


def www_authenticate() -> str | None:
    """RFC 9728 challenge so MCP clients can discover this authorization server."""
    if get_provider() is None:
        return None
    issuer = config.mcp_oauth_issuer()
    if not issuer:
        return None
    resource = AnyHttpUrl(f"{issuer}/mcp")
    from mcp.server.auth.routes import build_resource_metadata_url

    metadata = build_resource_metadata_url(resource)
    return f'Bearer realm="unified-agent", resource_metadata="{metadata}"'


def http_routes() -> list[Route]:
    """Starlette routes to mount on the hub app. Empty when OAuth is off."""
    provider = get_provider()
    issuer = config.mcp_oauth_issuer()
    if provider is None or not issuer:
        return []
    issuer_url = AnyHttpUrl(issuer)
    resource_url = AnyHttpUrl(f"{issuer}/mcp")
    routes = create_auth_routes(
        provider,
        issuer_url=issuer_url,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[_SCOPE],
            default_scopes=[_SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    for index, route in enumerate(routes):
        if route.path != "/token":
            continue
        default_token_endpoint = route.endpoint
        default_token_handler = default_token_endpoint.app.app.__closure__[0].cell_contents

        async def token_endpoint(
            request: Request, _default_token_handler=default_token_handler
        ) -> Response:
            if request.method == "OPTIONS":
                return Response(status_code=204)
            await request.body()
            form = await request.form()
            if str(form.get("grant_type") or "") == _DEVICE_CODE_GRANT:
                return _device_token_response(provider, request, form)
            if str(form.get("grant_type") or "") != _CLIENT_CREDENTIALS_GRANT:
                return await _default_token_handler(request)
            client_id, client_secret = _client_credentials(request, form)
            configured = config.mcp_oauth_clients().get(client_id)
            if configured is None or not hmac.compare_digest(
                configured[1].encode(), client_secret.encode()
            ):
                return _token_error("invalid_client", "client authentication failed", 401)
            requested_scope = str(form.get("scope") or "").split()
            if requested_scope and requested_scope != [_SCOPE]:
                return _token_error("invalid_scope", "requested scope is not allowed", 400)
            resource = str(form.get("resource") or "") or f"{issuer}/mcp"
            if resource != f"{issuer}/mcp":
                return _token_error("invalid_target", "resource is not this hub", 400)
            token = provider.issue_client_credentials(
                client_id,
                configured[0],
                resource=resource,
            )
            return JSONResponse(
                token.model_dump(exclude_none=True),
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )

        if hasattr(default_token_endpoint, "app") and hasattr(default_token_endpoint.app, "app"):
            default_token_endpoint.app.app = request_response(token_endpoint)
        else:
            routes[index] = Route("/token", token_endpoint, methods=["POST", "OPTIONS"])
        break
    for index, route in enumerate(routes):
        if route.path in ("/token", "/revoke"):
            routes[index] = Route(
                route.path,
                _SecretBinding(route.app),
                methods=sorted(route.methods or {"POST", "OPTIONS"}),
            )
    for index, route in enumerate(routes):
        if route.path == "/.well-known/oauth-authorization-server":
            routes[index] = Route(
                "/.well-known/oauth-authorization-server",
                _authorization_server_metadata,
                methods=["GET", "OPTIONS"],
            )
            break
    routes.extend(
        create_protected_resource_routes(
            resource_url=resource_url,
            authorization_servers=[issuer_url],
            scopes_supported=[_SCOPE],
            resource_name="unified-agent",
        )
    )
    from . import oauth_approver

    # The approval pages may refresh the WorkOS approver cookie while checking
    # it (task #2114); the wrapper writes the refreshed session back.
    routes.append(
        Route(
            "/oauth/consent",
            oauth_approver.with_refreshed_cookie(consent),
            methods=["GET", "POST"],
        )
    )
    routes.append(Route("/oauth/client-credentials", client_credentials, methods=["POST"]))
    routes.append(Route("/oauth/whoami", whoami, methods=["GET"]))
    routes.append(Route("/oauth/device_authorization", device_authorization, methods=["POST"]))
    routes.append(
        Route(
            "/oauth/device",
            oauth_approver.with_refreshed_cookie(device_page),
            methods=["GET", "POST"],
        )
    )
    routes.append(
        Route("/oauth/approver/workos/login", oauth_approver.workos_login, methods=["GET"])
    )
    routes.append(
        Route("/oauth/approver/workos/callback", oauth_approver.workos_callback, methods=["GET"])
    )
    return routes


_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class _SecretBinding:
    """ASGI wrapper for /token and /revoke: expose the presented client secret.

    Buffers the (bounded) form body, reads the client secret from Basic auth or
    the form, binds it to ``_presented_client_secret`` for the SDK
    authenticator's ``get_client`` call, and replays the body unchanged. A body
    over the bound is passed through unbound (the SDK limit rejects it anyway).
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        try:
            too_big = declared is not None and int(declared) > _SECRET_BIND_MAX_BODY
        except ValueError:
            too_big = True
        if too_big:
            # Never buffer a body declared over the bound; the SDK's own body
            # limit rejects it downstream. Retention is otherwise bounded by
            # the limit plus the one chunk that crossed it.
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            chunks.append(chunk)
            more = message.get("more_body", False)
            if size > _SECRET_BIND_MAX_BODY:
                break
        body = b"".join(chunks)
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": more}
            return await receive()

        secret: str | None = None
        if not more:
            try:
                form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items() if v}
                request = Request(scope)
                _, secret = _client_credentials(request, form)
            except (UnicodeDecodeError, ValueError):
                secret = None
        token = _presented_client_secret.set(secret or None)
        try:
            await self.app(scope, replay, send)
        finally:
            _presented_client_secret.reset(token)


async def _authorization_server_metadata(request: Request) -> Response:
    """RFC 8414 metadata, extended with the RFC 8628 device endpoint."""
    cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS"}
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=cors)
    issuer = config.mcp_oauth_issuer() or ""
    from mcp.server.auth.routes import build_metadata

    metadata = build_metadata(
        AnyHttpUrl(issuer),
        None,
        ClientRegistrationOptions(enabled=True, valid_scopes=[_SCOPE], default_scopes=[_SCOPE]),
        RevocationOptions(enabled=True),
    ).model_dump(mode="json", exclude_none=True)
    grants = list(metadata.get("grant_types_supported") or [])
    for grant in (_DEVICE_CODE_GRANT, _CLIENT_CREDENTIALS_GRANT):
        if grant not in grants:
            grants.append(grant)
    metadata["grant_types_supported"] = grants
    methods = list(metadata.get("token_endpoint_auth_methods_supported") or [])
    if "none" not in methods:
        methods.append("none")
    metadata["token_endpoint_auth_methods_supported"] = methods
    metadata["device_authorization_endpoint"] = f"{issuer.rstrip('/')}/oauth/device_authorization"
    if config.mcp_oauth_cimd_enabled():
        # MCP auth spec 2025-11-25: URL client_ids are accepted (DCR remains).
        metadata["client_id_metadata_document_supported"] = True
    return JSONResponse(metadata, headers=cors)


def _peer_ip(request: Request) -> str | None:
    # Direct peer only. A forwarded-for header is never trusted here: any local
    # process can connect to loopback and forge it (PR #750 review). The trusted
    # Tailscale Serve path is the dedicated Unix listener (task #1892): its
    # forwarded claim arrives already settled in scope["client"] by
    # TrustedListenerProxyHeaders, so it reads as a direct peer here.
    return request.client.host if request.client else None


def _device_token_response(provider: HubOAuthProvider, request: Request, form) -> Response:
    client_id, client_secret = _client_credentials(request, form)
    device_code = str(form.get("device_code") or "")
    if not client_id or not device_code:
        return _token_error("invalid_request", "client_id and device_code are required", 400)
    auth_err = _require_client_auth(provider, client_id, client_secret)
    if auth_err is not None:
        return auth_err
    try:
        token = provider.poll_device(client_id, device_code)
    except DeviceGrantError as err:
        return _token_error(err.error, err.description, err.status_code)
    return JSONResponse(token.model_dump(exclude_none=True), headers=_NO_STORE)


def _require_client_auth(
    provider: HubOAuthProvider, client_id: str, client_secret: str
) -> JSONResponse | None:
    """Public clients (auth method ``none``) need no secret; confidential ones do."""
    client = provider.clients.get(client_id)
    if client is None:
        return _token_error("invalid_client", "unknown client", 401)
    method = (client.token_endpoint_auth_method or "none").lower()
    if method in ("none",):
        return None
    if not provider.client_secret_matches(client_id, client_secret):
        return _token_error("invalid_client", "client authentication failed", 401)
    return None


def _valid_agent_hint(raw: str) -> str | None:
    """A device may *suggest* its label; only the approver decides."""
    raw = raw.strip()
    if not raw:
        return None
    canonical = config.canonical_agent_label(raw) or raw
    return canonical if config.is_known_agent_label(canonical) else None


def _same_origin(request: Request) -> bool:
    """Refuse a cross-site POST. Absent Origin (non-browser clients) is allowed;
    the CSRF form token still applies to approvals."""
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin is None
    from urllib.parse import urlsplit

    got = urlsplit(origin)
    issuer = urlsplit(config.mcp_oauth_issuer() or "")
    host = (request.headers.get("host") or "").lower()
    return (got.scheme, got.netloc.lower()) == (issuer.scheme, issuer.netloc.lower()) or (
        got.netloc.lower() == host
    )


async def device_authorization(request: Request) -> Response:
    """RFC 8628 §3.1 device authorization endpoint."""
    provider = get_provider()
    if provider is None:
        return JSONResponse({"error": "oauth disabled"}, status_code=404)
    form = await request.form()
    client_id, client_secret = _client_credentials(request, form)
    auth_err = _require_client_auth(provider, client_id, client_secret) if client_id else None
    if not client_id:
        return _token_error("invalid_client", "client_id is required", 401)
    if auth_err is not None:
        return auth_err
    scope_raw = str(form.get("scope") or "").strip()
    issuer = config.mcp_oauth_issuer() or ""
    resource = str(form.get("resource") or "") or f"{issuer}/mcp"
    if resource != f"{issuer}/mcp":
        return _token_error("invalid_target", "resource is not this hub", 400)
    # A tailnet peer is the binding. With no peer (hosted hub: whois is off),
    # the public device key from `unified-agent login` is the binding instead.
    # A present but unusable key is refused so login does not mint an unbound
    # token the node will then be unable to register.
    peer = _resolve_peer_device(_peer_ip(request))
    if peer is not None:
        device_binding: dict[str, Any] | None = peer
    else:
        raw_key = str(form.get(device_key.FORM_FIELD) or "").strip()
        if raw_key:
            device_binding = device_key.binding_from_form(raw_key)
            if device_binding is None:
                return _token_error(
                    "invalid_request", "device_key must be an Ed25519 public JWK", 400
                )
        else:
            device_binding = None
    try:
        body = provider.start_device_authorization(
            client_id,
            scope_raw.split() if scope_raw else None,
            resource,
            device_binding,
            agent_hint=_valid_agent_hint(str(form.get("agent_hint") or "")),
        )
    except DeviceGrantError as err:
        return _token_error(err.error, err.description, err.status_code)
    return JSONResponse(body, headers=_NO_STORE)


async def device_page(request: Request) -> Response:
    """Human approval page for a device code (same consent gate as /oauth/consent)."""
    provider = get_provider()
    if provider is None:
        return JSONResponse({"error": "oauth disabled"}, status_code=404)
    public_host = _request_is_public_host(request)
    pinned_agent = config.mcp_oauth_agent() if public_host else None
    if request.method == "GET":
        user_code = request.query_params.get("user_code", "")
        grant = provider.pending_device_grant(user_code) if user_code else None
        if grant is None:
            # Step 1 of manual entry: a lookup form that GETs this page with the
            # typed code. The approval form (and its CSRF token) is only ever
            # rendered bound to one live code, so no token works across codes
            # (PR #750 review 4).
            note = "That code was not found or has expired. " if user_code else ""
            return HTMLResponse(
                _DEVICE_LOOKUP_PAGE.format(
                    note=html.escape(note), user_code=html.escape(user_code, quote=True)
                ),
                headers=_NO_STORE,
                status_code=404 if user_code else 200,
            )
        client_name = CLI_CLIENT_ID
        banner = "Enter the code shown on the device you are signing in."
        if grant is not None:
            client = await provider.get_client(grant.client_id)
            client_name = (client.client_name if client else None) or grant.client_id
            banner = _device_sign_in_banner(grant.client_id, grant.device_node)
        hint = grant.agent_hint if grant is not None else None
        suggested = pinned_agent or hint or config.mcp_oauth_agent()
        if pinned_agent is not None:
            agent_block = _AGENT_BLOCK_PINNED.format(
                suggested_agent=html.escape(suggested, quote=True)
            )
        else:
            agent_block = _AGENT_BLOCK_SELECTABLE.format(
                suggested_agent=html.escape(suggested, quote=True),
                agent_options=_render_agent_options(suggested),
            )
        approver_block, approver_script = await _render_approver(
            request,
            binding=_device_binding_key(grant.user_code),
            next_path=f"/oauth/device?user_code={quote(grant.user_code)}",
        )
        page = _DEVICE_PAGE.format(
            banner=banner,
            user_code=html.escape(grant.user_code if grant else user_code, quote=True),
            agent_block=agent_block,
            approver_block=approver_block,
            approver_script=approver_script,
            csrf=html.escape(_device_csrf_token(grant.user_code), quote=True),
        )
        # client_name is informational; keep it visible for the approver.
        page = page.replace("</h1>", f'</h1><p class="hint">{html.escape(client_name)}</p>', 1)
        return HTMLResponse(page, headers=_NO_STORE)

    if not _same_origin(request):
        return _device_result_page("Cross-site request refused.", 403)
    form = await request.form()
    user_code = str(form.get("user_code") or "")
    action = str(form.get("action") or "approve")
    approver_node = _resolve_peer_device(_peer_ip(request))
    if action == "deny":
        # Denying needs no approver: anyone holding the code may refuse it.
        if provider.deny_device(user_code, approver_node=approver_node):
            return _device_result_page("Device request denied. You can close this tab.")
        return _device_result_page("Code not found or expired.", 400)
    if not _device_csrf_ok(user_code, str(form.get("csrf") or "")):
        # The token comes from GET /oauth/device?user_code=… and is bound to that
        # one code; a form hosted elsewhere cannot supply it (device-flow CSRF).
        return _device_result_page(
            "This approval form expired or did not come from the hub. Reopen the link.", 403
        )
    from . import oauth_approver

    try:
        approval = await run_in_threadpool(
            oauth_approver.verify, request, form, binding=_device_binding_key(user_code)
        )
    except oauth_approver.ApprovalRefused as refused:
        return _device_result_page(f"{refused} The device is still waiting.", 403)
    agent_input = str(form.get("agent") or "").strip()
    chosen_agent = None
    if agent_input:
        chosen_agent = config.canonical_agent_label(agent_input) or agent_input
        if not config.is_known_agent_label(chosen_agent):
            return _device_result_page(f"Invalid agent label: {agent_input}.", 400)
    if pinned_agent is not None and chosen_agent and chosen_agent != pinned_agent:
        return _device_result_page(
            f"Public-Host approval is pinned; agent {chosen_agent} is not allowed.", 403
        )
    try:
        subject = provider.approve_device(
            user_code,
            chosen_agent,
            pinned_agent=pinned_agent,
            approver_node=approver_node,
            approval=approval,
        )
    except AuthorizeError as err:
        return _device_result_page(err.error_description or err.error, 400)
    return _device_result_page(
        f"Device approved as {subject}. It will finish signing in within a few seconds."
    )


def _client_credentials(request: Request, form) -> tuple[str, str]:
    """Read RFC 6749 client credentials from Basic auth or the request form."""
    client_id = str(form.get("client_id") or "")
    client_secret = str(form.get("client_secret") or "")
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            basic_id, separator, basic_secret = decoded.partition(":")
            if not separator:
                return "", ""
            basic_id, basic_secret = unquote(basic_id), unquote(basic_secret)
            if client_id and client_id != basic_id:
                return "", ""
            client_id, client_secret = basic_id, basic_secret
        except (ValueError, UnicodeDecodeError):
            return "", ""
    return client_id, client_secret


def _token_error(error: str, description: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _public_host_request(request: Request) -> bool:
    return _request_is_public_host(request)


def _device_binding_key(user_code: str) -> str:
    """Passkey-challenge binding for one device code."""
    return f"device:{_normalize_user_code(user_code)}"


def _bearer_from_request(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        return token or None
    return None


async def whoami(request: Request) -> Response:
    """One-shot verification: agent, profile, scopes, token age for a Bearer token."""
    provider = get_provider()
    if provider is None:
        return JSONResponse({"error": "oauth disabled"}, status_code=404)
    token = _bearer_from_request(request)
    if not token:
        return JSONResponse({"error": "missing bearer token"}, status_code=401)
    payload = provider.whoami(token)
    if payload is None:
        return JSONResponse({"error": "invalid_token"}, status_code=401)
    return JSONResponse(payload)


async def client_credentials(request: Request) -> Response:
    """RFC 6749 client_credentials for pre-provisioned cloud-bot clients."""
    provider = get_provider()
    if provider is None:
        return JSONResponse({"error": "oauth disabled"}, status_code=404)
    form = await request.form()
    grant = str(form.get("grant_type") or "client_credentials").strip()
    if grant != "client_credentials":
        return JSONResponse(
            {"error": "unsupported_grant_type", "error_description": "use client_credentials"},
            status_code=400,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    client_id = str(form.get("client_id") or "").strip()
    client_secret = str(form.get("client_secret") or "")
    scope_raw = str(form.get("scope") or "").strip()
    scopes = scope_raw.split() if scope_raw else None
    if not client_id or not client_secret:
        return JSONResponse(
            {
                "error": "invalid_client",
                "error_description": "client_id and client_secret required",
            },
            status_code=401,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    try:
        token = provider.exchange_client_credentials(client_id, client_secret, scopes)
    except TokenError as err:
        status = 401 if err.error == "invalid_client" else 400
        return JSONResponse(
            {"error": err.error, "error_description": err.error_description},
            status_code=status,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    return JSONResponse(
        token.model_dump(exclude_none=True),
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


async def consent(request: Request) -> Response:
    """Verified-approver gate in front of the authorization code (task #1322). Public URL."""
    provider = get_provider()
    if provider is None:
        return JSONResponse({"error": "oauth disabled"}, status_code=404)
    public_host = _request_is_public_host(request)
    pinned_agent = config.mcp_oauth_agent() if public_host else None
    if request.method == "GET":
        rid = request.query_params.get("rid", "")
        if not rid:
            return JSONResponse({"error": "missing rid"}, status_code=400)
        with _lock:
            pending_entry = provider.pending.get(rid)
        if pending_entry is None:
            return HTMLResponse(
                "<!DOCTYPE html><html><body><p>Consent request not found or expired. Close this tab.</p></body></html>",
                status_code=400,
            )
        client, params, expires_at = pending_entry
        if expires_at < time.time():
            return HTMLResponse(
                "<!DOCTYPE html><html><body><p>Consent request expired. Close this tab.</p></body></html>",
                status_code=400,
            )
        client_name = html.escape(client.client_name or client.client_id or "Remote Client")
        suggested_agent = pinned_agent or _resolve_suggested_agent(client, params)
        if pinned_agent is not None:
            agent_block = _AGENT_BLOCK_PINNED.format(
                suggested_agent=html.escape(suggested_agent, quote=True),
            )
        else:
            agent_block = _AGENT_BLOCK_SELECTABLE.format(
                suggested_agent=html.escape(suggested_agent, quote=True),
                agent_options=_render_agent_options(suggested_agent),
            )
        approver_block, approver_script = await _render_approver(
            request, binding=f"consent:{rid}", next_path=f"/oauth/consent?rid={quote(rid)}"
        )
        return HTMLResponse(
            _CONSENT_PAGE.format(
                rid=html.escape(rid, quote=True),
                csrf=html.escape(_consent_csrf_token(rid), quote=True),
                client_name=client_name,
                agent_block=agent_block,
                approver_block=approver_block,
                approver_script=approver_script,
            ),
            headers=_NO_STORE,
        )
    if not _same_origin(request):
        return HTMLResponse(
            "<!DOCTYPE html><html><body><p>Cross-site request refused.</p></body></html>",
            status_code=403,
        )
    form = await request.form()
    rid = str(form.get("rid") or "")
    agent_input = str(form.get("agent") or "").strip()
    if not rid or not _consent_csrf_ok(rid, str(form.get("csrf") or "")):
        return HTMLResponse(
            "<!DOCTYPE html><html><body><p>This approval form expired or did not come "
            "from the hub. Reopen the link.</p></body></html>",
            status_code=403,
        )
    from . import oauth_approver

    try:
        approval = await run_in_threadpool(
            oauth_approver.verify, request, form, binding=f"consent:{rid}"
        )
    except oauth_approver.ApprovalRefused as refused:
        return HTMLResponse(
            "<!DOCTYPE html><html><body><p>"
            + html.escape(str(refused))
            + " Close this tab.</p></body></html>",
            status_code=403,
        )
    chosen_agent = None
    if agent_input:
        canonical = config.canonical_agent_label(agent_input) or agent_input
        if not config.is_known_agent_label(canonical):
            return HTMLResponse(
                f"<!DOCTYPE html><html><body><p>Invalid agent label: {html.escape(agent_input)}. Close this tab.</p></body></html>",
                status_code=400,
            )
        chosen_agent = canonical
    if pinned_agent is not None:
        # Public front door: refuse any attempt to mint a different identity.
        if chosen_agent and chosen_agent != pinned_agent:
            return HTMLResponse(
                "<!DOCTYPE html><html><body><p>Public-Host consent is pinned; "
                f"agent {html.escape(chosen_agent)} is not allowed. Close this tab.</p></body></html>",
                status_code=403,
            )
        chosen_agent = pinned_agent
    try:
        redirect_to = provider.complete_consent(
            rid, agent=chosen_agent, pinned_agent=pinned_agent, approval=approval
        )
    except AuthorizeError as err:
        return JSONResponse(
            {"error": err.error, "error_description": err.error_description}, status_code=400
        )
    return RedirectResponse(redirect_to, status_code=302)
