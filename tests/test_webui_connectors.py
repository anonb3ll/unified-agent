"""Connectors pane Control Center routes (task #1147).

Ground truth is the real horizon2 list tools against the fixture DB — server
registry + package catalog statuses are asserted through the HTTP surface, not
stubs. Strictly read-only: no route may reach register/revoke/authorize.
"""

from __future__ import annotations

import asyncio
import inspect
import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import connectors, server, storage, webui
from unified_agent.horizon2 import CONNECTOR_PACKAGES
from unified_agent.tools import horizon2
from unified_agent.webui import api as webui_api
from unified_agent.webui.routes import web_routes

TOKEN = f"tok-connectors-{secrets.token_urlsafe(8)}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_GCP_PROJECT", "example-project")
    monkeypatch.setenv("UA_GCP_WORKER", "example-worker")
    monkeypatch.delenv("UA_H2_MCP_CONNECTIONS", raising=False)

    async def cloud_unavailable(force=False):
        return {
            "status": "error",
            "sections": {"instances": "unavailable"},
            "instances": [],
            "errors": [
                {"section": "instances", "error": "compute.instances.list permission denied"}
            ],
            "checked_at": "2026-09-23T00:00:00+00:00",
            "cached_age_s": 0.0,
        }

    monkeypatch.setattr(webui_api, "cloud_payload", cloud_unavailable)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _insert_server(
    server_id: str,
    connector: str,
    *,
    revoked: bool = False,
    scopes: list[str] | None = None,
    created_at: str | None = None,
) -> None:
    """Seed a governed_servers row without consuming an approval gate."""
    timestamp = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO governed_servers "
            "(server_id, connector, provenance, scopes_json, reviewed_by, review_ref, "
            "revoked_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                server_id,
                connector,
                "sha256:abcdef0123456789",
                __import__("json").dumps(scopes or ["read"]),
                "Desktop_Agent1",
                "review:1147",
                timestamp if revoked else None,
                created_at or timestamp,
                timestamp,
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestConnectorsInventoryRoute:
    def test_lists_packages_and_servers_from_real_horizon2_tools(self, client):
        _insert_server("slack-prod", "slack")
        res = client.get("/api/connectors", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        assert body.get("error") in (None, "")
        assert "servers" in body
        assert "packages" in body

        # Real package catalog — not a stub list.
        package_names = {row["package"] for row in body["packages"]}
        assert package_names == set(CONNECTOR_PACKAGES) | set(connectors.all_manifests())
        assert body["package_count"] == len(package_names)
        zulip_pkg = next(row for row in body["packages"] if row["package"] == "zulip")
        assert zulip_pkg["scopes"] == []
        assert zulip_pkg["status"] == "dormant"
        assert zulip_pkg["manifest"]["default_enabled"] is False
        assert zulip_pkg["manifest"]["capabilities"] == ["chat", "worker"]
        assert zulip_pkg["secrets"]["missing"] == []
        assert zulip_pkg["secrets"]["satisfied"] is True

        # github-app (task #1264) is both a CONNECTOR_PACKAGES entry (Horizon 2
        # scopes) and a registered ConnectorManifest, unlike zulip -- so it
        # carries real `scopes` alongside `manifest`/`secrets`, not empty ones.
        github_app_pkg = next(row for row in body["packages"] if row["package"] == "github-app")
        assert github_app_pkg["scopes"] == ["checks", "comments", "read"]
        assert github_app_pkg["status"] == "dormant"
        assert github_app_pkg["manifest"]["default_enabled"] is False
        assert github_app_pkg["manifest"]["capabilities"] == [
            "check_runs",
            "commit_statuses",
            "evidence_comments",
            "poll",
        ]
        # Default token mode is app, so its credentials are what's missing.
        assert set(github_app_pkg["secrets"]["missing"]) == {
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY",
        }

        # Real server inventory from horizon2_server_list(include_revoked=True).
        listed = horizon2.horizon2_server_list(include_revoked=True)
        assert body["server_count"] == listed["count"]
        assert {s["server_id"] for s in body["servers"]} == {
            s["server_id"] for s in listed["servers"]
        }
        slack = next(s for s in body["servers"] if s["server_id"] == "slack-prod")
        assert slack["status"] in {"registered", "authorized"}
        assert slack["active"] is True
        assert slack["policy"] == "one-shot approval"
        assert slack["last_verified"] is not None
        assert slack["last_verified"] == slack["created_at"]

        slack_pkg = next(p for p in body["packages"] if p["package"] == "slack")
        assert slack_pkg["policy"] == "one-shot approval"
        assert slack_pkg["last_verified"] == slack["created_at"]

        drive_pkg = next(p for p in body["packages"] if p["package"] == "drive")
        assert drive_pkg["policy"] == "one-shot approval"
        assert drive_pkg["last_verified"] is None

    def test_dormant_package_distinct_from_authorized(self, client):
        """Catalog packages without a registration stay dormant; a live one is authorized."""
        _insert_server("slack-prod", "slack")
        res = client.get("/api/connectors", headers=_auth())
        assert res.status_code == 200, res.text
        by_name = {row["package"]: row for row in res.json()["packages"]}

        assert by_name["slack"]["status"] == "authorized"
        assert by_name["drive"]["status"] == "dormant"
        assert by_name["drive"]["status"] != by_name["slack"]["status"]

    def test_revoked_server_and_package_labeled_revoked(self, client):
        _insert_server("drive-old", "drive", revoked=True)
        res = client.get("/api/connectors", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        drive_server = next(s for s in body["servers"] if s["server_id"] == "drive-old")
        assert drive_server["status"] == "revoked"
        assert drive_server["active"] is False
        drive_pkg = next(p for p in body["packages"] if p["package"] == "drive")
        assert drive_pkg["status"] == "revoked"

    def test_legacy_rows_cannot_make_manifest_only_connector_live(self, monkeypatch):
        """Active, revoked, and mixed legacy rows have honest pane status."""

        active_row = {
            "server_id": "browser-legacy",
            "connector": "browser-projection",
            "active": True,
            "created_at": storage.now(),
        }
        revoked_row = {
            "server_id": "browser-revoked",
            "connector": "browser-projection",
            "active": False,
            "created_at": storage.now(),
        }
        legacy_rows = [active_row]

        async def legacy_horizon2_reads(name: str, **_kwargs):
            if name == "horizon2_server_list":
                return {"servers": list(legacy_rows)}
            assert name == "horizon2_package_list"
            return {
                "packages": [
                    {
                        "package": "browser-projection",
                        "scopes": [],
                        "manifest": connectors.BROWSER_PROJECTION_MANIFEST.to_dict(),
                    }
                ]
            }

        monkeypatch.setattr(webui_api, "call_tool", legacy_horizon2_reads)

        def payload() -> dict:
            return asyncio.run(webui_api.connectors_payload())

        active_only = payload()
        active_server = active_only["servers"][0]
        active_package = active_only["packages"][0]
        assert active_server["active"] is True
        assert active_server["status"] == "dormant"
        assert active_server["last_verified"] is None
        assert active_package["status"] == "dormant"
        assert active_package["health"] == "DORMANT"
        assert active_package["last_verified"] is None

        legacy_rows[:] = [revoked_row]
        revoked_only = payload()
        assert revoked_only["servers"][0]["status"] == "revoked"
        assert revoked_only["servers"][0]["last_verified"] is None
        assert revoked_only["packages"][0]["status"] == "revoked"
        assert revoked_only["packages"][0]["health"] == "DORMANT"

        legacy_rows[:] = [active_row, revoked_row]
        mixed = payload()
        assert [row["status"] for row in mixed["servers"]] == ["dormant", "revoked"]
        assert all(row["last_verified"] is None for row in mixed["servers"])
        assert mixed["packages"][0]["status"] == "dormant"
        assert mixed["packages"][0]["health"] == "DORMANT"
        assert mixed["packages"][0]["last_verified"] is None
        doctor = mixed["doctor"]["checks"][0]
        assert doctor["status"] == "dormant"
        assert doctor["health"] == "DORMANT"

    def test_registered_not_live_bound_when_mcp_connections_set(self, client, monkeypatch):
        """Active registration without a live MCP binding is registered, not authorized."""
        monkeypatch.setenv("UA_H2_MCP_CONNECTIONS", "obsidian:gbrain")
        _insert_server("slack-prod", "slack")
        res = client.get("/api/connectors", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        slack_server = next(s for s in body["servers"] if s["server_id"] == "slack-prod")
        assert slack_server["status"] == "registered"
        slack_pkg = next(p for p in body["packages"] if p["package"] == "slack")
        assert slack_pkg["status"] == "registered"

    def test_doctor_section_labels_each_package(self, client):
        _insert_server("slack-prod", "slack")
        res = client.get("/api/connectors", headers=_auth())
        assert res.status_code == 200, res.text
        doctor = res.json()["doctor"]
        assert doctor["label"]
        checks = {c["package"]: c for c in doctor["checks"]}
        assert checks["slack"]["status"] == "authorized"
        assert checks["drive"]["status"] == "dormant"
        assert "LIVE" in checks["slack"]["health"] or checks["slack"]["health"] == "LIVE"
        assert checks["drive"]["health"] == "DORMANT"

    def test_gcp_permission_denial_is_reported_as_degraded(self, client, monkeypatch):
        async def denied_cloud_payload(force=False):
            assert force is False
            return {
                "status": "error",
                "sections": {"instances": "unavailable"},
                "instances": [],
                "errors": [
                    {"section": "instances", "error": "compute.instances.list permission denied"}
                ],
                "checked_at": "2026-09-23T00:00:00+00:00",
                "cached_age_s": 23.0,
            }

        monkeypatch.setattr(webui_api, "cloud_payload", denied_cloud_payload)
        res = client.get("/api/connectors", headers=_auth())
        assert res.status_code == 200, res.text
        body = res.json()
        package = next(row for row in body["packages"] if row["package"] == "int-gcp-agent20")
        doctor = next(
            row for row in body["doctor"]["checks"] if row["package"] == "int-gcp-agent20"
        )
        assert package["status"] == "dormant"
        assert package["health"] == "DEGRADED"
        assert "permission denied" in package["health_detail"]
        assert package["health_cached_age_s"] == 23.0
        assert doctor["health"] == "DEGRADED"
        assert "permission denied" in doctor["detail"]

    @pytest.mark.parametrize("missing", ["UA_GCP_PROJECT", "UA_GCP_WORKER"])
    def test_unconfigured_gcp_health_has_guidance_without_inventory(
        self, client, monkeypatch, missing
    ):
        monkeypatch.delenv(missing, raising=False)
        monkeypatch.setattr(
            webui_api,
            "cloud_payload",
            lambda **kwargs: pytest.fail("unconfigured pane probed cloud"),
        )
        # The pane must classify configuration before requesting inventory.
        res = client.get("/api/connectors", headers=_auth())
        assert res.status_code == 200
        package = next(row for row in res.json()["packages"] if row["package"] == "int-gcp-agent20")
        assert package["health"] == "UNKNOWN"
        assert missing in package["health_detail"]

    def test_requires_auth(self, client):
        assert client.get("/api/connectors").status_code == 401


class TestConnectorsUnavailable:
    def test_named_error_when_horizon2_unavailable(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        storage.init_db()

        def boom(**_kwargs):
            raise OSError("horizon2 storage unreachable")

        monkeypatch.setattr(webui_api, "call_tool", boom)
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as client:
            res = client.get("/api/connectors", headers=_auth())
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["error"] == "horizon2 unavailable"
            assert body["code"] == "horizon2_unavailable"
            assert body["servers"] == []
            assert body["packages"] == []
            assert "doctor" in body


class TestConnectorsReadOnlyInvariant:
    def test_horizon2_inventory_stays_read_only_and_oauth_actions_are_explicit(self):
        connector_paths = [
            (route.path, frozenset(m for m in (route.methods or set()) if m != "HEAD"))
            for route in web_routes()
            if route.path == "/api/connectors" or "/horizon2" in route.path
        ]
        assert connector_paths, "expected connectors read routes to be registered"
        for path, methods in connector_paths:
            assert methods == frozenset({"GET"}), f"{path} must be GET-only, got {methods}"
        # Task #2470 (CC2): the Control Center's connector pane gained four
        # explicit mutation actions. Each is POST-only and each reaches a
        # one-shot-approval-gated horizon2 tool, so the pane's own read surface
        # still cannot mutate anything. Pin the exact action set: a new action
        # has to be added here deliberately, not appear as a side effect.
        connector_actions = {
            (route.path, tuple(sorted(m for m in (route.methods or []) if m != "HEAD")))
            for route in web_routes()
            if route.path.startswith("/api/action/connector")
        }
        assert connector_actions == {
            ("/api/action/connector/register", ("POST",)),
            ("/api/action/connector/register-package", ("POST",)),
            ("/api/action/connector/authorize", ("POST",)),
            ("/api/action/connector/revoke", ("POST",)),
        }, f"unexpected connector action surface: {connector_actions}"
        assert not any("/api/action/horizon2" in route.path for route in web_routes())
        oauth_routes = {
            (route.path, tuple(sorted(m for m in (route.methods or []) if m != "HEAD")))
            for route in web_routes()
            if "/connectors/oauth/" in route.path or route.path == "/oauth/connectors/callback"
        }
        assert oauth_routes == {
            ("/api/connectors/oauth/{provider}", ("GET",)),
            ("/api/connectors/oauth/{provider}/begin", ("POST",)),
            ("/api/connectors/oauth/{provider}/disconnect", ("POST",)),
            ("/oauth/connectors/callback", ("GET",)),
        }

    def test_api_helpers_never_reach_horizon2_mutations(self):
        blob = inspect.getsource(webui_api.connectors_payload)
        for forbidden in (
            "horizon2_server_register",
            "horizon2_server_revoke",
            "horizon2_connector_authorize",
            "horizon2_package_register",
        ):
            assert forbidden not in blob, f"connectors_payload must not call {forbidden}"


class TestConnectorsRegistryDivergence:
    def test_divergence_pins_payload_to_horizon2_truth(self, client):
        """Pane payload must faithfully surface every entry in the Horizon-2 connector registry truth."""
        _insert_server("slack-prod", "slack")
        _insert_server("obsidian-work", "obsidian", scopes=["read", "write"])
        _insert_server("drive-archive", "drive", revoked=True)

        res = client.get("/api/connectors", headers=_auth())
        assert res.status_code == 200, res.text
        payload = res.json()

        truth = horizon2.horizon2_server_list(include_revoked=True)
        truth_servers = {s["server_id"]: s for s in truth["servers"]}
        payload_servers = {s["server_id"]: s for s in payload["servers"]}

        # Truth pinning: every registry entry in truth is surfaced in payload
        assert set(payload_servers.keys()) == set(truth_servers.keys())
        assert payload["server_count"] == truth["count"]

        for server_id, truth_entry in truth_servers.items():
            payload_entry = payload_servers[server_id]
            assert payload_entry["connector"] == truth_entry["connector"]
            assert payload_entry["active"] == truth_entry["active"]
            assert payload_entry["scopes"] == truth_entry["scopes"]
            assert payload_entry["policy"] == "one-shot approval"
            assert payload_entry["last_verified"] == truth_entry["created_at"]

    def test_divergence_fails_when_registry_entry_not_surfaced(self, client):
        """A registry entry not surfaced by the payload must fail the divergence test."""
        _insert_server("slack-prod", "slack")
        _insert_server("obsidian-work", "obsidian")

        res = client.get("/api/connectors", headers=_auth())
        assert res.status_code == 200, res.text
        valid_payload = res.json()

        truth = horizon2.horizon2_server_list(include_revoked=True)

        def assert_no_registry_divergence(p: dict, t: dict) -> None:
            t_ids = {s["server_id"] for s in t.get("servers", [])}
            p_ids = {s["server_id"] for s in p.get("servers", [])}
            missing = t_ids - p_ids
            if missing:
                raise AssertionError(
                    f"Registry divergence: entries in registry truth not surfaced by payload: {sorted(missing)}"
                )
            extra = p_ids - t_ids
            if extra:
                raise AssertionError(
                    f"Registry divergence: entries in payload not in registry truth: {sorted(extra)}"
                )

        # Valid payload passes
        assert_no_registry_divergence(valid_payload, truth)

        # Omission of a registry entry MUST fail
        divergent_payload = {
            **valid_payload,
            "servers": [s for s in valid_payload["servers"] if s["server_id"] != "slack-prod"],
        }
        with pytest.raises(AssertionError, match="Registry divergence.*slack-prod"):
            assert_no_registry_divergence(divergent_payload, truth)

        # Extra entry not in registry truth MUST fail
        divergent_extra = {
            **valid_payload,
            "servers": [
                *valid_payload["servers"],
                {"server_id": "ghost-server", "connector": "ghost"},
            ],
        }
        with pytest.raises(AssertionError, match="Registry divergence.*ghost-server"):
            assert_no_registry_divergence(divergent_extra, truth)
