"""Per-agent cost rollups and budgets with a warning threshold and hard stop (task #1348).

Cost is read from the self-pickup run record (#1436) and the broker spend
ledger (J7 #1326); budgets live in policy.yaml. Ground truth is real claims and
finishes through task_claim/task_update against the fixture DB, not stubs.
"""

from __future__ import annotations

import secrets
from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import budgets, config, llm_spend, monitor, server, storage
from unified_agent.tools.tasks import task_add, task_claim, task_update
from unified_agent.webui.routes import web_routes


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture
def policy(tmp_path, monkeypatch):
    path = tmp_path / "policy.yaml"
    monkeypatch.setenv("UA_POLICY_FILE", str(path))
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "50")

    def write(body: str) -> None:
        path.write_text(body, encoding="utf-8")

    return write


def _budget(
    agents="[Desktop_Agent2]", limit=1.0, warn=80, reserve=0, action="block", period="month"
):
    return (
        "budgets:\n"
        "  - name: codex\n"
        f"    agents: {agents}\n"
        f"    period: {period}\n"
        f"    limit_usd: {limit}\n"
        f"    warn_pct: {warn}\n"
        f"    run_reserve_usd: {reserve}\n"
        f"    action: {action}\n"
    )


def _new_task(title: str = "t") -> int:
    return task_add(title, lane="dispatch-ready")["id"]


def _run(agent: str, usage: dict | None) -> int:
    """One finished self-pickup run by ``agent`` reporting ``usage``."""
    tid = _new_task()
    with _as(agent):
        task_claim(tid, agent, expected_lane="dispatch-ready")
        task_update(tid, status="done", evidence_ref="https://example/pr/1", run_usage=usage)
    return tid


def _budget_events(kind: str) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? AND json_extract(payload, '$.kind')=?",
            (budgets.CHANNEL, kind),
        ).fetchall()
    finally:
        conn.close()
    return [budgets.run_records._payload(r) for r in rows]


def _claim(agent: str, tid: int | None = None) -> dict:
    tid = tid if tid is not None else _new_task()
    with _as(agent):
        return task_claim(tid, agent, expected_lane="dispatch-ready")


# --- enforcement -------------------------------------------------------------


def test_no_budgets_leaves_claims_untouched(policy):
    _run("Desktop_Agent2", {"cost_usd": 50.0})
    result = _claim("Desktop_Agent2")
    assert result["status"] == "doing"
    assert "budget_warnings" not in result


