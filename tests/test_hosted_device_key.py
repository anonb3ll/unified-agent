"""Hosted ua-node enrollment bound to a per-device Ed25519 key, not a tailnet peer.

Acceptance:
1. ``test_hosted_device_login_registers_heartbeats_and_claims``
2. ``test_copied_token_without_proof_or_different_key_is_refused``
3. ``test_static_bearer_cannot_register_or_drain_device_bound_node``
4. ``test_tailnet_peer_binding_ignores_supplied_device_key`` (plus the existing
   tailnet suites, which this file does not modify)
5. ``test_old_token_without_device_key_claim_says_rerun_login``
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
from email.message import Message

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import device_key, device_login, node_queue, oauth, oauth_approver, server, webui

HUB = "http://127.0.0.1:8765"
AGENT = "Desktop_Agent1"
STATIC = "static-hosted-token"
NODE = "desk-hosted"
CLI_CLIENT = "unified-agent-cli"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
TAILNET = {"node": "desktop.tail1234.ts.net", "node_id": "nABC123", "user": "operator@example.com"}


@pytest.fixture(autouse=True)
def _reset_oauth():
    oauth.reset_provider()
    yield
    oauth.reset_provider()


def _hub_app(monkeypatch, *, hosted: bool) -> TestClient:
    if hosted:
        monkeypatch.setenv("UA_HOSTED", "1")
        # Enrollment is not the acceptance gate. That gate stays on by default
        # for a hosted hub; this suite turns it off so a normal item can be claimed.
        monkeypatch.setenv("UA_NODE_REQUIRE_ACCEPTANCE", "0")
    else:
        monkeypatch.delenv("UA_HOSTED", raising=False)
        monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
        monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE_FALLBACK", "1")
    monkeypatch.setenv("UA_MCP_OAUTH", "1")
    monkeypatch.setenv("UA_MCP_OAUTH_ISSUER", HUB)
    monkeypatch.setenv("UA_MCP_OAUTH_PASSPHRASE", "hub-pass")
    monkeypatch.setenv("UA_MCP_OAUTH_AGENT", AGENT)
    monkeypatch.setenv("UA_AGENT_TOKENS", f"{AGENT}:{STATIC}")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
    oauth.reset_provider()
    app = server.HubAuthASGIMiddleware(
        Starlette(routes=[*webui.web_routes(), *oauth.http_routes()])
    )
    return TestClient(app, follow_redirects=False)


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf" value="([^"]*)"', page)
    return match.group(1) if match else ""


def _approve(
    client: TestClient, user_code: str, *, passphrase: str | None = None
) -> tuple[str, object]:
    page = client.get("/oauth/device", params={"user_code": user_code})
    form = {
        "user_code": user_code,
        "agent": AGENT,
        "action": "approve",
        "csrf": _csrf(page.text),
    }
    if passphrase:
        form["passphrase"] = passphrase
    return page.text, client.post("/oauth/device", data=form)


def _login(client: TestClient, out, *, send_device_key: bool = True, passphrase: str | None = None):
    pages: list[str] = []
    forms: list[dict] = []

    def post(url: str, form: dict) -> tuple[int, dict]:
        path = url.split("127.0.0.1:8765", 1)[1]
        sent = dict(form)
        if not send_device_key:
            sent.pop(device_key.FORM_FIELD, None)
        forms.append(sent)
        resp = client.post(path, data=sent)
        if path == "/oauth/device_authorization" and resp.status_code == 200:
            page, approved = _approve(client, resp.json()["user_code"], passphrase=passphrase)
            pages.append(page)
            assert approved.status_code == 200, approved.text
        return resp.status_code, resp.json()

    def get(url: str, token: str) -> tuple[int, dict]:
        path = url.split("127.0.0.1:8765", 1)[1]
        resp = client.get(path, headers={"Authorization": f"Bearer {token}"})
        return resp.status_code, resp.json()

    echoes: list[str] = []
    result = device_login.login(
        HUB,
        out_path=out,
        post=post,
        sleep=lambda _seconds: None,
        echo=echoes.append,
        get=get,
        agent_hint=AGENT,
    )
    return result, echoes, pages, forms


class _Response:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def _bridge(client: TestClient, proofs: list[str]):
    """Send HubTransport's exact bytes through the in-process hub."""

    def open_no_redirect(req, timeout):  # noqa: ARG001 - HubTransport's signature
        parsed = urllib.parse.urlsplit(req.full_url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        headers = {
            key: value
            for key, value in req.header_items()
            if key.lower() not in {"host", "content-length"}
        }
        if any(key.lower() == device_key.PROOF_HEADER.lower() for key in headers):
            proofs.append(path)
        resp = client.post(path, content=req.data, headers=headers)
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url,
                resp.status_code,
                "error",
                Message(),
                io.BytesIO(resp.content),
            )
        return _Response(resp.status_code, resp.content)

    return open_no_redirect


