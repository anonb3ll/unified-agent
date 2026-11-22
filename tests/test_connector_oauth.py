from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import (
    connector_oauth,
    connectors,
    github_app,
    managed_secrets,
    oauth,
    server,
    storage,
    webui,
)


@pytest.fixture
def ready(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_HOME", str(tmp_path / "ua"))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", Fernet.generate_key().decode())
    monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://hub.example.test")
    monkeypatch.setenv("UA_GOOGLE_CONNECTOR_OAUTH_ENABLED", "1")
    monkeypatch.setattr(oauth, "get_provider", lambda: object())
    storage.init_db()
    for provider in ("github", "google"):
        managed_secrets.set_secret(
            f"oauth:{provider}:client_id",
            "synthetic-client-id",
            requested_by="Desktop_Agent2",
            consumer="connector",
        )
        managed_secrets.set_secret(
            f"oauth:{provider}:client_secret",
            "synthetic-client-secret",
            requested_by="Desktop_Agent2",
            consumer="connector",
        )
    return tmp_path


def _state(result):
    return parse_qs(urlsplit(result["authorize_url"]).query)["state"][0]


def test_begin_requires_encryption_and_fixed_pkce(monkeypatch, ready):
    result = connector_oauth.begin("google", actor="Desktop_Agent2")
    parsed = urlsplit(result["authorize_url"])
    params = parse_qs(parsed.query)
    assert parsed.scheme == "https" and parsed.hostname == "accounts.google.com"
    assert params["code_challenge_method"] == ["S256"]
    assert params["access_type"] == ["offline"]
    assert params["redirect_uri"] == ["https://hub.example.test/oauth/connectors/callback"]
    assert "synthetic-client-secret" not in result["authorize_url"]
    with storage.connect() as conn:
        pending = conn.execute("SELECT code_verifier FROM connector_oauth_pending").fetchone()[0]
    assert pending.startswith("gAAAA")  # Fernet ciphertext, never a raw PKCE verifier.
    monkeypatch.delenv("UA_MANAGED_SECRET_KEYS")
    with pytest.raises(connector_oauth.ConnectorOAuthError, match="encryption"):
        connector_oauth.begin("google", actor="Desktop_Agent2")


def test_google_connection_requires_explicit_scope_review_gate(monkeypatch, ready):
    monkeypatch.delenv("UA_GOOGLE_CONNECTOR_OAUTH_ENABLED")
    with pytest.raises(connector_oauth.ConnectorOAuthError, match="scope review"):
        connector_oauth.begin("google", actor="Desktop_Agent2")


def test_callback_single_use_and_encrypted_grant(monkeypatch, ready):
    result = connector_oauth.begin("google", actor="Desktop_Agent2")
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            seen.update(url=url, **kwargs)
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access",
                    "refresh_token": "synthetic-refresh",
                    "expires_in": 3600,
                    "scope": " ".join(connectors.GOOGLE_OAUTH.scopes),
                },
            )

    monkeypatch.setattr(httpx, "Client", Client)
    state = _state(result)
    assert connector_oauth.complete(state, "synthetic-code")["status"] == "connected"
    assert seen["data"]["code_verifier"]
    assert seen["data"]["client_secret"] == "synthetic-client-secret"
    path = ready / "ua" / "managed-secrets" / "oauth:google:grant"
    assert "synthetic-refresh" not in path.read_text()
    with pytest.raises(connector_oauth.ConnectorOAuthError, match="state"):
        connector_oauth.complete(state, "synthetic-code")


def test_refresh_rotates_grant_and_invalid_grant_marks_reconnect(monkeypatch, ready):
    responses = [
        httpx.Response(
            200,
            json={
                "access_token": "first",
                "refresh_token": "refresh-1",
                "expires_in": 1,
                "scope": " ".join(connectors.GOOGLE_OAUTH.scopes),
            },
        ),
        httpx.Response(
            200, json={"access_token": "second", "refresh_token": "refresh-2", "expires_in": 3600}
        ),
        httpx.Response(400, json={"error": "invalid_grant"}),
    ]

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return responses.pop(0)

    monkeypatch.setattr(httpx, "Client", Client)
    state = _state(connector_oauth.begin("google", actor="Desktop_Agent2"))
    connector_oauth.complete(state, "synthetic-code")
    assert (
        connector_oauth.access_token_for("google", actor="Desktop_Agent2", purpose="drive.read")
        == "second"
    )
    grant = json.loads(managed_secrets._load_for_consumer("oauth:google:grant", "connector"))
    assert grant["refresh_token"] == "refresh-2"
    grant["expires_at"] = 0
    managed_secrets.set_secret(
        "oauth:google:grant", json.dumps(grant), requested_by="Desktop_Agent2", consumer="connector"
    )
    with pytest.raises(connector_oauth.ConnectorOAuthError, match="needs reconnect"):
        connector_oauth.access_token_for("google", actor="Desktop_Agent2", purpose="drive.read")
    assert connector_oauth.status("google")["status"] == "needs_reconnect"


