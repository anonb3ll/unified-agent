"""Isolated LangGraph execution pilot under Unified-Agent authority.

This module registers no MCP tools itself and has no service entrypoint,
task-state writer, or approval resolver. It proves the execution-plane mechanics
behind an injected gateway. The hub exposes it indirectly: `tools/langgraph.py`
registers `langgraph_run` in `server.py`, wrapping this runner and the governed
adapter in `langgraph_dispatch.py`. `langgraph_progress_note` and
`langgraph_workflow_run` use their own runners and adapters (see
docs/architecture.md, "LangGraph execution plane").
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import random
import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langsmith import tracing_context

from . import config
from .telemetry.redaction import redact_sensitive_content, sanitize_metadata

CONTRACT_VERSION = "ua.langgraph.run/v1"
GRAPH_NAME = "governed_research_pilot"
GRAPH_VERSION = "0.2.0"
STATE_SCHEMA_VERSION = 5
ALLOWED_READ_TOOLS = frozenset({"web_search"})
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_MAX_QUERY_CHARS = 500
_MAX_REDUCED_RESULT_BYTES = 4096
_GOVERNED_TOOL_TIMEOUT_S = 20.0
_MAX_GOVERNED_TOOL_ATTEMPTS = 4
_SANITIZER_MAX_METADATA_ENTRIES = 50
_DELIVERY_LOCKS: dict[str, threading.RLock] = {}
_DELIVERY_LOCKS_GUARD = threading.Lock()


class PilotError(RuntimeError):
    """Base class for fail-closed pilot errors."""


class InvalidRunError(PilotError):
    """The immutable run contract is malformed or conflicts with a checkpoint."""


class DefinitionMismatchError(PilotError):
    """A checkpoint belongs to a different immutable graph definition."""


class CheckpointMissingError(InvalidRunError):
    """No checkpoint exists for the requested run (task #2522)."""


class CheckpointCorruptError(InvalidRunError):
    """A run's checkpoint cannot be decoded or its authority fields are malformed."""


class FenceLostError(PilotError):
    """A checkpoint write was refused: the writer does not hold the run's resume lease."""


#: Resume leases live in the checkpoint store itself, so a lease check and the
#: checkpoint write it fences commit in one SQLite transaction (task #2522).
#: ``langgraph_resume`` takes, renews and releases them.
RESUME_LEASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS langgraph_resume_leases (
  thread_id TEXT PRIMARY KEY,
  org_id TEXT,
  run_id TEXT NOT NULL,
  holder TEXT NOT NULL,
  token TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_langgraph_resume_leases_org
  ON langgraph_resume_leases (org_id, thread_id);
"""


def lease_is_live(expires_at: object, now: datetime | None = None) -> bool:
    """Whether a stored lease expiry is a valid timestamp still in the future."""
    if not isinstance(expires_at, str):
        return False
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed > (now or datetime.now(UTC))


class _FencedSqliteSaver(SqliteSaver):
    """A ``SqliteSaver`` whose writes are fenced on the run's resume lease.

    Every ``put`` and ``put_writes`` runs in one ``BEGIN IMMEDIATE`` transaction
    that first reads the thread's lease row. A thread with a live lease accepts
    writes only from the runner fenced on that lease's token; a runner fenced on
    a thread accepts its writes only while its token is the live one. Anything
    else raises ``FenceLostError`` and rolls the write back, so a resume that lost
    its lease stops at its next checkpoint and cannot record a completion.
    """

    def __init__(self, conn: sqlite3.Connection, *, serde: Any) -> None:
        super().__init__(conn, serde=serde)
        self.fence: tuple[str, str] | None = None
        self._writing = threading.local()

    def put(self, config, checkpoint, metadata, new_versions):  # type: ignore[override]
        self._writing.thread_id = str(config["configurable"]["thread_id"])
        try:
            return super().put(config, checkpoint, metadata, new_versions)
        finally:
            self._writing.thread_id = None

    def put_writes(  # type: ignore[override]
        self, config, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        self._writing.thread_id = str(config["configurable"]["thread_id"])
        try:
            super().put_writes(config, writes, task_id, task_path)
        finally:
            self._writing.thread_id = None

    @contextmanager
    def cursor(self, transaction: bool = True) -> Iterator[sqlite3.Cursor]:
        thread_id = getattr(self._writing, "thread_id", None)
        if not transaction or thread_id is None:
            with super().cursor(transaction) as cur:
                yield cur
            return
        with self.lock:
            self.setup()
            if self.conn.in_transaction:
                self.conn.commit()
            self.conn.execute("BEGIN IMMEDIATE")
            cur = self.conn.cursor()
            try:
                self._check_fence(cur, thread_id)
                yield cur
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            finally:
                cur.close()

    def _check_fence(self, cur: sqlite3.Cursor, thread_id: str) -> None:
        row = cur.execute(
            "SELECT token, expires_at FROM langgraph_resume_leases WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        live = row is not None and lease_is_live(row[1])
        fenced = self.fence is not None and self.fence[0] == thread_id
        if fenced:
            if not live or row[0] != self.fence[1]:
                raise FenceLostError("this resume no longer holds the run's lease")
        elif live:
            raise FenceLostError("another resume holds the run's lease")


class StaleAuthorityError(PilotError):
    """Canonical Unified-Agent authority no longer matches the run preconditions."""


class DeniedByAuthorityError(PilotError):
    """Canonical Unified-Agent authority denied the proposed governed call."""


class TransientGatewayError(PilotError):
    """A bounded gateway attempt may be retried with the same idempotency key."""


class GatewayExhaustedError(PilotError):
    """The governed gateway exhausted its bounded transient retry budget."""


RunStatus = Literal["created", "running", "denied", "failed", "completed"]


@dataclass(frozen=True)
class PilotRun:
    """Control-plane-issued identity and bounded input for one logical run."""

    run_id: str
    task_id: int
    task_updated_at: str
    claimed_by: str
    claim_expires_at: str
    expected_profile: str
    required_capabilities: tuple[str, ...]
    budget_revision: str
    trace_id: str
    query: str

    @property
    def thread_id(self) -> str:
        return f"ua-lg:{self.run_id}"

    @property
    def input_digest(self) -> str:
        return _digest_json({"query": self.query})

    def validate(self) -> None:
        if not _ULID_RE.fullmatch(self.run_id):
            raise InvalidRunError("run_id must be a 26-character Crockford ULID")
        if self.task_id <= 0:
            raise InvalidRunError("task_id must be positive")
        for name in (
            "task_updated_at",
            "claimed_by",
            "claim_expires_at",
            "expected_profile",
            "budget_revision",
            "trace_id",
        ):
            if not str(getattr(self, name)).strip():
                raise InvalidRunError(f"{name} must be non-empty")
        if not self.required_capabilities or any(
            not capability.strip() for capability in self.required_capabilities
        ):
            raise InvalidRunError("required_capabilities must contain non-empty names")
        if not self.query.strip() or len(self.query) > _MAX_QUERY_CHARS:
            raise InvalidRunError(f"query must contain 1-{_MAX_QUERY_CHARS} characters")
        if redact_sensitive_content(self.query, max_len=len(self.query) + 1) != self.query:
            raise InvalidRunError("query contains a recognized secret and cannot be checkpointed")


@dataclass(frozen=True)
class ToolProposal:
    """A bounded, statically allowlisted proposal emitted by the agentic node."""

    tool_name: str
    arguments: Mapping[str, Any]

    @property
    def payload_digest(self) -> str:
        return _digest_json({"tool": self.tool_name, "arguments": self.arguments})

    def normalized(self) -> ToolProposal:
        if self.tool_name not in ALLOWED_READ_TOOLS:
            raise PilotError(f"agentic proposal selected disallowed tool {self.tool_name!r}")
        if self.tool_name == "web_search":
            if set(self.arguments) != {"query", "max_results"}:
                raise PilotError("web_search proposal must contain exactly query and max_results")
            query = self.arguments.get("query")
            max_results = self.arguments.get("max_results")
            if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY_CHARS:
                raise PilotError("web_search query is outside the bounded pilot contract")
            if redact_sensitive_content(query, max_len=len(query) + 1) != query:
                raise PilotError("web_search query contains a recognized secret")
            if not isinstance(max_results, int) or isinstance(max_results, bool):
                raise PilotError("web_search max_results must be an integer")
            if not 1 <= max_results <= 3:
                raise PilotError("web_search max_results must be between 1 and 3")
            return ToolProposal("web_search", {"query": query.strip(), "max_results": max_results})
        raise PilotError("no validator exists for the proposed tool")


@dataclass(frozen=True)
class GovernedToolOutcome:
    """The authority gateway's current verdict for the exact proposed call.

    ``denied`` is terminal and distinct from ``pending``.  Without it a denial
    is indistinguishable from an unresolved approval, and the graph's approval
    loop would re-interrupt for a decision that has already been made.
    """

    status: Literal["allowed", "pending", "denied"]
    approval_id: str | None = None
    result: Any = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"allowed", "pending", "denied"}:
            raise PilotError(f"unsupported governed tool outcome {self.status!r}")
        if self.status == "allowed":
            if self.reason is not None:
                raise PilotError("an allowed outcome cannot include a denial reason")
            return
        if self.result is not None:
            raise PilotError(f"a {self.status} outcome cannot include a tool result")
        if self.status == "pending":
            if not (self.approval_id or "").strip():
                raise PilotError("a pending outcome requires a canonical approval_id")
            if self.reason is not None:
                raise PilotError("a pending outcome cannot include a denial reason")
            return
        if not (self.reason or "").strip():
            raise PilotError("a denied outcome requires a canonical reason")

    @classmethod
    def pending(cls, approval_id: str) -> GovernedToolOutcome:
        if not approval_id.strip():
            raise PilotError("a pending outcome requires a canonical approval_id")
        return cls(status="pending", approval_id=approval_id)

    @classmethod
    def allowed(cls, result: Any, *, approval_id: str | None = None) -> GovernedToolOutcome:
        return cls(status="allowed", approval_id=approval_id, result=result)

    @classmethod
    def denied(cls, reason: str, *, approval_id: str | None = None) -> GovernedToolOutcome:
        return cls(status="denied", approval_id=approval_id, reason=reason)


class AgentPlanner(Protocol):
    """Injected model/planner seam used by the graph's agentic node."""

    @property
    def definition_digest(self) -> str: ...

    def propose(self, query: str) -> ToolProposal: ...


class GovernedGateway(Protocol):
    """Injected Unified-Agent authority and governed-tool seam.

    Implementations must query the authenticated control plane.  They must not
    derive authority from checkpoint or resume values.
    """

    def revalidate(self, run: PilotRun) -> None: ...

    def call_read_tool(
        self,
        *,
        run: PilotRun,
        proposal: ToolProposal,
        idempotency_key: str,
        timeout_s: float,
    ) -> GovernedToolOutcome: ...


class PilotState(TypedDict, total=False):
    contract_version: str
    graph_name: str
    graph_version: str
    definition_digest: str
    state_schema_version: int
    run_id: str
    task_id: int
    task_updated_at: str
    claimed_by: str
    claim_expires_at: str
    expected_profile: str
    required_capabilities: list[str]
    budget_revision: str
    trace_id: str
    input_digest: str
    query: str
    normalized_query: str
    tool_name: str
    tool_arguments: dict[str, Any]
    tool_payload_digest: str
    operation_key: str
    idempotency_key: str
    approval_id: str | None
    denial_reason: str
    failure_reason: str
    transient_attempt_count: int
    transient_backoff_s: float
    awaiting_transient_retry: bool
    awaiting_approval_interrupt: bool
    reduced_result: Any
    result_digest: str
    output: dict[str, Any]
    output_digest: str
    status: RunStatus


@dataclass(frozen=True)
class PilotEvent:
    """A bounded stream event with no raw model, input, argument, or result body."""

    kind: Literal[
        "node_completed",
        "approval_interrupted",
        "run_completed",
        "run_denied",
        "run_failed",
    ]
    node_key: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class LoadedRun:
    """A run rebuilt from its latest checkpoint by ``PilotRunner.load_run``.

    ``run`` carries the original claim, task revision, profile, capabilities,
    budget revision and trace exactly as checkpointed; nothing is re-read from the
    caller. ``revision`` is the latest checkpoint id, the value a resume compares
    against before it executes.
    """

    run: PilotRun
    revision: str
    status: str
    paused: bool

    @property
    def terminal(self) -> bool:
        return not self.paused and self.status in {"completed", "denied", "failed"}


@dataclass(frozen=True)
class PilotResult:
    status: Literal["completed"]
    output: dict[str, Any]
    duplicate: bool
    recovered: bool


class PilotRunner:
    """Single-process pilot runner backed by a dedicated owner-only SQLite DB."""

    def __init__(
        self,
        checkpoint_path: str | Path | None,
        *,
        planner: AgentPlanner,
        gateway: GovernedGateway,
    ) -> None:
        self.checkpoint_path = Path(checkpoint_path or default_checkpoint_path()).expanduser()
        lock_key = str(self.checkpoint_path.resolve(strict=False))
        with _DELIVERY_LOCKS_GUARD:
            self._delivery_lock = _DELIVERY_LOCKS.setdefault(lock_key, threading.RLock())
        self.planner = planner
        self.gateway = gateway
        self.definition_digest = self._definition_digest()
        self._closed = False
        self._conn = _open_checkpoint(self.checkpoint_path)
        try:
            serializer = JsonPlusSerializer(
                pickle_fallback=False,
                allowed_json_modules=None,
                allowed_msgpack_modules=None,
            )
            self._checkpointer = _FencedSqliteSaver(self._conn, serde=serializer)
            self._checkpointer.setup()
            self._conn.executescript(RESUME_LEASE_SCHEMA)
            self._graph = self._build_graph()
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    def __enter__(self) -> PilotRunner:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    def stream(self, run: PilotRun, *, resume: Any | None = None) -> Iterator[PilotEvent]:
        """Stream bounded node events; resume data is transport-only and never authority."""
        # The pilot is single-process. Serializing deliveries by checkpoint DB
        # makes concurrent duplicates observe the first committed checkpoint or
        # terminal result instead of entering the graph twice.
        return self._stream_with_delivery_lock(run, resume=resume)

    def _stream_with_delivery_lock(
        self, run: PilotRun, *, resume: Any | None = None
    ) -> Iterator[PilotEvent]:
        self._delivery_lock.acquire()
        try:
            yield from self._stream_locked(run, resume=resume)
        finally:
            self._delivery_lock.release()

    def _stream_locked(self, run: PilotRun, *, resume: Any | None = None) -> Iterator[PilotEvent]:
        self._ensure_open()
        run.validate()
        with tracing_context(enabled=False, parent=False):
            self.gateway.revalidate(run)
        config_value = {"configurable": {"thread_id": run.thread_id}}
        snapshot = self._graph.get_state(config_value)
        existed = bool(snapshot.values)
        if existed:
            self._validate_checkpoint(snapshot.values, run)
            if not snapshot.next:
                if snapshot.values.get("status") == "completed":
                    yield PilotEvent(
                        kind="run_completed",
                        node_key=None,
                        payload={
                            "duplicate": True,
                            "output_digest": snapshot.values["output_digest"],
                            "recovered": False,
                        },
                    )
                    return
                if snapshot.values.get("status") == "denied":
                    yield self._denied_event(snapshot.values, duplicate=True, recovered=False)
                    return
                if snapshot.values.get("status") == "failed":
                    yield self._failed_event(snapshot.values, duplicate=True, recovered=False)
                    return
            if resume is None:
                if snapshot.values.get("status") == "failed":
                    raise GatewayExhaustedError(
                        snapshot.values.get("failure_reason")
                        or "governed tool gateway exhausted retry budget"
                    )
                raise InvalidRunError("an interrupted run requires an explicit resume value")
            # A pause at an interrupt takes the (ignored) resume value; a run that
            # stopped between nodes, such as a resume fenced out mid-graph,
            # continues its pending node from the checkpoint.
            graph_input: PilotState | Command | None = (
                Command(resume=resume) if snapshot.interrupts else None
            )
        else:
            if resume is not None:
                raise InvalidRunError("cannot resume a run without a checkpoint")
            graph_input = self._initial_state(run)

        interrupted = False
        # Retry counters and approval state must be durable before the graph
        # advances to another node.  The default durability mode can schedule
        # checkpoint writes asynchronously, allowing a concurrent observer to
        # see the pre-retry state while the next attempt is already running.
        updates = iter(
            self._graph.stream(
                graph_input,
                config_value,
                stream_mode="updates",
                durability="sync",
            )
        )
        while True:
            try:
                # The pilot never opts into LangSmith, even if the host process
                # has tracing environment variables set for another workload.
                with tracing_context(enabled=False, parent=False):
                    update = next(updates)
            except StopIteration:
                break
            event = self._event_from_update(update)
            if event is not None:
                interrupted = event.kind == "approval_interrupted"
                yield event

        final = self._graph.get_state(config_value)
        if not interrupted and not final.next and final.values.get("status") == "completed":
            yield PilotEvent(
                kind="run_completed",
                node_key=None,
                payload={
                    "duplicate": False,
                    "output_digest": final.values["output_digest"],
                    "recovered": existed,
                },
            )
        elif not interrupted and not final.next and final.values.get("status") == "denied":
            yield self._denied_event(final.values, duplicate=False, recovered=existed)
        elif not interrupted and not final.next and final.values.get("status") == "failed":
            yield self._failed_event(final.values, duplicate=False, recovered=existed)

    def run(self, run: PilotRun, *, resume: Any | None = None) -> PilotResult:
        events = list(self.stream(run, resume=resume))
        if events and events[-1].kind == "run_denied":
            raise DeniedByAuthorityError(str(events[-1].payload["reason"]))
        if events and events[-1].kind == "run_failed":
            raise GatewayExhaustedError(str(events[-1].payload["reason"]))
        if not events or events[-1].kind != "run_completed":
            raise InvalidRunError("run is interrupted; resume after canonical approval changes")
        snapshot = self._graph.get_state({"configurable": {"thread_id": run.thread_id}})
        return PilotResult(
            status="completed",
            output=dict(snapshot.values["output"]),
            duplicate=bool(events[-1].payload["duplicate"]),
            recovered=bool(events[-1].payload["recovered"]),
        )

    def load_run(self, run_id: str) -> LoadedRun:
        """Rebuild a run from its latest checkpoint, validated, without executing it.

        Refuses a missing checkpoint (``CheckpointMissingError``), one written by a
        different graph contract, version, schema or definition
        (``DefinitionMismatchError``), and one that cannot be decoded or whose
        authority fields are malformed (``CheckpointCorruptError``). Authority is
        not checked here: the caller revalidates the returned run against the
        control plane before resuming it.
        """
        if not isinstance(run_id, str) or not _ULID_RE.fullmatch(run_id):
            raise InvalidRunError("run_id must be a 26-character Crockford ULID")
        with self._delivery_lock, tracing_context(enabled=False, parent=False):
            self._ensure_open()
            config_value = {"configurable": {"thread_id": f"ua-lg:{run_id}"}}
            try:
                snapshot = self._graph.get_state(config_value)
            except Exception as exc:  # noqa: BLE001 - any decode failure is corruption
                raise CheckpointCorruptError("the run's checkpoint cannot be decoded") from exc
            values = snapshot.values
            if not values:
                raise CheckpointMissingError("no checkpoint exists for this run")
            for key, expected in (
                ("contract_version", CONTRACT_VERSION),
                ("graph_name", GRAPH_NAME),
                ("graph_version", GRAPH_VERSION),
                ("state_schema_version", STATE_SCHEMA_VERSION),
                ("definition_digest", self.definition_digest),
            ):
                if values.get(key) != expected:
                    raise DefinitionMismatchError(
                        f"checkpoint {key} is incompatible with this graph revision"
                    )
            try:
                _check_run_field_types(values)
                run = self._run_from_state(values)
                run.validate()
                if run.run_id != run_id:
                    raise InvalidRunError("checkpoint run_id does not match the requested run")
                self._validate_checkpoint(values, run)
            except (KeyError, TypeError, ValueError, AttributeError, InvalidRunError) as exc:
                raise CheckpointCorruptError(
                    "the run's checkpoint authority fields are missing or malformed"
                ) from exc
            revision = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
            if not revision:
                raise CheckpointCorruptError("the run's checkpoint has no revision id")
            return LoadedRun(
                run=run,
                revision=str(revision),
                status=str(values.get("status") or ""),
                paused=bool(snapshot.next),
            )

    def fence(self, thread_id: str, token: str | None) -> None:
        """Fence this runner's checkpoint writes on ``thread_id`` to a lease token.

        ``None`` removes the fence; the runner may then write the thread only
        while no live lease names it.
        """
        self._checkpointer.fence = (thread_id, token) if token else None

    def output(self, run: PilotRun) -> dict[str, Any] | None:
        """The checkpointed output of a completed run, or None when it has none."""
        with self._delivery_lock:
            self._ensure_open()
            values = self._graph.get_state({"configurable": {"thread_id": run.thread_id}}).values
        output = values.get("output") if values.get("status") == "completed" else None
        return dict(output) if isinstance(output, Mapping) and output else None

    def terminal_event(self, run: PilotRun) -> PilotEvent | None:
        """The stored outcome of a finished run as a duplicate event, else None.

        Reads the checkpoint only; it never enters the graph or the gateway, so a
        repeat of a finished resume cannot execute anything.
        """
        with self._delivery_lock:
            self._ensure_open()
            snapshot = self._graph.get_state({"configurable": {"thread_id": run.thread_id}})
        values = snapshot.values
        if not values or snapshot.next:
            return None
        self._validate_checkpoint(values, run)
        if values.get("status") == "completed":
            return PilotEvent(
                kind="run_completed",
                node_key=None,
                payload={
                    "duplicate": True,
                    "output_digest": values.get("output_digest"),
                    "recovered": False,
                },
            )
        if values.get("status") == "denied":
            return self._denied_event(values, duplicate=True, recovered=False)
        if values.get("status") == "failed":
            return self._failed_event(values, duplicate=True, recovered=False)
        return None

    def _build_graph(self):
        builder = StateGraph(PilotState)
        builder.add_node("intake", self._intake_node)
        builder.add_node("agentic", self._agentic_node)
        builder.add_node("governed_tool", self._governed_tool_node)
        builder.add_node(
            "governed_tool_approval_gate",
            self._governed_tool_approval_gate_node,
        )
        builder.add_node("synthesis", self._synthesis_node)
        builder.add_edge(START, "intake")
        builder.add_edge("intake", "agentic")
        builder.add_edge("agentic", "governed_tool")
        builder.add_conditional_edges(
            "governed_tool",
            self._route_after_governed_tool,
            {
                "approval": "governed_tool_approval_gate",
                "transient_retry": "governed_tool",
                "allowed": "synthesis",
                "denied": END,
                "failed": END,
            },
        )
        builder.add_edge("governed_tool_approval_gate", "governed_tool")
        builder.add_edge("synthesis", END)
        return builder.compile(checkpointer=self._checkpointer, name=GRAPH_NAME)

    def _intake_node(self, state: PilotState) -> PilotState:
        query = " ".join(state["query"].split())
        return {"normalized_query": query, "status": "running"}

    def _agentic_node(self, state: PilotState) -> PilotState:
        proposal = self.planner.propose(state["normalized_query"]).normalized()
        operation_key = "read-evidence"
        return {
            "tool_name": proposal.tool_name,
            "tool_arguments": dict(proposal.arguments),
            "tool_payload_digest": proposal.payload_digest,
            "operation_key": operation_key,
            "idempotency_key": f"ua-lg:{state['run_id']}:governed_tool:{operation_key}",
        }

    def _governed_tool_node(self, state: PilotState) -> PilotState:
        run = self._run_from_state(state)
        proposal = ToolProposal(state["tool_name"], state["tool_arguments"]).normalized()
        attempt_count = int(state.get("transient_attempt_count", 0))
        backoff_s = float(state.get("transient_backoff_s", 0.1))

        if state.get("awaiting_transient_retry"):
            time.sleep(backoff_s * (0.5 + random.random()))

        # Revalidate claim, capability, and budget before every gateway attempt
        # (transient retries and approval resumes must not reuse stale authority).
        try:
            self.gateway.revalidate(run)
            outcome = self.gateway.call_read_tool(
                run=run,
                proposal=proposal,
                idempotency_key=state["idempotency_key"],
                timeout_s=_GOVERNED_TOOL_TIMEOUT_S,
            )
        except TransientGatewayError:
            attempt_count += 1
            if attempt_count >= _MAX_GOVERNED_TOOL_ATTEMPTS:
                return {
                    "transient_attempt_count": attempt_count,
                    "awaiting_transient_retry": False,
                    "idempotency_key": state["idempotency_key"],
                    "operation_key": state["operation_key"],
                    "status": "failed",
                    "failure_reason": redact_sensitive_content(
                        "governed tool gateway exhausted retry budget",
                        max_len=200,
                    ),
                }
            # Checkpoint the increment before the next attempt so a crash during
            # backoff cannot reload a stale count and grant another full budget.
            return {
                "transient_attempt_count": attempt_count,
                "transient_backoff_s": min(backoff_s * 2.0, 1.0),
                "awaiting_transient_retry": True,
                "awaiting_approval_interrupt": False,
                "idempotency_key": state["idempotency_key"],
                "operation_key": state["operation_key"],
            }

        common = {
            "transient_attempt_count": attempt_count,
            "awaiting_transient_retry": False,
            "idempotency_key": state["idempotency_key"],
            "operation_key": state["operation_key"],
        }

        if outcome.status == "pending":
            # Checkpoint transient_attempt_count before interrupt(): interrupt()
            # halts the node without returning, so a sibling approval-gate node
            # owns the pause while this node's state update is already persisted.
            return {
                **common,
                "approval_id": outcome.approval_id,
                "awaiting_approval_interrupt": True,
            }

        if outcome.status == "denied":
            return {
                **common,
                "approval_id": outcome.approval_id,
                "denial_reason": redact_sensitive_content(outcome.reason, max_len=200),
                "awaiting_approval_interrupt": False,
                "status": "denied",
            }

        reduced = _bounded_result(outcome.result)
        return {
            **common,
            "approval_id": outcome.approval_id,
            "reduced_result": reduced,
            "result_digest": _digest_json(reduced),
            "awaiting_approval_interrupt": False,
        }

    def _governed_tool_approval_gate_node(self, state: PilotState) -> PilotState:
        run = self._run_from_state(state)
        proposal = ToolProposal(state["tool_name"], state["tool_arguments"]).normalized()
        interrupt(
            {
                "run_id": run.run_id,
                "task_id": run.task_id,
                "node_key": "governed_tool",
                "tool_name": proposal.tool_name,
                "payload_digest": proposal.payload_digest,
                "approval_id": state["approval_id"],
            }
        )
        # The resume value returned by interrupt() is deliberately ignored.
        # governed_tool revalidates and re-queries the gateway on the next hop.
        return {"awaiting_approval_interrupt": False}

    def _route_after_governed_tool(
        self, state: PilotState
    ) -> Literal["approval", "transient_retry", "allowed", "denied", "failed"]:
        if state.get("awaiting_approval_interrupt"):
            return "approval"
        if state.get("awaiting_transient_retry"):
            return "transient_retry"
        if state.get("status") == "denied":
            return "denied"
        if state.get("status") == "failed":
            return "failed"
        return "allowed"

    def _synthesis_node(self, state: PilotState) -> PilotState:
        output = {
            "summary": f"Governed evidence collected for task {state['task_id']}",
            "evidence_digest": state["result_digest"],
            "tool_name": state["tool_name"],
        }
        return {"output": output, "output_digest": _digest_json(output), "status": "completed"}

    def _initial_state(self, run: PilotRun) -> PilotState:
        return {
            "contract_version": CONTRACT_VERSION,
            "graph_name": GRAPH_NAME,
            "graph_version": GRAPH_VERSION,
            "definition_digest": self.definition_digest,
            "state_schema_version": STATE_SCHEMA_VERSION,
            "run_id": run.run_id,
            "task_id": run.task_id,
            "task_updated_at": run.task_updated_at,
            "claimed_by": run.claimed_by,
            "claim_expires_at": run.claim_expires_at,
            "expected_profile": run.expected_profile,
            "required_capabilities": list(run.required_capabilities),
            "budget_revision": run.budget_revision,
            "trace_id": run.trace_id,
            "input_digest": run.input_digest,
            "query": run.query,
            "status": "created",
        }

    def _validate_checkpoint(self, state: Mapping[str, Any], run: PilotRun) -> None:
        if state.get("definition_digest") != self.definition_digest:
            raise DefinitionMismatchError("checkpoint definition digest does not match this runner")
        expected = {
            "contract_version": CONTRACT_VERSION,
            "graph_name": GRAPH_NAME,
            "graph_version": GRAPH_VERSION,
            "state_schema_version": STATE_SCHEMA_VERSION,
            "run_id": run.run_id,
            "task_id": run.task_id,
            "task_updated_at": run.task_updated_at,
            "claimed_by": run.claimed_by,
            "claim_expires_at": run.claim_expires_at,
            "expected_profile": run.expected_profile,
            "required_capabilities": list(run.required_capabilities),
            "budget_revision": run.budget_revision,
            "trace_id": run.trace_id,
            "input_digest": run.input_digest,
        }
        mismatches = [key for key, value in expected.items() if state.get(key) != value]
        if mismatches:
            raise InvalidRunError(
                "checkpoint conflicts with immutable run fields: " + ", ".join(mismatches)
            )

    def _run_from_state(self, state: PilotState) -> PilotRun:
        return PilotRun(
            run_id=state["run_id"],
            task_id=state["task_id"],
            task_updated_at=state["task_updated_at"],
            claimed_by=state["claimed_by"],
            claim_expires_at=state["claim_expires_at"],
            expected_profile=state["expected_profile"],
            required_capabilities=tuple(state["required_capabilities"]),
            budget_revision=state["budget_revision"],
            trace_id=state["trace_id"],
            query=state["query"],
        )

    def _event_from_update(self, update: Mapping[str, Any]) -> PilotEvent | None:
        if "__interrupt__" in update:
            interrupts = update["__interrupt__"]
            if not interrupts:
                raise PilotError("LangGraph returned an empty interrupt payload")
            payload = sanitize_metadata(dict(interrupts[0].value))
            return PilotEvent("approval_interrupted", "governed_tool", payload)
        for node_key, values in update.items():
            if node_key == "intake":
                payload = {"input_digest": _digest_json(values["normalized_query"])}
            elif node_key == "agentic":
                payload = {
                    "tool_name": values["tool_name"],
                    "payload_digest": values["tool_payload_digest"],
                }
            elif node_key == "governed_tool":
                if values.get("status") == "denied":
                    payload = {
                        "idempotency_key": values.get("idempotency_key"),
                        "operation_key": values.get("operation_key"),
                        "outcome": "denied",
                        "reason": values["denial_reason"],
                    }
                elif values.get("status") == "failed":
                    payload = {
                        "idempotency_key": values.get("idempotency_key"),
                        "operation_key": values.get("operation_key"),
                        "outcome": "failed",
                        "reason": values["failure_reason"],
                        "transient_attempt_count": values.get("transient_attempt_count"),
                    }
                elif values.get("awaiting_approval_interrupt") or values.get(
                    "awaiting_transient_retry"
                ):
                    return None
                else:
                    payload = {
                        "idempotency_key": values.get("idempotency_key"),
                        "operation_key": values.get("operation_key"),
                        "result_digest": values["result_digest"],
                    }
            elif node_key == "synthesis":
                payload = {"output_digest": values["output_digest"]}
            else:
                continue
            return PilotEvent("node_completed", node_key, sanitize_metadata(payload))
        return None

    def _denied_event(
        self,
        state: Mapping[str, Any],
        *,
        duplicate: bool,
        recovered: bool,
    ) -> PilotEvent:
        return PilotEvent(
            kind="run_denied",
            node_key=None,
            payload=sanitize_metadata(
                {
                    "approval_id": state.get("approval_id"),
                    "duplicate": duplicate,
                    "reason": state.get("denial_reason") or "denied by canonical authority",
                    "recovered": recovered,
                }
            ),
        )

    def _failed_event(
        self,
        state: Mapping[str, Any],
        *,
        duplicate: bool,
        recovered: bool,
    ) -> PilotEvent:
        return PilotEvent(
            kind="run_failed",
            node_key=None,
            payload=sanitize_metadata(
                {
                    "duplicate": duplicate,
                    "reason": state.get("failure_reason")
                    or "governed tool gateway exhausted retry budget",
                    "recovered": recovered,
                    "transient_attempt_count": state.get("transient_attempt_count"),
                }
            ),
        )

    def _definition_digest(self) -> str:
        methods = (
            self._build_graph,
            self._intake_node,
            self._agentic_node,
            self._governed_tool_node,
            self._governed_tool_approval_gate_node,
            self._route_after_governed_tool,
            self._synthesis_node,
        )
        manifest = {
            "contract": CONTRACT_VERSION,
            "graph": GRAPH_NAME,
            "version": GRAPH_VERSION,
            "state_schema_version": STATE_SCHEMA_VERSION,
            "state_fields": sorted(PilotState.__annotations__),
            "allowed_tools": sorted(ALLOWED_READ_TOOLS),
            "governed_tool_policy": {
                "max_attempts": _MAX_GOVERNED_TOOL_ATTEMPTS,
                "timeout_s": _GOVERNED_TOOL_TIMEOUT_S,
                "retry_on": TransientGatewayError.__name__,
                "attempt_counter": "transient_attempt_count",
            },
            "tool_schemas": {
                "web_search": {
                    "query": f"string[1:{_MAX_QUERY_CHARS}]",
                    "max_results": "integer[1:3]",
                }
            },
            "planner_definition_digest": self.planner.definition_digest,
            "node_sources": [inspect.getsource(method.__func__) for method in methods],
            "helper_sources": [
                inspect.getsource(ToolProposal.normalized),
                inspect.getsource(_bounded_result),
                inspect.getsource(_digest_json),
                # Governed-tool nodes rebuild PilotRun via this helper before every
                # authority call; hash the bound implementation like node_sources.
                inspect.getsource(self._run_from_state.__func__),
            ],
        }
        if not re.fullmatch(r"[0-9a-f]{64}", self.planner.definition_digest):
            raise PilotError("planner definition_digest must be a lowercase SHA-256 digest")
        return _digest_json(manifest)

    def _ensure_open(self) -> None:
        if self._closed:
            raise PilotError("pilot runner is closed")


#: The string fields of a checkpointed run, checked by type before a run is rebuilt.
_RUN_TEXT_FIELDS = (
    "run_id",
    "task_updated_at",
    "claimed_by",
    "claim_expires_at",
    "expected_profile",
    "budget_revision",
    "trace_id",
    "query",
)


def _check_run_field_types(values: Mapping[str, Any]) -> None:
    """Refuse a checkpoint whose authority fields have the wrong types (TypeError)."""
    for name in _RUN_TEXT_FIELDS:
        if not isinstance(values.get(name), str):
            raise TypeError(f"checkpoint field {name} must be a string")
    task_id = values.get("task_id")
    if not isinstance(task_id, int) or isinstance(task_id, bool):
        raise TypeError("checkpoint field task_id must be an integer")
    capabilities = values.get("required_capabilities")
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise TypeError("checkpoint field required_capabilities must be a list of strings")


def default_checkpoint_path() -> Path:
    """Return the dedicated pilot DB path; never the authoritative memory DB."""
    return config.data_home() / "langgraph" / "checkpoints.sqlite3"


def _open_checkpoint(path: Path) -> sqlite3.Connection:
    resolved = path.resolve(strict=False)
    memory_db = config.db_path().resolve(strict=False)
    if resolved == memory_db:
        raise PilotError("LangGraph checkpoints must not share memory.db")
    if resolved.exists() and memory_db.exists() and os.path.samefile(resolved, memory_db):
        raise PilotError("LangGraph checkpoints must not share memory.db")
    parent = resolved.parent
    parent_existed = parent.exists()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not parent_existed:
        parent.chmod(0o700)
    if path.is_symlink():
        raise PilotError("checkpoint path must not be a symbolic link")
    if not resolved.exists():
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(resolved, flags, 0o600)
        os.close(fd)
    if not resolved.is_file():
        raise PilotError("checkpoint path must be a regular file")
    resolved.chmod(0o600)
    return sqlite3.connect(resolved, check_same_thread=False)


def _bounded_result(result: Any) -> Any:
    try:
        raw_encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PilotError("governed tool result is not JSON-serializable") from exc
    if len(raw_encoded.encode("utf-8")) > _MAX_REDUCED_RESULT_BYTES:
        raise PilotError("governed tool result exceeds the pilot checkpoint bound")
    if isinstance(result, dict):
        if len(result) > _SANITIZER_MAX_METADATA_ENTRIES:
            raise PilotError("governed tool result exceeds the pilot checkpoint bound")
        reduced = sanitize_metadata(result, max_payload_bytes=_MAX_REDUCED_RESULT_BYTES)
    else:
        wrapped = sanitize_metadata({"value": result}, max_payload_bytes=_MAX_REDUCED_RESULT_BYTES)
        if "_truncated" in wrapped:
            raise PilotError("governed tool result exceeds the pilot checkpoint bound")
        reduced = wrapped["value"]
    if isinstance(reduced, dict) and "_truncated" in reduced:
        raise PilotError("governed tool result exceeds the pilot checkpoint bound")
    try:
        encoded = json.dumps(reduced, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PilotError("governed tool result is not JSON-serializable") from exc
    if len(encoded.encode("utf-8")) > _MAX_REDUCED_RESULT_BYTES:
        raise PilotError("governed tool result exceeds the pilot checkpoint bound")
    return reduced


def _digest_json(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise PilotError("pilot values must be JSON-serializable") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
