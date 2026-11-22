"""Plan-and-reason primitives backed by SQLite (task #61).

Dumb storage for the agent cluster: goal decomposition, checkpointing, and
bounded self-correction. There is NO LLM in the loop and NO automatic execution
here — the calling agent writes its own decomposition, decides each step, and
drives progress. These tools only persist and read back that state so work can
survive across turns, agents, and cold restarts.

Three independent capabilities:
  (A) Goal decomposition  — plan_create / plan_update
  (B) Checkpointing       — plan_checkpoint / plan_resume
  (C) Self-correction     — plan_step_retry (bounded retries → human escalation)
Plus plan_get / plan_list / plan_close / plan_delete for browsing and cleanup.
(Reasoning traces — part D of task #61 — are deferred to a later pass.)
"""

import inspect
import json
import re
import uuid
from collections.abc import Callable

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, edition, profiles, safety, storage
from ..sync import schedule_sync_to_vault
from . import plan_preview as preview_widget

STEP_STATUSES = ("open", "doing", "done", "failed", "blocked")
PLAN_STATUSES = ("active", "done", "blocked", "abandoned")


def _row(row) -> dict:
    return {k: row[k] for k in row.keys()}


def _connect_for(plan_id: str, agent: str | None = None):
    """Open a write transaction that refuses callers outside a joint plan's office (#1455).

    The membership check and the caller's write share one immediate transaction,
    so a concurrent joint bind cannot slip between them.
    """
    from ..joint_planning import require_joint_member

    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        require_joint_member(plan_id, agent, conn=conn)
    except BaseException:
        conn.rollback()
        conn.close()
        raise
    return conn


def _plan_dict(row) -> dict:
    """Plan row → dict, surfacing `needs_human` (bool alias of the `escalated` flag).

    ``title`` and ``description`` are read from ``goal`` (task #2480): its first
    line is the title and the text after it, stripped, is the description.
    """
    d = _row(row)
    d["needs_human"] = bool(d.get("escalated"))
    d["title"], d["description"] = _split_goal(d.get("goal") or "")
    return d


def _split_goal(goal: str) -> tuple[str, str]:
    title, _, rest = goal.strip().partition("\n")
    return title.strip(), rest.strip()


def _step_dict(row) -> dict:
    d = _row(row)
    d["depends_on"] = json.loads(d.get("depends_on") or "[]")
    # outputs (task 62): inter-agent payload left by the step's finisher.
    d["outputs"] = json.loads(d.get("outputs") or "{}")
    return d


def _new_plan_id() -> str:
    return uuid.uuid4().hex[:12]


def _normalize_steps(steps: list) -> list[dict]:
    """Accept a list of plain strings or dicts and return normalized step specs.

    Each item may be a bare description string, or a dict with keys
    `description` (required), `owner` (optional), and `depends_on` (optional list
    of 0-based step indices). `depends_on` entries must reference earlier-or-other
    valid steps and not the step itself.
    """
    if not isinstance(steps, list) or not steps:
        raise ToolError("steps must be a non-empty list")
    n = len(steps)
    normalized: list[dict] = []
    for i, raw in enumerate(steps):
        if isinstance(raw, str):
            spec = {"description": raw}
        elif isinstance(raw, dict):
            spec = dict(raw)
        else:
            raise ToolError(f"step {i} must be a string or an object, got {type(raw).__name__}")
        desc = str(spec.get("description", "")).strip()
        if not desc:
            raise ToolError(f"step {i} must have a non-empty description")
        owner = spec.get("owner")
        deps = spec.get("depends_on") or []
        if not isinstance(deps, list):
            raise ToolError(f"step {i} depends_on must be a list of step indices")
        clean_deps: list[int] = []
        for d in deps:
            if not isinstance(d, int) or isinstance(d, bool):
                raise ToolError(f"step {i} depends_on entries must be integer step indices")
            if d < 0 or d >= n:
                raise ToolError(f"step {i} depends_on index {d} is out of range (0..{n - 1})")
            if d == i:
                raise ToolError(f"step {i} cannot depend on itself")
            clean_deps.append(d)
        normalized.append(
            {
                "description": desc,
                "owner": str(owner) if owner is not None else None,
                "depends_on": sorted(set(clean_deps)),
            }
        )
    return normalized


def _get_plan(conn, plan_id: str):
    # Tenant-fenced (#2042): another tenant's plan reads exactly like an
    # unknown one. Every step and checkpoint read here follows this check.
    plan = conn.execute(
        "SELECT * FROM plans WHERE id=? AND org_id IS ?", (plan_id, edition.tenant_org_id())
    ).fetchone()
    if plan is None:
        raise ToolError(f"no plan with id {plan_id!r}")
    return plan


def _steps_for(conn, plan_id: str) -> list:
    return conn.execute(
        "SELECT * FROM plan_steps WHERE plan_id=? ORDER BY step_idx", (plan_id,)
    ).fetchall()


def _next_actionable(step_rows: list[dict]) -> int | None:
    """First not-done step whose dependencies are all done (a cold-resume hint)."""
    done = {s["step_idx"] for s in step_rows if s["status"] == "done"}
    for s in step_rows:
        if s["status"] == "done":
            continue
        if all(dep in done for dep in s["depends_on"]):
            return s["step_idx"]
    return None


def _maybe_autocomplete(conn, plan_id: str, ts: str) -> None:
    """Flip a non-escalated plan to done once every step is done."""
    rows = _steps_for(conn, plan_id)
    if rows and all(r["status"] == "done" for r in rows):
        conn.execute(
            "UPDATE plans SET status='done', updated_at=? "
            "WHERE id=? AND status='active' AND escalated=0",
            (ts, plan_id),
        )


def plan_create(
    goal: str,
    steps: list,
    scope: str = "default",
    author: str | None = None,
) -> dict:
    """Decompose a goal into ordered steps and persist them as a plan (task #61 · A).

    `steps` is a list of step descriptions; each item may be a plain string, or an
    object `{description, owner?, depends_on?}` where `depends_on` is a list of
    0-based indices of steps that must finish first. Steps are stored 0-indexed in
    list order. `scope` groups plans (mirrors memory's single-pool scope); `author`
    is your agent label, recorded as owner.

    This is dumb storage: no step is executed and no reasoning is done for you.
    Returns {plan_id, steps: [...]}.
    """
    if not goal.strip():
        raise ToolError("goal must not be empty")
    specs = _normalize_steps(steps)
    plan_id = _new_plan_id()
    ts = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO plans (id, goal, scope, status, author, created_at, updated_at, "
            "org_id) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)",
            (plan_id, goal.strip(), scope or "default", author, ts, ts, edition.tenant_org_id()),
        )
        for idx, spec in enumerate(specs):
            conn.execute(
                "INSERT INTO plan_steps "
                "(plan_id, step_idx, description, owner, depends_on, status, "
                " retries, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'open', 0, ?, ?)",
                (
                    plan_id,
                    idx,
                    spec["description"],
                    spec["owner"],
                    json.dumps(spec["depends_on"]),
                    ts,
                    ts,
                ),
            )
        conn.commit()
        rows = [_step_dict(r) for r in _steps_for(conn, plan_id)]
        return {"plan_id": plan_id, "steps": rows}
    finally:
        conn.close()


