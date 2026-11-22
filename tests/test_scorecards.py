"""Per-agent scorecards and saved eval runs (task #1354).

Ground truth is real claims/finishes through task_claim/task_update (the #1436
run record), real review-pipeline note markers and executor_attempts rows.
"""

from __future__ import annotations

import secrets
from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from unified_agent import config, events, monitor, run_records, scorecards, server, storage
from unified_agent.tools.tasks import task_add, task_claim, task_update
from unified_agent.webui.routes import web_routes


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "50")
    evals = tmp_path / "evals"
    evals.mkdir()
    (evals / "mini.yaml").write_text(
        "name: mini\nversion: '2'\ncases:\n"
        "  - id: a\n    task: do a\n    checks: [a done]\n"
        "  - id: b\n    task: do b\n    checks: [b done]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_EVALS_DIR", str(evals))
    return evals


def _run(agent: str, status: str = "done", note: str | None = None) -> int:
    tid = task_add("t", lane="dispatch-ready")["id"]
    with _as(agent):
        task_claim(tid, agent, expected_lane="dispatch-ready")
        kwargs = {"evidence_ref": "https://example/pr/1"} if status == "done" else {}
        task_update(tid, status=status, **kwargs)
        if note:
            task_update(tid, append_note=note)
    return tid


def _card(agent: str) -> dict:
    [card] = scorecards.agent_scorecard(agent)["agents"]
    return card


def test_delivery_rate_excludes_neutral_outcomes():
    _run("Desktop_Agent2", "done")
    _run("Desktop_Agent2", "waiting_review")
    _run("Desktop_Agent2", "blocked")
    _run("Desktop_Agent2", "open")  # released: neutral
    card = _card("Desktop_Agent2")
    assert card["runs"]["finished"] == 4
    assert card["delivery_rate"] == {
        "value": 0.6667,
        "numerator": 2,
        "denominator": 3,
        "sample": "small",
    }


def test_expired_open_claim_counts_as_inferred_abandonment():
    tid = task_add("expired", lane="dispatch-ready")["id"]
    with _as("Desktop_Agent2"):
        task_claim(tid, "Desktop_Agent2", expected_lane="dispatch-ready")
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE tasks SET lock_expires_at='2020-01-01T00:00:00+00:00' WHERE id=?", (tid,)
        )
        conn.commit()
    finally:
        conn.close()
    card = _card("Desktop_Agent2")
    assert card["runs"]["outcomes"]["abandoned"] == 1
    assert card["abandon_rate"]["numerator"] == 1
    assert card["delivery_rate"]["denominator"] == 1


def test_unfinished_claim_uses_run_record_abandonment_classification():
    tid = task_add("unfinished", lane="dispatch-ready")["id"]
    with _as("Desktop_Agent2"):
        task_claim(tid, "Desktop_Agent2", expected_lane="dispatch-ready")
    assert scorecards.agent_scorecard("Desktop_Agent2")["agents"] == []
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET status='open', claimed_by=NULL WHERE id=?", (tid,))
        conn.commit()
    finally:
        conn.close()
    assert _card("Desktop_Agent2")["runs"]["outcomes"] == {"abandoned": 1}


@pytest.mark.parametrize("task_status", sorted(run_records._NOT_IN_FLIGHT))
def test_terminal_task_with_unclosed_run_is_not_scored_as_abandoned(task_status):
    tid = task_add("unclosed", lane="dispatch-ready")["id"]
    with _as("Desktop_Agent2"):
        task_claim(tid, "Desktop_Agent2", expected_lane="dispatch-ready")
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (task_status, tid))
        conn.commit()
    finally:
        conn.close()
    assert scorecards.agent_scorecard("Desktop_Agent2")["agents"] == []


def test_window_uses_claim_time_for_persisted_outcomes():
    tid = _run("Desktop_Agent2")
    conn = storage.connect()
    try:
        with events.ledger_maintenance(conn):
            conn.execute(
                "UPDATE event_log SET payload=json_set(payload, '$.claimed_at', ?) "
                "WHERE channel=? AND json_extract(payload, '$.kind')=? "
                "AND json_extract(payload, '$.task_id')=?",
                ("2020-01-01T00:00:00+00:00", run_records.CHANNEL, run_records.KIND_CLAIMED, tid),
            )
        conn.commit()
    finally:
        conn.close()
    assert scorecards.agent_scorecard("Desktop_Agent2")["agents"] == []


def test_review_verdicts_are_consumed_from_pipeline_markers():
    _run("Desktop_Agent2", note="[review: approved @ 2026-09-28] ok")
    _run("Desktop_Agent2", note="[review: rejected @ 2026-09-28] bad")
    _run("Desktop_Agent2", note="[review: rejected @ x] first\n[review: validated @ y] fixed")
    _run("Desktop_Agent2", note="[review: validated @ t1] ok\n[review: rework — tests missing]")
    _run("Desktop_Agent2")
    review = _card("Desktop_Agent2")["review_approval_rate"]
    assert (review["numerator"], review["denominator"], review["unreviewed"]) == (2, 4, 1)
    assert review["unit"] == "task"


