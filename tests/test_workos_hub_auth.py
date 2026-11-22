"""Hub auth leg: a WorkOS AuthKit session mapped to a UA identity (task #1202)."""

import time
from types import SimpleNamespace

import pytest

from unified_agent import config, workos_hub_auth
from unified_agent.pilot_auth import WorkOSClaims


class FakeProvider:
    """Stands in for `WorkOSSDKProvider` at the same two seams the leg uses."""

    def __init__(self, claims, *, active=True, authenticate_error=None, active_error=None):
        self.organization_id = "org-a"
        self.claims = claims
        self._active = active
        self.authenticate_error = authenticate_error
        self.active_error = active_error
        self.active_calls = 0

    def authenticate_sealed(self, sealed_session):
        if self.authenticate_error:
            raise self.authenticate_error
        return self.claims

    def active(self, claims):
        self.active_calls += 1
        if self.active_error:
            raise self.active_error
        return self._active


def claims(**overrides):
    fields = {
        "subject": "user-a",
        "organization_id": "org-a",
        "membership_status": "active",
        "session_id": "session-a",
        "expires_at": int(time.time()) + 300,
    }
    return WorkOSClaims(**(fields | overrides))


def make_leg(provider=None, identity_map=None):
    return workos_hub_auth.WorkOSHubAuth(
        provider or FakeProvider(claims()),
        organization_id="org-a",
        identity_map=identity_map if identity_map is not None else {"user-a": "Desktop_Agent1"},
    )


def test_a_verified_session_resolves_to_the_mapped_identity():
    leg = make_leg()
    resolved = leg.resolve("sealed")
    assert resolved["identity"] == "Desktop_Agent1"
    assert resolved["subject"] == "user-a"
    assert resolved["organization_id"] == "org-a"
    assert resolved["session_id"] == "session-a"


def test_membership_is_rechecked_on_every_request():
    provider = FakeProvider(claims())
    leg = make_leg(provider)
    assert leg.resolve("sealed")
    assert leg.resolve("sealed")
    # Revocation must take effect on the next request, not at token expiry.
    assert provider.active_calls == 2


@pytest.mark.parametrize(
    "provider_kwargs, claims_overrides, identity_map, reason",
    [
        ({}, {"organization_id": "org-b"}, None, "wrong org"),
        ({"active": False}, {}, None, "revoked or inactive at the provider"),
        ({}, {"expires_at": int(time.time()) - 1}, None, "expired"),
        ({}, {"membership_status": "inactive"}, None, "membership not active"),
        ({}, {"session_id": None}, None, "no session id"),
        ({}, {"expires_at": None}, None, "no expiry"),
        ({}, {}, {"user-b": "Desktop_Agent1"}, "unmapped subject"),
        ({}, {}, {"user-a": "not a label!"}, "mapped to an unknown UA label"),
        ({"authenticate_error": ValueError("boom")}, {}, None, "provider outage on verify"),
        ({"active_error": ValueError("boom")}, {}, None, "provider outage on re-check"),
    ],
)
def test_denials(provider_kwargs, claims_overrides, identity_map, reason):
    provider = FakeProvider(claims(**claims_overrides), **provider_kwargs)
    assert make_leg(provider, identity_map).resolve("sealed") is None, reason


def test_provider_message_never_reaches_the_log(caplog):
    provider = FakeProvider(claims(), active_error=ValueError("provider secret response"))
    with caplog.at_level("WARNING", logger="unified_agent.workos_hub_auth"):
        assert make_leg(provider).resolve("sealed") is None
    assert "ValueError" in caplog.text
    assert "provider secret response" not in caplog.text


def test_empty_and_oversized_sessions_never_reach_the_provider():
    provider = FakeProvider(claims())
    leg = make_leg(provider)
    assert leg.resolve(None) is None
    assert leg.resolve("") is None
    assert leg.resolve("x" * (workos_hub_auth.MAX_SEALED_SESSION + 1)) is None
    assert provider.active_calls == 0


def test_workos_roles_and_domains_grant_nothing():
    """Provider-side authority attributes are not read and cannot leak through."""

    class AuthorityClaims(WorkOSClaims):
        pass

    decorated = AuthorityClaims(
        "user-a",
        "org-a",
        session_id="session-a",
        expires_at=int(time.time()) + 300,
    )
    # Attributes a provider might carry; none of them may influence the result.
    object.__setattr__(decorated, "role", "admin")
    object.__setattr__(decorated, "permissions", ["*"])
    object.__setattr__(decorated, "email", "someone@example.com")

    provider = FakeProvider(decorated)
    resolved = make_leg(provider, {"user-a": "Desktop_Agent1"}).resolve("sealed")
    assert resolved == {
        "identity": "Desktop_Agent1",
        "subject": "user-a",
        "organization_id": "org-a",
        "session_id": "session-a",
        "expires_at": decorated.expires_at,
    }
    # An admin role on the provider side does not survive an absent mapping.
    assert make_leg(FakeProvider(decorated), {"user-b": "Desktop_Agent1"}).resolve("x") is None


