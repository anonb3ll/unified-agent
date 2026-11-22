"""Platform object model registry and its MCP derivation (task #1070).

These tests lock the seam, not just the code: the object model is the single
place a resource is declared, and everything the MCP adapter used to hand-write
(URI, description, profile gate, telemetry key) must now fall out of that
declaration. The parity tests are the load-bearing ones — they pin the derived
behaviour against the exact hand-written implementation it replaced, so this
refactor cannot silently widen or narrow what a client can see.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from unified_agent import config, events, mcp_live_state, object_model, profiles, storage
from unified_agent.mcp_live_state import (
    BOARD_URI,
    RUN_URI_TEMPLATE,
    TASK_URI_TEMPLATE,
    run_uri,
    task_uri,
)
from unified_agent.server import mcp
from unified_agent.tools import approvals, tasks

_TOOLS_REFERENCE = Path(__file__).parent.parent / "docs" / "tools-reference.md"
_RESOURCE_URI_RE = re.compile(r"`(unified-agent://[^`]+)`")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _legacy_visible(uri_or_template: str) -> bool:
    """The hand-written visibility rule this registry replaced, kept verbatim.

    Frozen on purpose: it is the specification the derivation must still satisfy.
    """
    if uri_or_template == BOARD_URI or "/tasks/" in uri_or_template:
        return profiles.tool_is_allowed("task_list")
    if "/runs/" in uri_or_template:
        return profiles.tool_is_allowed("dispatch_report_self")
    return True


# ---------------------------------------------------------------------------
# Registry invariants
# ---------------------------------------------------------------------------


class TestRegistryInvariants:
    def test_registry_is_sound(self):
        assert object_model.validate_registry() == []

    def test_kinds_uris_and_names_are_unique(self):
        kinds = [ot.kind for ot in object_model.OBJECT_TYPES]
        uris = [ot.uri_template for ot in object_model.OBJECT_TYPES]
        names = [ot.name for ot in object_model.OBJECT_TYPES]
        assert len(set(kinds)) == len(kinds)
        assert len(set(uris)) == len(uris)
        assert len(set(names)) == len(names)

    def test_every_uri_is_inside_the_platform_namespace(self):
        for object_type in object_model.OBJECT_TYPES:
            assert object_type.uri_template.startswith("unified-agent://")

    def test_id_patterns_compile(self):
        for object_type in object_model.OBJECT_TYPES:
            if object_type.id_pattern is not None:
                re.compile(object_type.id_pattern)

    def test_render_extract_round_trips_for_the_legacy_ids(self):
        for kind, sample in {"task": "7", "run": "run-1"}.items():
            object_type = object_model.by_kind(kind)
            assert object_type.extract_id(object_type.render(sample)) == sample

    def test_validator_actually_rejects_broken_declarations(self, monkeypatch):
        """A validator that never fires is theatre — prove each rule bites."""
        good = object_model.by_kind("task")

        cases = {
            "duplicate kind": (good, replace(good, uri_template="unified-agent://other/{task_id}")),
            "not in lifecycle": (replace(good, terminal_states=("nonsense",)), good),
            "served=True but reader_path is None": (
                replace(good, reader_path=None),
                good,
            ),
            "collection URIs must not declare an id_param": (
                replace(object_model.by_kind("board"), id_param="task_id"),
                good,
            ),
            "outside the unified-agent:// namespace": (
                replace(good, uri_template="http://example.com/tasks/{task_id}"),
                good,
            ),
        }

        for expectation, declarations in cases.items():
            monkeypatch.setattr(object_model, "OBJECT_TYPES", declarations)
            problems = object_model.validate_registry()
            assert problems, f"{expectation!r} produced no findings"
            assert any(expectation in problem for problem in problems), (
                f"expected a finding mentioning {expectation!r}, got {problems}"
            )

    def test_legacy_uris_are_byte_identical(self):
        """The three resources task #823 shipped must not move."""
        assert BOARD_URI == "unified-agent://board"
        assert TASK_URI_TEMPLATE == "unified-agent://tasks/{task_id}"
        assert RUN_URI_TEMPLATE == "unified-agent://runs/{run_id}"
        assert object_model.BOARD_URI == BOARD_URI
        assert object_model.TASK_URI_TEMPLATE == TASK_URI_TEMPLATE
        assert object_model.RUN_URI_TEMPLATE == RUN_URI_TEMPLATE

    def test_legacy_uri_helpers_are_unchanged(self):
        assert task_uri(42) == "unified-agent://tasks/42"
        # Historically task_uri coerced with int(); that tolerance is preserved.
        assert task_uri("42") == "unified-agent://tasks/42"  # type: ignore[arg-type]
        assert run_uri("run-1") == "unified-agent://runs/run-1"

    def test_served_set_is_the_full_object_model(self):
        """Guards against a type being flipped to served=True without a reader.

        Task #1072 wired approval + evidence_event (the auditor-facing pair),
        then plan, delegation, agent, session, and memory_note. Task #1198
        added agent_token. Task #1278 added work_product. Every declared type
        is now served; an edit here is the signal that the served surface
        genuinely changed.
        """
        assert [ot.kind for ot in object_model.served_types()] == [
            "board",
            "task",
            "run",
            "approval",
            "evidence_event",
            "plan",
            "delegation",
            "agent",
            "session",
            "memory_note",
            "agent_token",
            "work_product",
            "org_unit",
            "goal",
            "comment",
            "org_proposal",
        ]

    def test_served_types_all_publish_a_schema_and_a_gate(self):
        for object_type in object_model.served_types():
            assert object_type.schema, object_type.kind
            assert object_type.profile_tool, object_type.kind
            assert callable(object_type.reader())

    def test_write_seams_are_declared_for_mutable_object_types(self):
        assert object_model.by_kind("task").writers == {
            "add": "unified_agent.tools.tasks:task_add",
            "update": "unified_agent.tools.tasks:task_update",
            "claim": "unified_agent.tools.tasks:task_claim",
        }
        assert object_model.by_kind("approval").writers == {
            "request": "unified_agent.tools.approvals:check_approval_gate",
            "resolve": "unified_agent.tools.approvals:approval_resolve",
        }
        assert object_model.by_kind("evidence_event").writers == {
            "append": "unified_agent.events:record_event_once",
        }
        assert object_model.by_kind("agent_token").writers == {
            "issue": "unified_agent.tools.token_lifecycle:agent_token_issue",
            "rotate": "unified_agent.tools.token_lifecycle:agent_token_rotate",
            "claim": "unified_agent.tools.token_lifecycle:deployment_claim",
        }
        assert object_model.by_kind("work_product").writers == {
            "submit": "unified_agent.tools.work_product:work_product_submit",
        }
        assert object_model.by_kind("goal").writers == {
            "add": "unified_agent.goals:goal_add",
            "link": "unified_agent.goals:link_task",
        }
        # Task #2480: the operator ends another identity's session (governed writer).
        assert object_model.by_kind("session").writers == {
            "operator_end": "unified_agent.tools.registry:session_operator_end",
        }
        assert object_model.by_kind("comment").writers == {
            "append": "unified_agent.task_comments:append_comment",
        }
        assert object_model.by_kind("org_proposal").writers == {
            "propose": "unified_agent.self_organization:propose_org_change",
            "review": "unified_agent.self_organization:review_org_proposal",
            "apply": "unified_agent.self_organization:apply_org_proposal",
            "reject": "unified_agent.self_organization:reject_org_proposal",
        }
        assert "update" not in object_model.by_kind("comment").writers
        assert "delete" not in object_model.by_kind("comment").writers
        for object_type in object_model.OBJECT_TYPES:
            if object_type.kind not in {
                "task",
                "session",
                "approval",
                "evidence_event",
                "agent_token",
                "work_product",
                "goal",
                "comment",
                "org_proposal",
            }:
                assert object_type.writers == {}

    def test_write_seams_resolve_to_callables(self):
        for kind, actions in {
            "task": ("add", "update", "claim"),
            "approval": ("request", "resolve"),
            "evidence_event": ("append",),
            "agent_token": ("issue", "rotate", "claim"),
            "work_product": ("submit",),
            "goal": ("add", "link"),
            "comment": ("append",),
            "org_proposal": ("propose", "review", "apply", "reject"),
        }.items():
            object_type = object_model.by_kind(kind)
            for action in actions:
                assert callable(object_type.writer(action))


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


