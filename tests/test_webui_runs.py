"""Runs v0 projection routes (task #1652).

One run is recorded in one of three stores — ``dispatch_runs`` (approved
dispatch), the ``agent.run.selfpickup`` event-log channel (self-pickup claims)
and ``agent_sessions`` (node/harness sessions). ``GET /api/runs`` folds all
three into one uniform row list and ``GET /api/runs/{run_id}`` reads a single
row. The projection is read-only: a lapsed self-pickup lease reads as
``abandoned`` without a write, exactly as ``run_records`` defines it.
"""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, events, registry, run_records, server, storage, webui
from unified_agent.tools.tasks import task_add, task_claim, task_update

DEV_TOKEN = f"tok-runs-dev-{secrets.token_urlsafe(8)}"
PUBLIC_TOKEN = f"tok-runs-pub-{secrets.token_urlsafe(8)}"
RESEARCH_TOKEN = f"tok-runs-res-{secrets.token_urlsafe(8)}"
OPERATOR_TOKEN = f"tok-runs-opr-{secrets.token_urlsafe(8)}"
OPERATOR = "Desktop_Person"  # conftest's UA_OPERATOR_LABEL default
AGENT = "Desktop_Agent1"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv(
        "UA_AGENT_TOKENS",
        ",".join(
            [
                f"Desktop_Agent2:{DEV_TOKEN}",
                f"server_agent8:{PUBLIC_TOKEN}",
                f"Desktop_Agent8:{RESEARCH_TOKEN}",
                f"{OPERATOR}:{OPERATOR_TOKEN}",
            ]
        ),
    )
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as c:
        yield c


def _auth(token: str = DEV_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


def _exec(sql: str, params: tuple = ()) -> None:
    conn = storage.connect()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _new_task(title: str = "runs-task") -> int:
    return task_add(title, lane="dispatch-ready")["id"]


def _dispatch_run(
    task_id: int,
    run_id: str,
    *,
    state: str = "running",
    agent: str = "Desktop_Agent2",
    created_at: str | None = None,
    lease_expires_at: str | None = None,
) -> str:
    ts = created_at or storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO dispatch_runs (run_id, task_id, agent_label, state, actor, "
            "lease_expires_at, created_at, updated_at) VALUES (?, ?, ?, ?, 'fixture', ?, ?, ?)",
            (run_id, task_id, agent, state, lease_expires_at, ts, ts),
        )
        conn.commit()
    finally:
        conn.close()
    return run_id


def _selfpickup_run(task_id: int, agent: str = AGENT) -> str:
    with _as(agent):
        task_claim(task_id, agent, expected_lane="dispatch-ready")
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT json_extract(payload, '$.run_id') AS rid FROM event_log "
            "WHERE channel=? AND json_extract(payload, '$.kind')=? "
            "AND json_extract(payload, '$.task_id')=?",
            (run_records.CHANNEL, run_records.KIND_CLAIMED, task_id),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row["rid"]


def _session_run(agent: str = AGENT, *, kind: str = "acp", task_id: int | None = None) -> str:
    session_id = registry.start_session(agent, kind, task_id=task_id)
    assert session_id is not None
    return session_id


def _seconds_ago(n: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=n)).isoformat(timespec="seconds")


