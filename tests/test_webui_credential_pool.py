"""Credential Pool Control Center routes (task #1121).

Ground truth is the real ``credential_pool_list`` projection against the
fixture DB — rows are asserted through the HTTP surface, not stubs. Strictly
read-only: no route may register or revoke a credential, and no route may
ever surface a secret value — the pool stores and returns metadata only.
"""

from __future__ import annotations

import inspect
import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import credential_pool, server, storage, webui
from unified_agent.webui import api as webui_api
from unified_agent.webui.routes import web_routes

TOKEN = f"tok-credpool-{secrets.token_urlsafe(8)}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("UA_WORKSPACE", str(ws))
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class TestCredentialPoolRoute:
    def test_lists_metadata_from_real_credential_pool_list(self, client):
        credential_pool.credential_pool_register("anthropic", "primary", "console seat")
        credential_pool.credential_pool_register("openai", "codex-cli")

        res = client.get("/api/credential_pool", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body.get("error") in (None, "")
        assert body["count"] == 2
        labels = {row["label"] for row in body["credentials"]}
        assert labels == {"primary", "codex-cli"}
        for row in body["credentials"]:
            assert set(row) == {
                "id",
                "provider",
                "label",
                "note",
                "status",
                "active",
                "created_at",
                "updated_at",
            }

    def test_revoked_credential_appears_and_is_marked_revoked(self, client):
        """Task #1184's stated objective is "registered/revoked" status — a
        revoked credential must be visible to the operator, not hidden. The
        route requests ``include_revoked=True`` explicitly, overriding
        ``credential_pool_list``'s own opt-in default so the operator view
        actually shows both states.
        """
        credential_id = credential_pool.credential_pool_register("google", "svc")["credential"][
            "id"
        ]
        credential_pool.credential_pool_revoke(credential_id)

        res = client.get("/api/credential_pool", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        ids = {row["id"]: row for row in body["credentials"]}
        assert credential_id in ids, "revoked credential must still appear in the operator view"
        row = ids[credential_id]
        assert row["status"] == "revoked"
        assert row["active"] is False
        # Still metadata only — same field allowlist as a registered row.
        assert set(row) == {
            "id",
            "provider",
            "label",
            "note",
            "status",
            "active",
            "created_at",
            "updated_at",
        }

    def test_never_forwards_a_secret_shaped_field(self, client):
        credential_pool.credential_pool_register("anthropic", "primary", "console seat")
        res = client.get("/api/credential_pool", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        blob = str(body).lower()
        for forbidden in ("secret", "api_key", "token", "password"):
            # Neither the field names (id/provider/label/note/status/active/
            # created_at/updated_at) nor the registered note text contain
            # these — register() rejects secret-shaped text, and the route's
            # field allowlist means a future schema change on the tool side
            # can't add a new field that would leak one either.
            assert forbidden not in blob, blob

    def test_requires_auth(self, client):
        assert client.get("/api/credential_pool").status_code == 401


class TestCredentialPoolUnavailable:
    def test_named_error_when_projection_unavailable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("UA_WORKSPACE", str(ws))
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        storage.init_db()

        def boom(**_kwargs):
            raise OSError("credential pool storage unreachable")

        monkeypatch.setattr(webui_api, "call_tool", boom)
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as client:
            res = client.get("/api/credential_pool", headers=_auth())
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["error"] == "credential pool unavailable"
            assert body["code"] == "credential_pool_unavailable"
            assert body["credentials"] == []


class TestCredentialPoolReadOnlyInvariant:
    def test_credential_pool_route_is_get_only(self):
        paths = [
            (route.path, frozenset(m for m in (route.methods or set()) if m != "HEAD"))
            for route in web_routes()
            if "/credential_pool" in route.path
        ]
        assert paths, "expected the credential_pool read route to be registered"
        for path, methods in paths:
            assert methods == frozenset({"GET"}), f"{path} must be GET-only, got {methods}"
        assert not any("/api/action/credential_pool" in route.path for route in web_routes())

    def test_api_helper_never_reaches_credential_mutations(self):
        blob = inspect.getsource(webui_api.credential_pool_payload)
        for forbidden in ("credential_pool_register", "credential_pool_revoke"):
            assert forbidden not in blob, f"credential_pool_payload must not call {forbidden}"
