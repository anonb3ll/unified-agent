"""Session-scoped hub MCP credentials for node-launched ACP sessions (task #1744).

Hub tests drive the real middleware and web routes in-process. ``/mcp`` is a
probe route behind ``HubAuthASGIMiddleware`` that lists the request's tool
surface or calls a registered tool through the governed wrapper, so every
check, narrowing and evidence write runs exactly as on the MCP transport.

Node tests run ``NodeAgent`` against that hub with a fake ACP agent that
advertises ``mcpCapabilities.http`` on request and traces only a SHA-256 of
the Authorization header it receives.

No credential value is ever written in this file: every ``uas_`` value is
built at runtime, and the secret sweep searches for the value the hub minted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from unified_agent import (
    acp_backend,
    config,
    gateway,
    node_agent,
    node_queue,
    node_registry,
    oauth,
    profiles,
    server,
    session_credentials,
    storage,
    tailnet,
    webui,
)
from unified_agent.audit import rules as audit_rules
from unified_agent.telemetry.redaction import redact_sensitive_content

LABEL = "Local_Worker"
PROFILE_LABEL = "Local_Profile"
OTHER_LABEL = "Local_Other"
NODE = "session-node-01"
DEVICE = {"node": "worker-device.example.test", "node_id": "nDEVICE01"}
OTHER_DEVICE = {"node": "other-device.example.test", "node_id": "nDEVICE02"}
DEVICE_TOKEN = f"dev-{secrets.token_urlsafe(12)}"
OTHER_DEVICE_TOKEN = f"dev-{secrets.token_urlsafe(12)}"
STATIC_TOKEN = f"tok-{secrets.token_urlsafe(12)}"
OPERATOR_TOKEN = f"tok-{secrets.token_urlsafe(12)}"
ACP_SID = "acp-session-1744"


def _fake_session_token() -> str:
    """A well-formed value the hub never minted. Built at runtime, never a literal."""
    return session_credentials.PREFIX + secrets.token_urlsafe(32)


# --- hub fixture --------------------------------------------------------------------


class _FakeOAuth:
    """Stands in for the hub OAuth provider: device-code access tokens only.

    ``tokens`` maps an access value to ``(label, claims)``; ``_device_login``
    adds one together with its durable sign-in. Records every token it is
    asked about, so a test can prove a session credential never reached the
    OAuth leg.
    """

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.tokens: dict[str, tuple[str, dict]] = {}

    async def load_access_token(self, token: str):
        self.seen.append(token)
        entry = self.tokens.get(token)
        if entry is None:
            return None
        subject, claims = entry
        return SimpleNamespace(subject=subject, claims=dict(claims), scopes=[])


class _Peer:
    """What ``tailnet.resolve_peer_device`` answers for the test client."""

    def __init__(self) -> None:
        self.device: dict | None = dict(DEVICE)

    def __call__(self, ip):
        return dict(self.device) if self.device is not None else None


def _probe_body(payload) -> Response:
    return Response(json.dumps(payload, default=str), media_type="application/json")


async def _mcp_probe(request):
    """The /mcp stand-in: report the request's surface, or call one governed tool."""
    body = json.loads(await request.body() or b"{}")
    if body.get("op") == "list":
        return _probe_body(
            {
                "tools": sorted(server.manifest_tool_names()),
                "callable": sorted(server.callable_tool_names()),
                "identity": config.verified_agent_identity(),
                "episode": config.current_episode_id(),
                "auth": config.current_auth_metadata(),
                "profile": profiles.current(),
            }
        )
    if body.get("op") == "revoke_then_call":
        # An admitted request whose credential is revoked while it runs.
        session_credentials.revoke(
            reason="operator", actor="test", credential_id=body["credential_id"]
        )
    tool = server.REGISTERED_TOOLS[body["tool"]]
    try:
        result = tool(**(body.get("args") or {}))
    except Exception as exc:  # noqa: BLE001 - reported to the test
        return _probe_body({"error": str(exc), "type": type(exc).__name__})
    return _probe_body({"result": result})


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        f"{LABEL}:{STATIC_TOKEN},{config.operator_label()}:{OPERATOR_TOKEN}",
    )
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
    monkeypatch.setenv("UA_ACP_SESSION_MCP", "on")
    monkeypatch.delenv("UA_ACP_SESSION_MCP_NODES", raising=False)
    monkeypatch.delenv("UA_ACP_SESSION_DEVICE_CHECK", raising=False)
    monkeypatch.delenv("UA_NODE_ALLOW_BEARER", raising=False)
    monkeypatch.delenv("UA_REVOKED_AGENTS", raising=False)
    fake = _FakeOAuth()
    monkeypatch.setattr(oauth, "get_provider", lambda: fake)
    peer = _Peer()
    monkeypatch.setattr(tailnet, "resolve_peer_device", peer)
    monkeypatch.setattr(session_credentials, "_refused_last", {})
    storage.init_db()
    signins = [
        _device_login(fake, LABEL, DEVICE, token=DEVICE_TOKEN),
        _device_login(fake, OTHER_LABEL, OTHER_DEVICE, token=OTHER_DEVICE_TOKEN),
    ]
    app = server.HubAuthASGIMiddleware(
        Starlette(routes=[Route("/mcp", _mcp_probe, methods=["POST"]), *webui.web_routes()])
    )
    root = tmp_path / "dev"
    (root / "repo").mkdir(parents=True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.oauth = fake
        client.peer = peer
        client.root = root
        client.signin_values = [s["value"] for s in signins]
        yield client


def _sign_in(label: str, device: dict | None, signin_id: str | None = None) -> dict:
    """A durable device sign-in (an OAuth refresh row) like `unified-agent login` leaves.

    Returns its ``signin_id``, ``session_id`` and refresh ``value`` (random,
    never used as a credential).
    """
    from unified_agent import oauth_store

    value = f"refresh-{secrets.token_urlsafe(16)}"
    signin_id = signin_id or secrets.token_hex(16)
    claims: dict = {"auth_method": "device_code", "signin_id": signin_id}
    if device is not None:
        claims["device_node"] = dict(device)
    oauth_store.save_refresh(
        value,
        client_id="unified-agent-cli",
        subject=label,
        scopes=[],
        resource=None,
        expires_at=None,
        claims=claims,
    )
    return {
        "signin_id": signin_id,
        "session_id": oauth_store.digest(value)[: oauth_store.SESSION_ID_LEN],
        "value": value,
        "claims": claims,
    }


def _device_login(fake, label: str, device: dict | None, *, token: str | None = None) -> dict:
    """A sign-in plus the device-code access token the fake provider accepts for it."""
    signin = _sign_in(label, device)
    token = token or f"dev-{secrets.token_urlsafe(12)}"
    fake.tokens[token] = (label, signin["claims"])
    return {**signin, "token": token}


async def _never_refused(*_args, **_kwargs) -> bool:
    return False


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _facts(root: Path, *, features=(session_credentials.FEATURE,)) -> dict:
    return {
        "os": {"system": "Linux"},
        "harnesses": {"acp:gemini": {"path": "/usr/bin/gemini", "acp": True}},
        "backends": ["acp:gemini", "codex"],
        "worktree_roots": [{"path": str(root), "exists": True, "worktrees": 0}],
        "node_features": [*features, "session_settings"],
    }


def _register(hub, *, node=NODE, token=DEVICE_TOKEN, features=(session_credentials.FEATURE,)):
    resp = hub.post(
        "/api/node/register",
        json={"node": node, "facts": _facts(hub.root, features=features), "version": "test"},
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _claimed(
    hub,
    *,
    node=NODE,
    task_id=None,
    timeout_s=600,
    backend="acp:gemini",
    device="__auto__",
    **extra,
):
    node_queue.enqueue(
        node,
        created_by=LABEL,
        prompt="work",
        cwd=str(hub.root / "repo"),
        backend=backend,
        task_id=task_id,
        timeout_s=timeout_s,
        **extra,
    )
    identity, auto_device = {
        NODE: (LABEL, DEVICE["node"]),
        "other-node-01": (OTHER_LABEL, OTHER_DEVICE["node"]),
    }.get(node, (LABEL, None))
    if device == "__auto__":
        device = auto_device
    item = node_queue.claim_next(node, identity=identity, device_node=device)
    assert item is not None
    return item


def _mint(hub, item, *, token=DEVICE_TOKEN, attempt=None, node=NODE):
    return hub.post(
        "/api/node/session_credential",
        json={
            "node": node,
            "item_id": item["item_id"],
            "attempt": item["attempt"] if attempt is None else attempt,
        },
        headers=_bearer(token),
    )


def _session(hub, **kwargs) -> tuple[dict, dict]:
    _register(hub)
    item = _claimed(hub, **kwargs)
    resp = _mint(hub, item)
    assert resp.status_code == 201, resp.json()
    return item, resp.json()


def _mcp(hub, credential, body=None, *, path="/mcp"):
    return hub.post(path, json=body or {"op": "list"}, headers=_bearer(credential))


def _call(hub, credential, tool, **args):
    resp = _mcp(hub, credential, {"op": "call", "tool": tool, "args": args})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _events(channel: str) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload, originator, episode_id FROM event_log WHERE channel=? ORDER BY rowid",
            (channel,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {**json.loads(r["payload"]), "_originator": r["originator"], "_episode": r["episode_id"]}
        for r in rows
    ]


def _row(credential_id: str) -> dict:
    row = session_credentials.credential_row(credential_id)
    assert row is not None
    return row


def _task(title="session fixture") -> int:
    from unified_agent.tools import tasks

    return tasks.task_add(title)["id"]


EXPECTED_TOOLS = sorted(profiles.ACP_SESSION_ALLOWED_TOOLS)


# --- mint ---------------------------------------------------------------------------


class TestMint:
    def test_value_returned_once_and_only_its_hash_stored(self, hub):
        item, minted = _session(hub)
        value = minted["credential"]
        assert value.startswith(session_credentials.PREFIX)
        assert len(value) == len(session_credentials.PREFIX) + 43
        assert minted["tools"] == EXPECTED_TOOLS
        conn = storage.connect()
        try:
            row = dict(
                conn.execute(
                    "SELECT * FROM session_credentials WHERE credential_id=?",
                    (minted["credential_id"],),
                ).fetchone()
            )
        finally:
            conn.close()
        assert row["token_sha256"] == hashlib.sha256(value.encode()).hexdigest()
        assert value not in json.dumps(row)
        assert row["agent_label"] == LABEL and row["device_agent"] == LABEL
        assert row["node"] == NODE and row["device_node"] == DEVICE["node"]
        assert row["item_id"] == item["item_id"] and row["attempt"] == item["attempt"]
        claimed = datetime.fromisoformat(
            node_queue.get_item(item["item_id"])["claimed_at"].replace("Z", "+00:00")
        )
        assert minted["expires_at"] == (
            claimed + timedelta(seconds=600 + node_queue.LEASE_GRACE_S)
        ).isoformat(timespec="seconds")
        minted_events = _events(session_credentials.CHANNEL_MINTED)
        assert [e["credential_id"] for e in minted_events] == [minted["credential_id"]]
        assert value not in json.dumps(minted_events)

    def test_response_is_not_cached(self, hub):
        _register(hub)
        resp = _mint(hub, _claimed(hub))
        assert resp.status_code == 201
        assert resp.headers["cache-control"] == "no-store"

    def test_second_mint_for_the_attempt_revokes_the_first(self, hub):
        item, first = _session(hub)
        second = _mint(hub, item).json()
        assert _row(first["credential_id"])["revoke_reason"] == "superseded"
        assert _mcp(hub, first["credential"]).status_code == 401
        assert _mcp(hub, second["credential"]).status_code == 200
        conn = storage.connect()
        try:
            active = conn.execute(
                "SELECT COUNT(*) FROM session_credentials WHERE item_id=? AND revoked_at IS NULL",
                (item["item_id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        assert active == 1

    def test_refused_while_the_flag_is_off(self, hub, monkeypatch):
        _register(hub)
        item = _claimed(hub)
        monkeypatch.setenv("UA_ACP_SESSION_MCP", "off")
        resp = _mint(hub, item)
        assert resp.status_code == 403
        assert resp.json()["reason"] == "disabled"

    def test_refused_for_a_stale_attempt(self, hub):
        _register(hub)
        item = _claimed(hub)
        resp = _mint(hub, item, attempt=item["attempt"] + 1)
        assert resp.status_code == 409
        assert resp.json()["reason"] == "stale_attempt"

    def test_refused_for_a_finished_item(self, hub):
        _register(hub)
        item = _claimed(hub)
        node_queue.post_receipt(
            NODE,
            item["item_id"],
            identity=LABEL,
            receipt={"status": "finished", "attempt": item["attempt"]},
            device_node=DEVICE["node"],
        )
        resp = _mint(hub, item)
        assert resp.status_code == 409
        assert resp.json()["reason"] == "item_not_claimed"

    def test_refused_for_a_native_item(self, hub):
        _register(hub)
        resp = _mint(hub, _claimed(hub, backend="codex"))
        assert resp.status_code == 409
        assert resp.json()["reason"] == "not_acp"

    def test_refused_for_a_foreign_node(self, hub):
        _register(hub)
        _register(hub, node="other-node-01", token=OTHER_DEVICE_TOKEN)
        item = _claimed(hub, node="other-node-01")
        # The caller owns NODE, not other-node-01.
        resp = _mint(hub, item, node="other-node-01")
        assert resp.status_code == 403
        assert resp.json()["reason"] == "node_binding"

    def test_refused_from_the_wrong_device(self, hub, monkeypatch):
        _register(hub)
        item = _claimed(hub)
        # Same label, a different device sign-in: the node is bound to DEVICE.
        real = hub.oauth.load_access_token

        async def other_device(token):
            access = await real(token)
            if access is not None and token == DEVICE_TOKEN:
                access.claims = {"auth_method": "device_code", "device_node": dict(OTHER_DEVICE)}
            return access

        monkeypatch.setattr(hub.oauth, "load_access_token", other_device)
        resp = _mint(hub, item)
        assert resp.status_code == 403
        assert resp.json()["reason"] == "node_binding"

    def test_refused_for_an_unknown_or_revoked_label(self, hub, monkeypatch):
        _register(hub)
        item = _claimed(hub)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_dispatch SET profile_agent=? WHERE id=?",
                ("not a label!", item["item_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        assert _mint(hub, item).json()["reason"] == "label_refused"
        item2 = _claimed(hub)
        monkeypatch.setenv("UA_REVOKED_AGENTS", LABEL)
        resp = _mint(hub, item2)
        assert resp.status_code == 403
        # Since task #2112 the node registry refuses a revoked agent's node on
        # every call, so the mint stops at the ownership check, before the label check.
        assert resp.json()["reason"] == "node_binding"
        assert "revoked" in resp.json()["error"]

    def test_refused_for_a_node_without_the_feature_or_off_the_allowlist(self, hub, monkeypatch):
        _register(hub, features=())
        item = _claimed(hub)
        assert _mint(hub, item).json()["reason"] == "node_feature_missing"
        _register(hub)
        monkeypatch.setenv("UA_ACP_SESSION_MCP_NODES", "some-other-node")
        resp = _mint(hub, item)
        assert resp.status_code == 403
        assert resp.json()["reason"] == "node_not_allowed"
        monkeypatch.setenv("UA_ACP_SESSION_MCP_NODES", NODE)
        assert _mint(hub, item).status_code == 201

    def test_static_bearer_never_mints_even_with_allow_bearer(self, hub, monkeypatch):
        monkeypatch.setenv("UA_NODE_ALLOW_BEARER", "1")
        _register(hub, node="bearer-node-01", token=STATIC_TOKEN)
        item = _claimed(hub, node="bearer-node-01")
        resp = _mint(hub, item, token=STATIC_TOKEN, node="bearer-node-01")
        assert resp.status_code == 403
        assert resp.json()["reason"] == "device_signin_required"
        monkeypatch.delenv("UA_NODE_ALLOW_BEARER")
        assert _mint(hub, item, token=STATIC_TOKEN, node="bearer-node-01").status_code == 403
        conn = storage.connect()
        try:
            assert conn.execute("SELECT COUNT(*) FROM session_credentials").fetchone()[0] == 0
        finally:
            conn.close()

    def test_refused_without_a_signin_id(self, hub):
        # A sign-in from before signin_id existed: claims carry none.
        hub.oauth.tokens[DEVICE_TOKEN][1].pop("signin_id")
        _register(hub)
        resp = _mint(hub, _claimed(hub))
        assert resp.status_code == 403
        assert resp.json()["reason"] == "signin_missing"

    def test_refused_when_the_sign_in_is_not_durable(self, hub):
        # The access token names a sign-in with no refresh row behind it.
        hub.oauth.tokens[DEVICE_TOKEN][1]["signin_id"] = secrets.token_hex(16)
        _register(hub)
        resp = _mint(hub, _claimed(hub))
        assert resp.status_code == 403
        assert resp.json()["reason"] == "signin_missing"

    def test_refused_for_a_human_subject(self, hub):
        _register(hub)
        item = _claimed(hub)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_dispatch SET profile_agent=? WHERE id=?",
                (config.operator_label(), item["item_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        resp = _mint(hub, item)
        assert resp.status_code == 403
        assert resp.json()["reason"] == "human_subject"

    def test_tools_are_capped_by_the_device_agent_too(self, hub, monkeypatch):
        real = profiles.profiles_by_identity
        monkeypatch.setattr(
            profiles, "profiles_by_identity", lambda: {**real(), LABEL: profiles.PUBLIC}
        )
        _register(hub)
        item = _claimed(hub)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_dispatch SET profile_agent=? WHERE id=?",
                (PROFILE_LABEL, item["item_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        minted = _mint(hub, item).json()
        assert minted["tools"] == ["profile_status", "task_list"]
        assert _mcp(hub, minted["credential"]).json()["tools"] == ["profile_status", "task_list"]

    def test_refused_for_a_closed_task(self, hub):
        task_id = _task()
        conn = storage.connect()
        try:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
            conn.commit()
        finally:
            conn.close()
        _register(hub)
        resp = _mint(hub, _claimed(hub, task_id=task_id))
        assert resp.status_code == 409
        assert resp.json()["reason"] == "task_closed"

    def test_subject_is_the_profile_agent_with_the_device_agent_recorded(self, hub):
        _register(hub)
        item = _claimed(hub)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_dispatch SET profile_agent=? WHERE id=?",
                (PROFILE_LABEL, item["item_id"]),
            )
            conn.commit()
        finally:
            conn.close()
        minted = _mint(hub, item).json()
        row = _row(minted["credential_id"])
        assert row["agent_label"] == PROFILE_LABEL and row["device_agent"] == LABEL
        listed = _mcp(hub, minted["credential"]).json()
        assert listed["identity"] == PROFILE_LABEL
        assert listed["auth"]["device_agent"] == LABEL


# --- revoke -------------------------------------------------------------------------


class TestRevoke:
    def _assert_revoked(self, hub, minted, reason):
        assert _mcp(hub, minted["credential"]).status_code == 401
        assert _row(minted["credential_id"])["revoke_reason"] == reason
        revoked = [
            e
            for e in _events(session_credentials.CHANNEL_REVOKED)
            if e["credential_id"] == minted["credential_id"]
        ]
        assert [e["reason"] for e in revoked] == [reason]
        assert minted["credential"] not in json.dumps(revoked)

    def test_receipt_ends_the_session(self, hub):
        item, minted = _session(hub)
        assert _mcp(hub, minted["credential"]).status_code == 200
        resp = hub.post(
            "/api/node/receipt",
            json={
                "node": NODE,
                "item_id": item["item_id"],
                "receipt": {"status": "finished", "attempt": item["attempt"]},
            },
            headers=_bearer(DEVICE_TOKEN),
        )
        assert resp.status_code == 201, resp.json()
        self._assert_revoked(hub, minted, "session_end")

    def test_cancel_revokes_at_the_request(self, hub):
        item, minted = _session(hub)
        node_queue.cancel(item["item_id"], actor=config.operator_label())
        # Still claimed (the node has not answered), but access is gone now.
        assert node_queue.get_item(item["item_id"])["status"] == "claimed"
        self._assert_revoked(hub, minted, "cancelled")

    def test_governed_run_cancel_revokes(self, hub):
        item, minted = _session(hub)
        outcome = node_queue.cancel_for_run_evidence(f"node:{NODE}:{item['item_id']}")
        assert outcome == "node_cancel_requested"
        self._assert_revoked(hub, minted, "cancelled")

    @pytest.mark.parametrize("status", ["done", "cancelled"])
    def test_closing_the_task_revokes(self, hub, status):
        from unified_agent.tools import tasks

        task_id = _task()
        _, minted = _session(hub, task_id=task_id)
        tasks.task_update(task_id, status=status, actor=config.operator_label())
        self._assert_revoked(hub, minted, "task_closed")

    def test_deleting_the_task_revokes(self, hub):
        from unified_agent.tools import tasks

        task_id = _task()
        _, minted = _session(hub, task_id=task_id)
        tasks.task_delete(task_id)
        self._assert_revoked(hub, minted, "task_deleted")

    def test_lease_expiry_revokes(self, hub):
        item, minted = _session(hub)
        later = datetime.now(UTC) + timedelta(seconds=600 + node_queue.LEASE_GRACE_S + 5)
        node_queue.claim_next(NODE, identity=LABEL, device_node=DEVICE["node"], now=later)
        assert node_queue.get_item(item["item_id"])["status"] == "failed"
        self._assert_revoked(hub, minted, "lease_expired")

    def test_coordinator_lease_settlement_revokes(self, hub):
        item, minted = _session(hub)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_dispatch SET run_id='run-1744' WHERE id=?", (item["item_id"],)
            )
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            assert node_queue.settle_expired_run(conn, "run-1744", storage.now()) == "claimed"
            conn.commit()
        finally:
            conn.close()
        node_queue.sweep_ended_followups(actor="dispatch-coordinator")
        self._assert_revoked(hub, minted, "lease_expired")

    @pytest.mark.parametrize("selector", ["device", "session_id"])
    def test_a_failed_cascade_still_ends_access(self, hub, monkeypatch, selector):
        """Fault injection: the OAuth revoke commits, the cascade raises.

        The node keeps heartbeating (as it would through another sign-in or a
        bearer), so liveness alone would keep the credential valid. The
        credential must still be refused, and the revoke must say the cascade
        failed instead of answering a plain ok.
        """
        from unified_agent import oauth_store, safety
        from unified_agent.tools import oauth_devices

        item, minted = _session(hub)
        assert _mcp(hub, minted["credential"]).status_code == 200
        monkeypatch.setattr(safety, "check_prod_action_gate", lambda *a, **k: None)
        monkeypatch.setattr(oauth, "get_provider", lambda: None)

        def broken(**kwargs):
            raise RuntimeError("cascade write failed")

        monkeypatch.setattr(session_credentials, "revoke_device_cascade", broken)
        if selector == "device":
            out = oauth_devices.revoke_device_sessions(
                device=DEVICE["node"], session_id=None, actor=config.operator_label()
            )
        else:
            sessions = [
                s
                for s in oauth_store.list_sessions()
                if s.get("device_node") == DEVICE["node"] and s.get("agent") == LABEL
            ]
            assert len(sessions) == 1, sessions
            out = oauth_devices.revoke_device_sessions(
                device=None, session_id=sessions[0]["session_id"], actor=config.operator_label()
            )
        # The OAuth revoke itself is committed.
        assert out["revoked"] == 1
        assert not [s for s in oauth_store.list_sessions() if s.get("agent") == LABEL]
        monkeypatch.setattr(oauth, "get_provider", lambda: hub.oauth)
        # The node is still live.
        node_queue.heartbeat(
            NODE, identity=LABEL, running=item["item_id"], device_node=DEVICE["node"]
        )
        assert _mcp(hub, minted["credential"]).status_code == 401
        # The sign-in check refuses; it does not write. The row stays for the
        # cascade (or an explicit revoke) to mark, and the miss is visible:
        assert _row(minted["credential_id"])["revoked_at"] is None
        # And the failed cascade is visible, not a silent ok.
        assert out["session_credentials"] == "failed"
        assert out["status"] == "partial"
        assert out.get("warning")

    def test_a_sign_in_gap_refuses_without_revoking(self, hub):
        """Between a rotation's delete and insert (or any read miss) access is
        refused, never destroyed: the next request after the gap succeeds."""
        from unified_agent import oauth_store

        _, minted = _session(hub)
        (signin,) = [s for s in oauth_store.list_sessions() if s.get("agent") == LABEL]
        conn = storage.connect()
        try:
            row = dict(
                conn.execute(
                    "SELECT * FROM oauth_refresh_tokens WHERE token_sha256 LIKE ?",
                    (signin["session_id"] + "%",),
                ).fetchone()
            )
            conn.execute(
                "DELETE FROM oauth_refresh_tokens WHERE token_sha256=?", (row["token_sha256"],)
            )
            conn.commit()
            assert _mcp(hub, minted["credential"]).status_code == 401
            assert _row(minted["credential_id"])["revoked_at"] is None
            cols = ",".join(row)
            conn.execute(
                f"INSERT INTO oauth_refresh_tokens ({cols}) VALUES ({','.join('?' * len(row))})",
                tuple(row.values()),
            )
            conn.commit()
        finally:
            conn.close()
        assert _mcp(hub, minted["credential"]).status_code == 200

    def test_a_sign_in_read_error_refuses_without_revoking(self, hub, monkeypatch):
        import sqlite3

        from unified_agent import oauth_store

        _, minted = _session(hub)

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(oauth_store, "live_signin_exists", broken)
        assert _mcp(hub, minted["credential"]).status_code == 401
        assert _row(minted["credential_id"])["revoked_at"] is None
        monkeypatch.undo()

    def test_a_rotation_mid_session_keeps_the_credential(self, hub):
        from unified_agent import oauth_store

        _, minted = _session(hub)
        (signin,) = [s for s in oauth_store.list_sessions() if s.get("agent") == LABEL]
        old = next(
            v for v in hub.signin_values if oauth_store.digest(v).startswith(signin["session_id"])
        )
        new_value = f"refresh-{secrets.token_urlsafe(16)}"
        rotated = oauth_store.rotate_refresh(
            old, client_id="unified-agent-cli", new_token=new_value, scopes=None
        )
        assert rotated is not None and rotated["rotated"] is True
        assert oauth_store.load_refresh(old) is None
        assert (
            oauth_store.load_refresh(new_value)["claims"]["signin_id"]
            == (_row(minted["credential_id"])["signin_id"])
        )
        assert _mcp(hub, minted["credential"]).status_code == 200

    def test_a_null_signin_id_is_refused(self, hub):
        _, minted = _session(hub)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE session_credentials SET signin_id=NULL WHERE credential_id=?",
                (minted["credential_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        assert _mcp(hub, minted["credential"]).status_code == 401

    def test_other_sign_ins_of_the_label_do_not_keep_it_alive(self, hub, monkeypatch):
        """Bound to one sign-in family: not to the label, not to the machine name."""
        monkeypatch.setenv("UA_ACP_SESSION_DEVICE_CHECK", "best_effort")
        name_only = {"node": "shared-name.example.test"}
        cases = [
            # An unbound sign-in, kept company by unbound sign-ins elsewhere
            # (another machine, a browser authorization_code login).
            ("unbound-node-01", None, [None, None]),
            # A name-only binding, and another node id reporting the same name.
            ("name-node-01", name_only, [{**name_only, "node_id": "nOTHER"}]),
        ]
        for node, device, others in cases:
            login = _device_login(hub.oauth, LABEL, device)
            hub.peer.device = dict(device) if device else None
            _register(hub, node=node, token=login["token"])
            item = _claimed(hub, node=node, device=(device or {}).get("node"))
            minted = _mint(hub, item, token=login["token"], node=node).json()
            assert "credential" in minted, minted
            for other in others:
                _sign_in(LABEL, other)
            assert _mcp(hub, minted["credential"]).status_code == 200
            conn = storage.connect()
            try:
                conn.execute(
                    "DELETE FROM oauth_refresh_tokens WHERE json_extract(claims, '$.signin_id')=?",
                    (login["signin_id"],),
                )
                conn.commit()
            finally:
                conn.close()
            assert _mcp(hub, minted["credential"]).status_code == 401, node

    def test_device_revoke_cascades(self, hub, monkeypatch):
        from unified_agent import safety
        from unified_agent.tools import oauth_devices

        _, minted = _session(hub)
        # The approval gate is not under test; the cascade after it is.
        monkeypatch.setattr(safety, "check_prod_action_gate", lambda *a, **k: None)
        # OAuth off in this process: the durable-row branch must cascade too.
        monkeypatch.setattr(oauth, "get_provider", lambda: None)
        out = oauth_devices.revoke_device_sessions(
            device=DEVICE["node"], session_id=None, actor=config.operator_label()
        )
        assert out["status"] == "ok"
        monkeypatch.setattr(oauth, "get_provider", lambda: hub.oauth)
        self._assert_revoked(hub, minted, "device_revoked")

    @pytest.mark.parametrize(
        ("sql", "reason"),
        [
            ("UPDATE tasks SET status='done' WHERE id=?", "task_closed"),
            ("UPDATE tasks SET status='cancelled' WHERE id=?", "task_closed"),
            ("DELETE FROM tasks WHERE id=?", "task_deleted"),
        ],
    )
    def test_a_task_closed_outside_task_update_revokes(self, hub, sql, reason):
        task_id = _task()
        _, minted = _session(hub, task_id=task_id)
        assert _mcp(hub, minted["credential"]).status_code == 200
        conn = storage.connect()
        try:
            conn.execute(sql, (task_id,))
            conn.commit()
        finally:
            conn.close()
        self._assert_revoked(hub, minted, reason)

    def test_an_admitted_request_finishes_and_the_next_one_is_refused(self, hub):
        _, minted = _session(hub)
        resp = _mcp(
            hub,
            minted["credential"],
            {
                "op": "revoke_then_call",
                "credential_id": minted["credential_id"],
                "tool": "memory_search",
                "args": {"query": "anything"},
            },
        )
        # Authority is decided once, at admission; the call in flight completes.
        assert resp.status_code == 200 and "result" in resp.json(), resp.json()
        assert _mcp(hub, minted["credential"]).status_code == 401

    def test_a_stashed_revocation_that_never_commits_expires(self, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(node_queue.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(node_queue, "_PENDING_REVOKED", [])
        storage.init_db()
        node_queue._stash_revoked(["0123456789abcdef"], "lease_expired")
        node_queue.flush_revoked_events(actor="test")
        assert len(node_queue._PENDING_REVOKED) == 1  # not committed yet: kept
        clock["now"] += node_queue._PENDING_TTL_S + 1
        node_queue.flush_revoked_events(actor="test")
        assert node_queue._PENDING_REVOKED == []  # its transaction never committed
        assert not _events(session_credentials.CHANNEL_REVOKED)

    def test_a_rolled_back_stash_never_reports_a_later_revoke(self, hub, monkeypatch):
        """Task #1893 item 1: a stashed reason is emitted only if it is the one stored."""
        monkeypatch.setattr(node_queue, "_PENDING_REVOKED", [])
        item, minted = _session(hub)
        cid = minted["credential_id"]
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            stashed = session_credentials.revoke_for_item(conn, item["item_id"], "lease_expired")
            node_queue._stash_revoked(stashed, "lease_expired")
            conn.rollback()  # the settling transaction never commits
        finally:
            conn.close()
        assert stashed == [cid] and _row(cid)["revoked_at"] is None
        # A different revoke of the same credential commits within the TTL.
        session_credentials.revoke(reason="operator", actor="test", credential_id=cid)
        node_queue.flush_revoked_events(actor="test")
        reasons = [
            e["reason"]
            for e in _events(session_credentials.CHANNEL_REVOKED)
            if e["credential_id"] == cid
        ]
        assert reasons == ["operator"]
        assert node_queue._PENDING_REVOKED == []

    def _lease_expired_sweep(self, item_id: str, *, commit: bool) -> list[str]:
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            stashed = session_credentials.revoke_for_item(conn, item_id, "lease_expired")
            node_queue._stash_revoked(stashed, "lease_expired", conn=conn)
            if commit:
                conn.commit()
            else:
                conn.rollback()
        finally:
            conn.close()
        return stashed

    @pytest.mark.parametrize("second_later", [False, True])
    def test_two_same_reason_sweeps_emit_once(self, hub, monkeypatch, second_later):
        """Task #1898: a rolled-back sweep and a committed one sharing a reason emit once."""
        monkeypatch.setattr(node_queue, "_PENDING_REVOKED", [])
        item, minted = _session(hub)
        cid = minted["credential_id"]
        start = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
        clock = {"now": start}
        monkeypatch.setattr(session_credentials, "_utcnow", lambda: clock["now"])
        assert self._lease_expired_sweep(item["item_id"], commit=False) == [cid]
        assert _row(cid)["revoked_at"] is None
        if second_later:
            clock["now"] = start + timedelta(seconds=5)
        assert self._lease_expired_sweep(item["item_id"], commit=True) == [cid]
        node_queue.flush_revoked_events(actor="test")
        node_queue.flush_revoked_events(actor="test")
        reasons = [
            e["reason"]
            for e in _events(session_credentials.CHANNEL_REVOKED)
            if e["credential_id"] == cid
        ]
        assert reasons == ["lease_expired"]
        assert node_queue._PENDING_REVOKED == []

    def test_a_rolled_back_stash_never_doubles_a_same_reason_direct_revoke(self, hub, monkeypatch):
        """Task #1898: the stash is keyed on its revocation, not on the reason alone."""
        monkeypatch.setattr(node_queue, "_PENDING_REVOKED", [])
        item, minted = _session(hub)
        cid = minted["credential_id"]
        start = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
        clock = {"now": start}
        monkeypatch.setattr(session_credentials, "_utcnow", lambda: clock["now"])
        assert self._lease_expired_sweep(item["item_id"], commit=False) == [cid]
        clock["now"] = start + timedelta(seconds=5)
        # A revoke that commits and records its own event, outside the stash.
        conn = storage.connect()
        try:
            revoked = session_credentials.revoke_for_item(conn, item["item_id"], "lease_expired")
            conn.commit()
        finally:
            conn.close()
        session_credentials.emit_revoked(revoked, "lease_expired", actor="direct")
        node_queue.flush_revoked_events(actor="test")
        events = [
            e for e in _events(session_credentials.CHANNEL_REVOKED) if e["credential_id"] == cid
        ]
        assert [(e["reason"], e["actor"]) for e in events] == [("lease_expired", "direct")]
        assert node_queue._PENDING_REVOKED == []

    def test_revoked_events_survive_a_flush_before_commit(self, hub):
        item, minted = _session(hub)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_dispatch SET run_id='run-1744b' WHERE id=?", (item["item_id"],)
            )
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            assert node_queue.settle_expired_run(conn, "run-1744b", storage.now()) == "claimed"
            # Another caller flushes while the settling transaction is still open.
            node_queue.flush_revoked_events(actor="someone-else")
            assert not [
                e
                for e in _events(session_credentials.CHANNEL_REVOKED)
                if e["credential_id"] == minted["credential_id"]
            ]
            conn.commit()
        finally:
            conn.close()
        node_queue.sweep_ended_followups(actor="dispatch-coordinator")
        node_queue.sweep_ended_followups(actor="dispatch-coordinator")
        self._assert_revoked(hub, minted, "lease_expired")

    def test_single_session_revoke_cascades(self, hub, monkeypatch):
        from unified_agent import oauth_store, safety
        from unified_agent.tools import oauth_devices

        _, minted = _session(hub)
        monkeypatch.setattr(safety, "check_prod_action_gate", lambda *a, **k: None)
        monkeypatch.setattr(oauth, "get_provider", lambda: None)
        monkeypatch.setattr(
            oauth_store,
            "revoke_sessions",
            lambda **kw: [
                {
                    "token_sha256": "0" * 64,
                    "session_id": "abcdef012345",
                    "client_id": "cli",
                    "agent": LABEL,
                    "device_node": DEVICE["node"],
                    "device_node_id": DEVICE["node_id"],
                }
            ],
        )
        oauth_devices.revoke_device_sessions(
            device=None, session_id="abcdef012345", actor=config.operator_label()
        )
        monkeypatch.setattr(oauth, "get_provider", lambda: hub.oauth)
        self._assert_revoked(hub, minted, "device_revoked")

    def test_unbound_sign_in_revoke_reaches_unbound_nodes_of_its_label(self, hub):
        _, minted = _session(hub)
        conn = storage.connect()
        try:
            conn.execute("UPDATE session_credentials SET device_node=NULL")
            conn.commit()
        finally:
            conn.close()
        ids = session_credentials.revoke_device_cascade(
            devices=[], unbound_agents=[LABEL], actor=config.operator_label()
        )
        assert ids == [minted["credential_id"]]
        assert _row(minted["credential_id"])["revoke_reason"] == "device_revoked"

    def test_revoked_agent_is_refused_on_the_next_call(self, hub, monkeypatch):
        _, minted = _session(hub)
        assert _mcp(hub, minted["credential"]).status_code == 200
        monkeypatch.setenv("UA_REVOKED_AGENTS", LABEL)
        assert _mcp(hub, minted["credential"]).status_code == 401
        monkeypatch.delenv("UA_REVOKED_AGENTS")
        assert _mcp(hub, minted["credential"]).status_code == 200

    def test_retired_agent_is_revoked_and_refused(self, hub, monkeypatch):
        """Task #2305: retiring the label revokes its credential; a mint is refused."""
        from unified_agent import agent_lifecycle

        item, minted = _session(hub)
        assert _mcp(hub, minted["credential"]).status_code == 200
        token = config.agent_identity_var.set(config.operator_label())
        try:
            res = agent_lifecycle.agent_retire(LABEL, "decommissioned")
        finally:
            config.agent_identity_var.reset(token)
        assert minted["credential_id"] in res["revoked"]["session_credentials"]
        assert _row(minted["credential_id"])["revoke_reason"] == "agent_retired"
        assert _mcp(hub, minted["credential"]).status_code == 401
        assert session_credentials._label_refused(LABEL) is True
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE session_credentials SET revoked_at=NULL, revoke_reason=NULL "
                "WHERE credential_id=?",
                (minted["credential_id"],),
            )
            # The retire fan-out also cancelled the item; undo that too, so the
            # retired-label check is the one that answers.
            conn.execute(
                "UPDATE node_dispatch SET cancel_requested=0 WHERE id=?", (item["item_id"],)
            )
            conn.commit()
            # The hub refuses the retired label's device token on every request
            # (task #2315), typed, before any route runs.
            refused = _mint(hub, item)
            assert refused.status_code == 403
            assert refused.json()["code"] == "agent_retired"
            # Past that check, a mint for the retired label is still refused,
            # typed, inside its transaction.
            monkeypatch.setattr(server, "_refuse_retired_identity", _never_refused)
            minted_again = _mint(hub, item)
            assert minted_again.status_code == 403
            assert minted_again.json()["reason"] == "agent_retired"
            # A row that escaped the revoke is still refused, with the typed check.
            with pytest.raises(session_credentials.SessionCredentialError) as err:
                session_credentials._verify(
                    minted["credential"], path="/mcp", peer_ip=None, now=None
                )
        finally:
            conn.close()
        assert err.value.reason == "agent_retired" and "is retired" in str(err.value)
        # Both refusals are lifecycle evidence too, the mint's despite its rollback.
        agent_lifecycle.join_refusal_writers()
        refused = [
            e for e in _events(agent_lifecycle.CHANNEL) if e["kind"] == agent_lifecycle.KIND_REFUSED
        ]
        sites = {e["site"] for e in refused if e["agent"] == LABEL}
        # The mint is refused at node ownership now (task #2315), before its own check.
        assert {
            "hub.auth.oauth",
            "node_registry.owned_node",
            "session_credentials.verify",
        } <= sites

    def test_retired_node_owner_cannot_queue_or_claim_work(self, hub, monkeypatch):
        """Review of #2305: a retained device sign-in claims nothing once retired."""
        from unified_agent import agent_lifecycle
        from unified_agent.node_registry import NodeError

        _register(hub)
        token = config.agent_identity_var.set(config.operator_label())
        try:
            agent_lifecycle.agent_retire(LABEL, "decommissioned")
        finally:
            config.agent_identity_var.reset(token)
        with pytest.raises(NodeError, match="is retired"):
            node_queue.enqueue(
                NODE, created_by=LABEL, prompt="work", cwd=str(hub.root / "repo"), backend="codex"
            )
        # An item that is already queued (written before the refusal existed).
        conn = storage.connect()
        try:
            now = storage.now()
            conn.execute(
                "INSERT INTO node_dispatch (id, node, agent, backend, prompt, cwd, timeout_s, "
                "status, created_by, created_at, updated_at) VALUES (?, ?, ?, 'codex', 'p', ?, "
                "60, 'queued', ?, ?, ?)",
                ("nd-00000000000a2305", NODE, LABEL, str(hub.root / "repo"), LABEL, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        claim = {"node": NODE, "wait_s": 0}
        resp = hub.post("/api/node/claim", json=claim, headers=_bearer(DEVICE_TOKEN))
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "agent_retired"  # the per-request refusal (#2315)
        # Past it, the node path refuses on its own.
        monkeypatch.setattr(server, "_refuse_retired_identity", _never_refused)
        resp = hub.post("/api/node/claim", json=claim, headers=_bearer(DEVICE_TOKEN))
        assert resp.status_code == 403, resp.text
        assert "is retired" in resp.json()["error"]
        conn = storage.connect()
        try:
            status = conn.execute(
                "SELECT status FROM node_dispatch WHERE id='nd-00000000000a2305'"
            ).fetchone()["status"]
        finally:
            conn.close()
        assert status == "queued"
        agent_lifecycle.join_refusal_writers()
        sites = {
            e["site"]
            for e in _events(agent_lifecycle.CHANNEL)
            if e["kind"] == agent_lifecycle.KIND_REFUSED
        }
        assert {"node_queue.native_item", "node_registry.owned_node"} <= sites

    def test_operator_route_revokes_by_credential_item_or_node(self, hub):
        item, minted = _session(hub)
        refused = hub.post(
            "/api/action/session-credential-revoke",
            json={"credential_id": minted["credential_id"]},
            headers=_bearer(STATIC_TOKEN),
        )
        assert refused.status_code == 403
        assert _mcp(hub, minted["credential"]).status_code == 200
        resp = hub.post(
            "/api/action/session-credential-revoke",
            json={"credential_id": minted["credential_id"]},
            headers=_bearer(OPERATOR_TOKEN),
        )
        assert resp.status_code == 200, resp.json()
        assert resp.json()["revoked"] == [minted["credential_id"]]
        self._assert_revoked(hub, minted, "operator")
        again = _mint(hub, item).json()
        for selector in ({"item_id": item["item_id"]}, {"node": NODE}):
            fresh = _mint(hub, item).json() if selector.get("node") else again
            resp = hub.post(
                "/api/action/session-credential-revoke",
                json=selector,
                headers=_bearer(OPERATOR_TOKEN),
            )
            assert resp.status_code == 200
            assert _mcp(hub, fresh["credential"]).status_code == 401
        bad = hub.post(
            "/api/action/session-credential-revoke",
            json={"credential_id": "x", "node": NODE},
            headers=_bearer(OPERATOR_TOKEN),
        )
        assert bad.status_code == 400

    def test_flag_off_is_a_kill_switch(self, hub, monkeypatch):
        _, minted = _session(hub)
        monkeypatch.setenv("UA_ACP_SESSION_MCP", "off")
        resp = _mcp(hub, minted["credential"])
        assert resp.status_code == 401
        # No standing identity is served in its place.
        assert "tools" not in resp.json()
        monkeypatch.setenv("UA_ACP_SESSION_MCP", "on")
        assert _mcp(hub, minted["credential"]).status_code == 200

    def test_allowlist_removal_leaves_running_sessions_alone(self, hub, monkeypatch):
        monkeypatch.setenv("UA_ACP_SESSION_MCP_NODES", NODE)
        item, minted = _session(hub)
        monkeypatch.setenv("UA_ACP_SESSION_MCP_NODES", "")
        assert _mcp(hub, minted["credential"]).status_code == 200
        item2 = _claimed(hub)
        assert _mint(hub, item2).json()["reason"] == "node_not_allowed"


# --- expiry (injected clock) ------------------------------------------------------


class TestExpiry:
    @pytest.fixture
    def clock(self, monkeypatch):
        state = {"now": datetime.now(UTC)}
        monkeypatch.setattr(session_credentials, "_utcnow", lambda: state["now"])
        return state

    def _beat(self, *, running=None, at):
        node_queue.heartbeat(
            NODE, identity=LABEL, running=running, device_node=DEVICE["node"], now=at
        )

    def test_idle_window_lapses_without_a_heartbeat_for_the_item(self, hub, clock):
        _, minted = _session(hub)
        start = clock["now"]
        clock["now"] = start + timedelta(seconds=session_credentials.IDLE_WINDOW_S + 30)
        # The node is alive, but nothing renewed the item's session.
        self._beat(at=clock["now"])
        assert _mcp(hub, minted["credential"]).status_code == 401
        assert _row(minted["credential_id"])["revoke_reason"] == "idle_expired"

    def test_idle_window_slides_with_heartbeats(self, hub, clock):
        item, minted = _session(hub, timeout_s=3600)
        start = clock["now"]
        self._beat(running=item["item_id"], at=start + timedelta(minutes=8))
        clock["now"] = start + timedelta(minutes=11)
        self._beat(at=clock["now"])
        assert _mcp(hub, minted["credential"]).status_code == 200
        row = _row(minted["credential_id"])
        assert row["idle_expires_at"] == (start + timedelta(minutes=18)).isoformat(
            timespec="seconds"
        )

    def test_absolute_cap_holds_despite_heartbeats(self, hub, clock):
        item, minted = _session(hub, timeout_s=300)
        start = clock["now"]
        cap = datetime.fromisoformat(minted["expires_at"])
        for minutes in (4, 8):
            self._beat(running=item["item_id"], at=start + timedelta(minutes=minutes))
        clock["now"] = cap + timedelta(seconds=1)
        self._beat(running=item["item_id"], at=clock["now"])
        row = _row(minted["credential_id"])
        assert row["expires_at"] == minted["expires_at"]
        assert datetime.fromisoformat(row["idle_expires_at"]) <= cap
        assert _mcp(hub, minted["credential"]).status_code == 401
        assert _row(minted["credential_id"])["revoke_reason"] == "expired"

    def test_stale_node_heartbeat_is_refused(self, hub, clock):
        _, minted = _session(hub)
        clock["now"] = clock["now"] + timedelta(seconds=node_queue.NODE_LIVE_WINDOW_S + 30)
        assert _mcp(hub, minted["credential"]).status_code == 401
        # Not revoked: a node that comes back within the idle window resumes.
        assert _row(minted["credential_id"])["revoked_at"] is None
        self._beat(at=clock["now"])
        assert _mcp(hub, minted["credential"]).status_code == 200

    def test_a_beat_from_before_unretire_is_not_live(self, hub, clock):
        """#2196 review: verify reads heartbeat_floor, as routing does.

        The row is put in the state ``node_unretire`` leaves it in (the stored
        beat copied into ``heartbeat_floor``); a node with a claimed item
        cannot be retired, so the state is set directly.
        """
        _, minted = _session(hub)
        conn = storage.connect()
        try:
            conn.execute("UPDATE nodes SET heartbeat_floor=last_heartbeat WHERE node=?", (NODE,))
            conn.commit()
        finally:
            conn.close()
        assert _mcp(hub, minted["credential"]).status_code == 401
        with pytest.raises(session_credentials.SessionCredentialError) as refused:
            session_credentials.verify(minted["credential"], path="/mcp", peer_ip="testclient")
        assert refused.value.reason == "node_stale"
        assert _row(minted["credential_id"])["revoked_at"] is None
        clock["now"] = clock["now"] + timedelta(seconds=5)
        self._beat(at=clock["now"])
        assert _mcp(hub, minted["credential"]).status_code == 200

    def test_future_dated_node_heartbeat_is_refused(self, hub, clock):
        """A beat past the clock-skew allowance is not live (#1997).

        Before the fix any future-dated beat passed, so a stopped node whose
        row was hand-edited or stamped by a rolled-back clock kept its
        session credential working."""
        _, minted = _session(hub)
        ahead = clock["now"] + timedelta(seconds=node_registry.NODE_CLOCK_SKEW_S + 30)
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE nodes SET last_heartbeat=? WHERE node=?",
                (ahead.isoformat(timespec="seconds"), NODE),
            )
            conn.commit()
        finally:
            conn.close()
        assert _mcp(hub, minted["credential"]).status_code == 401
        assert _row(minted["credential_id"])["revoked_at"] is None
        self._beat(at=clock["now"])
        assert _mcp(hub, minted["credential"]).status_code == 200


# --- narrowing -----------------------------------------------------------------------


class TestNarrowing:
    def test_tools_list_is_exactly_the_effective_set(self, hub):
        item, minted = _session(hub)
        listed = _mcp(hub, minted["credential"]).json()
        assert listed["tools"] == EXPECTED_TOOLS
        assert listed["callable"] == EXPECTED_TOOLS
        assert listed["profile"] == profiles.ACP_SESSION
        assert listed["identity"] == LABEL
        assert listed["episode"] == f"acp:{item['item_id']}"

    def test_effective_set_never_exceeds_the_identity_ceiling(self, hub, monkeypatch):
        real = profiles.profiles_by_identity
        monkeypatch.setattr(
            profiles, "profiles_by_identity", lambda: {**real(), LABEL: profiles.PUBLIC}
        )
        _, minted = _session(hub)
        assert minted["tools"] == ["profile_status", "task_list"]
        assert _mcp(hub, minted["credential"]).json()["tools"] == ["profile_status", "task_list"]

    def test_excluded_tool_is_denied_with_a_deny_event(self, hub):
        item, minted = _session(hub)
        out = _call(hub, minted["credential"], "task_update", id=1, status="done")
        assert out["type"] == "ToolError" and "not exposed" in out["error"]
        denies = [
            e
            for e in _events(gateway.CHANNEL_GATEWAY_DECISION)
            if e.get("tool_name") == "task_update"
        ]
        assert denies and denies[-1]["decision"] == "deny"
        assert denies[-1]["reason_code"] == "profile_denied"
        assert denies[-1]["session"]["credential_id"] == minted["credential_id"]
        assert denies[-1]["session"]["item_id"] == item["item_id"]

    def test_a_chief_of_staff_label_gets_no_recovery_path(self, hub, monkeypatch):
        real = profiles.profiles_by_identity
        monkeypatch.setattr(
            profiles, "profiles_by_identity", lambda: {**real(), LABEL: profiles.CHIEF_OF_STAFF}
        )
        _, minted = _session(hub)
        assert _mcp(hub, minted["credential"]).json()["tools"] == EXPECTED_TOOLS
        for action in ("list", "show", "diff", "rollback"):
            out = _call(hub, minted["credential"], "config_revision_manage", action=action)
            assert out["type"] == "ToolError" and "not exposed" in out["error"], (action, out)
        from unified_agent.config_revisions import rollback_recovery_allowed

        token = profiles.session_grant_var.set(
            profiles.SessionGrant(
                tools=frozenset(), credential_id="0" * 16, item_id="x", task_id=None
            )
        )
        try:
            assert rollback_recovery_allowed(LABEL, "list") is False
        finally:
            profiles.session_grant_var.reset(token)

    def test_comment_resources_only_for_the_bound_task(self, hub):
        from mcp.server.mcpserver.exceptions import ResourceError

        from unified_agent import platform_readers, task_comments

        bound, other = _task("bound"), _task("other")
        mine = task_comments.append_comment(task_id=bound, body="here", author=LABEL)
        theirs = task_comments.append_comment(task_id=other, body="there", author=LABEL)
        mine_id, theirs_id = mine["comment"]["id"], theirs["comment"]["id"]
        token = config.agent_identity_var.set(LABEL)
        grant = profiles.session_grant_var.set(
            profiles.SessionGrant(
                tools=frozenset(profiles.ACP_SESSION_ALLOWED_TOOLS),
                credential_id="0" * 16,
                item_id="x",
                task_id=bound,
            )
        )
        try:
            assert platform_readers.read_comment(str(mine_id))["comment"]["task_id"] == bound
            with pytest.raises(ResourceError):
                platform_readers.read_comment(str(theirs_id))
        finally:
            profiles.session_grant_var.reset(grant)
            config.agent_identity_var.reset(token)

    def test_profile_switching_is_refused(self, hub):
        _, minted = _session(hub)
        for tool, args in (("profile_activate", {"profile": "steward"}), ("profile_reset", {})):
            out = _call(hub, minted["credential"], tool, **args)
            assert out["type"] == "ToolError", out
        grant = profiles.SessionGrant(
            tools=frozenset({"profile_status"}), credential_id="0" * 16, item_id="x", task_id=None
        )
        token = profiles.session_grant_var.set(grant)
        try:
            with pytest.raises(Exception, match="cannot switch profiles"):
                profiles.activate(profiles.STEWARD)
            with pytest.raises(Exception, match="cannot switch profiles"):
                profiles.reset()
        finally:
            profiles.session_grant_var.reset(token)
        with pytest.raises(Exception, match="cannot be activated"):
            profiles.activate(profiles.ACP_SESSION)

    def test_task_comment_only_on_the_bound_task(self, hub):
        task_id = _task("bound")
        other = _task("other")
        _, minted = _session(hub, task_id=task_id)
        ok = _call(
            hub,
            minted["credential"],
            "task_comment",
            action="append",
            params={"task_id": task_id, "body": "progress"},
        )
        assert "result" in ok, ok
        comment_id = ok["result"]["comment"]["id"]
        assert "result" in _call(
            hub, minted["credential"], "task_comment", action="list", params={"task_id": task_id}
        )
        assert "result" in _call(
            hub,
            minted["credential"],
            "task_comment",
            action="get",
            params={"comment_id": comment_id},
        )
        refused = _call(
            hub,
            minted["credential"],
            "task_comment",
            action="append",
            params={"task_id": other, "body": "elsewhere"},
        )
        assert refused["type"] == "ToolError" and "bound task" in refused["error"]
        denies = [
            e
            for e in _events(gateway.CHANNEL_GATEWAY_DECISION)
            if e.get("reason_code") == "session_task_scope"
        ]
        assert len(denies) == 1 and denies[0]["session"]["task_id"] == task_id

    def test_task_comment_refused_without_a_bound_task(self, hub):
        task_id = _task()
        _, minted = _session(hub)
        out = _call(
            hub,
            minted["credential"],
            "task_comment",
            action="list",
            params={"task_id": task_id},
        )
        assert out["type"] == "ToolError"

    def test_non_mcp_paths_are_403(self, hub):
        _, minted = _session(hub)
        for path in ("/api/whoami", "/api/node/register", "/api/tasks"):
            resp = hub.post(path, json={}, headers=_bearer(minted["credential"]))
            assert resp.status_code == 403, path
        assert hub.get("/api/tasks", headers=_bearer(minted["credential"])).status_code == 403

    def test_session_tokens_never_reach_the_other_legs(self, hub, monkeypatch):
        _, minted = _session(hub)
        forged = _fake_session_token()
        # Even a static bearer entry with the session prefix is never consulted.
        monkeypatch.setenv(
            "UA_AGENT_TOKENS",
            f"{LABEL}:{STATIC_TOKEN},{config.operator_label()}:{OPERATOR_TOKEN},"
            f"{OTHER_LABEL}:{forged}",
        )
        assert _mcp(hub, minted["credential"]).status_code == 200
        assert _mcp(hub, forged).status_code == 401
        assert hub.get("/api/whoami", headers=_bearer(forged)).status_code == 403
        malformed = session_credentials.PREFIX + "short"
        assert _mcp(hub, malformed).status_code == 401
        assert not [t for t in hub.oauth.seen if t.startswith(session_credentials.PREFIX)]

    def test_device_mismatch_is_401(self, hub):
        _, minted = _session(hub)
        hub.peer.device = dict(OTHER_DEVICE)
        assert _mcp(hub, minted["credential"]).status_code == 401
        refused = _events(session_credentials.CHANNEL_REFUSED)
        assert any(
            e["check"] == "device_mismatch" and e.get("credential_id") == minted["credential_id"]
            for e in refused
        )
        assert minted["credential"] not in json.dumps(refused)

    def test_unresolvable_peer_strict_refuses_best_effort_records_unavailable(
        self, hub, monkeypatch
    ):
        _, minted = _session(hub)
        hub.peer.device = None
        assert _mcp(hub, minted["credential"]).status_code == 401
        monkeypatch.setenv("UA_ACP_SESSION_DEVICE_CHECK", "best_effort")
        out = _call(hub, minted["credential"], "memory_search", query="anything")
        assert "result" in out, out
        decision = [
            e
            for e in _events(gateway.CHANNEL_GATEWAY_DECISION)
            if e.get("tool_name") == "memory_search"
        ][-1]
        assert decision["session"]["device_check"] == "unavailable"
        # best_effort still refuses a peer that resolves to another device.
        hub.peer.device = dict(OTHER_DEVICE)
        assert _mcp(hub, minted["credential"]).status_code == 401

    def test_a_websocket_with_a_session_credential_is_closed(self, hub):
        from starlette.websockets import WebSocketDisconnect

        _, minted = _session(hub)
        with pytest.raises(WebSocketDisconnect):
            with hub.websocket_connect("/mcp", headers=_bearer(minted["credential"])) as ws:
                ws.receive_text()

    def test_public_host_never_accepts_a_session_credential(self, hub, monkeypatch):
        _, minted = _session(hub)
        monkeypatch.setenv("UA_HTTP_PUBLIC_HOSTS", "127.0.0.1")
        assert _mcp(hub, minted["credential"]).status_code == 401


# --- attribution ---------------------------------------------------------------------


class TestAttribution:
    def test_allowed_call_carries_the_session_block(self, hub):
        task_id = _task()
        item, minted = _session(hub, task_id=task_id)
        out = _call(hub, minted["credential"], "memory_search", query="anything")
        assert "result" in out, out
        decision = [
            e
            for e in _events(gateway.CHANNEL_GATEWAY_DECISION)
            if e.get("tool_name") == "memory_search"
        ][-1]
        assert decision["decision"] == "allow"
        assert decision["_originator"] == LABEL
        assert decision["_episode"] == f"acp:{item['item_id']}"
        assert decision["profile"] == profiles.ACP_SESSION
        session = decision["session"]
        assert session["credential_id"] == minted["credential_id"]
        assert session["item_id"] == item["item_id"]
        assert session["attempt"] == item["attempt"]
        assert session["node"] == NODE
        assert session["task_id"] == task_id
        assert session["device_agent"] == LABEL
        assert session["device_check"] == "verified"
        assert minted["credential"] not in json.dumps(decision)

    def test_profile_status_shows_the_grant(self, hub):
        _, minted = _session(hub)
        status = _call(hub, minted["credential"], "profile_status")["result"]
        assert status["profile"] == profiles.ACP_SESSION
        assert status["auth_method"] == "acp_session"
        assert status["session"]["credential_id"] == minted["credential_id"]
        assert status["session"]["tools"] == EXPECTED_TOOLS
        assert minted["credential"] not in json.dumps(status)

    def test_memory_save_records_its_source_session(self, hub):
        _, minted = _session(hub)
        out = _call(hub, minted["credential"], "memory_save", content="learned a thing")
        note_id = out["result"]["id"]
        provenance = _events("hub.session_credential.memory_save")
        assert provenance[-1]["note_id"] == note_id
        assert provenance[-1]["source_session"] == minted["credential_id"]

    def test_a_session_cannot_paste_its_credential_into_memory(self, hub):
        """Task #1893 item 5: the ``uas_`` shape is redacted from a save, with a warning."""
        from unified_agent.tools import memory

        _, minted = _session(hub)
        value = minted["credential"]
        out = _call(
            hub,
            value,
            "memory_save",
            title=f"creds {value}",
            content=f"my hub credential is {value} for later",
        )
        assert "result" in out, out
        note_id = out["result"]["id"]
        assert "redacted" in out["result"]["warning"]
        stored = memory.memory_get(note_id)
        assert not _leaks(json.dumps(stored, default=str), value)
        assert "[REDACTED_SECRET]" in json.dumps(stored, default=str)
        appended = _call(
            hub,
            value,
            "memory_save",
            id=note_id,
            append=True,
            content=f"again {value}",
            idempotency_key="append-once",
        )
        assert "result" in appended and appended["result"].get("warning"), appended
        # Task #1893 P3-4: the idempotent replay of that update keeps the warning.
        replay = _call(
            hub,
            value,
            "memory_save",
            id=note_id,
            append=True,
            content=f"again {value}",
            idempotency_key="append-once",
        )
        assert replay["result"] == appended["result"], replay
        assert not _leaks(json.dumps(memory.memory_get(note_id), default=str), value)
        refused = _call(hub, value, "memory_save", content="tagged", tags=[value])
        assert refused["type"] == "ToolError", refused
        assert not _leaks(json.dumps(refused), value)

    def test_a_session_cannot_paste_its_credential_into_a_comment(self, hub):
        """Task #1893 item 5: the same for ``task_comment`` append."""
        from unified_agent import task_comments

        task_id = _task("bound")
        _, minted = _session(hub, task_id=task_id)
        value = minted["credential"]
        out = _call(
            hub,
            value,
            "task_comment",
            action="append",
            params={"task_id": task_id, "body": f"use {value} to call the hub"},
        )
        assert "result" in out, out
        assert "redacted" in out["result"]["warning"]
        listed = task_comments.list_comments(task_id)
        assert not _leaks(json.dumps(listed, default=str), value)
        assert "[REDACTED_SECRET]" in listed["comments"][-1]["body"]
        refused = _call(
            hub,
            value,
            "task_comment",
            action="append",
            params={"task_id": task_id, "body": "keyed", "idempotency_key": value},
        )
        assert refused["type"] == "ToolError", refused
        assert not _leaks(json.dumps(refused), value)

    def test_session_open_ingest_binds_the_acp_session(self, hub):
        item, minted = _session(hub)
        node_queue.record_node_event(
            NODE,
            item["item_id"],
            [{"kind": "session_open", "session_id": ACP_SID, "data": {"hub_mcp": "scoped"}}],
            identity=LABEL,
            device_node=DEVICE["node"],
        )
        row = _row(minted["credential_id"])
        assert row["acp_session_id"] == ACP_SID
        assert row["agent_session_id"]
        conn = storage.connect()
        try:
            meta = json.loads(
                conn.execute(
                    "SELECT meta FROM agent_sessions WHERE id=?", (row["agent_session_id"],)
                ).fetchone()["meta"]
            )
        finally:
            conn.close()
        assert meta["credential_id"] == minted["credential_id"]
        assert meta["credential_ids"] == [minted["credential_id"]]
        listed = _mcp(hub, minted["credential"]).json()
        assert listed["auth"]["acp_session_id"] == ACP_SID

    def test_resume_gets_a_new_credential_on_the_existing_row(self, hub):
        _register(
            hub,
            features=(session_credentials.FEATURE, node_agent.acp_matrix.RESUME_FEATURE),
        )
        first = _claimed(hub)
        c1 = _mint(hub, first).json()
        node_queue.record_node_event(
            NODE,
            first["item_id"],
            [{"kind": "session_open", "session_id": ACP_SID, "data": {}}],
            identity=LABEL,
            device_node=DEVICE["node"],
        )
        node_queue.post_receipt(
            NODE,
            first["item_id"],
            identity=LABEL,
            receipt={"status": "finished", "attempt": first["attempt"], "session_id": ACP_SID},
            device_node=DEVICE["node"],
        )
        second = _claimed(hub, resume_binding={"source": "resume"}, resume_session_id=ACP_SID)
        c2 = _mint(hub, second).json()
        node_queue.record_node_event(
            NODE,
            second["item_id"],
            [{"kind": "session_open", "session_id": ACP_SID, "data": {}}],
            identity=LABEL,
            device_node=DEVICE["node"],
        )
        row1, row2 = _row(c1["credential_id"]), _row(c2["credential_id"])
        assert row1["revoke_reason"] == "session_end"
        assert row2["agent_session_id"] == row1["agent_session_id"]
        conn = storage.connect()
        try:
            meta = json.loads(
                conn.execute(
                    "SELECT meta FROM agent_sessions WHERE id=?", (row2["agent_session_id"],)
                ).fetchone()["meta"]
            )
        finally:
            conn.close()
        assert meta["credential_id"] == c2["credential_id"]
        assert meta["credential_ids"] == [c1["credential_id"], c2["credential_id"]]

    def test_hub_redacts_credential_shapes_from_node_events(self, hub):
        item, minted = _session(hub)
        leaked = _fake_session_token()
        node_queue.record_node_event(
            NODE,
            item["item_id"],
            [
                {
                    "kind": "agent_message_chunk",
                    "text": f"my header is Bearer {leaked}",
                    "data": {"nested": [{"value": leaked}]},
                    "tool_call": {"rawInput": {"auth": leaked}},
                }
            ],
            identity=LABEL,
            device_node=DEVICE["node"],
        )
        stored = [
            e
            for e in _events(node_queue.CHANNEL_NODE)
            if e.get("acp_kind") == "agent_message_chunk"
        ][-1]
        assert leaked not in json.dumps(stored)
        assert "[REDACTED_SECRET]" in stored["text"]
        assert stored["data"]["nested"][0]["value"] == "[REDACTED_SECRET]"

    def test_hub_redacts_node_event_text_before_truncating_it(self, hub):
        """Task #1893 item 3: a value straddling the 4000-character cut is still caught."""
        item, _ = _session(hub)
        leaked = _fake_session_token()
        # The cut leaves ``uas_`` plus 30 characters: too short for the shape.
        text = "x" * (4_000 - len(session_credentials.PREFIX) - 31) + " " + leaked + " tail"
        node_queue.record_node_event(
            NODE,
            item["item_id"],
            [{"kind": "agent_message_chunk", "text": text, "data": {}}],
            identity=LABEL,
            device_node=DEVICE["node"],
        )
        stored = [
            e
            for e in _events(node_queue.CHANNEL_NODE)
            if e.get("acp_kind") == "agent_message_chunk"
        ][-1]
        assert not _leaks(json.dumps(stored), leaked), stored["text"][-60:]
        assert len(stored["text"]) <= 4_000

    def test_hub_redacts_every_permission_prompt_field_deterministically(self, hub):
        """Task #1893 item 4: session_id, optionId and kind are scrubbed too, and a
        re-polled prompt still matches its pending approval row."""
        item, _ = _session(hub)
        leaked = _fake_session_token()
        payload = {
            "session_id": f"sid {leaked}",
            "tool_call": {"toolCallId": "tc-1", "title": "fetch"},
            "options": [{"optionId": f"allow {leaked}", "name": "Allow", "kind": f"k {leaked}"}],
        }
        first = node_queue.request_node_permission(
            NODE, item["item_id"], payload, identity=LABEL, device_node=DEVICE["node"]
        )
        again = node_queue.request_node_permission(
            NODE, item["item_id"], payload, identity=LABEL, device_node=DEVICE["node"]
        )
        assert first["status"] == "pending" and first["approval_id"]
        assert again["approval_id"] == first["approval_id"]
        conn = storage.connect()
        try:
            rows = [json.dumps(tuple(r)) for r in conn.execute("SELECT * FROM approvals")]
        finally:
            conn.close()
        assert rows and not any(_leaks(blob, leaked) for blob in rows)
        assert any("[REDACTED_SECRET]" in blob for blob in rows)


# --- the audit rule ------------------------------------------------------------------


def test_the_shared_text_shape_is_exactly_one_credential():
    """Task #1893 P3-3: a value glued after a prefix is caught; a longer run is not."""
    from unified_agent.telemetry.redaction import redact_session_credentials

    value = _fake_session_token()
    glued, count = redact_session_credentials(f"key_{value} and ={value}.")
    assert count == 2 and value not in glued
    assert glued == "key_[REDACTED_SECRET] and =[REDACTED_SECRET]."
    longer = value + secrets.token_urlsafe(12).replace("-", "a").replace("_", "b")
    for text in (longer, "bon" + longer, f"blob {longer} end"):
        assert redact_session_credentials(text) == (text, 0)


def test_a_session_credential_literal_trips_redaction_and_the_audit_rule():
    literal = _fake_session_token()
    fixture_text = f'SESSION = "{literal}"\n'
    assert literal not in redact_sensitive_content(fixture_text, max_len=1000)
    findings = audit_rules.check_secrets("tests/fixture_module.py", fixture_text)
    assert findings, "a uas_ literal must be an audit finding"
    assert literal not in json.dumps([str(f) for f in findings])


# --- node side -----------------------------------------------------------------------

FAKE_AGENT = r"""
import hashlib
import json
import os
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def note(key, value):
    with open(os.environ["FAKE_TRACE"], "a") as fh:
        fh.write(json.dumps({key: value}) + "\n")


def servers_summary(servers):
    out = []
    for server in servers or []:
        headers = server.get("headers") or []
        auth = next((h.get("value", "") for h in headers if h.get("name") == "Authorization"), "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else auth
        out.append({
            "type": server.get("type"),
            "name": server.get("name"),
            "url": server.get("url"),
            "auth_sha256": hashlib.sha256(token.encode()).hexdigest() if token else None,
        })
    return out


note("argv", sys.argv)
captured = {"token": ""}
pending = {"prompt": None, "sid": None}


def chunk(sid, text):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid,
          "update": {"sessionUpdate": "agent_message_chunk",
                     "content": {"type": "text", "text": text}}}})


def tool(sid, update):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid,
          "update": update}})


def tool_output(sid, call_id, text):
    tool(sid, {"sessionUpdate": "tool_call_update", "toolCallId": call_id,
               "status": "in_progress",
               "content": [{"type": "content", "content": {"type": "text", "text": text}}]})


def finish(sid, rid):
    token = captured["token"]
    if os.environ.get("FAKE_TOOL_SPLIT") == "1" and token:
        # A tool's streamed output carries the value split across three
        # updates, with another tool call's output interleaved.
        tool(sid, {"sessionUpdate": "tool_call", "toolCallId": "tc-9", "title": "cat env",
                   "kind": "execute", "status": "pending"})
        tool_output(sid, "tc-9", "out: " + token[:12])
        tool_output(sid, "tc-8", "unrelated output")
        tool_output(sid, "tc-9", token[12:31])
        tool_output(sid, "tc-9", token[31:] + " done")
    if os.environ.get("FAKE_SPLIT") == "1" and token:
        # The value split across three chunks, the middle one wholly inside it.
        chunk(sid, "split credential: " + token[:9])
        chunk(sid, token[9:30])
        chunk(sid, token[30:] + " end")
    else:
        text = "done"
        if os.environ.get("FAKE_ECHO") == "1" and token:
            text = "my credential is " + token
        chunk(sid, text)
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None and rid == "perm-1" and pending["prompt"] is not None:
        finish(pending["sid"], pending["prompt"])
        pending["prompt"] = None
        continue
    if method == "initialize":
        caps = {}
        if os.environ.get("FAKE_MCP_HTTP") == "1":
            caps["mcpCapabilities"] = {"http": True}
        if os.environ.get("FAKE_RESUME") == "1":
            caps["sessionCapabilities"] = {"resume": {}}
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": 1, "agentCapabilities": caps,
            "agentInfo": {"name": "fake", "version": "0"}}})
    elif method in ("session/new", "session/resume", "session/load"):
        summary = servers_summary(params.get("mcpServers"))
        note(method, summary)
        for server in params.get("mcpServers") or []:
            for header in server.get("headers") or []:
                captured["token"] = header.get("value", "")[len("Bearer "):]
        if os.environ.get("FAKE_ECHO") == "1" and captured["token"]:
            # A careless harness: logs its config and echoes it to the model.
            sys.stderr.write("mcp config: " + captured["token"] + "\n")
            sys.stderr.flush()
        if os.environ.get("FAKE_FAIL_BODY") == "1" and captured["token"]:
            # The random body alone, repeated so a truncation cut lands inside one.
            body = captured["token"][len("uas_"):]
            send({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32602, "message": "bad server header " + " ".join([body] * 14)}})
            continue
        if os.environ.get("FAKE_FAIL_OPEN") == "1":
            send({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32602, "message": "bad server header " + captured["token"]}})
            continue
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "SID",
            "modes": {"currentModeId": "default", "availableModes": [{"id": "default", "name": "Default"}]}}})
    elif method == "session/set_mode":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "session/prompt":
        sid = params["sessionId"]
        if os.environ.get("FAKE_PERMISSION") == "1" and captured["token"]:
            # A tool call whose input carries the credential asks for permission.
            pending["prompt"], pending["sid"] = rid, sid
            send({"jsonrpc": "2.0", "id": "perm-1", "method": "session/request_permission",
                  "params": {"sessionId": sid,
                             "toolCall": {"toolCallId": "tc-1",
                                          "title": "curl -H " + captured["token"],
                                          "rawInput": {"header": captured["token"]}},
                             "options": [{"optionId": "allow", "name": "Allow",
                                          "kind": "allow_once"}]}})
            continue
        finish(sid, rid)
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32601, "message": "unknown " + str(method)}})
""".replace("SID", ACP_SID)


@pytest.fixture
def fake_agent(tmp_path, monkeypatch) -> Path:
    script = tmp_path / "fake_session_agent.py"
    script.write_text("#!" + sys.executable + "\n" + FAKE_AGENT)
    script.chmod(0o755)
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("UA_GEMINI_BIN", str(script))
    monkeypatch.setenv("FAKE_TRACE", str(trace))
    return trace


def _trace(path: Path) -> list[tuple[str, object]]:
    if not path.exists():
        return []
    return [next(iter(json.loads(line).items())) for line in path.read_text().splitlines()]


class _Spy:
    """The node's transport into the in-process hub, recording what crossed it."""

    def __init__(self, hub, *, fail_mint=False, missing_route=False):
        self.hub = hub
        self.fail_mint = fail_mint
        self.missing_route = missing_route
        self.hub_url = "http://127.0.0.1"
        self.sent: list[tuple[str, dict]] = []
        self.minted: list[str] = []

    def __call__(self, path, body, timeout):
        self.sent.append((path, json.loads(json.dumps(body))))
        if path == node_agent.SESSION_CREDENTIAL_PATH:
            if self.fail_mint:
                raise node_agent.NodeTransportError("hub unreachable")
            if self.missing_route:
                return 404, {"error": "http_404"}
        resp = self.hub.post(path, json=body, headers=_bearer(DEVICE_TOKEN))
        payload = resp.json()
        if path == node_agent.SESSION_CREDENTIAL_PATH and resp.status_code == 201:
            self.minted.append(payload["credential"])
        return resp.status_code, payload


def _node(hub, tmp_path, spy) -> node_agent.NodeAgent:
    agent = node_agent.NodeAgent(
        node=NODE,
        transport=spy,
        roots=[hub.root],
        backends=("acp:gemini",),
        facts_fn=lambda: _facts(hub.root),
        sleep=lambda s: None,
        rng=random.Random(7),
        claim_wait_s=0,
        session_root=tmp_path / "sessions",
        spool_dir=tmp_path / "spool",
        hub_url=spy.hub_url,
    )
    assert agent.run_once() == "registered"
    assert session_credentials.FEATURE in agent.hub_features
    return agent


def _session_open(spy) -> dict:
    for path, body in spy.sent:
        if path == "/api/node/events":
            for event in body["events"]:
                if event["kind"] == "session_open":
                    return event["data"]
    raise AssertionError("no session_open was posted")


class TestNodeFallback:
    def test_agent_without_http_mcp_gets_no_servers(self, hub, tmp_path, fake_agent):
        spy = _Spy(hub)
        agent = _node(hub, tmp_path, spy)
        receipt = agent.execute(_claimed(hub))
        assert receipt["status"] == "finished", receipt
        assert ("session/new", []) in _trace(fake_agent)
        opened = _session_open(spy)
        assert opened["hub_mcp"] == "none"
        assert opened["hub_mcp_reason"] == "agent_lacks_mcp_http"
        assert not [p for p, _ in spy.sent if p == node_agent.SESSION_CREDENTIAL_PATH]

    def test_agent_with_http_gets_one_scoped_server(self, hub, tmp_path, fake_agent, monkeypatch):
        monkeypatch.setenv("FAKE_MCP_HTTP", "1")
        spy = _Spy(hub)
        agent = _node(hub, tmp_path, spy)
        item = _claimed(hub)
        receipt = agent.execute(item)
        assert receipt["status"] == "finished", receipt
        (servers,) = [v for k, v in _trace(fake_agent) if k == "session/new"]
        assert len(servers) == 1
        server_entry = servers[0]
        assert server_entry["type"] == "http"
        assert server_entry["name"] == session_credentials.MCP_SERVER_NAME
        assert server_entry["url"] == "http://127.0.0.1/mcp"
        opened = _session_open(spy)
        assert opened["hub_mcp"] == "scoped"
        row = _row(opened["credential_id"])
        conn = storage.connect()
        try:
            digest = conn.execute(
                "SELECT token_sha256 FROM session_credentials WHERE credential_id=?",
                (opened["credential_id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        # The header the agent received is exactly the minted credential.
        assert server_entry["auth_sha256"] == digest
        assert row["acp_session_id"] == ACP_SID

    def test_failed_mint_falls_back_and_the_run_finishes(
        self, hub, tmp_path, fake_agent, monkeypatch
    ):
        monkeypatch.setenv("FAKE_MCP_HTTP", "1")
        for spy, reason in (
            (_Spy(hub, fail_mint=True), "mint_failed"),
            (_Spy(hub, missing_route=True), "hub_unsupported"),
        ):
            agent = _node(hub, tmp_path, spy)
            receipt = agent.execute(_claimed(hub))
            assert receipt["status"] == "finished", receipt
            opened = _session_open(spy)
            assert (opened["hub_mcp"], opened["hub_mcp_reason"]) == ("none", reason)
        monkeypatch.setenv("UA_ACP_SESSION_MCP", "off")
        spy = _Spy(hub)
        agent = _node(hub, tmp_path, spy)
        assert agent.execute(_claimed(hub))["status"] == "finished"
        assert _session_open(spy)["hub_mcp_reason"] == "hub_disabled"
        assert ("session/new", []) in _trace(fake_agent)

    def test_node_opt_out_neither_advertises_nor_mints(
        self, hub, tmp_path, fake_agent, monkeypatch
    ):
        monkeypatch.setenv("FAKE_MCP_HTTP", "1")
        monkeypatch.setenv("UA_NODE_SESSION_MCP", "0")
        assert session_credentials.FEATURE not in node_agent.node_features()
        spy = _Spy(hub)
        agent = _node(hub, tmp_path, spy)
        assert agent.execute(_claimed(hub))["status"] == "finished"
        assert _session_open(spy)["hub_mcp_reason"] == "node_opt_out"
        assert not [p for p, _ in spy.sent if p == node_agent.SESSION_CREDENTIAL_PATH]

    def test_hub_without_the_feature_is_not_asked(self, hub, tmp_path, fake_agent, monkeypatch):
        monkeypatch.setenv("FAKE_MCP_HTTP", "1")
        spy = _Spy(hub)
        agent = _node(hub, tmp_path, spy)
        agent.hub_features = frozenset()
        assert agent.execute(_claimed(hub))["status"] == "finished"
        assert _session_open(spy)["hub_mcp_reason"] == "hub_unsupported"
        assert not [p for p, _ in spy.sent if p == node_agent.SESSION_CREDENTIAL_PATH]

    def test_a_raising_credential_fn_never_fails_the_run(self, tmp_path, fake_agent, monkeypatch):
        monkeypatch.setenv("FAKE_MCP_HTTP", "1")
        events: list[dict] = []

        def broken():
            raise RuntimeError("mint exploded")

        result = acp_backend.AcpBackend("gemini").run(
            prompt="go",
            cwd=str(tmp_path),
            session_dir=str(tmp_path / "sess"),
            session_id="nd-0000000000000002",
            agent_label=LABEL,
            timeout_s=30,
            on_event=events.append,
            credential_fn=broken,
        )
        assert result["status"] == "success", result
        opened = next(e for e in events if e["kind"] == "session_open")["data"]
        assert (opened["hub_mcp"], opened["hub_mcp_reason"]) == ("none", "mint_failed")
        assert ("session/new", []) in _trace(fake_agent)

    def test_resume_mints_a_new_credential(self, hub, tmp_path, fake_agent, monkeypatch):
        monkeypatch.setenv("FAKE_MCP_HTTP", "1")
        monkeypatch.setenv("FAKE_RESUME", "1")
        spy = _Spy(hub)
        agent = _node(hub, tmp_path, spy)
        result = acp_backend.AcpBackend("gemini").run(
            prompt="again",
            cwd=str(hub.root / "repo"),
            session_dir=str(tmp_path / "resume-sess"),
            session_id="nd-0000000000000001",
            agent_label=LABEL,
            timeout_s=30,
            resume_session_id=ACP_SID,
            credential_fn=agent._session_credential(_claimed(hub)),
        )
        assert result["status"] == "success", result
        (servers,) = [v for k, v in _trace(fake_agent) if k == "session/resume"]
        assert len(servers) == 1 and servers[0]["auth_sha256"]


def _fragments(secret: str, size: int = 10) -> set[str]:
    """Every ``size``-character window of the value: a partial leak is a leak."""
    return {secret[i : i + size] for i in range(len(secret) - size + 1)}


def _leaks(blob: str, secret: str) -> list[str]:
    return sorted(f for f in _fragments(secret) if f in blob)


class TestSecretSweep:
    def test_the_value_appears_nowhere(self, hub, tmp_path, fake_agent, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG)
        monkeypatch.setenv("FAKE_MCP_HTTP", "1")
        # The harness misbehaves: it writes the credential to stderr and to the
        # model, and asks permission for a tool call that carries it.
        monkeypatch.setenv("FAKE_ECHO", "1")
        monkeypatch.setenv("FAKE_PERMISSION", "1")
        # One permission poll: the hub parks the approval and the node gives up.
        monkeypatch.setattr(node_agent, "ACP_PERMISSION_MAX_S", 0.0)
        spy = _Spy(hub)
        agent = _node(hub, tmp_path, spy)
        item = _claimed(hub)
        receipt = agent.execute(item)
        assert receipt["status"] == "finished", receipt
        (value,) = spy.minted
        # The permission prompt reached the hub's approval queue.
        assert [p for p, _ in spy.sent if p == "/api/node/permission"]
        # The harness really used it, so the sweep below is meaningful.
        assert _mcp(hub, value).status_code == 200
        agent._spool(item["item_id"], {**receipt, "attempt": item["attempt"]})
        agent._pending_receipts.append((item["item_id"], {**receipt, "attempt": item["attempt"]}))
        agent._flush_receipts()
        assert _mcp(hub, value).status_code == 401

        # A second run where the value reaches the model split across chunks.
        monkeypatch.delenv("FAKE_ECHO")
        monkeypatch.delenv("FAKE_PERMISSION")
        monkeypatch.setenv("FAKE_SPLIT", "1")
        split = agent.execute(_claimed(hub))
        assert split["status"] == "finished", split
        split_value = spy.minted[-1]

        # A third run where the agent echoes the value in an RPC error.
        monkeypatch.delenv("FAKE_SPLIT")
        monkeypatch.setenv("FAKE_FAIL_OPEN", "1")
        failed = agent.execute(_claimed(hub))
        assert failed["status"] == "failed"
        failed_value = spy.minted[-1]
        assert len({value, split_value, failed_value}) == 3

        conn = storage.connect()
        try:
            tables = [
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ]
            db_rows: list[tuple[str, str]] = []
            for table in tables:
                try:
                    rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
                except Exception:  # noqa: BLE001 - virtual tables without a reader
                    continue
                db_rows.extend((table, json.dumps(tuple(r), default=str)) for r in rows)
        finally:
            conn.close()
        files = [
            (str(path), path.read_bytes().decode("utf-8", errors="replace"))
            for root in (tmp_path / "spool", tmp_path / "sessions")
            for path in root.rglob("*")
            if path.is_file()
        ]
        argv = json.dumps([v for k, v in _trace(fake_agent) if k == "argv"])
        for secret in (value, split_value, failed_value):
            assert not _leaks(argv, secret)
            # every event, permission prompt and receipt the node posted
            for path, body in spy.sent:
                assert not _leaks(json.dumps(body), secret), path
            for result in (receipt, split, failed):
                assert not _leaks(json.dumps(result), secret)
            # every row of every table in the hub database: event_log, approvals, ...
            for table, blob in db_rows:
                assert not _leaks(blob, secret), table
            # hub and node logs
            assert not _leaks(caplog.text, secret)
            # the receipt spool and every file the node owns for its sessions
            for name, blob in files:
                assert not _leaks(blob, secret), name
        # The echoes did happen, and were scrubbed rather than never produced.
        assert any("[REDACTED_SECRET]" in blob for name, blob in files if "stderr" in name)
        assert "[REDACTED_SECRET]" in receipt["text"]
        assert "[REDACTED_SECRET]" in split["text"]
        assert "[REDACTED_SECRET]" in str(failed.get("reason"))
        approvals = [blob for table, blob in db_rows if table == "approvals"]
        assert any("[REDACTED_SECRET]" in blob for blob in approvals)

    def test_an_error_echoing_the_random_body_is_scrubbed_before_its_cut(
        self, hub, tmp_path, fake_agent, monkeypatch
    ):
        """Task #1893: error text is cut to 300/500 characters; a body cut there
        could no longer be matched later, so it is masked before the cut."""
        monkeypatch.setenv("FAKE_MCP_HTTP", "1")
        monkeypatch.setenv("FAKE_FAIL_BODY", "1")
        spy = _Spy(hub)
        agent = _node(hub, tmp_path, spy)
        failed = agent.execute(_claimed(hub))
        assert failed["status"] == "failed", failed
        (value,) = spy.minted
        for path, body in spy.sent:
            assert not _leaks(json.dumps(body), value), path
        assert not _leaks(json.dumps(failed), value)
        assert "[REDACTED_SECRET]" in str(failed.get("reason"))


class TestStreamRedaction:
    def test_a_value_split_across_tool_output_updates_is_redacted(
        self, tmp_path, fake_agent, monkeypatch
    ):
        """Task #1893 item 2: tool_call updates get the per-call stream tail too."""
        monkeypatch.setenv("FAKE_MCP_HTTP", "1")
        monkeypatch.setenv("FAKE_TOOL_SPLIT", "1")
        value = _fake_session_token()
        events: list[dict] = []
        result = acp_backend.AcpBackend("gemini").run(
            prompt="go",
            cwd=str(tmp_path),
            session_dir=str(tmp_path / "sess"),
            session_id="nd-0000000000000003",
            agent_label=LABEL,
            timeout_s=30,
            on_event=events.append,
            credential_fn=lambda: {
                "status": "scoped",
                "credential": value,
                "url": "http://127.0.0.1/mcp",
                "credential_id": "0123456789abcdef",
            },
        )
        assert result["status"] == "success", result
        updates = [e for e in events if e["kind"] == "tool_call_update"]
        assert len(updates) == 4
        blob = json.dumps(events)
        assert not _leaks(blob, value), _leaks(blob, value)
        assert not _leaks(json.dumps(result), value)
        assert "[REDACTED_SECRET]" in blob
        # The interleaved call's output is untouched.
        assert "unrelated output" in blob

    @pytest.mark.parametrize(
        "cuts",
        [(), (3,), (9,), (20,), (5, 30), (9, 30), (1, 2, 3, 40), tuple(range(1, 47))],
    )
    def test_a_value_split_anywhere_is_redacted(self, cuts):
        value = _fake_session_token()
        stream = f"before {value} after"
        offset = len("before ")
        points = [0, *(offset + c for c in cuts), len(stream)]
        tail, out = "", []
        for a, b in zip(points, points[1:], strict=False):
            piece, tail = acp_backend._redact_stream(tail, stream[a:b], value)
            out.append(piece)
        emitted = "".join(out)
        assert not _leaks(emitted, value), emitted
        assert emitted.startswith("before ") and emitted.endswith(" after")

    def test_a_flood_from_another_call_never_evicts_a_split_value_tail(self):
        """Task #1893 P3-2: a huge update for another call cannot drop this call's tail."""
        value = _fake_session_token()
        tails: dict = {}

        def output(call_id, text):
            return {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "content": [{"type": "content", "content": {"type": "text", "text": text}}],
            }

        out = [acp_backend._redact_tool_stream(tails, output("tc-1", value[:4]), value)]
        flood = {"sessionUpdate": "tool_call_update", "toolCallId": "tc-2"}
        flood["rawOutput"] = {f"k{i}": f"v{i}" for i in range(4200)}
        flooded = acp_backend._redact_tool_stream(tails, flood, value)
        out.append(acp_backend._redact_tool_stream(tails, output("tc-1", value[4:]), value))
        assert not _leaks(json.dumps(out), value), out
        assert len(tails) <= acp_backend._MAX_TOOL_STREAMS
        assert flooded["rawOutput"]["k4199"] == "v4199"  # flattened, scrubbed, restored

    def test_a_same_call_flood_after_the_prefix_never_emits_the_random_body(self):
        """Task #1893: a split right after ``uas_`` then a wide update of the same call.

        The flattened update starts a fresh tail, so the stream pass alone lets
        the rest through; the emit-time scrub still removes the random body.
        """
        value = _fake_session_token()
        body = value[len("uas_") :]
        tails: dict = {}
        first = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "content": [{"type": "content", "content": {"type": "text", "text": value[:4]}}],
        }
        flood = {"sessionUpdate": "tool_call_update", "toolCallId": "tc-1"}
        flood["rawOutput"] = {f"k{i}": f"v{i}" for i in range(300)}
        flood["rawOutput"]["k0"] = value[4:]
        out = [acp_backend._redact_tool_stream(tails, first, value)]
        out.append(acp_backend._redact_tool_stream(tails, flood, value))
        emitted = json.dumps(acp_backend._scrub_value(out, value))
        assert body not in emitted
        assert "[REDACTED_SECRET]" in emitted

    def test_the_emit_scrub_masks_the_random_body_alone(self):
        value = _fake_session_token()
        body = value[len("uas_") :]
        assert acp_backend._scrub_value(f"x {body} y", value) == "x [REDACTED_SECRET] y"
        assert acp_backend._scrub_value(f"x {value} y", value) == "x [REDACTED_SECRET] y"
        assert acp_backend._scrub_value("uas_ alone", value) == "uas_ alone"

    def test_a_deep_tool_update_is_walked_to_a_bounded_depth(self):
        """Task #1893 P3-2: below depth 12 a subtree is scrubbed as one flattened stream."""
        value = _fake_session_token()
        deep: dict = {"text": "deep " + value}
        for _ in range(40):
            deep = {"n": deep}
        update = {"sessionUpdate": "tool_call_update", "toolCallId": "tc-3", "rawOutput": deep}
        out = acp_backend._redact_tool_stream({}, update, value)
        assert not _leaks(json.dumps(out), value)
        assert "[REDACTED_SECRET]" in json.dumps(out)

    def test_text_without_the_value_is_untouched(self):
        value = _fake_session_token()
        tail = ""
        for piece in ("plain text ", "with uas_ mentioned ", "and more"):
            out, tail = acp_backend._redact_stream(tail, piece, value)
            assert out == piece
