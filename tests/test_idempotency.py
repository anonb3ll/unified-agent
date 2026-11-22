"""Tests for write tool idempotency (Task #429)."""

import pytest

from unified_agent import storage
from unified_agent.tools import memory as memory_tools
from unified_agent.tools import tasks as task_tools
from unified_agent.tools.memory import memory_save
from unified_agent.tools.tasks import task_add, task_update


def test_memory_save_idempotency_key(isolated_env):
    """Calling memory_save with the same idempotency key returns the cached result."""
    storage.init_db()
    res1 = memory_save(
        "Persisted note content",
        title="Idempotent Note",
        idempotency_key="mem-save-key-001",
    )
    assert res1["created"] is True
    note_id = res1["id"]

    # Re-call with same idempotency key
    res2 = memory_save(
        "Persisted note content",
        title="Idempotent Note",
        idempotency_key="mem-save-key-001",
    )
    assert res2["id"] == note_id
    assert res2["created"] is True

    # Verify only one note was created in DB
    conn = storage.connect()
    try:
        count = conn.execute(
            "SELECT count(*) as c FROM memory WHERE title='Idempotent Note'"
        ).fetchone()["c"]
        assert count == 1
    finally:
        conn.close()


def test_task_add_idempotency_key(isolated_env):
    """Calling task_add with the same idempotency key returns the cached result."""
    storage.init_db()
    res1 = task_add("Idempotent Task Title", idempotency_key="task-add-key-001")
    task_id = res1["id"]

    res2 = task_add("Idempotent Task Title", idempotency_key="task-add-key-001")
    assert res2["id"] == task_id

    conn = storage.connect()
    try:
        count = conn.execute(
            "SELECT count(*) as c FROM tasks WHERE title='Idempotent Task Title'"
        ).fetchone()["c"]
        assert count == 1
    finally:
        conn.close()


def test_task_update_idempotency_key(isolated_env):
    """Calling task_update with the same idempotency key returns the cached result."""
    storage.init_db()
    task = task_add("Task to update")
    t_id = task["id"]

    res1 = task_update(
        id=t_id,
        status="doing",
        worktree=False,
        idempotency_key="task-update-key-001",
    )
    assert res1["status"] == "doing"

    res2 = task_update(
        id=t_id,
        status="doing",
        worktree=False,
        idempotency_key="task-update-key-001",
    )
    assert res2["id"] == t_id
    assert res2["status"] == "doing"


# --- tenant scope for receipts (task #1796) --------------------------------------
# Tenant scope for idempotency receipts (task #1796).
#
# ``idempotency_keys.key`` is one global PRIMARY KEY. Before this fix org_b
# replaying org_a's idempotency_key got org_a's receipt back (memory_save,
# task_add, task_update and every other storage-helper caller), and a key one
# tenant used was unusable for the other. Each tenant now has its own key
# space; the untenanted private hub keeps the stored key unchanged.


ORG_A = "org_a"
ORG_B = "org_b"


@pytest.fixture
def as_org(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")

    def _switch(org: str) -> None:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)

    return _switch


def test_memory_save_replay_does_not_cross_tenants(as_org):
    as_org(ORG_A)
    a = memory_tools.memory_save("org a body", title="org a note", idempotency_key="k")
    as_org(ORG_B)
    b = memory_tools.memory_save("org b body", title="org b note", idempotency_key="k")
    assert b["id"] != a["id"]
    assert b.get("created", True) is True
    # Each tenant's own replay still returns its own receipt.
    assert (
        memory_tools.memory_save("org b body", title="org b note", idempotency_key="k")["id"]
        == b["id"]
    )
    as_org(ORG_A)
    assert (
        memory_tools.memory_save("org a body", title="org a note", idempotency_key="k")["id"]
        == a["id"]
    )


def test_task_add_replay_does_not_cross_tenants(as_org):
    as_org(ORG_A)
    a = task_tools.task_add("org a task", idempotency_key="same")
    as_org(ORG_B)
    b = task_tools.task_add("org b task", idempotency_key="same")
    assert b["id"] != a["id"]
    assert task_tools.task_add("org b task", idempotency_key="same")["id"] == b["id"]


def test_task_update_receipt_does_not_cross_tenants(as_org):
    as_org(ORG_A)
    a_id = task_tools.task_add("org a task")["id"]
    task_tools.task_update(a_id, title="a renamed", idempotency_key="upd")
    as_org(ORG_B)
    b_id = task_tools.task_add("org b task")["id"]
    out = task_tools.task_update(b_id, title="b renamed", idempotency_key="upd")
    assert out["id"] == b_id
    assert out["title"] == "b renamed"


def test_untenanted_hub_stores_the_key_unchanged(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "core")
    assert storage.idempotency_storage_key("k") == "k"
    task_tools.task_add("plain", idempotency_key="plain-key")
    conn = storage.connect()
    try:
        row = conn.execute("SELECT key FROM idempotency_keys WHERE tool='task_add'").fetchone()
    finally:
        conn.close()
    assert row["key"] == "plain-key"


def test_tenant_key_spaces_are_distinct(as_org):
    as_org(ORG_A)
    a_key = storage.idempotency_storage_key("k")
    as_org(ORG_B)
    b_key = storage.idempotency_storage_key("k")
    assert len({a_key, b_key, "k"}) == 3