def test_leg_refuses_a_provider_bound_to_another_organization():
    provider = FakeProvider(claims())
    provider.organization_id = "org-b"
    with pytest.raises(ValueError):
        make_leg(provider)


@pytest.mark.parametrize("identity_map", [{}, {"user-a": ""}, {"": "Desktop_Agent1"}, {"a": 1}])
def test_leg_refuses_an_unusable_mapping(identity_map):
    with pytest.raises(ValueError):
        make_leg(None, identity_map)


@pytest.mark.parametrize(
    "header, expected",
    [
        ("WorkOS sealed-value", "sealed-value"),
        ("workos sealed-value", "sealed-value"),
        ("Bearer sealed-value", None),
        ("WorkOS", None),
        ("WorkOS   ", None),
        (None, None),
        ("", None),
    ],
)
def test_header_parsing(header, expected):
    assert workos_hub_auth.sealed_session_from_header(header) == expected


class TestGating:
    def setup_method(self):
        workos_hub_auth.reset_hub_auth_cache()

    teardown_method = setup_method

    def test_off_without_configuration(self, monkeypatch):
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
        assert not workos_hub_auth.enabled()
        assert workos_hub_auth.hub_auth() is None

    def test_off_in_core_edition_even_when_configured(self, monkeypatch):
        monkeypatch.setenv("UA_EDITION", "core")
        monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org-a")
        monkeypatch.setenv("UA_HUB_WORKOS_IDENTITY_MAP", '{"user-a": "Desktop_Agent1"}')
        assert not workos_hub_auth.enabled()
        assert workos_hub_auth.hub_auth() is None

    def test_off_without_a_usable_identity_map(self, monkeypatch):
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org-a")
        for raw in ["", "not json", "[]", "{}"]:
            monkeypatch.setenv("UA_HUB_WORKOS_IDENTITY_MAP", raw)
            workos_hub_auth.reset_hub_auth_cache()
            assert workos_hub_auth.build_hub_auth() is None

    def test_enabling_the_leg_without_enforcement_warns(self, monkeypatch, caplog):
        """An IdP login that gates nothing is a footgun; say so loudly."""
        import unified_agent.workos_hub_auth as mod

        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org-a")
        monkeypatch.setenv("UA_HUB_WORKOS_IDENTITY_MAP", '{"user-a": "Desktop_Agent1"}')
        monkeypatch.setattr(
            mod,
            "WorkOSSDKProvider",
            lambda *a, **kw: type("P", (), {"organization_id": "org-a"})(),
        )
        monkeypatch.setenv("WORKOS_API_KEY", "sk_test")
        monkeypatch.setenv("WORKOS_CLIENT_ID", "client_test")
        monkeypatch.setenv("WORKOS_ISSUER", "https://api.workos.com/user_management/client_test")
        monkeypatch.setenv("WORKOS_REDIRECT_URI", "https://pilot.example/pilot/callback")
        monkeypatch.setenv("WORKOS_COOKIE_PASSWORD", "x" * 44)

        monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
        with caplog.at_level("WARNING", logger="unified_agent.workos_hub_auth"):
            assert mod.build_hub_auth() is not None
        assert "UA_ENFORCE_AUTH" in caplog.text

        caplog.clear()
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        with caplog.at_level("WARNING", logger="unified_agent.workos_hub_auth"):
            assert mod.build_hub_auth() is not None
        assert "UA_ENFORCE_AUTH" not in caplog.text

    def test_missing_workos_credentials_leave_it_off_rather_than_raising(self, monkeypatch):
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org-a")
        monkeypatch.setenv("UA_HUB_WORKOS_IDENTITY_MAP", '{"user-a": "Desktop_Agent1"}')
        for name in ("WORKOS_API_KEY", "WORKOS_CLIENT_ID", "WORKOS_ISSUER"):
            monkeypatch.delenv(name, raising=False)
        assert workos_hub_auth.build_hub_auth() is None


def test_identity_binds_like_the_bearer_leg(monkeypatch):
    """A request resolved through this leg reports the mapped human identity."""
    leg = make_leg()
    resolved = leg.resolve("sealed")
    token = config.agent_identity_var.set(resolved["identity"])
    metadata = config.auth_metadata_var.set(
        {"auth_method": "workos", "scopes": [], "workos_user_id": resolved["subject"]}
    )
    try:
        assert config.verified_agent_identity() == "Desktop_Agent1"
        assert config.current_auth_metadata()["auth_method"] == "workos"
    finally:
        config.agent_identity_var.reset(token)
        config.auth_metadata_var.reset(metadata)