def plan_update(
    plan_id: str,
    step_idx: int,
    status: str | None = None,
    note: str | None = None,
    owner: str | None = None,
    expected_step_id: int | None = None,
) -> dict:
    """Update one step's status/note/owner (task #61 · A).

    `status` is one of open|doing|done|failed|blocked. Setting it to 'done' stamps
    `done_at`; any other status clears it. When every step is done the plan
    auto-completes (unless it has been escalated). This does not enforce
    dependency order — it is the agent's job to respect `depends_on`.

    Returns {step, plan, next_step_idx} where next_step_idx is the first not-done
    step whose dependencies are satisfied (or null).
    """
    if status is not None and status not in STEP_STATUSES:
        raise ToolError(f"status must be one of {STEP_STATUSES}")
    ts = storage.now()
    conn = _connect_for(plan_id)
    try:
        _get_plan(conn, plan_id)
        row = conn.execute(
            "SELECT * FROM plan_steps WHERE plan_id=? AND step_idx=?", (plan_id, step_idx)
        ).fetchone()
        _check_step_id(plan_id, step_idx, row, expected_step_id)
        if row is None:
            raise ToolError(f"plan {plan_id!r} has no step at index {step_idx}")

        sets, params = ["updated_at=?"], [ts]
        if status is not None:
            sets.append("status=?")
            params.append(status)
            sets.append("done_at=?")
            params.append(ts if status == "done" else None)
        if note is not None:
            sets.append("note=?")
            params.append(note)
        if owner is not None:
            sets.append("owner=?")
            params.append(owner)
        params.extend([plan_id, step_idx])
        conn.execute(
            f"UPDATE plan_steps SET {', '.join(sets)} WHERE plan_id=? AND step_idx=?", params
        )
        conn.execute("UPDATE plans SET updated_at=? WHERE id=?", (ts, plan_id))
        _maybe_autocomplete(conn, plan_id, ts)
        conn.commit()

        step = _step_dict(
            conn.execute(
                "SELECT * FROM plan_steps WHERE plan_id=? AND step_idx=?", (plan_id, step_idx)
            ).fetchone()
        )
        rows = [_step_dict(r) for r in _steps_for(conn, plan_id)]
        plan = _plan_dict(_get_plan(conn, plan_id))
        return {"step": step, "plan": plan, "next_step_idx": _next_actionable(rows)}
    finally:
        conn.close()


def plan_get(plan_id: str) -> dict:
    """Read a full plan: metadata, all steps, and all checkpoints (task #61).

    The plan carries a `needs_human` flag — true once a step has exhausted its
    retries and the plan is waiting on a human (see plan_step_retry).
    Returns {plan, steps: [...], checkpoints: [...], next_step_idx}.
    """
    conn = storage.connect()
    try:
        plan = _plan_dict(_get_plan(conn, plan_id))
        rows = [_step_dict(r) for r in _steps_for(conn, plan_id)]
        cps = conn.execute(
            "SELECT * FROM plan_checkpoints WHERE plan_id=? ORDER BY seq", (plan_id,)
        ).fetchall()
        checkpoints = []
        for c in cps:
            d = _row(c)
            d["context"] = json.loads(d.get("context") or "{}")
            d["context"].pop("joint", None)
            checkpoints.append(d)
        return {
            "plan": plan,
            "steps": rows,
            "checkpoints": checkpoints,
            "next_step_idx": _next_actionable(rows),
        }
    finally:
        conn.close()


def plan_list(status: str = "active", scope: str | None = None, limit: int = 50) -> dict:
    """List plans by status (active|done|blocked|abandoned|all), newest first (task #61).

    Optional `scope` filter. Plans waiting on a human (a step exhausted its
    retries) carry `needs_human=true`. Returns {plans: [...]}.
    """
    if status != "all" and status not in PLAN_STATUSES:
        raise ToolError(f"status must be one of {PLAN_STATUSES + ('all',)}")
    # Tenant-fenced (#2042): only the caller's plans are listed.
    sql, params = "SELECT * FROM plans WHERE org_id IS ?", [edition.tenant_org_id()]
    if status != "all":
        sql += " AND status=?"
        params.append(status)
    if scope is not None:
        sql += " AND scope=?"
        params.append(scope)
    params.append(max(1, min(limit, 200)))
    conn = storage.connect()
    try:
        rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", params).fetchall()
        return {"plans": [_plan_dict(r) for r in rows]}
    finally:
        conn.close()


def plan_checkpoint(
    plan_id: str,
    memory_ids: list[str] | None = None,
    file_paths: list[str] | None = None,
    outputs: dict | None = None,
    note: str | None = None,
) -> dict:
    """Snapshot a plan's context slice so an agent can resume it cold (task #61 · B).

    Stores the pointers an agent needs to pick the work back up: relevant memory
    note ids, file paths, and a free-form `outputs` map of key results. The
    current per-step status is captured automatically (read back by plan_resume);
    you only supply the surrounding context. Checkpoints are sequenced per plan
    starting at 1.

    Returns {plan_id, seq, checkpoint}.
    """
    context = {
        "memory_ids": list(memory_ids or []),
        "file_paths": list(file_paths or []),
        "outputs": outputs or {},
    }
    ts = storage.now()
    conn = _connect_for(plan_id)
    try:
        _get_plan(conn, plan_id)
        seq = _insert_checkpoint(conn, plan_id, context, note, ts)
        conn.execute("UPDATE plans SET updated_at=? WHERE id=?", (ts, plan_id))
        conn.commit()
        cp = conn.execute(
            "SELECT * FROM plan_checkpoints WHERE plan_id=? AND seq=?", (plan_id, seq)
        ).fetchone()
        d = _row(cp)
        d["context"] = json.loads(d["context"])
        return {"plan_id": plan_id, "seq": seq, "checkpoint": d}
    finally:
        conn.close()


def _insert_checkpoint(conn, plan_id: str, context: dict, note: str | None, ts: str) -> int:
    seq_row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM plan_checkpoints WHERE plan_id=?", (plan_id,)
    ).fetchone()
    seq = seq_row["m"] + 1
    # Snapshot step state alongside the caller-supplied context so a cold
    # reader sees exactly where things stood at checkpoint time.
    context["step_state"] = [
        {"step_idx": r["step_idx"], "status": r["status"], "description": r["description"]}
        for r in _steps_for(conn, plan_id)
    ]
    conn.execute(
        "INSERT INTO plan_checkpoints (plan_id, seq, context, note, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (plan_id, seq, json.dumps(context), note, ts),
    )
    return seq


