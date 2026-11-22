"""Approval Ledger poll cadence, poll-now and worker attribution (task #1867).

Approval rows lagged up to 30 minutes behind a PR push because the
``github_app_worker`` poll rode the fixed 30-minute ``pr-watch`` schedule.
Three changes, tested here with GitHub mocked:

(a) The pr-watch job decides each tick what is due from persisted state. While
    the Approval Ledger has a row in flight (pending or approved, not yet
    consumed) the connector poll is due every active interval; otherwise it and
    the legacy journal run every ``SCHEDULE_INTERVAL_S``. A tick with nothing
    due makes no GitHub call, so a faster launchd StartInterval cannot hammer
    GitHub when idle, and the current 1800 s interval still runs every tick.
(b) ``approval_ledger_poll`` (coordination_manage action, HTTP route and
    ``unified-agent-review ledger poll``) runs one poll now on the hub. It is an
    operator action, and it only requests approval rows; it never resolves one.
(c) Worker-created rows carry the ``approval_ledger_worker`` service identity
    instead of the borrowed ``Desktop_Agent2`` bearer.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient
from test_github_app_worker import RecordingTransport, _policy, _pr

from unified_agent import (
    config,
    github_app,
    github_app_fastpath,
    github_app_worker,
    pr_watch,
    review_cli,
    server,
    storage,
)
from unified_agent.tools import approvals
from unified_agent.tools.graph import coordination_manage
from unified_agent.webui.routes import web_routes

OPERATOR = "Operator"
SERVICE = github_app_worker.SERVICE_IDENTITY
T0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture
def connector(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/project")
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", OPERATOR)
    monkeypatch.setenv(github_app_worker.TOKEN_VAR, "service-bearer")
    monkeypatch.setattr(config, "agent_tokens", lambda: {"service-bearer": SERVICE})
    prs = [_pr(sha="head-1")]
    calls: list[list[str]] = []

    def gh(cmd, **_kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=json.dumps(prs))

    return SimpleNamespace(prs=prs, gh=gh, calls=calls, transport=RecordingTransport())


def _poll(c):
    return github_app_worker.run_once(run_gh=c.gh, transport=c.transport, policy_config=_policy())


def _set_status(approval_id: int, status: str) -> None:
    conn = storage.connect()
    try:
        conn.execute("UPDATE approvals SET status=? WHERE id=?", (status, approval_id))
        conn.commit()
    finally:
        conn.close()


# --- (c) attribution ---------------------------------------------------------------


def test_worker_rows_carry_the_service_identity(connector):
    [result] = _poll(connector)
    assert result["status"] == "pending_approval"
    row = approvals.get_approval(result["approval_id"])
    assert row["requested_by"] == SERVICE == "approval_ledger_worker"
    binding = github_app_worker._record(github_app.PENDING_CHANNEL, result["event_id"])
    assert binding["approval_id"] == result["approval_id"]


def test_service_identity_is_a_well_formed_non_fleet_label():
    assert config.agent_label_is_well_formed(SERVICE)
    assert SERVICE != github_app_worker.LEGACY_WORKER_IDENTITY
    assert config.actor_kind_for_identity(SERVICE) != "human"


def test_legacy_bearer_still_polls_but_warns(connector, monkeypatch, caplog):
    # Until the operator registers the service bearer, the old one keeps the
    # ledger working rather than failing every scheduled poll.
    monkeypatch.setattr(config, "agent_tokens", lambda: {"service-bearer": "Desktop_Agent2"})
    with caplog.at_level("WARNING", logger=github_app_worker.LOGGER.name):
        [result] = _poll(connector)
    assert approvals.get_approval(result["approval_id"])["requested_by"] == "Desktop_Agent2"
    assert any(SERVICE in record.getMessage() for record in caplog.records)


def test_any_other_bearer_is_refused(connector, monkeypatch):
    monkeypatch.setattr(config, "agent_tokens", lambda: {"service-bearer": "Desktop_Agent1"})
    with pytest.raises(PermissionError, match=SERVICE):
        _poll(connector)
    assert connector.calls == []


# --- (a) in-flight detection ---------------------------------------------------------


def test_ledger_in_flight_tracks_pending_and_approved_rows(connector):
    assert github_app_worker.ledger_in_flight() is False
    [result] = _poll(connector)
    approval_id = result["approval_id"]
    assert github_app_worker.ledger_in_flight() is True
    _set_status(approval_id, "approved")
    assert github_app_worker.ledger_in_flight() is True
    for settled in ("consumed", "denied", "expired", "revoked"):
        _set_status(approval_id, settled)
        assert github_app_worker.ledger_in_flight() is False, settled


def test_a_review_read_since_the_last_poll_is_in_flight_for_an_open_pr(connector, monkeypatch):
    monkeypatch.setenv("UA_REVIEW_HEAD_CHECK", "1")
    monkeypatch.setattr(
        github_app_worker.review_head_status, "sync_open_heads", lambda *_a, **_k: []
    )
    [result] = _poll(connector)
    _set_status(result["approval_id"], "consumed")
    assert github_app_worker.ledger_in_flight() is False
    read = dict(head_sha="a" * 40, kind="steward-read", reader="r", verdict="pass")
    reads = github_app_worker.review_reads
    reads.record(repo="example/project", number=999, **read)
    assert github_app_worker.ledger_in_flight() is False  # not an open PR
    reads.record(repo="example/project", number=123, **read)
    assert github_app_worker.ledger_in_flight() is True
    _poll(connector)
    assert github_app_worker.ledger_in_flight() is False
    reads.record(repo="example/project", number=123, **{**read, "head_sha": "b" * 40})
    monkeypatch.setenv("UA_REVIEW_HEAD_CHECK", "0")
    assert github_app_worker.ledger_in_flight() is False


def test_ledger_in_flight_ignores_other_repos_and_a_disabled_connector(connector, monkeypatch):
    [result] = _poll(connector)
    assert github_app_worker.ledger_in_flight() is True
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/other")
    assert github_app_worker.ledger_in_flight() is False
    monkeypatch.setenv("UA_GITHUB_CONNECTOR_REPO", "example/project")
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "0")
    assert github_app_worker.ledger_in_flight() is False
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    monkeypatch.delenv("UA_GITHUB_CONNECTOR_REPO")
    assert github_app_worker.ledger_in_flight() is False
    assert result["approval_id"]


# --- (a) tick planning -----------------------------------------------------------------


def _never():
    pytest.fail("in-flight check ran when it was not needed")


def _state(journal=T0, connector=T0):
    return {
        "journal_last_run_at": journal.isoformat() if journal else None,
        "connector_last_run_at": connector.isoformat() if connector else None,
    }


def test_fresh_state_runs_everything_without_the_in_flight_check():
    plan = pr_watch.plan_tick({}, T0, in_flight=_never)
    assert plan.journal and plan.connector


def test_idle_tick_before_the_interval_does_nothing():
    flights = []
    plan = pr_watch.plan_tick(
        _state(), T0 + timedelta(seconds=120), in_flight=lambda: flights.append(1) or False
    )
    assert not plan.journal and not plan.connector
    assert flights == [1]


def test_in_flight_row_makes_the_connector_due_at_the_active_interval():
    plan = pr_watch.plan_tick(_state(), T0 + timedelta(seconds=120), in_flight=lambda: True)
    assert plan.connector and not plan.journal
    assert "in flight" in plan.reason


def test_in_flight_check_is_skipped_inside_the_active_interval():
    plan = pr_watch.plan_tick(_state(), T0 + timedelta(seconds=30), in_flight=_never)
    assert not plan.journal and not plan.connector


@pytest.mark.parametrize("elapsed", [1650, 1790, 1800, 1830, 7200])
def test_idle_interval_runs_both_without_the_in_flight_check(elapsed):
    # Covers the current 1800 s launchd interval (every tick, even a little early)
    # and a 120 s interval (the first tick past 90% of the interval).
    plan = pr_watch.plan_tick(_state(), T0 + timedelta(seconds=elapsed), in_flight=_never)
    assert plan.journal and plan.connector


def test_future_stamp_from_clock_skew_counts_as_due():
    plan = pr_watch.plan_tick(_state(), T0 - timedelta(hours=1), in_flight=_never)
    assert plan.journal and plan.connector


def test_force_runs_everything():
    plan = pr_watch.plan_tick(_state(), T0, in_flight=_never, force=True)
    assert plan.journal and plan.connector


def test_in_flight_check_error_fails_toward_polling():
    def boom():
        raise RuntimeError("db unavailable")

    plan = pr_watch.plan_tick(_state(), T0 + timedelta(seconds=150), in_flight=boom)
    assert plan.connector and not plan.journal


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 120), ("300", 300), ("5", 60), ("99999", pr_watch.SCHEDULE_INTERVAL_S), ("x", 120)],
)
def test_active_interval_env_is_clamped(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("UA_APPROVAL_LEDGER_ACTIVE_INTERVAL_S", raising=False)
    else:
        monkeypatch.setenv("UA_APPROVAL_LEDGER_ACTIVE_INTERVAL_S", raw)
    assert pr_watch.ledger_active_interval_s() == expected


# --- (a) the scheduled job end to end ---------------------------------------------------


@pytest.fixture
def job(isolated_env, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "1")
    calls = SimpleNamespace(connector=0, journal=0, connector_result=[])

    def run_once():
        calls.connector += 1
        return calls.connector_result

    def run(**_kwargs):
        calls.journal += 1
        return []

    monkeypatch.setattr(github_app_worker, "run_once", run_once)
    monkeypatch.setattr(pr_watch, "run", run)
    monkeypatch.setattr(sys, "argv", ["pr-watch"])
    clock = SimpleNamespace(now=T0)
    monkeypatch.setattr(pr_watch, "_now", lambda: clock.now)
    in_flight = SimpleNamespace(value=False)
    monkeypatch.setattr(github_app_worker, "ledger_in_flight", lambda: in_flight.value)
    # The open-head probe (task #2234) is one GitHub request; keep it local.
    changed = SimpleNamespace(value=False, calls=0)

    def heads_changed():
        changed.calls += 1
        return changed.value

    monkeypatch.setattr(github_app_fastpath, "open_heads_changed", heads_changed)
    return SimpleNamespace(calls=calls, clock=clock, in_flight=in_flight, changed=changed)


def test_fast_ticks_make_no_github_calls_while_idle(job):
    pr_watch.main()
    assert (job.calls.connector, job.calls.journal) == (1, 1)
    first_record = pr_watch.load_run_status()
    for minutes in range(2, 27, 2):
        job.clock.now = T0 + timedelta(minutes=minutes)
        pr_watch.main()
    assert (job.calls.connector, job.calls.journal) == (1, 1)
    # A skipped tick is not a run: the doctor's record is untouched.
    assert pr_watch.load_run_status() == first_record
    job.clock.now = T0 + timedelta(minutes=28)
    pr_watch.main()
    assert (job.calls.connector, job.calls.journal) == (2, 2)


def test_fast_ticks_poll_the_connector_while_a_row_is_in_flight(job):
    pr_watch.main()
    job.in_flight.value = True
    for minutes in (2, 4, 6):
        job.clock.now = T0 + timedelta(minutes=minutes)
        pr_watch.main()
    assert (job.calls.connector, job.calls.journal) == (4, 1)
    job.in_flight.value = False
    job.clock.now = T0 + timedelta(minutes=8)
    pr_watch.main()
    assert job.calls.connector == 4


def test_current_half_hour_interval_runs_every_tick(job):
    for tick in range(4):
        job.clock.now = T0 + timedelta(seconds=1800 * tick - (5 if tick else 0))
        pr_watch.main()
    assert (job.calls.connector, job.calls.journal) == (4, 4)


def test_failed_connector_poll_is_retried_on_the_next_tick(job):
    job.calls.connector_result = [{"status": "error", "error": "RuntimeError"}]
    with pytest.raises(SystemExit):
        pr_watch.main()
    job.calls.connector_result = []
    job.clock.now = T0 + timedelta(minutes=2)
    pr_watch.main()
    # The journal succeeded the first time and is not due again yet.
    assert (job.calls.connector, job.calls.journal) == (2, 1)
    assert pr_watch.load_run_status()["last_exit"] == 0


def test_force_flag_runs_a_tick_that_is_not_due(job, monkeypatch):
    pr_watch.main()
    job.clock.now = T0 + timedelta(minutes=2)
    monkeypatch.setattr(sys, "argv", ["pr-watch", "--force"])
    pr_watch.main()
    assert (job.calls.connector, job.calls.journal) == (2, 2)


# --- (b) poll now ---------------------------------------------------------------------


def _poll_now(c, who=OPERATOR):
    with _as(who):
        return github_app_worker.poll_now(
            run_gh=c.gh, transport=c.transport, policy_config=_policy()
        )


def _poll_ledger() -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT payload, originator FROM event_log WHERE channel=? ORDER BY ulid",
            (github_app_worker.POLL_REQUEST_CHANNEL,),
        ).fetchall()
    finally:
        conn.close()
    return [{**json.loads(r["payload"]), "_originator": r["originator"]} for r in rows]


def test_poll_now_requests_rows_and_never_resolves_them(connector):
    out = _poll_now(connector)
    assert out["status"] == "polled" and out["requested_by"] == OPERATOR
    [result] = out["results"]
    assert result["status"] == "pending_approval"
    row = approvals.get_approval(result["approval_id"])
    assert row["status"] == "pending"
    assert row["requested_by"] == SERVICE
    assert out["summary"] == {"pending_approval": 1}
    [event] = _poll_ledger()
    assert event["actor"] == OPERATOR and event["_originator"] == OPERATOR
    assert event["outcome"] == "polled" and event["summary"] == {"pending_approval": 1}
    # The caller's identity is restored after the worker's service identity.
    assert config.verified_agent_identity() is None
    # A second poll finds the row and changes nothing.
    again = _poll_now(connector)
    assert again["results"] == []
    assert row["status"] == approvals.get_approval(result["approval_id"])["status"] == "pending"


def test_poll_now_is_operator_only(connector):
    with pytest.raises(ToolError, match="operator"):
        _poll_now(connector, who="Desktop_Agent1")
    with pytest.raises(ToolError, match="authenticated"):
        github_app_worker.poll_now(run_gh=connector.gh, transport=connector.transport)
    assert connector.calls == []
    assert _poll_ledger() == []


def test_poll_now_needs_the_connector_enabled(connector, monkeypatch):
    monkeypatch.setenv("UA_GITHUB_APP_ENABLED", "0")
    with pytest.raises(ToolError, match="not enabled"):
        _poll_now(connector)
    assert connector.calls == []


def test_poll_now_records_a_failed_poll(connector):
    connector.gh = lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="")
    with pytest.raises(RuntimeError, match="GitHub PR poll failed"):
        _poll_now(connector)
    [event] = _poll_ledger()
    assert event["outcome"] == "error" and event["error"] == "RuntimeError"
    assert event["actor"] == OPERATOR


def test_coordination_manage_exposes_the_poll(connector, monkeypatch):
    seen = []
    monkeypatch.setattr(
        github_app_worker, "poll_now", lambda: seen.append(True) or {"status": "polled"}
    )
    with _as(OPERATOR):
        assert coordination_manage("approval_ledger_poll", {}) == {"status": "polled"}
    assert seen == [True]


TOKENS = {"Desktop_Agent1": "fixture-peer-token", OPERATOR: "fixture-operator-token"}


@pytest.fixture
def http(connector, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "policy.yaml"))
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    monkeypatch.setenv("UA_OPERATOR_LABEL", OPERATOR)
    monkeypatch.setenv("UA_AGENT_TOKENS", ",".join(f"{k}:{v}" for k, v in TOKENS.items()))
    monkeypatch.setattr(
        config,
        "agent_tokens",
        lambda: {**{v: k for k, v in TOKENS.items()}, "service-bearer": SERVICE},
    )
    for name in ("UA_AGENT_ACCESS_ENABLED", "UA_APPROVAL_TOOLS"):
        monkeypatch.delenv(name, raising=False)
    storage.init_db()
    return TestClient(
        server.HubAuthASGIMiddleware(Starlette(routes=web_routes())),
        base_url="http://127.0.0.1",
    )


def test_http_poll_by_the_verified_operator(http, monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", "Desktop_Someone_Else")
    seen = []

    def fake():
        who = config.verified_agent_identity()
        seen.append(who)
        github_app_worker._authorize_operator(who, "poll the Approval Ledger")
        return {"status": "polled", "results": [], "summary": {}, "requested_by": who}

    monkeypatch.setattr(github_app_worker, "poll_now", fake)
    url = "/api/approvals/ledger-poll"
    peer = http.post(url, headers={"Authorization": "Bearer " + TOKENS["Desktop_Agent1"]})
    assert peer.status_code == 403, peer.text
    auth = {"Authorization": "Bearer " + TOKENS[OPERATOR]}
    extra = http.post(url, headers=auth, json={"resolve": True})
    assert extra.status_code == 400
    ok = http.post(url, headers=auth)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "polled"
    assert http.post(url, headers=auth, json={}).status_code == 200
    assert seen[-1] == OPERATOR


def test_http_poll_runs_the_policy_gate(http, tmp_path, monkeypatch):
    (tmp_path / "policy.yaml").write_text(
        "rules:\n"
        "  - name: no-ledger-poll\n"
        "    type: tool\n"
        "    tool: approval_ledger_poll\n"
        "    action: deny\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(github_app_worker, "poll_now", lambda: pytest.fail("polled"))
    resp = http.post(
        "/api/approvals/ledger-poll", headers={"Authorization": "Bearer " + TOKENS[OPERATOR]}
    )
    assert resp.status_code == 403, resp.text
    assert "policy" in resp.json()["error"]


# --- (b) the desktop CLI ----------------------------------------------------------------


@pytest.fixture
def hub(monkeypatch):
    sent = []
    reply = SimpleNamespace(status=200, body={"status": "polled", "results": [], "summary": {}})

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(reply.status, json=reply.body)

    real_client = httpx.Client

    def client(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(review_cli.httpx, "Client", client)
    monkeypatch.setenv("UA_HTTP_HUB_URL", "http://hub.example:8765/mcp")
    monkeypatch.setenv("UA_AGENT_BEARER_TOKEN", "fixture-operator-token")
    monkeypatch.setenv("UA_EXECUTOR_AGENT", OPERATOR)
    return SimpleNamespace(sent=sent, reply=reply)


def test_cli_ledger_poll_posts_to_the_hub(hub, capsys):
    assert review_cli.main(["ledger", "poll"]) == 0
    [request] = hub.sent
    assert request.method == "POST"
    assert str(request.url) == "http://hub.example:8765/api/approvals/ledger-poll"
    assert request.headers["Authorization"] == "Bearer fixture-operator-token"
    assert json.loads(request.content) == {}
    assert json.loads(capsys.readouterr().out)["status"] == "polled"


def test_cli_ledger_poll_reports_a_refusal(hub, capsys):
    hub.reply.status = 403
    hub.reply.body = {"error": "'Desktop_Agent1' may not poll the Approval Ledger"}
    assert review_cli.main(["ledger", "poll"]) == 1
    assert "may not poll" in capsys.readouterr().err


def test_cli_ledger_poll_exits_nonzero_on_a_failed_head(hub, capsys):
    hub.reply.body = {"status": "polled", "results": [{"status": "error"}], "summary": {}}
    assert review_cli.main(["ledger", "poll"]) == 1


def test_cli_ledger_poll_needs_a_hub_url(hub, monkeypatch, capsys):
    monkeypatch.delenv("UA_HTTP_HUB_URL")
    assert review_cli.main(["ledger", "poll"]) == 2
    assert "UA_HTTP_HUB_URL" in capsys.readouterr().err
    assert hub.sent == []


# --- review round 1 (PR #1228): only reachable rows drive the fast cadence -------------


def _expire_pass(approval_id: int) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE approvals SET resolved_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (approval_id,),
        )
        conn.commit()
    finally:
        conn.close()


def test_orphan_approved_row_on_a_closed_pr_does_not_pin_the_fast_cadence(connector, monkeypatch):
    # Codex P2 reproduction: the pending-status POST fails after the row is
    # queued, the human approves it, the PR closes, and the pass outlives its
    # TTL. The worker never reaches the row (no pending marker), so it stays
    # 'approved'; it must not keep the 120 s cadence on forever.
    original = connector.transport.create_check_run

    def fail(**_kwargs):
        raise TimeoutError("status POST lost")

    monkeypatch.setattr(connector.transport, "create_check_run", fail)
    [failed] = _poll(connector)
    assert failed["status"] == "error"
    monkeypatch.setattr(connector.transport, "create_check_run", original)
    [row] = [
        a
        for a in approvals.approval_pending(tool=github_app.TOOL_NAME)["pending"]
        if a["tool"] == github_app.TOOL_NAME
    ]
    with _as(OPERATOR):
        approvals.approval_resolve(row["id"], "approve")
    connector.prs.clear()  # the PR closed before the next poll
    for _ in range(3):
        assert _poll(connector) == []
    assert approvals.get_approval(row["id"])["status"] == "approved"
    assert github_app_worker.ledger_in_flight() is False
    _expire_pass(row["id"])
    assert github_app_worker.ledger_in_flight() is False


def test_live_approved_pass_on_an_open_pr_keeps_the_fast_cadence(connector, monkeypatch):
    [first] = _poll(connector)
    with _as(OPERATOR):
        approvals.approval_resolve(first["approval_id"], "approve")
    assert github_app_worker.ledger_in_flight() is True
    # Past its TTL the pass can no longer be spent: inert, not in flight.
    monkeypatch.setattr(config, "approval_pass_ttl_hours", lambda: 1)
    _expire_pass(first["approval_id"])
    assert github_app_worker.ledger_in_flight() is False


def test_row_for_a_superseded_head_is_not_in_flight(connector):
    [first] = _poll(connector)
    assert github_app_worker.ledger_in_flight() is True
    # The last poll saw a newer head; until a poll retires the old row, it
    # alone does not count.
    github_app_worker._save_open_heads("example/project", {123: "head-2"})
    assert approvals.get_approval(first["approval_id"])["status"] == "pending"
    assert github_app_worker.ledger_in_flight() is False


def test_stale_or_missing_open_head_snapshot_is_not_in_flight(connector, monkeypatch):
    [first] = _poll(connector)
    assert github_app_worker.ledger_in_flight() is True
    stale = datetime.now(UTC) - timedelta(seconds=github_app_worker.OPEN_HEADS_MAX_AGE_S + 60)
    github_app_worker._save_open_heads("example/project", {123: "head-1"}, at=stale)
    assert github_app_worker.ledger_in_flight() is False
    github_app_worker.open_heads_path().unlink()
    assert github_app_worker.ledger_in_flight() is False
    assert first["approval_id"]


# --- review round 1 (PR #1228): overlapping launches run a tick once ----------------------


def test_overlapping_launch_skips_while_another_holds_the_tick_lock(job, capsys):
    import fcntl

    pr_watch.main()
    assert (job.calls.connector, job.calls.journal) == (1, 1)
    job.clock.now = T0 + timedelta(hours=1)  # everything due again
    lock_path = pr_watch.tick_lock_path()
    with lock_path.open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        pr_watch.main()
    assert (job.calls.connector, job.calls.journal) == (1, 1)
    assert "another pr-watch run" in capsys.readouterr().out
    pr_watch.main()
    assert (job.calls.connector, job.calls.journal) == (2, 2)


def test_concurrent_launches_run_one_tick(job, monkeypatch):
    # Codex P3 reproduction: two launches that both read a due state must not
    # both run it. A barrier after the state read forces the overlap.
    import threading

    barrier = threading.Barrier(2, timeout=5)
    real_load = pr_watch.load_schedule

    def load_then_wait():
        state = real_load()
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return state

    monkeypatch.setattr(pr_watch, "load_schedule", load_then_wait)
    threads = [threading.Thread(target=pr_watch.main) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert (job.calls.connector, job.calls.journal) == (1, 1)
