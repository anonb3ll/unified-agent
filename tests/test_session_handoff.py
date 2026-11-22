"""Agent-authored session handoff notes, commit point, failover, epochs (task #2397)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import events, storage
from unified_agent.lifecycle import handoff
from unified_agent.lifecycle.packet import build_session_packet

T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def _clock(offset_minutes: float = 0):
    return lambda: T0 + timedelta(minutes=offset_minutes)


def _session(sid: str, agent: str, *, status: str = "running", org_id: str | None = None):
    conn = storage.connect()
    try:
        ts = storage.now()
        conn.execute(
            "INSERT INTO agent_sessions (id, agent, kind, status, started_at, "
            "last_heartbeat_at, org_id) VALUES (?, ?, 'interactive', ?, ?, ?, ?)",
            (sid, agent, status, ts, ts, org_id),
        )
        conn.commit()
    finally:
        conn.close()


def _task(task_id: int, *, org_id: str | None = None):
    conn = storage.connect()
    try:
        ts = storage.now()
        conn.execute(
            "INSERT INTO tasks (id, title, status, priority, created_at, updated_at, org_id) "
            "VALUES (?, 'handoff target', 'open', 'med', ?, ?, ?)",
            (task_id, ts, ts, org_id),
        )
        conn.commit()
    finally:
        conn.close()


def _propose(sid="s1", agent="Desk_A", at=0, **kw):
    kw.setdefault("done", ["wrote the module"])
    kw.setdefault("next_steps", ["wire packet.py"])
    return handoff.propose(sid, agent=agent, now=_clock(at), **kw)


@pytest.fixture
def org_chart(tmp_path, monkeypatch):
    chart = tmp_path / "org.yaml"
    chart.write_text(
        "schema_version: 1\n"
        "offices:\n"
        "  - id: eng\n"
        "    name: Engineering\n"
        "    lead: Desk_A\n"
        "    members: [Desk_A, Desk_B, Desk_C]\n"
        "  - id: ops\n"
        "    name: Ops\n"
        "    lead: Ops_X\n"
        "    members: [Ops_X]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", str(chart))
    return chart


def test_init_db_creates_tables_and_ensure_schema_is_idempotent():
    conn = storage.connect()
    try:
        handoff.ensure_schema(conn)
        handoff.ensure_schema(conn)
        names = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert {"session_handoff_notes", "context_epochs"} <= names


def test_propose_requires_done_and_next_steps():
    _session("s1", "Desk_A")
    with pytest.raises(ToolError, match="done"):
        _propose(done=[])
    with pytest.raises(ToolError, match="next_steps"):
        _propose(next_steps=["  "])
    with pytest.raises(ToolError, match="next_steps"):
        _propose(next_steps="not a list")


def test_propose_refuses_non_owner_unknown_and_other_tenant_sessions():
    _session("s1", "Desk_A")
    _session("s2", "Desk_A", org_id="another-org")
    with pytest.raises(ToolError, match="owned by"):
        _propose(agent="Desk_B")
    with pytest.raises(ToolError, match="not found"):
        _propose(sid="nope")
    # Another tenant's session reads as not found, never as "not yours".
    with pytest.raises(ToolError, match="not found"):
        _propose(sid="s2")


def test_propose_refuses_a_task_that_is_missing_or_in_another_tenant():
    """A note may only reference a task the caller's tenant can see (#1481 review)."""
    _session("s1", "Desk_A")
    _task(4100, org_id="another-org")
    for task_id in (4100, 999999):
        with pytest.raises(ToolError, match=f"task {task_id} not found"):
            _propose(task_id=task_id)
    conn = storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM session_handoff_notes").fetchone()[0] == 0
    finally:
        conn.close()