def test_unknown_labels_are_refused_by_the_same_roster_check():
    assert config.is_known_agent_label("Desktop_Agent1")
    assert not config.is_known_agent_label("not a label!")
    provider = FakeProvider(claims())
    assert make_leg(provider, {"user-a": "Desktop_NotInCatalog"}).resolve("sealed") is None


def test_end_to_end_through_the_real_provider_on_the_pinned_sdk():
    """The leg accepts a genuinely signed session through `WorkOSSDKProvider`.

    The fakes above pin the leg's own logic. This wires the real adapter in, so
    the pinned-SDK call shape (`list_sessions(id=...)`, a `datetime`
    `expires_at`) is exercised end to end — the combination that could not
    authenticate anyone before the fix in PR #671.
    """
    from test_pilot_auth import SessionStatus, sdk_provider, sdk_session
    from workos.session import seal_data

    provider = sdk_provider()
    memberships = [SimpleNamespace(user_id="user-a", organization_id="org-a", status="active")]
    sessions = [sdk_session()]
    provider.client.organization_membership.list_organization_memberships = lambda **kw: (
        SimpleNamespace(auto_paging_iter=lambda: iter(memberships))
    )

    def list_sessions(*, id, limit=10, before=None, after=None, order=None, request_options=None):
        assert id == "user-a"
        return SimpleNamespace(auto_paging_iter=lambda: iter(sessions))

    provider.client.user_management.list_sessions = list_sessions

    leg = workos_hub_auth.WorkOSHubAuth(
        provider, organization_id="org-a", identity_map={"user-a": "Desktop_Agent1"}
    )
    # A genuinely sealed session, produced the same way `authenticate` does.
    result = provider.client.user_management.authenticate_with_code(code="code")
    sealed = seal_data(result.to_dict(), provider.cookie_password)

    resolved = leg.resolve(sealed)
    assert resolved["identity"] == "Desktop_Agent1"
    assert resolved["subject"] == "user-a"

    # And a revocation at the provider denies on the very next request.
    sessions[0].status = SessionStatus.REVOKED
    assert leg.resolve(sealed) is None


# ---------------------------------------------------------------------------
# Sealed-session refresh (task #2114). AuthKit access tokens live ~300 s; a
# session held longer (the approver cookie, a Control Center sign-in) must be
# refreshed through the SDK, not re-checked against an expiry it has passed.
# ---------------------------------------------------------------------------


class RefreshingProvider(FakeProvider):
    """`FakeProvider` plus the two refresh seams, keyed by sealed value."""

    def __init__(self, *, expires_at, refresh_error=None, **kwargs):
        super().__init__(claims(), **kwargs)
        self.expires = {"sealed-old": expires_at}
        self.refresh_error = refresh_error
        self.refreshed: list[str] = []
        self.authenticated: list[str] = []

    def sealed_expires_at(self, sealed):
        return self.expires.get(sealed)

    def refresh_sealed(self, sealed):
        if self.refresh_error:
            raise self.refresh_error
        self.refreshed.append(sealed)
        self.expires["sealed-new"] = int(time.time()) + 300
        return "sealed-new"

    def authenticate_sealed(self, sealed_session):
        self.authenticated.append(sealed_session)
        return super().authenticate_sealed(sealed_session)


def test_an_expired_access_token_is_refreshed_then_verified():
    provider = RefreshingProvider(expires_at=int(time.time()) - 5)
    facts, replacement = make_leg(provider).resolve_refreshing("sealed-old")
    assert replacement == "sealed-new"
    assert facts["identity"] == "Desktop_Agent1"
    assert provider.refreshed == ["sealed-old"]
    # The replacement, not the stale session, is what gets verified.
    assert provider.authenticated == ["sealed-new"]
    assert provider.active_calls == 1


def test_a_token_about_to_expire_is_refreshed_and_a_fresh_one_is_not():
    soon = RefreshingProvider(expires_at=int(time.time()) + 5)
    assert make_leg(soon).resolve_refreshing("sealed-old")[1] == "sealed-new"
    fresh = RefreshingProvider(expires_at=int(time.time()) + 250)
    facts, replacement = make_leg(fresh).resolve_refreshing("sealed-old")
    assert facts and replacement is None
    assert fresh.refreshed == []


def test_a_denied_refresh_fails_closed_without_logging_provider_text(caplog):
    provider = RefreshingProvider(
        expires_at=int(time.time()) - 5, refresh_error=ValueError("provider secret body")
    )
    with caplog.at_level("WARNING", logger="unified_agent.workos_hub_auth"):
        assert make_leg(provider).resolve_refreshing("sealed-old") == (None, None)
    assert "ValueError" in caplog.text
    assert "provider secret body" not in caplog.text
    assert provider.active_calls == 0


