"""UA_ENFORCE_AUTH=1 must not authenticate when no credential leg is usable.

The token parser used to swallow a missing or unreadable UA_AGENT_TOKENS path
and return {}, and the hub middleware then served the request anonymously
without consulting enforcement. These tests pin the fail-closed contract.
"""

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from unified_agent import config, oauth, workos_hub_auth
from unified_agent.server import HubAuthASGIMiddleware


def _clear_credential_env(monkeypatch):
    for name in (
        "UA_AGENT_TOKENS",
        "UA_MCP_OAUTH",
        "UA_MCP_OAUTH_ISSUER",
        "UA_MCP_OAUTH_PASSPHRASE",
        "UA_HUB_WORKOS_ORGANIZATION_ID",
        "UA_HUB_WORKOS_IDENTITY_MAP",
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_token_file_does_not_parse_as_an_empty_map(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_AGENT_TOKENS", "/nonexistent/tokens.txt")
    with pytest.raises(ValueError, match="missing or unreadable"):
        config.agent_tokens()


def test_tilde_token_file_missing_raises():
    with pytest.raises(ValueError, match="missing or unreadable"):
        config._parse_colon_pairs("UA_AGENT_TOKENS", "~/no-such-ua-tokens-8abe.txt")


def test_unreadable_token_file_raises(tmp_path, monkeypatch):
    path = tmp_path / "tokens.txt"
    path.write_text("label:secret\n")
    path.chmod(0)
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_AGENT_TOKENS", str(path))
    try:
        with pytest.raises(ValueError, match="missing or unreadable"):
            config.agent_tokens()
    finally:
        path.chmod(0o600)


def test_readable_token_file_still_parses(tmp_path, monkeypatch):
    path = tmp_path / "tokens.txt"
    path.write_text("label:secret\nnot-a-pair\n")
    monkeypatch.setenv("UA_AGENT_TOKENS", str(path))
    assert config.agent_tokens() == {"secret": "label"}


def test_inline_label_token_still_parses(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_AGENT_TOKENS", "label:secret")
    assert config.agent_tokens() == {"secret": "label"}


def test_malformed_inline_entries_are_still_dropped(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", "not-a-pair,label:secret,also:")
    assert config.agent_tokens() == {"secret": "label"}


def test_empty_token_env_is_an_empty_map(monkeypatch):
    monkeypatch.delenv("UA_AGENT_TOKENS", raising=False)
    assert config.agent_tokens() == {}
    monkeypatch.setenv("UA_AGENT_TOKENS", "")
    assert config.agent_tokens() == {}


def test_relative_missing_name_is_not_treated_as_a_path():
    """Only `/`, `~`, or an existing path are file reads. A bare typo stays a dropped entry."""
    assert config._parse_colon_pairs("UA_AGENT_TOKENS", "no-such-tokens.txt") == []


def test_http_enforce_without_credentials_exits(monkeypatch, capsys):
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("UA_TRANSPORT", "http")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    with pytest.raises(SystemExit) as exc:
        config.require_configured_http_credentials()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "refusing to start" in err
    assert "UA_ENFORCE_AUTH=1" in err


def test_http_enforce_with_missing_token_file_exits(monkeypatch, capsys):
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("UA_TRANSPORT", "http")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_AGENT_TOKENS", "/nonexistent/tokens.txt")
    with pytest.raises(SystemExit) as exc:
        config.require_configured_http_credentials()
    assert exc.value.code == 1
    assert "missing or unreadable" in capsys.readouterr().err


def test_stdio_enforce_without_tokens_does_not_exit(monkeypatch):
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("UA_TRANSPORT", "stdio")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    config.require_configured_http_credentials()


def test_stdio_does_not_parse_a_broken_token_file_at_startup(monkeypatch):
    """Startup refuses HTTP only. A broken file still raises when something parses it."""
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("UA_TRANSPORT", "stdio")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_AGENT_TOKENS", "/nonexistent/tokens.txt")
    config.require_configured_http_credentials()
    with pytest.raises(ValueError, match="missing or unreadable"):
        config.agent_tokens()


def test_http_warn_mode_without_tokens_still_starts(monkeypatch, capsys):
    """Explicit auth-off still starts, but the startup path says so out loud."""
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("UA_TRANSPORT", "http")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
    warned = set(config._POSTURE_WARNED)
    config._POSTURE_WARNED.discard("http-auth-disabled")
    try:
        config.require_configured_http_credentials()
    finally:
        config._POSTURE_WARNED.clear()
        config._POSTURE_WARNED.update(warned)
    err = capsys.readouterr().err
    assert "[Warning]" in err
    assert "UA_ENFORCE_AUTH is off" in err
    assert "anonymous clients can list and call tools" in err


def test_http_enforce_with_inline_tokens_starts(monkeypatch):
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("UA_TRANSPORT", "http")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_AGENT_TOKENS", "label:secret")
    config.require_configured_http_credentials()


def test_http_enforce_with_oauth_and_no_tokens_starts(monkeypatch):
    _clear_credential_env(monkeypatch)
    monkeypatch.delenv("UA_AGENT_LABELS", raising=False)
    monkeypatch.setenv("UA_TRANSPORT", "http")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_MCP_OAUTH", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "https://auth.example.test")
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", "test-only")
    assert config.mcp_oauth_enabled() is True
    config.require_configured_http_credentials()


def test_http_enforce_with_workos_enabled_and_no_tokens_starts(monkeypatch):
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("UA_TRANSPORT", "http")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org-test")
    assert workos_hub_auth.enabled() is True
    config.require_configured_http_credentials()


# A protected (non-public) route. "/" is in server._PUBLIC_PATHS and, since
# #872, is deliberately served even when UA_ENFORCE_AUTH=1 has no credential
# leg (it only redirects to /ui), so it cannot stand in for "the app" here.
_PROTECTED_PATH = "/api/fail-closed-probe"


def _reached_app():
    reached = []

    async def homepage(request):
        reached.append(request.method)
        return JSONResponse({"identity": config.verified_agent_identity(), "reached": True})

    app = HubAuthASGIMiddleware(
        Starlette(routes=[Route(_PROTECTED_PATH, homepage, methods=["GET", "POST"])])
    )
    return app, reached


def test_middleware_enforce_without_credentials_is_401_and_does_not_reach_app(monkeypatch):
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setattr(oauth, "get_provider", lambda: None)
    monkeypatch.setattr(workos_hub_auth, "enabled", lambda: False)
    app, reached = _reached_app()
    client = TestClient(app)
    resp = client.post(_PROTECTED_PATH)
    assert resp.status_code == 401
    assert resp.json()["error"] == "Unauthorized: invalid or missing bearer token"
    assert reached == []


def test_middleware_warn_mode_without_credentials_reaches_the_app(monkeypatch):
    _clear_credential_env(monkeypatch)
    monkeypatch.setenv("UA_ENFORCE_AUTH", "0")
    monkeypatch.setattr(oauth, "get_provider", lambda: None)
    monkeypatch.setattr(workos_hub_auth, "enabled", lambda: False)
    app, reached = _reached_app()
    client = TestClient(app)
    resp = client.post(_PROTECTED_PATH)
    assert resp.status_code == 200
    assert resp.json()["reached"] is True
    assert resp.json()["identity"] is None
    assert reached == ["POST"]
