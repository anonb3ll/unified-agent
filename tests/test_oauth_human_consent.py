"""Verified-human consent for OAuth sign-in (task #1322, Epic J3).

Consent (``/oauth/consent``) and device approval (``/oauth/device``) used to
accept anyone holding the shared ``UA_MCP_OAUTH_PASSPHRASE``. Now the approver
must be a verified human — a WorkOS session (enterprise) or a tailnet node owned
by the configured login plus a registered passkey (private) — and the passphrase
approves only behind ``UA_MCP_OAUTH_PASSPHRASE_FALLBACK=1``. The approval is
recorded as ``human.verified`` on the audit events and in the token's claims.

Deterministic: WebAuthn ceremonies are built here with a real P-256 key, the
tailnet lookup and the WorkOS leg are replaced by fakes, nothing touches the
network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import struct
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from unified_agent import (
    config,
    events,
    oauth,
    oauth_approver,
    passkeys,
    server,
    storage,
    webui,
    workos_hub_auth,
)
from unified_agent.server import HubAuthASGIMiddleware

ISSUER = "https://hub.example.ts.net"
RP_ID = "hub.example.ts.net"
LOGIN = "operator@example.com"
APPROVER_NODE = {"node": "mac.tail1234.ts.net", "node_id": "nMAC1", "user": LOGIN}
OTHER_NODE = {"node": "shared.tail1234.ts.net", "node_id": "nOTHER", "user": "guest@example.com"}
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLI_CLIENT = "unified-agent-cli"
HUMAN_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
AGENT_TOKEN = f"tok-{secrets.token_urlsafe(8)}"


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Authenticator:
    """A software passkey: a P-256 key plus WebAuthn byte layout."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = secrets.token_bytes(32)
        self.count = 0

    def spki(self) -> bytes:
        return self.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    @staticmethod
    def client_data(kind: str, challenge: str, origin: str = ISSUER) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin}).encode()

    def auth_data(self, *, flags: int = 0x05, rp_id: str = RP_ID, attested: bool = False) -> bytes:
        self.count += 1
        body = hashlib.sha256(rp_id.encode()).digest() + bytes([flags])
        body += struct.pack(">I", self.count)
        if attested:
            body += b"\x00" * 16 + struct.pack(">H", len(self.cred_id)) + self.cred_id + b"\xa0"
        return body

    def registration(self, challenge: str) -> dict:
        return {
            "credential_id": b64u(self.cred_id),
            "client_data_json": b64u(self.client_data("webauthn.create", challenge)),
            "authenticator_data": b64u(self.auth_data(flags=0x45, attested=True)),
            "public_key": b64u(self.spki()),
            "public_key_algorithm": -7,
        }

    def assertion(
        self, challenge: str, *, origin: str = ISSUER, flags: int = 0x05, sign_with=None
    ) -> dict:
        client = self.client_data("webauthn.get", challenge, origin)
        auth = self.auth_data(flags=flags)
        key = sign_with or self.key
        sig = key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "passkey_credential_id": b64u(self.cred_id),
            "passkey_client_data": b64u(client),
            "passkey_authenticator_data": b64u(auth),
            "passkey_signature": b64u(sig),
        }


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    oauth.reset_provider()
    passkeys.reset_challenges()
    workos_hub_auth.reset_hub_auth_cache()
    for var in (
        "UA_MCP_OAUTH_PASSPHRASE_FALLBACK",
        "UA_MCP_OAUTH_APPROVER_LOGINS",
        "UA_MCP_OAUTH_PASSKEY_RP_ID",
        "UA_HTTP_PUBLIC_HOSTS",
        "UA_EDITION",
        "UA_HUB_WORKOS_ORGANIZATION_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    storage.init_db()
    yield
    oauth.reset_provider()
    passkeys.reset_challenges()
    workos_hub_auth.reset_hub_auth_cache()


@pytest.fixture
def peer(monkeypatch):
    """Which tailnet node the direct peer resolves to; records the IPs asked."""
    state = {"node": dict(APPROVER_NODE), "seen": []}

    def fake(ip):
        state["seen"].append(ip)
        return dict(state["node"]) if state["node"] else None

    monkeypatch.setattr(oauth, "_resolve_peer_device", fake)
    return state


@pytest.fixture
def audit(monkeypatch):
    rows: list[tuple[str, dict]] = []
    real = events.record_event

    def capture(channel, payload=None, **kwargs):
        rows.append((channel, payload or {}))
        return real(channel, payload=payload, **kwargs)

    monkeypatch.setattr(events, "record_event", capture)
    return rows


def _app(monkeypatch, *, passphrase: str = "hub-pass", logins: str | None = LOGIN) -> TestClient:
    monkeypatch.setenv("UA_MCP_OAUTH", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", ISSUER)
    if passphrase:
        monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", passphrase)
    else:
        monkeypatch.delenv("UA_MCP_OAUTH_PASSPHRASE", raising=False)
    if logins:
        monkeypatch.setenv("UA_MCP_OAUTH_APPROVER_LOGINS", logins)
    monkeypatch.setenv("UA_MCP_OAUTH_AGENT", "Desktop_Agent8")
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:static-token")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    oauth.reset_provider()

    async def homepage(request):
        return JSONResponse(
            {
                "identity": config.verified_agent_identity(),
                "meta": config.current_auth_metadata(),
            }
        )

    # Not "/": exact "/" is public and redirects in the real hub (task
    # #1400), which would defeat the 401/identity assertions below.
    app = Starlette(routes=[Route("/whoami", homepage, methods=["GET"]), *oauth.http_routes()])
    return TestClient(HubAuthASGIMiddleware(app), follow_redirects=False)


def _register(auth: Authenticator, login: str = LOGIN) -> None:
    options = passkeys.registration_options(login)
    passkeys.register(login=login, **auth.registration(options["challenge"]))


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = b64u(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _start_consent(client: TestClient) -> tuple[str, str, str]:
    reg = client.post(
        "/register",
        json={
            "redirect_uris": ["http://localhost:9999/cb"],
            "client_name": "Remote Client",
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "none",
        },
    ).json()
    verifier, challenge = _pkce()
    resp = client.get(
        "/authorize",
        params={
            "client_id": reg["client_id"],
            "redirect_uri": "http://localhost:9999/cb",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st",
        },
    )
    rid = parse_qs(urlparse(resp.headers["location"]).query)["rid"][0]
    return reg["client_id"], rid, verifier


def _consent_page_challenge(client: TestClient, rid: str) -> str | None:
    page = client.get("/oauth/consent", params={"rid": rid}).text
    match = re.search(r'id="ua-passkey-options">(.*?)</script>', page, re.S)
    return json.loads(match.group(1))["challenge"] if match else None


def _exchange(client: TestClient, client_id: str, location: str, verifier: str) -> dict:
    code = parse_qs(urlparse(location).query)["code"][0]
    resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://localhost:9999/cb",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _consent_post(client: TestClient, rid: str, **fields):
    data = {"rid": rid, "csrf": oauth._consent_csrf_token(rid), "agent": "Desktop_Agent2"}
    data.update(fields)
    return client.post("/oauth/consent", data=data)


def _whoami(client: TestClient, token: str) -> dict:
    resp = client.get("/oauth/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestConfig:
    def test_passphrase_alone_keeps_server_up(self, monkeypatch):
        _app(monkeypatch, logins=None)
        assert config.mcp_oauth_enabled() is True
        assert config.mcp_oauth_passphrase_fallback() is False

    def test_approver_logins_alone_enable(self, monkeypatch):
        _app(monkeypatch, passphrase="")
        assert config.mcp_oauth_enabled() is True
        assert config.mcp_oauth_approver_logins() == [LOGIN]

    def test_no_approver_at_all_stays_off(self, monkeypatch):
        _app(monkeypatch, passphrase="", logins=None)
        assert config.mcp_oauth_enabled() is False

    def test_rp_id_defaults_to_issuer_and_rejects_foreign_override(self, monkeypatch):
        _app(monkeypatch)
        assert config.mcp_oauth_passkey_rp_id() == RP_ID
        monkeypatch.setenv("UA_MCP_OAUTH_PASSKEY_RP_ID", "example.ts.net")
        assert config.mcp_oauth_passkey_rp_id() == "example.ts.net"
        monkeypatch.setenv("UA_MCP_OAUTH_PASSKEY_RP_ID", "evil.test")
        assert config.mcp_oauth_passkey_rp_id() == RP_ID


class TestPassphraseFallback:
    def test_passphrase_refused_by_default(self, monkeypatch, peer):
        peer["node"] = None
        client = _app(monkeypatch)
        _cid, rid, _v = _start_consent(client)
        resp = _consent_post(client, rid, passphrase="hub-pass")
        assert resp.status_code == 403
        assert "Passphrase approval is disabled" in resp.text

    def test_device_passphrase_refused_by_default(self, monkeypatch, peer):
        peer["node"] = None
        client = _app(monkeypatch)
        grant = client.post("/oauth/device_authorization", data={"client_id": CLI_CLIENT}).json()
        page = client.get("/oauth/device", params={"user_code": grant["user_code"]}).text
        csrf = re.search(r'name="csrf" value="([^"]*)"', page).group(1)
        assert 'name="passphrase"' not in page
        resp = client.post(
            "/oauth/device",
            data={
                "user_code": grant["user_code"],
                "passphrase": "hub-pass",
                "csrf": csrf,
                "action": "approve",
            },
        )
        assert resp.status_code == 403
        poll = client.post(
            "/token",
            data={
                "grant_type": DEVICE_GRANT,
                "device_code": grant["device_code"],
                "client_id": CLI_CLIENT,
            },
        )
        assert poll.json()["error"] == "authorization_pending"

    def test_fallback_approves_but_is_not_human_verified(self, monkeypatch, peer, audit):
        peer["node"] = None
        monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
        client = _app(monkeypatch)
        cid, rid, verifier = _start_consent(client)
        assert 'name="passphrase"' in client.get("/oauth/consent", params={"rid": rid}).text
        resp = _consent_post(client, rid, passphrase="hub-pass")
        assert resp.status_code == 302, resp.text
        token = _exchange(client, cid, resp.headers["location"], verifier)
        human = _whoami(client, token["access_token"])["human_approval"]
        assert human["verified"] is False
        assert human["verified_via"] == "passphrase"
        consent = [p for c, p in audit if c == "hub.oauth.consent"][-1]
        assert consent["human"] == {
            "verified": False,
            "verified_via": "passphrase",
            "approver": "passphrase-holder",
        }

    def test_wrong_passphrase_refused_with_fallback(self, monkeypatch, peer):
        peer["node"] = None
        monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
        client = _app(monkeypatch)
        _cid, rid, _v = _start_consent(client)
        assert _consent_post(client, rid, passphrase="nope").status_code == 403


class TestConsentCsrf:
    def test_missing_csrf_refused(self, monkeypatch, peer):
        monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
        client = _app(monkeypatch)
        _cid, rid, _v = _start_consent(client)
        resp = client.post("/oauth/consent", data={"rid": rid, "passphrase": "hub-pass"})
        assert resp.status_code == 403

    def test_cross_origin_refused(self, monkeypatch, peer):
        monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
        client = _app(monkeypatch)
        _cid, rid, _v = _start_consent(client)
        resp = client.post(
            "/oauth/consent",
            data={"rid": rid, "csrf": oauth._consent_csrf_token(rid), "passphrase": "hub-pass"},
            headers={"Origin": "https://evil.test"},
        )
        assert resp.status_code == 403


class TestTailnetPasskey:
    def test_consent_with_passkey_is_human_verified(self, monkeypatch, peer, audit):
        client = _app(monkeypatch)
        auth = Authenticator()
        _register(auth)
        cid, rid, verifier = _start_consent(client)
        challenge = _consent_page_challenge(client, rid)
        assert challenge
        resp = _consent_post(client, rid, **auth.assertion(challenge))
        assert resp.status_code == 302, resp.text
        token = _exchange(client, cid, resp.headers["location"], verifier)

        human = _whoami(client, token["access_token"])["human_approval"]
        assert human["verified"] is True
        assert human["verified_via"] == "tailnet+passkey"
        assert human["approver"] == LOGIN
        assert human["tailnet_node"] == APPROVER_NODE["node"]

        consent = [p for c, p in audit if c == "hub.oauth.consent"][-1]
        assert consent["human"]["verified"] is True
        assert consent["human"]["verified_via"] == "tailnet+passkey"
        issued = [p for c, p in audit if c == "hub.oauth.token_issue"][-1]
        assert issued["human"]["verified"] is True

        # The request auth metadata carries it, and refresh rotation keeps it.
        meta = client.get("/whoami", headers={"Authorization": f"Bearer {token['access_token']}"})
        assert meta.json()["meta"]["human_approval"]["verified"] is True
        rotated = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token["refresh_token"],
                "client_id": cid,
            },
        ).json()
        assert _whoami(client, rotated["access_token"])["human_approval"]["verified"] is True

    def test_device_approval_with_passkey(self, monkeypatch, peer, audit):
        client = _app(monkeypatch)
        auth = Authenticator()
        _register(auth)
        grant = client.post("/oauth/device_authorization", data={"client_id": CLI_CLIENT}).json()
        page = client.get("/oauth/device", params={"user_code": grant["user_code"]}).text
        csrf = re.search(r'name="csrf" value="([^"]*)"', page).group(1)
        options = json.loads(re.search(r'id="ua-passkey-options">(.*?)</script>', page).group(1))
        assert options["rpId"] == RP_ID
        assert options["allowCredentials"][0]["id"] == b64u(auth.cred_id)
        resp = client.post(
            "/oauth/device",
            data={
                "user_code": grant["user_code"],
                "csrf": csrf,
                "agent": "Desktop_Agent2",
                "action": "approve",
                **auth.assertion(options["challenge"]),
            },
        )
        assert resp.status_code == 200, resp.text
        token = client.post(
            "/token",
            data={
                "grant_type": DEVICE_GRANT,
                "device_code": grant["device_code"],
                "client_id": CLI_CLIENT,
            },
        ).json()
        who = _whoami(client, token["access_token"])
        assert who["agent"] == "Desktop_Agent2"
        assert who["human_approval"]["verified_via"] == "tailnet+passkey"
        approved = [p for c, p in audit if c == "hub.oauth.device_approved"][-1]
        assert approved["human"]["verified"] is True

    def test_tailnet_alone_does_not_approve(self, monkeypatch, peer):
        client = _app(monkeypatch)
        _register(Authenticator())
        _cid, rid, _v = _start_consent(client)
        resp = _consent_post(client, rid)
        assert resp.status_code == 403
        assert "No verified approver" in resp.text

    def test_valid_passkey_from_foreign_node_refused(self, monkeypatch, peer):
        client = _app(monkeypatch)
        auth = Authenticator()
        _register(auth)
        _cid, rid, _v = _start_consent(client)
        challenge = _consent_page_challenge(client, rid)
        peer["node"] = dict(OTHER_NODE)
        resp = _consent_post(client, rid, **auth.assertion(challenge))
        assert resp.status_code == 403
        assert "tailnet device owned by the approver" in resp.text

    def test_passkey_failure_is_not_downgraded_to_passphrase(self, monkeypatch, peer):
        monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
        client = _app(monkeypatch)
        auth = Authenticator()
        _register(auth)
        _cid, rid, _v = _start_consent(client)
        challenge = _consent_page_challenge(client, rid)
        forged = auth.assertion(challenge, sign_with=ec.generate_private_key(ec.SECP256R1()))
        resp = _consent_post(client, rid, passphrase="hub-pass", **forged)
        assert resp.status_code == 403
        assert "signature did not verify" in resp.text

    def test_spoofed_identity_headers_do_not_verify(self, monkeypatch, peer):
        """Only the direct peer is resolved; forwarded headers are ignored."""
        peer["node"] = None
        client = _app(monkeypatch)
        auth = Authenticator()
        _register(auth)
        _cid, rid, _v = _start_consent(client)
        headers = {
            "X-Forwarded-For": "100.64.0.7",
            "Tailscale-User-Login": LOGIN,
            "Tailscale-User-Name": "Operator",
        }
        page = client.get("/oauth/consent", params={"rid": rid}, headers=headers).text
        assert "ua-passkey-options" not in page
        challenge = passkeys.issue_challenge("assert", binding=f"consent:{rid}", login=LOGIN)
        resp = client.post(
            "/oauth/consent",
            data={
                "rid": rid,
                "csrf": oauth._consent_csrf_token(rid),
                **auth.assertion(challenge),
            },
            headers=headers,
        )
        assert resp.status_code == 403
        assert "100.64.0.7" not in peer["seen"]
        assert set(peer["seen"]) == {"testclient"}

    def test_replayed_assertion_refused(self, monkeypatch, peer):
        client = _app(monkeypatch)
        auth = Authenticator()
        _register(auth)
        _cid, rid, _v = _start_consent(client)
        challenge = _consent_page_challenge(client, rid)
        assertion = auth.assertion(challenge)
        assert _consent_post(client, rid, **assertion).status_code == 302
        _cid2, rid2, _v2 = _start_consent(client)
        assert _consent_post(client, rid2, **assertion).status_code == 403

    def test_challenge_bound_to_its_consent_request(self, monkeypatch, peer):
        client = _app(monkeypatch)
        auth = Authenticator()
        _register(auth)
        _cid, rid_a, _v = _start_consent(client)
        _cid, rid_b, _v = _start_consent(client)
        challenge_a = _consent_page_challenge(client, rid_a)
        resp = _consent_post(client, rid_b, **auth.assertion(challenge_a))
        assert resp.status_code == 403
        assert "does not belong to this request" in resp.text

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"origin": "https://evil.test"}, "origin does not match"),
            ({"flags": 0x01}, "must verify the user"),
        ],
    )
    def test_assertion_checks(self, monkeypatch, peer, kwargs, message):
        client = _app(monkeypatch)
        auth = Authenticator()
        _register(auth)
        _cid, rid, _v = _start_consent(client)
        challenge = _consent_page_challenge(client, rid)
        resp = _consent_post(client, rid, **auth.assertion(challenge, **kwargs))
        assert resp.status_code == 403
        assert message in resp.text

    def test_signature_counter_must_advance(self, monkeypatch, peer):
        client = _app(monkeypatch)
        auth = Authenticator()
        _register(auth)
        _cid, rid, _v = _start_consent(client)
        auth.count = 0  # a cloned authenticator replays an old counter
        resp = _consent_post(client, rid, **auth.assertion(_consent_page_challenge(client, rid)))
        assert resp.status_code == 403
        assert "counter did not advance" in resp.text