def test_hard_stop_refuses_new_claims_once_spend_reaches_the_limit(policy):
    policy(_budget(limit=1.0))
    _run("Desktop_Agent2", {"cost_usd": 0.6, "provider": "openai", "billing": "api"})
    _run("Desktop_Agent2", {"cost_usd": 0.4})
    tid = _new_task()
    with pytest.raises(ToolError, match="budget hard stop"):
        _claim("Desktop_Agent2", tid)
    with pytest.raises(ToolError, match="budget hard stop"):
        _claim("Desktop_Agent2", tid)
    conn = storage.connect()
    try:
        row = conn.execute("SELECT status, claimed_by FROM tasks WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()
    assert (row["status"], row["claimed_by"]) == ("open", None)
    # One blocked event per policy/agent/period, however often the claim retries.
    [blocked] = _budget_events(budgets.KIND_BLOCKED)
    assert blocked["agent"] == "Desktop_Agent2" and blocked["committed_usd"] == pytest.approx(1.0)
    # An agent outside the budget is unaffected.
    assert _claim("server_agent3", tid)["claimed_by"] == "server_agent3"


def test_warning_threshold_returns_budget_warnings(policy):
    policy(_budget(limit=1.0, warn=50))
    _run("Desktop_Agent2", {"cost_usd": 0.6})
    result = _claim("Desktop_Agent2")
    assert result["status"] == "doing"
    assert "budget_warnings" in result and "codex" in result["budget_warnings"][0]
    assert len(_budget_events(budgets.KIND_WARNING)) == 1


def test_warn_action_never_blocks(policy):
    policy(_budget(limit=1.0, action="warn"))
    _run("Desktop_Agent2", {"cost_usd": 3.0})
    result = _claim("Desktop_Agent2")
    assert result["status"] == "doing"
    assert "limit reached" in result["budget_warnings"][0]


def test_reserves_make_successive_claims_count_against_the_remaining_budget(policy):
    # Each held run reserves $4 of $10: the first two claims fit, the third
    # would take committed spend to $12 and is refused.
    policy(_budget(limit=10, reserve=4))
    _claim("Desktop_Agent2")
    _claim("Desktop_Agent2")
    with pytest.raises(ToolError, match="reserves \\$4.00"):
        _claim("Desktop_Agent2")


def test_unknown_cost_is_reserved_and_subscription_is_not_priced(policy):
    policy(_budget(limit=10, reserve=3))
    _run("Desktop_Agent2", None)  # unknown cost -> reserve
    _run("Desktop_Agent2", {"billing": "subscription", "input_tokens": 9000})
    _run("Desktop_Agent2", {"billing": "local"})
    [row] = budgets.budget_status("Desktop_Agent2")["budgets"][0]["agents_status"]
    assert row["unknown_cost_runs"] == 1
    assert row["subscription_runs"] == 1 and row["local_runs"] == 1
    assert row["committed_usd"] == pytest.approx(3.0)
    assert row["subscription_quota"]["availability"] in {"unknown", "available", "unavailable"}


def test_renewal_is_never_refused(policy):
    tid = _new_task()
    initial = _claim("Desktop_Agent2", tid)
    policy(_budget(limit=1.0))
    _run("Desktop_Agent2", {"cost_usd": 5.0})
    renewed = _claim("Desktop_Agent2", tid)
    assert renewed["status"] == "doing"
    assert renewed["current_run_id"] == initial["current_run_id"]


def test_entering_doing_through_task_update_is_also_stopped(policy):
    policy(_budget(limit=1.0))
    _run("Desktop_Agent2", {"cost_usd": 2.0})
    tid = _new_task()
    with _as("Desktop_Agent2"), pytest.raises(ToolError, match="budget hard stop"):
        task_update(tid, status="doing", worktree=False)


@pytest.mark.parametrize("prior_claim", ["unclaimed", "expired", "terminal"])
def test_unverified_actor_budget_does_not_gate_an_unattributed_run(policy, prior_claim):
    policy(_budget(limit=1.0))
    _run("Desktop_Agent2", {"cost_usd": 2.0})
    tid = _new_task()
    if prior_claim != "unclaimed":
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE tasks SET status=?, claimed_by='Desktop_Agent2', claimed_at=?, "
                "lock_expires_at=? WHERE id=?",
                (
                    "done" if prior_claim == "terminal" else "blocked",
                    "2000-01-01T00:00:00+00:00",
                    "2000-01-01T00:00:01+00:00",
                    tid,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    # The asserted actor remains opened_by evidence, but cannot charge or
    # reject admission under that label when the run has no verified agent.
    result = task_update(tid, status="doing", actor="Desktop_Agent2", worktree=False)
    assert result["claimed_by"] is None
    assert result["current_run_agent"] is None
    assert _budget_events(budgets.KIND_BLOCKED) == []
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT payload FROM event_log WHERE channel=? AND json_extract(payload, '$.run_id')=?",
            (budgets.run_records.CHANNEL, result["current_run_id"]),
        ).fetchone()
    finally:
        conn.close()
    recorded = budgets.run_records._payload(row)
    assert recorded["agent"] is None and recorded["opened_by"] == "Desktop_Agent2"


def test_budget_admission_prefers_verified_identity_over_supplied_actor(policy):
    policy(_budget(limit=1.0))
    _run("Desktop_Agent2", {"cost_usd": 2.0})
    with _as("server_agent3"):
        result = task_update(_new_task(), status="doing", actor="Desktop_Agent2", worktree=False)
    assert result["current_run_agent"] == "server_agent3"
    with _as("Desktop_Agent2"), pytest.raises(ToolError, match="budget hard stop"):
        task_update(_new_task(), status="doing", actor="server_agent3", worktree=False)


def test_unverified_budget_admission_still_uses_a_surviving_claimant(policy):
    policy(_budget(limit=1.0))
    _run("Desktop_Agent2", {"cost_usd": 2.0})
    tid = _new_task()
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status='blocked', claimed_by='Desktop_Agent2', "
            "claimed_at=?, lock_expires_at=? WHERE id=?",
            (storage.now(), "2999-01-01T00:00:00+00:00", tid),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ToolError, match="budget hard stop"):
        task_update(tid, status="doing", actor="server_agent3", worktree=False)
    assert _budget_events(budgets.KIND_BLOCKED)[0]["agent"] == "Desktop_Agent2"


