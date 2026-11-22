"""Control Center sign-in through WorkOS AuthKit (task #2114, epic #2107).

The hub's real WorkOS leg (`WorkOSHubAuth`) and the real middleware, routes and
approval store run here. Only the WorkOS SDK is a fake: `FakeWorkOS` stands in
for `WorkOSSDKProvider` at its seams (authorization URL, code exchange, sealed
session verification, live membership, refresh), and its sealed sessions are
real Fernet tokens under the configured cookie key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.testclient import TestClient
from workos.session import seal_data, unseal_data

from unified_agent import config, server, storage, webui, workos_hub_auth
from unified_agent.audit import producer_sign
from unified_agent.pilot_auth import PilotAuthError, WorkOSClaims
from unified_agent.tools.approvals import ApprovalPendingError, check_approval_gate
from unified_agent.webui import auth, workos_signin

ORG = "org-a"
LABEL = "Desktop_Person"
ISSUER = "https://hub.example.test"
CALLBACK = f"{ISSUER}/api/session/workos/callback"
BEARER = f"tok-{secrets.token_urlsafe(8)}"


class FakeWorkOS:
    """The WorkOS SDK provider, as the hub leg and the sign-in routes use it."""

    organization_id = ORG

    def __init__(self) -> None:
        self.cookie_password = Fernet.generate_key().decode()
        self.members = {"user-a", "user-unmapped"}
        self.access_ttl = 300
        self.authorize_calls: list[dict] = []
        self.exchanges: list[tuple[str, str | None]] = []
        self.active_calls = 0
        self.refreshes = 0
        self.client = SimpleNamespace(
            user_management=SimpleNamespace(
                get_authorization_url=self._authorization_url,
                authenticate_with_code=self._authenticate_with_code,
            )
        )

    # -- SDK client seams used by the routes
    def _authorization_url(self, **kwargs) -> str:
        self.authorize_calls.append(kwargs)
        return "https://auth.example.test/authorize?state=" + kwargs["state"]

    def _authenticate_with_code(self, *, code: str, code_verifier: str | None = None):
        self.exchanges.append((code, code_verifier))
        user = code.split(":", 1)[0]
        data = {
            "user": {"id": user},
            "organization_id": ORG,
            "access_token": f"at-{secrets.token_hex(4)}",
            "refresh_token": "refresh-0",
            "exp": int(time.time()) + self.access_ttl,
            "sid": f"session-{user}",
        }
        return SimpleNamespace(to_dict=lambda: data)

    # -- WorkOSSDKProvider seams used by WorkOSHubAuth
    def _data(self, sealed: str) -> dict:
        return unseal_data(sealed, self.cookie_password)

    def authenticate_sealed(self, sealed: str) -> WorkOSClaims:
        data = self._data(sealed)
        if data["exp"] <= time.time():
            raise PilotAuthError("expired")
        return WorkOSClaims(data["user"]["id"], ORG, session_id=data["sid"], expires_at=data["exp"])

    def active(self, claims: WorkOSClaims) -> bool:
        self.active_calls += 1
        return claims.subject in self.members

    def sealed_expires_at(self, sealed: str) -> int:
        return int(self._data(sealed)["exp"])

    def refresh_sealed(self, sealed: str) -> str:
        data = self._data(sealed)
        if data["user"]["id"] not in self.members:
            raise PilotAuthError("refresh denied")
        self.refreshes += 1
        data["exp"] = int(time.time()) + self.access_ttl
        data["refresh_token"] = f"refresh-{self.refreshes}"
        return seal_data(data, self.cookie_password)


@pytest.fixture
def workos(monkeypatch):
    fake = FakeWorkOS()
    leg = workos_hub_auth.WorkOSHubAuth(
        fake,
        organization_id=ORG,
        identity_map={"user-a": LABEL, "user-outsider": LABEL},
    )
    monkeypatch.setattr(workos_hub_auth, "enabled", lambda: True)
    monkeypatch.setattr(workos_hub_auth, "hub_auth", lambda: leg)
    # The producer stamp re-reads the leg's own settings at stamp time.
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", ORG)
    monkeypatch.setenv("UA_HUB_WORKOS_IDENTITY_MAP", json.dumps(leg.identity_map))
    monkeypatch.setenv("WORKOS_COOKIE_PASSWORD", fake.cookie_password)
    fake.leg = leg
    return fake


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent2:{BEARER}")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*,testserver")
    monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    monkeypatch.delenv("UA_PUBLIC_URL", raising=False)
    db_file = tmp_path / "memory.db"
    monkeypatch.setattr(config, "db_path", lambda: db_file)
    storage.init_db()
    workos_signin.reset_state()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    # https: the cookies are Secure because the configured origin is https.
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        yield c
    workos_signin.reset_state()


def _start(client) -> tuple[str, str, dict]:
    """GET the login route; returns (state, state cookie name, authorize kwargs)."""
    res = client.get(workos_signin.LOGIN_PATH)
    assert res.status_code == 302, res.text
    state = parse_qs(urlsplit(res.headers["location"]).query)["state"][0]
    nonce = workos_signin.state_nonce(state)
    return state, workos_signin.state_cookie_name(nonce), res


def _sign_in(client, workos, user: str = "user-a"):
    state, _cookie, _ = _start(client)
    return client.get(workos_signin.CALLBACK_PATH, params={"state": state, "code": f"{user}:c"})


def _ui_cookie(response) -> str | None:
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(f"{auth.COOKIE_NAME}=") and "Max-Age=0" not in header:
            return header.split(";", 1)[0].split("=", 1)[1]
    return None


def _workos_rows():
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM ui_session_workos").fetchall()
    finally:
        conn.close()


# -- discovery -----------------------------------------------------------------------


def test_options_advertise_workos_only_when_it_can_run(client, workos, monkeypatch):
    assert client.get("/api/session/options").json() == {"workos": True}
    monkeypatch.delenv("UA_MCP_OAUTH_ISSUER")
    # No configured origin: the callback URL cannot be built, so it is not offered.
    assert client.get("/api/session/options").json() == {"workos": False}
    monkeypatch.setenv("UA_PUBLIC_URL", "https://railway.example.test")
    assert client.get("/api/session/options").json() == {"workos": True}
    monkeypatch.setattr(workos_hub_auth, "enabled", lambda: False)
    assert client.get("/api/session/options").json() == {"workos": False}


def test_options_without_the_leg_and_login_404s(client):
    assert client.get("/api/session/options").json() == {"workos": False}
    assert client.get(workos_signin.LOGIN_PATH).status_code == 404


# -- login redirect --------------------------------------------------------------------


def test_login_redirects_to_authkit_for_the_org_with_state_and_pkce(client, workos):
    state, cookie_name, res = _start(client)
    sent = workos.authorize_calls[-1]
    assert sent["provider"] == "authkit"
    assert sent["organization_id"] == ORG
    assert sent["redirect_uri"] == CALLBACK
    assert sent["state"] == state
    assert sent["code_challenge_method"] == "S256"
    cookie = next(h for h in res.headers.get_list("set-cookie") if h.startswith(cookie_name))
    assert "HttpOnly" in cookie and "Secure" in cookie
    assert "Path=/api/session/workos" in cookie and "samesite=lax" in cookie.lower()
    nonce, verifier = cookie.split(";", 1)[0].split("=", 1)[1].split(".", 1)
    assert state.startswith(nonce + ".")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert sent["code_challenge"] == challenge.rstrip(b"=").decode()
    # The verifier itself never goes to AuthKit.
    assert verifier not in res.headers["location"]


def test_callback_origin_comes_from_configuration_not_the_host_header(client, workos, monkeypatch):
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*,testserver,attacker.example")
    client.get(workos_signin.LOGIN_PATH, headers={"host": "attacker.example"})
    assert workos.authorize_calls[-1]["redirect_uri"] == CALLBACK
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*,testserver")
    assert (
        client.get(workos_signin.LOGIN_PATH, headers={"host": "attacker.example"}).status_code
        == 403
    )


# -- callback ------------------------------------------------------------------------


def test_a_member_signs_in_and_gets_a_normal_session(client, workos):
    res = _sign_in(client, workos)
    assert res.status_code == 302, res.text
    assert res.headers["location"] == "/ui"
    session_cookie = _ui_cookie(res)
    assert session_cookie
    header = next(h for h in res.headers.get_list("set-cookie") if h.startswith(auth.COOKIE_NAME))
    assert "HttpOnly" in header and "SameSite=Strict" in header and "Secure" in header
    # The code was exchanged with the PKCE verifier from the browser cookie.
    code, verifier = workos.exchanges[-1]
    assert code == "user-a:c" and verifier and len(verifier) >= 43

    who = client.get("/api/whoami")
    assert who.status_code == 200
    assert who.json()["agent"] == LABEL
    assert who.json()["csrf"]

    rows = _workos_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == hashlib.sha256(session_cookie.encode()).hexdigest()
    assert row["workos_user_id"] == "user-a"
    assert row["workos_organization_id"] == ORG
    assert row["workos_session_id"] == "session-user-a"
    # Stored sealed (Fernet), never as a plain token.
    assert unseal_data(row["sealed"], workos.cookie_password)["user"]["id"] == "user-a"
    assert "refresh-0" not in row["sealed"]


@pytest.mark.parametrize(
    "user, why",
    [("user-outsider", "mapped, but not a member"), ("user-unmapped", "member, not mapped")],
)
def test_non_members_and_unmapped_users_get_no_session(client, workos, user, why):
    res = _sign_in(client, workos, user)
    assert res.status_code == 403, why
    assert _ui_cookie(res) is None, why
    assert _workos_rows() == []
    client.cookies.clear()
    assert client.get("/api/whoami").status_code == 401


def test_a_different_organization_gets_no_session(client, workos, monkeypatch):
    # The SDK's code exchange has no organization_id argument. Authorization
    # scopes the login, and the verified token must still name our organization.
    monkeypatch.setattr(
        workos,
        "authenticate_sealed",
        lambda sealed: WorkOSClaims(
            "user-a", "org-other", session_id="session-user-a", expires_at=int(time.time()) + 300
        ),
    )
    res = _sign_in(client, workos)
    assert res.status_code == 403
    assert _ui_cookie(res) is None
    assert _workos_rows() == []


@pytest.mark.parametrize("hosted_setting", ["UA_HOSTED", "RAILWAY_ENVIRONMENT_ID"])
def test_hosted_approvals_never_fall_back_to_a_passphrase(monkeypatch, hosted_setting):
    from starlette.requests import Request

    from unified_agent import oauth_approver

    monkeypatch.setenv(hosted_setting, "1")
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
    passphrase = secrets.token_urlsafe(32)
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", passphrase)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/oauth/device",
            "headers": [],
            "client": ("100.64.0.2", 12345),
        }
    )
    with pytest.raises(oauth_approver.ApprovalRefused, match="Passphrase approval is disabled"):
        oauth_approver.verify(request, {"passphrase": passphrase}, binding="device-code")


def test_a_tampered_state_is_refused(client, workos):
    state, cookie_name, _ = _start(client)
    nonce, expires, mac = state.split(".")
    for forged in (
        f"{nonce}.{int(expires) + 3600}.{mac}",  # extended expiry
        f"{nonce}.{expires}.{mac[:-2]}AA",  # broken MAC
        f"{secrets.token_urlsafe(24)}.{expires}.{mac}",  # another nonce
    ):
        res = client.get(workos_signin.CALLBACK_PATH, params={"state": forged, "code": "user-a:c"})
        assert res.status_code == 400, forged
    assert workos.exchanges == []


def test_a_state_from_another_browser_is_refused_without_burning_it(client, workos):
    """A copied callback URL carries the nonce, but not the browser's PKCE verifier."""
    state, cookie_name, _ = _start(client)
    nonce = state.split(".")[0]
    real_cookie = client.cookies.get(cookie_name, path="/api/session/workos")
    for foreign in (f"{nonce}.{'x' * 64}", f"other.{'x' * 64}"):
        client.cookies.set(cookie_name, foreign, path="/api/session/workos")
        bad = client.get(workos_signin.CALLBACK_PATH, params={"state": state, "code": "user-a:c"})
        assert bad.status_code == 400, foreign
    assert workos.exchanges == []
    client.cookies.set(cookie_name, real_cookie, path="/api/session/workos")
    good = client.get(workos_signin.CALLBACK_PATH, params={"state": state, "code": "user-a:c"})
    assert good.status_code == 302


