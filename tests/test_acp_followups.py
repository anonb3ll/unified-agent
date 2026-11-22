"""Turn lifecycle events and queue/steer follow-ups for ACP sessions (task #1740, I10).

Backend tests drive ``AcpBackend`` against a fake ACP agent script that speaks
newline-delimited JSON-RPC on stdio and traces every prompt, cancel and steer
it receives. Hub tests cover ``node_followups`` (submit, idempotent retry,
claim order, delivery folding, drop on receipt) through the real routes, and
one end-to-end run goes node -> hub -> fake agent -> receipt.
"""

from __future__ import annotations

import json
import random
import secrets
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import (
    acp_backend,
    acp_matrix,
    config,
    node_agent,
    node_followups,
    node_queue,
    server,
    storage,
    webui,
)

TOKEN = f"tok-{secrets.token_urlsafe(8)}"
RESEARCH_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
APPROVER_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
CODEX_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
HERMES_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
OPERATOR_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
#: The agent label ``TOKEN`` authenticates as in the ``hub`` fixture.
TOKEN_AGENT = "Desktop_Agent1"
NODE = "acp-followup-01"
ACP_SESSION = "acp-followup-session"

FAKE_AGENT = r"""
import json
import os
import select
import sys
import time


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def note(key, value):
    with open(os.environ["FAKE_TRACE"], "a") as fh:
        fh.write(json.dumps({key: value}) + "\n")


def chunk(sid, text):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid,
          "update": {"sessionUpdate": "agent_message_chunk",
                     "content": {"type": "text", "text": text}}}})


hang = set(filter(None, os.environ.get("FAKE_HANG", "").split("|")))
steering = os.environ.get("FAKE_STEERING", "")
# A steer with this text answers promptRequired and the hung turn settles on its
# own 1.5 s later (the window in which a later steer must not jump the queue).
steer_idle = os.environ.get("FAKE_STEER_IDLE", "")
pending = None
finish_at = None


def lines():
    global finish_at, pending
    buf = b""
    while True:
        timeout = None if finish_at is None else max(0.0, finish_at - time.time())
        ready, _, _ = select.select([0], [], [], timeout)
        if not ready:
            if pending is not None:
                send({"jsonrpc": "2.0", "id": pending, "result": {"stopReason": "end_turn"}})
                pending = None
            finish_at = None
            continue
        chunk_ = os.read(0, 65536)
        if not chunk_:
            return
        buf += chunk_
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode()


for raw in lines():
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {},
                  "agentInfo": {"name": "fake", "version": "0"}}
        if steering:
            result["_meta"] = {"steering": {"supported": True}}
        send({"jsonrpc": "2.0", "id": rid, "result": result})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "SID", "modes": {"currentModeId": "default", "availableModes": [{"id": "default", "name": "Default"}]}}})
    elif method == "session/set_mode":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "session/prompt":
        text = params["prompt"][0]["text"]
        note("prompt", text)
        chunk(params["sessionId"], "re:" + text + ";")
        if text in hang:
            pending = rid
            continue
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
    elif method == "session/cancel":
        note("cancel", params.get("sessionId"))
        if pending is not None:
            send({"jsonrpc": "2.0", "id": pending, "result": {"stopReason": "cancelled"}})
            pending = None
    elif method == "_session/steering":
        text = params["prompt"][0]["text"]
        note("steer", {"text": text, "meta": params.get("_meta")})
        if steering == "error":
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32603, "message": "steer failed"}})
            continue
        if pending is None or text == steer_idle:
            send({"jsonrpc": "2.0", "id": rid, "result": {"outcome": "promptRequired"}})
            if text == steer_idle:
                finish_at = time.time() + 1.5
            continue
        send({"jsonrpc": "2.0", "id": rid, "result": {"outcome": "injected"}})
        chunk(params["sessionId"], "steered:" + text + ";")
        send({"jsonrpc": "2.0", "id": pending, "result": {"stopReason": "end_turn"}})
        pending = None
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32601, "message": "unknown " + str(method)}})
""".replace("SID", ACP_SESSION)


@pytest.fixture
def fake_agent(tmp_path, monkeypatch) -> Path:
    script = tmp_path / "fake_followup_agent.py"
    script.write_text("#!" + sys.executable + "\n" + FAKE_AGENT)
    script.chmod(0o755)
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("UA_GEMINI_BIN", str(script))
    monkeypatch.setenv("FAKE_TRACE", str(trace))
    return trace


