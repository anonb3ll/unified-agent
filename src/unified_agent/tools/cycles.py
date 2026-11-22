"""Bounded, idempotent state transitions for autonomous development cycles."""

import inspect
import json
import math
import re
import time
import uuid

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, events, profiles, run_records, safety, storage
from .approvals import check_approval_gate

# Evidence contract (task #373 Tier 2, 3.2 — extends #372's evidence_ref
# pattern from task_update(done) to autonomous_cycle_transition). A "pr_url"
# that is just some non-empty string is not evidence, it's prose with a
# different name — this requires it to actually look like a pointer: an
# http(s) URL, or a bare commit SHA (7-40 hex chars, matching short-to-full
# git SHA lengths). `completed` inherits this for free: TRANSITIONS below
# only reaches `completed` from `pr`, so every completed cycle already carries
# a pr_url that passed this check when the cycle entered `pr`.
_EVIDENCE_URL_RE = re.compile(r"^https?://\S+$")
_EVIDENCE_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _looks_like_evidence(value: str) -> bool:
    return bool(_EVIDENCE_URL_RE.match(value) or _EVIDENCE_SHA_RE.match(value))


STATES = (
    "plan",
    "delegated",
    "executing",
    "validating",
    "reviewing",
    "rework",
    "pr",
    "blocked",
    "completed",
    "failed",
    "killed",
)
TERMINAL_STATES = {"completed", "failed", "killed"}
TRANSITIONS = {
    "plan": {"delegated", "blocked", "failed", "killed"},
    "delegated": {"executing", "blocked", "failed", "killed"},
    "executing": {"validating", "rework", "blocked", "failed", "killed"},
    "validating": {"reviewing", "rework", "blocked", "failed", "killed"},
    "reviewing": {"pr", "rework", "blocked", "failed", "killed"},
    "rework": {"delegated", "executing", "blocked", "failed", "killed"},
    "pr": {"completed", "blocked", "failed", "killed"},
    # 'killed' is reachable from 'blocked' too — an operator can definitively
    # stop a soft-blocked cycle rather than leave it resumable to 'plan'.
    "blocked": {"plan", "failed", "killed"},
    "completed": set(),
    "failed": set(),
    "killed": set(),
}
APPROVAL_ACTIONS = {
    "pr": "external_mutation",
    "completed": "merge",
    "blocked": "escalation",
    "failed": "escalation",
}
RECOVERY_CALL_BUDGET_S = 30.0


def _actor(agent: str | None) -> str:
    verified = config.verified_agent_identity()
    if verified and agent and verified != agent:
        raise ToolError(f"Auth identity mismatch: requested {agent!r} but verified as {verified!r}")
    actor = verified or (agent or "").strip()
    if not actor or not config.is_known_agent_label(actor):
        raise ToolError("an authenticated known agent identity is required")
    return actor


def _cycle(row) -> dict:
    return {key: row[key] for key in row.keys()}


def _transition(row) -> dict:
    return {key: row[key] for key in row.keys()}


