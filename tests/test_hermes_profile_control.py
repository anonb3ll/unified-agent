"""Per-profile Hermes control uses the existing node queue and ACP wire contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_acp_adapter import CLAUDE_TOKEN, FAKE_AGENT
from test_acp_adapter import hub as hub

from unified_agent import acp_backend, acp_matrix, node_agent, node_queue, storage


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    root.mkdir()
    (root / "config.yaml").write_text("private: do not read\n")
    for name in ("codex-dev", "reviewer"):
        home = root / "profiles" / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("private: do not read\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


@pytest.fixture
def registered(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent2:Desktop_Agent5:Desktop_Agent1")
    monkeypatch.setenv(
        "UA_SURFACE_MAP",
        "test-host/hermes/default:Desktop_Agent5,test-host/hermes/codex-dev:Desktop_Agent2",
    )
    node_queue.register(
        "test-host",
        identity="Desktop_Agent1",
        facts={
            "backends": ["acp:hermes"],
            "node_features": ["session_settings"],
            "surfaces": {"hermes_profiles": ["default", "codex-dev", "reviewer"]},
            "worktree_roots": [str(tmp_path)],
        },
    )
    return {
        "prompt": "synthetic prompt",
        "cwd": str(tmp_path),
        "backend": "acp:hermes",
        "hermes_profile": "codex-dev",
    }


def test_discovery_default_live_profiles_and_no_secret_reads(profiles, monkeypatch):
    ghost = profiles / "profiles" / "ghost"
    ghost.mkdir()
    deleted = profiles / "profiles" / "deleted"
    deleted.mkdir()
    (deleted / "config.yaml").touch()
    (profiles / "profiles" / ".deleted").mkdir()
    (profiles / "profiles" / ".deleted" / "deleted").touch()
    (profiles / "profiles" / "escape").symlink_to(profiles.parent, target_is_directory=True)
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: pytest.fail("discovery read contents"))
    assert node_agent.collect_surfaces(profiles)["hermes_profiles"] == [
        "codex-dev",
        "default",
        "reviewer",
    ]
    # A node launched inside a named HERMES_HOME still lists the same store.
    monkeypatch.setenv("HERMES_HOME", str(profiles / "profiles" / "codex-dev"))
    assert node_agent.collect_surfaces()["hermes_profiles"] == ["codex-dev", "default", "reviewer"]


@pytest.mark.parametrize(
    "profile",
    [
        "../codex-dev",
        "-p",
        "MyProfile",
        "x.y",
        "",
        "a" * 65,
        "test",
        "tmp",
        "root",
        "sudo",
        "hermes",
    ],
)
def test_argv_rejects_unsafe_profile(profile):
    with pytest.raises(ValueError):
        acp_matrix.resolve_argv("hermes", hermes_profile=profile)


def test_argv_explicit_default_prevents_sticky_selection(monkeypatch):
    monkeypatch.setenv("UA_HERMES_BIN", "/fake/hermes")
    assert acp_matrix.resolve_argv("hermes", hermes_profile="default") == [
        "/fake/hermes",
        "-p",
        "default",
        "acp",
    ]
    with pytest.raises(ValueError):
        acp_matrix.resolve_argv("cursor", hermes_profile="default")


@pytest.mark.parametrize("profile", [None, "reviewer", "missing", "../codex-dev"])
def test_hub_rejects_unmapped_missing_unsafe_profile(registered, profile):
    with pytest.raises(node_queue.NodeError):
        node_queue.enqueue(
            "test-host", created_by="Desktop_Agent2", **{**registered, "hermes_profile": profile}
        )


def test_queue_preserves_owner_and_profile_provenance_rechecks_mapping(registered, monkeypatch):
    queued = node_queue.enqueue("test-host", created_by="Desktop_Agent2", **registered)
    item = node_queue.get_item(queued["item_id"])
    assert item["agent"] == "Desktop_Agent1"
    assert item["profile_agent"] == "Desktop_Agent2"
    assert item["hermes_profile"] == "codex-dev"
    # Mapping drift after approval/enqueue must not silently retarget work.
    monkeypatch.setenv("UA_SURFACE_MAP", "test-host/hermes/codex-dev:Desktop_Agent5")
    assert node_queue.claim_next("test-host", identity="Desktop_Agent1") is None
    assert node_queue.get_item(queued["item_id"])["status"] == "failed"


def test_hub_profile_native_policy_independent_ceiling(registered, monkeypatch):
    from unified_agent import trust_presets

    seen = []

    def policy(target, **kw):
        seen.append(target)
        if target == "Desktop_Agent2":
            raise ValueError("profile denied")

    monkeypatch.setattr(trust_presets, "require_native_allowed", policy)
    with pytest.raises(node_queue.NodeError, match="profile denied"):
        node_queue.enqueue("test-host", created_by="Desktop_Agent5", **registered)
    assert seen == ["Desktop_Agent1", "Desktop_Agent2"]


def test_dispatch_gate_binds_profile_and_mapping(registered, monkeypatch):
    from unified_agent import safety

    gates = []
    monkeypatch.setattr(
        safety, "check_prod_action_gate", lambda tool, payload: gates.append((tool, payload.copy()))
    )
    first = node_queue.request_dispatch("test-host", actor="Desktop_Agent2", **registered)
    node_queue.request_dispatch(
        "test-host", actor="Desktop_Agent2", **{**registered, "hermes_profile": "default"}
    )
    assert gates[0][1]["hermes_profile"] == "codex-dev"
    assert gates[0][1]["profile_agent"] == "Desktop_Agent2"
    assert gates[1][1]["profile_agent"] == "Desktop_Agent5"
    claimed = node_queue.claim_next("test-host", identity="Desktop_Agent1")
    assert claimed["item_id"] == first["item_id"]
    assert claimed["agent"] == "Desktop_Agent1"
    assert claimed["hermes_profile"] == "codex-dev"
    assert claimed["profile_agent"] == "Desktop_Agent2"


def test_restart_keeps_profile_and_fresh_session(registered, monkeypatch):
    from unified_agent import safety

    monkeypatch.setattr(safety, "check_prod_action_gate", lambda *a, **kw: None)
    queued = node_queue.enqueue("test-host", created_by="Desktop_Agent2", **registered)
    node_queue.claim_next("test-host", identity="Desktop_Agent1")
    node_queue.post_receipt(
        "test-host",
        queued["item_id"],
        identity="Desktop_Agent1",
        receipt={"status": "finished", "text": "ok", "session_id": "prior-session"},
    )
    restarted = node_queue.restart(queued["item_id"], actor="Desktop_Agent2")
    fresh = node_queue.get_item(restarted["item_id"])
    assert fresh["hermes_profile"] == "codex-dev"
    assert fresh["profile_agent"] == "Desktop_Agent2"
    assert fresh["session_id"] is None
    monkeypatch.setenv("UA_SURFACE_MAP", "test-host/hermes/codex-dev:NONE")
    with pytest.raises(node_queue.NodeError):
        node_queue.restart(queued["item_id"], actor="Desktop_Agent2")


def test_permission_approval_contains_profile_provenance(registered, monkeypatch):
    from unified_agent.tools import approvals

    queued = node_queue.enqueue("test-host", created_by="Desktop_Agent2", **registered)
    node_queue.claim_next("test-host", identity="Desktop_Agent1")
    captured = []

    def gate(tool, canonical, **kwargs):
        captured.append((canonical, kwargs))
        return {"status": "denied"}

    monkeypatch.setattr(approvals, "check_approval_gate", gate)
    monkeypatch.setenv("UA_AGENT_SCOPES", "")
    result = node_queue.request_node_permission(
        "test-host",
        queued["item_id"],
        identity="Desktop_Agent1",
        payload={
            "session_id": "synthetic-session",
            "tool_call": {"title": "fixture tool"},
            "options": [],
        },
    )
    assert result["status"] == "denied"
    assert captured[0][0]["hermes_profile"] == "codex-dev"
    assert captured[0][0]["profile_agent"] == "Desktop_Agent2"
    assert captured[0][1]["requested_by"] == "Desktop_Agent1"


def test_session_mirror_attributes_profile_separately(registered):
    queued = node_queue.enqueue("test-host", created_by="Desktop_Agent2", **registered)
    item = node_queue.get_item(queued["item_id"])
    node_queue._open_acp_session(item, "test-host", queued["item_id"], "synthetic-1392")
    conn = storage.connect()
    try:
        row = dict(
            conn.execute(
                "SELECT * FROM agent_sessions WHERE external_session_id='synthetic-1392'"
            ).fetchone()
        )
    finally:
        conn.close()
    assert row["agent"] == "Desktop_Agent2"
    meta = json.loads(row["meta"])
    assert meta["device_agent"] == "Desktop_Agent1"
    assert meta["hermes_profile"] == "codex-dev"


def test_node_local_profile_deleted_after_claim_refuses_before_backend(profiles, tmp_path):
    calls = []
    node = node_agent.NodeAgent(
        node="test-host",
        transport=lambda *a: (200, {}),
        roots=[tmp_path],
        backends=("acp:hermes",),
        backend_factory=lambda *a, **kw: calls.append(kw),
        session_root=tmp_path / "sessions",
        spool_dir=tmp_path / "spool",
    )
    (profiles / "profiles" / "codex-dev" / "config.yaml").unlink()
    result = node.execute(
        {
            "item_id": "nd-" + "a" * 16,
            "cwd": str(tmp_path),
            "backend": "acp:hermes",
            "hermes_profile": "codex-dev",
            "profile_agent": "Desktop_Agent2",
            "agent": "Desktop_Agent1",
        }
    )
    assert result["status"] == "failed"
    assert "profile" in result["reason"]
    assert not calls


def test_profile_acp_wire_stream_permission_usage_and_cancel(profiles, tmp_path, monkeypatch):
    script = tmp_path / "synthetic_acp.py"
    script.write_text(
        "#!"
        + sys.executable
        + "\n"
        + FAKE_AGENT.split("\n", 1)[1]
        .replace("for raw in sys.stdin:", "note('argv', sys.argv[1:])\nfor raw in sys.stdin:")
        .replace('"end_turn")}})', '"end_turn"), "usage": {"inputTokens": 4, "outputTokens": 2}}})')
    )
    script.chmod(0o755)
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("UA_HERMES_BIN", str(script))
    monkeypatch.setenv("FAKE_ACP_TRACE", str(trace))
    events = []
    backend = acp_backend.AcpBackend("hermes")
    kwargs = {
        "prompt": "fixture",
        "cwd": str(tmp_path),
        "session_dir": str(tmp_path),
        "session_id": "item",
        "agent_label": "Desktop_Agent1",
        "timeout_s": 10,
        "hermes_profile": "codex-dev",
        "on_event": events.append,
    }
    result = backend.run(**kwargs)
    assert result["status"] == "success"
    assert result["text"] == "hello world"
    assert (
        next(e for e in events if e["kind"] == "session_open")["data"]["hermes_profile"]
        == "codex-dev"
    )
    assert next(e for e in events if e["kind"] == "turn_end")["data"]["usage"] == {
        "inputTokens": 4,
        "outputTokens": 2,
    }
    assert {"argv": ["-p", "codex-dev", "acp"]} in [
        json.loads(line) for line in trace.read_text().splitlines()
    ]
    monkeypatch.setenv("FAKE_ACP_PERMISSION", "1")
    permitted = backend.run(**kwargs, permission_fn=lambda p: {"status": "approved"})
    assert permitted["status"] == "success"
    assert any(
        row.get("response", {}).get("result", {}).get("outcome", {}).get("optionId") == "allow-1"
        for row in map(json.loads, trace.read_text().splitlines())
    )
    monkeypatch.delenv("FAKE_ACP_PERMISSION")
    monkeypatch.setenv("FAKE_ACP_HANG", "1")
    assert backend.run(**kwargs, kill_check=lambda: True)["status"] == "killed"


@pytest.mark.parametrize("path", ["/api/node/dispatch", "/api/action/node-session"])
def test_http_profile_launch_approval_status_and_reconnect(hub, registered, path):
    from unified_agent import config
    from unified_agent.tools.approvals import approval_resolve

    body = {"node": "test-host", **registered}
    if "action" in path:
        body["action"] = "start"
    headers = {"Authorization": f"Bearer {CLAUDE_TOKEN}"}
    pending = hub.post(path, json=body, headers=headers)
    assert pending.status_code == 202, pending.text
    verified = config.agent_identity_var.set("Desktop_Person")
    try:
        approval_resolve(pending.json()["approval_id"], "approve", resolved_by="Desktop_Person")
    finally:
        config.agent_identity_var.reset(verified)
    # An approval for coder must not permit a different profile.
    different = hub.post(path, json={**body, "hermes_profile": "default"}, headers=headers)
    assert different.status_code == 202
    queued = hub.post(path, json=body, headers=headers)
    assert queued.status_code == 201, queued.text
    item_id = queued.json()["item_id"]
    claim = hub.post("/api/node/claim", json={"node": "test-host", "wait_s": 0}, headers=headers)
    assert claim.status_code == 200
    item = claim.json()["item"]
    assert item["hermes_profile"] == "codex-dev"
    assert item["profile_agent"] == "Desktop_Agent2"
    assert item["agent"] == "Desktop_Agent1"
    # Re-registering a disconnected node preserves its claimed item and provenance.
    facts = node_queue.list_nodes()["test-host"]
    node_queue.register(
        "test-host",
        identity="Desktop_Agent1",
        facts={
            "backends": facts["backends"],
            "surfaces": facts["surfaces"],
        },
    )
    assert node_queue.claim_next("test-host", identity="Desktop_Agent1") is None
    recent = hub.get("/api/nodes", headers=headers).json()
    row = next(r for r in recent["recent_items"] if r["id"] == item_id)
    assert row["hermes_profile"] == "codex-dev" and row["status"] == "claimed"
    node = recent["nodes"][0]
    assert any(p["name"] == "reviewer" and not p["launchable"] for p in node["hermes_profiles"])
    # The re-registered facts carry no node_features, so the matrix truthfully
    # says this ua-node predates resume (task #1741).
    resume_row = node["controls"]["acp:hermes"]["capabilities"]["resume"]
    assert resume_row["state"] == "unavailable"
    assert "predates resume" in resume_row["detail"]
    # A node that advertises session_resume but has recorded no capability yet
    # reads "unknown": nothing is promised before the agent's initialize.
    unknown = node_queue.acp_matrix.control_capabilities(
        "acp:hermes",
        advertised=True,
        version=None,
        features=[node_queue.acp_matrix.RESUME_FEATURE],
        resume_mode=None,
    )["capabilities"]["resume"]
    assert unknown["state"] == "unknown"
    assert node["controls"]["devin"]["advertised"] is False
    assert node["controls"]["acp:cursor"]["advertised"] is False
    node_queue.cancel(item_id, actor="Desktop_Agent2")
    assert (
        item_id
        in node_queue.heartbeat("test-host", identity="Desktop_Agent1", running=item_id)["cancel"]
    )
    node_queue.post_receipt(
        "test-host", item_id, identity="Desktop_Agent1", receipt={"status": "killed"}
    )
    assert node_queue.get_item(item_id)["status"] == "killed"


def test_mapping_change_during_gate_cannot_retarget(registered, monkeypatch):
    from unified_agent import safety

    def gate(*a):
        monkeypatch.setenv("UA_SURFACE_MAP", "test-host/hermes/codex-dev:Desktop_Agent5")

    monkeypatch.setattr(safety, "check_prod_action_gate", gate)
    with pytest.raises(node_queue.NodeError, match="mapping changed"):
        node_queue.request_dispatch("test-host", actor="Desktop_Agent2", **registered)
    assert node_queue.list_recent_items() == []


def test_restart_cannot_change_mapped_agent(registered, monkeypatch):
    from unified_agent import safety

    gates = []
    monkeypatch.setattr(safety, "check_prod_action_gate", lambda *a: gates.append(a))
    queued = node_queue.enqueue("test-host", created_by="Desktop_Agent2", **registered)
    node_queue.claim_next("test-host", identity="Desktop_Agent1")
    node_queue.post_receipt(
        "test-host", queued["item_id"], identity="Desktop_Agent1", receipt={"status": "finished"}
    )
    monkeypatch.setenv("UA_SURFACE_MAP", "test-host/hermes/codex-dev:Desktop_Agent5")
    with pytest.raises(node_queue.NodeError, match="mapping changed"):
        node_queue.restart(queued["item_id"], actor="Desktop_Agent2")

    assert gates == []


def test_additive_queue_schema_migrates_without_guessing_profiles(registered):
    queued = node_queue.enqueue("test-host", created_by="Desktop_Agent2", **registered)
    conn = storage.connect()
    try:
        conn.execute("ALTER TABLE node_dispatch DROP COLUMN hermes_profile")
        conn.execute("ALTER TABLE node_dispatch DROP COLUMN profile_agent")
        conn.commit()
    finally:
        conn.close()
    storage.init_db()
    item = node_queue.get_item(queued["item_id"])
    assert item["hermes_profile"] is None and item["profile_agent"] is None
    assert item["prompt"] == registered["prompt"]
    # Legacy bare ACP Hermes must be explicitly reissued, not run a sticky profile.
    assert node_queue.claim_next("test-host", identity="Desktop_Agent1") is None
    assert node_queue.get_item(queued["item_id"])["status"] == "failed"


@pytest.mark.parametrize("surfaces", [[], "bad", 42, None])
def test_malformed_surface_report_cannot_launch(surfaces, registered):
    node_queue.register(
        "test-host",
        identity="Desktop_Agent1",
        facts={
            "backends": ["acp:hermes"],
            "surfaces": surfaces,
        },
    )
    with pytest.raises(node_queue.NodeError, match="not discovered"):
        node_queue.enqueue("test-host", created_by="Desktop_Agent2", **registered)


def test_per_profile_status_survives_recent_item_pagination(registered):
    queued = node_queue.enqueue("test-host", created_by="Desktop_Agent2", **registered)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE node_dispatch SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (queued["item_id"],),
        )
        conn.commit()
    finally:
        conn.close()
    for _ in range(30):
        node_queue.enqueue(
            "test-host", created_by="Desktop_Agent2", **{**registered, "hermes_profile": "default"}
        )
    assert queued["item_id"] not in {r["id"] for r in node_queue.list_recent_items()}
    assert (
        node_queue.hermes_profile_statuses()[("test-host", "codex-dev")]["item_id"]
        == queued["item_id"]
    )


def test_node_passes_profile_without_changing_authenticated_owner(profiles, tmp_path):
    calls = []

    class Backend:
        def run(self, **kwargs):
            calls.append(kwargs)
            return {"status": "success", "session_id": "fake-profile-session"}

    node = node_agent.NodeAgent(
        node="test-host",
        transport=lambda *a: (200, {}),
        roots=[tmp_path],
        backends=("acp:hermes",),
        backend_factory=lambda *a, **kw: Backend(),
        session_root=tmp_path / "sessions",
        spool_dir=tmp_path / "spool",
    )
    receipt = node.execute(
        {
            "item_id": "nd-" + "a" * 16,
            "cwd": str(tmp_path),
            "backend": "acp:hermes",
            "agent": "Desktop_Agent1",
            "hermes_profile": "codex-dev",
            "profile_agent": "Desktop_Agent2",
            "prompt": "fixture",
        }
    )
    assert receipt["status"] == "finished"
    assert calls[0]["hermes_profile"] == "codex-dev"
    assert calls[0]["agent_label"] == "Desktop_Agent1"


def test_stream_uses_hub_owned_profile_provenance(registered, monkeypatch):
    queued = node_queue.enqueue("test-host", created_by="Desktop_Agent2", **registered)
    node_queue.claim_next("test-host", identity="Desktop_Agent1")
    recorded = []
    monkeypatch.setattr(
        node_queue, "_record", lambda kind, payload, actor: recorded.append((kind, payload, actor))
    )
    node_queue.record_node_event(
        "test-host",
        queued["item_id"],
        [
            {
                "kind": "agent_message_chunk",
                "session_id": "fixture",
                "text": "hello",
                "data": {"hermes_profile": "reviewer", "profile_agent": "Desktop_Agent5"},
            }
        ],
        identity="Desktop_Agent1",
    )
    kind, payload, actor = recorded[0]
    assert kind == "acp_event" and actor == "Desktop_Agent1"
    assert payload["hermes_profile"] == "codex-dev"
    assert payload["profile_agent"] == "Desktop_Agent2"
    assert payload["device_agent"] == "Desktop_Agent1"


def test_discovery_reserves_default_with_many_earlier_named_profiles(profiles):
    for i in range(node_agent.MAX_SURFACES):
        home = profiles / "profiles" / f"a{i:02d}"
        home.mkdir()
        (home / "config.yaml").touch()
    found = node_agent.collect_surfaces(profiles)["hermes_profiles"]
    assert "default" in found
    assert len(found) == node_agent.MAX_SURFACES
    assert found == sorted(found)
    assert found == [f"a{i:02d}" for i in range(node_agent.MAX_SURFACES - 1)] + ["default"]


def test_enqueue_rejects_expected_profile_agent_before_inserting(registered, monkeypatch):
    monkeypatch.setenv("UA_SURFACE_MAP", "test-host/hermes/codex-dev:Desktop_Agent5")
    with pytest.raises(node_queue.NodeError, match="mapping changed"):
        node_queue.enqueue(
            "test-host",
            created_by="Desktop_Agent2",
            expected_profile_agent="Desktop_Agent2",
            **registered,
        )
    assert node_queue.list_recent_items() == []
