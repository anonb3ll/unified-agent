"""Durable role workflow projection onto ordinary task rows (#1737).

Only task writers call the connection hooks. The projector never dispatches or
claims work, and tool execution always uses the existing governed workflow path.
"""

import copy
import json
import re
from contextlib import closing, contextmanager
from contextvars import ContextVar
from uuid import uuid4

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, org_registry, storage, table_schemas, team_spec
from .telemetry.redaction import _key_looks_sensitive, redact_sensitive_content

# Moved to the kernel (#2450); re-exported so callers and patches keep working.
from .workflow_steps import _exists, is_workflow_task  # noqa: F401

_NAMES = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
_EXITS = {"handoff", "waiting", "blocked", "failed", "done"}
_REASONS = {"handed_off_to", "blocked_on", "denied", "cancelled", "no_follow_on", "escalation"}
_creation: ContextVar[dict | None] = ContextVar("workflow_creation", default=None)
_transition: ContextVar[dict | None] = ContextVar("workflow_transition", default=None)


def _ensure_schema(conn):
    # Individual DDL statements preserve a caller's transaction; executescript
    # would implicitly commit the task INSERT before its instance is recorded.
    for statement in table_schemas.WORKFLOW_ROLES.split(";"):
        if statement.strip():
            conn.execute(statement)


def assert_tasks_deletable(conn, task_ids):
    if (
        _exists(conn)
        and conn.execute(
            "SELECT 1 FROM workflow_instances WHERE task_id IN ("
            + ",".join("?" for _ in task_ids)
            + ") AND status NOT IN ('done','failed') LIMIT 1",
            task_ids,
        ).fetchone()
    ):
        raise ToolError("workflow frontier task cannot be deleted; retain its closure trail")


def is_failed_frontier(conn, task_id):
    """A hold may clear its cancellation request, but cannot reopen a failed workflow."""
    return bool(
        _exists(conn)
        and conn.execute(
            "SELECT 1 FROM workflow_instances WHERE task_id=? AND status='failed' AND org_id IS ?",
            (task_id, edition.tenant_org_id()),
        ).fetchone()
    )


def _encode(value):
    def clean(item):
        if isinstance(item, str):
            return redact_sensitive_content(item, max_len=200_000)
        if isinstance(item, dict):
            return {
                str(k): "[REDACTED_SECRET]" if _key_looks_sensitive(str(k)) else clean(v)
                for k, v in item.items()
            }
        if isinstance(item, list):
            return [clean(v) for v in item]
        return item

    result = json.dumps(clean(value), allow_nan=False)
    if len(result.encode()) > 200_000:
        raise ToolError("workflow state exceeds 200000 bytes; use bounded artifact references")
    return result