def test_a_state_is_single_use(client, workos):
    state, cookie_name, _ = _start(client)
    cookie = client.cookies.get(cookie_name, path="/api/session/workos")
    assert (
        client.get(
            workos_signin.CALLBACK_PATH, params={"state": state, "code": "user-a:c"}
        ).status_code
        == 302
    )
    client.cookies.set(cookie_name, cookie, path="/api/session/workos")
    replay = client.get(workos_signin.CALLBACK_PATH, params={"state": state, "code": "user-a:c"})
    assert replay.status_code == 400
    assert len(workos.exchanges) == 1


def test_an_expired_state_is_refused(workos):
    state, nonce, verifier = workos_signin.issue_state(workos.cookie_password, now=0)
    assert workos_signin.consume_state(workos.cookie_password, state, f"{nonce}.{verifier}") is None
    fresh, nonce, verifier = workos_signin.issue_state(workos.cookie_password)
    assert (
        workos_signin.consume_state(workos.cookie_password, fresh, f"{nonce}.{verifier}")
        == verifier
    )


@pytest.mark.parametrize("malformed", ["state", "cookie"])
def test_non_ascii_login_state_is_refused_without_an_exception(malformed):
    password = Fernet.generate_key().decode()
    state, nonce, verifier = workos_signin.issue_state(password)
    cookie = f"{nonce}.{verifier}"
    if malformed == "state":
        state = state.rsplit(".", 1)[0] + ".\N{SNOWMAN}"
    else:
        cookie = f"\N{SNOWMAN}.{verifier}"
    assert workos_signin.consume_state(password, state, cookie) is None


