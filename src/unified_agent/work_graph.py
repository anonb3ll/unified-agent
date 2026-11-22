"""Work graph projection (task #1460).

One read-only Control Center projection over readers that already exist:
goals and their why-ancestry (#1349, ``goals.py``), the task dependency DAG
(``task_dependencies`` / ``tools/graph.py``), self-pickup run records (#1436,
``run_records.py``), dispatch runs and autonomous cycles (``tools/graph.py``
``lineage``), agent sessions (``registry.py``), and joint-plan bindings
(#1455, ``joint_planning.py``). It adds no table, no scheduler, and no MCP
tool: the shape below is served over HTTP only
(``GET /api/work-graph``, ``webui/routes.py``).

Node kinds: ``goal``, ``plan``, ``task``, ``run``, ``session``, ``evidence``,
``agent``. Every edge below is a direct read of a column or table that
already declares that relation. None is inferred from notes, titles, or any
other free text — see the module docstring in ``goals.py`` for why that
distinction matters here.

======================  ============================================================
edge kind               source
======================  ============================================================
``goal_ancestry``       ``goals.parent_goal_id``
``goal_task``           ``task_goal_links``
``task_dependency``     ``task_dependencies``
``task_parent``         ``tasks.parent_id``
``plan_goal``           a joint-plan checkpoint's ``context.joint.goal_id`` (#1455)
``run_task``            ``dispatch_runs.task_id`` / self-pickup run ``task_id``
``run_agent``           ``dispatch_runs.agent_label`` / self-pickup run ``agent``
``session_task``        ``agent_sessions.task_id``
``session_agent``       ``agent_sessions.agent``
``evidence_task``       ``tasks.evidence_ref`` (opaque; see ``docs/work-graph.md``)
``claim``               ``tasks.claimed_by``
======================  ============================================================

Declared gaps — relations this projection does NOT invent because no reader
declares them (documented in full in ``docs/work-graph.md``):

* **run -> session.** Neither ``dispatch_runs`` nor the self-pickup run
  record carries a ``session_id``. A run and a session started for the same
  task are never linked here; only ``run_task``/``session_task`` are drawn,
  each separately, to the shared task node.
* **plan -> task.** ``plan_steps`` has no ``task_id`` column (only free-text
  ``owner``/``description``), so a plan's steps cannot be linked to the
  tasks that implement them. Only the plan-level ``plan_goal`` edge is
  drawn, when a plan was jointly bound to a goal (#1455 ``joint_planning.bind``).

Pagination: call with no ``goal_id`` for a page of root goals (no tasks
attached — an overview). Call with a ``goal_id`` for that goal's full
why-ancestry plus its descendant sub-goals, and a page of the tasks linked to
any goal in that scope, keyset-paginated by task id via ``cursor``/
``next_cursor``. ``revision`` is the max ``updated_at``/timestamp across every
row folded into the response; clients refetch on events/reconnect and polling,
even when the revision stays the same (edge removal and lease expiry can
change a graph without advancing a stored timestamp). ``unknowns`` lists telemetry
this projection could not resolve (an unregistered agent, or a task whose
self-pickup run history was truncated to its 20 most recent runs) rather
than guessing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import edition, registry, run_records, storage
from . import goals as goals_mod
from .tools import graph as graph_tools

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_GOAL_SCOPE = 1000
_TERMINAL_TASK_STATUSES = frozenset({"done", "cancelled"})
_IN_PROGRESS_TASK_STATUSES = frozenset({"doing", "waiting_review"})

TASK_COLUMNS = (
    "id",
    "title",
    "status",
    "priority",
    "parent_id",
    "claimed_by",
    "lock_expires_at",
    "lane",
    "project",
    "domain",
    "created_at",
    "updated_at",
    "completed_at",
    "evidence_ref",
    "worktree_path",
    "branch",
)


def _gid(kind: str, raw: Any) -> str:
    return f"{kind}:{raw}"


class _Builder:
    """Accumulates nodes/edges/unknowns/timestamps for one response."""

    def __init__(self, *, include_agent_metadata: bool = True) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.unknowns: list[dict[str, Any]] = []
        self._timestamps: list[str] = []
        self.include_agent_metadata = include_agent_metadata

    def touch(self, value: Any) -> None:
        if value:
            self._timestamps.append(str(value))

    def add_node(self, node_id: str, node: dict[str, Any]) -> None:
        # First writer wins: a node touched twice (e.g. a task that is both a
        # dependency neighbor and the page's own row) keeps its fullest form.
        if node_id in self.nodes:
            return
        self.nodes[node_id] = node

    def add_edge(self, kind: str, src: str, dst: str) -> None:
        self.edges.append({"kind": kind, "from": src, "to": dst})

    def unknown(self, node_id: str, field: str, reason: str) -> None:
        self.unknowns.append({"node_id": node_id, "field": field, "reason": reason})

    def revision(self) -> str | None:
        parsed = []
        for text in self._timestamps:
            dt = run_records._parse_ts(text)  # noqa: SLF001 - shared normalizer, same platform
            if dt is not None:
                parsed.append((dt, text))
        if not parsed:
            return None
        return max(parsed, key=lambda pair: pair[0])[1]


def _goal_node(row: Any) -> dict[str, Any]:
    """Read through ``goals.GOALS_WITH_VISIBLE_PARENT_SQL``: a legacy parent in
    another tenant serializes as None (#1999)."""
    return {
        "id": _gid("goal", row["id"]),
        "kind": "goal",
        "goal_id": int(row["id"]),
        "label": row["title"],
        "status": row["status"],
        "project": row["project"],
        "parent_goal_id": row["visible_parent_id"],
        "updated_at": row["updated_at"],
    }


def _task_node(row: Any, *, stub: bool) -> dict[str, Any]:
    node = {
        "id": _gid("task", row["id"]),
        "kind": "task",
        "task_id": int(row["id"]),
        "label": row["title"],
        "status": row["status"],
        "priority": row["priority"] if "priority" in row.keys() else None,
        "parent_id": row["parent_id"] if "parent_id" in row.keys() else None,
        "claimed_by": row["claimed_by"] if "claimed_by" in row.keys() else None,
        "lock_expires_at": row["lock_expires_at"] if "lock_expires_at" in row.keys() else None,
        "lane": row["lane"] if "lane" in row.keys() else None,
        "project": row["project"] if "project" in row.keys() else None,
        "updated_at": row["updated_at"] if "updated_at" in row.keys() else None,
        "evidence_ref": row["evidence_ref"] if "evidence_ref" in row.keys() else None,
        "stub": stub,
    }
    return node


def _agent_node(label: str) -> tuple[dict[str, Any], str | None]:
    agent = registry.get_agent(label)
    if agent is None:
        return (
            {
                "id": _gid("agent", label),
                "kind": "agent",
                "label": label,
                "status": "unknown",
            },
            "agent is not registered",
        )
    return (
        {
            "id": _gid("agent", label),
            "kind": "agent",
            "label": label,
            "status": agent.get("status"),
            "last_seen": agent.get("last_seen"),
        },
        None,
    )


def _ensure_agent(b: _Builder, label: str | None) -> str | None:
    if not label:
        return None
    node_id = _gid("agent", label)
    if node_id not in b.nodes:
        node, problem = (
            _agent_node(label)
            if b.include_agent_metadata
            else (
                {"id": node_id, "kind": "agent", "label": label, "status": "unknown"},
                "agent reader is unavailable to this profile",
            )
        )
        b.add_node(node_id, node)
        b.touch(node.get("last_seen"))
        if problem:
            b.unknown(node_id, "status", problem)
    return node_id


def _ready_info(
    task_row: Any, dep_rows: list[Any], status_by_id: dict[int, str | None]
) -> dict[str, Any]:
    # Match task_list(frontier=True)'s prerequisite rule, then describe the
    # task's own lifecycle state. Frontier is a prerequisite filter, so it can
    # include claimed or manually blocked tasks that this UI must not call ready.
    status = task_row["status"]
    if status in _TERMINAL_TASK_STATUSES:
        return {"state": "terminal", "reason": f"task is {status}", "unmet_dependency_ids": []}
    if status == "doing":
        claimant = task_row["claimed_by"] if "claimed_by" in task_row.keys() else None
        expiry = run_records._parse_ts(task_row["lock_expires_at"])
        if expiry is not None and expiry <= datetime.now(UTC):
            return {
                "state": "stalled",
                "reason": f"claim by {claimant} expired; task can be reclaimed"
                if claimant
                else "claim expired; task can be reclaimed",
                "unmet_dependency_ids": [],
            }
        reason = f"claimed by {claimant}" if claimant else "in progress"
        return {"state": "in_progress", "reason": reason, "unmet_dependency_ids": []}
    if status == "waiting_review":
        return {"state": "in_progress", "reason": "awaiting review", "unmet_dependency_ids": []}

    unmet: list[int] = []
    for dep in dep_rows:
        if int(dep["task_id"]) != int(task_row["id"]):
            continue
        prereq_id = int(dep["depends_on_task_id"])
        prereq_status = status_by_id.get(prereq_id)
        if prereq_status is None or prereq_status not in _TERMINAL_TASK_STATUSES:
            unmet.append(prereq_id)

    if status == "blocked":
        if unmet:
            return {
                "state": "blocked",
                "reason": "unmet dependency",
                "unmet_dependency_ids": unmet,
            }
        return {
            "state": "blocked",
            "reason": (
                "task status is 'blocked' with no unmet dependency on record; "
                "the human-entered reason, if any, lives in the task's notes "
                "and is not inferred here"
            ),
            "unmet_dependency_ids": [],
        }
    if unmet:
        return {"state": "blocked", "reason": "unmet dependency", "unmet_dependency_ids": unmet}
    return {"state": "ready", "reason": "no unmet dependency", "unmet_dependency_ids": []}


def _tenant() -> str | None:
    """The caller's tenant. Every goal and task read here is fenced on it with
    ``org_id IS ?``, the rule ``goals.py`` uses (task #1999): another tenant's
    row reads as absent, and an untenanted hub sees only NULL rows."""
    return goals_mod._tenant()  # noqa: SLF001 - one fence rule, shared


def _fetch_tasks(conn: Any, task_ids: list[int]) -> dict[int, Any]:
    if not task_ids:
        return {}
    out: dict[int, Any] = {}
    ids = sorted(set(task_ids))
    tenant = _tenant()
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        rows = conn.execute(
            f"SELECT {', '.join(TASK_COLUMNS)} FROM tasks WHERE id IN "
            f"({','.join('?' * len(chunk))}) AND org_id IS ?",
            [*chunk, tenant],
        ).fetchall()
        for row in rows:
            out[int(row["id"])] = row
    return out


def _add_runs_for_task(
    b: _Builder,
    task_id: int,
    task_node_id: str,
    *,
    include_dispatch_runs: bool,
    include_cycles: bool,
    include_self_pickup_runs: bool,
) -> None:
    lineage = (
        graph_tools.lineage(task_id)["lineage"] if include_dispatch_runs or include_cycles else {}
    )
    for cycle in lineage.get("dispatch_runs", []) if include_dispatch_runs else []:
        run_id = cycle.get("id")
        if not run_id:
            continue
        node_id = _gid("run", f"dispatch:{run_id}")
        b.add_node(
            node_id,
            {
                "id": node_id,
                "kind": "run",
                "subtype": "dispatch",
                "run_id": run_id,
                "drill_uri": f"/api/platform/objects/run/{run_id}",
                "task_id": task_id,
                "status": cycle.get("status"),
            },
        )
        b.touch(cycle.get("updated_at"))
        b.add_edge("run_task", node_id, task_node_id)
        agent_id = _ensure_agent(b, cycle.get("agent_label"))
        if agent_id:
            b.add_edge("run_agent", node_id, agent_id)
    for cycle in lineage.get("autonomous_cycles", []) if include_cycles else []:
        cycle_id = cycle.get("id")
        if not cycle_id:
            continue
        node_id = _gid("run", f"cycle:{cycle_id}")
        b.add_node(
            node_id,
            {
                "id": node_id,
                "kind": "run",
                "subtype": "autonomous_cycle",
                "run_id": cycle_id,
                "drill_uri": f"/api/cycles/{cycle_id}",
                "task_id": task_id,
                "status": cycle.get("status"),
            },
        )
        b.touch(cycle.get("updated_at"))
        b.add_edge("run_task", node_id, task_node_id)

    history = (
        run_records.run_history(task_id=task_id, limit=20)
        if include_self_pickup_runs
        else {"runs": [], "truncated": False}
    )
    for run in history["runs"]:
        run_id = run.get("run_id")
        if not run_id:
            continue
        node_id = _gid("run", run_id)
        b.add_node(
            node_id,
            {
                "id": node_id,
                "kind": "run",
                "subtype": "self_pickup",
                "run_id": run_id,
                "drill_uri": f"/api/platform/objects/run/{run_id}",
                "task_id": task_id,
                "state": run.get("state"),
                "outcome": run.get("outcome"),
                "claimed_at": run.get("claimed_at"),
                "finished_at": run.get("finished_at"),
                "wall_s": run.get("wall_s"),
                "reason": run.get("reason"),
            },
        )
        b.touch(run.get("claimed_at"))
        b.touch(run.get("finished_at"))
        b.add_edge("run_task", node_id, task_node_id)
        agent_id = _ensure_agent(b, run.get("agent"))
        if agent_id:
            b.add_edge("run_agent", node_id, agent_id)

    if include_self_pickup_runs and history.get("truncated"):
        b.unknown(
            task_node_id,
            "run_history",
            f"run_history returned only its {history.get('limit', 20)} most recent "
            "self-pickup runs for this task; earlier runs exist but are not shown",
        )


def _add_sessions_for_tasks(b: _Builder, conn: Any, task_ids: list[int]) -> None:
    if not task_ids:
        return
    ids = sorted(set(task_ids))
    # Only the caller's tenant's sessions join the graph (task #1995).
    org_id = edition.tenant_org_id()
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        rows = conn.execute(
            "SELECT id, agent, kind, status, started_at, last_heartbeat_at, ended_at, "
            "task_id, delegation_id, worktree_path, branch, summary "
            f"FROM agent_sessions WHERE task_id IN ({','.join('?' * len(chunk))}) "
            "AND org_id IS ? ORDER BY started_at DESC",
            (*chunk, org_id),
        ).fetchall()
        for row in rows:
            node_id = _gid("session", row["id"])
            b.add_node(
                node_id,
                {
                    "id": node_id,
                    "kind": "session",
                    "session_id": row["id"],
                    "drill_uri": f"/api/platform/objects/session/{row['id']}",
                    "task_id": row["task_id"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "last_heartbeat_at": row["last_heartbeat_at"],
                    "ended_at": row["ended_at"],
                    "worktree_path": row["worktree_path"],
                    "branch": row["branch"],
                    "summary": row["summary"],
                },
            )
            b.touch(row["last_heartbeat_at"])
            b.touch(row["ended_at"])
            b.add_edge("session_task", node_id, _gid("task", row["task_id"]))
            agent_id = _ensure_agent(b, row["agent"])
            if agent_id:
                b.add_edge("session_agent", node_id, agent_id)


def _add_evidence_for_task(b: _Builder, task_row: Any, task_node_id: str) -> None:
    ref = task_row["evidence_ref"] if "evidence_ref" in task_row.keys() else None
    if not ref:
        return
    node_id = _gid("evidence", task_row["id"])
    b.add_node(
        node_id,
        {
            "id": node_id,
            "kind": "evidence",
            "task_id": int(task_row["id"]),
            "ref": ref,
            # Drill-through only; this projection does not resolve the ref's
            # own chain (that is the evidence report's job, not this one's).
            "drill_uri": f"/api/evidence?task_id={int(task_row['id'])}",
        },
    )
    b.add_edge("evidence_task", node_id, task_node_id)


def _plan_bindings_for_goals(conn: Any, goal_ids: list[int]) -> list[dict[str, Any]]:
    """Plans jointly bound (#1455) to a goal in ``goal_ids``, latest checkpoint only."""
    if not goal_ids:
        return []
    # Checkpoints carry no org_id; read them through their plan's fence (#2042).
    rows = conn.execute(
        "SELECT c.plan_id, c.context, c.seq FROM plan_checkpoints c "
        "JOIN plans p ON p.id = c.plan_id AND p.org_id IS ? "
        "WHERE json_extract(c.context, '$.joint.goal_id') IS NOT NULL "
        "ORDER BY c.plan_id, c.seq DESC",
        (_tenant(),),
    ).fetchall()
    latest: dict[str, Any] = {}
    for row in rows:
        if row["plan_id"] in latest:
            continue
        latest[row["plan_id"]] = row
    import json as _json

    out = []
    wanted = set(goal_ids)
    for plan_id, row in latest.items():
        try:
            context = _json.loads(row["context"])
        except (TypeError, ValueError):
            continue
        joint = context.get("joint") or {}
        goal_id = joint.get("goal_id")
        if goal_id in wanted:
            out.append({"plan_id": plan_id, "goal_id": goal_id, "joint": joint})
    return out


def _root_page(conn: Any, cursor: str | None, page_size: int) -> dict[str, Any]:
    after = 0
    if cursor:
        try:
            after = int(cursor)
        except ValueError as exc:
            raise ToolError("cursor must be a task or goal id") from exc
    # A root has no parent visible in this tenant: a legacy child of another
    # tenant's goal pages in as a root rather than vanishing (#1999).
    rows = conn.execute(
        f"{goals_mod.GOALS_WITH_VISIBLE_PARENT_SQL} "
        "WHERE p.id IS NULL AND g.org_id IS ? AND g.id > ? ORDER BY g.id LIMIT ?",
        (_tenant(), after, page_size + 1),
    ).fetchall()
    truncated = len(rows) > page_size
    rows = rows[:page_size]
    b = _Builder()
    for row in rows:
        node = _goal_node(row)
        b.add_node(node["id"], node)
        b.touch(row["updated_at"])
    return {
        "nodes": list(b.nodes.values()),
        "edges": b.edges,
        "scope": {"root_goals": True},
        "cursor": cursor,
        "next_cursor": str(rows[-1]["id"]) if truncated and rows else None,
        "as_of": storage.now(),
        "revision": b.revision(),
        "unknowns": b.unknowns,
    }


def _goal_subgraph(
    conn: Any,
    goal_id: int,
    cursor: str | None,
    page_size: int,
    *,
    include_dispatch_runs: bool,
    include_cycles: bool,
    include_self_pickup_runs: bool,
    include_sessions: bool,
    include_plans: bool,
    include_agent_metadata: bool,
    unavailable_readers: list[str],
) -> dict[str, Any]:
    tenant = _tenant()
    root = conn.execute(
        "SELECT * FROM goals WHERE id=? AND org_id IS ?", (goal_id, tenant)
    ).fetchone()
    if root is None:
        raise ToolError(f"unknown goal: {goal_id}")

    ancestry_ids = goals_mod._ancestry_ids(conn, goal_id)  # noqa: SLF001 - shared walk, one edge
    descendant_rows = conn.execute(
        "WITH RECURSIVE sub(id) AS ("
        "  SELECT ? "
        "  UNION ALL "
        "  SELECT g.id FROM goals g JOIN sub ON g.parent_goal_id = sub.id"
        "  WHERE g.org_id IS ?"
        ") SELECT id FROM sub LIMIT ?",
        (goal_id, tenant, MAX_GOAL_SCOPE + 1),
    ).fetchall()
    scope_ids = set(ancestry_ids) | {int(row["id"]) for row in descendant_rows}
    if len(descendant_rows) > MAX_GOAL_SCOPE or len(scope_ids) > MAX_GOAL_SCOPE:
        raise ToolError(f"goal subtree exceeds maximum scope of {MAX_GOAL_SCOPE} goals")

    goal_rows = conn.execute(
        f"{goals_mod.GOALS_WITH_VISIBLE_PARENT_SQL} "
        f"WHERE g.id IN ({','.join('?' * len(scope_ids))}) AND g.org_id IS ?",
        [*sorted(scope_ids), tenant],
    ).fetchall()

    b = _Builder(include_agent_metadata=include_agent_metadata)
    for reader in unavailable_readers:
        b.unknown("graph", reader, f"{reader} reader is unavailable to this profile")
    for row in goal_rows:
        node = _goal_node(row)
        b.add_node(node["id"], node)
        b.touch(row["updated_at"])
        parent = row["visible_parent_id"]
        if parent is not None and int(parent) in scope_ids:
            b.add_edge("goal_ancestry", node["id"], _gid("goal", int(parent)))

    for binding in _plan_bindings_for_goals(conn, sorted(scope_ids)) if include_plans else []:
        # A binding written before the fence may hang another tenant's plan
        # under this goal; only the caller's plans become nodes (#2014).
        plan_row = conn.execute(
            "SELECT * FROM plans WHERE id=? AND org_id IS ?", (binding["plan_id"], tenant)
        ).fetchone()
        if plan_row is None:
            continue
        node_id = _gid("plan", plan_row["id"])
        b.add_node(
            node_id,
            {
                "id": node_id,
                "kind": "plan",
                "plan_id": plan_row["id"],
                "label": plan_row["goal"],
                "status": plan_row["status"],
                "escalated": bool(plan_row["escalated"]),
                "updated_at": plan_row["updated_at"],
            },
        )
        b.touch(plan_row["updated_at"])
        b.add_edge("plan_goal", node_id, _gid("goal", int(binding["goal_id"])))

    after = 0
    if cursor:
        try:
            after = int(cursor)
        except ValueError as exc:
            raise ToolError("cursor must be a task id") from exc

    # A link written before the fence may hang another tenant's task under
    # this goal; only the caller's tasks page in (task #1999).
    link_rows = conn.execute(
        f"SELECT l.task_id FROM task_goal_links l JOIN tasks t ON t.id = l.task_id "
        f"WHERE l.goal_id IN ({','.join('?' * len(scope_ids))}) AND t.org_id IS ? "
        "AND l.task_id > ? ORDER BY l.task_id LIMIT ?",
        [*sorted(scope_ids), tenant, after, page_size + 1],
    ).fetchall()
    truncated = len(link_rows) > page_size
    page_task_ids = [int(row["task_id"]) for row in link_rows[:page_size]]

    task_rows = _fetch_tasks(conn, page_task_ids)

    dep_rows = []
    if page_task_ids:
        dep_rows = conn.execute(
            "SELECT task_id, depends_on_task_id, created_at FROM task_dependencies WHERE task_id IN "
            f"({','.join('?' * len(page_task_ids))}) OR depends_on_task_id IN "
            f"({','.join('?' * len(page_task_ids))})",
            [*page_task_ids, *page_task_ids],
        ).fetchall()

    neighbor_ids = set()
    for dep in dep_rows:
        neighbor_ids.add(int(dep["task_id"]))
        neighbor_ids.add(int(dep["depends_on_task_id"]))
    for row in task_rows.values():
        parent = row["parent_id"]
        if parent is not None:
            neighbor_ids.add(int(parent))
    neighbor_ids -= set(page_task_ids)
    neighbor_rows = _fetch_tasks(conn, list(neighbor_ids))

    status_by_id: dict[int, str | None] = {
        tid: row["status"] for tid, row in {**task_rows, **neighbor_rows}.items()
    }

    for row in neighbor_rows.values():
        node = _task_node(row, stub=True)
        b.add_node(node["id"], node)
        b.touch(row["updated_at"])

    for tid in page_task_ids:
        row = task_rows.get(tid)
        if row is None:
            b.unknown(_gid("task", tid), "row", "linked task id has no task row")
            continue
        node = _task_node(row, stub=False)
        node["ready"] = _ready_info(row, dep_rows, status_by_id)
        b.add_node(node["id"], node)
        b.touch(row["updated_at"])

    # goal_task edges: re-query which goal each returned task links to, since
    # task_goal_links is single-valued per task (goals.py's own invariant).
    # linked_at is folded into the revision so a new link -- including the
    # first one a goal ever gets -- advances it even though it changes no
    # task or goal row (#1460 Devin finding).
    if page_task_ids:
        owner_rows = conn.execute(
            "SELECT task_id, goal_id, linked_at FROM task_goal_links WHERE task_id IN "
            f"({','.join('?' * len(page_task_ids))})",
            page_task_ids,
        ).fetchall()
        for row in owner_rows:
            if int(row["goal_id"]) in scope_ids:
                b.add_edge(
                    "goal_task",
                    _gid("goal", int(row["goal_id"])),
                    _gid("task", int(row["task_id"])),
                )
                b.touch(row["linked_at"])

    for dep in dep_rows:
        b.add_edge(
            "task_dependency",
            _gid("task", int(dep["task_id"])),
            _gid("task", int(dep["depends_on_task_id"])),
        )
        b.touch(dep["created_at"])

    for tid in page_task_ids:
        row = task_rows.get(tid)
        if row is None:
            continue
        parent = row["parent_id"]
        if parent is not None:
            b.add_edge("task_parent", _gid("task", tid), _gid("task", int(parent)))
        claimant = row["claimed_by"] if "claimed_by" in row.keys() else None
        agent_id = _ensure_agent(b, claimant)
        if agent_id:
            b.add_edge("claim", _gid("task", tid), agent_id)
        _add_runs_for_task(
            b,
            tid,
            _gid("task", tid),
            include_dispatch_runs=include_dispatch_runs,
            include_cycles=include_cycles,
            include_self_pickup_runs=include_self_pickup_runs,
        )
        _add_evidence_for_task(b, row, _gid("task", tid))

    if include_sessions:
        _add_sessions_for_tasks(b, conn, page_task_ids)

    return {
        "nodes": list(b.nodes.values()),
        "edges": b.edges,
        "scope": {"goal_id": goal_id, "ancestry_ids": ancestry_ids},
        "cursor": cursor,
        "next_cursor": str(page_task_ids[-1]) if truncated and page_task_ids else None,
        "as_of": storage.now(),
        "revision": b.revision(),
        "unknowns": b.unknowns,
    }


def work_graph(
    goal_id: int | None = None,
    cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    *,
    include_dispatch_runs: bool = True,
    include_cycles: bool = True,
    include_self_pickup_runs: bool = True,
    include_sessions: bool = True,
    include_plans: bool = True,
    include_agent_metadata: bool = True,
    unavailable_readers: list[str] | None = None,
) -> dict[str, Any]:
    """The Work Graph pane's one read.

    No ``goal_id``: a keyset-paginated page of root goals only (an overview
    to pick from). With ``goal_id``: that goal's why-ancestry, its descendant
    sub-goals, any plan jointly bound to a goal in that scope, and a
    keyset-paginated page of the tasks linked to a goal in that scope, each
    with its dependency/parent/claim/run/session/evidence edges and a
    ``ready`` explanation. See the module docstring for the edge vocabulary
    and the relations this projection deliberately does not invent.
    """
    size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    if goal_id is not None and include_sessions:
        registry.reap_abandoned_sessions()
    conn = storage.connect()
    try:
        if goal_id is None:
            return _root_page(conn, cursor, size)
        return _goal_subgraph(
            conn,
            int(goal_id),
            cursor,
            size,
            include_dispatch_runs=include_dispatch_runs,
            include_cycles=include_cycles,
            include_self_pickup_runs=include_self_pickup_runs,
            include_sessions=include_sessions,
            include_plans=include_plans,
            include_agent_metadata=include_agent_metadata,
            unavailable_readers=list(unavailable_readers or []),
        )
    finally:
        conn.close()