def _trace(path: Path) -> list[tuple[str, object]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        ((key, value),) = json.loads(line).items()
        out.append((key, value))
    return out


class _Script:
    """A followup_fn that hands out pre-set batches, one per call."""

    def __init__(self, *batches: list[dict]):
        self.batches = list(batches)
        self.calls = 0

    def __call__(self) -> list[dict]:
        self.calls += 1
        return self.batches.pop(0) if self.batches else []


def _run(tmp_path, followup_fn, *, kill_check=None, timeout_s=30):
    events: list[dict] = []
    result = acp_backend.AcpBackend("gemini").run(
        prompt="initial",
        cwd=str(tmp_path),
        session_dir=str(tmp_path / "sess"),
        session_id="item-1",
        agent_label="Desktop_Agent1",
        timeout_s=timeout_s,
        on_event=events.append,
        followup_fn=followup_fn,
        followup_poll_s=0.0,
        kill_check=kill_check,
    )
    return result, events


def _kinds(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events]


def _of(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["kind"] == kind]


# --- backend: turn lifecycle --------------------------------------------------------


class TestTurnLifecycle:
    def test_single_turn_emits_lifecycle_and_keeps_raw_kinds(self, fake_agent, tmp_path):
        result, events = _run(tmp_path, None)
        assert result["status"] == "success", result
        kinds = _kinds(events)
        assert kinds.index("turn_start") < kinds.index("agent_message_chunk")
        assert kinds.index("agent_message_chunk") < kinds.index("turn_end")
        assert kinds[-1] == "session_end"
        start = _of(events, "turn_start")[0]
        assert start["data"] == {
            "turn_id": "turn-1",
            "index": 1,
            "source": "initial",
            "followup_ids": [],
        }
        # The raw ACP update keeps its own kind and carries the turn id.
        update = _of(events, "agent_message_chunk")[0]
        assert update["turn_id"] == "turn-1"
        assert update["data"]["sessionUpdate"] == "agent_message_chunk"
        end = _of(events, "turn_end")[0]
        assert end["turn_id"] == "turn-1"
        assert end["data"]["stop_reason"] == "end_turn"
        assert _of(events, "session_end")[0]["data"] == {"stop_reason": "end_turn", "turns": 1}
        # Outside a turn there is no turn id.
        assert _of(events, "session_open")[0]["turn_id"] is None
        assert _of(events, "session_open")[0]["data"]["steering"] == "emulated"

    def test_kill_aborts_turn_and_drops_pending_followups(self, fake_agent, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_HANG", "initial")
        followups = _Script([{"id": "nf-0000000000000a01", "mode": "queue", "body": "later"}])
        ticks = {"n": 0}

        def kill_check() -> bool:
            # The first tick polls the hub; the hub cancels on the next one.
            ticks["n"] += 1
            return ticks["n"] > 1

        result, events = _run(tmp_path, followups, kill_check=kill_check)
        assert result["status"] == "killed", result
        aborted = _of(events, "turn_aborted")
        assert [a["data"]["reason"] for a in aborted] == ["cancel"]
        assert "turn_end" not in _kinds(events)
        assert ("prompt", "later") not in _trace(fake_agent)
        dropped = _of(events, "followup_dropped")
        assert [(d["data"]["followup_id"], d["data"]["reason"]) for d in dropped] == [
            ("nf-0000000000000a01", "cancelled")
        ]
        assert _kinds(events)[-1] == "session_end"


# --- backend: queue -----------------------------------------------------------------


class TestQueue:
    def test_queued_followups_run_in_order_after_each_turn(self, fake_agent, tmp_path):
        followups = _Script(
            [
                {"id": "nf-0000000000000a01", "mode": "queue", "body": "first follow-up"},
                {"id": "nf-0000000000000a02", "mode": "queue", "body": "second follow-up"},
            ],
        )
        result, events = _run(tmp_path, followups)
        assert result["status"] == "success", result
        prompts = [v for k, v in _trace(fake_agent) if k == "prompt"]
        assert prompts == ["initial", "first follow-up", "second follow-up"]
        starts = _of(events, "turn_start")
        assert [(s["data"]["turn_id"], s["data"]["source"]) for s in starts] == [
            ("turn-1", "initial"),
            ("turn-2", "queue"),
            ("turn-3", "queue"),
        ]
        assert [s["data"]["followup_ids"] for s in starts] == [
            [],
            ["nf-0000000000000a01"],
            ["nf-0000000000000a02"],
        ]
        delivered = _of(events, "followup_delivered")
        assert [(d["data"]["followup_id"], d["data"]["path"], d["turn_id"]) for d in delivered] == [
            ("nf-0000000000000a01", "prompt", "turn-2"),
            ("nf-0000000000000a02", "prompt", "turn-3"),
        ]
        assert len(_of(events, "turn_end")) == 3
        assert len(_of(events, "session_end")) == 1
        assert result["text"] == "re:initial;re:first follow-up;re:second follow-up;"

    def test_followup_seen_twice_is_sent_once(self, fake_agent, tmp_path):
        dup = {"id": "nf-0000000000000a01", "mode": "queue", "body": "once"}
        result, _events = _run(tmp_path, _Script([dup], [dup], [dup]))
        assert result["status"] == "success"
        prompts = [v for k, v in _trace(fake_agent) if k == "prompt"]
        assert prompts == ["initial", "once"]

    def test_malformed_followups_are_ignored(self, fake_agent, tmp_path):
        bad = [
            {"id": "nf-x", "mode": "shout", "body": "no"},
            {"id": "", "mode": "queue", "body": "no"},
            {"id": "nf-y", "mode": "queue", "body": "   "},
            "not a dict",
        ]
        result, _events = _run(tmp_path, _Script(bad))
        assert result["status"] == "success"
        assert [v for k, v in _trace(fake_agent) if k == "prompt"] == ["initial"]


# --- backend: steer -----------------------------------------------------------------


class TestSteer:
    def test_emulated_steer_cancels_then_reprompts(self, fake_agent, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_HANG", "initial")
        followups = _Script(
            [],
            [
                {"id": "nf-0000000000000b01", "mode": "steer", "body": "change course"},
                {"id": "nf-0000000000000a01", "mode": "queue", "body": "afterwards"},
            ],
        )
        result, events = _run(tmp_path, followups)
        assert result["status"] == "success", result
        trace = _trace(fake_agent)
        assert [k for k, _ in trace] == ["prompt", "cancel", "prompt", "prompt"]
        assert [v for k, v in trace if k == "prompt"] == [
            "initial",
            "change course",
            "afterwards",
        ]
        aborted = _of(events, "turn_aborted")
        assert len(aborted) == 1
        assert aborted[0]["data"] == {
            "turn_id": "turn-1",
            "reason": "steer",
            "stop_reason": "cancelled",
        }
        assert _of(events, "steer_cancel_sent")[0]["data"]["followup_ids"] == [
            "nf-0000000000000b01"
        ]
        delivered = {d["data"]["followup_id"]: d["data"] for d in _of(events, "followup_delivered")}
        assert delivered["nf-0000000000000b01"]["path"] == "cancel_reprompt"
        assert delivered["nf-0000000000000b01"]["turn_id"] == "turn-2"
        assert delivered["nf-0000000000000a01"]["path"] == "prompt"
        starts = [s["data"]["source"] for s in _of(events, "turn_start")]
        assert starts == ["initial", "steer", "queue"]

    def test_steer_takes_priority_over_earlier_queue(self, fake_agent, tmp_path):
        followups = _Script(
            [
                {"id": "nf-0000000000000a01", "mode": "queue", "body": "queued first"},
                {"id": "nf-0000000000000b01", "mode": "steer", "body": "steer A"},
                {"id": "nf-0000000000000b02", "mode": "steer", "body": "steer B"},
            ],
        )
        result, _events = _run(tmp_path, followups)
        assert result["status"] == "success"
        prompts = [v for k, v in _trace(fake_agent) if k == "prompt"]
        # Pending steer messages redirect together, then the queue resumes.
        assert prompts == ["initial", "steer A\n\nsteer B", "queued first"]

    def test_native_steer_injects_without_cancel(self, fake_agent, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_HANG", "initial")
        monkeypatch.setenv("FAKE_STEERING", "1")
        followups = _Script(
            [], [{"id": "nf-0000000000000b01", "mode": "steer", "body": "turn left"}]
        )
        result, events = _run(tmp_path, followups)
        assert result["status"] == "success", result
        trace = _trace(fake_agent)
        assert [k for k, _ in trace] == ["prompt", "steer"]
        steer = trace[1][1]
        assert steer["text"] == "turn left"
        assert steer["meta"] == {"steering": {"idleBehavior": "promptRequired"}}
        assert _of(events, "session_open")[0]["data"]["steering"] == "native"
        delivered = _of(events, "followup_delivered")
        assert [(d["data"]["path"], d["turn_id"]) for d in delivered] == [
            ("native_steer", "turn-1")
        ]
        assert len(_of(events, "turn_start")) == 1
        assert "turn_aborted" not in _kinds(events)
        assert "steered:turn left;" in result["text"]

    def test_native_steer_error_falls_back_to_cancel_and_reprompt(
        self, fake_agent, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("FAKE_HANG", "initial")
        monkeypatch.setenv("FAKE_STEERING", "error")
        followups = _Script(
            [], [{"id": "nf-0000000000000b01", "mode": "steer", "body": "turn left"}]
        )
        result, events = _run(tmp_path, followups)
        assert result["status"] == "success", result
        assert [k for k, _ in _trace(fake_agent)] == ["prompt", "steer", "cancel", "prompt"]
        assert _of(events, "steer_fallback")
        delivered = _of(events, "followup_delivered")
        assert [d["data"]["path"] for d in delivered] == ["cancel_reprompt"]


class TestDeliveryTruth:
    def test_prompt_write_failure_drops_instead_of_delivering(
        self, fake_agent, tmp_path, monkeypatch
    ):
        from unified_agent import acp_client

        real = acp_client.AcpClient._start
        calls = {"prompt": 0}

        def failing(self, method, params):
            if method == "session/prompt":
                calls["prompt"] += 1
                if calls["prompt"] == 2:
                    raise acp_client.AcpError("acp write failed: broken pipe")
            return real(self, method, params)

        monkeypatch.setattr(acp_client.AcpClient, "_start", failing)
        followups = _Script([{"id": "nf-00000000000000a1", "mode": "queue", "body": "next"}])
        result, events = _run(tmp_path, followups)
        assert result["status"] == "error", result
        assert _of(events, "followup_delivered") == []
        dropped = _of(events, "followup_dropped")
        assert [(d["data"]["followup_id"], d["data"]["reason"]) for d in dropped] == [
            ("nf-00000000000000a1", "error")
        ]

    def test_cancel_between_turns_starts_no_followup_turn(self, fake_agent, tmp_path):
        followups = _Script([{"id": "nf-00000000000000a2", "mode": "queue", "body": "next"}])
        # The hub cancels after the first turn ended and the follow-up arrived.
        result, events = _run(tmp_path, followups, kill_check=lambda: followups.calls > 0)
        assert result["status"] == "killed", result
        assert [v for k, v in _trace(fake_agent) if k == "prompt"] == ["initial"]
        assert len(_of(events, "turn_start")) == 1
        assert _of(events, "followup_delivered") == []
        dropped = _of(events, "followup_dropped")
        assert [(d["data"]["followup_id"], d["data"]["reason"]) for d in dropped] == [
            ("nf-00000000000000a2", "cancelled")
        ]

    def test_held_native_steer_keeps_later_steers_behind_it(
        self, fake_agent, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("FAKE_HANG", "initial")
        monkeypatch.setenv("FAKE_STEERING", "1")
        monkeypatch.setenv("FAKE_STEER_IDLE", "first")
        followups = _Script(
            [],
            [{"id": "nf-00000000000000b1", "mode": "steer", "body": "first"}],
            [{"id": "nf-00000000000000b2", "mode": "steer", "body": "second"}],
        )
        result, events = _run(tmp_path, followups)
        assert result["status"] == "success", result
        trace = _trace(fake_agent)
        # "second" is never injected ahead of the held "first".
        assert [v["text"] for k, v in trace if k == "steer"] == ["first"]
        assert [v for k, v in trace if k == "prompt"] == ["initial", "first\n\nsecond"]
        delivered = _of(events, "followup_delivered")
        assert [d["data"]["followup_id"] for d in delivered] == [
            "nf-00000000000000b1",
            "nf-00000000000000b2",
        ]

    def test_hub_reply_is_bounded_node_side(self):
        good = {"id": "nf-00000000000000c1", "mode": "queue", "body": "ok"}
        too_long = {**good, "id": "nf-00000000000000c2", "body": "x" * 20_001}
        bad_id = {**good, "id": "not-an-id"}
        flood = [{**good, "id": f"nf-{n:016x}"} for n in range(100)]
        assert acp_backend._clean_followups([too_long, bad_id, good]) == [good]
        assert len(acp_backend._clean_followups(flood)) == acp_backend.MAX_FOLLOWUPS_PER_POLL
        assert acp_backend.MAX_FOLLOWUPS_PER_POLL == node_followups.MAX_CLAIM_PER_CALL
        assert acp_backend.MAX_FOLLOWUP_BODY_CHARS == node_followups.MAX_BODY_CHARS

    def test_transport_refuses_an_oversized_reply(self, monkeypatch):
        import io

        monkeypatch.setattr(node_agent, "MAX_RESPONSE_BYTES", 10)
        with pytest.raises(ValueError):
            node_agent._bounded_read(io.BytesIO(b'{"followups": []}'))
        assert node_agent._bounded_read(io.BytesIO(b"{}")) == b"{}"


# --- capability matrix --------------------------------------------------------------


class TestCapabilityRows:
    def test_acp_with_feature_queue_supported_steer_emulated(self):
        caps = acp_matrix.control_capabilities(
            "acp:claude", advertised=True, version=None, features=["acp_followups"]
        )["capabilities"]
        assert caps["queue"]["state"] == "supported"
        assert caps["steer"]["state"] == "emulated"
        assert "session/cancel" in caps["steer"]["detail"]

    def test_acp_node_without_feature_is_unavailable(self):
        caps = acp_matrix.control_capabilities("acp:claude", advertised=True, version=None)[
            "capabilities"
        ]
        assert caps["queue"]["state"] == "unavailable"
        assert caps["steer"]["state"] == "unavailable"

    def test_cli_executor_is_unsupported(self):
        caps = acp_matrix.control_capabilities(
            "codex", advertised=True, version=None, features=["acp_followups"]
        )["capabilities"]
        assert caps["queue"]["state"] == "unsupported"
        assert caps["steer"]["state"] == "unsupported"

    def test_node_reports_the_feature(self, tmp_path):
        facts = node_agent.collect_facts(
            roots=[tmp_path],
            backends=(),
            which=lambda _b: None,
            run=lambda *a, **k: None,
        )
        assert node_followups.NODE_FEATURE in facts["node_features"]


# --- hub ----------------------------------------------------------------------------


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        f"Desktop_Agent1:{TOKEN},Desktop_Agent8:{RESEARCH_TOKEN},Desktop_Person:{APPROVER_TOKEN},"
        f"Desktop_Agent2:{CODEX_TOKEN},Desktop_Agent5:{HERMES_TOKEN},"
        f"{config.operator_label()}:{OPERATOR_TOKEN}",
    )
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
    monkeypatch.setenv("UA_NODE_ALLOW_BEARER", "1")
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _transport(client):
    def call(path, body, timeout):
        resp = client.post(path, json=body, headers=_auth())
        return resp.status_code, resp.json()

    return call


def _facts(root: Path, *, features: bool = True) -> dict:
    facts = {
        "os": {"system": "Linux"},
        "harnesses": {"acp:gemini": {"path": "/usr/bin/gemini", "acp": True}},
        "backends": ["acp:gemini", "codex"],
        "worktree_roots": [{"path": str(root), "exists": True, "worktrees": 0}],
    }
    facts["node_features"] = ["session_settings"]
    if features:
        facts["node_features"].append(node_followups.NODE_FEATURE)
    return facts


def _agent(hub, tmp_path, *, features: bool = True) -> node_agent.NodeAgent:
    root = tmp_path / "dev"
    (root / "repo").mkdir(parents=True, exist_ok=True)
    agent = node_agent.NodeAgent(
        node=NODE,
        transport=_transport(hub),
        roots=[root],
        backends=("acp:gemini",),
        facts_fn=lambda: _facts(root, features=features),
        sleep=lambda s: None,
        rng=random.Random(7),
        claim_wait_s=0,
        session_root=tmp_path / "sessions",
        spool_dir=tmp_path / "spool",
    )
    assert agent.run_once() == "registered"
    return agent


def _claimed_item(
    agent, *, backend: str = "acp:gemini", created_by: str = "Desktop_Agent1", task_id=None
) -> dict:
    root = agent.roots[0]
    node_queue.enqueue(
        NODE,
        created_by=created_by,
        prompt="initial",
        cwd=str(root / "repo"),
        backend=backend,
        task_id=task_id,
    )
    item = node_queue.claim_next(NODE, identity="Desktop_Agent1")
    assert item is not None
    return item


def _approve(approval_id: int, decision: str = "approve") -> None:
    from unified_agent.tools.approvals import approval_resolve

    verified = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(approval_id, decision, resolved_by="Desktop_Person")
    finally:
        config.agent_identity_var.reset(verified)


def _submit(hub, item_id, *, mode="queue", body="next step", key=None, token=TOKEN, approve=True):
    """POST a follow-up; by default approve the node_dispatch gate and re-send."""
    payload = {
        "item_id": item_id,
        "mode": mode,
        "body": body,
        "idempotency_key": key or f"key-{secrets.token_hex(6)}",
    }
    resp = hub.post("/api/action/node-followup", json=payload, headers=_auth(token))
    if approve and resp.status_code == 202 and resp.json().get("status") == "approval_pending":
        _approve(resp.json()["approval_id"])
        resp = hub.post("/api/action/node-followup", json=payload, headers=_auth(token))
    return resp


def _rows(item_id: str) -> list[dict]:
    return node_followups.list_for_item(item_id)


def _node_events(kind: str) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=?", (node_queue.CHANNEL_NODE,)
        ).fetchall()
    finally:
        conn.close()
    payloads = [json.loads(r["payload"]) for r in rows]
    return [p for p in payloads if p.get("kind") == kind]


class TestSubmit:
    def test_retry_with_same_key_is_deduped(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        first = _submit(hub, item["item_id"], key="retry-key-0001")
        assert first.status_code == 201, first.json()
        assert first.json()["duplicate"] is False
        again = _submit(hub, item["item_id"], key="retry-key-0001")
        assert again.status_code == 200, again.json()
        assert again.json()["duplicate"] is True
        assert again.json()["followup_id"] == first.json()["followup_id"]
        assert len(_rows(item["item_id"])) == 1
        assert len(_node_events("followup_queued")) == 1

    def test_same_key_different_payload_is_refused(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        assert _submit(hub, item["item_id"], key="retry-key-0002").status_code == 201
        clash = _submit(hub, item["item_id"], key="retry-key-0002", body="something else")
        assert clash.status_code == 409
        assert len(_rows(item["item_id"])) == 1

    def test_queued_event_is_visible_in_transcript_feed(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        resp = _submit(hub, item["item_id"], mode="steer", body="look at tests first")
        assert resp.status_code == 201
        (event,) = _node_events("followup_queued")
        assert event["item_id"] == item["item_id"]
        assert event["status"] == "queued"
        assert event["mode"] == "steer"
        assert event["text"] == "look at tests first"

    def test_transcript_event_carries_the_whole_body(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        long_body = "x" * (node_followups.MAX_BODY_CHARS - 1)
        assert _submit(hub, item["item_id"], body=long_body).status_code == 201
        (event,) = _node_events("followup_queued")
        assert event["text"] == long_body

    @pytest.mark.parametrize(
        ("payload", "status"),
        [
            ({"mode": "shout"}, 400),
            ({"body": "  "}, 400),
            ({"idempotency_key": "short"}, 400),
            ({"idempotency_key": "has spaces in it"}, 400),
        ],
    )
    def test_malformed_requests(self, hub, tmp_path, payload, status):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        body = {
            "item_id": item["item_id"],
            "mode": "queue",
            "body": "x",
            "idempotency_key": "k" * 9,
        }
        body.update(payload)
        resp = hub.post("/api/action/node-followup", json=body, headers=_auth())
        assert resp.status_code == status, resp.json()

    def test_unknown_field_is_refused(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        resp = hub.post(
            "/api/action/node-followup",
            json={
                "item_id": item["item_id"],
                "mode": "queue",
                "body": "x",
                "idempotency_key": "k" * 9,
                "permission_mode": "bypass",
            },
            headers=_auth(),
        )
        assert resp.status_code == 400

    def test_non_acp_item_is_refused(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        root = agent.roots[0]
        # Register codex too so the node accepts the item.
        node_queue.register(
            NODE,
            identity="Desktop_Agent1",
            facts={**_facts(root), "harnesses": {"codex": {"path": "/x"}}, "backends": ["codex"]},
        )
        node_queue.enqueue(
            NODE, created_by="Desktop_Agent1", prompt="p", cwd=str(root / "repo"), backend="codex"
        )
        item = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        resp = _submit(hub, item["item_id"])
        assert resp.status_code == 409
        assert "one prompt per dispatch" in resp.json()["error"]

    def test_steer_before_start_is_refused_but_queue_is_accepted(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        root = agent.roots[0]
        item = node_queue.enqueue(
            NODE,
            created_by="Desktop_Agent1",
            prompt="p",
            cwd=str(root / "repo"),
            backend="acp:gemini",
        )
        assert _submit(hub, item["item_id"], mode="steer").status_code == 409
        assert _submit(hub, item["item_id"], mode="queue").status_code == 201

    def test_finished_item_is_refused(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        agent.post_receipt(
            item["item_id"],
            {
                "status": "finished",
                "attempt": item["attempt"],
                "started_at": "t",
                "finished_at": "t",
            },
        )
        resp = _submit(hub, item["item_id"])
        assert resp.status_code == 409
        assert "Start again" in resp.json()["error"]

    def test_node_without_feature_is_refused(self, hub, tmp_path):
        agent = _agent(hub, tmp_path, features=False)
        item = _claimed_item(agent)
        resp = _submit(hub, item["item_id"])
        assert resp.status_code == 409
        assert "upgrade" in resp.json()["error"]

    def test_profile_without_crew_view_cannot_steer(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        resp = _submit(hub, item["item_id"], mode="steer", token=RESEARCH_TOKEN)
        assert resp.status_code == 403
        assert _rows(item["item_id"]) == []

    def test_open_followups_are_capped(self, hub, tmp_path, monkeypatch):
        monkeypatch.setattr(node_followups, "MAX_OPEN_PER_ITEM", 2)
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        assert _submit(hub, item["item_id"]).status_code == 201
        assert _submit(hub, item["item_id"]).status_code == 201
        assert _submit(hub, item["item_id"]).status_code == 429


def _pending_approvals() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT id, tool, payload, status FROM approvals WHERE tool=?",
            (node_queue.NODE_DISPATCH_TOOL,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


class TestApprovalGate:
    def test_followup_waits_for_its_own_approval(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        before = len(_pending_approvals())
        first = _submit(hub, item["item_id"], body="rm the cache", key="gate-key-01", approve=False)
        assert first.status_code == 202, first.json()
        assert first.json()["status"] == "approval_pending"
        assert first.json()["tool"] == "node_dispatch"
        assert _rows(item["item_id"]) == []
        approvals = _pending_approvals()
        assert len(approvals) == before + 1
        payload = json.loads(approvals[-1]["payload"])
        assert payload["prompt"] == "rm the cache"
        assert payload["followup"] == {
            "item_id": item["item_id"],
            "mode": "queue",
            "idempotency_key": "gate-key-01",
        }
        # A re-send before approval parks on the same request, nothing queues.
        again = _submit(hub, item["item_id"], body="rm the cache", key="gate-key-01", approve=False)
        assert again.json()["approval_id"] == first.json()["approval_id"]
        assert _rows(item["item_id"]) == []
        _approve(first.json()["approval_id"])
        queued = _submit(
            hub, item["item_id"], body="rm the cache", key="gate-key-01", approve=False
        )
        assert queued.status_code == 201, queued.json()
        assert queued.json()["status"] == "queued"
        # The approval was one-shot, but a retry of the queued key needs none.
        retry = _submit(hub, item["item_id"], body="rm the cache", key="gate-key-01", approve=False)
        assert retry.status_code == 200
        assert retry.json()["duplicate"] is True
        assert len(_rows(item["item_id"])) == 1

    def test_denied_followup_never_queues(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        first = _submit(hub, item["item_id"], mode="steer", key="gate-key-02", approve=False)
        _approve(first.json()["approval_id"], "deny")
        resp = _submit(hub, item["item_id"], mode="steer", key="gate-key-02", approve=False)
        assert resp.status_code == 403
        assert _rows(item["item_id"]) == []

    def test_refusals_run_before_the_gate(self, hub, tmp_path):
        agent = _agent(hub, tmp_path, features=False)
        item = _claimed_item(agent)
        before = len(_pending_approvals())
        assert _submit(hub, item["item_id"], approve=False).status_code == 409
        assert len(_pending_approvals()) == before


class TestAttribution:
    """Only the session's own identities, or the human operator, may follow up."""

    def test_another_identity_is_refused_before_any_approval(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        before = len(_pending_approvals())
        resp = _submit(hub, item["item_id"], mode="steer", token=HERMES_TOKEN, approve=False)
        assert resp.status_code == 403, resp.json()
        assert "only those identities" in resp.json()["error"]
        assert len(_pending_approvals()) == before
        assert _rows(item["item_id"]) == []

    def test_the_starter_may_even_when_the_session_runs_as_another_agent(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent, created_by="Desktop_Agent2")
        assert _submit(hub, item["item_id"], token=CODEX_TOKEN).status_code == 201
        # The session's own agent may too.
        assert _submit(hub, item["item_id"], token=TOKEN).status_code == 201
        assert len(_rows(item["item_id"])) == 2

    def test_the_operator_may(self, hub, tmp_path):
        # Not the item's identity, and no approval: the operator path (#1872).
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        resp = _submit(hub, item["item_id"], mode="steer", token=OPERATOR_TOKEN, approve=False)
        assert resp.status_code == 201, resp.json()
        assert resp.json()["status"] == "queued"

    def test_an_agent_on_the_operator_profile_is_not_the_operator(self, hub, tmp_path, monkeypatch):
        from unified_agent import profiles

        real = profiles.profiles_by_identity
        monkeypatch.setattr(
            profiles,
            "profiles_by_identity",
            lambda: {**real(), "Desktop_Agent5": profiles.OPERATOR},
        )
        assert profiles.default_for_identity("Desktop_Agent5") == profiles.OPERATOR
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        resp = _submit(hub, item["item_id"], token=HERMES_TOKEN, approve=False)
        # The operator profile passes the route ceiling; attribution still refuses.
        assert resp.status_code == 403, resp.json()
        assert "only those identities" in resp.json()["error"]

    def test_a_task_claimed_by_another_label_is_refused(self, hub, tmp_path):
        from unified_agent.tools import tasks

        task_id = tasks.task_add("follow-up attribution fixture")["id"]
        conn = storage.connect()
        try:
            conn.execute("UPDATE tasks SET claimed_by='Desktop_Agent2' WHERE id=?", (task_id,))
            conn.commit()
        finally:
            conn.close()
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent, task_id=task_id)
        resp = _submit(hub, item["item_id"], approve=False)
        assert resp.status_code == 403, resp.json()
        assert "claimed by Desktop_Agent2" in resp.json()["error"]


class _ScopedIdentity:
    """Stands in for an auth layer that names an identity on a given channel.

    Used to reach the route with the operator label on a channel the hub
    middleware never grants human recovery to; the real middleware only ever
    sets ``bearer``, ``oauth``, ``cookie`` or ``workos``.
    """

    def __init__(self, app, identity: str, via: str | None) -> None:
        self.app = app
        self.identity = identity
        self.via = via

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["ua_identity"] = self.identity
            scope["ua_identity_via"] = self.via
        await self.app(scope, receive, send)


def _queued_events_for(item_id: str) -> list[dict]:
    return [e for e in _node_events("followup_queued") if e.get("item_id") == item_id]


class TestOperatorPath:
    """Operator-authenticated follow-ups queue at once; any other identity is gated (#1872).

    The operator is ``routes._operator_recovery``: ``UA_OPERATOR_LABEL`` as the
    identity the hub middleware verified, on a bearer, OAuth, cookie or WorkOS
    channel. Identity-bound, never profile-bound, never read from the request.
    """

    def test_operator_followup_is_queued_without_an_approval(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        before = len(_pending_approvals())
        resp = _submit(
            hub,
            item["item_id"],
            mode="steer",
            key="op-key-0001",
            token=OPERATOR_TOKEN,
            approve=False,
        )
        assert resp.status_code == 201, resp.json()
        out = resp.json()
        assert out["status"] == "queued"
        assert out["operator_authored"] is True
        assert out["approval"] == node_followups.APPROVAL_NOT_REQUIRED_OPERATOR
        assert out["created_by"] == config.operator_label()
        assert len(_pending_approvals()) == before
        [row] = _rows(item["item_id"])
        assert row["operator_authored"] is True
        assert row["approval"] == "not_required_operator"
        [event] = _queued_events_for(item["item_id"])
        assert event["operator_authored"] is True
        assert event["approval"] == "not_required_operator"
        # A retry is the same row, still recorded as operator-authored.
        again = _submit(
            hub,
            item["item_id"],
            mode="steer",
            key="op-key-0001",
            token=OPERATOR_TOKEN,
            approve=False,
        )
        assert again.status_code == 200
        assert again.json()["duplicate"] is True
        assert again.json()["operator_authored"] is True
        assert len(_queued_events_for(item["item_id"])) == 1

    def test_agent_followup_is_gated_and_recorded_as_such(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        first = _submit(hub, item["item_id"], key="agent-key-01", approve=False)
        assert first.status_code == 202, first.json()
        assert first.json()["status"] == "approval_pending"
        assert _rows(item["item_id"]) == []
        resp = _submit(hub, item["item_id"], key="agent-key-01")
        assert resp.status_code == 201, resp.json()
        assert resp.json()["operator_authored"] is False
        assert resp.json()["approval"] == node_followups.APPROVAL_GATE_PASSED
        [event] = _queued_events_for(item["item_id"])
        assert event["operator_authored"] is False
        assert event["approval"] == "gate_passed"

    def test_an_agent_on_the_operator_profile_is_still_gated(self, hub, tmp_path, monkeypatch):
        # The item's own agent, attribution passes, and its profile is the
        # operator's: it is still not the operator identity, so it is gated.
        from unified_agent import profiles

        real = profiles.profiles_by_identity
        monkeypatch.setattr(
            profiles,
            "profiles_by_identity",
            lambda: {**real(), TOKEN_AGENT: profiles.OPERATOR},
        )
        assert profiles.default_for_identity(TOKEN_AGENT) == profiles.OPERATOR
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        resp = _submit(hub, item["item_id"], approve=False)
        assert resp.status_code == 202, resp.json()
        assert resp.json()["status"] == "approval_pending"
        assert _rows(item["item_id"]) == []

    @pytest.mark.parametrize("via", [None, "", "node", "device", "Bearer", "mcp"])
    def test_operator_label_off_a_human_channel_is_gated(self, hub, tmp_path, via):
        operator = config.operator_label()
        agent = _agent(hub, tmp_path)
        # Started by the operator label, so attribution passes and the gate is
        # what answers.
        item = _claimed_item(agent, created_by=operator)
        app = _ScopedIdentity(Starlette(routes=webui.web_routes()), operator, via)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            resp = client.post(
                "/api/action/node-followup",
                json={
                    "item_id": item["item_id"],
                    "mode": "queue",
                    "body": "from a non-human channel",
                    "idempotency_key": "via-key-0001",
                },
            )
        assert resp.status_code == 202, resp.json()
        assert resp.json()["status"] == "approval_pending"
        assert _rows(item["item_id"]) == []

    @pytest.mark.parametrize("field", ["operator_authored", "operator", "approval", "actor"])
    def test_body_cannot_claim_the_operator_path(self, hub, tmp_path, field):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        resp = hub.post(
            "/api/action/node-followup",
            json={
                "item_id": item["item_id"],
                "mode": "queue",
                "body": "spoofed",
                "idempotency_key": "spoof-key-001",
                field: True if field != "actor" else config.operator_label(),
            },
            headers=_auth(),
        )
        assert resp.status_code == 400, resp.json()
        assert "unknown field" in resp.json()["error"]
        assert _rows(item["item_id"]) == []

    def test_identity_headers_cannot_claim_the_operator_path(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        operator = config.operator_label()
        spoof = {
            "X-UA-Identity": operator,
            "X-UA-Agent": operator,
            "X-Agent-Label": operator,
            "X-Forwarded-User": operator,
            "X-Remote-User": operator,
        }
        resp = hub.post(
            "/api/action/node-followup",
            json={
                "item_id": item["item_id"],
                "mode": "queue",
                "body": "spoofed",
                "idempotency_key": "spoof-key-002",
            },
            headers={**_auth(), **spoof},
        )
        assert resp.status_code == 202, resp.json()
        assert resp.json()["status"] == "approval_pending"
        assert _rows(item["item_id"]) == []

    def test_an_operator_label_naming_an_agent_is_never_the_operator(
        self, hub, tmp_path, monkeypatch
    ):
        # A misconfigured UA_OPERATOR_LABEL that names a catalogued agent is
        # ignored (config.operator_label_problem), so that agent stays gated.
        monkeypatch.setenv("UA_OPERATOR_LABEL", TOKEN_AGENT)
        assert config.operator_label() != TOKEN_AGENT
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        resp = _submit(hub, item["item_id"], approve=False)
        assert resp.status_code == 202, resp.json()
        assert resp.json()["status"] == "approval_pending"

    def test_submit_refuses_operator_for_any_other_actor(self, hub, tmp_path):
        # Defence in depth below the route: operator=True must name the operator.
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        before = len(_pending_approvals())
        with pytest.raises(node_queue.NodeError) as exc:
            node_followups.submit(
                item["item_id"],
                mode="queue",
                body="not the operator",
                idempotency_key="direct-key-01",
                actor=TOKEN_AGENT,
                operator=True,
            )
        assert exc.value.status == 403
        assert _rows(item["item_id"]) == []
        assert len(_pending_approvals()) == before

    def test_a_revoked_operator_cannot_follow_up(self, hub, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_REVOKED_AGENTS", config.operator_label())
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        resp = _submit(hub, item["item_id"], token=OPERATOR_TOKEN, approve=False)
        assert resp.status_code == 403, resp.json()
        assert _rows(item["item_id"]) == []

    def test_an_operator_not_scoped_for_dispatch_is_refused(self, hub, tmp_path, monkeypatch):
        # The operator path skips only the approval: UA_AGENT_SCOPES still binds.
        monkeypatch.setenv("UA_AGENT_SCOPES", f"{TOKEN_AGENT}:dispatch")
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        before = len(_pending_approvals())
        resp = _submit(hub, item["item_id"], token=OPERATOR_TOKEN, approve=False)
        assert resp.status_code == 403, resp.json()
        assert "not scoped for prod action 'dispatch'" in resp.json()["error"]
        assert _rows(item["item_id"]) == []
        assert _queued_events_for(item["item_id"]) == []
        assert len(_pending_approvals()) == before
        # Scoped, the operator queues at once again.
        monkeypatch.setenv(
            "UA_AGENT_SCOPES", f"{TOKEN_AGENT}:dispatch,{config.operator_label()}:dispatch"
        )
        resp = _submit(hub, item["item_id"], token=OPERATOR_TOKEN, approve=False)
        assert resp.status_code == 201, resp.json()
        assert resp.json()["operator_authored"] is True

    def test_a_key_pending_on_an_agent_approval_is_not_the_operators(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        first = _submit(hub, item["item_id"], key="shared-key-01", approve=False)
        assert first.json()["status"] == "approval_pending"
        taken = _submit(hub, item["item_id"], key="shared-key-01", token=OPERATOR_TOKEN)
        assert taken.status_code == 409, taken.json()
        assert "another identity's approval" in taken.json()["error"]
        assert _rows(item["item_id"]) == []
        # Still refused once approved and not yet used; the agent's retry queues.
        _approve(first.json()["approval_id"])
        taken = _submit(hub, item["item_id"], key="shared-key-01", token=OPERATOR_TOKEN)
        assert taken.status_code == 409, taken.json()
        mine = _submit(hub, item["item_id"], key="shared-key-01", approve=False)
        assert mine.status_code == 201, mine.json()
        assert mine.json()["created_by"] == TOKEN_AGENT

    def test_an_agents_queued_key_is_refused_to_the_operator(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        assert _submit(hub, item["item_id"], key="shared-key-02").status_code == 201
        resp = _submit(
            hub, item["item_id"], key="shared-key-02", token=OPERATOR_TOKEN, approve=False
        )
        assert resp.status_code == 409, resp.json()
        assert "already used by another identity" in resp.json()["error"]
        [row] = _rows(item["item_id"])
        assert row["created_by"] == TOKEN_AGENT

    def test_the_operators_key_is_refused_to_an_agent(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        op = _submit(hub, item["item_id"], key="shared-key-03", token=OPERATOR_TOKEN)
        assert op.status_code == 201, op.json()
        before = len(_pending_approvals())
        resp = _submit(hub, item["item_id"], key="shared-key-03", approve=False)
        assert resp.status_code == 409, resp.json()
        assert "already used by another identity" in resp.json()["error"]
        # Refused before the gate: no approval request is created for it.
        assert len(_pending_approvals()) == before
        [row] = _rows(item["item_id"])
        assert row["operator_authored"] is True

    @pytest.mark.parametrize("mode", ["queue", "steer"])
    @pytest.mark.parametrize("restriction", ["revoked", "descoped"])
    def test_a_restricted_operator_retrying_a_queued_key_is_refused(
        self, hub, tmp_path, monkeypatch, mode, restriction
    ):
        # Authority (revocation, then scope) runs before admission, so a
        # duplicate is never read back by an operator who lost it.
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        key = f"restrict-{mode}-01"
        first = _submit(hub, item["item_id"], mode=mode, key=key, token=OPERATOR_TOKEN)
        assert first.status_code == 201, first.json()
        if restriction == "revoked":
            monkeypatch.setenv("UA_REVOKED_AGENTS", config.operator_label())
        else:
            monkeypatch.setenv("UA_AGENT_SCOPES", f"{TOKEN_AGENT}:dispatch")
        before = len(_pending_approvals())
        retry = _submit(
            hub, item["item_id"], mode=mode, key=key, token=OPERATOR_TOKEN, approve=False
        )
        assert retry.status_code == 403, retry.json()
        assert len(_rows(item["item_id"])) == 1
        assert len(_queued_events_for(item["item_id"])) == 1
        assert len(_pending_approvals()) == before

    @pytest.mark.parametrize("restriction", ["revoked", "descoped"])
    def test_a_restricted_agent_retrying_a_queued_key_is_refused(
        self, hub, tmp_path, monkeypatch, restriction
    ):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        assert _submit(hub, item["item_id"], key="restrict-agent-1").status_code == 201
        if restriction == "revoked":
            monkeypatch.setenv("UA_REVOKED_AGENTS", TOKEN_AGENT)
        else:
            monkeypatch.setenv("UA_AGENT_SCOPES", f"{config.operator_label()}:dispatch")
        before = len(_pending_approvals())
        retry = _submit(hub, item["item_id"], key="restrict-agent-1", approve=False)
        assert retry.status_code == 403, retry.json()
        assert len(_rows(item["item_id"])) == 1
        assert len(_queued_events_for(item["item_id"])) == 1
        assert len(_pending_approvals()) == before

    @pytest.mark.parametrize("restriction", ["revoked", "descoped"])
    def test_restricted_direct_operator_submit_is_refused(
        self, hub, tmp_path, monkeypatch, restriction
    ):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        operator = config.operator_label()
        kwargs = dict(
            mode="queue",
            body="direct",
            idempotency_key="direct-op-key1",
            actor=operator,
            operator=True,
        )
        assert node_followups.submit(item["item_id"], **kwargs)["duplicate"] is False
        if restriction == "revoked":
            monkeypatch.setenv("UA_REVOKED_AGENTS", operator)
        else:
            monkeypatch.setenv("UA_AGENT_SCOPES", f"{TOKEN_AGENT}:dispatch")
        with pytest.raises(ToolError):
            node_followups.submit(item["item_id"], **kwargs)
        with pytest.raises(ToolError):
            node_followups.submit(
                item["item_id"], **{**kwargs, "idempotency_key": "direct-op-key2"}
            )
        assert len(_rows(item["item_id"])) == 1


def test_schema_adds_the_admission_columns_to_an_older_table():
    """A table from before #1872 gains the columns in place; old rows read as gated."""
    import sqlite3

    from unified_agent import table_schemas

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE node_followups (id TEXT PRIMARY KEY, item_id TEXT NOT NULL, "
        "seq INTEGER NOT NULL, mode TEXT NOT NULL, body TEXT NOT NULL, "
        "idempotency_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', "
        "created_by TEXT NOT NULL, created_at TEXT NOT NULL, claimed_at TEXT, "
        "delivered_at TEXT, turn_id TEXT, path TEXT, reason TEXT, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO node_followups (id, item_id, seq, mode, body, idempotency_key, "
        "created_by, created_at, updated_at) VALUES "
        "('nf-0000000000000001', 'nd-0000000000000001', 1, 'queue', 'b', 'old-key-01', "
        "'agent-a', 't', 't')"
    )
    table_schemas.node_followups(conn)
    table_schemas.node_followups(conn)  # idempotent
    row = conn.execute("SELECT operator_authored, approval FROM node_followups").fetchone()
    assert (row["operator_authored"], row["approval"]) == (0, "gate_passed")


class TestNodeClaimAndDelivery:
    def _claim(self, hub, item_id, token=TOKEN, ack=None):
        body = {"node": NODE, "item_id": item_id}
        if ack is not None:
            body["ack"] = ack
        return hub.post("/api/node/followups", json=body, headers=_auth(token))

    def test_claim_returns_seq_order_until_acknowledged(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        ids = [
            _submit(hub, item["item_id"], body=f"step {n}").json()["followup_id"] for n in (1, 2, 3)
        ]
        first = self._claim(hub, item["item_id"])
        assert first.status_code == 200, first.json()
        assert [f["id"] for f in first.json()["followups"]] == ids
        assert [f["body"] for f in first.json()["followups"]] == ["step 1", "step 2", "step 3"]
        # The reply was lost: the next poll (nothing acknowledged) hands the
        # same rows out again instead of losing them.
        again = self._claim(hub, item["item_id"])
        assert [f["id"] for f in again.json()["followups"]] == ids
        assert {r["status"] for r in _rows(item["item_id"])} == {"queued"}
        # Acknowledging two of them moves exactly those to claimed.
        acked = self._claim(hub, item["item_id"], ack=ids[:2])
        assert [f["id"] for f in acked.json()["followups"]] == ids[2:]
        assert [r["status"] for r in _rows(item["item_id"])] == ["claimed", "claimed", "queued"]

    def test_ack_is_validated_and_bound_to_the_item(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        other = _claimed_item(agent)
        theirs = _submit(hub, other["item_id"]).json()["followup_id"]
        assert self._claim(hub, item["item_id"], ack=["not-an-id"]).status_code == 400
        assert self._claim(hub, item["item_id"], ack="nf-0000000000000000").status_code == 400
        assert self._claim(hub, item["item_id"], ack=[theirs]).status_code == 200
        assert _rows(other["item_id"])[0]["status"] == "queued"

    def test_node_fetcher_acknowledges_on_the_next_poll(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        fid = _submit(hub, item["item_id"]).json()["followup_id"]
        calls: list[dict] = []
        real = agent.transport

        def spy(path, body, timeout):
            if path == "/api/node/followups":
                calls.append(dict(body))
            return real(path, body, timeout)

        agent.transport = spy
        fetch = agent._followup_fetcher(item["item_id"])
        assert [f["id"] for f in fetch()] == [fid]
        assert fetch() == []
        assert [c["ack"] for c in calls] == [[], [fid]]
        assert _rows(item["item_id"])[0]["status"] == "claimed"

    def test_claim_needs_a_claimed_item(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        root = agent.roots[0]
        queued = node_queue.enqueue(
            NODE,
            created_by="Desktop_Agent1",
            prompt="p",
            cwd=str(root / "repo"),
            backend="acp:gemini",
        )
        assert self._claim(hub, queued["item_id"]).status_code == 409
        assert self._claim(hub, "nd-0000000000000000").status_code == 404

    def test_events_settle_rows_and_receipt_drops_the_rest(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        a = _submit(hub, item["item_id"], body="a").json()["followup_id"]
        b = _submit(hub, item["item_id"], body="b").json()["followup_id"]
        self._claim(hub, item["item_id"])
        c = _submit(hub, item["item_id"], body="c").json()["followup_id"]
        node_queue.record_node_event(
            NODE,
            item["item_id"],
            [
                {
                    "kind": "followup_delivered",
                    "turn_id": "turn-2",
                    "data": {"followup_id": a, "path": "prompt", "turn_id": "turn-2"},
                },
                {"kind": "followup_dropped", "data": {"followup_id": b, "reason": "cancelled"}},
                # Another item's id cannot be settled through this item.
                {"kind": "followup_delivered", "data": {"followup_id": "nf-ffffffffffffffff"}},
            ],
            identity="Desktop_Agent1",
        )
        agent.post_receipt(
            item["item_id"],
            {
                "status": "finished",
                "attempt": item["attempt"],
                "started_at": "t",
                "finished_at": "t",
            },
        )
        rows = {r["followup_id"]: r for r in _rows(item["item_id"])}
        assert rows[a]["status"] == "delivered"
        assert rows[a]["turn_id"] == "turn-2"
        assert rows[a]["path"] == "prompt"
        assert rows[b]["status"] == "dropped"
        assert rows[b]["reason"] == "cancelled"
        assert rows[c]["status"] == "dropped"
        assert rows[c]["reason"] == "session_ended_before_delivery"
        dropped = [e["followup_id"] for e in _node_events("followup_dropped")]
        assert dropped == [c]


class TestSessionMirror:
    def _open(self, item):
        node_queue.record_node_event(
            NODE,
            item["item_id"],
            [{"kind": "session_open", "session_id": ACP_SESSION, "at": "t", "data": {}}],
            identity="Desktop_Agent1",
        )

    def _status(self) -> str:
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT status FROM agent_sessions WHERE external_session_id=? AND kind='acp'",
                (ACP_SESSION,),
            ).fetchone()
        finally:
            conn.close()
        return row["status"]

    def test_turn_end_with_turn_id_keeps_session_open_until_session_end(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        self._open(item)
        node_queue.record_node_event(
            NODE,
            item["item_id"],
            [
                {
                    "kind": "turn_end",
                    "turn_id": "turn-1",
                    "session_id": ACP_SESSION,
                    "data": {"turn_id": "turn-1", "stop_reason": "end_turn"},
                }
            ],
            identity="Desktop_Agent1",
        )
        assert self._status() == "running"
        node_queue.record_node_event(
            NODE,
            item["item_id"],
            [
                {
                    "kind": "session_end",
                    "session_id": ACP_SESSION,
                    "data": {"stop_reason": "end_turn", "turns": 2},
                }
            ],
            identity="Desktop_Agent1",
        )
        assert self._status() == "completed"
        turn_ids = [e.get("turn_id") for e in _node_events("acp_event")]
        assert turn_ids == [None, "turn-1", None]


class TestEndToEnd:
    def test_node_delivers_a_queued_followup_in_the_same_session(self, hub, tmp_path, fake_agent):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        followup = _submit(hub, item["item_id"], body="and then this").json()["followup_id"]
        receipt = agent.execute(item)
        assert receipt["status"] == "finished", receipt
        prompts = [v for k, v in _trace(fake_agent) if k == "prompt"]
        assert prompts == ["initial", "and then this"]
        (row,) = _rows(item["item_id"])
        assert row["followup_id"] == followup
        assert row["status"] == "delivered"
        assert row["turn_id"] == "turn-2"
        kinds = [e["acp_kind"] for e in _node_events("acp_event")]
        assert kinds.count("turn_start") == 2
        assert kinds.count("turn_end") == 2
        assert kinds.count("session_end") == 1


class TestEndedWithoutReceipt:
    def test_cancel_before_start_closes_queued_followups(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        root = agent.roots[0]
        item = node_queue.enqueue(
            NODE,
            created_by="Desktop_Agent1",
            prompt="p",
            cwd=str(root / "repo"),
            backend="acp:gemini",
        )
        assert _submit(hub, item["item_id"]).status_code == 201
        node_queue.cancel(item["item_id"], actor="Desktop_Agent1")
        assert node_queue.claim_next(NODE, identity="Desktop_Agent1") is None
        (row,) = _rows(item["item_id"])
        assert row["status"] == "dropped"
        assert row["reason"] == "item_cancelled"
        (event,) = _node_events("followup_dropped")
        assert event["followup_id"] == row["followup_id"]
        assert event["reason"] == "item_cancelled"

    def _queued_with_followup(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        root = agent.roots[0]
        item = node_queue.enqueue(
            NODE,
            created_by="Desktop_Agent1",
            prompt="p",
            cwd=str(root / "repo"),
            backend="acp:gemini",
        )
        fid = _submit(hub, item["item_id"]).json()["followup_id"]
        return agent, item, fid

    def test_governed_cancel_of_a_queued_item_drops_at_once(self, hub, tmp_path):
        _agent_, item, fid = self._queued_with_followup(hub, tmp_path)
        outcome = node_queue.cancel_for_run_evidence(f"node:{NODE}:{item['item_id']}")
        assert outcome == "already_gone"
        # No claim pass needed: the follow-up closed with the item.
        (row,) = _rows(item["item_id"])
        assert (row["status"], row["reason"]) == ("dropped", "item_cancelled")
        assert [e["followup_id"] for e in _node_events("followup_dropped")] == [fid]

    def test_lease_expiry_drops_with_an_event(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        fid = _submit(hub, item["item_id"]).json()["followup_id"]
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_dispatch SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (item["item_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        node_queue.claim_next(NODE, identity="Desktop_Agent1")
        (row,) = _rows(item["item_id"])
        assert (row["status"], row["reason"]) == ("dropped", "session_ended_before_delivery")
        assert [e["followup_id"] for e in _node_events("followup_dropped")] == [fid]

    def test_sweep_after_an_external_transaction_records_each_drop_once(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _claimed_item(agent)
        fid = _submit(hub, item["item_id"]).json()["followup_id"]
        # As the coordinator's settle_expired_run does, inside its own transaction.
        conn = storage.connect()
        try:
            conn.execute("UPDATE node_dispatch SET status='failed' WHERE id=?", (item["item_id"],))
            conn.commit()
        finally:
            conn.close()
        assert node_queue.sweep_ended_followups(actor="dispatch-coordinator") == [fid]
        assert node_queue.sweep_ended_followups(actor="dispatch-coordinator") == []
        assert [e["followup_id"] for e in _node_events("followup_dropped")] == [fid]