def plan_resume(plan_id: str, from_checkpoint: int | None = None) -> dict:
    """Return everything needed to pick up a plan cold (task #61 · B).

    Reads back the plan, its live step state, the first actionable step, and a
    checkpoint's saved context slice — the latest checkpoint by default, or the
    one at `from_checkpoint` (its per-plan `seq`). If the plan has no checkpoints,
    `checkpoint` is null and the live step state is still returned.

    Returns {plan, steps, next_step_idx, checkpoint}.
    """
    from ..joint_planning import require_joint_member

    require_joint_member(plan_id)
    conn = storage.connect()
    try:
        plan = _plan_dict(_get_plan(conn, plan_id))
        rows = [_step_dict(r) for r in _steps_for(conn, plan_id)]
        if from_checkpoint is not None:
            cp = conn.execute(
                "SELECT * FROM plan_checkpoints WHERE plan_id=? AND seq=?",
                (plan_id, from_checkpoint),
            ).fetchone()
            if cp is None:
                raise ToolError(f"plan {plan_id!r} has no checkpoint seq {from_checkpoint}")
        else:
            cp = conn.execute(
                "SELECT * FROM plan_checkpoints WHERE plan_id=? ORDER BY seq DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        checkpoint = None
        if cp is not None:
            checkpoint = _row(cp)
            checkpoint["context"] = json.loads(checkpoint["context"] or "{}")
        return {
            "plan": plan,
            "steps": rows,
            "next_step_idx": _next_actionable(rows),
            "checkpoint": checkpoint,
        }
    finally:
        conn.close()


def plan_step_retry(
    plan_id: str, step_idx: int, reason: str, expected_step_id: int | None = None
) -> dict:
    """Record a failed attempt at a step with bounded retries + human escalation (task #61 · C).

    Increments the step's retry counter, marks it 'failed', and appends `reason`
    to its note. When the counter reaches the cap (UA_PLAN_MAX_RETRIES, default 3)
    the step is 'blocked' and the plan is set to 'blocked' with `needs_human=true`
    (surfaced by plan_get/plan_list) — a hard stop that flags the human rather
    than looping silently. This only RECORDS the escalation (channel =
    UA_PLAN_ESCALATION_CHANNEL); it sends no iMessage and takes no action —
    a caller/steward acts on the flag; delivery wiring is a later task.

    Returns {step, retries, escalated, max_retries, escalation_channel, plan}.
    """
    if not reason.strip():
        raise ToolError("reason must not be empty")
    cap = config.plan_max_retries()
    channel = config.plan_escalation_channel()
    ts = storage.now()
    conn = _connect_for(plan_id)
    try:
        _get_plan(conn, plan_id)
        row = conn.execute(
            "SELECT * FROM plan_steps WHERE plan_id=? AND step_idx=?", (plan_id, step_idx)
        ).fetchone()
        _check_step_id(plan_id, step_idx, row, expected_step_id)
        if row is None:
            raise ToolError(f"plan {plan_id!r} has no step at index {step_idx}")
        retries = row["retries"] + 1
        escalated = retries >= cap
        prior = row["note"]
        note = f"{prior}\n" if prior else ""
        note += f"[retry {retries}/{cap} @ {ts}] {reason.strip()}"
        step_status = "blocked" if escalated else "failed"
        conn.execute(
            "UPDATE plan_steps SET retries=?, status=?, note=?, updated_at=? "
            "WHERE plan_id=? AND step_idx=?",
            (retries, step_status, note, ts, plan_id, step_idx),
        )
        if escalated:
            esc_note = (
                f"step {step_idx} exhausted {retries}/{cap} retries; last reason: {reason.strip()}"
            )
            conn.execute(
                "UPDATE plans SET status='blocked', escalated=1, escalation_note=?, updated_at=? "
                "WHERE id=?",
                (esc_note, ts, plan_id),
            )
        else:
            conn.execute("UPDATE plans SET updated_at=? WHERE id=?", (ts, plan_id))
        conn.commit()

        step = _step_dict(
            conn.execute(
                "SELECT * FROM plan_steps WHERE plan_id=? AND step_idx=?", (plan_id, step_idx)
            ).fetchone()
        )
        plan = _plan_dict(_get_plan(conn, plan_id))
        return {
            "step": step,
            "retries": retries,
            "escalated": escalated,
            "max_retries": cap,
            "escalation_channel": channel,
            "plan": plan,
        }
    finally:
        conn.close()


def plan_step_claim(
    plan_id: str, step_idx: int, agent: str, expected_step_id: int | None = None
) -> dict:
    """Atomically claim a plan step for an agent (task #62 · chained workflows).

    The claim is a single conditional UPDATE, so two agents racing for the same
    step cannot both win: it succeeds only while the step is 'open' or 'failed'
    and either unowned or already owned by `agent` (re-claiming your own step
    after a failure is allowed). On success the step becomes 'doing' with
    `owner=agent`. On a lost race the error names the current owner/status.

    Returns {step, plan, claimed: true}.
    """
    if not agent.strip():
        raise ToolError("agent label must not be empty")
    agent = config.verify_write_identity(agent)
    if not agent:
        raise ToolError("agent label must not be empty")
    agent = agent.strip()
    ts = storage.now()
    conn = _connect_for(plan_id, agent)
    try:
        _get_plan(conn, plan_id)
        row = conn.execute(
            "SELECT * FROM plan_steps WHERE plan_id=? AND step_idx=?", (plan_id, step_idx)
        ).fetchone()
        _check_step_id(plan_id, step_idx, row, expected_step_id)
        if row is None:
            raise ToolError(f"plan {plan_id!r} has no step at index {step_idx}")
        cur = conn.execute(
            "UPDATE plan_steps SET owner=?, status='doing', updated_at=? "
            "WHERE plan_id=? AND step_idx=? AND status IN ('open','failed') "
            "AND (owner IS NULL OR owner=?)",
            (agent, ts, plan_id, step_idx, agent),
        )
        if cur.rowcount == 0:
            holder = row["owner"] or "(unowned)"
            raise ToolError(
                f"step {step_idx} of plan {plan_id!r} is not claimable: "
                f"status={row['status']!r}, owner={holder!r}. Claims only succeed on an "
                "open/failed step that is unowned or already yours."
            )
        conn.execute("UPDATE plans SET updated_at=? WHERE id=?", (ts, plan_id))
        conn.commit()
        try:
            storage.record_agent_event(
                "plan",
                ref_id=plan_id,
                agent=agent,
                summary=f"claimed step {step_idx}",
            )
        except Exception:
            pass
        step = _step_dict(
            conn.execute(
                "SELECT * FROM plan_steps WHERE plan_id=? AND step_idx=?", (plan_id, step_idx)
            ).fetchone()
        )
        return {"step": step, "plan": _plan_dict(_get_plan(conn, plan_id)), "claimed": True}
    finally:
        conn.close()


def _capture_worktree_diff() -> str | None:
    """Best-effort capture of uncommitted git diff from the active worktree."""
    try:
        from ..safety import get_active_worktree_path
        from .git import _run_git

        wt = get_active_worktree_path()
        if wt and wt.is_dir():
            res = _run_git(["diff", "HEAD"], str(wt))
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    except Exception:
        pass
    return None


def plan_handoff(
    plan_id: str,
    step_idx: int,
    outputs: dict | None = None,
    note: str | None = None,
    agent: str | None = None,
    notify_owner: bool = False,
    expected_step_id: int | None = None,
) -> dict:
    """Finish a step, record its outputs, and hand off to the next owner (task #62).

    Marks the step 'done', stores `outputs` (a JSON map — the explicit
    inter-agent payload the next step's owner reads via plan_get/plan_resume),
    and computes the next actionable step. The handoff is recorded in the
    agent_events activity index. With `notify_owner=True` and a next step that
    has an owner, a best-effort iMessage is additionally sent via `notify` so
    the operator can relay/prompt the next agent — delivery failure never fails the
    handoff (the DB record is the source of truth).

    Returns {step, next_step, plan, notified}.
    """
    agent = config.verify_write_identity(agent)
    ts = storage.now()
    outputs = outputs or {}
    diff = _capture_worktree_diff()
    if diff and "git_diff" not in outputs:
        outputs["git_diff"] = diff

    conn = _connect_for(plan_id, agent)
    try:
        _get_plan(conn, plan_id)
        row = conn.execute(
            "SELECT * FROM plan_steps WHERE plan_id=? AND step_idx=?", (plan_id, step_idx)
        ).fetchone()
        _check_step_id(plan_id, step_idx, row, expected_step_id)
        if row is None:
            raise ToolError(f"plan {plan_id!r} has no step at index {step_idx}")
        # Checkpoint for cold recovery, in the same transaction as the handoff.
        _insert_checkpoint(
            conn,
            plan_id,
            {"memory_ids": [], "file_paths": [], "outputs": dict(outputs)},
            note or f"Automatic checkpoint on step {step_idx} handoff",
            ts,
        )
        sets = ["status='done'", "done_at=?", "outputs=?", "updated_at=?"]
        params: list = [ts, json.dumps(outputs or {}), ts]
        if note is not None:
            sets.append("note=?")
            params.append(note)
        params.extend([plan_id, step_idx])
        conn.execute(
            f"UPDATE plan_steps SET {', '.join(sets)} WHERE plan_id=? AND step_idx=?", params
        )
        conn.execute("UPDATE plans SET updated_at=? WHERE id=?", (ts, plan_id))
        _maybe_autocomplete(conn, plan_id, ts)
        conn.commit()

        rows = [_step_dict(r) for r in _steps_for(conn, plan_id)]
        next_idx = _next_actionable(rows)
        next_step = next((s for s in rows if s["step_idx"] == next_idx), None)
        step = next(s for s in rows if s["step_idx"] == step_idx)
        plan = _plan_dict(_get_plan(conn, plan_id))
    finally:
        conn.close()

    finisher = agent or step.get("owner")
    next_owner = next_step.get("owner") if next_step else None
    try:
        storage.record_agent_event(
            "plan",
            ref_id=plan_id,
            agent=finisher,
            summary=(
                f"handoff: step {step_idx} done"
                + (f" -> step {next_idx} awaits {next_owner}" if next_owner else "")
            ),
        )
    except Exception:
        pass

    notified = False
    if notify_owner and next_step is not None and next_owner:
        try:
            from .notify import notify as _notify

            res = _notify(
                f"[plan {plan_id}] step {step_idx} done"
                + (f" by {finisher}" if finisher else "")
                + f"; step {next_idx} ({next_step['description'][:120]}) "
                f"awaits {next_owner}. Resume with plan_resume('{plan_id}')."
            )
            notified = bool(res.get("delivered"))
        except Exception:
            notified = False  # best-effort: never fail the handoff on delivery

    return {"step": step, "next_step": next_step, "plan": plan, "notified": notified}


TERMINAL_PLAN_STATUSES = ("done", "abandoned")


def plan_close(plan_id: str, reason: str, outputs: dict | None = None) -> dict:
    """Close a plan as 'abandoned' with a recorded reason, keeping its history (task #1496).

    A plan otherwise reaches a terminal state only when every step is done.
    A plan whose remaining steps were superseded, delivered elsewhere, or
    dropped would stay 'active' forever — and keep surfacing in
    ``plan_list(status='active')`` — unless deleted, which destroys its steps
    and checkpoints. This records a final checkpoint carrying ``reason``, the
    optional ``outputs`` (e.g. a step → task/PR evidence map) and the step
    state, then marks the plan 'abandoned'. Steps are left exactly as they
    were. A plan that is already terminal is refused.

    Returns {plan, seq, checkpoint}.
    """
    reason = (reason or "").strip() if isinstance(reason, str) else ""
    if not reason:
        raise ToolError("reason must be a non-empty string")
    if outputs is not None and not isinstance(outputs, dict):
        raise ToolError("outputs must be an object")
    ts = storage.now()
    conn = _connect_for(plan_id)
    try:
        plan = _get_plan(conn, plan_id)
        if plan["status"] in TERMINAL_PLAN_STATUSES:
            raise ToolError(f"plan {plan_id!r} is already {plan['status']}")
        seq = (
            conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM plan_checkpoints WHERE plan_id=?",
                (plan_id,),
            ).fetchone()["m"]
            + 1
        )
        context = {
            "memory_ids": [],
            "file_paths": [],
            "outputs": {**(outputs or {}), "closed_as": "abandoned", "reason": reason},
            "step_state": [
                {"step_idx": r["step_idx"], "status": r["status"], "description": r["description"]}
                for r in _steps_for(conn, plan_id)
            ],
        }
        conn.execute(
            "INSERT INTO plan_checkpoints (plan_id, seq, context, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (plan_id, seq, json.dumps(context), f"closed as abandoned: {reason}", ts),
        )
        conn.execute("UPDATE plans SET status='abandoned', updated_at=? WHERE id=?", (ts, plan_id))
        conn.commit()
        cp = _row(
            conn.execute(
                "SELECT * FROM plan_checkpoints WHERE plan_id=? AND seq=?", (plan_id, seq)
            ).fetchone()
        )
        cp["context"] = json.loads(cp["context"])
        return {"plan": _plan_dict(_get_plan(conn, plan_id)), "seq": seq, "checkpoint": cp}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def plan_delete(plan_id: str) -> dict:
    """Delete a plan and its steps + checkpoints (cascade). Returns {deleted}."""
    conn = _connect_for(plan_id)
    try:
        # A foreign plan deletes nothing, like an unknown one (#2042).
        cur = conn.execute(
            "DELETE FROM plans WHERE id=? AND org_id IS ?", (plan_id, edition.tenant_org_id())
        )
        conn.commit()
        return {"deleted": cur.rowcount > 0}
    finally:
        conn.close()


