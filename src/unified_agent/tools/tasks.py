"""Task tracking tools backed by SQLite."""

import base64
import binascii
import hashlib
import inspect
import json
import logging
import re
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.mcpserver.exceptions import ToolError

from .. import (
    agent_lifecycle,
    budgets,
    claim_release,
    completion_return,
    config,
    edition,
    events,
    operating_policy,
    org_registry,
    run_records,
    storage,
    task_holds,
    task_liveness,
    task_notes,
    task_ownership,
    task_search,
    workflow_roles,
)
from ..change_feed import task_changed
from ..sync import schedule_sync_to_vault
from ..tool_errors import ConflictError, PermissionDeniedError

logger = logging.getLogger(__name__)

_dispatch_completion_run: ContextVar[str | None] = ContextVar(
    "dispatch_completion_run", default=None
)


@contextmanager
def dispatch_completion_guard(run_id: str):
    """Internal scheduler fence, outside the public task_update argument schema."""
    token = _dispatch_completion_run.set(run_id)
    try:
        yield
    finally:
        _dispatch_completion_run.reset(token)


STATUSES = ("open", "doing", "waiting_review", "blocked", "done", "cancelled")
PRIORITIES = ("high", "med", "low")
# Urgency is an explicit notes marker, separate from priority. Unmarked work
# sorts alongside `next`; if multiple valid markers exist, the most urgent
# wins so an appended escalation takes effect without rewriting history.
URGENCY_RANK = {"now": 0, "next": 1, "later": 2, None: 1}
# Stable slugs for the initial project-scoped task streams (task #327).
# 'unified-agent' is also the backfill default for every task created before
# project separation existed, so it must stay first/authoritative — it is not
# just "the unified-agent repo's queue", it is "unscoped legacy queue".
PROJECTS = config._KNOWN_PROJECTS
DEFAULT_PROJECT = "unified-agent"
# Portfolio-domain classification (task #373 Tier 1, 2.3, decided 2026-08-11):
# explicit tag at creation, defaulting by project, overridable — inference
# from project name alone breaks once a project spans buckets. Every current
# PROJECTS entry is an engineering/hobby project (task #339's Hobby Builder
# bucket is exactly these four), so each defaults to 'hobby'. Personal OS
# capabilities (commitments, inbox triage, decision log — #2.2/#2.4/#2.5) live
# in the unified-agent project but need domain='personal', so they must pass
# `domain` explicitly at creation; there is no project value for 'personal' or
# 'professional' work, only the domain override.
DOMAINS = ("personal", "professional", "hobby")
DEFAULT_DOMAIN = "hobby"
DEFAULT_LANE = "default"
_PROJECT_DEFAULT_DOMAIN = config._projects.ProjectField("domain")
# 'done' and 'cancelled' are both terminal — transitioning between them must not
# erase completed_at (see the completed_at handling in task_update), and both
# must be excluded from "still claimable"/"still active" queries.
TERMINAL_STATUSES = ("done", "cancelled")
ACTIVE_STATUSES = tuple(status for status in STATUSES if status not in TERMINAL_STATUSES)


def _is_queued_routine_task(conn, task_id: int) -> bool:
    """Queued routine tasks cannot be claimed until the scheduler releases them."""
    return (
        conn.execute(
            "SELECT 1 FROM routine_runs WHERE task_id=? AND status='queued' LIMIT 1",
            (task_id,),
        ).fetchone()
        is not None
    )


# Worst-first derived status for a [project-spec] subtree (task #846).
_ROLLUP_PRIORITY = ("blocked", "doing", "waiting_review", "open")

# Task claims embed 'claimed-by: <agent>' in notes (same convention as
# tui._ASSIGNEE_RE); it is the best-effort agent attribution for task events.
# The label must start alphanumeric so placeholder markup right after the
# colon (e.g. 'claimed-by: [waiting-for: dev-claim]') is "unclaimed", not a
# garbage '[waiting-for:' label.
_CLAIMED_BY_RE = re.compile(r"claimed-by:\s*([A-Za-z0-9][\w.-]*)", re.IGNORECASE)
_REVIEW_WAIT_MARKER = "[waiting-for: review]"
# Shared with ordered review handling so SQL queue projection and Python
# completion guards agree on templates that describe a future handoff.
_FORWARD_LOOKING_REVIEW_QUALIFIERS = task_notes.FORWARD_LOOKING_REVIEW_QUALIFIERS
# `[merged commit: <sha>]` is git_merge_task's own completion record (merge.py);
# a landed merge is a review outcome, so it satisfies the guard like an explicit
# `[review: merged]` would.
_COMPLETION_REVIEW_RE = task_notes.REVIEW_COMPLETION


_next_task_revision = storage.next_revision


# Opt-in marker (a creator adds this to a task's notes, same convention family
# as _REVIEW_WAIT_MARKER) declaring that this task's completion is code-bearing
# and must not be marked `done` on say-so alone. `[code-task]` tasks require
# either an explicit `evidence_ref` (commit SHA / PR URL / CI run link) or an
# existing review-completion marker before `done` is accepted — see
# _evidence_required_error and monitor.py's `unverified_done` anomaly rule,
# which flags anything that slips through anyway.
_CODE_TASK_MARKER = "[code-task]"

# The same marker, matched strictly: a backticked `[code-task]` is a task
# *discussing* the convention, not claiming it. Lived in monitor.py until task
# #467 gave a second consumer (worktree source-repo routing) a reason to share
# one definition; monitor.py imports it from here, as it already does for
# _COMPLETION_REVIEW_RE. The distinction is load-bearing for routing: #467's own
# notes quote the marker in backticks and must not be routed as a code task.
_CODE_TASK_MARKER_RX = re.compile(r"(?<!`)\[code-task\](?!`)")
_URGENCY_MARKER_RX = {
    level: re.compile(rf"(?<!`)\[urgency: {level}\](?!`)", re.IGNORECASE)
    for level in ("now", "next", "later")
}

# Fleet-wide nesting cap (task #830 / parent #819). Root depth is 0; a child of
# a depth-8 task is refused. Eight covers project root → steps → substeps with
# headroom; raise later if a real tree needs more. Existing one-level parents
# stay well inside this bound.
TASK_MAX_DEPTH = 8

_PROJECT_SPEC_MARKER = "[project-spec]"
# Same strictness as `_CODE_TASK_MARKER_RX`: a backticked `[project-spec]` is a
# task *discussing* the marker, not carrying a spec.
_PROJECT_SPEC_MARKER_RX = re.compile(r"(?<!`)\[project-spec\](?!`)")


# Tenant fence for every nesting, lineage, subtree and rollup lookup (task
# #1810). Same rule as memory's `_TENANT_SQL` (#1762): `IS` so the untenanted
# private hub (tenant None) keeps matching NULL rows. Another tenant's row must
# read exactly like a missing id.
_TENANT_SQL = "org_id IS ?"


def _tenant() -> str | None:
    return edition.tenant_org_id()


def _visible_task(conn, task_id: int, columns: str = "id"):
    """The task row when it exists in the caller's tenant, else None."""
    return conn.execute(
        f"SELECT {columns} FROM tasks WHERE id=? AND {_TENANT_SQL}", (task_id, _tenant())
    ).fetchone()


class ProjectBuildResult(TypedDict):
    project_id: int
    parent_task_id: int
    identity: str
    event_ulid: str


def _ancestor_ids(conn, task_id: int) -> list[int]:
    """Return ancestor ids root-first, excluding `task_id`.

    Walks `parent_id` pointers. A cycle in already-stored data fails closed
    rather than looping; writers below prevent that shape.

    The walk stays inside the caller's tenant (task #1810): a pointer to a row
    of another tenant ends the lineage there, as if that row were a root, so
    neither its id nor anything above it is read. Only a pointer to a row that
    does not exist at all is malformed.
    """
    lineage: list[int] = []
    seen: set[int] = set()
    current = task_id
    tenant = _tenant()
    while True:
        if current in seen:
            raise ToolError(f"task {task_id} has a parent cycle")
        seen.add(current)
        row = conn.execute(
            f"SELECT parent_id FROM tasks WHERE id=? AND {_TENANT_SQL}", (current, tenant)
        ).fetchone()
        if row is None:
            if conn.execute("SELECT 1 FROM tasks WHERE id=?", (current,)).fetchone() is None:
                raise ToolError(f"task {task_id} has malformed lineage: missing task {current}")
            # Another tenant's row (the last id appended): drop it and stop.
            if lineage:
                lineage.pop()
            break
        if row["parent_id"] is None:
            break
        parent_id = int(row["parent_id"])
        lineage.append(parent_id)
        current = parent_id
    lineage.reverse()
    return lineage


def _assert_no_cancelled_ancestor(conn, task_id: int, *, new_parent_id: int | None = None) -> None:
    """Refuse a task with a cancelled ancestor.

    `new_parent_id` reads the lineage a reparent on the same call leaves
    behind: 0 detaches (no ancestors), another id puts that task and its
    ancestors above `task_id`. A new parent that is not visible is left to
    the nesting check, which refuses it with its own message.
    """
    if new_parent_id is None:
        lineage = _ancestor_ids(conn, task_id)
    elif new_parent_id == 0 or _visible_task(conn, new_parent_id) is None:
        lineage = []
    else:
        lineage = [*_ancestor_ids(conn, new_parent_id), new_parent_id]
    for ancestor_id in lineage:
        row = _visible_task(conn, ancestor_id, "status")
        if row is None:
            raise ToolError(f"task {task_id} has malformed lineage: missing ancestor {ancestor_id}")
        if row["status"] == "cancelled":
            raise ToolError(f"task {task_id} has cancelled ancestor {ancestor_id}")


def _assert_claimable(
    conn, task_id: int, task_lane: str, *, new_parent_id: int | None = None
) -> None:
    """Refusals shared by every new claim (task #1894).

    Both `task_claim` and `task_update` moving a task into `doing` start work
    on the row, so both refuse a task under a cancelled ancestor and a task in
    an unclaimable lane (`tracking` is an umbrella/index, never a unit of
    work). `task_lane` and `new_parent_id` describe the row the call leaves
    behind.
    """
    _assert_no_cancelled_ancestor(conn, task_id, new_parent_id=new_parent_id)
    _assert_claimable_lane(task_id, task_lane)


def _assert_claimable_lane(task_id: int, task_lane: str) -> None:
    """Refuse `task_lane` as the lane of work in 'doing' (tasks #1894, #2002)."""
    task_lane = config.canonical_task_lane(task_lane)
    if task_lane in config.TASK_UNCLAIMABLE_LANES:
        raise ToolError(
            f"task {task_id} is in lane {task_lane!r}, an umbrella that is never "
            "claimable work; move it to a work lane first"
        )


def _subtree_height(conn, task_id: int) -> int:
    """Longest downward path from `task_id` to a leaf; 0 when `task_id` is a leaf."""
    children = conn.execute(
        f"SELECT id FROM tasks WHERE parent_id=? AND {_TENANT_SQL}", (task_id, _tenant())
    ).fetchall()
    if not children:
        return 0
    return 1 + max(_subtree_height(conn, row["id"]) for row in children)


def _assert_nesting(conn, parent_id: int, *, moving_id: int | None = None) -> None:
    """Fail closed on a missing parent, a cycle, or a depth-cap breach.

    `moving_id` is the task being reparented (None when creating a new child).
    Both ids are fenced to the caller's tenant (task #1810): another tenant's
    task fails with the same text as an id that does not exist.
    """
    if moving_id is not None and _visible_task(conn, moving_id) is None:
        raise ToolError(f"no task with id {moving_id}")
    if moving_id is not None and parent_id == moving_id:
        raise ToolError("a task cannot be its own parent")
    if _visible_task(conn, parent_id) is None:
        raise ToolError(f"no task with id {parent_id} to use as parent")
    parent_lineage = _ancestor_ids(conn, parent_id)
    if moving_id is not None and moving_id in parent_lineage:
        raise ToolError(f"reparenting task {moving_id} under {parent_id} would create a cycle")
    parent_depth = len(parent_lineage)
    extra = _subtree_height(conn, moving_id) if moving_id is not None else 0
    new_max_depth = parent_depth + 1 + extra
    if new_max_depth > TASK_MAX_DEPTH:
        raise ToolError(
            f"nesting depth cap is {TASK_MAX_DEPTH} "
            f"(task {parent_id} is at depth {parent_depth}"
            + (f", subtree height {extra}" if extra else "")
            + ")"
        )


def _attach_lineage(conn, rows: list[dict]) -> list[dict]:
    """Add root-first ancestor ids on each listed task (task #830)."""
    cache: dict[int, list[int]] = {}
    for row in rows:
        tid = int(row["id"])
        if tid not in cache:
            cache[tid] = _ancestor_ids(conn, tid)
        row["lineage"] = list(cache[tid])
    return rows