class _FakeLeg:
    def __init__(self, identity: str = "Desktop_Person") -> None:
        self.identity = identity
        self.seen: list[str] = []
        self.refreshed: list[str] = []

    def resolve_refreshing(self, sealed):
        """`sealed-expired` holds an expired access token that refreshes (task #2114)."""
        if sealed == "sealed-expired":
            self.refreshed.append(sealed)
            return self.resolve("sealed-good"), "sealed-good"
        return self.resolve(sealed), None

    def resolve(self, sealed):
        self.seen.append(sealed)
        if sealed != "sealed-good":
            return None
        return {
            "identity": self.identity,
            "subject": "user_01",
            "organization_id": "org_01",
            "session_id": "session_01",
            "expires_at": 4_000_000_000,
        }


class TestWorkOS:
    @pytest.fixture
    def leg(self, monkeypatch):
        fake = _FakeLeg()
        monkeypatch.setattr(workos_hub_auth, "enabled", lambda: True)
        monkeypatch.setattr(workos_hub_auth, "hub_auth", lambda: fake)
        return fake

    def test_cookie_session_approves_as_verified_human(self, monkeypatch, peer, leg, audit):
        peer["node"] = None
        client = _app(monkeypatch, logins=None)
        cid, rid, verifier = _start_consent(client)
        client.cookies.set(oauth_approver.WORKOS_COOKIE, "sealed-good", path="/oauth")
        assert "Signed in with WorkOS" in client.get("/oauth/consent", params={"rid": rid}).text
        resp = _consent_post(client, rid)
        assert resp.status_code == 302, resp.text
        token = _exchange(client, cid, resp.headers["location"], verifier)
        human = _whoami(client, token["access_token"])["human_approval"]
        assert human["verified"] is True
        assert human["verified_via"] == "workos"
        assert human["approver"] == "Desktop_Person"
        assert human["workos_subject"] == "user_01"
        consent = [p for c, p in audit if c == "hub.oauth.consent"][-1]
        assert consent["human"]["verified_via"] == "workos"

    def test_workos_session_skips_passkey_prompt_on_approver_node(self, monkeypatch, peer, leg):
        """Devin #776: a cancelled passkey prompt must not block a WorkOS approval."""
        client = _app(monkeypatch)
        _register(Authenticator())
        cid, rid, verifier = _start_consent(client)
        client.cookies.set(oauth_approver.WORKOS_COOKIE, "sealed-good", path="/oauth")
        page = client.get("/oauth/consent", params={"rid": rid}).text
        assert "ua-passkey-options" not in page
        assert "navigator.credentials" not in page
        resp = _consent_post(client, rid)
        assert resp.status_code == 302, resp.text
        token = _exchange(client, cid, resp.headers["location"], verifier)
        assert _whoami(client, token["access_token"])["human_approval"]["verified_via"] == "workos"

    def test_expired_cookie_session_is_refreshed_and_written_back(
        self, monkeypatch, peer, leg, audit
    ):
        """Task #2114: the approver cookie outlives the ~5 minute access token."""
        peer["node"] = None
        client = _app(monkeypatch, logins=None)
        cid, rid, verifier = _start_consent(client)
        client.cookies.set(oauth_approver.WORKOS_COOKIE, "sealed-expired", path="/oauth")
        page = client.get("/oauth/consent", params={"rid": rid})
        assert "Signed in with WorkOS" in page.text
        assert leg.refreshed == ["sealed-expired"]
        cookie = page.headers["set-cookie"]
        assert "ua_oauth_workos=sealed-good" in cookie
        assert "HttpOnly" in cookie and "Path=/oauth" in cookie
        # The cookie is Secure (https issuer) and this client speaks http, so it
        # resends the old value: the approval refreshes again and writes back
        # on the redirect too.
        resp = _consent_post(client, rid)
        assert resp.status_code == 302, resp.text
        assert leg.refreshed == ["sealed-expired", "sealed-expired"]
        assert "ua_oauth_workos=sealed-good" in resp.headers["set-cookie"]
        token = _exchange(client, cid, resp.headers["location"], verifier)
        assert _whoami(client, token["access_token"])["human_approval"]["verified_via"] == "workos"

    def test_a_header_session_is_never_refreshed_into_the_cookie(self, monkeypatch, peer, leg):
        """The header's client owns its session; refreshing it would spend its token."""
        peer["node"] = None
        client = _app(monkeypatch, logins=None)
        _cid, rid, _v = _start_consent(client)
        page = client.get(
            "/oauth/consent",
            params={"rid": rid},
            headers={"authorization": "WorkOS sealed-expired"},
        )
        assert leg.refreshed == []
        assert "ua_oauth_workos=" not in page.headers.get("set-cookie", "")

    def test_stale_workos_cookie_can_fall_back_to_passkey(self, monkeypatch, peer, leg):
        client = _app(monkeypatch)
        auth = Authenticator()
        _register(auth)
        cid, rid, verifier = _start_consent(client)
        client.cookies.set(oauth_approver.WORKOS_COOKIE, "sealed-revoked", path="/oauth")
        challenge = _consent_page_challenge(client, rid)
        assert challenge
        resp = _consent_post(client, rid, **auth.assertion(challenge))
        assert resp.status_code == 302, resp.text
        token = _exchange(client, cid, resp.headers["location"], verifier)
        assert (
            _whoami(client, token["access_token"])["human_approval"]["verified_via"]
            == "tailnet+passkey"
        )

        grant = client.post("/oauth/device_authorization", data={"client_id": CLI_CLIENT}).json()
        device_page = client.get("/oauth/device", params={"user_code": grant["user_code"]}).text
        assert "ua-passkey-options" in device_page

    def test_invalid_session_refused_and_login_offered(self, monkeypatch, peer, leg):
        peer["node"] = None
        client = _app(monkeypatch, logins=None)
        _cid, rid, _v = _start_consent(client)
        page = client.get("/oauth/consent", params={"rid": rid}).text
        assert "/oauth/approver/workos/login?next=" in page
        client.cookies.set(oauth_approver.WORKOS_COOKIE, "sealed-forged", path="/oauth")
        assert _consent_post(client, rid).status_code == 403
        assert "sealed-forged" in leg.seen

    def test_workos_login_redirect_only_returns_to_approval_pages(self):
        assert oauth_approver._safe_next("/oauth/consent?rid=abc") == "/oauth/consent?rid=abc"
        assert oauth_approver._safe_next("https://evil.test/oauth/consent") == "/oauth/device"
        assert oauth_approver._safe_next("//evil.test/x") == "/oauth/device"
        assert oauth_approver._safe_next("/ui") == "/oauth/device"
        assert oauth_approver._safe_next("/oauth/consent/../../ui") == "/oauth/device"

    def test_login_round_trip_sets_session_cookie(self, monkeypatch, peer, leg):
        seen: dict = {}

        class _UserManagement:
            def get_authorization_url(self, **kwargs):
                seen.update(kwargs)
                return "https://auth.example.test/authorize?state=" + kwargs["state"]

        leg.organization_id = "org_01"
        leg.provider = type("P", (), {"client": type("C", (), {})()})()
        leg.provider.client.user_management = _UserManagement()
        monkeypatch.setattr(
            oauth_approver, "_seal_code", lambda _leg, code: "sealed-good" if code == "ok" else None
        )
        client = _app(monkeypatch, logins=None)
        start = client.get(
            "/oauth/approver/workos/login", params={"next": "/oauth/consent?rid=abc"}
        )
        assert start.status_code == 302
        assert seen["redirect_uri"] == f"{ISSUER}/oauth/approver/workos/callback"
        assert seen["organization_id"] == "org_01"
        state = seen["state"]
        # Wrong cookie state (another browser) is refused even with a valid state.
        client.cookies.set(oauth_approver._state_cookie_name(state), "other", path="/oauth")
        bad = client.get("/oauth/approver/workos/callback", params={"state": state, "code": "ok"})
        assert bad.status_code == 400
        start = client.get(
            "/oauth/approver/workos/login", params={"next": "/oauth/consent?rid=abc"}
        )
        state = seen["state"]
        client.cookies.set(oauth_approver._state_cookie_name(state), state, path="/oauth")
        done = client.get("/oauth/approver/workos/callback", params={"state": state, "code": "ok"})
        assert done.status_code == 302
        assert done.headers["location"] == "/oauth/consent?rid=abc"
        cookie = done.headers["set-cookie"]
        assert "ua_oauth_workos=sealed-good" in cookie
        assert "HttpOnly" in cookie and "Path=/oauth" in cookie and "Secure" in cookie

    def test_unmapped_workos_account_gets_no_cookie(self, monkeypatch, peer, leg):
        leg.organization_id = "org_01"

        class _UserManagement:
            def get_authorization_url(self, **kwargs):
                leg.state = kwargs["state"]
                return "https://auth.example.test/authorize"

        leg.provider = type("P", (), {"client": type("C", (), {})()})()
        leg.provider.client.user_management = _UserManagement()
        monkeypatch.setattr(oauth_approver, "_seal_code", lambda _leg, code: None)
        client = _app(monkeypatch, logins=None)
        client.get("/oauth/approver/workos/login", params={"next": "/oauth/device"})
        client.cookies.set(oauth_approver._state_cookie_name(leg.state), leg.state, path="/oauth")
        resp = client.get(
            "/oauth/approver/workos/callback", params={"state": leg.state, "code": "x"}
        )
        assert resp.status_code == 403
        assert "ua_oauth_workos=" not in resp.headers.get("set-cookie", "")

    def test_callback_rejects_state_mismatch(self, monkeypatch, peer, leg):
        client = _app(monkeypatch, logins=None)
        resp = client.get(
            "/oauth/approver/workos/callback", params={"state": "made-up", "code": "c"}
        )
        assert resp.status_code == 400

    def test_parallel_login_tabs_keep_independent_states(self, monkeypatch, peer, leg):
        states = []

        class _UserManagement:
            def get_authorization_url(self, **kwargs):
                states.append(kwargs["state"])
                return "https://auth.example.test/authorize"

        leg.organization_id = "org_01"
        leg.provider = type("P", (), {"client": type("C", (), {})()})()
        leg.provider.client.user_management = _UserManagement()
        monkeypatch.setattr(oauth_approver, "_seal_code", lambda _leg, code: "sealed-good")
        client = _app(monkeypatch, logins=None)
        for target in ("/oauth/device", "/oauth/consent?rid=abc"):
            assert (
                client.get("/oauth/approver/workos/login", params={"next": target}).status_code
                == 302
            )
        assert len(states) == 2
        assert oauth_approver._state_cookie_name(states[0]) in client.cookies
        assert oauth_approver._state_cookie_name(states[1]) in client.cookies
        for state, target in zip(states, ("/oauth/device", "/oauth/consent?rid=abc"), strict=True):
            client.cookies.set(oauth_approver._state_cookie_name(state), state, path="/oauth")
            done = client.get(
                "/oauth/approver/workos/callback", params={"state": state, "code": "ok"}
            )
            assert done.status_code == 302
            assert done.headers["location"] == target

    def test_page_context_runs_off_event_loop(self, monkeypatch, peer, leg):
        import threading

        seen = []
        original = oauth_approver.page_context

        def traced(*args, **kwargs):
            seen.append(threading.current_thread().name)
            return original(*args, **kwargs)

        monkeypatch.setattr(oauth_approver, "page_context", traced)
        client = _app(monkeypatch, logins=None)
        _cid, rid, _verifier = _start_consent(client)
        assert client.get("/oauth/consent", params={"rid": rid}).status_code == 200
        assert seen and all("worker thread" in name.lower() for name in seen)