# --- plan structure (task #2480) ------------------------------------------------------
#
# Steps keep their row id (``plan_steps.id``); ``step_idx`` is their position and is
# what every other plan action addresses. A structure change renumbers positions and
# rewrites ``depends_on`` through the same old -> new map in one write transaction.
# A step that is claimed (``doing``) or ``done`` is never edited, removed or
# renumbered: its claimant and its handoff address it by ``step_idx``. Joint plans
# change their steps only through ``joint_propose`` (peer-reviewed revisions).

_UNTOUCHABLE_STEP_STATUSES = ("doing", "done")
PLAN_TITLE_MAX = 200
#: A LangGraph workflow binds its plan by this exact goal text
#: (``langgraph_workflow.PLAN_GOAL_PREFIX`` + run id); recovery matches it in full.
_WORKFLOW_PLAN_GOAL_RE = re.compile(r"governed workflow [0-9A-HJKMNP-TV-Z]{26}")
PLAN_DESCRIPTION_MAX = 4_000


def _open_structure_edit(plan_id: str, expected_revision: int | None, *, steps: bool = True):
    """Start a structure edit: (conn, plan row, step dicts), or raise.

    Refuses a workflow-bound plan (its goal text and its fixed step layout are the
    LangGraph run's binding), a terminal plan, a joint plan's steps, and a plan
    whose ``revision`` no longer matches ``expected_revision`` (``ConflictError``,
    HTTP 409).
    """
    from ..joint_planning import _latest as joint_latest
    from ..tool_errors import ConflictError

    conn = _connect_for(plan_id)
    try:
        plan = _get_plan(conn, plan_id)
        if _WORKFLOW_PLAN_GOAL_RE.fullmatch(plan["goal"] or ""):
            raise ToolError(
                f"plan {plan_id!r} is workflow-bound: its goal text and step layout bind it "
                "to a LangGraph run, so its title, description and steps cannot be edited"
            )
        if expected_revision is not None and (
            isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
        ):
            raise ToolError("expected_revision must be an integer")
        if expected_revision is not None and plan["revision"] != expected_revision:
            raise ConflictError(
                f"plan {plan_id!r} changed since it was read (revision {plan['revision']}, "
                f"expected {expected_revision}); reload it and try again",
                expected_revision=expected_revision,
                current_revision=plan["revision"],
            )
        if steps and plan["status"] in TERMINAL_PLAN_STATUSES:
            raise ToolError(f"plan {plan_id!r} is {plan['status']}; its steps cannot change")
        if steps and joint_latest(conn, plan_id) is not None:
            raise ToolError(
                f"plan {plan_id!r} is a joint plan; change its steps through joint_propose"
            )
        rows = [_step_dict(r) for r in _steps_for(conn, plan_id)]
    except BaseException:
        conn.rollback()
        conn.close()
        raise
    return conn, plan, rows


