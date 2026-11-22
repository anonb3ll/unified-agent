"""Mission autonomy rows carry a tenant, and every mission read and write is fenced
(task #1978).

Before this, the autonomy tables were keyed by a global mission id with no
``org_id``: a tenanted caller could read another tenant's mission mode, grants,
revisions and pending actions, change its mode, and review or start its pending
actions. Another tenant's mission now behaves exactly like a missing one. The
private, untenanted hub is unchanged (every row keeps ``org_id IS NULL``).
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import autonomy_modes, storage
from unified_agent.goals import goal_add

HUMAN = "Desktop_Person"
AGENT = "Desktop_Agent2"
ORG_A = "org_a"
ORG_B = "org_b"
MISSION_TABLES = (
    "autonomy_mission_mode",
    "autonomy_mode_revisions",
    "autonomy_pending_actions",
    "autonomy_standing_grants",
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(tmp_path / "no-codex-sessions"))
    (tmp_path / "ws").mkdir()
    storage.init_db()


@pytest.fixture
def as_org(monkeypatch):
    def _switch(org: str | None) -> None:
        if org is None:
            monkeypatch.delenv("UA_EDITION", raising=False)
            monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
        else:
            monkeypatch.setenv("UA_EDITION", "enterprise")
            monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)

    return _switch


def _mission(title: str = "Ship the thing") -> int:
    return goal_add(title, author=HUMAN)["goal"]["id"]


def _org_ids(table: str, mission: int) -> set:
    conn = storage.connect()
    try:
        return {
            row["org_id"]
            for row in conn.execute(f"SELECT org_id FROM {table} WHERE mission_id=?", (mission,))
        }
    finally:
        conn.close()


def _seed_org_a_mission(as_org) -> tuple[int, int]:
    """Org A's mission in checkpoint mode with a grant and one paused deploy."""
    as_org(ORG_A)
    mission = _mission()
    autonomy_modes.set_mode(mission, "checkpoint", HUMAN)
    autonomy_modes.grant_standing(mission, "merge", HUMAN)
    pending = autonomy_modes.evaluate(mission, "deploy", AGENT)
    assert pending["decision"] == "pause_for_human"
    return mission, pending["pending_id"]


# --- schema and stamping ---------------------------------------------------------


def test_mission_tables_gain_a_nullable_org_id_idempotently():
    storage.init_db()  # a second initialization must not fail or duplicate anything
    conn = storage.connect()
    try:
        for table in MISSION_TABLES:
            columns = {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}
            assert "org_id" in columns, table
            assert columns["org_id"]["notnull"] == 0, table
    finally:
        conn.close()


def test_every_mission_row_is_stamped_with_the_caller_tenant(as_org):
    mission, _ = _seed_org_a_mission(as_org)
    for table in MISSION_TABLES:
        assert _org_ids(table, mission) == {ORG_A}, table


def test_untenanted_hub_rows_keep_a_null_org_id(as_org):
    as_org(None)
    mission = _mission()
    autonomy_modes.set_mode(mission, "checkpoint", HUMAN)
    autonomy_modes.grant_standing(mission, "merge", HUMAN)
    autonomy_modes.evaluate(mission, "deploy", AGENT)
    for table in MISSION_TABLES:
        assert _org_ids(table, mission) == {None}, table
    assert autonomy_modes.get_mode(mission)["mode"] == "checkpoint"


# --- another tenant's mission is a missing one ------------------------------------


def test_another_tenant_cannot_read_a_mission(as_org):
    mission, _ = _seed_org_a_mission(as_org)
    as_org(ORG_B)
    with pytest.raises(ToolError, match=f"unknown mission \\(goal\\): {mission}"):
        autonomy_modes.get_mode(mission)
    with pytest.raises(ToolError, match="unknown mission"):
        autonomy_modes.preview(mission)
    with pytest.raises(ToolError, match="unknown mission"):
        autonomy_modes.preview_action(mission, "deploy", AGENT)


def test_another_tenant_cannot_change_a_missions_mode_or_grants(as_org):
    mission, _ = _seed_org_a_mission(as_org)
    as_org(ORG_B)
    with pytest.raises(ToolError, match="unknown mission"):
        autonomy_modes.set_mode(mission, "observe", HUMAN)
    with pytest.raises(ToolError, match="unknown mission"):
        autonomy_modes.grant_standing(mission, "deploy", HUMAN)
    with pytest.raises(ToolError, match="unknown mission"):
        autonomy_modes.revoke_grant(mission, "merge", HUMAN)
    with pytest.raises(ToolError, match="unknown mission"):
        autonomy_modes.propose_mode(mission, "observe", AGENT)

    as_org(ORG_A)
    state = autonomy_modes.get_mode(mission)
    assert state["mode"] == "checkpoint"
    assert state["standing_grants"] == ["merge"]
    assert len(state["revisions"]) == 2


def test_another_tenant_cannot_evaluate_against_a_mission(as_org):
    mission, pending_id = _seed_org_a_mission(as_org)
    as_org(ORG_B)
    with pytest.raises(ToolError, match="unknown mission"):
        autonomy_modes.evaluate(mission, "deploy", AGENT)
    for table in MISSION_TABLES:
        assert _org_ids(table, mission) == {ORG_A}, table


def test_another_tenant_cannot_review_or_start_a_pending_action(as_org):
    _mission_id, pending_id = _seed_org_a_mission(as_org)
    as_org(ORG_B)
    with pytest.raises(ToolError, match="unknown not-started pending autonomy action"):
        autonomy_modes.review_action(pending_id, HUMAN)
    with pytest.raises(ToolError, match="unknown pending autonomy action"):
        autonomy_modes.mark_started(pending_id, AGENT)

    as_org(ORG_A)
    reviewed = autonomy_modes.review_action(pending_id, HUMAN)
    assert reviewed["decision"] == "proceed"
    assert autonomy_modes.mark_started(pending_id, AGENT)["started"] == 1


def test_each_tenant_keeps_its_own_missions(as_org):
    mission_a, _ = _seed_org_a_mission(as_org)
    as_org(ORG_B)
    mission_b = _mission("B's mission")
    autonomy_modes.set_mode(mission_b, "observe", HUMAN)
    assert autonomy_modes.evaluate(mission_b, "merge", AGENT)["decision"] == "proceed"
    assert autonomy_modes.preview(mission_b)["mode"] == "observe"

    as_org(ORG_A)
    assert autonomy_modes.get_mode(mission_a)["mode"] == "checkpoint"
    with pytest.raises(ToolError, match="unknown mission"):
        autonomy_modes.get_mode(mission_b)


def test_untenanted_mission_rows_are_not_a_tenants(as_org):
    as_org(None)
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)
    as_org(ORG_A)
    with pytest.raises(ToolError, match="unknown mission"):
        autonomy_modes.get_mode(mission)
    with pytest.raises(ToolError, match="unknown mission"):
        autonomy_modes.set_mode(mission, "checkpoint", HUMAN)
    as_org(None)
    assert autonomy_modes.get_mode(mission)["mode"] == "observe"