class TestAddressing:
    def test_render_validates_the_id(self):
        task = object_model.by_kind("task")
        assert task.render(7) == "unified-agent://tasks/7"
        with pytest.raises(ValueError, match="invalid task_id"):
            task.render(0)
        with pytest.raises(ValueError, match="invalid task_id"):
            task.render("not-an-id")

    def test_collection_cannot_be_rendered(self):
        with pytest.raises(ValueError, match="not addressed by id"):
            object_model.by_kind("board").render(1)

    def test_extract_id_round_trips(self):
        task = object_model.by_kind("task")
        assert task.extract_id("unified-agent://tasks/123") == "123"
        assert task.extract_id("unified-agent://tasks/") is None
        assert task.extract_id(BOARD_URI) is None

    def test_matches_uri_covers_template_and_instance(self):
        task = object_model.by_kind("task")
        assert task.matches_uri(TASK_URI_TEMPLATE)
        assert task.matches_uri(task_uri(5))
        assert not task.matches_uri(BOARD_URI)
        assert not task.matches_uri("unified-agent://runs/x")

    def test_match_uri_prefers_exact_template_then_instance(self):
        def kind_of(uri: str) -> str:
            match = object_model.match_uri(uri)
            assert match is not None, uri
            return match.kind

        assert kind_of(BOARD_URI) == "board"
        assert kind_of(TASK_URI_TEMPLATE) == "task"
        assert kind_of(task_uri(9)) == "task"
        assert kind_of(RUN_URI_TEMPLATE) == "run"
        assert object_model.match_uri("unified-agent://nothing/here") is None

    def test_agents_are_addressed_by_label_not_rowid(self):
        agent = object_model.by_kind("agent")
        assert agent.id_param == "agent_label"
        assert agent.render("Desktop_Agent5").endswith("/Desktop_Agent5")


