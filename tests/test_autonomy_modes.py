"""Autonomy modes contract and operator controls (task #1457).

Covers the acceptance list from the task spec: the same fixture under two
modes yields different pause behavior, a mission set looser than the org
ceiling is refused, an agent can only propose a mode change (a human
applies it), a revoked grant / tightened mode flips pending work, attribution
distinguishes human-approved from policy-authorized, existing explicit gates
(#1351 lifecycle, #1348 budgets, the global approval gate) stay enforced even
in the loosest mode.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import agent_lifecycle, autonomy_modes, budgets, config, events, storage
from unified_agent.goals import goal_add, goal_manage
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_resolve,
    check_approval_gate,
)
from unified_agent.tools.tasks import task_add, task_claim, task_update

HUMAN = "Desktop_Person"
AGENT = "Desktop_Agent2"
OTHER_HUMAN = "Operator"  # alias of Desktop_Person, still a distinct approver label


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "ws"))
    # Budget projections must never discover the operator's Codex transcripts.
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(tmp_path / "no-codex-sessions"))
    (tmp_path / "ws").mkdir()
    storage.init_db()


def _mission(title: str = "Ship the thing") -> int:
    return goal_add(title, author=HUMAN)["goal"]["id"]


# --- 1. same fixture, two modes, different pause behavior --------------------


def test_same_mission_two_modes_different_pause():
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)
    observed = autonomy_modes.evaluate(mission, "merge", AGENT)
    assert observed["decision"] == "proceed"
    assert observed["attribution"] == "policy-authorized"

    autonomy_modes.set_mode(mission, "action-review", HUMAN)
    reviewed = autonomy_modes.evaluate(mission, "merge", AGENT)
    assert reviewed["decision"] == "pause_for_human"
    assert reviewed["surfaced"] is True


def test_autonomy_evaluation_does_not_scan_operator_codex_sessions(tmp_path, monkeypatch):
    operator_home = tmp_path / "operator-home"
    operator_sessions = operator_home / ".codex" / "sessions"
    operator_sessions.mkdir(parents=True)
    meter = {
        "timestamp": storage.now(),
        "payload": {
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": 100, "window_minutes": 10080},
            }
        },
    }
    (operator_sessions / "operator-meter.jsonl").write_text(
        json.dumps(meter) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: operator_home))
    operator_scans = []
    original_rglob = Path.rglob

    def guarded_rglob(root, pattern, *args, **kwargs):
        if root == operator_sessions:
            operator_scans.append(root)
            raise AssertionError("autonomy tests must not scan operator sessions")
        return original_rglob(root, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", guarded_rglob)
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)
    assert autonomy_modes.evaluate(mission, "merge", AGENT)["decision"] == "proceed"
    assert operator_scans == []

    # An intentional synthetic override still exercises the real quota reader.
    sessions = tmp_path / "synthetic-codex-sessions"
    sessions.mkdir()
    meter["payload"]["rate_limits"]["primary"]["used_percent"] = 25
    (sessions / "meter.jsonl").write_text(json.dumps(meter) + "\n", encoding="utf-8")
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(sessions))
    policy = tmp_path / "quota-budget.yaml"
    policy.write_text(
        "budgets:\n"
        "  - name: codex-monthly\n"
        "    agents: [Desktop_Agent2]\n"
        "    period: month\n"
        "    limit_usd: 1.0\n"
        "    action: block\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    status = budgets.budget_status(AGENT)["budgets"][0]["agents_status"][0]
    assert status["subscription_quota"]["availability"] == "available"
    assert autonomy_modes.evaluate(mission, "merge", AGENT)["decision"] == "proceed"
    _finish_run(AGENT, 1.5)
    denied = autonomy_modes.evaluate(mission, "merge", AGENT)
    assert denied["decision"] == "deny"
    assert "budget" in denied["reason"]
    assert operator_scans == []


def test_checkpoint_mode_pauses_only_declared_checkpoints():
    mission = _mission()
    autonomy_modes.set_mode(mission, "checkpoint", HUMAN)
    merge = autonomy_modes.evaluate(mission, "merge", AGENT)
    assert merge["decision"] == "pause_for_human"
    read = autonomy_modes.evaluate(mission, "read_status", AGENT)
    assert read["decision"] == "proceed"


def test_exception_mode_is_quiet_on_proceed_but_surfaces_pauses():
    mission = _mission()
    autonomy_modes.set_mode(mission, "exception", HUMAN)
    quiet = autonomy_modes.evaluate(mission, "read_status", AGENT)
    assert quiet["decision"] == "proceed"
    assert quiet["surfaced"] is False

    autonomy_modes.set_mode(mission, "action-review", HUMAN)
    loud = autonomy_modes.evaluate(mission, "merge", AGENT)
    assert loud["decision"] == "pause_for_human"
    assert loud["surfaced"] is True


# --- 2. org ceiling refuses a looser-than-allowed mission mode ----------------


def test_mission_mode_cannot_be_looser_than_org_ceiling(monkeypatch, tmp_path):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("autonomy_ceiling: checkpoint\n", encoding="utf-8")
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_path))
    mission = _mission()
    assert autonomy_modes.org_ceiling() == "checkpoint"
    with pytest.raises(ToolError, match="looser than the org ceiling"):
        autonomy_modes.set_mode(mission, "observe", HUMAN)
    # Equal-or-stricter than the ceiling is fine.
    autonomy_modes.set_mode(mission, "checkpoint", HUMAN)
    autonomy_modes.set_mode(mission, "action-review", HUMAN)


def test_default_ceiling_is_least_strict_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "missing.yaml"))
    assert autonomy_modes.org_ceiling() == "observe"
    mission = _mission()
    # Every mode is legal against the default ceiling.
    for mode in autonomy_modes.MODES:
        autonomy_modes.set_mode(mission, mode, HUMAN)


# --- 3. agent proposes; only a human applies (#1431) --------------------------


def test_agent_cannot_set_mode_directly():
    mission = _mission()
    with pytest.raises(ToolError, match="may only PROPOSE"):
        autonomy_modes.set_mode(mission, "observe", AGENT)


def test_agent_proposes_human_applies_with_truthful_attribution():
    mission = _mission()
    proposal = autonomy_modes.propose_mode(mission, "checkpoint", AGENT, reason="reduce noise")
    assert proposal["status"] == "pending"
    assert proposal["attribution"] == "proposal-pending"
    approval_id = proposal["approval_id"]

    # Not yet approved: apply refuses.
    with pytest.raises(ToolError, match="not 'approved'"):
        autonomy_modes.apply_proposal(approval_id)

    approval_resolve(approval_id, "approve", resolved_by=HUMAN)
    applied = autonomy_modes.apply_proposal(approval_id)
    assert applied["mode"] == "checkpoint"
    assert applied["attribution"] == "human-approved"

    state = autonomy_modes.get_mode(mission)
    latest_revision = state["revisions"][0]
    # The revision names the human who approved it, never the proposing agent.
    assert latest_revision["actor"] == HUMAN
    assert latest_revision["attribution"] == "human-approved"
    assert latest_revision["approval_id"] == approval_id


def test_agent_cannot_apply_even_its_own_approved_proposal_as_itself():
    # apply_proposal always attributes to the resolving human; an agent has no
    # path to call set_mode directly, and apply_proposal ignores who calls it.
    mission = _mission()
    proposal = autonomy_modes.propose_mode(mission, "action-review", AGENT)
    approval_resolve(proposal["approval_id"], "approve", resolved_by=HUMAN)
    with _as(AGENT):
        applied = autonomy_modes.apply_proposal(proposal["approval_id"])
    assert applied["attribution"] == "human-approved"
    state = autonomy_modes.get_mode(mission)
    assert state["revisions"][0]["actor"] == HUMAN


def test_applied_proposal_does_not_restore_pass_after_event_failure(monkeypatch):
    mission = _mission()
    proposal = autonomy_modes.propose_mode(mission, "checkpoint", AGENT)
    approval_resolve(proposal["approval_id"], "approve", resolved_by=HUMAN)

    def event_failure(*args, **kwargs):
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr(events, "record_event", event_failure)
    with pytest.raises(RuntimeError, match="event store unavailable"):
        autonomy_modes.apply_proposal(proposal["approval_id"])

    assert autonomy_modes.get_mode(mission)["mode"] == "checkpoint"
    from unified_agent.tools.approvals import get_approval

    assert get_approval(proposal["approval_id"])["status"] == "consumed"


# --- 4. revoked grant / tightened mode flips pending work ---------------------


def test_revoked_standing_grant_flips_pending_action_to_pause():
    mission = _mission()
    autonomy_modes.set_mode(mission, "checkpoint", HUMAN)
    autonomy_modes.grant_standing(mission, "merge", HUMAN, reason="known-safe pipeline")

    granted = autonomy_modes.evaluate(mission, "merge", AGENT)
    assert granted["decision"] == "proceed"
    assert granted["attribution"] == "standing-grant"
    pending_id = granted["pending_id"]

    result = autonomy_modes.revoke_grant(mission, "merge", HUMAN, reason="incident")
    flipped_ids = {row["pending_id"] for row in result["reevaluated_pending"]}
    assert pending_id in flipped_ids

    preview = autonomy_modes.preview(mission)
    [row] = [p for p in preview["pending_not_started"] if p["id"] == pending_id]
    assert row["decision"] == "pause_for_human"


def test_tightening_mode_flips_pending_action_to_pause():
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)
    observed = autonomy_modes.evaluate(mission, "merge", AGENT)
    assert observed["decision"] == "proceed"
    pending_id = observed["pending_id"]

    result = autonomy_modes.set_mode(
        mission, "action-review", HUMAN, reason="tighten after incident"
    )
    flipped_ids = {row["pending_id"] for row in result["reevaluated_pending"]}
    assert pending_id in flipped_ids

    preview = autonomy_modes.preview(mission)
    [row] = [p for p in preview["pending_not_started"] if p["id"] == pending_id]
    assert row["decision"] == "pause_for_human"


def test_started_pending_action_is_not_retroactively_contested():
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)
    observed = autonomy_modes.evaluate(mission, "merge", AGENT)
    autonomy_modes.mark_started(observed["pending_id"], actor=AGENT)

    autonomy_modes.set_mode(mission, "action-review", HUMAN)
    preview = autonomy_modes.preview(mission)
    assert preview["pending_not_started"] == []


def test_human_can_review_one_pending_action_without_standing_grant():
    mission = _mission()
    autonomy_modes.set_mode(mission, "checkpoint", HUMAN)
    pending = autonomy_modes.evaluate(mission, "deploy", AGENT)
    assert pending["decision"] == "pause_for_human"
    with pytest.raises(ToolError, match="not a human"):
        autonomy_modes.review_action(pending["pending_id"], AGENT)
    reviewed = autonomy_modes.review_action(pending["pending_id"], HUMAN)
    assert reviewed["decision"] == "proceed"
    assert reviewed["attribution"] == "human-approved"
    autonomy_modes.mark_started(pending["pending_id"], AGENT)
    assert autonomy_modes.evaluate(mission, "deploy", AGENT)["decision"] == "pause_for_human"


def test_review_waits_for_live_global_approval(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "deploy")
    mission = _mission()
    autonomy_modes.set_mode(mission, "checkpoint", HUMAN)
    pending = autonomy_modes.evaluate(mission, "deploy", AGENT)
    with pytest.raises(ToolError, match="global approval must be live"):
        autonomy_modes.review_action(pending["pending_id"], HUMAN)
    payload = {"mission_id": mission, "action_class": "deploy", "actor": AGENT}
    with pytest.raises(ApprovalPendingError) as excinfo:
        check_approval_gate("deploy", payload, requested_by=AGENT)
    approval_resolve(excinfo.value.approval_id, "approve", resolved_by=HUMAN)
    assert autonomy_modes.review_action(pending["pending_id"], HUMAN)["decision"] == "proceed"
    approval_resolve(excinfo.value.approval_id, "revoke", resolved_by=HUMAN)
    assert autonomy_modes.evaluate(mission, "deploy", AGENT)["decision"] == "pause_for_human"
    with pytest.raises(ApprovalPendingError) as next_request:
        check_approval_gate("deploy", payload, requested_by=AGENT)
    approval_resolve(next_request.value.approval_id, "approve", resolved_by=HUMAN)
    assert autonomy_modes.evaluate(mission, "deploy", AGENT)["decision"] == "pause_for_human"
    assert autonomy_modes.review_action(pending["pending_id"], HUMAN)["decision"] == "proceed"
    assert autonomy_modes.evaluate(mission, "deploy", AGENT)["decision"] == "proceed"


def test_preview_recomputes_live_gate_without_mutating_pending(monkeypatch):
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)
    pending = autonomy_modes.evaluate(mission, "merge", AGENT)
    monkeypatch.setattr(agent_lifecycle, "agent_state", lambda actor: {"state": "paused"})
    preview = autonomy_modes.preview(mission)
    assert preview["pending_not_started"][0]["decision"] == "deny"
    conn = storage.connect()
    try:
        stored = conn.execute(
            "SELECT decision FROM autonomy_pending_actions WHERE id=?", (pending["pending_id"],)
        ).fetchone()
        assert stored["decision"] == "proceed"
    finally:
        conn.close()


def test_grant_does_not_make_default_mode_explicit():
    mission = _mission()
    autonomy_modes.grant_standing(mission, "merge", HUMAN)
    state = autonomy_modes.get_mode(mission)
    assert state["mode"] == "action-review"
    assert state["explicit"] is False
    assert state["standing_grants"] == ["merge"]


def test_live_ceiling_blocks_stale_proceed_at_start(monkeypatch, tmp_path):
    policy = tmp_path / "policy.yaml"
    monkeypatch.setenv("UA_POLICY_FILE", str(policy))
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)
    pending = autonomy_modes.evaluate(mission, "deploy", AGENT)
    assert pending["decision"] == "proceed"
    policy.write_text("autonomy_ceiling: checkpoint\n", encoding="utf-8")
    assert autonomy_modes.evaluate(mission, "deploy", AGENT)["decision"] == "pause_for_human"
    with pytest.raises(ToolError, match="only a proceed decision"):
        autonomy_modes.mark_started(pending["pending_id"], AGENT)


def test_exception_surfaces_budget_warning(monkeypatch):
    mission = _mission()
    autonomy_modes.set_mode(mission, "exception", HUMAN)
    monkeypatch.setattr(
        budgets,
        "budget_status",
        lambda actor: {
            "budgets": [
                {
                    "name": "daily",
                    "action": "warn",
                    "agents_status": [{"agent": actor, "state": "warning"}],
                }
            ]
        },
    )
    result = autonomy_modes.evaluate(mission, "merge", AGENT)
    assert result["decision"] == "proceed"
    assert result["surfaced"] is True


def test_only_owner_can_start_a_proceed_decision():
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)
    pending = autonomy_modes.evaluate(mission, "merge", AGENT)
    with _as("Desktop_Agent4"):
        with pytest.raises(ToolError, match="different actor"):
            autonomy_modes.mark_started(pending["pending_id"], actor=AGENT)
    with _as("Desktop_Agent4"):
        with pytest.raises(ToolError, match="only the owning actor"):
            autonomy_modes.mark_started(pending["pending_id"])

    autonomy_modes.set_mode(mission, "action-review", HUMAN)
    with pytest.raises(ToolError, match="only a proceed decision"):
        autonomy_modes.mark_started(pending["pending_id"], actor=AGENT)


def test_mode_change_and_pending_reevaluation_are_atomic(monkeypatch):
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)
    autonomy_modes.evaluate(mission, "merge", AGENT)
    before = autonomy_modes.get_mode(mission)

    def broken_decide(*args, **kwargs):
        raise RuntimeError("reevaluation failed")

    monkeypatch.setattr(autonomy_modes, "_decide", broken_decide)
    with pytest.raises(RuntimeError, match="reevaluation failed"):
        autonomy_modes.set_mode(mission, "action-review", HUMAN)
    after = autonomy_modes.get_mode(mission)
    assert after["mode"] == before["mode"]
    assert after["revisions"] == before["revisions"]


# --- 5. attribution: human-approved vs policy-authorized ----------------------


def test_attribution_policy_authorized_by_default():
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)
    decision = autonomy_modes.evaluate(mission, "merge", AGENT)
    assert decision["attribution"] == "policy-authorized"


def test_attribution_human_approved_when_an_explicit_pass_authorizes_it(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "deploy")
    mission = _mission()
    autonomy_modes.set_mode(mission, "action-review", HUMAN)
    payload = {"mission_id": mission, "action_class": "deploy", "actor": AGENT}
    with pytest.raises(ApprovalPendingError) as excinfo:
        check_approval_gate("deploy", payload, requested_by=AGENT)
    approval_id = excinfo.value.approval_id
    approval_resolve(approval_id, "approve", resolved_by=HUMAN)

    decision = autonomy_modes.evaluate(mission, "deploy", AGENT)
    assert decision["decision"] == "pause_for_human"
    assert "mission review required" in decision["reason"]

    autonomy_modes.grant_standing(mission, "deploy", HUMAN)
    decision = autonomy_modes.evaluate(mission, "deploy", AGENT)
    assert decision["decision"] == "proceed"
    assert decision["attribution"] == "human-approved"


def test_missing_global_approval_pass_pauses_even_in_observe_mode(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "deploy")
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)
    decision = autonomy_modes.evaluate(mission, "deploy", AGENT)
    assert decision["decision"] == "pause_for_human"
    assert "no approval pass" in decision["reason"]


def test_explicit_gate_applies_to_read_labeled_action(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "read_status")
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)
    decision = autonomy_modes.evaluate(mission, "read_status", AGENT)
    assert decision["decision"] == "pause_for_human"
    assert "no approval pass" in decision["reason"]


def test_denial_overrides_older_live_approval(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "deploy")
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)
    payload = {"mission_id": mission, "action_class": "deploy", "actor": AGENT}
    with pytest.raises(ApprovalPendingError) as excinfo:
        check_approval_gate("deploy", payload, requested_by=AGENT)
    approval_resolve(excinfo.value.approval_id, "approve", resolved_by=HUMAN)
    conn = storage.connect()
    try:
        now = storage.now()
        conn.execute(
            "INSERT INTO approvals (tool, payload, status, reason, created_at, updated_at) "
            "SELECT tool, payload, 'denied', 'later denial', ?, ? FROM approvals WHERE id=?",
            (now, now, excinfo.value.approval_id),
        )
        conn.commit()
    finally:
        conn.close()
    result = autonomy_modes.evaluate(mission, "deploy", AGENT)
    assert result["decision"] == "deny"
    assert "later denial" in result["reason"]


AUTONOMY_TABLES = {
    "autonomy_mission_mode",
    "autonomy_mode_revisions",
    "autonomy_pending_actions",
    "autonomy_standing_grants",
    "autonomy_action_reviews",
}


def _table_names() -> set[str]:
    conn = storage.connect()
    try:
        return {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def _traced_connect(monkeypatch) -> list[str]:
    """Record every SQL statement any storage.connect() connection runs."""
    statements: list[str] = []
    real_connect = storage.connect

    def connect():
        conn = real_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(storage, "connect", connect)
    return statements


def _fail_ensure_schema(monkeypatch) -> None:
    def boom(conn):  # noqa: ARG001
        raise AssertionError("autonomy ensure_schema must not run on the request path")

    monkeypatch.setattr(autonomy_modes, "ensure_schema", boom)


def _is_ddl(statement: str) -> bool:
    return statement.lstrip().upper().startswith(("CREATE", "ALTER", "DROP"))


def test_init_db_creates_autonomy_tables():
    assert AUTONOMY_TABLES <= _table_names()
    conn = storage.connect()
    try:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(autonomy_action_reviews)")
        }
    finally:
        conn.close()
    assert "approval_id" in columns


def test_init_db_autonomy_schema_is_idempotent():
    storage.init_db()
    storage.init_db()
    assert AUTONOMY_TABLES <= _table_names()


def test_connect_issues_no_ddl_and_never_calls_ensure_schema(monkeypatch):
    statements = _traced_connect(monkeypatch)
    _fail_ensure_schema(monkeypatch)
    conn = autonomy_modes._connect()
    try:
        conn.execute("SELECT 1 FROM autonomy_mission_mode LIMIT 1").fetchall()
    finally:
        conn.close()
    assert not any(_is_ddl(s) for s in statements), statements


def test_autonomy_get_and_preview_are_read_only(monkeypatch):
    mission = _mission()
    autonomy_modes.set_mode(mission, "checkpoint", HUMAN)
    statements = _traced_connect(monkeypatch)
    _fail_ensure_schema(monkeypatch)
    assert autonomy_modes.get_mode(mission)["explicit"] is True
    assert autonomy_modes.preview(mission)["mission_id"] == mission
    assert statements
    assert not any(_is_ddl(s) for s in statements), statements
    assert not any(
        s.lstrip().upper().startswith(("COMMIT", "INSERT", "UPDATE", "DELETE")) for s in statements
    ), statements


def test_evaluate_and_mutations_run_no_ddl(monkeypatch):
    mission = _mission()
    statements = _traced_connect(monkeypatch)
    _fail_ensure_schema(monkeypatch)
    autonomy_modes.set_mode(mission, "action-review", HUMAN)
    autonomy_modes.grant_standing(mission, "deploy", HUMAN)
    pending = autonomy_modes.evaluate(mission, "merge", AGENT)
    assert pending["decision"] == "pause_for_human"
    autonomy_modes.review_action(pending["pending_id"], HUMAN)
    autonomy_modes.mark_started(pending["pending_id"], AGENT)
    autonomy_modes.revoke_grant(mission, "deploy", HUMAN)
    assert not any(_is_ddl(s) for s in statements), statements


def _seed_legacy_grants(mission: int, grants: list[str]) -> None:
    conn = storage.connect()
    try:
        autonomy_modes.ensure_schema(conn)
        conn.execute(
            "INSERT INTO autonomy_mission_mode "
            "(mission_id, mode, standing_grants, updated_at, updated_by) VALUES (?, ?, ?, ?, ?)",
            (mission, "checkpoint", json.dumps(grants), storage.now(), HUMAN),
        )
        conn.commit()
    finally:
        conn.close()


def _grant_rows(mission: int) -> list[str]:
    conn = storage.connect()
    try:
        return [
            row["action_class"]
            for row in conn.execute(
                "SELECT action_class FROM autonomy_standing_grants "
                "WHERE mission_id=? ORDER BY action_class",
                (mission,),
            )
        ]
    finally:
        conn.close()


def test_init_db_upgrades_legacy_review_and_action_context_schema():
    conn = storage.connect()
    try:
        conn.execute("DROP TABLE autonomy_action_reviews")
        conn.execute(
            "CREATE TABLE autonomy_action_reviews (pending_id INTEGER PRIMARY KEY, "
            "reviewed_by TEXT NOT NULL, reviewed_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO autonomy_action_reviews VALUES (7, ?, ?)", (HUMAN, storage.now()))
        conn.execute("DROP TABLE autonomy_pending_actions")
        conn.execute(
            "CREATE TABLE autonomy_pending_actions (id INTEGER PRIMARY KEY, "
            "mission_id INTEGER NOT NULL, action_class TEXT NOT NULL, actor TEXT NOT NULL, "
            "decision TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', attribution TEXT NOT NULL, "
            "started INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO autonomy_pending_actions VALUES (7, 1, 'merge', ?, 'proceed', '', "
            "'human-approved', 0, ?, ?)",
            (AGENT, storage.now(), storage.now()),
        )
        conn.execute(
            "CREATE UNIQUE INDEX uq_autonomy_pending_actions_open "
            "ON autonomy_pending_actions(mission_id, action_class, actor) WHERE started=0"
        )
        conn.commit()
    finally:
        conn.close()
    storage.init_db()
    conn = storage.connect()
    try:
        review = dict(
            conn.execute("SELECT * FROM autonomy_action_reviews WHERE pending_id=7").fetchone()
        )
        assert review["reviewed_by"] == HUMAN
        assert review["approval_id"] is None
        pending = dict(conn.execute("SELECT * FROM autonomy_pending_actions WHERE id=7").fetchone())
        assert pending["action_context"] == "null"
        assert [
            row["name"]
            for row in conn.execute("PRAGMA index_info(uq_autonomy_pending_actions_open)")
        ] == ["mission_id", "action_class", "actor", "action_context"]
        with pytest.raises(sqlite3.IntegrityError, match="autonomy action context is immutable"):
            conn.execute("UPDATE autonomy_pending_actions SET action_context='{}' WHERE id=7")
    finally:
        conn.close()


def test_connect_does_not_commit_a_callers_transaction(monkeypatch):
    conn = storage.connect()
    conn.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(storage, "connect", lambda: conn)
    try:
        assert autonomy_modes._connect() is conn
        assert conn.in_transaction
    finally:
        conn.rollback()
        conn.close()


def test_failed_legacy_grant_migration_rolls_back_and_can_retry(monkeypatch):
    mission = _mission()
    _seed_legacy_grants(mission, ["merge", "deploy"])
    real_backfill = autonomy_modes._backfill_standing_grants

    def interrupted(conn):
        real_backfill(conn)
        raise RuntimeError("interrupted migration")

    monkeypatch.setattr(autonomy_modes, "_backfill_standing_grants", interrupted)
    with pytest.raises(RuntimeError, match="interrupted migration"):
        storage.init_db()
    assert _grant_rows(mission) == []
    conn = storage.connect()
    try:
        assert json.loads(
            conn.execute(
                "SELECT standing_grants FROM autonomy_mission_mode WHERE mission_id=?", (mission,)
            ).fetchone()["standing_grants"]
        ) == ["merge", "deploy"]
    finally:
        conn.close()
    monkeypatch.setattr(autonomy_modes, "_backfill_standing_grants", real_backfill)
    storage.init_db()
    assert _grant_rows(mission) == ["deploy", "merge"]


def test_legacy_json_grants_migrate_exactly_once_across_init_db_runs():
    mission = _mission()
    _seed_legacy_grants(mission, ["merge", "deploy"])
    assert _grant_rows(mission) == []

    storage.init_db()
    assert _grant_rows(mission) == ["deploy", "merge"]
    conn = storage.connect()
    try:
        raw = conn.execute(
            "SELECT standing_grants FROM autonomy_mission_mode WHERE mission_id=?", (mission,)
        ).fetchone()["standing_grants"]
        conn.execute(
            "DELETE FROM autonomy_standing_grants WHERE mission_id=? AND action_class='merge'",
            (mission,),
        )
        conn.commit()
    finally:
        conn.close()
    assert raw == "[]"

    storage.init_db()
    # A revoked grant is not resurrected: the JSON source was cleared the first time.
    assert _grant_rows(mission) == ["deploy"]
    assert autonomy_modes.get_mode(mission)["standing_grants"] == ["deploy"]


def test_preview_does_not_migrate_legacy_review_schema():
    mission = _mission()
    conn = storage.connect()
    try:
        conn.execute("DROP TABLE autonomy_action_reviews")
        conn.execute(
            "CREATE TABLE autonomy_action_reviews (pending_id INTEGER PRIMARY KEY, "
            "reviewed_by TEXT NOT NULL, reviewed_at TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()
    assert autonomy_modes.preview(mission)["pending_not_started"] == []
    conn = storage.connect()
    try:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(autonomy_action_reviews)")
        }
        assert "approval_id" not in columns
    finally:
        conn.close()


def test_preview_honors_legacy_ungated_review_without_migration():
    mission = _mission()
    autonomy_modes.set_mode(mission, "checkpoint", HUMAN)
    pending = autonomy_modes.evaluate(mission, "deploy", AGENT)
    autonomy_modes.review_action(pending["pending_id"], HUMAN)
    conn = storage.connect()
    try:
        conn.execute("DROP TABLE autonomy_action_reviews")
        conn.execute(
            "CREATE TABLE autonomy_action_reviews (pending_id INTEGER PRIMARY KEY, "
            "reviewed_by TEXT NOT NULL, reviewed_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO autonomy_action_reviews VALUES (?, ?, ?)",
            (pending["pending_id"], HUMAN, storage.now()),
        )
        conn.commit()
    finally:
        conn.close()
    preview = autonomy_modes.preview(mission)
    assert preview["pending_not_started"][0]["decision"] == "proceed"
    conn = storage.connect()
    try:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(autonomy_action_reviews)")
        }
        assert "approval_id" not in columns
    finally:
        conn.close()


def test_unreadable_lifecycle_and_budget_state_fail_closed(monkeypatch):
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)

    def unavailable(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(agent_lifecycle, "agent_state", unavailable)
    assert autonomy_modes.evaluate(mission, "merge", AGENT)["decision"] == "deny"
    monkeypatch.setattr(agent_lifecycle, "agent_state", lambda actor: {"state": "active"})
    monkeypatch.setattr(
        budgets, "budget_status", lambda actor: {"error": "bad policy", "budgets": []}
    )
    assert autonomy_modes.evaluate(mission, "merge", AGENT)["decision"] == "deny"
    monkeypatch.setattr(budgets, "budget_status", lambda actor: {"budgets": "invalid"})
    assert autonomy_modes.evaluate(mission, "merge", AGENT)["decision"] == "deny"


# --- 6. existing explicit gates stay enforced in the loosest mode -------------


def test_explicit_denial_is_enforced_even_in_observe_mode(monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "deploy")
    mission = _mission()
    autonomy_modes.set_mode(mission, "observe", HUMAN)  # loosest mode
    payload = {"mission_id": mission, "action_class": "deploy", "actor": AGENT}
    with pytest.raises(ApprovalPendingError) as excinfo:
        check_approval_gate("deploy", payload, requested_by=AGENT)
    approval_resolve(excinfo.value.approval_id, "deny", resolved_by=HUMAN, reason="not now")

    decision = autonomy_modes.evaluate(mission, "deploy", AGENT)
    assert decision["decision"] == "deny"
    assert "enforced in every mode" in decision["reason"]


def test_agent_lifecycle_stop_denies_in_every_mode():
    mission = _mission()
    with _as(HUMAN):
        agent_lifecycle.agent_pause(AGENT, "incident response")
    for mode in autonomy_modes.MODES:
        autonomy_modes.set_mode(mission, mode, HUMAN)
        decision = autonomy_modes.evaluate(mission, "merge", AGENT)
        assert decision["decision"] == "deny", mode
        assert "#1351" in decision["reason"]


# --- 7. budget hard stop denies in every mode ---------------------------------


def _finish_run(agent: str, cost_usd: float) -> None:
    tid = task_add("t", lane="dispatch-ready")["id"]
    with _as(agent):
        task_claim(tid, agent, expected_lane="dispatch-ready")
        task_update(
            tid,
            status="done",
            evidence_ref="https://example/pr/1",
            run_usage={"cost_usd": cost_usd, "provider": "openai", "billing": "api"},
        )


def test_budget_hard_stop_denies_in_every_mode(monkeypatch, tmp_path):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        "budgets:\n"
        "  - name: codex-monthly\n"
        "    agents: [Desktop_Agent2]\n"
        "    period: month\n"
        "    limit_usd: 1.0\n"
        "    action: block\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UA_POLICY_FILE", str(policy_path))
    _finish_run(AGENT, 1.5)
    status = budgets.budget_status(AGENT)
    assert status["budgets"][0]["agents_status"][0]["state"] == "exhausted"

    mission = _mission()
    for mode in autonomy_modes.MODES:
        autonomy_modes.set_mode(mission, mode, HUMAN)
        decision = autonomy_modes.evaluate(mission, "merge", AGENT)
        assert decision["decision"] == "deny", mode
        assert "budget" in decision["reason"]


# --- goal_manage wiring --------------------------------------------------------


def test_goal_manage_exposes_autonomy_actions():
    mission = _mission()
    with _as(HUMAN):
        set_result = goal_manage("autonomy_set", {"mission_id": mission, "mode": "checkpoint"})
    assert set_result["mode"] == "checkpoint"
    got = goal_manage("autonomy_get", {"mission_id": mission})
    assert got["mode"] == "checkpoint"
    preview = goal_manage("autonomy_preview", {"mission_id": mission})
    assert preview["mission_id"] == mission
    with _as(AGENT):
        evaluated = goal_manage(
            "autonomy_evaluate", {"mission_id": mission, "action_class": "read_status"}
        )
    assert evaluated["decision"] == "proceed"
    with _as(AGENT):
        started = goal_manage("autonomy_mark_started", {"pending_id": evaluated["pending_id"]})
    assert started["started"] == 1
    with _as(AGENT):
        with pytest.raises(ToolError, match="may only PROPOSE"):
            goal_manage("autonomy_set", {"mission_id": mission, "mode": "observe"})


def test_goal_manage_ignores_a_caller_supplied_human_actor(monkeypatch):
    """A verified agent cannot claim to be a human through the body's actor."""
    mission = _mission()
    with _as(AGENT):
        with pytest.raises(ToolError, match="may only PROPOSE"):
            goal_manage("autonomy_set", {"mission_id": mission, "mode": "observe", "actor": HUMAN})
    monkeypatch.setattr(config, "enforce_auth", lambda: True)
    with pytest.raises(ToolError, match="verified identity"):
        goal_manage("autonomy_set", {"mission_id": mission, "mode": "observe", "actor": HUMAN})


def test_direct_autonomy_api_rejects_actor_spoofing():
    mission = _mission()
    with _as(AGENT):
        with pytest.raises(ToolError, match="different actor"):
            autonomy_modes.set_mode(mission, "observe", HUMAN)
        with pytest.raises(ToolError, match="different actor"):
            autonomy_modes.grant_standing(mission, "merge", HUMAN)


def test_registered_autonomy_reads_work_in_read_only_session(monkeypatch):
    from unified_agent import server

    mission = _mission()
    monkeypatch.setenv("UA_AGENT_ACCESS_READ_ONLY", "1")
    with _as(AGENT):
        assert (
            server.REGISTERED_TOOLS["goal_manage"]("autonomy_get", {"mission_id": mission})[
                "mission_id"
            ]
            == mission
        )
        assert (
            server.REGISTERED_TOOLS["goal_manage"]("autonomy_preview", {"mission_id": mission})[
                "mission_id"
            ]
            == mission
        )


def test_unknown_mission_is_refused():
    with pytest.raises(ToolError, match="unknown mission"):
        autonomy_modes.get_mode(999999)
    with pytest.raises(ToolError, match="unknown mission"):
        autonomy_modes.evaluate(999999, "merge", AGENT)