def test_draft_persists_before_seal_and_is_not_recoverable():
    _session("s1", "Desk_A")
    _task(2397)
    note = _propose(open_questions=["which office?"], task_id=2397)
    assert note["state"] == "draft"
    assert note["done"] == ["wrote the module"]
    assert note["open_questions"] == ["which office?"]
    assert note["task_id"] == 2397
    assert handoff.get_note(note["id"])["state"] == "draft"
    # Recovery only reads past the commit point.
    assert handoff.recover("s1") is None


def test_one_pending_note_per_session():
    _session("s1", "Desk_A")
    note = _propose()
    with pytest.raises(ToolError, match="pending"):
        _propose(at=10)
    handoff.seal(note["id"], agent="Desk_A", now=_clock(11))
    # A sealed, undelivered note is still pending: a new note must say it supersedes it.
    with pytest.raises(ToolError, match="supersede"):
        _propose(at=20)
    assert _propose(at=20, supersede=True)["supersedes"] == note["id"]


def test_draft_can_be_replaced_or_discarded_before_seal():
    _session("s1", "Desk_A")
    note = _propose()
    edited = handoff.update_draft(
        note["id"], agent="Desk_A", next_steps=["wire node_agent.py"], now=_clock(1)
    )
    assert edited["next_steps"] == ["wire node_agent.py"]
    assert edited["done"] == ["wrote the module"]
    with pytest.raises(ToolError, match="owned by"):
        handoff.discard(note["id"], agent="Desk_B")
    assert handoff.discard(note["id"], agent="Desk_A")["state"] == "discarded"
    assert _propose(at=6)["state"] == "draft"


def test_cooldown_counts_from_the_last_proposal():
    _session("s1", "Desk_A")
    first = _propose()
    handoff.discard(first["id"], agent="Desk_A")
    with pytest.raises(ToolError, match="cooldown"):
        _propose(at=4.9)
    assert _propose(at=5)["state"] == "draft"


def test_hourly_limit_is_five_proposals():
    _session("s1", "Desk_A")
    for i in range(5):
        handoff.discard(_propose(at=i * 6)["id"], agent="Desk_A")
    with pytest.raises(ToolError, match="5 per hour"):
        _propose(at=30)
    # The window slides: the first proposal (t=0) ages out at t=60.
    assert _propose(at=60)["state"] == "draft"


def test_seal_is_the_commit_point():
    _session("s1", "Desk_A")
    note = _propose()
    with pytest.raises(ToolError, match="owned by"):
        handoff.seal(note["id"], agent="Desk_B")
    sealed = handoff.seal(note["id"], agent="Desk_A", now=_clock(1))
    assert sealed["state"] == "sealed"
    assert sealed["sealed_at"] == (T0 + timedelta(minutes=1)).isoformat(timespec="seconds")
    with pytest.raises(ToolError, match="sealed"):
        handoff.update_draft(note["id"], agent="Desk_A", done=["rewrite"])
    with pytest.raises(ToolError, match="sealed"):
        handoff.discard(note["id"], agent="Desk_A")
    with pytest.raises(ToolError, match="sealed"):
        handoff.seal(note["id"], agent="Desk_A")
    assert handoff.recover("s1")["id"] == note["id"]


def test_store_refuses_backward_transitions_and_sealed_edits():
    _session("s1", "Desk_A")
    note = handoff.seal(_propose()["id"], agent="Desk_A")
    conn = storage.connect()
    try:
        for sql in (
            "UPDATE session_handoff_notes SET state='draft' WHERE id=?",
            "UPDATE session_handoff_notes SET done='[\"x\"]' WHERE id=?",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="forward-only"):
                conn.execute(sql, (note["id"],))
            conn.rollback()
    finally:
        conn.close()


def test_sealing_a_superseding_note_supersedes_the_old_one():
    _session("s1", "Desk_A")
    old = handoff.seal(_propose()["id"], agent="Desk_A", now=_clock(1))
    new = _propose(at=10, supersede=True, done=["second pass"])
    # Until the new note seals, recovery still returns the old commit.
    assert handoff.recover("s1")["id"] == old["id"]
    handoff.seal(new["id"], agent="Desk_A", now=_clock(11))
    assert handoff.get_note(old["id"])["state"] == "superseded"
    assert handoff.recover("s1")["id"] == new["id"]


