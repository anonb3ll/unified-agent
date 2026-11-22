"""Resume and adopt harness sessions (task #1741, I11).

Backend tests drive ``AcpBackend`` against a fake ACP agent that advertises
``loadSession`` and/or ``sessionCapabilities.resume`` by env and traces every
method it receives. Discovery tests build fake Claude Code and Codex session
stores in tmp dirs (no real transcripts). Hub tests cover the resume and adopt
routes, the identity rule, idempotency and the persisted resume binding, and
one end-to-end run goes node -> hub -> fake agent -> receipt.
"""

from __future__ import annotations

import json
import os
import random
import secrets
import sys
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import (
    acp_backend,
    acp_client,
    acp_matrix,
    config,
    native_sessions,
    node_agent,
    node_followups,
    node_queue,
    node_registry,
    node_resume,
    server,
    storage,
    webui,
)
from unified_agent.executor import backends as exec_backends
from unified_agent.tools.approvals import approval_resolve

TOKEN = f"tok-{secrets.token_urlsafe(8)}"
OTHER_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
RESEARCH_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
OPERATOR_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
NODE_B = "resume-node-02"
NODE = "resume-node-01"
OLD_SESSION = "acp-old-session"
CLAUDE_SID = "0f0e0d0c-1111-4222-8333-444455556666"
CODEX_SID = "01a0df18-49fb-7571-9239-eae4c7c70a14"

FAKE_AGENT = r"""
import json
import os
import sys


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


caps = os.environ.get("FAKE_CAPS", "")
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        agent_caps = {}
        if caps in ("load", "both"):
            agent_caps["loadSession"] = True
        if caps in ("resume", "both"):
            agent_caps["sessionCapabilities"] = {"resume": {}}
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1,
              "agentCapabilities": agent_caps, "agentInfo": {"name": "fake", "version": "0"}}})
    elif method == "session/new":
        note("new", params.get("cwd"))
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "fresh-session", "modes": {"currentModeId": "default", "availableModes": [{"id": "default", "name": "Default"}]}}})
    elif method == "session/load":
        note("load", params.get("sessionId"))
        chunk(params["sessionId"], "old turn 1;")
        chunk(params["sessionId"], "old turn 2;")
        send({"jsonrpc": "2.0", "id": rid, "result": {"modes": {"currentModeId": "default", "availableModes": [{"id": "default", "name": "Default"}]}}})
    elif method == "session/resume":
        note("resume", params.get("sessionId"))
        send({"jsonrpc": "2.0", "id": rid, "result": {"modes": {"currentModeId": "default", "availableModes": [{"id": "default", "name": "Default"}]}}})
    elif method == "session/set_mode":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "session/prompt":
        text = params["prompt"][0]["text"]
        note("prompt", {"sid": params["sessionId"], "text": text})
        chunk(params["sessionId"], "re:" + text + ";")
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32601, "message": "unknown " + str(method)}})
"""


@pytest.fixture
def fake_agent(tmp_path, monkeypatch) -> Path:
    script = tmp_path / "fake_resume_agent.py"
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


def _run(tmp_path, *, resume=None, followup_fn=None, idle_s=0.0, prompt="next"):
    events: list[dict] = []
    result = acp_backend.AcpBackend("gemini").run(
        prompt=prompt,
        cwd=str(tmp_path),
        session_dir=str(tmp_path / "sess"),
        session_id="item-1",
        agent_label="Desktop_Agent1",
        timeout_s=30,
        on_event=events.append,
        followup_fn=followup_fn,
        followup_poll_s=0.0,
        resume_session_id=resume,
        idle_s=idle_s,
    )
    return result, events


def _of(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["kind"] == kind]


# --- ACP client: capability detection ------------------------------------------------


class TestResumeMode:
    def _mode(self, caps: dict) -> str | None:
        client = acp_client.AcpClient.__new__(acp_client.AcpClient)
        client.agent_capabilities = caps
        return client.resume_mode

    def test_modes(self):
        assert self._mode({}) is None
        assert self._mode({"loadSession": False}) is None
        assert self._mode({"loadSession": True}) == "load"
        assert self._mode({"sessionCapabilities": {"resume": {}}}) == "resume"
        # Resume wins: the hub already holds the transcript a load would replay.
        assert self._mode({"loadSession": True, "sessionCapabilities": {"resume": {}}}) == "resume"
        # Only the documented shapes count.
        assert self._mode({"loadSession": "yes", "sessionCapabilities": {"resume": True}}) is None


# --- backend: resume paths ----------------------------------------------------------


