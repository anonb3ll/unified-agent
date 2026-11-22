"""Device acceptance (task #2112): a harmless job a ua-node passes before real work.

Same harness as ``test_node.py``: the real route table behind the real auth
middleware and the SQLite queue, with the device side driven through
``NodeAgent`` over a TestClient transport. The acceptance backend is the real
built-in one; only its sleep is injected, and that injected sleep runs the
node's heartbeat, standing in for the heartbeat thread that relays a cancel.
"""

from __future__ import annotations

import json
import random
import secrets
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import (
    acceptance_backend,
    config,
    node_acceptance,
    node_agent,
    node_queue,
    node_registry,
    server,
    storage,
    webui,
)
from unified_agent.tools import doctor
from unified_agent.tools.graph import coordination_manage

CLAUDE_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
CODEX_TOKEN = f"tok-{secrets.token_urlsafe(8)}"
NODE = "desk-01"


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setenv(
        "UA_AGENT_TOKENS", f"Desktop_Agent1:{CLAUDE_TOKEN},Desktop_Agent2:{CODEX_TOKEN}"
    )
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_HEARTBEAT_MIN_INTERVAL_S", "0")
    monkeypatch.setenv("UA_NODE_ALLOW_BEARER", "1")
    monkeypatch.delenv("UA_NODE_REQUIRE_ACCEPTANCE", raising=False)
    monkeypatch.delenv("UA_REVOKED_AGENTS", raising=False)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        yield client


def _headers(token=CLAUDE_TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _transport(client, token=CLAUDE_TOKEN):
    def call(path, body, timeout):
        resp = client.post(path, json=body, headers=_headers(token))
        return resp.status_code, resp.json()

    return call


class FakeBackend:
    def __init__(self):
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "success", "text": "done", "exit_code": 0}


def _facts(root: Path):
    return {
        "os": {"system": "Linux"},
        "harnesses": {"codex": {"path": "/usr/bin/codex", "version": "codex 1.0"}},
        "backends": ["codex", "acceptance"],
        "node_features": node_agent.node_features(),
        "worktree_roots": [{"path": str(root), "exists": True, "worktrees": 0}],
    }


def _agent(hub, tmp_path, *, relay_cancel=True):
    """A node whose acceptance sleep runs a heartbeat (the cancel relay) when asked."""
    root = tmp_path / "dev"
    (root / "repo").mkdir(parents=True, exist_ok=True)
    fake = FakeBackend()
    box: dict = {}

    def acceptance_sleep(_seconds):
        if relay_cancel:
            box["agent"].heartbeat()

    agent = node_agent.NodeAgent(
        node=NODE,
        transport=_transport(hub),
        roots=[root],
        backends=("codex",),
        backend_factory=lambda name, agent_label: fake,
        facts_fn=lambda: _facts(root),
        sleep=lambda s: None,
        rng=random.Random(7),
        claim_wait_s=0,
        session_root=tmp_path / "sessions",
        spool_dir=tmp_path / "spool",
        acceptance_sleep=acceptance_sleep,
        vault_sync_fn=lambda: None,
    )
    box["agent"] = agent
    return agent, fake, root


def _start(hub, token=CLAUDE_TOKEN, node=NODE):
    return hub.post(
        "/api/action/node-acceptance",
        json={"action": "start", "node": node},
        headers=_headers(token),
    )


def _status(hub, node=NODE):
    resp = hub.get(f"/api/nodes/acceptance?node={node}", headers=_headers())
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _run_acceptance(hub, agent, *, attest=True):
    started = _start(hub)
    assert started.status_code == 201, started.json()
    assert agent.run_once() == "ran"  # the item that completes
    assert agent.run_once() == "ran"  # the item the hub cancels mid-run
    diagnostics = _status(hub)
    if attest and diagnostics.get("diagnostics_passed"):
        node_acceptance.attest(
            NODE,
            started.json()["run_id"],
            "test:independent-device-output-and-cancellation",
            actor="Operator",
        )
    return started.json()


def _acp_kinds(item_id: str) -> list[str]:
    return [e["kind"] for e in node_acceptance._item_events(item_id)]


