"""Operator node retire (task #2181).

A device registered again under a new hostname leaves its old ``nodes`` row
behind forever. ``coordination_manage`` action ``node_retire`` (lifecycle
operators only) marks such a row retired without deleting it and records a
``kind="retire"`` event on ``agent.node``. A retired row is left out of fleet
rows, the node listing and routing, and its re-registration and heartbeats are
refused until ``node_unretire``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, node_queue, node_registry, profiles, storage
from unified_agent.tool_errors import ConflictError, PermissionDeniedError
from unified_agent.tools import doctor
from unified_agent.tools.graph import coordination_manage

OPERATOR = "Desktop_Operator"
OWNER = "Desktop_Agent1"
OLD = "old-host.local"
NEW = "new-host.local"


@contextmanager
def _as(label: str | None):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", OPERATOR)
    monkeypatch.delenv("UA_FLEET_NODES", raising=False)
    monkeypatch.delenv("UA_FLEET_OPTIONAL_NODES", raising=False)
    monkeypatch.delenv("UA_NODE_REQUIRE_ACCEPTANCE", raising=False)
    monkeypatch.delenv("UA_REVOKED_AGENTS", raising=False)
    storage.init_db()


def _register(node: str, *, now: datetime | None = None) -> None:
    node_queue.register(node, identity=OWNER, facts={"backends": ["codex"]}, now=now)


def _retire(node: str = OLD, who: str | None = OPERATOR, reason: str = "renamed device"):
    with _as(who):
        return coordination_manage("node_retire", {"node": node, "reason": reason})


def _unretire(node: str = OLD, who: str | None = OPERATOR, reason: str = "device is back"):
    with _as(who):
        return coordination_manage("node_unretire", {"node": node, "reason": reason})


def _node_row(node: str) -> dict:
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM nodes WHERE node=?", (node,)).fetchone()
    finally:
        conn.close()
    return dict(row)


def _lifecycle_events() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload, originator FROM event_log WHERE channel='agent.node' "
            "AND json_extract(payload, '$.kind') IN ('retire', 'unretire') ORDER BY ulid"
        ).fetchall()
    finally:
        conn.close()
    return [{**json.loads(r["payload"]), "_originator": r["originator"]} for r in rows]


# ── authority ────────────────────────────────────────────────────────────────


def test_an_ordinary_agent_cannot_retire_a_node_even_its_own():
    _register(OLD)
    with pytest.raises(PermissionDeniedError, match="operator action"):
        _retire(who=OWNER)
    assert _node_row(OLD)["retired_at"] is None
    assert _lifecycle_events() == []


def test_an_unauthenticated_caller_cannot_retire_a_node():
    _register(OLD)
    with pytest.raises(ToolError, match="authenticated identity"):
        _retire(who=None)
    assert _node_row(OLD)["retired_at"] is None


def test_unretire_is_operator_only_too():
    _register(OLD)
    _retire()
    with pytest.raises(PermissionDeniedError):
        _unretire(who=OWNER)
    assert _node_row(OLD)["retired_at"] is not None


def test_reason_and_node_are_required():
    _register(OLD)
    with pytest.raises(ToolError, match="reason is required"):
        _retire(reason="   ")
    with pytest.raises(ToolError, match="not registered"):
        _retire(node="never-seen.local")
    with pytest.raises(ToolError, match="node must be"):
        _retire(node="../etc")


def test_the_action_is_reachable_on_the_developer_profile_without_a_profile_change():
    # Authorization lives in the action itself; the profile only has to expose it.
    for action in ("node_retire", "node_unretire"):
        assert profiles.consolidated_action_is_allowed_for(
            "developer", "coordination_manage", action
        )


# ── retire: recorded, kept, excluded ─────────────────────────────────────────


def test_retire_marks_the_row_and_records_one_ledger_event():
    _register(OLD)
    out = _retire()

    assert out["changed"] is True and out["retired"] is True
    assert out["retired_by"] == OPERATOR and out["retired_reason"] == "renamed device"
    row = _node_row(OLD)
    assert row["retired_at"] == out["retired_at"]
    assert row["retired_by"] == OPERATOR
    assert row["agent"] == OWNER  # kept, not deleted
    [event] = _lifecycle_events()
    assert event["kind"] == "retire"
    assert event["node"] == OLD and event["agent"] == OWNER
    assert event["actor"] == OPERATOR and event["_originator"] == OPERATOR
    assert event["reason"] == "renamed device"
    assert out["event_ulid"]


def test_retire_is_idempotent():
    _register(OLD)
    first = _retire()
    again = _retire(reason="second try")
    assert again["changed"] is False
    assert again["retired_at"] == first["retired_at"]
    assert again["retired_reason"] == "renamed device"
    assert len(_lifecycle_events()) == 1


def test_retired_rows_leave_the_node_listing():
    _register(OLD)
    _register(NEW)
    _retire()

    assert set(node_registry.list_nodes()) == {NEW}
    assert set(node_registry.read_nodes()) == {NEW}
    every = node_registry.read_nodes(include_retired=True)
    assert set(every) == {OLD, NEW}
    assert every[OLD]["retired_by"] == OPERATOR
    assert every[NEW]["retired_at"] is None


def test_retire_refuses_a_node_with_queued_or_claimed_work():
    _register(OLD)
    ts = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO node_dispatch (id, node, agent, backend, prompt, cwd, timeout_s, "
            "status, created_by, created_at, updated_at) "
            "VALUES ('nd-0000000000000001', ?, ?, 'codex', 'p', '/w', 60, 'queued', ?, ?, ?)",
            (OLD, OWNER, OPERATOR, ts, ts),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ConflictError, match="nd-0000000000000001"):
        _retire()
    assert _node_row(OLD)["retired_at"] is None
    assert _lifecycle_events() == []


# ── a retired device is refused until un-retired ─────────────────────────────


def test_a_retired_node_cannot_register_or_heartbeat_until_unretired():
    _register(OLD)
    _retire()

    with pytest.raises(node_registry.NodeError, match="was retired by an operator") as reg:
        _register(OLD)
    assert reg.value.status == 403
    with pytest.raises(node_registry.NodeError, match="was retired by an operator") as beat:
        node_queue.heartbeat(OLD, identity=OWNER)
    assert beat.value.status == 403
    assert OLD not in node_registry.list_nodes()

    out = _unretire()
    assert out["changed"] is True and out["retired"] is False
    assert _node_row(OLD)["retired_at"] is None
    _register(OLD)
    node_queue.heartbeat(OLD, identity=OWNER)
    assert OLD in node_registry.list_nodes()
    assert [e["kind"] for e in _lifecycle_events()] == ["retire", "unretire"]
    assert _unretire()["changed"] is False


# ── routing ──────────────────────────────────────────────────────────────────


def test_routing_skips_a_retired_row_and_picks_the_live_one():
    now = datetime.now(UTC)
    _register(OLD, now=now)
    _register(NEW, now=now)
    _retire()
    picked = node_queue.select_node_for_agent(OWNER, "codex", now=now)
    assert picked["ok"] is True and picked["node"] == NEW


def test_a_label_whose_only_node_is_retired_fails_closed():
    now = datetime.now(UTC)
    _register(OLD, now=now)
    _retire()
    picked = node_queue.select_node_for_agent(OWNER, "codex", now=now)
    # Still node-routed: never a silent fall back to a hub-local spawn.
    assert picked == {"routed": True, "ok": False, "error": f"node_retired:{OLD}"}


def test_direct_enqueue_to_a_retired_node_is_refused():
    _register(OLD)
    _retire()
    with pytest.raises(node_registry.NodeError, match="was retired by an operator") as exc:
        node_queue.enqueue(OLD, created_by=OPERATOR, prompt="p", cwd="/w", backend="codex")
    assert exc.value.status == 409


# ── doctor ───────────────────────────────────────────────────────────────────


def test_doctor_fleet_nodes_reports_retired_rows_instead_of_a_stale_host(monkeypatch):
    _register(OLD)
    _register(NEW)
    _retire()
    monkeypatch.setenv("UA_FLEET_NODES", NEW)

    row = doctor._fleet_nodes_check()
    assert row["status"] == "LIVE", row
    assert row["retired"] == [OLD]
    assert f"1 retired node row(s) excluded: {OLD}" in row["detail"]
    assert [h["node"] for h in row["hosts"]] == [NEW]


def test_doctor_names_a_declared_retired_node_as_retired(monkeypatch):
    _register(OLD)
    _register(NEW)
    _retire()
    monkeypatch.setenv("UA_FLEET_NODES", f"{NEW},{OLD}")

    row = doctor._fleet_nodes_check()
    hosts = {h["node"]: h["state"] for h in row["hosts"]}
    assert hosts == {NEW: "live", OLD: "retired"}
    assert f"{OLD} retired by an operator (still declared in UA_FLEET_NODES)" in row["detail"]
    # Required but retired: the declaration needs fixing, so not green.
    assert row["status"] == "DEGRADED"
    assert "excluded" not in row["detail"]


def test_other_fleet_rows_ignore_a_retired_node(monkeypatch):
    _register(OLD)
    _retire()
    assert doctor._fleet_node_versions_check()["status"] == "DORMANT"


# ── schema ───────────────────────────────────────────────────────────────────


def test_retire_columns_are_added_idempotently_to_an_older_store():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE TABLE nodes (node TEXT PRIMARY KEY, agent TEXT NOT NULL, device_node TEXT, "
            "facts_json TEXT, version TEXT, registered_at TEXT NOT NULL, last_heartbeat TEXT, "
            "updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO nodes VALUES ('legacy.local', 'Desktop_Agent1', NULL, '{}', NULL, "
            "'2026-01-01', NULL, '2026-01-01')"
        )
        storage._add_node_retired_columns(conn)
        storage._add_node_retired_columns(conn)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
        assert {"retired_at", "retired_by", "retired_reason", "heartbeat_floor"} <= cols
        row = conn.execute("SELECT retired_at FROM nodes").fetchone()
        assert row["retired_at"] is None
    finally:
        conn.close()


# ── review follow-ups: ownership first, a distinct refusal, acceptance ──────


def test_a_foreign_identity_learns_nothing_about_retirement():
    _register(OLD)
    _retire()
    with pytest.raises(node_registry.NodeError, match="bound to a different agent") as exc:
        node_queue.heartbeat(OLD, identity="Desktop_Agent2")
    assert exc.value.status == 403 and exc.value.code is None
    with pytest.raises(node_registry.NodeError, match="retired") as owner:
        node_queue.heartbeat(OLD, identity=OWNER)
    assert owner.value.code == node_registry.RETIRED_CODE


def test_revocation_probe_still_reports_each_check_on_a_retired_row():
    from unified_agent import node_acceptance

    _register(OLD)
    _retire()
    probe = node_acceptance.revocation_probe(OLD)
    assert probe["status"] == "pass", probe
    assert "foreign_agent" in probe["detail"] and "revoked_agent" in probe["detail"]


def test_the_retired_refusal_carries_a_machine_readable_code_over_http():
    from unified_agent.webui import routes

    resp = routes._node_error(
        node_registry.NodeError("node 'x' was retired", 403, code=node_registry.RETIRED_CODE)
    )
    assert resp.status_code == 403
    assert json.loads(resp.body) == {"error": "node 'x' was retired", "code": "node_retired"}
    plain = routes._node_error(node_registry.NodeError("bound to a different agent", 403))
    assert json.loads(plain.body) == {"error": "bound to a different agent"}


def test_the_node_agent_backs_off_long_on_retirement_and_never_suggests_a_login(tmp_path, caplog):
    from unified_agent import node_agent

    assert node_agent.RETIRED_CODE == node_registry.RETIRED_CODE
    sleeps: list[float] = []
    refusal = {"error": node_registry.retired_message(OLD), "code": "node_retired"}
    agent = node_agent.NodeAgent(
        node=OLD,
        transport=lambda path, body, timeout: (403, refusal),
        roots=[tmp_path],
        facts_fn=dict,
        sleep=sleeps.append,
    )
    with caplog.at_level("ERROR", logger=node_agent.logger.name):
        assert agent.run_once() == "retired"
        assert agent.run_once() == "retired"
    assert sleeps == [node_agent.RETIRED_BACKOFF_S] * 2
    assert node_agent.RETIRED_BACKOFF_S > node_agent.AUTH_BACKOFF_S
    assert agent.registered is False
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1, errors  # logged once, not every backoff
    assert "retired by an operator" in errors[0] and "node_unretire" in errors[0]
    assert "login" not in errors[0].lower()


def test_acceptance_summaries_leave_out_a_retired_node():
    from unified_agent import node_acceptance

    _register(OLD)
    _register(NEW)
    ts = storage.now()
    conn = storage.connect()
    try:
        node_acceptance.ensure_schema(conn)
        for name in (OLD, NEW):
            conn.execute(
                "INSERT INTO node_acceptance (node, run_id, agent, state, started_by, "
                "started_at, updated_at) VALUES (?, ?, ?, 'failed', ?, ?, ?)",
                (name, f"run-{name}", OWNER, OPERATOR, ts, ts),
            )
        conn.commit()
    finally:
        conn.close()
    assert set(node_acceptance.read_states()) == {OLD, NEW}
    _retire()
    assert set(node_acceptance.read_states()) == {NEW}
    assert set(node_acceptance.node_acceptance_status()["nodes"]) == {NEW}
    row = doctor._fleet_node_acceptance_check()
    assert OLD not in row["detail"]


# ── follow-ups (task #2196) ──────────────────────────────────────────────────

_EMPTY_DOCTOR = {
    "checks": [],
    "summary": {"live": 0, "dormant": 0, "misconfigured": 0, "unknown": 0},
}


def _fleet_hosts() -> set[str]:
    from unified_agent import host_registry

    return {row["host"] for row in host_registry.fleet_payload()["hosts"]}


def test_devices_leave_out_the_host_report_of_a_retired_node():
    """A retired node's host_reports row (its doctor snapshot) leaves Devices (#2196)."""
    from unified_agent import host_registry

    _register(OLD)
    _register(NEW)
    for host in (OLD, NEW):
        host_registry.record_doctor_snapshot(host, _EMPTY_DOCTOR)
    assert {OLD, NEW} <= _fleet_hosts()

    _retire()
    hosts = _fleet_hosts()
    assert OLD not in hosts
    assert NEW in hosts

    _unretire()
    _register(OLD)
    assert {OLD, NEW} <= _fleet_hosts()