def _transport(monkeypatch, client: TestClient, out, proofs: list[str]):
    from unified_agent.node_agent import HubTransport

    monkeypatch.setattr(device_login, "open_no_redirect", _bridge(client, proofs))
    return HubTransport(HUB, token_path=out)


def _work(monkeypatch, client, out) -> tuple[list[str], object]:
    proofs: list[str] = []
    transport = _transport(monkeypatch, client, out, proofs)
    return proofs, transport


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr(
        oauth_approver,
        "verify",
        lambda _request, _form, *, binding: oauth_approver.Approval(  # noqa: ARG005
            oauth_approver.VIA_WORKOS, "Operator"
        ),
    )
    with _hub_app(monkeypatch, hosted=True) as client:
        yield client


def test_hosted_device_login_registers_heartbeats_and_claims(hosted, monkeypatch, tmp_path):
    out = tmp_path / "oauth-token.json"
    _result, echoes, pages, forms = _login(hosted, out)
    key_file = device_key.key_path(out)
    signing = json.loads(key_file.read_text())
    thumb = device_key.thumbprint(signing)
    private = signing["d"]

    assert oct(os.stat(key_file).st_mode & 0o777) == "0o600"
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"
    assert f"Device key thumbprint: {thumb}" in "\n".join(echoes)
    assert thumb in pages[0]
    assert "device-key thumbprint" in pages[0]
    assert private not in out.read_text()
    assert private not in pages[0]
    assert all(private not in json.dumps(form) for form in forms)
    assert '"d"' not in forms[0][device_key.FORM_FIELD]

    saved = json.loads(out.read_text())
    who = hosted.get("/oauth/whoami", headers={"Authorization": f"Bearer {saved['access_token']}"})
    assert who.status_code == 200, who.text
    assert who.json()["agent"] == AGENT
    assert who.json()["auth_method"] == "device_code"
    assert who.json()["device_node"] == {"node": f"device-key:{thumb}", "node_id": thumb}

    proofs, transport = _work(monkeypatch, hosted, out)
    status, registered = transport(
        "/api/node/register", {"node": NODE, "facts": {"backends": ["codex"]}}, 10
    )
    assert status == 200, registered
    assert registered["agent"] == AGENT
    assert node_queue.list_nodes()[NODE]["device_node"] == f"device-key:{thumb}"

    status, beat = transport("/api/node/heartbeat", {"node": NODE}, 10)
    assert status == 200, beat

    queued = node_queue.enqueue(
        NODE, prompt="ship the grocery list", cwd="/tmp", backend="codex", created_by="A"
    )
    assert queued["status"] == "queued"
    assert "waiting" not in queued
    status, claimed = transport("/api/node/claim", {"node": NODE, "wait_s": 0}, 10)
    assert status == 200, claimed
    assert claimed["item"]["item_id"] == queued["item_id"]
    assert claimed["item"]["prompt"] == "ship the grocery list"
    assert node_queue.get_item(queued["item_id"])["status"] == "claimed"
    assert proofs == ["/api/node/register", "/api/node/heartbeat", "/api/node/claim"]

    refreshed = hosted.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": saved["refresh_token"],
            "client_id": CLI_CLIENT,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    new = refreshed.json()
    again = hosted.get("/oauth/whoami", headers={"Authorization": f"Bearer {new['access_token']}"})
    assert again.json()["device_node"]["node_id"] == thumb
    assert again.json()["auth_method"] == "device_code"
    saved["access_token"] = new["access_token"]
    saved["refresh_token"] = new["refresh_token"]
    saved["expires_at"] = int(time.time()) + 3600
    out.write_text(json.dumps(saved))
    os.chmod(out, 0o600)
    status, beat = transport("/api/node/heartbeat", {"node": NODE}, 10)
    assert status == 200, beat
    assert proofs[-1] == "/api/node/heartbeat"