def test_refresh_event_failure_leaves_rotated_grant_unusable(monkeypatch, ready):
    responses = [
        httpx.Response(
            200,
            json={
                "access_token": "first",
                "refresh_token": "refresh-1",
                "expires_in": 1,
                "scope": " ".join(connectors.GOOGLE_OAUTH.scopes),
            },
        ),
        httpx.Response(
            200, json={"access_token": "second", "refresh_token": "refresh-2", "expires_in": 3600}
        ),
    ]

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return responses.pop(0)

    monkeypatch.setattr(httpx, "Client", Client)
    state = _state(connector_oauth.begin("google", actor="Desktop_Agent2"))
    connector_oauth.complete(state, "synthetic-code")
    monkeypatch.setattr(
        connector_oauth.events,
        "record_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
    )
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        connector_oauth.access_token_for("google", actor="Desktop_Agent2", purpose="drive.read")
    assert connector_oauth.status("google")["status"] == "needs_reconnect"
    with pytest.raises(connector_oauth.ConnectorOAuthError, match="needs reconnect"):
        connector_oauth.access_token_for("google", actor="Desktop_Agent2", purpose="drive.read")


def test_disconnect_removes_grant_and_keeps_client_credentials(ready):
    state = _state(connector_oauth.begin("github", actor="Desktop_Agent2"))
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO connector_oauth_connections "
            "(provider, org_id, status, scopes_json, updated_at) VALUES "
            "('github', '', 'connected', '[\"repo\"]', 'now')"
        )
        conn.commit()
    managed_secrets.set_secret(
        "oauth:github:grant",
        json.dumps({"access_token": "synthetic"}),
        requested_by="Desktop_Agent2",
        consumer="connector",
    )
    assert connector_oauth.disconnect("github", actor="Desktop_Agent2")["status"] == "disconnected"
    assert connector_oauth.status("github")["scopes"] == []
    with pytest.raises(Exception, match="not configured"):
        managed_secrets._load_for_consumer("oauth:github:grant", "connector")
    assert managed_secrets.metadata("oauth:github:client_id")["consumer"] == "connector"
    with pytest.raises(connector_oauth.ConnectorOAuthError, match="state"):
        connector_oauth.complete(state, "synthetic-code")


def test_disconnect_audit_failure_keeps_durable_intent_and_retries(monkeypatch, ready):
    managed_secrets.set_secret(
        "oauth:github:grant",
        json.dumps({"access_token": "synthetic"}),
        requested_by="Desktop_Agent2",
        consumer="connector",
    )
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO connector_oauth_connections "
            "(provider, org_id, status, scopes_json, updated_at) VALUES "
            "('github', '', 'connected', '[\"repo\"]', 'now')"
        )
        conn.commit()
    original = connector_oauth.events.record_event_once_on_connection

    def fail_final(conn, channel, **kwargs):
        if channel == "connector.oauth.disconnected":
            raise RuntimeError("ledger unavailable")
        return original(conn, channel, **kwargs)

    monkeypatch.setattr(connector_oauth.events, "record_event_once_on_connection", fail_final)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        connector_oauth.disconnect("github", actor="Desktop_Agent2")
    assert connector_oauth.status("github")["status"] == "needs_reconnect"
    with storage.connect() as conn:
        channels = [row[0] for row in conn.execute("SELECT channel FROM event_log")]
    assert "connector.oauth.disconnect_requested" in channels
    assert "connector.oauth.disconnected" not in channels
    monkeypatch.setattr(connector_oauth.events, "record_event_once_on_connection", original)
    assert connector_oauth.disconnect("github", actor="Desktop_Agent2")["status"] == "disconnected"


def test_invalid_credential_audit_failure_rolls_back_status(monkeypatch, ready):
    managed_secrets.set_secret(
        "oauth:github:grant",
        json.dumps({"access_token": "synthetic"}),
        requested_by="Desktop_Agent2",
        consumer="connector",
    )
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO connector_oauth_connections "
            "(provider, org_id, status, scopes_json, updated_at) VALUES "
            "('github', '', 'connected', '[\"repo\"]', 'now')"
        )
        conn.commit()
    monkeypatch.setattr(
        connector_oauth.events,
        "record_event_once_on_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
    )
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        connector_oauth.mark_invalid_credential(
            "github", actor="Desktop_Agent2", purpose="github_app", rejected_token="synthetic"
        )
    assert connector_oauth.status("github")["status"] == "connected"


