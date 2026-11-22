import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import SimpleNamespace

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa

from unified_agent.pilot_auth import PilotAuth, PilotAuthError, WorkOSClaims, WorkOSSDKProvider


class Provider:
    enabled = True
    outage = False

    def __init__(self):
        self.claims = WorkOSClaims(
            "user-a", "org-a", session_id="session-a", expires_at=int(time.time()) + 300
        )

    def authorization_url(self, state):
        return "https://auth.example/authorize?state=" + state

    def authenticate(self, code):
        return self.claims

    def active(self, claims):
        if self.outage:
            raise ValueError("provider secret response must never escape")
        return self.enabled

    def logout_url(self, session_id):
        return "https://auth.example/logout?session_id=" + session_id

    def lifecycle(self, body, signature):
        if signature != "verified-fixture":
            raise ValueError("invalid signature")
        return {"event_id": "event-a", "subject": "user-a"}


def make_auth(tmp_path, provider=None):
    return PilotAuth(
        tmp_path / "pilot.sqlite", "org-a", {"user-a": "pilot-reader"}, provider or Provider()
    )


def test_state_expiry_replay_and_session_expiry(tmp_path):
    auth = make_auth(tmp_path)
    state = auth.begin_login()
    with auth._connect() as conn:
        conn.execute("UPDATE pilot_states SET expires_at='2000-01-01'")
    with pytest.raises(PilotAuthError):
        auth.login("code", state)
    state = auth.begin_login()
    session = auth.login("code", state)
    assert auth.resolve(session["id"])
    with pytest.raises(PilotAuthError):
        auth.login("code", state)
    with auth._connect() as conn:
        conn.execute("UPDATE pilot_sessions SET expires_at=1")
    assert auth.resolve(session["id"]) is None


@pytest.mark.parametrize(
    "change",
    [
        {"organization_id": "org-b"},
        {"organization_id": None},
        {"subject": "unmapped"},
        {"expires_at": 1},
        {"expires_at": None},
        {"session_id": None},
        {"membership_status": "inactive"},
    ],
)
def test_claims_deny(tmp_path, change):
    provider = Provider()
    provider.claims = replace(provider.claims, **change)
    auth = make_auth(tmp_path, provider)
    with pytest.raises(PilotAuthError):
        auth.login("code", auth.begin_login())


def test_login_logs_the_cause_but_keeps_the_denial_generic(tmp_path, caplog):
    provider = Provider()
    provider.outage = True
    auth = make_auth(tmp_path, provider)
    with caplog.at_level("WARNING", logger="unified_agent.pilot_auth"):
        with pytest.raises(PilotAuthError) as raised:
            auth.login("code", auth.begin_login())
    assert str(raised.value) == "WorkOS validation unavailable or denied"
    # The class is recorded; the provider's own message is not, because it may
    # carry a response body or credential (Devin review, PR #671).
    assert "ValueError" in caplog.text
    assert "provider secret response" not in caplog.text
    assert "<provider detail withheld>" in caplog.text


def test_login_logs_the_message_for_sdk_drift_errors(tmp_path, caplog):
    provider = Provider()
    provider.active = lambda claims: (_ for _ in ()).throw(
        TypeError("unexpected keyword argument 'user_id'")
    )
    auth = make_auth(tmp_path, provider)
    with caplog.at_level("WARNING", logger="unified_agent.pilot_auth"):
        with pytest.raises(PilotAuthError):
            auth.login("code", auth.begin_login())
    assert "TypeError" in caplog.text and "unexpected keyword argument 'user_id'" in caplog.text


