"""Exact execution contexts cannot share mission reviews or approval passes."""

import json
import sqlite3
from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import autonomy_modes as modes
from unified_agent import config, storage
from unified_agent.goals import goal_add
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_resolve,
    check_approval_gate,
)

AGENT = "Desktop_Agent2"
HUMAN = "Desktop_Person"


@contextmanager
def identity(label):
    token = config.agent_identity_var.set(label)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture
def mission(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv("UA_POLICY_FILE", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("UA_CODEX_SESSIONS_ROOT", str(tmp_path / "empty-codex-sessions"))
    monkeypatch.setenv("UA_APPROVAL_BASELINE", "0")
    monkeypatch.delenv("UA_APPROVAL_TOOLS", raising=False)
    storage.init_db()
    with identity(HUMAN):
        result = goal_add("Exact merge boundary")
    return result["goal"]["id"]


def context(task=1, sha="a" * 40):
    return {"task_id": task, "repo": "/tmp/review-repo", "branch": "codex/fix", "head": sha}


def evaluate(mission, value):
    with identity(AGENT):
        return modes.evaluate(mission, "merge", AGENT, action_context=value)


def review(pending):
    with identity(HUMAN):
        return modes.review_action(pending, HUMAN)


def test_distinct_tasks_cannot_share_review_and_canonical_retry_can(mission):
    first = evaluate(mission, context(1))
    second = evaluate(mission, context(2))
    assert first["pending_id"] != second["pending_id"]
    review(first["pending_id"])
    assert evaluate(mission, dict(reversed(list(context(1).items()))))["decision"] == "proceed"
    assert evaluate(mission, context(2))["decision"] == "pause_for_human"
    with identity(AGENT):
        with pytest.raises(ToolError):
            modes.mark_started(second["pending_id"], AGENT, action_context=context(2))
        started = modes.mark_started(first["pending_id"], AGENT, action_context=context(1))
    assert started["action_context"] == context(1)
    assert evaluate(mission, context(1))["pending_id"] != first["pending_id"]


@pytest.mark.parametrize("different", [None, {}, context(2), context(1, "b" * 40)])
def test_mark_started_requires_exact_context_even_after_human_review(mission, different):
    pending = evaluate(mission, context())
    review(pending["pending_id"])
    with identity(AGENT):
        with pytest.raises(ToolError, match="context"):
            modes.mark_started(pending["pending_id"], AGENT, action_context=different)
        assert modes.mark_started(pending["pending_id"], AGENT, action_context=context())["started"]


def approve(mission, value, decision="approve"):
    payload = {"mission_id": mission, "action_class": "merge", "actor": AGENT}
    if value is not None:
        payload["action_context"] = value
    with identity(AGENT):
        with pytest.raises(ApprovalPendingError) as pending:
            check_approval_gate("merge", payload, requested_by=AGENT, force=True)
    with identity(HUMAN):
        approval_resolve(pending.value.approval_id, decision, resolved_by=HUMAN)
    return pending.value.approval_id


def test_explicit_approval_and_review_are_bound_to_context_and_revocation(mission, monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "merge")
    first = evaluate(mission, context(1))
    second = evaluate(mission, context(2))
    approval = approve(mission, context(1))
    review(first["pending_id"])
    assert evaluate(mission, context(1))["decision"] == "proceed"
    assert evaluate(mission, context(2))["decision"] == "pause_for_human"
    with identity(HUMAN):
        with pytest.raises(ToolError, match="global approval"):
            modes.review_action(second["pending_id"], HUMAN)
    conn = storage.connect()
    try:
        conn.execute("UPDATE approvals SET status='denied' WHERE id=?", (approval,))
        conn.commit()
    finally:
        conn.close()
    assert modes.preview(mission)["pending_not_started"][0]["decision"] == "deny"
    with identity(AGENT):
        with pytest.raises(ToolError):
            modes.mark_started(first["pending_id"], AGENT, action_context=context(1))


def test_tightening_rechecks_each_stored_context(mission, monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "merge")
    approve(mission, context(1))
    approve(mission, context(2), decision="deny")
    with identity(HUMAN):
        modes.set_mode(mission, "observe", HUMAN)
    first = evaluate(mission, context(1))
    second = evaluate(mission, context(2))
    assert first["decision"] == "proceed" and second["decision"] == "deny"
    with identity(HUMAN):
        modes.set_mode(mission, "checkpoint", HUMAN)
    decisions = {row["id"]: row for row in modes.preview(mission)["pending_not_started"]}
    assert decisions[first["pending_id"]]["decision"] == "pause_for_human"
    assert decisions[second["pending_id"]]["decision"] == "deny"


@pytest.mark.parametrize(
    "invalid", [[], "text", {1: "key"}, {"x": float("nan")}, {"x": object()}, {"x": "x" * 20000}]
)
def test_invalid_context_is_refused_before_creating_pending(mission, invalid):
    with pytest.raises(ToolError, match="context"):
        evaluate(mission, invalid)
    assert modes.preview(mission)["pending_not_started"] == []


def test_context_metadata_never_grants_authority(mission):
    assert (
        evaluate(mission, {"actor": HUMAN, "mode": "observe", "grant": True})["decision"]
        == "pause_for_human"
    )


def test_stored_context_is_immutable(mission):
    pending = evaluate(mission, context())
    conn = storage.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE autonomy_pending_actions SET action_context=? WHERE id=?",
                (json.dumps(context(2)), pending["pending_id"]),
            )
    finally:
        conn.close()


def test_legacy_pending_and_review_survive_idempotent_context_migration(mission):
    conn = storage.connect()
    try:
        conn.executescript("""
        DROP TABLE IF EXISTS autonomy_pending_actions;
        DROP TABLE IF EXISTS autonomy_action_reviews;
        CREATE TABLE autonomy_pending_actions (
          id INTEGER PRIMARY KEY, mission_id INTEGER NOT NULL, action_class TEXT NOT NULL,
          actor TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
          attribution TEXT NOT NULL, started INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX uq_autonomy_pending_actions_open
          ON autonomy_pending_actions(mission_id,action_class,actor) WHERE started=0;
        CREATE TABLE autonomy_action_reviews (
          pending_id INTEGER PRIMARY KEY, reviewed_by TEXT NOT NULL, reviewed_at TEXT NOT NULL,
          approval_id INTEGER
        );
        """)
        conn.execute(
            "INSERT INTO autonomy_pending_actions VALUES (77,?,'merge',?,'proceed','','human-approved',0,?,?)",
            (mission, AGENT, storage.now(), storage.now()),
        )
        conn.execute(
            "INSERT INTO autonomy_action_reviews VALUES (77,?,?,NULL)", (HUMAN, storage.now())
        )
        conn.commit()
    finally:
        conn.close()
    storage.init_db()
    storage.init_db()
    with identity(AGENT):
        legacy = modes.evaluate(mission, "merge", AGENT)
        assert legacy["pending_id"] == 77 and legacy["decision"] == "proceed"
        assert "action_context" not in legacy
        assert modes.mark_started(77, AGENT)["started"] == 1
    assert evaluate(mission, context())["decision"] == "pause_for_human"


def test_contextual_preview_never_migrates_or_creates_pending(mission):
    conn = storage.connect()
    try:
        before = list(conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
    finally:
        conn.close()
    with identity(AGENT):
        result = modes.preview_action(mission, "merge", AGENT, action_context=context())
    assert result["decision"] == "pause_for_human" and result["pending_id"] is None
    conn = storage.connect()
    try:
        after = list(conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name"))
        assert [tuple(row) for row in before] == [tuple(row) for row in after]
    finally:
        conn.close()


def test_preview_uses_review_only_for_identical_context_without_starting(mission):
    pending = evaluate(mission, context(1))
    review(pending["pending_id"])
    with identity(AGENT):
        matching = modes.preview_action(mission, "merge", AGENT, action_context=context(1))
        other = modes.preview_action(mission, "merge", AGENT, action_context=context(2))
    assert matching["decision"] == "proceed" and matching["pending_id"] == pending["pending_id"]
    assert other["decision"] == "pause_for_human" and other["pending_id"] is None
    conn = storage.connect()
    try:
        rows = conn.execute("SELECT started FROM autonomy_pending_actions").fetchall()
        assert len(rows) == 1 and rows[0]["started"] == 0
    finally:
        conn.close()


def test_rejected_start_persists_current_denial_without_consuming_action(mission):
    from unified_agent import agent_lifecycle

    pending = evaluate(mission, context())
    review(pending["pending_id"])
    with identity(HUMAN):
        agent_lifecycle.agent_pause(AGENT, "revoked before merge")
    with identity(AGENT):
        with pytest.raises(ToolError):
            modes.mark_started(pending["pending_id"], AGENT, action_context=context())
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT decision,reason,started,action_context FROM autonomy_pending_actions WHERE id=?",
            (pending["pending_id"],),
        ).fetchone()
        assert row["decision"] == "deny"
        assert "paused" in row["reason"]
        assert row["started"] == 0
        assert json.loads(row["action_context"]) == context()
    finally:
        conn.close()


def test_started_action_is_not_redecided_when_caller_retries_after_pause(mission):
    from unified_agent import agent_lifecycle

    pending = evaluate(mission, context())
    review(pending["pending_id"])
    with identity(AGENT):
        original = modes.mark_started(pending["pending_id"], AGENT, action_context=context())
    with identity(HUMAN):
        agent_lifecycle.agent_pause(AGENT, "paused after merge started")
    with identity(AGENT):
        with pytest.raises(ToolError, match="already started"):
            modes.mark_started(pending["pending_id"], AGENT, action_context=context())
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT * FROM autonomy_pending_actions WHERE id=?", (pending["pending_id"],)
        ).fetchone()
        assert row["decision"] == original["decision"] == "proceed"
        assert row["reason"] == original["reason"]
        assert row["started"] == 1
    finally:
        conn.close()


def test_start_rechecks_takeover_on_owning_locked_connection(mission):
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id,title,status,priority,current_run_id,created_at,updated_at) "
            "VALUES (1,'Merge source','doing','med','sp-reviewed',?,?)",
            (storage.now(), storage.now()),
        )
        conn.commit()
        outer_run = conn.execute("SELECT current_run_id FROM tasks WHERE id=1").fetchone()[0]
    finally:
        conn.close()
    value = {**context(), "current_run_id": outer_run}
    pending = evaluate(mission, value)
    review(pending["pending_id"])
    # A successor commits after the outer boundary snapshot but before start
    # obtains its lock. Checking the old snapshot alone would start this action.
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET current_run_id='sp-successor' WHERE id=1")
        conn.commit()
    finally:
        conn.close()
    checked = []

    def current_admission(owning):
        assert owning.in_transaction
        competing = storage.connect()
        try:
            competing.execute("PRAGMA busy_timeout=0")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("BEGIN IMMEDIATE")
        finally:
            competing.close()
        actual = owning.execute("SELECT current_run_id FROM tasks WHERE id=1").fetchone()[0]
        checked.append(actual)
        if actual != outer_run:
            raise ToolError("merge task run changed before start")

    with identity(AGENT), pytest.raises(ToolError, match="run changed"):
        modes.mark_started(
            pending["pending_id"], AGENT, action_context=value, _admission_check=current_admission
        )
    assert checked == ["sp-successor"]
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT decision,reason,started FROM autonomy_pending_actions WHERE id=?",
            (pending["pending_id"],),
        ).fetchone()
        assert row["decision"] == "deny" and row["started"] == 0
        assert "run changed" in row["reason"]
    finally:
        conn.close()


def test_internal_admission_callback_cannot_replace_mission_review(mission):
    pending = evaluate(mission, context())
    checked = []
    with identity(AGENT), pytest.raises(ToolError, match="only a proceed"):
        modes.mark_started(
            pending["pending_id"],
            AGENT,
            action_context=context(),
            _admission_check=lambda conn: checked.append(conn),
        )
    assert checked == []
