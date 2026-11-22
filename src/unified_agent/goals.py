"""Goals: a why-chain, not a dependency graph (tasks #1349, mission #1450).

``parent_goal_id`` walks ancestry from the root down to the goal a task
serves. That edge is ``goal_ancestry``. ``task_goal_links`` is ``goal_task``:
one goal per task, and many tasks may serve that same goal. Neither edge is
the task ``parent_id`` tree, and neither is the ``task_dependencies`` DAG
(``dependency_add``). Joint planning (#1455)
consumes this chain; it does not redefine it.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from . import config, edition, events, storage, table_schemas

EDGE_ANCESTRY = "goal_ancestry"
EDGE_TASK = "goal_task"
EDGE_DEPENDENCY = "task_dependency"
MAX_DEPTH = 8
STATUSES = ("open", "achieved", "abandoned")
EDGE_MEANING = (
    "goal_ancestry is why (root first). goal_task is the goal a task serves. "
    "task_dependency (dependency_add) is the prerequisite DAG. "
    "tasks.parent_id is decomposition nesting. These are different edges."
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create this module's tables (the DDL lives in ``table_schemas``)."""
    table_schemas.goals(conn)
    storage._add_goal_org_id(conn)
    storage._add_revision_counters(conn)


def _tenant() -> str | None:
    """The caller's tenant. Every goal and task row here is fenced on it
    with ``org_id IS ?`` (task #1999, the #1834 rule): another tenant's row
    reads as unknown, and an untenanted hub keeps seeing NULL rows only."""
    return edition.tenant_org_id()


def _author(author: str | None) -> str:
    identity = config.verified_agent_identity()
    if identity:
        return identity
    text = (author or "").strip()
    if not text:
        raise ToolError("author is required when no verified identity is present")
    return text


def _row(conn: sqlite3.Connection, goal_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM goals WHERE id=? AND org_id IS ?", (goal_id, _tenant())
    ).fetchone()


def _task_row(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, project FROM tasks WHERE id=? AND org_id IS ?", (int(task_id), _tenant())
    ).fetchone()


def _goal_ref(conn: sqlite3.Connection, goal_id: int) -> str:
    """Name a linked goal in an error without disclosing a foreign goal's id."""
    if _row(conn, goal_id) is None:
        return "a goal not visible here"
    return f"goal {goal_id}"