def autonomous_cycle_start(
    agent: str,
    task_id: int | None = None,
    plan_id: str | None = None,
    delegation_id: int | None = None,
    max_rework: int = 2,
    budgets: dict | None = None,
    capabilities: list[str] | None = None,
) -> dict:
    """Create a cycle in ``plan`` state; no execution is started."""
    actor = _actor(agent)
    if max_rework < 0 or max_rework > 20:
        raise ToolError("max_rework must be between 0 and 20")
    defaults = config.autonomy_budget_defaults()
    budgets = dict(budgets or {})
    # The task's run carries operator-set caps in its operating snapshot
    # (task #1756). They fill only caps the caller did not pass.
    profile_caps: dict = {}
    if task_id is not None:
        from .. import operating_profiles

        profile_caps = {
            key: value
            for key, value in operating_profiles.budget_caps(
                operating_profiles.task_run_snapshot(task_id)
            ).items()
            if key not in budgets
        }
        budgets.update(profile_caps)
    budget_values = {
        "time_s": float(budgets.get("time_s", defaults["time_s"])),
        "tokens": int(budgets.get("tokens", defaults["tokens"])),
        "retries": int(budgets.get("retries", defaults["retries"])),
        "cost_cents": float(budgets.get("cost_cents", defaults["cost_cents"])),
    }
    if any(not math.isfinite(value) or value <= 0 for value in budget_values.values()):
        raise ToolError("autonomy budgets must be positive and finite")
    if capabilities is not None and (
        not isinstance(capabilities, list)
        or any(not isinstance(item, str) or not item.strip() for item in capabilities)
    ):
        raise ToolError("capabilities must be a list of non-empty tool names")
    requested = set(capabilities or [])
    from .capability import get_agent_allowed_tools

    agent_allowed = get_agent_allowed_tools(actor)
    denied = requested - agent_allowed if agent_allowed is not None else set()
    allowed_capabilities = (
        sorted(requested)
        if capabilities is not None
        else (sorted(agent_allowed) if agent_allowed is not None else None)
    )
    cycle_id = uuid.uuid4().hex[:12]
    ts = storage.now()
    conn = storage.connect()
    try:
        if (
            task_id is not None
            and conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None
        ):
            raise ToolError(f"no task with id {task_id}")
        if (
            delegation_id is not None
            and conn.execute("SELECT 1 FROM delegations WHERE id=?", (delegation_id,)).fetchone()
            is None
        ):
            raise ToolError(f"no delegation with id {delegation_id}")
        initial_state = "blocked" if denied else "plan"
        stop_reason = f"capability policy denied: {', '.join(sorted(denied))}" if denied else None
        conn.execute(
            "INSERT INTO autonomous_cycles "
            "(id, task_id, plan_id, delegation_id, state, max_rework, max_time_s, max_tokens, "
            "max_retries, max_cost_cents, allowed_capabilities, stop_reason, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cycle_id,
                task_id,
                plan_id,
                delegation_id,
                initial_state,
                max_rework,
                budget_values["time_s"],
                budget_values["tokens"],
                budget_values["retries"],
                budget_values["cost_cents"],
                json.dumps(allowed_capabilities) if allowed_capabilities is not None else None,
                stop_reason,
                actor,
                ts,
                ts,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM autonomous_cycles WHERE id=?", (cycle_id,)).fetchone()
        cycle_data = _cycle(row)
        try:
            from .. import events

            events.record_event(
                channel="agent.cycle.updated",
                payload={"cycle": cycle_data, "action": "start"},
                originator=actor,
                notify=True,
            )
        except Exception:
            pass
        result = {"cycle": cycle_data, "created": True}
        if profile_caps:
            result["operating_budget"] = sorted(profile_caps)
        return result
    finally:
        conn.close()


def autonomous_cycle_get(cycle_id: str) -> dict:
    """Read one cycle and its bounded transition history."""
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM autonomous_cycles WHERE id=?", (cycle_id,)).fetchone()
        if row is None:
            raise ToolError(f"no autonomous cycle with id {cycle_id!r}")
        transitions = conn.execute(
            "SELECT id, cycle_id, from_state, to_state, idempotency_key, actor, reason, created_at "
            "FROM autonomous_cycle_transitions WHERE cycle_id=? ORDER BY id",
            (cycle_id,),
        ).fetchall()
        return {"cycle": _cycle(row), "transitions": [_transition(item) for item in transitions]}
    finally:
        conn.close()


def autonomous_cycle_transition(
    cycle_id: str,
    to_state: str,
    idempotency_key: str,
    agent: str,
    expected_version: int | None = None,
    reason: str | None = None,
    pr_url: str | None = None,
    capability: str | None = None,
) -> dict:
    """Apply one bounded transition exactly once.

    Repeating an idempotency key returns the original result. A version mismatch
    rejects stale concurrent writers rather than silently overwriting state.
    """
    actor = _actor(agent)
    if to_state not in STATES:
        raise ToolError(f"to_state must be one of {STATES}")
    key = (idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise ToolError("idempotency_key is required and must be at most 128 characters")
    if to_state in {"blocked", "failed", "killed"} and not (reason or "").strip():
        raise ToolError(f"reason is required when stopping in {to_state}")
    if to_state == "pr":
        stripped_pr_url = (pr_url or "").strip()
        if not stripped_pr_url:
            raise ToolError("pr_url is required before entering pr state")
        if not _looks_like_evidence(stripped_pr_url):
            raise ToolError(
                "pr_url must be a verifiable pointer — an http(s) URL or a commit SHA "
                "(7-40 hex chars) — not free text. Evidence contract (task #373 3.2): "
                "a cycle claiming pr/completed state must carry something checkable, "
                "never prose alone."
            )
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM autonomous_cycles WHERE id=?", (cycle_id,)).fetchone()
        if row is None:
            raise ToolError(f"no autonomous cycle with id {cycle_id!r}")
        existing = conn.execute(
            "SELECT * FROM autonomous_cycle_transitions WHERE cycle_id=? AND idempotency_key=?",
            (cycle_id, key),
        ).fetchone()
        if existing:
            if existing["to_state"] != to_state:
                raise ToolError("idempotency key was already used for another transition")
            return {"cycle": _cycle(row), "transition": _transition(existing), "idempotent": True}
        if expected_version is not None and row["version"] != expected_version:
            raise ToolError(
                f"stale cycle version: expected {expected_version}, current {row['version']}"
            )
        current = row["state"]
        if to_state not in TRANSITIONS[current]:
            raise ToolError(f"invalid transition {current!r} -> {to_state!r}")
        allowed = json.loads(row["allowed_capabilities"]) if row["allowed_capabilities"] else None
        capability_denied = bool(allowed is not None and capability and capability not in allowed)
        approval_action = APPROVAL_ACTIONS.get(to_state) if not capability_denied else None

        # Green-path auto-merge (task #384, wiring the CRITERIA task #373 3.4
        # already defined and got reviewed: reviewer.autonomous_cycle_green_
        # path_eligibility). Per docs/s5-bounded-autonomy-acceptance-gates.md
        # MUST 8-9: only when the pure-function eligibility check says so.
        # 'blocked'/'failed' (escalation) stay fully gated, exactly as before.
        # A cycle without a task_id, or any failure inside the eligibility
        # check itself, comes back ineligible (fails closed) and falls through
        # to the human approval path below, unchanged.
        # Both eligibility checks below run the full validation gates — security
        # audit and pytest — which take minutes. They must NOT run while this
        # BEGIN IMMEDIATE write transaction is open: every connection shares a
        # 5-second SQLite busy timeout, so holding the write lock that long
        # makes unrelated task/memory/approval writes fail with "database is
        # locked". Release it first, evaluate, then reacquire and revalidate
        # the version and transition legality against a fresh row — the same
        # commit/reacquire/revalidate dance the approval gate below already
        # performs, and for the same reason.
        green_path_reasons: list[str] | None = None
        pr_path_reasons: list[str] | None = None
        if approval_action in ("merge", "external_mutation"):
            eligibility_task_id, cycle_snapshot = row["task_id"], _cycle(row)
            pending_action = approval_action
            conn.commit()
            try:
                if pending_action == "merge":
                    from ..cycle_eligibility import autonomous_cycle_green_path_eligibility

                    eligibility = autonomous_cycle_green_path_eligibility(
                        eligibility_task_id, cycle_snapshot
                    )
                else:
                    from ..cycle_eligibility import autonomous_cycle_pr_path_eligibility

                    eligibility = autonomous_cycle_pr_path_eligibility(
                        eligibility_task_id, cycle_snapshot, pr_url=pr_url
                    )
            finally:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM autonomous_cycles WHERE id=?", (cycle_id,)).fetchone()
            if row is None:
                raise ToolError(f"no autonomous cycle with id {cycle_id!r}")
            if expected_version is not None and row["version"] != expected_version:
                raise ToolError(
                    f"stale cycle version: expected {expected_version}, current {row['version']}"
                )
            current = row["state"]
            if to_state not in TRANSITIONS[current]:
                raise ToolError(f"invalid transition {current!r} -> {to_state!r}")
            if eligibility["eligible"]:
                approval_action = None
                if pending_action == "merge":
                    green_path_reasons = eligibility["reasons"]
                else:
                    pr_path_reasons = eligibility["reasons"]

        if approval_action:
            # The approval queue writes to the same SQLite database. Release the
            # cycle read transaction before it inserts a pending request, then
            # reacquire the write transaction and revalidate the state below.
            conn.commit()
            check_approval_gate(
                f"autonomous_cycle:{approval_action}",
                {
                    "cycle_id": cycle_id,
                    "to_state": to_state,
                    "idempotency_key": key,
                    "expected_version": expected_version,
                    "pr_url": pr_url,
                },
                requested_by=actor,
                force=True,
            )
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM autonomous_cycles WHERE id=?", (cycle_id,)).fetchone()
            if row is None:
                raise ToolError(f"no autonomous cycle with id {cycle_id!r}")
            if expected_version is not None and row["version"] != expected_version:
                raise ToolError(
                    f"stale cycle version: expected {expected_version}, current {row['version']}"
                )
            current = row["state"]
            if to_state not in TRANSITIONS[current]:
                raise ToolError(f"invalid transition {current!r} -> {to_state!r}")
        rework_count = row["rework_count"]
        final_state = "blocked" if capability_denied else to_state
        if capability_denied:
            final_reason = f"capability policy denied: {capability}"
        elif green_path_reasons:
            # Audit trail for the bypass itself (task #384): the transition
            # row must show WHY no human approved this, not just that none
            # did — this is the first auto-merge capability in the codebase.
            final_reason = f"[green-path auto-merge] {'; '.join(green_path_reasons)}"
        elif pr_path_reasons:
            # Same audit obligation as the green-path bypass above: the row has
            # to record WHY no human approved opening this PR, not merely that
            # none did.
            final_reason = f"[auto-pr] {'; '.join(pr_path_reasons)}"
        else:
            final_reason = reason.strip() if reason else None
        if to_state == "rework":
            rework_count += 1
            if rework_count > row["max_rework"]:
                final_state = "blocked"
                final_reason = "rework budget exhausted"
        ts = storage.now()
        # `final_reason` is the transition row's reason. It is normally also
        # written to the cycle's stop_reason, but the auto-PR rationale must
        # NOT be: 'pr' is a mid-cycle success state, and green-path merge
        # eligibility rejects any cycle carrying a stop_reason. Writing it
        # there would leave a clean auto-PR cycle ineligible for auto-merge one
        # step later — the bypass would silently disable the next one. Caught
        # by test_green_path_eligible_cycle_reaches_pr_and_merge_with_no_human.
        cycle_stop_reason = row["stop_reason"] if pr_path_reasons else final_reason
        cur = conn.execute(
            "UPDATE autonomous_cycles SET state=?, rework_count=?, stop_reason=?, pr_url=?, "
            "version=version+1, updated_at=? WHERE id=? AND version=?",
            (final_state, rework_count, cycle_stop_reason, pr_url, ts, cycle_id, row["version"]),
        )
        if cur.rowcount != 1:
            raise ToolError("cycle changed concurrently; retry with the current version")
        cur = conn.execute(
            "INSERT INTO autonomous_cycle_transitions "
            "(cycle_id, from_state, to_state, idempotency_key, actor, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cycle_id, current, final_state, key, actor, final_reason, ts),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM autonomous_cycles WHERE id=?", (cycle_id,)).fetchone()
        transition = conn.execute(
            "SELECT id, cycle_id, from_state, to_state, idempotency_key, actor, reason, created_at "
            "FROM autonomous_cycle_transitions WHERE id=?",
            (cur.lastrowid,),
        ).fetchone()
        cycle_res = _cycle(updated)
        trans_res = _transition(transition)
        try:
            from .. import events

            events.record_event(
                channel="agent.cycle.updated",
                payload={"cycle": cycle_res, "transition": trans_res, "action": "transition"},
                originator=actor,
                notify=True,
            )
        except Exception:
            pass
        return {
            "cycle": cycle_res,
            "transition": trans_res,
            "idempotent": False,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def autonomous_cycle_kill(cycle_id: str, agent: str, reason: str) -> dict:
    """The kill switch (task #373 Tier 2, 3.3 / S5 gate MUST 3-4).

    A single documented command that forces a cycle to the terminal `killed`
    state from ANY non-terminal state — unlike a normal transition, it does
    not check `TRANSITIONS[current]` state-by-state (kill must work from
    `executing`, `delegated`, wherever the cycle happens to be) and it is
    NEVER approval-gated: `killed` is deliberately absent from
    APPROVAL_ACTIONS, because a kill switch that waits on a human round-trip
    before stopping defeats its own purpose.

    This does not send a signal to an already-running subprocess — the daemon
    has no handle to one (see executor/daemon.py; PiBackend/CodexBackend/
    AgyBackend all block inside `subprocess.run`). What it guarantees, and
    what the daemon enforces on every poll (`_find_active_cycle_for_task`):
    no further execution starts for this cycle, checked before every
    dispatch. Combined with the daemon capping each subprocess's timeout to
    the cycle's remaining time budget (never the old unconditional 300s), an
    already-in-flight run terminates on its own within
    `min(300, remaining_budget)` seconds even without this call — killing a
    cycle mid-flight bounds that same window rather than making it instant.
    Document this honestly: it is "no new work, and a bounded stop," not
    "instant SIGTERM." See scripts/kill_switch_rehearsal.py.

    Every kill is idempotency-keyed with a fresh uuid (kill-<uuid>) so the
    caller never has to think about idempotency, and is recorded in
    autonomous_cycle_transitions like any other transition — the audit trail
    (who, when, why) is the existing transition log, not a new column.
    """
    if not (reason or "").strip():
        raise ToolError("reason is required to kill a cycle")
    actor = _actor(agent)
    key = f"kill-{uuid.uuid4().hex[:16]}"
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM autonomous_cycles WHERE id=?", (cycle_id,)).fetchone()
        if row is None:
            raise ToolError(f"no autonomous cycle with id {cycle_id!r}")
        current = row["state"]
        if current in TERMINAL_STATES:
            raise ToolError(
                f"cycle {cycle_id!r} is already terminal ({current!r}); nothing to kill"
            )
        ts = storage.now()
        final_reason = f"killed by {actor}: {reason.strip()}"
        cur = conn.execute(
            "UPDATE autonomous_cycles SET state='killed', stop_reason=?, "
            "version=version+1, updated_at=? WHERE id=? AND version=?",
            (final_reason, ts, cycle_id, row["version"]),
        )
        if cur.rowcount != 1:
            raise ToolError("cycle changed concurrently; retry")
        conn.execute(
            "INSERT INTO autonomous_cycle_transitions "
            "(cycle_id, from_state, to_state, idempotency_key, actor, reason, created_at) "
            "VALUES (?, ?, 'killed', ?, ?, ?, ?)",
            (cycle_id, current, key, actor, final_reason, ts),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM autonomous_cycles WHERE id=?", (cycle_id,)).fetchone()
        cycle_res = _cycle(updated)
        try:
            from .. import events

            events.record_event(
                channel="agent.cycle.updated",
                payload={"cycle": cycle_res, "killed_from": current, "action": "kill"},
                originator=actor,
                notify=True,
            )
        except Exception:
            pass
        return {"cycle": cycle_res, "killed_from": current}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def autonomous_cycle_record_usage(
    cycle_id: str,
    agent: str,
    elapsed_s: float = 0,
    tokens: int = 0,
    retries: int = 0,
    cost_cents: float = 0,
    capability: str | None = None,
    usage_run_id: str | None = None,
    usage_id: str | None = None,
) -> dict:
    """Atomically record usage and block the cycle when a budget is exceeded."""
    actor = _actor(agent)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in (elapsed_s, tokens, retries, cost_cents)
    ):
        raise ToolError("usage values must be finite and nonnegative")
    if (usage_run_id is None) != (usage_id is None):
        raise ToolError("usage_run_id and usage_id must be supplied together")
    if usage_id is not None and (not usage_id.strip() or len(usage_id) > 128):
        raise ToolError("usage_id must be nonempty and at most 128 characters")
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM autonomous_cycles WHERE id=?", (cycle_id,)).fetchone()
        if row is None:
            raise ToolError(f"no autonomous cycle with id {cycle_id!r}")
        if usage_run_id is not None:
            if config.verified_agent_identity() != actor:
                raise ToolError("cycle usage requires the original authenticated actor")
            claim_row = conn.execute(
                "SELECT payload FROM event_log WHERE channel=? "
                "AND json_extract(payload,'$.kind')=? AND json_extract(payload,'$.run_id')=?",
                (run_records.CHANNEL, run_records.KIND_CLAIMED, usage_run_id),
            ).fetchone()
            claim = run_records._payload(claim_row) if claim_row is not None else {}
            if claim.get("agent") != actor or claim.get("task_id") != row["task_id"]:
                raise ToolError(
                    "cycle usage requires the bound task's original authenticated run actor"
                )
            key = f"cycle:usage:{cycle_id}:{usage_run_id}:{usage_id}"
            payload = {
                "kind": "cycle.usage_recorded",
                "cycle_id": cycle_id,
                "run_id": usage_run_id,
                "usage_id": usage_id,
                "agent": actor,
                "elapsed_s": elapsed_s,
                "tokens": tokens,
                "retries": retries,
                "cost_cents": cost_cents,
                "capability": capability,
            }
            previous = conn.execute(
                "SELECT payload FROM event_log WHERE dedupe_key=?", (key,)
            ).fetchone()
            if previous is not None:
                if run_records._payload(previous) != payload:
                    raise ToolError("usage_id already recorded with different usage")
                conn.commit()
                return {
                    "cycle": _cycle(row),
                    "blocked": row["state"] == "blocked",
                    "reason": row["stop_reason"],
                    "replayed": True,
                }
            events.record_event_once_on_connection(
                conn, "agent.cycle.usage", payload=payload, originator=actor, dedupe_key=key
            )
        if row["state"] in TERMINAL_STATES and usage_run_id is None:
            raise ToolError(f"cannot record usage for terminal cycle state {row['state']!r}")
        allowed = json.loads(row["allowed_capabilities"]) if row["allowed_capabilities"] else None
        if capability and allowed is not None and capability not in allowed:
            reason = f"capability policy denied: {capability}"
        else:
            used = {
                "time_s": row["used_time_s"] + elapsed_s,
                "tokens": row["used_tokens"] + tokens,
                "retries": row["used_retries"] + retries,
                "cost_cents": row["used_cost_cents"] + cost_cents,
            }
            limits = {
                "time_s": row["max_time_s"],
                "tokens": row["max_tokens"],
                "retries": row["max_retries"],
                "cost_cents": row["max_cost_cents"],
            }
            exceeded = [key for key in limits if used[key] > limits[key]]
            reason = f"autonomy budget exhausted: {', '.join(exceeded)}" if exceeded else None
        if capability and allowed is not None and capability not in allowed:
            used = {
                "time_s": row["used_time_s"],
                "tokens": row["used_tokens"],
                "retries": row["used_retries"],
                "cost_cents": row["used_cost_cents"],
            }
        state = "blocked" if reason and row["state"] not in TERMINAL_STATES else row["state"]
        if row["state"] in TERMINAL_STATES or (
            row["state"] == "blocked" and usage_run_id is not None
        ):
            reason = row["stop_reason"]
        ts = storage.now()
        conn.execute(
            "UPDATE autonomous_cycles SET used_time_s=?, used_tokens=?, used_retries=?, used_cost_cents=?, "
            "state=?, stop_reason=?, version=version+1, updated_at=? WHERE id=?",
            (
                used["time_s"],
                used["tokens"],
                used["retries"],
                used["cost_cents"],
                state,
                reason,
                ts,
                cycle_id,
            ),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM autonomous_cycles WHERE id=?", (cycle_id,)).fetchone()
        cycle_res = _cycle(updated)
        # start/transition/kill all publish agent.cycle.updated; this call was
        # the one path that changes displayed usage/progress and can move a
        # cycle to 'blocked' on its own (a budget exhaustion, not a human
        # transition) without emitting anything — the SSE-triggered webui
        # refresh never ran for it, so an operator saw stale progress or a
        # stale runnable state until the next 10s poll (task #445 / PR #191
        # review).
        try:
            events.record_event(
                channel="agent.cycle.updated",
                payload={"cycle": cycle_res, "blocked": state == "blocked", "action": "usage"},
                originator=actor,
                notify=True,
            )
        except Exception:
            pass
        return {"cycle": cycle_res, "blocked": state == "blocked", "reason": reason}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def autonomous_cycle_list(state: str | None = None, limit: int = 100) -> dict:
    """List cycle summaries without execution logs or prompts."""
    if state is not None and state not in STATES:
        raise ToolError(f"state must be one of {STATES}")
    conn = storage.connect()
    try:
        if state:
            rows = conn.execute(
                "SELECT * FROM autonomous_cycles WHERE state=? ORDER BY updated_at DESC LIMIT ?",
                (state, max(1, min(limit, 500))),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM autonomous_cycles ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return {"cycles": [_cycle(row) for row in rows]}
    finally:
        conn.close()


def autonomous_cycle_get_for_task(task_id: int) -> dict:
    """MCP-exposed wrapper around `active_cycle_for_task` (task #992).

    Exists so the executor daemon's pre-dispatch kill/budget gate
    (`ExecutorDaemon._execute_claimed_task`) can get the SAME answer over the
    hub when its local `storage.connect()` is unusable — e.g. a desktop whose
    local memory.db is an intentional tombstone (task #987) and whose real
    state lives on the Mac hub. This wraps the lookup only; it changes
    nothing about the query itself or its state-exclusion semantics (still
    excludes only completed/failed, not killed — see
    `active_cycle_for_task`'s docstring for why that exclusion is deliberate
    and must not change: excluding `killed` here would make the kill switch a
    no-op for exactly the remote-desktop-executor case this exists to serve).
    """
    return {"cycle": active_cycle_for_task(task_id)}


_CYCLE_ACTIONS = {
    "start": autonomous_cycle_start,
    "get": autonomous_cycle_get,
    "transition": autonomous_cycle_transition,
    "kill": autonomous_cycle_kill,
    "record_usage": autonomous_cycle_record_usage,
    "list": autonomous_cycle_list,
    "get_for_task": autonomous_cycle_get_for_task,
}


def _recovery_cycle_snapshot(
    cycle_id: str, actor: str, *, minimum_remaining: float = RECOVERY_CALL_BUDGET_S * 2
) -> dict:
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM autonomous_cycles WHERE id=?", (cycle_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ToolError(f"no autonomous cycle with id {cycle_id!r}")
    if row["created_by"] != actor:
        raise ToolError("recovery cycle owner does not match authenticated identity")
    if row["state"] != "executing":
        raise ToolError(f"recovery requires an executing cycle, got {row['state']!r}")
    allowed = json.loads(row["allowed_capabilities"]) if row["allowed_capabilities"] else None
    required = {"cloud_worker_recover", "gcloud_ops"}
    if allowed is None or not required.issubset(allowed):
        raise ToolError(
            "recovery requires explicit cloud_worker_recover and gcloud_ops capabilities"
        )
    numeric = (
        row["max_time_s"],
        row["used_time_s"],
        row["max_tokens"],
        row["used_tokens"],
        row["max_retries"],
        row["used_retries"],
        row["max_cost_cents"],
        row["used_cost_cents"],
    )
    if not all(math.isfinite(float(value)) for value in numeric):
        raise ToolError("recovery cycle budget is not finite")
    if row["max_time_s"] - row["used_time_s"] < minimum_remaining:
        raise ToolError("recovery cycle has insufficient time budget remaining")
    if row["max_tokens"] <= row["used_tokens"]:
        raise ToolError("recovery cycle token budget is exhausted")
    if row["max_retries"] <= row["used_retries"]:
        raise ToolError("recovery cycle retry budget is exhausted")
    if row["max_cost_cents"] <= row["used_cost_cents"]:
        raise ToolError("recovery cycle cost budget is exhausted")
    return {key: row[key] for key in row.keys()}


def autonomous_cycle_recover_worker(cycle_id: str, agent: str, dry_run: bool = True) -> dict:
    """Run the existing bounded worker recovery inside an executing cycle."""
    if not config.verified_agent_identity():
        raise ToolError("a verified identity is required for worker recovery")
    actor = _actor(agent)
    snapshot = _recovery_cycle_snapshot(cycle_id, actor)
    started = time.monotonic()

    def before_mutation() -> None:
        current = _recovery_cycle_snapshot(
            cycle_id, actor, minimum_remaining=RECOVERY_CALL_BUDGET_S
        )
        if current["version"] != snapshot["version"]:
            raise ToolError("cycle changed while worker health was being checked")
        remaining = current["max_time_s"] - current["used_time_s"] - (time.monotonic() - started)
        if remaining < RECOVERY_CALL_BUDGET_S:
            raise ToolError("recovery cycle budget expired before mutation")

    from .cloud_recovery import _cloud_worker_recover

    try:
        result = _cloud_worker_recover(
            dry_run=dry_run,
            before_mutation=None if dry_run else before_mutation,
            cycle_id=cycle_id,
        )
    except Exception as exc:  # noqa: BLE001 - convert provider failures to a bounded outcome
        result = {
            "outcome": "failed",
            "detail": f"recovery core raised {type(exc).__name__}",
            "target": config.gcp_worker() or "",
        }
    result["cycle_id"] = cycle_id
    if dry_run:
        return result

    elapsed_s = max(0.0, time.monotonic() - started)
    result["elapsed_s"] = elapsed_s
    try:
        events.record_event(
            "agent.cycle.recovery",
            payload={
                "cycle_id": cycle_id,
                "actor": actor,
                "outcome": result.get("outcome", "failed"),
                "elapsed_s": elapsed_s,
            },
            originator=actor,
        )
    except Exception as exc:
        result["evidence_error"] = type(exc).__name__
    try:
        usage = autonomous_cycle_record_usage(
            cycle_id,
            actor,
            elapsed_s=elapsed_s,
            retries=1,
            capability="cloud_worker_recover",
        )
    except ToolError as exc:
        result["usage_error"] = str(exc)
        return result
    result["usage"] = usage
    if usage.get("blocked") or result.get("evidence_error"):
        return result
    if result.get("outcome") not in {"healthy", "recovered"}:
        return result
    current = usage["cycle"]
    if current["state"] != "executing":
        return result
    result["transition"] = autonomous_cycle_transition(
        cycle_id,
        "validating",
        f"recover-worker-{cycle_id}-{current['version']}",
        actor,
        expected_version=current["version"],
        reason=f"worker recovery outcome: {result['outcome']}",
        capability="cloud_worker_recover",
    )
    return result


_CYCLE_ACTIONS["recover_worker"] = autonomous_cycle_recover_worker


def autonomous_cycle_manage(action: str, params: dict | None = None) -> dict:
    """Dispatch autonomous-cycle lifecycle operations through one API.

    Every action delegates to the existing state machine, approval gate, kill
    switch, and budget checks. Legacy cycle tools remain available to stdio
    clients during the compatibility migration.
    """
    if not isinstance(action, str):
        raise ToolError("action must be a string")
    action = action.strip().lower()
    fn = _CYCLE_ACTIONS.get(action)
    if fn is None:
        raise ToolError(f"action must be one of {tuple(_CYCLE_ACTIONS)}")
    action_tool = "cloud_worker_recover" if action == "recover_worker" else fn.__name__
    if not profiles.consolidated_action_is_allowed("autonomous_cycle_manage", action_tool):
        raise ToolError(f"cycle action {action!r} is not exposed by the active profile")
    if action == "recover_worker" and not profiles.tool_is_allowed("gcloud_ops"):
        raise ToolError("cycle action 'recover_worker' requires gcloud_ops in the active profile")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ToolError("params must be an object")
    try:
        bound = inspect.signature(fn).bind(**params)
    except TypeError as exc:
        raise ToolError(f"invalid params for cycle action {action!r}: {exc}") from exc
    safety.check_policy_gate(action_tool, bound.arguments)
    return fn(*bound.args, **bound.kwargs)


def active_cycle_for_task(task_id: int) -> dict | None:
    """The most recently updated cycle bound to a task that still governs
    execution of it, or None.

    Read-only lookup, not an MCP tool — the executor daemon's pre-dispatch
    budget/kill check (task #373 3.3) calls this directly via
    `cycles_tool.active_cycle_for_task`, mirroring how `reclaim_expired_locks`
    /`renew_lock` already read `storage.connect()` directly for internal
    bookkeeping rather than round-tripping the hub. A task can only usefully
    gate on its OWN active cycle, so this is scoped to task_id rather than a
    general filter.

    Deliberately excludes only `completed`/`failed` (a cycle that reached its
    own natural end should not block a later, unrelated re-delegation of the
    same task forever) — NOT `killed`. A killed cycle is exactly the case the
    daemon's gate exists to catch: excluding it here would make the kill
    switch a no-op, since the daemon would simply stop seeing the cycle it
    was supposed to refuse further work for. The caller decides what to do
    with the state (`killed`/`blocked` refuse; anything else proceeds) — this
    function's job is only to find the governing cycle, not judge it.
    """
    conn = storage.connect()
    try:
        # updated_at has only second precision (storage.now()), so two
        # cycles for the same task created inside one second tie on it —
        # rowid DESC (monotonic on insert) is the tiebreaker that makes
        # "most recent" unambiguous rather than whatever order SQLite
        # happens to return tied rows in. Caught by
        # scripts/kill_switch_rehearsal.py, which found a fresh cycle
        # started right after a kill in the same second lost the tiebreak.
        row = conn.execute(
            "SELECT * FROM autonomous_cycles WHERE task_id=? AND state NOT IN "
            "('completed','failed') ORDER BY updated_at DESC, rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return _cycle(row) if row is not None else None
    finally:
        conn.close()


def cycle_state(cycle_id: str) -> str | None:
    """The current `state` column for one cycle, or None if it no longer exists.

    Read-only, not an MCP tool — deliberately just the one column rather than
    `autonomous_cycle_get`'s full row-plus-transition-history join. This is
    the mid-flight kill-switch poll (task #383): the executor daemon calls it
    every `poll_interval_s` while a backend subprocess is running, so it needs
    to be cheap enough to call in a tight loop without becoming its own
    source of latency.
    """
    conn = storage.connect()
    try:
        row = conn.execute("SELECT state FROM autonomous_cycles WHERE id=?", (cycle_id,)).fetchone()
        return row["state"] if row is not None else None
    finally:
        conn.close()