def test_a_state_survives_a_restart_because_it_is_signed(workos):
    """Nothing about a pending sign-in lives in process memory."""
    state, nonce, verifier = workos_signin.issue_state(workos.cookie_password)
    workos_signin.reset_state()  # what a restart loses
    assert (
        workos_signin.consume_state(workos.cookie_password, state, f"{nonce}.{verifier}")
        == verifier
    )
    # A different cookie key (another hub) cannot validate it.
    assert (
        workos_signin.consume_state(Fernet.generate_key().decode(), state, f"{nonce}.{verifier}")
        is None
    )


def test_an_authkit_error_is_not_echoed(client, workos):
    state, _cookie, _ = _start(client)
    res = client.get(
        workos_signin.CALLBACK_PATH,
        params={"state": state, "error": "access_denied", "error_description": "<script>"},
    )
    assert res.status_code == 400
    assert "<script>" not in res.text


# -- the session is an IdP-verified human ----------------------------------------------


def _pending_approval() -> int:
    with pytest.raises(ApprovalPendingError):
        check_approval_gate("run_cmd", {"command": "echo 2114"}, requested_by="Desktop_Agent2")
    conn = storage.connect()
    try:
        return conn.execute("SELECT id FROM approvals WHERE status='pending'").fetchone()["id"]
    finally:
        conn.close()


