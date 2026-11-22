"""Tenancy inventory: every SQLite table the platform creates and how it is tenanted.

Task #1433 makes the organization (``org_id``) a structural partition key. Its
first slice is this inventory plus ``tests/test_tenant_table_inventory.py``,
which fails when a table ships without an entry here. The rule is stated for
developers in ``docs/architecture/tenancy.md``.

Each table is one of four kinds:

``partitioned``
    Rows hold data owned by an organization (an agent's, a task's, a user's,
    a device's). The table must carry an ``org_id`` column and an index whose
    leading column is ``org_id``. A table that should be partitioned but has
    no ``org_id`` yet is listed in ``PARTITION_PENDING``.
``derived``
    Rows belong to exactly one row of a partitioned parent through a NOT NULL
    reference column (``via``), so they inherit that row's tenant and carry no
    ``org_id`` of their own. Reads must reach them through the parent.
``accelerator``
    A virtual search table (FTS5 or sqlite-vec) whose rows mirror rows of a
    tenant-bearing ``parent`` one to one, keyed by the parent's ``via`` column
    (``rowid`` or a rowid alias). It inherits the parent row's tenant, and a
    query must join back to the parent to apply it.
``global``
    Rows hold no tenant data at all: store metadata, process cursors and
    locks, and hub telemetry with no free text. Anything that can hold an
    agent's, task's, user's or device's text or identifier is not global.

When unsure, classify a table ``partitioned`` (with a ``PARTITION_PENDING``
entry if it has no ``org_id``): the ratchet test then reports the gap, while a
wrong ``global`` would hide it.

``store`` names the database a table lives in. Almost every table lives in the
hub store (``memory.db``); the cloud mirror receiver, the pilot datastore, the invocation receipt
schema, the disposable synthetic provider and the LangGraph checkpoint store (whose
library-owned checkpoint tables are not ours to inventory) keep their own.

This module is data only. ``org_partition`` reads it once at import to learn
which tables its chokepoint covers. Task #2326 added the org_id indexes and the
key constraints (``constraint_migration``); #1433 slice 3 added the sentinel
organization and that chokepoint, off until an operator applies the backfill.
org_id NOT NULL is later #1433 work, and enabling a second organization is a
separate decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

Kind = Literal["partitioned", "derived", "accelerator", "global"]
KINDS: frozenset[str] = frozenset({"partitioned", "derived", "accelerator", "global"})
#: Kinds that inherit their tenant from a named parent row.
INHERITING: frozenset[str] = frozenset({"derived", "accelerator"})

HUB = "hub"
CLOUD_MIRROR = "cloud_mirror"
PILOT = "pilot"
INVOCATION = "invocation"
SYNTHETIC = "synthetic"
LANGGRAPH = "langgraph"
STORES: frozenset[str] = frozenset({HUB, CLOUD_MIRROR, PILOT, INVOCATION, SYNTHETIC, LANGGRAPH})


@dataclass(frozen=True, slots=True)
class TenancyEntry:
    """How one table is tenanted. ``parent``/``via`` are set only for inheriting kinds."""

    kind: Kind
    reason: str
    parent: str | None = None
    via: str | None = None
    store: str = HUB

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown tenancy kind {self.kind!r}")
        if self.store not in STORES:
            raise ValueError(f"unknown store {self.store!r}")
        if not self.reason.strip() or "\n" in self.reason:
            raise ValueError("a tenancy reason is one non-empty line")
        inherits = self.kind in INHERITING
        if inherits != bool(self.parent and self.via):
            raise ValueError(
                "derived and accelerator entries, and only they, name a parent table and column"
            )


def _part(reason: str, store: str = HUB) -> TenancyEntry:
    return TenancyEntry("partitioned", reason, store=store)


def _glob(reason: str, store: str = HUB) -> TenancyEntry:
    return TenancyEntry("global", reason, store=store)


def _der(parent: str, via: str, reason: str) -> TenancyEntry:
    return TenancyEntry("derived", reason, parent=parent, via=via)


def _acc(parent: str, via: str, reason: str) -> TenancyEntry:
    return TenancyEntry("accelerator", reason, parent=parent, via=via)


_CHAT = "chat-bridge state for an agent's conversation"


TABLE_TENANCY: MappingProxyType[str, TenancyEntry] = MappingProxyType(
    {
        # --- core domain objects: already carry org_id ---------------------
        "tasks": _part("the task queue; each task belongs to an organization"),
        "memory": _part("memory notes written by agents"),
        "event_log": _part("the evidence ledger; every event is attributable to a tenant"),
        "approvals": _part("approval requests raised by agents"),
        "delegations": _part("agent-to-agent handoffs"),
        "agent_tokens": _part("bearer tokens issued to an organization's agents"),
        "agent_sessions": _part("an agent's working sessions"),
        "work_products": _part("artifacts agents submit for release"),
        "goals": _part("an organization's goals"),
        "plans": _part("agent plans"),
        "routines": _part("scheduled routines that create tasks"),
        "sprints": _part("operating-policy sprints"),
        "task_holds": _part("holds placed over a task subtree"),
        "task_origins": _part("where a task was requested from"),
        "completion_deliveries": _part("delivery of a task's completion to its requester"),
        "workflow_instances": _part("running workflow instances"),
        "message_index": _part("agent-to-agent mailbox"),
        "session_credentials": _part("credentials minted for an agent run"),
        "org_proposals": _part("self-organization proposals"),
        "session_handoff_notes": _part("an agent's handoff notes for its own session"),
        "context_epochs": _part("context epochs of an agent session"),
        "harness_config_bundles": _part("published harness configuration bundles"),
        "harness_config_proposals": _part("proposed harness configuration changes"),
        "connector_oauth_pending": _part("a connector OAuth flow in progress for an org"),
        "connector_oauth_connections": _part("an org's connected OAuth providers"),
        "governed_servers": _part("MCP servers an organization governs"),
        "approval_slack_outbox": _part("approval notifications queued for Slack"),
        "approval_receipt_intents": _part(
            "approval decision receipts awaiting delivery to the requester"
        ),
        "autonomy_mission_mode": _part("autonomy mode of a mission"),
        "autonomy_mode_revisions": _part("history of a mission's autonomy mode"),
        "autonomy_pending_actions": _part("actions awaiting an autonomy decision"),
        "autonomy_standing_grants": _part("standing grants on a mission"),
        "memory_chunks": _part("chunk embeddings of memory notes, fenced by org"),
        "memory_entities": _part("entities extracted from memory notes, fenced by org"),
        "memory_links": _part("the memory graph's edges, fenced by org"),
        "llm_broker_spend": _part("LLM broker spend reserved per org and agent"),
        "invocation_receipts": _part("idempotent invocation receipts", store=INVOCATION),
        "effects": _part(
            "durable effects of one organization's disposable synthetic acceptance",
            store=SYNTHETIC,
        ),
        "sends": _part(
            "send markers for one organization's disposable synthetic acceptance",
            store=SYNTHETIC,
        ),
        # --- derived: inherit the tenant of one parent row -------------------
        "memory_versions": _der("memory", "note_id", "prior versions of one memory note"),
        "memory_embeddings": _der("memory", "note_id", "embedding of one memory note"),
        "plan_steps": _der("plans", "plan_id", "steps of one plan"),
        "plan_checkpoints": _der("plans", "plan_id", "checkpoints of one plan"),
        "task_dependencies": _der("tasks", "task_id", "prerequisite edges of one task"),
        "task_hold_members": _der("task_holds", "hold_id", "tasks covered by one hold"),
        "task_goal_links": _der("tasks", "task_id", "goal links of one task"),
        "task_comments": _der("tasks", "task_id", "comments on one task"),
        "task_comment_threads": _der("tasks", "task_id", "comment-thread revision of one task"),
        "task_embeddings": _der("tasks", "task_id", "embedding of one task"),
        "task_liveness": _der("tasks", "task_id", "liveness monitor of one task"),
        "run_settings": _der("tasks", "task_id", "run settings of one task"),
        "sprint_tasks": _der("sprints", "sprint_id", "tasks linked to one sprint"),
        "routine_runs": _der("routines", "routine_id", "fires of one routine"),
        "workflow_task_steps": _der("workflow_instances", "instance_id", "steps of one instance"),
        "workflow_closures": _der("workflow_instances", "instance_id", "closures of one instance"),
        "autonomy_action_reviews": _der(
            "autonomy_pending_actions", "pending_id", "review of one pending autonomy action"
        ),
        "autonomous_cycle_transitions": _der(
            "autonomous_cycles", "cycle_id", "state transitions of one autonomous cycle"
        ),
        "elicitation_challenges": _der("approvals", "approval_id", "challenge for one approval"),
        "message_reads": _der("message_index", "message_id", "read receipts of one message"),
        "workspace_symbol_embeddings": _der(
            "workspace_symbols", "symbol_id", "embedding of one indexed symbol"
        ),
        "node_followups": _der("node_dispatch", "item_id", "follow-up turns for one item"),
        "node_claim_ops": _der("nodes", "node", "claim operations issued to one node"),
        "dispatch_launch_keys": _der(
            "tasks", "task_id", "a Control Center launch's operation key bound to its run"
        ),
        "harness_config_drift": _der("nodes", "node", "config drift observed on one node"),
        "session_wake_offers": _der("session_wake", "session_id", "wake offers to one session"),
        # --- partitioned, no org_id yet (see PARTITION_PENDING) --------------
        "agents": _part("the agent roster; each agent belongs to an organization"),
        "agent_events": _part("an agent's activity events"),
        "executor_attempts": _part("executor attempts for a task or delegation"),
        "dispatch_runs": _part("dispatch runs for a task or delegation"),
        "autonomous_cycles": _part("autonomous cycles over a task"),
        "run_operating_snapshots": _part("operating-policy snapshot of an agent run"),
        "run_usage_heartbeats": _part("usage heartbeats of an agent run"),
        "usage_events": _part("token and cost usage by actor"),
        "idempotency_keys": _part("cached tool results, which can hold tenant data"),
        "anomaly_notifications": _part("anomaly alerts naming an agent or task"),
        "recall_quality_log": _part("memory recall queries, which are tenant text"),
        "memory_index_dirty": _part("index-note rebuild queue, keyed by org_key not org_id"),
        "nodes": _part("registered device nodes of an organization's fleet"),
        "node_dispatch": _part("work dispatched to a device node"),
        "host_reports": _part("doctor reports from an organization's hosts"),
        "ui_sessions": _part("Control Center login sessions of an agent or user"),
        "session_wake": _part("wake preferences of an agent session"),
        "session_friction_nudges": _part("friction nudges sent to an agent session"),
        "credential_pool": _part("provider credentials supplied by an organization"),
        "graph_canvas_scenes": _part("graph canvas boards an operator saved"),
        "project_repo_bindings": _part("an organization's project-to-repo bindings"),
        "review_reads": _part("PR review reads recorded by agents"),
        "work_proposals": _part("batches of work items an agent proposed"),
        "review_hold_decisions": _part("the operator's release or keep of a task's review hold"),
        "work_proposal_items": _part("work items of one proposal, frozen at approval"),
        "task_workflow": _part("a task's stage and resume capsule, written by its claimant"),
        "task_checks": _part("check results recorded on a task, with their proof grade"),
        "design_panels": _part("design questions put to a panel of agents"),
        "design_panel_seats": _part("sealed answers of one design panel's seats"),
        "friction_reports": _part("friction reports an agent or operator filed"),
        "friction_verdicts": _part("fix-claim and accept verdicts on a friction key"),
        "skill_versions": _part("published skill content"),
        "skill_assignments": _part("skill assignments to agents or roles"),
        "skill_sources": _part("skill source repositories"),
        "vault_ingest_ledger": _part("vault notes an agent ingested"),
        "workspace_symbols": _part("symbol index of an organization's workspaces"),
        "notify_delivery_log": _part("an agent's notification delivery attempts"),
        "pending_notifications": _part("queued digest notifications from an agent"),
        "oauth_clients": _part("OAuth clients registered with the hub"),
        "oauth_access_tokens": _part("OAuth access tokens issued to a subject"),
        "oauth_refresh_tokens": _part("OAuth refresh tokens issued to a subject"),
        "oauth_passkeys": _part("passkeys registered by a login"),
        "ts_cursors": _part(_CHAT),
        "zulip_cursors": _part(_CHAT),
        "zulip_worker_health": _part(_CHAT),
        "zulip_event_queues": _part(_CHAT),
        "zulip_bridge_state": _part(_CHAT),
        "zulip_outbound_notifications": _part(_CHAT),
        "zulip_chat_replies": _part(_CHAT),
        "zulip_chat_topics": _part(_CHAT),
        "slack_chat_replies": _part(_CHAT),
        "slack_chat_conversations": _part(_CHAT),
        "slack_outbound_slots": _part("Slack send slots; the event reference is nullable"),
        "mirror_events": _part("hub events mirrored to the cloud receiver", store=CLOUD_MIRROR),
        "memory_recall_votes": _part("recall counters; note_id is NOT NULL but has no foreign key"),
        "node_acceptance": _part("device acceptance runs; node is NOT NULL, no foreign key"),
        "agent_perf_samples": _part("tool latency samples whose error text can quote tenant data"),
        "web_search_cache": _part("cached web results keyed by an agent's query text"),
        "control_flags": _part("operator flags whose free-text reason can name tenant work"),
        "device_proof_replays": _part("anti-replay nonces naming an organization's device"),
        "memory_graph_import_ownership": _part("import ownership keyed by tenant graph rows"),
        "migration_import_row_ownership": _part("import ownership keyed by note and task ids"),
        "pilot_sessions": _part("pilot user sessions; store bound to one org", store=PILOT),
        "pilot_lifecycle": _part("pilot lifecycle events per subject", store=PILOT),
        "ui_session_workos": _part("WorkOS binding of a UI session; session_id has no foreign key"),
        "node_dispatch_cwd": _part("cwd of a dispatch item; item_id has no foreign key"),
        "node_dispatch_resume": _part(
            "resume source of a dispatch item; item_id has no foreign key"
        ),
        "node_dispatch_wait": _part("wait state of a dispatch item; item_id has no foreign key"),
        "node_command": _part("supervised command of a dispatch item; item_id has no foreign key"),
        "node_command_notice": _part(
            "owner-message outbox of a supervised command; item_id has no foreign key"
        ),
        # --- accelerators: virtual tables mirroring a parent by rowid -------
        "memory_fts": _acc("memory", "rowid", "FTS5 index over memory title and content"),
        "memory_vec": _acc("memory", "rowid", "sqlite-vec KNN mirror of memory embeddings"),
        "memory_chunk_vec": _acc("memory_chunks", "rowid", "sqlite-vec mirror of chunk vectors"),
        "task_fts": _acc("tasks", "id", "FTS5 index over task title and notes"),
        # --- global: no tenant data ------------------------------------------
        "meta": _glob("store metadata: schema version and the store's tenant binding"),
        "launchd_first_seen": _glob("first-seen times of the hub host's own launchd jobs"),
        "db_size_samples": _glob("hub database size telemetry: a timestamp and a byte count"),
        "supervisor_cursors": _glob("a supervisor process's read position in the event log"),
        "event_log_maintenance": _glob("prune lock row that never outlives its transaction"),
        "pilot_owner": _glob("binds the pilot datastore to its one organization", store=PILOT),
        "pilot_states": _glob("random login states, no identity", store=PILOT),
        "langgraph_resume_leases": _part(
            "resume leases on an agent's paused LangGraph runs", store=LANGGRAPH
        ),
    }
)


# Connection-scoped scratch tables: created only as CREATE TEMP TABLE, so they
# live in one connection's temp schema and vanish with it. Each holds ids of
# rows the calling query already selected, so it carries no tenant of its own.
# The test checks every site creates them TEMP, never in the main schema.
CONNECTION_TEMP_TABLES: MappingProxyType[str, str] = MappingProxyType(
    {
        "_ua_contradiction_targets": "memory ids the contradiction pass is checking",
        "_ua_graph_skip": "memory ids a graph walk has already visited",
        "_ua_task_search_candidates": "task ids a ranked task search is scoring",
        "_ua_semantic_candidates": "memory ids a semantic search is scoring",
        "_ua_search_candidates": "memory ids a hybrid search is scoring",
        "_ua_search_eligible": "memory ids that pass one search's eligibility filters",
        "_ua_rollback_tasks": "task ids a migration-import rollback will remove",
        "_ua_rollback_memory": "memory ids a migration-import rollback will remove",
        "_ua_rollback_graph": "graph-row keys a migration-import rollback will remove",
    }
)


_SLICE2 = "#1433 slice 2: add nullable org_id + org_id index, stamp at write, backfill"
_SLICE2_VIA = "#1433 slice 2: add org_id, or make the reference NOT NULL and reclassify derived"
_SLICE3_FK = (
    "#1433: key is NOT NULL since #2326; add org_id, or a foreign key and reclassify derived"
)
_SLICE2_CHAT = "#1433 slice 2: org_id on chat-bridge state with per-org bridge config"
_SLICE2_FLEET = "#1433 slice 2 with #1416: org_id on fleet devices and their reports"
_SLICE2_AUTH = "#1433 slice 2 with #1416: org_id on hub OAuth state and UI sessions"
_SLICE2_SKILLS = "#1433 slice 2: org_id on the skill registry"

# Tables that should be partitioned but carry no org_id yet. This may only
# shrink: tests/test_tenant_table_inventory.py checks every member still
# lacks org_id (so adding the column forces removing the member) and is in
# PARTITION_PENDING_BASELINE, so a new table cannot join it, not even in
# exchange for one that leaves. Each value names the follow-up.
PARTITION_PENDING: MappingProxyType[str, str] = MappingProxyType(
    {
        "agents": _SLICE2_FLEET,
        "agent_events": _SLICE2,
        "executor_attempts": _SLICE2_VIA,
        "dispatch_runs": _SLICE2_VIA,
        "autonomous_cycles": _SLICE2_VIA,
        "run_operating_snapshots": _SLICE2_VIA,
        "run_usage_heartbeats": _SLICE2_VIA,
        "usage_events": _SLICE2,
        "idempotency_keys": _SLICE2,
        "anomaly_notifications": _SLICE2,
        "recall_quality_log": _SLICE2,
        "memory_index_dirty": "#1433 slice 2: align the org_key column with org_id",
        "nodes": _SLICE2_FLEET,
        "node_dispatch": _SLICE2_FLEET,
        "host_reports": _SLICE2_FLEET,
        "ui_sessions": _SLICE2_AUTH,
        "session_wake": _SLICE2,
        "session_friction_nudges": _SLICE2,
        "credential_pool": "#1433 slice 2 with #1416: per-org provider credentials",
        "graph_canvas_scenes": _SLICE2,
        "project_repo_bindings": _SLICE2,
        "review_reads": _SLICE2,
        "skill_versions": _SLICE2_SKILLS,
        "skill_assignments": _SLICE2_SKILLS,
        "skill_sources": _SLICE2_SKILLS,
        "vault_ingest_ledger": "#1433 slice 2: org_id with per-org vault roots",
        "workspace_symbols": "#1433 slice 2: org_id with per-org workspace roots",
        "notify_delivery_log": _SLICE2,
        "pending_notifications": _SLICE2,
        "oauth_clients": _SLICE2_AUTH,
        "oauth_access_tokens": _SLICE2_AUTH,
        "oauth_refresh_tokens": _SLICE2_AUTH,
        "oauth_passkeys": _SLICE2_AUTH,
        "ts_cursors": _SLICE2_CHAT,
        "zulip_cursors": _SLICE2_CHAT,
        "zulip_worker_health": _SLICE2_CHAT,
        "zulip_event_queues": _SLICE2_CHAT,
        "zulip_bridge_state": _SLICE2_CHAT,
        "zulip_outbound_notifications": _SLICE2_CHAT,
        "zulip_chat_replies": _SLICE2_CHAT,
        "zulip_chat_topics": _SLICE2_CHAT,
        "slack_chat_replies": _SLICE2_CHAT,
        "slack_chat_conversations": _SLICE2_CHAT,
        "slack_outbound_slots": _SLICE2_CHAT,
        "mirror_events": "#1433 slice 2: carry the source org through the cloud mirror",
        "memory_recall_votes": _SLICE3_FK,
        "node_acceptance": _SLICE3_FK,
        "agent_perf_samples": _SLICE2,
        "web_search_cache": "#1433 slice 2: org_id on the search cache, or drop query text",
        "control_flags": "#1433 slice 2 with #1416: per-org operator flags",
        "device_proof_replays": _SLICE2_FLEET,
        "memory_graph_import_ownership": _SLICE2,
        "migration_import_row_ownership": _SLICE2,
        "pilot_sessions": "#1433 slice 2: per-row org_id when the pilot store joins the hub",
        "pilot_lifecycle": "#1433 slice 2: per-row org_id when the pilot store joins the hub",
        "ui_session_workos": _SLICE2_AUTH,
        "node_dispatch_cwd": _SLICE3_FK,
        "node_dispatch_resume": _SLICE3_FK,
        "node_dispatch_wait": _SLICE3_FK,
        "node_command": _SLICE3_FK,
        "node_command_notice": _SLICE3_FK,
    }
)

# Partitioned tables that already carry org_id but have no index whose leading
# column is org_id. Same ratchet: may only shrink (ORG_INDEX_PENDING_BASELINE),
# and every member must still lack the index. Empty since task #2326 added the
# eighteen indexes slice 1 listed; a new partitioned table ships with its index.
ORG_INDEX_PENDING: MappingProxyType[str, str] = MappingProxyType({})


# Frozen when the ratchets were introduced (#1433 slice 1). The pending maps
# must stay subsets: a table may leave, none may join. Do not add to these.
# Task #2326 removed memory_embeddings (now derived) and every index member.
PARTITION_PENDING_BASELINE: frozenset[str] = frozenset(
    {
        "agent_events",
        "agent_perf_samples",
        "agents",
        "anomaly_notifications",
        "autonomous_cycles",
        "control_flags",
        "credential_pool",
        "device_proof_replays",
        "dispatch_runs",
        "executor_attempts",
        "governed_servers",
        "graph_canvas_scenes",
        "host_reports",
        "idempotency_keys",
        "memory_graph_import_ownership",
        "memory_index_dirty",
        "memory_recall_votes",
        "migration_import_row_ownership",
        "mirror_events",
        "node_acceptance",
        "node_dispatch",
        "node_dispatch_cwd",
        "node_dispatch_resume",
        "node_dispatch_wait",
        "node_command",
        "node_command_notice",
        "nodes",
        "notify_delivery_log",
        "oauth_access_tokens",
        "oauth_clients",
        "oauth_passkeys",
        "oauth_refresh_tokens",
        "pending_notifications",
        "pilot_lifecycle",
        "pilot_sessions",
        "project_repo_bindings",
        "recall_quality_log",
        "review_reads",
        "run_operating_snapshots",
        "run_usage_heartbeats",
        "session_friction_nudges",
        "session_wake",
        "skill_assignments",
        "skill_sources",
        "skill_versions",
        "slack_chat_conversations",
        "slack_chat_replies",
        "slack_outbound_slots",
        "ts_cursors",
        "ui_session_workos",
        "ui_sessions",
        "usage_events",
        "vault_ingest_ledger",
        "web_search_cache",
        "workspace_symbols",
        "zulip_bridge_state",
        "zulip_chat_replies",
        "zulip_chat_topics",
        "zulip_cursors",
        "zulip_event_queues",
        "zulip_outbound_notifications",
        "zulip_worker_health",
    }
)

# Same rule for ORG_INDEX_PENDING.
ORG_INDEX_PENDING_BASELINE: frozenset[str] = frozenset()


def kind_of(table: str) -> str:
    """The tenancy kind of ``table``; ``KeyError`` for an unclassified table."""
    return TABLE_TENANCY[table].kind


def partitioned_tables() -> frozenset[str]:
    """Every table classified ``partitioned``, whether or not it has org_id yet."""
    return frozenset(t for t, e in TABLE_TENANCY.items() if e.kind == "partitioned")