def validate(spec):
    roles = spec.get("roles")
    if roles is None:
        if any("actor_role" in s for s in spec["steps"]):
            raise ToolError("actor_role requires a roles mapping")
        return
    if not isinstance(roles, dict) or not roles or len(roles) > 32:
        raise ToolError("roles must be a non-empty mapping of at most 32 roles")
    for name, role in roles.items():
        if not isinstance(name, str) or not _NAMES.fullmatch(name) or not isinstance(role, dict):
            raise ToolError("each role needs a valid name and mapping")
        if set(role) - {"skills", "preferred_seats", "team", "office"}:
            raise ToolError(f"unknown fields in role {name!r}")
        for field in ("skills", "preferred_seats"):
            values = role.get(field, [])
            if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
                raise ToolError(f"role {name!r} {field} must be a list of non-empty strings")
        if not any(role.get(k) for k in ("preferred_seats", "team", "office")):
            raise ToolError(f"role {name!r} needs preferred_seats, team or office")
        for field in ("team", "office"):
            if field in role and (not isinstance(role[field], str) or not role[field].strip()):
                raise ToolError(f"role {name!r} {field} must be a non-empty string")
    for index, step in enumerate(spec["steps"]):
        if not isinstance(step.get("actor_role"), str) or step["actor_role"] not in roles:
            raise ToolError(f"step {step['name']!r} has an unknown actor_role")
        if step.get("retries", 0) != 0:
            raise ToolError("role steps cannot automatically retry an uncertain effect")
        if not isinstance(step.get("objective"), str) or not step["objective"].strip():
            raise ToolError("each role step needs an objective")
        exits = step.get("allowed_exits")
        if (
            not isinstance(exits, list)
            or not exits
            or not all(isinstance(e, str) and e in _EXITS for e in exits)
        ):
            raise ToolError("allowed_exits must name handoff, waiting, blocked, failed or done")
        hop = step.get("next_hop", {})
        if not isinstance(hop, dict) or set(hop) - {"require", "suggest"}:
            raise ToolError("next_hop must contain require and/or suggest role lists")
        for values in hop.values():
            if not isinstance(values, list) or not all(
                isinstance(v, str) and v in roles for v in values
            ):
                raise ToolError("next_hop names an unknown role")
        if index + 1 < len(spec["steps"]):
            next_role = spec["steps"][index + 1].get("actor_role")
            if "handoff" not in exits or (hop.get("require") and next_role not in hop["require"]):
                raise ToolError("a non-final step must permit handoff to the next required role")
        elif "handoff" in exits or "done" not in exits:
            raise ToolError("the final step must permit done and cannot handoff past the workflow")
        if "handoff_commit" in step and (
            step["handoff_commit"] is not True or index + 1 == len(spec["steps"])
        ):
            raise ToolError("handoff_commit must be true on a non-final step")


def _candidates(role):
    seats = list(role["preferred_seats"]) if role.get("preferred_seats") else None
    if role.get("team"):
        team = team_spec.resolve(team=role["team"])
        skills = set(role.get("skills", []))
        members = [s["label"] for s in team["seats"] if skills <= set(s["skills"])]
        seats = [s for s in seats if s in members] if seats is not None else members
    if role.get("office"):
        office = org_registry.find_office(role["office"])
        if office is None:
            raise ToolError(f"unknown office {role['office']!r}")
        members = office["members"]
        seats = [s for s in seats if s in members] if seats is not None else list(members)
    # The queue's lane validator is the established roster check.
    from .tools.tasks import _validate_lane

    for seat in seats or []:
        if _validate_lane(seat) != seat or seat in {
            "default",
            "tracking",
            "dispatch-ready",
            "interactive-only",
            "operator-only",
            "laptop",
        }:
            raise ToolError(f"role seat {seat!r} must be a registered agent label")
    if not seats:
        raise ToolError("role has no eligible seats")
    return seats


def _bindings(spec, overrides):
    if not isinstance(overrides, dict) or set(overrides) - spec["roles"].keys():
        raise ToolError("role_bindings must map declared roles to eligible seats")
    out = {}
    for role, body in spec["roles"].items():
        seats = _candidates(body)
        selected = overrides.get(role, seats[0])
        if selected not in seats:
            raise ToolError(f"agent {selected!r} is not eligible for role {role!r}")
        out[role] = selected
    return out


def task_args(spec, index, bindings, instance_id):
    step = spec["steps"][index]
    return {
        "title": f"Workflow {spec['name']}: {step['name']}",
        "notes": f"[workflow-step] Workflow: {spec['name']}\nInstance: {instance_id}\n"
        f"Role: {step['actor_role']}\n{step['objective']}",
        "lane": bindings[step["actor_role"]],
    }


def _handoff_spec(spec, index, task):
    """Bind review to the builder's resulting commit, before any step effect."""
    if not spec["steps"][index].get("handoff_commit"):
        return spec
    evidence = task["evidence_ref"]
    if not isinstance(evidence, str) or not re.fullmatch(
        r"commit:([0-9a-f]{40}|[0-9a-f]{64})", evidence
    ):
        raise ToolError("builder handoff requires task evidence_ref=commit:<full resulting SHA>")
    result = copy.deepcopy(spec)
    result["steps"][index + 1]["objective"] += "\nExact review head: " + evidence[7:]
    return result