def _approve(client, approval_id: int):
    csrf = client.get("/api/whoami").json()["csrf"]
    return client.post(
        "/api/action/approval",
        json={"id": approval_id, "decision": "approve"},
        headers={"x-ua-csrf": csrf},
    )


def _producer_rows():
    conn = storage.connect()
    try:
        return [
            json.loads(r["payload"])
            for r in conn.execute(
                "SELECT payload FROM event_log WHERE channel=?",
                (producer_sign.CHANNEL_PRODUCER_AUTHENTICATED,),
            ).fetchall()
        ]
    finally:
        conn.close()


def test_a_workos_session_approves_as_an_idp_verified_human(client, workos):
    assert _sign_in(client, workos).status_code == 302
    approval_id = _pending_approval()
    checks_before = workos.active_calls
    res = _approve(client, approval_id)
    assert res.status_code == 200, res.text
    # A write re-checks membership live, even right after sign-in.
    assert workos.active_calls > checks_before

    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "approved"
    assert row["resolved_by"] == LABEL
    stamps = [p for p in _producer_rows() if p.get("ref_id") == str(approval_id)]
    assert stamps, "approval_resolve must stamp a producer event"
    assert stamps[0]["scheme"] == producer_sign.SCHEME_WORKOS_IDP_V1
    assert stamps[0]["workos_subject"] == "user-a"
    assert stamps[0]["workos_organization_id"] == ORG
    assert producer_sign.verify_producer_signature(stamps[0]) is True


