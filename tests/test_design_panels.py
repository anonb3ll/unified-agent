"""Independent-first design panels (task #2399).

Design questions only; every seat gets the same neutral dispatch; answers are
final and sealed until all are revealed together (last answer or deadline); the
outcome is a decision with reasons, never a tally.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, design_panels, storage
from unified_agent.tool_errors import PermissionDeniedError
from unified_agent.tools.graph import coordination_manage
from unified_agent.tools.tasks import task_add

OPERATOR = "Desktop_Operator"
OPENER = "Desktop_Agent2"
SEAT_A = "Desktop_Agent4"
SEAT_B = "Desktop_agent3"
OUTSIDER = "Desktop_agent10"
QUESTION = "Should run receipts live in the event log or in their own table?"


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", OPERATOR)


def _open(**kw) -> dict:
    kw.setdefault("notify", False)
    with _as(OPENER):
        return design_panels.panel_open(QUESTION, [SEAT_A, SEAT_B], **kw)


def _inbox(label: str) -> list[dict]:
    conn = storage.connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT message_id, sender, recipient FROM message_index WHERE recipient = ?",
                (label,),
            )
        ]
    finally:
        conn.close()


def test_open_refuses_code_review_and_leading_questions():
    with _as(OPENER):
        for question in (
            "Can you review PR #1483 before merge?",
            "Please approve the diff on this branch",
            f"Is {'c' * 40} safe?",
        ):
            with pytest.raises(ToolError, match="design questions only"):
                design_panels.panel_open(question, [SEAT_A, SEAT_B], notify=False)
        with pytest.raises(ToolError, match="without your own answer"):
            design_panels.panel_open(
                "I think receipts belong in the log; agree?", [SEAT_A, SEAT_B], notify=False
            )
        with pytest.raises(ToolError, match="does not sit"):
            design_panels.panel_open(QUESTION, [OPENER, SEAT_A], notify=False)
        with pytest.raises(ToolError, match="2-7 seats"):
            design_panels.panel_open(QUESTION, [SEAT_A], notify=False)
        with pytest.raises(ToolError, match="listed twice"):
            design_panels.panel_open(QUESTION, [SEAT_A, SEAT_A.lower()], notify=False)


def test_every_seat_gets_the_same_dispatch():
    with _as(OPENER):
        out = design_panels.panel_open(QUESTION, [SEAT_A, SEAT_B], context="Ledger is append-only.")
    assert set(out["notified"]) == {SEAT_A, SEAT_B}
    assert all(not mid.startswith("failed") for mid in out["notified"].values())
    body = design_panels.dispatch_body(out["panel_id"], QUESTION, "Ledger is append-only.", "x")
    assert "Question:" in body and "Context:" in body and OPENER not in body
    assert len(_inbox(SEAT_A)) == 1 and len(_inbox(SEAT_B)) == 1


def test_answers_sealed_until_last_answer_reveals_together():
    pid = _open()["panel_id"]
    with _as(SEAT_A):
        mine = design_panels.panel_answer(pid, "own table", confidence="high")
    assert mine["sealed"] is True
    seat_a = next(s for s in mine["seats"] if s["seat"] == SEAT_A)
    assert seat_a["answer"] == "own table"  # a seat sees its own answer
    with _as(SEAT_B):
        before = design_panels.panel_get(pid)
    assert all("answer" not in s for s in before["seats"])
    assert next(s for s in before["seats"] if s["seat"] == SEAT_A)["answered"] is True
    with _as(SEAT_A), pytest.raises(ToolError, match="answers are final"):
        design_panels.panel_answer(pid, "changed my mind")
    with _as(OUTSIDER), pytest.raises(PermissionDeniedError, match="no seat"):
        design_panels.panel_answer(pid, "me too")
    with _as(SEAT_B):
        out = design_panels.panel_answer(pid, "event log")
    assert out["status"] == "revealed" and out["sealed"] is False
    assert {s["seat"]: s["answer"] for s in out["seats"]} == {
        SEAT_A: "own table",
        SEAT_B: "event log",
    }
    with _as(OUTSIDER), pytest.raises(ToolError, match="answers are closed"):
        design_panels.panel_answer(pid, "late")


def test_deadline_reveal_by_opener_or_sweep():
    pid = _open(deadline_hours=1)["panel_id"]
    with _as(SEAT_A):
        design_panels.panel_answer(pid, "own table")
    with _as(OPENER), pytest.raises(ToolError, match="revealing early"):
        design_panels.panel_reveal(pid)
    with _as(SEAT_A), pytest.raises(PermissionDeniedError):
        design_panels.panel_reveal(pid)
    later = datetime.now(UTC) + timedelta(hours=2)
    assert design_panels.reveal_due(now=later) == {"revealed": [pid]}
    assert design_panels.reveal_due(now=later) == {"revealed": []}
    with _as(OPENER):
        view = design_panels.panel_get(pid)
    assert {s["seat"]: s["answer"] for s in view["seats"]} == {
        SEAT_A: "own table",
        SEAT_B: "no answer",
    }


def test_decision_not_tally_and_task_note():
    tid = task_add("receipts design", lane="dispatch-ready")["id"]
    pid = _open(task_id=tid)["panel_id"]
    with _as(OPENER), pytest.raises(ToolError, match="follows the reveal"):
        design_panels.panel_decide(pid, "own table", "why")
    for seat, answer in ((SEAT_A, "own table"), (SEAT_B, "event log")):
        with _as(seat):
            design_panels.panel_answer(pid, answer)
    with _as(SEAT_A), pytest.raises(PermissionDeniedError):
        design_panels.panel_decide(pid, "own table", "why")
    with _as(OPENER):
        with pytest.raises(ToolError, match="gave no answer"):
            design_panels.panel_decide(pid, "x", "y", considered=[OUTSIDER])
        out = design_panels.panel_decide(
            pid, "Own table, with a log event per write", "Both concerns hold: ..."
        )
    decision = out["decision"]
    assert decision["considered"] == [SEAT_A, SEAT_B]
    assert out["status"] == "decided" and out["task_note"] == "appended"
    assert not any(k in decision for k in ("votes", "tally", "count"))
    conn = storage.connect()
    try:
        notes = conn.execute("SELECT notes FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    finally:
        conn.close()
    assert f"[panel-decision: {pid}]" in notes


def test_cancel_list_and_coordination_actions():
    with _as(OPENER):
        out = coordination_manage(
            "panel_open",
            {"question": QUESTION, "seats": [SEAT_A, SEAT_B], "deadline_hours": 2},
        )
        pid = out["panel_id"]
        listed = coordination_manage("panel_list", {"seat": SEAT_A})
        assert [p["id"] for p in listed["panels"]] == [pid]
        with pytest.raises(ToolError, match="status must be"):
            coordination_manage("panel_list", {"status": "weird"})
    with _as(SEAT_A), pytest.raises(PermissionDeniedError):
        design_panels.panel_cancel(pid, "no")
    with _as(OPERATOR):
        assert design_panels.panel_cancel(pid, "superseded")["status"] == "cancelled"
    with _as(SEAT_A), pytest.raises(ToolError, match="cancelled"):
        design_panels.panel_answer(pid, "x")


def test_a_revealed_panel_cannot_be_cancelled_and_its_answers_stay():
    """A revealed panel's answers are a record: cancel is refused, nothing vanishes."""
    pid = _open()["panel_id"]
    for seat, answer in ((SEAT_A, "own table"), (SEAT_B, "event log")):
        with _as(seat):
            design_panels.panel_answer(pid, answer)
    for who in (OPENER, OPERATOR):
        with _as(who), pytest.raises(ToolError, match="already revealed"):
            design_panels.panel_cancel(pid, "changed course")
    with _as(OUTSIDER):
        view = design_panels.panel_get(pid)
    assert view["status"] == "revealed"
    assert {s["seat"]: s["answer"] for s in view["seats"]} == {
        SEAT_A: "own table",
        SEAT_B: "event log",
    }
    with _as(OPENER):
        assert design_panels.panel_decide(pid, "own table", "why")["status"] == "decided"


def test_tenant_fence(monkeypatch):
    pid = _open()["panel_id"]
    monkeypatch.setattr(design_panels.edition, "tenant_org_id", lambda: "org-other")
    with _as(SEAT_A), pytest.raises(ToolError, match="not found"):
        design_panels.panel_get(pid)
