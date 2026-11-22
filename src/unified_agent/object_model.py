"""Platform object model — the control-plane registry (task #1070).

Why this module exists
----------------------
unified-agent grew as an MCP tool server, and every surface inherited that shape:
the MCP manifest registers tools, ``webui/api.py`` dispatches by tool *name*,
``profiles.py`` is an allow/deny list *of tool names*, ``edition.py`` gates
feature identifiers at entry points, and the TypeScript SDK is a typed tool
caller whose conformance suite asserts tool names.

The consequence is that the domain — tasks, runs, plans, approvals, delegations,
agents, sessions, memory notes, evidence events — is expressible **only as a tool
call**. Every consumer re-derives it, and the artifacts worth selling (the
approval gate, the tamper-evident evidence chain) cannot be read by anyone who is
not an agent client.

This module is the seam that inverts that. Object types are declared here, once.
The MCP adapter derives its registrations, profile gates, and subscription
telemetry keys from the declaration, and ``manifest()`` publishes the same
declarations as plain data so an HTTP/JSON adapter, the SDK, or the capability
catalog can consume the platform contract without importing MCP at all.

    ObjectType                    ->  MCP resource registration   (mcp_live_state)
    (declared once, here)         ->  profile visibility gate     (mcp_live_state)
                                  ->  subscription telemetry      (mcp_live_state)
                                  ->  platform manifest           (manifest())

Design rules
------------
1. **Declaration only.** This module executes nothing and imports no MCP, no
   profile, and no tool code. Readers resolve lazily by dotted path, so importing
   the registry can never import the tool layer (and cannot create a cycle).
2. **``served`` means a reader exists.** A declared-but-unserved object type is an
   honest placeholder: it names a real table and a real state machine from
   ``storage.py`` with no reader wired yet, and it stays out of resource
   registration until one is. It appears in ``manifest()`` with
   ``served: false`` — a roadmap entry, never a lie.
3. **Lifecycles are transcribed, not invented.** Every ``lifecycle`` tuple is
   copied from the backing table's SQL ``CHECK`` constraint, and each
   ``terminal_states`` subset is grounded in the owning module's own constants or
   its documented state machine. Where that grounding does not exist, the tuple is
   empty and ``notes`` says why.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from .org_registry import OFFICE_ID_PATTERN

#: Bumped when a manifest field changes meaning or disappears. Consumers (HTTP
#: adapter, SDK, capability catalog) should refuse a major they do not know.
MANIFEST_SCHEMA_VERSION = "1.0"

DEFAULT_MIME_TYPE = "application/json"

_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _compile_template(uri_template: str) -> re.Pattern[str] | None:
    """Compile ``unified-agent://tasks/{task_id}`` into a matchable regex.

    Returns ``None`` for a static URI (no parameters), which the callers treat as
    "only an exact string match counts".
    """
    if "{" not in uri_template:
        return None
    parts: list[str] = []
    cursor = 0
    for match in _PARAM_RE.finditer(uri_template):
        parts.append(re.escape(uri_template[cursor : match.start()]))
        parts.append(f"(?P<{match.group(1)}>[^/]+)")
        cursor = match.end()
    parts.append(re.escape(uri_template[cursor:]))
    return re.compile("".join(parts) + "$")


@dataclass(frozen=True)
class ObjectType:
    """One addressable platform object type.

    The fields split into three groups: how the object is *addressed*
    (``kind``/``uri_template``/``id_param``/``id_pattern``/``collection``), how it
    is *described to a client* (``name``/``title``/``description``/``mime_type``/
    ``schema``), and how it is *governed* (``profile_tool``/``lifecycle``/
    ``terminal_states``/``event_kinds``).
    """

    #: Canonical machine name, unique across the registry (e.g. ``"task"``).
    kind: str
    #: Resource URI, or a template when the type is addressed by id.
    uri_template: str
    #: MCP resource name; also the stable name other adapters should reuse.
    name: str
    title: str
    description: str
    #: The tool whose profile permission gates this object. Visibility is derived
    #: from it, so an object can never be reachable when its tool is denied.
    profile_tool: str
    #: True only when ``reader_path`` resolves to a callable.
    served: bool = False
    #: Backing SQLite table, so a reader can be written without re-discovery.
    table: str | None = None
    #: URI template parameter name (``None`` for a static/collection URI).
    id_param: str | None = None
    #: Regex an instance id must satisfy; ``None`` means "accept any non-empty".
    id_pattern: str | None = None
    #: True for a collection/root URI that has no id of its own.
    collection: bool = False
    #: Every state the backing table's CHECK constraint permits, in schema order.
    lifecycle: tuple[str, ...] = ()
    #: The subset from which no further transition is expected.
    terminal_states: tuple[str, ...] = ()
    #: Event/telemetry channel names this object publishes or is notified by.
    event_kinds: tuple[str, ...] = ()
    #: JSON Schema for one instance. Modest and honest beats aspirational.
    schema: Mapping[str, Any] = field(default_factory=dict)
    #: ``"module.path:function"`` resolved lazily; ``None`` when not served.
    reader_path: str | None = None
    #: Action name -> ``"module.path:function"`` resolved lazily for mutations.
    writers: Mapping[str, str] = field(default_factory=dict)
    #: Legacy telemetry counter key, kept so existing metric series keep
    #: reporting under the names dashboards already know.
    telemetry_key: str | None = None
    #: Operator-facing caveats: why a lifecycle is empty, what is still missing.
    notes: str = ""

    # -- addressing ---------------------------------------------------------

    def render(self, object_id: Any) -> str:
        """Build the concrete URI for one instance, validating the id shape."""
        if self.id_param is None:
            raise ValueError(f"object type {self.kind!r} is not addressed by id")
        text = str(object_id)
        if self.id_pattern is not None and not re.fullmatch(self.id_pattern, text):
            raise ValueError(f"invalid {self.id_param} for {self.kind!r}: {text!r}")
        return self.uri_template.replace("{" + self.id_param + "}", text)

    def extract_id(self, uri: str) -> str | None:
        """Return the id embedded in a concrete URI, or ``None`` if it doesn't match."""
        pattern = _compile_template(self.uri_template)
        if pattern is None:
            return None
        match = pattern.fullmatch(uri)
        if match is None or self.id_param is None:
            return None
        return match.group(self.id_param)

    def matches_uri(self, uri_or_template: str) -> bool:
        """Whether this type is what ``uri_or_template`` addresses.

        The template string itself matches (that is what ``resources/list``
        carries), as does any concrete instance URI.
        """
        if uri_or_template == self.uri_template:
            return True
        pattern = _compile_template(self.uri_template)
        return pattern is not None and pattern.fullmatch(uri_or_template) is not None

    # -- readers ------------------------------------------------------------

    def reader(self) -> Callable[..., Any]:
        """Resolve and return the reader callable. Raises when not served."""
        if self.reader_path is None:
            raise ValueError(
                f"object type {self.kind!r} declares no reader (served=False); "
                "wire one before registering it"
            )
        return _resolve_reader(self.reader_path)

    def writer(self, action: str) -> Callable[..., Any]:
        """Resolve one declared mutation writer without importing it at registry load."""
        writer_path = self.writers.get(action)
        if writer_path is None:
            raise ValueError(f"object type {self.kind!r} declares no writer for {action!r}")
        return _resolve_writer(writer_path)

    # -- publication --------------------------------------------------------

    def manifest_entry(self) -> dict[str, Any]:
        """JSON-serializable description of this type.

        Deliberately carries ``reader`` as the dotted *path string*, never the
        resolved callable: the manifest is a wire artifact.
        """
        return {
            "kind": self.kind,
            "uri": self.uri_template,
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "mime_type": DEFAULT_MIME_TYPE,
            "collection": self.collection,
            "served": self.served,
            "table": self.table,
            "id_param": self.id_param,
            "id_pattern": self.id_pattern,
            "profile_tool": self.profile_tool,
            "lifecycle": list(self.lifecycle),
            "terminal_states": list(self.terminal_states),
            "event_kinds": list(self.event_kinds),
            "schema": dict(self.schema),
            "reader": self.reader_path,
            "writers": dict(self.writers),
            "telemetry_key": self.telemetry_key,
            "notes": self.notes,
        }