def _subtree_ids(conn, root_id: int) -> list[int]:
    """`root_id` plus every descendant in the caller's tenant, via a recursive CTE.

    Empty when `root_id` is missing or belongs to another tenant (task #1810).
    """
    tenant = _tenant()
    rows = conn.execute(
        f"""
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM tasks WHERE id=? AND {_TENANT_SQL}
            UNION ALL
            SELECT t.id FROM tasks t JOIN subtree s ON t.parent_id = s.id
            WHERE t.{_TENANT_SQL}
        )
        SELECT id FROM subtree
        """,
        (root_id, tenant, tenant),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def _is_project_spec(notes: str | None) -> bool:
    return bool(_PROJECT_SPEC_MARKER_RX.search(notes or ""))


def _descendant_status_rows(conn, root_id: int) -> list:
    """Every descendant of `root_id` (not the root) in the caller's tenant."""
    tenant = _tenant()
    return conn.execute(
        f"""
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM tasks WHERE parent_id=? AND {_TENANT_SQL}
            UNION ALL
            SELECT t.id FROM tasks t JOIN subtree s ON t.parent_id = s.id
            WHERE t.{_TENANT_SQL}
        )
        SELECT tasks.id, tasks.title, tasks.status
        FROM tasks JOIN subtree ON subtree.id = tasks.id
        ORDER BY tasks.id
        """,
        (root_id, tenant, tenant),
    ).fetchall()


def _project_rollup(conn, root_id: int) -> dict:
    """Derived [project-spec] status from the entire descendant subtree (task #846)."""
    rows = _descendant_status_rows(conn, root_id)
    counts = {status: 0 for status in STATUSES}
    active: list[dict] = []
    for row in rows:
        status = row["status"]
        counts[status] = counts.get(status, 0) + 1
        if status in ACTIVE_STATUSES:
            active.append({"id": int(row["id"]), "status": status, "title": row["title"]})
    derived = "done"
    for candidate in _ROLLUP_PRIORITY:
        if counts.get(candidate, 0):
            derived = candidate
            break
    return {
        "complete": not active,
        "derived_status": derived,
        "active": active,
        "counts": counts,
    }


def _project_view(conn, root_id: int) -> dict:
    """Evidence vs merge-verified counts for a [project-spec] root (task #847).

    Matches ``project_chain`` counting: only *done* descendants, and a merge
    marker verifies only the PR URL captured in that descendant's notes.
    ``notes`` is read as ``proof_notes`` so compact listings never SELECT a
    bare ``notes`` column (task #164).
    """
    from ..evidence_claims import _PR_URL_RE, _SHA_RE, _merge_claim

    tenant = _tenant()
    rows = conn.execute(
        f"""
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM tasks WHERE parent_id=? AND {_TENANT_SQL}
            UNION ALL
            SELECT t.id FROM tasks t JOIN subtree s ON t.parent_id = s.id
            WHERE t.{_TENANT_SQL}
        )
        SELECT tasks.id, tasks.status, tasks.evidence_ref,
               COALESCE(tasks.notes, '') AS proof_notes
        FROM tasks JOIN subtree ON subtree.id = tasks.id
        """,
        (root_id, tenant, tenant),
    ).fetchall()
    evidence_count = 0
    merge_verified_count = 0
    for row in rows:
        if row["status"] != "done":
            continue
        evidence_ref = row["evidence_ref"]
        notes = row["proof_notes"] or ""
        pr_url = None
        sha = None
        if evidence_ref:
            found_pr = _PR_URL_RE.search(evidence_ref)
            if found_pr:
                pr_url = found_pr.group(0)
            found_sha = _SHA_RE.search(evidence_ref)
            if found_sha:
                sha = found_sha.group(0)
        if pr_url is None:
            found_pr = _PR_URL_RE.search(notes)
            if found_pr:
                pr_url = found_pr.group(0)
        if sha is None:
            found_sha = re.search(r"\b(?:SHA|commit):\s*([0-9a-f]{7,40})\b", notes, re.IGNORECASE)
            if found_sha:
                sha = found_sha.group(1)
        if not (evidence_ref or pr_url or sha):
            continue
        evidence_count += 1
        if pr_url and _merge_claim(notes, pr_url)["verified"]:
            merge_verified_count += 1
    return {
        "evidence_count": evidence_count,
        "merge_verified_count": merge_verified_count,
        "evidence_cli": f"unified-agent evidence report --project {root_id}",
    }


def _attach_project_rollup(conn, rows: list[dict]) -> list[dict]:
    """Attach `rollup` on listed [project-spec] roots only.

    Compact listings must not SELECT the notes body (task #164). When notes
    are already on the row, reuse `_PROJECT_SPEC_MARKER_RX`. Otherwise detect
    the marker in SQL via instr/REPLACE so backticked mentions stay excluded.
    """
    if not rows:
        return rows
    spec_ids: set[int] = set()
    missing: list[int] = []
    for row in rows:
        tid = int(row["id"])
        if "notes" in row:
            if _is_project_spec(row.get("notes")):
                spec_ids.add(tid)
        else:
            missing.append(tid)
    if missing:
        placeholders = ",".join("?" * len(missing))
        found = conn.execute(
            f"SELECT id FROM tasks WHERE id IN ({placeholders}) "
            "AND instr(REPLACE(COALESCE(notes, ''), '`[project-spec]`', ''), "
            "'[project-spec]') > 0",
            missing,
        ).fetchall()
        spec_ids.update(int(r["id"]) for r in found)
    if spec_ids:
        # Roll up only the caller's own roots (task #1810).
        ordered = sorted(spec_ids)
        placeholders = ",".join("?" * len(ordered))
        visible = conn.execute(
            f"SELECT id FROM tasks WHERE id IN ({placeholders}) AND {_TENANT_SQL}",
            [*ordered, _tenant()],
        ).fetchall()
        spec_ids = {int(r["id"]) for r in visible}
    for row in rows:
        tid = int(row["id"])
        if tid in spec_ids:
            row["rollup"] = _project_rollup(conn, tid)
            row["project_view"] = _project_view(conn, tid)
    return rows


def _project_completion_error(conn, task_id: int, notes: str | None) -> str | None:
    """Refuse completing a [project-spec] root while any descendant is active."""
    if not _is_project_spec(notes):
        return None
    rollup = _project_rollup(conn, task_id)
    if rollup["complete"]:
        return None
    listed = ", ".join(f"#{item['id']} ({item['status']})" for item in rollup["active"])
    return f"cannot mark project-spec task {task_id} done while active descendants exist: {listed}"


def _row(row) -> dict:
    d = {k: row[k] for k in row.keys()}
    if "lane" in d:
        d["lane"] = config.canonical_task_lane(d["lane"])
    if "urgency" not in d:
        notes = d.get("notes") or ""
        d["urgency"] = next(
            (level for level, pattern in _URGENCY_MARKER_RX.items() if pattern.search(notes)),
            None,
        )
    d["urgency_rank"] = URGENCY_RANK[d["urgency"]]
    return d


def _claimed_by(notes: str | None) -> str | None:
    """Best-effort agent label from a task's notes, or None if not claimed."""
    m = _CLAIMED_BY_RE.search(notes or "")
    return m.group(1) if m else None


def _validate_lane(lane: str) -> str:
    lane = config.canonical_task_lane(lane)
    if lane in config.TASK_LEGACY_LANES:
        # #883: these were lanes *and* domains; now they are domains only. In
        # open-roster mode is_valid_task_lane() would wave them through as a
        # custom agent label, so reject them explicitly with the fix.
        raise ToolError(
            f"lane {lane!r} is a portfolio bucket, not an actor lane — pass it as "
            f"`domain={lane!r}` and set `lane` to one of {config.TASK_SYSTEM_LANES}"
        )
    if not config.is_valid_task_lane(lane):
        roster = config.agent_labels()
        if roster:
            raise ToolError(f"lane must be one of {config.task_lane_values()}")
        raise ToolError(
            f"lane must be one of {config.TASK_SYSTEM_LANES} or a well-formed agent label"
        )
    return lane


def _resolve_lane_for_add(lane: str | None, parent_lane: str | None = None) -> str:
    """Pick the stored lane for a new task (explicit lane wins, else parent).

    `lane` is the actor lane (#883) — who acts on the task — so the portfolio
    bucket on `domain` never leaks into it. A new task with no explicit lane and
    no parent lands in the untriaged 'default' pool.
    """
    if lane is not None:
        return _validate_lane(lane)
    if parent_lane is not None:
        return _validate_lane(parent_lane)
    return DEFAULT_LANE


def _lane_claim_allowed(
    task_lane: str, expected_lane: str | None, agent: str | None = None
) -> bool:
    """Return whether `expected_lane` may claim a task in `task_lane`.

    Unclaimable lanes (`tracking`) are refused before this, by
    `_assert_claimable`, so this only matches the claim scope (task #2002).
    """
    task_lane = config.canonical_task_lane(task_lane)
    if expected_lane is None:
        if task_lane in config.TASK_OPEN_POOL_LANES:
            return True
        if agent and task_lane == agent:
            return True
        return False
    expected_lane = _validate_lane(expected_lane)
    if task_lane in config.TASK_OPEN_POOL_LANES:
        return True
    return task_lane == expected_lane


def _count_agent_wip(conn, agent: str, now_ts: str, exclude_id: int | None = None) -> int:
    """Active `doing` tasks held by `agent`, excluding expired locks.

    Claimed rows count while their lease is live. An unclaimed `doing` row
    (reached through `task_update`, task #1886) counts for the agent its open
    self-pickup run is attributed to (`current_run_agent`, written in the same
    transaction as the move) while it is inside its implicit lease. A row with
    no recorded run agent is attributed to nobody and binds only the fleet cap.
    """
    params: list = [
        agent,
        now_ts,
        agent,
        run_records.unclaimed_doing_cutoff(now_ts),
    ]
    sql = (
        "SELECT COUNT(*) AS n FROM tasks WHERE status='doing' AND ("
        "(claimed_by=? AND (lock_expires_at IS NULL OR lock_expires_at > ?)) "
        f"OR (current_run_agent=? AND {run_records.UNCLAIMED_LIVE_DOING_SQL}))"
    )
    if exclude_id is not None:
        sql += " AND id!=?"
        params.append(exclude_id)
    return int(conn.execute(sql, params).fetchone()["n"])


def _count_lane_wip(conn, lane: str, now_ts: str, exclude_id: int | None = None) -> int:
    lanes = (
        ("operator-only", config.LEGACY_OPERATOR_LANE)
        if config.canonical_task_lane(lane) == "operator-only"
        else (lane,)
    )
    # Claimed rows count while their lease is live; an unclaimed row (no lease)
    # counts inside its implicit lease, the same rule as the fleet count
    # (task #1886). It used to count forever through lock_expires_at IS NULL.
    params: list = [*lanes, now_ts, run_records.unclaimed_doing_cutoff(now_ts)]
    sql = (
        f"SELECT COUNT(*) AS n FROM tasks WHERE lane IN ({','.join('?' for _ in lanes)}) "
        "AND status='doing' AND ("
        "(claimed_by IS NOT NULL AND (lock_expires_at IS NULL OR lock_expires_at > ?)) "
        f"OR ({run_records.UNCLAIMED_LIVE_DOING_SQL}))"
    )
    if exclude_id is not None:
        sql += " AND id!=?"
        params.append(exclude_id)
    return int(conn.execute(sql, params).fetchone()["n"])


def _enforce_wip_caps(
    conn, agent: str | None, task_lane: str, now_ts: str, exclude_id: int | None
) -> list[str]:
    """Raise on agent or hard lane caps; return soft lane warnings otherwise.

    With no known agent (an unverified ``task_update`` caller, task #1886)
    only the lane caps apply.
    """
    warnings: list[str] = []
    if agent:
        agent_cap = config.agent_wip_max_for(agent)
        agent_wip = _count_agent_wip(conn, agent, now_ts, exclude_id)
        if agent_wip >= agent_cap:
            raise ToolError(
                f"WIP cap: {agent!r} already holds {agent_wip} doing task(s) (max {agent_cap})"
            )
    task_lane = config.canonical_task_lane(task_lane)
    lane_limits = config.lane_wip_limits()
    if task_lane in lane_limits:
        cap = int(lane_limits[task_lane]["wip_max"])
        lane_wip = _count_lane_wip(conn, task_lane, now_ts, exclude_id)
        if lane_wip >= cap:
            msg = f"lane {task_lane!r} already has {lane_wip} doing task(s) (max {cap})"
            if str(lane_limits[task_lane]["mode"]) == "hard":
                raise ToolError(f"WIP cap: {msg} (mode hard)")
            warnings.append(msg)
    return warnings


def _enforce_fleet_wip(
    conn, task_lane: str, priority: str | None, notes: str | None, now_ts: str, exclude_id: int
) -> list[str]:
    """Fleet-wide WIP on a new claim or move into ``doing`` (#1838, #1886): soft, then hard.

    The count is ``run_records.live_wip_counts``: claimed rows with a live
    lease plus unclaimed ``doing`` rows inside their implicit lease.
    At or past ``UA_FLEET_WIP_MAX`` live claims fleet-wide the claim succeeds
    with a warning; at ``UA_FLEET_WIP_HARD_MAX`` it is refused. High-priority
    and ``[urgency: now]`` work is warned, never refused, and the exempt lanes
    (operator-only, interactive-only) keep their claim semantics unchanged.
    A task the dispatcher already holds a live slot for is not checked: the
    launch gate admitted it, and its spawned child's claim must not be refused
    after the approval was consumed. Its claim still counts for others.
    """
    if config.canonical_task_lane(task_lane) in config.FLEET_WIP_EXEMPT_LANES:
        return []
    if run_records.has_live_dispatcher_slot(conn, exclude_id):
        return []
    soft, hard = config.fleet_wip_max(), config.fleet_wip_hard_max()
    claimed, unclaimed = run_records.live_wip_counts(conn, now_ts, exclude_id)
    live = claimed + unclaimed
    if live < soft:
        return []
    detail = f", {unclaimed} with no claimant" if unclaimed else ""
    msg = f"fleet already has {live} live doing task(s){detail} (soft cap {soft}, hard cap {hard})"
    if live < hard:
        return [msg]
    urgent = priority == "high" or bool(_URGENCY_MARKER_RX["now"].search(notes or ""))
    if not urgent:
        raise ToolError(
            f"WIP cap: {msg}; finish or release a claim first "
            "(high-priority or [urgency: now] work is never refused)"
        )
    return [f"{msg}; admitted past the hard cap because the task is urgent"]


def _slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", s)[:30].strip("-")


def _completion_review_error(notes: str | None) -> str | None:
    """Return a completion error when review work is still explicitly open."""
    if task_notes.latest_review_request_end(notes) is None:
        return None
    if task_notes.current_review_is_complete(notes):
        return None
    return (
        "task is waiting for review; record a merged/approved review outcome "
        "or explicit [review: no-merge] before marking it done"
    )


def _is_project_worktree_path(dest: Path, project_repo: Path) -> bool:
    home = (project_repo.resolve() / ".worktrees").resolve()
    return dest != home and dest.is_relative_to(home)


def _git_worktree_add_from_known_project_repo(dest: Path, branch: str, project_repo: Path) -> None:
    """Create a worktree at `dest`, sourced from `project_repo`'s git history.

    Task #371: deliberately bypasses tools.git's sandboxed git_worktree_add /
    resolve_in_workspace. `project_repo` is never agent- or caller-supplied —
    it is always one of the small, fixed, existence-checked set returned by
    config.project_repo_path()/project_repo_roots(), resolved from a task's
    own `project` column. Widening the general-purpose git_* MCP tools
    (callable by any agent with an arbitrary repo_path string) to reach
    ~/dev/<project> was explicitly considered and rejected in favor of this
    narrow, internal-only path — the agent-facing tools' reach is unchanged.

    `dest` is required to be a task worktree under that project's own
    `.worktrees/` directory.
    """
    from .git import BRANCH_NAME_REGEX, GitOperationError

    if not BRANCH_NAME_REGEX.match(branch):
        raise ToolError(f"invalid branch name {branch!r}")
    dest = dest.resolve()
    if not _is_project_worktree_path(dest, project_repo):
        raise ToolError(f"worktree destination {dest} is not under {project_repo}/.worktrees")
    dest.parent.mkdir(parents=True, exist_ok=True)

    exists = (
        subprocess.run(
            ["git", "-C", str(project_repo), "show-ref", "--verify", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            timeout=30,
        ).returncode
        == 0
    )
    args = ["git", "-C", str(project_repo), "worktree", "add", str(dest)]
    args += [branch] if exists else ["-b", branch]
    res = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise GitOperationError(f"git worktree add failed: {res.stderr.strip()}")


def _git_worktree_remove_from_known_project_repo(
    dest: Path, project_repo: Path, force: bool = False
) -> None:
    """Remove a worktree at `dest` that was registered against `project_repo`.

    Mirror image of `_git_worktree_add_from_known_project_repo` (task #467 /
    PR #203 review): a project-sourced worktree's git metadata lives in
    `project_repo`, not the workspace repo, so the sandboxed
    `git.git_worktree_remove` — which always operates against the workspace
    repo by default — refuses to remove it. That left `task_update(status=
    "done")` and `task_delete` reporting `remove_failed` and abandoning both
    the checkout and its branch. Same boundary as the add-side helper:
    `project_repo` is never agent- or caller-supplied, only ever one of
    `config.project_repo_roots()`.
    """
    dest = dest.resolve()
    if not _is_project_worktree_path(dest, project_repo):
        raise ToolError(f"worktree path {dest} is not under {project_repo}/.worktrees")
    args = ["git", "-C", str(project_repo), "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(dest))
    res = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise ToolError(f"git worktree remove failed: {res.stderr.strip()}")


def _evidence_required_error(notes: str | None, evidence_ref: str | None) -> str | None:
    """Return a completion error when a [code-task]-marked task has no evidence.

    Mirrors _completion_review_error's shape: opt-in via marker, satisfied by
    either a fresh evidence_ref on this same update or a pre-existing
    completion-review marker already in notes (a merged PR is itself
    evidence, so a task that already cleared the review gate doesn't also
    need a redundant evidence_ref).
    """
    body = notes or ""
    if _CODE_TASK_MARKER.lower() not in body.lower():
        return None
    if evidence_ref and evidence_ref.strip():
        return None
    if _COMPLETION_REVIEW_RE.search(body):
        return None
    return (
        "task is marked [code-task]; supply evidence_ref (commit SHA, PR URL, "
        "or CI run link) or a [review: merged/...] marker before marking it done"
    )


def _worktree_source_repo(project: str | None) -> Path | None:
    """The product checkout a task's worktree is cut from, or None when none resolves.

    Task #2041: the task's project alone picks the repo. Task #467 also sent
    unmarked DEFAULT_PROJECT rows to the workspace, which is the cockpit, so a
    batch claim cut product branches from cockpit master and stored relative
    paths. ``[code-task]`` still drives completion evidence
    (``_evidence_required_error``) and review lookup exclusivity
    (``merge._task_repo_routing``), but never which repo is used. None means
    skip provisioning; UA_WORKSPACE is never a fallback source.
    """
    return config.project_repo_path(project)


def _historical_delivery_notes(notes: str | None, evidence_ref: str | None, execution: str) -> str:
    """Retain predecessor provenance without projecting it as current delivery."""
    body = re.sub(
        r"\[waiting-for:\s*review\](?! when done| once shipped)",
        "[previous-run waiting-for-review]",
        notes or "",
        flags=re.IGNORECASE,
    )
    body = re.sub(r"\[(review:|merged commit:)", r"[previous-run \1", body, flags=re.IGNORECASE)
    if evidence_ref:
        body += f"\nPrevious execution {execution} evidence: {evidence_ref}"
    return body


def _worktree_run_suffix(
    run_id: str, generation: int | None = None, delegation_id: int | None = None
) -> str:
    suffix = "-run-" + hashlib.sha256(run_id.encode()).hexdigest()[:16]
    if delegation_id is not None:
        suffix += f"-delegation-{delegation_id}"
    return suffix + (f"-generation-{generation}" if generation is not None else "")


def _assert_worktree_authority(
    conn,
    task_id: int,
    run_id: str | None,
    agent: str | None,
    delegation_id: int | None = None,
    generation: int | None = None,
    *,
    require_in_flight: bool = True,
    allow_cancelled: bool = False,
):
    row = _visible_task(conn, task_id, "*")
    if (
        row is None
        or row["current_run_id"] != run_id
        or (run_id is not None and row["current_run_agent"] != agent)
        or (
            row["claimed_by"] != agent
            and not (
                row["claimed_by"] is None
                and run_id is not None
                and row["current_run_agent"] == agent
                and delegation_id is None
            )
        )
        or row["status"] != "doing"
        or (row["cancel_requested"] and not allow_cancelled)
    ):
        raise ToolError(f"task {task_id} worktree provisioning run_not_current")
    if delegation_id is not None:
        state_guard = (
            "status='in_flight'"
            if require_in_flight
            else (
                "status IN ('pending','in_flight','completed','failed','cancelled')"
                if allow_cancelled
                else "status IN ('pending','in_flight','completed','failed')"
            )
        )
        delegation = conn.execute(
            "SELECT 1 FROM delegations WHERE id=? AND task_id=? AND to_agent=? "
            "AND claim_generation=? AND " + state_guard,
            (delegation_id, task_id, agent, generation),
        ).fetchone()
        if delegation is None:
            raise ToolError(f"task {task_id} worktree provisioning delegation_not_current")
    return row


def _task_worktree_source_sha(task: dict) -> str | None:
    """Exact attached checkout provenance, never a guessed local HEAD base."""
    raw_path, branch = task.get("worktree_path"), task.get("branch")
    if not raw_path or not branch:
        return None
    candidate = Path(raw_path)
    # A relative path is a pre-#2041 row cut from the workspace.
    repo = _worktree_source_repo(task.get("project")) if candidate.is_absolute() else None
    repo = repo or config.workspace_root()
    dest = (candidate if candidate.is_absolute() else config.workspace_root() / candidate).resolve()
    if not _is_project_worktree_path(dest, repo):
        return None
    try:

        def read(args, cwd):
            result = subprocess.run(
                ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=30
            )
            if result.returncode:
                raise ValueError("checkout provenance unavailable")
            return result.stdout.strip()

        if read(["symbolic-ref", "--quiet", "--short", "HEAD"], dest) != branch:
            return None
        common = read(["rev-parse", "--path-format=absolute", "--git-common-dir"], repo)
        checkout_common = read(["rev-parse", "--path-format=absolute", "--git-common-dir"], dest)
        if Path(common).resolve() != Path(checkout_common).resolve():
            return None
        sha = read(["rev-parse", "--verify", "HEAD^{commit}"], dest)
        return sha if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _ensure_task_worktree(
    task_id: int,
    title: str,
    conn=None,
    project: str | None = None,
    notes: str | None = None,
    *,
    expected_run_id: str | None = None,
    expected_agent: str | None = None,
    isolate_run: bool = False,
    expected_delegation_id: int | None = None,
    expected_claim_generation: int | None = None,
) -> tuple[str | None, str | None]:
    """Create source in the known repo, then CAS-bind it to its captured owner.

    OS failures remain best effort for non-git workspaces. A superseded run
    raises instead of returning a successor's checkout. Old checkouts and
    branches are always preserved; run/generation suffixes never reuse them.
    """
    from .git import GitOperationError

    slug = _slugify(title) or "task"
    branch = f"task/{task_id}-{slug}"
    wt_rel_path = f".worktrees/task-{task_id}"
    repo = _worktree_source_repo(project)
    if repo is None:
        # Task #2041: no product checkout here; never cut one from the cockpit.
        logger.info("no worktree for task #%s: project %r has no checkout here", task_id, project)
        if conn is not None:
            _assert_worktree_authority(
                conn,
                task_id,
                expected_run_id,
                expected_agent,
                expected_delegation_id,
                expected_claim_generation,
            )
        return None, None
    # A retry cannot adopt a pre-existing legacy checkout (including one that
    # was created before a failed CAS). New run/attempt isolation is mandatory.
    if expected_run_id is not None and (
        isolate_run or expected_delegation_id is not None or (repo / wt_rel_path).exists()
    ):
        suffix = _worktree_run_suffix(
            expected_run_id, expected_claim_generation, expected_delegation_id
        )
        branch += suffix
        wt_rel_path += suffix
    wt_path = str((repo / wt_rel_path).resolve())
    if conn is not None:
        _assert_worktree_authority(
            conn,
            task_id,
            expected_run_id,
            expected_agent,
            expected_delegation_id,
            expected_claim_generation,
        )
        if expected_run_id is not None:
            originator = run_records.run_originator(conn, task_id, expected_agent)
            native_status = run_records.native_execution_status(expected_agent, originator)
            if not native_status["native_execution_allowed"]:
                raise ToolError(native_status["native_execution_denial"])
    try:
        from ..project_bindings import check_worktree_binding

        check_worktree_binding(project, repo)
        _git_worktree_add_from_known_project_repo(repo / wt_rel_path, branch, repo)
    except (GitOperationError, OSError, subprocess.SubprocessError) as exc:
        logger.warning("no worktree for task #%s (branch %r): %s", task_id, branch, exc)
        if conn is not None:
            _assert_worktree_authority(
                conn,
                task_id,
                expected_run_id,
                expected_agent,
                expected_delegation_id,
                expected_claim_generation,
            )
        return None, None
    if conn is not None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            admitted = _assert_worktree_authority(
                conn,
                task_id,
                expected_run_id,
                expected_agent,
                expected_delegation_id,
                expected_claim_generation,
            )
            cur = conn.execute(
                "UPDATE tasks SET worktree_path=?, branch=? WHERE id=? AND current_run_id IS ? "
                "AND claimed_by IS ? AND status='doing' AND cancel_requested=0 AND worktree_path IS NULL",
                (wt_path, branch, task_id, expected_run_id, admitted["claimed_by"]),
            )
            if cur.rowcount != 1:
                raise ToolError(f"task {task_id} worktree provisioning binding changed")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return wt_path, branch


def _compact_row(row) -> dict:
    """Task row without notes, with lightweight metadata needed for queue views.

    Accepts both row shapes: a full row (notes present, measured in Python) and
    the projected row task_list fetches for compact mode, which never selects the
    notes column at all and carries `notes_chars` plus boolean note-marker flags
    instead.
    """
    d = _row(row)
    if "notes" in row.keys():
        notes = d.pop("notes", None) or ""
        d["notes_chars"] = len(notes)
    else:
        d["notes_chars"] = d.pop("notes_chars", None) or 0
    for flag in ("waiting_for_review", "laptop_lane", "notes_archived"):
        if flag in d and type(d[flag]) is int:
            d[flag] = bool(d[flag])
    # `claimed_by` is authoritative and comes from the projected database
    # column; note markers are descriptive queue metadata only.
    return d


def _review_marker_live_sql(notes_expr: str = "lower(notes)") -> str:
    """SQL boolean expression: the review marker is present and reads as live.

    "Live" means present (`_REVIEW_WAIT_MARKER` in the notes) and not
    immediately followed by one of `_FORWARD_LOOKING_REVIEW_QUALIFIERS`
    (e.g. "... once shipped"), which describes a future condition rather than
    a current state. Built from those two module constants rather than
    hand-written per-phrase SQL so a new qualifier is a one-line tuple edit,
    not a SQL change.
    """
    marker = _REVIEW_WAIT_MARKER.lower()
    # Strip every forward-looking occurrence first, then ask whether any
    # marker survives. Checking the qualifier at instr()'s offset instead
    # would only ever inspect the FIRST marker, so a task carrying
    # "[waiting-for: review] once shipped" that later has a genuine bare
    # marker appended would read as not-live -- a false negative that hides
    # real review work, which is worse than the false positive this fixes.
    stripped = notes_expr
    for qualifier in _FORWARD_LOOKING_REVIEW_QUALIFIERS:
        phrase = (marker + qualifier).lower()
        stripped = f"replace({stripped}, '{phrase}', '')"
    return f"(instr({stripped}, '{marker}') > 0)"


def _compact_select_columns(conn) -> str:
    """Column list for compact mode: every task column except `notes`.

    Read from the schema rather than hardcoded so a column added later shows up
    in compact listings automatically instead of silently going missing.

    Note-marker flags are projections only; ownership remains the typed
    `claimed_by` column.

    `waiting_for_review` is additionally gated on `status` (a `done` or
    `cancelled` task is never awaiting review — the marker is appended once
    and never retracted, so without this gate a task that shipped and was
    later closed keeps reporting waiting_for_review=true forever) and
    excludes forward-looking marker text via `_review_marker_live_sql` (a
    task whose notes only say the marker will apply *once shipped* isn't
    awaiting review yet either, #1038-1040).
    """
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    projected = ", ".join(c for c in cols if c != "notes")

    def live_urgency(level: str) -> str:
        marker = f"[urgency: {level}]"
        notes = "lower(notes)"
        # Match _URGENCY_MARKER_RX: a backtick touching either side means an
        # example, while a separate unquoted marker remains active.
        unquoted = f"replace(replace({notes}, '`{marker}', ''), '{marker}`', '')"
        return f"instr({unquoted}, '{marker}') > 0"

    return (
        f"{projected}, LENGTH(notes) AS notes_chars, "
        f"CASE WHEN {live_urgency('now')} THEN 'now' "
        f"WHEN {live_urgency('next')} THEN 'next' "
        f"WHEN {live_urgency('later')} THEN 'later' "
        "ELSE NULL END AS urgency, "
        f"({_review_marker_live_sql()} "
        "AND status NOT IN ('done', 'cancelled')) AS waiting_for_review, "
        "(lane='laptop' OR instr(lower(notes), 'laptop-lane') > 0 "
        "OR instr(lower(notes), 'laptop-main') > 0 "
        "OR instr(lower(notes), '[waiting-for: laptop-dev-claim]') > 0) AS laptop_lane, "
        "instr(lower(notes), '[archived') > 0 AS notes_archived"
    )


def task_add(
    title: str,
    notes: str | None = None,
    priority: str = "med",
    parent_id: int | None = None,
    untrusted: bool = False,
    project: str | None = None,
    domain: str | None = None,
    due_at: str | None = None,
    evidence_ref: str | None = None,
    idempotency_key: str | None = None,
    lane: str | None = None,
) -> dict:
    """Add a task. `priority` is high|med|low; pass `parent_id` to create a subtask.

    `parent_id` may point at a task that itself has a parent (multi-level
    nesting, task #830). Cycle prevention is implicit on create (the new row
    cannot already be an ancestor). Depth is capped at `TASK_MAX_DEPTH`.

    `project` scopes the task to one of the stable project streams
    (configured project stream ids); omitted defaults to
    'unified-agent'. A subtask with no explicit `project` inherits its
    parent's project rather than the default, so a project's own subtasks
    don't silently leak back into the legacy queue.

    `domain` scopes the task to a portfolio bucket (personal|professional|hobby)
    for Command Center display. A subtask with no explicit `domain` inherits
    its parent's domain, same as project. A top-level task with no explicit
    `domain` defaults by its (resolved) project via `_PROJECT_DEFAULT_DOMAIN` —
    every current project defaults to 'hobby'. Personal OS work (commitments,
    inbox triage, decision log) has no dedicated project value, so it must
    pass `domain='personal'` explicitly.

    `due_at` (ISO date or datetime) is the light commitment/reminder field
    (task #373 Tier 1, 2.2): when this task is due or should be reviewed.
    A commitment is a task with `due_at` set and, by convention, an
    `[owed-to: <person>]` marker in notes (mirrors the existing
    `[waiting-for: X]` follow-up marker vocabulary — COLLABORATION.md Rule 5).
    `task_list(due_before=...)` filters on it. Omitted (the default, most
    tasks) means no due date.

    `evidence_ref` optionally pre-declares a completion pointer (e.g. an
    in-flight PR URL) for a task whose notes carry `[code-task]` — see
    task_update for the enforcement this backs.

    `idempotency_key` (task #429): optional client token for retry-safety;
    retried requests with the same key return the previously committed task
    without creating a duplicate.

    `lane` is the actor lane — who acts on the task (#883):
    `default` (untriaged shared pool), `dispatch-ready` (triaged; review passes
    and the dispatch scheduler work from here), `operator-only` (the operator's
    human step), `interactive-only` (operator-started work, explicit lane claim),
    `tracking` (an umbrella/roadmap index, never claimable), `laptop`, or a
    registered agent label for delegation-targeted work. Omitted lanes inherit
    the parent's lane, else `default`. The portfolio bucket lives on `domain`,
    never on `lane`.

    Returns {id}.
    """
    if not title.strip():
        raise ToolError("title must not be empty")
    if title.strip().startswith("#") or "\n" in title:
        raise ToolError(
            "title looks like a markdown heading or a multi-line body, not a title "
            f"({title.strip()[:60]!r}) — pass a short summary, not the section/spec text"
        )
    if priority not in PRIORITIES:
        raise ToolError(f"priority must be one of {PRIORITIES}")
    if project is not None and project not in PROJECTS:
        raise ToolError(f"project must be one of {PROJECTS}")
    if domain is not None and domain not in DOMAINS:
        raise ToolError(f"domain must be one of {DOMAINS}")

    creator = config.verified_agent_identity()
    creator_untrusted = config.is_agent_untrusted(creator)

    conn = storage.connect()
    try:
        if idempotency_key:
            cached = storage.get_idempotency_record(conn, idempotency_key, "task_add")
            if cached is not None:
                return cached

        parent_untrusted = False
        project_val = project
        domain_val = domain
        parent_lane = None
        if parent_id is not None:
            _assert_nesting(conn, parent_id)
            parent = _visible_task(conn, parent_id, "id, untrusted, project, domain, lane")
            if parent is None:
                raise ToolError(f"no task with id {parent_id} to use as parent")
            if parent["untrusted"]:
                parent_untrusted = True
            if project_val is None:
                project_val = parent["project"]
            if domain_val is None:
                domain_val = parent["domain"]
            parent_lane = parent["lane"] if "lane" in parent.keys() else None
        if project_val is None:
            project_val = DEFAULT_PROJECT
        if domain_val is None:
            domain_val = _PROJECT_DEFAULT_DOMAIN.get(project_val, DEFAULT_DOMAIN)

        lane_val = _resolve_lane_for_add(lane, parent_lane)
        untrusted_val = 1 if (untrusted or creator_untrusted or parent_untrusted) else 0
        ts = storage.now()
        # Stamp the originating machine (audit trail; auto-resolved, not caller-supplied).
        cur = conn.execute(
            "INSERT INTO tasks (title, notes, priority, parent_id, created_at, updated_at, "
            "source_host, untrusted, project, domain, due_at, evidence_ref, lane, org_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title,
                notes,
                priority,
                parent_id,
                ts,
                ts,
                config.write_source_host(),
                untrusted_val,
                project_val,
                domain_val,
                due_at,
                evidence_ref,
                lane_val,
                edition.tenant_org_id(),
            ),
        )
        workflow_roles.on_task_created(conn, cur.lastrowid)
        res = {"id": cur.lastrowid}
        # Completion return (task #1751): a gateway that created this task from
        # an authenticated chat request attached its thread; record it with the
        # task, so a task never exists without the origin it was asked from.
        origin = completion_return.pending_creation_origin()
        if origin is not None:
            completion_return.record_origin_on_connection(conn, cur.lastrowid, origin)
        if idempotency_key:
            storage.record_idempotency(conn, idempotency_key, "task_add", res)
        from .. import gateway

        gateway.commit_registered_call_on_connection(conn, "task_add", res)
        schedule_sync_to_vault()
        # Activity index: best-effort, must never break the task write.
        try:
            storage.record_agent_event(
                "task",
                ref_id=str(cur.lastrowid),
                agent=creator,
                summary=f"created ({priority}): {title}",
            )
        except Exception:
            pass
        task_changed(cur.lastrowid)
        return res
    finally:
        conn.close()


def _append_note_is_duplicate(current_notes: str, note_text: str) -> bool:
    """True only when ``note_text`` is already recorded verbatim in
    ``current_notes`` — a genuine duplicate such as a retried
    ``[slack-event: Ev-1]`` delivery.

    The check compares whole stripped lines, never substrings. The raw
    ``marker in current_notes`` test it replaces also dropped any note that
    was merely a prefix or fragment of a longer recorded line — a shared
    ``[tag]``, a repeated word — while ``updated_at`` still advanced: a
    silent no-op (task #1246).
    """
    existing = [line.strip() for line in current_notes.splitlines()]
    wanted = [line.strip() for line in note_text.splitlines()]
    width = len(wanted)
    return any(existing[i : i + width] == wanted for i in range(len(existing) - width + 1))


#: Approval-queue tool name for a one-shot pass to clear one task's quarantine.
UNTRUSTED_CLEAR_APPROVAL_TOOL = "task_untrusted_clear"
#: Ledger channel recording every cleared quarantine and the authority used.
CHANNEL_UNTRUSTED_CLEARED = "agent.task.untrusted_cleared"


def _task_semantic_digest(arguments: dict) -> str:
    from ..invocation_service import _json_object

    bound = inspect.signature(task_update).bind(**arguments)
    bound.apply_defaults()
    normalized = dict(bound.arguments)
    normalized["actor"] = config.verified_agent_identity()
    return hashlib.sha256(_json_object(normalized).encode()).hexdigest()


def _registered_task_binding() -> dict | None:
    """Bind the actual selected registration, never a request-supplied grant.

    Legacy verified agent credentials use their label as the stable subject;
    WorkOS uses its verified subject. Request-level profile, lifecycle, trust,
    policy and tenant checks still run in the registered wrapper.
    """
    from .. import gateway
    from ..invocation_service import principal_from_request

    arguments = gateway.registered_task_update_arguments()
    actor = config.verified_agent_identity()
    if arguments is None or not actor or not arguments.get("idempotency_key"):
        return None
    bound = inspect.signature(task_update).bind(**arguments)
    bound.apply_defaults()
    normalized = dict(bound.arguments)
    # The writer attributes every authenticated request to verified identity.
    # HTTP supplies that actor explicitly; MCP may omit or claim another label.
    normalized["actor"] = actor
    if config.workos_request_is_bound():
        principal = principal_from_request()
        if principal.organization != _tenant():
            raise ToolError("verified request organization does not match task storage")
        subject = "workos:" + principal.subject
    else:
        subject = "agent:" + actor
    key = normalized["idempotency_key"]
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise ToolError("idempotency key must be 1-200 characters")
    try:
        digest = _task_semantic_digest(arguments)
    except ValueError as exc:
        raise ToolError("task arguments must be strict JSON values") from exc
    scope = [_tenant(), subject, actor, "task_update", 1, key]
    return {
        "org_id": _tenant(),
        "subject": subject,
        "actor": actor,
        "operation": "task_update",
        "version": 1,
        "scope_key": hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest(),
        "digest": digest,
        "task_id": normalized["id"],
    }


@contextmanager
def registered_update_binding():
    """Compose the selected task service's authority ports into approval core."""
    from .. import gateway
    from ..invocation_service import InvocationService
    from . import approvals

    def committed_receipt(conn):
        arguments = gateway.registered_task_update_arguments()
        if arguments is None:
            return None
        return approvals.bound_task_receipt(conn, arguments["idempotency_key"], arguments["id"])

    with (
        approvals.selected_task_binding(
            _registered_task_binding, _task_semantic_digest, committed_receipt
        ),
        gateway.registered_transaction_owner(InvocationService.complete_mutation),
    ):
        yield


def registered_task_replay(arguments: dict) -> dict | None:
    """Read-only current authority/resource/committed receipt preflight."""
    from . import approvals

    if approvals.registered_task_binding() is None:
        return None
    conn = storage.connect()
    try:
        return approvals.bound_task_receipt(
            conn, arguments["idempotency_key"], arguments["id"], allow_legacy=False
        )
    finally:
        conn.close()


def _with_verified_caller(result: dict) -> dict:
    """``result`` plus the caller's own verified label, if any (task #2037).

    A client hook uses it to tell the runs it owns from a surviving claimant's.
    ``task_claim`` keeps no receipt, so every reply carries it; ``task_update``
    adds it to unkeyed replies only (see there).
    """
    caller = config.verified_agent_identity()
    if caller is None:
        return result
    return {**result, "verified_caller": caller}


def acknowledge_registered_task_replay(arguments: dict) -> dict:
    """Recheck the trusted receipt and acknowledge without another write."""
    from .. import gateway
    from . import approvals

    conn = storage.connect()
    try:
        conn.execute("BEGIN")
        cached = approvals.bound_task_receipt(conn, arguments["idempotency_key"], arguments["id"])
        if cached is None:
            raise ToolError("committed task receipt disappeared before replay")
        gateway.commit_registered_call_on_connection(conn, "task_update", cached, replay=True)
        return cached
    finally:
        conn.close()


def _untrusted_clear_receipt(conn, task_id: int, key: str) -> dict | None:
    """Replay a clear without spending authority, within the task's tenant."""
    if _visible_task(conn, task_id) is None:
        raise ToolError(f"no visible task with id {task_id}")
    from . import approvals

    cached = approvals.bound_task_receipt(conn, key, task_id)
    if cached is not None:
        if cached.get("id") != task_id:
            raise ToolError("idempotency key is already registered for a different task")
        if cached.get("org_id") != _tenant():
            raise ToolError(f"no visible task with id {task_id}")
    return cached


def _authorize_untrusted_clear(task_id: int, updater: str | None, requester: str | None):
    """Authority to clear a quarantined task's ``untrusted`` flag (task #1911).

    The executor's Trust Ladder refuses ``untrusted=1`` work, so clearing the
    flag is a trust decision, not bookkeeping. Setting it stays open to
    anyone. Clearing needs a verified human identity, or else a one-shot pass
    from the approval queue that a human approved (consumed here, before the
    write transaction, because the gate commits on its own connection).

    Returns None when the row is not quarantined (nothing to authorize), else
    ``{"authority": "human"|"approval", "approval_id"?}``.
    """
    conn = storage.connect()
    try:
        row = _visible_task(conn, task_id, "untrusted")
    finally:
        conn.close()
    if row is None:
        raise ToolError(f"no visible task with id {task_id}")
    if not row["untrusted"]:
        return None
    if updater and config.actor_kind_for_identity(updater) == "human":
        return {"authority": "human"}
    from .approvals import ApprovalPendingError, check_approval_gate

    try:
        gate = check_approval_gate(
            UNTRUSTED_CLEAR_APPROVAL_TOOL,
            {"task_id": task_id},
            requested_by=updater or requester,
            force=True,
        )
    except ApprovalPendingError as exc:
        raise ApprovalPendingError(
            f"task {task_id} is quarantined (untrusted); clearing that needs a verified "
            f"human identity or a human-approved one-shot pass. {exc}",
            approval_id=exc.approval_id,
        ) from None
    return {"authority": "approval", "approval_id": gate.get("approval_id")}


def _quarantine_clear_scope(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        from . import approvals

        # Join the selected registered writer's not-yet-started approval scope;
        # direct native calls still receive an isolated scope. Both generic
        # and quarantine-clear passes remain owned until the SQLite commit.
        with approvals.approval_invocation(join_existing=True, restore_unstarted=True):
            return fn(*args, **kwargs)

    return guarded


@_quarantine_clear_scope
def task_update(
    id: int,
    status: str | None = None,
    title: str | None = None,
    notes: str | None = None,
    append_note: str | None = None,
    priority: str | None = None,
    parent_id: int | None = None,
    untrusted: bool | None = None,
    archived: bool | None = None,
    worktree: bool | None = None,
    project: str | None = None,
    expected_project: str | None = None,
    expected_updated_at: str | None = None,
    domain: str | None = None,
    due_at: str | None = None,
    clear_due_at: bool = False,
    evidence_ref: str | None = None,
    idempotency_key: str | None = None,
    lane: str | None = None,
    actor: str | None = None,
    release_claim: bool = False,
    run_usage: dict | None = None,
    request_cancel: bool = False,
    expected_run_id: str | None = None,
    expected_delegation_id: int | None = None,
    expected_claim_generation: int | None = None,
    executor_finish: dict | None = None,
    monitor_at: str | None = None,
    monitor_note: str | None = None,
    clear_monitor: bool = False,
    declared_wait: dict | None = None,
    next_owner: str | None = None,
    waiting_on: str | None = None,
    terminal_predicate: str | None = None,
) -> dict:
    """Update a task's status (open|doing|waiting_review|blocked|done|cancelled), title, notes, priority, or parent.

    ``append_note`` atomically appends a non-empty note to the existing task
    notes. Re-delivering a note already recorded verbatim — e.g. a retried
    ``[slack-event: Ev-1]`` line — is treated as a duplicate and does not
    append a second copy; a note that merely shares a marker, prefix, or
    substring with a recorded line is new content and always appends.

    ``executor_finish`` stamps a fenced executor handoff from the current task
    notes and project inside the same transaction. Pass ``{"kind": "delivery"}``
    or ``{"kind": "scout", "report_path": "<vault-relative path>"}``, status
    ``doing`` and all three expected run/delegation/generation
    tokens. It is mutually exclusive with notes/append_note/project updates.

    Setting status to 'done' records completion time and requires an existing
    claimant, verified identity, or explicit service actor. A task explicitly
    waiting for review must first carry a merged/approved outcome or
    `[review: no-merge]`.

    Moving a task into 'doing' from another status passes the same WIP caps
    as a new `task_claim` (task #1886): per-agent (UA_AGENT_WIP_MAX, or the agent's UA_AGENT_WIP_OVERRIDES cap), lane
    (UA_LANE_WIP) and fleet (UA_FLEET_WIP_MAX / UA_FLEET_WIP_HARD_MAX), for
    the surviving claimant or else the verified caller, with the same
    exemptions. Soft caps return `wip_warnings`; hard caps refuse the update.
    Only a row already in 'doing' is not checked. A call with no verified
    identity gets the lane and fleet caps only, and its `actor` is not charged
    (the run opens with no agent). Lifecycle and per-agent budget admission
    use that same run identity; an unverified `actor` remains attribution only.
    A surviving claim whose lease lapsed is
    renewed when the claimant makes the move and dropped otherwise. A move
    that leaves the row archived is refused. Like `task_claim`, it also
    refuses a task in the `tracking` lane or under a cancelled ancestor,
    judged by the lane and parent the call leaves behind (task #1894).
    Relaning a task already in 'doing' checks the destination lane cap and
    the claimant's per-agent cap, returning soft `wip_warnings`.
    This path still does not take a claim: use `task_claim` for the lock.

    `parent_id` reparents the
    task; pass 0 to detach it from its parent. Nesting is multi-level (task
    #830): the new parent may itself be a subtask, and a task with children
    may move as a subtree. Self-parenting, ancestor cycles, and depth above
    `TASK_MAX_DEPTH` fail closed. Omitted (None) leaves the parent unchanged.

    Worktrees follow the *transition*, not the resulting state. Moving a task
    into 'doing' creates `.worktrees/task-<id>`; moving it to 'done' releases
    that worktree and its branch, provided neither holds work: a clean tree, and
    a branch that would contribute nothing if merged into the base. That last
    test is containment rather than ancestry or a tip-to-tip diff, because a
    squash-merged branch is not an ancestor of the base, and a merged branch the
    base has since advanced past differs from it in both directions — either
    would otherwise read as unmerged forever (see `_worktree_is_disposable`).
    Anything that cannot be established is kept, never force-deleted.

    Pass `worktree=False` to record a claim without creating one — the right
    call when the work is already done in another checkout — or `worktree=True`
    to materialize one for a task that is already 'doing'. Previously any
    update leaving a row in 'doing' created a worktree, so appending notes to
    an in-flight task minted a directory and a permanent branch.

    `project` reassigns the task to a different project stream (a deliberate
    move, not a filter). `expected_project` is a guard, not a mutation: when
    set, the update is rejected unless the task's *current* project matches —
    the check a project-scoped session should pass so it cannot accidentally
    mutate another project's task by id.

    `expected_updated_at` is an optimistic authority precondition. The update
    succeeds only while the task still has that exact canonical revision. It
    is checked again in the UPDATE statement so a concurrent claim, status, or
    note change cannot slip through a read-then-write race.

    `domain` reassigns the task's portfolio bucket (personal|professional|hobby).

    `untrusted=True` quarantines the task (and its subtree); anyone may set it.
    `untrusted=False` on a quarantined task needs a verified human identity,
    or else parks an approval request (`task_untrusted_clear`) whose
    human-approved one-shot pass the identical retry consumes (task #1911).
    Every clear is recorded on `agent.task.untrusted_cleared`.

    `lane` reassigns the task's queue lane (see task_add for allowed values).

    `release_claim=True` clears the authoritative claim columns without
    changing lifecycle status. The verified claimant may release its own live
    claim; another caller may release only an expired claim. This is the
    explicit recovery path for abandoned leases and never writes a new claim.

    `due_at` (ISO date or datetime) sets or changes the commitment/reminder due
    date (task #373 Tier 1, 2.2). Omitted (None) leaves it unchanged — pass
    `clear_due_at=True` to remove it (e.g. a commitment was fulfilled or set
    in error); `due_at` and `clear_due_at=True` together is a ToolError.

    `evidence_ref` records a completion pointer (commit SHA, PR URL, CI run
    link). A task whose notes carry `[code-task]` cannot transition to `done`
    without one already stored or supplied on this same call, unless its
    notes already carry a review-completion marker — see
    _evidence_required_error. A `[project-spec]` root cannot be marked `done`
    while any descendant is still `open`/`doing`/`waiting_review`/`blocked`;
    the error lists those descendants.

    `idempotency_key` (task #429): optional client token for retry-safety;
    retried requests with the same key return the previously committed result.

    Under the steward profile only (task #591): a task claimed by a
    *different* agent is refused with a ToolError rather than updated —
    steward may groom unclaimed and self-claimed tasks (close, reparent,
    re-prioritize, annotate) but may release another agent's claim only when
    its lease has expired. Developer and chief-of-staff are unaffected.

    `run_usage` (task #1436) optionally reports what the finishing run cost:
    `{provider, model, input_tokens, output_tokens, cost_usd, billing, phases,
    contended}`, any subset. `billing` is api|subscription|local, `phases` maps
    a phase name to seconds, `contended` flags a queued or rate-limited
    subscription lane (task #1348).
    It is stored on the self-pickup run record when this call ends the run;
    unreported fields stay typed null. Unknown keys or wrong types are refused.

    `request_cancel=True` (task #1525) flags the task's currently open
    self-pickup run for cancellation, without itself changing `status` (it
    may be combined with other fields on the same call, e.g. an
    `append_note`). Refused unless the task is `doing` with an open run —
    never reopens a run that already finished, including one a direct-SQL
    write left looking open on a task that has since moved on. The caller
    must be the task's own claimant or a lifecycle operator
    (`UA_LIFECYCLE_OPERATORS`, the same set `agent_lifecycle.py` uses for
    pause/resume/terminate) — an arbitrary agent may not stop another
    agent's run. The flag is advisory: it does not kill anything by itself.
    An executor observes it on its next `task_claim` renewal or mid-run
    `kill_check` poll, and queue-hygiene lease renewal must not extend a
    cancelled task's lease (see `queue_hygiene.renew_live_claims`). Returns
    `cancel_requested: true` on success; a second call is a no-op, not an
    error.

    `expected_run_id` is a fencing token (task #1525): when set, this call is
    refused unless it equals the task's current self-pickup run id
    (`current_run_id` on the task row, or a prior `task_claim`/`task_update`
    response's `run_id`). Optional and unrelated to `expected_updated_at`:
    passing it lets a caller that holds a specific run's token (an executor
    that claimed, or renewed, run `sp-...`) assert it is still ending *that*
    run and not one a takeover has since opened — a stale caller loses the
    race instead of silently finishing or overwriting a newer run. Omitted,
    this call behaves exactly as before (no fencing is enforced by default).

    `monitor_at` (ISO datetime with a UTC offset, at most 14 days ahead; task
    #1749) sets a one-shot monitor: when it is due, the liveness sweep wakes
    the claimant once with a hub message (or records a digest entry when
    nobody holds the task), then clears it. `monitor_note` (optional, at most
    500 chars) is sent with the wake and needs `monitor_at` on the same call.
    `clear_monitor=True` cancels a pending monitor. A pending future monitor
    counts as the task's action path.

    `declared_wait={"reason": ..., "until": ISO}` (task #1749) declares that
    the task is waiting on something outside the hub until `until` (with a UTC
    offset, in the future, at most 14 days ahead). While live it counts as the
    task's action path; once expired it no longer does. `declared_wait={}`
    clears it. Only the claimant, the lane's agent when nobody holds the task,
    a lifecycle operator or the human operator may set or clear a monitor or
    a declared wait. Calls that pass any of these four return `liveness`: the
    task's current `monitor`, `declared_wait` (with `first_declared_at`, the
    start of an unbroken chain of waits) and monitor attempt record.

    `next_owner` hands the task's next move to a known agent label;
    `waiting_on` says what that move waits for and `terminal_predicate` what
    ends the task (each at most 500 characters). An empty string clears any of
    them. Only the stored owner, the claimant, the lane's agent when both are
    empty, a lifecycle operator or the human operator may change them. Moving
    to done or cancelled and `release_claim` clear `next_owner`, so neither may
    name a new one on the same call. A stored owner holds the task against
    other claims only while a declared wait or monitor is pending
    (see `task_claim`).

    Returns the full updated task, plus `worktree`: what happened to the
    checkout ("removed", "already_gone", "retained:<reason>", or None).
    """
    usage = run_records.validate_usage(run_usage)
    liveness_spec = task_liveness.normalize_update(
        monitor_at=monitor_at,
        monitor_note=monitor_note,
        clear_monitor=clear_monitor,
        declared_wait=declared_wait,
    )
    liveness_state = None
    ownership_spec = task_ownership.normalize_update(
        next_owner=next_owner, waiting_on=waiting_on, terminal_predicate=terminal_predicate
    )
    if status is not None and status not in STATUSES:
        raise ToolError(f"status must be one of {STATUSES}")
    if title is not None and not title.strip():
        raise ToolError("title must not be empty")
    if append_note is not None and not append_note.strip():
        raise ToolError("append_note must not be empty")
    if append_note is not None and notes is not None:
        raise ToolError("pass append_note or notes, not both")
    if executor_finish is not None:
        if notes is not None or append_note is not None or project is not None:
            raise ToolError(
                "executor_finish cannot be combined with notes, append_note, or project"
            )
        if (
            status != "doing"
            or not expected_run_id
            or expected_delegation_id is None
            or expected_claim_generation is None
            or release_claim
            or request_cancel
            or worktree is True
        ):
            raise ToolError("executor_finish requires doing status and full run fencing")
        if not isinstance(executor_finish, dict):
            raise ToolError("executor_finish must be a delivery or scout operation")
        kind = executor_finish.get("kind")
        if kind == "delivery":
            if set(executor_finish) != {"kind"}:
                raise ToolError("delivery executor_finish accepts only kind")
        elif kind == "scout":
            path = executor_finish.get("report_path")
            if (
                set(executor_finish) != {"kind", "report_path"}
                or not isinstance(path, str)
                or not path.strip()
                or any(char in path for char in "\r\n[]")
            ):
                raise ToolError("scout executor_finish requires a single report_path")
            from ..scout import evidence_ref_for_report

            if evidence_ref != evidence_ref_for_report(path):
                raise ToolError("scout executor_finish evidence_ref must match report_path")
        else:
            raise ToolError("executor_finish kind must be delivery or scout")
    if priority is not None and priority not in PRIORITIES:
        raise ToolError(f"priority must be one of {PRIORITIES}")
    if project is not None and project not in PROJECTS:
        raise ToolError(f"project must be one of {PROJECTS}")
    if domain is not None and domain not in DOMAINS:
        raise ToolError(f"domain must be one of {DOMAINS}")
    if lane is not None:
        lane = _validate_lane(lane)
    if due_at is not None and clear_due_at:
        raise ToolError("pass either due_at or clear_due_at=True, not both")

    updater = config.verified_agent_identity()
    updater_untrusted = config.is_agent_untrusted(updater)
    # `actor` is the fallback attribution for callers with no verified auth
    # context — the dispatch scheduler completing a real-spawn run, or the web
    # UI completing a task on an operator's behalf. It never overrides a
    # verified identity (an authenticated agent cannot spoof someone else), so
    # it only takes effect when `updater` is None.
    effective_actor = updater or (actor.strip() if actor and actor.strip() else None)
    budget = None
    wip_warnings: list[str] = []

    untrusted_val = None
    if untrusted is not None:
        untrusted_val = 1 if untrusted else 0
    elif updater_untrusted:
        untrusted_val = 1

    sets, params = ["updated_at=?"], [storage.now()]
    for col, val in (
        ("status", status),
        ("title", title),
        ("notes", notes),
        ("priority", priority),
        ("project", project),
        ("domain", domain),
        ("lane", lane),
        ("evidence_ref", evidence_ref),
    ):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    if untrusted_val is not None:
        sets.append("untrusted=?")
        params.append(untrusted_val)
    if archived is True:
        sets.append("archived_at=?")
        params.append(storage.now())
    elif archived is False:
        sets.append("archived_at=NULL")
    if due_at is not None:
        sets.append("due_at=?")
        params.append(due_at)
    elif clear_due_at:
        sets.append("due_at=NULL")
    if parent_id is not None:
        if parent_id == 0:
            sets.append("parent_id=NULL")
        else:
            sets.append("parent_id=?")
            params.append(parent_id)
    if status == "done":
        # COALESCE, not an overwrite: re-asserting 'done' (appending notes to a
        # finished task) used to restamp the completion time, resetting the
        # created_at->completed_at cycle time monitor.py reports. Backfills a
        # NULL left by a pre-guard row.
        sets.append("completed_at=COALESCE(completed_at, ?)")
        params.append(storage.now())
    elif status == "cancelled":
        # Terminal, but not "completed" — leave completed_at exactly as it was
        # (NULL if never done, preserved if a done task is being reclassified).
        pass
    elif status is not None:
        sets.append("completed_at=NULL")
    params.append(id)

    if (expected_delegation_id is None) != (expected_claim_generation is None):
        raise ToolError(
            "expected_delegation_id and expected_claim_generation must be supplied together"
        )
    if expected_delegation_id is not None:
        if expected_run_id is None:
            raise ToolError("delegation task fencing requires expected_run_id")
        if (
            not isinstance(expected_delegation_id, int)
            or isinstance(expected_delegation_id, bool)
            or expected_delegation_id <= 0
        ):
            raise ToolError("expected_delegation_id must be a positive integer")
        if (
            not isinstance(expected_claim_generation, int)
            or isinstance(expected_claim_generation, bool)
            or expected_claim_generation < 0
        ):
            raise ToolError("expected_claim_generation must be a nonnegative integer")
    # A committed receipt is a read, not another clear. Check it before the
    # approval gate: a later, independent quarantine must stay set on replay.
    # The transaction lookup below still covers a receipt committed in flight.
    if idempotency_key and untrusted_val == 0:
        replay_conn = storage.connect()
        try:
            cached = _untrusted_clear_receipt(replay_conn, id, idempotency_key)
        finally:
            replay_conn.close()
        if cached is not None:
            from . import approvals

            if approvals.registered_task_binding() is not None:
                # Wrapper preflight may have missed a competing commit. Recheck
                # the proven receipt and acknowledge its existing evidence.
                return acknowledge_registered_task_replay(
                    {"id": id, "idempotency_key": idempotency_key}
                )
            return cached
    # The native scope restores this pass if transaction validation fails.
    clear_authority = (
        _authorize_untrusted_clear(id, updater, effective_actor) if untrusted_val == 0 else None
    )
    from ..invocation_service import InvocationService
    from . import approvals

    conn = InvocationService.begin_mutation(storage.connect)
    try:
        # Tenant fence (task #1814): another tenant's id reads exactly like a
        # missing one, before any receipt replay or write. Every later statement
        # in this IMMEDIATE transaction names the same, now-visible row.
        if _visible_task(conn, id) is None:
            raise ToolError(f"no task with id {id}")
        if idempotency_key:
            cached = (
                _untrusted_clear_receipt(conn, id, idempotency_key)
                if untrusted_val == 0
                else approvals.bound_task_receipt(conn, idempotency_key, id)
            )
            if cached is not None:
                if cached.get("id") != id or cached.get("org_id") != _tenant():
                    raise ToolError("task receipt does not match the authorized resource")
                from .. import gateway

                gateway.commit_registered_call_on_connection(
                    conn, "task_update", cached, replay=True
                )
                return cached
        if approvals.committed_task_replay_active():
            # Admitted as a replay, so no approval pass was spent: never let
            # that admission reach a write when the receipt is missing (#1980).
            raise ToolError("committed task replay has no matching receipt; nothing was written")
        old = conn.execute(
            "SELECT status, notes, project, evidence_ref, updated_at, claimed_by, "
            "claimed_at, lock_expires_at, lane, current_run_id, current_run_agent, "
            f"cancel_requested, priority, archived_at, untrusted, next_owner FROM tasks "
            f"WHERE id=? AND {_TENANT_SQL}",
            (id, _tenant()),
        ).fetchone()
        if old is None:
            raise ToolError(f"no task with id {id}")
        if liveness_spec is not None:
            # Task #1749: only the task's owner may silence its liveness check.
            task_liveness.authorize_update(old, effective_actor)
        if ownership_spec is not None:
            task_liveness.authorize_update(old, effective_actor, ownership=True)
        # Internal scheduler fence; not exposed by the MCP argument model.
        # Read inside the task writer transaction so a newer run cannot launch
        # between the scheduler's precheck and this completion write.
        expected_dispatch_run_id = _dispatch_completion_run.get()
        if expected_dispatch_run_id is not None:
            live = conn.execute(
                "SELECT run_id FROM dispatch_runs WHERE task_id=? AND "
                f"({storage.DISPATCH_RUN_LIVE_SLOT_PREDICATE}) "
                "AND run_id != ? LIMIT 1",
                (id, expected_dispatch_run_id),
            ).fetchone()
            if live is not None:
                raise ToolError("task completion refused: a newer dispatch run owns this task")
        # Attached early (task #1525): both the cancellation write below and
        # the run-transition write further down share this same open
        # transaction, so their effects commit or roll back with the task row.
        run_tx = run_records.attach(conn)
        # Task #1700: an operator release (claim_release.task_claim_release) is
        # the one exception, scoped to its own call, task and caller, and fenced
        # on the claimant, claim and status read here under the write lock.
        # Checked before the run fence so a stale run is its 409 (task #2003).
        operator_release = claim_release.active_grant(id, updater) if release_claim else None
        if operator_release is not None:
            claim_release.check_in_tx(operator_release, old, conn)
        if expected_run_id is not None and old["current_run_id"] != expected_run_id:
            # Fencing (task #1525): a stale token loses to whatever run is
            # actually current now — a takeover, a renewal that rotated the
            # run, or no open run at all — rather than finishing or
            # overwriting work it no longer owns.
            raise ConflictError(
                f"task {id}'s current run is {old['current_run_id']!r}, "
                f"not the expected {expected_run_id!r}"
            )
        if expected_delegation_id is not None:
            _assert_worktree_authority(
                conn,
                id,
                expected_run_id,
                effective_actor or old["claimed_by"],
                expected_delegation_id,
                expected_claim_generation,
                require_in_flight=worktree is True or executor_finish is not None,
                allow_cancelled=worktree is not True
                and (status in ("blocked", "cancelled") or release_claim),
            )
        elif expected_run_id is not None and worktree is True:
            _assert_worktree_authority(
                conn, id, expected_run_id, effective_actor or old["claimed_by"]
            )
        if request_cancel:
            if old["status"] != "doing" or not old["current_run_id"]:
                # Never reopens a finished/released run for cancellation, even
                # if a direct-SQL write left an old claim event looking open
                # (PR #866 review: cancellation must not target a finished
                # task, and must be scoped to the task's *current* run).
                raise ToolError(f"task {id} has no open run to cancel")
            if updater != old["claimed_by"] and updater not in agent_lifecycle.operators():
                raise ToolError(
                    f"{updater!r} may not cancel {old['claimed_by']!r}'s run on task {id}: "
                    "cancelling another agent's run is an operator action "
                    "(UA_LIFECYCLE_OPERATORS)"
                )
        if status is not None and status not in ("blocked", "cancelled"):
            if _is_queued_routine_task(conn, id):
                raise ToolError(f"task {id} is queued routine work; wait for scheduler release")
        now = storage.now()
        claim_expired = bool(
            old["claimed_by"] and old["lock_expires_at"] and old["lock_expires_at"] <= now
        )
        if (
            release_claim
            and old["claimed_by"]
            and old["claimed_by"] != updater
            and not claim_expired
            and operator_release is None
        ):
            raise ConflictError(
                f"task {id} is claimed by {old['claimed_by']!r} with an active lease; "
                "only the claimant or an expired claim may be released"
            )
        if old["claimed_by"] and old["claimed_by"] != updater and operator_release is None:
            from .. import profiles

            if profiles.current() == profiles.STEWARD and not (release_claim and claim_expired):
                raise PermissionDeniedError(
                    f"task {id} is claimed by {old['claimed_by']!r} — steward "
                    "may not update another agent's claimed task; groom "
                    "unclaimed or self-claimed tasks, or ask the claimant"
                )
        from . import approvals

        # Every consumed grant, including the generic registered wrapper gate,
        # is checked on the transaction owner connection before any mutation.
        approvals.revalidate_invocation_approvals(conn)
        if untrusted_val == 0 and old["untrusted"]:
            if clear_authority is None:
                raise ToolError(
                    f"task {id} was quarantined while this update was in flight; "
                    "retry so clearing it is authorized"
                )
        if release_claim and old["claimed_by"]:
            sets.extend(("claimed_by=NULL", "claimed_at=NULL", "lock_expires_at=NULL"))
        ownership_ended = bool(release_claim and old["claimed_by"]) or (
            status in TERMINAL_STATUSES and old["status"] not in TERMINAL_STATUSES
        )
        if ownership_ended:
            if ownership_spec is not None and ownership_spec.get("next_owner"):
                raise ToolError(
                    "next_owner cannot be set on a call that closes the task or releases "
                    "its claim, which clears the owner"
                )
            sets.extend(task_ownership.END_OWNERSHIP)
        if ownership_spec is not None:
            clauses, values = task_ownership.update_sets(old, ownership_spec, ended=ownership_ended)
            sets.extend(clauses)
            params[-1:-1] = values
        params[0] = _next_task_revision(old["updated_at"])
        if executor_finish is not None:
            # This row was read under BEGIN IMMEDIATE and admitted against the
            # exact run and delegation generation above. Never use daemon claim
            # snapshots here: reviewers can edit notes/project during execution.
            current_notes = old["notes"] or ""
            if executor_finish["kind"] == "scout":
                from ..scout import apply_scout_finish

                notes = apply_scout_finish(
                    current_notes, executor_finish["report_path"], fresh_review=True
                )
            else:
                from ..delivery import apply_finish_markers, resolve_delivery
                from ..operating_profiles import run_snapshot

                # The finishing run is still open here: its frozen operating
                # snapshot decides the delivery marker (task #1756).
                notes = apply_finish_markers(
                    current_notes,
                    resolve_delivery(
                        old["project"],
                        notes=current_notes,
                        snapshot=run_snapshot(conn, id),
                    ),
                    fresh_review=True,
                )
            sets.append("notes=?")
            params.insert(-1, notes)
        if append_note is not None:
            note_text = append_note.strip()
            current_notes = old["notes"] or ""
            notes = (
                current_notes
                if _append_note_is_duplicate(current_notes, note_text)
                else (f"{current_notes}\n{note_text}" if current_notes else note_text)
            )
            note_param = next((i for i, col in enumerate(sets) if col == "notes=?"), None)
            if note_param is None:
                sets.append("notes=?")
                params.insert(-1, notes)
            else:
                # params has the same order as assignments, after the leading
                # updated_at value; find the matching assignment by counting it.
                params[note_param] = notes
        if expected_project is not None and old["project"] != expected_project:
            raise ToolError(
                f"task {id} belongs to project {old['project']!r}, not {expected_project!r}"
            )
        if project is not None and project != old["project"]:
            linked = conn.execute(
                "SELECT goal_id FROM task_goal_links WHERE task_id=?", (id,)
            ).fetchone()
            goal = None
            if linked is not None:
                # Fenced like goals.py (#1999): a link written before the fence
                # to another tenant's goal reads as no link, so this error
                # never names a foreign goal's id or project.
                goal = conn.execute(
                    f"SELECT project FROM goals WHERE id=? AND {_TENANT_SQL}",
                    (int(linked["goal_id"]), _tenant()),
                ).fetchone()
            if goal is not None:
                goal_project = str(goal["project"])
                if goal_project != project:
                    raise ToolError(
                        f"task {id} serves goal {linked['goal_id']} in project {goal_project}; "
                        "refusing to move it to a different project"
                    )
        if expected_updated_at is not None and old["updated_at"] != expected_updated_at:
            raise ConflictError(
                f"task {id} authority revision changed: expected updated_at "
                f"{expected_updated_at!r}, found {old['updated_at']!r}"
            )
        effective_status = status if status is not None else old["status"]
        if status is not None and status not in TERMINAL_STATUSES:
            if old["status"] in TERMINAL_STATUSES:
                # A reopened task starts a new claim lifecycle. This also
                # clears actor-only completion attribution, which has no lock
                # metadata and would otherwise be permanent in task_claim.
                sets.extend(("claimed_by=NULL", "claimed_at=NULL", "lock_expires_at=NULL"))
        entering_doing = status == "doing" and old["status"] != "doing"
        doing_agent = old["claimed_by"]
        if (
            not entering_doing
            and effective_status == "doing"
            and (status == "doing" or (lane is not None and lane != old["lane"]))
        ):
            # A row already in 'doing' that this call relanes, or re-asserts as
            # doing, must be left in a claimable lane (task #2002). Entering
            # doing is judged by _assert_claimable below. A legacy doing row
            # already in an unclaimable lane can still be annotated, released,
            # moved out of doing or relaned to a work lane.
            _assert_claimable_lane(id, lane if lane is not None else old["lane"])
        if entering_doing:
            # Entering 'doing' starts work as a claim does, so the same
            # claimability refusals apply first (task #1894), read against the
            # lane and parent this call leaves behind.
            _assert_claimable(
                conn,
                id,
                lane if lane is not None else old["lane"],
                new_parent_id=parent_id,
            )
            # Moving a task into 'doing' adds WIP exactly as a new task_claim
            # does (task #1886), so the same per-agent, lane and fleet caps
            # apply. Only a row already 'doing' (renewal-equivalent) is not
            # checked. Every caller is checked, verified or not. The one
            # in-process caller that moves a task *into* doing is Slack
            # `/progress` (slack_gateway.dispatch), which arrives on a public
            # signed-webhook path with no verified identity; an MCP client
            # without hub auth is the other unverified case. Both get the
            # identity-independent lane and fleet caps (refused at the fleet
            # hard cap unless high-priority or [urgency: now]; the gateway
            # replies with the refusal) plus the per-agent cap of a live
            # surviving claimant, and on a lapsed claim they drop the holder's
            # claimed_by (the caller is not the holder). The scheduler, bug
            # worker and crew use other statuses; the executor re-asserts
            # doing->doing after its own task_claim. A fenced call
            # (expected_run_id) is checked too: a run token can only match
            # here on an anomalous row, since leaving doing closes the run and
            # clears it.
            # Admission reads the row this call leaves behind: archived=True
            # parks it on the same call, archived=False unparks it.
            stays_archived = archived is True or (
                archived is None and old["archived_at"] is not None
            )
            if stays_archived:
                raise ToolError(
                    f"task {id} is parked: unpark it first (coordination_manage queue_unpark) "
                    "so the queue and its notes agree before work starts"
                )
            claim_cleared = old["status"] in TERMINAL_STATUSES or bool(
                release_claim and old["claimed_by"]
            )
            surviving_claimant = None if claim_cleared else old["claimed_by"]
            if surviving_claimant and claim_expired:
                # A lapsed lease (most blocked tasks) would leave a claimed
                # row that neither the live-claim nor the unclaimed count
                # sees, and hygiene would send it back to open. The holder
                # re-entering doing renews its lease, as task_claim would;
                # anyone else drops the dead claim so the row counts as
                # unclaimed WIP under the caller's run.
                if surviving_claimant == updater:
                    renewed_until = datetime.fromisoformat(now) + timedelta(
                        seconds=config.TASK_CLAIM_DEFAULT_LEASE_SECONDS
                    )
                    sets.append("lock_expires_at=?")
                    params.insert(-1, renewed_until.isoformat(timespec="seconds"))
                else:
                    sets.extend(("claimed_by=NULL", "claimed_at=NULL", "lock_expires_at=NULL"))
                    surviving_claimant = None
            # Charged to the surviving claimant, else the verified caller. An
            # unverified caller's `actor` is never charged: its run is opened
            # with no agent (attribute_actor below), so only the fleet and
            # lane caps bind it.
            doing_agent = surviving_claimant
            wip_lane = lane if lane is not None else old["lane"]
            wip_warnings = _enforce_wip_caps(conn, surviving_claimant or updater, wip_lane, now, id)
            wip_warnings += _enforce_fleet_wip(
                conn,
                wip_lane,
                priority if priority is not None else old["priority"],
                notes if notes is not None else old["notes"],
                now,
                id,
            )
        elif lane is not None and lane != old["lane"] and effective_status == "doing":
            # Only a row already in 'doing' reaches here (entering doing took
            # the branch above, lane change included): relaning in-flight work
            # into a capped lane checks that lane, for every caller.
            # The per-agent cap of the claimant (else the verified caller)
            # applies too, and soft lane warnings are returned (task #1894).
            wip_warnings = _enforce_wip_caps(
                conn, old["claimed_by"] or updater, lane, params[0], id
            )
        # Match the recorder's claimant-or-verified-actor identity. An
        # unverified explicit actor is only opened_by evidence, not the agent
        # charged for this run, so its pause or budget cannot gate admission.
        lifecycle_agent = doing_agent or updater
        if status == "doing" and old["status"] != "doing" and lifecycle_agent:
            blocked = agent_lifecycle.claim_block(conn, lifecycle_agent)
            if blocked:
                raise ToolError(blocked)
        if entering_doing:
            # Entering 'doing' from another status is a new claim: a held
            # subtree refuses it (task #1747). A hold this releases on its own
            # policy is published with the run effects after commit.
            held = task_holds.claim_block(conn, id, pending=run_tx.info_pending_publish)
            if held:
                raise ToolError(held)
        budget_agent = doing_agent or updater
        if status == "doing" and old["status"] != "doing" and budget_agent:
            # Entering 'doing' without task_claim opens a run too (task #1436);
            # the same budget hard stop applies (task #1348).
            budget = budgets.evaluate_claim_or_config_error(conn, budget_agent, task_id=id)
            if budget.blocked:
                conn.rollback()
                budgets.record_decision(budget)
                raise ToolError(budget.blocked)
        if status == "done":
            effective_notes = notes if notes is not None else old["notes"]
            review_error = _completion_review_error(effective_notes)
            if review_error:
                raise ToolError(review_error)
            effective_evidence = evidence_ref if evidence_ref is not None else old["evidence_ref"]
            evidence_error = _evidence_required_error(effective_notes, effective_evidence)
            if evidence_error:
                raise ToolError(evidence_error)
            project_error = _project_completion_error(conn, id, effective_notes)
            if project_error:
                raise ToolError(project_error)
            if not str(old["claimed_by"] or "").strip() and not effective_actor:
                raise ToolError(
                    "task completion requires an attributed actor "
                    "(verified identity or explicit actor)"
                )
            if not old["claimed_by"] and effective_actor:
                if "claimed_by=?" not in sets:
                    sets.append("claimed_by=?")
                    params.insert(-1, effective_actor)
            elif effective_status != "done" and effective_actor:
                # Re-stamp attribution only on the completion transition;
                # already-done rows keep their original claimant.
                if "claimed_by=?" not in sets:
                    sets.append("claimed_by=?")
                    params.insert(-1, effective_actor)
        taint_subtree = untrusted_val == 1
        if parent_id is not None and parent_id != 0:
            _assert_nesting(conn, parent_id, moving_id=id)
            parent = _visible_task(conn, parent_id, "parent_id, untrusted")
            if parent is None:
                raise ToolError(f"no task with id {parent_id} to use as parent")
            taint_subtree = taint_subtree or bool(parent["untrusted"])
        where = f"id=? AND {_TENANT_SQL}"
        params.append(_tenant())
        if expected_updated_at is not None:
            where += " AND updated_at=?"
            params.append(expected_updated_at)
        updated = conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE {where}", params)
        if expected_updated_at is not None and updated.rowcount != 1:
            conn.rollback()
            raise ConflictError(
                f"task {id} authority revision changed during update; retry only after "
                "reading the canonical task again"
            )
        if liveness_spec is not None:
            # Same transaction as the task row (task #1749): a refused update
            # never leaves a monitor or declared wait behind.
            liveness_state = task_liveness.apply_update(
                conn, id, liveness_spec, actor=effective_actor
            )
        if taint_subtree:
            subtree = _subtree_ids(conn, id)
            placeholders = ",".join("?" * len(subtree))
            conn.execute(
                f"UPDATE tasks SET untrusted=1 WHERE id IN ({placeholders})",
                subtree,
            )
            # Each descendant gets a strictly newer optimistic revision of its
            # own (task #1818), as the #1810 detach does: a shared second-
            # precision storage.now() could equal or predate a descendant's
            # microsecond revision, so a stale expected_updated_at would pass.
            descendants = conn.execute(
                f"SELECT id, updated_at FROM tasks WHERE id IN ({placeholders}) AND id != ?",
                [*subtree, id],
            ).fetchall()
            for row in descendants:
                conn.execute(
                    "UPDATE tasks SET updated_at=? WHERE id=?",
                    (_next_task_revision(row["updated_at"]), row["id"]),
                )
        if request_cancel:
            # Idempotent: a retried request is a no-op, not a second event
            # (record_cancel_request_in_tx also dedupes on the event side).
            newly_set = (
                conn.execute(
                    "UPDATE tasks SET cancel_requested=1 WHERE id=? AND cancel_requested=0",
                    (id,),
                ).rowcount
                > 0
            )
            if newly_set:
                # Remote executors observe the run event, not this task row.
                # Cancellation is not acknowledged unless both commit.
                run_records.record_cancel_request_in_tx(
                    run_tx,
                    id,
                    run_id=old["current_run_id"],
                    agent=old["current_run_agent"],
                    requested_by=updater or effective_actor or "unknown",
                )
        event_to_publish = None
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (id,)).fetchone()
        actually_cleared = bool(old["untrusted"] and not row["untrusted"])
        if actually_cleared:
            events.record_event_once_on_connection(
                conn,
                CHANNEL_UNTRUSTED_CLEARED,
                payload={"kind": "task", "ref_id": str(id), "task_id": id, **clear_authority},
                originator=updater or effective_actor,
            )
        if operator_release is not None:
            payload = claim_release.ledger_payload(operator_release, old, row["status"])
            operator_release["event"] = payload
            operator_release["event_ulid"] = events.record_event_once_on_connection(
                conn, claim_release.CHANNEL, payload=payload, originator=updater
            )[0]

        # Worktree lifecycle. Keyed on what this call *changed*, not on the
        # status the row happens to hold: the old check ("is doing and has no
        # worktree") fired for notes-only edits to in-flight tasks and for
        # bookkeeping claims, which is where the residue came from. `task_claim`
        # is untouched — the executor resolves its sandbox and merge target from
        # the worktree_path that claim persists.
        entered_doing = status == "doing" and old["status"] != "doing"
        wants_worktree = worktree if worktree is not None else entered_doing
        if config.agent_skips_task_worktree(row["claimed_by"]) or config.agent_skips_task_worktree(
            updater
        ):
            wants_worktree = False
        if expected_delegation_id is not None and wants_worktree:
            suffix = _worktree_run_suffix(
                expected_run_id, expected_claim_generation, expected_delegation_id
            )
            if row["worktree_path"] is None or not Path(row["worktree_path"]).name.endswith(suffix):
                historical_notes = _historical_delivery_notes(
                    row["notes"],
                    row["evidence_ref"],
                    f"{row['current_run_id']} / {row['worktree_path'] or 'unbound checkout'}",
                )
                conn.execute(
                    "UPDATE tasks SET notes=?, evidence_ref=NULL, worktree_path=NULL, branch=NULL WHERE id=?",
                    (historical_notes, id),
                )
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (id,)).fetchone()
        worktree_pending = (
            wants_worktree and row["status"] == "doing" and row["worktree_path"] is None
        ) or (status == "done" and old["status"] != "done" and row["worktree_path"] is not None)
        result = _row(row)
        # A replay receipt cannot claim an OS-side worktree result that was not
        # part of the SQLite commit. Keep the cached response deterministic;
        # non-idempotent callers retain the historical post-commit status field.
        result["worktree"] = "pending" if idempotency_key and worktree_pending else None
        if status is not None and status != old["status"]:
            _, inserted, event_to_publish = events.record_event_once_on_connection(
                conn,
                events.CHANNEL_TASK_UPDATED,
                payload={
                    "kind": "task",
                    "ref_id": str(id),
                    "task_id": id,
                    "from_status": old["status"],
                    "status": status,
                    "updated_at": result["updated_at"],
                    "summary": f"status {old['status']}->{status}: {row['title']}",
                },
                originator=updater,
                dedupe_key=(
                    f"task_update:{id}:{result['updated_at']}" if idempotency_key else None
                ),
            )
            if not inserted:
                raise RuntimeError(
                    "task transition event dedupe collision; refusing unevidenced mutation"
                )
        # Self-pickup run record (task #1436): open on entering 'doing', close
        # on leaving it or on a claim release. The recorder also queues the
        # completion return (task #1751) in this transaction; flush() below
        # schedules its drain after commit. Made atomic with the task write
        # above (task #1525): recorded in this same transaction, before
        # commit, rather than best-effort afterward — a crash between the two
        # writes can no longer leave the task's new status durable with no
        # record of what happened to its run, or the reverse.
        # Unrecorded tasks retain best-effort recorder behavior. Once a run
        # token is bound, the transition must retire or replace it atomically;
        # otherwise an old executor could keep writing after a release/finish.
        claim_released = bool(release_claim and old["claimed_by"])
        if status is not None and status != old["status"]:
            # Queue the completion return outside the recorder's best-effort
            # savepoint, so a recorder failure cannot drop the notice. The
            # recorder's own call then hits the (task, revision) key and is a
            # no-op.
            run_records.queue_completion_return(
                run_tx,
                id,
                old_status=old["status"],
                new_status=status,
                revision=result["updated_at"],
            )
        if (status is not None and status != old["status"]) or claim_released:
            recorded_transition = run_records.safe_in_tx(
                run_records.record_transition_in_tx,
                run_tx,
                id,
                old_status=old["status"],
                new_status=status,
                actor=effective_actor,
                # Releasing a claim clears the row before the run recorder
                # executes. Preserve the owner whose doing run this transition
                # is ending so it cannot finish a different claimant's run.
                claimed_by=(
                    old["claimed_by"]
                    if old["status"] == "doing" and status != "doing"
                    else row["claimed_by"]
                ),
                revision=result["updated_at"],
                released=claim_released,
                usage=usage,
                lane=row["lane"],
                project=row["project"],
                attribute_actor=updater is not None,
                # Task #1747: the executor's fenced cancel of the very run a
                # cancellation request stands on is an acknowledgement, not a
                # cancel of its own; a subtree hold's restore reads this.
                # Only the run's own agent can acknowledge: another caller's
                # fenced cancel is a cancel of its own, never an acknowledgement.
                cancel_acknowledged=bool(
                    status == "cancelled"
                    and expected_run_id is not None
                    and old["cancel_requested"]
                    and old["current_run_id"] == expected_run_id
                    and updater is not None
                    and updater == old["current_run_agent"]
                ),
            )
            if old["current_run_id"] is not None and recorded_transition is None:
                raise ToolError(f"task {id}'s run recording failed; lifecycle update rolled back")

        # The run recorder can clear current_run_id and cancel_requested (or
        # open a new run) in this transaction. Return and cache that committed
        # row, not the snapshot from before its write.
        result = _row(conn.execute("SELECT * FROM tasks WHERE id=?", (id,)).fetchone())
        if (
            result["current_run_id"] is not None
            and result["current_run_id"] != old["current_run_id"]
        ):
            prior = conn.execute(
                "SELECT json_extract(payload,'$.run_id') AS run_id FROM event_log "
                "WHERE channel=? AND json_extract(payload,'$.kind')=? "
                "AND json_extract(payload,'$.task_id')=? AND json_extract(payload,'$.run_id') IS NOT ? "
                "ORDER BY rowid DESC LIMIT 1",
                (run_records.CHANNEL, run_records.KIND_CLAIMED, id, result["current_run_id"]),
            ).fetchone()
            if prior is not None or result["worktree_path"] is not None:
                # New logical runs cannot inherit a predecessor's Git index or
                # delivery marker, even when this call suppresses provisioning.
                previous_run = old["current_run_id"] or (prior["run_id"] if prior else "legacy")
                historical_notes = _historical_delivery_notes(
                    result["notes"], result["evidence_ref"], previous_run
                )
                conn.execute(
                    "UPDATE tasks SET notes=?, evidence_ref=NULL, worktree_path=NULL, branch=NULL WHERE id=?",
                    (historical_notes, id),
                )
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (id,)).fetchone()
                result = _row(row)
                worktree_pending = wants_worktree and result["status"] == "doing"
        result["worktree"] = "pending" if idempotency_key and worktree_pending else None
        if wip_warnings:
            # Part of the receipt, so a keyed retry returns the same warnings.
            result["wip_warnings"] = wip_warnings
        if liveness_state is not None:
            # Part of the receipt: main records it below with the rest of result.
            result["liveness"] = liveness_state
        if status == "cancelled" and old["status"] != "cancelled":
            from .delegate import cancel_delegations_for_task

            # Retire only the inventory protected by this task/run transition.
            # A post-commit callback could race a reopen and cancel a successor
            # delegation generation after the owning run already changed.
            cancel_delegations_for_task(id, conn=conn)
        revoked_sessions: list[str] = []
        if status in TERMINAL_STATUSES and old["status"] != status:
            # Closing a task cuts off any live session bound to it, in this
            # same transaction (task #1744).
            from .. import session_credentials

            revoked_sessions = session_credentials.revoke_for_tasks(conn, [id], "task_closed")
        successor_task = workflow_roles.on_task_transition(
            conn, id, status, updater, old, expected_run_id=expected_run_id
        )
        if successor_task is not None:
            result["workflow_successor_task_id"] = successor_task
        from .. import gateway

        def prepare_required_state(transaction):
            if idempotency_key:
                approvals.record_task_idempotency_on_connection(
                    transaction, idempotency_key, result
                )
            if not actually_cleared:
                approvals.restore_unused_task_clear_on_connection(transaction)
            approvals.record_task_receipt_on_connection(transaction, result)

        gateway.commit_registered_call_on_connection(
            conn, "task_update", result, prepare=prepare_required_state
        )
        approvals.mark_governed_execution()
        if revoked_sessions:
            from .. import session_credentials

            session_credentials.emit_revoked(revoked_sessions, "task_closed", actor=updater)
        try:
            schedule_sync_to_vault()
        except Exception:
            pass
        # Activity index: record status transitions only; best-effort, must
        # never break the acknowledged task write.
        if status is not None and status != old["status"]:
            try:
                storage.record_agent_event(
                    "task",
                    ref_id=str(id),
                    agent=updater,
                    summary=f"status {old['status']}->{status}: {row['title']}",
                    mirror=False,
                )
            except Exception:
                pass
        if event_to_publish is not None:
            try:
                events.publish(event_to_publish)
            except Exception:
                pass
        # Flushed after the task's own transition event, so a subscriber sees
        # the task change before the run-ledger side effect it caused.
        run_records.flush(run_tx)

        # OS-side worktree effects are deliberately after the durable logical
        # transition. They may be retried/reconciled independently and never
        # participate in the SQLite rollback boundary.
        admitted_run_id = result["current_run_id"]
        admitted_agent = result["claimed_by"] or result["current_run_agent"]
        admitted_status = result["status"]
        worktree_status = None
        if wants_worktree and row["status"] == "doing" and row["worktree_path"] is None:
            _ensure_task_worktree(
                id,
                row["title"],
                conn,
                project=row["project"],
                notes=row["notes"],
                expected_run_id=admitted_run_id,
                expected_agent=admitted_agent,
                expected_delegation_id=expected_delegation_id,
                expected_claim_generation=expected_claim_generation,
            )
        elif status == "done" and old["status"] != "done":
            worktree_status = _release_task_worktree(
                row["worktree_path"], row["branch"], project=row["project"], notes=row["notes"]
            )
            if worktree_status in ("removed", "already_gone"):
                conn.execute(
                    "UPDATE tasks SET worktree_path=NULL, branch=NULL WHERE id=? "
                    "AND status=? AND updated_at=? AND current_run_id IS ? "
                    "AND worktree_path IS ? AND branch IS ?",
                    (
                        id,
                        result["status"],
                        result["updated_at"],
                        result["current_run_id"],
                        result["worktree_path"],
                        result["branch"],
                    ),
                )
                conn.commit()
        if wants_worktree and admitted_status == "doing":
            _assert_worktree_authority(
                conn,
                id,
                admitted_run_id,
                admitted_agent,
                expected_delegation_id,
                expected_claim_generation,
            )
        if not idempotency_key and worktree_pending:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (id,)).fetchone()
            result = _row(row)
            result["worktree"] = worktree_status
        if worktree is True and not idempotency_key:
            result["worktree_source_sha"] = _task_worktree_source_sha(result)
            if wants_worktree and admitted_status == "doing":
                _assert_worktree_authority(
                    conn,
                    id,
                    admitted_run_id,
                    admitted_agent,
                    expected_delegation_id,
                    expected_claim_generation,
                )
    finally:
        conn.close()
    # Delegation callbacks (task 62 · A): if this task was delegated and just
    # completed, fire its callback. Best-effort — a callback failure must never
    # fail the task update; the delegation row records the outcome.
    if status == "done" and old["status"] != "done":
        try:
            from .delegate import fire_completion_callbacks

            fire_completion_callbacks(id)
        except Exception:
            pass
    # FirstMate-style zero-token supervisor wake (task #1028): emit only on
    # completion / block / needs-decision. Best-effort — never break the update.
    if status is not None and status != old["status"]:
        try:
            from ..supervision import maybe_emit_supervisor_wake

            maybe_emit_supervisor_wake(
                task_id=id,
                old_status=old["status"],
                new_status=status,
                title=result.get("title"),
                agent=updater,
                project=result.get("project"),
                updated_at=result.get("updated_at"),
            )
        except Exception:
            pass
    if wip_warnings:
        result["wip_warnings"] = wip_warnings
    if liveness_state is not None:
        result["liveness"] = liveness_state
    if updater is not None and not idempotency_key:
        # The caller's own label (task #2037): a client hook tells its own runs
        # from a surviving claimant's by it. Keyed calls omit it: their reply is
        # the stored receipt, which another caller may legitimately replay.
        result["verified_caller"] = updater
    if budget is not None:
        if budget.warnings:
            result["budget_warnings"] = budget.warnings
        budgets.record_decision(budget)
    task_changed(id)
    if successor_task is not None:
        task_changed(successor_task)
    return result