# ---------------------------------------------------------------------------
# Visibility parity — the derivation must not change who sees what
# ---------------------------------------------------------------------------


_LEGACY_SURFACE = (BOARD_URI, TASK_URI_TEMPLATE, RUN_URI_TEMPLATE)


class TestVisibilityParity:
    @pytest.mark.parametrize(
        "allowed",
        [
            {"task_list", "dispatch_report_self"},
            {"task_list"},
            {"dispatch_report_self"},
            set(),
        ],
    )
    def test_parity_across_permission_states(self, allowed):
        candidates = [
            *_LEGACY_SURFACE,
            task_uri(1),
            run_uri("r1"),
            "unified-agent://unknown/thing",
        ]
        with patch.object(profiles, "tool_is_allowed", side_effect=lambda name: name in allowed):
            for uri in candidates:
                assert mcp_live_state.resource_visible(uri) == _legacy_visible(uri), uri

    @pytest.mark.parametrize(
        "identity",
        [
            "Desktop_Agent1",
            "Desktop_Agent5",
            "Laptop_Agent1",
            "Desktop_Agent8",
            "definitely_not_a_registered_agent",
        ],
    )
    def test_parity_against_real_profiles(self, identity):
        token = config.agent_identity_var.set(identity)
        try:
            for uri in (*_LEGACY_SURFACE, task_uri(3), run_uri("r3")):
                assert mcp_live_state.resource_visible(uri) == _legacy_visible(uri), (
                    f"{identity} / {uri}"
                )
        finally:
            config.agent_identity_var.reset(token)

    def test_unknown_uris_keep_the_historical_default(self):
        with patch.object(profiles, "tool_is_allowed", return_value=False):
            assert mcp_live_state.resource_visible("unified-agent://never-declared") is True

    def test_declared_gate_is_the_one_consulted(self):
        with patch.object(profiles, "tool_is_allowed", return_value=True) as allowed:
            mcp_live_state.resource_visible(RUN_URI_TEMPLATE)
        allowed.assert_called_once_with("dispatch_report_self")


# ---------------------------------------------------------------------------
# Derivation: the registered MCP surface is the registry's
# ---------------------------------------------------------------------------