def _check_step_id(plan_id: str, step_idx: int, row, expected_step_id: int | None) -> None:
    """Refuse an index-addressed write when the step at ``step_idx`` is no longer the
    one the caller read (task #2480): a reorder, insert or removal moved it.

    ``expected_step_id`` is the step's immutable row id (``plan_steps.id``). Checked
    inside the writer's transaction, before the missing-index check, so an index
    that no longer exists is the same conflict. Omitted, the write addresses the
    index alone, as before.
    """
    from ..tool_errors import ConflictError

    if expected_step_id is None:
        return
    if isinstance(expected_step_id, bool) or not isinstance(expected_step_id, int):
        raise ToolError("expected_step_id must be an integer")
    if row is None or row["id"] != expected_step_id:
        raise ConflictError(
            f"step {step_idx} of plan {plan_id!r} is not step id {expected_step_id}: the "
            "steps moved since they were read; reload the plan and try again",
            expected_step_id=expected_step_id,
            current_step_id=row["id"] if row is not None else None,
        )


def _refuse_untouchable(step: dict, verb: str) -> None:
    from ..tool_errors import ConflictError

    if step["status"] in _UNTOUCHABLE_STEP_STATUSES:
        raise ConflictError(
            f"step {step['step_idx']} is {step['status']}; a claimed or done step cannot be {verb}"
        )


def _step_at(rows: list[dict], plan_id: str, step_idx: int) -> dict:
    if isinstance(step_idx, bool) or not isinstance(step_idx, int):
        raise ToolError("step_idx must be an integer")
    step = next((s for s in rows if s["step_idx"] == step_idx), None)
    if step is None:
        raise ToolError(f"plan {plan_id!r} has no step at index {step_idx}")
    return step


def _apply_layout(conn, rows: list[dict], new_index: dict[int, int], ts: str) -> None:
    """Move each kept step to ``new_index[old]`` and remap every ``depends_on``.

    Refuses before writing when a claimed or done step would be renumbered. Two
    passes keep ``UNIQUE(plan_id, step_idx)`` satisfied: park every row at a
    negative index unique to its row id, then write the final positions.
    """
    kept = [r for r in rows if r["step_idx"] in new_index]
    for r in kept:
        if new_index[r["step_idx"]] != r["step_idx"]:
            _refuse_untouchable(r, "renumbered")
    for r in kept:
        conn.execute("UPDATE plan_steps SET step_idx=? WHERE id=?", (-1 - r["id"], r["id"]))
    for r in kept:
        old = r["step_idx"]
        deps = sorted({new_index[d] for d in r["depends_on"] if d in new_index})
        moved = new_index[old] != old or deps != r["depends_on"]
        conn.execute(
            "UPDATE plan_steps SET step_idx=?, depends_on=?, updated_at=? WHERE id=?",
            (new_index[old], json.dumps(deps), ts if moved else r["updated_at"], r["id"]),
        )


def _structure_result(conn, plan_id: str, ts: str, summary: str, step_idx: int | None) -> dict:
    conn.execute("UPDATE plans SET updated_at=? WHERE id=?", (ts, plan_id))
    conn.commit()
    try:
        storage.record_agent_event(
            "plan", ref_id=plan_id, agent=config.verified_agent_identity(), summary=summary
        )
    except Exception:
        pass
    rows = [_step_dict(r) for r in _steps_for(conn, plan_id)]
    out = {
        "plan": _plan_dict(_get_plan(conn, plan_id)),
        "steps": rows,
        "next_step_idx": _next_actionable(rows),
    }
    if step_idx is not None:
        out["step"] = next(s for s in rows if s["step_idx"] == step_idx)
    return out


def _clean_description(description: str) -> str:
    text = description.strip() if isinstance(description, str) else ""
    if not text:
        raise ToolError("description must be a non-empty string")
    return text


