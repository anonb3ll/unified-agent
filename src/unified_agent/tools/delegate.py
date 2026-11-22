"""Delegation protocol: agent-to-agent task handoff with audit trail (task #62 · A).

`delegate` links the three existing surfaces instead of inventing a new one:
a durable child task (the work item the receiving agent claims), an attributed
memory note (the cross-agent audit record), and a `delegations` row tying them
together with from/to labels, an optional deadline, and a completion callback.

Nothing here executes work or bypasses any safety chokepoint — delegation only
records who asked whom to do what. The callback fires when the child task is
marked done (hooked from task_update), and is best-effort: callback failure
never blocks the task completion that triggered it.
"""

import inspect
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import config, edition, lease_reaper, profiles, safety, storage
from . import delegate_receipts

CALLBACKS = ("notify", "memory", "none")
DELEGATION_STATUSES = ("pending", "in_flight", "completed", "cancelled", "failed")

# Consecutive from→to failures that refuse new claims at the eligibility gate
# (task #1142). A success or a review annotation resets the counter.
FAILURE_PAIR_THRESHOLD = 3
FAILURE_PAIR_GATE_REASON = "failure_pair_gate"
FAILURE_PAIR_GATE_UNVERIFIABLE = "failure_pair_gate_unverifiable"

# Shared with the ledger: `[review: merged|rework|…]` is the human closeout.
_REVIEW_ANNOTATION_RE = re.compile(r"\[review:\s*([^\]]+)\]", re.IGNORECASE)

# Markdown heading marker ("### Objective") or a bare section label ("OBJECTIVE:")
# — neither is ever real title content, just structure from the Delegation Spec
# Standard template (delegation_spec.DELEGATION_SPEC_TEMPLATE).
_HEADING_OR_LABEL_RE = re.compile(r"^(?:#{1,6}\s*)?[A-Za-z][A-Za-z /]{2,40}[:*]?$")


def _derive_title(task_spec: str) -> str:
    """First real content line of a delegation spec, for use as the task title.

    Task #378: `task_spec.splitlines()[0][:120]` took the spec's literal first
    line, which is a markdown heading ("### Objective") when the spec follows
    the Delegation Spec Standard template — every delegation does, since
    `enforce_delegation_spec` requires the section. That produced tasks titled
    "### Objective" (#367, #368, #370). Skip heading/label lines and take the
    first line that is actual prose instead.
    """
    for raw in task_spec.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _HEADING_OR_LABEL_RE.match(line):
            continue
        return line[:120]
    # Every line was a heading/label (or the spec is a single such line) —
    # strip the heading marker rather than pass a "#"-prefixed title through.
    fallback = task_spec.strip().splitlines()[0] if task_spec.strip() else ""
    return re.sub(r"^#{1,6}\s*", "", fallback).strip()[:120] or "untitled delegation"


def _row(row) -> dict:
    return {k: row[k] for k in row.keys()}


def _validate_label(label: str, field: str) -> str:
    label = (label or "").strip()
    if not config.is_known_agent_label(label):
        raise ToolError(config.unknown_agent_label_error(field, label))
    return config.canonical_agent_label(label) or label


def _caller_label(agent: str) -> str:
    """`agent`, refused unless it is the verified caller (or an allowlisted alias).

    Same identity rule as task_claim (``verify_write_identity``): naming another
    agent never borrows its authority over a delegation. Unauthenticated stdio
    keeps the claimed label; unauthenticated HTTP is refused.
    """
    agent = _validate_label(agent, "agent")
    actor = config.verify_write_identity(agent)
    if actor != agent:
        raise ToolError(
            f"delegation transition as {agent!r} requires that agent's own authenticated identity"
        )
    return agent


def _validate_claim_generation(generation: int | None) -> int | None:
    if generation is not None and (
        not isinstance(generation, int) or isinstance(generation, bool) or generation < 0
    ):
        raise ToolError("expected_claim_generation must be a nonnegative integer")
    return generation


def _delegation_run_guard(
    agent: str, task_id: int | None, expected_run_id: str | None, *, allow_finished: bool = False
) -> tuple[str, list[Any]]:
    """Additional atomic denials; neither a run token nor task id grants authority."""
    if task_id is not None and (
        not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0
    ):
        raise ToolError("task_id must be a positive integer")
    if expected_run_id is None:
        return (" AND task_id=?", [task_id]) if task_id is not None else ("", [])
    if (
        not isinstance(expected_run_id, str)
        or not expected_run_id.strip()
        or len(expected_run_id) > 256
    ):
        raise ToolError("expected_run_id must be a nonempty bounded string")
    actor = config.verify_write_identity(agent)
    if actor != agent:
        raise ToolError(
            "run-fenced delegation transition requires the actual owning caller identity"
        )
    guard = " AND to_agent=? AND EXISTS (SELECT 1 FROM tasks t WHERE t.id=delegations.task_id "
    params: list[Any] = [agent]
    if task_id is not None:
        guard += "AND t.id=? "
        params.append(task_id)
    guard += "AND ((t.claimed_by=? AND t.cancel_requested=0 "
    params.append(agent)
    guard += "AND t.current_run_id=? AND t.current_run_agent=? AND t.status='doing')"
    params.extend([expected_run_id, agent])
    if allow_finished:
        # task_update closes the run when it publishes its review handoff.
        # Only that latest, truly finished run may complete afterward, and a
        # successor claim (even one already finished) invalidates this receipt.
        # The task's own status is deliberately not consulted here: once this
        # run's handed_off receipt exists, a reviewer or human may already have
        # moved the task (done, blocked, reopened) before the executor records
        # its delegation completion, and that must not strand the delegation.
        guard += " OR (t.current_run_id IS NULL "
        guard += "AND (SELECT json_extract(c.payload,'$.run_id') FROM event_log c "
        guard += "WHERE c.channel='agent.run.selfpickup' AND json_extract(c.payload,'$.kind')='selfpickup.claimed' "
        guard += "AND json_extract(c.payload,'$.task_id')=t.id ORDER BY c.rowid DESC LIMIT 1)=? "
        guard += "AND EXISTS (SELECT 1 FROM event_log f WHERE f.channel='agent.run.selfpickup' "
        guard += "AND json_extract(f.payload,'$.kind')='selfpickup.finished' "
        guard += (
            "AND json_extract(f.payload,'$.task_id')=t.id AND json_extract(f.payload,'$.run_id')=? "
        )
        guard += (
            "AND json_extract(f.payload,'$.agent')=? AND json_extract(f.payload,'$.finished_by')=? "
        )
        guard += "AND json_extract(f.payload,'$.outcome')='handed_off')"
        params.extend([expected_run_id, expected_run_id, agent, agent])
        guard += ")"
    return guard + "))", params


