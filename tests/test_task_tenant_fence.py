"""Tenant (org_id) fence on task reads and writes by id (tasks #1814, #1931).

#1810 fenced nesting, lineage and subtree walks. The tool entry points that
name a task by id still read and wrote it by id alone: one tenant could update,
claim, list, build or comment on another tenant's task. Another tenant's id
must now behave exactly like a missing id, and a listing must never show
another tenant's rows. The untenanted private hub (tenant ``None``) keeps
matching NULL rows via ``IS``.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, storage, task_comments
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


def _error(fn) -> str:
    with pytest.raises(ToolError) as exc:
        fn()
    return str(exc.value)


def _same_as_missing(foreign: int, fn) -> None:
    """`fn(id)` refuses a foreign id with the message a missing id gets."""
    expected = _error(lambda: fn(MISSING)).replace(str(MISSING), "<id>")
    assert _error(lambda: fn(foreign)).replace(str(foreign), "<id>") == expected


def test_update_of_another_tenants_task_reads_like_missing(as_org):
    as_org(ORG_A)
    task_id = _add("org a task")
    before = _raw(task_id)
    as_org(ORG_B)
    _same_as_missing(task_id, lambda i: task_tools.task_update(i, title="org b overwrote"))
    _same_as_missing(task_id, lambda i: task_tools.task_update(i, status="blocked"))
    _same_as_missing(task_id, lambda i: task_tools.task_update(i, append_note="org b note"))
    assert _raw(task_id) == before


def test_update_by_own_tenant_still_works(as_org):
    as_org(ORG_A)
    task_id = _add("org a task")
    task_tools.task_update(task_id, title="renamed")
    assert _raw(task_id)["title"] == "renamed"


def test_claim_of_another_tenants_task_reads_like_missing(as_org):
    as_org(ORG_A)
    task_id = _add("org a task")
    before = _raw(task_id)
    as_org(ORG_B)
    _same_as_missing(task_id, lambda i: task_tools.task_claim(i, agent="org_b_agent"))
    after = _raw(task_id)
    assert after["claimed_by"] is None
    assert after["status"] == before["status"] == "open"


def test_usage_heartbeat_cannot_land_on_another_tenants_task(as_org):
    """Agent labels are not tenant-scoped, so the fence is the task's tenant:
    the same label replaying its own org-A run from org B is refused."""
    as_org(ORG_A)
    task_id = _add("org a task")
    token = config.agent_identity_var.set("shared_agent")
    try:
        run_id = task_tools.task_claim(task_id, agent="shared_agent")["current_run_id"]
        as_org(ORG_B)
        heartbeat = lambda i: task_tools.task_claim(  # noqa: E731
            i,
            agent="shared_agent",
            usage_only=True,
            expected_run_id=run_id,
            run_usage={"input_tokens": 7},
        )
        assert _error(lambda: heartbeat(task_id)) == f"no task with id {task_id}"
        as_org(ORG_A)
        assert heartbeat(task_id)["usage_only"] is True
    finally:
        config.agent_identity_var.reset(token)


def test_list_shows_only_the_callers_tenant(as_org):
    as_org(ORG_A)
    a_id = _add("org a only")
    as_org(ORG_B)
    b_id = _add("org b only")
    for kwargs in ({}, {"status": "all"}, {"query": "only"}):
        ids = {t["id"] for t in task_tools.task_list(**kwargs)["tasks"]}
        assert a_id not in ids, kwargs
        assert b_id in ids, kwargs
    as_org(ORG_A)
    ids = {t["id"] for t in task_tools.task_list(status="all")["tasks"]}
    assert ids == {a_id}


def test_untenanted_hub_lists_untenanted_rows_only(as_org, monkeypatch):
    as_org(ORG_A)
    _add("org a row")
    monkeypatch.setenv("UA_EDITION", "core")
    plain = _add("private hub row")
    ids = {t["id"] for t in task_tools.task_list(status="all")["tasks"]}
    assert ids == {plain}


def test_project_build_of_another_tenants_spec_reads_like_missing(as_org):
    as_org(ORG_A)
    spec = _add("org a spec", notes="[project-spec]")
    as_org(ORG_B)
    token = config.agent_identity_var.set("Desktop_Agent4")
    try:
        _same_as_missing(spec, task_tools.project_build)
    finally:
        config.agent_identity_var.reset(token)


def test_comments_on_another_tenants_task_read_like_missing(as_org):
    as_org(ORG_A)
    task_id = _add("org a task")
    comment = task_comments.append_comment(task_id, "org a comment", author="a_agent")
    comment_id = comment["comment"]["id"]
    as_org(ORG_B)
    _same_as_missing(task_id, lambda i: task_comments.append_comment(i, "org b", author="b"))
    _same_as_missing(task_id, task_comments.list_comments)
    assert task_comments.get_comment(comment_id) is None
    as_org(ORG_A)
    assert task_comments.get_comment(comment_id) is not None