def test_a_bearer_session_is_unchanged_and_never_idp_verified(client, workos):
    res = client.post("/api/session", json={"token": BEARER})
    assert res.status_code == 200
    who = client.get("/api/whoami").json()
    assert who["agent"] == "Desktop_Agent2"
    assert _workos_rows() == []
    status, facts = workos_signin.verify_ui_session(
        client.cookies.get(auth.COOKIE_NAME), identity="Desktop_Agent2", method="POST"
    )
    assert (status, facts) == (workos_signin.NOT_WORKOS, None)


# -- revocation and refresh ------------------------------------------------------------


def test_revoked_membership_stops_the_session(client, workos, monkeypatch):
    assert _sign_in(client, workos).status_code == 302
    approval_id = _pending_approval()
    assert client.get("/api/whoami").status_code == 200
    workos.members.discard("user-a")
    # A write is re-checked at once: the approval is refused.
    assert _approve(client, approval_id).status_code == 401
    # A read may ride the last check for at most RECHECK_S.
    monkeypatch.setattr(workos_signin, "RECHECK_S", 0)
    assert client.get("/api/whoami").status_code == 401
    conn = storage.connect()
    try:
        assert (
            conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()[
                "status"
            ]
            == "pending"
        )
    finally:
        conn.close()


def test_a_changed_identity_map_ends_the_session(client, workos):
    assert _sign_in(client, workos).status_code == 302
    workos.leg.identity_map["user-a"] = "Desktop_Agent1"
    assert client.get("/api/whoami").status_code == 401
    # Ended for good, not just for this request.
    workos.leg.identity_map["user-a"] = LABEL
    assert client.get("/api/whoami").status_code == 401


def test_turning_the_leg_off_ends_workos_sessions(client, workos, monkeypatch):
    assert _sign_in(client, workos).status_code == 302
    monkeypatch.setattr(workos_hub_auth, "enabled", lambda: False)
    assert client.get("/api/whoami").status_code == 401


def test_an_expiring_access_token_is_refreshed_and_stored(client, workos):
    workos.access_ttl = 10  # inside the refresh skew: the next check refreshes
    assert _sign_in(client, workos).status_code == 302
    before = _workos_rows()[0]["sealed"]
    workos.access_ttl = 300
    assert client.get("/api/whoami").status_code == 200
    assert workos.refreshes == 1
    row = _workos_rows()[0]
    assert row["sealed"] != before
    assert unseal_data(row["sealed"], workos.cookie_password)["refresh_token"] == "refresh-1"
    assert row["access_expires_at"] > time.time() + 200
    # Fresh again: reads ride the stored check, with no further refresh.
    assert client.get("/api/whoami").status_code == 200
    assert workos.refreshes == 1
    # An approval after the refresh is still IdP-verified.
    approval_id = _pending_approval()
    assert _approve(client, approval_id).status_code == 200
    assert any(
        p.get("ref_id") == str(approval_id) and p["scheme"] == producer_sign.SCHEME_WORKOS_IDP_V1
        for p in _producer_rows()
    )