def _transition_result(key: str, changed: bool, expected_run_id: str | None) -> dict:
    return {
        key: changed,
        **({"reason": "run_not_current"} if not changed and expected_run_id is not None else {}),
    }


def _validate_deadline(deadline: str | None) -> str | None:
    if deadline is None:
        return None
    try:
        datetime.fromisoformat(deadline)
    except ValueError:
        raise ToolError(
            f"deadline {deadline!r} is not ISO-8601 (e.g. '2026-07-20' or "
            "'2026-07-20T18:00:00+00:00')"
        ) from None
    return deadline


def _has_review_annotation(notes: str | None) -> bool:
    """True when task notes carry a human `[review: …]` closeout annotation."""
    return bool(_REVIEW_ANNOTATION_RE.search(notes or ""))


def _resolve_failure_class(conn, delegation_id: int, callback_result: str | None) -> str:
    """Prefer #1081 failure_class on linked outcome rows; else classify the reason."""
    from ..quota_observe import classify_failure_reason

    row = conn.execute(
        "SELECT failure_class FROM executor_attempts "
        "WHERE delegation_id=? AND failure_class IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (delegation_id,),
    ).fetchone()
    if row and row["failure_class"]:
        return row["failure_class"]
    row = conn.execute(
        "SELECT failure_class FROM dispatch_runs "
        "WHERE delegation_id=? AND failure_class IS NOT NULL "
        "ORDER BY updated_at DESC LIMIT 1",
        (delegation_id,),
    ).fetchone()
    if row and row["failure_class"]:
        return row["failure_class"]
    return classify_failure_reason(callback_result)


def _pair_outcome_rows(conn, from_agent: str, to_agent: str) -> list:
    """Terminal outcomes for a from→to pair, newest first (real storage schema)."""
    return conn.execute(
        "SELECT d.id, d.from_agent, d.to_agent, d.task_id, d.status, "
        "d.callback_result, d.created_at, d.updated_at, d.completed_at, t.notes "
        "FROM delegations d "
        "LEFT JOIN tasks t ON d.task_id = t.id "
        "WHERE d.from_agent = ? AND d.to_agent = ? "
        "AND d.status IN ('failed', 'cancelled', 'completed') "
        "ORDER BY COALESCE(d.completed_at, d.updated_at, d.created_at) DESC, d.id DESC",
        (from_agent, to_agent),
    ).fetchall()


def pair_failure_status(from_agent: str, to_agent: str) -> dict:
    """Consecutive failed outcomes for a from→to pair (task #1142).

    Walks terminal outcomes newest-first. A `completed` success or any
    `[review: …]` annotation resets the streak. `cancelled` is terminal for
    the awaiting-review projection but does not increment the failure streak.
    Returns `{consecutive_failures, last_failure_class, blocked, reason}`.
    Raises `storage.LiveStoreError` / `sqlite3.Error` so callers can fail closed.
    """
    from_agent = _validate_label(from_agent, "from_agent")
    to_agent = _validate_label(to_agent, "to_agent")

    conn = storage.connect()
    try:
        rows = _pair_outcome_rows(conn, from_agent, to_agent)
        consecutive = 0
        last_failure_class = None
        for r in rows:
            if _has_review_annotation(r["notes"]):
                break
            if r["status"] == "completed":
                break
            if r["status"] != "failed":
                # cancelled (and any future non-failed terminal) — not a
                # consecutive *failure*, but also not a success reset.
                continue
            consecutive += 1
            if last_failure_class is None:
                last_failure_class = _resolve_failure_class(conn, r["id"], r["callback_result"])
        blocked = consecutive >= FAILURE_PAIR_THRESHOLD
        return {
            "from_agent": from_agent,
            "to_agent": to_agent,
            "consecutive_failures": consecutive,
            "last_failure_class": last_failure_class,
            "blocked": blocked,
            "reason": FAILURE_PAIR_GATE_REASON if blocked else None,
            "threshold": FAILURE_PAIR_THRESHOLD,
        }
    finally:
        conn.close()