def test_discarding_a_superseding_draft_keeps_the_old_commit():
    _session("s1", "Desk_A")
    old = handoff.seal(_propose()["id"], agent="Desk_A")
    handoff.discard(_propose(at=10, supersede=True)["id"], agent="Desk_A")
    assert handoff.get_note(old["id"])["state"] == "sealed"
    assert handoff.recover("s1")["id"] == old["id"]


def test_failover_picks_a_live_office_peer_and_records_one_event(org_chart):
    _session("s1", "Desk_A")
    _session("sb", "Desk_B", status="completed")  # not live
    _session("sc", "Desk_C", status="running")
    _session("sx", "Ops_X", status="running")  # live, but another office
    _task(2397)
    note = handoff.seal(_propose(task_id=2397)["id"], agent="Desk_A")
    out = handoff.failover(note["id"], reason="quota")
    # The default delivery only records: the note is assigned, not delivered.
    assert (out["state"], out["assigned_to"], out["delivered_to"]) == ("assigned", "Desk_C", None)
    assert out["delivery"] == {"delivered": False, "mode": "record_only"}
    stored = handoff.get_note(note["id"])
    assert (stored["state"], stored["assigned_to"]) == ("assigned", "Desk_C")
    assert stored["delivered_to"] is None and stored["delivered_at"] is None
    rows = events.query_events(channels=["agent.lifecycle.handoff"], limit=10)
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["note_id"] == note["id"]
    assert payload["from_agent"] == "Desk_A"
    assert payload["to_agent"] == "Desk_C"
    assert payload["reason"] == "quota"
    assert payload["state"] == "assigned"
    assert out["event_id"] == rows[0]["ulid"]
    # The existing packet already surfaces it as the prior handoff.
    packet = build_session_packet(session_id="s1")
    assert packet["prior_handoff"]["payload"]["note_id"] == note["id"]


def test_failover_uses_injected_pick_and_deliver():
    _session("s1", "Desk_A")
    note = handoff.seal(_propose()["id"], agent="Desk_A")
    seen = {}

    def pick(n):
        seen["pick"] = n["id"]
        return "Remote_Z"

    def deliver(n, to_agent):
        seen["deliver"] = (n["id"], to_agent)
        return {"delivered": True, "mode": "test"}

    out = handoff.failover(note["id"], pick=pick, deliver=deliver)
    assert seen == {"pick": note["id"], "deliver": (note["id"], "Remote_Z")}
    assert (out["state"], out["delivered_to"]) == ("delivered", "Remote_Z")
    assert out["delivery"] == {"delivered": True, "mode": "test"}
    assert handoff.get_note(note["id"])["state"] == "delivered"


@pytest.mark.parametrize("answer", [None, {"delivered": False}], ids=["record-only", "refused"])
def test_an_undelivered_note_stays_pending_and_fails_over_again(answer):
    """Choosing a receiver is not delivery: the note can still go to someone else."""
    _session("s1", "Desk_A")
    note = handoff.seal(_propose()["id"], agent="Desk_A")
    deliver = None if answer is None else (lambda n, to_agent: answer)
    first = handoff.failover(note["id"], pick=lambda n: "Desk_B", deliver=deliver, now=_clock(1))
    assert (first["state"], first["delivered_to"]) == ("assigned", None)
    assert handoff.recover("s1")["state"] == "assigned"
    # Still pending: a new note needs supersede, and failover runs again.
    with pytest.raises(ToolError, match="pending"):
        _propose(at=10)
    second = handoff.failover(
        note["id"],
        pick=lambda n: "Desk_C",
        deliver=lambda n, to_agent: {"delivered": True},
        now=_clock(2),
    )
    assert (second["state"], second["delivered_to"]) == ("delivered", "Desk_C")
    stored = handoff.get_note(note["id"])
    assert (stored["assigned_to"], stored["delivered_to"]) == ("Desk_C", "Desk_C")
    states = [
        (r["payload"]["to_agent"], r["payload"]["state"])
        for r in events.query_events(channels=["agent.lifecycle.handoff"], limit=10)
    ]
    assert sorted(states) == [
        ("Desk_B", "assigned"),
        ("Desk_C", "assigned"),
        ("Desk_C", "delivered"),
    ]
    with pytest.raises(ToolError, match="already delivered"):
        handoff.failover(note["id"], pick=lambda n: "Desk_B")