@cache
def _resolve_reader(reader_path: str) -> Callable[..., Any]:
    """Import ``"module:function"`` on first use.

    Cached so repeated reads do not re-import; lazy so the registry itself stays
    free of the tool layer. A broken path fails loudly here rather than at import
    time, which keeps an unrelated registry edit from taking the hub down.
    """
    module_name, separator, attribute = reader_path.partition(":")
    if not separator:
        raise ValueError(f"reader path must be 'module:function', got {reader_path!r}")
    module = importlib.import_module(module_name)
    reader = getattr(module, attribute)
    if not callable(reader):
        raise TypeError(f"reader {reader_path!r} is not callable")
    return reader


@cache
def _resolve_writer(writer_path: str) -> Callable[..., Any]:
    """Import one writer on first use, keeping the registry declaration-only."""
    module_name, separator, attribute = writer_path.partition(":")
    if not separator:
        raise ValueError(f"writer path must be 'module:function', got {writer_path!r}")
    module = importlib.import_module(module_name)
    writer = getattr(module, attribute)
    if not callable(writer):
        raise TypeError(f"writer {writer_path!r} is not callable")
    return writer


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
#
# Served types are the three the hub already exposed (task #823) — declared here
# so the MCP adapter derives them instead of hand-writing them. Everything below
# them is the real domain, declared against real tables, waiting for a reader;
# see task #1072.