def test_broker_spend_counts_toward_the_budget(policy):
    policy(_budget(limit=1.0))
    conn = storage.connect()
    try:
        llm_spend._table(conn)
        conn.execute(
            "INSERT INTO llm_broker_spend (request_id, org_id, agent, device_node, day_utc, "
            "reserve_usd, actual_usd, outcome, updated_at) VALUES (?, '', ?, 'n', ?, 0.5, NULL, NULL, ?)",
            ("r1", "Desktop_Agent2", storage.now()[:10], storage.now()),
        )
        conn.execute(
            "INSERT INTO llm_broker_spend (request_id, org_id, agent, device_node, day_utc, "
            "reserve_usd, actual_usd, outcome, updated_at) VALUES (?, '', ?, 'n', ?, 0.5, 0.5, 'completed', ?)",
            ("r2", "Desktop_Agent2", storage.now()[:10], storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ToolError, match="budget hard stop"):
        _claim("Desktop_Agent2")
    rollup = budgets.cost_rollup()
    [broker] = rollup["broker"]
    assert broker == {
        "agent": "Desktop_Agent2",
        "calls": 2,
        "actual_usd": 0.5,
        "unreconciled_reserve_usd": 0.5,
    }


@pytest.mark.parametrize(
    "body",
    [
        "budgets: {}\n",
        "budgets:\n  - name: x\n    agents: [' ']\n    limit_usd: 1\n",
        "budgets:\n  - name: x\n    agents: []\n    limit_usd: 1\n",
        "budgets:\n  - name: x\n    period: week\n    limit_usd: 1\n",
        "budgets:\n  - name: x\n",
        "budgets:\n  - name: x\n    limit_usd: 1\n    warn_pct: 150\n",
        "budgets:\n  - name: x\n    limit_usd: 1\n    run_reserve_usd: 2\n",
        "budgets:\n  - name: x\n    limit_usd: 1\n  - name: x\n    limit_usd: 2\n",
    ],
)
def test_malformed_budgets_fail_closed(policy, body):
    policy(body)
    with pytest.raises(ToolError, match="fails closed"):
        _claim("Desktop_Agent2")
    status = budgets.budget_status()
    assert status["error"] and status["budgets"] == []


def test_invalid_policy_yaml_denies_before_budget_admission(policy):
    tid = _new_task()
    policy("budgets: [\n")
    with pytest.raises(ToolError, match="Failed to load or parse policy YAML"):
        _claim("Desktop_Agent2", tid)
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT status, claimed_by, current_run_id FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        assert tuple(row) == ("open", None, None)
    finally:
        conn.close()
    status = budgets.budget_status()
    assert status["error"] and status["budgets"] == []


# --- readers -----------------------------------------------------------------


def test_cost_rollup_groups_and_keeps_classes_separate(policy):
    a = _run(
        "Desktop_Agent2",
        {
            "provider": "openai",
            "model": "gpt",
            "cost_usd": 0.25,
            "input_tokens": 100,
            "output_tokens": 10,
            "billing": "api",
            "phases": {"build": 12, "test": 3.5},
            "contended": False,
        },
    )
    _run("Desktop_Agent2", {"billing": "subscription", "input_tokens": 50, "contended": True})
    _run("server_agent3", None)
    by_agent = {g["agent"]: g for g in budgets.cost_rollup()["groups"]}
    codex = by_agent["Desktop_Agent2"]
    assert codex["runs"] == 2 and codex["api_cost_usd"] == pytest.approx(0.25)
    assert codex["priced_runs"] == 1 and codex["subscription_runs"] == 1
    assert codex["input_tokens"] == 100 and codex["subscription_input_tokens"] == 50
    assert codex["phases_s"] == {"build": 12.0, "test": 3.5}
    assert codex["contended_runs"] == 1
    assert by_agent["server_agent3"]["unknown_cost_runs"] == 1
    assert by_agent["server_agent3"]["api_cost_usd"] == 0.0

    by_billing = {g["billing"] for g in budgets.cost_rollup("billing")["groups"]}
    assert by_billing == {"api", "subscription", "(unknown)"}
    by_task = {g["task"] for g in budgets.cost_rollup("task")["groups"]}
    assert str(a) in by_task
    projects = budgets.cost_rollup("project")["groups"]
    assert sum(g["runs"] for g in projects) == 3
    totals = budgets.cost_rollup()["totals"]
    assert totals["runs"] == 3 and totals["unknown_cost_runs"] == 1


def test_cost_rollup_validates_inputs(policy):
    with pytest.raises(ToolError, match="group_by"):
        budgets.cost_rollup("team")
    with pytest.raises(ToolError, match="since"):
        budgets.cost_rollup(since="yesterday")
    assert budgets.cost_rollup(since="2999-01-01T00:00:00Z")["groups"] == []


def test_observability_manage_exposes_cost_actions(policy):
    policy(_budget(limit=5))
    _run("Desktop_Agent2", {"cost_usd": 1.0})
    rollup = monitor.observability_manage("cost_rollup", {"group_by": "agent"})
    assert rollup["totals"]["api_cost_usd"] == pytest.approx(1.0)
    status = monitor.observability_manage("budget_status", {})
    [row] = status["budgets"][0]["agents_status"]
    assert row["state"] == "ok" and row["remaining_usd"] == pytest.approx(4.0)


def test_costs_route_serves_rollup_and_budgets(policy, tmp_path, monkeypatch):
    token = f"tok-costs-{secrets.token_urlsafe(8)}"
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{token}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    policy(_budget(limit=5))
    _run("Desktop_Agent2", {"cost_usd": 1.0})
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as client:
        res = client.get("/api/costs", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200
        body = res.json()
        assert body["rollup"]["totals"]["api_cost_usd"] == pytest.approx(1.0)
        assert body["budgets"]["budgets"][0]["name"] == "codex"
        bad = client.get("/api/costs?since=nope", headers={"Authorization": f"Bearer {token}"})
        assert bad.json()["code"] == "costs_unavailable"
        assert client.get("/api/costs").status_code in {401, 403}


# --- Devin review regressions (PR #818) --------------------------------------


def test_unclaimed_doing_runs_hold_their_reserve(policy):
    # task_update(status="doing") without task_claim records no claimed_by but
    # opens a run under the actor; that run must still hold a reserve.
    policy(_budget(limit=10, reserve=6))
    first, second = _new_task(), _new_task()
    with _as("Desktop_Agent2"):
        task_update(first, status="doing", worktree=False)
        with pytest.raises(ToolError, match="budget hard stop"):
            task_update(second, status="doing", worktree=False)


def test_crossing_the_warning_threshold_on_this_claim_warns(policy):
    policy(_budget(limit=100, warn=80, reserve=10))
    _run("Desktop_Agent2", {"cost_usd": 75.0})
    result = _claim("Desktop_Agent2")
    assert result.get("budget_warnings"), "the claim itself crosses 80% ($85 committed)"


def test_non_finite_cost_is_refused():
    tid = _new_task()
    with _as("Desktop_Agent2"):
        task_claim(tid, "Desktop_Agent2", expected_lane="dispatch-ready")
        for bad in (float("nan"), float("inf")):
            with pytest.raises(ToolError, match="finite"):
                task_update(tid, status="done", evidence_ref="x", run_usage={"cost_usd": bad})


def test_non_finite_cost_in_the_ledger_is_unknown_not_a_bypass():
    assert budgets._classify({"cost_usd": float("nan")}) == "unknown_cost"
    assert budgets._classify({"cost_usd": float("inf")}) == "unknown_cost"
    assert budgets._classify({"cost_usd": 1.5}) == "priced"


def test_project_rollup_uses_the_claim_time_project(policy):
    tid = task_add("t", lane="dispatch-ready", project="unified-agent")["id"]
    with _as("Desktop_Agent2"):
        task_claim(tid, "Desktop_Agent2", expected_lane="dispatch-ready")
        task_update(tid, status="done", evidence_ref="x", run_usage={"cost_usd": 5.0})
        task_update(tid, project="browser")
    groups = {g["project"]: g for g in budgets.cost_rollup("project")["groups"]}
    assert groups["unified-agent"]["api_cost_usd"] == pytest.approx(5.0)
    assert "browser" not in groups


def test_fractional_bounds_round_up_to_whole_seconds():
    assert budgets._parse_bound("2026-09-28T12:00:00.4Z", "since") == "2026-09-28T12:00:01+00:00"
    assert budgets._parse_bound("2026-09-28T12:00:00Z", "since") == "2026-09-28T12:00:00+00:00"


def test_broker_spend_covers_whole_days_the_window_touches(policy):
    conn = storage.connect()
    try:
        llm_spend._table(conn)
        for rid, day in (("a", "2026-09-27"), ("b", "2026-09-28"), ("c", "2026-09-29")):
            conn.execute(
                "INSERT INTO llm_broker_spend (request_id, org_id, agent, device_node, day_utc, "
                "reserve_usd, actual_usd, outcome, updated_at) VALUES (?, '', 'X', 'n', ?, 1, 1, 'completed', ?)",
                (rid, day, storage.now()),
            )
        conn.commit()
    finally:
        conn.close()
    rollup = budgets.cost_rollup(since="2026-09-28T12:00:00Z", until="2026-09-28T18:00:00Z")
    assert rollup["broker"][0]["calls"] == 1  # only 09-28, never an empty window
    assert rollup["broker_days"] == {
        "from": "2026-09-28",
        "to": "2026-09-28",
        "granularity": "utc_day",
    }
    midnight = budgets.cost_rollup(since="2026-09-28T00:00:00Z", until="2026-09-29T00:00:00Z")
    assert midnight["broker"][0]["calls"] == 1  # an exclusive midnight bound excludes 09-29


def test_expired_lease_reacquisition_is_budget_refused_without_changing_run(policy):
    tid = _new_task()
    _claim("Desktop_Agent2", tid)
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (tid,)
        )
        conn.commit()
        before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    finally:
        conn.close()
    policy(_budget(limit=1.0))
    _run("server_agent3", None)
    _run("Desktop_Agent2", {"cost_usd": 5.0})
    with pytest.raises(ToolError, match="budget hard stop"):
        _claim("Desktop_Agent2", tid)
    conn = storage.connect()
    try:
        after = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    finally:
        conn.close()
    assert after == before
    assert after["current_run_id"] is not None


def test_warning_summary_reports_post_claim_spend(policy):
    policy(_budget(limit=100, warn=80, reserve=10))
    _run("Desktop_Agent2", {"cost_usd": 75.0})
    [warning] = _claim("Desktop_Agent2")["budget_warnings"]
    assert "$85.00 of $100.00" in warning


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_expiry_boundary_releases_reserve_for_new_claim(policy, monkeypatch, offset):
    from datetime import UTC, datetime, timedelta

    from unified_agent.tools import tasks

    expiry = datetime(2026, 9, 30, 4, tzinfo=UTC)
    now = expiry + timedelta(seconds=offset)

    class ClaimClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(storage, "now", lambda: now.isoformat(timespec="seconds"))
    monkeypatch.setattr(tasks, "datetime", ClaimClock)
    monkeypatch.setattr(budgets, "datetime", ClaimClock)
    policy(_budget(limit=2, reserve=1))
    _claim("Desktop_Agent2")
    expiring = _claim("Desktop_Agent2")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET lock_expires_at=? WHERE id=?",
            (expiry.isoformat(timespec="seconds"), expiring["id"]),
        )
        conn.commit()
        decision = budgets.evaluate_claim(conn, "Desktop_Agent2", now=now)
        assert bool(decision.blocked) is (offset < 0)
    finally:
        conn.close()
    if offset < 0:
        with pytest.raises(ToolError, match="budget hard stop"):
            _claim("Desktop_Agent2")
    else:
        assert _claim("Desktop_Agent2")["status"] == "doing"


