"""Queue closure (task #1848): staleness, park/fold, close-on-merge, health, dry-run sweep.

Operator rules under test: a task untouched for 30 days is PARKED with a reason, not
closed; children of a parked epic FOLD into it; merged work CLOSES with the merge SHA as
evidence_ref; nothing closes on a date alone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, pr_models, pr_watch, queue_closure, queue_hygiene, storage
from unified_agent.tools import graph
from unified_agent.tools.tasks import task_add, task_claim, task_list, task_update

NOW = datetime.now(UTC).replace(microsecond=0)
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SHA2 = "0f9e8d7c6b5a49382716f5e4d3c2b1a098765432"


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(queue_closure, "_clock", lambda: NOW)


def _ago(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat(timespec="seconds")


def _set(task_id: int, **fields) -> None:
    conn = storage.connect()
    try:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))
        conn.commit()
    finally:
        conn.close()


def _row(task_id: int):
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()


def _age(task_id: int, **delta) -> None:
    stamp = _ago(**delta)
    _set(task_id, created_at=stamp, updated_at=stamp)


def _snapshot() -> list[tuple]:
    conn = storage.connect()
    try:
        return [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]
    finally:
        conn.close()


def _write_cursor(prs: dict[str, dict[int, dict]]) -> None:
    path = queue_closure.pr_watch_cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prs))


def _merged_pr(branch: str, number: int = 7, sha: str = SHA, repo: str = "example/repo") -> dict:
    return {
        repo: {
            number: {
                "state": "MERGED",
                "head_sha": "f" * 40,
                "head_branch": branch,
                "merged_at": _ago(days=1),
                "merge_sha": sha,
            }
        }
    }


def _stale_ids(days: int | None = None) -> list[int]:
    return [t["task_id"] for t in queue_closure.queue_stale(days)["tasks"]]


# --- staleness ------------------------------------------------------------------------


def test_staleness_boundary_is_thirty_days_inclusive():
    just_inside = task_add("29 days 23 hours")["id"]
    exactly = task_add("exactly 30 days")["id"]
    _age(just_inside, days=29, hours=23)
    _age(exactly, days=30)
    assert _stale_ids() == [exactly]
    report = queue_closure.queue_stale()
    assert report["stale_days"] == 30
    assert report["tasks"][0]["age_days"] == 30.0


def test_threshold_is_configurable(monkeypatch):
    task_id = task_add("ten days old")["id"]
    _age(task_id, days=10)
    assert _stale_ids() == []
    assert _stale_ids(7) == [task_id]
    monkeypatch.setenv("UA_QUEUE_STALE_DAYS", "5")
    assert _stale_ids() == [task_id]
    with pytest.raises(ToolError, match="at least 1"):
        queue_closure.queue_stale(0)


def test_a_status_change_a_note_or_a_claim_resets_the_clock():
    by_status = task_add("status changed")["id"]
    by_note = task_add("note appended")["id"]
    by_comment = task_add("comment added")["id"]
    by_claim = task_add("claimed")["id"]
    untouched = task_add("untouched")["id"]
    for task_id in (by_status, by_note, by_comment, by_claim, untouched):
        _age(task_id, days=45)
    assert set(_stale_ids()) == {by_status, by_note, by_comment, by_claim, untouched}

    task_update(by_status, status="blocked")
    task_update(by_note, append_note="still relevant")
    from unified_agent.task_comments import append_comment

    append_comment(by_comment, "checked in", author="test-agent")
    # A claim writes claimed_at; age updated_at back so only the claim stamp is recent.
    task_claim(by_claim, agent="Desktop_Agent1")
    _set(by_claim, updated_at=_ago(days=45), lock_expires_at=_ago(days=1), status="open")

    assert _stale_ids() == [untouched]


def test_live_claims_are_never_stale_but_lapsed_ones_are():
    live = task_add("live lease")["id"]
    lapsed = task_add("lapsed lease")["id"]
    for task_id in (live, lapsed):
        task_claim(task_id, agent="Desktop_Agent1")
        _set(task_id, claimed_at=_ago(days=40), updated_at=_ago(days=40))
    _set(live, lock_expires_at=(NOW + timedelta(hours=1)).isoformat(timespec="seconds"))
    _set(lapsed, lock_expires_at=_ago(days=39), current_run_id=None)
    assert _stale_ids() == [lapsed]


def test_finished_and_parked_tasks_are_not_stale():
    done = task_add("finished")["id"]
    task_update(done, status="done", actor="test-agent")
    parked = task_add("parked")["id"]
    for task_id in (done, parked):
        _age(task_id, days=90)
    queue_closure.queue_park(parked, "no longer relevant", actor="test-agent")
    assert _stale_ids() == []


# --- park / unpark ----------------------------------------------------------------------


def test_park_and_unpark_round_trip_with_reasons():
    task_id = task_add("old idea", notes="original notes")["id"]
    _age(task_id, days=40)
    before = _row(task_id)["status"]

    parked = queue_closure.queue_park(task_id, "untouched for 40 days", actor="test-agent")
    assert parked["parked"] == task_id and parked["folded"] == []
    row = _row(task_id)
    assert row["status"] == before  # parked is not cancelled, and nothing else moved
    assert row["archived_at"] is not None
    assert "original notes" in row["notes"]
    assert "by test-agent] untouched for 40 days" in row["notes"]
    state = queue_closure.closure_state(row["notes"])
    assert state["kind"] == "parked" and state["reason"] == "untouched for 40 days"

    # Visible in the parked view, with its reason; hidden from the default queue.
    view = task_list(status="all", parked=True)["tasks"]
    assert [t["id"] for t in view] == [task_id]
    assert view[0]["parked"]["reason"] == "untouched for 40 days"
    assert view[0]["parked"]["kind"] == "parked"
    assert task_id not in [t["id"] for t in task_list(status="open")["tasks"]]
    assert task_id in [t["id"] for t in task_list(status="all", archived=True)["tasks"]]
    with pytest.raises(ToolError, match="mutually exclusive"):
        task_list(parked=True, archived=True)
    # Nothing can start work on it while it is parked.
    with pytest.raises(ToolError, match="parked"):
        task_claim(task_id, agent="Desktop_Agent1")
    # Counted as parked, not open.
    health = queue_closure.queue_health()
    assert health["parked"] == 1 and health["open"] == 0

    restored = queue_closure.queue_unpark(task_id, "back in scope", actor="test-agent")
    assert restored["restored"] == [task_id]
    row = _row(task_id)
    assert row["archived_at"] is None and row["status"] == before
    assert queue_closure.closure_state(row["notes"])["kind"] == "unparked"
    assert "[parked @" in row["notes"]  # history kept
    assert task_id in [t["id"] for t in task_list(status="open")["tasks"]]
    assert task_list(status="all", parked=True)["tasks"] == []
    # The clock restarted: it is no longer stale.
    assert task_id not in _stale_ids()


def test_park_needs_a_reason_and_refuses_unsafe_targets():
    task_id = task_add("target")["id"]
    for bad in ("", "   ", None):
        with pytest.raises(ToolError, match="reason is required"):
            queue_closure.queue_park(task_id, bad, actor="test-agent")
    with pytest.raises(ToolError, match="actor is required"):
        queue_closure.queue_park(task_id, "because")
    with pytest.raises(ToolError, match="no task"):
        queue_closure.queue_park(999999, "because", actor="test-agent")

    task_claim(task_id, agent="Desktop_Agent1")
    with pytest.raises(ToolError, match="live claim"):
        queue_closure.queue_park(task_id, "because", actor="test-agent")

    finished = task_add("finished")["id"]
    task_update(finished, status="done", actor="test-agent")
    with pytest.raises(ToolError, match="already done"):
        queue_closure.queue_park(finished, "because", actor="test-agent")

    twice = task_add("twice")["id"]
    queue_closure.queue_park(twice, "first", actor="test-agent")
    with pytest.raises(ToolError, match="already parked"):
        queue_closure.queue_park(twice, "second", actor="test-agent")
    with pytest.raises(ToolError, match="not parked"):
        queue_closure.queue_unpark(task_id, "nothing to restore", actor="test-agent")


def test_protected_rows_need_a_named_approver_to_park():
    gated = task_add("gated", notes="[waiting-for: plan-approval]")["id"]
    with pytest.raises(ToolError, match="approved_by"):
        queue_closure.queue_park(gated, "stale", actor="test-agent")
    done = queue_closure.queue_park(gated, "stale", approved_by="the operator", actor="test-agent")
    assert done["parked"] == gated
    assert "(approved by the operator)" in _row(gated)["notes"]


def test_dry_run_park_writes_nothing():
    task_id = task_add("dry")["id"]
    before = _snapshot()
    result = queue_closure.queue_park(task_id, "would park", actor="test-agent", dry_run=True)
    assert result["dry_run"] is True
    assert _snapshot() == before


# --- fold ---------------------------------------------------------------------------------


def _epic_with_children():
    epic = task_add("epic")["id"]
    child = task_add("child", parent_id=epic)["id"]
    grandchild = task_add("grandchild", parent_id=child)["id"]
    done_child = task_add("finished child", parent_id=epic)["id"]
    task_update(done_child, status="done", actor="test-agent")
    return epic, child, grandchild, done_child


def test_children_fold_into_a_parked_parent_and_come_back_with_it():
    epic, child, grandchild, done_child = _epic_with_children()
    result = queue_closure.queue_park(epic, "epic shelved", actor="test-agent")
    assert sorted(result["folded"]) == sorted([child, grandchild])
    for task_id in (child, grandchild):
        row = _row(task_id)
        assert row["archived_at"] is not None
        assert f"[folded-into: #{epic} @" in row["notes"]
        assert "parent #" in row["notes"] and "epic shelved" in row["notes"]
        state = queue_closure.closure_state(row["notes"])
        assert state["kind"] == "folded" and state["root"] == epic
    # Finished children are left alone; no open child remains under the parked root.
    assert _row(done_child)["archived_at"] is None
    conn = storage.connect()
    try:
        orphans = conn.execute(
            "SELECT id FROM tasks WHERE parent_id=? AND archived_at IS NULL "
            "AND status NOT IN ('done','cancelled')",
            (epic,),
        ).fetchall()
    finally:
        conn.close()
    assert orphans == []
    parked_view = {
        t["id"]: t["parked"]["kind"] for t in task_list(status="all", parked=True)["tasks"]
    }
    assert parked_view == {epic: "parked", child: "folded", grandchild: "folded"}

    # A folded child is restored through its root, never on its own.
    with pytest.raises(ToolError, match="unpark that root"):
        queue_closure.queue_unpark(child, "just this one", actor="test-agent")
    restored = queue_closure.queue_unpark(epic, "epic is back", actor="test-agent")
    assert sorted(restored["restored"]) == sorted([epic, child, grandchild])
    assert all(_row(i)["archived_at"] is None for i in (epic, child, grandchild))


def test_parking_never_orphans_children_or_parks_over_a_live_claim():
    epic, child, _grandchild, _done = _epic_with_children()
    with pytest.raises(ToolError, match="open under a parked root"):
        queue_closure.queue_park(epic, "no fold", fold_children=False, actor="test-agent")
    task_claim(child, agent="Desktop_Agent1")
    with pytest.raises(ToolError, match=f"child #{child} has live claim"):
        queue_closure.queue_park(epic, "shelved", actor="test-agent")
    assert _row(epic)["archived_at"] is None  # nothing was written


def test_a_child_added_later_folds_by_an_explicit_action():
    epic = task_add("epic")["id"]
    queue_closure.queue_park(epic, "shelved", actor="test-agent")
    late = task_add("late child", parent_id=epic)["id"]
    stranger = task_add("unrelated")["id"]
    with pytest.raises(ToolError, match="not an ancestor"):
        queue_closure.queue_fold(stranger, epic, "wrong tree", actor="test-agent")
    with pytest.raises(ToolError, match="reason is required"):
        queue_closure.queue_fold(late, epic, "", actor="test-agent")
    queue_closure.queue_fold(late, epic, "added after parking", actor="test-agent")
    assert queue_closure.closure_state(_row(late)["notes"])["root"] == epic
    open_parent = task_add("open parent")["id"]
    open_child = task_add("c", parent_id=open_parent)["id"]
    with pytest.raises(ToolError, match="not parked"):
        queue_closure.queue_fold(open_child, open_parent, "nope", actor="test-agent")


# --- close-on-merge -------------------------------------------------------------------------


def _task_with_branch(title="shipped work", **fields) -> tuple[int, str]:
    task_id = task_add(title)["id"]
    branch = f"task/{task_id}-shipped"
    _set(task_id, branch=branch, **fields)
    return task_id, branch


def test_unambiguous_merge_closes_with_the_merge_sha_and_dry_run_writes_nothing():
    task_id, branch = _task_with_branch()
    _write_cursor(_merged_pr(branch))
    before = _snapshot()

    plan = queue_closure.close_on_merge(dry_run=True)
    assert [e["task_id"] for e in plan["would_close"]] == [task_id]
    assert plan["would_close"][0]["merge_sha"] == SHA and plan["closed"] == []
    assert _snapshot() == before

    result = queue_closure.close_on_merge(dry_run=False)
    assert [e["task_id"] for e in result["closed"]] == [task_id]
    row = _row(task_id)
    assert row["status"] == "done"
    assert row["evidence_ref"] == SHA
    assert "closed-on-merge" in row["notes"] and f"merge_sha={SHA}" in row["notes"]
    # Idempotent: a finished task never matches again.
    assert queue_closure.close_on_merge(dry_run=False)["closed"] == []


def test_evidence_ref_pr_url_matches_and_the_old_ref_is_kept_in_notes():
    task_id = task_add("by evidence")["id"]
    url = "https://github.com/example/repo/pull/7"
    _set(task_id, evidence_ref=url)
    _write_cursor(_merged_pr(f"task/{task_id}-by-evidence", number=7))
    result = queue_closure.close_on_merge(dry_run=False)
    assert result["closed"][0]["matched_by"] == "evidence_ref"
    row = _row(task_id)
    assert row["evidence_ref"] == SHA and url in row["notes"]


def test_a_follow_up_citing_a_merged_pr_it_did_not_ship_is_blocked_not_closed():
    # "PR #7 follow-ups" cites the parent PR; that PR's branch names another task.
    follow_up = task_add("PR 7 follow-ups")["id"]
    _set(follow_up, evidence_ref="https://github.com/example/repo/pull/7")
    _write_cursor(_merged_pr(f"task/{follow_up + 1000}-parent-work", number=7))
    result = queue_closure.close_on_merge(dry_run=False)
    assert result["closed"] == []
    assert [b["task_id"] for b in result["blocked"]] == [follow_up]
    assert "does not name this task" in result["blocked"][0]["reason"]
    assert _row(follow_up)["status"] == "open"


def test_ambiguous_matches_never_close():
    two_merged, branch = _task_with_branch("two merged PRs on one branch")
    prs = _merged_pr(branch, number=7)
    prs["example/repo"][8] = {**prs["example/repo"][7], "merge_sha": SHA2}
    open_plus_merged, branch2 = _task_with_branch("a PR is still open")
    prs["example/other"] = {
        3: {"state": "MERGED", "head_branch": branch2, "merged_at": _ago(days=2), "merge_sha": SHA},
        4: {"state": "OPEN", "head_branch": branch2},
    }
    conflicting = task_add("evidence says one PR, branch says another")["id"]
    branch3 = f"task/{conflicting}-x"
    _set(conflicting, branch=branch3, evidence_ref="https://github.com/example/repo/pull/7")
    prs["example/third"] = {9: {"state": "MERGED", "head_branch": branch3, "merge_sha": SHA2}}
    _write_cursor(prs)

    result = queue_closure.close_on_merge(dry_run=False)
    assert result["closed"] == []
    assert {a["task_id"] for a in result["ambiguous"]} == {
        two_merged,
        open_plus_merged,
        conflicting,
    }
    for task_id in (two_merged, open_plus_merged, conflicting):
        assert _row(task_id)["status"] == "open"


def test_a_closed_unmerged_pr_does_not_make_a_merge_ambiguous():
    task_id, branch = _task_with_branch()
    prs = _merged_pr(branch, number=7)
    prs["example/repo"][6] = {"state": "CLOSED", "head_branch": branch, "closed_at": _ago(days=5)}
    _write_cursor(prs)
    assert [e["task_id"] for e in queue_closure.close_on_merge(dry_run=False)["closed"]] == [
        task_id
    ]


def test_branch_must_name_the_task_and_a_missing_sha_blocks():
    unrelated = task_add("branch does not name it")["id"]
    _set(unrelated, branch="main")
    nosha, branch = _task_with_branch("sha unknown")
    prs = _merged_pr(branch, number=7, sha="")
    prs["example/repo"][8] = {"state": "MERGED", "head_branch": "main", "merge_sha": SHA}
    _write_cursor(prs)
    result = queue_closure.close_on_merge(dry_run=False)
    assert result["closed"] == []
    assert [b["task_id"] for b in result["blocked"]] == [nosha]
    assert "merge SHA not recorded" in result["blocked"][0]["reason"]
    assert _row(unrelated)["status"] == "open"


@pytest.mark.parametrize(
    "setup, expected",
    [
        (lambda t: _set(t, lane="operator-only"), "lane operator-only"),
        (lambda t: _set(t, lane="tracking"), "tracking lane"),
        (lambda t: _set(t, notes="[waiting-for: human-merge]"), "human gate"),
        (lambda t: _set(t, notes="red-tier: touches AGENTS.md"), "red-tier"),
        (lambda t: _set(t, status="waiting_review"), "waiting_review"),
        (lambda t: task_claim(t, agent="Desktop_Agent1"), "live claim"),
    ],
)
def test_close_on_merge_never_acts_on_protected_rows(setup, expected):
    task_id, branch = _task_with_branch()
    setup(task_id)
    _write_cursor(_merged_pr(branch))
    result = queue_closure.close_on_merge(dry_run=False)
    assert result["closed"] == []
    assert expected in result["blocked"][0]["reason"]
    assert _row(task_id)["status"] != "done"


def _deployment_wait(task_id: int, until: datetime) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO task_liveness (task_id, wait_reason, wait_until, wait_deploy_target, "
            "wait_deploy_sha, updated_at) VALUES (?, 'rolling out', ?, 'hub', ?, ?)",
            (task_id, until.isoformat(), SHA, until.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_merge_does_not_close_a_task_waiting_on_its_deployment():
    task_id, branch = _task_with_branch()
    _deployment_wait(task_id, datetime.now(UTC) + timedelta(hours=1))
    _write_cursor(_merged_pr(branch))
    result = queue_closure.close_on_merge(dry_run=False)
    assert result["closed"] == [] and "deployment" in result["blocked"][0]["reason"]
    assert _row(task_id)["status"] != "done"


def test_an_expired_deployment_wait_no_longer_holds_a_merged_task():
    task_id, branch = _task_with_branch()
    _deployment_wait(task_id, datetime.now(UTC) - timedelta(hours=1))
    _write_cursor(_merged_pr(branch))
    assert [c["task_id"] for c in queue_closure.close_on_merge(dry_run=False)["closed"]] == [
        task_id
    ]


def test_a_task_with_open_children_is_not_closed_by_a_merge():
    parent, branch = _task_with_branch("parent")
    task_add("open child", parent_id=parent)
    _write_cursor(_merged_pr(branch))
    result = queue_closure.close_on_merge(dry_run=False)
    assert result["closed"] == [] and "open child" in result["blocked"][0]["reason"]


def test_nothing_closes_on_a_date_alone():
    old = task_add("ancient", notes="[milestone-review] Gate: #1.")["id"]
    _age(old, days=400)
    _set(old, due_at="2020-01-01")
    _write_cursor({})  # no PR evidence at all
    assert queue_closure.close_on_merge(dry_run=False)["closed"] == []
    assert queue_closure.queue_sweep()["counts"].get("close") is None
    assert _row(old)["status"] == "open"


def test_no_pr_cursor_is_not_an_error():
    assert not queue_closure.pr_watch_cursor_path().exists()
    assert queue_closure.close_on_merge(dry_run=False)["prs_considered"] == 0


def test_hygiene_pass_closes_merged_work_and_honors_dry_run_and_the_switch(monkeypatch):
    monkeypatch.setattr(queue_hygiene, "_clock", lambda: NOW)
    task_id, branch = _task_with_branch()
    _write_cursor(_merged_pr(branch))

    dry = queue_hygiene.run_hygiene_pass(send_notifications=False, dry_run=True)
    assert [e["task_id"] for e in dry["would_close_on_merge"]] == [task_id]
    assert dry["closed_on_merge"] == [] and _row(task_id)["status"] == "open"

    # Applying is opt-in: by default every pass only proposes.
    monkeypatch.delenv("UA_CLOSE_ON_MERGE", raising=False)
    default = queue_hygiene.run_hygiene_pass(send_notifications=False)
    assert default["closed_on_merge"] == [] and _row(task_id)["status"] == "open"
    assert [e["task_id"] for e in default["would_close_on_merge"]] == [task_id]

    monkeypatch.setenv("UA_CLOSE_ON_MERGE", "0")
    off = queue_hygiene.run_hygiene_pass(send_notifications=False)
    assert off["closed_on_merge"] == [] and _row(task_id)["status"] == "open"

    monkeypatch.setenv("UA_CLOSE_ON_MERGE", "1")
    on = queue_hygiene.run_hygiene_pass(send_notifications=False)
    assert [e["task_id"] for e in on["closed_on_merge"]] == [task_id]
    assert _row(task_id)["evidence_ref"] == SHA


# --- the PR event stream carries the merge SHA ------------------------------------------------


def test_pr_watch_records_the_merge_sha_without_making_it_an_event():
    payload = [
        {
            "number": 7,
            "title": "ship it",
            "url": "https://github.com/example/repo/pull/7",
            "state": "MERGED",
            "headRefName": "task/5-x",
            "headRefOid": "f" * 40,
            "mergedAt": "2026-10-01T00:00:00Z",
            "mergeCommit": {"oid": SHA},
        },
        {"number": 8, "title": "open", "state": "OPEN", "mergeCommit": None},
    ]
    prs = pr_watch.parse_prs(payload)
    assert prs[7].merge_sha == SHA and prs[8].merge_sha == ""
    assert prs[7].to_cursor()["merge_sha"] == SHA and "merge_sha" not in prs[8].to_cursor()
    # Learning the SHA later is not a PR event.
    earlier = pr_models.PRSnapshot(**{**prs[7].__dict__, "merge_sha": ""})
    assert earlier.diff_key() == prs[7].diff_key()
    assert pr_watch.diff_events("example/repo", {7: earlier}, {7: prs[7]}) == []
    merged_event = pr_models.PREvent("example/repo", "merged", prs[7])
    assert f"Merge commit: {SHA}" in pr_watch.note_for_event(merged_event)[1]
    # The closure reader and the poller agree on the cursor file.
    assert queue_closure.PR_WATCH_CURSOR_FILE == pr_watch.CURSOR_FILE
    path = config.data_home() / pr_watch.CURSOR_FILE
    pr_watch.save_cursor(path, {"example/repo": prs})
    index = {p["number"]: p for p in queue_closure.load_pr_index()}
    assert index[7]["merge_sha"] == SHA and index[7]["merged"] and not index[8]["merged"]
    assert pr_watch.load_cursor(path)["example/repo"][7].merge_sha == SHA


# --- health -------------------------------------------------------------------------------------


def test_health_reports_open_parked_stale_and_unjudged_review_ready_milestones():
    fresh = task_add("fresh")["id"]
    stale_a = task_add("stale a")["id"]
    stale_b = task_add("stale b", notes="[waiting-for: review]")["id"]  # stale but protected
    parked = task_add("parked")["id"]
    for task_id in (stale_a, stale_b, parked):
        _age(task_id, days=50)
    queue_closure.queue_park(parked, "shelved", actor="test-agent")

    gate_a = task_add("gate a")["id"]
    gate_b = task_add("gate b")["id"]
    for task_id in (gate_a, gate_b):
        task_update(task_id, status="done", actor="test-agent")
    ready = task_add(
        "M-ready",
        notes=f"[milestone-review] Gate: #{gate_a} first; #{gate_b} second. PASS needs x.",
    )["id"]
    judged = task_add(
        "M-judged",
        notes=f"[milestone-review] Gate: #{gate_a}.\nVERDICT: FAIL. Do not close.",
    )["id"]
    waiting_gate = task_add("gate c still open")["id"]
    not_ready = task_add(
        "M-not-ready", notes=f"[milestone-review] Gate: #{gate_a}, #{waiting_gate}."
    )["id"]
    via_dependency = task_add("M-dependency", notes="[milestone-review] no gate line")["id"]
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO task_dependencies (task_id, depends_on_task_id, created_by, created_at) "
            "VALUES (?,?,?,?)",
            (via_dependency, gate_a, "test-agent", _ago(days=1)),
        )
        conn.commit()
    finally:
        conn.close()

    health = queue_closure.queue_health()
    open_ids = {fresh, stale_a, stale_b, waiting_gate, ready, judged, not_ready, via_dependency}
    assert health["open"] == len(open_ids)
    assert health["parked"] == 1
    assert health["stale"] == 2 and health["stale_actionable"] == 1
    assert health["stale_days"] == 30
    assert health["milestones"]["review_ready"] == 3
    assert sorted(health["milestones"]["unjudged_ids"]) == sorted([ready, via_dependency])
    assert health["milestones_review_ready_unjudged"] == 2
    assert not_ready not in health["milestones"]["unjudged_ids"]
    assert judged not in health["milestones"]["unjudged_ids"]


def test_a_milestone_is_not_review_ready_on_its_date_alone():
    due = task_add("dated milestone", notes="[milestone-review] Gate: #999999.")["id"]
    _set(due, due_at="2020-01-01")
    assert queue_closure.queue_health()["milestones"]["review_ready"] == 0


# --- the dry-run sweep -----------------------------------------------------------------------------


def test_sweep_proposes_and_writes_nothing():
    stale = task_add("stale root")["id"]
    child = task_add("its open child", parent_id=stale)["id"]
    protected_lane = task_add("operator decision")["id"]
    gated = task_add("gated", notes="[waiting-for: plan-approval]")["id"]
    red = task_add("RED-TIER change")["id"]
    tracking = task_add("roadmap index")["id"]
    claimed = task_add("being worked")["id"]
    fresh = task_add("fresh")["id"]
    for task_id in (stale, child, protected_lane, gated, red, tracking, claimed):
        _age(task_id, days=60)
    _set(protected_lane, lane="operator-only")
    _set(tracking, lane="tracking")
    task_claim(claimed, agent="Desktop_Agent1")
    _set(claimed, updated_at=_ago(days=60), claimed_at=_ago(days=60))

    parked_root = task_add("already parked")["id"]
    under_parked = task_add("still open under it", parent_id=parked_root)["id"]
    queue_closure.queue_park(parked_root, "shelved", actor="test-agent")
    _set(under_parked, archived_at=None, notes="")  # a child created after the park

    merged, branch = _task_with_branch("merged work")
    _write_cursor(_merged_pr(branch))

    before = _snapshot()
    sweep = queue_closure.queue_sweep()
    assert _snapshot() == before and sweep["dry_run"] is True and sweep["writes"] == 0

    by_task = {p["task_id"]: p for p in sweep["proposals"]}
    assert by_task[stale]["proposed_action"] == "park"
    assert "no claim, status change or note for 60 days" in by_task[stale]["reason"]
    assert by_task[stale]["evidence"]["fold_children"] == [child]
    assert by_task[stale]["auto"] is False
    assert by_task[child]["proposed_action"] == "fold"
    assert by_task[child]["evidence"]["parked_root"] == stale
    assert by_task[under_parked]["proposed_action"] == "fold"
    assert by_task[under_parked]["evidence"]["parked_root"] == parked_root
    assert by_task[merged]["proposed_action"] == "close"
    assert by_task[merged]["auto"] is True
    assert by_task[merged]["evidence"]["merge_sha"] == SHA
    assert all(not p["auto"] for p in sweep["proposals"] if p["proposed_action"] != "close")
    # Boundaries: none of these is ever proposed for park.
    for task_id in (protected_lane, gated, red, tracking, claimed, fresh):
        assert task_id not in by_task
    skipped = {s["task_id"]: s["reason"] for s in sweep["skipped"]}
    assert "lane operator-only" in skipped[protected_lane]
    assert "human gate" in skipped[gated]
    assert "red-tier" in skipped[red]
    assert "tracking lane" in skipped[tracking]
    assert claimed not in skipped  # a live claim is never even stale
    assert sweep["counts"] == {"park": 1, "fold": 2, "close": 1}


def test_sweep_does_not_propose_parking_over_a_child_with_a_live_claim():
    parent = task_add("stale parent")["id"]
    child = task_add("claimed child", parent_id=parent)["id"]
    _age(parent, days=60)
    task_claim(child, agent="Desktop_Agent1")
    sweep = queue_closure.queue_sweep()
    assert sweep["proposals"] == []
    assert f"child #{child}" in sweep["skipped"][0]["reason"]


# --- the surface ---------------------------------------------------------------------------------------


def test_actions_are_reachable_through_coordination_manage():
    task_id = task_add("via the consolidated tool")["id"]
    health = graph.coordination_manage("queue_health", {})
    assert health["open"] == 1
    parked = graph.coordination_manage(
        "queue_park", {"task_id": task_id, "reason": "shelved", "actor": "test-agent"}
    )
    assert parked["parked"] == task_id
    assert graph.coordination_manage("queue_sweep", {})["writes"] == 0
    assert graph.coordination_manage("queue_close_merged", {})["dry_run"] is True
    graph.coordination_manage(
        "queue_unpark", {"task_id": task_id, "reason": "back", "actor": "test-agent"}
    )
    with pytest.raises(ToolError, match="invalid params"):
        graph.coordination_manage("queue_park", {"task_id": task_id})


def test_the_review_cli_exposes_the_reads_and_dry_runs(capsys):
    from unified_agent import review_cli

    task_add("cli task")
    assert review_cli.main(["queue", "health"]) == 0
    assert json.loads(capsys.readouterr().out)["open"] == 1
    assert review_cli.main(["queue", "sweep", "--stale-days", "7"]) == 0
    assert json.loads(capsys.readouterr().out)["writes"] == 0
    assert review_cli.main(["queue", "close-merged"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


# --- review follow-ups (PR #1003) ------------------------------------------------------------


def test_a_task_edited_after_it_was_matched_is_not_closed(monkeypatch):
    task_id, branch = _task_with_branch()
    _write_cursor(_merged_pr(branch))
    real = queue_closure.merge_matches

    def match_then_edit(conn, now, prs):
        matches = real(conn, now, prs)
        # Between matching and closing, an operator reassigns the task for human review.
        task_update(task_id, append_note="[waiting-for: human-merge]")
        return matches

    monkeypatch.setattr(queue_closure, "merge_matches", match_then_edit)
    result = queue_closure.close_on_merge(dry_run=False)
    assert result["closed"] == []
    assert result["skipped"][0]["reason"] == "task changed since it was matched"
    row = _row(task_id)
    assert row["status"] == "open" and row["evidence_ref"] is None


def test_closing_releases_the_tasks_worktree_and_branch(monkeypatch):
    from unified_agent.tools import tasks as tasks_tool

    task_id, branch = _task_with_branch(worktree_path=".worktrees/task-x")
    _write_cursor(_merged_pr(branch))
    calls = []

    def fake_release(path, branch=None, project=None, notes=None):
        calls.append((path, branch))
        return "removed"

    monkeypatch.setattr(tasks_tool, "_release_task_worktree", fake_release)
    result = queue_closure.close_on_merge(dry_run=False)
    assert result["closed"][0]["worktree"] == "removed"
    assert calls == [(".worktrees/task-x", branch)]
    row = _row(task_id)
    assert row["status"] == "done" and row["worktree_path"] is None and row["branch"] is None


def test_a_worktree_holding_work_is_kept_but_the_close_stands(monkeypatch):
    from unified_agent.tools import tasks as tasks_tool

    task_id, branch = _task_with_branch(worktree_path=".worktrees/task-y")
    _write_cursor(_merged_pr(branch))
    monkeypatch.setattr(
        tasks_tool, "_release_task_worktree", lambda *a, **k: "retained:uncommitted changes"
    )
    result = queue_closure.close_on_merge(dry_run=False)
    assert result["closed"][0]["worktree"] == "retained:uncommitted changes"
    row = _row(task_id)
    assert row["status"] == "done" and row["worktree_path"] == ".worktrees/task-y"


def test_a_task_reopened_before_release_keeps_its_new_worktree(monkeypatch):
    from unified_agent.tools import tasks as tasks_tool

    task_id, branch = _task_with_branch(worktree_path=".worktrees/task-z")
    _write_cursor(_merged_pr(branch))
    calls = []
    monkeypatch.setattr(
        tasks_tool, "_release_task_worktree", lambda *a, **k: calls.append(a) or "removed"
    )
    real_release = queue_closure._release_closed_worktree

    def reopen_then_release(tid):
        _set(tid, status="doing")  # reopened and reclaimed between close and release
        return real_release(tid)

    monkeypatch.setattr(queue_closure, "_release_closed_worktree", reopen_then_release)
    result = queue_closure.close_on_merge(dry_run=False)
    assert result["closed"][0]["worktree"] == "skipped:reopened"
    assert calls == []
    assert _row(task_id)["worktree_path"] == ".worktrees/task-z"


def test_the_parked_view_carries_details_in_ranked_search_too():
    task_id = task_add("zebra crossing redesign")["id"]
    queue_closure.queue_park(task_id, "shelved for later", actor="test-agent")
    ranked = task_list(status="all", parked=True, query="zebra crossing")
    assert [t["id"] for t in ranked["tasks"]] == [task_id]
    assert ranked["tasks"][0]["parked"]["reason"] == "shelved for later"


def test_unpark_always_restores_the_children_folded_into_the_root():
    epic = task_add("epic")["id"]
    child = task_add("child", parent_id=epic)["id"]
    queue_closure.queue_park(epic, "shelved", actor="test-agent")
    with pytest.raises(TypeError):
        queue_closure.queue_unpark(epic, "back", include_folded=False, actor="test-agent")
    restored = queue_closure.queue_unpark(epic, "back", actor="test-agent")
    assert sorted(restored["restored"]) == sorted([epic, child])
    assert _row(child)["archived_at"] is None


def test_sweep_proposes_the_parent_and_folds_a_stale_child_even_when_the_child_has_a_lower_id():
    child = task_add("stale child")["id"]
    parent = task_add("stale parent")["id"]
    task_update(child, parent_id=parent)
    for task_id in (child, parent):
        _age(task_id, days=60)
    proposals = {p["task_id"]: p for p in queue_closure.queue_sweep()["proposals"]}
    assert proposals[parent]["proposed_action"] == "park"
    assert proposals[parent]["evidence"]["fold_children"] == [child]
    assert proposals[child]["proposed_action"] == "fold"
    assert len(queue_closure.queue_sweep()["proposals"]) == 2


def test_a_gate_list_that_continues_past_a_full_stop_keeps_every_gate():
    ids = queue_closure._gate_task_ids
    assert ids("[milestone-review] Gate: #1 done. #2 also. PASS needs #9.") == {1, 2}
    assert ids("Gate: #1, #2. PASS: #7 rehearsal.") == {1, 2}
    assert ids("Gate: #1 first\n#8 not part of it") == {1}
    assert ids("no gate here #5") == set()

    gate_done = task_add("gate done")["id"]
    task_update(gate_done, status="done", actor="test-agent")
    later_open = task_add("later gate, still open")["id"]
    milestone = task_add(
        "M", notes=f"[milestone-review] Gate: #{gate_done} done. #{later_open} also."
    )["id"]
    health = queue_closure.queue_health()
    assert milestone not in health["milestones"]["unjudged_ids"]
    assert health["milestones"]["review_ready"] == 0


def test_the_review_cli_dry_run_environment_overrides_apply(monkeypatch, capsys):
    from unified_agent import review_cli

    task_id, branch = _task_with_branch()
    _write_cursor(_merged_pr(branch))
    monkeypatch.setenv("UA_REVIEW_DRY_RUN", "1")
    assert review_cli.main(["queue", "close-merged", "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and _row(task_id)["status"] == "open"


def test_queue_health_reports_each_stuck_ownership_state_and_changes_nothing():
    def row(status, **cols):
        task_id = task_add(status)["id"]
        sets = ", ".join(f"{name}=?" for name in ("status", *cols))
        conn = storage.connect()
        try:
            conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (status, *cols.values(), task_id))
            conn.commit()
        finally:
            conn.close()
        return task_id

    past = "2000-01-01T00:00:00+00:00"
    future = "2999-01-01T00:00:00+00:00"
    dead = row("doing", claimed_by="server_agent2", lock_expires_at=past)
    void = row("open", next_owner="server_agent2")
    shelved = row("open", next_owner="server_agent2")
    sleeping = row("open", next_owner="server_agent2")
    healthy = row("open")
    conn = storage.connect()
    try:
        for task_id, until in ((shelved, past), (sleeping, future)):
            conn.execute(
                "INSERT INTO task_liveness (task_id, wait_reason, wait_until, updated_at) "
                "VALUES (?, 'ci', ?, ?)",
                (task_id, until, past),
            )
        conn.commit()
        before = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        before = [tuple(r) for r in before]
    finally:
        conn.close()

    report = queue_closure.queue_health()

    assert report["ownership_examples"]["dead"] == [dead]
    assert report["ownership_examples"]["void"] == [void]
    assert report["ownership_examples"]["shelved"] == [shelved]
    assert report["ownership_examples"]["sleeping"] == [sleeping]
    assert report["ownership_states"]["zombie"] == 0
    assert healthy not in sum(report["ownership_examples"].values(), [])
    conn = storage.connect()
    try:
        assert [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")] == before
    finally:
        conn.close()


def test_an_expired_deployment_wait_surfaces_in_queue_health():
    def wait(until):
        task_id = task_add("waiting on a deploy")["id"]
        conn = storage.connect()
        try:
            conn.execute(
                "INSERT INTO task_liveness (task_id, wait_reason, wait_until, wait_deploy_target, "
                "wait_deploy_sha, updated_at) VALUES (?, 'deploy', ?, 'hub', ?, ?)",
                (task_id, until, "a" * 40, until),
            )
            conn.commit()
        finally:
            conn.close()
        return task_id

    expired = wait("2000-01-01T00:00:00+00:00")
    waiting = wait("2999-01-01T00:00:00+00:00")
    report = queue_closure.queue_health()["deployment_waits"]
    assert [w["task_id"] for w in report["expired"]] == [expired]
    assert [w["task_id"] for w in report["waiting"]] == [waiting]
    assert report["expired"][0]["target"] == "hub"