def test_only_the_receiver_acknowledges_and_that_delivers_an_assigned_note():
    _session("s1", "Desk_A")
    note = handoff.seal(_propose()["id"], agent="Desk_A")
    with pytest.raises(ToolError, match="not handed to"):
        handoff.acknowledge(note["id"], agent="Desk_B")  # sealed: handed to nobody yet
    handoff.failover(note["id"], pick=lambda n: "Desk_B", now=_clock(1))
    for other in ("Desk_A", "Desk_C"):
        with pytest.raises(ToolError, match="not handed to"):
            handoff.acknowledge(note["id"], agent=other)
    acked = handoff.acknowledge(note["id"], agent="Desk_B", now=_clock(2))
    stamp = (T0 + timedelta(minutes=2)).isoformat(timespec="seconds")
    assert (acked["state"], acked["delivered_to"]) == ("delivered", "Desk_B")
    assert (acked["delivered_at"], acked["acknowledged_at"]) == (stamp, stamp)
    assert handoff.acknowledge(note["id"], agent="Desk_B", now=_clock(9)) == acked
    assert len(events.query_events(channels=["agent.lifecycle.handoff"], limit=10)) == 2


def test_failed_delivery_leaves_the_note_assigned_and_retryable():
    """A send that fails after the claim committed leaves a visible, pending assignment."""
    _session("s1", "Desk_A")
    note = handoff.seal(_propose()["id"], agent="Desk_A")

    def deliver(n, to_agent):
        raise RuntimeError("bus down")

    with pytest.raises(ToolError, match="delivery to Remote_Z failed.*assigned"):
        handoff.failover(note["id"], pick=lambda n: "Remote_Z", deliver=deliver)
    stored = handoff.get_note(note["id"])
    assert (stored["state"], stored["assigned_to"], stored["delivered_to"]) == (
        "assigned",
        "Remote_Z",
        None,
    )
    rows = events.query_events(channels=["agent.lifecycle.handoff"], limit=10)
    assert [(r["payload"]["to_agent"], r["payload"]["state"]) for r in rows] == [
        ("Remote_Z", "assigned")
    ]
    retried = handoff.failover(
        note["id"], pick=lambda n: "Remote_Z", deliver=lambda n, to_agent: {"delivered": True}
    )
    assert (retried["state"], retried["delivered_to"]) == ("delivered", "Remote_Z")


def test_failover_sends_only_after_its_claim_wins():
    """The CAS commits before the send; a lost CAS sends nothing (#1481 review)."""
    _session("s1", "Desk_A")
    note = handoff.seal(_propose()["id"], agent="Desk_A")
    sends = []

    def deliver(n, to_agent):
        stored = handoff.get_note(n["id"])
        sends.append((to_agent, stored["state"], stored["assigned_to"]))
        return {"delivered": True}

    def racing_pick(n):
        # Another failover claims the note between this call's read and its CAS.
        handoff.failover(n["id"], pick=lambda m: "Desk_B", deliver=deliver)
        return "Desk_C"

    with pytest.raises(ToolError, match="changed state during failover"):
        handoff.failover(note["id"], pick=racing_pick, deliver=deliver)
    # The winner sent exactly once, after its claim was committed; the loser sent nothing.
    assert sends == [("Desk_B", "assigned", "Desk_B")]
    stored = handoff.get_note(note["id"])
    assert (stored["state"], stored["delivered_to"]) == ("delivered", "Desk_B")