OBJECT_TYPES: tuple[ObjectType, ...] = (
    ObjectType(
        kind="board",
        uri_template="unified-agent://board",
        name="board",
        title="Task board",
        description="Read-only compact task board (same visibility as task_list).",
        profile_tool="task_list",
        served=True,
        collection=True,
        table="tasks",
        lifecycle=("open", "doing", "waiting_review", "blocked", "done", "cancelled"),
        terminal_states=("done", "cancelled"),
        event_kinds=("task.created", "task.updated"),
        schema={
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "tasks": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["uri", "tasks"],
        },
        reader_path="unified_agent.mcp_live_state:read_board",
        telemetry_key="board",
        notes="Collection root; carries the same rows task_list returns in compact form.",
    ),
    ObjectType(
        kind="task",
        uri_template="unified-agent://tasks/{task_id}",
        name="task",
        title="Task row",
        description="Read-only task row. Missing or deleted ids return not-found.",
        profile_tool="task_list",
        served=True,
        table="tasks",
        id_param="task_id",
        id_pattern=r"[1-9][0-9]*",
        lifecycle=("open", "doing", "waiting_review", "blocked", "done", "cancelled"),
        terminal_states=("done", "cancelled"),
        event_kinds=("task.created", "task.updated", "task.completed"),
        schema={
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "task": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "title": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": [
                                "open",
                                "doing",
                                "waiting_review",
                                "blocked",
                                "done",
                                "cancelled",
                            ],
                        },
                        "priority": {"type": "string", "enum": ["high", "med", "low"]},
                        "parent_id": {"type": ["integer", "null"]},
                        "project": {"type": "string"},
                        "domain": {"type": "string"},
                        "lane": {"type": "string"},
                        "claimed_by": {"type": ["string", "null"]},
                        "created_at": {"type": "string"},
                        "updated_at": {"type": "string"},
                        "completed_at": {"type": ["string", "null"]},
                        "evidence_ref": {"type": ["string", "null"]},
                    },
                    "required": ["id", "title", "status", "priority"],
                },
            },
            "required": ["uri", "task"],
        },
        reader_path="unified_agent.mcp_live_state:read_task",
        writers={
            "add": "unified_agent.tools.tasks:task_add",
            "update": "unified_agent.tools.tasks:task_update",
            "claim": "unified_agent.tools.tasks:task_claim",
        },
        telemetry_key="tasks",
        notes="Lifecycle and terminal states copied from the tasks CHECK constraint in storage.py.",
    ),
    ObjectType(
        kind="run",
        uri_template="unified-agent://runs/{run_id}",
        name="run",
        title="Dispatch run",
        description=(
            "Read-only run status: dispatch runs and self-pickup runs (sp-<ULID>, "
            "task #1436); same profile as dispatch_report_self."
        ),
        profile_tool="dispatch_report_self",
        served=True,
        table="dispatch_runs",
        id_param="run_id",
        id_pattern=r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
        lifecycle=(
            "claimed",
            "launched",
            "running",
            "finished",
            "failed",
            "retryable",
            "blocked",
            "denied",
            "cancelled",
            "expired",
            "refused",
        ),
        terminal_states=("finished", "failed", "denied", "cancelled", "expired"),
        event_kinds=(
            "dispatch.claimed",
            "dispatch.launched",
            "dispatch.finished",
            "selfpickup.claimed",
            "selfpickup.cancel_requested",
            "selfpickup.finished",
            "selfpickup.usage_reported",
        ),
        schema={
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "run": {
                    "type": "object",
                    "properties": {
                        "run_id": {"type": "string"},
                        "task_id": {"type": "integer"},
                        "agent_label": {"type": "string"},
                        "state": {"type": "string"},
                        "created_at": {"type": "string"},
                        "updated_at": {"type": "string"},
                    },
                    "required": ["run_id", "state"],
                },
            },
            "required": ["uri", "run"],
        },
        reader_path="unified_agent.mcp_live_state:read_run",
        telemetry_key="runs",
        notes=(
            "CHECK constraint in storage.py supplies the lifecycle. terminal_states "
            "follows the documented 'never relaunch' set in "
            "docs/architecture/approved-dispatch-state-machine.md; 'retryable' and "
            "'blocked' are non-live (coordinator.LIVE_STATES) but reclaimable, so they "
            "are deliberately excluded. Task #1436: self-pickup runs (run_id "
            "'sp-<ULID>') share this kind and reader but are folded from the "
            "event_log channel agent.run.selfpickup, not dispatch_runs; their "
            "state is 'open' or 'finished' and their outcome follows "
            "run_records.OUTCOMES, so they do not widen the dispatch_runs CHECK."
        ),
    ),
    # --- served by task #1072 (reader slice) ------------------------------------
    # Approval and evidence_event were the first two readers wired (slice 3,
    # merged); the remaining five followed. All declared types now resolve a real
    # reader - `served=False` is no longer expected to appear in the registry.
    ObjectType(
        kind="approval",
        uri_template="unified-agent://approvals/{approval_id}",
        name="approval",
        title="Approval request",
        description="A one-shot human approval gate and its resolution.",
        profile_tool="approval_pending",
        served=True,
        table="approvals",
        id_param="approval_id",
        id_pattern=r"[1-9][0-9]*",
        lifecycle=("pending", "approved", "denied", "consumed", "revoked", "expired"),
        terminal_states=("denied", "consumed", "revoked", "expired"),
        event_kinds=("agent.approval.resolved",),
        schema={
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "tool": {"type": "string"},
                "status": {"type": "string"},
                "requested_by": {"type": ["string", "null"]},
                "resolved_by": {"type": ["string", "null"]},
                "reason": {"type": ["string", "null"]},
                "created_at": {"type": "string"},
                "resolved_at": {"type": ["string", "null"]},
                "run_id": {"type": ["string", "null"]},
            },
            "required": ["id", "tool", "status"],
        },
        reader_path="unified_agent.platform_readers:read_approval",
        writers={
            "request": "unified_agent.tools.approvals:check_approval_gate",
            "resolve": "unified_agent.tools.approvals:approval_resolve",
        },
        notes=(
            "The 'approval gate enforced by the system' the paid pilot is sold on. "
            "Served by task #1072; before that it was reachable only by calling "
            "approval_pending."
        ),
    ),
    ObjectType(
        kind="evidence_event",
        uri_template="unified-agent://evidence/{event_ulid}",
        name="evidence_event",
        title="Evidence event",
        description="One hash-chained entry of the tamper-evident audit ledger.",
        profile_tool="event_log_query",
        served=True,
        table="event_log",
        id_param="event_ulid",
        id_pattern=r"[0-9A-Za-z]{10,64}",
        lifecycle=(),
        terminal_states=(),
        event_kinds=("event_log.appended",),
        schema={
            "type": "object",
            "properties": {
                "ulid": {"type": "string"},
                "channel": {"type": "string"},
                "payload": {"type": "string"},
                "originator": {"type": ["string", "null"]},
                "causal_parent_id": {"type": ["string", "null"]},
                "created_at": {"type": "string"},
                "prev_hash": {"type": ["string", "null"]},
                "entry_hash": {"type": ["string", "null"]},
            },
            "required": ["ulid", "channel", "created_at"],
        },
        reader_path="unified_agent.platform_readers:read_evidence_event",
        writers={"append": "unified_agent.events:record_event_once"},
        notes=(
            "Append-only, so an empty lifecycle is correct rather than missing. "
            "Keyed by ULID (event_log's primary key), not a rowid. Served by task "
            "#1072."
        ),
    ),
    ObjectType(
        kind="plan",
        uri_template="unified-agent://plans/{plan_id}",
        name="plan",
        title="Plan",
        description="Read-only plan row (same profile as plan_get).",
        profile_tool="plan_get",
        served=True,
        table="plans",
        id_param="plan_id",
        id_pattern=r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
        lifecycle=("active", "done", "blocked", "abandoned"),
        terminal_states=("done", "abandoned"),
        event_kinds=("plan.updated",),
        schema={
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "goal": {"type": "string"},
                "scope": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["active", "done", "blocked", "abandoned"],
                },
                "author": {"type": ["string", "null"]},
                "escalated": {"type": "integer"},
                "needs_human": {"type": "boolean"},
                "created_at": {"type": "string"},
                "updated_at": {"type": "string"},
            },
            "required": ["id", "goal", "status"],
        },
        reader_path="unified_agent.platform_readers:read_plan",
        notes=(
            "Served by task #1072. needs_human is derived from escalated at read "
            "time, matching plan_get's representation."
        ),
    ),
    ObjectType(
        kind="delegation",
        uri_template="unified-agent://delegations/{delegation_id}",
        name="delegation",
        title="Delegation",
        description="Read-only agent-to-agent handoff record (same profile as delegate_list).",
        profile_tool="delegate_list",
        served=True,
        table="delegations",
        id_param="delegation_id",
        id_pattern=r"[1-9][0-9]*",
        lifecycle=("pending", "in_flight", "completed", "cancelled", "failed"),
        terminal_states=("completed", "cancelled", "failed"),
        event_kinds=("delegation.created", "delegation.completed"),
        schema={
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "from_agent": {"type": "string"},
                "to_agent": {"type": "string"},
                "task_id": {"type": "integer"},
                "task_spec": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_flight", "completed", "cancelled", "failed"],
                },
                "callback": {"type": "string", "enum": ["notify", "memory", "none"]},
                "deadline": {"type": ["string", "null"]},
                "memory_id": {"type": ["string", "null"]},
                "callback_result": {"type": ["string", "null"]},
                "attempts": {"type": "integer"},
                "claim_generation": {"type": "integer"},
                "last_attempt_at": {"type": ["string", "null"]},
                "created_at": {"type": "string"},
                "updated_at": {"type": "string"},
                "completed_at": {"type": ["string", "null"]},
            },
            "required": ["id", "from_agent", "to_agent", "status"],
        },
        reader_path="unified_agent.platform_readers:read_delegation",
        notes=(
            "Served by task #1072. Lifecycle transcribed from the delegations "
            "CHECK constraint in storage.py; schema mirrors the stored row."
        ),
    ),
    ObjectType(
        kind="agent",
        uri_template="unified-agent://agents/{agent_label}",
        name="agent",
        title="Agent identity",
        description="A registered agent label and its derived liveness (same profile as agent_list).",
        profile_tool="agent_list",
        served=True,
        table="agents",
        id_param="agent_label",
        id_pattern=r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",
        lifecycle=(),
        terminal_states=(),
        event_kinds=("agent.seen",),
        schema={
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "kind": {"type": "string"},
                "host": {"type": ["string", "null"]},
                "known": {"type": "boolean"},
                "first_seen": {"type": ["string", "null"]},
                "last_seen": {"type": ["string", "null"]},
                "last_seen_via": {"type": ["string", "null"]},
                "pid": {"type": ["integer", "null"]},
                "version": {"type": ["string", "null"]},
                "status": {
                    "type": "string",
                    "enum": ["online", "idle", "stale", "offline", "unknown"],
                },
                "meta": {"type": "object"},
                "display_name": {"type": ["string", "null"]},
                "avatar": {"type": ["string", "null"]},
                "color": {"type": ["string", "null"]},
                "colour": {"type": ["string", "null"]},
                "bio": {"type": ["string", "null"]},
                "model_routing_preference": {"type": ["string", "null"]},
                "persona": {
                    "type": "object",
                    "properties": {
                        "display_name": {"type": "string"},
                        "avatar": {"type": ["string", "null"]},
                        "color": {"type": ["string", "null"]},
                        "colour": {"type": ["string", "null"]},
                        "bio": {"type": ["string", "null"]},
                        "model_routing_preference": {"type": ["string", "null"]},
                    },
                },
            },
            "required": ["label"],
        },
        reader_path="unified_agent.platform_readers:read_agent",
        notes=(
            "Served by task #1072. No lifecycle: registry.derive_status() computes "
            "liveness from last_seen at read time on purpose, so a status column "
            "would read 'online' forever exactly when it matters. An "
            "'online/offline' enum here would be a stored lie. Task #1427 adds "
            "persisted visual identity / persona display metadata (avatar, color, "
            "display_name, bio, model_routing_preference); purely presentational."
        ),
    ),
    ObjectType(
        kind="session",
        uri_template="unified-agent://sessions/{session_id}",
        name="session",
        title="Agent session",
        description="One agent session and its outcome (same profile as session_list).",
        profile_tool="session_list",
        served=True,
        table="agent_sessions",
        id_param="session_id",
        id_pattern=r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
        lifecycle=("starting", "running", "waiting", "completed", "failed", "abandoned"),
        terminal_states=("completed", "failed", "abandoned"),
        event_kinds=("session.started", "session.ended", "session.operator_ended"),
        schema={
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "agent": {"type": "string"},
                "kind": {"type": "string"},
                "status": {"type": "string"},
                "started_at": {"type": "string"},
                "last_heartbeat_at": {"type": "string"},
                "ended_at": {"type": ["string", "null"]},
                "task_id": {"type": ["integer", "null"]},
                "task_title": {"type": ["string", "null"]},
                "task_status": {"type": ["string", "null"]},
                "duration_s": {"type": ["number", "null"]},
                "heartbeat_age_s": {"type": ["number", "null"]},
                "delegation_id": {"type": ["integer", "null"]},
                "worktree_path": {"type": ["string", "null"]},
                "branch": {"type": ["string", "null"]},
                "host": {"type": ["string", "null"]},
                "pid": {"type": ["integer", "null"]},
                "exit_status": {"type": ["string", "null"]},
                "summary": {"type": ["string", "null"]},
                "meta": {"type": "object"},
            },
            "required": ["id", "agent", "status"],
        },
        reader_path="unified_agent.platform_readers:read_session",
        writers={"operator_end": "unified_agent.tools.registry:session_operator_end"},
        notes=(
            "Served by task #1072. Lifecycle transcribed from the "
            "agent_sessions CHECK constraint in storage.py; duration/heartbeat "
            "ages and joined task fields match registry.list_sessions."
        ),
    ),
    ObjectType(
        kind="memory_note",
        uri_template="unified-agent://memory/{memory_id}",
        name="memory_note",
        title="Memory note",
        description="One shared-memory note (same profile as memory_get).",
        profile_tool="memory_get",
        served=True,
        table="memory",
        id_param="memory_id",
        id_pattern=r"[A-Za-z0-9_-]{1,64}",
        lifecycle=(),
        terminal_states=(),
        event_kinds=("memory.saved",),
        schema={
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "title": {"type": ["string", "null"]},
                "content": {"type": "string"},
                "author": {"type": ["string", "null"]},
                "tags": {"type": "string"},
                "created_at": {"type": "string"},
                "updated_at": {"type": "string"},
                "superseded_by": {"type": ["string", "null"]},
                "expires_at": {"type": ["string", "null"]},
            },
            "required": ["id", "content"],
        },
        reader_path="unified_agent.platform_readers:read_memory_note",
        notes=(
            "Served by task #1072. Mutable and supersedable rather than "
            "stateful: versions are tracked via superseded_by and a tombstone, "
            "so there is no closed lifecycle. tags is the raw JSON-encoded "
            "column value, never rewritten on read."
        ),
    ),
    ObjectType(
        kind="agent_token",
        uri_template="unified-agent://agent-tokens/{token_id}",
        name="agent_token",
        title="Agent bearer token metadata",
        description=(
            "Hash-only lifecycle record for one issued agent bearer token — "
            "never the token value itself."
        ),
        profile_tool="agent_token_list",
        served=True,
        table="agent_tokens",
        id_param="token_id",
        id_pattern=r"[1-9][0-9]*",
        lifecycle=("active", "rotated", "revoked", "expired"),
        terminal_states=("revoked", "expired"),
        event_kinds=("security.agent_token.issued", "security.agent_token.rotated"),
        schema={
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "agent_token": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "agent_label": {"type": "string"},
                        "token_hash": {"type": "string"},
                        "hash_algo": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["active", "rotated", "revoked", "expired"],
                        },
                        "issued_at": {"type": "string"},
                        "expires_at": {"type": ["string", "null"]},
                        "rotated_from": {"type": ["integer", "null"]},
                        "rotated_at": {"type": ["string", "null"]},
                        "revoked_at": {"type": ["string", "null"]},
                        "issued_by": {"type": ["string", "null"]},
                        "note": {"type": ["string", "null"]},
                    },
                    "required": ["id", "agent_label", "token_hash", "status", "issued_at"],
                },
            },
            "required": ["uri", "agent_token"],
        },
        reader_path="unified_agent.platform_readers:read_agent_token",
        writers={
            "issue": "unified_agent.tools.token_lifecycle:agent_token_issue",
            "rotate": "unified_agent.tools.token_lifecycle:agent_token_rotate",
            "claim": "unified_agent.tools.token_lifecycle:deployment_claim",
        },
        notes=(
            "Task #1198. token_hash is the SHA-256 digest managed_secret_set "
            "computed at issuance; the table has no column capable of holding "
            "a plaintext value, so this reader cannot leak one by "
            "construction. 'rotated' is non-terminal by design — a rotated "
            "token's row stays linked via rotated_from so a rotation chain is "
            "reconstructable, but it does not itself transition further."
        ),
    ),
    ObjectType(
        kind="work_product",
        uri_template="unified-agent://work-products/{work_product_id}",
        name="work_product",
        title="Work product release",
        description=(
            "A submitted document/file awaiting or having completed named-"
            "reviewer approval and release, with its evidence chain."
        ),
        profile_tool="work_product_submit",
        served=True,
        table="work_products",
        id_param="work_product_id",
        id_pattern=r"[1-9][0-9]*",
        lifecycle=("submitted", "approved", "rejected", "released", "tampered"),
        terminal_states=("rejected", "released", "tampered"),
        event_kinds=(
            "work_product.submitted",
            "work_product.approved",
            "work_product.rejected",
            "work_product.released",
            "work_product.tampered",
        ),
        schema={
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "work_product": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "title": {"type": "string"},
                        "content_sha256": {"type": "string"},
                        "source_path": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "mime_type": {"type": ["string", "null"]},
                        "producing_agent": {"type": "string"},
                        "producing_model": {"type": ["string", "null"]},
                        "template_pack": {"type": "string"},
                        "tags": {"type": "object"},
                        "status": {
                            "type": "string",
                            "enum": ["submitted", "approved", "rejected", "released", "tampered"],
                        },
                        "previous_version_id": {"type": ["integer", "null"]},
                        "approval_id": {"type": ["integer", "null"]},
                        "released_path": {"type": ["string", "null"]},
                        "released_sha256": {"type": ["string", "null"]},
                        "reject_reason": {"type": ["string", "null"]},
                        "submitted_by": {"type": "string"},
                        "resolved_by": {"type": ["string", "null"]},
                        "resolved_at": {"type": ["string", "null"]},
                        "released_at": {"type": ["string", "null"]},
                        "created_at": {"type": "string"},
                        "updated_at": {"type": "string"},
                    },
                    "required": ["id", "title", "content_sha256", "status", "template_pack"],
                },
            },
            "required": ["uri", "work_product"],
        },
        reader_path="unified_agent.platform_readers:read_work_product",
        writers={"submit": "unified_agent.tools.work_product:work_product_submit"},
        notes=(
            "Task #1278 (H2): the non-code pilot wedge — an agent-produced "
            "document, a named human approval (the existing approval queue; "
            "the submitter can never approve its own request — "
            "approvals.SEPARATION_OF_DUTIES_TOOLS), a released copy, and the "
            "evidence chain linking human -> device -> agent -> model -> "
            "document hash -> approval id. 'tampered' is reachable from any "
            "non-terminal state when work_product_verify detects a hash "
            "mismatch against the approved content. Vertical specifics "
            "(matter/client tags, reviewer role, release subfolder) come from "
            "the chosen template pack (templates/work_product/*.yaml), never "
            "hardcoded here."
        ),
    ),
    ObjectType(
        kind="org_unit",
        uri_template="unified-agent://org-units/{org_unit_id}",
        name="org_unit",
        title="Organization office",
        description="One checked-in office charter with its lead, members, work ownership, and decision rights.",
        profile_tool="task_list",
        served=True,
        id_param="org_unit_id",
        id_pattern=OFFICE_ID_PATTERN,
        schema={
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "org_unit": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "lead": {"type": "string"},
                        "members": {"type": "array", "items": {"type": "string"}},
                        "owned_epics": {"type": "array", "items": {"type": "integer"}},
                        "default_lanes": {"type": "array", "items": {"type": "string"}},
                        "decision_rights": {"type": "array", "items": {"type": "string"}},
                        "default_profiles": {"type": "object"},
                    },
                    "required": [
                        "id",
                        "name",
                        "lead",
                        "members",
                        "owned_epics",
                        "default_lanes",
                        "decision_rights",
                        "default_profiles",
                    ],
                },
            },
            "required": ["uri", "org_unit"],
        },
        reader_path="unified_agent.platform_readers:read_org_unit",
        notes="Read-only projection of checked-in org.yaml; membership and profile defaults grant no authority.",
    ),
    ObjectType(
        kind="goal",
        uri_template="unified-agent://goals/{goal_id}",
        name="goal",
        title="Goal",
        description=(
            "One goal and its why-ancestry. parent_goal_id is not a task "
            "dependency and not tasks.parent_id."
        ),
        profile_tool="task_list",
        served=True,
        table="goals",
        id_param="goal_id",
        id_pattern=r"[1-9][0-9]*",
        lifecycle=("open", "achieved", "abandoned"),
        terminal_states=("achieved", "abandoned"),
        event_kinds=(
            "goal.created",
            "goal.linked",
            "goal.updated",
            "goal.closed",
            "goal.reopened",
            "goal.unlinked",
        ),
        schema={
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "goal": {"type": "object"},
                "ancestry": {"type": "array"},
                "edge": {"type": "string"},
            },
            "required": ["uri", "goal"],
        },
        reader_path="unified_agent.platform_readers:read_goal",
        writers={
            "add": "unified_agent.goals:goal_add",
            "link": "unified_agent.goals:link_task",
        },
        notes=(
            "Task #1349. edge goal_ancestry is the why-chain (root first). "
            "task_goal_links is goal_task: one goal per task, and many tasks "
            "may serve that same goal. New goals stay open; goal_manage close "
            "and reopen move them between open, achieved and abandoned (#2480). "
            "task_dependencies remains the prerequisite DAG."
        ),
    ),
    ObjectType(
        kind="comment",
        uri_template="unified-agent://comments/{comment_id}",
        name="comment",
        title="Task comment",
        description=(
            "One append-only utterance on a task. Distinct from task notes, "
            "which stay the specification."
        ),
        profile_tool="task_list",
        served=True,
        table="task_comments",
        id_param="comment_id",
        id_pattern=r"[1-9][0-9]*",
        lifecycle=(),
        terminal_states=(),
        event_kinds=("comment.appended",),
        schema={
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "comment": {"type": "object"},
            },
            "required": ["uri", "comment"],
        },
        reader_path="unified_agent.platform_readers:read_comment",
        writers={"append": "unified_agent.task_comments:append_comment"},
        notes=(
            "Task #1423. Append-only: the only writer is append. A thread "
            "snapshot (task_comment_threads.revision) advances on each append "
            "so list readers observe the change. Live-state polling revises a "
            "comment from its id and thread_revision, not by rehashing the "
            "body. No update or delete route."
        ),
    ),
    ObjectType(
        kind="org_proposal",
        uri_template="unified-agent://org-proposals/{proposal_id}",
        name="org_proposal",
        title="Organization proposal",
        description=(
            "One proposed organizational change (roles, lanes, routing, delegation, routines) "
            "pending review or resolution."
        ),
        profile_tool="task_list",
        served=True,
        table="org_proposals",
        id_param="proposal_id",
        id_pattern=r"orgprop-[a-zA-Z0-9_-]+",
        lifecycle=("proposed", "reviewed", "applied", "rejected"),
        terminal_states=("applied", "rejected"),
        event_kinds=(
            "org_proposal_created",
            "org_proposal_reviewed",
            "org_proposal_applied",
            "org_proposal_rejected",
        ),
        schema={
            "type": "object",
            "properties": {
                "uri": {"type": "string"},
                "org_proposal": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "proposal_type": {"type": "string"},
                        "author": {"type": "string"},
                        "scope": {"type": "string"},
                        "rationale": {"type": "string"},
                        "proposed_changes": {"type": ["object", "array", "string"]},
                        "status": {
                            "type": "string",
                            "enum": ["proposed", "reviewed", "applied", "rejected"],
                        },
                        "approval_id": {"type": ["integer", "null"]},
                        "created_at": {"type": "string"},
                        "updated_at": {"type": "string"},
                        "reviewed_at": {"type": ["string", "null"]},
                        "reviewed_by": {"type": ["string", "null"]},
                        "applied_at": {"type": ["string", "null"]},
                        "applied_by": {"type": ["string", "null"]},
                        "rejected_at": {"type": ["string", "null"]},
                        "rejected_by": {"type": ["string", "null"]},
                        "resolution_reason": {"type": ["string", "null"]},
                    },
                    "required": [
                        "id",
                        "title",
                        "proposal_type",
                        "author",
                        "scope",
                        "rationale",
                        "proposed_changes",
                        "status",
                        "created_at",
                        "updated_at",
                    ],
                },
            },
            "required": ["uri", "org_proposal"],
        },
        reader_path="unified_agent.platform_readers:read_org_proposal",
        writers={
            "propose": "unified_agent.self_organization:propose_org_change",
            "review": "unified_agent.self_organization:review_org_proposal",
            "apply": "unified_agent.self_organization:apply_org_proposal",
            "reject": "unified_agent.self_organization:reject_org_proposal",
        },
        notes=(
            "Task #1431 self-organization. Proposing is always on by default; "
            "applying is strictly gated behind human/policy approval."
        ),
    ),
)