def plan_step_add(
    plan_id: str,
    description: str,
    position: int | None = None,
    owner: str | None = None,
    depends_on: list | None = None,
    expected_revision: int | None = None,
) -> dict:
    """Insert a step at ``position`` (default: the end) of an active plan (task #2480).

    ``depends_on`` lists the current 0-based indices of steps the new one waits
    for. Steps at or after ``position`` move down one place and every
    ``depends_on`` is remapped; a claimed (doing) or done step is never moved, so an
    insertion that would move one is refused. ``expected_revision`` (the plan's
    ``updated_at`` as last read) turns a concurrent change into a conflict.

    Returns {plan, steps, step, next_step_idx}.
    """
    text = _clean_description(description)
    conn, _plan, rows = _open_structure_edit(plan_id, expected_revision)
    ts = storage.now()  # under the write lock
    try:
        n = len(rows)
        pos = n if position is None else position
        if isinstance(pos, bool) or not isinstance(pos, int) or not 0 <= pos <= n:
            raise ToolError(f"position must be an integer between 0 and {n}")
        deps = depends_on or []
        if not isinstance(deps, list) or any(
            isinstance(d, bool) or not isinstance(d, int) for d in deps
        ):
            raise ToolError("depends_on must be a list of integer step indices")
        for d in deps:
            if not 0 <= d < n:
                raise ToolError(f"depends_on index {d} is out of range (0..{n - 1})")
        new_index = {s["step_idx"]: s["step_idx"] + (s["step_idx"] >= pos) for s in rows}
        _apply_layout(conn, rows, new_index, ts)
        conn.execute(
            "INSERT INTO plan_steps (plan_id, step_idx, description, owner, depends_on, status, "
            "retries, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'open', 0, ?, ?)",
            (
                plan_id,
                pos,
                text,
                str(owner) if owner is not None else None,
                json.dumps(sorted({new_index[d] for d in deps})),
                ts,
                ts,
            ),
        )
        return _structure_result(conn, plan_id, ts, f"added step {pos}", pos)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def plan_step_edit(
    plan_id: str,
    step_idx: int,
    description: str,
    expected_revision: int | None = None,
    expected_step_id: int | None = None,
) -> dict:
    """Replace one open, failed or blocked step's text (task #2480).

    A claimed (doing) or done step keeps its text. Returns {plan, steps, step,
    next_step_idx}.
    """
    text = _clean_description(description)
    conn, _plan, rows = _open_structure_edit(plan_id, expected_revision)
    ts = storage.now()  # under the write lock
    try:
        _check_step_id(
            plan_id,
            step_idx,
            next((r for r in rows if r["step_idx"] == step_idx), None),
            expected_step_id,
        )
        step = _step_at(rows, plan_id, step_idx)
        _refuse_untouchable(step, "edited")
        conn.execute(
            "UPDATE plan_steps SET description=?, updated_at=? WHERE id=?", (text, ts, step["id"])
        )
        return _structure_result(conn, plan_id, ts, f"edited step {step_idx}", step_idx)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def plan_step_remove(
    plan_id: str,
    step_idx: int,
    expected_revision: int | None = None,
    expected_step_id: int | None = None,
) -> dict:
    """Remove one step and close the gap (task #2480).

    Refused when another step depends on it, when it is claimed (doing) or done, or
    when closing the gap would renumber a claimed or done step. Later steps move up
    one place and every ``depends_on`` is remapped. Returns {plan, steps,
    next_step_idx, removed}.
    """
    conn, _plan, rows = _open_structure_edit(plan_id, expected_revision)
    ts = storage.now()  # under the write lock
    try:
        _check_step_id(
            plan_id,
            step_idx,
            next((r for r in rows if r["step_idx"] == step_idx), None),
            expected_step_id,
        )
        step = _step_at(rows, plan_id, step_idx)
        _refuse_untouchable(step, "removed")
        if len(rows) == 1:
            raise ToolError(f"step {step_idx} is the plan's only step; a plan keeps at least one")
        dependents = [s["step_idx"] for s in rows if step_idx in s["depends_on"]]
        if dependents:
            names = ", ".join(f"step {d}" for d in dependents)
            raise ToolError(
                f"cannot remove step {step_idx}: {names} depends on it; "
                "remove or edit that dependency first"
            )
        rest = [s for s in rows if s["step_idx"] != step_idx]
        new_index = {s["step_idx"]: s["step_idx"] - (s["step_idx"] > step_idx) for s in rest}
        for s in rest:
            if new_index[s["step_idx"]] != s["step_idx"]:
                _refuse_untouchable(s, "renumbered")
        conn.execute("DELETE FROM plan_steps WHERE id=?", (step["id"],))
        _apply_layout(conn, rest, new_index, ts)
        out = _structure_result(conn, plan_id, ts, f"removed step {step_idx}", None)
        out["removed"] = step
        return out
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def plan_step_reorder(plan_id: str, order: list, expected_revision: int | None = None) -> dict:
    """Reorder all steps (task #2480).

    ``order`` is a permutation of the current indices: ``order[i]`` is the current
    index of the step that moves to position ``i``. Every ``depends_on`` is
    remapped. Refused when it moves a step ahead of a step it depends on, or moves
    a claimed (doing) or done step. Returns {plan, steps, next_step_idx}.
    """
    conn, _plan, rows = _open_structure_edit(plan_id, expected_revision)
    ts = storage.now()  # under the write lock
    try:
        n = len(rows)
        if (
            not isinstance(order, list)
            or any(isinstance(i, bool) or not isinstance(i, int) for i in order)
            or sorted(order) != list(range(n))
        ):
            raise ToolError(f"order must be a permutation of the step indices 0..{n - 1}")
        new_index = {old: new for new, old in enumerate(order)}
        for s in rows:
            for d in s["depends_on"]:
                was_before = d < s["step_idx"]
                if was_before and new_index[d] > new_index[s["step_idx"]]:
                    raise ToolError(
                        f"order moves step {s['step_idx']} ahead of step {d}, which it depends on"
                    )
        _apply_layout(conn, rows, new_index, ts)
        return _structure_result(conn, plan_id, ts, "reordered steps", None)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def plan_edit(
    plan_id: str,
    title: str | None = None,
    description: str | None = None,
    expected_revision: int | None = None,
) -> dict:
    """Edit a plan's title and description (task #2480).

    Both live in the existing ``goal`` column: the title is its first line and the
    description the text after a blank line (``plan_get`` reports them as ``title``
    and ``description``). An omitted field keeps its current value; an empty
    ``description`` clears it. Returns {plan, steps, next_step_idx}.
    """
    if title is None and description is None:
        raise ToolError("pass title, description or both")
    conn, plan, _rows = _open_structure_edit(plan_id, expected_revision, steps=False)
    ts = storage.now()  # under the write lock
    try:
        current_title, current_description = _split_goal(plan["goal"])
        if title is not None:
            title = title.strip() if isinstance(title, str) else ""
            if not title or "\n" in title or len(title) > PLAN_TITLE_MAX:
                raise ToolError(
                    f"title must be one nonempty line of at most {PLAN_TITLE_MAX} characters"
                )
        if description is not None:
            if not isinstance(description, str):
                raise ToolError("description must be a string")
            description = description.strip()
            if len(description) > PLAN_DESCRIPTION_MAX:
                raise ToolError(f"description must be at most {PLAN_DESCRIPTION_MAX} characters")
        new_title = title if title is not None else current_title
        new_description = description if description is not None else current_description
        goal = new_title + (f"\n\n{new_description}" if new_description else "")
        conn.execute("UPDATE plans SET goal=? WHERE id=?", (goal, plan_id))
        return _structure_result(conn, plan_id, ts, "edited title and description", None)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# The plan-review task reads and rewrite below are fenced on the plan row's