def test_devices_keep_a_host_that_also_has_a_live_node(monkeypatch):
    """Only a host with no live, non-retired node is excluded (#2196).

    The retired and active lists are separate reads, so a node retired and
    brought back between them must not vanish from Devices.
    """
    from unified_agent import host_registry

    _register(OLD)
    _register(NEW)
    for host in (OLD, NEW):
        host_registry.record_doctor_snapshot(host, _EMPTY_DOCTOR)
    real = node_registry.read_retired_nodes
    monkeypatch.setattr(
        node_registry,
        "read_retired_nodes",
        lambda: {**real(), NEW: {"retired_at": storage.now()}},
    )
    _retire()
    hosts = _fleet_hosts()
    assert NEW in hosts
    assert OLD not in hosts


def test_unretire_waits_for_a_fresh_heartbeat_before_routing():
    """A beat from before retirement does not route work after node_unretire (#2196).

    The ua-node backs off for a long time once retired, so a heartbeat still
    inside the live window would route work to a device that is not polling.
    """
    # Still inside the 90 s live window when the operator brings it back.
    _register(OLD, now=datetime.now(UTC) - timedelta(seconds=30))
    beat = _node_row(OLD)["last_heartbeat"]
    _retire()
    _unretire()

    picked = node_queue.select_node_for_agent(OWNER, "codex")
    assert picked == {"routed": True, "ok": False, "error": f"node_offline:{OLD}"}
    # The history the doctor reads is kept, not cleared.
    assert _node_row(OLD)["last_heartbeat"] == beat

    node_queue.heartbeat(OLD, identity=OWNER)
    picked = node_queue.select_node_for_agent(OWNER, "codex")
    assert picked["ok"] is True and picked["node"] == OLD


