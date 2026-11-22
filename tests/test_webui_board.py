"""Read-only kanban board monitor (task #728 / Lane A Increment A1).

Ground-truth contracts:
- six status columns (backlog == open; no seventh)
- cancelled hidden by default; Done is a rolling 30d window
- nesting, WIP alerts (#339 lane caps), aging alerts
- SSE status-change → board payload refresh path
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import server, storage, webui
from unified_agent.tools import graph as graph_tool
from unified_agent.tools import tasks as tasks_tool
from unified_agent.tools.delegate import delegate
from unified_agent.webui import api as webui_api
from unified_agent.webui import sse

TOKEN = f"tok-{secrets.token_urlsafe(8)}"
FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

BOARD_STATUSES = (
    "open",
    "doing",
    "waiting_review",
    "blocked",
    "done",
    "cancelled",
)


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
def board_clock(monkeypatch):
    monkeypatch.setattr(webui_api, "_board_now", lambda: FIXED_NOW)


def _set_timestamps(task_id: int, **fields: str) -> None:
    conn = storage.connect()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE tasks SET {sets} WHERE id=?",
            (*fields.values(), task_id),
        )
        conn.commit()
    finally:
        conn.close()


def _card_ids(column: dict) -> set[int]:
    ids: set[int] = set()
    for card in column.get("cards") or []:
        if card.get("kind") == "delegation":
            continue
        ids.add(card["id"])
        for sub in card.get("subtasks") or []:
            ids.add(sub["id"])
    return ids


def _column(body: dict, status: str) -> dict:
    return next(c for c in body["columns"] if c["status"] == status)


class TestBoardZeroState:
    def test_empty_db_renders_six_status_columns(self, client, board_clock):
        res = client.get("/api/board")
        assert res.status_code == 200
        body = res.json()
        assert [c["status"] for c in body["columns"]] == list(BOARD_STATUSES)
        assert body["columns"][0]["label"] == "Backlog"
        for col in body["columns"]:
            assert col["cards"] == []
            assert col["count"] == 0
        assert body["columns"][-1]["hidden_by_default"] is True


class TestBoardSeeding:
    def test_seeded_rows_render_cards_nesting_and_wip_alerts(
        self, client, board_clock, monkeypatch
    ):
        monkeypatch.setenv("UA_LANE_WIP", "default:1:soft")
        monkeypatch.setenv("UA_AGENT_WIP_MAX", "2")

        parent = tasks_tool.task_add(
            "Parent board card", priority="high", project="unified-agent", domain="professional"
        )
        child = tasks_tool.task_add(
            "Child board card",
            priority="med",
            parent_id=parent["id"],
            project="unified-agent",
        )
        blocked = tasks_tool.task_add(
            "Blocked board card",
            notes="unblock-condition: waiting on review gate\n",
            project="unified-agent",
        )
        tasks_tool.task_update(blocked["id"], status="blocked")
        doing_a = tasks_tool.task_add("Doing A", project="unified-agent")
        doing_b = tasks_tool.task_add("Doing B", project="unified-agent")
        tasks_tool.task_update(doing_a["id"], status="doing", worktree=False)
        tasks_tool.task_update(doing_b["id"], status="doing", worktree=False)
        _set_timestamps(
            doing_a["id"],
            claimed_by="Desktop_Agent1",
            claimed_at=(FIXED_NOW - timedelta(hours=1)).isoformat(),
            lock_expires_at=(FIXED_NOW + timedelta(hours=1)).isoformat(),
            lane="default",
        )
        _set_timestamps(
            doing_b["id"],
            claimed_by="Desktop_Agent1",
            claimed_at=(FIXED_NOW - timedelta(hours=1)).isoformat(),
            lock_expires_at=(FIXED_NOW + timedelta(hours=1)).isoformat(),
            lane="default",
        )
        graph_tool.dependency_add(child["id"], blocked["id"], agent="Desktop_Agent1")

        body = client.get("/api/board").json()
        open_col = _column(body, "open")
        parent_card = next(c for c in open_col["cards"] if c["id"] == parent["id"])
        assert parent_card["title"] == "Parent board card"
        assert parent_card["priority"] == "high"
        assert parent_card["domain"] == "professional"  # CC2 filters the board by domain (#2480)
        assert {s["id"] for s in parent_card["subtasks"]} == {child["id"]}
        child_card = parent_card["subtasks"][0]
        assert child_card["dep_count"] >= 1

        blocked_col = _column(body, "blocked")
        blocked_card = next(c for c in blocked_col["cards"] if c["id"] == blocked["id"])
        assert "waiting on review gate" in (blocked_card.get("blocked_reason") or "")

        assert body["wip"]["lanes"]["default"]["count"] == 2
        assert body["wip"]["lanes"]["default"]["cap"] == 1
        assert body["wip"]["lanes"]["default"]["over"] is True
        assert any(a["kind"] == "wip_lane" for a in body["alerts"])

    def test_nested_descendants_render_recursively(self, client, board_clock):
        root = tasks_tool.task_add("Project root")
        child = tasks_tool.task_add("Project step", parent_id=root["id"])
        grandchild = tasks_tool.task_add("Project substep", parent_id=child["id"])

        body = client.get("/api/board").json()
        root_card = next(c for c in _column(body, "open")["cards"] if c["id"] == root["id"])
        child_card = next(c for c in root_card["subtasks"] if c["id"] == child["id"])

        assert [card["id"] for card in child_card["subtasks"]] == [grandchild["id"]]

    def test_project_spec_root_card_shows_descendant_rollup(self, client, board_clock):
        root = tasks_tool.task_add("Project board root", notes="[project-spec]")
        child = tasks_tool.task_add("Open step", parent_id=root["id"])
        blocked = tasks_tool.task_add("Blocked step", parent_id=root["id"])
        tasks_tool.task_update(blocked["id"], status="blocked")

        body = client.get("/api/board").json()
        root_card = next(c for c in _column(body, "open")["cards"] if c["id"] == root["id"])
        rollup = root_card["rollup"]
        assert rollup["complete"] is False
        assert rollup["derived_status"] == "blocked"
        active_ids = {row["id"] for row in rollup["active"]}
        assert active_ids == {child["id"], blocked["id"]}
        ordinary = tasks_tool.task_add("Ordinary board card")
        body = client.get("/api/board").json()
        ordinary_card = next(c for c in _column(body, "open")["cards"] if c["id"] == ordinary["id"])
        assert ordinary_card.get("rollup") is None
        assert ordinary_card.get("project_view") is None
        assert ordinary_card.get("steps") in (None, [])

    def test_project_spec_card_shows_mixed_status_steps_and_evidence_counts(
        self, client, board_clock
    ):
        root = tasks_tool.task_add("Project view root", notes="[project-spec]")
        open_step = tasks_tool.task_add("Open step", parent_id=root["id"])
        doing_step = tasks_tool.task_add("Doing step", parent_id=root["id"])
        nested = tasks_tool.task_add("Nested doing leaf", parent_id=doing_step["id"])
        merged = tasks_tool.task_add(
            "Merged step",
            parent_id=root["id"],
            evidence_ref="https://github.com/anonb3ll/unified-agent/pull/423",
            notes="[review: merged] https://github.com/anonb3ll/unified-agent/pull/423",
        )
        evidence_only = tasks_tool.task_add(
            "Evidence only",
            parent_id=root["id"],
            evidence_ref="https://github.com/anonb3ll/unified-agent/pull/999",
        )
        tasks_tool.task_update(doing_step["id"], status="doing", worktree=False)
        tasks_tool.task_update(nested["id"], status="doing", worktree=False)
        tasks_tool.task_update(merged["id"], status="done", actor="test-agent")
        tasks_tool.task_update(evidence_only["id"], status="done", actor="test-agent")

        body = client.get("/api/board").json()
        root_card = next(c for c in _column(body, "open")["cards"] if c["id"] == root["id"])
        view = root_card["project_view"]
        assert view["evidence_count"] == 2
        assert view["merge_verified_count"] == 0
        assert view["evidence_cli"] == f"unified-agent evidence report --project {root['id']}"

        by_id = {step["id"]: step for step in root_card["steps"]}
        assert set(by_id) == {
            open_step["id"],
            doing_step["id"],
            merged["id"],
            evidence_only["id"],
        }
        assert by_id[open_step["id"]]["status"] == "open"
        assert by_id[doing_step["id"]]["status"] == "doing"
        assert by_id[doing_step["id"]]["subtasks"][0]["id"] == nested["id"]
        assert by_id[doing_step["id"]]["subtasks"][0]["status"] == "doing"
        assert by_id[merged["id"]]["status"] == "done"
        assert by_id[evidence_only["id"]]["status"] == "done"

        # Kanban columns stay authoritative: mixed-status children are not
        # swallowed into the root's column.
        assert doing_step["id"] in _card_ids(_column(body, "doing"))
        assert doing_step["id"] not in _card_ids(_column(body, "open"))

    def test_cancelled_hidden_by_default_and_done_is_rolling_30d(self, client, board_clock):
        recent = tasks_tool.task_add("Recently done")
        old = tasks_tool.task_add("Ancient done")
        cancelled = tasks_tool.task_add("Cancelled card")
        tasks_tool.task_update(recent["id"], status="done", actor="test-agent")
        tasks_tool.task_update(old["id"], status="done", actor="test-agent")
        tasks_tool.task_update(cancelled["id"], status="cancelled")
        _set_timestamps(
            recent["id"],
            completed_at=(FIXED_NOW - timedelta(days=2)).isoformat(),
        )
        _set_timestamps(
            old["id"],
            completed_at=(FIXED_NOW - timedelta(days=45)).isoformat(),
        )

        hidden = client.get("/api/board").json()
        assert cancelled["id"] not in _card_ids(_column(hidden, "cancelled"))
        done_ids = _card_ids(_column(hidden, "done"))
        assert recent["id"] in done_ids
        assert old["id"] not in done_ids

        shown = client.get("/api/board?include_cancelled=1").json()
        assert cancelled["id"] in _card_ids(_column(shown, "cancelled"))


class TestBoardAgingAlerts:
    def test_past_lock_expiry_and_stale_doing_render_aging_alerts(self, client, board_clock):
        expired = tasks_tool.task_add("Lock expired doing")
        stale = tasks_tool.task_add("Stale doing")
        aged_open = tasks_tool.task_add("Aged open")
        tasks_tool.task_update(expired["id"], status="doing", worktree=False)
        tasks_tool.task_update(stale["id"], status="doing", worktree=False)
        _set_timestamps(
            expired["id"],
            claimed_by="Desktop_Agent1",
            claimed_at=(FIXED_NOW - timedelta(hours=3)).isoformat(),
            lock_expires_at=(FIXED_NOW - timedelta(hours=1)).isoformat(),
            updated_at=(FIXED_NOW - timedelta(hours=3)).isoformat(),
        )
        _set_timestamps(
            stale["id"],
            claimed_by="Desktop_Agent1",
            claimed_at=(FIXED_NOW - timedelta(hours=60)).isoformat(),
            lock_expires_at=(FIXED_NOW + timedelta(hours=1)).isoformat(),
            updated_at=(FIXED_NOW - timedelta(hours=60)).isoformat(),
        )
        _set_timestamps(
            aged_open["id"],
            created_at=(FIXED_NOW - timedelta(days=16)).isoformat(),
            updated_at=(FIXED_NOW - timedelta(days=16)).isoformat(),
        )

        body = client.get("/api/board").json()
        expired_card = next(c for c in _column(body, "doing")["cards"] if c["id"] == expired["id"])
        stale_card = next(c for c in _column(body, "doing")["cards"] if c["id"] == stale["id"])
        open_card = next(c for c in _column(body, "open")["cards"] if c["id"] == aged_open["id"])
        assert "lock_expired" in expired_card["alerts"]
        assert "aging_doing" in stale_card["alerts"]
        assert "aging_open" in open_card["alerts"]


class TestBoardFiltersAndDelegations:
    def test_filters_and_pending_delegation_deadline_countdown(
        self, client, board_clock, monkeypatch
    ):
        monkeypatch.setenv(
            "UA_AGENT_LABELS",
            "Desktop_Agent1:Desktop_agent3:Desktop_Agent4:Operator",
        )
        match = tasks_tool.task_add("Filter match", priority="high", project="browser")
        tasks_tool.task_add("Filter miss", priority="low", project="unified-agent")
        _set_timestamps(match["id"], lane="default", claimed_by="Desktop_Agent1")

        deadline = (FIXED_NOW + timedelta(hours=6)).isoformat()
        d = delegate(
            task_spec="Ship the board slice",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
            deadline=deadline,
            callback="none",
        )

        body = client.get(
            "/api/board?project=browser&priority=high&agent=Desktop_Agent1&lane=default"
        ).json()
        open_ids = _card_ids(_column(body, "open"))
        assert match["id"] in open_ids
        assert all(c.get("project") == "browser" for c in _column(body, "open")["cards"])

        full = client.get("/api/board").json()
        delegation_cards = [
            c
            for col in full["columns"]
            for c in col["cards"]
            if c.get("kind") == "delegation" and c.get("delegation_id") == d["delegation_id"]
        ]
        assert len(delegation_cards) == 1
        card = delegation_cards[0]
        assert card["deadline"] == deadline
        assert card["deadline_remaining_s"] == pytest.approx(6 * 3600, abs=2)
        assert card["to_agent"] == "Desktop_agent3"


class TestBoardSseLiveUpdate:
    def test_status_change_updates_board_and_is_observed_on_sse(
        self, client, board_clock, monkeypatch
    ):
        monkeypatch.setenv("UA_UI_STREAM_POLL_S", "0.02")
        task = tasks_tool.task_add("SSE board card")
        before = client.get("/api/board").json()
        assert task["id"] in _card_ids(_column(before, "open"))
        assert task["id"] not in _card_ids(_column(before, "doing"))

        async def scenario():
            gen = sse.event_stream(channels=["agent.task.*"])
            try:
                cursor = await gen.__anext__()
                assert cursor["event"] == "cursor"
                await anyio.to_thread.run_sync(
                    lambda: tasks_tool.task_update(task["id"], status="doing", worktree=False)
                )
                with anyio.fail_after(5.0):
                    frame = await gen.__anext__()
                assert frame["event"] == "message"
                payload = __import__("json").loads(frame["data"])
                assert payload["channel"].startswith("agent.task.")
            finally:
                await gen.aclose()

        anyio.run(scenario)

        after = client.get("/api/board").json()
        assert task["id"] not in _card_ids(_column(after, "open"))
        assert task["id"] in _card_ids(_column(after, "doing"))


class TestBoardReadOnly:
    def test_board_rejects_write_methods(self, client):
        for method in ("post", "put", "delete", "patch"):
            assert getattr(client, method)("/api/board").status_code == 405

    def test_board_requires_auth_under_enforce(self, monkeypatch):
        monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{TOKEN}")
        monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
        monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
        storage.init_db()
        app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
        with TestClient(app) as c:
            assert c.get("/api/board").status_code == 401
            assert (
                c.get("/api/board", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
            )


class TestBoardReviewFollowups:
    """Regression tests for PR #342 review follow-ups (2026-08-31)."""

    def test_cross_status_subtask_stays_in_own_column(self, client, board_clock):
        parent = tasks_tool.task_add("Cross-status parent")
        tasks_tool.task_update(parent["id"], status="doing", worktree=False)
        child = tasks_tool.task_add("Cross-status child", parent_id=parent["id"])
        # child stays open while the parent is doing

        body = client.get("/api/board").json()
        assert child["id"] in _card_ids(_column(body, "open"))
        doing_ids = _card_ids(_column(body, "doing"))
        assert parent["id"] in doing_ids
        assert child["id"] not in doing_ids

    def test_subtasks_filtered_even_when_parent_matches(self, client, board_clock):
        parent = tasks_tool.task_add("High-priority parent", priority="high")
        tasks_tool.task_add("Low-priority child", priority="low", parent_id=parent["id"])

        body = client.get("/api/board?priority=high").json()
        open_col = _column(body, "open")
        parent_card = next(c for c in open_col["cards"] if c["id"] == parent["id"])
        assert all(s["priority"] == "high" for s in parent_card["subtasks"])

    def test_claimed_by_matches_the_claim_lock_alone(self, client, board_clock, monkeypatch):
        """Task #2523: ``agent`` also matches either end of a delegation, so the Agents
        area filtered the board by ``claimed_by`` in the browser. ``claimed_by`` keeps
        only tasks whose claim lock is that label, and no delegation card."""
        monkeypatch.setenv("UA_AGENT_LABELS", "Desktop_Agent1:Desktop_agent3")
        mine = tasks_tool.task_add("Claimed by me")
        theirs = tasks_tool.task_add("Claimed by them")
        _set_timestamps(mine["id"], claimed_by="Desktop_Agent1")
        _set_timestamps(theirs["id"], claimed_by="Desktop_agent3")
        delegate(
            task_spec="Sent by me",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
            callback="none",
        )

        def cards(query: str) -> list[dict]:
            body = client.get(f"/api/board?{query}").json()
            return [c for col in body["columns"] for c in col["cards"]]

        claimed = cards("claimed_by=Desktop_Agent1")
        assert [c["id"] for c in claimed] == [mine["id"]]
        assert any(c.get("kind") == "delegation" for c in cards("agent=Desktop_Agent1"))
        body = client.get("/api/board?claimed_by=Desktop_Agent1").json()
        assert body["filters"]["claimed_by"] == "Desktop_Agent1"

    def test_delegation_inherits_linked_task_scope(self, client, board_clock, monkeypatch):
        monkeypatch.setenv(
            "UA_AGENT_LABELS",
            "Desktop_Agent1:Desktop_agent3:Desktop_Agent4:Operator",
        )
        task = tasks_tool.task_add("Scoped delegation target", project="browser")
        d = delegate(
            task_spec="Work the scoped item",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
            parent_task_id=task["id"],
            callback="none",
        )

        body = client.get("/api/board?project=browser").json()
        delegation_cards = [
            c
            for col in body["columns"]
            for c in col["cards"]
            if c.get("kind") == "delegation" and c.get("delegation_id") == d["delegation_id"]
        ]
        assert len(delegation_cards) == 1
        assert delegation_cards[0]["project"] == "browser"

    def test_nonfinite_min_age_days_is_rejected(self, client):
        for bad in ("nan", "inf", "-inf", "infinity"):
            res = client.get(f"/api/board?min_age_days={bad}")
            assert res.status_code == 200
            body = res.json()
            assert body["filters"]["min_age_days"] is None
            # A board snapshot still comes back — the request degrades, not breaks.
            assert isinstance(body["columns"], list)

    def test_board_payload_uses_compact_fields_with_full_blocked(self, client, board_clock):
        blocked = tasks_tool.task_add(
            "Compact-projection blocked card",
            notes="unblock-condition: needs unblock text\n",
        )
        tasks_tool.task_update(blocked["id"], status="blocked")

        body = client.get("/api/board").json()
        blocked_card = next(
            c for c in _column(body, "blocked")["cards"] if c["id"] == blocked["id"]
        )
        # Full reload of blocked tasks keeps blocker text working under compact.
        assert "needs unblock text" in (blocked_card.get("blocked_reason") or "")

    def test_dep_counts_cover_all_edges_up_to_tool_cap(self, client, board_clock):
        dependent = tasks_tool.task_add("Many-edge dependent")
        blockers = [tasks_tool.task_add(f"Blocker {i}") for i in range(3)]
        for blocker in blockers:
            graph_tool.dependency_add(dependent["id"], blocker["id"], agent="Desktop_Agent1")

        body = client.get("/api/board").json()
        card = next(c for c in _column(body, "open")["cards"] if c["id"] == dependent["id"])
        assert card["dep_count"] == len(blockers)


class TestBoardEvidenceRefs:
    def test_cards_copy_evidence_ref_including_nested(self, client, board_clock):
        parent = tasks_tool.task_add(
            "Parent with evidence",
            evidence_ref="https://github.com/anonb3ll/unified-agent/pull/402",
        )
        child = tasks_tool.task_add(
            "Child with evidence",
            parent_id=parent["id"],
            evidence_ref="commit abc1234",
        )

        body = client.get("/api/board").json()
        parent_card = next(c for c in _column(body, "open")["cards"] if c["id"] == parent["id"])
        assert parent_card["evidence_ref"] == "https://github.com/anonb3ll/unified-agent/pull/402"
        child_card = next(s for s in parent_card["subtasks"] if s["id"] == child["id"])
        assert child_card["evidence_ref"] == "commit abc1234"
