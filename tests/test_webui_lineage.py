"""Per-task lineage endpoint (task #730 / Lane A Increment A3).

Ground-truth contract (pinned — the frontend workstream builds against it):

    GET /api/execution-graph?task=N -> {
      "task": {"id": int, "title": str|null},
      "lineage": {
        "delegations": [{"id", "status", "from_agent"|"null", "to_agent"|"null",
                         "task_id"|"null"}],
        "dispatch_runs": [{"id", "task_id"|"null", "status"|"null",
                           "updated_at"|"null", "agent_label"|"null"}],
        "autonomous_cycles": [{"id", "task_id"|"null", "status"|"null",
                               "updated_at"|"null"}],
        "refs": {"pr_url"|"null", "worktree_path"|"null", "branch"|"null"}
      },
      "empty": bool}

Null-tolerant single JOIN only (delegations status != cancelled; left-join
dispatch_runs + autonomous_cycles on task_id). Legacy rows with no lane/run/PR
render as empty nodes — NEVER 500; missing task id -> 200 with {"empty": true}.
No ?task= param at all keeps the legacy full-graph payload (the Graph pane
still polls it), so the enhancement is purely additive.

No new tables; JOIN only per #730 boundaries. `webui/static/**` is owned by
the drawer workstream and deliberately untouched here.
"""

from __future__ import annotations

import secrets

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import server, storage, webui
from unified_agent.tools import graph as graph_tool
from unified_agent.tools.delegate import delegate

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


def _add_task(title: str) -> int:
    from unified_agent.tools import tasks as tasks_tool

    return tasks_tool.task_add(title)["id"]


