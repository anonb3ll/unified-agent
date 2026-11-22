"""Joint planning contract (task #1455).

Two agents share one plan: propose, review, revise. Concurrent revisions
conflict instead of merging. A new connection resumes the same plan. Membership
comes from the checked-in org chart. Goals and comments are linked by id.
"""

from __future__ import annotations

import contextlib
import threading

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from org_test_helpers import installation_org as installation_org

from unified_agent import config, profiles, storage
from unified_agent.goals import goal_add
from unified_agent.joint_planning import JointConflict
from unified_agent.task_comments import append_comment
from unified_agent.tools.plan import (
    plan_checkpoint,
    plan_close,
    plan_create,
    plan_delete,
    plan_get,
    plan_handoff,
    plan_manage,
    plan_resume,
    plan_review,
    plan_step_claim,
    plan_step_retry,
    plan_update,
)
from unified_agent.tools.tasks import task_add

OFFICE = "product-engineering"
MEMBER = "Desktop_Agent4"
LEAD = "Desktop_Agent7"
OUTSIDER = "Desktop_Agent5"


@contextlib.contextmanager
def _as(identity: str):
    token = config.agent_identity_var.set(identity)
    try:
        yield
    finally:
        config.agent_identity_var.reset(token)


pytestmark = pytest.mark.usefixtures("installation_org")


def _tables() -> set[str]:
    conn = storage.connect()
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _comment(task_id: int, body: str, author: str, plan_id: str | None = None) -> int:
    refs = {"plan_id": plan_id} if plan_id else None
    posted = append_comment(task_id, body, author=author, refs=refs)
    return int(posted["comment"]["id"])


def _setup() -> dict:
    parent = goal_add("Share one plan", author=MEMBER)
    goal = goal_add("Revise it together", parent_goal_id=parent["goal"]["id"], author=MEMBER)
    task = task_add(title="Joint plan thread", notes="spec stays in notes")
    created = plan_create("Decompose the goal once", ["inspect", "ship"], author=MEMBER)
    plan_id = created["plan_id"]
    return {
        "goal_id": int(goal["goal"]["id"]),
        "parent_goal_id": int(parent["goal"]["id"]),
        "task_id": int(task["id"]),
        "plan_id": plan_id,
    }


def _bind(world: dict, author: str = MEMBER) -> dict:
    comment_id = _comment(world["task_id"], "bind the plan", author, world["plan_id"])
    return plan_manage(
        "joint_bind",
        {
            "plan_id": world["plan_id"],
            "goal_id": world["goal_id"],
            "org_unit_id": OFFICE,
            "comment_id": comment_id,
            "author": author,
        },
    )


