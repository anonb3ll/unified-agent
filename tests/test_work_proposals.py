"""Batch work proposals with frozen authority (task #2393).

An agent files a batch of work items; only the verified human operator
approves or rejects them, one item at a time. Approval freezes the item's
title, notes and scope into a contract digest and spawns one task carrying
that digest. A claim-time check recomputes it and fails closed on any
mismatch. An approval that stopped half-way (``approving``) recovers forward
when its task exists and rolls back to ``pending`` when it does not.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, events, storage, work_proposals
from unified_agent.tools import tasks

HUMAN = "Desktop_Person"
AGENT = "Desktop_Agent2"
OTHER_AGENT = "Desktop_Agent4"


@contextmanager
def _as(identity: str | None):
    token = config.agent_identity_var.set(identity)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


def _items(n: int = 2) -> list[dict]:
    return [
        {
            "title": f"Build piece {i}",
            "notes": f"Do part {i} of the thing.",
            "scope": {"repo": "anonb3ll/unified-agent", "files": [f"src/b{i}.py", "src/a.py"]},
            "priority": "med",
        }
        for i in range(n)
    ]


def _file(n: int = 2, **over) -> dict:
    with _as(AGENT):
        return work_proposals.work_proposal_file(
            title=over.pop("title", "Lane B batch"), items=_items(n), **over
        )


def _task(task_id: int):
    conn = storage.connect()
    try:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    finally:
        conn.close()


def _channels(proposal_id: str) -> list[str]:
    rows = events.query_events(channels=["agent.work_proposal.*"], limit=200)
    return [r["channel"] for r in rows if r["payload"].get("proposal_id") == proposal_id]


def test_agent_files_and_items_are_pending_with_digests():
    filed = _file()
    prop = filed["proposal"]
    assert prop["status"] == work_proposals.PENDING
    assert prop["proposer"] == AGENT
    assert prop["reporting_mode"] == "final_only"
    assert [i["status"] for i in prop["items"]] == ["pending", "pending"]
    item = prop["items"][0]
    # Scope files are canonical: deduplicated and sorted.
    assert item["scope"]["files"] == ["src/a.py", "src/b0.py"]
    assert item["contract_digest"] == work_proposals.contract_digest(
        item["title"], item["notes"], item["scope"]
    )
    assert _channels(prop["id"]) == ["agent.work_proposal.filed"]


def test_agent_cannot_file_as_someone_else():
    with _as(AGENT), pytest.raises(ToolError, match="cannot file"):
        work_proposals.work_proposal_file(title="x", items=_items(1), proposer=OTHER_AGENT)


@pytest.mark.parametrize(
    "bad, match",
    [
        ([], "at least one item"),
        ([{"title": ""}], "title"),
        ([{"title": "ok", "scope": {"repo": "r", "branch": "b"}}], "scope"),
        ([{"title": "ok", "priority": "urgent"}], "priority"),
        ([{"title": "ok", "surprise": 1}], "unknown item field"),
    ],
)
def test_file_refuses_malformed_items(bad, match):
    with _as(AGENT), pytest.raises(ToolError, match=match):
        work_proposals.work_proposal_file(title="x", items=bad)


def test_file_refuses_unknown_reporting_mode():
    with _as(AGENT), pytest.raises(ToolError, match="reporting_mode"):
        work_proposals.work_proposal_file(title="x", items=_items(1), reporting_mode="chatty")


def test_agent_cannot_approve_or_reject():
    pid = _file()["proposal"]["id"]
    for who in (AGENT, OTHER_AGENT):
        with _as(who), pytest.raises(ToolError, match="only the human operator"):
            work_proposals.work_proposal_decide(pid, [{"index": 0, "approve": False}])
    # An agent naming the human as approver is still the agent.
    with _as(AGENT), pytest.raises(ToolError, match="only the human operator"):
        work_proposals.work_proposal_decide(pid, [{"index": 0, "approve": True}], approver=HUMAN)
    assert work_proposals.work_proposal_get(pid)["proposal"]["status"] == "pending"


def test_unverified_caller_must_name_a_human_approver():
    pid = _file()["proposal"]["id"]
    with _as(None), pytest.raises(ToolError, match="only the human operator"):
        work_proposals.work_proposal_decide(pid, [{"index": 0, "approve": True}], approver=AGENT)
    with _as(None):
        out = work_proposals.work_proposal_decide(
            pid, [{"index": 0, "approve": False}], approver=HUMAN
        )
    assert out["proposal"]["items"][0]["decided_by"] == HUMAN


def test_human_approves_per_item_and_spawns_one_task_each():
    pid = _file(3)["proposal"]["id"]
    with _as(HUMAN):
        out = work_proposals.work_proposal_decide(
            pid,
            [{"index": 0, "approve": True}, {"index": 2, "approve": False}],
        )
    items = out["proposal"]["items"]
    assert [i["status"] for i in items] == ["approved", "pending", "rejected"]
    assert out["proposal"]["status"] == "pending"
    task = _task(items[0]["task_id"])
    assert task["title"] == "Build piece 0"
    assert f"[proposal: {pid}#0]" in task["notes"]
    assert f"[contract: {items[0]['contract_digest']}]" in task["notes"]
    assert "[reporting: final_only]" in task["notes"]
    assert items[0]["approving_by"] == HUMAN
    assert items[2]["task_id"] is None

    with _as(HUMAN):
        out = work_proposals.work_proposal_decide(pid, [{"index": 1, "approve": True}])
    assert out["proposal"]["status"] == "approved"
    assert _channels(pid) == [
        "agent.work_proposal.filed",
        "agent.work_proposal.item_rejected",
        "agent.work_proposal.item_approving",
        "agent.work_proposal.item_approved",
        "agent.work_proposal.item_approving",
        "agent.work_proposal.item_approved",
    ]


def test_all_rejected_rolls_up_to_rejected_and_decided_items_are_final():
    pid = _file(2)["proposal"]["id"]
    with _as(HUMAN):
        out = work_proposals.work_proposal_decide(
            pid, [{"index": 0, "approve": False}, {"index": 1, "approve": False}]
        )
        assert out["proposal"]["status"] == "rejected"
        with pytest.raises(ToolError, match="is rejected, not pending"):
            work_proposals.work_proposal_decide(pid, [{"index": 0, "approve": True}])


@pytest.mark.parametrize(
    "decisions, match",
    [
        ([], "at least one decision"),
        ([{"index": 5, "approve": True}], "no item 5"),
        ([{"index": 0, "approve": True}, {"index": 0, "approve": False}], "twice"),
        ([{"index": 0, "approve": "yes"}], "approve must be a boolean"),
        ([{"index": 0, "approve": False, "edits": {"title": "x"}}], "only an approval"),
        ([{"index": 0, "approve": True, "edits": {"owner": "x"}}], "edits may change"),
    ],
)
def test_decide_refuses_malformed_decisions(decisions, match):
    pid = _file(1)["proposal"]["id"]
    with _as(HUMAN), pytest.raises(ToolError, match=match):
        work_proposals.work_proposal_decide(pid, decisions)
    assert work_proposals.work_proposal_get(pid)["proposal"]["items"][0]["status"] == "pending"


def test_human_edit_at_approval_refreezes_the_digest():
    filed = _file(1)["proposal"]
    pid, before = filed["id"], filed["items"][0]["contract_digest"]
    edits = {"title": "Build piece 0, narrowed", "scope": {"repo": "o/r", "files": ["x.py"]}}
    with _as(HUMAN):
        out = work_proposals.work_proposal_decide(
            pid, [{"index": 0, "approve": True, "edits": edits}]
        )
    item = out["proposal"]["items"][0]
    assert item["title"] == "Build piece 0, narrowed"
    assert item["filed_digest"] == before
    assert item["contract_digest"] != before
    assert item["contract_digest"] == work_proposals.contract_digest(
        item["title"], item["notes"], {"repo": "o/r", "files": ["x.py"]}
    )
    assert item["edited"] is True
    task = _task(item["task_id"])
    assert task["title"] == "Build piece 0, narrowed"
    work_proposals.check_contract(task)


def test_check_contract_passes_and_ignores_non_proposal_tasks():
    pid = _file(1)["proposal"]["id"]
    with _as(HUMAN):
        out = work_proposals.work_proposal_decide(pid, [{"index": 0, "approve": True}])
    task_id = out["proposal"]["items"][0]["task_id"]
    # Notes appended after approval (a claim line, a comment) do not break it.
    tasks.task_update(task_id, append_note="progress: started")
    assert work_proposals.check_contract(_task(task_id)) is None
    plain = tasks.task_add("Unrelated task")["id"]
    assert work_proposals.check_contract(_task(plain)) is None


@pytest.mark.parametrize("field", ["title", "notes", "marker", "item_row"])
def test_check_contract_fails_closed_on_mismatch(field):
    pid = _file(1)["proposal"]["id"]
    with _as(HUMAN):
        out = work_proposals.work_proposal_decide(pid, [{"index": 0, "approve": True}])
    item = out["proposal"]["items"][0]
    row = dict(_task(item["task_id"]))
    if field == "title":
        row["title"] = "Something broader"
    elif field == "notes":
        row["notes"] = row["notes"].replace("Do part 0", "Do every part")
    elif field == "marker":
        row["notes"] = row["notes"].replace(item["contract_digest"], "0" * 64)
    else:
        conn = storage.connect()
        try:
            conn.execute(
                "UPDATE work_proposal_items SET scope_json=? WHERE proposal_id=? AND idx=0",
                ('{"files":["everything"],"repo":"o/r"}', pid),
            )
            conn.commit()
        finally:
            conn.close()
    with pytest.raises(ToolError, match="contract"):
        work_proposals.check_contract(row)


def test_check_contract_fails_closed_on_orphan_or_unapproved_marker():
    row = {"id": 999, "title": "t", "notes": "[contract: " + "a" * 64 + "]"}
    with pytest.raises(ToolError, match="no \\[proposal"):
        work_proposals.check_contract(row)
    row = {"id": 999, "title": "t", "notes": "[proposal: wp_000000000000#0]"}
    with pytest.raises(ToolError, match="unknown"):
        work_proposals.check_contract(row)


def test_withdraw_is_proposer_only_and_only_while_pending():
    pid = _file(2)["proposal"]["id"]
    with _as(OTHER_AGENT), pytest.raises(ToolError, match="only the proposer"):
        work_proposals.work_proposal_withdraw(pid)
    with _as(AGENT):
        out = work_proposals.work_proposal_withdraw(pid)
    assert out["proposal"]["status"] == "withdrawn"
    assert {i["status"] for i in out["proposal"]["items"]} == {"withdrawn"}
    with _as(HUMAN), pytest.raises(ToolError, match="withdrawn"):
        work_proposals.work_proposal_decide(pid, [{"index": 0, "approve": True}])

    pid2 = _file(2)["proposal"]["id"]
    with _as(HUMAN):
        work_proposals.work_proposal_decide(pid2, [{"index": 0, "approve": False}])
    with _as(AGENT), pytest.raises(ToolError, match="already has decided items"):
        work_proposals.work_proposal_withdraw(pid2)


def _stick_in_approving(pid: str, idx: int, *, age_s: int = 3600) -> None:
    """Put one item where a crashed approval leaves it."""
    conn = storage.connect()
    try:
        conn.execute(
            "UPDATE work_proposal_items SET status='approving', approving_by=?, "
            "approving_at=? WHERE proposal_id=? AND idx=?",
            (HUMAN, work_proposals._iso_ago(age_s), pid, idx),
        )
        conn.commit()
    finally:
        conn.close()


def test_recover_stale_rolls_back_when_no_task_exists():
    pid = _file(2)["proposal"]["id"]
    _stick_in_approving(pid, 0)
    _stick_in_approving(pid, 1, age_s=10)  # too fresh to touch
    out = work_proposals.work_proposal_recover()
    assert out["rolled_back"] == [{"proposal_id": pid, "index": 0}]
    assert out["recovered_forward"] == []
    items = work_proposals.work_proposal_get(pid)["proposal"]["items"]
    assert [i["status"] for i in items] == ["pending", "approving"]
    assert items[0]["approving_by"] is None
    assert work_proposals.work_proposal_recover() == {
        "recovered_forward": [],
        "rolled_back": [],
    }


def test_recover_stale_moves_forward_when_the_task_exists(monkeypatch):
    pid = _file(1)["proposal"]["id"]

    def crash_after_task(*args, **kwargs):
        raise RuntimeError("hub died after the task commit")

    # The task is created; the approved write never happens.
    with monkeypatch.context() as m, _as(HUMAN), pytest.raises(RuntimeError):
        m.setattr(work_proposals, "_mark_approved", crash_after_task)
        work_proposals.work_proposal_decide(pid, [{"index": 0, "approve": True}])
    item = work_proposals.work_proposal_get(pid)["proposal"]["items"][0]
    assert item["status"] == "approving"
    out = work_proposals.work_proposal_recover(older_than_s=0)
    assert len(out["recovered_forward"]) == 1
    task_id = out["recovered_forward"][0]["task_id"]
    prop = work_proposals.work_proposal_get(pid)["proposal"]
    assert prop["status"] == "approved"
    assert prop["items"][0]["task_id"] == task_id
    assert "agent.work_proposal.item_recovered" in _channels(pid)
    # Re-running is a no-op, and no second task was spawned.
    assert work_proposals.work_proposal_recover(older_than_s=0)["recovered_forward"] == []
    conn = storage.connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE instr(notes, ?) > 0", (f"[proposal: {pid}#0]",)
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_failed_task_spawn_rolls_back_the_unspawned_items(monkeypatch):
    pid = _file(3)["proposal"]["id"]
    real = work_proposals._spawn_task

    def refuse_second(proposal_id, row, mode, *, untrusted):
        if row["idx"] == 1:
            raise ToolError("task store refused")
        return real(proposal_id, row, mode, untrusted=untrusted)

    monkeypatch.setattr(work_proposals, "_spawn_task", refuse_second)
    decisions = [{"index": i, "approve": True} for i in range(3)]
    with _as(HUMAN), pytest.raises(ToolError, match="task store refused"):
        work_proposals.work_proposal_decide(pid, decisions)
    items = work_proposals.work_proposal_get(pid)["proposal"]["items"]
    assert [i["status"] for i in items] == ["approved", "pending", "pending"]
    assert "agent.work_proposal.item_rolled_back" in _channels(pid)


def test_list_filters_by_status_and_proposer():
    a = _file(1)["proposal"]["id"]
    with _as(OTHER_AGENT):
        b = work_proposals.work_proposal_file(title="other", items=_items(1))["proposal"]["id"]
    with _as(HUMAN):
        work_proposals.work_proposal_decide(b, [{"index": 0, "approve": False}])
    ids = [p["id"] for p in work_proposals.work_proposal_list()["proposals"]]
    assert set(ids) == {a, b}
    pending = work_proposals.work_proposal_list(status="pending")["proposals"]
    assert [p["id"] for p in pending] == [a]
    mine = work_proposals.work_proposal_list(proposer=OTHER_AGENT)["proposals"]
    assert [p["id"] for p in mine] == [b]


def test_another_tenant_sees_nothing(monkeypatch):
    pid = _file(1)["proposal"]["id"]
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_b")
    with pytest.raises(ToolError, match="unknown work proposal"):
        work_proposals.work_proposal_get(pid)
    assert work_proposals.work_proposal_list()["proposals"] == []
    with _as(HUMAN), pytest.raises(ToolError, match="unknown work proposal"):
        work_proposals.work_proposal_decide(pid, [{"index": 0, "approve": True}])


# --- Wiring: claim check, operator route, hygiene (task #2393) --------------------


def test_task_claim_refuses_a_proposal_task_that_drifted_from_its_contract():
    pid = _file(2)["proposal"]["id"]
    with _as(HUMAN):
        out = work_proposals.work_proposal_decide(
            pid, [{"index": 0, "approve": True}, {"index": 1, "approve": True}]
        )
    good, drifted = (item["task_id"] for item in out["proposal"]["items"])
    assert tasks.task_claim(good, agent=AGENT)["claimed_by"] == AGENT
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET title='Something else' WHERE id=?", (drifted,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ToolError):
        tasks.task_claim(drifted, agent=AGENT)
    assert _task(drifted)["claimed_by"] is None


@pytest.fixture
def control_center(monkeypatch):
    import secrets

    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from unified_agent import server, webui

    tokens = {HUMAN: f"tok-{secrets.token_urlsafe(8)}", AGENT: f"tok-{secrets.token_urlsafe(8)}"}
    monkeypatch.setenv("UA_AGENT_TOKENS", ",".join(f"{k}:{v}" for k, v in tokens.items()))
    monkeypatch.setenv("UA_HTTP_ALLOWED_HOSTS", "127.0.0.1:*")
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    storage.init_db()
    app = server.HubAuthASGIMiddleware(Starlette(routes=webui.web_routes()))
    with TestClient(app) as client:
        yield client, tokens


def test_the_operator_decides_through_the_control_center_route(control_center):
    client, tokens = control_center
    pid = _file(1)["proposal"]["id"]
    route = "/api/action/work-proposal-decide"
    body = {"proposal_id": pid, "decisions": [{"index": 0, "approve": True}]}

    agent = client.post(route, json=body, headers={"Authorization": f"Bearer {tokens[AGENT]}"})
    assert agent.status_code == 403, agent.json()
    extra = client.post(
        route,
        json={**body, "approver": HUMAN},
        headers={"Authorization": f"Bearer {tokens[HUMAN]}"},
    )
    assert extra.status_code == 400

    human = client.post(route, json=body, headers={"Authorization": f"Bearer {tokens[HUMAN]}"})
    assert human.status_code == 200, human.json()
    item = human.json()["proposal"]["items"][0]
    assert item["status"] == "approved" and item["decided_by"] == HUMAN
    assert _task(item["task_id"]) is not None