class TestRegistrationDerivation:
    @pytest.mark.anyio
    async def test_registered_templates_match_served_types(self):
        templates = await mcp.list_resource_templates()
        registered = {str(template.uri_template) for template in templates}
        expected = {ot.uri_template for ot in object_model.served_types() if not ot.collection}
        assert registered == expected

    @pytest.mark.anyio
    async def test_registered_static_resources_match_served_collections(self):
        resources = await mcp.list_resources()
        registered = {str(resource.uri) for resource in resources}
        expected = {ot.uri_template for ot in object_model.served_types() if ot.collection}
        assert registered == expected

    @pytest.mark.anyio
    async def test_unserved_declarations_are_not_registered(self):
        """Declared-but-unserved types are roadmap entries, never a live surface."""
        templates = await mcp.list_resource_templates()
        resources = await mcp.list_resources()
        live = {str(t.uri_template) for t in templates} | {str(r.uri) for r in resources}
        for object_type in object_model.OBJECT_TYPES:
            if object_type.served:
                continue
            assert object_type.uri_template not in live, object_type.kind

    @pytest.mark.anyio
    async def test_registered_metadata_comes_from_the_declaration(self):
        templates = {str(t.uri_template): t for t in await mcp.list_resource_templates()}
        task = object_model.by_kind("task")
        registered = templates[TASK_URI_TEMPLATE]
        assert registered.name == task.name
        assert registered.title == task.title
        assert registered.description == task.description


# ---------------------------------------------------------------------------
# The derived handler
# ---------------------------------------------------------------------------


class TestDerivedHandler:
    def test_template_handler_declares_exactly_the_declared_parameter(self):
        task = object_model.by_kind("task")
        signature = inspect.signature(mcp_live_state._handler_for(task))
        assert list(signature.parameters) == ["task_id"]

    def test_static_handler_declares_no_parameters(self):
        board = object_model.by_kind("board")
        signature = inspect.signature(mcp_live_state._handler_for(board))
        assert list(signature.parameters) == []

    def test_handler_dispatches_to_the_declared_reader(self):
        sentinel = {"uri": TASK_URI_TEMPLATE, "task": {"id": 1}}
        task = object_model.by_kind("task")
        with patch.object(object_model.ObjectType, "reader", return_value=lambda **kw: sentinel):
            handler = mcp_live_state._handler_for(task)
            assert handler(task_id="1") == sentinel

    def test_handler_identity_is_the_declaration(self):
        for object_type in object_model.served_types():
            handler = mcp_live_state._handler_for(object_type)
            assert handler.__name__ == object_type.name
            assert handler.__doc__ == object_type.description


# ---------------------------------------------------------------------------
# Telemetry derivation
# ---------------------------------------------------------------------------


class TestTelemetryDerivation:
    def test_counts_are_derived_from_declared_telemetry_keys(self):
        server = MagicMock()
        server._subscriptions._listeners = {
            BOARD_URI: [1, 2],
            task_uri(1): [1],
            run_uri("r1"): [1, 2, 3],
            "unified-agent://undeclared/thing": [1],
        }
        with patch("unified_agent.mcp_runtime_telemetry.record_subscription_counts") as record:
            mcp_live_state.update_subscription_telemetry(server)
        record.assert_called_once_with(total=7, board=2, tasks=1, runs=3)


