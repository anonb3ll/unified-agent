"""task_claim and task_update replies name the verified caller (task #2037).

The usage hook must tell the runs its own session owns from a surviving
claimant's. Reply fields such as ``current_run_agent`` can name another agent
(a doing reassert keeps the existing run), so the hub states the caller's own
verified label instead of leaving the hook to guess.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from unified_agent import config
from unified_agent.tools import tasks
from unified_agent.tools.tasks import task_add, task_claim, task_update


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture(autouse=True)
def _no_worktrees(monkeypatch):
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *args, **kwargs: None)


def test_a_claim_names_the_verified_caller():
    tid = task_add("claimed", lane="dispatch-ready")["id"]
    with _as("agent-a"):
        reply = task_claim(tid, "agent-a", expected_lane="dispatch-ready")
    assert reply["verified_caller"] == "agent-a"


def test_a_reassert_names_the_caller_not_the_runs_agent():
    tid = task_add("shared")["id"]
    with _as("agent-a"):
        opened = task_update(tid, status="doing")
    assert opened["current_run_agent"] == "agent-a"
    assert opened["verified_caller"] == "agent-a"
    with _as("agent-b"):
        reasserted = task_update(tid, status="doing")
    # The hub kept agent-a's run; the reply still says who is asking.
    assert reasserted["current_run_id"] == opened["current_run_id"]
    assert reasserted["current_run_agent"] == "agent-a"
    assert reasserted["verified_caller"] == "agent-b"


def test_an_unverified_call_names_no_caller():
    tid = task_add("anonymous")["id"]
    assert "verified_caller" not in task_update(tid, notes="progress")


def test_a_keyed_update_omits_the_caller_so_its_receipt_stays_shareable():
    # A keyed reply is the stored receipt, and another caller may legitimately
    # replay it (a racing clear), so it never names who first asked.
    from unified_agent import storage

    tid = task_add("retried")["id"]
    with _as("agent-a"):
        first = task_update(tid, status="doing", idempotency_key="k-2037")
    with _as("agent-b"):
        replay = task_update(tid, status="doing", idempotency_key="k-2037")
    assert "verified_caller" not in first
    assert replay == first
    conn = storage.connect()
    try:
        stored = storage.get_idempotency_record(conn, "k-2037", "task_update")
    finally:
        conn.close()
    assert stored is not None and "verified_caller" not in stored


def test_a_usage_only_report_names_the_caller():
    tid = task_add("measured", lane="dispatch-ready")["id"]
    with _as("agent-a"):
        claim = task_claim(tid, "agent-a", expected_lane="dispatch-ready")
        reply = task_claim(
            tid,
            "agent-a",
            usage_only=True,
            expected_run_id=claim["current_run_id"],
            run_usage={"output_tokens": 5},
        )
    assert reply["usage_only"] is True
    assert reply["verified_caller"] == "agent-a"