def test_unretire_routes_again_after_a_fresh_registration():
    _register(OLD, now=datetime.now(UTC) - timedelta(seconds=30))
    _retire()
    _unretire()
    assert node_queue.select_node_for_agent(OWNER, "codex")["ok"] is False
    _register(OLD)
    picked = node_queue.select_node_for_agent(OWNER, "codex")
    assert picked["ok"] is True and picked["node"] == OLD


def test_doctor_reads_the_kept_heartbeat_but_not_as_live_after_unretire(monkeypatch):
    """Doctor keeps the last-seen history and agrees with routing that it is not live."""
    monkeypatch.setenv("UA_FLEET_NODES", OLD)
    _register(OLD, now=datetime.now(UTC) - timedelta(seconds=30))
    _retire()
    _unretire()
    row = doctor._fleet_nodes_check()
    host = next(h for h in row["hosts"] if h["node"] == OLD)
    assert host["state"] == "stale"
    assert host["last_heartbeat"] is not None
    assert "last heartbeat" in row["detail"]


def test_a_retirement_refused_mid_register_is_logged_at_warning(tmp_path, caplog):
    """Retired between register and its config sync: a warning, not debug (#2194)."""
    from unified_agent import node_agent

    refusal = {"error": node_registry.retired_message(OLD), "code": "node_retired"}

    def transport(path, body, timeout):
        if path == "/api/node/register":
            return 200, {"agent": OWNER, "hub_features": []}
        return 403, refusal

    agent = node_agent.NodeAgent(
        node=OLD, transport=transport, roots=[tmp_path], facts_fn=dict, sleep=lambda _s: None
    )
    with caplog.at_level("DEBUG", logger=node_agent.logger.name):
        assert agent.run_once() == "registered"
    retired = [r for r in caplog.records if "retired by an operator" in r.getMessage()]
    assert [r.levelname for r in retired] == ["WARNING"]


