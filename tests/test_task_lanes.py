import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage
from unified_agent.tools.tasks import (
    DEFAULT_LANE,
    task_add,
    task_claim,
    task_list,
    task_update,
)


def test_task_add_defaults_lane_and_filters_by_lane():
    t = task_add("lane default task")
    assert task_list(lane=DEFAULT_LANE)["tasks"]
    assert any(x["id"] == t["id"] for x in task_list(lane=DEFAULT_LANE)["tasks"])
    assert t["id"] not in {x["id"] for x in task_list(lane="laptop")["tasks"]}


def test_task_add_explicit_lane_and_update_relane():
    t = task_add("laptop work", lane="laptop")
    row = task_list(lane="laptop")["tasks"]
    assert any(x["id"] == t["id"] for x in row)
    task_update(t["id"], lane="default")
    assert t["id"] in {x["id"] for x in task_list(lane=DEFAULT_LANE)["tasks"]}


def test_task_claim_lane_scope_rejects_wrong_lane():
    t = task_add("scoped", lane="laptop")
    with pytest.raises(ToolError, match="lane 'laptop'"):
        task_claim(t["id"], agent="Desktop_Agent4")
    claimed = task_claim(t["id"], agent="Desktop_Agent4", expected_lane="laptop")
    assert claimed["status"] == "doing"


def test_agent_wip_cap_refuses_third_claim(monkeypatch):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "2")
    ids = [task_add(f"wip {i}")["id"] for i in range(3)]
    task_claim(ids[0], agent="Desktop_Agent5")
    task_claim(ids[1], agent="Desktop_Agent5")
    with pytest.raises(ToolError, match="WIP cap"):
        task_claim(ids[2], agent="Desktop_Agent5")


def test_agent_wip_overrides_set_one_labels_claim_cap(monkeypatch):
    # UA_AGENT_WIP_OVERRIDES raises or lowers the cap for the labels it names;
    # every other label keeps UA_AGENT_WIP_MAX.
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "2")
    monkeypatch.setenv("UA_AGENT_WIP_OVERRIDES", "Desktop_Agent5:4, Desktop_agent3:1")
    monkeypatch.setenv("UA_FLEET_WIP_MAX", "50")
    monkeypatch.setenv("UA_FLEET_WIP_HARD_MAX", "50")
    for agent, cap in (("Desktop_Agent5", 4), ("Desktop_Agent2", 2), ("Desktop_agent3", 1)):
        for _ in range(cap):
            task_claim(task_add(f"{agent} wip")["id"], agent=agent)
        extra = task_add(f"{agent} over")["id"]
        held = rf"'{agent}' already holds {cap} doing task\(s\) \(max {cap}\)"
        with pytest.raises(ToolError, match=held):
            task_claim(extra, agent=agent)


def test_expired_lock_not_counted_toward_wip(monkeypatch):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "1")
    t1 = task_add("held")["id"]
    t2 = task_add("next")["id"]
    task_claim(t1, agent="Desktop_Agent5", timeout_seconds=3600)
    conn = storage.connect()
    conn.execute("UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (t1,))
    conn.commit()
    conn.close()
    claimed = task_claim(t2, agent="Desktop_Agent5")
    assert claimed["claimed_by"] == "Desktop_Agent5"


def test_lane_wip_soft_warns_hard_refuses(monkeypatch):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "10")
    monkeypatch.setenv("UA_LANE_WIP", "laptop:1:soft")
    a = task_add("laptop a", lane="laptop")["id"]
    b = task_add("laptop b", lane="laptop")["id"]
    task_claim(a, agent="Desktop_Agent4", expected_lane="laptop")
    warned = task_claim(b, agent="Desktop_Agent4", expected_lane="laptop")
    assert warned.get("wip_warnings")

    monkeypatch.setenv("UA_LANE_WIP", "laptop:1:hard")
    c = task_add("laptop c", lane="laptop")["id"]
    with pytest.raises(ToolError, match="mode hard"):
        task_claim(c, agent="Desktop_Agent4", expected_lane="laptop")