# --- harness spend on open runs (task #1917) ----------------------------------


def _heartbeat(agent: str, tid: int, usage: dict) -> dict:
    with _as(agent):
        return task_claim(tid, agent, expected_lane="dispatch-ready", run_usage=usage)


def _status_row(agent: str = "Desktop_Agent2") -> dict:
    [row] = budgets.budget_status(agent)["budgets"][0]["agents_status"]
    return row


def test_open_run_heartbeat_cost_counts_before_the_run_finishes(policy):
    # An executor run reports its harness cost on a heartbeat while the task
    # stays doing (awaiting review). That cost is spend now, not at merge.
    policy(_budget(limit=1.0, warn=50))
    tid = _new_task()
    _claim("Desktop_Agent2", tid)
    _heartbeat("Desktop_Agent2", tid, {"cost_usd": 0.6})
    row = _status_row()
    assert row["committed_usd"] == pytest.approx(0.6)
    assert row["open_run_cost_usd"] == pytest.approx(0.6)
    assert "budget_warnings" in _claim("Desktop_Agent2")


def test_open_run_cost_survives_a_lapsed_lease(policy):
    # The executor finish leaves the task doing; once the lease lapses the run
    # holds no reserve, but its reported cost still counts.
    policy(_budget(limit=10, reserve=2))
    tid = _new_task()
    _claim("Desktop_Agent2", tid)
    _heartbeat("Desktop_Agent2", tid, {"cost_usd": 0.5})
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET lock_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (tid,)
        )
        conn.commit()
    finally:
        conn.close()
    row = _status_row()
    assert row["in_flight"] == 0
    assert row["committed_usd"] == pytest.approx(0.5)


