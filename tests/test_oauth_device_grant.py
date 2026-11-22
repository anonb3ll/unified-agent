"""RFC 8628 device authorization grant on the hub OAuth server (task #1320, Epic J).

A headless agent (CLI, node daemon, cloud worker) signs in the way a modern
connector does: it asks the hub for a short user code, a human approves that
code on any browser, and the device polls for a refreshable token. The token
carries the requesting device's tailnet node so the evidence chain can bind
actions to a device, not just to a shared bearer.
"""

from __future__ import annotations

import json
import os
import re

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from unified_agent import config, oauth
from unified_agent.server import HubAuthASGIMiddleware

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLI_CLIENT = "unified-agent-cli"
NODE = {"node": "desktop.tail1234.ts.net", "node_id": "nABC123", "user": "operator@example.com"}


@pytest.fixture(autouse=True)
def _reset_oauth():
    oauth.reset_provider()
    yield
    oauth.reset_provider()


class _Clock:
    def __init__(self) -> None:
        self.now = 1_900_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(oauth, "_now", fake)
    return fake


@pytest.fixture
def peer_node(monkeypatch):
    seen: list[str | None] = []

    def fake_resolve(ip):
        seen.append(ip)
        return dict(NODE)

    monkeypatch.setattr(oauth, "_resolve_peer_device", fake_resolve)
    return seen