class TestEndToEnd:
    def test_acceptance_passes_every_check_through_the_real_routes(self, hub, tmp_path):
        agent, fake, _ = _agent(hub, tmp_path)
        assert agent.run_once() == "registered"
        started = _run_acceptance(hub, agent)
        assert started["state"] == "pending"

        status = _status(hub)
        assert status["state"] == "accepted", status
        assert {name: c["status"] for name, c in status["checks"].items()} == {
            name: "pass" for name in node_acceptance.CHECKS
        }
        # The SHA the node itself reported at registration.
        assert status["code_sha"] == node_queue.list_nodes()[NODE]["code_sha"]
        assert status["accepted_at"]
        # Recorded, not only computed: the stored row says the same.
        assert node_acceptance.get(NODE)["state"] == "accepted"

        complete = node_queue.get_item(started["complete_item_id"])
        assert complete["status"] == "finished"
        assert complete["receipt"]["text"].startswith("acceptance:complete")
        cancelled = node_queue.get_item(started["cancel_item_id"])
        assert cancelled["status"] == "killed"
        assert cancelled["cancel_requested"] == 1
        assert cancelled["receipt"]["reason"] == "cancelled_by_hub"
        assert "acceptance_stopped" in _acp_kinds(started["cancel_item_id"])
        # The output file was written on the device, inside the session dir.
        out = tmp_path / "sessions" / started["complete_item_id"] / "acceptance-output.txt"
        assert out.read_bytes() == acceptance_backend.expected_output(started["run_id"], 3)
        # The built-in backend ran; the injected harness backend never did.
        assert fake.calls == []

    def test_node_listing_and_doctor_show_acceptance(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        row = doctor._fleet_node_acceptance_check()
        assert row["status"] == "DORMANT" and f"{NODE} never run" in row["detail"]

        _run_acceptance(hub, agent)
        nodes = hub.get("/api/nodes", headers=_headers()).json()
        [entry] = [n for n in nodes["nodes"] if n["node"] == NODE]
        assert entry["acceptance"]["state"] == "accepted"
        assert entry["acceptance"]["checks"]["cancel_confirmed"] == "pass"
        assert nodes["acceptance_required"] is False
        listing = hub.get("/api/nodes/acceptance", headers=_headers()).json()
        assert listing["nodes"][NODE]["state"] == "accepted"
        row = doctor._fleet_node_acceptance_check()
        assert row["status"] == "LIVE", row
        assert f"accepted: {NODE}" in row["detail"]

    def test_doctor_names_but_does_not_count_a_long_offline_node(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        _run_acceptance(hub, agent)
        # A leftover registration for the same device under an old name,
        # silent for two days and never accepted.
        old_beat = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
        conn = storage.connect()
        try:
            conn.execute("CREATE TEMP TABLE old_node AS SELECT * FROM nodes WHERE node=?", (NODE,))
            conn.execute("UPDATE old_node SET node='desk-old', last_heartbeat=?", (old_beat,))
            conn.execute("INSERT INTO nodes SELECT * FROM old_node")
            conn.commit()
        finally:
            conn.close()
        row = doctor._fleet_node_acceptance_check()
        assert row["status"] == "LIVE", row
        assert f"accepted: {NODE}" in row["detail"]
        assert "not counted: desk-old offline since" in row["detail"]
        assert (
            "desk-old offline since" in row["detail"] and "(last run: never run)" in row["detail"]
        )

    def test_doctor_all_nodes_long_offline_is_dormant_and_keeps_last_run(
        self, hub, tmp_path, monkeypatch
    ):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        old_beat = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
        conn = storage.connect()
        try:
            conn.execute("UPDATE nodes SET last_heartbeat=? WHERE node=?", (old_beat, NODE))
            conn.commit()
        finally:
            conn.close()
        failed = {NODE: {"agent": "x", "state": "failed", "reason": "timed_out"}}
        monkeypatch.setattr(node_acceptance, "read_states", lambda: failed)
        monkeypatch.setenv("UA_NODE_REQUIRE_ACCEPTANCE", "1")
        row = doctor._fleet_node_acceptance_check()
        # A failed run on a long-offline node is named, not counted, even
        # with the gate on.
        assert row["status"] == "DORMANT", row
        assert f"{NODE} offline since {old_beat} (last run: failed)" in row["detail"]

    def test_doctor_still_counts_a_briefly_offline_node(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        recent = (datetime.now(UTC) - timedelta(minutes=10)).isoformat(timespec="seconds")
        conn = storage.connect()
        try:
            conn.execute("UPDATE nodes SET last_heartbeat=? WHERE node=?", (recent, NODE))
            conn.commit()
        finally:
            conn.close()
        row = doctor._fleet_node_acceptance_check()
        assert f"{NODE} never run" in row["detail"]
        assert "not counted" not in row["detail"]

    def test_doctor_counts_a_future_dated_heartbeat(self, hub, tmp_path, monkeypatch):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        # Past the skew allowance the registry reads a future beat as offline,
        # but it is not old: a failed run on that node must still degrade.
        ahead = (datetime.now(UTC) + timedelta(hours=2)).isoformat(timespec="seconds")
        conn = storage.connect()
        try:
            conn.execute("UPDATE nodes SET last_heartbeat=? WHERE node=?", (ahead, NODE))
            conn.commit()
        finally:
            conn.close()
        bound = node_registry.read_nodes()[NODE]["agent"]
        failed = {NODE: {"agent": bound, "state": "failed", "reason": "timed_out"}}
        monkeypatch.setattr(node_acceptance, "read_states", lambda: failed)
        row = doctor._fleet_node_acceptance_check()
        assert row["status"] == "DEGRADED", row
        assert "not counted" not in row["detail"]

    def test_mcp_actions_start_and_read(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        token = config.agent_identity_var.set("Desktop_Agent1")
        try:
            started = coordination_manage("node_acceptance_start", {"node": NODE})
            assert started["state"] == "pending"
            assert agent.run_once() == "ran"
            assert agent.run_once() == "ran"
            node_acceptance.attest(
                NODE, started["run_id"], "test:independent-observation", actor="Operator"
            )
            assert coordination_manage("node_acceptance_status", {"node": NODE})["state"] == (
                "accepted"
            )
            summary = coordination_manage("node_acceptance_status", {})
            assert summary["nodes"][NODE]["state"] == "accepted"
            assert coordination_manage("node_acceptance_evaluate", {"node": NODE})["state"] == (
                "accepted"
            )
        finally:
            config.agent_identity_var.reset(token)


class TestFailuresAndRefusals:
    def test_cancel_that_never_reaches_the_device_fails_acceptance(self, hub, tmp_path):
        # No heartbeat runs during the hold, so the hub's cancel is never relayed.
        agent, _, _ = _agent(hub, tmp_path, relay_cancel=False)
        agent.run_once()
        _run_acceptance(hub, agent)
        status = _status(hub)
        assert status["state"] == "failed"
        assert status["checks"]["receipt"]["status"] == "pass"
        assert status["checks"]["cancel_confirmed"]["status"] == "fail"
        assert status["reason"].startswith("cancel_confirmed")
        row = doctor._fleet_node_acceptance_check()
        assert row["status"] == "DEGRADED" and "failed" in row["detail"]

    def test_pending_run_refuses_a_second_start_then_times_out(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        first = _start(hub)
        assert first.status_code == 201
        again = _start(hub)
        assert again.status_code == 409 and "still pending" in again.json()["error"]
        late = datetime.now(UTC) + timedelta(seconds=node_acceptance.DEADLINE_S + 60)
        timed_out = node_acceptance.evaluate(NODE, now=late)
        assert timed_out["state"] == "failed"
        assert timed_out["reason"].startswith("timed_out")
        # Nothing ran, so the timeout does not blame a missing attestation.
        assert "awaiting_operator_attestation" not in timed_out["reason"]
        # A restart cancels the old run's unclaimed items so they never execute.
        restarted = _start(hub)
        assert restarted.status_code == 201, restarted.json()
        old = first.json()
        assert node_queue.get_item(old["complete_item_id"])["cancel_requested"] == 1

    def test_another_agent_cannot_start_acceptance_on_this_node(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        resp = _start(hub, token=CODEX_TOKEN)
        assert resp.status_code == 403, resp.json()
        assert node_acceptance.get(NODE) is None

    def test_node_without_the_backend_is_refused(self, hub, tmp_path):
        node_queue.register(NODE, identity="Desktop_Agent1", facts={"backends": ["codex"]})
        resp = _start(hub)
        assert resp.status_code == 409
        assert "acceptance" in resp.json()["error"]


class TestRevocation:
    def test_probe_refuses_foreign_agent_device_and_revoked_label(self, hub):
        node_queue.register(
            NODE,
            identity="Desktop_Agent1",
            facts={"backends": ["acceptance"]},
            device_node="desk-tailnet",
        )
        probe = node_acceptance.revocation_probe(NODE)
        assert probe["status"] == "pass", probe
        for label in ("foreign_agent", "foreign_device", "revoked_agent"):
            assert label in probe["detail"]

    def test_revoked_agent_node_claim_is_refused(self, hub, tmp_path, monkeypatch):
        agent, _, root = _agent(hub, tmp_path)
        agent.run_once()
        node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="Operator"
        )
        monkeypatch.setenv("UA_REVOKED_AGENTS", "Desktop_Agent1")
        resp = hub.post("/api/node/claim", json={"node": NODE, "wait_s": 0}, headers=_headers())
        assert resp.status_code == 403
        assert "revoked" in resp.json()["error"]
        with pytest.raises(node_queue.NodeError) as refused:
            node_queue.heartbeat(NODE, identity="Desktop_Agent1")
        assert refused.value.status == 403


class TestGate:
    def test_gate_off_lets_unaccepted_nodes_take_work(self, hub, tmp_path):
        agent, _, root = _agent(hub, tmp_path)
        agent.run_once()
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="Operator"
        )
        assert item["status"] == "queued"
        assert node_queue.select_node_for_agent("Desktop_Agent1", "codex")["ok"] is True

    def test_gate_on_holds_real_work_until_accepted(self, hub, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_NODE_REQUIRE_ACCEPTANCE", "1")
        agent, fake, root = _agent(hub, tmp_path)
        agent.run_once()
        with pytest.raises(node_queue.NodeError, match="device acceptance") as refused:
            node_queue.enqueue(
                NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="Operator"
            )
        assert refused.value.status == 409
        assert "node_acceptance_start" in str(refused.value)
        approvals_before = _approval_count()
        with pytest.raises(node_queue.NodeError, match="device acceptance"):
            node_queue.request_dispatch(
                NODE, actor="Operator", prompt="p", cwd=str(root / "repo"), backend="codex"
            )
        assert _approval_count() == approvals_before  # refused before the approval gate
        sel = node_queue.select_node_for_agent("Desktop_Agent1", "codex")
        assert sel == {"routed": True, "ok": False, "error": f"node_not_accepted:{NODE}"}

        # The acceptance job itself is exempt, and passing it opens the gate.
        _run_acceptance(hub, agent)
        assert _status(hub)["state"] == "accepted"
        item = node_queue.enqueue(
            NODE, prompt="p", cwd=str(root / "repo"), backend="codex", created_by="Operator"
        )
        assert item["status"] == "queued"
        assert node_queue.select_node_for_agent("Desktop_Agent1", "codex")["ok"] is True
        row = doctor._fleet_node_acceptance_check()
        assert row["status"] == "LIVE"
        assert fake.calls == []

    def test_gate_on_doctor_degrades_for_an_unaccepted_node(self, hub, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_NODE_REQUIRE_ACCEPTANCE", "1")
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        row = doctor._fleet_node_acceptance_check()
        assert row["status"] == "DEGRADED"
        assert "UA_NODE_REQUIRE_ACCEPTANCE=1" in row["gate"]


def _approval_count() -> int:
    conn = storage.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
    finally:
        conn.close()


# --- the device-side backend on its own -----------------------------------------


def _spec(mode="complete", **extra):
    return json.dumps({"acceptance": mode, "run": "acc-" + "0" * 16, **extra})


class TestAcceptanceBackend:
    def test_cancel_mid_run_reports_a_confirmed_stop(self, tmp_path):
        events: list[dict] = []
        checks = {"n": 0}

        def kill_check():
            checks["n"] += 1
            return checks["n"] > 4  # the cancel lands during the second step

        backend = acceptance_backend.AcceptanceBackend(sleep=lambda s: None)
        result = backend.run(
            prompt=_spec("cancel", steps=3, step_s=1, hold_s=60),
            session_dir=str(tmp_path),
            kill_check=kill_check,
            on_event=events.append,
        )
        assert result["status"] == "killed"
        kinds = [e["kind"] for e in events]
        assert kinds[0] == "acceptance_launch"
        assert kinds[-1] == "acceptance_stopped"
        assert events[-1]["data"]["confirmed"] is True
        # It stopped before finishing its steps, and wrote no output.
        assert kinds.count("acceptance_progress") < 3
        assert not (tmp_path / acceptance_backend.OUTPUT_FILE).exists()

    def test_cancel_job_without_a_cancel_is_an_error(self, tmp_path):
        backend = acceptance_backend.AcceptanceBackend(sleep=lambda s: None)
        result = backend.run(
            prompt=_spec("cancel", steps=1, step_s=0, hold_s=2),
            session_dir=str(tmp_path),
            kill_check=lambda: False,
        )
        assert result == {"status": "error", "error": "acceptance_cancel_not_received"}

    def test_complete_job_writes_bounded_output_and_starts_no_process(self, tmp_path, monkeypatch):
        def no_process(*_a, **_k):
            raise AssertionError("the acceptance job must not start a process")

        monkeypatch.setattr(subprocess, "Popen", no_process)
        backend = acceptance_backend.AcceptanceBackend(sleep=lambda s: None)
        result = backend.run(
            prompt=_spec(steps=2), session_dir=str(tmp_path), kill_check=lambda: False
        )
        assert result["status"] == "success"
        data = (tmp_path / acceptance_backend.OUTPUT_FILE).read_bytes()
        assert len(data) <= acceptance_backend.MAX_OUTPUT_BYTES
        assert result["text"] == acceptance_backend.receipt_text("acc-" + "0" * 16, data)

    @pytest.mark.parametrize(
        "prompt",
        [
            "not json",
            json.dumps({"acceptance": "complete", "run": "acc-" + "0" * 16, "shell": "rm"}),
            json.dumps({"acceptance": "explode", "run": "acc-" + "0" * 16}),
            json.dumps({"acceptance": "complete", "run": "../x"}),
            _spec(steps=99),
            _spec(hold_s=10_000),
        ],
    )
    def test_invalid_spec_is_refused(self, tmp_path, prompt):
        backend = acceptance_backend.AcceptanceBackend(sleep=lambda s: None)
        result = backend.run(prompt=prompt, session_dir=str(tmp_path), kill_check=lambda: False)
        assert result["status"] == "error"
        assert result["error"].startswith("acceptance_spec_invalid")

    def test_every_node_advertises_the_builtin_backend(self, tmp_path):
        facts = node_agent.collect_facts(
            roots=[tmp_path],
            backends=("codex", node_agent.ACCEPTANCE_BACKEND),
            which=lambda _b: None,
            run=lambda *a, **k: None,
        )
        assert facts["backends"] == ["acceptance"]
        agent = node_agent.NodeAgent(node=NODE, transport=lambda *a: (200, {}), backends=("codex",))
        assert agent.backends == ("codex", "acceptance")


class TestRecoveredReviewBoundaries:
    def test_diagnostics_require_independent_run_bound_attestation(
        self, hub, tmp_path, monkeypatch
    ):
        agent, _, root = _agent(hub, tmp_path)
        agent.run_once()
        started = _run_acceptance(hub, agent, attest=False)
        status = _status(hub)
        assert status["state"] == "pending"
        assert status["diagnostics_passed"] is True
        assert status["evidence_trust"] == "node_self_report"
        assert status["reason"].startswith("awaiting_operator_attestation")
        monkeypatch.setenv("UA_NODE_REQUIRE_ACCEPTANCE", "1")
        with pytest.raises(node_queue.NodeError, match="device acceptance"):
            node_queue.enqueue(
                NODE, created_by="Operator", backend="codex", prompt="real", cwd=str(root / "repo")
            )
        with pytest.raises(node_queue.NodeError, match="independent lifecycle operator"):
            node_acceptance.attest(NODE, started["run_id"], "test:report", actor="Desktop_Agent1")
        monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Desktop_Agent1,Operator")
        with pytest.raises(node_queue.NodeError, match="own agent"):
            node_acceptance.attest(NODE, started["run_id"], "test:report", actor="Desktop_Agent1")
        with pytest.raises(node_queue.NodeError, match="exact pending"):
            node_acceptance.attest(NODE, "acc-" + "f" * 16, "test:report", actor="Operator")
        accepted = node_acceptance.attest(
            NODE, started["run_id"], "test:operator-observed-device", actor="Operator"
        )
        assert accepted["state"] == "accepted" and accepted["attested_by"] == "Operator"
        assert (
            node_queue.enqueue(
                NODE, created_by="Operator", backend="codex", prompt="real", cwd=str(root / "repo")
            )["status"]
            == "queued"
        )

    def test_enabling_gate_holds_old_work_and_claims_diagnostics_behind_it(
        self, hub, tmp_path, monkeypatch
    ):
        agent, fake, root = _agent(hub, tmp_path)
        agent.run_once()
        old = node_queue.enqueue(
            NODE, created_by="Operator", backend="codex", prompt="held", cwd=str(root / "repo")
        )
        monkeypatch.setenv("UA_NODE_REQUIRE_ACCEPTANCE", "1")
        assert agent.run_once() == "idle"
        run = _run_acceptance(hub, agent, attest=False)
        assert node_queue.get_item(old["item_id"])["status"] == "queued"
        assert fake.calls == []
        node_acceptance.attest(NODE, run["run_id"], "test:operator-observation", actor="Operator")
        assert agent.run_once() == "ran"
        assert len(fake.calls) == 1

    def test_rerun_holds_previously_admitted_work(self, hub, tmp_path, monkeypatch):
        agent, fake, root = _agent(hub, tmp_path)
        agent.run_once()
        _run_acceptance(hub, agent)
        monkeypatch.setenv("UA_NODE_REQUIRE_ACCEPTANCE", "1")
        old = node_queue.enqueue(
            NODE, created_by="Operator", backend="codex", prompt="held", cwd=str(root / "repo")
        )
        started = _start(hub).json()
        claim = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        assert claim["item_id"] == started["complete_item_id"]
        assert node_queue.get_item(old["item_id"])["status"] == "queued"
        assert fake.calls == []

    def test_http_and_mcp_obey_action_policy_deny(self, hub, tmp_path, monkeypatch):
        from mcp.server.mcpserver.exceptions import ToolError

        from unified_agent import policy

        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        path = tmp_path / "policy.yaml"
        path.write_text(
            "enabled: true\nrules:\n  - name: no_acceptance\n"
            "    tool: node_acceptance_start\n    type: tool\n    action: deny\n"
        )
        monkeypatch.setattr(config, "policy_file_path", lambda: path)
        token = config.agent_identity_var.set("Desktop_Agent1")
        try:
            with pytest.raises((policy.PolicyViolation, ToolError)):
                coordination_manage("node_acceptance_start", {"node": NODE})
        finally:
            config.agent_identity_var.reset(token)
        response = _start(hub)
        assert response.status_code == 403, response.json()
        assert node_acceptance.get(NODE) is None

    def test_concurrent_start_publishes_exactly_one_pair(self, hub, tmp_path, monkeypatch):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        barrier = Barrier(2)
        evaluate = node_acceptance.evaluate

        def synchronized(*args, **kwargs):
            result = evaluate(*args, **kwargs)
            barrier.wait(timeout=15)
            return result

        monkeypatch.setattr(node_acceptance, "evaluate", synchronized)

        def start():
            try:
                return node_acceptance.start(NODE, actor="Desktop_Agent1")
            except node_queue.NodeError as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(start) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        assert len([result for result in results if result == 409]) == 1
        conn = storage.connect()
        try:
            items = conn.execute(
                "SELECT id FROM node_dispatch WHERE backend='acceptance'"
            ).fetchall()
        finally:
            conn.close()
        row = node_acceptance.get(NODE)
        assert {item["id"] for item in items} == {row["complete_item_id"], row["cancel_item_id"]}

    def test_second_insert_failure_rolls_back_tracking_and_both_items(
        self, hub, tmp_path, monkeypatch
    ):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        enqueue = node_queue.enqueue
        calls = []

        def fail_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("injected second insert failure")
            return enqueue(*args, **kwargs)

        monkeypatch.setattr(node_queue, "enqueue", fail_second)
        with pytest.raises(RuntimeError, match="injected"):
            node_acceptance.start(NODE, actor="Desktop_Agent1")
        assert node_acceptance.get(NODE) is None
        conn = storage.connect()
        try:
            assert conn.execute("SELECT COUNT(*) FROM node_dispatch").fetchone()[0] == 0
        finally:
            conn.close()

    def test_duplicate_steps_and_stop_in_same_batch_never_pass(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        started = _start(hub).json()
        run = started["run_id"]
        complete = node_queue.claim_next(NODE, identity="Desktop_Agent1")

        def event(kind, **data):
            return {"kind": kind, "data": {"run": run, **data}}

        node_queue.record_node_event(
            NODE,
            complete["item_id"],
            [
                event("acceptance_launch"),
                *[event("acceptance_progress", step=1, of=3)] * 3,
            ],
            identity="Desktop_Agent1",
        )
        result = node_acceptance.evaluate(NODE)
        assert result["checks"]["progress"]["status"] != "pass"
        cancel = node_queue.claim_next(NODE, identity="Desktop_Agent1")
        node_queue.record_node_event(
            NODE,
            cancel["item_id"],
            [
                event("acceptance_launch"),
                event("acceptance_progress", step=1, of=2),
                event("acceptance_stopped", confirmed=True, at_step=1),
            ],
            identity="Desktop_Agent1",
        )
        node_queue.heartbeat(NODE, identity="Desktop_Agent1", running=cancel["item_id"])
        node_queue.post_receipt(
            NODE,
            cancel["item_id"],
            identity="Desktop_Agent1",
            receipt={"status": "killed", "reason": "cancelled_by_hub"},
        )
        result = node_acceptance.evaluate(NODE)
        assert result["checks"]["cancel_confirmed"]["status"] == "fail"
        assert result["state"] == "failed"

    def test_late_success_cannot_resurrect_a_timed_out_run(self, hub, tmp_path, monkeypatch):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        run = _run_acceptance(hub, agent, attest=False)
        late = datetime.now(UTC) + timedelta(seconds=node_acceptance.DEADLINE_S + 1)
        failed = node_acceptance.evaluate(NODE, now=late)
        assert failed["state"] == "failed" and failed["reason"].startswith("timed_out")
        # Every diagnostic passed: the reason names the missing attestation.
        assert "awaiting_operator_attestation" in failed["reason"]
        # All late evidence still matches; a prior terminal timeout wins.
        assert failed["diagnostics_passed"] is True
        assert node_acceptance.evaluate(NODE)["state"] == "failed"
        with pytest.raises(node_queue.NodeError, match="exact pending"):
            node_acceptance.attest(NODE, run["run_id"], "test:late-observation", actor="Operator")

    def test_diagnostics_finishing_after_the_deadline_do_not_blame_attestation(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        _run_acceptance(hub, agent, attest=False)
        # The last evaluation inside the deadline still had the receipt
        # pending; it only passes when re-evaluated after the deadline.
        stored = node_acceptance.get(NODE)["checks"]
        stored["receipt"] = {"status": "pending", "detail": "no receipt yet"}
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_acceptance SET checks_json=? WHERE node=?",
                (json.dumps(stored), NODE),
            )
            conn.commit()
        finally:
            conn.close()
        late = datetime.now(UTC) + timedelta(seconds=node_acceptance.DEADLINE_S + 1)
        failed = node_acceptance.evaluate(NODE, now=late)
        assert failed["state"] == "failed" and failed["reason"].startswith("timed_out")
        assert failed["checks"]["receipt"]["status"] == "pass"
        assert "awaiting_operator_attestation" not in failed["reason"]

    def test_stored_summary_names_a_missing_attestation_after_the_deadline(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        _run_acceptance(hub, agent, attest=False)
        assert node_acceptance.get(NODE)["state"] == "pending"
        late = datetime.now(UTC) + timedelta(seconds=node_acceptance.DEADLINE_S + 1)
        summary = node_acceptance.read_states(now=late)[NODE]
        assert summary["state"] == "failed"
        assert "awaiting_operator_attestation" in summary["reason"]
        assert "every diagnostic passed" in summary["reason"]

    def test_periodic_sweep_stops_expired_offline_diagnostics(self, hub, tmp_path):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        run = _start(hub).json()
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE node_acceptance SET started_at=? WHERE node=?",
                ((datetime.now(UTC) - timedelta(seconds=1000)).isoformat(), NODE),
            )
            conn.commit()
        finally:
            conn.close()
        server._node_wait_sweep_once()
        assert node_acceptance.get(NODE)["state"] == "failed"
        for key in ("complete_item_id", "cancel_item_id"):
            assert node_queue.get_item(run[key])["cancel_requested"] == 1
        assert node_queue.claim_next(NODE, identity="Desktop_Agent1") is None

    def test_output_symlink_never_modifies_target(self, tmp_path):
        target = tmp_path / "outside.txt"
        target.write_bytes(b"preserve")
        session = tmp_path / "session"
        session.mkdir()
        (session / acceptance_backend.OUTPUT_FILE).symlink_to(target)
        result = acceptance_backend.AcceptanceBackend(sleep=lambda _: None).run(
            prompt=_spec(step_s=0), session_dir=str(session), kill_check=lambda: False
        )
        assert result["status"] == "error"
        assert target.read_bytes() == b"preserve"

    def test_hosted_defaults_acceptance_on_and_refuses_bearer_override(self, monkeypatch):
        monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_public")
        monkeypatch.delenv("UA_NODE_REQUIRE_ACCEPTANCE", raising=False)
        monkeypatch.setenv("UA_NODE_ALLOW_BEARER", "1")
        assert config.node_require_acceptance() is True
        assert config.node_allow_bearer() is False

    def test_new_device_binding_cannot_inherit_operator_attestation(
        self, hub, tmp_path, monkeypatch
    ):
        agent, _, root = _agent(hub, tmp_path)
        agent.run_once()
        _run_acceptance(hub, agent)
        monkeypatch.setenv("UA_NODE_REQUIRE_ACCEPTANCE", "1")
        conn = storage.connect()
        try:
            conn.execute("UPDATE nodes SET device_node='another-device' WHERE node=?", (NODE,))
            conn.commit()
        finally:
            conn.close()
        summary = node_acceptance.read_states()[NODE]
        assert summary["state"] == "failed" and "binding changed" in summary["reason"]
        assert doctor._fleet_node_acceptance_check()["status"] == "DEGRADED"
        with pytest.raises(node_queue.NodeError, match="device acceptance"):
            node_queue.enqueue(
                NODE, created_by="Operator", backend="codex", prompt="real", cwd=str(root / "repo")
            )
        assert node_acceptance.evaluate(NODE)["state"] == "failed"

    @pytest.mark.parametrize(
        "indicator,value", [("UA_HOSTED", "1"), ("RAILWAY_ENVIRONMENT_ID", "test-env")]
    )
    def test_hosted_indicator_overrides_private_bearer_compatibility(
        self, monkeypatch, indicator, value
    ):
        monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_private")
        monkeypatch.delenv("UA_PUBLIC_URL", raising=False)
        monkeypatch.delenv("UA_NODE_REQUIRE_ACCEPTANCE", raising=False)
        monkeypatch.delenv("UA_HOSTED", raising=False)
        monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
        monkeypatch.setenv(indicator, value)
        monkeypatch.setenv("UA_NODE_ALLOW_BEARER", "1")
        assert config.node_require_acceptance() is True
        assert config.node_allow_bearer() is False

    def test_hosted_legacy_unbound_node_cannot_claim_or_post_until_bound_reregistration(
        self, hub, tmp_path, monkeypatch
    ):
        agent, _, _ = _agent(hub, tmp_path)
        agent.run_once()
        _run_acceptance(hub, agent)
        monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_public")
        item_id = "nd-" + "0" * 16
        operations = [
            lambda: node_queue.claim_next(
                NODE, identity="Desktop_Agent1", device_node="signed-device"
            ),
            lambda: node_queue.heartbeat(
                NODE, identity="Desktop_Agent1", device_node="signed-device"
            ),
            lambda: node_queue.post_receipt(
                NODE,
                item_id,
                identity="Desktop_Agent1",
                device_node="signed-device",
                receipt={"status": "finished"},
            ),
            lambda: node_queue.record_node_event(
                NODE, item_id, [], identity="Desktop_Agent1", device_node="signed-device"
            ),
            lambda: node_queue.register("new-unbound", identity="Desktop_Agent1"),
        ]
        for operation in operations:
            with pytest.raises(node_queue.NodeError, match="device-bound") as refused:
                operation()
            assert refused.value.status == 403
        node_queue.register(
            NODE, identity="Desktop_Agent1", facts=_facts(tmp_path), device_node="signed-device"
        )
        assert node_acceptance.evaluate(NODE)["state"] == "failed"
        assert (
            node_queue.heartbeat(NODE, identity="Desktop_Agent1", device_node="signed-device")[
                "node"
            ]
            == NODE
        )
        with pytest.raises(node_queue.NodeError, match="different device"):
            node_queue.register(NODE, identity="Desktop_Agent1", device_node="foreign-device")
        with pytest.raises(node_queue.NodeError, match="different agent"):
            node_queue.register(NODE, identity="Desktop_Agent2", device_node="signed-device")

    def test_hosted_bound_registration_still_requires_the_bound_token(self, hub, monkeypatch):
        monkeypatch.setenv("UA_DEPLOYMENT_MODE", "authenticated_public")
        node_queue.register(NODE, identity="Desktop_Agent1", device_node="signed-device")
        with pytest.raises(node_queue.NodeError, match="device-bound"):
            node_queue.heartbeat(NODE, identity="Desktop_Agent1")
        with pytest.raises(node_queue.NodeError, match="different device"):
            node_queue.claim_next(NODE, identity="Desktop_Agent1", device_node="foreign-device")
        # A genuine new device registration is usable under the matching token.
        node_queue.register(NODE, identity="Desktop_Agent1", device_node="signed-device")
        assert (
            node_queue.claim_next(NODE, identity="Desktop_Agent1", device_node="signed-device")
            is None
        )