# tenant (#2045), not the caller's: another tenant's task naming this plan id
# in its notes is neither a gate nor rewritten.
def _gate_tasks_for_plan(plan_id: str, org_id: str | None) -> list[dict]:
    """Tasks currently sitting on the human plan-confirmation gate for this plan."""
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT id, notes, status FROM tasks WHERE notes LIKE ? AND org_id IS ?"
            " AND status NOT IN ('done','cancelled')",
            (f"%{preview_widget.WAITING_FOR_PLAN_APPROVAL}%", org_id),
        ).fetchall()
    finally:
        conn.close()
    matched = []
    for row in rows:
        notes = row["notes"] or ""
        if preview_widget.plan_id_from_notes(notes) == plan_id:
            matched.append({k: row[k] for k in row.keys()})
    return matched


def _approved_tasks_for_plan(plan_id: str, org_id: str | None) -> list[dict]:
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT id, notes, status FROM tasks WHERE notes LIKE ? AND org_id IS ?",
            (f"%{preview_widget.APPROVED_MARKER}%", org_id),
        ).fetchall()
    finally:
        conn.close()
    matched = []
    for row in rows:
        notes = row["notes"] or ""
        if preview_widget.plan_id_from_notes(notes) != plan_id:
            continue
        if preview_widget.WAITING_FOR_PLAN_APPROVAL in notes:
            continue
        matched.append({k: row[k] for k in row.keys()})
    return matched


def _rewrite_task_notes(task_id: int, edit: Callable[[str], str], org_id: str | None) -> None:
    """Apply ``edit`` to the task's current notes without claiming the row.

    The read and the write share the writer lock (``next_task_revision`` takes
    it), so a note appended after the caller's gate read is edited, not lost.
    """
    conn = storage.connect()
    try:
        revision = storage.next_task_revision(conn, task_id)
        row = conn.execute(
            "SELECT notes FROM tasks WHERE id=? AND org_id IS ?", (task_id, org_id)
        ).fetchone()
        if row is None:
            raise ToolError(f"no task with id {task_id}")
        conn.execute(
            "UPDATE tasks SET notes=?, updated_at=? WHERE id=? AND org_id IS ?",
            (edit(row["notes"] or ""), revision, task_id, org_id),
        )
        conn.commit()
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
    schedule_sync_to_vault()


def _select_gate_task(plan_id: str, task_id: int | None, org_id: str | None) -> dict | None:
    waiting = _gate_tasks_for_plan(plan_id, org_id)
    if task_id is not None:
        for row in waiting:
            if int(row["id"]) == int(task_id):
                return row
        approved = [
            r for r in _approved_tasks_for_plan(plan_id, org_id) if int(r["id"]) == int(task_id)
        ]
        return approved[0] if approved else None
    if len(waiting) == 1:
        return waiting[0]
    if not waiting:
        approved = _approved_tasks_for_plan(plan_id, org_id)
        return approved[0] if len(approved) == 1 else None
    raise ToolError(
        f"plan {plan_id!r} has {len(waiting)} tasks waiting for plan-approval; pass task_id"
    )


def _preview_payload(plan_id: str, plan_data: dict | None = None) -> dict:
    data = plan_data or plan_get(plan_id)
    pid = preview_widget.require_plan_id(str(data["plan"]["id"]))
    approved = bool(_approved_tasks_for_plan(pid, data["plan"]["org_id"]))
    html_doc = preview_widget.render_plan_preview_html(data, approved=approved)
    path = preview_widget.write_preview_html(pid, html_doc)
    fingerprint = preview_widget.plan_state_fingerprint(data)
    return {
        "plan_id": pid,
        "html_path": str(path),
        "html_relpath": preview_widget.preview_relpath(pid),
        "directive": preview_widget.preview_directive(pid),
        "fingerprint": fingerprint,
        "approved": approved,
    }


def plan_preview(plan_id: str) -> dict:
    """Render an active plan as a desktop ``::preview`` widget (task #838).

    Writes escaped HTML to ``.unified-agent/plan-preview/<plan_id>.html`` and
    returns the ``::preview{file=…}`` directive plus a state fingerprint.
    Rewriting the same path is the live-reload mechanism: the desktop re-reads
    the file after the turn settles. Plan body text is escaped; buttons emit
    only the fixed ``plan-review v1`` intent schema.
    """
    return _preview_payload(plan_id)


def plan_review(intent: str, task_id: int | None = None) -> dict:
    """Apply a ``plan-review v1`` widget intent onto the canonical plan gate.

    Approve replaces ``[waiting-for: plan-approval]`` with ``[plan: approved @ ts]``
    on the linked task — the same marker the plan-approval consumer and
    approved-dispatch coordinator already honor. Request-changes is idempotent
    on the current plan fingerprint and does **not** stamp approved or drop
    the waiting marker. Annotations append to the step note. The pipeline
    still never auto-approves.
    """
    parsed = preview_widget.parse_review_intent(intent)
    plan_id = parsed["plan_id"]
    from ..joint_planning import require_joint_member

    require_joint_member(plan_id)
    action = parsed["action"]
    data = plan_get(plan_id)
    org_id = data["plan"]["org_id"]
    gate = _select_gate_task(plan_id, task_id, org_id)
    ts = storage.now()

    if action == "approve":
        if gate is None:
            raise ToolError(f"plan {plan_id!r} is not on the [waiting-for: plan-approval] gate")
        notes = gate["notes"] or ""
        if preview_widget.WAITING_FOR_PLAN_APPROVAL not in notes:
            preview = _preview_payload(plan_id, data)
            preview.update(
                {"applied": False, "already": True, "action": action, "task_id": gate["id"]}
            )
            return preview
        _rewrite_task_notes(
            int(gate["id"]),
            lambda current: current.replace(
                preview_widget.WAITING_FOR_PLAN_APPROVAL, f"[plan: approved @ {ts}]", 1
            ),
            org_id,
        )
        data = plan_get(plan_id)
        preview = _preview_payload(plan_id, data)
        preview.update({"applied": True, "already": False, "action": action, "task_id": gate["id"]})
        return preview

    if action == "changes":
        if gate is None or preview_widget.WAITING_FOR_PLAN_APPROVAL not in (gate["notes"] or ""):
            raise ToolError(f"plan {plan_id!r} is not on the [waiting-for: plan-approval] gate")
        fingerprint = preview_widget.plan_state_fingerprint(data)
        notes = gate["notes"] or ""
        marker_line = f"[plan: changes-requested @ {ts}] {fingerprint}"
        if fingerprint in notes and preview_widget.CHANGES_MARKER in notes:
            preview = _preview_payload(plan_id, data)
            preview.update(
                {"applied": False, "already": True, "action": action, "task_id": gate["id"]}
            )
            return preview
        _rewrite_task_notes(
            int(gate["id"]), lambda current: current.rstrip() + "\n" + marker_line, org_id
        )
        preview = _preview_payload(plan_id, data)
        preview.update({"applied": True, "already": False, "action": action, "task_id": gate["id"]})
        return preview

    # annotate
    if gate is None:
        raise ToolError(f"plan {plan_id!r} is not on the [waiting-for: plan-approval] gate")
    step_idx = parsed["step_idx"]
    comment = parsed["comment"]
    marker = f"[plan-annotation:{preview_widget.annotation_id(plan_id, step_idx, comment)}]"
    steps = data["steps"]
    step = next((s for s in steps if s["step_idx"] == step_idx), None)
    if step is None:
        raise ToolError(f"plan {plan_id!r} has no step at index {step_idx}")
    prior = step.get("note") or ""
    if marker in prior:
        preview = _preview_payload(plan_id, data)
        preview.update({"applied": False, "already": True, "action": action, "task_id": gate["id"]})
        return preview
    note = (prior + "\n" if prior else "") + f"{marker} @ {ts}: {comment}"
    plan_update(plan_id, step_idx, note=note)
    data = plan_get(plan_id)
    preview = _preview_payload(plan_id, data)
    preview.update({"applied": True, "already": False, "action": action, "task_id": gate["id"]})
    return preview


