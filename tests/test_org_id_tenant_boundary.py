"""Nullable org_id tenant boundary (task #1232).

Adds a nullable ``org_id`` column to tasks/memory/event_log/approvals/
delegations so the column *exists* ahead of a second pilot needing it. This
is explicitly NOT turning on multi-tenancy: enforcement (here, just the
stamping of a value at write time) is gated on BOTH
``UA_EDITION=enterprise`` AND an org actually being configured
(``WORKOS_ORGANIZATION_ID``, reusing the pilot boundary's own organization
setting rather than inventing a second one). Every other configuration --
including today's private hub, which sets neither -- must write
``org_id IS NULL``, identically to before this migration.

These tests cover:
  * the gate function itself (edition.tenant_org_id)
  * schema presence on a fresh DB and a migrated pre-existing DB
  * the write path WITH an org configured (org_id stamped) across all five
    tables
  * the write path WITHOUT an org configured (org_id stays NULL) across all
    five tables -- the zero-behavior-change proof for the private hub
  * migration reversibility (see test_storage_migrations.py for the
    schema-level down-path test)
"""

from __future__ import annotations

import sqlite3

import pytest

from unified_agent import edition, events, storage
from unified_agent.tools.approvals import (
    ApprovalPendingError,
    approval_resolve,
    check_approval_gate,
)
from unified_agent.tools.delegate import delegate
from unified_agent.tools.memory import memory_save
from unified_agent.tools.tasks import task_add

ORG_TABLES = ("tasks", "memory", "event_log", "approvals", "delegations")


def _org_id_column(db_path, table: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    finally:
        conn.close()
    return "org_id" in cols


# ---------------------------------------------------------------------------
# The gate: edition.tenant_org_id()
# ---------------------------------------------------------------------------


class TestTenantOrgIdGate:
    def test_default_configuration_has_no_org(self, monkeypatch):
        """Private hub default: no UA_EDITION, no WORKOS_ORGANIZATION_ID."""
        monkeypatch.delenv("UA_EDITION", raising=False)
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
        monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
        assert edition.current_org_id() is None
        assert edition.tenant_org_id() is None

    def test_enterprise_without_org_is_still_none(self, monkeypatch):
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
        monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
        assert edition.tenant_org_id() is None

    def test_org_configured_without_enterprise_is_none(self, monkeypatch):
        """Core edition never stamps org_id, even if an org id is set."""
        monkeypatch.setenv("UA_EDITION", "core")
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_pilot_2")
        assert edition.tenant_org_id() is None

    def test_enterprise_and_org_configured_is_enforced(self, monkeypatch):
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_pilot_2")
        assert edition.tenant_org_id() == "org_pilot_2"

    def test_blank_org_env_is_treated_as_unconfigured(self, monkeypatch):
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "   ")
        monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
        assert edition.current_org_id() is None
        assert edition.tenant_org_id() is None

    def test_hosted_workos_org_is_the_tenant_when_pilot_org_is_unset(self, monkeypatch):
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
        monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org_01HOSTED")
        assert edition.current_org_id() == "org_01HOSTED"
        assert edition.tenant_org_id() == "org_01HOSTED"

    def test_blank_pilot_org_falls_through_to_the_hosted_org(self, monkeypatch):
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "   ")
        monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org_01HOSTED")
        assert edition.tenant_org_id() == "org_01HOSTED"

    def test_explicit_pilot_org_wins_over_the_hosted_org(self, monkeypatch):
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_pilot_2")
        monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org_01HOSTED")
        assert edition.tenant_org_id() == "org_pilot_2"

    def test_core_edition_does_not_stamp_the_hosted_org(self, monkeypatch):
        monkeypatch.setenv("UA_EDITION", "core")
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
        monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org_01HOSTED")
        assert edition.tenant_org_id() is None