def _public(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


# A parent written before the tenant fence may be another tenant's goal
# (#1999). Serializers read goals through this join and report the parent
# only when it is visible in the caller's tenant; otherwise the goal is a root.
GOALS_WITH_VISIBLE_PARENT_SQL = (
    "SELECT g.*, p.id AS visible_parent_id FROM goals g "
    "LEFT JOIN goals p ON p.id = g.parent_goal_id AND p.org_id IS g.org_id"
)


def public_goal(row: sqlite3.Row) -> dict[str, Any]:
    """Serialize a row read through ``GOALS_WITH_VISIBLE_PARENT_SQL``: a parent
    id that is not visible in the caller's tenant reads as None."""
    goal = _public(row)
    goal["parent_goal_id"] = goal.pop("visible_parent_id")
    return goal


def _ancestry_ids(conn: sqlite3.Connection, goal_id: int) -> list[int]:
    """Root-first ids. Cycles and depth above MAX_DEPTH fail closed."""
    chain: list[int] = []
    seen: set[int] = set()
    current: int | None = goal_id
    while current is not None:
        if current in seen:
            raise ToolError("goal ancestry contains a cycle")
        if len(chain) >= MAX_DEPTH:
            raise ToolError(f"goal ancestry is deeper than {MAX_DEPTH}")
        seen.add(current)
        row = _row(conn, current)
        if row is None:
            if chain:
                # A parent written before the tenant fence may be another
                # tenant's goal (#1999): fail closed without naming its id.
                raise ToolError("goal ancestry reaches a goal not visible here")
            raise ToolError(f"unknown goal: {current}")
        chain.append(current)
        parent = row["parent_goal_id"]
        current = int(parent) if parent is not None else None
    chain.reverse()
    return chain


def goal_add(
    title: str,
    statement: str | None = None,
    parent_goal_id: int | None = None,
    project: str | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    """Create a goal. ``parent_goal_id`` is why-ancestry, not a task dependency."""
    text = (title or "").strip()
    if not text or "\n" in text or len(text) > 200:
        raise ToolError("title must be one nonempty line of at most 200 characters")
    body = (statement or "").strip()
    if len(body) > 4000:
        raise ToolError("statement must be at most 4000 characters")
    who = _author(author)
    project_name = (project or "unified-agent").strip() or "unified-agent"
    conn = storage.connect()
    try:
        if parent_goal_id is not None:
            parent = _row(conn, int(parent_goal_id))
            if parent is None:
                raise ToolError(f"unknown parent goal: {parent_goal_id}")
            if str(parent["project"]) != project_name:
                raise ToolError("parent goal must belong to the same project")
            if len(_ancestry_ids(conn, int(parent_goal_id))) >= MAX_DEPTH:
                raise ToolError(f"goal ancestry is deeper than {MAX_DEPTH}")
        now = storage.now()
        cur = conn.execute(
            "INSERT INTO goals (title, statement, parent_goal_id, project, status, "
            "created_by, created_at, updated_at, org_id) VALUES (?,?,?,?, 'open', ?,?,?,?)",
            (text, body, parent_goal_id, project_name, who, now, now, _tenant()),
        )
        goal_id = int(cur.lastrowid)
        conn.commit()
        stored = _public(_row(conn, goal_id))  # type: ignore[arg-type]
    finally:
        conn.close()
    events.record_event(
        "goal.created",
        payload={"goal_id": goal_id, "parent_goal_id": parent_goal_id, "edge": EDGE_ANCESTRY},
        originator=who,
        notify=False,
    )
    return {"goal": stored, "edge": EDGE_ANCESTRY, "meaning": EDGE_MEANING}


def get_goal(goal_id: int) -> dict[str, Any] | None:
    conn = storage.connect()
    try:
        row = _row(conn, int(goal_id))
        if row is None:
            return None
        chain = [_public(_row(conn, item)) for item in _ancestry_ids(conn, int(goal_id))]  # type: ignore[arg-type]
        goal = _public(row)
    finally:
        conn.close()
    return {
        "goal": goal,
        "ancestry": chain,
        "edge": EDGE_ANCESTRY,
        "meaning": EDGE_MEANING,
    }


def goal_list(project: str | None = None) -> dict[str, Any]:
    conn = storage.connect()
    try:
        if project:
            rows = conn.execute(
                f"{GOALS_WITH_VISIBLE_PARENT_SQL} WHERE g.project=? AND g.org_id IS ? "
                "ORDER BY g.id",
                (project, _tenant()),
            ).fetchall()
        else:
            rows = conn.execute(
                f"{GOALS_WITH_VISIBLE_PARENT_SQL} WHERE g.org_id IS ? ORDER BY g.id",
                (_tenant(),),
            ).fetchall()
        goals = [public_goal(row) for row in rows]
    finally:
        conn.close()
    return {"goals": goals, "edge": EDGE_ANCESTRY, "meaning": EDGE_MEANING}


def goal_tree(project: str | None = None) -> dict[str, Any]:
    """Nested why-tree. Children are sub-goals, not dependent tasks."""
    listed = goal_list(project=project)
    visible = {goal["id"] for goal in listed["goals"]}
    by_parent: dict[int | None, list[dict[str, Any]]] = {}
    for goal in listed["goals"]:
        parent = goal["parent_goal_id"]
        # A parent outside this filter is not a reason to hide the child. A
        # foreign parent is already None here, so its child is a root.
        by_parent.setdefault(parent if parent in visible else None, []).append(goal)

    placed: set[int] = set()

    def nest(goal: dict[str, Any]) -> dict[str, Any]:
        placed.add(int(goal["id"]))
        children = []
        for child in by_parent.get(goal["id"], []):
            if int(child["id"]) in placed:
                continue
            children.append(nest(child))
        return {**goal, "children": children}

    roots = [nest(goal) for goal in by_parent.get(None, [])]
    # A cycle has no parent outside itself, so it never appears under None.
    for goal in listed["goals"]:
        if int(goal["id"]) not in placed:
            roots.append(nest(goal))
    return {"tree": roots, "edge": EDGE_ANCESTRY, "meaning": EDGE_MEANING}


def link_task(task_id: int, goal_id: int, author: str | None = None) -> dict[str, Any]:
    """Point a task at the goal it serves. One goal per task. Not a dependency."""
    who = _author(author)
    conn = storage.connect()
    try:
        task = _task_row(conn, int(task_id))
        if task is None:
            raise ToolError(f"unknown task: {task_id}")
        goal = _row(conn, int(goal_id))
        if goal is None:
            raise ToolError(f"unknown goal: {goal_id}")
        task_project = (task["project"] or "unified-agent").strip() or "unified-agent"
        if task_project != str(goal["project"]):
            raise ToolError("task and goal must belong to the same project")
        _ancestry_ids(conn, int(goal_id))
        existing = conn.execute(
            "SELECT goal_id FROM task_goal_links WHERE task_id=?", (int(task_id),)
        ).fetchone()
        if existing is not None and int(existing["goal_id"]) != int(goal_id):
            raise ToolError(
                f"task {task_id} already serves {_goal_ref(conn, int(existing['goal_id']))}; "
                "goal_task is single-valued"
            )
        before = conn.execute("SELECT COUNT(*) AS n FROM task_dependencies").fetchone()
        now = storage.now()
        conn.execute(
            "INSERT INTO task_goal_links (task_id, goal_id, linked_by, linked_at) "
            "VALUES (?,?,?,?) ON CONFLICT(task_id) DO NOTHING",
            (int(task_id), int(goal_id), who, now),
        )
        stored = conn.execute(
            "SELECT goal_id FROM task_goal_links WHERE task_id=?", (int(task_id),)
        ).fetchone()
        conn.commit()
        if stored is None or int(stored["goal_id"]) != int(goal_id):
            served = "no goal" if stored is None else _goal_ref(conn, int(stored["goal_id"]))
            raise ToolError(f"task {task_id} serves {served}; goal_task is single-valued")
        after = conn.execute("SELECT COUNT(*) AS n FROM task_dependencies").fetchone()
        dependency_rows_before = int(before["n"]) if before else 0
        dependency_rows_after = int(after["n"]) if after else 0
    finally:
        conn.close()
    events.record_event(
        "goal.linked",
        payload={"task_id": int(task_id), "goal_id": int(goal_id), "edge": EDGE_TASK},
        originator=who,
        notify=False,
    )
    ancestry = why_chain(int(task_id))
    return {
        "task_id": int(task_id),
        "goal_id": int(goal_id),
        "edge": EDGE_TASK,
        "dependency_rows_before": dependency_rows_before,
        "dependency_rows_after": dependency_rows_after,
        "why_chain": ancestry,
    }


CLOSED_STATUSES = ("achieved", "abandoned")


def _require_goal(conn: sqlite3.Connection, goal_id: Any) -> sqlite3.Row:
    if isinstance(goal_id, bool) or not isinstance(goal_id, int):
        raise ToolError("goal_id must be an integer")
    row = _row(conn, goal_id)
    if row is None:
        raise ToolError(f"unknown goal: {goal_id}")
    return row


def _check_revision(row: sqlite3.Row, expected_revision: int | None) -> None:
    from .tool_errors import ConflictError

    if expected_revision is not None and (
        isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
    ):
        raise ToolError("expected_revision must be an integer")
    if expected_revision is not None and row["revision"] != expected_revision:
        raise ConflictError(
            f"goal {row['id']} changed since it was read (revision {row['revision']}, "
            f"expected {expected_revision}); reload it and try again",
            expected_revision=expected_revision,
            current_revision=row["revision"],
        )


def goal_edit(
    goal_id: int,
    title: str | None = None,
    statement: str | None = None,
    expected_revision: int | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    """Change a goal's title or statement (task #2480). Ancestry and project stay."""
    if title is None and statement is None:
        raise ToolError("pass title, statement or both")
    sets: list[str] = []
    values: list[Any] = []
    if title is not None:
        text = title.strip() if isinstance(title, str) else ""
        if not text or "\n" in text or len(text) > 200:
            raise ToolError("title must be one nonempty line of at most 200 characters")
        sets.append("title=?")
        values.append(text)
    if statement is not None:
        if not isinstance(statement, str) or len(statement.strip()) > 4000:
            raise ToolError("statement must be a string of at most 4000 characters")
        sets.append("statement=?")
        values.append(statement.strip())
    who = _author(author)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _require_goal(conn, goal_id)
        _check_revision(row, expected_revision)
        conn.execute(
            f"UPDATE goals SET {', '.join(sets)}, updated_at=? WHERE id=? AND org_id IS ?",
            (*values, storage.now(), goal_id, _tenant()),
        )
        conn.commit()
        stored = _public(_row(conn, goal_id))  # type: ignore[arg-type]
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
    events.record_event(
        "goal.updated",
        payload={"goal_id": goal_id, "fields": [s.split("=")[0] for s in sets]},
        originator=who,
        notify=False,
    )
    return {"goal": stored, "edge": EDGE_ANCESTRY, "meaning": EDGE_MEANING}


def goal_set_status(
    goal_id: int,
    status: str,
    reason: str | None = None,
    expected_revision: int | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    """Close a goal as achieved or abandoned, or reopen it (task #2480).

    Linked tasks keep their link; closing a goal does not close its tasks. A goal
    already in the requested state is a conflict.
    """
    from .tool_errors import ConflictError

    if status not in STATUSES:
        raise ToolError(f"status must be one of {', '.join(STATUSES)}")
    note = (reason or "").strip() if isinstance(reason, str) or reason is None else None
    if note is None or len(note) > 4000:
        raise ToolError("reason must be a string of at most 4000 characters")
    who = _author(author)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _require_goal(conn, goal_id)
        _check_revision(row, expected_revision)
        if row["status"] == status or (
            status in CLOSED_STATUSES and row["status"] in CLOSED_STATUSES
        ):
            raise ConflictError(f"goal {goal_id} is already {row['status']}")
        conn.execute(
            "UPDATE goals SET status=?, updated_at=? WHERE id=? AND org_id IS ?",
            (status, storage.now(), goal_id, _tenant()),
        )
        conn.commit()
        stored = _public(_row(conn, goal_id))  # type: ignore[arg-type]
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
    events.record_event(
        "goal.reopened" if status == "open" else "goal.closed",
        payload={"goal_id": goal_id, "from": row["status"], "status": status, "reason": note},
        originator=who,
        notify=False,
    )
    return {"goal": stored, "edge": EDGE_ANCESTRY, "meaning": EDGE_MEANING}


def unlink_task(task_id: int, goal_id: int, author: str | None = None) -> dict[str, Any]:
    """Remove a task's goal_task link (task #2480). ``goal_id`` must be the goal it serves."""
    for name, value in (("task_id", task_id), ("goal_id", goal_id)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(f"{name} must be an integer")
    who = _author(author)
    conn = storage.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _task_row(conn, task_id) is None:
            raise ToolError(f"unknown task: {task_id}")
        _require_goal(conn, goal_id)
        existing = conn.execute(
            "SELECT goal_id FROM task_goal_links WHERE task_id=?", (task_id,)
        ).fetchone()
        if existing is None:
            raise ToolError(f"task {task_id} serves no goal")
        if int(existing["goal_id"]) != goal_id:
            raise ToolError(
                f"task {task_id} serves {_goal_ref(conn, int(existing['goal_id']))}, "
                f"not goal {goal_id}"
            )
        conn.execute("DELETE FROM task_goal_links WHERE task_id=?", (task_id,))
        conn.commit()
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()
    events.record_event(
        "goal.unlinked",
        payload={"task_id": task_id, "goal_id": goal_id, "edge": EDGE_TASK},
        originator=who,
        notify=False,
    )
    return {"task_id": task_id, "goal_id": goal_id, "edge": EDGE_TASK, "unlinked": True}


def goal_id_for_task(task_id: int) -> int | None:
    """The goal a task serves, or None. Both the task and the goal are fenced
    to the caller's tenant: a link written before the fence that points at
    another tenant's goal reads as no link, so its id never crosses (#1999)."""
    tenant = _tenant()
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT l.goal_id FROM task_goal_links l JOIN tasks t ON t.id=l.task_id "
            "JOIN goals g ON g.id=l.goal_id "
            "WHERE l.task_id=? AND t.org_id IS ? AND g.org_id IS ?",
            (int(task_id), tenant, tenant),
        ).fetchone()
    finally:
        conn.close()
    return int(row["goal_id"]) if row else None


def _task_exists(task_id: int) -> bool:
    conn = storage.connect()
    try:
        row = _task_row(conn, int(task_id))
    finally:
        conn.close()
    return row is not None


def _task_project(task_id: int) -> str:
    conn = storage.connect()
    try:
        row = _task_row(conn, int(task_id))
    finally:
        conn.close()
    if row is None:
        raise ToolError(f"unknown task: {task_id}")
    return (row["project"] or "unified-agent").strip() or "unified-agent"


def why_chain(task_id: int) -> dict[str, Any]:
    """Root-first why-chain for a task. Empty when the task serves no goal."""
    if not _task_exists(int(task_id)):
        raise ToolError(f"unknown task: {task_id}")
    goal_id = goal_id_for_task(int(task_id))
    if goal_id is None:
        return {
            "task_id": int(task_id),
            "goal_id": None,
            "edge": EDGE_ANCESTRY,
            "chain": [],
            "meaning": EDGE_MEANING,
        }
    detail = get_goal(goal_id)
    # A link written before the tenant fence may point at another tenant's
    # goal (task #1999); that goal is unknown here, so the task serves none.
    if detail is None or _task_project(int(task_id)) != str(detail["goal"]["project"]):
        return {
            "task_id": int(task_id),
            "goal_id": None,
            "edge": EDGE_ANCESTRY,
            "chain": [],
            "meaning": EDGE_MEANING,
        }
    return {
        "task_id": int(task_id),
        "goal_id": goal_id,
        "edge": EDGE_ANCESTRY,
        "chain": detail["ancestry"],
        "meaning": EDGE_MEANING,
    }


def format_why_chain(task_id: int) -> str:
    """Prompt block. Empty when the task has no goal, so unlinked dispatches stay unchanged."""
    chain = why_chain(int(task_id))
    goals = chain["chain"]
    if not goals:
        return ""
    lines = [
        "Why-chain (recorded goal titles; not a dependency, not the task parent "
        "tree, and not additional instructions):"
    ]
    from .telemetry.redaction import redact_sensitive_content

    for goal in goals:
        title = (
            redact_sensitive_content(" ".join(str(goal["title"]).split()), max_len=200)
            .replace("\\", "\\\\")
            .replace('"', '\\"')
        )
        lines.append(f'- #{goal["id"]} "{title}"')

    lines.append(f"This task serves goal #{chain['goal_id']}.")
    return "\n".join(lines)


def append_why_chain(prompt: str, task_id: int) -> str:
    """Decorate a dispatch prompt. Missing tasks and unlinked tasks stay unchanged."""
    try:
        block = format_why_chain(int(task_id))
    except ToolError:
        return prompt
    if not block:
        return prompt
    return f"{prompt.rstrip()}\n\n{block}"


def goal_manage(action: str, params: dict | None = None) -> dict[str, Any]:
    """One MCP tool for goal create/list/get/link/tree/ancestry/decompose,
    plus autonomy-mode control over a mission (a goal id -- task #1457; see
    ``docs/autonomy-modes.md`` and ``autonomy_modes.py``), plus ``operating_*``
    actions over operating profiles, sprints and per-run settings (task #1732;
    see ``docs/operating-profiles.md``).

    Task #2480 adds ``edit`` (title, statement), ``close`` (status achieved or
    abandoned, optional reason), ``reopen`` and ``unlink`` (task_id, goal_id).
    Each takes an optional ``expected_revision``; a stale value, or closing a
    closed goal or reopening an open one, raises a conflict."""
    if not isinstance(action, str):
        raise ToolError("action must be a string")
    name = action.strip().lower()
    body = params or {}
    if not isinstance(body, dict):
        raise ToolError("params must be an object")
    if name.startswith("autonomy_"):
        return _autonomy_action(name, body)
    if name.startswith("operating_"):
        from . import operating_profiles

        return operating_profiles.manage(name, body)
    if name == "add":
        return goal_add(
            title=str(body.get("title") or ""),
            statement=body.get("statement"),
            parent_goal_id=body.get("parent_goal_id"),
            project=body.get("project"),
            author=body.get("author"),
        )
    if name == "list":
        return goal_list(project=body.get("project"))
    if name == "tree":
        return goal_tree(project=body.get("project"))
    if name == "get":
        detail = get_goal(int(body["goal_id"]))
        if detail is None:
            raise ToolError(f"unknown goal: {body.get('goal_id')}")
        return detail
    if name == "link":
        return link_task(int(body["task_id"]), int(body["goal_id"]), author=body.get("author"))
    if name == "edit":
        return goal_edit(
            body.get("goal_id"),
            title=body.get("title"),
            statement=body.get("statement"),
            expected_revision=body.get("expected_revision"),
            author=body.get("author"),
        )
    if name in ("close", "reopen"):
        status = body.get("status", "achieved") if name == "close" else "open"
        if name == "close" and status not in CLOSED_STATUSES:
            raise ToolError("close takes status achieved or abandoned")
        return goal_set_status(
            body.get("goal_id"),
            status,
            reason=body.get("reason"),
            expected_revision=body.get("expected_revision"),
            author=body.get("author"),
        )
    if name == "unlink":
        return unlink_task(body.get("task_id"), body.get("goal_id"), author=body.get("author"))
    if name == "ancestry":
        return why_chain(int(body["task_id"]))
    if name == "decompose":
        from .planning_loop import decompose_into_goal_linked_tasks

        return decompose_into_goal_linked_tasks(
            int(body["goal_id"]), int(body["epic_id"]), author=body.get("author")
        )
    raise ToolError(
        "action must be one of add, list, tree, get, link, unlink, edit, close, reopen, "
        "ancestry, decompose, "
        "autonomy_get, autonomy_preview, autonomy_set, autonomy_propose, autonomy_apply, "
        "autonomy_grant, autonomy_revoke_grant, autonomy_evaluate, autonomy_mark_started, "
        "autonomy_review_action, operating_profiles, operating_resolve, operating_sprint_add, "
        "operating_sprint_list, operating_sprint_get, operating_sprint_update, "
        "operating_sprint_link, operating_sprint_unlink, operating_run_set, operating_run_clear"
    )


def _autonomy_action(name: str, body: dict) -> dict[str, Any]:
    """Dispatch the ``autonomy_*`` actions of ``goal_manage`` (task #1457).

    Kept as a thin dispatcher so ``autonomy_modes.py`` stays the single owner
    of the contract's logic; this file only maps MCP params onto it. A new
    top-level MCP tool would push the default profile toward the 100-tool
    cap, so the vertical slice is exposed as actions on the existing
    ``goal_manage`` tool instead (missions ARE goals here).
    """
    from . import autonomy_modes, config

    def _actor() -> str:
        # Same binding as approval_resolve: a verified caller is always its own
        # actor, so a caller-supplied human name cannot stand in for
        # authentication. The body's actor only counts on unauthenticated
        # local transports, and never where auth is enforced.
        verified = config.verified_agent_identity()
        if verified:
            return verified
        if config.enforce_auth():
            raise ToolError("autonomy actions require a verified identity")
        return str(body.get("actor") or "")

    if name == "autonomy_get":
        return autonomy_modes.get_mode(int(body["mission_id"]))
    if name == "autonomy_preview":
        return autonomy_modes.preview(int(body["mission_id"]))
    if name == "autonomy_set":
        return autonomy_modes.set_mode(
            int(body["mission_id"]),
            str(body.get("mode") or ""),
            actor=_actor(),
            reason=body.get("reason"),
        )
    if name == "autonomy_propose":
        return autonomy_modes.propose_mode(
            int(body["mission_id"]),
            str(body.get("mode") or ""),
            actor=_actor(),
            reason=body.get("reason"),
        )
    if name == "autonomy_apply":
        return autonomy_modes.apply_proposal(int(body["approval_id"]))
    if name == "autonomy_grant":
        return autonomy_modes.grant_standing(
            int(body["mission_id"]),
            str(body.get("action_class") or ""),
            actor=_actor(),
            reason=body.get("reason"),
        )
    if name == "autonomy_revoke_grant":
        return autonomy_modes.revoke_grant(
            int(body["mission_id"]),
            str(body.get("action_class") or ""),
            actor=_actor(),
            reason=body.get("reason"),
        )
    if name == "autonomy_evaluate":
        return autonomy_modes.evaluate(
            int(body["mission_id"]), str(body.get("action_class") or ""), actor=_actor()
        )
    if name == "autonomy_mark_started":
        return autonomy_modes.mark_started(int(body["pending_id"]), actor=_actor())
    if name == "autonomy_review_action":
        return autonomy_modes.review_action(int(body["pending_id"]), actor=_actor())
    raise ToolError(
        f"unknown autonomy action {name!r}; expected one of autonomy_get, autonomy_preview, "
        "autonomy_set, autonomy_propose, autonomy_apply, autonomy_grant, autonomy_revoke_grant, "
        "autonomy_evaluate, autonomy_mark_started, autonomy_review_action"
    )
