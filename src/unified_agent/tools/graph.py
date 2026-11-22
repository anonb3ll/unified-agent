"""Safe execution-graph dependency and handoff operations."""

import inspect

from mcp.server.mcpserver.exceptions import ToolError

from .. import (
    agent_lifecycle,
    claim_release,
    config,
    design_panels,
    friction_rollup,
    node_acceptance,
    node_command,
    node_registry,
    profiles,
    project_bindings,
    queue_closure,
    safety,
    storage,
    task_holds,
    task_workflow,
    team_snapshot,
    team_spec,
    work_proposals,
)
from ..lifecycle import handoff as session_handoff
from ..messaging import handoff as conversation_handoff
from . import delegate as delegate_tool
from . import messages as message_tools


def _agent(agent: str | None) -> str:
    verified = config.verified_agent_identity()
    if verified and agent and agent != verified:
        raise ToolError(f"Auth identity mismatch: requested {agent!r} but verified as {verified!r}")
    resolved = verified or (agent or "").strip()
    if not resolved or not config.is_known_agent_label(resolved):
        raise ToolError("an authenticated known agent identity is required")
    return resolved


def _task_exists(conn, task_id: int) -> None:
    if conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
        raise ToolError(f"no task with id {task_id}")


def dependency_list(limit: int = 500, task_id: int | None = None) -> dict:
    """List explicit task dependencies using stable edge IDs.

    Pass `task_id` to retrieve just one task's prerequisite edges. `truncated`
    reports whether further edges exist beyond the clamped (at most 2000) limit.
    """
    conn = storage.connect()
    try:
        where, params = "", []
        if task_id is not None:
            where = " WHERE task_id=?"
            params.append(task_id)
        page_limit = max(1, min(limit, 2000))
        params.append(page_limit + 1)
        rows = conn.execute(
            "SELECT id, task_id, depends_on_task_id, created_by, created_at "
            f"FROM task_dependencies{where} ORDER BY id LIMIT ?",
            params,
        ).fetchall()
        return {
            "dependencies": [{key: row[key] for key in row.keys()} for row in rows[:page_limit]],
            "truncated": len(rows) > page_limit,
        }
    finally:
        conn.close()


def dependency_add(task_id: int, depends_on_task_id: int, agent: str | None = None) -> dict:
    """Add a prerequisite edge, rejecting missing nodes and cycles atomically."""
    created_by = _agent(agent)
    if task_id == depends_on_task_id:
        raise ToolError("a task cannot depend on itself")
    conn = storage.connect()
    try:
        _task_exists(conn, task_id)
        _task_exists(conn, depends_on_task_id)
        cycle = conn.execute(
            "WITH RECURSIVE reachable(id) AS ("
            " SELECT depends_on_task_id FROM task_dependencies WHERE task_id=?"
            " UNION "
            " SELECT d.depends_on_task_id FROM task_dependencies d "
            " JOIN reachable r ON d.task_id=r.id"
            ") SELECT 1 FROM reachable WHERE id=? LIMIT 1",
            (depends_on_task_id, task_id),
        ).fetchone()
        if cycle:
            raise ToolError("dependency would create a cycle")
        try:
            cur = conn.execute(
                "INSERT INTO task_dependencies "
                "(task_id, depends_on_task_id, created_by, created_at) VALUES (?, ?, ?, ?)",
                (task_id, depends_on_task_id, created_by, storage.now()),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise ToolError("dependency already exists") from None
            raise
        conn.commit()
        row = conn.execute(
            "SELECT id, task_id, depends_on_task_id, created_by, created_at "
            "FROM task_dependencies WHERE id=?",
            (cur.lastrowid,),
        ).fetchone()
        return {"dependency": {key: row[key] for key in row.keys()}}
    finally:
        conn.close()


def dependency_remove(dependency_id: int, agent: str | None = None) -> dict:
    """Remove an edge only when the caller owns it."""
    actor = _agent(agent)
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT * FROM task_dependencies WHERE id=?", (dependency_id,)
        ).fetchone()
        if row is None:
            raise ToolError(f"no dependency with id {dependency_id}")
        if row["created_by"] != actor:
            raise ToolError("only the dependency creator may remove this edge")
        conn.execute("DELETE FROM task_dependencies WHERE id=?", (dependency_id,))
        conn.commit()
        return {"removed": True, "dependency_id": dependency_id}
    finally:
        conn.close()