def test_failover_refuses_drafts_delivered_notes_and_ineligible_picks(org_chart):
    _session("s1", "Desk_A")
    draft = _propose()
    with pytest.raises(ToolError, match="seal"):
        handoff.failover(draft["id"], pick=lambda n: "Desk_B")
    note = handoff.seal(draft["id"], agent="Desk_A")
    with pytest.raises(ToolError, match="original agent"):
        handoff.failover(note["id"], pick=lambda n: "Desk_A")
    # Nobody else in the office is live.
    with pytest.raises(ToolError, match="no eligible agent"):
        handoff.failover(note["id"])
    handoff.failover(
        note["id"], pick=lambda n: "Desk_B", deliver=lambda n, to_agent: {"delivered": True}
    )
    with pytest.raises(ToolError, match="already delivered"):
        handoff.failover(note["id"], pick=lambda n: "Desk_C")


def test_failover_can_target_a_session_without_a_note_id():
    _session("s1", "Desk_A")
    with pytest.raises(ToolError, match="no sealed handoff note"):
        handoff.failover_session("s1", pick=lambda n: "Desk_B")
    note = handoff.seal(_propose()["id"], agent="Desk_A")
    assert handoff.failover_session("s1", pick=lambda n: "Desk_B")["note_id"] == note["id"]


def test_context_epochs_start_at_zero_and_bump_forward():
    _session("s1", "Desk_A")
    assert handoff.current_epoch("s1") == {
        "session_id": "s1",
        "epoch": 0,
        "reason": None,
        "started_at": None,
    }
    first = handoff.bump_epoch("s1", now=_clock(1))
    assert first["epoch"] == 1
    assert first["reason"] == "compaction"
    second = handoff.bump_epoch("s1", reason="resume", now=_clock(2))
    assert second["epoch"] == 2
    assert handoff.current_epoch("s1")["epoch"] == 2
    assert handoff.current_epoch("s1")["reason"] == "resume"
    # A note records the epoch it was written in.
    assert _propose(at=3)["epoch"] == 2
    with pytest.raises(ToolError, match="not found"):
        handoff.bump_epoch("nope")
    with pytest.raises(ToolError, match="reason"):
        handoff.bump_epoch("s1", reason="")


def test_note_text_is_redacted_and_bounded():
    _session("s1", "Desk_A")
    secret = "sk-ant-" + "a" * 40
    note = _propose(done=[f"used key {secret}", "x" * 5000], next_steps=["go"] * 40)
    assert secret not in note["done"][0]
    assert len(note["done"][1]) <= handoff.ITEM_MAX_CHARS
    assert len(note["next_steps"]) == handoff.ITEMS_MAX


def test_trust_tiers_label_packet_sources():
    packet = {
        "schema": "ua.session_packet.v1",
        "approvals": [],
        "operating_profile": None,
        "task": {},
        "recent_events": [],
        "memory": [],
        "prior_handoff": None,
        "mystery_section": 1,
    }
    labels = handoff.label_packet_sources(packet)
    assert labels["approvals"] == "T0"
    assert labels["operating_profile"] == "T0"
    assert labels["task"] == "T1"
    assert labels["recent_events"] == "T1"
    assert labels["memory"] == "T2"
    assert labels["prior_handoff"] == "T2"
    # Unknown sources fall to the least-trusted tier.
    assert labels["mystery_section"] == "T2"
    assert "schema" not in labels
    assert handoff.trust_tier("handoff_note") == "T2"
    assert set(handoff.TRUST_TIERS) == {"T0", "T1", "T2"}


# --- wiring: coordination actions, packet, compaction ---------------------------


def _coord(action, **params):
    from unified_agent.tools import graph

    return graph.coordination_manage(action, params)