def test_review_examples_inside_prose_are_not_verdicts():
    _run("Desktop_Agent2", note='Example: "[review: approved]" is only a quote')
    _run("Desktop_Agent2", note="[review: rejected] real\n> [review: approved] quoted")
    review = _card("Desktop_Agent2")["review_approval_rate"]
    assert (review["numerator"], review["denominator"], review["unreviewed"]) == (0, 1, 1)


def test_unclaimed_finish_is_not_credited_to_closer():
    task_id = task_add("unclaimed", lane="dispatch-ready")["id"]
    with _as("Desktop_Agent2"):
        task_update(task_id, status="done", evidence_ref="https://example/pr/2")
    assert scorecards.agent_scorecard("Desktop_Agent2")["agents"] == []


def test_reopen_counts_a_later_claim_of_a_delivered_task():
    tid = _run("Desktop_Agent2")
    _run("Desktop_Agent2")
    with _as("server_agent3"):
        task_update(tid, status="open")
        task_claim(tid, "server_agent3", expected_lane="dispatch-ready")
    reopen = _card("Desktop_Agent2")["reopen_rate"]
    assert (reopen["numerator"], reopen["denominator"]) == (1, 2)


def test_harness_failures_are_kept_apart_from_quality():
    conn = storage.connect()
    try:
        now = storage.now()
        rows = [
            ("failure", "provider_quota_exhausted"),
            ("failure", "zero_output_attempt"),
            ("failure", "code_error"),
            ("blocked", "code_error"),
            ("quarantined", "code_error"),
            ("cooldown", "code_error"),
            ("killed", "code_error"),
            ("failure", None),  # older unclassified failure is not harness evidence
            ("success", None),
        ]
        for outcome, cls in rows:
            conn.execute(
                "INSERT INTO executor_attempts (agent, backend, started_at, outcome, failure_class) "
                "VALUES ('server_agent2', 'codex', ?, ?, ?)",
                (now, outcome, cls),
            )
        conn.commit()
    finally:
        conn.close()
    harness = _card("server_agent2")["harness"]
    assert harness["attempts"] == 9 and harness["harness_failures"] == 2
    assert harness["quality_success_rate"]["numerator"] == 1
    assert harness["quality_success_rate"]["denominator"] == 6


def test_administrative_kill_is_neutral_for_quality():
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO executor_attempts (agent, backend, started_at, outcome, failure_class) "
            "VALUES ('server_agent2', 'codex', ?, 'killed', 'code_error')",
            (storage.now(),),
        )
        conn.commit()
    finally:
        conn.close()
    harness = _card("server_agent2")["harness"]
    assert harness["attempts"] == 1
    assert harness["quality_success_rate"]["value"] is None
    assert harness["quality_success_rate"]["denominator"] == 0


def test_eval_record_validates_and_scores_per_suite_and_model():
    with _as("Desktop_Agent2"):
        scorecards.eval_record(
            "mini",
            "Desktop_Agent2",
            [{"case": "a", "passed": True}, {"case": "b", "passed": False}],
            model="gpt-x",
        )
        scorecards.eval_record(
            "mini", "Desktop_Agent2", harness_error="sandbox failed to start", model="gpt-x"
        )
        with pytest.raises(ToolError, match="missing case"):
            scorecards.eval_record("mini", "Desktop_Agent2", [{"case": "a", "passed": True}])
        with pytest.raises(ToolError, match="unknown or repeated"):
            scorecards.eval_record(
                "mini",
                "Desktop_Agent2",
                [{"case": "a", "passed": True}, {"case": "a", "passed": True}],
            )
        for bad in ("../etc", "..", ".hidden", "a/b"):
            with pytest.raises(ToolError, match="unknown eval suite"):
                scorecards.eval_record(bad, "Desktop_Agent2", [])
        with pytest.raises(ToolError, match="non-empty description"):
            scorecards.eval_record("mini", "Desktop_Agent2", harness_error="  ")
        with pytest.raises(ToolError, match="not both"):
            scorecards.eval_record(
                "mini", "Desktop_Agent2", [{"case": "a", "passed": True}], harness_error="x"
            )
    [row] = _card("Desktop_Agent2")["evals"]
    assert (row["suite"], row["model"], row["runs"], row["harness_errors"]) == (
        "mini",
        "gpt-x",
        2,
        1,
    )
    assert row["pass_rate"]["value"] == 0.5
    assert row["suite_version"] == "2"