def test_missing_provider_scopes_refuse_connection(monkeypatch, ready):
    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return httpx.Response(200, json={"access_token": "synthetic", "scope": "read:user"})

    monkeypatch.setattr(httpx, "Client", Client)
    state = _state(connector_oauth.begin("github", actor="Desktop_Agent2"))
    with pytest.raises(connector_oauth.ConnectorOAuthError, match="fewer scopes"):
        connector_oauth.complete(state, "synthetic-code")
    assert connector_oauth.status("github")["status"] == "disconnected"


def test_ledger_failure_keeps_new_grant_unusable(monkeypatch, ready):
    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic",
                    "scope": ",".join(connectors.GITHUB_OAUTH.scopes),
                },
            )

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(
        connector_oauth.events,
        "record_event_once_on_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
    )
    state = _state(connector_oauth.begin("github", actor="Desktop_Agent2"))
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        connector_oauth.complete(state, "synthetic-code")
    assert connector_oauth.status("github")["status"] != "connected"
    with pytest.raises(connector_oauth.ConnectorOAuthError, match="needs reconnect"):
        connector_oauth.access_token_for("github", actor="Desktop_Agent2", purpose="github_app")


def test_github_user_transport_uses_broker_without_legacy_fallback(monkeypatch, ready):
    called = {}

    def broker(provider, **kwargs):
        called.update(provider=provider, **kwargs)
        return "synthetic-provider-token"

    monkeypatch.setattr(connector_oauth, "access_token_for", broker)
    env = {"UA_GITHUB_TOKEN_MODE": "user", "UA_GITHUB_USER_TOKEN_SOURCE": "connector_oauth"}
    assert (
        github_app.resolve_user_token(
            env, run_gh=lambda *_args, **_kwargs: pytest.fail("gh fallback")
        )
        == "synthetic-provider-token"
    )
    assert called["provider"] == "github"
    assert called["purpose"] == "github_app"
    with pytest.raises(ValueError, match="SOURCE"):
        github_app.resolve_user_token({"UA_GITHUB_USER_TOKEN_SOURCE": "typo"})


def test_github_api_401_marks_reconnect_but_403_does_not(monkeypatch, ready):
    monkeypatch.setenv("UA_GITHUB_USER_TOKEN_SOURCE", "connector_oauth")
    seen = []
    monkeypatch.setattr(
        connector_oauth,
        "mark_invalid_credential",
        lambda provider, **_kwargs: seen.append(provider),
    )

    class Opener:
        code = 401

        def open(self, request, timeout):
            raise HTTPError(request.full_url, self.code, "synthetic", {}, None)

    opener = Opener()
    monkeypatch.setattr(github_app, "_REDIRECT_SAFE_OPENER", opener)
    transport = github_app.GitHubUserTokenTransport(lambda: "synthetic-token", brokered=True)
    with pytest.raises(HTTPError):
        transport._request("POST", "/repos/o/r/statuses/head", {"state": "pending"})
    assert seen == ["github"]
    opener.code = 403
    with pytest.raises(HTTPError):
        transport._request("POST", "/repos/o/r/statuses/head", {"state": "pending"})
    assert seen == ["github"]


def test_github_401_uses_transport_bound_broker_mode(monkeypatch, ready):
    monkeypatch.delenv("UA_GITHUB_USER_TOKEN_SOURCE", raising=False)
    seen = []
    monkeypatch.setattr(
        connector_oauth,
        "access_token_for",
        lambda *_args, **_kwargs: "synthetic-token",
    )
    monkeypatch.setattr(
        connector_oauth,
        "mark_invalid_credential",
        lambda provider, **_kwargs: seen.append(provider),
    )

    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 401, "synthetic", {}, None)

    monkeypatch.setattr(github_app, "_REDIRECT_SAFE_OPENER", Opener())
    transport = github_app.build_transport(
        env={"UA_GITHUB_TOKEN_MODE": "user", "UA_GITHUB_USER_TOKEN_SOURCE": "connector_oauth"}
    )
    with pytest.raises(HTTPError):
        transport._request("POST", "/repos/o/r/statuses/head", {"state": "pending"})
    assert seen == ["github"]