def test_coordination_actions_act_as_the_verified_label(monkeypatch):
    from unified_agent import config

    _session("s1", "Desk_A")
    _session("s2", "Desk_A")
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desk_A")
    draft = _coord("session_handoff_propose", session_id="s1", done=["a"], next_steps=["b"])
    assert draft["agent"] == "Desk_A" and draft["state"] == "draft"
    edited = _coord("session_handoff_update", note_id=draft["id"], open_questions=["why?"])
    assert edited["open_questions"] == ["why?"]
    assert _coord("session_handoff_recover", session_id="s1") == {"note": None}
    sealed = _coord("SESSION_HANDOFF_SEAL", note_id=draft["id"])
    assert sealed["state"] == "sealed"
    assert _coord("session_handoff_recover", session_id="s1")["note"]["id"] == draft["id"]
    other = _coord("session_handoff_propose", session_id="s2", done=["a"], next_steps=["b"])
    assert _coord("session_handoff_discard", note_id=other["id"])["state"] == "discarded"
    # A verified caller cannot name someone else, nor write another agent's session.
    with pytest.raises(ToolError, match="identity mismatch"):
        _coord("session_handoff_seal", note_id=draft["id"], agent="Desk_B")
    monkeypatch.setattr(config, "verified_agent_identity", lambda: "Desk_B")
    with pytest.raises(ToolError):
        _coord("session_handoff_propose", session_id="s2", done=["a"], next_steps=["b"])


def test_coordination_actions_take_agent_only_without_auth():
    _session("s1", "Desk_A")
    with pytest.raises(ToolError, match="identity is required"):
        _coord("session_handoff_propose", session_id="s1", done=["a"], next_steps=["b"])
    note = _coord(
        "session_handoff_propose", session_id="s1", done=["a"], next_steps=["b"], agent="Desk_A"
    )
    assert note["agent"] == "Desk_A"
    with pytest.raises(ToolError, match="invalid params"):
        _coord("session_handoff_recover", session_id="s1", now=None)


def test_packet_carries_sealed_note_epoch_and_source_tiers():
    _session("s1", "Desk_A")
    note = handoff.seal(_propose()["id"], agent="Desk_A", now=_clock(1))
    handoff.bump_epoch("s1", agent="Desk_A", now=_clock(2))
    packet = build_session_packet(session_id="s1", agent="Desk_A")
    assert packet["handoff_note"]["id"] == note["id"]
    assert packet["context_epoch"]["epoch"] == 1
    tiers = packet["labels"]["source_tiers"]
    assert tiers["handoff_note"] == "T2"
    assert tiers["context_epoch"] == "T1"
    assert tiers["approvals"] == "T0"
    assert tiers["workflow"] == "T2"
    assert "schema" not in tiers and "labels" not in tiers
    # Without a session there is nothing to recover.
    bare = build_session_packet(task_id=None, session_id="missing")
    assert bare["handoff_note"] is None and bare["context_epoch"] is None


def test_packet_survives_a_failed_handoff_read(monkeypatch):
    _session("s1", "Desk_A")

    def boom(_sid):
        raise sqlite3.OperationalError("no such table: session_handoff_notes")

    monkeypatch.setattr(handoff, "recover", boom)
    monkeypatch.setattr(handoff, "current_epoch", boom)
    packet = build_session_packet(session_id="s1")
    assert packet["handoff_note"] is None and packet["context_epoch"] is None
    assert packet["session"]["id"] == "s1"


def test_oversized_packet_drops_the_handoff_note_before_collapsing():
    from unified_agent.lifecycle import packet as packet_mod

    big = {"id": 1, "done": ["x" * 500] * 20, "next_steps": ["y" * 500] * 20}
    bounded = packet_mod._bound_packet(
        {
            "schema": packet_mod.PACKET_SCHEMA,
            "labels": {},
            "task": {"id": 7, "title": "t", "status": "doing"},
            "handoff_note": big,
            "context_epoch": {"epoch": 3},
        }
    )
    assert bounded["handoff_note"] is None
    assert bounded["context_epoch"] == {"epoch": 3}
    assert bounded["task"]["id"] == 7
