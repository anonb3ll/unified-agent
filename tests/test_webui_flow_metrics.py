"""Flow-metrics endpoint (task #729 / Lane A Increment A2).

Ground-truth contract (pinned — the frontend workstream builds against it):

    GET /api/flow-metrics -> {
      "cycle_time": {"p50_s": float|null, "p90_s": float|null},
      "lead_time":  {"p50_s": float|null, "p90_s": float|null},
      "wip_age": {"buckets": [{"label": str, "count": int}], "oldest_s": float|null},
      "window_days": 30, "as_of": iso}

cycle = claimed_at -> completed_at; lead = created_at -> completed_at; only
done tasks inside the rolling 30-day completion window are sampled; null
p50/p90 when no samples. WIP age buckets cover the active queue
(open/doing/waiting_review/blocked) by created_at age.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import server, storage, webui
from unified_agent.tools import tasks as tasks_tool
from unified_agent.webui import api as webui_api

TOKEN = f"tok-{secrets.token_urlsafe(8)}"
FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.delenv("UA_ENFORCE_AUTH", raising=False)
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def metrics_clock(monkeypatch):
    monkeypatch.setattr(webui_api, "_board_now", lambda: FIXED_NOW)


def _set_timestamps(task_id: int, **fields: str) -> None:
    conn = storage.connect()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))
        conn.commit()
    finally:
        conn.close()


def _add_done(
    title: str,
    *,
    created: datetime | None = None,
    claimed: datetime | None = None,
    completed: datetime | None = None,
) -> int:
    t = tasks_tool.task_add(title)
    tasks_tool.task_update(t["id"], status="done", actor="test-agent")
    fields: dict[str, str] = {}
    if created is not None:
        fields["created_at"] = created.isoformat()
    if claimed is not None:
        fields["claimed_at"] = claimed.isoformat()
    if completed is not None:
        fields["completed_at"] = completed.isoformat()
    if fields:
        _set_timestamps(t["id"], **fields)
    return t["id"]


# ── percentile math (pure helper) ────────────────────────────────────


class TestPercentile:
    def test_empty_samples_yield_none(self):
        assert webui_api._percentile([], 50) is None
        assert webui_api._percentile([], 90) is None

    def test_single_sample_is_both_p50_and_p90(self):
        assert webui_api._percentile([42.0], 50) == 42.0
        assert webui_api._percentile([42.0], 90) == 42.0

    def test_nearest_rank_p50_and_p90(self):
        # nearest-rank: rank = ceil(p/100 * n), 1-indexed into the sorted list
        assert webui_api._percentile([30.0, 10.0, 20.0], 50) == 20.0
        assert webui_api._percentile([30.0, 10.0, 20.0], 90) == 30.0
        assert webui_api._percentile([10.0, 20.0, 30.0, 40.0], 50) == 20.0
        assert webui_api._percentile([10.0, 20.0, 30.0, 40.0], 90) == 40.0

    def test_unsorted_input_is_sorted_not_trusted(self):
        assert webui_api._percentile([300.0, 100.0, 200.0], 50) == 200.0


# ── endpoint contract ────────────────────────────────────────────────


class TestFlowMetricsContract:
    def test_empty_db_yields_nulls_zero_buckets_and_pinned_shape(self, client, metrics_clock):
        res = client.get("/api/flow-metrics")
        assert res.status_code == 200
        body = res.json()
        assert body["cycle_time"] == {"p50_s": None, "p90_s": None}
        assert body["lead_time"] == {"p50_s": None, "p90_s": None}
        assert body["wip_age"]["oldest_s"] is None
        assert body["wip_age"]["buckets"] == [
            {"label": "0-1d", "count": 0},
            {"label": "1-3d", "count": 0},
            {"label": "3-7d", "count": 0},
            {"label": "7-14d", "count": 0},
            {"label": "14-30d", "count": 0},
            {"label": "30d+", "count": 0},
        ]
        assert body["window_days"] == 30
        assert body["as_of"] == FIXED_NOW.isoformat(timespec="seconds")

    def test_cycle_and_lead_percentiles_from_window_samples(self, client, metrics_clock):
        # two done tasks, same shape: created -> claimed -> completed
        _add_done(
            "Fast cycle",
            created=FIXED_NOW - timedelta(seconds=1000),
            claimed=FIXED_NOW - timedelta(seconds=900),
            completed=FIXED_NOW - timedelta(seconds=800),
        )  # cycle 100s, lead 200s
        _add_done(
            "Slow cycle",
            created=FIXED_NOW - timedelta(seconds=500),
            claimed=FIXED_NOW - timedelta(seconds=400),
            completed=FIXED_NOW - timedelta(seconds=100),
        )  # cycle 300s, lead 400s

        body = client.get("/api/flow-metrics").json()
        assert body["cycle_time"]["p50_s"] == 100.0
        assert body["cycle_time"]["p90_s"] == 300.0
        assert body["lead_time"]["p50_s"] == 200.0
        assert body["lead_time"]["p90_s"] == 400.0

    def test_done_outside_rolling_window_is_excluded(self, client, metrics_clock):
        _add_done(
            "Ancient completion",
            created=FIXED_NOW - timedelta(days=46),
            claimed=FIXED_NOW - timedelta(days=45, seconds=-100),
            completed=FIXED_NOW - timedelta(days=45),
        )

        body = client.get("/api/flow-metrics").json()
        assert body["cycle_time"]["p50_s"] is None
        assert body["lead_time"]["p90_s"] is None
        assert body["lead_time"]["p50_s"] is None

    def test_completed_at_at_window_edge_is_inclusive(self, client, metrics_clock):
        _add_done(
            "Edge completion",
            created=FIXED_NOW - timedelta(days=30, seconds=100),
            claimed=FIXED_NOW - timedelta(days=30, seconds=50),
            completed=FIXED_NOW - timedelta(days=30),
        )

        body = client.get("/api/flow-metrics").json()
        assert body["cycle_time"]["p50_s"] == 50.0
        assert body["lead_time"]["p50_s"] == 100.0

    def test_done_task_without_claimed_at_counts_lead_but_not_cycle(self, client, metrics_clock):
        _add_done(
            "Never claimed",
            created=FIXED_NOW - timedelta(seconds=500),
            completed=FIXED_NOW - timedelta(seconds=100),
        )

        body = client.get("/api/flow-metrics").json()
        assert body["cycle_time"]["p50_s"] is None
        assert body["lead_time"]["p50_s"] == 400.0

    def test_active_tasks_land_in_wip_age_buckets(self, client, metrics_clock):
        fresh = tasks_tool.task_add("Fresh open")
        _set_timestamps(fresh["id"], created_at=(FIXED_NOW - timedelta(hours=2)).isoformat())
        mid = tasks_tool.task_add("Mid doing")
        tasks_tool.task_update(mid["id"], status="doing", worktree=False)
        _set_timestamps(mid["id"], created_at=(FIXED_NOW - timedelta(days=5)).isoformat())
        old = tasks_tool.task_add("Ancient open")
        _set_timestamps(old["id"], created_at=(FIXED_NOW - timedelta(days=40)).isoformat())

        body = client.get("/api/flow-metrics").json()
        buckets = {b["label"]: b["count"] for b in body["wip_age"]["buckets"]}
        assert buckets["0-1d"] == 1
        assert buckets["3-7d"] == 1
        assert buckets["30d+"] == 1
        assert body["wip_age"]["oldest_s"] == pytest.approx(40 * 86400, abs=2)

    def test_done_tasks_do_not_count_as_wip(self, client, metrics_clock):
        _add_done(
            "Completed not wip",
            created=FIXED_NOW - timedelta(days=3),
            claimed=FIXED_NOW - timedelta(days=2),
            completed=FIXED_NOW - timedelta(hours=1),
        )

        body = client.get("/api/flow-metrics").json()
        assert all(b["count"] == 0 for b in body["wip_age"]["buckets"])
        assert body["wip_age"]["oldest_s"] is None


class TestFlowMetricsRoute:
    def test_rejects_write_methods(self, client):
        for method in ("post", "put", "delete", "patch"):
            assert getattr(client, method)("/api/flow-metrics").status_code == 405

    def test_requires_auth_under_enforce(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        storage.init_db()
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as c:
            assert c.get("/api/flow-metrics").status_code == 401
            assert (
                c.get("/api/flow-metrics", headers={"Authorization": f"Bearer {TOKEN}"}).status_code
                == 200
            )
