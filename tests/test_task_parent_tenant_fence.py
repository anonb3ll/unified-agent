"""Tenant (org_id) fence on task nesting, lineage, subtree and rollup walks (task #1810).

Before this fence ``task_add(parent_id=...)`` and ``task_update(parent_id=...)``
looked the parent up by id alone, so one tenant could attach a task under
another tenant's row (and a missing id answered differently from a foreign
one -- an existence oracle). With ``ON DELETE CASCADE`` on ``parent_id`` the
other tenant deleting its parent would then delete the attached child.

Another tenant's id must read exactly like a missing id, and the ancestor,
depth, cycle, subtree and rollup walks must never read another tenant's rows.
The untenanted private hub (tenant ``None``) keeps matching NULL rows via IS.
"""

from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import storage
from unified_agent.tools import tasks as task_tools

ORG_A = "org_a"
ORG_B = "org_b"
MISSING = 99999


@pytest.fixture
def as_org(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")

    def _switch(org: str) -> None:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)

    return _switch


def _add(title: str, **kwargs) -> int:
    return task_tools.task_add(title=title, **kwargs)["id"]


def _raw(task_id: int) -> dict:
    conn = storage.connect()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row is not None else {}
    finally:
        conn.close()


def _count() -> int:
    conn = storage.connect()
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    finally:
        conn.close()


def _force_parent(child_id: int, parent_id: int) -> None:
    """Forge a cross-tenant edge the way pre-fence data could hold one."""
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET parent_id=? WHERE id=?", (parent_id, child_id))
        conn.commit()
    finally:
        conn.close()


def _error(fn) -> str:
    with pytest.raises(ToolError) as exc:
        fn()
    return str(exc.value)


# --- add / reparent: another tenant's id == a nonexistent id ------------------


def test_add_under_another_tenants_parent_reads_like_missing(as_org):
    as_org(ORG_A)
    theirs = _add("org a parent")
    as_org(ORG_B)
    before = _count()
    foreign = _error(lambda: task_tools.task_add("y", parent_id=theirs))
    absent = _error(lambda: task_tools.task_add("y", parent_id=MISSING))
    assert foreign == absent.replace(str(MISSING), str(theirs))
    assert foreign == f"no task with id {theirs} to use as parent"
    assert _count() == before


def test_reparent_under_another_tenants_parent_reads_like_missing(as_org):
    as_org(ORG_A)
    theirs = _add("org a parent")
    as_org(ORG_B)
    mine = _add("org b child")
    foreign = _error(lambda: task_tools.task_update(mine, parent_id=theirs))
    absent = _error(lambda: task_tools.task_update(mine, parent_id=MISSING))
    assert foreign == absent.replace(str(MISSING), str(theirs))
    assert _raw(mine)["parent_id"] is None


def test_reparent_of_another_tenants_task_reads_like_missing(as_org):
    as_org(ORG_A)
    theirs = _add("org a task")
    as_org(ORG_B)
    mine = _add("org b parent")
    foreign = _error(lambda: task_tools.task_update(theirs, parent_id=mine))
    absent = _error(lambda: task_tools.task_update(MISSING, parent_id=mine))
    assert foreign == absent.replace(str(MISSING), str(theirs))
    assert _raw(theirs)["parent_id"] is None


def test_untenanted_rows_are_not_parents_for_a_tenant(as_org, monkeypatch):
    monkeypatch.delenv("UA_EDITION", raising=False)
    private = _add("private hub row")
    assert _raw(private)["org_id"] is None
    as_org(ORG_A)
    msg = _error(lambda: task_tools.task_add("y", parent_id=private))
    assert msg == f"no task with id {private} to use as parent"


# --- cascade: org_a deleting its parent cannot reach org_b ---------------------


def test_deleting_a_parent_cannot_cascade_into_another_tenant(as_org):
    as_org(ORG_A)
    theirs = _add("org a parent")
    as_org(ORG_B)
    with pytest.raises(ToolError):
        task_tools.task_add("would-be child", parent_id=theirs)
    mine = _add("org b standalone")
    with pytest.raises(ToolError):
        task_tools.task_update(mine, parent_id=theirs)
    # org_b cannot delete org_a's row either: it reads like a missing id.
    assert task_tools.task_delete(theirs) == task_tools.task_delete(MISSING)
    assert _raw(theirs)
    as_org(ORG_A)
    assert task_tools.task_delete(theirs)["deleted"] is True
    assert _raw(mine)["org_id"] == ORG_B


def test_deleting_a_parent_detaches_a_legacy_cross_tenant_child(as_org):
    as_org(ORG_B)
    root = _add("org b root")
    mid = _add("org b mid", parent_id=root)
    as_org(ORG_A)
    under_root = _add("org a child under root")
    under_mid = _add("org a child under mid")
    _add("org a grandchild", parent_id=under_mid)
    # Legacy edges written before the fence, forged directly in SQL.
    _force_parent(under_root, root)
    _force_parent(under_mid, mid)
    as_org(ORG_B)
    out = task_tools.task_delete(root)
    assert out["deleted"] is True
    assert out["children_deleted"] == 1
    assert not _raw(root) and not _raw(mid)
    for survivor in (under_root, under_mid):
        row = _raw(survivor)
        assert row["org_id"] == ORG_A
        assert row["parent_id"] is None
    as_org(ORG_A)
    kids = task_tools.task_list(parent_id=under_mid, status="all")["tasks"]
    assert [t["title"] for t in kids] == ["org a grandchild"]