class TestRunsList:
    def test_one_row_per_kind(self, client):
        tid = _new_task()
        dispatch_id = _dispatch_run(tid, f"run-{tid}-aa01")
        sp_id = _selfpickup_run(_new_task())
        session_id = _session_run(task_id=_new_task())

        res = client.get("/api/runs", headers=_auth())
        assert res.status_code == 200, res.text
        runs = res.json()["runs"]
        by_kind = {r["kind"]: r for r in runs}
        assert set(by_kind) == {"dispatch", "selfpickup", "session"}
        assert by_kind["dispatch"]["run_id"] == dispatch_id
        assert by_kind["dispatch"]["task_id"] == tid
        assert by_kind["dispatch"]["agent"] == "Desktop_Agent2"
        assert by_kind["selfpickup"]["run_id"] == sp_id
        assert by_kind["selfpickup"]["agent"] == AGENT
        assert by_kind["session"]["run_id"] == session_id
        for row in runs:
            for field in (
                "run_id",
                "kind",
                "task_id",
                "agent",
                "status",
                "outcome",
                "started_at",
                "ended_at",
                "lease_expires_at",
                "abandoned",
                "links",
            ):
                assert field in row, (row["run_id"], field)
            assert row["links"]["self"]["uri"]

    def test_kind_filter(self, client):
        _dispatch_run(_new_task(), "run-kt-0001")
        _selfpickup_run(_new_task())
        _session_run()
        res = client.get("/api/runs?kind=selfpickup", headers=_auth())
        assert res.status_code == 200, res.text
        kinds = {r["kind"] for r in res.json()["runs"]}
        assert kinds == {"selfpickup"}

    def test_bad_kind_is_400(self, client):
        res = client.get("/api/runs?kind=bogus", headers=_auth())
        assert res.status_code == 400

    def test_lapsed_selfpickup_lease_reads_abandoned_without_a_write(self, client):
        tid = _new_task()
        run_id = _selfpickup_run(tid)
        _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_seconds_ago(120), tid))

        res = client.get("/api/runs?kind=selfpickup", headers=_auth())
        assert res.status_code == 200, res.text
        run = next(r for r in res.json()["runs"] if r["run_id"] == run_id)
        assert run["abandoned"] is True
        assert run["abandon_reason"] == "lease_expired"

        # Read-only: no finish event was written for the lapsed run.
        conn = storage.connect()
        try:
            finishes = conn.execute(
                "SELECT COUNT(*) AS c FROM event_log WHERE dedupe_key=?",
                (f"selfpickup:finished:{run_id}",),
            ).fetchone()["c"]
        finally:
            conn.close()
        assert finishes == 0

    def test_stale_session_reads_abandoned_without_a_write(self, client):
        session_id = _session_run()
        _exec(
            "UPDATE agent_sessions SET last_heartbeat_at=? WHERE id=?",
            (_seconds_ago(60 * 60), session_id),
        )
        res = client.get("/api/runs?kind=session", headers=_auth())
        assert res.status_code == 200, res.text
        run = next(r for r in res.json()["runs"] if r["run_id"] == session_id)
        assert run["abandoned"] is True
        conn = storage.connect()
        try:
            row = conn.execute(
                "SELECT status FROM agent_sessions WHERE id=?", (session_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "running"  # untouched: detection was on read

    def test_dispatch_run_links(self, client):
        tid = _new_task()
        run_id = _dispatch_run(tid, f"run-{tid}-bb02")
        res = client.get(f"/api/runs/{run_id}", headers=_auth())
        assert res.status_code == 200, res.text
        run = res.json()["run"]
        assert run["kind"] == "dispatch"
        assert run["links"]["self"]["uri"] == f"unified-agent://runs/{run_id}"
        assert run["links"]["task"]["uri"] == f"unified-agent://tasks/{tid}"

    def test_pagination_by_started_at_run_id_is_stable(self, client):
        ids = []
        for i in range(4):
            # One live dispatch slot per task (idx_dispatch_runs_live_task).
            ids.append(
                _dispatch_run(_new_task(), f"run-pg-{i:04d}", created_at=_seconds_ago(100 - i))
            )
        ids.append(_session_run())

        seen: list[str] = []
        cursor = None
        for _ in range(8):
            url = "/api/runs?limit=2" + (f"&cursor={cursor}" if cursor else "")
            res = client.get(url, headers=_auth())
            assert res.status_code == 200, res.text
            body = res.json()
            seen.extend(r["run_id"] for r in body["runs"])
            cursor = body.get("next_cursor")
            if not cursor:
                break
        assert len(seen) == len(set(seen)) == 5
        assert set(seen) == set(ids)

        # Keys strictly decrease down the pages: a stable total order.
        keys = []
        cursor = None
        for _ in range(8):
            url = "/api/runs?limit=2" + (f"&cursor={cursor}" if cursor else "")
            body = client.get(url, headers=_auth()).json()
            keys.extend((r["started_at"], r["run_id"]) for r in body["runs"])
            cursor = body.get("next_cursor")
            if not cursor:
                break
        assert keys == sorted(keys, reverse=True)

    def test_tenant_fence_hides_other_orgs_runs(self, client, monkeypatch):
        # Records written while the hub's tenant is org-b (event_log is
        # append-only, so the foreign stamp must happen at write time).
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org-b")
        tid = _new_task()
        dispatch_id = _dispatch_run(tid, f"run-{tid}-cc03")
        sp_id = _selfpickup_run(_new_task())
        session_id = _session_run()

        # The caller's tenant is org-a: nothing from org-b may be returned.
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org-a")
        res = client.get("/api/runs", headers=_auth())
        assert res.status_code == 200, res.text
        assert res.json()["runs"] == []
        for rid in (dispatch_id, sp_id, session_id):
            assert client.get(f"/api/runs/{rid}", headers=_auth()).status_code == 404


class TestRunDetail:
    def test_detail_per_kind(self, client):
        tid = _new_task()
        dispatch_id = _dispatch_run(tid, f"run-{tid}-dd04")
        sp_id = _selfpickup_run(_new_task())
        session_id = _session_run(task_id=_new_task())

        dispatch = client.get(f"/api/runs/{dispatch_id}", headers=_auth()).json()["run"]
        assert dispatch["kind"] == "dispatch" and dispatch["status"] == "running"
        sp = client.get(f"/api/runs/{sp_id}", headers=_auth()).json()["run"]
        assert sp["kind"] == "selfpickup" and sp["status"] == "open"
        session = client.get(f"/api/runs/{session_id}", headers=_auth()).json()["run"]
        assert session["kind"] == "session" and session["status"] == "running"

    def test_unknown_run_id_is_404(self, client):
        for rid in ("run-9999-dead", "sp-01JZZZZZZZZZZZZZZZZZZZZZZZ", "01JZZZZZZZZZZZZZZZZZZZZZZZ"):
            res = client.get(f"/api/runs/{rid}", headers=_auth())
            assert res.status_code == 404, (rid, res.text)

    def test_selfpickup_detail_reads_abandoned(self, client):
        tid = _new_task()
        run_id = _selfpickup_run(tid)
        _exec("UPDATE tasks SET lock_expires_at=? WHERE id=?", (_seconds_ago(120), tid))
        res = client.get(f"/api/runs/{run_id}", headers=_auth())
        assert res.status_code == 200, res.text
        run = res.json()["run"]
        assert run["abandoned"] is True
        assert run["status"] == "abandoned"

    def test_acp_session_links_to_its_node_dispatch_item(self, client):
        # node_dispatch.session_id holds the ACP session id, which joins to
        # agent_sessions.external_session_id — not the session row's ULID.
        session_id = registry.start_session(AGENT, "acp", external_session_id="acp-42")
        assert session_id is not None
        now = storage.now()
        _exec(
            "INSERT INTO node_dispatch (id, node, agent, backend, prompt, cwd, timeout_s, "
            "status, created_by, created_at, updated_at, session_id, run_id) "
            "VALUES ('nd-acp-1','mbp','server_agent3','acp','p','/w',60,'claimed','fixture',?,?,?,?)",
            (now, now, "acp-42", "run-T-x"),
        )
        ulid = events.record_event(
            "agent.node",
            {"kind": "claim", "node": "mbp", "item_id": "nd-acp-1"},
            originator=AGENT,
        )

        res = client.get(f"/api/runs/{session_id}", headers=_auth())
        assert res.status_code == 200, res.text
        run = res.json()["run"]
        assert run["kind"] == "session"
        assert "_external_session_id" not in run
        assert run["links"]["dispatch_run"]["id"] == "run-T-x"
        assert [e["id"] for e in run["links"]["evidence"]] == [ulid]

        # A session without an external id gets no node links.
        plain = client.get(f"/api/runs/{_session_run()}", headers=_auth()).json()["run"]
        assert plain["links"]["dispatch_run"] is None
        assert plain["links"]["evidence"] == []

    def test_open_run_reports_the_live_lease_after_renewal(self, client):
        tid = _new_task()
        run_id = _selfpickup_run(tid)
        # A live renewal moves tasks.lock_expires_at without a new claim event.
        with _as(AGENT):
            task_claim(tid, AGENT, timeout_seconds=7200, expected_lane="dispatch-ready")
        conn = storage.connect()
        try:
            live_lease = conn.execute(
                "SELECT lock_expires_at FROM tasks WHERE id=?", (tid,)
            ).fetchone()["lock_expires_at"]
        finally:
            conn.close()

        res = client.get(f"/api/runs/{run_id}", headers=_auth())
        assert res.status_code == 200, res.text
        run = res.json()["run"]
        assert run["abandoned"] is False
        assert run["lease_expires_at"] == live_lease

    def test_finished_run_keeps_its_claim_snapshot_lease(self, client):
        tid = _new_task()
        run_id = _selfpickup_run(tid)
        conn = storage.connect()
        try:
            snapshot = conn.execute(
                "SELECT json_extract(payload, '$.lock_expires_at') AS lease FROM event_log "
                "WHERE channel=? AND json_extract(payload, '$.run_id')=?",
                (run_records.CHANNEL, run_id),
            ).fetchone()["lease"]
        finally:
            conn.close()
        with _as(AGENT):
            task_claim(tid, AGENT, timeout_seconds=7200, expected_lane="dispatch-ready")
            task_update(tid, status="done", evidence_ref="ev-finished-1")

        res = client.get(f"/api/runs/{run_id}", headers=_auth())
        assert res.status_code == 200, res.text
        run = res.json()["run"]
        assert run["status"] == "finished"
        assert run["lease_expires_at"] == snapshot

    def test_retryable_dispatch_attempt_reports_an_outcome(self, client):
        run_id = _dispatch_run(_new_task(), "run-rt-0001", state="retryable")
        conn = storage.connect()
        try:
            updated_at = conn.execute(
                "SELECT updated_at FROM dispatch_runs WHERE run_id=?", (run_id,)
            ).fetchone()["updated_at"]
        finally:
            conn.close()

        run = client.get(f"/api/runs/{run_id}", headers=_auth()).json()["run"]
        assert run["status"] == "retryable"
        assert run["outcome"] == "retryable"
        assert run["ended_at"] == updated_at

        _exec("UPDATE dispatch_runs SET state='claimed' WHERE run_id=?", (run_id,))
        run = client.get(f"/api/runs/{run_id}", headers=_auth()).json()["run"]
        assert run["status"] == "claimed"
        assert run["outcome"] is None
        assert run["ended_at"] is None


class TestRunsGate:
    def test_unauthenticated_is_401(self, client):
        assert client.get("/api/runs").status_code == 401

    def test_profile_without_the_reader_gets_403(self, client):
        # server_agent8 is the public profile; Desktop_Agent8 is research. Neither
        # holds dispatch_report_self, the run object's profile_tool.
        for token in (PUBLIC_TOKEN, RESEARCH_TOKEN):
            assert client.get("/api/runs", headers=_auth(token)).status_code == 403
            assert client.get("/api/runs/run-1-aa", headers=_auth(token)).status_code == 403

    def test_operator_reads_runs(self, client):
        # The operator profile holds no dispatch_report_self; the route's
        # operator exemption admits the verified human operator anyway.
        res = client.get("/api/runs", headers=_auth(OPERATOR_TOKEN))
        assert res.status_code == 200, res.text