# ── review of PR #1290: every liveness reader honours heartbeat_floor ───────


def _returned_with_old_beat() -> None:
    """Retire and bring back a node whose stored beat is still inside the window."""
    _register(OLD, now=datetime.now(UTC) - timedelta(seconds=30))
    _retire()
    _unretire()


def test_direct_enqueue_and_selection_agree_after_unretire():
    """node_wait_reason and select_node_for_agent see the same not-live node (#2110)."""
    _returned_with_old_beat()
    ref = datetime.now(UTC)
    assert node_queue.node_wait_reason(_node_row(OLD), ref) == "node_offline"
    assert node_queue.select_node_for_agent(OWNER, "codex", now=ref)["ok"] is False

    node_queue.heartbeat(OLD, identity=OWNER)
    ref = datetime.now(UTC)
    assert node_queue.node_wait_reason(_node_row(OLD), ref) is None
    assert node_queue.select_node_for_agent(OWNER, "codex", now=ref)["ok"] is True


def test_doctor_node_versions_does_not_count_an_unreturned_node(monkeypatch):
    _returned_with_old_beat()
    monkeypatch.setattr(doctor, "_hub_code_sha", lambda: "a" * 40)
    row = doctor._fleet_node_versions_check()
    assert row["status"] == "DORMANT"
    assert "no live ua-node" in row["detail"]