def _set_revision(task_id: int, revision: str) -> None:
    conn = storage.connect()
    try:
        conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (revision, task_id))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("shape", ["same_second", "microsecond_ahead"])
def test_detached_child_gets_a_strictly_newer_revision(as_org, monkeypatch, shape):
    as_org(ORG_B)
    root = _add("org b root")
    as_org(ORG_A)
    child = _add("org a child")
    _force_parent(child, root)
    task_tools.task_update(child, notes="touch")  # a microsecond-precision revision
    if shape == "same_second":
        # The detach lands in the same second as a second-precision revision.
        frozen = storage.now()
        monkeypatch.setattr(storage, "now", lambda: frozen)
        _set_revision(child, frozen)
    else:
        ahead = (datetime.now(UTC) + timedelta(seconds=5)).isoformat(timespec="microseconds")
        _set_revision(child, ahead)
    stale = _raw(child)["updated_at"]
    as_org(ORG_B)
    assert task_tools.task_delete(root)["deleted"] is True
    detached = _raw(child)
    assert detached["parent_id"] is None
    assert datetime.fromisoformat(detached["updated_at"]) > datetime.fromisoformat(stale)
    as_org(ORG_A)
    with pytest.raises(ToolError, match="expected_updated_at|revision"):
        task_tools.task_update(child, notes="stale edit", expected_updated_at=stale)
    assert "stale edit" not in (_raw(child)["notes"] or "")


# --- walks never read another tenant's rows -------------------------------------


def test_depth_check_ignores_another_tenants_ancestors(as_org):
    as_org(ORG_A)
    cursor = _add("a0")
    for i in range(task_tools.TASK_MAX_DEPTH):
        cursor = _add(f"a{i + 1}", parent_id=cursor)
    as_org(ORG_B)
    mine = _add("org b root")
    _force_parent(mine, cursor)
    # Inside org_b, `mine` is a root: org_a's depth-8 chain is not counted.
    child = _add("org b child", parent_id=mine)
    assert _raw(child)["parent_id"] == mine


def test_subtree_height_ignores_another_tenants_children(as_org):
    as_org(ORG_B)
    cursor = _add("b0")
    for i in range(task_tools.TASK_MAX_DEPTH - 1):
        cursor = _add(f"b{i + 1}", parent_id=cursor)
    mover = _add("org b mover")
    as_org(ORG_A)
    foreign_child = _add("org a child")
    as_org(ORG_B)
    _force_parent(foreign_child, mover)
    # Own height of `mover` is 0, so depth 8 is allowed; org_a's row would make it 9.
    row = task_tools.task_update(mover, parent_id=cursor)
    assert row["parent_id"] == cursor


def test_cycle_check_and_lineage_stop_at_the_tenant_boundary(as_org):
    as_org(ORG_A)
    foreign_root = _add("org a root")
    as_org(ORG_B)
    mine = _add("org b root")
    leaf = _add("org b leaf", parent_id=mine)
    _force_parent(mine, foreign_root)
    listed = task_tools.task_list(parent_id=mine, status="all")["tasks"]
    assert [t["id"] for t in listed] == [leaf]
    assert listed[0]["lineage"] == [mine]
    roots = {t["id"]: t for t in task_tools.task_list(status="all")["tasks"]}
    assert roots[mine]["lineage"] == []
    with pytest.raises(ToolError, match="cycle"):
        task_tools.task_update(mine, parent_id=leaf)


def test_task_list_parent_filter_does_not_list_another_tenants_children(as_org):
    as_org(ORG_A)
    theirs = _add("org a parent")
    _add("org a child", parent_id=theirs)
    assert len(task_tools.task_list(parent_id=theirs)["tasks"]) == 1
    as_org(ORG_B)
    assert task_tools.task_list(parent_id=theirs)["tasks"] == []


def test_project_rollup_ignores_another_tenants_descendants(as_org):
    as_org(ORG_B)
    root = _add("org b project", notes="[project-spec] ship it")
    as_org(ORG_A)
    foreign = _add("Zirconfence org a secret step")
    as_org(ORG_B)
    _force_parent(foreign, root)
    listed = {t["id"]: t for t in task_tools.task_list(status="all")["tasks"]}
    rollup = listed[root]["rollup"]
    assert rollup["complete"] is True
    assert rollup["active"] == []
    assert sum(rollup["counts"].values()) == 0
    row = task_tools.task_update(root, status="done", actor="test-agent")
    assert row["status"] == "done"


# --- same-tenant nesting unchanged ------------------------------------------------


def test_same_tenant_nesting_unchanged(as_org):
    as_org(ORG_B)
    root = _add("root")
    step = _add("step", parent_id=root)
    sub = _add("substep", parent_id=step)
    kids = task_tools.task_list(parent_id=step, status="all")["tasks"]
    assert [t["id"] for t in kids] == [sub]
    assert kids[0]["lineage"] == [root, step]
    other = _add("other root")
    assert task_tools.task_update(step, parent_id=other)["parent_id"] == other
    with pytest.raises(ToolError, match="cycle"):
        task_tools.task_update(other, parent_id=sub)
    out = task_tools.task_delete(other)
    assert out["deleted"] is True
    assert out["children_deleted"] == 2
    assert _raw(root)
