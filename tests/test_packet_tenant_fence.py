"""Session packets are tenant-fenced and list only their task's approvals (task #1985)."""

from __future__ import annotations

import json

import pytest

from unified_agent import storage
from unified_agent.lifecycle.packet import build_session_packet
from unified_agent.tools import tasks as task_tools

ORG_A = "org_a"
ORG_B = "org_b"


@pytest.fixture
def as_org(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")

    def _switch(org: str) -> None:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)

    return _switch


def _approval(payload: dict, org: str | None, tool: str = "delegate") -> int:
    ts = storage.now()
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO approvals (tool, payload, requested_by, status, created_at, "
            "updated_at, org_id) VALUES (?, ?, 'worker', 'pending', ?, ?, ?)",
            (tool, json.dumps(payload), ts, ts, org),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_another_tenants_task_reads_as_missing_with_no_derived_sections(as_org):
    as_org(ORG_A)
    task = task_tools.task_add("tenant a work", notes="private context")["id"]
    _approval({"task_id": task}, ORG_A)
    as_org(ORG_B)
    packet = build_session_packet(task_id=task)
    assert packet["task"] is None
    assert packet["approvals"] == [] and packet["delegations"] == []
    assert "private context" not in json.dumps(packet)


def test_approvals_are_only_this_tasks_and_this_tenants(as_org):
    as_org(ORG_A)
    task = task_tools.task_add("mine")["id"]
    other = task_tools.task_add("someone else's")["id"]
    mine = _approval({"task_id": task}, ORG_A)
    child = _approval({"parent_task_id": task}, ORG_A, tool="delegate")
    _approval({"task_id": other}, ORG_A)
    _approval({"no_task": True}, ORG_A)
    _approval({"task_id": task}, ORG_B)  # same id, other tenant
    _approval({"task_id": str(task)}, ORG_A, tool="string-id")  # ids compared as text
    ids = {a["id"] for a in build_session_packet(task_id=task)["approvals"]}
    assert mine in ids and child in ids
    assert len(ids) == 3


def test_untenanted_hub_still_builds_a_full_packet():
    task = task_tools.task_add("solo hub work")["id"]
    approval = _approval({"task_id": task}, None)
    packet = build_session_packet(task_id=task)
    assert packet["task"]["id"] == task
    assert [a["id"] for a in packet["approvals"]] == [approval]


def _session(task_id: int | None, agent: str = "agent_a") -> str:
    from unified_agent.tools import registry as registry_tools

    return registry_tools.session_start("claude", agent=agent, task_id=task_id)["session_id"]


def test_another_tenants_session_reads_as_not_found(as_org):
    from unified_agent import registry

    as_org(ORG_A)
    task = task_tools.task_add("tenant a work")["id"]
    anchored = _session(task)
    loose = _session(None)
    for sid in (anchored, loose):
        registry.end_session(sid, summary="tenant a private summary")
    as_org(ORG_B)
    own = task_tools.task_add("tenant b work")["id"]
    for sid in (anchored, loose):
        packet = build_session_packet(session_id=sid)
        assert packet["session"] is None and packet["worktree"] is None
        assert packet["task"] is None and packet["lifecycle"] == []
        assert "tenant a" not in json.dumps(packet)
        mixed = build_session_packet(task_id=own, session_id=sid)
        assert mixed["session"] is None and mixed["task"]["id"] == own


def test_session_start_refuses_another_tenants_task_and_recalls_nothing(as_org):
    from mcp.server.mcpserver.exceptions import ToolError

    from unified_agent.tools import registry as registry_tools

    as_org(ORG_A)
    task = task_tools.task_add("tenant a work")["id"]
    _session(task)
    as_org(ORG_B)
    with pytest.raises(ToolError, match=f"no task with id {task}"):
        registry_tools.session_start(
            "claude", agent="agent_b", task_id=task, recall_prior_packet=True
        )