class TestAcpResume:
    def test_load_supported_reopens_without_restreaming_history(
        self, fake_agent, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("FAKE_CAPS", "load")
        result, events = _run(tmp_path, resume=OLD_SESSION)
        assert result["status"] == "success", result
        assert result["session_id"] == OLD_SESSION
        trace = _trace(fake_agent)
        assert ("load", OLD_SESSION) in trace
        assert not [k for k, _ in trace if k == "new"]
        assert ("prompt", {"sid": OLD_SESSION, "text": "next"}) in trace
        opened = _of(events, "session_open")[0]["data"]
        assert opened["resume_capability"] == "load"
        assert opened["resume_path"] == "session/load"
        assert opened["resumed_from"] == OLD_SESSION
        assert opened["replayed_updates"] == 2
        # The replayed history is counted, never forwarded as new events.
        texts = [e["text"] for e in _of(events, "agent_message_chunk")]
        assert texts == ["re:next;"]

    def test_resume_supported_uses_session_resume(self, fake_agent, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CAPS", "both")
        result, events = _run(tmp_path, resume=OLD_SESSION)
        assert result["status"] == "success", result
        kinds = [k for k, _ in _trace(fake_agent)]
        assert "resume" in kinds and "load" not in kinds and "new" not in kinds
        assert _of(events, "session_open")[0]["data"]["resume_path"] == "session/resume"

    def test_load_unsupported_is_refused_not_downgraded(self, fake_agent, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CAPS", "")
        result, events = _run(tmp_path, resume=OLD_SESSION)
        assert result["status"] == "error"
        assert result["error"].startswith("resume_unsupported")
        # No fresh session was opened in its place, and nothing was prompted.
        assert _trace(fake_agent) == []
        assert _of(events, "resume_refused")
        assert _of(events, "session_end")[0]["data"]["stop_reason"] == "resume_unsupported"

    def test_fresh_session_records_capability(self, fake_agent, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CAPS", "")
        _result, events = _run(tmp_path)
        data = _of(events, "session_open")[0]["data"]
        assert data["resume_capability"] == "none"
        assert data["resumed_from"] is None
        assert data["resume_path"] is None

    def test_malformed_resume_id_is_refused(self, fake_agent, tmp_path):
        result, _events = _run(tmp_path, resume="--dangerous")
        assert result["status"] == "error"
        assert "malformed" in result["error"]
        assert _trace(fake_agent) == []


class TestIdleReaper:
    def test_idle_session_is_reaped_and_stays_resumable(self, fake_agent, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_CAPS", "load")
        result, events = _run(tmp_path, followup_fn=lambda: [], idle_s=0.2)
        assert result["status"] == "success", result
        reaped = _of(events, "session_idle_reaped")[0]["data"]
        assert reaped == {"idle_s": 0.2, "resume_capability": "load", "resumable": True}
        assert _of(events, "session_end")[0]["data"]["end_reason"] == "idle_reaped"

    def test_followup_during_idle_runs_in_the_same_session(self, fake_agent, tmp_path):
        calls = {"n": 0}

        def followups():
            calls["n"] += 1
            if calls["n"] == 3:
                return [{"id": "nf-0000000000000001", "mode": "queue", "body": "later"}]
            return []

        result, events = _run(tmp_path, followup_fn=followups, idle_s=5.0)
        assert result["status"] == "success", result
        prompts = [v["text"] for k, v in _trace(fake_agent) if k == "prompt"]
        assert prompts == ["next", "later"]
        assert len(_of(events, "session_idle")) == 2
        assert _of(events, "session_idle_reaped") == [] or len(_of(events, "turn_end")) == 2


# --- native CLI resume flags ----------------------------------------------------------


class TestNativeResumeArgv:
    def test_claude_resume_flag(self):
        backend = exec_backends.ClaudeBackend(claude_bin="claude", skip_permissions=False)
        argv = backend.build_command("go on", resume_session_id=CLAUDE_SID)
        assert argv[:2] == ["claude", "-p"]
        assert argv[-4:] == ["--resume", CLAUDE_SID, "--", "go on"]

    def test_codex_resume_subcommand(self, tmp_path):
        backend = exec_backends.CodexBackend(codex_bin="codex", model="m", sandbox="read-only")
        argv = backend.build_command("go on", str(tmp_path), resume_session_id=CODEX_SID)
        assert argv[:2] == ["codex", "exec"]
        # exec options parse before the subcommand; the prompt follows the id.
        assert argv.index("--cd") < argv.index("resume")
        assert argv[-4:] == ["resume", CODEX_SID, "--", "go on"]

    def test_flag_shaped_ids_are_refused(self, tmp_path):
        with pytest.raises(ValueError):
            exec_backends.ClaudeBackend(claude_bin="claude").build_command(
                "x", resume_session_id="--settings=evil"
            )
        with pytest.raises(ValueError):
            exec_backends.CodexBackend(codex_bin="codex", sandbox="read-only").build_command(
                "x", str(tmp_path), resume_session_id="-x"
            )


# --- discovery ------------------------------------------------------------------------


def _claude_session(store: Path, project: str, sid: str, cwd: str, *, title=None) -> Path:
    folder = store / project
    folder.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "queue-operation", "sessionId": sid},
        {"type": "user", "sessionId": sid, "cwd": cwd, "message": {"content": "SECRET-BODY"}},
    ]
    if title:
        records.append({"type": "custom-title", "sessionId": sid, "customTitle": title})
    path = folder / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _codex_session(home: Path, sid: str, cwd: str) -> Path:
    day = home / "sessions" / "2026" / "10" / "01"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-10-01T10-00-00-{sid}.jsonl"
    meta = {"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "base_instructions": {}}}
    body = {"type": "response_item", "payload": {"content": "SECRET-BODY"}}
    path.write_text(json.dumps(meta) + "\n" + json.dumps(body) + "\n")
    return path


@pytest.fixture
def stores(tmp_path):
    root = tmp_path / "dev"
    repo = root / "repo"
    repo.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    claude = tmp_path / "claude-store"
    codex = tmp_path / "codex-store"
    return {
        "root": root,
        "repo": repo,
        "outside": outside,
        "claude": claude,
        "codex": codex,
    }


class TestDiscovery:
    def _discover(self, s):
        return native_sessions.discover([s["root"]], claude_dir=s["claude"], codex_dir=s["codex"])

    def test_lists_sessions_inside_roots_with_metadata_only(self, stores):
        _claude_session(stores["claude"], "-repo", CLAUDE_SID, str(stores["repo"]), title="Fix it")
        _codex_session(stores["codex"], CODEX_SID, str(stores["repo"]))
        (stores["codex"] / "session_index.jsonl").write_text(
            json.dumps({"id": CODEX_SID, "thread_name": "Codex thread", "updated_at": "t"}) + "\n"
        )
        found = {r["harness"]: r for r in self._discover(stores)}
        assert set(found) == {"claude", "codex"}
        assert found["claude"]["external_session_id"] == CLAUDE_SID
        assert found["claude"]["cwd"] == str(stores["repo"])
        assert found["claude"]["cwd_realpath"] == os.path.realpath(stores["repo"])
        assert found["claude"]["title"] == "Fix it"
        # Codex thread names may be derived from the conversation: not reported.
        assert found["codex"]["title"] is None
        for row in found.values():
            assert set(row) == {
                "harness",
                "external_session_id",
                "cwd",
                "cwd_realpath",
                "last_activity",
                "scanned_at",
                "title",
            }
        assert "SECRET-BODY" not in json.dumps(list(found.values()))

    def test_a_derived_summary_is_never_a_title(self, stores):
        """Review #1741: only a title the user set leaves the device."""
        path = _claude_session(stores["claude"], "-repo", CLAUDE_SID, str(stores["repo"]))
        with path.open("a") as fh:
            fh.write(json.dumps({"type": "summary", "summary": "SECRET-BODY summary"}) + "\n")
        (row,) = self._discover(stores)
        assert row["title"] is None

    def test_cwd_outside_roots_is_not_listed(self, stores):
        _claude_session(stores["claude"], "-out", CLAUDE_SID, str(stores["outside"]))
        _codex_session(stores["codex"], CODEX_SID, str(stores["outside"]))
        assert self._discover(stores) == []

    def test_symlink_escape_is_not_listed(self, stores):
        _claude_session(stores["claude"], "-esc", CLAUDE_SID, str(stores["root"] / "escape"))
        assert self._discover(stores) == []

    def test_dotdot_escape_is_not_listed(self, stores):
        sneaky = f"{stores['root']}/repo/../../elsewhere"
        _claude_session(stores["claude"], "-dots", CLAUDE_SID, sneaky)
        _codex_session(stores["codex"], CODEX_SID, sneaky)
        assert self._discover(stores) == []

    def test_sibling_prefix_is_not_inside(self, stores, tmp_path):
        sibling = tmp_path / "dev-other"
        sibling.mkdir()
        _claude_session(stores["claude"], "-sib", CLAUDE_SID, str(sibling))
        assert self._discover(stores) == []

    def test_session_file_symlinked_out_of_the_store_is_skipped(self, stores, tmp_path):
        real = _claude_session(tmp_path / "foreign", "-x", CLAUDE_SID, str(stores["repo"]))
        folder = stores["claude"] / "-linked"
        folder.mkdir(parents=True)
        (folder / f"{CLAUDE_SID}.jsonl").symlink_to(real)
        assert self._discover(stores) == []

    def test_mismatched_session_id_is_skipped(self, stores):
        path = _claude_session(stores["claude"], "-m", CLAUDE_SID, str(stores["repo"]))
        path.rename(path.with_name("ffffffff-0000-4000-8000-000000000000.jsonl"))
        assert self._discover(stores) == []

    def test_missing_stores_and_no_roots(self, stores):
        assert self._discover(stores) == []
        assert native_sessions.discover([], claude_dir=stores["claude"]) == []

    def test_limit(self, stores):
        for n in range(5):
            sid = f"0f0e0d0c-1111-4222-8333-44445555000{n}"
            _claude_session(stores["claude"], "-repo", sid, str(stores["repo"]))
        rows = native_sessions.discover(
            [stores["root"]], claude_dir=stores["claude"], codex_dir=stores["codex"], limit=3
        )
        assert len(rows) == 3


class TestHubRootCheck:
    def test_path_in_roots(self):
        roots = ["/w/dev"]
        assert node_registry.path_in_roots("/w/dev/repo", roots)
        assert node_registry.path_in_roots("/w/dev", roots)
        assert not node_registry.path_in_roots("/w/dev-other/repo", roots)
        assert not node_registry.path_in_roots("/w/dev/../etc", roots)
        assert not node_registry.path_in_roots("relative/path", roots)

    def test_bounded_native_sessions_drops_extra_fields_and_outside_rows(self):
        def row(sid, real, harness="claude"):
            return {
                "harness": harness,
                "external_session_id": sid,
                "cwd": real,
                "cwd_realpath": real,
            }

        rows = node_registry.bounded_native_sessions(
            [
                {
                    **row(CLAUDE_SID, "/w/dev/repo"),
                    "last_activity": "2026-10-01T00:00:00+00:00",
                    "title": "t",
                    "transcript": "SECRET-BODY",
                },
                row("x1", "/etc"),
                row("x2", "/w/dev/repo", harness="pi"),
                row("-flag", "/w/dev/repo", harness="codex"),
            ],
            ["/w/dev"],
        )
        assert [r["external_session_id"] for r in rows] == [CLAUDE_SID]
        assert "transcript" not in rows[0]

    def test_only_node_resolved_paths_count(self):
        """Review #1741: the hub cannot resolve symlinks, so it compares realpaths only."""
        facts = {
            "worktree_roots": [
                # A root reported without its resolved form admits nothing.
                {"path": "/old/dev", "exists": True},
                # The root /w/dev resolves to /data/dev on the node.
                {"path": "/w/dev", "realpath": "/data/dev", "exists": True},
            ]
        }
        roots = node_registry.root_paths(facts)
        assert roots == ["/data/dev"]
        rows = node_registry.bounded_native_sessions(
            [
                # Recorded under the root's path, but resolves elsewhere: refused.
                {
                    "harness": "claude",
                    "external_session_id": "a1",
                    "cwd": "/w/dev/link",
                    "cwd_realpath": "/etc/secret",
                },
                # No resolved form at all: refused.
                {"harness": "claude", "external_session_id": "a2", "cwd": "/data/dev/repo"},
                # Under the old node's unresolved root: refused.
                {
                    "harness": "claude",
                    "external_session_id": "a3",
                    "cwd": "/old/dev/repo",
                    "cwd_realpath": "/old/dev/repo",
                },
                {
                    "harness": "claude",
                    "external_session_id": "ok",
                    "cwd": "/w/dev/repo",
                    "cwd_realpath": "/data/dev/repo",
                },
            ],
            roots,
        )
        assert [(r["external_session_id"], r["cwd"]) for r in rows] == [("ok", "/data/dev/repo")]
        # A stored list re-bounds to itself when read back.
        assert node_registry.bounded_native_sessions(rows, roots) == rows


# --- capability rows ------------------------------------------------------------------


class TestResumeCapabilityRow:
    def _row(self, backend, mode=None, features=(acp_matrix.RESUME_FEATURE,), advertised=True):
        caps = acp_matrix.control_capabilities(
            backend,
            advertised=advertised,
            version=None,
            features=list(features),
            resume_mode=mode,
        )
        return caps["capabilities"]["resume"]

    def test_rows_are_truthful(self):
        assert self._row("acp:claude", "resume")["state"] == "supported"
        assert "session/load" in self._row("acp:claude", "load")["detail"]
        assert self._row("acp:gemini", "none")["state"] == "unsupported"
        assert "Start again" in self._row("acp:gemini", "none")["detail"]
        assert self._row("acp:gemini", None)["state"] == "unknown"
        assert self._row("acp:gemini", "load", features=())["state"] == "unavailable"
        assert self._row("claude")["state"] == "supported"
        assert "--resume" in self._row("claude")["detail"]
        assert self._row("agy")["state"] == "unsupported"
        assert self._row("acp:gemini", "load", advertised=False)["state"] == "unavailable"


# --- hub ------------------------------------------------------------------------------


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        f"Desktop_Agent1:{TOKEN},Desktop_Agent2:{OTHER_TOKEN},Desktop_Agent8:{RESEARCH_TOKEN},"
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


def _approved(send):
    """Send; when the node_dispatch gate parks it, approve as the operator and resend."""
    resp = send()
    if resp.status_code != 202 or resp.json().get("status") != "approval_pending":
        return resp
    token = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(resp.json()["approval_id"], "approve", resolved_by="Desktop_Person")
    finally:
        config.agent_identity_var.reset(token)
    return send()


def _transport(client):
    def call(path, body, timeout):
        resp = client.post(path, json=body, headers=_auth())
        return resp.status_code, resp.json()

    return call


def _facts(root: Path, *, resume: bool = True, native: list | None = None) -> dict:
    features = [node_followups.NODE_FEATURE, "session_settings"]
    if resume:
        features.append(acp_matrix.RESUME_FEATURE)
    return {
        "os": {"system": "Linux"},
        "harnesses": {"acp:gemini": {"path": "/usr/bin/gemini", "acp": True}},
        "backends": ["acp:gemini", "claude", "acp:claude"],
        "worktree_roots": [{"path": str(root), "realpath": os.path.realpath(root), "exists": True}],
        "node_features": features,
        "native_sessions": native or [],
    }


def _agent(hub, tmp_path, *, resume: bool = True, native: list | None = None, node: str = NODE):
    root = tmp_path / "dev"
    (root / "repo").mkdir(parents=True, exist_ok=True)
    agent = node_agent.NodeAgent(
        node=node,
        transport=_transport(hub),
        roots=[root],
        backends=("acp:gemini", "claude", "acp:claude"),
        facts_fn=lambda: _facts(root, resume=resume, native=native),
        sleep=lambda s: None,
        rng=random.Random(7),
        claim_wait_s=0,
        session_root=tmp_path / "sessions",
        spool_dir=tmp_path / "spool",
    )
    assert agent.run_once() == "registered"
    return agent


def _finished_acp_item(
    agent,
    *,
    capability: str | None = "load",
    backend: str = "acp:gemini",
    sid: str = OLD_SESSION,
    cwd_realpath: str | None = None,
) -> dict:
    """An ACP item that ran, opened ``sid``, and finished."""
    OLD = sid
    root = agent.roots[0]
    node_queue.enqueue(
        NODE,
        created_by="Desktop_Agent1",
        prompt="first",
        cwd=str(root / "repo"),
        backend=backend,
        cwd_realpath=cwd_realpath,
    )
    item = node_queue.claim_next(NODE, identity="Desktop_Agent1")
    data = {"acp_session_id": OLD}
    if capability is not None:
        data["resume_capability"] = capability
    node_queue.record_node_event(
        NODE,
        item["item_id"],
        [
            {"kind": "session_open", "session_id": OLD, "data": data},
            {
                "kind": "session_end",
                "session_id": OLD,
                "data": {"stop_reason": "end_turn", "end_reason": "idle_reaped", "turns": 1},
            },
        ],
        identity="Desktop_Agent1",
    )
    agent.post_receipt(
        item["item_id"],
        {"status": "finished", "attempt": item["attempt"], "session_id": OLD},
    )
    return item


def _session_rows(external_sid: str) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_sessions WHERE external_session_id=?", (external_sid,)
        ).fetchall()
    finally:
        conn.close()
    return [{**dict(r), "meta": json.loads(r["meta"])} for r in rows]


def _recent(item_id: str) -> dict:
    return next(i for i in node_queue.list_recent_items(limit=100) if i["id"] == item_id)


class TestResumeRoute:
    def _resume(self, hub, item_id, *, token=TOKEN, prompt="carry on"):
        return _approved(
            lambda: hub.post(
                "/api/action/node-resume",
                json={"item_id": item_id, "prompt": prompt},
                headers=_auth(token),
            )
        )

    def test_resume_sits_behind_the_approval_gate(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _finished_acp_item(agent)
        resp = hub.post(
            "/api/action/node-resume",
            json={"item_id": item["item_id"], "prompt": "carry on"},
            headers=_auth(),
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "approval_pending"

    def test_binding_persisted_and_resume_queues_with_session_id(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _finished_acp_item(agent, capability="load")
        (row,) = _session_rows(OLD_SESSION)
        assert row["status"] == "completed"
        assert row["exit_status"] == "idle_reaped"
        # The resume binding outlives the process.
        assert row["meta"]["resume_capability"] == "load"
        assert row["meta"]["backend"] == "acp:gemini"
        assert row["meta"]["node"] == NODE
        assert _recent(item["item_id"])["resume"]["mode"] == "acp_load"

        resp = self._resume(hub, item["item_id"])
        assert resp.status_code == 201, resp.json()
        body = resp.json()
        assert body["resume_session_id"] == OLD_SESSION
        assert body["mode"] == "acp_load"
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert claimed["item_id"] == body["item_id"]
        assert claimed["resume_session_id"] == OLD_SESSION
        assert claimed["prompt"] == "carry on"

    def test_resume_keeps_the_checkout_realpath_binding(self, hub, tmp_path):
        """A resumed bound item re-binds the planned realpath (#1770/#2011)."""
        agent = _agent(hub, tmp_path)
        root = agent.roots[0]
        facts = _facts(root)
        facts["node_features"].append(node_queue.CWD_REALPATH_FEATURE)
        agent._facts_fn = lambda: facts
        agent.register()
        planned = os.path.realpath(root / "repo")
        item = _finished_acp_item(agent, cwd_realpath=planned)
        resp = self._resume(hub, item["item_id"])
        assert resp.status_code == 201, resp.json()
        assert node_queue.get_item(resp.json()["item_id"])["cwd_realpath"] == planned
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert claimed["cwd_realpath"] == planned

    def test_resume_freezes_original_effective_model(self, hub, tmp_path, monkeypatch):
        agent = _agent(hub, tmp_path)
        item = _finished_acp_item(agent)
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT id, meta FROM agent_sessions WHERE external_session_id=?", (OLD_SESSION,)
            ).fetchone()
            meta = json.loads(row["meta"])
            meta["session_settings"] = {
                "current_model_id": "original-effective",
                "current_mode_id": "default",
                "permission_mode": "supervised",
            }
            conn.execute(
                "UPDATE agent_sessions SET meta=?, model=? WHERE id=?",
                (json.dumps(meta), "original-effective", row["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        from unified_agent import operating_profiles

        monkeypatch.setattr(
            operating_profiles,
            "settings_for_launch",
            lambda *a: pytest.fail("resume inherited changed profile"),
        )
        response = self._resume(hub, item["item_id"])
        assert response.status_code == 201, response.json()
        resumed = node_queue.get_item(response.json()["item_id"])
        assert resumed["model"] == "original-effective"
        assert resumed["permission_mode"] == "supervised"

    def test_load_unsupported_offers_start_again_only(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _finished_acp_item(agent, capability="none")
        option = _recent(item["item_id"])["resume"]
        assert option["available"] is False
        assert "Start again" in option["reason"]
        resp = self._resume(hub, item["item_id"])
        assert resp.status_code == 409
        assert "neither" in resp.json()["error"]

    def test_unrecorded_capability_is_not_resumable(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _finished_acp_item(agent, capability=None)
        assert self._resume(hub, item["item_id"]).status_code == 409

    def test_node_without_resume_feature_is_refused(self, hub, tmp_path):
        agent = _agent(hub, tmp_path, resume=False)
        item = _finished_acp_item(agent)
        resp = self._resume(hub, item["item_id"])
        assert resp.status_code == 409
        assert "upgrade" in resp.json()["error"]
        # And a direct enqueue cannot slip a resume past it either.
        with pytest.raises(node_queue.NodeError):
            node_queue.enqueue(
                NODE,
                created_by="Desktop_Agent1",
                prompt="p",
                cwd=str(agent.roots[0] / "repo"),
                backend="acp:gemini",
                resume_session_id=OLD_SESSION,
            )

    def test_live_item_and_second_resume_are_refused(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _finished_acp_item(agent)
        assert self._resume(hub, item["item_id"]).status_code == 201
        resp = self._resume(hub, item["item_id"])
        assert resp.status_code == 409
        assert "already queued" in resp.json()["error"]

    def test_profile_without_crew_view_is_refused(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _finished_acp_item(agent)
        assert self._resume(hub, item["item_id"], token=RESEARCH_TOKEN).status_code == 403

    def test_unknown_field_and_missing_csrf_shape(self, hub, tmp_path):
        resp = hub.post(
            "/api/action/node-resume",
            json={"item_id": "nd-0000000000000000", "prompt": "x", "extra": 1},
            headers=_auth(),
        )
        assert resp.status_code == 400

    def test_end_to_end_resume_reopens_the_durable_row(
        self, hub, tmp_path, fake_agent, monkeypatch
    ):
        monkeypatch.setenv("FAKE_CAPS", "load")
        agent = _agent(hub, tmp_path)
        item = _finished_acp_item(agent)
        new_id = self._resume(hub, item["item_id"]).json()["item_id"]
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        receipt = agent.execute(claimed)
        assert receipt["status"] == "finished", receipt
        assert receipt["session_id"] == OLD_SESSION
        assert ("load", OLD_SESSION) in _trace(fake_agent)
        (row,) = _session_rows(OLD_SESSION)
        assert row["meta"]["item_id"] == new_id
        assert row["meta"]["resumed_items"] == [new_id]
        assert row["status"] == "completed"


class TestNodeExecuteResume:
    def test_native_resume_passes_the_flag_and_keeps_the_session(self, tmp_path):
        seen: dict = {}

        class FakeClaude:
            def run(self, **kwargs):
                seen.update(kwargs)
                return {"status": "success", "text": "ok"}

        root = tmp_path / "dev"
        (root / "repo").mkdir(parents=True)
        agent = node_agent.NodeAgent(
            node=NODE,
            transport=lambda *a: (200, {}),
            roots=[root],
            backends=("claude", "agy"),
            backend_factory=lambda name, agent_label=None: FakeClaude(),
            facts_fn=lambda: {},
            session_root=tmp_path / "s",
            spool_dir=tmp_path / "sp",
        )
        item = {
            "item_id": "nd-0000000000000001",
            "backend": "claude",
            "cwd": str(root / "repo"),
            "prompt": "p",
            "resume_session_id": CLAUDE_SID,
        }
        receipt = agent.execute(item)
        assert receipt["status"] == "finished"
        assert seen["resume_session_id"] == CLAUDE_SID
        assert receipt["session_id"] == CLAUDE_SID
        refused = agent.execute({**item, "backend": "agy"})
        assert refused["reason"] == "resume_unsupported_backend:agy"
        bad = agent.execute({**item, "resume_session_id": "-x"})
        assert bad["reason"] == "resume_session_id_invalid"


def _native(root: Path, sid: str = CLAUDE_SID, harness: str = "claude") -> dict:
    return {
        "harness": harness,
        "external_session_id": sid,
        "cwd": str(root / "repo"),
        "cwd_realpath": os.path.realpath(root / "repo"),
        "last_activity": "2026-10-01T00:00:00+00:00",
        "title": "Started in a terminal",
    }


class TestAdopt:
    def _adopt(self, hub, *, token=TOKEN, sid=CLAUDE_SID, **extra):
        body = {
            "node": NODE,
            "harness": "claude",
            "external_session_id": sid,
            "prompt": "take over",
            **extra,
        }
        return _approved(
            lambda: hub.post("/api/action/node-adopt", json=body, headers=_auth(token))
        )

    def test_listing_is_read_only_metadata_inside_roots(self, hub, tmp_path):
        root = tmp_path / "dev"
        outside = {
            **_native(root, sid="outside-1"),
            "cwd_realpath": str(tmp_path / "elsewhere"),
        }
        _agent(hub, tmp_path, native=[_native(root), outside])
        resp = hub.get("/api/nodes/native-sessions", headers=_auth())
        assert resp.status_code == 200, resp.json()
        (row,) = resp.json()["sessions"]
        assert row["external_session_id"] == CLAUDE_SID
        assert row["adoptable"] is True
        # acp:claude is advertised but has not proved it can reopen a session.
        assert row["backends"] == ["claude"]
        assert "not recorded a resume capability" in row["backend_note"]
        assert row["adopted"] is None
        assert hub.get("/api/nodes/native-sessions", headers=_auth(RESEARCH_TOKEN)).status_code == (
            403
        )

    def test_heartbeat_refreshes_the_list(self, hub, tmp_path):
        root = tmp_path / "dev"
        agent = _agent(hub, tmp_path)
        agent._discover_fn = lambda: [_native(root)]
        agent._discovered_at = None
        agent.heartbeat()
        info = node_queue.list_nodes()[NODE]
        assert [r["external_session_id"] for r in info["native_sessions"]] == [CLAUDE_SID]
        # A heartbeat without a rescan keeps the last list.
        agent._discover_fn = lambda: pytest.fail("rescanned too early")
        agent.heartbeat()
        assert node_queue.list_nodes()[NODE]["native_sessions"]

    def test_adopt_is_idempotent_on_harness_and_session(self, hub, tmp_path):
        root = tmp_path / "dev"
        agent = _agent(hub, tmp_path, native=[_native(root)])
        first = self._adopt(hub)
        assert first.status_code == 201, first.json()
        body = first.json()
        assert body["adopted"] is True and body["backend"] == "claude"
        (row,) = _session_rows(CLAUDE_SID)
        assert row["kind"] == "claude_native"
        assert row["agent"] == "Desktop_Agent1"
        assert row["meta"]["cwd"] == os.path.realpath(root / "repo")
        # A repeated click while the item is open returns the same item.
        again = self._adopt(hub)
        assert again.status_code == 200
        assert again.json()["item_id"] == body["item_id"]
        assert again.json()["duplicate"] is True
        # After that run ends, adopting again resumes the same durable row.
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert claimed["resume_session_id"] == CLAUDE_SID
        assert claimed["backend"] == "claude"
        agent.post_receipt(
            claimed["item_id"],
            {"status": "finished", "attempt": claimed["attempt"], "session_id": CLAUDE_SID},
        )
        (row,) = _session_rows(CLAUDE_SID)
        assert row["status"] == "completed"
        # The finished adopt item itself is resumable through the native flag.
        assert _recent(claimed["item_id"])["resume"]["mode"] == "native_flag"
        later = self._adopt(hub)
        assert later.status_code == 201
        assert later.json()["adopted"] is False
        assert later.json()["session_id"] == row["id"]
        assert len(_session_rows(CLAUDE_SID)) == 1
        # While that resume is queued, a second one is not offered.
        assert "already queued" in _recent(claimed["item_id"])["resume"]["reason"]

    def test_another_identitys_session_is_refused(self, hub, tmp_path):
        root = tmp_path / "dev"
        _agent(hub, tmp_path, native=[_native(root)])
        assert self._adopt(hub).status_code == 201
        resp = self._adopt(hub, token=OTHER_TOKEN)
        assert resp.status_code == 403, resp.json()
        assert "attributed to Desktop_Agent1" in resp.json()["error"]
        listing = hub.get("/api/nodes/native-sessions", headers=_auth(OTHER_TOKEN)).json()
        assert listing["sessions"][0]["adoptable"] is False

    def test_undiscovered_session_and_bad_backend_are_refused(self, hub, tmp_path):
        root = tmp_path / "dev"
        _agent(hub, tmp_path, native=[_native(root)])
        assert self._adopt(hub, sid="ffffffff-0000-4000-8000-000000000000").status_code == 404
        assert self._adopt(hub, backend="acp:gemini").status_code == 409
        assert self._adopt(hub, harness="pi").status_code == 400
        assert self._adopt(hub, token=RESEARCH_TOKEN).status_code == 403

    def test_adopt_over_acp_needs_a_recorded_resume_capability(self, hub, tmp_path):
        """Review #1741: no ACP adopt without a resume/load probe on that node."""
        root = tmp_path / "dev"
        agent = _agent(hub, tmp_path, native=[_native(root)])
        resp = self._adopt(hub, backend="acp:claude")
        assert resp.status_code == 409, resp.json()
        assert "not recorded a resume capability" in resp.json()["error"]
        _finished_acp_item(agent, capability="none", backend="acp:claude", sid="probe-1")
        resp = self._adopt(hub, backend="acp:claude")
        assert resp.status_code == 409
        assert "neither session/resume nor session/load" in resp.json()["error"]

    def test_adopt_over_acp_uses_the_acp_backend(self, hub, tmp_path):
        root = tmp_path / "dev"
        agent = _agent(hub, tmp_path, native=[_native(root)])
        _finished_acp_item(agent, capability="load", backend="acp:claude", sid="probe-1")
        listing = hub.get("/api/nodes/native-sessions", headers=_auth()).json()["sessions"]
        assert listing[0]["backends"] == ["claude", "acp:claude"]
        resp = self._adopt(hub, backend="acp:claude")
        assert resp.status_code == 201, resp.json()
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert claimed["backend"] == "acp:claude"
        assert claimed["resume_session_id"] == CLAUDE_SID
        # The ACP session_open for this item reopens the adopted row, not a new one.
        node_queue.record_node_event(
            NODE,
            claimed["item_id"],
            [
                {
                    "kind": "session_open",
                    "session_id": CLAUDE_SID,
                    "data": {"resume_capability": "load"},
                }
            ],
            identity="Desktop_Agent1",
        )
        (row,) = _session_rows(CLAUDE_SID)
        assert row["kind"] == "claude_native"
        assert row["meta"]["resume_capability"] == "load"
        assert row["meta"]["item_id"] == claimed["item_id"]
        # Native adoption remains the durable row after ACP mode/model updates.
        # A newer foreign row with the same opaque SID cannot mask its binding.
        from unified_agent import registry

        foreign = registry.start_session(
            agent="Desktop_Agent1",
            kind="claude_native",
            external_session_id=CLAUDE_SID,
            model="foreign-model",
            meta={"node": "foreign-node", "item_id": "foreign-item"},
        )
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE agent_sessions SET started_at='2099-01-01T00:00:00Z' WHERE id=?", (foreign,)
            )
            conn.commit()
        finally:
            conn.close()
        node_queue.record_node_event(
            NODE,
            claimed["item_id"],
            [
                {
                    "kind": "session_settings_update",
                    "session_id": CLAUDE_SID,
                    "data": {
                        "settings": {"current_mode_id": "default", "current_model_id": "model-c"}
                    },
                }
            ],
            identity="Desktop_Agent1",
        )
        rows = _session_rows(CLAUDE_SID)
        bound = next(r for r in rows if r["meta"].get("item_id") == claimed["item_id"])
        assert bound["model"] == "model-c"
        assert bound["meta"]["session_settings"]["current_mode_id"] == "default"
        assert next(r for r in rows if r["id"] == foreign)["model"] == "foreign-model"
        # Remove the synthetic SID collision before separate lifecycle lookups;
        # settings-update fencing alone owns the collision exercised above.
        conn = storage.connect()
        try:
            conn.execute("DELETE FROM agent_sessions WHERE id=?", (foreign,))
            conn.commit()
        finally:
            conn.close()
        node_queue.record_node_event(
            NODE,
            claimed["item_id"],
            [
                {
                    "kind": "session_end",
                    "session_id": CLAUDE_SID,
                    "data": {"stop_reason": "end_turn"},
                }
            ],
            identity="Desktop_Agent1",
        )
        agent.post_receipt(
            claimed["item_id"],
            {"status": "finished", "attempt": claimed["attempt"], "session_id": CLAUDE_SID},
        )
        resp = _approved(
            lambda: hub.post(
                "/api/action/node-resume",
                json={"item_id": claimed["item_id"], "prompt": "continue"},
                headers=_auth(),
            )
        )
        assert resp.status_code == 201, resp.json()
        assert node_queue.get_item(resp.json()["item_id"])["model"] == "model-c"

    def test_a_node_cannot_reopen_a_row_it_was_not_asked_to(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        _finished_acp_item(agent)
        root = agent.roots[0]
        node_queue.enqueue(
            NODE,
            created_by="Desktop_Agent1",
            prompt="p",
            cwd=str(root / "repo"),
            backend="acp:gemini",
        )
        item = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        node_queue.record_node_event(
            NODE,
            item["item_id"],
            [{"kind": "session_open", "session_id": OLD_SESSION, "data": {}}],
            identity="Desktop_Agent1",
        )
        (row,) = _session_rows(OLD_SESSION)
        assert row["status"] == "completed"
        assert row["meta"]["item_id"] != item["item_id"]


def test_resume_module_exports():
    assert node_resume.ADOPTED_KINDS == {"claude": "claude_native", "codex": "codex_native"}


# --- review fixes (PR #1015) ------------------------------------------------------------


class _FakeProc:
    """Enough of Popen for AcpClient's request plumbing, with no process."""

    def __init__(self):
        self.stdin = None


class TestAcpWaiterRegistration:
    def _client(self, tmp_path):
        script = tmp_path / "exit_agent.py"
        script.write_text("import sys\nsys.stdin.readline()\n")
        return acp_client.AcpClient(
            [sys.executable, str(script)], cwd=str(tmp_path), session_dir=str(tmp_path)
        )

    def test_fast_reply_does_not_leave_a_registered_waiter(self, tmp_path):
        client = self._client(tmp_path)
        try:
            # The reply lands while the request is being written, as with a fast agent.
            client._send = lambda message: client._dispatch(
                {"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}}
            )
            req_id, waiter = client._start("session/prompt", {"sessionId": "s"})
            assert req_id not in client._pending
            assert waiter.get_nowait() == {"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}}
        finally:
            client.close()

    def test_exit_cleanup_never_blocks_on_a_full_waiter(self, tmp_path):
        client = self._client(tmp_path)
        full = __import__("queue").Queue(maxsize=1)
        full.put({"result": {}})
        client._pending[99] = full
        client._proc.stdin.write("\n")
        client._proc.stdin.flush()
        client._reader.join(timeout=5)
        assert not client._reader.is_alive()
        client.close()


class TestAtomicResume:
    def test_second_open_resume_of_one_session_is_refused_at_enqueue(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        root = agent.roots[0]
        fields = {
            "prompt": "p",
            "cwd": str(root / "repo"),
            "backend": "acp:gemini",
            "resume_session_id": OLD_SESSION,
        }
        node_queue.enqueue(NODE, created_by="Desktop_Agent1", **fields)
        with pytest.raises(node_queue.NodeError) as err:
            node_queue.enqueue(NODE, created_by="Desktop_Agent1", **fields)
        assert err.value.status == 409
        assert "already queued or running" in str(err.value)


class TestResumeAttribution:
    def _resume(self, hub, item_id, token):
        return _approved(
            lambda: hub.post(
                "/api/action/node-resume",
                json={"item_id": item_id, "prompt": "carry on"},
                headers=_auth(token),
            )
        )

    def test_another_identity_cannot_continue_a_hub_session(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _finished_acp_item(agent)
        resp = self._resume(hub, item["item_id"], OTHER_TOKEN)
        assert resp.status_code == 403, resp.json()
        assert "Desktop_Agent1" in resp.json()["error"]

    def test_the_operator_may(self, hub, tmp_path, monkeypatch):
        # The operator cannot approve their own dispatch (separation of
        # duties), so the gate is out of scope here: attribution is the test.
        monkeypatch.setattr("unified_agent.safety.check_prod_action_gate", lambda *a, **k: None)
        agent = _agent(hub, tmp_path)
        item = _finished_acp_item(agent)
        resp = self._resume(hub, item["item_id"], OPERATOR_TOKEN)
        assert resp.status_code == 201, resp.json()

    def test_denied_approval_is_a_403_not_a_500(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        item = _finished_acp_item(agent)

        def send():
            return hub.post(
                "/api/action/node-resume",
                json={"item_id": item["item_id"], "prompt": "carry on"},
                headers=_auth(),
            )

        pending = send()
        assert pending.status_code == 202
        token = config.agent_identity_var.set("Desktop_Person")
        try:
            approval_resolve(pending.json()["approval_id"], "deny", resolved_by="Desktop_Person")
        finally:
            config.agent_identity_var.reset(token)
        denied = send()
        assert denied.status_code == 403, denied.json()
        assert "dispatch refused" in denied.json()["error"]


class TestAdoptBoundaries:
    def _adopt(self, hub, node=NODE):
        body = {
            "node": node,
            "harness": "claude",
            "external_session_id": CLAUDE_SID,
            "prompt": "take over",
        }
        return _approved(lambda: hub.post("/api/action/node-adopt", json=body, headers=_auth()))

    def test_same_session_id_on_another_node_is_refused(self, hub, tmp_path):
        root = tmp_path / "dev"
        agent_a = _agent(hub, tmp_path, native=[_native(root)])
        _agent(hub, tmp_path, native=[_native(root)], node=NODE_B)
        assert self._adopt(hub).status_code == 201
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        agent_a.post_receipt(
            claimed["item_id"],
            {"status": "finished", "attempt": claimed["attempt"], "session_id": CLAUDE_SID},
        )
        resp = self._adopt(hub, node=NODE_B)
        assert resp.status_code == 409, resp.json()
        assert NODE in resp.json()["error"]
        (row,) = _session_rows(CLAUDE_SID)
        assert row["meta"]["node"] == NODE
        listing = hub.get("/api/nodes/native-sessions", headers=_auth()).json()["sessions"]
        on_b = next(r for r in listing if r["node"] == NODE_B)
        assert on_b["adoptable"] is False
        assert "bound to node" in on_b["reason"]

    def test_cancelled_adoption_does_not_stay_running(self, hub, tmp_path):
        root = tmp_path / "dev"
        _agent(hub, tmp_path, native=[_native(root)])
        resp = self._adopt(hub)
        assert resp.status_code == 201
        node_queue.cancel(resp.json()["item_id"], actor="Desktop_Agent1")
        assert node_queue.claim_next(NODE, identity="Desktop_Agent1") is None
        (row,) = _session_rows(CLAUDE_SID)
        assert row["status"] == "failed"
        assert row["exit_status"] == "item_cancelled"
        # The binding stays: the session can still be adopted and resumed.
        assert self._adopt(hub).status_code == 201


class TestDiscoveryClock:
    def test_first_heartbeat_scans_when_registration_carried_none(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        calls = []
        agent._discover_fn = lambda: calls.append(1) or []
        # _facts carries native_sessions, so registration started the clock.
        agent.heartbeat()
        assert calls == []
        bare = node_agent.NodeAgent(
            node=NODE,
            transport=_transport(hub),
            roots=agent.roots,
            backends=("acp:gemini",),
            facts_fn=lambda: {
                k: v for k, v in _facts(agent.roots[0]).items() if k != "native_sessions"
            },
            sleep=lambda s: None,
            rng=random.Random(7),
            claim_wait_s=0,
            session_root=tmp_path / "sessions2",
            spool_dir=tmp_path / "spool2",
        )
        bare._discover_fn = lambda: calls.append(2) or []
        assert bare.run_once() == "registered"
        bare.heartbeat()
        assert calls == [2]


# --- second review round (PR #1015) ---------------------------------------------------


class TestLiveSessionsAreNotTakenOver:
    def _adopt(self, hub, sid=CLAUDE_SID):
        body = {"node": NODE, "harness": "claude", "external_session_id": sid, "prompt": "go"}
        return _approved(lambda: hub.post("/api/action/node-adopt", json=body, headers=_auth()))

    def test_open_native_row_is_not_adopted(self, hub, tmp_path):
        root = tmp_path / "dev"
        _agent(hub, tmp_path, native=[_native(root)])
        from unified_agent import registry

        registry.register_session(
            "Desktop_Agent1",
            "claude_native",
            external_session_id=CLAUDE_SID,
            meta={"node": NODE},
        )
        resp = self._adopt(hub)
        assert resp.status_code == 409, resp.json()
        assert "still live" in resp.json()["error"]
        listing = hub.get("/api/nodes/native-sessions", headers=_auth()).json()["sessions"]
        assert listing[0]["adoptable"] is False

    def test_session_of_a_running_node_item_is_not_adopted(self, hub, tmp_path):
        """Review #1741: the item's session id comes from its real session_open event."""
        root = tmp_path / "dev"
        _agent(hub, tmp_path, native=[_native(root)])
        node_queue.enqueue(
            NODE,
            created_by="Desktop_Agent1",
            prompt="p",
            cwd=str(root / "repo"),
            backend="acp:claude",
        )
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        node_queue.record_node_event(
            NODE,
            claimed["item_id"],
            [
                {"kind": "session_open", "session_id": CLAUDE_SID, "data": {}},
                # The session reported its end, but the item has no receipt yet.
                {"kind": "session_end", "session_id": CLAUDE_SID, "data": {"turns": 1}},
            ],
            identity="Desktop_Agent1",
        )
        assert node_queue.get_item(claimed["item_id"])["session_id"] == CLAUDE_SID
        resp = self._adopt(hub)
        assert resp.status_code == 409, resp.json()
        assert claimed["item_id"] in resp.json()["error"]
        assert "still running this session" in resp.json()["error"]

    def test_a_hub_acp_session_is_not_adopted(self, hub, tmp_path):
        root = tmp_path / "dev"
        agent = _agent(hub, tmp_path, native=[_native(root)])
        _finished_acp_item(agent, backend="acp:claude", sid=CLAUDE_SID)
        resp = self._adopt(hub)
        assert resp.status_code == 409, resp.json()
        assert "use Resume on its item" in resp.json()["error"]
        (row,) = _session_rows(CLAUDE_SID)
        assert row["kind"] == "acp"
        assert "adopted" not in row["meta"]
        listing = hub.get("/api/nodes/native-sessions", headers=_auth()).json()["sessions"]
        assert "Started by the hub over ACP" in listing[0]["reason"]

    def test_recently_active_session_is_not_adoptable(self, hub, tmp_path, monkeypatch):
        from datetime import UTC, datetime

        monkeypatch.setenv("UA_NODE_ADOPT_QUIET_S", "120")
        root = tmp_path / "dev"
        fresh = {**_native(root), "last_activity": datetime.now(UTC).isoformat()}
        _agent(hub, tmp_path, native=[fresh])
        listing = hub.get("/api/nodes/native-sessions", headers=_auth()).json()["sessions"]
        assert listing[0]["adoptable"] is False
        assert "may still be open in a terminal" in listing[0]["reason"]
        resp = self._adopt(hub)
        assert resp.status_code == 409
        assert "quiet" in resp.json()["error"]

    def test_resume_refuses_a_session_whose_row_is_still_open(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        root = agent.roots[0]
        node_queue.enqueue(
            NODE,
            created_by="Desktop_Agent1",
            prompt="p",
            cwd=str(root / "repo"),
            backend="acp:gemini",
        )
        item = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        node_queue.record_node_event(
            NODE,
            item["item_id"],
            [
                {
                    "kind": "session_open",
                    "session_id": OLD_SESSION,
                    "data": {"resume_capability": "load"},
                }
            ],
            identity="Desktop_Agent1",
        )
        # The receipt lands but the session never reported its end.
        agent.post_receipt(
            item["item_id"],
            {"status": "finished", "attempt": item["attempt"], "session_id": OLD_SESSION},
        )
        resp = _approved(
            lambda: hub.post(
                "/api/action/node-resume",
                json={"item_id": item["item_id"], "prompt": "go"},
                headers=_auth(),
            )
        )
        assert resp.status_code == 409, resp.json()
        assert "still live" in resp.json()["error"]


class TestAdoptionRecordIsAtomic:
    def test_a_sweep_between_reopen_and_rebind_cannot_end_the_row(self, hub, tmp_path, monkeypatch):
        root = tmp_path / "dev"
        agent = _agent(hub, tmp_path, native=[_native(root)])
        body = {
            "node": NODE,
            "harness": "claude",
            "external_session_id": CLAUDE_SID,
            "prompt": "go",
        }

        def adopt():
            return _approved(lambda: hub.post("/api/action/node-adopt", json=body, headers=_auth()))

        assert adopt().status_code == 201
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        agent.post_receipt(
            claimed["item_id"],
            {"status": "finished", "attempt": claimed["attempt"], "session_id": CLAUDE_SID},
        )
        from unified_agent import registry

        real = registry.register_session

        def reopen_then_sweep(*args, **kwargs):
            out = real(*args, **kwargs)
            node_resume.close_ended_adoptions()
            return out

        monkeypatch.setattr(registry, "register_session", reopen_then_sweep)
        again = adopt()
        assert again.status_code == 201, again.json()
        node_resume.close_ended_adoptions()
        (row,) = _session_rows(CLAUDE_SID)
        assert row["status"] == "running"
        assert row["meta"]["item_id"] == again.json()["item_id"]


class TestRegistrationFitsTheFactsCap:
    def test_oversized_discovery_is_trimmed_before_register(self, hub, tmp_path):
        root = tmp_path / "dev"
        many = [
            {**_native(root, sid=f"s{n:04d}"), "title": "t" * 120, "cwd": "/" + "x" * 900}
            for n in range(200)
        ]
        agent = _agent(hub, tmp_path, native=many)
        assert agent.registered
        facts = node_agent.fit_facts({"native_sessions": many})
        assert 0 < len(facts["native_sessions"]) < len(many)
        assert facts["native_sessions"] == many[: len(facts["native_sessions"])]


class TestIdleHoldIsInteractiveOnly:
    def test_hub_marks_only_human_unscheduled_items(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        cwd = str(agent.roots[0] / "repo")
        node_queue.enqueue(
            NODE, created_by=config.operator_label(), prompt="p", cwd=cwd, backend="acp:gemini"
        )
        assert node_queue.claim_next(NODE, identity="Desktop_Agent1")["hold_for_followups"] is True
        node_queue.enqueue(
            NODE, created_by="Desktop_Agent1", prompt="p", cwd=cwd, backend="acp:gemini"
        )
        item = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert item["hold_for_followups"] is False

    def test_scheduler_runs_end_at_once(self, fake_agent, tmp_path):
        events: list[dict] = []
        result = acp_backend.AcpBackend("gemini").run(
            prompt="go",
            cwd=str(tmp_path),
            session_dir=str(tmp_path / "sess"),
            session_id="item-1",
            agent_label="Desktop_Agent1",
            timeout_s=30,
            on_event=events.append,
            followup_fn=lambda: [],
            followup_poll_s=0.0,
            idle_s=30.0,
            hold_idle=False,
        )
        assert result["status"] == "success"
        assert _of(events, "session_idle") == []

    def test_a_session_that_took_a_followup_still_holds(self, fake_agent, tmp_path):
        batches = [[{"id": "nf-0000000000000001", "mode": "queue", "body": "more"}]]
        events: list[dict] = []
        acp_backend.AcpBackend("gemini").run(
            prompt="go",
            cwd=str(tmp_path),
            session_dir=str(tmp_path / "sess"),
            session_id="item-1",
            agent_label="Desktop_Agent1",
            timeout_s=30,
            on_event=events.append,
            followup_fn=lambda: batches.pop(0) if batches else [],
            followup_poll_s=0.0,
            idle_s=0.2,
            hold_idle=False,
        )
        assert len(_of(events, "session_idle")) == 1
        assert _of(events, "session_idle_reaped")


# --- third review round (PR #1015) ----------------------------------------------------


def _adopt_item(root: Path, *, backend: str = "claude", source: str = "adopt") -> dict:
    return {
        "item_id": "nd-0000000000000002",
        "backend": backend,
        "cwd": str(root / "repo"),
        "prompt": "p",
        "resume_session_id": CLAUDE_SID,
        "resume_source": source,
    }


class TestAdoptPreflightOnTheNode:
    """Review #1741: the node re-checks the session file right before it spawns."""

    def _agent(self, tmp_path, spawned):
        class Fake:
            def run(self, **kwargs):
                spawned.append(kwargs)
                return {"status": "success", "text": "ok"}

        root = tmp_path / "dev"
        (root / "repo").mkdir(parents=True, exist_ok=True)
        return node_agent.NodeAgent(
            node=NODE,
            transport=lambda *a: (200, {}),
            roots=[root],
            backends=("claude", "acp:claude"),
            backend_factory=lambda name, agent_label=None: Fake(),
            facts_fn=lambda: {},
            session_root=tmp_path / "s",
            spool_dir=tmp_path / "sp",
        )

    def _session(self, tmp_path, monkeypatch, *, age_s: float) -> Path:
        store = tmp_path / "claude-store"
        monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(store))
        path = _claude_session(store, "-repo", CLAUDE_SID, str(tmp_path / "dev" / "repo"))
        stamp = __import__("time").time() - age_s
        os.utime(path, (stamp, stamp))
        return path

    def test_recently_written_session_is_refused_without_spawning(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_NODE_ADOPT_QUIET_S", "120")
        self._session(tmp_path, monkeypatch, age_s=5)
        spawned: list = []
        agent = self._agent(tmp_path, spawned)
        for backend, source in (("claude", "adopt"), ("acp:claude", "resume_native")):
            receipt = agent.execute(_adopt_item(tmp_path / "dev", backend=backend, source=source))
            assert receipt["status"] == "failed"
            assert receipt["reason"] == "session_recently_active"
        assert spawned == []

    def test_quiet_session_runs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_NODE_ADOPT_QUIET_S", "120")
        self._session(tmp_path, monkeypatch, age_s=600)
        spawned: list = []
        receipt = self._agent(tmp_path, spawned).execute(_adopt_item(tmp_path / "dev"))
        assert receipt["status"] == "finished", receipt
        assert spawned[0]["resume_session_id"] == CLAUDE_SID

    def test_missing_session_file_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(tmp_path / "empty"))
        spawned: list = []
        receipt = self._agent(tmp_path, spawned).execute(_adopt_item(tmp_path / "dev"))
        assert receipt["reason"] == "session_not_found"
        assert spawned == []

    def test_a_hub_resume_is_not_preflighted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(tmp_path / "empty"))
        spawned: list = []
        item = _adopt_item(tmp_path / "dev", source="resume")
        receipt = self._agent(tmp_path, spawned).execute(item)
        assert receipt["status"] == "finished"

    def test_claim_carries_the_binding_source(self, hub, tmp_path):
        root = tmp_path / "dev"
        _agent(hub, tmp_path, native=[_native(root)])
        body = {
            "node": NODE,
            "harness": "claude",
            "external_session_id": CLAUDE_SID,
            "prompt": "go",
        }
        resp = _approved(lambda: hub.post("/api/action/node-adopt", json=body, headers=_auth()))
        assert resp.status_code == 201, resp.json()
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert claimed["resume_source"] == "adopt"

    def test_scan_time_is_reported_and_kept(self, stores):
        _claude_session(stores["claude"], "-repo", CLAUDE_SID, str(stores["repo"]))
        (row,) = native_sessions.discover(
            [stores["root"]], claude_dir=stores["claude"], codex_dir=stores["codex"]
        )
        assert row["scanned_at"]
        kept = node_registry.bounded_native_sessions([row], [os.path.realpath(stores["root"])])
        assert kept[0]["scanned_at"] == row["scanned_at"]


class TestCloseEndedAdoptionsIsConditional:
    def _adopted_then_ended(self, hub, tmp_path) -> str:
        """Adopt, then end the adopt item without a receipt (cancelled)."""
        root = tmp_path / "dev"
        _agent(hub, tmp_path, native=[_native(root)])
        body = {
            "node": NODE,
            "harness": "claude",
            "external_session_id": CLAUDE_SID,
            "prompt": "go",
        }
        resp = _approved(lambda: hub.post("/api/action/node-adopt", json=body, headers=_auth()))
        item_id = resp.json()["item_id"]
        conn = storage.connect()
        try:
            conn.execute("UPDATE node_dispatch SET status='cancelled' WHERE id=?", (item_id,))
            conn.commit()
        finally:
            conn.close()
        return item_id

    def _rebind(self, item_id: str) -> None:
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE agent_sessions SET meta=json_set(meta, '$.item_id', ?) "
                "WHERE external_session_id=?",
                (item_id, CLAUDE_SID),
            )
            conn.commit()
        finally:
            conn.close()

    def test_a_row_bound_to_an_ended_item_closes(self, hub, tmp_path):
        self._adopted_then_ended(hub, tmp_path)
        assert node_resume.close_ended_adoptions() == 1
        (row,) = _session_rows(CLAUDE_SID)
        assert (row["status"], row["exit_status"]) == ("failed", "item_cancelled")

    def test_a_row_rebound_to_a_live_item_stays_open(self, hub, tmp_path):
        self._adopted_then_ended(hub, tmp_path)
        root = tmp_path / "dev"
        live = node_queue.enqueue(
            NODE, created_by="Desktop_Agent1", prompt="p", cwd=str(root / "repo"), backend="claude"
        )["item_id"]
        # Re-adopted onto a queued item: the sweep's join on a terminal item
        # status no longer matches the row, so it is skipped.
        self._rebind(live)
        assert node_resume.close_ended_adoptions() == 0
        (row,) = _session_rows(CLAUDE_SID)
        assert row["status"] == "running"
        assert row["meta"]["item_id"] == live

    def test_a_database_error_reports_nothing_closed(self, hub, tmp_path, monkeypatch):
        self._adopted_then_ended(hub, tmp_path)
        import sqlite3

        class Broken:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                return self._conn.execute(sql, *args)

            def commit(self):
                # The UPDATE counted a row; the commit then fails and rolls back.
                raise sqlite3.OperationalError("disk I/O error")

            def __getattr__(self, name):
                return getattr(self._conn, name)

        real = storage.connect
        monkeypatch.setattr(storage, "connect", lambda: Broken(real()))
        assert node_resume.close_ended_adoptions() == 0
        monkeypatch.setattr(storage, "connect", real)
        (row,) = _session_rows(CLAUDE_SID)
        assert row["status"] == "running"

    def test_receipt_status_wins_over_the_sweep(self, hub, tmp_path):
        root = tmp_path / "dev"
        agent = _agent(hub, tmp_path, native=[_native(root)])
        body = {
            "node": NODE,
            "harness": "claude",
            "external_session_id": CLAUDE_SID,
            "prompt": "go",
        }
        _approved(lambda: hub.post("/api/action/node-adopt", json=body, headers=_auth()))
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        agent.post_receipt(
            claimed["item_id"],
            {"status": "failed", "attempt": claimed["attempt"], "session_id": CLAUDE_SID},
        )
        (row,) = _session_rows(CLAUDE_SID)
        assert row["status"] == "failed"
        assert row["exit_status"] == "failed"


class TestHeartbeatStartsTheDiscoveryClock:
    def test_a_failed_heartbeat_does_not_start_it(self, hub, tmp_path):
        agent = _agent(hub, tmp_path)
        agent._discovered_at = None
        scans: list = []
        agent._discover_fn = lambda: scans.append(1) or []

        def down(*_a):
            raise node_agent.NodeTransportError("hub down")

        agent.transport = down
        with pytest.raises(node_agent.NodeTransportError):
            agent.heartbeat()
        assert agent._discovered_at is None
        agent.transport = _transport(hub)
        agent.heartbeat()
        assert scans == [1, 1]
        assert agent._discovered_at is not None


# --- fourth review round (PR #1015) ---------------------------------------------------


class TestOwnWritesAreNotATerminal:
    """Review #1741: a Resume right after the node's own turn is not refused."""

    def _writing_agent(self, tmp_path, path: Path, spawned: list):
        class TurnWrites:
            def run(self, **kwargs):
                spawned.append(kwargs)
                with path.open("a") as fh:
                    fh.write(json.dumps({"type": "assistant", "sessionId": CLAUDE_SID}) + "\n")
                return {"status": "success", "text": "ok"}

        root = tmp_path / "dev"
        (root / "repo").mkdir(parents=True, exist_ok=True)
        return node_agent.NodeAgent(
            node=NODE,
            transport=lambda *a: (200, {}),
            roots=[root],
            backends=("claude",),
            backend_factory=lambda name, agent_label=None: TurnWrites(),
            facts_fn=lambda: {},
            session_root=tmp_path / "s",
            spool_dir=tmp_path / "sp",
        )

    def _quiet_session(self, tmp_path, monkeypatch) -> Path:
        monkeypatch.setenv("UA_NODE_ADOPT_QUIET_S", "120")
        store = tmp_path / "claude-store"
        monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(store))
        path = _claude_session(store, "-repo", CLAUDE_SID, str(tmp_path / "dev" / "repo"))
        stamp = __import__("time").time() - 600
        os.utime(path, (stamp, stamp))
        return path

    def test_immediate_resume_after_an_adopt_runs(self, tmp_path, monkeypatch):
        path = self._quiet_session(tmp_path, monkeypatch)
        spawned: list = []
        agent = self._writing_agent(tmp_path, path, spawned)
        root = tmp_path / "dev"
        assert agent.execute(_adopt_item(root, source="adopt"))["status"] == "finished"
        receipt = agent.execute(_adopt_item(root, source="resume_native"))
        assert receipt["status"] == "finished", receipt
        assert len(spawned) == 2
        # A fresh agent on the same node (restart) remembers its own write.
        again = self._writing_agent(tmp_path, path, spawned)
        assert again.execute(_adopt_item(root, source="resume_native"))["status"] == "finished"

    def test_a_terminal_write_after_the_node_run_still_refuses(self, tmp_path, monkeypatch):
        path = self._quiet_session(tmp_path, monkeypatch)
        spawned: list = []
        agent = self._writing_agent(tmp_path, path, spawned)
        root = tmp_path / "dev"
        assert agent.execute(_adopt_item(root, source="adopt"))["status"] == "finished"
        with path.open("a") as fh:
            fh.write(json.dumps({"type": "user", "sessionId": CLAUDE_SID}) + "\n")
        receipt = agent.execute(_adopt_item(root, source="resume_native"))
        assert receipt["reason"] == "session_recently_active"
        assert len(spawned) == 1

    def test_quiet_zero_still_refuses_a_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_NODE_ADOPT_QUIET_S", "0")
        monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(tmp_path / "empty"))
        spawned: list = []
        agent = self._writing_agent(tmp_path, tmp_path / "unused.jsonl", spawned)
        receipt = agent.execute(_adopt_item(tmp_path / "dev"))
        assert receipt["reason"] == "session_not_found"
        assert spawned == []


class TestResumeOfAnAdoptedItem:
    def test_claim_is_marked_resume_native(self, hub, tmp_path):
        root = tmp_path / "dev"
        agent = _agent(hub, tmp_path, native=[_native(root)])
        body = {
            "node": NODE,
            "harness": "claude",
            "external_session_id": CLAUDE_SID,
            "prompt": "go",
        }
        assert (
            _approved(
                lambda: hub.post("/api/action/node-adopt", json=body, headers=_auth())
            ).status_code
            == 201
        )
        adopted = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert adopted["resume_source"] == "adopt"
        agent.post_receipt(
            adopted["item_id"],
            {"status": "finished", "attempt": adopted["attempt"], "session_id": CLAUDE_SID},
        )
        resp = _approved(
            lambda: hub.post(
                "/api/action/node-resume",
                json={"item_id": adopted["item_id"], "prompt": "more"},
                headers=_auth(),
            )
        )
        assert resp.status_code == 201, resp.json()
        assert resp.json()["mode"] == "native_flag"
        claimed = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert claimed["resume_session_id"] == CLAUDE_SID
        assert claimed["resume_source"] == "resume_native"


class TestOnlyAFinishedRunIsOwnWrite:
    def test_a_failed_run_does_not_absorb_a_terminal_write(self, tmp_path, monkeypatch):
        """Review #1741: an errored run must not record a terminal's write as its own."""
        monkeypatch.setenv("UA_NODE_ADOPT_QUIET_S", "120")
        store = tmp_path / "claude-store"
        monkeypatch.setenv("UA_NODE_CLAUDE_PROJECTS", str(store))
        path = _claude_session(store, "-repo", CLAUDE_SID, str(tmp_path / "dev" / "repo"))
        stamp = __import__("time").time() - 600
        os.utime(path, (stamp, stamp))
        spawned: list = []

        class ErrorsWhileTerminalWrites:
            def run(self, **kwargs):
                spawned.append(kwargs)
                # A terminal writes while this run fails without touching it.
                with path.open("a") as fh:
                    fh.write(json.dumps({"type": "user", "sessionId": CLAUDE_SID}) + "\n")
                return {"status": "error", "error": "harness crashed"}

        root = tmp_path / "dev"
        (root / "repo").mkdir(parents=True, exist_ok=True)
        agent = node_agent.NodeAgent(
            node=NODE,
            transport=lambda *a: (200, {}),
            roots=[root],
            backends=("claude",),
            backend_factory=lambda name, agent_label=None: ErrorsWhileTerminalWrites(),
            facts_fn=lambda: {},
            session_root=tmp_path / "s",
            spool_dir=tmp_path / "sp",
        )
        first = agent.execute(_adopt_item(root, source="adopt"))
        assert first["status"] == "failed"
        receipt = agent.execute(_adopt_item(root, source="resume_native"))
        assert receipt["reason"] == "session_recently_active"
        assert len(spawned) == 1
