"""Team snapshot, read-only preview and restore (task #1738).

A test-double harness stands in for the ua-node queue: node items, their resume
options and the node registry are fixtures, and resume/dispatch calls are
recorded instead of reaching a device.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, node_queue, node_registry, node_resume, storage, team_snapshot
from unified_agent.tools import tasks as task_tools
from unified_agent.tools.approvals import ApprovalPendingError
from unified_agent.tools.graph import coordination_manage

ME = "Desktop_Agent1"


def _item(
    item_id,
    *,
    status="finished",
    node="desk",
    task_id=None,
    sid="sess-1",
    backend="acp:claude",
    created_by=ME,
    cwd_realpath=None,
):
    # The node's agent label differs from the caller: ownership is the identity
    # that started the item (created_by), as node_resume decides.
    return {
        "id": item_id,
        "node": node,
        "agent": "Desktop_agent3",
        "created_by": created_by,
        "backend": backend,
        "model": "fixture-model",
        "cwd": "/work/repo",
        "cwd_realpath": cwd_realpath,
        "status": status,
        "task_id": task_id,
        "session_id": sid,
        "permission_mode": "supervised",
        "hermes_profile": None,
        "profile_agent": None,
        "timeout_s": 600,
    }


@pytest.fixture
def harness(monkeypatch):
    storage.init_db()
    token = config.agent_identity_var.set(ME)
    items: dict[str, dict] = {}
    resumable: set[str] = set()
    # A fresh heartbeat per test: the node liveness window is short.
    nodes = {"desk": {"last_heartbeat": datetime.now(UTC).isoformat(), "heartbeat_floor": None}}
    calls: list[tuple] = []
    behaviour: dict[str, object] = {}
    busy: set[str] = set()
    sessions: dict[str, dict] = {}

    monkeypatch.setattr(
        node_queue, "get_item", lambda item_id: dict(items[item_id]) if item_id in items else None
    )

    def annotate(rows):
        for row in rows:
            ok = row["id"] in resumable
            row["resume"] = {
                "available": ok,
                "mode": "acp_load" if ok else None,
                "label": "Resume (ACP session/load)" if ok else None,
                "reason": None
                if ok
                else "The agent advertised neither session/resume nor session/load",
            }

    monkeypatch.setattr(node_resume, "annotate_items", annotate)
    monkeypatch.setattr(
        node_resume,
        "_open_resume_item",
        lambda conn, sid: {"id": f"resume-{sid}", "status": "queued"} if sid in busy else None,
    )
    monkeypatch.setattr(node_resume, "_session_row", lambda conn, sid: sessions.get(sid))
    monkeypatch.setattr(node_registry, "list_nodes", lambda: nodes)

    def fake_resume(item_id, *, prompt, actor, operator=False):
        calls.append(("resume", item_id, prompt, operator))
        if behaviour.get("resume") == "pending":
            raise ApprovalPendingError("pending", approval_id=7)
        if behaviour.get("resume") == "fail":
            raise node_registry.NodeError("cannot resume: agent refused session/load", status=409)
        return {"item_id": f"new-{item_id}", "status": "queued"}

    def fake_dispatch(node, *, actor, **fields):
        calls.append(("dispatch", node, fields))
        if behaviour.get("dispatch") == "pending":
            raise ApprovalPendingError("pending", approval_id=42)
        return {"item_id": f"new-{node}-{len(calls)}", "status": "queued"}

    monkeypatch.setattr(node_resume, "resume", fake_resume)
    monkeypatch.setattr(node_queue, "request_dispatch", fake_dispatch)
    yield {
        "items": items,
        "resumable": resumable,
        "nodes": nodes,
        "calls": calls,
        "behaviour": behaviour,
        "busy": busy,
        "sessions": sessions,
    }
    config.agent_identity_var.reset(token)


def _event_count() -> int:
    with storage.connect() as conn:
        return conn.execute("SELECT count(*) FROM event_log").fetchone()[0]


def test_snapshot_captures_each_seat_without_transcripts(harness):
    task = task_tools.task_add("seat work", notes="context for the packet")["id"]
    harness["items"]["it-1"] = _item("it-1", task_id=task)
    harness["resumable"].add("it-1")
    snap = team_snapshot.snapshot(item_ids=["it-1", "it-1"])
    assert len(snap["seats"]) == 1
    seat = snap["seats"][0]
    assert seat["node"] == "desk" and seat["backend"] == "acp:claude"
    assert seat["session_id"] == "sess-1" and seat["task_id"] == task
    assert seat["resume"] == {"available": True, "mode": "acp_load", "reason": None}
    # The bounded packet is task context, never a transcript.
    assert seat["packet"]["labels"]["transcript_included"] is False
    assert seat["packet"]["task"]["id"] == task
    stored = team_snapshot.get_snapshot(snap["snapshot_id"])
    assert stored["seats"] == snap["seats"]
    assert team_snapshot.list_snapshots()[0]["snapshot_id"] == snap["snapshot_id"]


def test_snapshot_needs_exactly_one_selector_and_an_identity(harness):
    with pytest.raises(ToolError, match="exactly one"):
        team_snapshot.snapshot()
    token = config.agent_identity_var.set(None)
    try:
        with pytest.raises(ToolError, match="verified identity"):
            team_snapshot.snapshot(item_ids=["x"])
    finally:
        config.agent_identity_var.reset(token)


def _all_outcomes(harness):
    task = task_tools.task_add("packet task")["id"]
    other = task_tools.task_add("held by another")["id"]
    with storage.connect() as conn:
        conn.execute(
            "UPDATE tasks SET claimed_by='Desktop_Agent2', lock_expires_at=? WHERE id=?",
            ((datetime.now(UTC) + timedelta(hours=1)).isoformat(), other),
        )
    items = harness["items"]
    items["resumable"] = _item("resumable", task_id=task)
    harness["resumable"].add("resumable")
    items["rebuild"] = _item("rebuild", task_id=task, backend="acp:gemini")
    items["fresh"] = _item("fresh", task_id=None)
    items["offline"] = _item("offline", node="gone")
    items["claimed"] = _item("claimed", task_id=other)
    items["running"] = _item("running", status="claimed")
    ids = ["resumable", "rebuild", "fresh", "offline", "claimed", "running", "never-recorded"]
    return team_snapshot.snapshot(item_ids=ids)


def test_preview_lists_every_outcome_and_changes_nothing(harness):
    snap = _all_outcomes(harness)
    before = _event_count()
    plan = {seat["item_id"]: seat for seat in team_snapshot.preview(snap["snapshot_id"])["seats"]}
    assert {k: v["planned"] for k, v in plan.items()} == {
        "resumable": "resumed",
        "rebuild": "rebuilt",
        "fresh": "fresh",
        "offline": "failed",
        "claimed": "failed",
        "running": "live",
        "never-recorded": "failed",
    }
    assert "offline" in plan["offline"]["reason"]
    assert "claimed by Desktop_Agent2" in plan["claimed"]["reason"]
    assert _event_count() == before
    assert harness["calls"] == []


def test_restore_reports_honest_outcomes_through_existing_paths(harness):
    snap = _all_outcomes(harness)
    result = team_snapshot.restore(snap["snapshot_id"])
    outcomes = {seat["item_id"]: seat["outcome"] for seat in result["seats"]}
    assert outcomes["resumable"] == "resumed"
    assert outcomes["rebuild"] == "rebuilt"
    assert outcomes["fresh"] == "fresh"
    assert outcomes["offline"] == outcomes["claimed"] == "failed"
    assert outcomes["running"] == "live"
    kinds = [call[0] for call in harness["calls"]]
    assert kinds.count("resume") == 1 and kinds.count("dispatch") == 2
    rebuilt_prompt = next(
        c[2]["prompt"]
        for c in harness["calls"]
        if c[0] == "dispatch" and "packet" in c[2]["prompt"]
    )
    assert "session packet" in rebuilt_prompt
    # Every dispatch stays on the recorded node and place.
    assert all(c[1] == "desk" for c in harness["calls"] if c[0] == "dispatch")
    assert result["summary"]["resumed"] == 1 and result["summary"]["failed"] == 3


def test_restore_keeps_the_checkout_realpath_binding(harness):
    """#2011: a bound seat restored fresh or rebuilt re-binds its planned checkout.

    Without it, a cwd symlink retargeted to another in-root checkout after the
    restore approval would launch there with no cwd_realpath_changed refusal.
    """
    task = task_tools.task_add("bound seat")["id"]
    harness["items"]["fresh"] = _item("fresh", cwd_realpath="/work/checkouts/a")
    harness["items"]["rebuild"] = _item("rebuild", task_id=task, cwd_realpath="/work/checkouts/a")
    snap = team_snapshot.snapshot(item_ids=["fresh", "rebuild"])
    assert {seat["cwd_realpath"] for seat in snap["seats"]} == {"/work/checkouts/a"}
    plan = team_snapshot.preview(snap["snapshot_id"])["seats"]
    assert {seat["cwd_realpath"] for seat in plan} == {"/work/checkouts/a"}
    result = team_snapshot.restore(snap["snapshot_id"])
    assert {seat["outcome"] for seat in result["seats"]} == {"fresh", "rebuilt"}
    dispatched = [c[2] for c in harness["calls"] if c[0] == "dispatch"]
    assert len(dispatched) == 2
    assert all(fields["cwd_realpath"] == "/work/checkouts/a" for fields in dispatched)


def test_restore_of_a_legacy_snapshot_rebinds_from_the_original_item(harness, monkeypatch):
    """A snapshot taken before seats recorded cwd_realpath still restores bound."""
    harness["items"]["fresh"] = _item("fresh", cwd_realpath="/work/checkouts/a")
    snap = team_snapshot.snapshot(item_ids=["fresh"])
    real = team_snapshot.get_snapshot

    def legacy(snapshot_id):
        stored = real(snapshot_id)
        for seat in stored["seats"]:
            seat.pop("cwd_realpath", None)  # recorded before seats carried it
        return stored

    monkeypatch.setattr(team_snapshot, "get_snapshot", legacy)
    assert "cwd_realpath" not in team_snapshot.get_snapshot(snap["snapshot_id"])["seats"][0]
    team_snapshot.restore(snap["snapshot_id"])
    (fields,) = [c[2] for c in harness["calls"] if c[0] == "dispatch"]
    assert fields["cwd_realpath"] == "/work/checkouts/a"


def test_a_failed_resume_never_falls_back_to_a_fresh_session(harness):
    harness["items"]["r"] = _item("r")
    harness["resumable"].add("r")
    harness["behaviour"]["resume"] = "fail"
    snap = team_snapshot.snapshot(item_ids=["r"])
    seat = team_snapshot.restore(snap["snapshot_id"])["seats"][0]
    assert seat["outcome"] == "failed" and "session/load" in seat["reason"]
    assert [c[0] for c in harness["calls"]] == ["resume"]


def test_approval_gated_dispatch_is_reported_as_waiting(harness):
    harness["items"]["f"] = _item("f", task_id=None)
    harness["behaviour"]["dispatch"] = "pending"
    snap = team_snapshot.snapshot(item_ids=["f"])
    result = team_snapshot.restore(snap["snapshot_id"])
    seat = result["seats"][0]
    assert seat["outcome"] == "awaiting_approval" and seat["awaiting_approval"] == 42
    assert seat["planned"] == "fresh"
    # Waiting is counted on its own, never as restored.
    assert result["summary"]["awaiting_approval"] == 1 and result["summary"]["fresh"] == 0


def test_approval_gated_resume_is_not_reported_as_resumed(harness):
    harness["items"]["r"] = _item("r")
    harness["resumable"].add("r")
    harness["behaviour"]["resume"] = "pending"
    snap = team_snapshot.snapshot(item_ids=["r"])
    result = team_snapshot.restore(snap["snapshot_id"])
    seat = result["seats"][0]
    assert (seat["planned"], seat["outcome"], seat["awaiting_approval"]) == (
        "resumed",
        "awaiting_approval",
        7,
    )
    assert result["summary"]["resumed"] == 0


def test_a_busy_resume_plans_live_and_starts_nothing(harness):
    harness["items"]["b"] = _item("b", sid="sess-busy")
    harness["resumable"].add("b")
    snap = team_snapshot.snapshot(item_ids=["b"])
    # A resume of the same session was queued after the snapshot.
    harness["busy"].add("sess-busy")
    harness["resumable"].discard("b")
    seat = team_snapshot.preview(snap["snapshot_id"])["seats"][0]
    assert seat["planned"] == "live" and "already queued or running" in seat["reason"]
    restored = team_snapshot.restore(snap["snapshot_id"])["seats"][0]
    assert restored["outcome"] == "live"
    assert harness["calls"] == []


def test_a_seat_recorded_resumable_fails_rather_than_starting_fresh(harness):
    task = task_tools.task_add("packet task")["id"]
    harness["items"]["was"] = _item("was", task_id=task)
    harness["resumable"].add("was")
    snap = team_snapshot.snapshot(item_ids=["was"])
    assert snap["seats"][0]["packet"]  # a packet exists, yet it is not rebuilt
    harness["resumable"].discard("was")
    seat = team_snapshot.preview(snap["snapshot_id"])["seats"][0]
    assert seat["planned"] == "failed"
    assert "can no longer be resumed" in seat["reason"] and "session/load" in seat["reason"]
    assert team_snapshot.restore(snap["snapshot_id"])["seats"][0]["outcome"] == "failed"
    assert harness["calls"] == []


def _claim(task_id, holder, *, hours):
    with storage.connect() as conn:
        conn.execute(
            "UPDATE tasks SET claimed_by=?, lock_expires_at=? WHERE id=?",
            (holder, (datetime.now(UTC) + timedelta(hours=hours)).isoformat(), task_id),
        )


def test_another_identitys_seat_is_refused_on_every_path(harness):
    task = task_tools.task_add("other's task")["id"]
    items = harness["items"]
    items["theirs-resume"] = _item("theirs-resume", created_by="Desktop_agent3")
    harness["resumable"].add("theirs-resume")
    items["theirs-fresh"] = _item("theirs-fresh", created_by="Desktop_agent3")
    items["theirs-claimed-by-me"] = _item(
        "theirs-claimed-by-me", task_id=task, created_by="Desktop_agent3"
    )
    _claim(task, ME, hours=1)
    snap = team_snapshot.snapshot(
        item_ids=["theirs-resume", "theirs-fresh", "theirs-claimed-by-me"]
    )
    plan = {s["item_id"]: s for s in team_snapshot.preview(snap["snapshot_id"])["seats"]}
    assert plan["theirs-resume"]["planned"] == plan["theirs-fresh"]["planned"] == "failed"
    assert "belongs to Desktop_agent3" in plan["theirs-fresh"]["reason"]
    # The task's current claimant may restore the seat.
    assert plan["theirs-claimed-by-me"]["planned"] == "rebuilt"
    result = team_snapshot.restore(snap["snapshot_id"])
    outcomes = {s["item_id"]: s["outcome"] for s in result["seats"]}
    assert outcomes == {
        "theirs-resume": "failed",
        "theirs-fresh": "failed",
        "theirs-claimed-by-me": "rebuilt",
    }
    assert [c[0] for c in harness["calls"]] == ["dispatch"]


def test_a_claimant_who_does_not_own_the_session_cannot_plan_a_resume(harness):
    # node_resume admits only the session's owners or the operator, never the
    # task's claimant, so the preview must not promise a resume restore refuses.
    task = task_tools.task_add("claimed, not owned")["id"]
    harness["items"]["c"] = _item("c", task_id=task, created_by="Desktop_agent3")
    harness["resumable"].add("c")
    _claim(task, ME, hours=1)
    snap = team_snapshot.snapshot(item_ids=["c"])
    seat = team_snapshot.preview(snap["snapshot_id"])["seats"][0]
    assert seat["planned"] == "failed"
    assert "only that identity or the operator may resume it" in seat["reason"]
    assert team_snapshot.restore(snap["snapshot_id"])["seats"][0]["outcome"] == "failed"
    assert harness["calls"] == []


def test_an_adopted_sessions_starter_is_not_its_owner(harness):
    # As in node_resume: an adopted (native) session stays with its adopter.
    harness["items"]["adopted"] = _item("adopted", sid="sess-native")
    harness["sessions"]["sess-native"] = {"agent": "Desktop_agent3", "kind": "claude_native"}
    snap = team_snapshot.snapshot(item_ids=["adopted"])
    seat = team_snapshot.preview(snap["snapshot_id"])["seats"][0]
    assert seat["planned"] == "failed" and "belongs to Desktop_agent3" in seat["reason"]


def test_a_claim_held_by_anyone_else_fails_even_expired_or_the_seat_label(harness):
    live_seat = task_tools.task_add("held by the seat's own label")["id"]
    expired = task_tools.task_add("held by an expired lease")["id"]
    _claim(live_seat, "Desktop_agent3", hours=1)
    _claim(expired, "Desktop_Agent2", hours=-1)
    harness["items"]["label"] = _item("label", task_id=live_seat)
    harness["items"]["expired"] = _item("expired", task_id=expired)
    snap = team_snapshot.snapshot(item_ids=["label", "expired"])
    plan = {s["item_id"]: s for s in team_snapshot.preview(snap["snapshot_id"])["seats"]}
    assert plan["label"]["planned"] == "failed"
    assert "claimed by Desktop_agent3" in plan["label"]["reason"]
    assert plan["expired"]["planned"] == "failed"
    assert "claimed by Desktop_Agent2" in plan["expired"]["reason"]
    team_snapshot.restore(snap["snapshot_id"])
    assert harness["calls"] == []


def test_the_operator_path_may_restore_any_seat(harness):
    task = task_tools.task_add("held elsewhere")["id"]
    _claim(task, "Desktop_Agent2", hours=1)
    harness["items"]["op"] = _item("op", task_id=task, created_by="Desktop_agent3")
    harness["resumable"].add("op")
    snap = team_snapshot.snapshot(item_ids=["op"])
    assert team_snapshot.preview(snap["snapshot_id"])["seats"][0]["planned"] == "failed"
    planned = team_snapshot.preview_for(snap["snapshot_id"], operator=True)["seats"][0]
    assert planned["planned"] == "resumed"
    seat = team_snapshot.restore_for(snap["snapshot_id"], operator=True)["seats"][0]
    assert seat["outcome"] == "resumed"
    assert harness["calls"] == [("resume", "op", team_snapshot.RESUME_PROMPT, True)]


def test_team_mode_resolves_each_seat_to_its_latest_item(harness, monkeypatch):
    from unified_agent import team_policy, team_spec

    monkeypatch.setattr(
        team_policy,
        "load_specs",
        lambda: {
            "core": {
                "team": "core",
                "seats": [{"label": "Desktop_Agent2"}, {"label": "Desktop_Agent4"}],
            }
        },
    )
    monkeypatch.setattr(
        team_spec,
        "_latest_run",
        lambda label, team: {"item_id": "it-codex"} if label == "Desktop_Agent2" else None,
    )
    harness["items"]["it-codex"] = _item("it-codex")
    snap = team_snapshot.snapshot(team="core")
    labels = {seat["label"]: seat["recorded"] for seat in snap["seats"]}
    assert labels == {"Desktop_Agent2": True, "Desktop_Agent4": False}
    with pytest.raises(ToolError, match="no committed team spec"):
        team_snapshot.snapshot(team="missing")


def test_coordination_manage_exposes_the_actions(harness):
    harness["items"]["a"] = _item("a")
    snap = coordination_manage("team_snapshot", {"item_ids": ["a"]})
    plan = coordination_manage("team_restore_preview", {"snapshot_id": snap["snapshot_id"]})
    assert plan["seats"][0]["planned"] == "fresh"
    assert coordination_manage("team_snapshots", {})[0]["snapshot_id"] == snap["snapshot_id"]
    restored = coordination_manage("team_restore", {"snapshot_id": snap["snapshot_id"]})
    assert restored["seats"][0]["outcome"] == "fresh"
    assert all(len(c) < 4 or c[3] is False for c in harness["calls"])


def test_coordination_manage_cannot_claim_operator_standing(harness):
    # Operator standing comes only from the HTTP recovery channel; an MCP caller
    # that passes it is refused before anything is planned or dispatched.
    harness["items"]["x"] = _item("x", created_by="Desktop_agent3")
    harness["resumable"].add("x")
    snap = team_snapshot.snapshot(item_ids=["x"])
    for action in ("team_restore", "team_restore_preview"):
        with pytest.raises(ToolError, match="invalid params"):
            coordination_manage(action, {"snapshot_id": snap["snapshot_id"], "operator": True})
    assert harness["calls"] == []


# --- HTTP ----------------------------------------------------------------------------


@pytest.fixture
def http(harness, monkeypatch):
    import secrets

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from unified_agent import server, webui

    who = {"dev": "Desktop_Agent2", "op": "Desktop_Operator"}
    tokens = {label: secrets.token_urlsafe(24) for label in who.values()}
    monkeypatch.setenv("UA_AGENT_TOKENS", ",".join(f"{k}:{v}" for k, v in tokens.items()))
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_OPERATOR_LABEL", who["op"])
    with TestClient(server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))) as client:
        yield client, {k: {"Authorization": f"Bearer {tokens[v]}"} for k, v in who.items()}


def test_http_snapshot_preview_and_restore(http, harness):
    client, auth = http
    harness["items"]["h1"] = _item("h1")
    assert client.post("/api/teams/snapshots", json={"item_ids": ["h1"]}).status_code == 401
    taken = client.post("/api/teams/snapshots", headers=auth["dev"], json={"item_ids": ["h1"]})
    assert taken.status_code == 201, taken.text
    sid = taken.json()["snapshot_id"]
    listed = client.get("/api/teams/snapshots", headers=auth["dev"]).json()["snapshots"]
    assert listed[0]["snapshot_id"] == sid
    preview = client.get(f"/api/teams/snapshots/{sid}/preview", headers=auth["op"])
    assert preview.status_code == 200 and preview.json()["seats"][0]["planned"] == "fresh"
    restored = client.post(f"/api/teams/snapshots/{sid}/restore", headers=auth["op"])
    assert restored.status_code == 200 and restored.json()["seats"][0]["outcome"] == "fresh"
    # An agent that does not own the seat plans (and gets) a failed seat.
    theirs = client.get(f"/api/teams/snapshots/{sid}/preview", headers=auth["dev"])
    assert theirs.status_code == 200 and theirs.json()["seats"][0]["planned"] == "failed"
    missing = client.get("/api/teams/snapshots/nope/preview", headers=auth["dev"])
    assert missing.status_code == 404
    bad = client.post("/api/teams/snapshots", headers=auth["dev"], json={"item_ids": "h1"})
    assert bad.status_code == 400


# --- review follow-ups: repeated restore, offline and lapsed items, large packets ----


def test_a_repeated_restore_never_starts_the_seat_twice(harness):
    harness["items"]["f"] = _item("f", task_id=None)
    snap = team_snapshot.snapshot(item_ids=["f"])
    first = team_snapshot.restore(snap["snapshot_id"])["seats"][0]
    assert first["outcome"] == "fresh" and first["new_item_id"]
    # The fresh session the first restore queued is still waiting for its node.
    successor = first["new_item_id"]
    harness["items"][successor] = _item(successor, task_id=None, status="queued")
    seat = team_snapshot.preview(snap["snapshot_id"])["seats"][0]
    assert seat["planned"] == "live"
    assert seat["reason"] == "a previous restore's session is still queued or running"
    assert team_snapshot.restore(snap["snapshot_id"])["seats"][0]["outcome"] == "live"
    assert [c[0] for c in harness["calls"]] == ["dispatch"]
    # Once that session has ended, the seat may be restored again.
    harness["items"][successor]["status"] = "finished"
    assert team_snapshot.preview(snap["snapshot_id"])["seats"][0]["planned"] == "fresh"
    assert team_snapshot.restore(snap["snapshot_id"])["seats"][0]["outcome"] == "fresh"
    assert [c[0] for c in harness["calls"]] == ["dispatch", "dispatch"]


def test_an_offline_nodes_claimed_item_is_failed_not_live(harness):
    harness["items"]["c"] = _item("c", status="claimed")
    harness["nodes"]["desk"]["last_heartbeat"] = (
        datetime.now(UTC) - timedelta(hours=1)
    ).isoformat()
    snap = team_snapshot.snapshot(item_ids=["c"])
    seat = team_snapshot.preview(snap["snapshot_id"])["seats"][0]
    assert seat["planned"] == "failed"
    assert seat["reason"] == "node desk is offline; item c is still recorded as claimed"
    assert team_snapshot.restore(snap["snapshot_id"])["seats"][0]["outcome"] == "failed"
    assert harness["calls"] == []


def test_a_lapsed_lease_must_be_reconciled_before_restore(harness):
    items = harness["items"]
    # Claimed, lease passed, not yet failed by the hub: not live work.
    items["lapsed"] = _item("lapsed", status="claimed")
    items["lapsed"]["lease_expires_at"] = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    # Failed by the hub for a lapsed lease: the node may still be running it.
    items["unknown"] = _item("unknown", sid="sess-unknown")
    items["unknown"]["receipt"] = {
        "status": "failed",
        "reason": "lease_expired_outcome_unknown",
        "hub_generated": True,
    }
    harness["resumable"].add("unknown")
    # A claimed item whose lease is still current is live.
    items["current"] = _item("current", status="claimed", sid="sess-current")
    items["current"]["lease_expires_at"] = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    snap = team_snapshot.snapshot(item_ids=["lapsed", "unknown", "current"])
    plan = {s["item_id"]: s for s in team_snapshot.preview(snap["snapshot_id"])["seats"]}
    assert plan["lapsed"]["planned"] == plan["unknown"]["planned"] == "failed"
    assert plan["lapsed"]["reason"] == team_snapshot.RECONCILE_REASON
    assert plan["unknown"]["reason"] == team_snapshot.RECONCILE_REASON
    assert plan["current"]["planned"] == "live"
    team_snapshot.restore(snap["snapshot_id"])
    assert harness["calls"] == []


def _large_packet(task_id, *, handoff=None):
    return {
        "schema": "ua.session_packet.v1",
        "labels": {"role": "historical_context", "transcript_included": False},
        "generated_at": "2026-01-01T00:00:00+00:00",
        "requested_by": None,
        "task": {
            "id": task_id,
            "title": "large packet task",
            "status": "doing",
            "priority": "high",
            "notes": "old note line. " * 2_000 + "latest note",
            "worktree_path": "/work/repo",
        },
        "memory": [{"text": "memory hit " * 50} for _ in range(30)],
        "recent_events": [{"summary": "event " * 40} for _ in range(30)],
        "approvals": [{"id": i, "tool": "x"} for i in range(30)],
        "prior_handoff": handoff or {"summary": "handoff summary " * 300},
    }


def test_a_large_packet_keeps_the_task_and_handoff(harness, monkeypatch):
    from unified_agent.lifecycle import packet as packet_mod

    task = task_tools.task_add("large packet task")["id"]
    monkeypatch.setattr(packet_mod, "build_session_packet", lambda task_id: _large_packet(task))
    bounded = team_snapshot._bounded_packet(task)
    size = len(team_snapshot.json.dumps(bounded, default=str).encode("utf-8"))
    assert size <= team_snapshot.MAX_PACKET_BYTES
    assert bounded["truncated"] is True
    assert bounded["schema"] and bounded["generated_at"]
    assert (bounded["task"]["id"], bounded["task"]["title"], bounded["task"]["status"]) == (
        task,
        "large packet task",
        "doing",
    )
    # Optional lists go first; notes keep their latest tail; the handoff stays.
    assert "memory" not in bounded and "recent_events" not in bounded
    assert bounded["task"]["notes"].endswith("latest note")
    assert bounded["prior_handoff"]["summary"]
    # A rebuilt seat's prompt still names its task.
    harness["items"]["big"] = _item("big", task_id=task)
    snap = team_snapshot.snapshot(item_ids=["big"])
    assert team_snapshot.restore(snap["snapshot_id"])["seats"][0]["outcome"] == "rebuilt"
    prompt = harness["calls"][0][2]["prompt"]
    assert f"Task #{task}: large packet task" in prompt


def test_an_oversized_handoff_still_leaves_the_task_core(harness, monkeypatch):
    from unified_agent.lifecycle import packet as packet_mod

    handoff = {f"field_{i}": "x" * 400 for i in range(100)}
    monkeypatch.setattr(
        packet_mod, "build_session_packet", lambda task_id: _large_packet(7, handoff=handoff)
    )
    bounded = team_snapshot._bounded_packet(7)
    assert len(team_snapshot.json.dumps(bounded).encode("utf-8")) <= team_snapshot.MAX_PACKET_BYTES
    assert bounded["task"] == {"id": 7, "title": "large packet task", "status": "doing"}
    assert bounded["truncated"] is True and bounded["schema"]