def outcomes_awaiting_review(limit: int = 100) -> dict:
    """Queryable projection of terminal outcomes lacking a review annotation (#1142).

    Source: `delegations` joined to task notes. Consumed by the review-cron
    surface so unreviewed outcomes become visible work. Never blocks completion.
    """
    limit = max(1, min(int(limit), 500))
    conn = storage.connect()
    try:
        rows = conn.execute(
            "SELECT d.id, d.from_agent, d.to_agent, d.task_id, d.status, "
            "d.callback_result, d.created_at, d.updated_at, d.completed_at, t.notes "
            "FROM delegations d "
            "LEFT JOIN tasks t ON d.task_id = t.id "
            "WHERE d.status IN ('failed', 'cancelled', 'completed') "
            "ORDER BY COALESCE(d.completed_at, d.updated_at, d.created_at) DESC, d.id DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        outcomes = []
        for r in rows:
            if _has_review_annotation(r["notes"]):
                continue
            outcomes.append(
                {
                    "delegation_id": r["id"],
                    "task_id": r["task_id"],
                    "from_agent": r["from_agent"],
                    "to_agent": r["to_agent"],
                    "status": r["status"],
                    "callback_result": r["callback_result"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "completed_at": r["completed_at"],
                }
            )
        return {"outcomes": outcomes, "count": len(outcomes)}
    finally:
        conn.close()


def failure_pair_report(limit: int = 100) -> dict:
    """Failing from→to pairs with consecutive-failure count + last failure_class.

    Uses the real pair counter (`pair_failure_status`) against the storage
    schema. Pairs with zero consecutive failures are omitted.
    """
    limit = max(1, min(int(limit), 500))
    conn = storage.connect()
    try:
        pair_rows = conn.execute(
            "SELECT DISTINCT from_agent, to_agent FROM delegations "
            "WHERE status = 'failed' "
            "ORDER BY from_agent, to_agent "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    pairs = []
    for pr in pair_rows:
        status = pair_failure_status(pr["from_agent"], pr["to_agent"])
        if status["consecutive_failures"] <= 0:
            continue
        pairs.append(
            {
                "from_agent": status["from_agent"],
                "to_agent": status["to_agent"],
                "consecutive_failures": status["consecutive_failures"],
                "last_failure_class": status["last_failure_class"],
                "blocked": status["blocked"],
            }
        )
    pairs.sort(key=lambda p: (-p["consecutive_failures"], p["from_agent"], p["to_agent"]))
    return {"pairs": pairs, "count": len(pairs), "threshold": FAILURE_PAIR_THRESHOLD}


def _product_repo_without_code_task_warnings(task_spec: str) -> list[str]:
    from .tasks import _CODE_TASK_MARKER_RX

    if _CODE_TASK_MARKER_RX.search(task_spec):
        return []
    hits = []
    home = Path.home()
    for repo in config.project_repo_roots():
        names = {str(repo)}
        try:
            names.add("~/" + str(repo.relative_to(home)))
        except ValueError:
            pass
        if any(name in task_spec for name in names):
            hits.append(str(repo))
    if not hits:
        return []
    return [
        "spec references a product checkout path but the child task lacks "
        "[code-task]; the task's project selects its checkout regardless of "
        "that marker. Add [code-task] when completion requires code evidence "
        f"for: {', '.join(sorted(hits))}"
    ]


def delegate(
    task_spec: str,
    from_agent: str,
    to_agent: str,
    deadline: str | None = None,
    callback: str = "notify",
    priority: str = "med",
    parent_task_id: int | None = None,
    project: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Delegate a work item from one agent to another (task #62 · A).

    Validates both labels against the known-agent set, creates a linked child
    task addressed to `to_agent` (annotated `[delegated-to: X]` so task_list
    filters find it), records the delegation as an attributed memory note, and
    registers a completion callback: 'notify' (iMessage the operator when the child
    task is done), 'memory' (append the completion to the delegation note), or
    'none'. `deadline` is ISO-8601 and advisory — delegate_list flags overdue
    rows; nothing auto-expires. Delegation grants no new powers: the receiving
    agent works under exactly the same tool gates as before.

    `project` scopes the created child task (configured project stream ids); omitted inherits `parent_task_id`'s project, or
    defaults to 'unified-agent' with no parent — same rule as `task_add`.

    `idempotency_key` (task #1938): optional caller-scoped retry token. A retry
    of the same request with the same key returns the committed result and
    never creates a second delegation; the same key with a different request is
    a conflict. Recover a dropped response with
    `delegation_manage(action="lookup", params={"idempotency_key": ...})`.

    Returns {delegation_id, task_id, memory_id}.
    """
    if not task_spec.strip():
        raise ToolError("task_spec must not be empty")
    task_spec = task_spec.strip()
    from_agent = config.verify_write_identity(from_agent)
    from_agent = _validate_label(from_agent, "from_agent")
    to_agent = _validate_label(to_agent, "to_agent")
    if from_agent == to_agent:
        raise ToolError("from_agent and to_agent must differ — you cannot delegate to yourself")
    if callback not in CALLBACKS:
        raise ToolError(f"callback must be one of {CALLBACKS}")
    if idempotency_key is not None:
        idempotency_key = delegate_receipts.validate_key(idempotency_key)
    if callback == "notify":
        # The completion callback messages the operator on from_agent's behalf.
        from ..operating_profiles import check_operator_contact

        check_operator_contact(from_agent, route="delegate_callback")
    deadline = _validate_deadline(deadline)

    # Delegation Spec Standard (task #160): the spec must state how the work
    # will be proven before it is handed to another agent. Runs BEFORE the
    # approval gate — there is no point queueing a human decision on a
    # delegation that is malformed. Enforcing (UA_DELEGATION_SPEC_ENFORCE=0
    # downgrades to a warning).
    from .delegation_spec import enforce_delegation_spec

    spec_check = enforce_delegation_spec(task_spec, to_agent)
    dispatch_warnings = _product_repo_without_code_task_warnings(task_spec)
    request = {
        "task_spec": task_spec,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "deadline": deadline,
        "callback": callback,
        "priority": priority,
        "parent_task_id": parent_task_id,
        "project": project,
    }
    if idempotency_key is None:
        return _create_delegation(request, spec_check, dispatch_warnings, None)

    # Keyed path (task #1938): replay a committed receipt before any gate, so
    # a retry of committed work neither re-asks a human nor repeats an effect.
    receipt_key = delegate_receipts.storage_key(from_agent, idempotency_key)
    digest = delegate_receipts.request_digest(request)
    role, value = delegate_receipts.claim(receipt_key, digest)
    if role == "replay":
        return value
    keyed = (receipt_key, value, digest)
    from .approvals import ApprovalPendingError

    try:
        return _create_delegation(request, spec_check, dispatch_warnings, keyed)
    except ApprovalPendingError as exc:
        delegate_receipts.park(receipt_key, value, digest, exc.approval_id)
        raise
    except BaseException:
        delegate_receipts.release(receipt_key, value)
        raise


def _create_delegation(
    request: dict,
    spec_check: dict,
    dispatch_warnings: list[str],
    keyed: tuple[str, str, str] | None,
) -> dict:
    """Gate and create one delegation; ``keyed`` is (receipt key, owner token, digest)."""
    task_spec = request["task_spec"]
    from_agent = request["from_agent"]
    to_agent = request["to_agent"]
    deadline = request["deadline"]
    callback = request["callback"]
    priority = request["priority"]
    parent_task_id = request["parent_task_id"]
    project = request["project"]

    # Approval gate (task 62 · C): layered AFTER the label/format validation
    # above — it can only tighten, never bypass them. `delegate` is in the
    # default approval baseline; UA_APPROVAL_BASELINE=0 or
    # UA_LEGACY_OPEN_POSTURE=1 restores the old opt-in (a no-op unless
    # UA_APPROVAL_TOOLS lists "delegate").
    #
    # Task #366: skip it entirely when the ACTUAL VERIFIED caller (not the
    # claimed from_agent, which could be anything) is the human operator —
    # config.verified_caller_is_operator(), the same definition the approval
    # gate's operator self-approval uses (task #2535): the operator's login
    # label or an alias, case-insensitively. That identity only reaches delegate() through an
    # authenticated Dispatch submission in /ui; the human already gave their
    # authorization by clicking submit, so a second approval-queue round trip
    # for the same action is redundant, not an extra safety check. This is
    # keyed off already-enforced identity verification, not a new
    # caller-supplied flag — an agent authenticated with its own bearer token
    # can never trigger it by merely claiming a different from_agent, since
    # verify_write_identity() above already pins from_agent to a value the
    # verified identity is allowed to use. Every other UA_APPROVAL_TOOLS
    # caller (git_commit_staged, run_cmd, workflow_run) is untouched.
    human_already_authorized = config.verified_caller_is_operator()

    if not human_already_authorized:
        from .approvals import check_approval_gate

        # The approval payload is unchanged by an idempotency key: the human
        # approves the delegation itself, and its retry consumes that pass.
        check_approval_gate(
            "delegate",
            {
                "task_spec": task_spec,
                "from_agent": from_agent,
                "to_agent": to_agent,
                "deadline": deadline,
                "callback": callback,
                "priority": priority,
                "parent_task_id": parent_task_id,
            },
            requested_by=from_agent,
        )

    title = _derive_title(task_spec)
    notes_lines = [
        f"[delegated-by: {from_agent}] [delegated-to: {to_agent}]",
    ]
    if deadline:
        notes_lines.append(f"[deadline: {deadline}]")
    notes_lines += ["", "task-spec:", task_spec]

    # Check parent task trust status if parent_task_id is set
    parent_untrusted = False
    if parent_task_id is not None:
        conn = storage.connect()
        try:
            parent = conn.execute(
                "SELECT untrusted FROM tasks WHERE id=?", (parent_task_id,)
            ).fetchone()
            if parent and parent["untrusted"]:
                parent_untrusted = True
        finally:
            conn.close()

    delegator_untrusted = config.is_agent_untrusted(from_agent)
    target_untrusted = config.is_agent_untrusted(to_agent)

    # Check trust boundary escalation: untrusted delegator/parent cannot delegate to trusted target
    if (delegator_untrusted or parent_untrusted) and not target_untrusted:
        raise ToolError(
            f"Trust boundary violation: untrusted agent/task {from_agent!r} "
            f"cannot delegate to privileged agent {to_agent!r}"
        )

    # If the origin/parent is untrusted, the child task is untrusted
    task_untrusted = delegator_untrusted or parent_untrusted

    from .tasks import task_add

    # Open roster has an empty agent_labels() set; the validation result is
    # the lane signal so Local_B (and fleet identities) still get a target lane.
    delegate_lane = to_agent if config.is_known_agent_label(to_agent) else None
    # A keyed request derives its inner keys from the receipt, so a takeover
    # after a crash reuses the child task and note instead of orphaning them.
    task_id = task_add(
        title=title,
        notes="\n".join(notes_lines),
        priority=priority,
        parent_id=parent_task_id,
        untrusted=task_untrusted,
        project=project,
        lane=delegate_lane,
        idempotency_key=delegate_receipts.derived_key(keyed[0], "task") if keyed else None,
    )["id"]

    # Attributed audit record in shared memory (best-effort: the delegation
    # stands on the task + delegations row even if the note fails to save).
    memory_id = None
    try:
        from .memory import memory_save

        memory_id = memory_save(
            content=(
                f"Delegation: {from_agent} -> {to_agent} (task #{task_id})\n\n"
                f"Spec:\n{task_spec}\n\n"
                + (f"Deadline: {deadline}\n" if deadline else "")
                + f"Callback on completion: {callback}"
            ),
            title=f"Delegation {from_agent}->{to_agent}: {title}",
            tags=["delegation", "project:unified-agent"],
            author=from_agent,
            idempotency_key=delegate_receipts.derived_key(keyed[0], "memory") if keyed else None,
        )["id"]
    except Exception:
        pass

    warnings = spec_check.get("warnings", []) + spec_check.get("errors", [])
    if dispatch_warnings:
        warnings += dispatch_warnings
    ts = storage.now()
    conn = storage.connect()
    try:
        if keyed:
            conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO delegations (from_agent, to_agent, task_id, task_spec, deadline, "
            "callback, status, memory_id, created_at, updated_at, org_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (
                from_agent,
                to_agent,
                task_id,
                task_spec,
                deadline,
                callback,
                memory_id,
                ts,
                ts,
                edition.tenant_org_id(),
            ),
        )
        delegation_id = cur.lastrowid
        result = {"delegation_id": delegation_id, "task_id": task_id, "memory_id": memory_id}
        if dispatch_warnings:
            result["dispatch_warnings"] = dispatch_warnings
        if warnings:
            result["spec_warnings"] = warnings
        if keyed:
            # The delegations row and its receipt commit together: a dropped
            # response after this point replays, it never inserts again.
            competing = delegate_receipts.commit_on_connection(
                conn, keyed[0], keyed[1], keyed[2], result
            )
            if competing is not None:
                conn.rollback()
                return competing
        conn.commit()
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()

    try:
        storage.record_agent_event(
            "delegation",
            ref_id=str(delegation_id),
            agent=from_agent,
            summary=f"-> {to_agent} (task #{task_id}): {title}",
        )
    except Exception:
        pass
    return result


def delegate_lookup(idempotency_key: str, from_agent: str | None = None) -> dict:
    """Recover a keyed delegation's outcome without any effect (task #1938).

    Reads only the caller's own receipt: `from_agent` follows the verified
    identity exactly as `delegate` does. `state` is `committed` (with the
    original `result`), `approval_required` (with `approval_id`),
    `in_progress` (with `since` and `stale`), or `not_found` (never received,
    or refused before commit: the identical retry with the same key is safe).
    """
    actor = config.verify_write_identity(from_agent)
    if not actor:
        raise ToolError("delegation lookup requires an authenticated identity")
    actor = _validate_label(actor, "from_agent")
    key = delegate_receipts.validate_key(idempotency_key)
    view = delegate_receipts.lookup(delegate_receipts.storage_key(actor, key))
    return {"idempotency_key": key, **view}


def delegate_list(
    status: str = "pending",
    to_agent: str | None = None,
    limit: int = 50,
    task_id: int | None = None,
) -> dict:
    """List delegations by status (pending|completed|cancelled|all) (task #62 · A).

    Optional `to_agent` and `task_id` filters provide bounded canonical inventory.
    `truncated` reports whether matching rows remain beyond the returned limit. Pending
    rows past their deadline carry `overdue: true` — advisory only, nothing
    auto-expires. A pending row whose task is held as interrupted work (task #2482)
    carries `held` with the reason: it is listed, but `delegate_claim` refuses it, so
    a poller must skip it. Returns {delegations: [...]}.
    """
    if status != "all" and status not in DELEGATION_STATUSES:
        raise ToolError(f"status must be one of {DELEGATION_STATUSES + ('all',)}")
    where, params = [], []
    if status != "all":
        where.append("status=?")
        params.append(status)
    if to_agent is not None:
        where.append("to_agent=?")
        params.append(to_agent)
    if task_id is not None:
        if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
            raise ToolError("task_id must be a positive integer")
        where.append("task_id=?")
        params.append(task_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    page_limit = max(1, min(limit, 200))
    params.append(page_limit + 1)
    now = storage.now()
    conn = storage.connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM delegations {clause} ORDER BY created_at DESC LIMIT ?", params
        ).fetchall()
        out = []
        for r in rows[:page_limit]:
            d = _row(r)
            d["overdue"] = bool(d["status"] == "pending" and d["deadline"] and d["deadline"] < now)
            out.append(d)
        held = lease_reaper.interrupted_holds(
            conn, [d["task_id"] for d in out if d["status"] == "pending"]
        )
        for d in out:
            if d["status"] == "pending" and d["task_id"] in held:
                d["held"] = held[d["task_id"]]
        return {"delegations": out, "truncated": len(rows) > page_limit}
    finally:
        conn.close()


def fire_completion_callbacks(task_id: int) -> None:
    """Fire pending delegation callbacks for a just-completed child task.

    Called (best-effort) from task_update when a task transitions to 'done'.
    Marks matching pending delegations completed and runs each one's callback:
    'notify' sends an iMessage via the bridge, 'memory' appends an attributed
    completion block to the delegation note, 'none' only flips the row. Any
    callback failure is recorded on the row (`callback_result`) and swallowed —
    completing a task must never fail because a callback did.
    """
    try:
        conn = storage.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM delegations WHERE task_id=? AND status IN ('pending','in_flight')",
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return

    for r in rows:
        d = _row(r)
        result = "ok"
        try:
            if d["callback"] == "notify":
                from .notify import notify_as

                # The callback speaks for the delegator, judged by the
                # delegator's comms policy at fire time, not the completer's
                # (task #1735): a funnel adopted after delegating still holds.
                res = notify_as(
                    f"[delegation {d['id']}] task #{task_id} "
                    f"({d['from_agent']} -> {d['to_agent']}) is done: "
                    f"{d['task_spec'].splitlines()[0][:200]}",
                    sender=d["from_agent"],
                    route="delegate_callback",
                )
                if not res.get("delivered"):
                    result = f"notify failed: {res.get('error')}"
            elif d["callback"] == "memory" and d["memory_id"]:
                from .memory import memory_save

                # author intentionally omitted: verify_write_identity() would
                # reject claiming d["to_agent"] as author whenever the actual
                # caller of task_update isn't that agent's own authenticated
                # session (e.g. the delegator completing on the assignee's
                # behalf) — a real, common live pattern, not an edge case.
                # Omitting author lets it fall through to the real verified
                # caller (or None if unauthenticated); the assignee is still
                # named in the note text below.
                memory_save(
                    content=(
                        f"Completed: task #{task_id} (assignee: {d['to_agent']}) "
                        f"marked done at {storage.now()}."
                    ),
                    id=d["memory_id"],
                    append=True,
                )
        except Exception as e:  # noqa: BLE001 - callbacks are strictly best-effort
            result = f"{type(e).__name__}: {e}"

        try:
            ts = storage.now()
            conn = storage.connect()
            try:
                conn.execute(
                    "UPDATE delegations SET status='completed', completed_at=?, "
                    "updated_at=?, callback_result=? WHERE id=? "
                    "AND status IN ('pending','in_flight')",
                    (ts, ts, result[:500], d["id"]),
                )
                conn.commit()
            finally:
                conn.close()
            storage.record_agent_event(
                "delegation",
                ref_id=str(d["id"]),
                agent=d["to_agent"],
                summary=f"completed (task #{task_id}), callback={d['callback']}: {result[:80]}",
            )
            # Attribution wedge (#1155): signed producer for the authenticated
            # actor who completed the delegation (fail-closed if unauthenticated
            # or no bearer mapping for that identity).
            if config.verified_agent_identity():
                try:
                    from ..audit.producer_sign import stamp_authenticated_producer_event

                    stamp_authenticated_producer_event(
                        kind="delegation",
                        ref_id=d["id"],
                        task_id=task_id,
                    )
                except Exception:
                    pass
        except Exception:
            pass


def delegation_ledger(agent: str | None = None, limit: int = 100) -> dict:
    """Calculate per-agent delegation stats and review outcome ledger (Task #126 / Phase 2.2).

    Args:
        agent: Optional agent label filter (e.g. "Desktop_agent3"). If omitted, reports on all agents.
        limit: Max delegation records to evaluate (default 100, capped at 500).

    Returns:
        Dict with agent (the filter, or None), total_delegations_analyzed,
        agent_stats, failure_pairs, outcomes_awaiting_review and
        summary_markdown.
    """
    if agent is not None:
        agent = _validate_label(agent, "agent")

    limit = max(1, min(limit, 500))
    conn = storage.connect()
    try:
        query = (
            "SELECT d.id, d.from_agent, d.to_agent, d.task_id, d.status AS delegation_status, "
            "d.created_at, d.completed_at, t.notes, t.status AS task_status "
            "FROM delegations d "
            "LEFT JOIN tasks t ON d.task_id = t.id "
        )
        params = []
        if agent is not None:
            query += "WHERE d.from_agent = ? OR d.to_agent = ? "
            params.extend([agent, agent])
        query += "ORDER BY d.created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    review_rx = _REVIEW_ANNOTATION_RE

    stats: dict[str, dict] = {}

    def get_agent_stat(lbl: str) -> dict:
        if lbl not in stats:
            stats[lbl] = {
                "sent": 0,
                "received": 0,
                "completed": 0,
                "pending": 0,
                "outcomes": {
                    "merged": 0,
                    "rework": 0,
                    "denied": 0,
                    "duplicate": 0,
                    "unreviewed": 0,
                },
            }
        return stats[lbl]

    for r in rows:
        from_a = r["from_agent"]
        to_a = r["to_agent"]

        s_from = get_agent_stat(from_a)
        s_to = get_agent_stat(to_a)

        s_from["sent"] += 1
        s_to["received"] += 1

        if r["delegation_status"] == "completed" or r["task_status"] == "done":
            s_to["completed"] += 1
        elif r["delegation_status"] == "pending":
            s_to["pending"] += 1

        # Parse review annotation from task notes
        notes = r["notes"] or ""
        match = review_rx.search(notes)
        outcome = "unreviewed"
        if match:
            raw_rev = match.group(1).strip().lower()
            if any(k in raw_rev for k in ("merged", "clean", "approved")):
                outcome = "merged"
            elif any(k in raw_rev for k in ("rework", "fix", "changes")):
                outcome = "rework"
            elif any(k in raw_rev for k in ("denied", "rejected")):
                outcome = "denied"
            elif any(k in raw_rev for k in ("duplicate", "closed")):
                outcome = "duplicate"

        s_to["outcomes"][outcome] += 1

    # Calculate acceptance rates
    for st in stats.values():
        o = st["outcomes"]
        reviewed_total = o["merged"] + o["rework"] + o["denied"]
        if reviewed_total > 0:
            st["acceptance_rate"] = round(o["merged"] / reviewed_total * 100, 1)
        else:
            st["acceptance_rate"] = 100.0 if o["unreviewed"] == 0 else 0.0

    # Build Markdown table
    lines = [
        "# Delegation Outcome Ledger",
        "",
        "| Agent | Sent | Received | Completed | Pending | Merged | Rework | Denied | Duplicate | Acceptance Rate |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for lbl in sorted(stats.keys()):
        st = stats[lbl]
        o = st["outcomes"]
        lines.append(
            f"| `{lbl}` | {st['sent']} | {st['received']} | {st['completed']} | {st['pending']} | "
            f"{o['merged']} | {o['rework']} | {o['denied']} | {o['duplicate']} | {st['acceptance_rate']}% |"
        )

    if not rows:
        lines.append("\n*No delegation records found.*")

    # Failure-pair report (task #1142): consecutive failures + last failure_class.
    pair_report = failure_pair_report(limit=limit)
    if pair_report["pairs"]:
        lines.extend(
            [
                "",
                "## Failure pairs",
                "",
                "| From → To | Consecutive failures | Last failure_class | Gated |",
                "| --- | --- | --- | --- |",
            ]
        )
        for p in pair_report["pairs"]:
            gated = "yes" if p["blocked"] else "no"
            lines.append(
                f"| `{p['from_agent']}` → `{p['to_agent']}` | "
                f"{p['consecutive_failures']} | `{p['last_failure_class']}` | {gated} |"
            )

    awaiting = outcomes_awaiting_review(limit=limit)
    if awaiting["outcomes"]:
        lines.extend(
            [
                "",
                f"## Outcomes awaiting review ({awaiting['count']})",
                "",
            ]
        )
        for o in awaiting["outcomes"][:20]:
            lines.append(
                f"- delegation #{o['delegation_id']} "
                f"(`{o['from_agent']}` → `{o['to_agent']}`, {o['status']})"
            )

    summary_md = "\n".join(lines)
    return {
        "agent": agent,
        "total_delegations_analyzed": len(rows),
        "agent_stats": stats,
        "failure_pairs": pair_report["pairs"],
        "outcomes_awaiting_review": awaiting["outcomes"],
        "summary_markdown": summary_md,
    }


def delegate_claim(id: int, agent: str) -> dict:
    """Atomically take a pending delegation for execution (task #169).

    A single conditional UPDATE moves the row `pending` -> `in_flight`, so two
    daemons racing for the same delegation cannot both win, and — the actual bug
    this fixes — the row stops matching the executor's poll query. Without it
    nothing marked a delegation as taken: `find_pending_work` re-listed it every
    5s tick and the daemon re-claimed it, 138 times on task #149.

    Returns {claimed, delegation}. `claimed` is False when another daemon got
    there first or the row is no longer pending; the caller must skip it rather
    than execute. It is also False, with `held` naming the reason and the row
    left pending, while the delegation's task is held as interrupted work
    (task #2482): the lease reaper left it blocked and unowned, and nothing
    queued may pick it up until someone resumes, reopens or closes it.
    """
    agent = _caller_label(agent)
    ts = storage.now()
    conn = storage.connect()
    try:
        # One write transaction, so the hold read and the claim cannot be
        # split by the reaper's release.
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute(
            "SELECT * FROM delegations WHERE id=? AND status='pending' AND to_agent=?",
            (id, agent),
        ).fetchone()
        if pending is not None:
            held = lease_reaper.interrupted_hold(conn, pending["task_id"])
            if held:
                conn.rollback()
                return {"claimed": False, "held": held, "delegation": _row(pending)}
        cur = conn.execute(
            "UPDATE delegations SET status='in_flight', attempts=attempts+1, "
            "claim_generation=claim_generation+1, last_attempt_at=?, updated_at=? "
            "WHERE id=? AND status='pending' AND to_agent=?",
            (ts, ts, id, agent),
        )
        claimed = cur.rowcount > 0
        # Read the generation while this write transaction still owns the
        # lock. A release/reclaim after commit must not replace our token in
        # the response with the successor's generation.
        row = conn.execute("SELECT * FROM delegations WHERE id=?", (id,)).fetchone()
        if row is None:
            raise ToolError(f"no delegation with id {id}")
        conn.commit()
        if claimed:
            try:
                storage.record_agent_event(
                    "delegation",
                    ref_id=str(id),
                    agent=agent,
                    summary=f"in_flight: task #{row['task_id']}",
                )
            except Exception:
                pass
        return {"claimed": claimed, "delegation": _row(row)}
    finally:
        conn.close()


def delegate_release(
    id: int,
    agent: str,
    expected_claim_generation: int | None = None,
    expected_run_id: str | None = None,
    task_id: int | None = None,
) -> dict:
    """Return an in-flight delegation to `pending` (task #169).

    For recovery, not for retry-on-failure: a daemon that crashed mid-execution
    leaves the row in_flight, and the startup reclaim releases it once the task
    lock has expired. Releasing on every failure instead would re-run a failing
    delegation forever at poll cadence.
    """
    agent = _caller_label(agent)
    expected_claim_generation = _validate_claim_generation(expected_claim_generation)
    run_guard, run_params = _delegation_run_guard(
        agent, task_id, expected_run_id, allow_finished=False
    )
    ts = storage.now()
    conn = storage.connect()
    try:
        cur = conn.execute(
            "UPDATE delegations SET status='pending', attempts=MAX(0, attempts-1), last_attempt_at=NULL, updated_at=? "
            "WHERE id=? AND status='in_flight' AND to_agent=? "
            "AND claim_generation=?" + run_guard,
            [ts, id, agent, expected_claim_generation, *run_params],
        )
        conn.commit()
        return _transition_result("released", cur.rowcount > 0, expected_run_id)
    finally:
        conn.close()


def delegate_cancel(
    id: int,
    agent: str,
    reason: str = "delegation abandoned",
    expected_claim_generation: int | None = None,
    expected_run_id: str | None = None,
    require_no_run: bool = False,
) -> dict:
    """Retire a delegation without claiming successful completion.

    A cancelled task cannot trigger the normal completion callback, so this
    explicit terminal transition prevents pending rows from becoming
    permanent queue residue. Only the delegator or assignee may retire it.
    """
    agent = _caller_label(agent)
    expected_claim_generation = _validate_claim_generation(expected_claim_generation)
    if expected_run_id is not None and expected_claim_generation is None:
        raise ToolError("expected_claim_generation is required with expected_run_id")
    reason = (reason or "delegation abandoned").strip()[:500]
    ts = storage.now()
    if expected_run_id is not None and require_no_run:
        raise ToolError("expected_run_id and require_no_run are mutually exclusive")
    guards = []
    params: list[Any] = [ts, ts, reason, id, agent, agent]
    if expected_claim_generation is not None:
        guards.append("AND claim_generation=?")
        params.append(expected_claim_generation)
    if expected_run_id is not None:
        # The old attempt may retire its delegation when its own run was
        # cancelled or superseded, but never after a new delegation attempt.
        guards.append(
            "AND EXISTS (SELECT 1 FROM tasks t WHERE t.id=delegations.task_id "
            "AND (t.current_run_id IS NOT ? OR t.cancel_requested=1))"
        )
        params.append(expected_run_id)
    if require_no_run:
        guards.append(
            "AND EXISTS (SELECT 1 FROM tasks t WHERE t.id=delegations.task_id "
            "AND t.current_run_id IS NULL)"
        )
    conn = storage.connect()
    try:
        cur = conn.execute(
            "UPDATE delegations SET status='cancelled', completed_at=?, updated_at=?, "
            "callback_result=? WHERE id=? AND status IN ('pending','in_flight') "
            "AND (from_agent=? OR to_agent=?) " + " ".join(guards),
            params,
        )
        conn.commit()
        return {"cancelled": cur.rowcount > 0}
    finally:
        conn.close()


def cancel_delegations_for_task(task_id: int, reason: str = "task cancelled", *, conn=None) -> int:
    """Retire current task delegations in the caller's cancellation transaction.

    A borrowed connection is neither committed nor closed here: the task/run
    transition and its delegation retirement must win or roll back together.
    """
    ts = storage.now()
    own_connection = conn is None
    if own_connection:
        conn = storage.connect()
    try:
        cur = conn.execute(
            "UPDATE delegations SET status='cancelled', completed_at=?, updated_at=?, "
            "callback_result=? WHERE task_id=? AND status IN ('pending','in_flight')",
            (ts, ts, reason[:500], task_id),
        )
        if own_connection:
            conn.commit()
        return cur.rowcount
    finally:
        if own_connection:
            conn.close()


def delegate_fail(
    id: int,
    agent: str,
    reason: str = "delegation failed",
    expected_claim_generation: int | None = None,
    expected_run_id: str | None = None,
    task_id: int | None = None,
) -> dict:
    """Terminal-fail a delegation (task #1000).

    Hub-callable equivalent of the executor daemon's direct
    ``UPDATE delegations SET status='failed' ...`` write in
    ``ExecutorDaemon._mark_delegation_failed``. That local write can raise
    `storage.LiveStoreError` when the caller's local memory.db is the Mac
    hub tombstone (task #987) — this is the fallback the daemon round-trips
    to instead of losing the mutation. Executor calls pass their claim
    generation so a delayed write cannot fail a successor attempt.
    """
    agent = _caller_label(agent)
    expected_claim_generation = _validate_claim_generation(expected_claim_generation)
    run_guard, run_params = _delegation_run_guard(
        agent, task_id, expected_run_id, allow_finished=False
    )
    reason = (reason or "delegation failed").strip()[:500]
    ts = storage.now()
    conn = storage.connect()
    try:
        cur = conn.execute(
            "UPDATE delegations SET status='failed', updated_at=?, callback_result=? WHERE id=? "
            "AND (from_agent=? OR to_agent=?) "
            "AND ((? IS NULL AND status='pending') "
            "OR (claim_generation=? AND status='in_flight'))" + run_guard,
            [
                ts,
                reason,
                id,
                agent,
                agent,
                expected_claim_generation,
                expected_claim_generation,
                *run_params,
            ],
        )
        conn.commit()
        failed = cur.rowcount > 0
        if failed:
            try:
                storage.record_agent_event(
                    "delegation",
                    ref_id=str(id),
                    agent=agent,
                    summary=f"failed: {reason[:80]}",
                )
            except Exception:
                pass
        return _transition_result("failed", failed, expected_run_id)
    finally:
        conn.close()


def delegate_complete(
    id: int,
    agent: str,
    result: str = "",
    expected_claim_generation: int | None = None,
    expected_run_id: str | None = None,
    task_id: int | None = None,
) -> dict:
    """Mark a delegation completed (task #1000).

    Hub-callable equivalent of the executor daemon's finish-to-review
    completion write. Completion is the most severe of the five call
    sites task #1000 fixes: without this fallback, a task the executor
    fully ran to success could still lose its completion record forever
    if local memory.db were unusable at that exact moment (see
    `delegate_fail` for the same tombstone scenario).
    """
    agent = _caller_label(agent)
    expected_claim_generation = _validate_claim_generation(expected_claim_generation)
    run_guard, run_params = _delegation_run_guard(
        agent, task_id, expected_run_id, allow_finished=True
    )
    result = (result or "").strip()[:500]
    ts = storage.now()
    conn = storage.connect()
    try:
        cur = conn.execute(
            "UPDATE delegations SET status='completed', completed_at=?, updated_at=?, "
            "callback_result=? WHERE id=? AND (from_agent=? OR to_agent=?) "
            "AND ((? IS NULL AND status='pending') "
            "OR (claim_generation=? AND status='in_flight'))" + run_guard,
            [
                ts,
                ts,
                result,
                id,
                agent,
                agent,
                expected_claim_generation,
                expected_claim_generation,
                *run_params,
            ],
        )
        conn.commit()
        return _transition_result("completed", cur.rowcount > 0, expected_run_id)
    finally:
        conn.close()


def delegate_retry(
    id: int,
    agent: str,
    reason: str = "",
    expected_claim_generation: int | None = None,
    expected_run_id: str | None = None,
    task_id: int | None = None,
    clear_backoff: bool = False,
) -> dict:
    """Return a delegation to 'pending' for a backoff-scheduled retry (task #1000).

    Distinct from `delegate_release`: release is a claim-recovery
    primitive that decrements `attempts` and clears `last_attempt_at` (the
    "we never actually started" case, task #169). This is the
    self-healing-guardrails retry-after-failed-execution path — `attempts`
    stays exactly as `delegate_claim` already incremented it, and
    `last_attempt_at` is left alone so backoff enforcement keeps working.
    Fenced startup recovery may set `clear_backoff=True` to retry immediately
    while preserving the real attempt count and its ceiling.
    """
    if not isinstance(clear_backoff, bool):
        raise ToolError("clear_backoff must be a boolean")
    agent = _caller_label(agent)
    expected_claim_generation = _validate_claim_generation(expected_claim_generation)
    run_guard, run_params = _delegation_run_guard(
        agent, task_id, expected_run_id, allow_finished=False
    )
    reason = (reason or "").strip()[:500]
    ts = storage.now()
    conn = storage.connect()
    try:
        cur = conn.execute(
            "UPDATE delegations SET status='pending', updated_at=?, callback_result=?, "
            "last_attempt_at=CASE WHEN ? THEN NULL ELSE last_attempt_at END WHERE id=? "
            "AND to_agent=? AND claim_generation=? AND status='in_flight'" + run_guard,
            [ts, reason, clear_backoff, id, agent, expected_claim_generation, *run_params],
        )
        conn.commit()
        return _transition_result("retried", cur.rowcount > 0, expected_run_id)
    finally:
        conn.close()


def delegate_get(id: int) -> dict:
    """Fetch one delegation row by id (task #1000).

    Hub-callable equivalent of the executor daemon's direct
    ``SELECT attempts FROM delegations WHERE id=?`` read, so the
    self-healing-guardrails attempt-count check still works when local
    storage is unusable (see `delegate_fail`).
    """
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM delegations WHERE id=?", (id,)).fetchone()
        if row is None:
            raise ToolError(f"no delegation with id {id}")
        return {"delegation": _row(row)}
    finally:
        conn.close()


_DELEGATION_ACTIONS = {
    "create": delegate,
    "list": delegate_list,
    "claim": delegate_claim,
    "release": delegate_release,
    "cancel": delegate_cancel,
    "ledger": delegation_ledger,
    "fail": delegate_fail,
    "complete": delegate_complete,
    "retry": delegate_retry,
    "get": delegate_get,
    "lookup": delegate_lookup,
    "failure_pair_gate": pair_failure_status,
    "outcomes_awaiting_review": outcomes_awaiting_review,
    "failure_pairs": failure_pair_report,
}


def delegation_manage(action: str, params: dict | None = None) -> dict:
    """Dispatch delegation lifecycle and specification operations.

    ``action`` is one of create, list, claim, release, cancel, ledger, fail,
    complete, retry, get, lookup, failure_pair_gate, outcomes_awaiting_review,
    failure_pairs, spec_check, spec_template, or spec_build. `lookup`
    (task #1938) reads the caller's receipt for a keyed `create` without any
    effect, so a client that lost a response can recover its outcome. `fail`,
    `complete`, `retry`, and `get` (task #1000) are the hub-callable
    fallbacks for the executor daemon's direct-local-storage delegation
    writes/reads, mirroring the same shape `create`/`claim`/`release`/
    `cancel` already have. `failure_pair_gate` / `outcomes_awaiting_review`
    / `failure_pairs` (task #1142) are the dispatch-gate read surface.
    Authorization is checked against the underlying
    operation so the consolidated entry point cannot broaden delegation
    powers.
    """
    from .delegation_spec import (
        delegation_spec_build,
        delegation_spec_check,
        delegation_spec_template,
    )

    actions = {
        **_DELEGATION_ACTIONS,
        "spec_check": delegation_spec_check,
        "spec_template": delegation_spec_template,
        "spec_build": delegation_spec_build,
    }
    if not isinstance(action, str):
        raise ToolError("action must be a string")
    action = action.strip().lower()
    fn = actions.get(action)
    if fn is None:
        raise ToolError(f"action must be one of {tuple(actions)}")
    if not profiles.consolidated_action_is_allowed("delegation_manage", fn.__name__):
        raise ToolError(f"delegation action {action!r} is not exposed by the active profile")
    if action == "lookup" and not profiles.consolidated_action_is_allowed(
        "delegation_manage", delegate.__name__
    ):
        # A lookup reads a keyed create's receipt, so it follows create permission.
        raise ToolError("delegation action 'lookup' is not exposed by the active profile")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ToolError("params must be an object")
    try:
        bound = inspect.signature(fn).bind(**params)
    except TypeError as exc:
        raise ToolError(f"invalid params for delegation action {action!r}: {exc}") from exc
    safety.check_policy_gate(fn.__name__, bound.arguments)
    return fn(*bound.args, **bound.kwargs)