def test_eval_attribution_and_request_bounds():
    with _as("Desktop_Agent2"):
        with pytest.raises(ToolError, match="match the verified"):
            scorecards.eval_record("mini", "Desktop_Agent1", harness_error="x")
        with pytest.raises(ToolError, match="detail"):
            scorecards.eval_record(
                "mini",
                "Desktop_Agent2",
                [{"case": "a", "passed": True, "detail": "x" * 301}, {"case": "b", "passed": True}],
            )
        with pytest.raises(ToolError, match="model"):
            scorecards.eval_record("mini", "Desktop_Agent2", harness_error="x", model="x" * 201)
        with pytest.raises(ToolError, match="harness_error"):
            scorecards.eval_record("mini", "Desktop_Agent2", harness_error="x" * 501)
    with pytest.raises(ToolError, match="verified caller"):
        scorecards.eval_record("mini", "Desktop_Agent2", harness_error="x")


def test_eval_versions_are_scored_separately(_env):
    with _as("Desktop_Agent2"):
        scorecards.eval_record(
            "mini", "Desktop_Agent2", [{"case": "a", "passed": True}, {"case": "b", "passed": True}]
        )
        path = _env / "mini.yaml"
        path.write_text(path.read_text().replace("version: '2'", "version: '3'"))
        scorecards.eval_record(
            "mini",
            "Desktop_Agent2",
            [{"case": "a", "passed": False}, {"case": "b", "passed": False}],
        )
    rows = _card("Desktop_Agent2")["evals"]
    assert [(r["suite_version"], r["pass_rate"]["value"]) for r in rows] == [("2", 1.0), ("3", 0.0)]


def test_oversized_suite_is_reported_and_cannot_be_recorded(_env):
    (_env / "huge.yaml").write_text(
        "name: huge\nversion: '1'\ncases:\n"
        + "".join(f"  - id: c{i}\n    task: t\n    checks: [ok]\n" for i in range(201))
    )
    assert any(e["file"] == "huge.yaml" for e in scorecards.eval_suites()["errors"])
    with _as("Desktop_Agent2"), pytest.raises(ToolError, match="1 to 200"):
        scorecards.eval_record("huge", "Desktop_Agent2", harness_error="x")


def test_malformed_suites_are_reported_not_raised(_env):
    (_env / "broken.yaml").write_text("name: other\ncases: []\n", encoding="utf-8")
    (_env / "unversioned.yaml").write_text(
        "name: unversioned\ncases:\n  - id: a\n    task: do a\n    checks: [ok]\n",
        encoding="utf-8",
    )
    listing = scorecards.eval_suites()
    assert [s["name"] for s in listing["suites"]] == ["mini"]
    assert {e["file"] for e in listing["errors"]} == {"broken.yaml", "unversioned.yaml"}
    with _as("Desktop_Agent2"), pytest.raises(ToolError, match="version"):
        scorecards.eval_record("unversioned", "Desktop_Agent2", harness_error="x")


def test_packaged_eval_suites_are_valid(monkeypatch):
    monkeypatch.delenv("UA_EVALS_DIR")
    assert scorecards.evals_dir().parent.name == "unified_agent"
    listing = scorecards.eval_suites()
    assert listing["errors"] == []
    assert "task-lifecycle" in {s["name"] for s in listing["suites"]}


def test_ci_first_pass_is_unavailable_not_invented():
    _run("Desktop_Agent2")
    ci = _card("Desktop_Agent2")["ci_first_pass_rate"]
    assert ci["value"] is None and ci["status"] == "unavailable"


def test_window_is_validated():
    assert scorecards.agent_scorecard()["run_cohort"] == "claimed_at"
    with pytest.raises(ToolError, match="window_days"):
        scorecards.agent_scorecard(window_days=0)
    with pytest.raises(ToolError, match="window_days"):
        scorecards.agent_scorecard(window_days="x")


def test_observability_actions_and_route(monkeypatch):
    _run("Desktop_Agent2")
    assert monitor.observability_manage("scorecard", {"agent": "Desktop_Agent2"})["agents"]
    assert monitor.observability_manage("eval_suites", {})["suites"][0]["name"] == "mini"
    token = f"tok-sc-{secrets.token_urlsafe(8)}"
    monkeypatch.setenv("UA_AGENT_TOKENS", f"Desktop_Agent1:{token}")
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    app = server.HubAuthASGIMiddleware(Starlette(routes=web_routes()))
    with TestClient(app) as client:
        body = client.get("/api/scorecards", headers={"Authorization": f"Bearer {token}"}).json()
        assert body["run_cohort"] == "claimed_at"
        assert body["agents"][0]["agent"] == "Desktop_Agent2"
        assert body["suites"] == [{"name": "mini", "cases": 2}]
        assert client.get("/api/scorecards").status_code in {401, 403}