@contextmanager
def creation_context(state):
    token = _creation.set(state)
    try:
        yield
    finally:
        _creation.reset(token)


def on_task_created(conn, task_id):
    state = _creation.get()
    if state is None:
        return
    _ensure_schema(conn)
    conn.execute(
        """INSERT INTO workflow_instances
        (id,name,spec,inputs,bindings,outputs,step_index,task_id,status,execution,creator,org_id,created_at)
        VALUES (?,?,?,?,?, ?,0,?,'waiting','idle',?,?,?)""",
        (
            state["id"],
            state["name"],
            _encode(state["spec"]),
            _encode(state["inputs"]),
            _encode(state["bindings"]),
            "{}",
            task_id,
            state["creator"],
            edition.tenant_org_id(),
            storage.now(),
        ),
    )
    conn.execute("INSERT INTO workflow_task_steps VALUES (?,?,0)", (task_id, state["id"]))


def _get(conn, instance_id):
    if not _exists(conn):
        raise ToolError("no workflow instance")
    row = conn.execute(
        "SELECT * FROM workflow_instances WHERE id=? AND org_id IS ?",
        (instance_id, edition.tenant_org_id()),
    ).fetchone()
    if row is None:
        raise ToolError("no workflow instance")
    return dict(row)


def snapshot(instance_id, name):
    with closing(storage.connect()) as conn:
        row = _get(conn, instance_id)
        if row["name"] != name:
            raise ToolError("workflow instance belongs to a different definition")
        spec, bindings = json.loads(row["spec"]), json.loads(row["bindings"])
        actor = config.verified_agent_identity()
        if not actor or actor not in {row["creator"], *bindings.values()}:
            raise ToolError("workflow snapshot requires a participant identity")
        frontier = None
        if row["task_id"] is None and row["status"] not in {"done", "failed"}:
            raise ToolError("workflow frontier task is missing; explicit repair is required")
        if row["task_id"] is not None and row["status"] != "done":
            step = spec["steps"][row["step_index"]]
            task = conn.execute(
                "SELECT status,updated_at,claimed_by FROM tasks WHERE id=? AND org_id IS ?",
                (row["task_id"], row["org_id"]),
            ).fetchone()
            if task is None:
                raise ToolError("workflow frontier task is missing; explicit repair is required")
            frontier = {
                "task_id": row["task_id"],
                "name": step["name"],
                "actor_role": step["actor_role"],
                "agent": bindings[step["actor_role"]],
                "task_status": task["status"],
                "updated_at": task["updated_at"],
                "execution": row["execution"],
            }
        trail = [
            dict(r)
            for r in conn.execute(
                "SELECT task_id,actor,outcome,reason,target,created_at FROM workflow_closures "
                "WHERE instance_id=? ORDER BY id",
                (instance_id,),
            )
        ]
        return {
            "workflow": name,
            "instance_id": instance_id,
            "status": row["status"],
            "frontier": frontier,
            "outputs": json.loads(row["outputs"]),
            "trail": trail,
        }


def _validate_closure(row, spec, closure):
    if not isinstance(closure, dict) or set(closure) - {
        "exit",
        "reason",
        "target",
        "task_id",
        "expected_updated_at",
    }:
        raise ToolError("closure must contain exit, reason, task_id and expected_updated_at")
    step = spec["steps"][row["step_index"]]
    exit, reason, target = closure.get("exit"), closure.get("reason"), closure.get("target")
    if (
        not isinstance(exit, str)
        or not isinstance(reason, str)
        or exit not in step["allowed_exits"]
        or reason not in _REASONS
    ):
        raise ToolError("closure exit or reason is not allowed")
    if target is not None and (
        not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_.:#/-]{1,256}", target)
    ):
        raise ToolError("closure target must be a bounded reference, not free text")
    if exit == "handoff":
        next_role = spec["steps"][row["step_index"] + 1]["actor_role"]
        if reason != "handed_off_to" or target != next_role:
            raise ToolError("handoff closure requires handed_off_to the next role")
    elif exit == "done":
        if row["step_index"] + 1 != len(spec["steps"]) or reason != "no_follow_on" or target:
            raise ToolError("done requires no_follow_on on the final step")
    elif exit in {"waiting", "blocked"}:
        if (
            reason not in {"blocked_on", "escalation"}
            or not isinstance(target, str)
            or not target.strip()
        ):
            raise ToolError("waiting/blocked closure requires a blocked_on or escalation target")
        if reason == "escalation" and target not in spec["roles"]:
            raise ToolError("escalation target must be a declared role")
    elif exit == "failed" and (reason not in {"denied", "cancelled"} or target):
        raise ToolError("failed closure requires denied/cancelled without a target")
    if (
        not isinstance(closure.get("task_id"), int)
        or isinstance(closure["task_id"], bool)
        or closure["task_id"] != row["task_id"]
    ):
        raise ToolError("closure does not name the current frontier task")
    if not isinstance(closure.get("expected_updated_at"), str):
        raise ToolError("closure requires the current task revision")


