"""ACP adapter layer (task #1328, Epic I2).

Covers the stdlib JSON-RPC client (``acp_client``), the launch matrix
(``acp_matrix``), the executor adapter (``acp_backend``), and the hub side
(``node_queue.record_node_event`` / ``request_node_permission`` + routes),
driven end-to-end against an in-process Starlette hub and a fake ACP agent
script speaking real newline-delimited JSON-RPC over stdio.

The fake agent is a Python script launched with ``sys.executable``; for the
backend tests it is also the ``UA_GEMINI_BIN`` override (executable + shebang),
so ``acp_matrix`` resolves ``[script, --acp]`` — no harness binary needed.
"""

from __future__ import annotations

import json
import random
import secrets
import sys
import threading
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import (
    acp_backend,
    acp_client,
    acp_matrix,
    config,
    node_agent,
    node_queue,
    server,
    storage,
    webui,
)
from unified_agent.tools.approvals import approval_resolve

CLAUDE_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
# The human resolver's bearer: approving acp_permission (high evidence tier,
# task #1672) needs a signed producer stamp.
HUMAN_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
NODE = "acp-node-01"
ACP_SESSION = "acp-fake-session-1"


FAKE_AGENT = '''#!/usr/bin/env python3
"""Fake ACP agent for the adapter tests — newline-delimited JSON-RPC on stdio."""
import json
import os
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


def note(key, value):
    path = os.environ.get("FAKE_ACP_TRACE")
    if path:
        with open(path, "a") as fh:
            fh.write(json.dumps({key: value}) + "\\n")


def update(sid, payload):
    return {"jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": sid, "update": payload}}


pending_prompt = None
pending_perm = None
pending_probe = None

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    try:
        msg = json.loads(raw)
    except ValueError:
        continue
    if "id" in msg and "method" not in msg:
        # a response to one of OUR requests (permission or probe)
        note("response", {"id": msg.get("id"),
                          "error": msg.get("error"),
                          "result": msg.get("result")})
        if pending_perm is not None and msg.get("id") == pending_perm:
            pending_perm = None
            if pending_prompt is not None:
                send({"jsonrpc": "2.0", "id": pending_prompt,
                      "result": {"stopReason": "end_turn"}})
                pending_prompt = None
        if pending_probe is not None and msg.get("id") == pending_probe:
            pending_probe = None
            if pending_prompt is not None:
                send({"jsonrpc": "2.0", "id": pending_prompt,
                      "result": {"stopReason": "end_turn"}})
                pending_prompt = None
        continue
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        note("initialize", params)
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"promptCapabilities": {"image": False}},
            "authMethods": [],
            "agentInfo": {"name": "fake-acp", "version": "0.0.1"},
        }})
    elif method == "session/new":
        note("new_session", params)
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "SESSION_ID", "modes": {"currentModeId": "default", "availableModes": [{"id": "default", "name": "Default"}]}}})
    elif method == "session/set_mode":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "session/prompt":
        sid = params["sessionId"]
        prompt_text = params["prompt"][0].get("text", "")
        note("prompt", prompt_text)
        pending_prompt = rid
        for chunk in ("hello ", "world"):
            send(update(sid, {"sessionUpdate": "agent_message_chunk",
                              "content": {"type": "text", "text": chunk}}))
        send(update(sid, {"sessionUpdate": "agent_thought_chunk",
                          "content": {"type": "text", "text": "thinking"}}))
        send(update(sid, {"sessionUpdate": "tool_call", "toolCallId": "tc-1",
                          "title": "Edit file", "kind": "edit", "status": "in_progress",
                          "locations": [{"path": "/tmp/x.py"}],
                          "content": [{"type": "diff", "path": "/tmp/x.py", "newText": "x"}]}))
        send(update(sid, {"sessionUpdate": "vendor_future_variant", "mystery": {"a": 1}}))
        if os.environ.get("FAKE_ACP_PROBE") == "1":
            # An fs/* request we never advertised must come back -32601, and a
            # $/cancel_request notification must be tolerated. The prompt answer
            # waits for the probe response so close() cannot race it.
            pending_probe = "probe-1"
            send({"jsonrpc": "2.0", "id": "probe-1", "method": "fs/read_text_file",
                  "params": {"sessionId": sid, "path": "/etc/passwd"}})
            send({"jsonrpc": "2.0", "method": "$/cancel_request",
                  "params": {"requestId": "probe-1"}})
            continue
        if os.environ.get("FAKE_ACP_PERMISSION") == "1":
            pending_perm = "perm-1"
            send({"jsonrpc": "2.0", "id": "perm-1",
                  "method": "session/request_permission",
                  "params": {"sessionId": sid,
                             "toolCall": {"toolCallId": "tc-1", "title": "Edit file",
                                          "kind": "edit"},
                             "options": [
                                 {"optionId": "deny-1", "name": "No", "kind": "reject_once"},
                                 {"optionId": "allow-all", "name": "Always",
                                  "kind": "allow_always"},
                                 {"optionId": "allow-1", "name": "Allow once",
                                  "kind": "allow_once"},
                             ]}})
            continue  # the prompt is answered when our permission reply arrives
        if os.environ.get("FAKE_ACP_HANG") == "1":
            continue  # never answer until session/cancel arrives
        pending_prompt = None
        send({"jsonrpc": "2.0", "id": rid,
              "result": {"stopReason": os.environ.get("FAKE_ACP_STOP", "end_turn")}})
    elif method == "session/cancel":
        note("cancelled", params.get("sessionId"))
        if pending_prompt is not None:
            send({"jsonrpc": "2.0", "id": pending_prompt,
                  "result": {"stopReason": "cancelled"}})
            pending_prompt = None
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32601, "message": f"unknown method {method}"}})
'''.replace("SESSION_ID", ACP_SESSION)