def _insert_delegation(task_id: int, status: str = "pending") -> int:
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO delegations (from_agent, to_agent, task_id, task_spec, "
            "callback, status, created_at, updated_at) "
            "VALUES ('Desktop_Agent1', 'Desktop_agent3', ?, 'spec', 'none', ?, ?, ?)",
            (task_id, status, storage.now(), storage.now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _insert_dispatch_run(task_id: int, run_id: str, state: str = "running") -> str:
    """Insert a dispatch run and return its ``updated_at`` stamp."""
    stamp = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
            "created_at, updated_at) VALUES (?, ?, 'Desktop_agent3', ?, 'test', ?, ?)",
            (run_id, task_id, state, stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()
    return stamp


def _insert_cycle(task_id: int, cycle_id: str, state: str = "pr", pr_url: str | None = None) -> str:
    """Insert an autonomous cycle and return its ``updated_at`` stamp."""
    stamp = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO autonomous_cycles (id, task_id, state, created_by, created_at, "
            "updated_at, pr_url) VALUES (?, ?, ?, 'test', ?, ?, ?)",
            (cycle_id, task_id, state, stamp, stamp, pr_url),
        )
        conn.commit()
    finally:
        conn.close()
    return stamp


def _set_task_fields(task_id: int, **fields: str) -> None:
    conn = storage.connect()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))
        conn.commit()
    finally:
        conn.close()


# ── endpoint contract ────────────────────────────────────────────────


class TestLineageEndpoint:
    def test_missing_task_param_keeps_full_graph_payload(self, client):
        res = client.get("/api/execution-graph")
        assert res.status_code == 200
        body = res.json()
        assert "nodes" in body and "links" in body

    def test_blank_task_param_yields_empty_true_200(self, client):
        res = client.get("/api/execution-graph?task=")
        assert res.status_code == 200
        assert res.json() == {"empty": True}

    def test_non_integer_task_param_yields_empty_true_200(self, client):
        res = client.get("/api/execution-graph?task=abc")
        assert res.status_code == 200
        assert res.json() == {"empty": True}

    def test_unknown_task_id_returns_empty_payload_never_500(self, client):
        res = client.get("/api/execution-graph?task=424242")
        assert res.status_code == 200
        body = res.json()
        assert body["task"] == {"id": 424242, "title": None}
        assert body["lineage"]["delegations"] == []
        assert body["lineage"]["dispatch_runs"] == []
        assert body["lineage"]["autonomous_cycles"] == []
        assert body["lineage"]["refs"] == {
            "pr_url": None,
            "worktree_path": None,
            "branch": None,
        }
        assert body["empty"] is True

    def test_lineage_join_correctness_on_real_delegation_history(self, client):
        result = delegate(
            "lineage probe spec",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
            callback="none",
        )
        task_id = result["task_id"]
        delegation_id = result["delegation_id"]
        run_stamp = _insert_dispatch_run(task_id, "run-lineage-1")
        cycle_stamp = _insert_cycle(task_id, "cycle-lineage-1", state="executing")

        res = client.get(f"/api/execution-graph?task={task_id}")
        assert res.status_code == 200
        body = res.json()
        assert body["task"] == {"id": task_id, "title": "lineage probe spec"}
        assert body["empty"] is False
        assert body["lineage"]["delegations"] == [
            {
                "id": delegation_id,
                "status": "pending",
                "from_agent": "Desktop_Agent1",
                "to_agent": "Desktop_agent3",
                "task_id": task_id,
            }
        ]
        assert body["lineage"]["dispatch_runs"] == [
            {
                "id": "run-lineage-1",
                "task_id": task_id,
                "status": "running",
                "updated_at": run_stamp,
                "agent_label": "Desktop_agent3",
            }
        ]
        assert body["lineage"]["autonomous_cycles"] == [
            {
                "id": "cycle-lineage-1",
                "task_id": task_id,
                "status": "executing",
                "updated_at": cycle_stamp,
            }
        ]

    def test_cancelled_delegations_are_filtered(self, client):
        task_id = _add_task("Cancelled-filter task")
        _insert_delegation(task_id, status="in_flight")
        _insert_delegation(task_id, status="cancelled")

        body = client.get(f"/api/execution-graph?task={task_id}").json()
        statuses = [d["status"] for d in body["lineage"]["delegations"]]
        assert statuses == ["in_flight"]
        assert body["empty"] is False

    def test_legacy_rows_render_as_empty_nodes(self, client):
        task_id = _add_task("Legacy task with no lane run or PR")

        body = client.get(f"/api/execution-graph?task={task_id}").json()
        assert body["lineage"]["refs"]["pr_url"] is None
        assert body["lineage"]["refs"]["worktree_path"] is None
        assert body["lineage"]["refs"]["branch"] is None
        assert body["lineage"]["delegations"] == []
        assert body["empty"] is True

    def test_refs_carry_worktree_branch_and_pr_url(self, client):
        task_id = _add_task("Refs task")
        _set_task_fields(
            task_id,
            worktree_path="/home/you/dev/unified_agent/.worktrees/t_x",
            branch="wt/t_x",
        )
        _insert_cycle(task_id, "cycle-pr-1", state="pr", pr_url="https://github.com/ex/re/pull/1")

        body = client.get(f"/api/execution-graph?task={task_id}").json()
        assert body["lineage"]["refs"] == {
            "pr_url": "https://github.com/ex/re/pull/1",
            "worktree_path": "/home/you/dev/unified_agent/.worktrees/t_x",
            "branch": "wt/t_x",
        }

    def test_pr_url_falls_back_to_url_shaped_evidence_ref(self, client):
        task_id = _add_task("Evidence-ref task")
        _set_task_fields(task_id, evidence_ref="https://github.com/ex/re/pull/9")

        body = client.get(f"/api/execution-graph?task={task_id}").json()
        assert body["lineage"]["refs"]["pr_url"] == "https://github.com/ex/re/pull/9"

    def test_non_url_evidence_ref_is_not_reported_as_pr(self, client):
        task_id = _add_task("Branch evidence task")
        _set_task_fields(task_id, evidence_ref="pushed branch wt/t_y")

        body = client.get(f"/api/execution-graph?task={task_id}").json()
        assert body["lineage"]["refs"]["pr_url"] is None

    def test_rejects_write_methods(self, client):
        for method in ("post", "put", "delete", "patch"):
            assert getattr(client, method)("/api/execution-graph?task=1").status_code == 405


# ── graph tool parity ────────────────────────────────────────────────


class TestGraphLineageTool:
    def test_lineage_unknown_task_is_empty_not_error(self):
        result = graph_tool.lineage(987654)
        assert result["empty"] is True
        assert result["task"] == {"id": 987654, "title": None}

    def test_lineage_via_coordination_manage_dispatch(self):
        task_id = _add_task("Coordination dispatch task")
        _insert_delegation(task_id, status="pending")

        result = graph_tool.coordination_manage("lineage", {"task_id": task_id})
        assert result["task"]["id"] == task_id
        assert [d["id"] for d in result["lineage"]["delegations"]] != []
        assert result["empty"] is False

    def test_coordination_manage_rejects_bad_lineage_params(self):
        with pytest.raises(Exception, match="invalid params"):
            graph_tool.coordination_manage("lineage", {"task_id": 1, "unexpected": True})
