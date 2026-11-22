"""Interactive task/plan graph pane (task #806 / G1, parent #805).

Ground-truth contract:

    /api/overview carries "task_graph" — {nodes, links, counts, errors} built
    by task_graph_payload over EXISTING rows only (tasks_payload active queue,
    delegate_list, dependency_list, plan_list). No new route, no new tables,
    read-only. Node ids: task:{id}, agent:{label}, plan:{id}. Link kinds:
    subtask, depends_on, claim, delegates, authored — every edge joins two
    nodes that both exist in the payload (the seen-node guard drops edges
    into the done/cancelled/unknown void).

The frontend lives in packages/web (the `task-graph` NavId, #918). The legacy
vanilla-shell asset tests and node-executed render harness were removed with
the /ui/legacy retirement (#993); what stays pinned here is the payload
contract the modern pane consumes plus the read-only guarantee.
"""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import server, storage, webui
from unified_agent.tools import graph as graph_tool
from unified_agent.tools import plan as plan_tool
from unified_agent.tools import tasks as tasks_tool
from unified_agent.tools.delegate import delegate
from unified_agent.webui import api as webui_api

TOKEN = f"tok-{secrets.token_urlsafe(8)}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def _run(coro):
    return asyncio.run(coro)


# ── payload invariants ───────────────────────────────────────────────