def _app(monkeypatch, *, public_hosts: str = "") -> TestClient:
    monkeypatch.setenv("UA_MCP_OAUTH", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", "http://127.0.0.1:8765")
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", "hub-pass")
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_AGENT", "Desktop_Agent8")
    monkeypatch.setenv("UA_AGENT_TOKENS", "Desktop_Agent1:static-token")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    if public_hosts:
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", public_hosts)
    oauth.reset_provider()

    async def homepage(request):
        return JSONResponse({"identity": config.verified_agent_identity()})

    # Not "/": exact "/" is public and redirects in the real hub (task
    # #1400), which would defeat the 401/identity assertions below.
    app = Starlette(routes=[Route("/whoami", homepage, methods=["GET"]), *oauth.http_routes()])
    return TestClient(HubAuthASGIMiddleware(app), follow_redirects=False)


def _start(client: TestClient, client_id: str = CLI_CLIENT) -> dict:
    resp = client.post("/oauth/device_authorization", data={"client_id": client_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _poll(client: TestClient, device_code: str, client_id: str = CLI_CLIENT):
    return client.post(
        "/token",
        data={"grant_type": DEVICE_GRANT, "device_code": device_code, "client_id": client_id},
    )


def _csrf(client: TestClient, user_code: str, **kwargs) -> str:
    page = client.get("/oauth/device", params={"user_code": user_code}, **kwargs).text
    match = re.search(r'name="csrf" value="([^"]*)"', page)
    return match.group(1) if match else ""


def _approve(client: TestClient, user_code: str, *, agent: str = "Desktop_Agent2", **extra):
    form = {
        "user_code": user_code,
        "passphrase": "hub-pass",
        "agent": agent,
        "action": "approve",
        "csrf": _csrf(client, user_code),
    }
    form.update(extra)
    return client.post("/oauth/device", data=form)


class TestDeviceGrant:
    def test_metadata_advertises_device_endpoint_and_grant(self, monkeypatch):
        client = _app(monkeypatch)
        meta = client.get("/.well-known/oauth-authorization-server").json()
        assert meta["device_authorization_endpoint"] == (
            "http://127.0.0.1:8765/oauth/device_authorization"
        )
        assert DEVICE_GRANT in meta["grant_types_supported"]
        assert meta["token_endpoint"].endswith("/token")

    def test_device_authorization_response_shape(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        body = _start(client)
        assert re.fullmatch(
            r"[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}", body["user_code"]
        )
        assert len(body["device_code"]) >= 32
        assert body["verification_uri"] == "http://127.0.0.1:8765/oauth/device"
        assert body["verification_uri_complete"].endswith(
            "/oauth/device?user_code=" + body["user_code"]
        )
        assert body["expires_in"] == 600
        assert body["interval"] == 5
        assert peer_node, "requesting peer must be resolved for device binding"

    def test_full_flow_issues_device_bound_token(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        grant = _start(client)

        pending = _poll(client, grant["device_code"])
        assert pending.status_code == 400
        assert pending.json()["error"] == "authorization_pending"

        page = client.get("/oauth/device", params={"user_code": grant["user_code"]})
        assert page.status_code == 200
        assert grant["user_code"] in page.text
        assert "unified-agent-cli" in page.text

        approved = _approve(client, grant["user_code"].lower().replace("-", " "))
        assert approved.status_code == 200, approved.text
        assert "approved" in approved.text.lower()

        clock.now += 6
        issued = _poll(client, grant["device_code"])
        assert issued.status_code == 200, issued.text
        token = issued.json()
        assert token["token_type"] == "Bearer"
        assert token["refresh_token"]

        me = client.get("/whoami", headers={"Authorization": f"Bearer {token['access_token']}"})
        assert me.json()["identity"] == "Desktop_Agent2"

        who = client.get(
            "/oauth/whoami", headers={"Authorization": f"Bearer {token['access_token']}"}
        ).json()
        assert who["agent"] == "Desktop_Agent2"
        assert who["auth_method"] == "device_code"
        assert who["device_node"] == {"node": NODE["node"], "node_id": NODE["node_id"]}

    def test_device_code_is_single_use(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        grant = _start(client)
        assert _approve(client, grant["user_code"]).status_code == 200
        assert _poll(client, grant["device_code"]).status_code == 200
        clock.now += 6
        again = _poll(client, grant["device_code"])
        assert again.status_code == 400
        assert again.json()["error"] == "invalid_grant"

    def test_polling_faster_than_interval_is_slow_down(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        grant = _start(client)
        assert _poll(client, grant["device_code"]).json()["error"] == "authorization_pending"
        clock.now += 1
        assert _poll(client, grant["device_code"]).json()["error"] == "slow_down"
        clock.now += 11
        assert _poll(client, grant["device_code"]).json()["error"] == "authorization_pending"

    def test_denied_request_returns_access_denied(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        grant = _start(client)
        denied = client.post(
            "/oauth/device", data={"user_code": grant["user_code"], "action": "deny"}
        )
        assert denied.status_code == 200
        assert _poll(client, grant["device_code"]).json()["error"] == "access_denied"

    def test_expired_code(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        grant = _start(client)
        clock.now += 601
        assert _poll(client, grant["device_code"]).json()["error"] == "expired_token"
        late = _approve(client, grant["user_code"])
        assert late.status_code in (400, 403)  # refused: no live form token for an expired code

    def test_wrong_passphrase_leaves_request_pending(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        grant = _start(client)
        refused = _approve(client, grant["user_code"], passphrase="nope")
        assert refused.status_code == 403
        assert _poll(client, grant["device_code"]).json()["error"] == "authorization_pending"

    def test_unknown_agent_label_refused(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        grant = _start(client)
        refused = _approve(client, grant["user_code"], agent="Desktop_NotInCatalog")
        assert refused.status_code == 400
        assert _poll(client, grant["device_code"]).json()["error"] == "authorization_pending"

    def test_unknown_client_rejected(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        resp = client.post("/oauth/device_authorization", data={"client_id": "nobody"})
        assert resp.status_code == 401
        assert resp.json()["error"] == "invalid_client"

    def test_device_code_bound_to_requesting_client(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        grant = _start(client)
        _approve(client, grant["user_code"])
        stolen = _poll(client, grant["device_code"], client_id="some-other-client")
        assert stolen.status_code in (400, 401)
        assert stolen.json()["error"] in ("invalid_grant", "invalid_client")

    def test_public_host_pins_identity(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch, public_hosts="hub.example.com")
        grant = _start(client)
        refused = client.post(
            "/oauth/device",
            data={
                "user_code": grant["user_code"],
                "passphrase": "hub-pass",
                "agent": "Desktop_Agent2",
                "action": "approve",
                "csrf": _csrf(client, grant["user_code"]),
            },
            headers={"host": "hub.example.com"},
        )
        assert refused.status_code == 403
        ok = client.post(
            "/oauth/device",
            data={
                "user_code": grant["user_code"],
                "passphrase": "hub-pass",
                "action": "approve",
                "csrf": _csrf(client, grant["user_code"]),
            },
            headers={"host": "hub.example.com"},
        )
        assert ok.status_code == 200
        token = _poll(client, grant["device_code"]).json()["access_token"]
        me = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
        assert me.json()["identity"] == "Desktop_Agent8"

    def test_refresh_keeps_identity_and_device(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        grant = _start(client)
        _approve(client, grant["user_code"])
        first = _poll(client, grant["device_code"]).json()
        refreshed = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                "client_id": CLI_CLIENT,
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        new_access = refreshed.json()["access_token"]
        who = client.get("/oauth/whoami", headers={"Authorization": f"Bearer {new_access}"}).json()
        assert who["agent"] == "Desktop_Agent2"
        assert who["device_node"]["node_id"] == NODE["node_id"]
        old = client.get("/whoami", headers={"Authorization": f"Bearer {first['access_token']}"})
        assert old.status_code == 401

    def test_approval_is_audited_with_both_nodes(self, monkeypatch, clock, peer_node):
        audited: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            oauth,
            "_audit_oauth",
            lambda ch, payload, originator=None: audited.append((ch, payload)),
        )
        client = _app(monkeypatch)
        grant = _start(client)
        _approve(client, grant["user_code"])
        channels = [c for c, _ in audited]
        assert "hub.oauth.device_authorization" in channels
        approval = next(p for c, p in audited if c == "hub.oauth.device_approved")
        assert approval["subject"] == "Desktop_Agent2"
        assert approval["device_node"]["node_id"] == NODE["node_id"]
        assert approval["approver_node"]["node_id"] == NODE["node_id"]
        assert "device_code" not in json.dumps(approval)


class TestLoginCli:
    def test_device_login_writes_private_token_file(self, monkeypatch, clock, peer_node, tmp_path):
        from unified_agent import device_login

        client = _app(monkeypatch)
        prompts: list[str] = []

        def post(url: str, form: dict) -> tuple[int, dict]:
            path = url.split("127.0.0.1:8765", 1)[1]
            resp = client.post(path, data=form)
            if path == "/oauth/device_authorization":
                # Approve out of band as soon as the code is shown, like a phone would.
                _approve(client, resp.json()["user_code"])
            return resp.status_code, resp.json()

        def sleep(seconds: float) -> None:
            clock.now += seconds

        def get(url: str, token: str) -> tuple[int, dict]:
            path = url.split("127.0.0.1:8765", 1)[1]
            resp = client.get(path, headers={"Authorization": f"Bearer {token}"})
            return resp.status_code, resp.json()

        out = tmp_path / "oauth-token.json"
        result = device_login.login(
            "http://127.0.0.1:8765",
            out_path=out,
            post=post,
            sleep=sleep,
            echo=prompts.append,
            get=get,
        )
        assert result["agent_hint"] is None
        saved = json.loads(out.read_text())
        assert saved["hub"] == "http://127.0.0.1:8765"
        assert saved["client_id"] == CLI_CLIENT
        assert saved["agent"] == "Desktop_Agent2"
        assert result["agent"] == "Desktop_Agent2"
        assert saved["refresh_token"]
        assert oct(os.stat(out).st_mode & 0o777) == "0o600"
        joined = "\n".join(prompts)
        assert "/oauth/device?user_code=" in joined
        assert saved["access_token"] not in joined
        assert saved["refresh_token"] not in joined

    def test_device_login_reports_denial(self, monkeypatch, clock, peer_node, tmp_path):
        from unified_agent import device_login

        client = _app(monkeypatch)

        def post(url: str, form: dict) -> tuple[int, dict]:
            path = url.split("127.0.0.1:8765", 1)[1]
            resp = client.post(path, data=form)
            if path == "/oauth/device_authorization":
                client.post(
                    "/oauth/device", data={"user_code": resp.json()["user_code"], "action": "deny"}
                )
            return resp.status_code, resp.json()

        with pytest.raises(device_login.LoginError, match="access_denied"):
            device_login.login(
                "http://127.0.0.1:8765",
                out_path=tmp_path / "t.json",
                post=post,
                sleep=lambda s: None,
                echo=lambda s: None,
                get=lambda u, t: (401, {}),
            )
        assert not (tmp_path / "t.json").exists()


class TestDeviceGrantHardening:
    """Regressions for the PR #750 Devin review findings."""

    def test_approval_without_form_token_is_refused(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        grant = _start(client)
        forged = _approve(client, grant["user_code"], csrf="")
        assert forged.status_code == 403
        other = _start(client)
        wrong = _approve(client, grant["user_code"], csrf=_csrf(client, other["user_code"]))
        assert wrong.status_code == 403
        assert _poll(client, grant["device_code"]).json()["error"] == "authorization_pending"

    def test_cross_origin_post_is_refused(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        grant = _start(client)
        resp = client.post(
            "/oauth/device",
            data={
                "user_code": grant["user_code"],
                "passphrase": "hub-pass",
                "action": "approve",
                "csrf": _csrf(client, grant["user_code"]),
            },
            headers={"origin": "https://evil.example"},
        )
        assert resp.status_code == 403
        same = _approve(client, grant["user_code"], agent="Desktop_Agent2")
        assert same.status_code == 200

    def test_deny_cannot_override_a_won_approval(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        grant = _start(client)
        provider = oauth.get_provider()
        stale = provider.pending_device_grant(grant["user_code"])
        provider.approve_device(grant["user_code"], "Desktop_Agent2")
        # A deny that read the grant before approval won must not flip it.
        monkeypatch.setattr(provider, "pending_device_grant", lambda code: stale)
        assert provider.deny_device(grant["user_code"]) is False
        monkeypatch.undo()
        _app(monkeypatch)  # keep env for the poll below
        assert stale.status == "approved"

    def test_configured_bot_with_reserved_id_is_not_shadowed(self, monkeypatch):
        monkeypatch.setenv(
            "UA_MCP_OAUTH_CLIENT_CREDENTIALS", "unified-agent-cli|bot-secret|server_agent2"
        )
        _app(monkeypatch)
        client = oauth.get_provider().clients["unified-agent-cli"]
        assert client.client_secret == "bot-secret"

    async def test_registration_cannot_claim_reserved_id(self, monkeypatch):
        from mcp.server.auth.provider import RegistrationError
        from mcp.shared.auth import OAuthClientInformationFull

        _app(monkeypatch)
        provider = oauth.get_provider()
        with pytest.raises(RegistrationError):
            await provider.register_client(
                OAuthClientInformationFull(client_id="unified-agent-cli", redirect_uris=[])
            )
        assert provider.clients["unified-agent-cli"].token_endpoint_auth_method == "none"

    def test_agent_hint_prefills_approval_page(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        resp = client.post(
            "/oauth/device_authorization",
            data={"client_id": CLI_CLIENT, "agent_hint": "desktop_agent2"},
        )
        user_code = resp.json()["user_code"]
        page = client.get("/oauth/device", params={"user_code": user_code}).text
        assert 'value="Desktop_Agent2"' in page
        bad = client.post(
            "/oauth/device_authorization",
            data={"client_id": CLI_CLIENT, "agent_hint": "Desktop_NotInCatalog"},
        ).json()["user_code"]
        assert (
            'value="Desktop_Agent8"' in client.get("/oauth/device", params={"user_code": bad}).text
        )


class TestStoredToken:
    def test_fresh_token_returned_without_network(self, tmp_path):
        from unified_agent import device_login

        path = tmp_path / "t.json"
        path.write_text(json.dumps({"access_token": "A", "expires_at": 2_000, "hub": "h"}))

        def no_post(url, form):
            raise AssertionError("must not refresh a fresh token")

        assert device_login.stored_access_token(path, post=no_post, now=lambda: 1_000) == "A"

    def test_near_expiry_refreshes_and_rewrites_privately(self, tmp_path):
        from unified_agent import device_login

        path = tmp_path / "t.json"
        path.write_text(
            json.dumps(
                {
                    "hub": "http://h",
                    "client_id": CLI_CLIENT,
                    "access_token": "OLD",
                    "refresh_token": "R1",
                    "expires_at": 1_030,
                }
            )
        )
        calls = []

        def post(url, form):
            calls.append((url, form["grant_type"], form["refresh_token"]))
            return 200, {"access_token": "NEW", "refresh_token": "R2", "expires_in": 3600}

        assert device_login.stored_access_token(path, post=post, now=lambda: 1_000) == "NEW"
        assert calls == [("http://h/token", "refresh_token", "R1")]
        saved = json.loads(path.read_text())
        assert saved["refresh_token"] == "R2"
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"

    def test_refused_refresh_and_missing_file_return_none(self, tmp_path):
        from unified_agent import device_login

        assert device_login.stored_access_token(tmp_path / "absent.json") is None
        path = tmp_path / "t.json"
        path.write_text(json.dumps({"hub": "http://h", "refresh_token": "R", "expires_at": 0}))
        refused = device_login.stored_access_token(
            path, post=lambda u, f: (400, {"error": "invalid_grant"}), now=lambda: 1_000
        )
        assert refused is None

    def test_planted_symlink_never_receives_tokens(self, tmp_path):
        from unified_agent import device_login

        victim = tmp_path / "victim.txt"
        victim.write_text("untouched")
        target = tmp_path / "oauth-token.json"
        (tmp_path / "oauth-token.json.tmp").symlink_to(victim)
        device_login._write_private(target, {"access_token": "SECRET"})
        assert victim.read_text() == "untouched"
        assert json.loads(target.read_text())["access_token"] == "SECRET"

    def test_refuses_plain_http_to_non_tailnet_host(self, tmp_path):
        from unified_agent import device_login

        def never(url, form):
            raise AssertionError("must not contact an insecure hub")

        with pytest.raises(device_login.LoginError, match="plain HTTP"):
            device_login.login(
                "http://hub.example.com:8765", out_path=tmp_path / "t.json", post=never
            )
        for ok in (
            "https://hub.example.com",
            "http://127.0.0.1:8765",
            "http://100.64.0.10:8765",
            "http://mac.tail1234.ts.net:8765",
        ):
            assert device_login.transport_is_protected(ok), ok
        assert not device_login.transport_is_protected("http://10.0.0.5:8765")


class TestStoredTokenScoping:
    def _saved(self, tmp_path, **extra):
        path = tmp_path / "t.json"
        data = {
            "hub": "http://100.64.0.10:8765",
            "agent": "Desktop_Agent2",
            "client_id": CLI_CLIENT,
            "access_token": "A",
            "refresh_token": "R",
            "expires_at": 2_000,
        }
        data.update(extra)
        path.write_text(json.dumps(data))
        return path

    def test_only_used_for_matching_hub_and_agent(self, tmp_path):
        from unified_agent import device_login

        path = self._saved(tmp_path)
        now = lambda: 1_000  # noqa: E731
        assert (
            device_login.stored_access_token(
                path, hub="http://100.64.0.10:8765/mcp", agent="Desktop_Agent2", now=now
            )
            == "A"
        )
        assert device_login.stored_access_token(path, hub="http://other:8765/mcp", now=now) is None
        assert device_login.stored_access_token(path, agent="Desktop_Agent1", now=now) is None

    def test_offline_issuer_returns_none_instead_of_raising(self, tmp_path):
        from unified_agent import device_login

        path = self._saved(tmp_path, expires_at=0)

        def offline(url, form):
            raise OSError("network unreachable")

        assert device_login.stored_access_token(path, post=offline, now=lambda: 1_000) is None

    def test_lost_refresh_race_uses_the_winners_token(self, tmp_path):
        from unified_agent import device_login

        path = self._saved(tmp_path, expires_at=0)

        def post(url, form):
            # Simulate a concurrent process having rotated first: on disk now,
            # and our (stale) refresh token is refused.
            path.write_text(
                json.dumps(
                    {
                        "hub": "http://100.64.0.10:8765",
                        "agent": "Desktop_Agent2",
                        "access_token": "WINNER",
                        "refresh_token": "R2",
                        "expires_at": 9_999,
                    }
                )
            )
            return 400, {"error": "invalid_grant"}

        first = device_login.stored_access_token(path, post=post, now=lambda: 1_000)
        # After the refused refresh we re-read under the lock and pick up the
        # winner's rotated token in the same call.
        assert first == "WINNER"
        assert device_login.stored_access_token(path, now=lambda: 1_000) == "WINNER"


class TestDeviceGrantReview3:
    """Regressions for the third Devin review on PR #750."""

    def test_manual_verification_uri_can_approve(self, monkeypatch, clock, peer_node):
        """Bare /oauth/device shows a lookup form; the typed code then gets a
        code-bound approval form (review 4: no token that works across codes)."""
        client = _app(monkeypatch)
        grant = _start(client)
        lookup = client.get("/oauth/device").text
        assert 'method="get"' in lookup and 'name="csrf"' not in lookup
        token = _csrf(client, grant["user_code"])
        assert token
        resp = client.post(
            "/oauth/device",
            data={
                "user_code": grant["user_code"],
                "passphrase": "hub-pass",
                "agent": "Desktop_Agent2",
                "action": "approve",
                "csrf": token,
            },
        )
        assert resp.status_code == 200
        assert _poll(client, grant["device_code"]).status_code == 200

    def test_https_without_hostname_is_rejected(self):
        from unified_agent import device_login

        assert not device_login.transport_is_protected("https:///oauth")
        assert not device_login.transport_is_protected("https:")

    def test_confidential_device_client_requires_secret(self, monkeypatch, clock, peer_node):
        from mcp.shared.auth import OAuthClientInformationFull

        from unified_agent import oauth as oauth_mod

        client = _app(monkeypatch)
        provider = oauth_mod.get_provider()
        assert provider is not None
        provider.clients["device-bot"] = OAuthClientInformationFull(
            client_id="device-bot",
            client_secret="bot-secret",
            client_name="Device Bot",
            client_id_issued_at=1,
            redirect_uris=[],
            grant_types=[oauth_mod._DEVICE_CODE_GRANT, "refresh_token"],
            response_types=[],
            token_endpoint_auth_method="client_secret_post",
            scope="mcp",
        )
        denied = client.post("/oauth/device_authorization", data={"client_id": "device-bot"})
        assert denied.status_code == 401
        ok = client.post(
            "/oauth/device_authorization",
            data={"client_id": "device-bot", "client_secret": "bot-secret"},
        )
        assert ok.status_code == 200
        body = ok.json()
        poll_denied = _poll(client, body["device_code"], client_id="device-bot")
        assert poll_denied.status_code == 401
        # Approve then poll with secret.
        assert _approve(client, body["user_code"]).status_code == 200
        poll_ok = client.post(
            "/token",
            data={
                "grant_type": DEVICE_GRANT,
                "device_code": body["device_code"],
                "client_id": "device-bot",
                "client_secret": "bot-secret",
            },
        )
        assert poll_ok.status_code == 200

    def test_pending_device_grant_cap(self, monkeypatch, clock, peer_node):
        from unified_agent import oauth as oauth_mod

        monkeypatch.setattr(oauth_mod, "_DEVICE_MAX_PENDING", 2)
        client = _app(monkeypatch)
        assert _start(client)["device_code"]
        assert _start(client)["device_code"]
        third = client.post("/oauth/device_authorization", data={"client_id": CLI_CLIENT})
        assert third.status_code == 503
        assert third.json()["error"] == "temporarily_unavailable"

    def test_refresh_write_failure_drops_stale_file(self, tmp_path, monkeypatch):
        from unified_agent import device_login

        path = tmp_path / "t.json"
        path.write_text(
            json.dumps(
                {
                    "hub": "http://h",
                    "client_id": CLI_CLIENT,
                    "access_token": "OLD",
                    "refresh_token": "R1",
                    "expires_at": 0,
                }
            )
        )

        def post(url, form):
            return 200, {"access_token": "NEW", "refresh_token": "R2", "expires_in": 3600}

        def boom(p, payload):
            raise OSError("disk full")

        monkeypatch.setattr(device_login, "_write_private", boom)
        assert device_login.stored_access_token(path, post=post, now=lambda: 1_000) == "NEW"
        assert not path.exists()


class TestDeviceGrantReview4:
    """Regressions for the fourth Devin review on PR #750."""

    def test_form_token_for_one_code_cannot_approve_another(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        mine = _start(client)
        victim = _start(client)
        stolen = _csrf(client, mine["user_code"])
        resp = client.post(
            "/oauth/device",
            data={
                "user_code": victim["user_code"],
                "passphrase": "hub-pass",
                "action": "approve",
                "csrf": stolen,
            },
        )
        assert resp.status_code == 403
        assert _poll(client, victim["device_code"]).json()["error"] == "authorization_pending"

    def test_unknown_code_lookup_is_404_with_retry_form(self, monkeypatch, clock, peer_node):
        client = _app(monkeypatch)
        resp = client.get("/oauth/device", params={"user_code": "BCDF-GHJK"})
        assert resp.status_code == 404
        assert "not found" in resp.text and 'name="csrf"' not in resp.text

    def test_tilde_and_explicit_token_paths(self, monkeypatch, tmp_path):
        from unified_agent import device_login

        monkeypatch.delenv("UA_OAUTH_TOKEN_PATH", raising=False)
        monkeypatch.setenv("UA_HOME", "~/uahome")
        assert device_login.default_token_path() == (
            device_login.Path.home() / "uahome" / "oauth-token.json"
        )
        monkeypatch.setenv("UA_OAUTH_TOKEN_PATH", str(tmp_path / "custom.json"))
        assert device_login.default_token_path() == tmp_path / "custom.json"

    def test_network_error_during_login_is_a_login_error(self, tmp_path):
        from unified_agent import device_login

        def down(url, form):
            raise device_login.urllib.error.URLError("connection refused")

        with pytest.raises(device_login.LoginError, match="could not reach"):
            device_login.login("http://127.0.0.1:8765", out_path=tmp_path / "t.json", post=down)

    def test_oauth_off_gives_actionable_error(self, tmp_path):
        from unified_agent import device_login

        with pytest.raises(device_login.LoginError, match="OAuth server is off"):
            device_login.login(
                "http://127.0.0.1:8765",
                out_path=tmp_path / "t.json",
                post=lambda u, f: (404, {"error": "oauth disabled"}),
            )

    def test_login_refuses_to_save_an_unconfirmed_identity(
        self, monkeypatch, clock, peer_node, tmp_path
    ):
        from unified_agent import device_login

        client = _app(monkeypatch)

        def post(url: str, form: dict) -> tuple[int, dict]:
            path = url.split("127.0.0.1:8765", 1)[1]
            resp = client.post(path, data=form)
            if path == "/oauth/device_authorization":
                _approve(client, resp.json()["user_code"])
            return resp.status_code, resp.json()

        out = tmp_path / "t.json"
        with pytest.raises(device_login.LoginError, match="could not confirm"):
            device_login.login(
                "http://127.0.0.1:8765",
                out_path=out,
                post=post,
                sleep=lambda s: None,
                echo=lambda s: None,
                get=lambda u, t: (503, {}),
            )
        assert not out.exists()

    def test_signin_without_matching_agent_does_not_unlock_executor(self, tmp_path):
        from unified_agent import device_login

        path = tmp_path / "t.json"
        path.write_text(
            json.dumps({"hub": "http://127.0.0.1:9", "refresh_token": "R", "expires_at": 0})
        )
        assert not device_login.has_saved_signin(
            path, hub="http://127.0.0.1:9", agent="Desktop_Agent2"
        )
        assert device_login.stored_access_token(path, agent="Desktop_Agent2") is None

    def test_bearer_requests_do_not_follow_redirects(self):
        from unified_agent import device_login

        handler = device_login._NoRedirect()
        assert handler.redirect_request(None, None, 302, "Found", {}, "http://evil/") is None

    def test_executor_starts_with_saved_signin_during_issuer_outage(self, monkeypatch, tmp_path):
        from unified_agent import device_login
        from unified_agent.executor import daemon

        path = tmp_path / "t.json"
        path.write_text(
            json.dumps(
                {
                    # Port 9 (discard) on loopback: refresh fails locally and
                    # fast, standing in for an unreachable issuer — no network.
                    "hub": "http://127.0.0.1:9",
                    "agent": "Desktop_Agent2",
                    "access_token": "OLD",
                    "refresh_token": "R",
                    "expires_at": 0,
                }
            )
        )
        monkeypatch.setenv("UA_OAUTH_TOKEN_PATH", str(path))
        for var in ("UA_EXECUTOR_TOKEN", "UA_AGENT_BEARER_TOKEN", "UA_AGENT_TOKENS"):
            monkeypatch.delenv(var, raising=False)
        assert device_login.has_saved_signin(hub="http://127.0.0.1:9/mcp", agent="Desktop_Agent2")
        client = daemon.HubClient(
            agent_label="Desktop_Agent2",
            hub_url="http://127.0.0.1:9/mcp",
            allow_fallback=False,
        )
        assert client.bearer_token is None  # constructed, retries per request