def test_agent_lane_claimable_without_expected_lane():
    t = task_add("agent lane task", lane="Desktop_Agent5")["id"]
    claimed = task_claim(t, agent="Desktop_Agent5")
    assert claimed["lane"] == "Desktop_Agent5"
    assert claimed["status"] == "doing"


def test_task_update_relane_refuses_full_hard_lane(monkeypatch):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "10")
    monkeypatch.setenv("UA_LANE_WIP", "laptop:1:hard")
    a = task_add("default lane", lane="default")["id"]
    task_claim(a, agent="Desktop_Agent4")
    occupied = task_add("laptop full", lane="laptop")["id"]
    task_claim(occupied, agent="Desktop_Agent4", expected_lane="laptop")
    with pytest.raises(ToolError, match="mode hard"):
        task_update(a, lane="laptop")


def test_init_db_populates_lane_column(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    t = task_add("lane column present")["id"]
    conn = storage.connect()
    try:
        row = conn.execute("SELECT lane FROM tasks WHERE id=?", (t,)).fetchone()
        assert row["lane"] == DEFAULT_LANE
        nulls = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE lane IS NULL OR lane=''"
        ).fetchone()["n"]
        assert nulls == 0
    finally:
        conn.close()


# --- actor lane model (task #883) --------------------------------------------


def test_legacy_portfolio_lanes_rejected_on_write():
    for bad in ("personal", "professional", "hobby"):
        with pytest.raises(ToolError, match="portfolio bucket"):
            task_add("legacy lane", lane=bad)


def test_domain_does_not_leak_into_lane():
    t = task_add("personal-domain task", domain="personal")
    assert t["id"] in {x["id"] for x in task_list(lane=DEFAULT_LANE)["tasks"]}
    conn = storage.connect()
    try:
        row = conn.execute("SELECT lane, domain FROM tasks WHERE id=?", (t["id"],)).fetchone()
        assert row["lane"] == DEFAULT_LANE
        assert row["domain"] == "personal"
    finally:
        conn.close()


def test_dispatch_ready_is_open_pool():
    t = task_add("vetted work", lane="dispatch-ready")["id"]
    claimed = task_claim(t, agent="Desktop_Agent5")  # no expected_lane
    assert claimed["status"] == "doing"
    assert claimed["lane"] == "dispatch-ready"


def test_tracking_lane_is_unclaimable():
    t = task_add("roadmap umbrella", lane="tracking")["id"]
    with pytest.raises(ToolError):
        task_claim(t, agent="Desktop_Agent5")
    with pytest.raises(ToolError):
        task_claim(t, agent="Desktop_Agent5", expected_lane="tracking")


def test_operator_only_needs_explicit_lane():
    t = task_add("human step", lane="operator-only")["id"]
    with pytest.raises(ToolError, match="lane 'operator-only'"):
        task_claim(t, agent="Desktop_Agent5")
    claimed = task_claim(t, agent="Desktop_Agent5", expected_lane="operator-only")
    assert claimed["status"] == "doing"


def test_interactive_only_is_distinct_and_requires_explicit_lane():
    assert "interactive-only" in config.TASK_SYSTEM_LANES
    assert "interactive-only" not in config.TASK_OPEN_POOL_LANES
    t = task_add("interactive step", lane="interactive-only")["id"]
    assert t in {row["id"] for row in task_list(lane="interactive-only")["tasks"]}
    assert t not in {row["id"] for row in task_list(lane="dispatch-ready")["tasks"]}
    with pytest.raises(ToolError, match="lane 'interactive-only'"):
        task_claim(t, agent="Desktop_Agent5")
    with pytest.raises(ToolError, match="lane 'interactive-only'"):
        task_claim(t, agent="Desktop_Agent5", expected_lane="dispatch-ready")
    claimed = task_claim(t, agent="Desktop_Agent5", expected_lane="interactive-only")
    assert claimed["lane"] == "interactive-only"