def plan_joint_bind(
    plan_id: str,
    goal_id: int,
    org_unit_id: str,
    comment_id: int | None = None,
    author: str | None = None,
) -> dict:
    """Bind an active plan to a goal and an org unit (task #1455)."""
    from ..joint_planning import bind

    return bind(
        plan_id=plan_id,
        goal_id=goal_id,
        org_unit_id=org_unit_id,
        comment_id=comment_id,
        author=author,
    )


def plan_joint_propose(
    plan_id: str,
    expected_revision: int,
    steps: list,
    comment_id: int,
    author: str | None = None,
) -> dict:
    """Stage a joint step revision without changing live steps (task #1455)."""
    from ..joint_planning import propose

    return propose(
        plan_id=plan_id,
        expected_revision=expected_revision,
        steps=steps,
        comment_id=comment_id,
        author=author,
    )


def plan_joint_review(
    plan_id: str,
    expected_revision: int,
    decision: str,
    comment_id: int,
    author: str | None = None,
) -> dict:
    """A different org member accepts or requests changes (task #1455)."""
    from ..joint_planning import review

    return review(
        plan_id=plan_id,
        expected_revision=expected_revision,
        decision=decision,
        comment_id=comment_id,
        author=author,
    )


def plan_joint_decide(
    plan_id: str,
    expected_revision: int,
    text: str,
    comment_id: int,
    author: str | None = None,
) -> dict:
    """Record a decision linked to a goal id and a comment id (task #1455)."""
    from ..joint_planning import decide

    return decide(
        plan_id=plan_id,
        expected_revision=expected_revision,
        text=text,
        comment_id=comment_id,
        author=author,
    )


def plan_joint_get(plan_id: str, author: str | None = None) -> dict:
    """Read the joint contract, why-chain, and live team roles (task #1455)."""
    from ..joint_planning import get_contract

    return get_contract(plan_id, author=author)


def plan_joint_recover(plan_id: str, author: str | None = None) -> dict:
    """Re-read the winning contract after a conflict or restart (task #1455)."""
    from ..joint_planning import recover

    return recover(plan_id, author=author)


# The HTTP-facing compatibility layer uses one stable entry point while the
# legacy plan_* functions remain available to stdio clients during migration.
_PLAN_ACTIONS = {
    "create": plan_create,
    "update": plan_update,
    "get": plan_get,
    "list": plan_list,
    "checkpoint": plan_checkpoint,
    "resume": plan_resume,
    "retry": plan_step_retry,
    "claim": plan_step_claim,
    "handoff": plan_handoff,
    "close": plan_close,
    "delete": plan_delete,
    "step_add": plan_step_add,
    "step_edit": plan_step_edit,
    "step_remove": plan_step_remove,
    "step_reorder": plan_step_reorder,
    "edit": plan_edit,
    "preview": plan_preview,
    "review": plan_review,
    "joint_bind": plan_joint_bind,
    "joint_propose": plan_joint_propose,
    "joint_review": plan_joint_review,
    "joint_decide": plan_joint_decide,
    "joint_get": plan_joint_get,
    "joint_recover": plan_joint_recover,
}


def plan_manage(action: str, params: dict | None = None) -> dict:
    """Dispatch the complete plan lifecycle through one versioned API.

    ``action`` is one of create, update, get, list, checkpoint, resume, retry,
    claim, handoff, close, delete, step_add, step_edit, step_remove,
    step_reorder, edit, preview, review, joint_bind, joint_propose,
    joint_review, joint_decide, joint_get, or joint_recover. ``close`` marks a
    plan 'abandoned' with a recorded reason and keeps its history (task #1496).
    ``step_add``/``step_edit``/``step_remove``/``step_reorder`` change an active,
    non-joint plan's steps without touching a claimed or done step, and ``edit``
    sets the title and description kept in ``goal`` (task #2480).
    The joint_* actions are the shared-revision contract (task #1455): optimistic
    proposals over an existing plan, peer review, recorded decisions, and
    conflict recovery. They add no table and no scheduler. ``params`` contains
    the exact keyword arguments for that action. Legacy ``plan_*`` tools remain registered
    for stdio and older clients; authenticated HTTP profiles use this
    compatibility surface. ``preview`` / ``review`` are the desktop
    ``::preview`` widget contract (task #838): escaped HTML plus the fixed
    ``plan-review v1`` intent schema mapped onto the existing plan-approval gate.
    """
    if not isinstance(action, str):
        raise ToolError("action must be a string")
    action = action.strip().lower()
    fn = _PLAN_ACTIONS.get(action)
    if fn is None:
        raise ToolError(f"action must be one of {tuple(_PLAN_ACTIONS)}")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ToolError("params must be an object")
    if not profiles.consolidated_action_is_allowed("plan_manage", fn.__name__):
        raise ToolError(f"plan action {action!r} is not exposed by the active profile")
    try:
        bound = inspect.signature(fn).bind(**params)
    except TypeError as exc:
        raise ToolError(f"invalid params for plan action {action!r}: {exc}") from exc
    safety.check_policy_gate(fn.__name__, bound.arguments)
    return fn(*bound.args, **bound.kwargs)