def test_live_run_charges_the_larger_of_reserve_and_reported_cost(policy):
    policy(_budget(limit=10, reserve=2))
    tid = _new_task()
    _claim("Desktop_Agent2", tid)
    _heartbeat("Desktop_Agent2", tid, {"cost_usd": 0.5})
    assert _status_row()["committed_usd"] == pytest.approx(2.0)
    _heartbeat("Desktop_Agent2", tid, {"cost_usd": 3.5})
    assert _status_row()["committed_usd"] == pytest.approx(3.5)


def test_heartbeat_cost_is_not_double_counted_after_the_finish(policy):
    policy(_budget(limit=10))
    tid = _new_task()
    _claim("Desktop_Agent2", tid)
    _heartbeat("Desktop_Agent2", tid, {"cost_usd": 0.4})
    with _as("Desktop_Agent2"):
        task_update(tid, status="done", evidence_ref="https://example/pr/1")
    row = _status_row()
    assert row["committed_usd"] == pytest.approx(0.4)
    assert row["priced_runs"] == 1
    assert row["open_run_cost_usd"] == 0


def test_subscription_heartbeat_is_not_priced(policy):
    policy(_budget(limit=10, reserve=0))
    tid = _new_task()
    _claim("Desktop_Agent2", tid)
    _heartbeat("Desktop_Agent2", tid, {"billing": "subscription", "cost_usd": 9.0})
    assert _status_row()["committed_usd"] == pytest.approx(0.0)