_BY_KIND: dict[str, ObjectType] = {object_type.kind: object_type for object_type in OBJECT_TYPES}

#: Backward-compatible URI constants (task #823 exposed these literally). Derived
#: here so the registry is the only place a URI is written down.
BOARD_URI = _BY_KIND["board"].uri_template
TASK_URI_TEMPLATE = _BY_KIND["task"].uri_template
RUN_URI_TEMPLATE = _BY_KIND["run"].uri_template


def by_kind(kind: str) -> ObjectType:
    """Look up one type, raising ``KeyError`` for an unknown kind."""
    return _BY_KIND[kind]


def get(kind: str) -> ObjectType | None:
    """Look up one type without raising."""
    return _BY_KIND.get(kind)


def served_types() -> tuple[ObjectType, ...]:
    """The types a reader can actually be resolved for, in declaration order."""
    return tuple(object_type for object_type in OBJECT_TYPES if object_type.served)


def match_uri(uri_or_template: str) -> ObjectType | None:
    """Which declared type ``uri_or_template`` addresses, or ``None``.

    Exact template equality wins over instance matching so a literal
    ``.../tasks/{task_id}`` can never be mistaken for some other type's instance.
    """
    for object_type in OBJECT_TYPES:
        if uri_or_template == object_type.uri_template:
            return object_type
    for object_type in OBJECT_TYPES:
        pattern = _compile_template(object_type.uri_template)
        if pattern is not None and pattern.fullmatch(uri_or_template) is not None:
            return object_type
    return None