def test_provider_revocation_outage_mapping_change_and_events(tmp_path):
    provider = Provider()
    auth = make_auth(tmp_path, provider)
    session = auth.login("code", auth.begin_login())
    provider.enabled = False
    assert auth.resolve(session["id"]) is None
    provider.enabled = True
    provider.outage = True
    assert auth.resolve(session["id"]) is None
    provider.outage = False
    auth.identity_map.clear()
    assert auth.resolve(session["id"]) is None
    auth.identity_map["user-a"] = "pilot-reader"
    auth.apply_lifecycle(event_id="new", subject="user-a")
    assert auth.resolve(session["id"]) is None
    newer = auth.login("code", auth.begin_login())
    auth.apply_lifecycle(event_id="new", subject="user-a")
    assert auth.resolve(newer["id"])  # duplicate does not revoke a newly validated login
    auth.apply_lifecycle(event_id="old", subject="user-a")
    assert auth.resolve(newer["id"]) is None  # delayed notification cannot grant access


def test_store_cannot_be_reused_by_other_org_or_private_store(tmp_path, monkeypatch):
    auth = make_auth(tmp_path)
    auth.login("code", auth.begin_login())
    with pytest.raises(PilotAuthError, match="another organization"):
        PilotAuth(auth.db_path, "org-b", {"user-a": "pilot-reader"}, Provider())
    private = tmp_path / "private"
    monkeypatch.setenv("UA_PILOT_PRIVATE_HOME", str(private))
    with pytest.raises(ValueError):
        make_auth(private)
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(private / "memory.db")
    with pytest.raises(ValueError):
        PilotAuth(alias, "org-a", {"user-a": "pilot-reader"}, Provider())


def sdk_provider(overrides=None):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = {
        "iss": "https://api.workos.com/",
        "sub": "user-a",
        "sid": "session-a",
        "org_id": "org-a",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    claims.update(overrides or {})
    claims = {k: v for k, v in claims.items() if v is not None}
    token = jwt.encode(claims, key, algorithm="RS256")
    session = SimpleNamespace(
        session_data="sealed",
        authenticate=lambda: SimpleNamespace(
            authenticated=True,
            # WorkOS v10 returns `user` as a plain dict; the fixture must
            # exercise that shape, not the old SDK object (Devin, PR #616).
            user={"id": "user-a"},
            session_id="session-a",
            organization_id="org-a",
        ),
        unseal_data=lambda *a: {"access_token": token},
        jwks=SimpleNamespace(
            get_signing_key_from_jwt=lambda t: SimpleNamespace(key=key.public_key())
        ),
    )
    client = SimpleNamespace(
        client_id="client-a",
        user_management=SimpleNamespace(
            authenticate_with_code=lambda *, code: SimpleNamespace(
                to_dict=lambda: {"access_token": token, "user": {"id": "user-a"}}
            ),
            load_sealed_session=lambda *, session_data, cookie_password: session,
        ),
        organization_membership=SimpleNamespace(),
    )
    return WorkOSSDKProvider(
        client,
        redirect_uri="https://pilot.example/pilot/callback",
        cookie_password=Fernet.generate_key().decode(),
        organization_id="org-a",
    )


def test_production_adapter_validates_signed_claims():
    assert sdk_provider().authenticate("code").organization_id == "org-a"
    assert sdk_provider({"aud": ["client-a"]}).authenticate("code").organization_id == "org-a"


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://attacker.example/"},
        {"exp": 1},
        {"exp": None},
        {"iat": None},
        {"org_id": "org-b"},
        {"org_id": None},
        {"sub": "other"},
        {"sid": "other"},
        {"aud": "other-client"},
        {"aud": ["other-client"]},
    ],
)
def test_production_adapter_rejects_signed_bad_claims(claims):
    with pytest.raises((jwt.InvalidTokenError, PilotAuthError)):
        sdk_provider(claims).authenticate("code")


def test_production_adapter_checks_signature_and_provider_status():
    provider = sdk_provider()
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    session = provider.client.user_management.load_sealed_session(
        session_data="sealed", cookie_password=provider.cookie_password
    )
    session.jwks.get_signing_key_from_jwt = lambda _: SimpleNamespace(key=other_key.public_key())
    with pytest.raises(jwt.InvalidSignatureError):
        provider.authenticate("code")
    memberships = [SimpleNamespace(user_id="user-a", organization_id="org-a", status="active")]
    sessions = [sdk_session()]
    provider.client.organization_membership.list_organization_memberships = lambda **kw: (
        SimpleNamespace(auto_paging_iter=lambda: iter(memberships))
    )
    provider.client.user_management.list_sessions = lambda **kw: SimpleNamespace(
        auto_paging_iter=lambda: iter(sessions)
    )
    assert provider.active(Provider().claims)
    sessions[0].status = SessionStatus.REVOKED
    assert not provider.active(Provider().claims)
    sessions[0].status = SessionStatus.ACTIVE
    memberships[0].status = "inactive"
    assert not provider.active(Provider().claims)