class TestTaskGraphPayload:
    def test_overview_carries_task_graph(self, client):
        res = client.get("/api/overview")
        assert res.status_code == 200
        body = res.json()
        assert "task_graph" in body
        graph = body["task_graph"]
        assert isinstance(graph["nodes"], list)
        assert isinstance(graph["links"], list)
        assert isinstance(graph["counts"], dict)

    def test_no_new_route_was_added(self, client):
        """The graph rides /api/overview; a new route would break the frozen
        inventory (tests/test_webui_route_inventory.py) on purpose."""
        res = client.get("/api/task-graph")
        assert res.status_code == 404

    def test_edges_join_nodes_that_both_exist(self, client):
        parent = tasks_tool.task_add("G1 parent", priority="high", project="unified-agent")
        child = tasks_tool.task_add("G1 child", parent_id=parent["id"], project="unified-agent")
        tasks_tool.task_update(child["id"], status="doing", worktree=False)
        graph_tool.dependency_add(child["id"], parent["id"], agent="Desktop_Agent1")

        graph = _run(webui_api.task_graph_payload())
        node_ids = {n["id"] for n in graph["nodes"]}
        assert f"task:{parent['id']}" in node_ids
        assert f"task:{child['id']}" in node_ids
        for link in graph["links"]:
            assert link["source"] in node_ids, link
            assert link["target"] in node_ids, link

    def test_node_kinds_and_link_kinds_are_exhaustive(self, client):
        parent = tasks_tool.task_add("G1 kinds parent")
        child = tasks_tool.task_add("G1 kinds child", parent_id=parent["id"])
        tasks_tool.task_update(child["id"], status="doing", worktree=False)
        _set_claim(child["id"], "Desktop_Agent1")
        graph_tool.dependency_add(child["id"], parent["id"], agent="Desktop_Agent1")
        delegate(
            task_spec="G1 kinds delegation",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
            callback="none",
        )
        plan_tool.plan_create(goal="G1 kinds plan", steps=["step one"], author="Desktop_Agent5")

        graph = _run(webui_api.task_graph_payload())
        kinds = {n["kind"] for n in graph["nodes"]}
        assert kinds <= {"task", "agent", "plan"}
        assert kinds == {"task", "agent", "plan"}
        link_kinds = {ln["kind"] for ln in graph["links"]}
        for expected in ("subtask", "depends_on", "claim", "delegates", "authored"):
            assert expected in link_kinds

    def test_claim_edge_binds_agent_to_task(self, client):
        task = tasks_tool.task_add("G1 claim target")
        tasks_tool.task_update(task["id"], status="doing", worktree=False)
        _set_claim(task["id"], "Desktop_agent3")

        graph = _run(webui_api.task_graph_payload())
        claims = [
            ln
            for ln in graph["links"]
            if ln["kind"] == "claim" and ln["target"] == f"task:{task['id']}"
        ]
        assert claims == [
            {"source": "agent:Desktop_agent3", "target": f"task:{task['id']}", "kind": "claim"}
        ]
        agent_node = next(n for n in graph["nodes"] if n["id"] == "agent:Desktop_agent3")
        assert agent_node["claims"] == 1

    def test_delegates_edge_carries_delegation_id_and_status(self, client):
        result = delegate(
            task_spec="G1 delegates probe",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
            callback="none",
        )
        graph = _run(webui_api.task_graph_payload())
        edges = [
            ln
            for ln in graph["links"]
            if ln["kind"] == "delegates" and ln.get("delegation_id") == result["delegation_id"]
        ]
        assert len(edges) == 1
        assert edges[0]["source"] == "agent:Desktop_Agent1"
        assert edges[0]["target"] == "agent:Desktop_agent3"
        assert edges[0]["status"] == "pending"

    def test_plan_node_carries_goal_status_and_author(self, client):
        plan = plan_tool.plan_create(goal="G1 plan node", steps=["a", "b"], author="Desktop_Agent1")

        graph = _run(webui_api.task_graph_payload())
        node = next(n for n in graph["nodes"] if n["id"] == f"plan:{plan['plan_id']}")
        assert node["kind"] == "plan"
        assert node["label"] == "G1 plan node"
        assert node["status"] == "active"
        assert node["needs_human"] is False

        authored = [
            ln
            for ln in graph["links"]
            if ln["kind"] == "authored" and ln["target"] == f"plan:{plan['plan_id']}"
        ]
        assert authored == [
            {
                "source": "agent:Desktop_Agent1",
                "target": f"plan:{plan['plan_id']}",
                "kind": "authored",
            }
        ]

    def test_escalated_plan_flags_needs_human(self, client):
        """A step that exhausts its retries escalates the plan — the graph node
        must surface that as needs_human so the canvas shows where work is stuck."""
        plan = plan_tool.plan_create(
            goal="G1 stuck plan", steps=["only step"], author="Desktop_agent3"
        )
        for _ in range(3):
            plan_tool.plan_step_retry(plan["plan_id"], 0, "still failing")

        graph = _run(webui_api.task_graph_payload())
        node = next(n for n in graph["nodes"] if n["id"] == f"plan:{plan['plan_id']}")
        assert node["needs_human"] is True

    def test_orphan_edges_are_dropped_not_carried(self, client):
        """A parent_id pointing outside the active window must not fabricate a
        node for it — done history is deliberately out of the graph (#685)."""
        done = tasks_tool.task_add("G1 done parent")
        tasks_tool.task_update(done["id"], status="done", actor="test-agent")
        child = tasks_tool.task_add("G1 child of done", parent_id=done["id"])

        graph = _run(webui_api.task_graph_payload())
        node_ids = {n["id"] for n in graph["nodes"]}
        assert f"task:{child['id']}" in node_ids
        assert f"task:{done['id']}" not in node_ids
        assert not any(
            ln["kind"] == "subtask" and ln["target"] == f"task:{child['id']}"
            for ln in graph["links"]
        )

    def test_duplicate_edges_are_deduped(self, client):
        """dependency_add rejects duplicate rows, but legacy data (or a repeated
        parent_id relation arriving by two paths) must not paint the same edge
        twice — the payload's own (source, target, kind) guard dedupes."""
        parent = tasks_tool.task_add("G1 dedupe parent")
        child = tasks_tool.task_add("G1 dedupe child", parent_id=parent["id"])

        dep_row = {"id": 1, "task_id": child["id"], "depends_on_task_id": parent["id"]}
        graph = _run(
            webui_api.task_graph_payload(
                tasks_res={
                    "tasks": [
                        {"id": parent["id"], "title": "p", "status": "open"},
                        {
                            "id": child["id"],
                            "title": "c",
                            "status": "open",
                            "parent_id": parent["id"],
                        },
                    ]
                },
                delegations_res={"delegations": []},
                dependencies_res={"dependencies": [dep_row, {**dep_row, "id": 2}]},
                plans_res={"plans": []},
            )
        )
        keys = [(ln["source"], ln["target"], ln["kind"]) for ln in graph["links"]]
        assert len(keys) == len(set(keys))
        # One subtask edge (parent_id) + exactly one depends_on edge despite
        # the duplicated dependency row.
        dep_edges = [k for k in keys if k[2] == "depends_on"]
        assert dep_edges == [(f"task:{parent['id']}", f"task:{child['id']}", "depends_on")]
        # Prerequisite -> dependent, carrying the id dependency remove takes (#2480).
        [dep_link] = [ln for ln in graph["links"] if ln["kind"] == "depends_on"]
        assert dep_link["dependency_id"] == 1

    def test_tool_failures_surface_in_errors_never_crash(self, client, monkeypatch):
        async def boom(**kwargs):
            raise RuntimeError("dependency window shut")

        monkeypatch.setattr(webui_api, "call_tool", boom)
        graph = _run(
            webui_api.task_graph_payload(
                tasks_res={"tasks": []},
                delegations_res={"delegations": []},
            )
        )
        assert graph["nodes"] == []
        assert any("dependency_list" in e for e in graph["errors"])
        # Scalar `error` rides along, same contract as board_payload /
        # executor_delegation_payload — the renderer's distinct error state
        # branches on it (review round 1, finding 1).
        assert graph["error"] == "; ".join(graph["errors"])

    def test_direct_delegation_payload_arg_keeps_explicit_contract(self, client):
        """Passing delegations_res explicitly bypasses executor_delegation_payload
        (which also runs health_payload) — the payload must accept either shape."""
        graph = _run(
            webui_api.task_graph_payload(
                tasks_res={"tasks": [{"id": 1, "title": "t", "status": "open"}]},
                delegations_res={"delegations": []},
                dependencies_res={"dependencies": []},
                plans_res={"plans": []},
            )
        )
        assert graph["counts"]["tasks"] == 1
        assert graph["errors"] == []

    def test_task_nodes_carry_audit_overlay_and_proof_state(self, client):
        # 1. Attributed task
        task_attr = tasks_tool.task_add(
            "G3 attributed task", priority="high", project="unified-agent"
        )
        tasks_tool.task_update(
            task_attr["id"],
            status="doing",
            worktree=False,
            evidence_ref="commit: abc12345",
        )
        _set_claim(task_attr["id"], "Desktop_Agent1")

        # 2. Unattributed task (unclaimed, missing host)
        task_unattr = tasks_tool.task_add(
            "G3 unattributed task", priority="low", project="unified-agent"
        )

        # 3. Plan node
        plan = plan_tool.plan_create(
            goal="G3 test plan", steps=["step one"], author="Desktop_Agent5"
        )

        graph = _run(webui_api.task_graph_payload())
        nodes_by_id = {n["id"]: n for n in graph["nodes"]}

        node_attr = nodes_by_id[f"task:{task_attr['id']}"]
        assert node_attr["claimed_by"] == "Desktop_Agent1"
        assert node_attr["agent"] == "Desktop_Agent1"
        assert node_attr["evidence_ref"] == "commit: abc12345"
        assert "audit" in node_attr
        assert node_attr["audit"]["has_evidence"] is True
        assert node_attr["audit"]["claimed_by"] == "Desktop_Agent1"
        assert "unknown" in node_attr["audit"]["chain"]

        node_unattr = nodes_by_id[f"task:{task_unattr['id']}"]
        assert node_unattr["unattributed"] is True
        assert node_unattr["audit"]["unattributed"] is True
        assert node_unattr["claimed_by"] is None
        assert node_unattr["audit"]["has_evidence"] is False

        plan_id = plan.get("plan_id") or plan.get("id")
        plan_node = nodes_by_id[f"plan:{plan_id}"]
        assert "audit" in plan_node
        assert plan_node["agent"] == "Desktop_Agent5"
        assert plan_node["claimed_by"] == "Desktop_Agent5"
        assert plan_node["unattributed"] is False


def _set_claim(task_id: int, owner: str) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET claimed_by=? WHERE id=?",
            (owner, task_id),
        )
        conn.commit()
    finally:
        conn.close()


class TestTaskGraphDocs:
    def test_docs_describe_the_pane(self):
        repo_root = Path(__file__).resolve().parents[1]
        text = (repo_root / "docs" / "web-ui.md").read_text(encoding="utf-8")
        assert "task_graph" in text
        assert "Task & Plan Graph" in text