def _insert_successor(conn, old_task, spec, index, bindings, instance_id):
    # Projection creates OPEN inventory only, never a claim/dispatch/authority.
    # Copy the admitted first task's tenant/project/domain/quarantine. A new
    # agent still has to pass normal task_claim WIP, hold and budget admission.
    args = task_args(spec, index, bindings, instance_id)
    ts = storage.now()
    cur = conn.execute(
        """INSERT INTO tasks
        (title,notes,priority,parent_id,created_at,updated_at,source_host,untrusted,project,domain,lane,org_id)
        VALUES (?,?,'med',?,?,?,?,?,?,?,?,?)""",
        (
            args["title"],
            args["notes"],
            old_task["parent_id"],
            ts,
            ts,
            config.write_source_host(),
            old_task["untrusted"],
            old_task["project"],
            old_task["domain"],
            args["lane"],
            old_task["org_id"],
        ),
    )
    conn.execute(
        "INSERT INTO workflow_task_steps VALUES (?,?,?)", (cur.lastrowid, instance_id, index)
    )
    return cur.lastrowid


def on_task_transition(conn, task_id, status, actor, prior_task, expected_run_id=None):
    if not _exists(conn):
        return None
    mapping = conn.execute(
        "SELECT * FROM workflow_task_steps WHERE task_id=?", (task_id,)
    ).fetchone()
    if mapping is None or status is None:
        return None
    row = _get(conn, mapping["instance_id"])
    if row["task_id"] != task_id or row["status"] in {"done", "failed"}:
        if status != prior_task["status"]:
            raise ToolError("completed workflow steps cannot be reopened independently")
        return None
    if status not in {"done", "cancelled", "blocked", "waiting_review"}:
        return None
    spec, bindings = json.loads(row["spec"]), json.loads(row["bindings"])
    bound_owner = bindings[spec["steps"][row["step_index"]]["actor_role"]]
    pending = _transition.get()
    from . import agent_lifecycle

    explicit_closure = (
        pending is not None and pending["id"] == row["id"] and pending["actor"] == actor
    )
    lifecycle_cancel = (
        not explicit_closure
        and status == "cancelled"
        and actor is not None
        and (
            actor in agent_lifecycle.operators()
            or (
                actor == bound_owner == prior_task["claimed_by"] == prior_task["current_run_agent"]
                and expected_run_id is not None
                and expected_run_id == prior_task["current_run_id"]
                and prior_task["cancel_requested"]
            )
        )
    )
    if lifecycle_cancel:
        pending = {
            "id": row["id"],
            "actor": actor,
            "closure": {
                "exit": "failed",
                "reason": "cancelled",
                "task_id": task_id,
                "expected_updated_at": prior_task["updated_at"],
            },
        }
    if pending is None or pending["id"] != row["id"] or pending["actor"] != actor:
        raise ToolError("workflow task requires an explicit workflow_run closure")
    cancelling = (
        pending["closure"]["exit"] == "failed" and pending["closure"]["reason"] == "cancelled"
    )
    if not lifecycle_cancel and (
        prior_task["claimed_by"] != actor
        or not prior_task["lock_expires_at"]
        or prior_task["lock_expires_at"] <= storage.now()
        or (prior_task["cancel_requested"] and not cancelling)
        or prior_task["untrusted"]
        or prior_task["archived_at"]
    ):
        raise ToolError("workflow owner admission changed before task completion")
    from . import task_holds

    held = task_holds.claim_block(conn, task_id)
    if held and not cancelling:
        raise ToolError(f"workflow task is held: {held}")
    from .tools.tasks import _assert_no_cancelled_ancestor

    if not cancelling:
        _assert_no_cancelled_ancestor(conn, task_id)
    closure = pending["closure"]
    if not lifecycle_cancel:
        _validate_closure(row, spec, closure)
    if row["task_id"] != task_id:
        raise ToolError("workflow task is no longer the frontier")
    index, next_task = row["step_index"], task_id
    if closure["exit"] == "handoff":
        old_task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        spec = _handoff_spec(spec, index, old_task)
        index += 1
        next_task = _insert_successor(conn, old_task, spec, index, bindings, row["id"])
    outputs = json.loads(row["outputs"])
    if row["execution"] == "ready":
        receipt = json.loads(row["receipt"])
        outputs.update(receipt.get("outputs", {}))
    state = {
        "handoff": "waiting",
        "done": "done",
        "waiting": "waiting",
        "blocked": "blocked",
        "failed": "failed",
    }[closure["exit"]]
    execution = "idle" if closure["exit"] in {"handoff", "done"} else row["execution"]
    if execution == "executing":
        execution = "indeterminate"
    conn.execute(
        """UPDATE workflow_instances SET spec=?,task_id=?,step_index=?,status=?,outputs=?,
        execution=?,receipt=CASE WHEN ?='idle' THEN NULL ELSE receipt END WHERE id=?""",
        (_encode(spec), next_task, index, state, _encode(outputs), execution, execution, row["id"]),
    )
    conn.execute(
        """INSERT INTO workflow_closures
        (instance_id,task_id,actor,outcome,reason,target,created_at) VALUES (?,?,?,?,?,?,?)""",
        (
            row["id"],
            task_id,
            actor,
            closure["exit"],
            closure["reason"],
            closure.get("target"),
            storage.now(),
        ),
    )
    return next_task if next_task != task_id else None