# ---------------------------------------------------------------------------
# Schema presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", ORG_TABLES)
def test_fresh_db_has_org_id_column(table, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    assert _org_id_column(tmp_path / "memory.db", table)


@pytest.mark.parametrize("table", ORG_TABLES)
def test_migration_backfills_org_id_on_a_pre_existing_db(table, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    db = tmp_path / "memory.db"
    # Simulate "a DB that predates this migration" via the tested downgrade
    # path itself (it drops the org_id index before the column -- SQLite
    # refuses to drop an indexed column otherwise), not raw ALTER TABLE.
    conn = storage.connect()
    try:
        storage.downgrade_org_id_columns(conn)
        conn.commit()
    finally:
        conn.close()
    assert not _org_id_column(db, table)

    storage.init_db()  # ONE call, as a service restart does

    assert _org_id_column(db, table)


@pytest.mark.parametrize("table", ORG_TABLES)
def test_migration_is_idempotent(table, tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    storage._connection = None
    storage.init_db()
    storage.init_db()
    storage.init_db()
    assert _org_id_column(tmp_path / "memory.db", table)


# ---------------------------------------------------------------------------
# Write path — WITH an org configured
# ---------------------------------------------------------------------------


def _row(table: str, id_col: str, id_val) -> dict:
    conn = storage.connect()
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE {id_col}=?", (id_val,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    return {k: row[k] for k in row.keys()}


class TestWritePathWithOrgConfigured:
    def test_task_add_stamps_org_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_pilot_2")
        storage._connection = None
        storage.init_db()
        result = task_add("org-scoped task")
        row = _row("tasks", "id", result["id"])
        assert row["org_id"] == "org_pilot_2"

    def test_memory_save_stamps_org_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_pilot_2")
        storage._connection = None
        storage.init_db()
        result = memory_save(content="org-scoped note")
        row = _row("memory", "id", result["id"])
        assert row["org_id"] == "org_pilot_2"

    def test_record_event_stamps_org_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_pilot_2")
        storage._connection = None
        storage.init_db()
        ulid, _won = events.record_event_once(
            "agent.parity", payload={"scoped": True}, notify=False
        )
        row = _row("event_log", "ulid", ulid)
        assert row["org_id"] == "org_pilot_2"

    def test_approval_gate_stamps_org_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_pilot_2")
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        storage._connection = None
        storage.init_db()
        with pytest.raises(ApprovalPendingError) as exc_info:
            check_approval_gate("run_cmd", {"command": "echo org-scoped"})
        approval_id = exc_info.value.approval_id
        row = _row("approvals", "id", approval_id)
        assert row["org_id"] == "org_pilot_2"
        # resolving does not clear the stamp
        approval_resolve(approval_id, "approve", resolved_by="Operator")
        row = _row("approvals", "id", approval_id)
        assert row["org_id"] == "org_pilot_2"

    def test_delegate_stamps_org_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        monkeypatch.setenv("UA_EDITION", "enterprise")
        monkeypatch.setenv("WORKOS_ORGANIZATION_ID", "org_pilot_2")
        storage._connection = None
        storage.init_db()
        result = delegate(
            task_spec="Rebase the fix branch onto main",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
            callback="none",
        )
        row = _row("delegations", "id", result["delegation_id"])
        assert row["org_id"] == "org_pilot_2"


def _hosted_org(monkeypatch, tmp_path):
    """Enterprise hub whose only organization id is the hosted WorkOS org."""
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.setenv("UA_EDITION", "enterprise")
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    monkeypatch.setenv("UA_HUB_WORKOS_ORGANIZATION_ID", "org_01HOSTED")
    storage._connection = None
    storage.init_db()


class TestHostedWorkOSOrgStampsEveryWritePath:
    """The five tables that stamp edition.tenant_org_id(), with only the
    hosted variable set — the Railway hub's configuration."""

    def test_task_add_stamps_hosted_org(self, tmp_path, monkeypatch):
        _hosted_org(monkeypatch, tmp_path)
        result = task_add("hosted task")
        assert _row("tasks", "id", result["id"])["org_id"] == "org_01HOSTED"

    def test_memory_save_stamps_hosted_org(self, tmp_path, monkeypatch):
        _hosted_org(monkeypatch, tmp_path)
        result = memory_save(content="hosted note")
        assert _row("memory", "id", result["id"])["org_id"] == "org_01HOSTED"

    def test_record_event_stamps_hosted_org(self, tmp_path, monkeypatch):
        _hosted_org(monkeypatch, tmp_path)
        ulid, _won = events.record_event_once(
            "agent.parity", payload={"hosted": True}, notify=False
        )
        assert _row("event_log", "ulid", ulid)["org_id"] == "org_01HOSTED"

    def test_approval_gate_stamps_hosted_org(self, tmp_path, monkeypatch):
        _hosted_org(monkeypatch, tmp_path)
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        with pytest.raises(ApprovalPendingError) as exc_info:
            check_approval_gate("run_cmd", {"command": "echo hosted"})
        assert _row("approvals", "id", exc_info.value.approval_id)["org_id"] == "org_01HOSTED"

    def test_delegate_stamps_hosted_org(self, tmp_path, monkeypatch):
        _hosted_org(monkeypatch, tmp_path)
        result = delegate(
            task_spec="Rebase the fix branch onto main",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
            callback="none",
        )
        assert _row("delegations", "id", result["delegation_id"])["org_id"] == "org_01HOSTED"


# ---------------------------------------------------------------------------
# Write path — WITHOUT an org configured (the private hub today): the
# zero-behavior-change proof. Every one of these asserts org_id IS NULL,
# which is the same as saying "unchanged from before this migration" since
# no prior version of this table had the column at all.
# ---------------------------------------------------------------------------


class TestWritePathWithoutOrgConfigured:
    """Covers every UA_EDITION value the private hub might plausibly run
    under, including the repo default (unset -> enterprise), to prove org_id
    depends on an org being CONFIGURED, not merely on the edition string."""

    @pytest.mark.parametrize("edition_value", [None, "core", "enterprise"])
    def test_task_add_leaves_org_id_null(self, edition_value, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
        monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
        if edition_value is None:
            monkeypatch.delenv("UA_EDITION", raising=False)
        else:
            monkeypatch.setenv("UA_EDITION", edition_value)
        storage._connection = None
        storage.init_db()
        result = task_add("private-hub task")
        row = _row("tasks", "id", result["id"])
        assert row["org_id"] is None

    @pytest.mark.parametrize("edition_value", [None, "core", "enterprise"])
    def test_memory_save_leaves_org_id_null(self, edition_value, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
        monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
        if edition_value is None:
            monkeypatch.delenv("UA_EDITION", raising=False)
        else:
            monkeypatch.setenv("UA_EDITION", edition_value)
        storage._connection = None
        storage.init_db()
        result = memory_save(content="private-hub note")
        row = _row("memory", "id", result["id"])
        assert row["org_id"] is None

    @pytest.mark.parametrize("edition_value", [None, "core", "enterprise"])
    def test_record_event_leaves_org_id_null(self, edition_value, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
        monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
        if edition_value is None:
            monkeypatch.delenv("UA_EDITION", raising=False)
        else:
            monkeypatch.setenv("UA_EDITION", edition_value)
        storage._connection = None
        storage.init_db()
        ulid, _won = events.record_event_once(
            "agent.parity", payload={"scoped": False}, notify=False
        )
        row = _row("event_log", "ulid", ulid)
        assert row["org_id"] is None

    @pytest.mark.parametrize("edition_value", [None, "core", "enterprise"])
    def test_approval_gate_leaves_org_id_null(self, edition_value, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
        monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
        monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
        if edition_value is None:
            monkeypatch.delenv("UA_EDITION", raising=False)
        else:
            monkeypatch.setenv("UA_EDITION", edition_value)
        storage._connection = None
        storage.init_db()
        with pytest.raises(ApprovalPendingError) as exc_info:
            check_approval_gate("run_cmd", {"command": "echo private-hub"})
        row = _row("approvals", "id", exc_info.value.approval_id)
        assert row["org_id"] is None

    @pytest.mark.parametrize("edition_value", [None, "core", "enterprise"])
    def test_delegate_leaves_org_id_null(self, edition_value, tmp_path, monkeypatch):
        monkeypatch.setenv("UA_HOME", str(tmp_path))
        monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
        monkeypatch.delenv("UA_HUB_WORKOS_ORGANIZATION_ID", raising=False)
        if edition_value is None:
            monkeypatch.delenv("UA_EDITION", raising=False)
        else:
            monkeypatch.setenv("UA_EDITION", edition_value)
        storage._connection = None
        storage.init_db()
        result = delegate(
            task_spec="Rebase the fix branch onto main",
            from_agent="Desktop_Agent1",
            to_agent="Desktop_agent3",
            callback="none",
        )
        row = _row("delegations", "id", result["delegation_id"])
        assert row["org_id"] is None


# ---------------------------------------------------------------------------
# Zero-behavior-change: the exact rows/events written for the private-hub
# path must be identical whether or not this migration's code is present on
# the columns that existed before it. We cannot literally run "before this
# PR" code in the same test process, so the strongest available proof is:
# every non-org_id column produced by a private-hub-shaped write is exactly
# what test_object_model.py's own writer-parity fixtures already pin, AND
# org_id is NULL on every row. Together with those pre-existing parity
# tests (which this PR does not modify), this shows the new column is
# additive-only for the ungated path.
# ---------------------------------------------------------------------------


def test_private_hub_task_row_is_unchanged_apart_from_the_new_null_column(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_HOME", str(tmp_path))
    monkeypatch.delenv("UA_EDITION", raising=False)
    monkeypatch.delenv("WORKOS_ORGANIZATION_ID", raising=False)
    storage._connection = None
    storage.init_db()
    result = task_add("private hub task", notes="fixture", priority="high")
    row = _row("tasks", "id", result["id"])
    # Every column the private hub could have observed before this migration
    # keeps its expected value; org_id is the only new thing, and it is NULL.
    assert row["title"] == "private hub task"
    assert row["notes"] == "fixture"
    assert row["priority"] == "high"
    assert row["status"] == "open"
    assert row["project"] == "unified-agent"
    assert row["org_id"] is None