def test_node_bound_agent_status_waits_for_a_fresh_beat():
    from unified_agent.tools import registry as registry_tool

    _returned_with_old_beat()
    nodes = node_registry.list_nodes()
    out = registry_tool._apply_node_liveness({"label": OWNER}, nodes, {OWNER: OLD})
    assert out["status"] != "online"
    # last_seen keeps the stored beat as history.
    assert out["last_seen"] == _node_row(OLD)["last_heartbeat"]


def test_team_seat_picking_waits_for_a_fresh_beat():
    from unified_agent import team_spec

    _returned_with_old_beat()
    seat = {"label": OWNER, "host": None, "harness": "codex"}
    _name, _row, refusal = team_spec._pick_node(seat, node_registry.list_nodes(), datetime.now(UTC))
    assert refusal is not None and refusal["code"] == "host_offline"


def test_team_seat_restore_treats_an_unreturned_node_as_offline():
    from unified_agent import team_snapshot

    _returned_with_old_beat()
    seat = {"recorded": True, "item_id": "nd-0000000000000099", "node": OLD, "label": OWNER}
    planned = team_snapshot._plan_seat(seat, OPERATOR, datetime.now(UTC), operator=True)
    assert planned["planned"] == "failed"
    assert f"node {OLD} is offline" in planned["reason"]


def test_node_live_beat_reads_rows_and_listing_entries_alike():
    """One helper for raw ``nodes`` rows and ``read_nodes`` entries."""
    _returned_with_old_beat()
    assert node_registry.node_live_beat(_node_row(OLD)) is None
    assert node_registry.node_live_beat(node_registry.list_nodes()[OLD]) is None
    node_queue.heartbeat(OLD, identity=OWNER)
    fresh = _node_row(OLD)["last_heartbeat"]
    assert node_registry.node_live_beat(_node_row(OLD)) == fresh
    assert node_registry.node_live_beat(node_registry.list_nodes()[OLD]) == fresh


@pytest.mark.parametrize("report_row", [True, False])
def test_devices_and_agent_list_agree_on_an_unreturned_node(report_row):
    """Devices and the agent list both read 'offline' until a fresh beat (#2192).

    Devices used to show 'unknown' for the fenced beat while the agent list
    showed 'offline'. Registration writes a host_reports row; without one,
    Devices builds a blank node row, and both paths take the same status.
    """
    from unified_agent import host_registry
    from unified_agent.tools import registry as registry_tool

    _returned_with_old_beat()
    if not report_row:
        conn = storage.connect()
        try:
            conn.execute("DELETE FROM host_reports WHERE host=?", (OLD,))
            conn.commit()
        finally:
            conn.close()

    def _device() -> dict:
        return next(r for r in host_registry.fleet_payload()["hosts"] if r["host"] == OLD)

    nodes = node_registry.list_nodes()
    agent = registry_tool._apply_node_liveness({"label": OWNER}, nodes, {OWNER: OLD})
    assert agent["status"] == "offline"
    assert _device()["last_seen_status"] == "offline"
    assert _device()["staleness"] == "offline"

    node_queue.heartbeat(OLD, identity=OWNER)
    assert _device()["last_seen_status"] == "online"