def run(name, spec, inputs, author, instance_id, overrides, closure, invoke, execute, prepare=None):
    if not author or not config.verified_agent_identity():
        raise ToolError("role workflows require a verified agent identity")
    if instance_id is None:
        if closure is not None:
            raise ToolError("closure requires an existing instance_id")
        bindings = _bindings(spec, {} if overrides is None else overrides)
        if json.loads(_encode(inputs)) != inputs:
            raise ToolError("workflow inputs contain sensitive text; use artifact references")
        state = {
            "id": uuid4().hex,
            "name": name,
            "spec": spec,
            "inputs": inputs,
            "bindings": bindings,
            "creator": author,
        }
        # Validate bounded serialization before creating the task.
        _encode(state)
        with creation_context(state):
            invoke("task_add", task_args(spec, 0, bindings, state["id"]))
        return snapshot(state["id"], name)
    if inputs or overrides:
        raise ToolError("instance inputs and role bindings are immutable")
    if closure is None:
        return snapshot(instance_id, name)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _get(conn, instance_id)
        if row["name"] != name or row["status"] in {"done", "failed"}:
            raise ToolError("workflow instance is complete or belongs to another definition")
        spec, bindings = json.loads(row["spec"]), json.loads(row["bindings"])
        _validate_closure(row, spec, closure)
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND org_id IS ?", (row["task_id"], row["org_id"])
        ).fetchone()
        if task is None or task["updated_at"] != closure["expected_updated_at"]:
            raise ToolError("workflow task revision changed")
        step = spec["steps"][row["step_index"]]
        cancelling = closure["exit"] == "failed" and closure["reason"] == "cancelled"
        if author != bindings[step["actor_role"]] or (
            not cancelling and author not in _candidates(spec["roles"][step["actor_role"]])
        ):
            raise ToolError("caller does not hold the current workflow role")
        if (
            task["claimed_by"] != author
            or not task["lock_expires_at"]
            or task["lock_expires_at"] <= storage.now()
        ):
            raise ToolError("workflow role requires a live claim by the current owner")
        if (
            task["archived_at"]
            or task["untrusted"]
            or (task["cancel_requested"] and not cancelling)
        ):
            raise ToolError("workflow task is archived, untrusted or cancelled")
        from . import task_holds

        held = task_holds.claim_block(conn, row["task_id"])
        if held and not cancelling:
            raise ToolError(f"workflow task is held: {held}")
        from .tools.tasks import _assert_no_cancelled_ancestor

        if not cancelling:
            _assert_no_cancelled_ancestor(conn, row["task_id"])
        needs_effect = closure["exit"] in {"handoff", "done"}
        if needs_effect and row["execution"] in {"executing", "indeterminate"}:
            raise ToolError("workflow execution is indeterminate; do not repeat the tool effect")
        if closure["exit"] == "handoff":
            _handoff_spec(spec, row["step_index"], task)
        if needs_effect and row["execution"] == "idle":
            preflight = prepare(json.loads(row["inputs"]), author) if prepare else None
            if preflight is not None:
                conn.commit()
                result = snapshot(instance_id, name)
                result["step_result"] = preflight
                return result
            conn.execute(
                "UPDATE workflow_instances SET execution='executing' WHERE id=?", (instance_id,)
            )
        conn.commit()
    finally:
        conn.close()
    if needs_effect and row["execution"] == "idle":
        try:
            receipt = execute(
                spec, step, json.loads(row["inputs"]), json.loads(row["outputs"]), author
            )
        except Exception:
            receipt = {
                "status": "indeterminate",
                "outputs": {},
                "error": "execution raised without a receipt; do not repeat its effect",
            }
        try:
            serialized = _encode(receipt)
            if json.loads(serialized) != receipt:
                raise ToolError("execution receipt contains sensitive text")
            combined = json.loads(row["outputs"])
            combined.update(receipt.get("outputs", {}))
            _encode(combined)
        except (ToolError, TypeError, ValueError):
            receipt = {
                "status": "indeterminate",
                "outputs": {},
                "error": "execution receipt cannot be safely persisted; use artifact references",
            }
            serialized = _encode(receipt)
        # A crash between effect and receipt leaves 'executing', never a retry.
        with closing(storage.connect()) as conn:
            state = (
                "ready"
                if receipt["status"] == "done"
                else "idle"
                if receipt["status"] in {"denied", "pending-approval"}
                else "indeterminate"
            )
            updated = conn.execute(
                "UPDATE workflow_instances SET execution=?,receipt=? WHERE id=? AND task_id=? "
                "AND execution='executing' AND status NOT IN ('done','failed')",
                (state, serialized, instance_id, row["task_id"]),
            )
            conn.commit()
        if updated.rowcount != 1:
            result = snapshot(instance_id, name)
            result["step_result"] = {
                "status": "indeterminate",
                "outputs": {},
                "error": "workflow disposition changed during execution; no handoff was applied",
            }
            return result
        if receipt["status"] != "done":
            result = snapshot(instance_id, name)
            result["step_result"] = receipt
            return result
    pending = {"id": instance_id, "actor": author, "closure": closure}
    status = {
        "handoff": "done",
        "done": "done",
        "waiting": "waiting_review",
        "blocked": "blocked",
        "failed": "cancelled",
    }[closure["exit"]]
    token = _transition.set(pending)
    try:
        invoke(
            "task_update",
            {
                "id": row["task_id"],
                "status": status,
                "actor": author,
                "expected_updated_at": closure["expected_updated_at"],
                "expected_run_id": task["current_run_id"],
                "worktree": False,
            },
        )
    finally:
        _transition.reset(token)
    return snapshot(instance_id, name)
