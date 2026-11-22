"""Joint planning over existing plans, goals, comments, and org membership.

Task #1455. Revisions are plan checkpoints. Optimistic concurrency uses the
checkpoint revision: a stale ``expected_revision`` leaves the plan unchanged.
Team roles come from the checked-in org chart. There is no new table and no
scheduler. The weekly planning loop still only writes vault projections.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, events, org_registry, storage

_MAX_DECISION = 4000
_SETTLED = frozenset({"doing", "done"})
_JOINT_STATUSES = frozenset({"active", "blocked"})


class JointConflict(ToolError):
    """A revision lost the optimistic race. The plan was not modified."""

    def __init__(self, plan_id: str, expected: int, current: int) -> None:
        self.plan_id = plan_id
        self.expected_revision = expected
        self.current_revision = current
        super().__init__(
            f"joint revision conflict on plan {plan_id!r}: "
            f"expected {expected}, current {current}. "
            "recover the plan and retry against current_revision"
        )


class LiveStepsChanged(JointConflict):
    """Live steps moved after the proposal was staged. The plan was not modified."""

    def __init__(self, plan_id: str, revision: int) -> None:
        self.plan_id = plan_id
        self.expected_revision = revision
        self.current_revision = revision
        ToolError.__init__(
            self,
            f"joint revision conflict on plan {plan_id!r}: live steps changed since "
            f"the proposal at revision {revision}. recover the plan and propose again",
        )


def bind(
    plan_id: str,
    goal_id: int,
    org_unit_id: str,
    comment_id: int | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    """Bind one active plan to a goal and an org unit.

    A second active joint plan for the same goal is refused so two agents
    cannot decompose it twice.
    """
    actor = _actor(author)
    goal = _require_goal(goal_id)
    office, role = _require_member(actor, org_unit_id)
    if len(office["members"]) < 2:
        raise ToolError(
            f"org unit {office['id']} needs at least two members for a joint plan "
            "so a peer can review each proposal"
        )
    plan_key = _plan_key(plan_id)
    linked = _optional_comment(comment_id, plan_key, actor)
    joint = {
        "revision": 0,
        "goal_id": int(goal["goal"]["id"]),
        "org_unit_id": office["id"],
        "actor": actor,
        "role": role,
        "action": "bind",
        "comment_id": linked,
        "base_revision": None,
        "pending": None,
        "decisions": [],
        "review": None,
        "comment_ids": [linked] if linked is not None else [],
    }
    return _commit(plan_key, joint, note=f"joint bind by {actor}", channel="plan.joint.bound")


def propose(
    plan_id: str,
    expected_revision: int,
    steps: list,
    comment_id: int,
    author: str | None = None,
) -> dict[str, Any]:
    """Stage a step revision. Live steps change only when a peer accepts it."""
    actor = _actor(author)
    expected = _revision(expected_revision)
    plan_key = _plan_key(plan_id)
    specs = _specs(steps)
    linked = _optional_comment(comment_id, plan_key, actor)
    if linked is None:
        raise ToolError("comment_id is required")

    def mutate(current: dict, conn) -> dict:
        pending = current.get("pending")
        if (
            isinstance(pending, dict)
            and pending.get("status") == "proposed"
            and pending.get("author") != actor
            and _is_member(pending.get("author"), current["org_unit_id"])
        ):
            raise ToolError(
                f"proposal from {pending.get('author')} is awaiting review "
                f"at revision {current['revision']}"
            )
        office, role = _require_member(actor, current["org_unit_id"])
        nxt = _advance(current, actor=actor, role=role, action="propose", comment_id=linked)
        nxt["pending"] = {
            "steps": specs,
            "author": actor,
            "comment_id": linked,
            "status": "proposed",
            "role": role,
            "office": office["id"],
            "live_fingerprint": _fingerprint(conn, plan_key),
        }
        nxt["review"] = None
        return nxt

    return _commit(
        plan_key,
        mutate,
        expected=expected,
        note=f"joint propose by {actor}",
        channel="plan.joint.proposed",
        needs_conn=True,
    )


def review(
    plan_id: str,
    expected_revision: int,
    decision: str,
    comment_id: int,
    author: str | None = None,
) -> dict[str, Any]:
    """A different member accepts a proposal or requests changes.

    Accept applies the staged steps. A doing or done step cannot be dropped
    or rewritten. Request-changes leaves the live steps in place.
    """
    actor = _actor(author)
    expected = _revision(expected_revision)
    verdict = (decision or "").strip().lower()
    if verdict not in {"accept", "changes"}:
        raise ToolError("decision must be accept or changes")
    plan_key = _plan_key(plan_id)
    linked = _optional_comment(comment_id, plan_key, actor)
    if linked is None:
        raise ToolError("comment_id is required")

    def mutate(current: dict, conn) -> dict:
        _require_member(actor, current["org_unit_id"])
        pending = current.get("pending")
        if not isinstance(pending, dict) or pending.get("status") != "proposed":
            raise ToolError("no proposal is awaiting review")
        if pending.get("author") == actor:
            raise ToolError("reviewer must be a different member than the proposer")
        _, role = _require_member(actor, current["org_unit_id"])
        proposer = pending.get("author")
        if verdict == "accept" and not _is_member(proposer, current["org_unit_id"]):
            raise ToolError(
                f"proposer {proposer} is no longer a member of org unit "
                f"{current['org_unit_id']}; request changes or have a current member "
                "propose again"
            )
        if verdict == "accept" and pending.get("live_fingerprint") != _fingerprint(conn, plan_key):
            raise LiveStepsChanged(plan_key, int(current["revision"]))
        nxt = _advance(current, actor=actor, role=role, action="review", comment_id=linked)
        nxt["review"] = {
            "decision": verdict,
            "reviewer": actor,
            "role": role,
            "comment_id": linked,
        }
        if verdict == "accept":
            _apply_steps(conn, plan_key, list(pending["steps"]))
            nxt["pending"] = None
        else:
            held = dict(pending)
            held["status"] = "changes_requested"
            nxt["pending"] = held
        return nxt

    return _commit(
        plan_key,
        mutate,
        expected=expected,
        note=f"joint review {verdict} by {actor}",
        channel="plan.joint.reviewed",
        needs_conn=True,
    )


def decide(
    plan_id: str,
    expected_revision: int,
    text: str,
    comment_id: int,
    author: str | None = None,
) -> dict[str, Any]:
    """Record a decision on the plan, linked to a goal id and a comment id."""
    actor = _actor(author)
    expected = _revision(expected_revision)
    body = (text or "").strip()
    if not body or len(body) > _MAX_DECISION:
        raise ToolError(f"text must be nonempty and at most {_MAX_DECISION} characters")
    plan_key = _plan_key(plan_id)
    linked = _optional_comment(comment_id, plan_key, actor)
    if linked is None:
        raise ToolError("comment_id is required")

    def mutate(current: dict) -> dict:
        _, role = _require_member(actor, current["org_unit_id"])
        nxt = _advance(current, actor=actor, role=role, action="decide", comment_id=linked)
        nxt["decisions"] = [
            *list(current.get("decisions") or []),
            {
                "revision": nxt["revision"],
                "author": actor,
                "role": role,
                "text": body,
                "comment_id": linked,
                "goal_id": int(current["goal_id"]),
                "at": storage.now(),
            },
        ]
        return nxt

    return _commit(
        plan_key,
        mutate,
        expected=expected,
        note=f"joint decision by {actor}",
        channel="plan.joint.decided",
    )


def get_contract(plan_id: str, author: str | None = None) -> dict[str, Any]:
    """Read the latest joint contract, including why-chain and live team roles."""
    return _read(plan_id, author, recovered=False)


def recover(plan_id: str, author: str | None = None) -> dict[str, Any]:
    """Read the winning contract after a conflict or a restart. Writes nothing."""
    return _read(plan_id, author, recovered=True)


def require_joint_member(plan_id: str | None, claimed: str | None = None, *, conn=None) -> None:
    """Refuse a caller outside the bound office. Plans that are not joint pass.

    Pass the caller's write connection so the check and the write share one
    transaction.
    """
    key = (plan_id or "").strip() if isinstance(plan_id, str) else ""
    if not key:
        return
    if conn is not None:
        joint = _latest(conn, key)
    else:
        own = storage.connect()
        try:
            joint = _latest(own, key)
        finally:
            own.close()
    if joint is None:
        return
    who = (config.verify_write_identity(claimed) or "").strip()
    if not who:
        raise ToolError(
            f"plan {key!r} is a joint plan; a caller identity is required "
            f"to act on it (org unit {joint['org_unit_id']})"
        )
    _require_member(who, joint["org_unit_id"])


def _read(plan_id: str, author: str | None, *, recovered: bool) -> dict[str, Any]:
    actor = _actor(author)
    plan_key = _plan_key(plan_id)
    conn = storage.connect()
    try:
        _require_plan(conn, plan_key)
        joint = _latest(conn, plan_key)
        if joint is None:
            raise ToolError(f"plan {plan_key!r} is not a joint plan")
        _require_member(actor, joint["org_unit_id"])
        return _view_on(conn, plan_key, joint, recovered=recovered)
    finally:
        conn.close()


def _commit(
    plan_id: str,
    joint_or_fn,
    *,
    expected: int | None = None,
    note: str,
    channel: str,
    needs_conn: bool = False,
) -> dict[str, Any]:
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_joint_status(conn, plan_id)
        current = _latest(conn, plan_id)
        if expected is None:
            if current is not None:
                raise ToolError(f"plan {plan_id!r} is already a joint plan")
            _refuse_duplicate_goal(conn, joint_or_fn["goal_id"], plan_id)
            joint = joint_or_fn
        else:
            if current is None:
                raise ToolError(f"plan {plan_id!r} is not a joint plan")
            if int(current["revision"]) != expected:
                raise JointConflict(plan_id, expected, int(current["revision"]))
            joint = joint_or_fn(current, conn) if needs_conn else joint_or_fn(current)
        _require_joint_status(conn, plan_id)
        _write(conn, plan_id, joint, note)
        _, won, published = events.record_event_once_on_connection(
            conn,
            channel,
            payload={
                "plan_id": plan_id,
                "goal_id": joint["goal_id"],
                "comment_id": joint.get("comment_id"),
                "revision": joint["revision"],
                "action": joint["action"],
                "org_unit_id": joint["org_unit_id"],
            },
            originator=joint["actor"],
        )
        view = _view_on(conn, plan_id, joint, recovered=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if won and published is not None:
        events.publish(published)
    return view


def _fingerprint(conn, plan_id: str) -> str:
    rows = conn.execute(
        "SELECT step_idx, description, owner, depends_on, status, note, retries, "
        "outputs, updated_at FROM plan_steps WHERE plan_id=? ORDER BY step_idx",
        (plan_id,),
    ).fetchall()
    blob = json.dumps([list(row) for row in rows], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _write(conn, plan_id: str, joint: dict, note: str) -> None:
    from .tools.plan import _step_dict, _steps_for

    ts = storage.now()
    seq_row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM plan_checkpoints WHERE plan_id=?",
        (plan_id,),
    ).fetchone()
    steps = [_step_dict(row) for row in _steps_for(conn, plan_id)]
    prior_row = conn.execute(
        "SELECT context FROM plan_checkpoints WHERE plan_id=? ORDER BY seq DESC LIMIT 1",
        (plan_id,),
    ).fetchone()
    prior = json.loads(prior_row["context"]) if prior_row is not None else {}
    outputs = dict(prior.get("outputs") or {})
    outputs["joint_action"] = joint["action"]
    context = {
        "memory_ids": list(prior.get("memory_ids") or []),
        "file_paths": list(prior.get("file_paths") or []),
        "outputs": outputs,
        "step_state": [
            {
                "step_idx": step["step_idx"],
                "status": step["status"],
                "description": step["description"],
            }
            for step in steps
        ],
        "joint": joint,
    }
    conn.execute(
        "INSERT INTO plan_checkpoints (plan_id, seq, context, note, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (plan_id, int(seq_row["m"]) + 1, json.dumps(context), note, ts),
    )
    conn.execute("UPDATE plans SET updated_at=? WHERE id=?", (ts, plan_id))


def _apply_steps(conn, plan_id: str, specs: list[dict]) -> None:
    rows = conn.execute(
        "SELECT * FROM plan_steps WHERE plan_id=? ORDER BY step_idx",
        (plan_id,),
    ).fetchall()
    existing = [dict(row) for row in rows]
    for index, row in enumerate(existing):
        if row["status"] not in _SETTLED:
            continue
        if index >= len(specs) or specs[index]["description"] != row["description"]:
            raise ToolError(f"replan cannot drop or rewrite a doing or done step (index {index})")
        prior_deps = json.loads(row["depends_on"] or "[]")
        if list(specs[index]["depends_on"]) != list(prior_deps):
            raise ToolError(f"replan cannot retarget a doing or done step (index {index})")
        spec_owner = specs[index]["owner"]
        if spec_owner is not None and spec_owner != row["owner"]:
            raise ToolError(f"replan cannot retarget a doing or done step (index {index})")
    ts = storage.now()
    kept = {
        index
        for index, row in enumerate(existing)
        if index < len(specs) and _unchanged(row, specs[index])
    }
    for index, row in enumerate(existing):
        if index not in kept:
            conn.execute("DELETE FROM plan_steps WHERE id=?", (row["id"],))
    for index, spec in enumerate(specs):
        if index in kept:
            continue
        conn.execute(
            "INSERT INTO plan_steps "
            "(plan_id, step_idx, description, owner, depends_on, status, "
            " retries, created_at, updated_at, outputs) "
            "VALUES (?, ?, ?, ?, ?, 'open', 0, ?, ?, '{}')",
            (
                plan_id,
                index,
                spec["description"],
                spec["owner"],
                json.dumps(spec["depends_on"]),
                ts,
                ts,
            ),
        )
    still_blocked = conn.execute(
        "SELECT 1 FROM plan_steps WHERE plan_id=? AND status='blocked' LIMIT 1",
        (plan_id,),
    ).fetchone()
    if still_blocked is None:
        conn.execute(
            "UPDATE plans SET status='active', escalated=0, updated_at=? "
            "WHERE id=? AND status='blocked'",
            (ts, plan_id),
        )


def _unchanged(row: dict, spec: dict) -> bool:
    if spec["description"] != row["description"]:
        return False
    if list(spec["depends_on"]) != list(json.loads(row["depends_on"] or "[]")):
        return False
    return spec["owner"] is None or spec["owner"] == row["owner"]


def _view_on(conn, plan_id: str, joint: dict, *, recovered: bool) -> dict[str, Any]:
    from .goals import get_goal
    from .tools.plan import _next_actionable, _step_dict, _steps_for

    steps = [_step_dict(row) for row in _steps_for(conn, plan_id)]
    office = org_registry.find_office(str(joint["org_unit_id"]))
    team = []
    if office is not None:
        team = [
            {"identity": label, "role": "lead" if label == office["lead"] else "member"}
            for label in office["members"]
        ]
    detail = get_goal(int(joint["goal_id"]))
    return {
        "plan_id": plan_id,
        "revision": int(joint["revision"]),
        "goal_id": int(joint["goal_id"]),
        "org_unit_id": joint["org_unit_id"],
        "actor": joint["actor"],
        "role": joint["role"],
        "action": joint["action"],
        "comment_id": joint.get("comment_id"),
        "comment_ids": list(joint.get("comment_ids") or []),
        "pending": joint.get("pending"),
        "decisions": list(joint.get("decisions") or []),
        "review": joint.get("review"),
        "steps": steps,
        "next_step_idx": _next_actionable(steps),
        "team": team,
        "why_chain": list(detail["ancestry"]) if detail else [],
        "recovered": recovered,
    }


def _latest(conn, plan_id: str) -> dict | None:
    # Read through the plan's tenant fence (#2042): checkpoints carry no
    # org_id, and a foreign joint binding must not answer before the plan
    # check does (it would name the plan's org unit).
    row = conn.execute(
        """
        SELECT c.context FROM plan_checkpoints c
        JOIN plans p ON p.id = c.plan_id AND p.org_id IS ?
        WHERE c.plan_id=? AND json_extract(c.context, '$.joint.revision') IS NOT NULL
        ORDER BY c.seq DESC LIMIT 1
        """,
        (edition.tenant_org_id(), plan_id),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["context"])["joint"]


def _refuse_duplicate_goal(conn, goal_id: int, plan_id: str) -> None:
    row = conn.execute(
        """
        SELECT p.id AS plan_id
        FROM plans p
        JOIN plan_checkpoints c ON c.plan_id = p.id
        WHERE p.status IN ('active', 'blocked')
          AND p.org_id IS ?
          AND p.id != ?
          AND json_extract(c.context, '$.joint.goal_id') = ?
        LIMIT 1
        """,
        (edition.tenant_org_id(), plan_id, int(goal_id)),
    ).fetchone()
    if row is not None:
        raise ToolError(
            f"goal {int(goal_id)} already has active joint plan {row['plan_id']}; "
            "refusing a second decomposition"
        )


def _advance(current: dict, *, actor: str, role: str, action: str, comment_id: int) -> dict:
    comment_ids = list(current.get("comment_ids") or [])
    if comment_id not in comment_ids:
        comment_ids.append(comment_id)
    return {
        "revision": int(current["revision"]) + 1,
        "goal_id": int(current["goal_id"]),
        "org_unit_id": current["org_unit_id"],
        "actor": actor,
        "role": role,
        "action": action,
        "comment_id": comment_id,
        "base_revision": int(current["revision"]),
        "pending": current.get("pending"),
        "decisions": list(current.get("decisions") or []),
        "review": current.get("review"),
        "comment_ids": comment_ids,
    }


def _require_joint_status(conn, plan_id: str):
    plan = _require_plan(conn, plan_id)
    if plan["status"] not in _JOINT_STATUSES:
        raise ToolError(f"plan {plan_id!r} is {plan['status']}, not active or blocked")
    return plan


def _require_plan(conn, plan_id: str):
    # Tenant-fenced like tools.plan._get_plan (#2042), with the same error.
    plan = conn.execute(
        "SELECT * FROM plans WHERE id=? AND org_id IS ?", (plan_id, edition.tenant_org_id())
    ).fetchone()
    if plan is None:
        raise ToolError(f"no plan with id {plan_id!r}")
    return plan


def _require_goal(goal_id: int) -> dict:
    from .goals import get_goal

    if isinstance(goal_id, bool) or not isinstance(goal_id, int) or goal_id < 1:
        raise ToolError("goal_id must be a positive integer")
    detail = get_goal(goal_id)
    if detail is None:
        raise ToolError(f"unknown goal: {goal_id}")
    return detail


def _optional_comment(comment_id: int | None, plan_id: str | None, actor: str) -> int | None:
    if comment_id is None:
        return None
    if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id < 1:
        raise ToolError("comment_id must be a positive integer")
    from .task_comments import get_comment

    found = get_comment(comment_id)
    if found is None:
        raise ToolError(f"unknown comment: {comment_id}")
    if found["comment"].get("author") != actor:
        raise ToolError(f"comment {comment_id} is not authored by {actor}")
    refs = found["comment"].get("refs") or {}
    linked = refs.get("plan_id")
    if plan_id is not None and linked not in (None, plan_id):
        raise ToolError(f"comment {comment_id} is linked to a different plan")
    return comment_id


def _require_member(actor: str, org_unit_id: str) -> tuple[dict, str]:
    office = org_registry.find_office(str(org_unit_id or "").strip())
    if office is None:
        raise ToolError(f"unknown org unit: {org_unit_id!r}")
    if actor not in office["members"]:
        raise ToolError(
            f"scoped membership refused: {actor} is not a member of org unit {office['id']}"
        )
    role = "lead" if actor == office["lead"] else "member"
    return office, role


def _is_member(identity: str | None, org_unit_id: str) -> bool:
    office = org_registry.find_office(str(org_unit_id or "").strip())
    return office is not None and identity in office["members"]


def _actor(author: str | None) -> str:
    who = config.verify_write_identity(author)
    who = (who or "").strip()
    if not who:
        raise ToolError("author is required when no verified identity is present")
    return who


def _plan_key(plan_id: str) -> str:
    key = (plan_id or "").strip()
    if not key:
        raise ToolError("plan_id must not be empty")
    return key


def _revision(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ToolError("expected_revision must be an integer >= 0")
    return value


def _specs(steps: list) -> list[dict]:
    from .tools.plan import _normalize_steps

    return _normalize_steps(steps)