@pytest.fixture
def fake_agent(tmp_path) -> Path:
    script = tmp_path / "fake_acp_agent.py"
    script.write_text("#!" + sys.executable + "\n" + FAKE_AGENT.split("\n", 1)[1])
    script.chmod(0o755)
    return script


@pytest.fixture
def trace_path(tmp_path) -> Path:
    return tmp_path / "acp_trace.jsonl"


def _trace(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def acp_env(monkeypatch, fake_agent, trace_path):
    """Point the gemini matrix entry at the fake agent and enable tracing."""
    monkeypatch.setenv("UA_GEMINI_BIN", str(fake_agent))
    monkeypatch.setenv("FAKE_ACP_TRACE", str(trace_path))
    return fake_agent


class TestAcpClient:
    def test_initialize_negotiates_v1(self, fake_agent, tmp_path):
        client = acp_client.AcpClient(
            [sys.executable, str(fake_agent)],
            cwd=str(tmp_path),
            session_dir=str(tmp_path),
        )
        try:
            result = client.initialize(timeout_s=10)
            assert result["protocolVersion"] == 1
            assert client.protocol_version == 1
            assert client.agent_info["name"] == "fake-acp"
            sid = client.new_session(str(tmp_path), timeout_s=10)
            assert sid == ACP_SESSION
        finally:
            client.close()

    def test_v2_escape_hatch_refuses(self, fake_agent, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_ACP_PROTOCOL_VERSION", "2")
        with pytest.raises(acp_client.AcpError, match="v2"):
            acp_client.AcpClient([sys.executable, str(fake_agent)], cwd=str(tmp_path))
        monkeypatch.setenv("UA_ACP_PROTOCOL_VERSION", "bogus")
        with pytest.raises(acp_client.AcpError):
            acp_client.AcpClient([sys.executable, str(fake_agent)], cwd=str(tmp_path))


class TestAcpMatrix:
    def test_backend_name_mapping(self):
        assert acp_matrix.acp_backend_name("acp:gemini") == "gemini"
        assert acp_matrix.acp_backend_name("ACP:Cursor") == "cursor"
        assert acp_matrix.acp_backend_name("codex") is None
        assert acp_matrix.acp_backend_name("acp:") is None or "acp:" not in acp_matrix.ACP_HARNESSES

    def test_resolve_argv_env_override(self, monkeypatch, tmp_path):
        shim = tmp_path / "gemini-shim"
        shim.write_text("#!\n")
        monkeypatch.setenv("UA_GEMINI_BIN", str(shim))
        assert acp_matrix.resolve_argv("gemini") == [str(shim), "--acp"]
        with pytest.raises(ValueError):
            acp_matrix.resolve_argv("bogus")

    def test_availability_requires_binary(self, monkeypatch, tmp_path):
        assert acp_matrix.acp_available("bogus") is False
        monkeypatch.setenv("UA_GEMINI_BIN", str(tmp_path / "missing"))
        assert acp_matrix.acp_available("gemini") is False


class TestAcpBackend:
    def test_streams_updates_and_normalizes(self, acp_env, tmp_path, trace_path):
        events: list[dict] = []
        backend = acp_backend.AcpBackend("gemini")
        result = backend.run(
            prompt="do the thing",
            cwd=str(tmp_path),
            session_dir=str(tmp_path / "sess"),
            session_id="item-1",
            agent_label="Desktop_Agent1",
            timeout_s=30,
            on_event=events.append,
        )
        assert result["status"] == "success", result
        assert result["session_id"] == ACP_SESSION
        assert result["text"] == "hello world"
        assert "Edit file" in result["tools_used"]
        assert "/tmp/x.py" in result["files_modified"]
        kinds = [e["kind"] for e in events]
        assert "spawn" in kinds and "session_open" in kinds
        assert kinds.count("agent_message_chunk") == 2
        assert "tool_call" in kinds
        assert "turn_end" in kinds
        # Unknown variants are preserved verbatim, not dropped or renamed.
        future = next(e for e in events if e["kind"] == "vendor_future_variant")
        assert future["data"]["mystery"] == {"a": 1}
        turn_end = next(e for e in events if e["kind"] == "turn_end")
        assert turn_end["data"].get("stop_reason") == "end_turn"

    def test_agent_without_http_mcp_gets_no_hub_server(self, acp_env, tmp_path, trace_path):
        """Task #1744 fallback: no ``mcpCapabilities.http``, no credential, ``[]``."""
        events: list[dict] = []
        asked: list[bool] = []
        result = acp_backend.AcpBackend("gemini").run(
            prompt="do the thing",
            cwd=str(tmp_path),
            session_dir=str(tmp_path / "sess"),
            session_id="item-1",
            agent_label="Local_Worker",
            timeout_s=30,
            on_event=events.append,
            credential_fn=lambda: asked.append(True) or {"status": "none"},
        )
        assert result["status"] == "success", result
        assert asked == []
        new_session = next(t["new_session"] for t in _trace(trace_path) if "new_session" in t)
        assert new_session["mcpServers"] == []
        opened = next(e for e in events if e["kind"] == "session_open")["data"]
        assert opened["hub_mcp"] == "none"
        assert opened["hub_mcp_reason"] == "agent_lacks_mcp_http"

    def test_permission_approved_maps_option_id(self, acp_env, tmp_path, trace_path, monkeypatch):
        monkeypatch.setenv("FAKE_ACP_PERMISSION", "1")
        backend = acp_backend.AcpBackend("gemini")
        result = backend.run(
            prompt="edit",
            cwd=str(tmp_path),
            session_dir=str(tmp_path / "sess"),
            session_id="item-1",
            agent_label="Desktop_Agent1",
            timeout_s=30,
            permission_fn=lambda payload: {"status": "approved", "option_id": "allow-1"},
        )
        assert result["status"] == "success"
        outcomes = [
            t["response"]["result"]["outcome"]
            for t in _trace(trace_path)
            if t.get("response", {}).get("id") == "perm-1"
        ]
        assert outcomes[-1] == {"outcome": "selected", "optionId": "allow-1"}

    @pytest.mark.parametrize("option_id", ["[REDACTED_SECRET]", "allow-forged"])
    def test_an_approved_option_not_offered_cancels(
        self, acp_env, tmp_path, trace_path, monkeypatch, option_id
    ):
        """Task #1893 P3-1: only an optionId offered in this prompt is ever selected."""
        monkeypatch.setenv("FAKE_ACP_PERMISSION", "1")
        backend = acp_backend.AcpBackend("gemini")
        result = backend.run(
            prompt="edit",
            cwd=str(tmp_path),
            session_dir=str(tmp_path / "sess"),
            session_id="item-1",
            agent_label="Desktop_Agent1",
            timeout_s=30,
            permission_fn=lambda payload: {"status": "approved", "option_id": option_id},
        )
        assert result["status"] == "success"
        outcomes = [
            t["response"]["result"]["outcome"]
            for t in _trace(trace_path)
            if t.get("response", {}).get("id") == "perm-1"
        ]
        assert outcomes[-1] == {"outcome": "cancelled"}

    def test_allow_once_preferred_when_hub_gives_no_option(
        self, acp_env, tmp_path, trace_path, monkeypatch
    ):
        monkeypatch.setenv("FAKE_ACP_PERMISSION", "1")
        backend = acp_backend.AcpBackend("gemini")
        result = backend.run(
            prompt="edit",
            cwd=str(tmp_path),
            session_dir=str(tmp_path / "sess"),
            session_id="item-1",
            agent_label="Desktop_Agent1",
            timeout_s=30,
            permission_fn=lambda payload: {"status": "approved"},
        )
        assert result["status"] == "success"
        outcomes = [
            t["response"]["result"]["outcome"]
            for t in _trace(trace_path)
            if t.get("response", {}).get("id") == "perm-1"
        ]
        # deny-1 is first, allow-all second — allow_once still wins the fallback.
        assert outcomes[-1] == {"outcome": "selected", "optionId": "allow-1"}

    @pytest.mark.parametrize("decision", [None, {"status": "denied"}, {"status": "pending"}])
    def test_permission_not_approved_cancels(
        self, acp_env, tmp_path, trace_path, monkeypatch, decision
    ):
        monkeypatch.setenv("FAKE_ACP_PERMISSION", "1")
        backend = acp_backend.AcpBackend("gemini")
        kwargs = {} if decision is None else {"permission_fn": lambda p: decision}
        result = backend.run(
            prompt="edit",
            cwd=str(tmp_path),
            session_dir=str(tmp_path / "sess"),
            session_id="item-1",
            agent_label="Desktop_Agent1",
            timeout_s=30,
            **kwargs,
        )
        assert result["status"] == "success"  # the turn still completes
        outcomes = [
            t["response"]["result"]["outcome"]
            for t in _trace(trace_path)
            if t.get("response", {}).get("id") == "perm-1"
        ]
        assert outcomes[-1] == {"outcome": "cancelled"}

    def test_permission_fn_error_cancels(self, acp_env, tmp_path, trace_path, monkeypatch):
        monkeypatch.setenv("FAKE_ACP_PERMISSION", "1")
        backend = acp_backend.AcpBackend("gemini")

        def boom(payload):
            raise RuntimeError("hub unreachable")

        result = backend.run(
            prompt="edit",
            cwd=str(tmp_path),
            session_dir=str(tmp_path / "sess"),
            session_id="item-1",
            agent_label="Desktop_Agent1",
            timeout_s=30,
            permission_fn=boom,
        )
        assert result["status"] == "success"
        outcomes = [
            t["response"]["result"]["outcome"]
            for t in _trace(trace_path)
            if t.get("response", {}).get("id") == "perm-1"
        ]
        assert outcomes[-1] == {"outcome": "cancelled"}

    def test_kill_check_sends_cancel_and_kills(self, acp_env, tmp_path, trace_path, monkeypatch):
        monkeypatch.setenv("FAKE_ACP_HANG", "1")
        backend = acp_backend.AcpBackend("gemini")
        result = backend.run(
            prompt="edit",
            cwd=str(tmp_path),
            session_dir=str(tmp_path / "sess"),
            session_id="item-1",
            agent_label="Desktop_Agent1",
            timeout_s=60,
            kill_check=lambda: True,
        )
        assert result["status"] == "killed", result
        cancels = [t for t in _trace(trace_path) if "cancelled" in t]
        assert cancels and cancels[0]["cancelled"] == ACP_SESSION

    def test_unsupported_inbound_methods_get_32601(
        self, acp_env, tmp_path, trace_path, monkeypatch
    ):
        monkeypatch.setenv("FAKE_ACP_PROBE", "1")
        backend = acp_backend.AcpBackend("gemini")
        result = backend.run(
            prompt="edit",
            cwd=str(tmp_path),
            session_dir=str(tmp_path / "sess"),
            session_id="item-1",
            agent_label="Desktop_Agent1",
            timeout_s=30,
        )
        assert result["status"] == "success"
        probe = [
            t["response"]
            for t in _trace(trace_path)
            if t.get("response", {}).get("id") == "probe-1"
        ]
        assert probe and probe[0]["error"]["code"] == -32601

    def test_missing_binary_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_GEMINI_BIN", str(tmp_path / "not-here"))
        backend = acp_backend.AcpBackend("gemini")
        result = backend.run(
            prompt="x",
            cwd=str(tmp_path),
            session_dir=str(tmp_path),
            session_id="i",
            agent_label="a",
            timeout_s=10,
        )
        assert result["status"] == "error"
        assert "not on PATH" in result["error"] or "unavailable" in result["error"].lower()


# --- hub side -----------------------------------------------------------------


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setenv(
        "UA_AGENT_TOKENS", f"Desktop_Agent1:{CLAUDE_TOKEN},Desktop_Person:{HUMAN_TOKEN}"
    )
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
    monkeypatch.setenv("UA_NODE_ALLOW_BEARER", "1")
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        yield client


def _transport(client, token=CLAUDE_TOKEN):
    def call(path, body, timeout):
        resp = client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
        return resp.status_code, resp.json()

    return call


def _acp_facts(root: Path):
    return {
        "os": {"system": "Linux"},
        "harnesses": {"acp:gemini": {"path": "/usr/bin/gemini", "acp": True}},
        "backends": ["acp:gemini"],
        "node_features": ["session_settings"],
        "worktree_roots": [{"path": str(root), "exists": True, "worktrees": 0}],
    }


def _register(hub, root):
    agent = node_agent.NodeAgent(
        node=NODE,
        transport=_transport(hub),
        roots=[root],
        backends=("acp:gemini",),
        facts_fn=lambda: _acp_facts(root),
        sleep=lambda s: None,
        rng=random.Random(7),
        claim_wait_s=0,
        session_root=root.parent / "sessions",
        spool_dir=root.parent / "spool",
    )
    assert agent.run_once() == "registered"
    return agent


def _queued_acp_item(tmp_path) -> dict:
    root = tmp_path / "dev"
    (root / "repo").mkdir(parents=True, exist_ok=True)
    return root


class TestNodeEventsRoute:
    def test_events_recorded_and_session_lifecycle(self, hub, tmp_path):
        root = _queued_acp_item(tmp_path)
        _register(hub, root)
        item = node_queue.enqueue(
            NODE,
            created_by="Desktop_Agent1",
            prompt="hi",
            cwd=str(root / "repo"),
            backend="acp:gemini",
        )
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert claimed and claimed["item_id"] == item["item_id"]
        resp = hub.post(
            "/api/node/events",
            json={
                "node": NODE,
                "item_id": item["item_id"],
                "events": [
                    {
                        "v": 1,
                        "kind": "session_open",
                        "session_id": ACP_SESSION,
                        "at": "2026-01-01T00:00:00+00:00",
                        "data": {"acp_session_id": ACP_SESSION, "agent_info": {"name": "fake"}},
                    },
                    {
                        "v": 1,
                        "kind": "agent_message_chunk",
                        "session_id": ACP_SESSION,
                        "at": "2026-01-01T00:00:01+00:00",
                        "data": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "hi"},
                        },
                    },
                    {
                        "v": 1,
                        "kind": "turn_end",
                        "session_id": ACP_SESSION,
                        "at": "2026-01-01T00:00:02+00:00",
                        "data": {"stopReason": "end_turn"},
                    },
                ],
            },
            headers={"Authorization": f"Bearer {CLAUDE_TOKEN}"},
        )
        assert resp.status_code == 200, resp.json()
        assert resp.json() == {"recorded": 3, "dropped": 0}
        conn = storage.connect()
        try:
            sess = conn.execute(
                "SELECT * FROM agent_sessions WHERE external_session_id=? AND kind='acp'",
                (ACP_SESSION,),
            ).fetchone()
            events = conn.execute(
                "SELECT payload FROM event_log WHERE channel=?", (node_queue.CHANNEL_NODE,)
            ).fetchall()
        finally:
            conn.close()
        assert sess is not None
        assert sess["status"] == "completed"  # turn_end closed it
        kinds = [
            json.loads(r["payload"])["acp_kind"]
            for r in events
            if json.loads(r["payload"]).get("kind") == "acp_event"
        ]
        assert kinds == ["session_open", "agent_message_chunk", "turn_end"]

    def test_events_bounded_and_scoped(self, hub, tmp_path):
        root = _queued_acp_item(tmp_path)
        _register(hub, root)
        item = node_queue.enqueue(
            NODE,
            created_by="Desktop_Agent1",
            prompt="hi",
            cwd=str(root / "repo"),
            backend="acp:gemini",
        )
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert claimed and claimed["item_id"] == item["item_id"]
        big = {"v": 1, "kind": "agent_message_chunk", "data": "x" * 40_000}
        resp = hub.post(
            "/api/node/events",
            json={
                "node": NODE,
                "item_id": item["item_id"],
                "events": [{"v": 1, "kind": "ok"}, big, "garbage", {"v": 1}],
            },
            headers={"Authorization": f"Bearer {CLAUDE_TOKEN}"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"recorded": 2, "dropped": 2}
        resp = hub.post(
            "/api/node/events",
            json={"node": NODE, "item_id": item["item_id"], "events": "nope"},
            headers={"Authorization": f"Bearer {CLAUDE_TOKEN}"},
        )
        assert resp.status_code == 400
        resp = hub.post(
            "/api/node/events",
            json={"node": NODE, "item_id": "nd-0000000000000000", "events": []},
            headers={"Authorization": f"Bearer {CLAUDE_TOKEN}"},
        )
        assert resp.status_code == 404


class TestNodePermissionRoute:
    def _claim(self, hub, root):
        agent = _register(hub, root)
        item = node_queue.enqueue(
            NODE,
            created_by="Desktop_Agent1",
            prompt="hi",
            cwd=str(root / "repo"),
            backend="acp:gemini",
        )
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert claimed and claimed["item_id"] == item["item_id"]
        return agent, item

    def _post_permission(self, hub, item_id):
        return hub.post(
            "/api/node/permission",
            json={
                "node": NODE,
                "item_id": item_id,
                "session_id": ACP_SESSION,
                "tool_call": {"toolCallId": "tc-1", "title": "Edit file"},
                "options": [
                    {"optionId": "deny-1", "kind": "reject_once"},
                    {"optionId": "allow-1", "kind": "allow_once"},
                ],
            },
            headers={"Authorization": f"Bearer {CLAUDE_TOKEN}"},
        )

    def test_pending_then_approved_with_option(self, hub, tmp_path):
        root = _queued_acp_item(tmp_path)
        _, item = self._claim(hub, root)
        resp = self._post_permission(hub, item["item_id"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending"
        approval_id = body["approval_id"]
        # Same request polls the same pending row, not a new one.
        again = self._post_permission(hub, item["item_id"]).json()
        assert again["status"] == "pending" and again["approval_id"] == approval_id
        verified = config.agent_identity_var.set("Desktop_Person")
        try:
            approval_resolve(approval_id, "approve", resolved_by="Desktop_Person")
        finally:
            config.agent_identity_var.reset(verified)
        decided = self._post_permission(hub, item["item_id"]).json()
        assert decided["status"] == "approved"
        assert decided["option_id"] == "allow-1"
        # The pass is one-shot: an identical re-poll parks a fresh pending row.
        follow_up = self._post_permission(hub, item["item_id"]).json()
        assert follow_up["status"] == "pending"
        assert follow_up["approval_id"] != approval_id

    def test_denied_returns_reason(self, hub, tmp_path):
        root = _queued_acp_item(tmp_path)
        _, item = self._claim(hub, root)
        body = self._post_permission(hub, item["item_id"]).json()
        approval_resolve(
            body["approval_id"], "deny", resolved_by="Operator", reason="not on this repo"
        )
        decided = self._post_permission(hub, item["item_id"]).json()
        assert decided["status"] == "denied"
        assert "not on this repo" in decided["reason"]

    def test_retired_identity_is_refused_with_evidence(self, hub, tmp_path):
        """Task #2305: a retired caller's permission request is a typed 403, recorded."""
        from unified_agent import agent_lifecycle

        root = _queued_acp_item(tmp_path)
        _, item = self._claim(hub, root)
        verified = config.agent_identity_var.set(config.operator_label())
        try:
            agent_lifecycle.agent_retire("Desktop_Agent1", "decommissioned")
        finally:
            config.agent_identity_var.reset(verified)
        resp = self._post_permission(hub, item["item_id"])
        assert resp.status_code == 403, resp.text
        assert "is retired" in resp.json()["error"]
        agent_lifecycle.join_refusal_writers()
        conn = storage.connect()
        try:
            sites = [
                json.loads(r["payload"])["site"]
                for r in conn.execute(
                    "SELECT payload FROM event_log WHERE channel=? "
                    "AND json_extract(payload, '$.kind')=?",
                    (agent_lifecycle.CHANNEL, agent_lifecycle.KIND_REFUSED),
                )
            ]
        finally:
            conn.close()
        # Since #2315 the auth middleware refuses a retired bearer first
        # (site hub.auth.bearer); the node-queue check stays as a second line.
        assert {"hub.auth.bearer", "node_queue.permission"} & set(sites), sites

    def _approve(self, approval_id: str) -> None:
        verified = config.agent_identity_var.set(config.operator_label())
        try:
            approval_resolve(approval_id, "approve", resolved_by=config.operator_label())
        finally:
            config.agent_identity_var.reset(verified)

    def _approval_status(self, approval_id: str) -> str:
        conn = storage.connect()
        try:
            return conn.execute(
                "SELECT status FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()["status"]
        finally:
            conn.close()

    def _approved_prompt(self, hub, tmp_path) -> tuple[dict, str]:
        root = _queued_acp_item(tmp_path)
        _, item = self._claim(hub, root)
        pending = self._post_permission(hub, item["item_id"]).json()
        assert pending["status"] == "pending"
        self._approve(pending["approval_id"])
        return item, pending["approval_id"]

    def test_paused_dispatch_target_is_refused_despite_an_approval(self, hub, tmp_path):
        """Task #2315 review: the dispatched target is checked, not only the device owner."""
        from unified_agent import agent_lifecycle

        root = _queued_acp_item(tmp_path)
        _, item = self._claim(hub, root)
        from unified_agent.tools.tasks import task_add

        run_id, target = "run-2315-target", "Local_Target"
        task_id = task_add(title="linked")["id"]
        conn = storage.connect()
        try:
            ts = storage.now()
            conn.execute(
                "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
                "created_at, updated_at) VALUES (?, ?, ?, 'running', 'test', ?, ?)",
                (run_id, task_id, target, ts, ts),
            )
            conn.execute("UPDATE node_dispatch SET run_id=? WHERE id=?", (run_id, item["item_id"]))
            conn.commit()
        finally:
            conn.close()
        pending = self._post_permission(hub, item["item_id"]).json()
        assert pending["status"] == "pending"
        self._approve(pending["approval_id"])
        verified = config.agent_identity_var.set(config.operator_label())
        try:
            agent_lifecycle.agent_pause(target, "hold")
        finally:
            config.agent_identity_var.reset(verified)
        resp = self._post_permission(hub, item["item_id"])
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "agent_paused"
        assert self._approval_status(pending["approval_id"]) == "approved"

    def test_cancel_requested_item_is_denied_despite_an_approval(self, hub, tmp_path):
        item, approval_id = self._approved_prompt(hub, tmp_path)
        node_queue.cancel(item["item_id"], actor="Desktop_Agent1")
        decided = self._post_permission(hub, item["item_id"]).json()
        assert decided["status"] == "denied"
        assert "cancellation was requested" in decided["reason"]
        assert self._approval_status(approval_id) == "approved"

    def test_consuming_an_approval_rechecks_cancellation(self, hub, tmp_path, monkeypatch):
        """A cancel landing between the gate read and the consume spends nothing."""
        from unified_agent.tools import approvals as approvals_tool

        item, approval_id = self._approved_prompt(hub, tmp_path)
        real_gate = approvals_tool.check_approval_gate

        def gate_then_cancel(*args, **kwargs):
            result = real_gate(*args, **kwargs)
            node_queue.cancel(item["item_id"], actor="Desktop_Agent1")
            return result

        monkeypatch.setattr(approvals_tool, "check_approval_gate", gate_then_cancel)
        decided = self._post_permission(hub, item["item_id"]).json()
        assert decided["status"] == "denied"
        assert decided["approval_id"] == approval_id
        assert self._approval_status(approval_id) == "approved"

    def test_permission_requires_claimed_item(self, hub, tmp_path):
        root = _queued_acp_item(tmp_path)
        _register(hub, root)
        item = node_queue.enqueue(
            NODE,
            created_by="Desktop_Agent1",
            prompt="hi",
            cwd=str(root / "repo"),
            backend="acp:gemini",
        )
        body = self._post_permission(hub, item["item_id"]).json()
        # Still queued, not claimed → deny-shaped 200, never an approval row.
        assert body["status"] == "denied"

    def test_missing_fields_rejected(self, hub, tmp_path):
        root = _queued_acp_item(tmp_path)
        _register(hub, root)
        resp = hub.post(
            "/api/node/permission",
            json={"node": NODE, "item_id": "nd-0000000000000000"},
            headers={"Authorization": f"Bearer {CLAUDE_TOKEN}"},
        )
        assert resp.status_code == 400


class TestAcpSessionMirror:
    def test_turn_end_timeout_marks_session_failed(self, hub, tmp_path):
        """timeout/error stop reasons close the mirror row as failed."""
        root = _queued_acp_item(tmp_path)
        _register(hub, root)
        item = node_queue.enqueue(
            NODE,
            prompt="x",
            cwd=str(root / "repo"),
            backend="acp:gemini",
            created_by="Operator",
        )
        node_queue.claim_next(NODE, identity="Desktop_Agent1")
        node_queue.record_node_event(
            NODE,
            item["item_id"],
            [{"kind": "session_open", "session_id": ACP_SESSION, "at": "t", "data": {}}],
            identity="Desktop_Agent1",
        )
        node_queue.record_node_event(
            NODE,
            item["item_id"],
            [
                {
                    "kind": "turn_end",
                    "session_id": ACP_SESSION,
                    "at": "t2",
                    "data": {"stop_reason": "timeout"},
                }
            ],
            identity="Desktop_Agent1",
        )
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT status, exit_status FROM agent_sessions "
                "WHERE external_session_id=? AND kind='acp'",
                (ACP_SESSION,),
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "failed"
        assert row["exit_status"] == "timeout"


class TestSessionIdMigration:
    def test_session_id_column_added_to_existing_db(self, tmp_path, monkeypatch):
        """A pre-I2 DB gains node_dispatch.session_id via _migrate (schema 36)."""
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        storage._connection = None
        storage.init_db()
        conn = storage.connect()
        try:
            conn.execute("ALTER TABLE node_dispatch DROP COLUMN session_id")
            conn.commit()
        finally:
            conn.close()
        storage._connection = None

        storage.init_db()

        conn = storage.connect()
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(node_dispatch)").fetchall()}
        finally:
            conn.close()
        assert "session_id" in cols
        storage._connection = None


class TestNodeAgentAcpWiring:
    def test_allowed_backends_drops_unknown_acp(self, monkeypatch):
        monkeypatch.setenv("UA_NODE_BACKENDS", "codex,acp:gemini,acp:bogus,acp:")
        assert node_agent.allowed_backends() == ("codex", "acp:gemini")

    def test_facts_advertise_only_resolved_acp(self, monkeypatch, tmp_path):
        fake = tmp_path / "fake-agent"
        fake.write_text("#!\n")
        fake.chmod(0o755)
        monkeypatch.setenv("UA_GEMINI_BIN", str(fake))

        def which(binary):
            return str(fake) if "fake-agent" in str(binary) else None

        facts = node_agent.collect_facts(
            roots=[tmp_path],
            backends=("acp:gemini", "acp:codex", "codex"),
            which=which,
            run=lambda *a, **k: type("P", (), {"stdout": "v1", "stderr": ""})(),
        )
        assert "acp:gemini" in facts["harnesses"]
        assert facts["harnesses"]["acp:gemini"]["acp"] is True
        assert "acp:codex" not in facts["harnesses"]  # codex-acp absent
        assert facts["backends"] == ["acp:gemini"]

    def test_execute_end_to_end(self, hub, tmp_path, acp_env, trace_path):
        """Full loop: claim acp item → real AcpBackend → fake agent → receipt."""
        root = _queued_acp_item(tmp_path)
        agent = node_agent.NodeAgent(
            node=NODE,
            transport=_transport(hub),
            roots=[root],
            backends=("acp:gemini",),
            facts_fn=lambda: _acp_facts(root),
            sleep=lambda s: None,
            rng=random.Random(7),
            claim_wait_s=0,
            session_root=tmp_path / "sessions",
            spool_dir=tmp_path / "spool",
        )
        assert agent.run_once() == "registered"
        node_queue.enqueue(
            NODE,
            created_by="Desktop_Agent1",
            prompt="hi",
            cwd=str(root / "repo"),
            backend="acp:gemini",
        )
        item = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert item["session_id"] is None
        receipt = agent.execute(item)
        assert receipt["status"] == "finished", receipt
        assert receipt["session_id"] == ACP_SESSION
        # The stream reached the hub while the run was live.
        conn = storage.connect()
        try:
            kinds = [
                json.loads(r["payload"]).get("acp_kind")
                for r in conn.execute(
                    "SELECT payload FROM event_log WHERE channel=?",
                    (node_queue.CHANNEL_NODE,),
                ).fetchall()
            ]
        finally:
            conn.close()
        assert "session_open" in kinds and "turn_end" in kinds
        resp = agent.post_receipt(item["item_id"], {**receipt, "attempt": item["attempt"]})
        assert resp["duplicate"] is False
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT session_id FROM node_dispatch WHERE id=?", (item["item_id"],)
            ).fetchone()
        finally:
            conn.close()
        assert row["session_id"] == ACP_SESSION

    def test_execute_permission_relay_approves(
        self, hub, tmp_path, acp_env, trace_path, monkeypatch
    ):
        """A harness permission prompt parks a hub approval; the approve lands."""
        monkeypatch.setenv("FAKE_ACP_PERMISSION", "1")
        monkeypatch.setattr(node_agent, "ACP_PERMISSION_POLL_S", 0.05)
        root = _queued_acp_item(tmp_path)
        agent = node_agent.NodeAgent(
            node=NODE,
            transport=_transport(hub),
            roots=[root],
            backends=("acp:gemini",),
            facts_fn=lambda: _acp_facts(root),
            sleep=lambda s: None,
            rng=random.Random(7),
            claim_wait_s=0,
            session_root=tmp_path / "sessions",
            spool_dir=tmp_path / "spool",
        )
        agent.run_once()
        node_queue.enqueue(
            NODE,
            created_by="Desktop_Agent1",
            prompt="hi",
            cwd=str(root / "repo"),
            backend="acp:gemini",
        )
        item = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert item["permission_mode"] == "supervised"
        outcome: dict = {}

        def _run():
            outcome["receipt"] = agent.execute(item)

        runner = threading.Thread(target=_run, daemon=True)
        runner.start()
        approval_id = None
        deadline = time.monotonic() + 15
        conn = storage.connect()
        try:
            while approval_id is None and time.monotonic() < deadline:
                row = conn.execute(
                    "SELECT id FROM approvals WHERE tool=? AND status='pending'",
                    (node_queue.ACP_PERMISSION_TOOL,),
                ).fetchone()
                if row is not None:
                    approval_id = row["id"]
                else:
                    time.sleep(0.01)
        finally:
            conn.close()
        assert approval_id is not None, "permission prompt never queued an approval"
        verified = config.agent_identity_var.set("Desktop_Person")
        try:
            approval_resolve(approval_id, "approve", resolved_by="Desktop_Person")
        finally:
            config.agent_identity_var.reset(verified)
        runner.join(timeout=20)
        assert not runner.is_alive()
        assert outcome["receipt"]["status"] == "finished"
        conn = storage.connect()
        try:
            opened = conn.execute(
                "SELECT payload FROM event_log WHERE channel=? AND json_extract(payload, '$.item_id')=? AND json_extract(payload, '$.acp_kind')='session_open'",
                (node_queue.CHANNEL_NODE, item["item_id"]),
            ).fetchone()
        finally:
            conn.close()
        settings = json.loads(opened["payload"])["data"]["settings"]
        assert settings["permission_mode"] == "supervised"
        assert settings["current_mode_id"] == "default"
        outcomes = [
            t["response"]["result"]["outcome"]
            for t in _trace(trace_path)
            if t.get("response", {}).get("id") == "perm-1"
        ]
        assert outcomes[-1] == {"outcome": "selected", "optionId": "allow-1"}
