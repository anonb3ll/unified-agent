"""Desktop compatibility discovery inherits the existing whoami identity plane."""

import secrets
from importlib import metadata

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import server, storage, webui
from unified_agent.webui import auth


@pytest.fixture
def client(monkeypatch):
    token = secrets.token_urlsafe(24)
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{token}")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        yield client, token


@pytest.mark.parametrize("transport", ["bearer", "cookie", "warn"])
def test_whoami_versions_preserve_identity_and_redaction(client, monkeypatch, transport):
    http, token = client
    headers = {}
    csrf = None
    if transport == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    elif transport == "cookie":
        login = http.post("/api/session", json={"token": token})
        assert login.status_code == 200
        csrf = login.json()["csrf"]
    else:
        monkeypatch.setenv("UA_ENFORCE_AUTH", "0")

    response = http.get("/api/whoami", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["hub_version"] == metadata.version("unified-agent")
    assert type(payload["api_version"]) is int
    assert payload["api_version"] == 1
    assert payload["agent"] == (None if transport == "warn" else "Desktop_Agent1")
    assert payload["via"] == (None if transport == "warn" else transport)
    assert payload["csrf"] == csrf
    assert payload["enforce_auth"] is (transport != "warn")
    assert payload["tokens_configured"] is True
    assert payload["control_enabled"] is True
    assert token not in response.text
    if transport == "cookie":
        assert http.cookies[auth.COOKIE_NAME] not in response.text


def test_version_discovery_does_not_bypass_authentication(client):
    http, _ = client
    response = http.get("/api/whoami")
    assert response.status_code == 401
    assert "hub_version" not in response.json()
    assert "api_version" not in response.json()


@pytest.mark.parametrize("installed_version", ["0.9.7", "2.1.0.dev4+desktop"])
def test_whoami_reads_installed_package_version(client, monkeypatch, installed_version):
    http, token = client
    requested = []

    def version(distribution):
        requested.append(distribution)
        return installed_version

    monkeypatch.setattr(metadata, "version", version)
    response = http.get("/api/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["hub_version"] == installed_version
    assert requested == ["unified-agent"]


def test_whoami_without_installed_metadata_preserves_identity(client, monkeypatch):
    http, token = client

    def version(distribution):
        raise metadata.PackageNotFoundError(distribution)

    monkeypatch.setattr(metadata, "version", version)
    response = http.get("/api/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["hub_version"] == "unknown"
    assert response.json()["api_version"] == 1
    assert response.json()["agent"] == "Desktop_Agent1"