def test_copied_token_without_proof_or_different_key_is_refused(hosted, tmp_path):
    out = tmp_path / "oauth-token.json"
    _login(hosted, out)
    saved = json.loads(out.read_text())
    token = saved["access_token"]
    body = b'{"node":"desk-hosted"}'
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    missing = hosted.post("/api/node/register", content=body, headers=headers)
    assert missing.status_code == 403
    assert missing.json()["error"] == device_key.MISSING_PROOF

    other = device_key.load_or_create(tmp_path / "other-token.json")
    foreign = device_key.sign_proof(
        other, method="POST", path="/api/node/register", access_token=token, body=body
    )
    wrong_key = hosted.post(
        "/api/node/register",
        content=body,
        headers={**headers, device_key.PROOF_HEADER: foreign},
    )
    assert wrong_key.status_code == 403
    assert wrong_key.json()["error"] == device_key.BAD_PROOF

    real = device_key.load_or_create(out)
    proof = device_key.sign_proof(
        real, method="POST", path="/api/node/register", access_token=token, body=body
    )
    mismatched = hosted.post(
        "/api/node/register",
        content=b'{"node":"desk-hosted","facts":{}}',
        headers={**headers, device_key.PROOF_HEADER: proof},
    )
    assert mismatched.status_code == 403
    assert mismatched.json()["error"] == device_key.BAD_PROOF

    config_get = hosted.get(
        "/api/node/config", params={"node": NODE}, headers={"Authorization": f"Bearer {token}"}
    )
    assert config_get.status_code == 403
    assert config_get.json()["error"] == device_key.MISSING_PROOF
    assert NODE not in node_queue.list_nodes()


def test_static_bearer_cannot_register_or_drain_device_bound_node(hosted, monkeypatch, tmp_path):
    out = tmp_path / "oauth-token.json"
    _login(hosted, out)
    _proofs, transport = _work(monkeypatch, hosted, out)
    status, registered = transport(
        "/api/node/register", {"node": NODE, "facts": {"backends": ["codex"]}}, 10
    )
    assert status == 200, registered
    bound = node_queue.list_nodes()[NODE]["device_node"]
    assert bound.startswith("device-key:")

    queued = node_queue.enqueue(
        NODE, prompt="do not drain", cwd="/tmp", backend="codex", created_by="A"
    )
    monkeypatch.setenv("UA_NODE_ALLOW_BEARER", "1")
    headers = {"Authorization": f"Bearer {STATIC}"}
    refused_register = hosted.post("/api/node/register", json={"node": NODE}, headers=headers)
    refused_claim = hosted.post(
        "/api/node/claim", json={"node": NODE, "wait_s": 0}, headers=headers
    )
    assert refused_register.status_code == 403
    assert refused_claim.status_code == 403
    assert "device sign-in" in refused_register.json()["error"]
    assert "device sign-in" in refused_claim.json()["error"]
    assert node_queue.get_item(queued["item_id"])["status"] == "queued"
    assert node_queue.list_nodes()[NODE]["device_node"] == bound


def test_old_token_without_device_key_claim_says_rerun_login(hosted, tmp_path):
    out = tmp_path / "old-token.json"
    _login(hosted, out, send_device_key=False)
    saved = json.loads(out.read_text())
    who = hosted.get(
        "/oauth/whoami", headers={"Authorization": f"Bearer {saved['access_token']}"}
    ).json()
    assert who["agent"] == AGENT
    assert not who.get("device_node")

    headers = {"Authorization": f"Bearer {saved['access_token']}"}
    refused = hosted.post("/api/node/register", json={"node": NODE}, headers=headers)
    assert refused.status_code == 403
    assert "hosted registration requires a device-bound sign-in" in refused.json()["error"]
    assert "unified-agent login" in refused.json()["error"]

    node_queue.register(NODE, identity=AGENT, device_node="legacy-host")
    claim = hosted.post("/api/node/claim", json={"node": NODE, "wait_s": 0}, headers=headers)
    assert claim.status_code == 403
    assert "hosted nodes require a device-bound sign-in" in claim.json()["error"]
    assert "unified-agent login" in claim.json()["error"]