def test_old_github_401_cannot_disable_reconnected_grant(ready):
    managed_secrets.set_secret(
        "oauth:github:grant",
        json.dumps({"access_token": "new-token", "refresh_token": None, "expires_at": None}),
        requested_by="Desktop_Agent2",
        consumer="connector",
    )
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO connector_oauth_connections "
            "(provider, org_id, status, scopes_json, updated_at) VALUES "
            "('github', '', 'connected', '[\"repo\"]', 'now')"
        )
        conn.commit()
    connector_oauth.mark_invalid_credential(
        "github", actor="Desktop_Agent2", purpose="github_app", rejected_token="old-token"
    )
    assert connector_oauth.status("github")["status"] == "connected"
    connector_oauth.mark_invalid_credential(
        "github", actor="Desktop_Agent2", purpose="github_app", rejected_token="new-token"
    )
    assert connector_oauth.status("github")["status"] == "needs_reconnect"


def test_broker_readiness_requires_decryptable_grant(monkeypatch, ready):
    managed_secrets.set_secret(
        "oauth:github:grant",
        json.dumps({"access_token": "synthetic-token"}),
        requested_by="Desktop_Agent2",
        consumer="connector",
    )
    with storage.connect() as conn:
        conn.execute(
            "INSERT INTO connector_oauth_connections "
            "(provider, org_id, status, scopes_json, updated_at) VALUES "
            "('github', '', 'connected', '[\"repo\"]', 'now')"
        )
        conn.commit()
    env = {"UA_GITHUB_TOKEN_MODE": "user", "UA_GITHUB_USER_TOKEN_SOURCE": "connector_oauth"}
    assert github_app.manifest_secret_status(env)["satisfied"] is True
    monkeypatch.setenv("UA_MANAGED_SECRET_KEYS", Fernet.generate_key().decode())
    assert github_app.manifest_secret_status(env)["satisfied"] is False


def test_brokered_grant_rejects_custom_api_origin(ready):
    env = {"UA_GITHUB_TOKEN_MODE": "user", "UA_GITHUB_USER_TOKEN_SOURCE": "connector_oauth"}
    with pytest.raises(ValueError, match="fixed GitHub API origin"):
        github_app.build_transport(env=env, api_base="https://example.test")


def test_connect_routes_require_auth_and_callback_returns_no_token(monkeypatch, ready):
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        "Desktop_Person:synthetic-hub-bearer,Desktop_Agent2:synthetic-agent-bearer",
    )
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*,hub.example.test")
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        assert client.post("/api/connectors/oauth/github/begin").status_code == 401
        agent_headers = {"Authorization": "Bearer synthetic-agent-bearer"}
        assert client.get("/api/connectors/oauth/github", headers=agent_headers).status_code == 403
        assert (
            client.post("/api/connectors/oauth/github/begin", headers=agent_headers).status_code
            == 403
        )
        assert (
            client.post(
                "/api/connectors/oauth/github/disconnect", headers=agent_headers
            ).status_code
            == 403
        )
        headers = {"Authorization": "Bearer synthetic-hub-bearer"}
        begin = client.post("/api/connectors/oauth/github/begin", headers=headers)
        assert begin.status_code == 201, begin.text
        state = _state(begin.json())
        assert "synthetic-client-secret" not in begin.text
        monkeypatch.setattr(
            connector_oauth,
            "complete",
            lambda supplied_state, code: (
                {"provider": "github"}
                if supplied_state == state and code == "synthetic-code"
                else pytest.fail("wrong callback arguments")
            ),
        )
        assert (
            client.get(
                "/oauth/connectors/callback",
                params={"state": state, "code": "synthetic-code"},
            ).status_code
            == 403
        )
        callback = client.get(
            "/oauth/connectors/callback",
            params={"state": state, "code": "synthetic-code"},
            headers={"host": "hub.example.test"},
            follow_redirects=False,
        )
        assert callback.status_code == 200
        assert "Return to your Control Center tab" in callback.text
        assert "synthetic-client-secret" not in callback.text


def test_managed_secret_connector_actions_respect_read_only(monkeypatch, ready):
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Person:synthetic-hub-bearer")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_MANAGED_SECRETS", "1")
    monkeypatch.setenv("UA_AGENT_ACCESS_READ_ONLY", "1")
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    headers = {"Authorization": "Bearer synthetic-hub-bearer"}
    with TestClient(app) as client:
        assert client.post("/api/connectors/oauth/github/begin", headers=headers).status_code == 403
        assert (
            client.post("/api/connectors/oauth/github/disconnect", headers=headers).status_code
            == 403
        )