_PRIORITY_RANK_SQL = "CASE priority WHEN 'high' THEN 0 WHEN 'med' THEN 1 ELSE 2 END"
_CURSOR_VERSION = 1
_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]+")


def _encode_task_cursor(rank: int, created_at: str, task_id: int) -> str:
    """Opaque keyset cursor: the sort key of the last row on a page."""
    raw = json.dumps([_CURSOR_VERSION, rank, created_at, task_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_task_cursor(cursor: str) -> tuple[int, str, int]:
    """Parse a cursor from `_encode_task_cursor`. Anything else is refused.

    The cursor only names a position in the sort order; it carries no authority
    and no filters, so a hand-made one can at worst skip rows the caller could
    already list.
    """
    # urlsafe_b64decode silently drops characters outside its alphabet, so
    # check the alphabet first, then require the canonical re-encoding.
    if not isinstance(cursor, str) or not _CURSOR_RE.fullmatch(cursor):
        raise ToolError("cursor is not a task_list cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (binascii.Error, UnicodeDecodeError, ValueError) as e:
        raise ToolError("cursor is not a task_list cursor") from e
    if (
        not isinstance(value, list)
        or len(value) != 4
        or type(value[0]) is not int
        or value[0] != _CURSOR_VERSION
        or not isinstance(value[1], int)
        or isinstance(value[1], bool)
        or not isinstance(value[2], str)
        or not isinstance(value[3], int)
        or isinstance(value[3], bool)
    ):
        raise ToolError("cursor is not a task_list cursor")
    if _encode_task_cursor(value[1], value[2], value[3]) != cursor:
        raise ToolError("cursor is not a task_list cursor")
    return value[1], value[2], value[3]


def _operator_tz(tz: str | None) -> tuple[str, ZoneInfo | None]:
    """The operator's zone: explicit `tz`, else UA_OPERATOR_TZ, else the host's.

    Returns (display name, zone). A None zone means "the host's local zone",
    which `datetime.astimezone()` resolves without a name.
    """
    name = tz or config.operator_tz()
    if name is None:
        local = datetime.now().astimezone().tzinfo
        return (str(local) if local else "local"), None
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ToolError(f"tz {name!r} is not a known IANA timezone") from e


def _local_day_window(local_day: str, tz: str | None) -> dict:
    """UTC [start, end) of one calendar day in the operator's zone.

    Storage stays UTC (docs/task-lifecycle-contract.md). Only the question is
    local: "today" for an operator in UTC-5 ends at 05:00Z the next UTC day.
    """
    name, zone = _operator_tz(tz)
    if local_day == "today":
        day = datetime.now(zone).date() if zone else datetime.now().astimezone().date()
    else:
        try:
            if len(local_day) != 10:
                raise ValueError(local_day)
            day = date.fromisoformat(local_day)
        except ValueError as e:
            raise ToolError("local_day must be 'today' or YYYY-MM-DD") from e
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    end = start + timedelta(days=1)
    if zone is None:
        start, end = start.astimezone(), end.astimezone()
    return {
        "local_day": day.isoformat(),
        "tz": name,
        "start_utc": start.astimezone(UTC).isoformat(timespec="seconds"),
        "end_utc": end.astimezone(UTC).isoformat(timespec="seconds"),
    }


# julianday() keeps only milliseconds, so two instants inside one millisecond
# compare equal. Break that tie on the text: every stored stamp and every bound
# is UTC `+00:00` isoformat (fraction omitted when zero), under which string
# order is time order within one second.
_AT_OR_AFTER_SQL = (
    "(julianday({col}) > julianday(?) OR (julianday({col}) = julianday(?) AND {col} >= ?))"
)


def _since_utc(value: str, arg: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as e:
        raise ToolError(f"{arg} must be an ISO timestamp with a UTC offset") from e
    if parsed.tzinfo is None:
        raise ToolError(
            f"{arg} must include a UTC offset (storage is UTC; a naive time is ambiguous)"
        )
    return parsed.astimezone(UTC).isoformat()


def _attach_parked(conn, tasks: list[dict]) -> None:
    """Add the `parked` object (kind, reason, by, at, root) to each listed task."""
    if not tasks:
        return
    from ..queue_closure import describe_parked

    notes_by_id = {
        r["id"]: r["notes"]
        for r in conn.execute(
            f"SELECT id, notes FROM tasks WHERE id IN ({','.join('?' for _ in tasks)})",
            [t["id"] for t in tasks],
        )
    }
    for task in tasks:
        task["parked"] = describe_parked(notes_by_id.get(task["id"]))


def task_list(
    status: str = "open",
    parent_id: int | None = None,
    notes_contain: str | None = None,
    priority: str | None = None,
    limit: int = 100,
    source_host: str | None = None,
    archived: bool = False,
    fields: str = "full",
    project: str | None = None,
    domain: str | None = None,
    lane: str | None = None,
    office: str | None = None,
    role: str | None = None,
    due_before: str | None = None,
    completed_after: str | None = None,
    frontier: bool = False,
    stale_claims: bool = False,
    cursor: str | None = None,
    created_since: str | None = None,
    updated_since: str | None = None,
    local_day: str | None = None,
    tz: str | None = None,
    query: str | None = None,
    parked: bool = False,
) -> dict:
    """List tasks by status (open|doing|waiting_review|blocked|done|cancelled|all), sorted by priority then age.

    Optional filters:
      - `query`: ranked mode (task #1419). Switches from the default
        priority-then-age listing to relevance ranking over the SAME filters
        (`status`, `project`, `domain`, `lane`, `priority`, `source_host`,
        `archived`, and the rest) using a task_fts5 index plus semantic
        similarity plus recency decay -- the same combined-score shape
        memory_search uses, minus the two terms (salience, access) that have
        no equivalent on a task row; see docs/task-search.md. Each returned
        task carries `combined_score` and `explanation` (same breakdown shape
        memory_search returns). Mutually exclusive with `notes_contain` --
        substring search stays exactly as it was; `query` is a new, separate
        mode, not a replacement. `cursor` is not supported in ranked mode
        (a fixed top-`limit` list, capped at 100, is returned instead;
        `truncated` reports further candidates; `next_cursor` is `None`).
      - `parent_id`: restrict to one task's direct subtasks. Each listed row
        also carries `lineage`: ancestor ids from the root down to the
        immediate parent (empty for a top-level task). Walk `parent_id`
        plus `lineage` to render a multi-level subtree. A `[project-spec]`
        root also carries `rollup`: derived status from its entire descendant
        subtree (complete only when no descendant is open/doing/
        waiting_review/blocked).
      - `notes_contain`: case-insensitive substring match against task notes
        (e.g. '[waiting-for: review]' to find tasks awaiting review).
      - `priority`: restrict to tasks with high|med|low priority.
      - `source_host`: restrict to tasks matching origin machine (case-insensitive).
      - `archived`: when false (default), exclude explicitly archived tasks;
        when true, return only explicitly archived tasks.
      - `parked`: the parked view (task #1848). True returns only unfinished
        tasks that were parked or folded (archived, not done/cancelled), each
        with a `parked` object {kind: parked|folded|null, reason, by, at, root}
        read from the recorded notes line. `status` still filters (pass "all"
        for every parked status). Exclusive with `archived`. Parking is not
        cancelling; `queue_unpark` (coordination_manage) reverses it.
      - `project`: restrict to one project stream
        (configured project stream ids). Omitted (the default)
        returns tasks across every project — the explicit cross-project view
        for the queue steward/operator; a project-scoped session should pass this on every
        call to stay inside its own queue.
      - `domain`: restrict to one portfolio bucket (personal|professional|hobby).
        Omitted (the default) returns tasks across every domain.
      - `lane`: restrict to one actor lane (default|dispatch-ready|operator-only|
        interactive-only|tracking|laptop, or a registered agent label). Omitted returns tasks
        across every lane.
      - `office` / `role`: restrict to tasks claimed by an office member or
        descended from one of that office's owned epic tasks. `role` is an
        alias for the office id/name.
      - `due_before`: restrict to tasks with `due_at` set and <= this ISO
        date/datetime (string comparison — pass the same precision `due_at`
        was set with). The due/overdue-commitments query: pass today's date
        to find everything due today or already overdue. Tasks with no
        `due_at` never match.
      - `completed_after`: restrict to tasks with `completed_at` set and >=
        this ISO date/datetime (string comparison, same precision rule as
        `due_before`). Use with `status="done"` to query a completion window
        directly in SQL rather than filtering a priority-ordered, limit-capped
        result in Python — the latter silently drops rows completed in the
        window once more than `limit` tasks are done overall. Tasks with no
        `completed_at` never match.
      - `frontier`: restrict to active tasks whose explicit prerequisites are
        all `done` or `cancelled`.
      - `stale_claims`: restrict to `doing` tasks with an expired claim lease.
      - `created_since` / `updated_since`: restrict to tasks created / last
        updated at or after this ISO timestamp. It must carry a UTC offset;
        a naive time is refused as ambiguous. Compared as instants, not strings.
      - `local_day`: 'today' or YYYY-MM-DD in the operator's timezone (`tz`,
        else UA_OPERATOR_TZ, else the host zone). Matches tasks created,
        completed, or last updated within that local calendar day, which is
        the "what happened today" query. Storage stays UTC; only the window
        is local, so late-evening work that is already the next UTC day is
        kept. The result then carries `window` with the UTC bounds used.

    Each row includes `urgency` (`now`|`next`|`later`|null) parsed from an
    exact, case-insensitive `[urgency: value]` note marker and `urgency_rank`
    (0|1|2). Multiple markers use the most urgent; an absent marker ranks
    alongside `next`. Consumers can sort by urgency_rank, priority, then age.
    The task_list API itself retains its established priority-then-age order.

    `fields` is "full" (default: every column including notes bodies) or
    "compact": notes bodies are dropped and replaced by `notes_chars` (length
    of the notes), `claimed_by` (authoritative database column), and
    booleans for the queue marker categories (`waiting_for_review`, `laptop_lane`,
    and `notes_archived`).
    Use compact for queue syncs — notes dominate payload size; fetch one
    task's notes afterwards with a filtered full call when needed.

    Pagination: results are ordered by priority, then `created_at`, then `id`.
    `truncated` is true when more matching rows exist beyond this page (including
    when `limit` was clamped to its maximum: 500 for full, 100000 for compact);
    `next_cursor` then resumes exactly after the last row returned. Pass it back
    as `cursor` with the SAME filters to read the next page. The cursor is a
    keyset position, so rows added or removed between pages neither duplicate
    nor shift the rows already read. A row whose priority changes mid-walk can
    move across the page boundary. Never treat a `truncated` result as the
    whole queue.

    Returns {tasks: [...], truncated: bool, next_cursor: str | None, limit: int}.
    """
    if query is not None and notes_contain is not None:
        raise ToolError(
            "query (ranked search) and notes_contain (substring search) are mutually "
            "exclusive -- pick one mode"
        )
    if query is not None and cursor is not None:
        raise ToolError("query (ranked mode) does not support cursor pagination")
    if status != "all" and status not in STATUSES:
        raise ToolError(f"status must be one of {STATUSES + ('all',)}")
    if priority is not None and priority not in PRIORITIES:
        raise ToolError(f"priority must be one of {PRIORITIES}")
    if fields not in ("full", "compact"):
        raise ToolError("fields must be 'full' or 'compact'")
    if project is not None and project not in PROJECTS:
        raise ToolError(f"project must be one of {PROJECTS}")
    if domain is not None and domain not in DOMAINS:
        raise ToolError(f"domain must be one of {DOMAINS}")
    if lane is not None:
        lane = _validate_lane(lane)
    if office is not None and role is not None:
        office_row = org_registry.find_office(office)
        role_row = org_registry.find_office(role)
        if office_row is None or role_row is None or office_row["id"] != role_row["id"]:
            raise ToolError("office and role filters must resolve to the same office")
    selected_office = office if office is not None else role
    office_row = org_registry.find_office(selected_office) if selected_office else None
    if selected_office and office_row is None:
        raise ToolError(f"unknown office or role {selected_office!r}")
    # Tenant fence (task #1814): every listing, substring or ranked, sees only
    # the caller's tenant. `IS` keeps the untenanted private hub on NULL rows.
    where, params = [_TENANT_SQL], [_tenant()]
    if project is not None:
        where.append("project=?")
        params.append(project)
    if domain is not None:
        where.append("domain=?")
        params.append(domain)
    if lane is not None:
        if lane == "operator-only":
            where.append("lane IN (?,?)")
            params.extend((lane, config.LEGACY_OPERATOR_LANE))
        else:
            where.append("lane=?")
            params.append(lane)
    if office_row is not None:
        office_labels = office_row["members"]
        epic_ids = office_row["owned_epics"]
        office_terms: list[str] = []
        if office_labels:
            office_terms.append(f"claimed_by IN ({','.join('?' for _ in office_labels)})")
            params.extend(office_labels)
        if epic_ids:
            placeholders = ",".join("?" for _ in epic_ids)
            office_terms.append(
                "id IN (WITH RECURSIVE descendants(id) AS ("
                f"SELECT id FROM tasks WHERE id IN ({placeholders}) AND {_TENANT_SQL} "
                "UNION ALL SELECT child.id FROM tasks AS child "
                "JOIN descendants AS parent ON child.parent_id=parent.id "
                f"WHERE child.{_TENANT_SQL}) "
                "SELECT id FROM descendants)"
            )
            params.extend([*epic_ids, _tenant(), _tenant()])
        where.append(f"({' OR '.join(office_terms)})" if office_terms else "0")
    if due_before is not None:
        where.append("due_at IS NOT NULL AND due_at <= ?")
        params.append(due_before)
    if completed_after is not None:
        where.append("completed_at IS NOT NULL AND completed_at >= ?")
        params.append(completed_after)
    if frontier:
        where.append("status NOT IN ('done', 'cancelled')")
        where.append(
            "NOT EXISTS ("
            "SELECT 1 FROM task_dependencies dependency "
            "JOIN tasks prerequisite ON prerequisite.id=dependency.depends_on_task_id "
            "WHERE dependency.task_id=tasks.id "
            "AND prerequisite.status NOT IN ('done', 'cancelled')"
            ")"
        )
    if stale_claims:
        where.append("status='doing' AND lock_expires_at IS NOT NULL AND lock_expires_at <= ?")
        params.append(storage.now())
    if status != "all":
        where.append("status=?")
        params.append(status)
    if parent_id is not None:
        # Fenced (task #1810): another tenant's parent lists like a missing one.
        where.append(f"parent_id=? AND {_TENANT_SQL}")
        params.extend([parent_id, _tenant()])
    if notes_contain is not None:
        # A substring match: %, _ and \ in the needle are literal characters.
        literal = notes_contain.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("notes LIKE ? ESCAPE '\\'")
        params.append(f"%{literal}%")
    if priority is not None:
        where.append("priority=?")
        params.append(priority)
    if source_host is not None:
        where.append("lower(source_host) = lower(?)")
        params.append(source_host)
    if parked and archived:
        raise ToolError("parked and archived are mutually exclusive views")
    if parked:
        where.append("archived_at IS NOT NULL AND status NOT IN ('done', 'cancelled')")
    else:
        where.append("archived_at IS NOT NULL" if archived else "archived_at IS NULL")
    if created_since is not None:
        bound = _since_utc(created_since, "created_since")
        where.append(_AT_OR_AFTER_SQL.format(col="created_at"))
        params.extend([bound, bound, bound])
    if updated_since is not None:
        bound = _since_utc(updated_since, "updated_since")
        where.append(_AT_OR_AFTER_SQL.format(col="updated_at"))
        params.extend([bound, bound, bound])
    window = _local_day_window(local_day, tz) if local_day is not None else None
    if window is not None:
        span = "(julianday({col}) >= julianday(?) AND julianday({col}) < julianday(?))"
        where.append(
            "("
            + " OR ".join(span.format(col=c) for c in ("created_at", "updated_at", "completed_at"))
            + ")"
        )
        params.extend([window["start_utc"], window["end_utc"]] * 3)

    if query is not None:
        # Ranked mode (task #1419): `where`/`params` at this point already
        # carry every filter above (status, parent_id, priority, source_host,
        # archived, project/domain/lane/office/role, due_before,
        # completed_after, frontier, stale_claims, created_since/
        # updated_since, local_day) -- notes_contain and cursor are the only
        # two skipped, and both are rejected up front when `query` is set.
        # This is deliberately the SAME where/params the substring path below
        # builds on, so ranked and substring search can never silently diverge
        # on what counts as a match against those filters.
        where_sql = " AND ".join(where) if where else ""
        effective_limit = max(1, min(limit, 100))
        ranked = task_search.ranked_task_search(
            query, where_sql, list(params), limit=effective_limit
        )
        conn = storage.connect()
        try:
            ordered_ids = [item["_row"]["id"] for item in ranked["tasks"]]
            score_by_id = {item["_row"]["id"]: item["combined_score"] for item in ranked["tasks"]}
            explanation_by_id = {
                item["_row"]["id"]: item["explanation"] for item in ranked["tasks"]
            }
            if fields == "compact" and ordered_ids:
                # Reuse the exact same compact projection the substring path
                # uses (_compact_select_columns) rather than re-deriving the
                # waiting_for_review/laptop_lane/notes_archived flags here --
                # ranked_task_search's own `SELECT *` has no way to produce
                # them (they are computed in SQL, not stored columns), so
                # re-fetch this specific id set through that projection.
                select_list = _compact_select_columns(conn)
                placeholders = ",".join("?" for _ in ordered_ids)
                compact_rows = conn.execute(
                    f"SELECT {select_list} FROM tasks WHERE id IN ({placeholders})",
                    ordered_ids,
                ).fetchall()
                rows_by_id = {r["id"]: r for r in compact_rows}
                tasks_out = []
                for tid in ordered_ids:
                    row = rows_by_id.get(tid)
                    if row is None:
                        continue
                    trow = _compact_row(row)
                    trow["combined_score"] = score_by_id[tid]
                    trow["explanation"] = explanation_by_id[tid]
                    tasks_out.append(trow)
            else:
                tasks_out = []
                for item in ranked["tasks"]:
                    trow = _row(item["_row"])
                    trow["combined_score"] = item["combined_score"]
                    trow["explanation"] = item["explanation"]
                    tasks_out.append(trow)
            tasks_out = _attach_project_rollup(conn, _attach_lineage(conn, tasks_out))
            if parked:
                _attach_parked(conn, tasks_out)
        finally:
            conn.close()
        tasks_out.sort(key=lambda t: -t["combined_score"])
        result = {
            "tasks": tasks_out,
            "truncated": ranked["truncated"],
            "next_cursor": None,
            "limit": effective_limit,
            "ranked": True,
        }
        if window is not None:
            result["window"] = window
        return result

    if cursor is not None:
        rank, created_at, after_id = _decode_task_cursor(cursor)
        where.append(
            f"({_PRIORITY_RANK_SQL} > ? OR ({_PRIORITY_RANK_SQL} = ? AND "
            "(created_at > ? OR (created_at = ? AND id > ?))))"
        )
        params.extend([rank, rank, created_at, created_at, after_id])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    max_limit = 500 if fields == "full" else 100_000
    effective_limit = max(1, min(limit, max_limit))
    # One extra row answers "is there more?" without a second COUNT query.
    params.append(effective_limit + 1)
    conn = storage.connect()
    try:
        # Compact mode projects the notes column away in SQL rather than reading
        # every body into Python and discarding it. Notes dominate a task row —
        # the queue's are kilobyte-scale delegation specs — so a compact listing
        # was paying full IO for a payload it exists to avoid.
        select_list = _compact_select_columns(conn) if fields == "compact" else "*"
        rows = conn.execute(
            f"SELECT {select_list}, {_PRIORITY_RANK_SQL} AS _page_rank FROM tasks {clause} "
            f"ORDER BY _page_rank, created_at, id LIMIT ?",
            params,
        ).fetchall()
        truncated = len(rows) > effective_limit
        rows = rows[:effective_limit]
        next_cursor = (
            _encode_task_cursor(rows[-1]["_page_rank"], rows[-1]["created_at"], rows[-1]["id"])
            if truncated
            else None
        )
        if fields == "compact":
            tasks = [_compact_row(r) for r in rows]
        else:
            tasks = [_row(r) for r in rows]
        for task in tasks:
            task.pop("_page_rank", None)
        if parked:
            _attach_parked(conn, tasks)
        result = {
            "tasks": _attach_project_rollup(conn, _attach_lineage(conn, tasks)),
            "truncated": truncated,
            "next_cursor": next_cursor,
            "limit": effective_limit,
        }
        if window is not None:
            result["window"] = window
        return result
    finally:
        conn.close()


def task_list_all(max_pages: int = 1000, **kwargs) -> list[dict]:
    """Every row ``task_list`` would return, following ``next_cursor`` (task #1430).

    For in-process callers that need the whole result, such as a lookup by id,
    rather than the first page. Not a registered tool. Raises instead of
    returning a partial list when the page guard trips.
    """
    kwargs.setdefault("limit", 500)
    rows: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        page = task_list(**kwargs, **({"cursor": cursor} if cursor else {}))
        rows.extend(page["tasks"])
        cursor = page.get("next_cursor")
        if not page.get("truncated") or not cursor:
            return rows
    raise ToolError(f"task_list_all stopped after {max_pages} pages")


#: Most descendants ``task_subtree`` visits; a larger tree is refused, never cut.
SUBTREE_MAX = 5_000
#: Parent ids per child query (SQLite's bound-parameter limit is far above it).
_SUBTREE_BATCH = 500

_SUBTREE_COLUMNS = (
    "id, parent_id, title, status, priority, project, domain, lane, claimed_by, "
    "evidence_ref, created_at, updated_at, completed_at, archived_at"
)


class SubtreeRefused(ToolError):
    """A subtree read that cannot answer a complete tree (task #2516).

    ``code`` is ``subtree_too_large`` (more than ``SUBTREE_MAX`` descendants) or
    ``subtree_cycle`` (a parent chain loops back into the tree).
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def task_subtree(task_id: int) -> dict:
    """Every descendant of a task, at any depth and in any status (task #2516).

    Done, cancelled and archived descendants are included, so a board that
    renders the tree does not omit old completed work. The walk is breadth
    first, one level of children per query, tenant-fenced at every level
    (another tenant's root reads as missing). One node budget counts every
    node visited at any depth: past ``SUBTREE_MAX`` descendants the read stops
    and raises ``SubtreeRefused`` (``subtree_too_large``), and a child already
    visited means the parent chain loops (``subtree_cycle``). It never answers
    a partial tree. Descendants are returned depth first by id. Not a
    registered tool.
    """
    budget = SUBTREE_MAX
    conn = storage.connect()
    try:
        root = _visible_task(conn, task_id, _SUBTREE_COLUMNS)
        if root is None:
            raise ToolError(f"task #{task_id} not found")
        visited = {task_id}
        children: dict[int, list[dict]] = {}
        frontier = [task_id]
        while frontier:
            next_frontier: list[int] = []
            for start in range(0, len(frontier), _SUBTREE_BATCH):
                batch = frontier[start : start + _SUBTREE_BATCH]
                remaining = budget - (len(visited) - 1)
                rows = conn.execute(
                    f"SELECT {_SUBTREE_COLUMNS} FROM tasks WHERE parent_id IN "
                    f"({', '.join('?' for _ in batch)}) AND {_TENANT_SQL} "
                    "ORDER BY id LIMIT ?",
                    (*batch, _tenant(), remaining + 1),
                ).fetchall()
                for row in rows:
                    child = dict(row)
                    if child["id"] in visited:
                        raise SubtreeRefused(
                            f"task #{task_id}'s subtree loops back to task #{child['id']}; "
                            "fix its parent chain",
                            "subtree_cycle",
                        )
                    visited.add(child["id"])
                    if len(visited) - 1 > budget:
                        raise SubtreeRefused(
                            f"task #{task_id} has more than {budget} descendants; read a "
                            "subtree lower down",
                            "subtree_too_large",
                        )
                    children.setdefault(child["parent_id"], []).append(child)
                    next_frontier.append(child["id"])
            frontier = next_frontier
    finally:
        conn.close()
    descendants: list[dict] = []
    stack = [(child, 1) for child in reversed(children.get(task_id, []))]
    while stack:
        node, depth = stack.pop()
        descendants.append({**node, "depth": depth})
        stack.extend((child, depth + 1) for child in reversed(children.get(node["id"], [])))
    return {
        "root": dict(root),
        "descendants": descendants,
        "count": len(descendants),
        "complete": True,
    }


def _delete_task_branch(branch: str | None, repo: Path | None = None) -> None:
    """Delete the branch a released worktree was sitting on. Best-effort.

    `git worktree remove` leaves the branch behind, so a task that created a
    worktree left a permanent ref even after the directory was gone — and
    `.worktrees/` is gitignored, so `git status` never showed either. Seven
    such branches, none of them ever committed to, accumulated in the hub clone
    in a single day (task #194).

    Only ever called once the worktree has been established as disposable, so
    the force delete is discarding a ref whose content is already in the base
    branch. Failure is logged, not raised: a leftover branch is untidy, whereas
    a raising bookkeeping call loses the status update that prompted it.

    `repo` is the repo the branch actually lives in — the workspace repo by
    default, or a project repo for a project-sourced worktree (task #467 /
    PR #203 review): the branch was created there by
    `_git_worktree_add_from_known_project_repo`, not in the workspace, so
    deleting it from the workspace repo is always a no-op for that case.
    """
    if not branch:
        return
    from .worktree_policy import _git

    res = _git(repo or config.workspace_root(), ["branch", "-D", branch])
    if res is None or res.returncode != 0:
        detail = res.stderr.strip() if res is not None else "git could not be run"
        logger.info("kept branch %r after releasing its worktree: %s", branch, detail)


def _release_task_worktree(
    worktree_path: str | None,
    branch: str | None = None,
    project: str | None = None,
    notes: str | None = None,
) -> str | None:
    """Remove a deleted task's worktree when it holds nothing. Returns its status.

    A task row is what makes a worktree identifiable: once it is gone,
    `cleanup_stale_worktrees` can only classify the directory `orphaned_task`,
    and nothing else knows what it was for. Deleting the row without releasing
    the worktree is how `.worktrees/task-183` and `task-184` outlived the
    probe tasks that created them (task #187) — both were real rows, claimed
    into 'doing' (which auto-creates the worktree), completed, then deleted.

    Never removes a worktree with uncommitted or unmerged work: deletion of a
    bookkeeping row must not be able to destroy code. Such a worktree is left
    for `cleanup_stale_worktrees` to surface.

    `project`/`notes` identify a project-sourced worktree the same way
    `_ensure_task_worktree` does when creating one (`_worktree_source_repo`).
    Paths are recorded as absolute paths under the project checkout's
    `.worktrees/`; a relative path is a pre-#2041 workspace-sourced row.
    """
    if not worktree_path:
        return None
    from .worktree_policy import _worktree_is_disposable

    raw_path = Path(worktree_path)
    if raw_path.is_absolute():
        abs_path = raw_path
    else:
        # Release runs after the durable commit and must never fail the call
        # (task #1857). A workspace-denied identity (explicit
        # UA_AGENT_WORKSPACES DENY) cannot resolve a workspace-relative path;
        # leave the checkout for `cleanup_stale_worktrees` to reconcile.
        try:
            abs_path = config.workspace_root() / raw_path
        except ToolError as e:
            logger.info(
                "kept worktree %s after task release: workspace denied (%s)", worktree_path, e
            )
            return "retained:workspace_denied"
    if not abs_path.is_dir():
        return "already_gone"

    disposable, why = _worktree_is_disposable(abs_path)
    if not disposable:
        logger.info("kept worktree %s after task delete: %s", worktree_path, why)
        return f"retained:{why}"

    # A relative path is a pre-#2041 row cut from the workspace.
    project_repo = _worktree_source_repo(project) if raw_path.is_absolute() else None
    try:
        if project_repo is not None:
            _git_worktree_remove_from_known_project_repo(abs_path, project_repo, force=True)
        else:
            from .git import git_worktree_remove

            git_worktree_remove(path=worktree_path, force=True)
    except Exception as e:
        logger.warning("could not remove worktree %s after task delete: %s", worktree_path, e)
        return "remove_failed"

    _delete_task_branch(branch, project_repo)
    return "removed"


def task_delete(id: int) -> dict:
    """Delete a task and its subtasks.

    Returns {deleted, children_deleted, worktrees}. `worktrees` maps each
    released checkout — the task's own and, since the subtree cascade-deletes,
    every descendant's too — to what happened to it: "removed", "already_gone",
    or "retained:<reason>" when it still holds work.
    """
    from ..safety import check_prod_action_gate

    check_prod_action_gate("task_delete", {"task_id": id})
    conn = storage.connect()
    try:
        # One write transaction: the fenced subtree read, the cross-tenant
        # detach and the delete below must see the same rows.
        conn.execute("BEGIN IMMEDIATE")
        subtree = _subtree_ids(conn, id)
        if not subtree:
            return {"deleted": False, "children_deleted": 0, "worktrees": {}}
        workflow_roles.assert_tasks_deletable(conn, subtree)
        children = len(subtree) - 1
        # `parent_id ... ON DELETE CASCADE` ignores tenants. A child of another
        # tenant under any row of this subtree (only possible in data written
        # before the #1810 fence) is detached to a root first, so the cascade
        # cannot delete it. Every subtree row is in the caller's tenant.
        placeholders = ",".join("?" * len(subtree))
        foreign_children = conn.execute(
            f"SELECT id, updated_at FROM tasks WHERE parent_id IN ({placeholders}) "
            f"AND NOT ({_TENANT_SQL})",
            [*subtree, _tenant()],
        ).fetchall()
        detached = [int(r["id"]) for r in foreign_children]
        for child in foreign_children:
            # A strictly newer optimistic revision, as task_update writes it, so
            # a stale expected_updated_at on the detached task is refused.
            conn.execute(
                "UPDATE tasks SET parent_id=NULL, updated_at=? WHERE id=?",
                (_next_task_revision(child["updated_at"]), child["id"]),
            )
        # Subtree cascade (parent_id ... ON DELETE CASCADE), so descendant
        # worktrees have to be collected before the row goes. `branch` rides
        # along so a released worktree's branch is actually deleted too (task
        # #315: omitting it here left task_delete's branches behind even
        # though task_update(status="done")'s equivalent path deletes them).
        rows = conn.execute(
            f"SELECT worktree_path, branch, project, notes FROM tasks WHERE id IN ({placeholders})",
            subtree,
        ).fetchall()
        worktree_branches = [
            (r["worktree_path"], r["branch"], r["project"], r["notes"])
            for r in rows
            if r["worktree_path"]
        ]
        cur = conn.execute("DELETE FROM tasks WHERE id=?", (id,))
        revoked_sessions: list[str] = []
        if cur.rowcount > 0:
            # Heartbeat usage of the subtree's open runs has no task left.
            run_records.drop_heartbeats_for_tasks(conn, subtree)
            # Sprint links and next-run settings of deleted tasks (task #1735).
            operating_policy.forget_tasks(conn, subtree)
            # Sessions bound to a deleted task lose hub access (task #1744).
            from .. import session_credentials

            revoked_sessions = session_credentials.revoke_for_tasks(conn, subtree, "task_deleted")
        conn.commit()
        if revoked_sessions:
            from .. import session_credentials

            session_credentials.emit_revoked(
                revoked_sessions, "task_deleted", actor=config.verified_agent_identity()
            )
        schedule_sync_to_vault()
        deleted = cur.rowcount > 0
        worktrees: dict[str, str] = {}
        if deleted:
            for path, branch, project, notes in worktree_branches:
                status = _release_task_worktree(path, branch, project=project, notes=notes)
                if status:
                    worktrees[path] = status
        if deleted:
            task_changed(id)
            for child_id in detached:
                task_changed(child_id)
        return {
            "deleted": deleted,
            "children_deleted": children if deleted else 0,
            "worktrees": worktrees,
        }
    finally:
        conn.close()


_MAX_CLAIM_LEASE_SECONDS = 7 * 86400


def task_claim(
    id: int,
    agent: str,
    timeout_seconds: int = config.TASK_CLAIM_DEFAULT_LEASE_SECONDS,
    expected_project: str | None = None,
    expected_lane: str | None = None,
    expected_run_id: str | None = None,
    run_usage: dict | None = None,
    usage_only: bool = False,
) -> dict:
    """Atomically claim/lock a task for an agent.

      A claim is a single conditional UPDATE, preventing two agents racing for
      the same task from both winning. Succeeds if the task is not terminal
      ('done' or 'cancelled') and is either unclaimed, claimed by the same
      agent, or its lock has expired.

      `expected_project` is a guard: when set, the claim is rejected unless the
      task actually belongs to that project — the check a project-scoped
      session should pass so it cannot claim another project's task by id.

      `expected_lane` scopes the claim to a queue lane (task #705). When set,
      the task's `lane` must be `default` or match `expected_lane`. When
      omitted, only tasks in the shared `default` lane are claimable.

      `expected_run_id` fences a renewal to the currently recorded run. A
      cancelled run cannot be renewed, regardless of this optional token.

      `run_usage` (task #1681, re-land of PR #866) is a heartbeat: on a live
      renewal by the agent already holding the task, it records the run's
      cumulative usage so far (same fields as `task_update`'s `run_usage`).
      Each heartbeat replaces the fields it reports. The open run shows the
      latest one, and its finish carries it unless the finishing `run_usage`
      reports the field. It is refused on a new claim or after the lease
      lapsed, where it could not belong to the run being renewed.

    Per-agent and per-lane WIP caps apply (UA_AGENT_WIP_MAX, with a per-label
    UA_AGENT_WIP_OVERRIDES; UA_LANE_WIP).
    Expired locks are excluded from WIP accounting. Soft lane caps return
    ``wip_warnings``; hard caps refuse the claim. A fleet-wide cap counts live
    claims across all agents (task #1838): at UA_FLEET_WIP_MAX a new claim
    warns, at UA_FLEET_WIP_HARD_MAX it is refused unless the task is
    high-priority or ``[urgency: now]`` (warned only). operator-only and
    interactive-only lanes are exempt.

    Budgets declared under ``budgets`` in policy.yaml apply too (task #1348):
    a ``block`` budget whose period spend is exhausted refuses a new claim; a
    budget past its warning threshold returns ``budget_warnings``. A lock
    renewal is never refused. A renewal carrying ``run_usage`` is judged
    against the budget with that usage counted (task #1917): past the warning
    threshold it returns ``budget_warnings``, and past a ``block`` limit it
    returns ``budget_stop``, the reason the runner should stop the run.

      `usage_only=True` records cumulative accounting on the immutable
      `expected_run_id` owned by the exact authenticated actor, including
      closed/expired runs. It changes no task or lease. Returns
      `{id, run_id, usage_only, usage}` plus budget warnings/stop.

      Updates status to 'doing', claimed_by, claimed_at, lock_expires_at, and
      appends 'claimed-by: {agent}' to task notes.

      A task held by someone else is not an error. The call returns
      `{"id", "claimed": False, "safe_wait": {"owner", "waiting_on", "until",
      "generation", "claimed_by"}}` and writes nothing, when another agent
      holds a live lease, or when no lease is live and a stored `next_owner`
      other than the caller has a declared wait or pending monitor still
      running. Always check for `safe_wait` before using the result as a
      claim. A call passing `expected_run_id` holds a fencing token and gets a
      ConflictError instead.

      Interrupted work (``blocked`` and unowned because the lease reaper found
      its owner's run without a recorded finish) is resumed by this call, unless
      a delegation to `agent` is pending or in flight for the task: that claim
      would execute queued work, and is refused until the task is reopened.

      A successor replaces live ownership only when the owner's latest run is
      recorded as ended without completing (abandoned, cancelled or released)
      and the owner has set no wait or monitor since. The result then carries
      `replaced: {predecessor, run_id, outcome, finished_at, generation}`, and
      an `agent.task.ownership` event records it.

      Returns the updated task dict, plus optional `wip_warnings` and
      `budget_warnings`. A durable claim with optional worktree setup skipped
      or unavailable includes `provisioning` (status and a bounded reason).
      Native execution remains subject to live admission and run ownership;
      `worktree_source_sha=None` never proves checkout provenance.
    """
    if not agent.strip():
        raise ToolError("agent label must not be empty")
    if not 1 <= timeout_seconds <= _MAX_CLAIM_LEASE_SECONDS:
        raise ToolError(
            f"timeout_seconds must be between 1 and {_MAX_CLAIM_LEASE_SECONDS} (7 days)"
        )
    agent = config.verify_write_identity(agent)
    if not agent:
        raise ToolError("agent label must not be empty")
    agent = agent.strip()
    heartbeat_usage = run_records.validate_usage(run_usage)

    if usage_only:
        if config.verified_agent_identity() != agent:
            raise ToolError("usage accounting requires the original authenticated actor")
        if expected_run_id is None or heartbeat_usage is None:
            raise ToolError("usage_only requires expected_run_id and run_usage")
        # The immutable claim binds actor + task + run; current ownership,
        # cancellation and lease state are irrelevant to historical accounting.
        conn = storage.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Tenant fence (task #1814): no usage lands on another tenant's task.
            if _visible_task(conn, id) is None:
                raise ToolError(f"no task with id {id}")
            tx = run_records.attach(conn)
            usage = run_records.record_accounting_usage_in_tx(
                tx,
                id,
                run_id=expected_run_id,
                agent=agent,
                usage=heartbeat_usage,
                expected_project=expected_project,
                expected_lane=expected_lane,
            )
            try:
                budget = budgets.evaluate_run(conn, agent, task_id=id)
            except budgets.BudgetConfigError:
                budget = None
            conn.commit()
            run_records.flush(tx)
        finally:
            conn.close()
        result = {"id": id, "run_id": expected_run_id, "usage_only": True, "usage": usage}
        if budget is not None:
            budgets.record_decision(budget)
            if budget.warnings:
                result["budget_warnings"] = budget.warnings
            if budget.stop:
                result["budget_stop"] = budget.stop
        return _with_verified_caller(result)

    from .. import trust_presets

    # Authority validation must not be swallowed by the best-effort recorder.
    trust_snapshot = trust_presets.snapshot(agent)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ts = storage.now()
        now_dt = datetime.now(UTC)
        lock_expires = (now_dt + timedelta(seconds=timeout_seconds)).isoformat(timespec="seconds")
        row = conn.execute(
            "SELECT status, notes, claimed_by, lock_expires_at, project, lane, updated_at, "
            "current_run_id, cancel_requested, worktree_path, evidence_ref, archived_at, "
            "priority, claimed_at, next_owner, waiting_on, ownership_generation, title "
            f"FROM tasks WHERE id=? AND {_TENANT_SQL}",
            (id, _tenant()),
        ).fetchone()
        # Tenant fence (task #1814): another tenant's id reads as missing.
        if row is None:
            raise ToolError(f"no task with id {id}")
        # Frozen work-proposal contract (task #2393): a task spawned from an
        # approved proposal item fails closed if its title, notes or scope
        # drifted from what the operator approved. Other tasks are untouched.
        from .. import work_proposals

        work_proposals.check_contract(
            {"id": id, "title": row["title"], "notes": row["notes"]}, conn=conn
        )
        prior_run_recorded = (
            conn.execute(
                "SELECT 1 FROM event_log WHERE channel=? AND json_extract(payload,'$.kind')=? "
                "AND json_extract(payload,'$.task_id')=? LIMIT 1",
                (run_records.CHANNEL, run_records.KIND_CLAIMED, id),
            ).fetchone()
            is not None
        )
        originator_agent = run_records.run_originator(conn, id, agent)
        originator_snapshot = trust_presets.snapshot(originator_agent) if originator_agent else None
        native_status = run_records.native_execution_status(agent, originator_agent)
        if (
            not native_status["native_execution_allowed"]
            and row["worktree_path"] is None
            and not config.agent_skips_task_worktree(agent)
        ):
            # Provision native git contexts before binding a restrictive preset.
            raise ToolError(native_status["native_execution_denial"])
        if expected_run_id is not None and row["current_run_id"] != expected_run_id:
            raise ConflictError(f"task {id}'s current run is not the expected {expected_run_id!r}")
        task_lane = config.canonical_task_lane(
            row["lane"] if "lane" in row.keys() else DEFAULT_LANE
        )
        _assert_claimable(conn, id, task_lane)
        if expected_project is not None and row["project"] != expected_project:
            raise ToolError(
                f"task {id} belongs to project {row['project']!r}, not {expected_project!r}"
            )

        if not _lane_claim_allowed(task_lane, expected_lane, agent):
            scope = expected_lane or DEFAULT_LANE
            raise ToolError(f"task {id} is in lane {task_lane!r}; claim scoped to {scope!r}")

        status = row["status"]
        if status in TERMINAL_STATUSES:
            raise ToolError(f"task {id} is already {status}")
        if row["archived_at"] is not None:
            raise ToolError(
                f"task {id} is parked: unpark it first (coordination_manage queue_unpark) "
                "so the queue and its notes agree before work starts"
            )
        if _is_queued_routine_task(conn, id):
            raise ToolError(f"task {id} is queued routine work; wait for scheduler release")

        current_claimed_by = row["claimed_by"]
        current_lock_expires = row["lock_expires_at"]
        if current_claimed_by == agent and status == "doing" and row["cancel_requested"]:
            raise ToolError(f"task {id}'s current run has a cancellation request")

        lock_active = task_ownership.lease_active(row, ts)
        replaced: dict | None = None

        if expected_run_id is None:
            # Live ownership is an answer, not a refusal, and takes nothing: no
            # second lease, no lock, no row write and no event (task #2388).
            wait_row = (
                None
                if lock_active or not row["next_owner"]
                else conn.execute(
                    "SELECT wait_until, monitor_at FROM task_liveness WHERE task_id=?", (id,)
                ).fetchone()
            )
            wait = task_ownership.safe_wait(
                row,
                agent,
                now=ts,
                wait_until=wait_row["wait_until"] if wait_row else None,
                monitor_at=wait_row["monitor_at"] if wait_row else None,
            )
            if wait is not None:
                acted = conn.execute(
                    "SELECT updated_at FROM task_liveness WHERE task_id=?", (id,)
                ).fetchone()
                replaced = task_ownership.predecessor_failure(
                    row,
                    run_records.latest_run_end(conn, id),
                    acted_at=acted["updated_at"] if acted else None,
                )
                if replaced is None:
                    conn.rollback()
                    return _with_verified_caller({"id": id, "claimed": False, "safe_wait": wait})
        # A fenced caller holds a run token: it is not asking to queue.
        if lock_active and current_claimed_by != agent and replaced is None:
            raise ConflictError(
                f"task {id} is already claimed/locked by {current_claimed_by!r} "
                f"until {current_lock_expires}"
            )
        if expected_run_id is not None and not lock_active:
            raise ConflictError(f"task {id}'s lease expired for run {expected_run_id!r}")

        # Lifecycle (task #1351): a paused or terminated agent cannot claim or
        # renew, so a running executor cannot keep extending its lease.
        blocked = agent_lifecycle.claim_block(conn, agent)
        if blocked:
            raise ToolError(blocked)
        # Subtree hold (task #1747): a held task takes no new claim, but a live
        # renewal of the claimant's own run is let through so it can finish. A
        # claim with no lease expiry is never a renewal here: it would renew
        # forever while held.
        # Interrupted work (task #2482) is held by the same check. A claim
        # resumes it only when it executes no queued delegation: the hub rows
        # that attribute this run to a delegation are what make it queued work.
        hold_events: list = []
        held = task_holds.claim_block(
            conn,
            id,
            renewal=current_claimed_by == agent
            and status == "doing"
            and lock_active
            and current_lock_expires is not None,
            pending=hold_events,
            resume=originator_agent is None,
        )
        if held:
            raise ToolError(held)

        # WIP caps: only a renewal of a still-live lease skips the cap check. A
        # lapsed lease may have let other claims fill the capacity meanwhile.
        live_renewal = current_claimed_by == agent and status == "doing" and lock_active
        if heartbeat_usage is not None and not live_renewal:
            raise ToolError(
                f"run_usage is a heartbeat: task {id} is not a live renewal of a run "
                f"{agent!r} holds, so the usage cannot be attributed"
            )
        wip_warnings = []
        budget = None
        if not live_renewal:
            wip_warnings = _enforce_wip_caps(conn, agent, task_lane, ts, id)
            wip_warnings += _enforce_fleet_wip(
                conn, task_lane, row["priority"], row["notes"], ts, id
            )
        if not live_renewal:
            # Budget hard stop (task #1348): read inside this write transaction
            # so simultaneous claims serialize against the same remaining budget.
            budget = budgets.evaluate_claim_or_config_error(conn, agent, task_id=id)
            if budget.blocked:
                conn.rollback()
                budgets.record_decision(budget)
                raise ToolError(budget.blocked)

        # Update notes to include claimed-by line if not already present
        notes = row["notes"] or ""
        claimed = _claimed_by(notes)
        if claimed != agent:
            if notes.strip():
                notes = notes.rstrip() + f"\n\nclaimed-by: {agent}"
            else:
                notes = f"claimed-by: {agent}"

        # Atomically update
        revision = _next_task_revision(row["updated_at"])
        # A fresh claim needs a distinct identity even within storage.now()'s
        # second. Keep it stable on renewal so hygiene markers and liveness
        # checks continue to refer to the original claim.
        claimed_at = (row["claimed_at"] or revision) if live_renewal else revision
        next_owner, waiting_on, generation = task_ownership.after_claim(
            row, agent, renewal=live_renewal
        )
        cur = conn.execute(
            "UPDATE tasks "
            "SET status='doing', claimed_by=?, claimed_at=?, lock_expires_at=?, notes=?, "
            "updated_at=?, next_owner=?, waiting_on=?, ownership_generation=? "
            f"WHERE id=? AND {_TENANT_SQL} AND status NOT IN ('done','cancelled') "
            "AND (claimed_by IS NULL OR claimed_by=? OR lock_expires_at <= ? "
            "OR ownership_generation=?)",
            (
                agent,
                claimed_at,
                lock_expires,
                notes,
                revision,
                next_owner,
                waiting_on,
                generation,
                id,
                _tenant(),
                agent,
                ts,
                # Generations start at 0, so -1 matches no row: only a replacement
                # may pass a live lease, and only of the ownership it read.
                replaced["generation"] if replaced is not None else -1,
            ),
        )
        if cur.rowcount == 0:
            raise ConflictError(f"lost race to claim task {id}")
        replaced_event = None
        if replaced is not None:
            _, _, replaced_event = events.record_event_once_on_connection(
                conn,
                task_ownership.CHANNEL,
                {"kind": "replaced", "task_id": id, "successor": agent, **replaced},
                originator=agent,
            )

        # A re-claim by the agent that already holds the task is a lock renewal,
        # not a new claim, and must not be recorded as one. The executor daemon
        # renews on every poll tick: task #149 logged 138 identical claim events
        # in 22 minutes (one per 5s), which flooded agent_events and pinned the
        # task_reassignment_thrash anomaly rule on — the rule counts re-entries
        # into 'doing', so a renewing agent reads as churn across claims.
        # A lapsed lease is a new claim even for the same agent: a previous
        # executor may still be running with the old run token.
        is_renewal = live_renewal

        # Self-pickup run record (task #1436), made atomic with the task claim
        # above (task #1525): recorded in this same transaction rather than
        # best-effort after commit, so a crash between the two can no longer
        # leave a claimed task with an unopened run, or vice versa. An initial
        # unrecorded claim remains best-effort. A takeover of a bound run must
        # create its new fencing token or roll back the entire claim.
        run_tx = run_records.attach(conn)
        recorded_run_id = run_records.safe_in_tx(
            run_records.record_claim_in_tx,
            run_tx,
            id,
            agent=agent,
            # The run ledger retains its existing second-resolution event
            # timestamp; the task row uses the precise claim identity above.
            claimed_at=ts,
            lock_expires_at=lock_expires,
            lane=task_lane,
            project=row["project"],
            renewal=is_renewal,
            revision=revision,
            prior_status=status,
            trust_snapshot=trust_snapshot,
            originator_snapshot=originator_snapshot,
            originator_agent=originator_agent,
        )
        if (
            trust_snapshot is not None or originator_snapshot is not None
        ) and recorded_run_id is None:
            raise ToolError(f"task {id}'s trust evidence recording failed; claim rolled back")
        if not is_renewal and row["current_run_id"] is not None and recorded_run_id is None:
            raise ToolError(f"task {id}'s run recording failed; claim rolled back")
        if heartbeat_usage is not None and recorded_run_id is not None:
            # Best effort like the recorder itself: a usage write must never
            # refuse a lease renewal.
            run_records.safe_in_tx(
                run_records.record_heartbeat_usage_in_tx,
                run_tx,
                id,
                run_id=recorded_run_id,
                agent=agent,
                usage=heartbeat_usage,
            )
        if live_renewal:
            # Re-check even when usage arrived by durable replay before this
            # renewal. A retry must not escape a stop by omitting run_usage.
            try:
                budget = budgets.evaluate_run(conn, agent, task_id=id)
            except budgets.BudgetConfigError:
                budget = None

        captured_run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id=?", (id,)
        ).fetchone()[0]
        new_run = captured_run_id is not None and captured_run_id != row["current_run_id"]
        isolate_run = new_run and (
            prior_run_recorded
            or row["worktree_path"] is not None
            or row["current_run_id"] is not None
        )
        if (
            isolate_run
            and not config.agent_skips_task_worktree(agent)
            and not native_status["native_execution_allowed"]
        ):
            raise ToolError(native_status["native_execution_denial"])
        if isolate_run:
            historical_notes = _historical_delivery_notes(
                notes, row["evidence_ref"], row["current_run_id"] or "closed run"
            )
            conn.execute(
                "UPDATE tasks SET notes=?, evidence_ref=NULL, worktree_path=CASE WHEN ? THEN NULL ELSE worktree_path END, "
                "branch=CASE WHEN ? THEN NULL ELSE branch END WHERE id=?",
                (historical_notes, isolate_run, isolate_run, id),
            )
        conn.commit()
        schedule_sync_to_vault()
        run_records.flush(run_tx)
        task_holds.publish(hold_events)
        if replaced_event is not None:
            events.publish(replaced_event)
        if not is_renewal:
            try:
                storage.record_agent_event(
                    "task",
                    ref_id=str(id),
                    agent=agent,
                    summary="claimed task: status open/doing->doing",
                )
            except Exception:
                pass

        updated_row = _assert_worktree_authority(conn, id, captured_run_id, agent)
        workspace_denied = isinstance(config.workspace_root_var.get(), config.WorkspaceDenied)
        provisioning = None
        if workspace_denied:
            # The claim is already durable. DENY forbids optional hub Git/file
            # work, not an otherwise admitted agent's native execution.
            provisioning = {"status": "native-required", "reason": "workspace_denied"}
        elif (
            updated_row["worktree_path"] is None
            and not config.agent_skips_task_worktree(agent)
            and _worktree_source_repo(updated_row["project"]) is None
        ):
            # Task #2041: no product checkout resolves; never cut one from the cockpit.
            provisioning = {"status": "skipped", "reason": "no_product_repo"}
        elif updated_row["worktree_path"] is None and not config.agent_skips_task_worktree(agent):
            try:
                _ensure_task_worktree(
                    id,
                    updated_row["title"],
                    conn,
                    project=updated_row["project"],
                    notes=updated_row["notes"],
                    expected_run_id=captured_run_id,
                    expected_agent=agent,
                    isolate_run=isolate_run,
                )
            except (OSError, subprocess.SubprocessError):
                # Only optional I/O failure is recoverable. Authority/policy
                # ToolErrors still propagate and never become successful claims.
                provisioning = {"status": "unavailable", "reason": "worktree_unavailable"}
            updated_row = _assert_worktree_authority(conn, id, captured_run_id, agent)
            if updated_row["worktree_path"] is None:
                provisioning = {"status": "unavailable", "reason": "worktree_unavailable"}
        result = _row(updated_row)
        if workspace_denied:
            result["worktree_source_sha"] = None
        else:
            try:
                result["worktree_source_sha"] = _task_worktree_source_sha(result)
            except (OSError, subprocess.SubprocessError):
                result["worktree_source_sha"] = None
                provisioning = {"status": "ready", "reason": "provenance_unavailable"}
            if result["worktree_path"] and result["worktree_source_sha"] is None:
                provisioning = {"status": "ready", "reason": "provenance_unavailable"}
        originator_agent = run_records.run_originator(conn, id, agent)
        native_status = run_records.native_execution_status(agent, originator_agent)
        _assert_worktree_authority(conn, id, captured_run_id, agent)
        if provisioning is not None:
            if not native_status["native_execution_allowed"]:
                raise ToolError(native_status["native_execution_denial"])
            result["provisioning"] = provisioning
        result.update(native_status)
        if trust_snapshot is not None:
            result["trust_preset"] = trust_snapshot
        if originator_snapshot is not None:
            result["originator_trust_preset"] = originator_snapshot
        # The run's frozen operating posture rides the claim so a remote
        # executor never reads its own (tombstoned) store for it (task #1756).
        from ..operating_profiles import run_snapshot

        try:
            result["operating_profile"] = run_snapshot(conn, id)
        except Exception:  # noqa: BLE001 - a snapshot read must never fail a claim
            logger.warning("task_claim: operating snapshot unreadable for task %s", id)
            result["operating_profile"] = None
        if wip_warnings:
            result["wip_warnings"] = wip_warnings
        if replaced is not None:
            result["replaced"] = replaced
        if budget is not None and budget.warnings:
            result["budget_warnings"] = budget.warnings
        if budget is not None and budget.stop:
            result["budget_stop"] = budget.stop
        if budget is not None:
            budgets.record_decision(budget)
        task_changed(id)
        return _with_verified_caller(result)
    finally:
        conn.close()


def project_build(parent_task_id: int) -> ProjectBuildResult:
    """Register a `[project-spec]` hub task as a project root (task #830).

    Pointer-to-task: `parent_task_id` is the spec-carrying row, and that row
    *is* the project root (`project_id == parent_task_id`). No child steps are
    created — file those afterward with `task_add(parent_id=...)`. No new
    project state machine: lifecycle stays the task subtree, and review gates
    inside it remain `waiting_review` + `approval_resolve` (human-only).

    Fail-closed, no partial state:
    - caller must be a verified, registered agent identity
    - task N must exist
    - notes must carry a non-backticked `[project-spec]` marker
    - N must be unclaimed or claimed by the caller (`claimed_by` column)

    Requires the enterprise ``audit_hash_chain`` feature. On success, records
    the calling identity on that chain (``agent.project.built``) and returns
    the project id. Not human-approval-gated.
    """
    edition.require_feature("audit_hash_chain")
    identity = config.verified_agent_identity()
    if not identity or not config.is_known_agent_label(identity):
        raise ToolError("project_build requires a registered agent identity")

    def validate(conn) -> None:
        # Tenant fence (task #1814): another tenant's spec reads as missing.
        row = _visible_task(conn, parent_task_id, "id, notes, claimed_by")
        if row is None:
            raise ToolError(f"no task with id {parent_task_id}")
        notes = row["notes"] or ""
        if not _PROJECT_SPEC_MARKER_RX.search(notes):
            raise ToolError(f"task {parent_task_id} does not carry {_PROJECT_SPEC_MARKER}")
        claimant = (row["claimed_by"] or "").strip() or None
        if claimant is not None and claimant != identity:
            # A claim held by another agent is a typed conflict (task #2056).
            raise ConflictError(f"task {parent_task_id} is claimed by {claimant}, not {identity}")

    ulid = events.record_event(
        events.CHANNEL_PROJECT_BUILT,
        payload={
            "parent_task_id": parent_task_id,
            "project_id": parent_task_id,
            "identity": identity,
        },
        originator=identity,
        precondition=validate,
    )
    return {
        "project_id": parent_task_id,
        "parent_task_id": parent_task_id,
        "identity": identity,
        "event_ulid": ulid,
    }