class TestPasskeyRegistrationApi:
    @pytest.fixture
    def ui(self, monkeypatch, peer):
        monkeypatch.setenv(
            "UA_AGENT_TOKENS", f"Desktop_Person:{HUMAN_TOKEN},Desktop_Agent1:{AGENT_TOKEN}"
        )
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", ISSUER)
        monkeypatch.setenv("UA_MCP_OAUTH_APPROVER_LOGINS", LOGIN)
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app, client=("127.0.0.1", 50000)) as c:
            yield c

    @staticmethod
    def _h(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def test_human_on_approver_node_registers(self, ui, audit):
        options = ui.post("/api/oauth/passkeys/options", headers=self._h(HUMAN_TOKEN), json={})
        assert options.status_code == 200, options.text
        body = options.json()
        assert body["rp"]["id"] == RP_ID
        assert body["authenticatorSelection"]["userVerification"] == "required"
        auth = Authenticator()
        resp = ui.post(
            "/api/oauth/passkeys",
            headers=self._h(HUMAN_TOKEN),
            json={**auth.registration(body["challenge"]), "label": "Touch ID"},
        )
        assert resp.status_code == 200, resp.text
        listed = ui.get("/api/oauth/passkeys", headers=self._h(HUMAN_TOKEN)).json()
        assert listed["this_device_login"] == LOGIN
        assert [p["label"] for p in listed["passkeys"]] == ["Touch ID"]
        assert "public_key_spki" not in listed["passkeys"][0]
        assert any(c == "hub.oauth.passkey_registered" for c, _ in audit)

        deleted = ui.post(
            "/api/oauth/passkeys/delete",
            headers=self._h(HUMAN_TOKEN),
            json={"credential_id": b64u(auth.cred_id)},
        )
        assert deleted.status_code == 200
        assert passkeys.list_passkeys() == []

    def test_get_reports_loopback_peer_without_relaxing_registration_gate(self, ui, peer):
        peer["node"] = None
        listed = ui.get("/api/oauth/passkeys", headers=self._h(HUMAN_TOKEN))
        assert listed.status_code == 200
        assert listed.json()["peer_is_loopback"] is True
        assert listed.json()["this_device_login"] is None

        options = ui.post("/api/oauth/passkeys/options", headers=self._h(HUMAN_TOKEN), json={})
        assert options.status_code == 403

    def test_cannot_delete_another_logins_passkey(self, ui, monkeypatch):
        """Devin #776: deletion is scoped to the caller's own login."""
        monkeypatch.setenv("UA_MCP_OAUTH_APPROVER_LOGINS", f"{LOGIN},other@example.com")
        other = Authenticator()
        _register(other, login="other@example.com")
        resp = ui.post(
            "/api/oauth/passkeys/delete",
            headers=self._h(HUMAN_TOKEN),
            json={"credential_id": b64u(other.cred_id)},
        )
        assert resp.status_code == 404
        assert [p["login"] for p in passkeys.list_passkeys()] == ["other@example.com"]

    def test_agent_identity_cannot_register(self, ui):
        resp = ui.post("/api/oauth/passkeys/options", headers=self._h(AGENT_TOKEN), json={})
        assert resp.status_code == 403

    def test_human_on_foreign_node_cannot_register(self, ui, peer):
        peer["node"] = dict(OTHER_NODE)
        resp = ui.post("/api/oauth/passkeys/options", headers=self._h(HUMAN_TOKEN), json={})
        assert resp.status_code == 403

    def test_anonymous_cannot_register(self, ui):
        assert ui.post("/api/oauth/passkeys/options", json={}).status_code == 401

    @pytest.mark.parametrize("path", ["/api/oauth/passkeys", "/api/oauth/passkeys/delete"])
    def test_passkey_mutation_body_is_bounded(self, ui, path):
        headers = self._h(HUMAN_TOKEN)
        oversized = b"{" + b" " * (128 * 1024) + b"}"
        resp = ui.post(path, headers=headers, content=oversized)
        assert resp.status_code == 413
        assert "exceeds limit" in resp.json()["error"]

        def chunks():
            yield b"{"
            yield b" " * (128 * 1024)
            yield b"}"

        resp = ui.post(path, headers=headers, content=chunks())
        assert resp.status_code == 413

    def test_registration_rejects_wrong_challenge_and_origin(self, ui):
        auth = Authenticator()
        ui.post("/api/oauth/passkeys/options", headers=self._h(HUMAN_TOKEN), json={})
        bogus = ui.post(
            "/api/oauth/passkeys",
            headers=self._h(HUMAN_TOKEN),
            json=auth.registration(b64u(secrets.token_bytes(32))),
        )
        assert bogus.status_code == 400
        challenge = ui.post(
            "/api/oauth/passkeys/options", headers=self._h(HUMAN_TOKEN), json={}
        ).json()["challenge"]
        reg = auth.registration(challenge)
        reg["client_data_json"] = b64u(
            Authenticator.client_data("webauthn.create", challenge, "https://evil.test")
        )
        assert (
            ui.post("/api/oauth/passkeys", headers=self._h(HUMAN_TOKEN), json=reg).status_code
            == 400
        )
        assert passkeys.list_passkeys() == []
