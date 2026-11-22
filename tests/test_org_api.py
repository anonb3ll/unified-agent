"""Org projection joins checked-in roles with live heartbeat and task state."""

import asyncio
import sqlite3

from unified_agent import org_registry
from unified_agent.webui import api


def test_org_payload_uses_heartbeats_and_epic_lineage(monkeypatch):
    office = {
        "id": "platform",
        "name": "Platform Engineering",
        "lead": "Desktop_Agent2",
        "members": ["Desktop_Agent2", "server_agent3"],
        "owned_epics": [1069],
        "default_lanes": ["dispatch-ready"],
        "decision_rights": ["implementation"],
        "default_profiles": {"Desktop_Agent2": "developer", "server_agent3": "developer"},
    }
    monkeypatch.setattr(org_registry, "load_registry", lambda: [office])
    monkeypatch.setattr(
        api.registry,
        "list_agents",
        lambda: [
            {"label": "Desktop_Agent2", "status": "online", "last_seen": "2026-09-22T21:00:00Z"}
        ],
    )

    async def queue():
        return (
            [
                {"id": 1, "lineage": [1069], "claimed_by": "Desktop_Agent2"},
                {"id": 2, "lineage": [1191], "claimed_by": "Desktop_Agent2"},
            ],
            None,
        )

    monkeypatch.setattr(api, "_active_queue_tasks", queue)
    result = asyncio.run(api.org_payload())
    row = result["offices"][0]
    assert row["open_tasks"] == 2
    assert row["members"][0]["status"] == "online"
    assert row["members"][1]["status"] == "offline"
    assert row["members"][0]["profile"] == "developer"


def test_exact_office_count_includes_nested_epic_and_claimed_work(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tasks (id INTEGER, parent_id INTEGER, claimed_by TEXT, "
        "status TEXT, archived_at TEXT, org_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO tasks (id, parent_id, claimed_by, status, archived_at) VALUES (?, ?, ?, ?, ?)",
        [
            (10, None, None, "done", None),
            (11, 10, None, "open", None),
            (12, 11, None, "doing", None),
            (13, None, "Desktop_Agent2", "blocked", None),
            (14, None, "Desktop_Agent2", "open", "archived"),
        ],
    )
    monkeypatch.setattr(api.storage, "connect", lambda: conn)
    office = {"id": "platform", "owned_epics": [10], "members": ["Desktop_Agent2"]}
    assert api._org_exact_open_counts([office]) == {"platform": 3}