def test_two_agents_propose_review_and_revise():
    before = _tables()
    world = _setup()
    bound = _bind(world)
    assert bound["revision"] == 0
    assert bound["team"]
    assert any(row["identity"] == LEAD and row["role"] == "lead" for row in bound["team"])
    assert any(row["identity"] == MEMBER and row["role"] == "member" for row in bound["team"])
    assert [item["id"] for item in bound["why_chain"]] == [
        world["parent_goal_id"],
        world["goal_id"],
    ]

    proposal_comment = _comment(world["task_id"], "propose steps", MEMBER, world["plan_id"])
    proposed = plan_manage(
        "joint_propose",
        {
            "plan_id": world["plan_id"],
            "expected_revision": 0,
            "steps": ["inspect seams", "ship the contract"],
            "comment_id": proposal_comment,
            "author": MEMBER,
        },
    )
    assert proposed["revision"] == 1
    assert proposed["pending"]["author"] == MEMBER
    assert proposed["pending"]["status"] == "proposed"
    assert [step["description"] for step in proposed["steps"]] == ["inspect", "ship"]

    review_comment = _comment(world["task_id"], "accept", LEAD, world["plan_id"])
    accepted = plan_manage(
        "joint_review",
        {
            "plan_id": world["plan_id"],
            "expected_revision": 1,
            "decision": "accept",
            "comment_id": review_comment,
            "author": LEAD,
        },
    )
    assert accepted["revision"] == 2
    assert accepted["pending"] is None
    assert accepted["role"] == "lead"
    assert [step["description"] for step in accepted["steps"]] == [
        "inspect seams",
        "ship the contract",
    ]

    change_comment = _comment(world["task_id"], "needs a recovery step", LEAD, world["plan_id"])
    revise_comment = _comment(world["task_id"], "add recovery", MEMBER, world["plan_id"])
    revised = plan_manage(
        "joint_propose",
        {
            "plan_id": world["plan_id"],
            "expected_revision": 2,
            "steps": ["inspect seams", "ship the contract", "recover conflicts"],
            "comment_id": revise_comment,
            "author": MEMBER,
        },
    )
    changes = plan_manage(
        "joint_review",
        {
            "plan_id": world["plan_id"],
            "expected_revision": revised["revision"],
            "decision": "changes",
            "comment_id": change_comment,
            "author": LEAD,
        },
    )
    assert changes["pending"]["status"] == "changes_requested"
    assert [step["description"] for step in changes["steps"]] == [
        "inspect seams",
        "ship the contract",
    ]

    final_comment = _comment(world["task_id"], "revised proposal", MEMBER, world["plan_id"])
    again = plan_manage(
        "joint_propose",
        {
            "plan_id": world["plan_id"],
            "expected_revision": changes["revision"],
            "steps": ["inspect seams", "ship the contract", "record the decision"],
            "comment_id": final_comment,
            "author": MEMBER,
        },
    )
    accept_comment = _comment(world["task_id"], "accept revision", LEAD, world["plan_id"])
    final = plan_manage(
        "joint_review",
        {
            "plan_id": world["plan_id"],
            "expected_revision": again["revision"],
            "decision": "accept",
            "comment_id": accept_comment,
            "author": LEAD,
        },
    )
    decision_comment = _comment(world["task_id"], "we ship one plan", LEAD, world["plan_id"])
    decided = plan_manage(
        "joint_decide",
        {
            "plan_id": world["plan_id"],
            "expected_revision": final["revision"],
            "text": "One active plan per goal; peers review each other's proposals.",
            "comment_id": decision_comment,
            "author": LEAD,
        },
    )
    assert decided["decisions"][-1]["goal_id"] == world["goal_id"]
    assert decided["decisions"][-1]["comment_id"] == decision_comment
    assert decision_comment in decided["comment_ids"]
    assert _tables() == before


