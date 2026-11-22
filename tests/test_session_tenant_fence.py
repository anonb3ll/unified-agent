"""Agent sessions carry a tenant, and session reads and writes are fenced (task #1995).

Before this, ``agent_sessions`` had no ``org_id``: a tenanted caller listed
another tenant's sessions (agents, worktree paths) and could end one, writing
its own summary into the other tenant's row.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import mcp_live_state, object_model, registry, storage, work_graph
from unified_agent.lifecycle import packet
from unified_agent.tools import registry as registry_tools
from unified_agent.tools import tasks as task_tools

ORG_A = "org_a"
ORG_B = "org_b"
AGENT = "Desktop_Agent2"


@pytest.fixture
def as_org(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")

    def _switch(org: str) -> None:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)

    return _switch


@pytest.fixture
def untenanted(monkeypatch):
    monkeypatch.delenv("UA_EDITION", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)


def _start(external: str | None = None, **kwargs) -> str:
    return registry_tools.session_start(
        kind="claude_code", agent=AGENT, external_session_id=external, **kwargs
    )["session_id"]


def _row(session_id: str):
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT status, ended_at, summary, org_id FROM agent_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()


def _ids(listed: dict) -> set[str]:
    return {s["id"] for s in listed["sessions"]}


# --- stamping ----------------------------------------------------------------------


def test_new_session_is_stamped_with_the_caller_tenant(as_org):
    as_org(ORG_A)
    assert _row(_start())["org_id"] == ORG_A


# --- session_list --------------------------------------------------------------------


def test_session_list_shows_only_own_tenant_sessions(as_org):
    as_org(ORG_A)
    a_sid = _start(worktree_path="/work/a")
    as_org(ORG_B)
    b_sid = _start(worktree_path="/work/b")

    listed = registry_tools.session_list()
    assert _ids(listed) == {b_sid}
    assert listed["total"] == 1
    assert all(s["worktree_path"] != "/work/a" for s in listed["sessions"])
    as_org(ORG_A)
    assert _ids(registry_tools.session_list()) == {a_sid}


def test_live_sessions_and_agent_counts_are_tenant_scoped(as_org):
    as_org(ORG_A)
    a_sid = _start()
    as_org(ORG_B)
    assert registry.list_live_sessions() == []
    counts = {a["label"]: a["open_sessions"] for a in registry.list_agents()}
    assert counts.get(AGENT, 0) == 0
    as_org(ORG_A)
    assert [s["session_id"] for s in registry.list_live_sessions()] == [a_sid]
    counts = {a["label"]: a["open_sessions"] for a in registry.list_agents()}
    assert counts[AGENT] == 1


# --- session_end ---------------------------------------------------------------------


def test_session_end_on_foreign_session_fails_like_a_missing_one(as_org):
    as_org(ORG_A)
    a_sid = _start()
    before = tuple(_row(a_sid))

    as_org(ORG_B)
    with pytest.raises(ToolError) as foreign:
        registry_tools.session_end(a_sid, summary="B WROTE THIS")
    missing_id = "01MISSINGSESSION0000000000"
    with pytest.raises(ToolError) as missing:
        registry_tools.session_end(missing_id, summary="B WROTE THIS")
    assert str(foreign.value) == str(missing.value).replace(missing_id, a_sid)
    # The registry write path refuses too, with the same False as a missing id.
    assert registry.end_session(a_sid, summary="B WROTE THIS") is False
    assert registry.end_session(missing_id) is False

    assert tuple(_row(a_sid)) == before
    assert _row(a_sid)["summary"] is None
    assert _row(a_sid)["status"] == "running"

    as_org(ORG_A)
    assert registry_tools.session_end(a_sid, summary="A wrote this")["closed"] is True
    assert _row(a_sid)["summary"] == "A wrote this"


def test_internal_end_session_can_skip_the_fence(as_org):
    as_org(ORG_A)
    a_sid = _start()
    as_org(ORG_B)
    assert registry.end_session(a_sid, status="failed", tenant_scoped=False) is True
    assert _row(a_sid)["status"] == "failed"


# --- get_session and other id readers ---------------------------------------------------


def test_get_session_treats_a_foreign_session_as_missing(as_org):
    as_org(ORG_A)
    a_sid = _start()
    as_org(ORG_B)
    assert registry.get_session(a_sid) is None
    as_org(ORG_A)
    assert registry.get_session(a_sid)["id"] == a_sid


def test_touch_and_packet_load_ignore_foreign_sessions(as_org):
    as_org(ORG_A)
    a_sid = _start()
    as_org(ORG_B)
    assert registry.touch_session(a_sid) is False
    conn = storage.connect()
    try:
        assert packet._load_session(conn, a_sid) is None
    finally:
        conn.close()
    as_org(ORG_A)
    assert registry.touch_session(a_sid) is True
    conn = storage.connect()
    try:
        assert packet._load_session(conn, a_sid)["id"] == a_sid
    finally:
        conn.close()


def test_register_session_never_reopens_a_foreign_row(as_org):
    as_org(ORG_A)
    a_sid = _start(external="ext-shared")
    as_org(ORG_B)
    b_sid, created = registry.register_session(
        AGENT, "claude_code", external_session_id="ext-shared"
    )
    assert created is True
    assert b_sid != a_sid
    as_org(ORG_A)
    again, created = registry.register_session(
        AGENT, "claude_code", external_session_id="ext-shared"
    )
    assert (again, created) == (a_sid, False)


def test_most_recent_prior_session_is_tenant_scoped(as_org):
    as_org(ORG_A)
    task_id = task_tools.task_add(title="shared")["id"]
    a_sid = _start(task_id=task_id)
    as_org(ORG_B)
    assert registry.most_recent_prior_session(task_id, exclude_session_id="x") is None
    as_org(ORG_A)
    assert registry.most_recent_prior_session(task_id, exclude_session_id="x")["id"] == a_sid


def test_tenanted_caller_sees_its_own_taskless_session(as_org):
    as_org(ORG_A)
    sid = _start()
    assert _row(sid)["org_id"] == ORG_A
    assert registry.get_session(sid)["task_id"] is None
    assert sid in _ids(registry_tools.session_list())


# --- lower-risk readers --------------------------------------------------------------


def test_live_state_snapshot_omits_foreign_sessions(as_org):
    as_org(ORG_A)
    a_sid = _start()
    as_org(ORG_B)
    b_sid = _start()
    session_type = object_model.by_kind("session")
    revisions, _cursor = mcp_live_state.snapshot_with_cursor()
    clock = mcp_live_state._session_clock_snapshot()
    assert session_type.render(b_sid) in revisions
    assert session_type.render(a_sid) not in revisions
    assert set(clock) == {session_type.render(b_sid)}


def test_work_graph_sessions_are_tenant_scoped(as_org):
    as_org(ORG_A)
    task_id = task_tools.task_add(title="graph")["id"]
    a_sid = _start(task_id=task_id)
    as_org(ORG_B)
    # session_start refuses another tenant's task now; plant the row directly,
    # as one written before that guard existed would be.
    b_sid = _start()
    conn = storage.connect()
    try:
        conn.execute("UPDATE agent_sessions SET task_id=? WHERE id=?", (task_id, b_sid))
        conn.commit()
    finally:
        conn.close()
    builder = work_graph._Builder()
    conn = storage.connect()
    try:
        work_graph._add_sessions_for_tasks(builder, conn, [task_id])
    finally:
        conn.close()
    assert f"session:{b_sid}" in builder.nodes
    assert f"session:{a_sid}" not in builder.nodes


# --- migration ----------------------------------------------------------------------


def test_backfill_sets_org_id_from_the_session_task(as_org):
    as_org(ORG_A)
    tenant_task = task_tools.task_add(title="tenant task")["id"]
    with_task = _start(task_id=tenant_task)
    taskless = _start()
    conn = storage.connect()
    try:
        conn.execute("DROP INDEX IF EXISTS idx_agent_sessions_org_id")
        conn.execute("ALTER TABLE agent_sessions DROP COLUMN org_id")
        conn.commit()
        storage._add_agent_session_org_id(conn)
        storage._add_agent_session_org_id(conn)  # idempotent
        conn.commit()
        indexes = {r["name"] for r in conn.execute("PRAGMA index_list(agent_sessions)")}
    finally:
        conn.close()
    assert "idx_agent_sessions_org_id" in indexes
    assert _row(with_task)["org_id"] == ORG_A
    # No task to inherit from: stays untenanted.
    assert _row(taskless)["org_id"] is None


def test_backfill_leaves_sessions_on_untenanted_tasks_null(untenanted):
    task_id = task_tools.task_add(title="untenanted task")["id"]
    sid = _start(task_id=task_id)
    conn = storage.connect()
    try:
        conn.execute("DROP INDEX IF EXISTS idx_agent_sessions_org_id")
        conn.execute("ALTER TABLE agent_sessions DROP COLUMN org_id")
        storage._migrate(conn)
        conn.commit()
    finally:
        conn.close()
    assert _row(sid)["org_id"] is None


# --- untenanted hub -----------------------------------------------------------------


def test_untenanted_hub_is_unchanged(untenanted):
    task_id = task_tools.task_add(title="plain")["id"]
    sid = _start(task_id=task_id)
    other = _start()
    assert _row(sid)["org_id"] is None
    assert _ids(registry_tools.session_list()) == {sid, other}
    assert registry.get_session(sid)["id"] == sid
    assert registry.most_recent_prior_session(task_id, exclude_session_id="x")["id"] == sid
    assert registry.touch_session(other) is True
    result = registry_tools.session_end(sid, summary="done")
    assert result["closed"] is True
    assert _row(sid)["summary"] == "done"


def test_untenanted_hub_does_not_see_tenant_rows(as_org, monkeypatch):
    """A tenant-stamped row is not NULL, so an untenanted read skips it too."""
    as_org(ORG_A)
    a_sid = _start()
    monkeypatch.delenv("UA_EDITION", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    plain = _start()
    assert _ids(registry_tools.session_list()) == {plain}
    assert registry.get_session(a_sid) is None


# --- sessions on another tenant's task (review round 1) ---------------------------


def test_session_start_refuses_another_tenants_task(as_org):
    as_org(ORG_A)
    task = task_tools.task_add("ORG A CONFIDENTIAL TITLE")["id"]
    as_org(ORG_B)
    with pytest.raises(ToolError, match=f"no task with id {task}"):
        _start(task_id=task)
    listed = registry_tools.session_list()
    assert "ORG A CONFIDENTIAL TITLE" not in str(listed)


def test_a_session_row_never_carries_another_tenants_task_title(as_org):
    # Even a row that names a foreign task (written before the guard) shows no
    # title or status from it.
    as_org(ORG_A)
    task = task_tools.task_add("ORG A CONFIDENTIAL TITLE")["id"]
    as_org(ORG_B)
    sid = _start()
    conn = storage.connect()
    try:
        conn.execute("UPDATE agent_sessions SET task_id=? WHERE id=?", (task, sid))
        conn.commit()
    finally:
        conn.close()
    assert "ORG A CONFIDENTIAL TITLE" not in str(registry_tools.session_list())
    assert "ORG A CONFIDENTIAL TITLE" not in str(registry.get_session(sid))


def test_experience_lists_only_own_tenant_session_summaries(as_org):
    as_org(ORG_A)
    task = task_tools.task_add("shared id work")["id"]
    own = _start(task_id=task)
    registry.end_session(own, summary="org a own summary")
    # A foreign row on the same task id (written before the guard existed).
    as_org(ORG_B)
    foreign = _start()
    conn = storage.connect()
    try:
        conn.execute("UPDATE agent_sessions SET task_id=? WHERE id=?", (task, foreign))
        conn.commit()
    finally:
        conn.close()
    registry.end_session(foreign, summary="ORG B SECRET SUMMARY")
    as_org(ORG_A)
    text = str(packet.build_session_packet(task_id=task)["experience"])
    assert "org a own summary" in text and "ORG B SECRET SUMMARY" not in text


def test_a_tenants_own_taskless_session_is_in_its_packet(as_org):
    as_org(ORG_A)
    sid = _start()
    built = packet.build_session_packet(session_id=sid)
    assert built["session"] is not None and built["session"]["id"] == sid
    as_org(ORG_B)
    assert packet.build_session_packet(session_id=sid)["session"] is None


def test_operator_end_is_the_verified_operators_fenced_and_ledgered(as_org, monkeypatch):
    """Task #2480: session.operator_end ends another identity's session for the
    verified human operator only, records owner and reason in the ledger, and keeps
    the tenant fence."""
    from unified_agent import config, events
    from unified_agent.tool_errors import ConflictError

    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Operator")
    as_org(ORG_A)
    own = _start()
    as_org(ORG_B)
    foreign = _start()
    as_org(ORG_A)

    def end(identity, session_id, operator="Desktop_Operator"):
        token = config.agent_identity_var.set(identity)
        try:
            return registry_tools.session_operator_end(
                session_id, operator=operator, reason="runaway loop"
            )
        finally:
            config.agent_identity_var.reset(token)

    for identity, operator in ((AGENT, AGENT), (AGENT, "Desktop_Operator"), (None, None)):
        with pytest.raises(ToolError, match="verified human operator"):
            end(identity, own, operator)
    with pytest.raises(ToolError, match="no such session"):
        end("Desktop_Operator", foreign)
    assert _row(foreign)["ended_at"] is None
    result = end("Desktop_Operator", own)
    assert result == {
        "session_id": own,
        "owner": AGENT,
        "status": "abandoned",
        "ended_by": "Desktop_Operator",
        "closed": True,
        "event_ulid": result["event_ulid"],
        "outstanding": result["outstanding"],
    }
    row = _row(own)
    assert row["status"] == "abandoned" and row["ended_at"]
    [event] = events.query_events(channels=["session.operator_ended"])
    assert event["originator"] == "Desktop_Operator"
    expected = {"owner": AGENT, "reason": "runaway loop", "session_id": own}
    assert expected.items() <= event["payload"].items()
    with pytest.raises(ConflictError, match="already ended"):
        end("Desktop_Operator", own)


def _operator_end_as_operator(session_id: str, reason: str) -> dict:
    from unified_agent import config

    token = config.agent_identity_var.set("Desktop_Operator")
    try:
        return registry_tools.session_operator_end(
            session_id, operator="Desktop_Operator", reason=reason
        )
    finally:
        config.agent_identity_var.reset(token)


def test_operator_end_is_atomic_with_its_ledger_event(untenanted, monkeypatch):
    """Review #1516 blocker: an append failure must not leave an ended session
    without its evidence event, and the retry must succeed."""
    from unified_agent import events

    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Operator")
    session = _start()
    real = events.record_event_once_on_connection

    def boom(*args, **kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(events, "record_event_once_on_connection", boom)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        _operator_end_as_operator(session, "first try")
    assert _row(session)["ended_at"] is None
    monkeypatch.setattr(events, "record_event_once_on_connection", real)
    ended = _operator_end_as_operator(session, "second try")
    assert ended["closed"] is True and _row(session)["ended_at"]
    [event] = events.query_events(channels=["session.operator_ended"])
    assert event["payload"]["reason"] == "second try"


def test_competing_operator_ends_have_one_winner(untenanted, monkeypatch):
    """Review #1516 blocker: two concurrent ends must not both pass the open check."""
    import threading

    from unified_agent import events
    from unified_agent.tool_errors import ConflictError

    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Operator")
    session = _start()
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}
    real_connect = storage.connect

    def synced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        if threading.current_thread().name.startswith("end-"):
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
        return conn

    monkeypatch.setattr(storage, "connect", synced_connect)

    def run(reason: str) -> None:
        try:
            outcomes[reason] = _operator_end_as_operator(session, reason)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            outcomes[reason] = exc

    threads = [threading.Thread(target=run, args=(r,), name=f"end-{r}") for r in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    monkeypatch.setattr(storage, "connect", real_connect)
    winners = [r for r, out in outcomes.items() if isinstance(out, dict)]
    losers = [r for r, out in outcomes.items() if isinstance(out, ConflictError)]
    assert len(winners) == 1 and len(losers) == 1, outcomes
    [event] = events.query_events(channels=["session.operator_ended"])
    assert event["payload"]["reason"] == winners[0]
    assert winners[0] in _row(session)["summary"]


def test_operator_ended_session_does_not_revive_and_reports_outstanding_work(
    untenanted, monkeypatch
):
    """Review #1516: a heartbeat or a re-registration must not reopen a session the
    operator ended; the owner starts a new one. Claims are reported, not released."""
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Operator")
    task = task_tools.task_add(title="still claimed")
    task_tools.task_claim(task["id"], AGENT)
    session = _start("ext-1", task_id=task["id"])
    ended = _operator_end_as_operator(session, "runaway")
    assert ended["status"] == "abandoned"
    assert task["id"] in ended["outstanding"]["claimed_task_ids"]
    assert "open_delegation_ids" in ended["outstanding"]
    assert registry.touch_session(session) is False
    assert _row(session)["status"] == "abandoned" and _row(session)["ended_at"]
    again, created = registry.register_session(AGENT, "claude_code", external_session_id="ext-1")
    assert created is True and again != session
    assert _row(session)["ended_at"]
    claimed = task_tools.task_list(status="doing")["tasks"]
    assert any(t["id"] == task["id"] and t["claimed_by"] == AGENT for t in claimed)


@pytest.mark.parametrize(
    "later_end", [("abandoned", "no_heartbeat"), ("completed", "owner closed it")]
)
def test_operator_end_marker_survives_later_ends(untenanted, monkeypatch, later_end):
    """#2502: a later cleanup or owner end_session rewrote status and exit_status and
    erased the marker, after which a heartbeat or a re-registration revived the row."""
    monkeypatch.setenv("UA_OPERATOR_LABEL", "Desktop_Operator")
    session = _start("ext-2502")
    _operator_end_as_operator(session, "runaway")
    status, exit_status = later_end
    assert registry.end_session(session, status=status, exit_status=exit_status)
    assert registry.touch_session(session) is False
    assert _row(session)["status"] == status and _row(session)["ended_at"]
    again, created = registry.register_session(AGENT, "claude_code", external_session_id="ext-2502")
    assert created is True and again != session
    assert _row(session)["ended_at"]