def test_renewal_with_usage_warns_at_the_threshold(policy):
    policy(_budget(limit=1.0, warn=50))
    tid = _new_task()
    _claim("Desktop_Agent2", tid)
    result = _heartbeat("Desktop_Agent2", tid, {"cost_usd": 0.6})
    assert result["status"] == "doing"
    assert result["budget_warnings"] and "codex" in result["budget_warnings"][0]
    assert "budget_stop" not in result


def test_renewal_with_usage_over_a_block_budget_returns_a_stop(policy):
    policy(_budget(limit=1.0))
    tid = _new_task()
    initial = _claim("Desktop_Agent2", tid)
    result = _heartbeat("Desktop_Agent2", tid, {"cost_usd": 1.3})
    # The renewal itself succeeds: the runner needs its lease to stop cleanly.
    assert result["status"] == "doing"
    assert result["current_run_id"] == initial["current_run_id"]
    assert "budget hard stop" in result["budget_stop"]
    [stop] = _budget_events(budgets.KIND_RUN_STOP)
    assert stop["task_id"] == tid and stop["committed_usd"] == pytest.approx(1.3)


def test_renewal_over_a_warn_budget_only_warns(policy):
    policy(_budget(limit=1.0, action="warn"))
    tid = _new_task()
    _claim("Desktop_Agent2", tid)
    result = _heartbeat("Desktop_Agent2", tid, {"cost_usd": 1.3})
    assert "budget_stop" not in result
    assert "limit reached" in result["budget_warnings"][0]