# ---------------------------------------------------------------------------
# The manifest — the platform contract, not an MCP detail
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_is_plain_json(self):
        payload = object_model.manifest()
        assert payload["schema_version"] == object_model.MANIFEST_SCHEMA_VERSION
        assert payload["product"] == "unified-agent"
        # Round-tripping proves no callable or exotic object leaked in.
        assert json.loads(json.dumps(payload)) == payload

    def test_manifest_publishes_writer_paths(self):
        entries = {entry["kind"]: entry for entry in object_model.manifest()["object_types"]}
        assert entries["task"]["writers"]["claim"].endswith(":task_claim")
        assert entries["approval"]["writers"]["resolve"].endswith(":approval_resolve")
        assert entries["evidence_event"]["writers"]["append"].endswith(":record_event_once")

    def test_run_manifest_declares_cancellation_event(self):
        entries = {entry["kind"]: entry for entry in object_model.manifest()["object_types"]}
        assert "selfpickup.cancel_requested" in entries["run"]["event_kinds"]
        assert "selfpickup.usage_reported" in entries["run"]["event_kinds"]

    def test_manifest_leaks_no_callables(self):
        def walk(node):
            if isinstance(node, dict):
                for value in node.values():
                    yield from walk(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    yield from walk(value)
            else:
                yield node

        assert [n for n in walk(object_model.manifest()) if callable(n)] == []

    def test_manifest_covers_every_declared_type_once(self):
        kinds = [entry["kind"] for entry in object_model.manifest()["object_types"]]
        assert kinds == [ot.kind for ot in object_model.OBJECT_TYPES]
        assert len(set(kinds)) == len(kinds)

    def test_manifest_marks_every_declared_type_served(self):
        entries = {e["kind"]: e for e in object_model.manifest()["object_types"]}
        assert entries["task"]["served"] is True
        assert entries["task"]["reader"] == "unified_agent.mcp_live_state:read_task"
        # Task #1072 wired the remaining readers (approval/evidence first,
        # then the other five); every declared type now resolves a real reader.
        # org_unit is sourced from the checked-in charter rather than SQLite.
        for kind in object_model.OBJECT_TYPES:
            title = kind.kind
            assert entries[title]["served"] is True, title
            assert entries[title]["reader"], title
            if title == "org_unit":
                assert entries[title]["table"] is None
            else:
                assert entries[title]["table"], f"{title} should name its backing table"
        for kind in ("plan", "delegation", "agent", "session", "memory_note"):
            assert entries[kind]["reader"].startswith("unified_agent.platform_readers:"), kind

    def test_manifest_lifecycles_come_from_the_schema(self):
        entries = {e["kind"]: e for e in object_model.manifest()["object_types"]}
        assert entries["approval"]["lifecycle"] == [
            "pending",
            "approved",
            "denied",
            "consumed",
            "revoked",
            "expired",
        ]
        assert entries["evidence_event"]["lifecycle"] == []
        assert entries["agent"]["lifecycle"] == []


def _table_rows(table: str, *, omit: set[str]) -> list[dict]:
    conn = storage.connect()
    try:
        return [
            {key: row[key] for key in row.keys() if key not in omit}
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        ]
    finally:
        conn.close()


def _run_task_path(use_writer: bool) -> tuple[list[dict], list[dict]]:
    add = object_model.by_kind("task").writer("add") if use_writer else tasks.task_add
    claim = object_model.by_kind("task").writer("claim") if use_writer else tasks.task_claim
    update = object_model.by_kind("task").writer("update") if use_writer else tasks.task_update
    task = add("writer parity", notes="fixture")
    task = claim(task["id"], agent="Desktop_Agent5", timeout_seconds=60)
    update(task["id"], append_note="updated")
    return (
        _table_rows(
            "tasks",
            omit={"created_at", "updated_at", "claimed_at", "lock_expires_at", "current_run_id"},
        ),
        _table_rows("agent_events", omit={"id", "ts"}),
    )


def test_task_writer_parity_matches_fixture_rows_and_events(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "_ensure_task_worktree", lambda *args, **kwargs: None)
    old_home = tmp_path / "old-task"
    new_home = tmp_path / "new-task"
    monkeypatch.setenv("UA_HOME", str(old_home))
    storage.init_db()
    old_rows, old_events = _run_task_path(use_writer=False)
    monkeypatch.setenv("UA_HOME", str(new_home))
    storage.init_db()
    new_rows, new_events = _run_task_path(use_writer=True)
    assert old_rows == new_rows
    assert old_events == new_events


def test_approval_writer_parity_matches_fixture_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("UA_APPROVAL_TOOLS", "run_cmd")
    old_home = tmp_path / "old-approval"
    new_home = tmp_path / "new-approval"
    monkeypatch.setenv("UA_HOME", str(old_home))
    storage.init_db()
    with pytest.raises(approvals.ApprovalPendingError):
        approvals.check_approval_gate("run_cmd", {"command": "echo parity"})
    old = approvals.approval_resolve(1, "approve", resolved_by="Operator")
    old_rows = _table_rows("approvals", omit={"created_at", "updated_at", "resolved_at"})

    monkeypatch.setenv("UA_HOME", str(new_home))
    storage.init_db()
    with pytest.raises(approvals.ApprovalPendingError):
        object_model.by_kind("approval").writer("request")("run_cmd", {"command": "echo parity"})
    new = object_model.by_kind("approval").writer("resolve")(1, "approve", resolved_by="Operator")
    new_rows = _table_rows("approvals", omit={"created_at", "updated_at", "resolved_at"})
    assert old["status"] == new["status"] == "approved"
    assert old_rows == new_rows


def test_evidence_writer_parity_matches_fixture_rows(tmp_path, monkeypatch):
    old_home = tmp_path / "old-evidence"
    new_home = tmp_path / "new-evidence"
    monkeypatch.setenv("UA_HOME", str(old_home))
    storage.init_db()
    events.record_event_once("agent.parity", payload={"path": "fixture"}, notify=False)
    old_rows = _table_rows("event_log", omit={"ulid", "created_at", "prev_hash", "entry_hash"})

    monkeypatch.setenv("UA_HOME", str(new_home))
    storage.init_db()
    object_model.by_kind("evidence_event").writer("append")(
        "agent.parity", payload={"path": "fixture"}, notify=False
    )
    new_rows = _table_rows("event_log", omit={"ulid", "created_at", "prev_hash", "entry_hash"})
    assert old_rows == new_rows


# ---------------------------------------------------------------------------
# Docs pin: tools-reference.md resources table == served_types() (task #1114)
# ---------------------------------------------------------------------------


def _parse_tools_reference_resource_uris(text: str) -> list[str]:
    """Extract URI column values from the MCP resources table in tools-reference.md.

    The section heading is the anchor; the first markdown table under it whose
    header names a URI column is the contract surface agents read. Rows are
    returned in document order so a reordering against ``served_types()`` fails.
    """
    section = re.search(
        r"^## MCP resources\b.*?(?=^## |\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert section, (
        "docs/tools-reference.md is missing an '## MCP resources …' section — "
        "if the heading was reworded, update this parser rather than deleting the pin."
    )
    table = re.search(
        r"^\| URI \|.*?\n\|[-| ]+\|\n((?:\|.*\|\n)+)",
        section.group(0),
        flags=re.MULTILINE,
    )
    assert table, (
        "docs/tools-reference.md MCP resources section has no '| URI | …' table — "
        "regenerate it from object_model.served_types(), do not weaken this check."
    )
    uris: list[str] = []
    for line in table.group(1).splitlines():
        match = _RESOURCE_URI_RE.search(line)
        if match:
            uris.append(match.group(1))
    assert uris, "MCP resources table parsed with zero URI rows"
    return uris


def _assert_tools_reference_resources_match_served() -> None:
    documented = _parse_tools_reference_resource_uris(_TOOLS_REFERENCE.read_text())
    served = [object_type.uri_template for object_type in object_model.served_types()]
    assert documented == served, (
        f"docs/tools-reference.md resources table {documented!r} diverges from "
        f"object_model.served_types() {served!r}. Regenerate the table from the "
        f"live model — do not edit this test to make it pass."
    )


class TestToolsReferenceResourcesPin:
    """Agent-facing discovery must track the object-model served set (audit gap 11)."""

    def test_tools_reference_resources_match_served_types(self):
        _assert_tools_reference_resources_match_served()

    def test_adding_a_served_kind_without_updating_the_doc_fails(self, monkeypatch):
        """Failure mode for the pin: an 11th served kind must break the equality.

        Simulated by extending the registry, not by editing the doc — so the
        assertion itself is what fails, proving the check has teeth.
        """
        extra = replace(
            object_model.by_kind("task"),
            kind="widget",
            uri_template="unified-agent://widgets/{widget_id}",
            name="widget",
            title="Widget",
            description="Synthetic 11th served kind for the docs divergence pin.",
            id_param="widget_id",
        )
        monkeypatch.setattr(
            object_model,
            "OBJECT_TYPES",
            (*object_model.OBJECT_TYPES, extra),
        )
        with pytest.raises(AssertionError, match="diverges from"):
            _assert_tools_reference_resources_match_served()