def test_concurrent_revisions_fail_and_the_loser_recovers():
    world = _setup()
    _bind(world)
    barrier = threading.Barrier(2)
    results: list[tuple[str, object]] = []

    def worker(author: str, body: str) -> None:
        barrier.wait()
        comment_id = _comment(world["task_id"], body, author, world["plan_id"])
        try:
            proposed = plan_manage(
                "joint_propose",
                {
                    "plan_id": world["plan_id"],
                    "expected_revision": 0,
                    "steps": [body],
                    "comment_id": comment_id,
                    "author": author,
                },
            )
        except JointConflict as exc:
            results.append(("conflict", exc))
        else:
            results.append(("ok", proposed))

    threads = [
        threading.Thread(target=worker, args=(MEMBER, "cursor proposal")),
        threading.Thread(target=worker, args=(LEAD, "devin proposal")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(kind for kind, _ in results) == ["conflict", "ok"]
    conflict = next(exc for kind, exc in results if kind == "conflict")
    assert isinstance(conflict, JointConflict)
    assert conflict.current_revision == 1
    assert "conflict" in str(conflict)
    winner = next(payload for kind, payload in results if kind == "ok")
    assert winner["revision"] == 1

    recovered = plan_manage("joint_recover", {"plan_id": world["plan_id"], "author": MEMBER})
    assert recovered["recovered"] is True
    assert recovered["revision"] == 1
    assert recovered["pending"]["author"] == winner["pending"]["author"]
    assert [step["description"] for step in recovered["steps"]] == ["inspect", "ship"]

    loser = LEAD if winner["pending"]["author"] == MEMBER else MEMBER
    with pytest.raises(ToolError, match="awaiting review"):
        plan_manage(
            "joint_propose",
            {
                "plan_id": world["plan_id"],
                "expected_revision": recovered["revision"],
                "steps": ["a different decomposition"],
                "comment_id": _comment(world["task_id"], "late", loser, world["plan_id"]),
                "author": loser,
            },
        )
    review = plan_manage(
        "joint_review",
        {
            "plan_id": world["plan_id"],
            "expected_revision": recovered["revision"],
            "decision": "accept",
            "comment_id": _comment(world["task_id"], "peer accepts", loser, world["plan_id"]),
            "author": loser,
        },
    )
    assert review["steps"][0]["description"] == winner["pending"]["steps"][0]["description"]


def test_stale_revision_does_not_apply():
    world = _setup()
    _bind(world)
    plan_manage(
        "joint_propose",
        {
            "plan_id": world["plan_id"],
            "expected_revision": 0,
            "steps": ["kept"],
            "comment_id": _comment(world["task_id"], "first", MEMBER, world["plan_id"]),
            "author": MEMBER,
        },
    )
    with pytest.raises(JointConflict, match="expected 0, current 1"):
        plan_manage(
            "joint_propose",
            {
                "plan_id": world["plan_id"],
                "expected_revision": 0,
                "steps": ["lost"],
                "comment_id": _comment(world["task_id"], "second", MEMBER, world["plan_id"]),
                "author": MEMBER,
            },
        )
    view = plan_manage("joint_get", {"plan_id": world["plan_id"], "author": MEMBER})
    assert view["pending"]["steps"][0]["description"] == "kept"


def test_restart_resumes_the_same_plan():
    world = _setup()
    bound = _bind(world)
    proposed = plan_manage(
        "joint_propose",
        {
            "plan_id": world["plan_id"],
            "expected_revision": bound["revision"],
            "steps": ["inspect seams", "ship the contract"],
            "comment_id": _comment(world["task_id"], "propose", MEMBER, world["plan_id"]),
            "author": MEMBER,
        },
    )
    accepted = plan_manage(
        "joint_review",
        {
            "plan_id": world["plan_id"],
            "expected_revision": proposed["revision"],
            "decision": "accept",
            "comment_id": _comment(world["task_id"], "accept", LEAD, world["plan_id"]),
            "author": LEAD,
        },
    )
    decided = plan_manage(
        "joint_decide",
        {
            "plan_id": world["plan_id"],
            "expected_revision": accepted["revision"],
            "text": "Resume from the checkpoint, not from memory.",
            "comment_id": _comment(world["task_id"], "decision", LEAD, world["plan_id"]),
            "author": LEAD,
        },
    )
    with _as(MEMBER):
        resumed = plan_resume(world["plan_id"])
    recovered = plan_manage("joint_recover", {"plan_id": world["plan_id"], "author": MEMBER})
    assert resumed["plan"]["id"] == world["plan_id"]
    assert recovered["revision"] == decided["revision"]
    assert recovered["goal_id"] == world["goal_id"]
    assert [step["description"] for step in recovered["steps"]] == [
        "inspect seams",
        "ship the contract",
    ]
    assert recovered["decisions"][-1]["text"] == decided["decisions"][-1]["text"]
    conn = storage.connect()
    try:
        row = conn.execute(
            """
            SELECT json_extract(context, '$.joint.revision') AS revision,
                   json_extract(context, '$.joint.goal_id') AS goal_id
            FROM plan_checkpoints
            WHERE plan_id=? AND json_extract(context, '$.joint.revision') IS NOT NULL
            ORDER BY seq DESC LIMIT 1
            """,
            (world["plan_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert int(row["revision"]) == decided["revision"]
    assert int(row["goal_id"]) == world["goal_id"]


def test_membership_is_scoped_to_the_bound_office():
    world = _setup()
    with pytest.raises(ToolError, match="scoped membership"):
        _bind(world, author=OUTSIDER)
    bound = _bind(world)
    assert bound["org_unit_id"] == OFFICE
    with pytest.raises(ToolError, match="scoped membership"):
        plan_manage(
            "joint_propose",
            {
                "plan_id": world["plan_id"],
                "expected_revision": 0,
                "steps": ["nope"],
                "comment_id": _comment(world["task_id"], "outsider", OUTSIDER, world["plan_id"]),
                "author": OUTSIDER,
            },
        )
    proposed = plan_manage(
        "joint_propose",
        {
            "plan_id": world["plan_id"],
            "expected_revision": 0,
            "steps": ["inspect"],
            "comment_id": _comment(world["task_id"], "own proposal", MEMBER, world["plan_id"]),
            "author": MEMBER,
        },
    )
    with pytest.raises(ToolError, match="different member"):
        plan_manage(
            "joint_review",
            {
                "plan_id": world["plan_id"],
                "expected_revision": proposed["revision"],
                "decision": "accept",
                "comment_id": _comment(world["task_id"], "self review", MEMBER, world["plan_id"]),
                "author": MEMBER,
            },
        )


def test_second_active_plan_for_the_same_goal_is_refused():
    world = _setup()
    _bind(world)
    other = plan_create("Another decomposition", ["duplicate"], author=LEAD)
    with pytest.raises(ToolError, match="already has active joint plan"):
        plan_manage(
            "joint_bind",
            {
                "plan_id": other["plan_id"],
                "goal_id": world["goal_id"],
                "org_unit_id": OFFICE,
                "author": LEAD,
            },
        )


def test_replan_keeps_doing_and_done_steps():
    world = _setup()
    _bind(world)
    plan_step_claim(world["plan_id"], 0, MEMBER)
    proposed = plan_manage(
        "joint_propose",
        {
            "plan_id": world["plan_id"],
            "expected_revision": 0,
            "steps": ["inspect", "ship", "recover"],
            "comment_id": _comment(world["task_id"], "extend", LEAD, world["plan_id"]),
            "author": LEAD,
        },
    )
    accepted = plan_manage(
        "joint_review",
        {
            "plan_id": world["plan_id"],
            "expected_revision": proposed["revision"],
            "decision": "accept",
            "comment_id": _comment(world["task_id"], "accept extend", MEMBER, world["plan_id"]),
            "author": MEMBER,
        },
    )
    assert accepted["steps"][0]["status"] == "doing"
    assert accepted["steps"][0]["owner"] == MEMBER
    assert accepted["steps"][2]["description"] == "recover"
    dropping = plan_manage(
        "joint_propose",
        {
            "plan_id": world["plan_id"],
            "expected_revision": accepted["revision"],
            "steps": ["replaced"],
            "comment_id": _comment(world["task_id"], "drop", LEAD, world["plan_id"]),
            "author": LEAD,
        },
    )
    with pytest.raises(ToolError, match="doing or done"):
        plan_manage(
            "joint_review",
            {
                "plan_id": world["plan_id"],
                "expected_revision": dropping["revision"],
                "decision": "accept",
                "comment_id": _comment(world["task_id"], "bad accept", MEMBER, world["plan_id"]),
                "author": MEMBER,
            },
        )
    view = plan_manage("joint_get", {"plan_id": world["plan_id"], "author": MEMBER})
    assert view["steps"][0]["status"] == "doing"
    assert view["revision"] == dropping["revision"]


def test_unknown_goal_and_comment_are_refused():
    world = _setup()
    with pytest.raises(ToolError, match="unknown goal"):
        plan_manage(
            "joint_bind",
            {
                "plan_id": world["plan_id"],
                "goal_id": 999999,
                "org_unit_id": OFFICE,
                "author": MEMBER,
            },
        )
    _bind(world)
    with pytest.raises(ToolError, match="unknown comment"):
        plan_manage(
            "joint_decide",
            {
                "plan_id": world["plan_id"],
                "expected_revision": 0,
                "text": "missing thread",
                "comment_id": 999999,
                "author": MEMBER,
            },
        )


def _propose(world: dict, revision: int, steps: list, author: str) -> dict:
    return plan_manage(
        "joint_propose",
        {
            "plan_id": world["plan_id"],
            "expected_revision": revision,
            "steps": steps,
            "comment_id": _comment(world["task_id"], "proposal", author, world["plan_id"]),
            "author": author,
        },
    )


def _review(world: dict, revision: int, decision: str, author: str) -> dict:
    return plan_manage(
        "joint_review",
        {
            "plan_id": world["plan_id"],
            "expected_revision": revision,
            "decision": decision,
            "comment_id": _comment(world["task_id"], "review", author, world["plan_id"]),
            "author": author,
        },
    )


def test_accept_keeps_history_of_unchanged_steps():
    world = _setup()
    _bind(world)
    with _as(MEMBER):
        plan_update(world["plan_id"], 1, owner=LEAD, note="keep me")
    with _as(MEMBER):
        plan_step_retry(world["plan_id"], 0, "flaky")
    proposed = _propose(world, 0, ["inspect", "ship", "recover"], MEMBER)
    accepted = _review(world, proposed["revision"], "accept", LEAD)
    first, second, third = accepted["steps"]
    assert first["status"] == "failed"
    assert first["retries"] == 1
    assert "flaky" in first["note"]
    assert second["owner"] == LEAD
    assert second["note"] == "keep me"
    assert third["description"] == "recover"
    assert third["retries"] == 0


def test_live_step_edit_after_proposal_blocks_accept():
    world = _setup()
    _bind(world)
    proposed = _propose(world, 0, ["inspect", "ship differently"], MEMBER)
    with _as(MEMBER):
        plan_update(world["plan_id"], 1, note="edited after the proposal")
    with pytest.raises(JointConflict, match="live steps changed"):
        _review(world, proposed["revision"], "accept", LEAD)
    view = plan_manage("joint_get", {"plan_id": world["plan_id"], "author": MEMBER})
    assert view["steps"][1]["note"] == "edited after the proposal"
    assert view["steps"][1]["description"] == "ship"
    assert view["revision"] == proposed["revision"]
    again = _propose(world, view["revision"], ["inspect", "ship differently"], MEMBER)
    accepted = _review(world, again["revision"], "accept", LEAD)
    assert accepted["steps"][1]["description"] == "ship differently"


def test_joint_revisions_publish_to_the_live_bus(monkeypatch):
    from unified_agent import events

    published: list[dict] = []
    monkeypatch.setattr(events, "publish", lambda event, *a, **k: published.append(event))
    world = _setup()
    _bind(world)
    _propose(world, 0, ["inspect", "ship"], MEMBER)
    channels = [event.get("channel") for event in published]
    assert "plan.joint.bound" in channels
    assert "plan.joint.proposed" in channels


def test_large_plan_can_replan_around_a_late_active_step():
    world = _setup()
    big = plan_create("Big plan", [f"step {i}" for i in range(45)], author=MEMBER)
    world["plan_id"] = big["plan_id"]
    goal = goal_add("A big goal", author=MEMBER)
    world["goal_id"] = int(goal["goal"]["id"])
    _bind(world)
    plan_step_claim(world["plan_id"], 44, MEMBER)
    steps = [f"step {i}" for i in range(45)] + ["step 45"]
    proposed = _propose(world, 0, steps, LEAD)
    accepted = _review(world, proposed["revision"], "accept", MEMBER)
    assert accepted["steps"][44]["status"] == "doing"
    assert len(accepted["steps"]) == 46


def _team_registry(tmp_path, members: list[str]) -> str:
    path = tmp_path / "org.yaml"
    rows = ", ".join(members)
    path.write_text(
        "schema_version: 1\n"
        "offices:\n"
        "  - id: joint-team\n"
        "    name: Joint team\n"
        f"    lead: {members[0]}\n"
        f"    members: [{rows}]\n",
        encoding="utf-8",
    )
    return str(path)


def test_former_member_proposal_cannot_be_accepted_and_can_be_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "UA_ORG_REGISTRY_PATH", _team_registry(tmp_path, [LEAD, MEMBER, "Desktop_Agent2"])
    )
    world = _setup()
    comment_id = _comment(world["task_id"], "bind", LEAD, world["plan_id"])
    plan_manage(
        "joint_bind",
        {
            "plan_id": world["plan_id"],
            "goal_id": world["goal_id"],
            "org_unit_id": "joint-team",
            "comment_id": comment_id,
            "author": LEAD,
        },
    )
    proposed = _propose(world, 0, ["inspect", "ship", "departed"], MEMBER)
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", _team_registry(tmp_path, [LEAD, "Desktop_Agent2"]))
    with pytest.raises(ToolError, match="no longer a member"):
        _review(world, proposed["revision"], "accept", LEAD)
    replaced = _propose(world, proposed["revision"], ["inspect", "ship", "kept"], "Desktop_Agent2")
    assert replaced["pending"]["author"] == "Desktop_Agent2"
    accepted = _review(world, replaced["revision"], "accept", LEAD)
    assert accepted["steps"][2]["description"] == "kept"


def test_single_member_office_cannot_bind(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_ORG_REGISTRY_PATH", _team_registry(tmp_path, [MEMBER]))
    world = _setup()
    with pytest.raises(ToolError, match="at least two members"):
        plan_manage(
            "joint_bind",
            {
                "plan_id": world["plan_id"],
                "goal_id": world["goal_id"],
                "org_unit_id": "joint-team",
                "author": MEMBER,
            },
        )


def test_comment_must_be_authored_by_the_actor():
    world = _setup()
    _bind(world)
    foreign = _comment(world["task_id"], "someone else said this", LEAD, world["plan_id"])
    with pytest.raises(ToolError, match="not authored by"):
        plan_manage(
            "joint_decide",
            {
                "plan_id": world["plan_id"],
                "expected_revision": 0,
                "text": "borrowed provenance",
                "comment_id": foreign,
                "author": MEMBER,
            },
        )


def test_escalated_plan_accepts_a_recovery_revision():
    world = _setup()
    _bind(world)
    with _as(MEMBER):
        for _ in range(10):
            retried = plan_step_retry(world["plan_id"], 1, "keeps failing")
            if retried["escalated"]:
                break
    assert retried["plan"]["status"] == "blocked"
    proposed = _propose(world, 0, ["inspect", "ship another way"], MEMBER)
    accepted = _review(world, proposed["revision"], "accept", LEAD)
    assert accepted["steps"][1]["status"] == "open"
    with _as(MEMBER):
        resumed = plan_resume(world["plan_id"])
    assert resumed["plan"]["status"] == "active"
    assert resumed["plan"]["needs_human"] is False


def test_joint_checkpoints_keep_resume_context():
    world = _setup()
    plan_checkpoint(world["plan_id"], memory_ids=["note-1"], file_paths=["src/a.py"])
    _bind(world)
    _propose(world, 0, ["inspect", "ship"], MEMBER)
    with _as(MEMBER):
        context = plan_resume(world["plan_id"])["checkpoint"]["context"]
    assert context["memory_ids"] == ["note-1"]
    assert context["file_paths"] == ["src/a.py"]


def test_mutation_returns_its_own_revision(monkeypatch):
    from unified_agent import events

    world = _setup()
    _bind(world)
    racing = {"done": False}

    def publish(event, *args, **kwargs):
        if event.get("channel") == "plan.joint.proposed" and not racing["done"]:
            racing["done"] = True
            plan_manage(
                "joint_decide",
                {
                    "plan_id": world["plan_id"],
                    "expected_revision": 1,
                    "text": "landed right after",
                    "comment_id": _comment(world["task_id"], "race", LEAD, world["plan_id"]),
                    "author": LEAD,
                },
            )
        return True

    monkeypatch.setattr(events, "publish", publish)
    proposed = _propose(world, 0, ["inspect", "ship"], MEMBER)
    assert racing["done"]
    assert proposed["revision"] == 1
    assert proposed["action"] == "propose"
    assert proposed["decisions"] == []


def test_joint_reads_are_scoped_to_the_office():
    world = _setup()
    _bind(world)
    for action in ("joint_get", "joint_recover"):
        with pytest.raises(ToolError, match="scoped membership refused"):
            plan_manage(action, {"plan_id": world["plan_id"], "author": OUTSIDER})
    with _as(OUTSIDER), pytest.raises(ToolError, match="scoped membership refused"):
        plan_resume(world["plan_id"])


def test_legacy_plan_actions_are_scoped_to_the_office():
    world = _setup()
    _bind(world)
    with pytest.raises(ToolError, match="caller identity is required"):
        plan_update(world["plan_id"], 0, note="anonymous")
    with pytest.raises(ToolError, match="scoped membership refused"):
        plan_step_claim(world["plan_id"], 0, OUTSIDER)
    with _as(OUTSIDER):
        for call in (
            lambda: plan_update(world["plan_id"], 0, status="done"),
            lambda: plan_close(world["plan_id"], "not mine"),
            lambda: plan_delete(world["plan_id"]),
        ):
            with pytest.raises(ToolError, match="scoped membership refused"):
                call()
    with _as(LEAD):
        updated = plan_update(world["plan_id"], 0, note="member edit")
    assert updated["step"]["note"] == "member edit"


def test_accept_keeps_step_ids_of_unchanged_steps():
    world = _setup()
    _bind(world)
    with _as(MEMBER):
        plan_step_claim(world["plan_id"], 0, MEMBER)
        before = [step["id"] for step in plan_resume(world["plan_id"])["steps"]]
    proposed = _propose(world, 0, ["inspect", "ship", "announce"], MEMBER)
    accepted = _review(world, proposed["revision"], "accept", LEAD)
    after = [step["id"] for step in accepted["steps"]]
    assert after[:2] == before
    assert after[2] not in before


def test_outsider_cannot_approve_a_joint_plan_gate():
    world = _setup()
    _bind(world)
    with _as(OUTSIDER), pytest.raises(ToolError, match="scoped membership refused"):
        plan_review(f"plan-review v1 approve {world['plan_id']}", world["task_id"])


def test_membership_check_and_write_share_one_transaction(monkeypatch):
    from unified_agent import joint_planning

    world = _setup()
    bind_comment = _comment(world["task_id"], "bind", MEMBER, world["plan_id"])
    original = joint_planning.require_joint_member
    state: dict = {}

    def bind_meanwhile():
        state["bound"] = plan_manage(
            "joint_bind",
            {
                "plan_id": world["plan_id"],
                "goal_id": world["goal_id"],
                "org_unit_id": OFFICE,
                "comment_id": bind_comment,
                "author": MEMBER,
            },
        )

    def racing_check(plan_id, claimed=None, *, conn=None):
        original(plan_id, claimed, conn=conn)
        if conn is not None and "thread" not in state:
            state["thread"] = threading.Thread(target=bind_meanwhile)
            state["thread"].start()
            state["thread"].join(0.5)
            state["blocked"] = state["thread"].is_alive()

    monkeypatch.setattr(joint_planning, "require_joint_member", racing_check)
    with _as(OUTSIDER):
        plan_update(world["plan_id"], 0, note="landed before the bind")
    state["thread"].join(10)
    assert state["blocked"] is True
    assert state["bound"]["revision"] == 0
    assert state["bound"]["steps"][0]["note"] == "landed before the bind"


def _checkpoint_count(plan_id: str) -> int:
    conn = storage.connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM plan_checkpoints WHERE plan_id=?", (plan_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_explicit_agent_handoff_keeps_its_checkpoint():
    world = _setup()
    _bind(world)
    plan_step_claim(world["plan_id"], 0, MEMBER)
    before = _checkpoint_count(world["plan_id"])
    plan_handoff(world["plan_id"], 0, outputs={"result": "inspected"}, agent=MEMBER)
    assert _checkpoint_count(world["plan_id"]) == before + 1
    with _as(MEMBER):
        latest = plan_resume(world["plan_id"])["checkpoint"]
    assert latest["context"]["outputs"]["result"] == "inspected"


def test_rejected_handoff_writes_no_checkpoint():
    world = _setup()
    _bind(world)
    before = _checkpoint_count(world["plan_id"])
    with pytest.raises(ToolError, match="no step at index"):
        plan_handoff(world["plan_id"], 9, outputs={"result": "nope"}, agent=MEMBER)
    assert _checkpoint_count(world["plan_id"]) == before


def test_bind_refuses_a_comment_linked_to_another_plan():
    world = _setup()
    other = plan_create("Other plan", ["elsewhere"], author=MEMBER)
    foreign = _comment(world["task_id"], "about the other plan", MEMBER, other["plan_id"])
    with pytest.raises(ToolError, match="linked to a different plan"):
        plan_manage(
            "joint_bind",
            {
                "plan_id": world["plan_id"],
                "goal_id": world["goal_id"],
                "org_unit_id": OFFICE,
                "comment_id": foreign,
                "author": MEMBER,
            },
        )


def test_plan_get_hides_joint_state():
    world = _setup()
    _bind(world)
    _propose(world, 0, ["inspect", "ship"], MEMBER)
    with _as(OUTSIDER):
        data = plan_get(world["plan_id"])
    assert data["checkpoints"]
    assert all("joint" not in cp["context"] for cp in data["checkpoints"])


def test_research_profile_cannot_propose(monkeypatch):
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    session = config.mcp_session_id_var.set("joint-plan-session")
    try:
        profiles.activate("developer")
        assert profiles.consolidated_action_is_allowed("plan_manage", "plan_joint_propose")
        assert profiles.consolidated_action_is_allowed("plan_manage", "plan_joint_recover")
        profiles.activate("research")
        assert not profiles.consolidated_action_is_allowed("plan_manage", "plan_joint_propose")
        assert not profiles.consolidated_action_is_allowed("plan_manage", "plan_joint_recover")
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)
        with profiles._lock:
            profiles._active.clear()
