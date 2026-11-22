"""Review findings on the tenant-isolation bundle (task #1834, PR review).

Four gaps the first fence pass left open:

1. LangGraph's pending-approval lookups picked the oldest matching row across
   tenants, so a second tenant was handed the first tenant's approval id.
2. A raw idempotency key spelled ``org:<tenant>\\x1f<key>`` was the same
   PRIMARY KEY as that tenant's scoped receipt.
3. Receipts written before tenant scoping use the raw key, so a scoped lookup
   missed them and a retried write ran again.
4. Identical work products from two tenants could reuse one row, and a local
   approval could then be linked onto the other tenant's row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unified_agent import config, langgraph_dispatch, storage
from unified_agent.tools import approvals, work_product
from unified_agent.tools import tasks as task_tools

ORG_A = "org_a"
ORG_B = "org_b"


@pytest.fixture
def as_org(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "enterprise")

    def _switch(org: str) -> None:
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", org)

    return _switch


def _insert_pending(tool: str, payload: dict, org: str) -> int:
    ts = storage.now()
    conn = storage.connect()
    try:
        cur = conn.execute(
            "INSERT INTO approvals (tool, payload, requested_by, status, created_at, "
            "updated_at, org_id) VALUES (?, ?, 'worker', 'pending', ?, ?, ?)",
            (tool, langgraph_dispatch.canonical_approval_payload(payload), ts, ts, org),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


# --- 1. LangGraph pending lookups ------------------------------------------------


def test_pending_tool_approval_lookup_returns_own_tenant_row(as_org):
    payload = {"id": 7, "title": "same action"}
    a_id = _insert_pending("task_update", payload, ORG_A)
    b_id = _insert_pending("task_update", payload, ORG_B)
    as_org(ORG_B)
    assert langgraph_dispatch._pending_tool_approval_id("task_update", payload) == b_id
    as_org(ORG_A)
    assert langgraph_dispatch._pending_tool_approval_id("task_update", payload) == a_id


def test_pending_adapter_approval_lookup_ignores_foreign_rows(as_org):
    envelope = {"tool": "task_update", "arguments": {"id": 7}}
    _insert_pending(langgraph_dispatch._APPROVAL_TOOL, envelope, ORG_A)
    as_org(ORG_B)
    assert langgraph_dispatch._pending_approval_id(envelope) is None


# --- 2. scoped and raw keys cannot collide --------------------------------------


def test_caller_key_with_the_scope_separator_is_refused(as_org, monkeypatch):
    as_org(ORG_A)
    scoped = storage.idempotency_storage_key("k")
    monkeypatch.setenv("UA_EDITION", "core")
    with pytest.raises(ValueError, match="separator"):
        storage.idempotency_storage_key(scoped)
    with pytest.raises(ValueError, match="separator"):
        task_tools.task_add("forged", idempotency_key=scoped)


def test_untenanted_caller_cannot_replay_a_tenant_receipt(as_org, monkeypatch):
    as_org(ORG_A)
    a = task_tools.task_add("org a task", idempotency_key="k")
    forged = f"org:{ORG_A}\x1fk"
    monkeypatch.setenv("UA_EDITION", "core")
    conn = storage.connect()
    try:
        with pytest.raises(ValueError):
            storage.get_idempotency_record(conn, forged, "task_add")
        row = conn.execute(
            "SELECT result_json FROM idempotency_keys WHERE key=?", (forged,)
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(row["result_json"])["id"] == a["id"]


# --- 3. pre-scoping receipts stop the retry -------------------------------------


def _legacy_receipt(key: str, tool: str, result: dict) -> None:
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO idempotency_keys (key, tool, result_json, created_at) VALUES (?,?,?,?)",
            (key, tool, json.dumps(result), storage.now()),
        )
        conn.commit()
    finally:
        conn.close()


def _task_count() -> int:
    conn = storage.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()


def test_legacy_receipt_refuses_task_add_retry_instead_of_repeating(as_org):
    _legacy_receipt("old-key", "task_add", {"id": 999, "title": "done before upgrade"})
    as_org(ORG_A)
    before = _task_count()
    with pytest.raises(storage.AmbiguousIdempotencyReceipt, match="new idempotency key"):
        task_tools.task_add("retry", idempotency_key="old-key")
    assert _task_count() == before


def test_legacy_receipt_refuses_claim_and_lookup(as_org):
    _legacy_receipt("old-msg", "message_send", {"message_id": "old-msg"})
    as_org(ORG_A)
    conn = storage.connect()
    try:
        with pytest.raises(storage.AmbiguousIdempotencyReceipt):
            storage.get_idempotency_record(conn, "old-msg", "message_send")
        with pytest.raises(storage.AmbiguousIdempotencyReceipt):
            storage.try_claim_idempotency(conn, "old-msg", "message_send")
    finally:
        conn.close()


def test_scoped_receipt_still_replays_beside_a_legacy_row(as_org):
    as_org(ORG_A)
    a = task_tools.task_add("org a task", idempotency_key="dup")
    _legacy_receipt("dup", "task_add", {"id": 999})
    assert task_tools.task_add("org a task", idempotency_key="dup")["id"] == a["id"]
    conn = storage.connect()
    try:
        assert storage.try_claim_idempotency(conn, "dup", "task_add") is False
    finally:
        conn.close()


def test_untenanted_hub_still_replays_raw_receipts(monkeypatch):
    monkeypatch.setenv("UA_EDITION", "core")
    first = task_tools.task_add("plain", idempotency_key="plain-key")
    assert task_tools.task_add("plain", idempotency_key="plain-key")["id"] == first["id"]


# --- 4. work products belong to the submitting tenant ---------------------------


@pytest.fixture
def work_products_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("UA_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("UA_WORK_PRODUCT_ENABLED", "1")
    monkeypatch.setenv("UA_WORK_PRODUCT_RELEASE_DIR", str(tmp_path / "released"))
    monkeypatch.delenv("UA_WORK_PRODUCT_TEMPLATE_DIR", raising=False)
    token = config.agent_identity_var.set("Desktop_Agent2")
    path = tmp_path / "workspace" / "drafts" / "memo.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Memo\n\nSynthetic draft.\n", encoding="utf-8")
    yield path
    config.agent_identity_var.reset(token)


def _park(path: Path) -> int:
    with pytest.raises(approvals.ApprovalPendingError) as pending:
        work_product.work_product_submit(
            str(path),
            "Synthetic memo",
            "legal",
            tags={"matter": "M-0001-synthetic", "client": "Example Client LLC"},
            model="model-x",
        )
    return int(pending.value.approval_id)


def _product_for(approval_id: int) -> int:
    conn = storage.connect()
    try:
        row = conn.execute(
            "SELECT id FROM work_products WHERE approval_id=?", (approval_id,)
        ).fetchone()
    finally:
        conn.close()
    return int(row["id"])


def test_identical_submissions_from_two_tenants_get_separate_rows(as_org, work_products_enabled):
    as_org(ORG_A)
    a_approval = _park(work_products_enabled)
    a_product = _product_for(a_approval)
    as_org(ORG_B)
    b_approval = _park(work_products_enabled)
    b_product = _product_for(b_approval)
    assert b_approval != a_approval
    assert b_product != a_product
    # Org B cannot read, list or diff org A's row; it behaves as missing.
    assert work_product.get_work_product(a_product) is None
    listed = {row["id"] for row in work_product.list_work_products()["work_products"]}
    assert listed == {b_product}
    assert work_product.get_work_product(b_product)["previous_version_id"] is None
    as_org(ORG_A)
    assert work_product.get_work_product(a_product)["approval_id"] == a_approval


def test_local_approval_cannot_be_linked_onto_a_foreign_row(as_org, work_products_enabled):
    as_org(ORG_A)
    a_product = _product_for(_park(work_products_enabled))
    as_org(ORG_B)
    b_approval = _park(work_products_enabled)
    payload = json.loads(
        approvals.get_approval(b_approval)["payload"]
        if isinstance(approvals.get_approval(b_approval)["payload"], str)
        else json.dumps(approvals.get_approval(b_approval)["payload"])
    )
    with pytest.raises(ToolError, match="disappeared"):
        work_product._link_approval(a_product, b_approval, payload)
    as_org(ORG_A)
    assert work_product.get_work_product(a_product)["approval_id"] != b_approval


def test_work_products_carry_the_submitting_tenant(as_org, work_products_enabled):
    as_org(ORG_A)
    a_product = _product_for(_park(work_products_enabled))
    conn = storage.connect()
    try:
        row = conn.execute("SELECT org_id FROM work_products WHERE id=?", (a_product,)).fetchone()
    finally:
        conn.close()
    assert row["org_id"] == ORG_A


def test_work_product_org_column_backfills_from_submitted_events(as_org, work_products_enabled):
    as_org(ORG_A)
    a_product = _product_for(_park(work_products_enabled))
    conn = storage.connect()
    try:
        conn.execute("DROP INDEX IF EXISTS idx_work_products_org_id")
        conn.execute("ALTER TABLE work_products DROP COLUMN org_id")
        storage._add_work_product_org_id(conn)
        storage._add_work_product_org_id(conn)  # idempotent
        conn.commit()
        row = conn.execute("SELECT org_id FROM work_products WHERE id=?", (a_product,)).fetchone()
    finally:
        conn.close()
    assert row["org_id"] == ORG_A
    assert work_product.get_work_product(a_product) is not None


# --- review follow-ups ------------------------------------------------------------


def test_autonomy_approval_lookup_ignores_foreign_decisions(as_org):
    from unified_agent import autonomy_modes

    payload = {"mission_id": "m-1", "action": "merge"}
    ts = storage.now()
    conn = storage.connect()
    try:
        conn.execute(
            "INSERT INTO approvals (tool, payload, requested_by, status, resolved_by, "
            "resolved_at, created_at, updated_at, org_id) "
            "VALUES (?, ?, 'worker', 'denied', 'operator', ?, ?, ?, ?)",
            (
                "mission_action",
                approvals.canonical_approval_payload(payload),
                ts,
                ts,
                ts,
                ORG_A,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    as_org(ORG_B)
    assert autonomy_modes._read_only_approval_lookup("mission_action", payload) is None
    as_org(ORG_A)
    assert autonomy_modes._read_only_approval_lookup("mission_action", payload)["status"] == (
        "denied"
    )


def test_message_id_used_by_another_tenant_is_its_own_message(as_org):
    """Since #1979 the mailbox index is per tenant: no refusal, and no disclosure."""
    from unified_agent.tools import messages as message_tools

    token = config.agent_identity_var.set("Desktop_Agent1")
    try:
        as_org(ORG_A)
        a = message_tools.message_send(
            act="inform", to_agent="Desktop_Agent5", body="a", message_id="shared-id"
        )
        as_org(ORG_B)
        b = message_tools.message_send(
            act="inform", to_agent="Desktop_Agent5", body="b", message_id="shared-id"
        )
        assert b["accepted"] is True and b["duplicate"] is False
        assert b["event_ulid"] != a["event_ulid"]
        # Inside one tenant the id is still taken: a retry replays that tenant's receipt.
        again = message_tools.message_send(
            act="inform", to_agent="Desktop_Agent2", body="c", message_id="shared-id"
        )
        assert again["duplicate"] is True
        assert again["event_ulid"] == b["event_ulid"]
    finally:
        config.agent_identity_var.reset(token)


def test_pre_tenant_database_upgrades_with_work_products(as_org, work_products_enabled):
    """A database older than every tenant column still starts (#1834 review).

    The work-product backfill reads event_log.org_id, so it must run after the
    migration that adds that column, not before it.
    """
    as_org(ORG_A)
    a_product = _product_for(_park(work_products_enabled))
    conn = storage.connect()
    try:
        storage.downgrade_org_id_columns(conn)
        conn.execute("DROP INDEX IF EXISTS idx_work_products_org_id")
        conn.execute("ALTER TABLE work_products DROP COLUMN org_id")
        conn.commit()
        assert "org_id" not in {
            r["name"] for r in conn.execute("PRAGMA table_info(event_log)").fetchall()
        }
        storage._migrate(conn)
        conn.commit()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(work_products)").fetchall()}
        row = conn.execute("SELECT org_id FROM work_products WHERE id=?", (a_product,)).fetchone()
    finally:
        conn.close()
    assert "org_id" in cols
    # The pre-tenant event carried no tenant, so the row stays untenanted.
    assert row["org_id"] is None