def test_a_refresh_that_then_fails_membership_still_returns_the_rotated_session():
    """WorkOS rotates the refresh token: the caller must keep the new one."""
    provider = RefreshingProvider(expires_at=int(time.time()) - 5, active=False)
    assert make_leg(provider).resolve_refreshing("sealed-old") == (None, "sealed-new")


def test_the_header_leg_never_refreshes():
    """`resolve` is the Authorization-header path; the client owns that session."""
    provider = RefreshingProvider(expires_at=int(time.time()) - 5)
    make_leg(provider).resolve("sealed-old")
    assert provider.refreshed == []


def test_refresh_through_the_real_provider_with_a_fake_sdk():
    """`WorkOSSDKProvider.refresh_sealed` drives the SDK's sealed-session refresh.

    The fake SDK session behaves like workos 10.5's: `authenticate` refuses an
    expired access token, and `refresh(organization_id=...)` answers with a new
    sealed session carrying a newly signed token.
    """
    import jwt
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.asymmetric import rsa
    from test_pilot_auth import sdk_session
    from workos.session import seal_data, unseal_data

    from unified_agent.pilot_auth import WorkOSSDKProvider

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    password = Fernet.generate_key().decode()
    issuer = "https://api.workos.com/user_management/client-a"

    def token(exp):
        claims = {"iss": issuer, "sub": "user-a", "sid": "session-a", "org_id": "org-a"}
        return jwt.encode(claims | {"iat": int(time.time()), "exp": exp}, key, algorithm="RS256")

    refresh_calls = []

    class FakeSDKSession:
        jwks = SimpleNamespace(
            get_signing_key_from_jwt=lambda t: SimpleNamespace(key=key.public_key())
        )

        def __init__(self, session_data):
            self.session_data = session_data

        def authenticate(self):
            data = unseal_data(self.session_data, password)
            try:
                jwt.decode(
                    data["access_token"], key.public_key(), algorithms=["RS256"], issuer=issuer
                )
            except jwt.InvalidTokenError:
                return SimpleNamespace(authenticated=False, reason="invalid_jwt")
            return SimpleNamespace(
                authenticated=True,
                user={"id": "user-a"},
                session_id="session-a",
                organization_id="org-a",
            )

        def refresh(self, *, organization_id=None, cookie_password=None):
            refresh_calls.append(organization_id)
            data = unseal_data(self.session_data, password)
            if data.get("refresh_token") != "refresh-1":
                return SimpleNamespace(authenticated=False, reason="refresh_denied")
            renewed = {
                "access_token": token(int(time.time()) + 300),
                "refresh_token": "refresh-2",
                "user": data["user"],
            }
            return SimpleNamespace(authenticated=True, sealed_session=seal_data(renewed, password))

    memberships = [SimpleNamespace(user_id="user-a", organization_id="org-a", status="active")]
    client = SimpleNamespace(
        client_id="client-a",
        user_management=SimpleNamespace(
            load_sealed_session=lambda *, session_data, cookie_password: FakeSDKSession(
                session_data
            ),
            list_sessions=lambda **kw: SimpleNamespace(
                auto_paging_iter=lambda: iter([sdk_session()])
            ),
        ),
        organization_membership=SimpleNamespace(
            list_organization_memberships=lambda **kw: SimpleNamespace(
                auto_paging_iter=lambda: iter(memberships)
            )
        ),
    )
    provider = WorkOSSDKProvider(
        client,
        redirect_uri="https://hub.example/oauth/approver/workos/callback",
        cookie_password=password,
        organization_id="org-a",
        issuer=issuer,
    )
    leg = workos_hub_auth.WorkOSHubAuth(
        provider, organization_id="org-a", identity_map={"user-a": "Desktop_Agent1"}
    )
    expired = seal_data(
        {
            "access_token": token(int(time.time()) - 60),
            "refresh_token": "refresh-1",
            "user": {"id": "user-a"},
        },
        password,
    )
    # Without a refresh the session is dead: the old behaviour, ~5 minutes in.
    assert leg.resolve(expired) is None
    assert provider.sealed_expires_at(expired) < time.time()

    facts, replacement = leg.resolve_refreshing(expired)
    assert facts["identity"] == "Desktop_Agent1"
    assert refresh_calls == ["org-a"]
    assert unseal_data(replacement, password)["refresh_token"] == "refresh-2"
    assert facts["expires_at"] > time.time() + 200

    # The spent session cannot be refreshed again.
    assert leg.resolve_refreshing(
        seal_data(
            {
                "access_token": token(int(time.time()) - 60),
                "refresh_token": "spent",
                "user": {"id": "user-a"},
            },
            password,
        )
    ) == (None, None)