def handoff(
    task_spec: str,
    to_agent: str,
    agent: str | None = None,
    deadline: str | None = None,
    priority: str = "med",
    parent_task_id: int | None = None,
) -> dict:
    """Create a delegation handoff bound to the verified sender identity."""
    from_agent = _agent(agent)
    result = delegate_tool.delegate(
        task_spec=task_spec,
        from_agent=from_agent,
        to_agent=to_agent,
        deadline=deadline,
        callback="none",
        priority=priority,
        parent_task_id=parent_task_id,
    )
    return {
        "handoff_id": result["delegation_id"],
        "task_id": result["task_id"],
        "status": "pending",
    }


def lineage(task_id: int) -> dict:
    """Per-task execution lineage via one null-tolerant JOIN (task #730).

    Read-only projection of existing rows: delegations (cancelled filtered
    out), left-joined dispatch_runs and autonomous_cycles on task_id, plus
    refs (pr_url / worktree_path / branch). Legacy rows with no lane, run, or
    PR render as empty nodes — missing or unknown task ids return 200-shaped
    ``{"empty": true}`` payloads, never errors. No new tables; JOIN only.
    """
    conn = storage.connect()
    try:
        task_row = conn.execute(
            "SELECT id, title, worktree_path, branch, evidence_ref FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        delegation_rows = conn.execute(
            "SELECT id, status, from_agent, to_agent, task_id "
            "FROM delegations WHERE task_id=? AND status != 'cancelled' ORDER BY id",
            (task_id,),
        ).fetchall()
        run_rows = conn.execute(
            "SELECT run_id, task_id, state, updated_at, agent_label FROM dispatch_runs "
            "WHERE task_id=? ORDER BY created_at",
            (task_id,),
        ).fetchall()
        cycle_rows = conn.execute(
            "SELECT id, task_id, state, pr_url, updated_at FROM autonomous_cycles "
            "WHERE task_id=? ORDER BY created_at",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()

    delegations = [
        {
            "id": row["id"],
            "status": row["status"],
            "from_agent": row["from_agent"],
            "to_agent": row["to_agent"],
            "task_id": row["task_id"],
        }
        for row in delegation_rows
    ]
    dispatch_runs = [
        {
            "id": row["run_id"],
            "task_id": row["task_id"],
            "status": row["state"],
            "updated_at": row["updated_at"],
            "agent_label": row["agent_label"],
        }
        for row in run_rows
    ]
    autonomous_cycles = [
        {
            "id": row["id"],
            "task_id": row["task_id"],
            "status": row["state"],
            "updated_at": row["updated_at"],
        }
        for row in cycle_rows
    ]

    worktree_path = task_row["worktree_path"] if task_row is not None else None
    branch = task_row["branch"] if task_row is not None else None
    evidence = (task_row["evidence_ref"] if task_row is not None else "") or ""
    cycle_pr_urls = [row["pr_url"] for row in cycle_rows if row["pr_url"]]
    pr_url = (
        cycle_pr_urls[0] if cycle_pr_urls else (evidence if evidence.startswith("http") else None)
    )

    return {
        "task": {"id": task_id, "title": task_row["title"] if task_row is not None else None},
        "lineage": {
            "delegations": delegations,
            "dispatch_runs": dispatch_runs,
            "autonomous_cycles": autonomous_cycles,
            "refs": {
                "pr_url": pr_url,
                "worktree_path": worktree_path,
                "branch": branch,
            },
        },
        "empty": not (
            delegations or dispatch_runs or autonomous_cycles or worktree_path or branch or pr_url
        ),
    }


def handoff_list(limit: int = 200) -> dict:
    """Return lifecycle metadata only, never specs or callback payloads."""
    result = delegate_tool.delegate_list(status="all", limit=limit)
    safe_fields = (
        "id",
        "task_id",
        "from_agent",
        "to_agent",
        "deadline",
        "status",
        "attempts",
        "last_attempt_at",
        "created_at",
        "updated_at",
        "completed_at",
    )
    return {
        "handoffs": [
            {key: row.get(key) for key in safe_fields} for row in result.get("delegations", [])
        ]
    }


def approval_ledger_rerequest(
    number: int, head_sha: str, independent_review: dict | None = None
) -> dict:
    """Re-request the Approval Ledger row for one open PR head (task #1854).

    Operators only; queues the approval and never resolves it. See
    ``github_app_worker.rerequest_head``.
    """
    from .. import github_app_worker

    if independent_review is None:
        return github_app_worker.rerequest_head(number, head_sha)
    return github_app_worker.rerequest_head(number, head_sha, independent_review=independent_review)


def approval_ledger_poll() -> dict:
    """Run one Approval Ledger poll on the hub now (task #1867).

    Operators only; requests approval rows and never resolves one. See
    ``github_app_worker.poll_now``.
    """
    from .. import github_app_worker

    return github_app_worker.poll_now()


def session_handoff_propose(
    session_id: str,
    done: list[str],
    next_steps: list[str],
    open_questions: list[str] | None = None,
    task_id: int | None = None,
    supersede: bool = False,
    agent: str | None = None,
) -> dict:
    """Write a draft handoff note for one of your own sessions (task #2397)."""
    return session_handoff.propose(
        session_id,
        agent=_agent(agent),
        done=done,
        next_steps=next_steps,
        open_questions=open_questions,
        task_id=task_id,
        supersede=supersede,
    )


def session_handoff_update(
    note_id: int,
    done: list[str] | None = None,
    next_steps: list[str] | None = None,
    open_questions: list[str] | None = None,
    agent: str | None = None,
) -> dict:
    """Edit your draft handoff note in place; a sealed note is immutable."""
    return session_handoff.update_draft(
        note_id,
        agent=_agent(agent),
        done=done,
        next_steps=next_steps,
        open_questions=open_questions,
    )


def session_handoff_seal(note_id: int, agent: str | None = None) -> dict:
    """Seal your draft handoff note: the commit point recovery reads from."""
    return session_handoff.seal(note_id, agent=_agent(agent))


def session_handoff_discard(note_id: int, agent: str | None = None) -> dict:
    """Discard your draft handoff note."""
    return session_handoff.discard(note_id, agent=_agent(agent))


def session_handoff_ack(note_id: int, agent: str | None = None) -> dict:
    """Acknowledge a handoff note handed to you; an assigned note becomes delivered."""
    return session_handoff.acknowledge(note_id, agent=_agent(agent))


def session_handoff_recover(session_id: str) -> dict:
    """The session's latest sealed, assigned or delivered handoff note, never a draft."""
    return {"note": session_handoff.recover(session_id)}


_COORDINATION_ACTIONS = {
    "dependency_list": dependency_list,
    "dependency_add": dependency_add,
    "dependency_remove": dependency_remove,
    "handoff": handoff,
    "handoff_list": handoff_list,
    "lineage": lineage,
    # Agent lifecycle controls (task #1351).
    "agent_state": agent_lifecycle.agent_state,
    "agent_states": agent_lifecycle.agent_states,
    "agent_pause": agent_lifecycle.agent_pause,
    "agent_resume": agent_lifecycle.agent_resume,
    "agent_terminate": agent_lifecycle.agent_terminate,
    # Retirement (task #2305): operators only; revokes the agent's credentials.
    "agent_retire": agent_lifecycle.agent_retire,
    "agent_unretire": agent_lifecycle.agent_unretire,
    # Task-scoped operator release of another agent's live claim (task #1700).
    "task_claim_release": claim_release.task_claim_release,
    # Project -> source-repository bindings (task #1425). Housed here rather
    # than behind a new top-level MCP tool: server.py's single tool-tuple is
    # a standing hotspot for concurrent branches, and `coordination_manage`
    # is already the precedent for adding a cross-module action family
    # without touching it. See `project_bindings.py`.
    "project_repo_bind": project_bindings.bind,
    "project_repo_unbind": project_bindings.unbind,
    "project_repo_get": project_bindings.get,
    "project_repo_list": project_bindings.list_bindings,
    # Team specs (task #1736): preview or boot a declared team, list specs,
    # read your own seat, and commit a spec as a policy revision. Housed here
    # for the same reason as the bindings above: the default profile is at
    # its tool cap, and server.py's tool tuple stays untouched.
    "team_up": team_spec.team_up,
    "team_list": team_spec.team_list,
    "team_seat": team_spec.team_seat,
    "team_spec_commit": team_spec.team_spec_commit,
    # Team snapshot and restore (task #1738): capture, read-only preview, restore.
    "team_snapshot": team_snapshot.snapshot,
    "team_snapshots": team_snapshot.list_snapshots,
    "team_restore_preview": team_snapshot.preview,
    "team_restore": team_snapshot.restore,
    # Queue closure (task #1848): staleness, park/fold, close-on-merge and a
    # health read. Housed here for the same reason as the families above: the
    # default profile is at its tool cap. See `queue_closure.py`.
    "queue_health": queue_closure.queue_health,
    "queue_stale": queue_closure.queue_stale,
    "queue_sweep": queue_closure.queue_sweep,
    "queue_park": queue_closure.queue_park,
    "queue_fold": queue_closure.queue_fold,
    "queue_unpark": queue_closure.queue_unpark,
    "queue_close_merged": queue_closure.queue_close_merged,
    # Mailbox state (task #1859): read-and-mark and ack. message_list stays a
    # read-only tool, so the actions that change read state live here.
    "message_inbox": message_tools.message_inbox,
    "message_ack": message_tools.message_ack,
    # Receiver grounding of a typed handoff (task #2390): report mode, never blocks.
    "handoff_ground": message_tools.handoff_ground,
    # Agent-authored session handoff (task #2397): propose, edit, seal (the
    # commit point), discard a draft, recover the latest sealed note.
    "session_handoff_propose": session_handoff_propose,
    "session_handoff_update": session_handoff_update,
    "session_handoff_seal": session_handoff_seal,
    "session_handoff_discard": session_handoff_discard,
    "session_handoff_recover": session_handoff_recover,
    "session_handoff_ack": session_handoff_ack,
    # Task subtree holds (task #1747): pause, cancel, resume or restore a
    # root task and its subtree in one action. Housed here for the same
    # reason as the families above: the default profile is at its tool cap.
    "task_hold": task_holds.task_hold,
    "task_holds": task_holds.task_holds,
    "task_hold_get": task_holds.task_hold_get,
    # Conversation handoff (task #1426): create or link a task that references
    # a visible agent conversation by id; the transcript is never copied.
    "conversation_handoff": conversation_handoff.hand_off_conversation,
    "conversation_handoffs": conversation_handoff.conversation_handoffs,
    # Approval Ledger re-request (task #1854): open the missing approval row for
    # one open PR head now instead of waiting for the next poll. Operators only.
    "approval_ledger_rerequest": approval_ledger_rerequest,
    # Approval Ledger poll-now (task #1867): run the scheduled poll once now,
    # e.g. right after a push. Operators only; never resolves an approval.
    "approval_ledger_poll": approval_ledger_poll,
    # Device acceptance (task #2112): queue a ua-node's harmless acceptance job,
    # re-evaluate it, or read the outcome. Housed here for the same reason as the
    # families above: server.py's tool tuple stays untouched.
    "node_acceptance_start": node_acceptance.node_acceptance_start,
    "node_acceptance_evaluate": node_acceptance.node_acceptance_evaluate,
    "node_acceptance_status": node_acceptance.node_acceptance_status,
    "node_acceptance_attest": node_acceptance.node_acceptance_attest,
    # Node retire (task #2181): take a renamed or decommissioned device's row out
    # of fleet rows and routing without deleting it. Lifecycle operators only.
    "node_retire": node_registry.node_retire,
    "node_unretire": node_registry.node_unretire,
    # Supervised long commands (task #2413): start an argv command on a ua-node
    # that outlives the starting session, read it, or adopt it from a later
    # session of the same agent. The owner gets one message when it ends.
    "node_command_start": node_command.node_command_start,
    "node_command_status": node_command.node_command_status,
    "node_command_adopt": node_command.node_command_adopt,
    # Batch work proposals (task #2393): agents file, the human operator
    # decides per item (over MCP for a human identity, or the Control Center's
    # POST /api/action/work-proposal-decide), and each approval spawns a task.
    "work_proposal_file": work_proposals.work_proposal_file,
    "work_proposal_decide": work_proposals.work_proposal_decide,
    "work_proposal_withdraw": work_proposals.work_proposal_withdraw,
    "work_proposal_get": work_proposals.work_proposal_get,
    "work_proposal_list": work_proposals.work_proposal_list,
    "work_proposal_recover": work_proposals.work_proposal_recover,
    # Task workflow record (task #2398): stage, resume capsule and checks with
    # proof grades (attested, verified, unknown). Report mode; gates nothing.
    "task_workflow_get": task_workflow.task_workflow_get,
    "task_workflow_set": task_workflow.task_workflow_set,
    "task_check_record": task_workflow.task_check_record,
    "task_check_verify": task_workflow.task_check_verify,
    # Independent-first design panels (task #2399): design questions only,
    # answers sealed until all are revealed together, recorded as a decision.
    "panel_open": design_panels.panel_open,
    "panel_answer": design_panels.panel_answer,
    "panel_get": design_panels.panel_get,
    "panel_list": design_panels.panel_list,
    "panel_reveal": design_panels.panel_reveal,
    "panel_decide": design_panels.panel_decide,
    "panel_cancel": design_panels.panel_cancel,
    # Friction rollup (task #2399): ranked by severity, count and channel
    # diversity; a fix claim closes only by re-check, or by a human accept.
    "friction_report": friction_rollup.friction_report,
    "friction_rollup": friction_rollup.friction_rollup,
    "friction_claim_fixed": friction_rollup.friction_claim_fixed,
    "friction_accept": friction_rollup.friction_accept,
    "friction_recheck": friction_rollup.friction_recheck,
}


def coordination_manage(action: str, params: dict | None = None) -> dict:
    """Dispatch dependency, handoff, agent lifecycle and project-repo-binding
    operations through one API.

    Lifecycle actions (task #1351): ``agent_state``/``agent_states`` read;
    ``agent_pause``/``agent_terminate`` (agent, reason) and ``agent_resume``
    (agent, reason) change state — operators may change any agent, an agent
    only itself. ``agent_retire`` (agent, reason, owner=None) and
    ``agent_unretire`` (agent, reason="") are operator-only (task #2305):
    retire is refused while the agent holds doing claims, revokes its
    agent_tokens rows and session credentials, and ``agent_resume`` cannot undo
    it. See ``agent_lifecycle``.

    Claim release (task #1700): ``task_claim_release`` (task_id,
    expected_claimant, reason, expected_run_id=None, expected_claimed_at=None) releases one task's live
    claim held by another agent. Lifecycle operators and the verified operator
    only; fenced on the claimant (and run), recorded on
    ``agent.task.claim_released``. A ``doing`` task returns to ``open``; done or
    cancelled is refused.
    A null run with expected_claimed_at also fences the absence of a run and
    that exact claim generation (HTTP requires this pair for an unbound claim).
    It does not stop a running executor: cancel first
    (``dispatch_cancel``, or ``task_update(request_cancel=True)``). See
    ``claim_release``.

    Project repo binding actions (task #1425): ``project_repo_bind``
    (project, repo, default_branch=None, bound_by=None), ``project_repo_unbind``
    (project, unbound_by=None), ``project_repo_get`` (project), and
    ``project_repo_list`` (). See ``project_bindings``.

    Team spec actions (task #1736): ``team_up`` (team=None, spec=None,
    plan=True, expected_plan_sha256=None) previews a team's boot plan, or with
    ``plan=False`` boots a committed spec behind a one-shot approval;
    ``team_list`` (); ``team_seat`` (label=None) returns your own seat, team
    and edges; ``team_spec_commit`` (team, spec) writes a spec as a policy
    revision (config_revision_manage authority). See ``team_spec``.

    Team snapshot actions (task #1738): ``team_snapshot`` (team=None,
    item_ids=None, reason="on_demand") captures every seat's session, resume
    capability and bounded packet; ``team_snapshots`` (limit=20) lists them;
    ``team_restore_preview`` (snapshot_id) shows the planned outcome per seat
    (resumed, rebuilt, fresh, failed or live) without changing anything;
    ``team_restore`` (snapshot_id) carries it out through the existing resume
    and approval-gated dispatch paths (a seat waiting for approval reports
    awaiting_approval). Only the seat's owner or the task's claimant may restore
    a seat, and restore needs dispatch authority. See ``team_snapshot``.

    Queue closure actions (task #1848, see ``queue_closure``): reads
    ``queue_health`` (open, parked, stale and unjudged review-ready milestone
    counts), ``queue_stale`` and ``queue_sweep`` (a dry-run PROPOSED park/fold/
    close list, writes nothing); explicit recorded writes ``queue_park``
    (task_id, reason, approved_by, fold_children), ``queue_fold`` (task_id,
    into, reason), ``queue_unpark`` (task_id, reason) and ``queue_close_merged``
    (dry_run defaults to true; false closes only unambiguous merged-PR matches
    with the merge SHA as evidence_ref). Parked tasks are listed with
    ``task_list(parked=True)``.

    Mailbox actions (task #1859): ``message_inbox`` (to_agent=None,
    conversation_id=None, act=None, limit=50, since=None, unread_only=True,
    session_id=None, after_seq=None) reads your inbox and marks the returned mail
    read (``after_seq`` = the previous ``next_seq`` reads only newer mail; while
    ``unread`` is above zero, call again with no cursor for older mail; the ULID ``since``
    is retired and raises); ``message_ack`` (message_id=None, through=None,
    to_agent=None, session_id=None, include_pruned=False) acks one message or
    everything up to a cursor (``through`` is a seq integer or a message_id). Pass
    ``session_id`` (task #1860) to act as one of your sessions. See
    ``messaging.send``.

    Handoff grounding (task #2390): ``handoff_ground`` (message_id,
    session_id=None, to_agent=None) checks a handoff you sent or received
    against live state, claim by claim (T0 human or git, T1 CI or PR, T2
    agent-written), in report mode: verdict verified, mismatch or insufficient,
    at most 15 resolver calls, never blocking. A reference that exists is not
    the sender's claim about it: ``authorization`` is ``not_checked`` whenever
    the handoff says an approval was given, since no cited message is read.
    See ``messaging.grounding``.

    Session handoff (task #2397, see ``lifecycle.handoff``):
    ``session_handoff_propose`` (session_id, done, next_steps,
    open_questions=None, task_id=None, supersede=False) writes a draft note for
    a session you own (``done`` and ``next_steps`` need one item each);
    ``session_handoff_update`` (note_id, done=None, next_steps=None,
    open_questions=None) edits the draft; ``session_handoff_seal`` (note_id) is
    the commit point; ``session_handoff_discard`` (note_id) drops a draft;
    ``session_handoff_recover`` (session_id) returns ``{"note": ...}``, the
    latest sealed, assigned or delivered note (never a draft);
    ``session_handoff_ack`` (note_id) is the receiver saying it has a note
    handed to it. Per session: one pending note (pass ``supersede=True`` to
    replace it), a 5-minute cooldown and 5 proposals per hour. The writers act
    as the verified identity (``agent`` only without auth). Failover to
    another agent is hub-side only: it leaves the note ``assigned`` until the
    transport accepts it or the receiver acknowledges.

    Task hold actions (task #1747, see ``task_holds``): ``task_hold``
    (root_task_id, mode="pause", reason="", release_policy="manual",
    actor=None, hold_id=None) with mode pause|cancel|resume|restore and
    release_policy manual|after_active_runs_finish (pause only, and only
    while something in the subtree is running); ``task_holds``
    (root_task_id=None, status="active", limit=200); ``task_hold_get``
    (hold_id). Holding needs ``task_update`` in the active profile; resume
    and restore need the hold's creator or a lifecycle operator. While a hold
    is active its members refuse new claims, dispatch and node dispatch; a
    live renewal is let through. Restore reports a member whose cancelled run
    is still open as ``pending`` and keeps the hold until it has closed.

    Conversation handoff (task #1426): ``conversation_handoff``
    (conversation_id, task_id=None, title=None, project=None, priority="med")
    links an existing task or creates one that references a conversation you
    can see, without copying its messages; ``conversation_handoffs``
    (conversation_id) lists where it went. See ``messaging.handoff``.

    Approval Ledger (task #1854): ``approval_ledger_rerequest`` (number,
    head_sha, independent_review=None) opens the ``github_app.process_pr_event`` approval row for one
    open PR head whose row a poll missed. Operators only; ``head_sha`` must be
    the PR's current head. Optional v1 ``independent_review`` proposes a human
    attestation in that same release decision, replacing only an undecided
    pending proposal. It queues the approval and never resolves it.
    ``approval_ledger_poll`` () (task #1867) runs the scheduled Approval Ledger
    poll once now for every open PR head. Operators only; it requests rows and
    never resolves one.

    Device acceptance (task #2112, see ``node_acceptance``):
    ``node_acceptance_start`` (node) queues a harmless no-model job on a ua-node
    (one item that completes, one the hub cancels mid-run) and records
    pending | accepted | failed with per-check results (launch, progress,
    receipt, cancel_confirmed, revocation); ``node_acceptance_evaluate`` (node)
    re-evaluates and records it; ``node_acceptance_status`` (node=None) reads
    one node's live result, or every node's stored summary. Start and evaluate
    need the node's own agent or a lifecycle operator. With
    ``UA_NODE_REQUIRE_ACCEPTANCE=1`` a node takes no other work until accepted.

    Node retire (task #2181, see ``node_registry``): ``node_retire`` (node,
    reason) marks a registered ua-node row retired (kept, never deleted) and
    records ``kind="retire"`` on the ``agent.node`` ledger channel. A retired
    node is left out of the doctor's fleet rows, the node/fleet listing,
    acceptance summaries and routing, and its owner's re-registration and
    heartbeats are refused (403, ``code="node_retired"``) until
    ``node_unretire`` (node, reason) clears the marker. Refused (409) while the
    node has queued or claimed work. Lifecycle operators
    (``UA_LIFECYCLE_OPERATORS``) only; repeating either action is a no-op.

    Supervised commands (task #2413, see ``node_command``):
    ``node_command_start`` (node, argv, cwd, timeout_s=None, task_id=None,
    session_id=None) runs an argv list (never a shell string) on a ua-node that
    allowlists ``argv[0]`` (``UA_NODE_COMMANDS``), behind the node_dispatch
    approval gate with the argv in the approved payload. The command outlives
    the starting session; when it ends, the caller's label gets one message
    from ``hub-node`` with the exit code, duration and log path.
    ``node_command_status`` (item_id) reads it (owner or starter);
    ``node_command_adopt`` (item_id, session_id=None) rebinds the owner session
    of an unfinished command to another session of the owner label (or by a
    lifecycle operator), never its attempt or fence. A completion from a claim
    that no longer holds the fence is refused (409 ``stale_fence``).

    Work proposals (task #2393, see ``work_proposals``): ``work_proposal_file``
    (title, items, proposer=None, reporting_mode="final_only") files 1-50 items
    {title, notes, scope {repo, files}, priority}; any verified identity may
    file, an agent only as itself. ``work_proposal_decide`` (proposal_id,
    decisions=[{index, approve, edits?}], approver=None) is human operator only
    and spawns one task per approved item with a frozen contract digest that
    ``task_claim`` re-checks; ``work_proposal_withdraw`` (proposal_id,
    actor=None, reason=None) is proposer only, before any decision;
    ``work_proposal_get`` (proposal_id); ``work_proposal_list`` (status=None,
    proposer=None, limit=50); ``work_proposal_recover`` (older_than_s=300)
    resolves items stuck in approving (queue hygiene runs it too).

    Task workflow (task #2398, see ``task_workflow``): ``task_workflow_get``
    (task_id, head_sha=None) returns the task's stage, resume capsule (goal,
    done, focus) and every check graded at a head: ``verified`` (the hub
    resolved its CI run: same head, finished, the stated outcome),
    ``attested`` (an agent's word, or a reference that contradicted it) or
    ``unknown`` (nothing recorded at that head). ``task_workflow_set``
    (task_id, stage=None, goal=None, done=None, focus=None, head_sha=None,
    expected_checks=None, expected_revision=None) and ``task_check_record``
    (task_id, name, outcome pass|fail, head_sha, ref={kind: "ci_run", ref,
    repo, job=None}=None, detail=None, verify=True; with ``job`` the check is
    graded on that named job, without it only on the whole run, shown as
    ``verified_scope``) are for the claimant, next owner,
    a lifecycle operator or the operator; ``task_check_verify`` (task_id,
    name, head_sha) re-resolves a stored check. ``session_packet`` carries
    the record as ``workflow``. Report mode: nothing here gates a claim.

    Design panels (task #2399, see ``design_panels``): ``panel_open``
    (question, seats, context=None, task_id=None, deadline_hours=24) sends
    2-7 seats the same neutral dispatch; a code-review question or one that
    leads with the opener's own answer is refused, and the opener does not sit.
    ``panel_answer`` (panel_id, answer, confidence=None) records a seat's one
    final answer; answers stay sealed (``panel_get`` (panel_id) shows only who
    answered, and the caller its own) until the last seat answers or the
    deadline passes (``panel_reveal`` (panel_id) by the opener, or queue
    hygiene). ``panel_decide`` (panel_id, decision, rationale,
    considered=None) records the decision and its reasons, never a tally, and
    appends it to the panel's task. ``panel_list`` (status=None, seat=None,
    limit=50); ``panel_cancel`` (panel_id, reason).

    Friction rollup (task #2399, see ``friction_rollup``): ``friction_report``
    (key, severity low|med|high, summary, channel="report", task_id=None)
    files one report; queue hygiene also ingests session-end friction nudges.
    ``friction_rollup`` (window_days=14, limit=50, include_closed=False) ranks
    keys by severity, count, channel and reporter diversity, with each key's
    verdict. ``friction_claim_fixed`` (key, evidence_ref, note=None,
    recheck_days=3) moves a key to fix_claimed; it closes only when
    ``friction_recheck`` (key=None) or hygiene finds no report after the claim
    once the window passes, and a later report reopens it. ``friction_accept``
    (key, note) closes a key as accepted, human operator only.

    Profile authorization uses the normalized public action. The policy gate
    uses the underlying function name, whose own authorization rules still apply.
    """
    if not isinstance(action, str):
        raise ToolError("action must be a string")
    action = action.strip().lower()
    fn = _COORDINATION_ACTIONS.get(action)
    if fn is None:
        raise ToolError(f"action must be one of {tuple(_COORDINATION_ACTIONS)}")
    if not profiles.consolidated_action_is_allowed("coordination_manage", action):
        raise ToolError(f"coordination action {action!r} is not exposed by the active profile")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ToolError("params must be an object")
    try:
        bound = inspect.signature(fn).bind(**params)
    except TypeError as exc:
        raise ToolError(f"invalid params for coordination action {action!r}: {exc}") from exc
    safety.check_policy_gate(fn.__name__, bound.arguments)
    return fn(*bound.args, **bound.kwargs)