def test_interactive_lane_can_be_assigned_on_update():
    t = task_add("needs an operator")["id"]
    task_update(t, lane="interactive-only")
    assert t in {row["id"] for row in task_list(lane="interactive-only")["tasks"]}


def test_migration_remaps_legacy_lane_values(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    conn = storage.connect()
    try:
        ts = storage.now()
        conn.execute(
            "INSERT INTO tasks (title, status, priority, created_at, updated_at, lane) "
            "VALUES ('legacy', 'open', 'med', ?, ?, 'professional')",
            (ts, ts),
        )
        conn.commit()
        storage._migrate(conn)
        rows = conn.execute("SELECT lane FROM tasks WHERE title='legacy'").fetchall()
        assert [r["lane"] for r in rows] == [DEFAULT_LANE]
    finally:
        conn.close()


@pytest.mark.parametrize("lane", ["operator-only", config.LEGACY_OPERATOR_LANE])
def test_operator_lane_aliases_canonicalize_add_update_filter_and_claim(lane, monkeypatch):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "10")
    task_id = task_add("operator compatibility", lane=lane)["id"]
    listed = task_list(lane=lane)["tasks"]
    assert next(t for t in listed if t["id"] == task_id)["lane"] == "operator-only"
    with pytest.raises(ToolError, match="lane 'operator-only'"):
        task_claim(task_id, agent="Desktop_Agent5")
    with pytest.raises(ToolError, match="lane 'operator-only'"):
        task_claim(task_id, agent="Desktop_Agent5", expected_lane="dispatch-ready")
    claimed = task_claim(task_id, agent="Desktop_Agent5", expected_lane=lane)
    assert claimed["lane"] == "operator-only"
    task_update(task_id, lane="default")
    task_update(task_id, lane=lane)
    assert (
        next(t for t in task_list(status="all", lane=lane)["tasks"] if t["id"] == task_id)["lane"]
        == "operator-only"
    )


def test_operator_lane_migration_preserves_claim_run_and_timestamps():
    task_id = task_add("existing claimed human step", lane="operator-only")["id"]
    task_claim(task_id, agent="Desktop_Agent5", expected_lane="operator-only")
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET lane=? WHERE id=?", (config.LEGACY_OPERATOR_LANE, task_id))
        conn.commit()
        before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        events_before = [tuple(r) for r in conn.execute("SELECT * FROM event_log ORDER BY rowid")]
        storage._migrate(conn)
        storage._migrate(conn)
        after = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        assert after == {**before, "lane": "operator-only"}
        assert [
            tuple(r) for r in conn.execute("SELECT * FROM event_log ORDER BY rowid")
        ] == events_before
    finally:
        conn.close()


def test_operator_alias_wip_limit_applies_to_canonical_lane(monkeypatch):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "10")
    monkeypatch.setenv("UA_LANE_WIP", f"{config.LEGACY_OPERATOR_LANE}:1:hard")
    first = task_add("operator limit one", lane=config.LEGACY_OPERATOR_LANE)["id"]
    second = task_add("operator limit two", lane="operator-only")["id"]
    task_claim(first, agent="Desktop_Agent5", expected_lane=config.LEGACY_OPERATOR_LANE)
    with pytest.raises(ToolError, match="mode hard"):
        task_claim(second, agent="Desktop_Agent5", expected_lane="operator-only")


def test_operator_lane_inheritance_and_pre_migration_read():
    parent = task_add("legacy parent", lane="operator-only")["id"]
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET lane=? WHERE id=?", (config.LEGACY_OPERATOR_LANE, parent))
        conn.commit()
    finally:
        conn.close()
    assert parent in {t["id"] for t in task_list(lane=config.LEGACY_OPERATOR_LANE)["tasks"]}
    assert parent in {t["id"] for t in task_list(lane="operator-only")["tasks"]}
    child = task_add("inherited human step", parent_id=parent)["id"]
    assert next(t for t in task_list()["tasks"] if t["id"] == parent)["lane"] == "operator-only"
    assert next(t for t in task_list()["tasks"] if t["id"] == child)["lane"] == "operator-only"