def _csrf(client) -> dict[str, str]:
    """The CSRF header of the session the client's cookie names (sign-out needs it)."""
    return {auth.CSRF_HEADER: client.get("/api/whoami").json()["csrf"]}


def test_a_denied_refresh_denies_the_request(client, workos):
    workos.access_ttl = 10
    assert _sign_in(client, workos).status_code == 302
    workos.members.discard("user-a")
    assert client.get("/api/whoami").status_code == 401
    assert workos.refreshes == 0


def test_sign_out_erases_the_credential_and_retains_the_workos_kind(client, workos):
    assert _sign_in(client, workos).status_code == 302
    assert len(_workos_rows()) == 1
    assert client.delete("/api/session", headers=_csrf(client)).status_code == 200
    # The credential is gone; the authentication-kind tombstone remains so
    # an in-flight request cannot downgrade this cookie to a bearer session.
    assert len(_workos_rows()) == 1
    assert _workos_rows()[0]["sealed"] == ""


@pytest.mark.parametrize("action", ["logout", "mapping_removed"])
def test_a_failed_cookie_revocation_cannot_remove_its_workos_binding(
    client, workos, monkeypatch, action
):
    assert _sign_in(client, workos).status_code == 302
    copied_cookie = client.cookies.get(auth.COOKIE_NAME)
    monkeypatch.setattr(auth, "revoke_session", lambda session_id: False)
    if action == "logout":
        assert client.delete("/api/session", headers=_csrf(client)).status_code == 200
        client.cookies.set(auth.COOKIE_NAME, copied_cookie, path="/")
    else:
        workos.leg.identity_map.pop("user-a")
        assert client.get("/api/whoami").status_code == 401
        workos.leg.identity_map["user-a"] = LABEL
    # If the ordinary cookie cannot be revoked, keep its WorkOS binding. A
    # later request must still check membership, rather than become bearer-like.
    assert len(_workos_rows()) == 1
    workos.members.discard("user-a")
    monkeypatch.setattr(workos_signin, "RECHECK_S", 0)
    assert client.get("/api/whoami").status_code == 401


def test_logout_between_cookie_resolution_and_workos_check_cannot_downgrade_approval(
    client, workos, monkeypatch
):
    assert _sign_in(client, workos).status_code == 302
    csrf = client.get("/api/whoami").json()["csrf"]
    approval_id = _pending_approval()
    original = auth.resolve_session
    revoked = []

    def resolve_then_logout(cookie):
        session = original(cookie)
        if session and not revoked:
            revoked.append(True)
            workos.members.discard("user-a")
            assert auth.revoke_session(cookie)
            workos_signin.forget(cookie)
        return session

    monkeypatch.setattr(auth, "resolve_session", resolve_then_logout)
    response = client.post(
        "/api/action/approval",
        json={"id": approval_id, "decision": "approve"},
        headers={"x-ua-csrf": csrf},
    )
    assert response.status_code == 401
    conn = storage.connect()
    try:
        assert (
            conn.execute("SELECT status FROM approvals WHERE id=?", (approval_id,)).fetchone()[
                "status"
            ]
            == "pending"
        )
    finally:
        conn.close()
    assert not any(p.get("ref_id") == str(approval_id) for p in _producer_rows())


def test_forget_during_workos_refresh_cannot_restore_the_session(client, workos, monkeypatch):
    workos.access_ttl = 10
    assert _sign_in(client, workos).status_code == 302
    cookie = client.cookies.get(auth.COOKIE_NAME)
    original = workos.leg.resolve_refreshing

    def resolve_then_forget(sealed):
        facts, replacement = original(sealed)
        assert facts and replacement
        assert auth.revoke_session(cookie)
        workos_signin.forget(cookie)
        return facts, replacement

    monkeypatch.setattr(workos.leg, "resolve_refreshing", resolve_then_forget)
    workos.access_ttl = 300
    assert client.get("/api/whoami").status_code == 401
    marker = _workos_rows()[0]
    assert marker["sealed"] == ""
    assert marker["access_expires_at"] == 0