def test_renewal_without_usage_reports_stop_from_already_recorded_spend(policy):
    policy(_budget(limit=1.0))
    tid = _new_task()
    _claim("Desktop_Agent2", tid)
    _run("Desktop_Agent2", {"cost_usd": 5.0})
    result = _claim("Desktop_Agent2", tid)
    assert "budget hard stop" in result["budget_stop"]
    assert result["status"] == "doing" and result["claimed_by"] == "Desktop_Agent2"


def test_reserve_only_at_the_limit_does_not_stop_a_run(policy):
    # Claims may fill the budget exactly with reserves; a run is stopped only
    # once committed spend goes past the limit.
    policy(_budget(limit=4, reserve=2))
    tid = _new_task()
    _claim("Desktop_Agent2", tid)
    _claim("Desktop_Agent2")
    result = _heartbeat("Desktop_Agent2", tid, {"cost_usd": 1.0})
    assert "budget_stop" not in result


def test_wildcard_status_includes_agent_with_only_detached_heartbeat(policy):
    policy(_budget(limit=10, reserve=2, agents=["*"]))
    tid = _new_task()
    _claim("Desktop_Agent2", tid)
    _heartbeat("Desktop_Agent2", tid, {"cost_usd": 0.5})
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET status='open', claimed_by=NULL, lock_expires_at=NULL WHERE id=?",
            (tid,),
        )
        conn.commit()
    finally:
        conn.close()
    rows = budgets.budget_status()["budgets"][0]["agents_status"]
    row = next(r for r in rows if r["agent"] == "Desktop_Agent2")
    assert row["committed_usd"] == pytest.approx(0.5)