def test_malformed_device_key_is_refused_and_absent_key_stays_unbound(hosted, tmp_path):
    bad = hosted.post(
        "/oauth/device_authorization",
        data={"client_id": CLI_CLIENT, "device_key": "not-a-jwk"},
    )
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_request"
    private = device_key.load_or_create(tmp_path / "unused-token.json")
    # The form must not accept a private scalar even when the public half is valid.
    leaked_jwk = {"kty": "OKP", "crv": "Ed25519", "x": private["x"]}
    leaked_jwk["d"] = private["d"]
    leaked = json.dumps(leaked_jwk, separators=(",", ":"), sort_keys=True)
    refused = hosted.post(
        "/oauth/device_authorization",
        data={"client_id": CLI_CLIENT, "device_key": leaked},
    )
    assert refused.status_code == 400

    started = hosted.post("/oauth/device_authorization", data={"client_id": CLI_CLIENT})
    assert started.status_code == 200, started.text
    _page, approved = _approve(hosted, started.json()["user_code"])
    assert approved.status_code == 200, approved.text
    token = hosted.post(
        "/token",
        data={
            "grant_type": DEVICE_GRANT,
            "device_code": started.json()["device_code"],
            "client_id": CLI_CLIENT,
        },
    )
    assert token.status_code == 200, token.text
    who = hosted.get(
        "/oauth/whoami",
        headers={"Authorization": f"Bearer {token.json()['access_token']}"},
    ).json()
    assert not who.get("device_node")


def test_tailnet_peer_binding_ignores_supplied_device_key(monkeypatch, tmp_path):
    monkeypatch.setattr(oauth, "_resolve_peer_device", lambda _ip: dict(TAILNET))
    with _hub_app(monkeypatch, hosted=False) as client:
        garbage = client.post(
            "/oauth/device_authorization",
            data={"client_id": CLI_CLIENT, "device_key": "not-a-jwk"},
        )
        assert garbage.status_code == 200, garbage.text
        client.post(
            "/oauth/device",
            data={"user_code": garbage.json()["user_code"], "action": "deny"},
        )

        out = tmp_path / "tailnet-token.json"
        _result, _echoes, pages, forms = _login(client, out, passphrase="hub-pass")
        assert device_key.FORM_FIELD in forms[0]
        assert "desktop.tail1234.ts.net" in pages[0]
        assert "device-key thumbprint" not in pages[0]
        saved = json.loads(out.read_text())
        who = client.get(
            "/oauth/whoami", headers={"Authorization": f"Bearer {saved['access_token']}"}
        ).json()
        assert who["device_node"]["node"] == TAILNET["node"]
        assert who["device_node"]["node_id"] == TAILNET["node_id"]
        assert not str(who["device_node"]["node"]).startswith(device_key.PREFIX)

        proofs: list[str] = []
        transport = _transport(monkeypatch, client, out, proofs)
        status, registered = transport("/api/node/register", {"node": NODE}, 10)
        assert status == 200, registered
        assert node_queue.list_nodes()[NODE]["device_node"] == TAILNET["node"]
        # The key file exists, so the client attaches a proof; a tailnet binding
        # does not require it. The same call without the header still registers.
        bare = client.post(
            "/api/node/register",
            json={"node": NODE},
            headers={"Authorization": f"Bearer {saved['access_token']}"},
        )
        assert bare.status_code == 200, bare.text
        assert proofs == ["/api/node/register"]


def test_device_key_proof_rejects_stale_and_future(tmp_path):
    out = tmp_path / "token.json"
    key = device_key.load_or_create(out)
    thumb = device_key.thumbprint(key)
    body = b"{}"
    proof = device_key.sign_proof(
        key,
        method="POST",
        path="/api/node/register",
        access_token="access",
        body=body,
        now=1_000,
    )
    kwargs = {
        "method": "POST",
        "path": "/api/node/register",
        "access_token": "access",
        "body": body,
        "expected_node_id": thumb,
    }
    device_key.verify_proof(proof, now=1_030, **kwargs)
    # Each successful verification is a separate request with a unique ID;
    # replaying the earlier proof is intentionally refused.
    proof = device_key.sign_proof(
        key,
        method="POST",
        path="/api/node/register",
        access_token="access",
        body=body,
        now=1_000,
    )
    device_key.verify_proof(proof, now=1_120, **kwargs)
    with pytest.raises(device_key.DeviceKeyError, match="rerun unified-agent login"):
        device_key.verify_proof(proof, now=1_121, **kwargs)
    with pytest.raises(device_key.DeviceKeyError, match="rerun unified-agent login"):
        device_key.verify_proof(proof, now=939, **kwargs)
    with pytest.raises(device_key.DeviceKeyError, match="rerun unified-agent login"):
        device_key.verify_proof("", now=1_030, **kwargs)