def manifest() -> dict[str, Any]:
    """The platform contract as plain data.

    This is the artifact an HTTP/JSON adapter, the TypeScript SDK, or the
    capability catalog should consume — the reason `MCP` can become one adapter
    among several instead of the only way to see the domain.
    """
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product": "unified-agent",
        "object_types": [object_type.manifest_entry() for object_type in OBJECT_TYPES],
    }


def validate_registry() -> list[str]:
    """Return a list of invariant violations; empty means the registry is sound.

    Returns rather than raises so a caller can report every problem at once. The
    test suite asserts the empty list, and any future ``--check`` entry point can
    reuse it.
    """
    problems: list[str] = []
    seen_kinds: set[str] = set()
    seen_uris: set[str] = set()
    seen_names: set[str] = set()

    for object_type in OBJECT_TYPES:
        where = f"object type {object_type.kind!r}"
        for label, value in (
            ("kind", object_type.kind),
            ("uri_template", object_type.uri_template),
            ("name", object_type.name),
            ("title", object_type.title),
            ("description", object_type.description),
            ("profile_tool", object_type.profile_tool),
        ):
            if not value:
                problems.append(f"{where}: {label} is empty")
        if object_type.kind in seen_kinds:
            problems.append(f"{where}: duplicate kind")
        if object_type.uri_template in seen_uris:
            problems.append(f"{where}: duplicate uri_template {object_type.uri_template!r}")
        if object_type.name in seen_names:
            problems.append(f"{where}: duplicate resource name {object_type.name!r}")
        seen_kinds.add(object_type.kind)
        seen_uris.add(object_type.uri_template)
        seen_names.add(object_type.name)

        if not object_type.uri_template.startswith("unified-agent://"):
            problems.append(f"{where}: uri_template is outside the unified-agent:// namespace")

        placeholders = _PARAM_RE.findall(object_type.uri_template)
        if object_type.collection:
            if placeholders:
                problems.append(f"{where}: collection URIs must be static")
            if object_type.id_param is not None:
                problems.append(f"{where}: collection URIs must not declare an id_param")
        elif not object_type.collection:
            if len(placeholders) != 1:
                problems.append(
                    f"{where}: non-collection URIs need exactly one placeholder, "
                    f"found {placeholders!r}"
                )
            elif placeholders[0] != object_type.id_param:
                problems.append(
                    f"{where}: id_param {object_type.id_param!r} does not match "
                    f"placeholder {placeholders[0]!r}"
                )

        if object_type.id_pattern is not None:
            try:
                re.compile(object_type.id_pattern)
            except re.error as exc:
                problems.append(f"{where}: id_pattern does not compile: {exc}")
        if object_type.collection and object_type.id_pattern is not None:
            problems.append(f"{where}: collection URIs must not constrain an id_pattern")

        unknown_terminal = set(object_type.terminal_states) - set(object_type.lifecycle)
        if unknown_terminal:
            problems.append(
                f"{where}: terminal_states {sorted(unknown_terminal)} are not in lifecycle"
            )
        if object_type.served and object_type.reader_path is None:
            problems.append(f"{where}: served=True but reader_path is None")
        if not object_type.served and object_type.reader_path is not None:
            problems.append(f"{where}: served=False but reader_path is set")
        if object_type.reader_path is not None and ":" not in object_type.reader_path:
            problems.append(
                f"{where}: reader_path {object_type.reader_path!r} is not 'module:function'"
            )
        for action, writer_path in object_type.writers.items():
            if not action:
                problems.append(f"{where}: writer action is empty")
            if not isinstance(writer_path, str) or ":" not in writer_path:
                problems.append(
                    f"{where}: writer {action!r} path {writer_path!r} is not 'module:function'"
                )
        if object_type.table is not None and not re.fullmatch(
            r"[a-z_][a-z0-9_]*", object_type.table
        ):
            problems.append(f"{where}: table {object_type.table!r} is not a plain identifier")
        if object_type.served and not object_type.schema:
            problems.append(f"{where}: served types must publish a schema")

    served = [object_type.kind for object_type in served_types()]
    for legacy in ("board", "task", "run"):
        if legacy not in served:
            problems.append(f"legacy resource {legacy!r} is no longer served")

    return problems