# The stubs above accept any keyword and hand back a string `expires_at`, so
# they stayed green while no live login could complete at all. These mirror the
# pinned SDK (workos 10.4.0) exactly: `list_sessions` names the user id `id`,
# and the session it yields carries a real `datetime` and a str-subclass status
# enum (task #1243).
class SessionStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


def sdk_session(**overrides):
    fields = {
        "id": "session-a",
        "user_id": "user-a",
        "organization_id": "org-a",
        "status": SessionStatus.ACTIVE,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    return SimpleNamespace(**(fields | overrides))


def sdk_10_4_provider(session, membership_status="active"):
    provider = sdk_provider()
    memberships = [
        SimpleNamespace(user_id="user-a", organization_id="org-a", status=membership_status)
    ]
    provider.client.organization_membership.list_organization_memberships = lambda **kw: (
        SimpleNamespace(auto_paging_iter=lambda: iter(memberships))
    )

    def list_sessions(*, id, limit=10, before=None, after=None, order=None, request_options=None):
        assert id, "SDK 10.4.0 takes the user id as `id`, not `user_id`"
        return SimpleNamespace(auto_paging_iter=lambda: iter([session]))

    provider.client.user_management.list_sessions = list_sessions
    return provider


def test_active_accepts_a_live_session_on_the_pinned_sdk():
    assert sdk_10_4_provider(sdk_session()).active(Provider().claims)


@pytest.mark.parametrize(
    "session_overrides, claims_overrides, membership_status",
    [
        ({"user_id": "user-b"}, {}, "active"),
        ({"organization_id": "org-b"}, {}, "active"),
        ({"status": SessionStatus.REVOKED}, {}, "active"),
        ({"expires_at": datetime.now(UTC) - timedelta(seconds=1)}, {}, "active"),
        ({}, {"organization_id": "org-b"}, "active"),
        ({"id": "session-b"}, {}, "active"),
        ({}, {}, "inactive"),
    ],
)
def test_active_denies_bad_sessions_on_the_pinned_sdk(
    session_overrides, claims_overrides, membership_status
):
    provider = sdk_10_4_provider(sdk_session(**session_overrides), membership_status)
    assert not provider.active(replace(Provider().claims, **claims_overrides))


def test_real_sdk_webhook_verification():
    import hashlib
    import hmac
    import json

    from workos.webhooks import Webhooks

    provider = sdk_provider()
    provider.webhook_secret = "synthetic-test-signing-key"
    provider.client.webhooks = Webhooks(provider.client)
    body = json.dumps(
        {
            "id": "event-a",
            "event": "session.revoked",
            "data": {"organization_id": "org-a", "user_id": "user-a"},
        }
    ).encode()

    def signature(payload, offset=0):
        timestamp = str(int((time.time() + offset) * 1000))
        digest = hmac.new(
            provider.webhook_secret.encode(), timestamp.encode() + b"." + payload, hashlib.sha256
        ).hexdigest()
        return f"t={timestamp}, v1={digest}"

    valid = signature(body)
    assert provider.lifecycle(body, valid) == {"event_id": "event-a", "subject": "user-a"}
    for payload, header in [
        (body + b" ", valid),
        (body, signature(body, 300)),
        (body, signature(body, -300)),
        (body, "bad"),
    ]:
        with pytest.raises(ValueError):
            provider.lifecycle(payload, header)
    wrong_org = body.replace(b"org-a", b"org-b")
    with pytest.raises(PilotAuthError):
        provider.lifecycle(wrong_org, signature(wrong_org))
