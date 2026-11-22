"""Contract fixtures for the approved-dispatch state machine (task #547).

These tests encode the acceptance table in
``docs/architecture/approved-dispatch-state-machine.md``. They do **not**
implement the coordinator (#548): they assert fail-closed predicates and
required transition fields against real task/delegation/approval persistence
with a stubbed launch boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from unified_agent import storage
from unified_agent.tools import delegate, tasks

APPROVAL_MARKER_RE = re.compile(r"\[plan:\s*approved\s+@", re.I)
KNOWN_AGENTS = frozenset(
    {
        "Desktop_Agent1",
        "Desktop_Agent2",
        "Desktop_agent3",
        "Desktop_Agent4",
        "Desktop_Agent6",
        "Laptop_Agent1",
        "Laptop_Agent2",
        "Laptop_agent3",
        "Laptop_Agent4",
        "Laptop_Agent6",
        "Desktop_Agent5",
        "Laptop_Agent5",
        "Operator",
    }
)


@dataclass
class Transition:
    actor: str
    task_id: int
    agent_label: str
    run_id: str | None
    timestamp: str
    evidence_ref: str | None = None
    failure_reason: str | None = None
    delegation_id: int | None = None


@dataclass
class DispatchContract:
    """Spec-level ready predicate + launch stub (no real process spawn)."""

    launches: list[dict[str, Any]] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    claimed: set[int] = field(default_factory=set)

    def is_ready(self, task: dict, *, approval_ok: bool, agent_label: str) -> tuple[bool, str]:
        if task.get("status") in {"done", "cancelled"}:
            return False, "task_terminal"
        if not approval_ok:
            return False, "missing_or_denied_approval"
        if agent_label not in KNOWN_AGENTS:
            return False, "unknown_agent"
        if task["id"] in self.claimed:
            return False, "already_claimed"
        notes = task.get("notes") or ""
        # Either an explicit note marker or a prior approvals-row (approval_ok).
        if not APPROVAL_MARKER_RE.search(notes) and not approval_ok:
            return False, "missing_approval_marker"
        return True, "ok"

    def claim_and_launch(
        self,
        task: dict,
        *,
        actor: str,
        agent_label: str,
        approval_ok: bool,
        timestamp: str,
        delegation_id: int | None = None,
    ) -> dict[str, Any]:
        ready, reason = self.is_ready(task, approval_ok=approval_ok, agent_label=agent_label)
        if not ready:
            self.transitions.append(
                Transition(
                    actor=actor,
                    task_id=task["id"],
                    agent_label=agent_label,
                    run_id=None,
                    timestamp=timestamp,
                    failure_reason=reason,
                    delegation_id=delegation_id,
                )
            )
            return {"launched": False, "reason": reason, "run_id": None}

        run_id = f"run-{task['id']}-1"
        self.claimed.add(task["id"])
        self.launches.append({"task_id": task["id"], "run_id": run_id, "agent": agent_label})
        self.transitions.append(
            Transition(
                actor=actor,
                task_id=task["id"],
                agent_label=agent_label,
                run_id=run_id,
                timestamp=timestamp,
                delegation_id=delegation_id,
            )
        )
        return {"launched": True, "reason": "ok", "run_id": run_id}


REQUIRED_TRANSITION_FIELDS = (
    "actor",
    "task_id",
    "agent_label",
    "run_id",
    "timestamp",
    "failure_reason",
    "delegation_id",
    "evidence_ref",
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage.init_db()
    return tmp_path


def _delegated_child(to_agent: str = "Desktop_Agent1") -> dict:
    spec = (
        "### Objective\nDispatch contract fixture for approved-delegation lane.\n"
        "### Scope\n- docs/architecture/approved-dispatch-state-machine.md\n"
        "- tests/test_dispatch_contract_spec.py\n"
        "### Definition of Done\n"
        "- claim_and_launch with Operator approval asserts launched once and "
        "records actor/task_id/agent_label/run_id/timestamp\n"
        "- denied and unknown-agent paths refuse and never spawn a process "
        "(assert launches == [])\n"
        "- Gates: uv run pytest tests/test_dispatch_contract_spec.py\n"
        "### TDD Constraints\n"
        "- Stub: process launch boundary only\n"
        "- Must be real: task/delegation persistence\n"
        "### Boundaries\n"
        "- Must not enable Kanban/cron autonomy or edit Red-tier config\n"
        "- Must not weaken approval fail-closed rules\n"
    )
    result = delegate.delegate(
        task_spec=spec,
        from_agent="Desktop_Agent5",
        to_agent=to_agent,
        callback="none",
    )
    child = next(t for t in tasks.task_list(status="all")["tasks"] if t["id"] == result["task_id"])
    return {"delegation_id": result["delegation_id"], "task": child}


def test_accepted_approval_launches_once_with_required_fields(db):
    packed = _delegated_child()
    tasks.task_update(
        packed["task"]["id"],
        append_note="[plan: approved @ 2026-08-23T00:00:00Z]",
    )
    task = next(
        t for t in tasks.task_list(status="all")["tasks"] if t["id"] == packed["task"]["id"]
    )

    contract = DispatchContract()
    first = contract.claim_and_launch(
        task,
        actor="Operator",
        agent_label="Desktop_Agent1",
        approval_ok=True,
        timestamp="2026-08-23T12:00:00Z",
        delegation_id=packed["delegation_id"],
    )
    second = contract.claim_and_launch(
        task,
        actor="dispatch-coordinator",
        agent_label="Desktop_Agent1",
        approval_ok=True,
        timestamp="2026-08-23T12:00:01Z",
        delegation_id=packed["delegation_id"],
    )

    assert first["launched"] is True and first["run_id"]
    assert second["launched"] is False and second["reason"] == "already_claimed"
    assert len(contract.launches) == 1

    ok = contract.transitions[0]
    for name in REQUIRED_TRANSITION_FIELDS:
        assert hasattr(ok, name)
    assert ok.actor == "Operator"
    assert ok.task_id == task["id"]
    assert ok.agent_label == "Desktop_Agent1"
    assert ok.run_id == first["run_id"]
    assert ok.timestamp.endswith("Z")
    assert ok.delegation_id == packed["delegation_id"]
    assert ok.failure_reason is None


def test_denied_never_launches(db):
    packed = _delegated_child()
    tasks.task_update(
        packed["task"]["id"],
        append_note="[plan: denied @ 2026-08-23T00:00:00Z]",
    )
    task = next(
        t for t in tasks.task_list(status="all")["tasks"] if t["id"] == packed["task"]["id"]
    )
    contract = DispatchContract()
    out = contract.claim_and_launch(
        task,
        actor="Operator",
        agent_label="Desktop_Agent1",
        approval_ok=False,
        timestamp="2026-08-23T12:00:00Z",
        delegation_id=packed["delegation_id"],
    )
    assert out["launched"] is False
    assert out["reason"] == "missing_or_denied_approval"
    assert contract.launches == []
    assert contract.transitions[0].failure_reason == "missing_or_denied_approval"
    assert contract.transitions[0].run_id is None


def test_unknown_agent_refused(db):
    packed = _delegated_child()
    tasks.task_update(
        packed["task"]["id"],
        append_note="[plan: approved @ 2026-08-23T00:00:00Z]",
    )
    task = next(
        t for t in tasks.task_list(status="all")["tasks"] if t["id"] == packed["task"]["id"]
    )
    contract = DispatchContract()
    out = contract.claim_and_launch(
        task,
        actor="dispatch-coordinator",
        agent_label="Desktop_Nobody",
        approval_ok=True,
        timestamp="2026-08-23T12:00:00Z",
        delegation_id=packed["delegation_id"],
    )
    assert out == {"launched": False, "reason": "unknown_agent", "run_id": None}
    assert contract.launches == []


def test_missing_approval_refused(db):
    packed = _delegated_child()
    task = packed["task"]
    contract = DispatchContract()
    out = contract.claim_and_launch(
        task,
        actor="dispatch-coordinator",
        agent_label="Desktop_Agent1",
        approval_ok=False,
        timestamp="2026-08-23T12:00:00Z",
        delegation_id=packed["delegation_id"],
    )
    assert out["launched"] is False
    assert "approval" in out["reason"]
    assert contract.launches == []
