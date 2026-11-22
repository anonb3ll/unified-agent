"""Per-task workflow record: stage, resume capsule and graded checks (task #2398).

A check starts ``attested``; only a resolved reference that agrees raises it to
``verified``; a check recorded at another head is ``unknown`` at this one. The
capsule is written by the claimant, next owner or an operator, versioned by
``revision``, and carried in ``session_packet``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, task_workflow
from unified_agent.lifecycle.packet import build_session_packet
from unified_agent.tool_errors import PermissionDeniedError
from unified_agent.tools.graph import coordination_manage
from unified_agent.tools.tasks import task_add, task_claim

OPERATOR = "Desktop_Operator"
DEV = "Desktop_Agent2"
OTHER = "Desktop_Agent4"
HEAD = "a" * 40
NEW_HEAD = "b" * 40
REPO = "example/repo"


@contextmanager
def _as(label: str):
    token = config.agent_identity_var.set(label)
    try:
        yield label
    finally:
        config.agent_identity_var.reset(token)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("UA_AGENT_WIP_MAX", "50")
    monkeypatch.setenv("UA_LIFECYCLE_OPERATORS", OPERATOR)


@pytest.fixture
def tid() -> int:
    task_id = task_add("workflow", lane="dispatch-ready")["id"]
    with _as(DEV):
        task_claim(task_id, DEV, expected_lane="dispatch-ready")
    return task_id


def _verifier(verdict: str, detail: str = "", observed: dict | None = None):
    calls: list[tuple] = []

    def verify(kind, ref, repo, expect, head_sha):
        calls.append((kind, ref, repo, expect, head_sha))
        return verdict, detail, observed or {}

    verify.calls = calls
    return verify


def test_capsule_set_get_and_revision_fence(tid):
    with _as(DEV):
        out = task_workflow.set_capsule(
            tid,
            stage="implement",
            goal="ship the thing",
            done=["schema", " ", "tests"],
            focus="wiring",
            head_sha=HEAD,
        )
        assert out["revision"] == 1
        assert out["stage"] == "implement"
        assert out["capsule"] == {
            "goal": "ship the thing",
            "done": ["schema", "tests"],
            "focus": "wiring",
        }
        assert out["trust"]["level"] == "untrusted"
        # Omitted fields keep their value.
        out = task_workflow.set_capsule(tid, stage="verify", expected_revision=1)
        assert out["revision"] == 2 and out["capsule"]["goal"] == "ship the thing"
        with pytest.raises(ToolError, match="revision 2, not 1"):
            task_workflow.set_capsule(tid, focus="x", expected_revision=1)
        with pytest.raises(ToolError, match="stage must be one of"):
            task_workflow.set_capsule(tid, stage="dreaming")
        with pytest.raises(ToolError, match="nothing to set"):
            task_workflow.set_capsule(tid)
        with pytest.raises(ToolError, match="done must be a list"):
            task_workflow.set_capsule(tid, done=["x"] * (task_workflow.DONE_MAX_ITEMS + 1))
        with pytest.raises(ToolError, match="hex commit SHA"):
            task_workflow.set_capsule(tid, head_sha="not-a-sha")


def test_only_claimant_next_owner_or_operator_may_write(tid):
    with _as(OTHER), pytest.raises(PermissionDeniedError, match="may not write"):
        task_workflow.set_capsule(tid, stage="plan")
    with _as(OTHER), pytest.raises(PermissionDeniedError, match="cannot act as"):
        task_workflow.set_capsule(tid, stage="plan", actor=DEV)
    with _as(OTHER), pytest.raises(PermissionDeniedError):
        task_workflow.record_check(tid, "tests", "pass", HEAD)
    with _as(OPERATOR):
        assert task_workflow.set_capsule(tid, stage="review")["updated_by"] == OPERATOR
    # Anyone who can see the task may read it.
    with _as(OTHER):
        assert task_workflow.get(tid)["stage"] == "review"
    with _as(DEV), pytest.raises(ToolError, match="not found"):
        task_workflow.get(999_999)


def test_check_grades_attested_verified_unknown(tid):
    ok = _verifier("verified", "run concluded success", {"head": HEAD})
    with _as(DEV):
        task_workflow.set_capsule(tid, head_sha=HEAD, expected_checks=["tests", "ci", "lint"])
        task_workflow.record_check(tid, "tests", "pass", HEAD, detail="412 passed")
        out = task_workflow.record_check(
            tid,
            "ci",
            "pass",
            HEAD,
            ref={"kind": "ci_run", "ref": "#123", "repo": REPO},
            verifier=ok,
        )
    assert ok.calls == [("ci_run", "123", REPO, "success", HEAD)]
    grades = {c["name"]: c["grade"] for c in out["checks"]}
    assert grades == {"ci": "verified", "lint": "unknown", "tests": "attested"}
    assert out["summary"]["verified"] == ["ci"] and out["summary"]["unknown"] == ["lint"]
    ci = next(c for c in out["checks"] if c["name"] == "ci")
    assert ci["verified_at"] and ci["verification"]["verdict"] == "verified"
    # Judged at a newer head, every check recorded at the old one is unknown.
    with _as(DEV):
        moved = task_workflow.get(tid, NEW_HEAD)
    assert {c["name"]: c["grade"] for c in moved["checks"]} == {
        "ci": "unknown",
        "lint": "unknown",
        "tests": "unknown",
    }
    assert next(c for c in moved["checks"] if c["name"] == "ci")["stale_head"] == HEAD


def _jobs(*jobs, attempt=1):
    """A ``gh run view --json jobs,attempt`` answer; each job is (name, status, conclusion)."""

    def gh(args):
        assert args[:2] == ["run", "view"] and args[-1] == "jobs,attempt"
        body = {
            "attempt": attempt,
            "jobs": [{"name": n, "status": s, "conclusion": c} for n, s, c in jobs],
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")

    return task_workflow.default_job_verifier(gh)


@pytest.mark.parametrize(
    ("jobs", "grade", "verdict", "why"),
    [
        ([("lint", "completed", "success")], "verified", "verified", "concluded success"),
        ([("lint", "completed", "skipped")], "attested", "mismatch", "was skipped"),
        ([("tests", "completed", "success")], "attested", "mismatch", "no job named 'lint'"),
        ([("lint", "completed", "failure")], "attested", "mismatch", "concluded failure"),
        ([("lint", "in_progress", "")], "attested", "insufficient", "not finished"),
        (
            [("lint", "completed", "success"), ("lint", "completed", "failure")],
            "attested",
            "mismatch",
            "concluded failure, success",
        ),
    ],
    ids=["ran", "skipped", "missing", "failed", "running", "disagree"],
)
def test_a_named_job_is_graded_on_that_job_not_on_the_green_run(tid, jobs, grade, verdict, why):
    """The run is green in every case; only a job that ran and agrees verifies the check."""
    run_ok = _verifier("verified", "run concluded success", {"head": HEAD})
    ref = {"kind": "ci_run", "ref": "123", "repo": REPO, "job": "lint"}
    with _as(DEV):
        out = task_workflow.record_check(
            tid, "lint", "pass", HEAD, ref=ref, verifier=run_ok, job_verifier=_jobs(*jobs)
        )
    lint = out["checks"][0]
    assert (lint["grade"], lint["verification"]["verdict"]) == (grade, verdict)
    assert why in lint["verification"]["detail"] and lint["ref"]["job"] == "lint"
    assert lint["verified_scope"] == ("job" if grade == "verified" else None)
    assert out["summary"]["verified_run_only"] == []
    assert out["summary"]["contradicted"] == (["lint"] if verdict == "mismatch" else [])


def test_a_check_with_no_job_named_is_verified_for_the_run_only(tid):
    run_ok = _verifier("verified", "run concluded success", {"head": HEAD})
    never = _jobs()
    with _as(DEV):
        out = task_workflow.record_check(
            tid,
            "lint",
            "pass",
            HEAD,
            ref={"kind": "ci_run", "ref": "123", "repo": REPO},
            verifier=run_ok,
            job_verifier=never,
        )
        lint = out["checks"][0]
        assert (lint["grade"], lint["verified_scope"]) == ("verified", "run")
        assert "the whole run only" in lint["verification"]["detail"]
        assert out["summary"]["verified_run_only"] == ["lint"]
        # A run that does not verify is never asked about its jobs.
        bad = _verifier("mismatch", "run concluded failure")
        named = {"kind": "ci_run", "ref": "123", "repo": REPO, "job": "lint"}
        out = task_workflow.record_check(
            tid, "lint", "pass", HEAD, ref=named, verifier=bad, job_verifier=never
        )
        assert out["checks"][0]["verification"]["detail"] == "run concluded failure"
        for job in ("", " ", "x" * 121, 7):
            with pytest.raises(ToolError, match="ref.job"):
                task_workflow.record_check(tid, "lint", "pass", HEAD, ref={**named, "job": job})


def test_contradicting_reference_stays_attested_and_rerecord_resets(tid):
    bad = _verifier("mismatch", f"run covers {NEW_HEAD}, not the reviewed head {HEAD}")
    ref = {"kind": "ci_run", "ref": "77", "repo": REPO}
    with _as(DEV):
        out = task_workflow.record_check(tid, "ci", "pass", HEAD, ref=ref, verifier=bad)
    ci = out["checks"][0]
    assert ci["grade"] == "attested" and ci["verification"]["verdict"] == "mismatch"
    assert out["summary"]["contradicted"] == ["ci"]
    good = _verifier("verified")
    with _as(DEV):
        assert task_workflow.verify_check(tid, "ci", HEAD, verifier=good)["checks"][0]["grade"] == (
            "verified"
        )
        # A new claim at the same head has not been verified: back to attested.
        out = task_workflow.record_check(tid, "ci", "fail", HEAD, ref=ref, verify=False)
    ci = out["checks"][0]
    assert ci["grade"] == "attested" and ci["outcome"] == "fail" and ci["verification"] is None
    assert out["summary"]["failing"] == ["ci"]
    # A resolver that blows up is "not verified", never an error.

    def boom(*_a):
        raise RuntimeError("gh exploded")

    with _as(DEV):
        out = task_workflow.verify_check(tid, "ci", HEAD, verifier=boom)
    assert out["checks"][0]["grade"] == "attested"
    assert out["checks"][0]["verification"]["verdict"] == "insufficient"


def test_a_rerecord_in_the_same_second_survives_a_stale_resolution(tid, monkeypatch):
    """The fence on ``recorded_at`` must hold inside one wall-clock second.

    A resolver runs outside any transaction. If the check is re-recorded while it
    runs, the stale grade must not land on the new claim, even when both records
    fall in the same second.
    """
    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
    ticks = iter(range(1, 1_000))

    class _SameSecondClock:
        @staticmethod
        def now(tz=None):
            return base + timedelta(microseconds=next(ticks))

    monkeypatch.setattr(task_workflow, "datetime", _SameSecondClock)
    ref = {"kind": "ci_run", "ref": "77", "repo": REPO}
    with _as(DEV):
        task_workflow.record_check(tid, "ci", "pass", HEAD, ref=ref, verify=False)
    good = _verifier("verified")

    def rerecord_then_resolve(*args):
        task_workflow.record_check(tid, "ci", "fail", HEAD, ref=ref, verify=False)
        return good(*args)

    with _as(DEV):
        out = task_workflow.verify_check(tid, "ci", HEAD, verifier=rerecord_then_resolve)
    ci = out["checks"][0]
    assert ci["outcome"] == "fail"
    assert ci["grade"] == "attested" and ci["verification"] is None


def test_check_validation(tid):
    with _as(DEV):
        with pytest.raises(ToolError, match="check name"):
            task_workflow.record_check(tid, "Has Spaces", "pass", HEAD)
        with pytest.raises(ToolError, match="outcome must be"):
            task_workflow.record_check(tid, "tests", "green", HEAD)
        with pytest.raises(ToolError, match="ref.kind"):
            task_workflow.record_check(tid, "tests", "pass", HEAD, ref={"kind": "pr", "ref": "1"})
        with pytest.raises(ToolError, match="numeric run id"):
            task_workflow.record_check(
                tid, "ci", "pass", HEAD, ref={"kind": "ci_run", "ref": "abc", "repo": REPO}
            )
        with pytest.raises(ToolError, match="no check"):
            task_workflow.verify_check(tid, "ci", HEAD, verifier=_verifier("verified"))
        # A ref without a repo is stored but cannot be resolved: attested.
        out = task_workflow.record_check(
            tid,
            "ci",
            "pass",
            HEAD,
            ref={"kind": "ci_run", "ref": "5"},
            verifier=_verifier("verified"),
        )
        assert out["checks"][0]["grade"] == "attested"


def test_coordination_actions_and_session_packet(tid):
    with _as(DEV):
        assert build_session_packet(task_id=tid, agent=DEV)["workflow"] is None
        coordination_manage(
            "task_workflow_set",
            {"task_id": tid, "stage": "review", "goal": "g", "focus": "f", "head_sha": HEAD},
        )
        coordination_manage(
            "task_check_record",
            {"task_id": tid, "name": "tests", "outcome": "pass", "head_sha": HEAD},
        )
        got = coordination_manage("task_workflow_get", {"task_id": tid})
        assert got["stage"] == "review" and got["checks"][0]["grade"] == "attested"
        section = build_session_packet(task_id=tid, agent=DEV)["workflow"]
    assert section["stage"] == "review"
    assert section["capsule"]["goal"] == "g"
    assert section["checks"] == {"tests": "attested"}


def test_writes_are_recorded_on_the_ledger(tid):
    from unified_agent import storage

    with _as(DEV):
        task_workflow.set_capsule(tid, stage="plan")
        task_workflow.record_check(tid, "tests", "pass", HEAD)
    conn = storage.connect()
    try:
        kinds = [
            r["payload"]
            for r in conn.execute(
                "SELECT payload FROM event_log WHERE channel = ? ORDER BY ulid",
                (task_workflow.CHANNEL,),
            )
        ]
    finally:
        conn.close()
    assert len(kinds) == 2 and '"kind": "capsule"' in kinds[0] and '"kind": "check"' in kinds[1]
