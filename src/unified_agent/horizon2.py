"""Small provider boundary for Horizon 2 integrations.

This module deliberately does not execute work, mutate tasks, or resolve an
approval.  Those remain responsibilities of the authenticated Unified-Agent
hub.  It gives provider adapters one neutral envelope, keeps A2A exchanges
non-authoritative, and applies a reviewed, revocable connector registry before
the caller invokes the existing policy/approval path.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from . import config
from .telemetry.redaction import sanitize_metadata
from .telemetry.schema import SpanOutcome, TelemetrySpan

CONTRACT_VERSION = "ua.execution-envelope/v1"
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PROVENANCE_RE = re.compile(r"^sha256:[0-9a-f]{3,64}$")
DEFAULT_REVIEW_TTL_DAYS = 90


class ExecutionAction(StrEnum):
    START = "start"
    RESUME = "resume"
    CANCEL = "cancel"
    STATUS = "status"
    EVENTS = "events"
    OUTCOME = "outcome"


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _identifier(value: str, field_name: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field_name} must match {_IDENTIFIER_RE.pattern}")
    return value


@dataclass(frozen=True)
class ExecutionEnvelope:
    """One provider-neutral request or observation.

    ``authority_revision`` is copied from the canonical task/claim state by a
    hub-side adapter.  Remote providers may report work, but cannot use this
    object to establish task ownership or bypass the hub's policy gate.
    """

    action: ExecutionAction
    execution_id: str
    task_id: int
    provider: str
    authority_revision: str
    trace_id: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    payload: Mapping[str, Any] = field(default_factory=dict)
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError(f"unsupported execution contract: {self.contract_version}")
        _identifier(self.execution_id, "execution_id")
        _identifier(self.provider, "provider")
        if not isinstance(self.task_id, int) or isinstance(self.task_id, bool) or self.task_id < 1:
            raise ValueError("task_id must be a positive integer")
        if not self.authority_revision or len(self.authority_revision) > 128:
            raise ValueError("authority_revision must be 1-128 characters")
        if not self.trace_id or len(self.trace_id) > 128:
            raise ValueError("trace_id must be 1-128 characters")
        if self.action is ExecutionAction.START and self.status is not ExecutionStatus.PENDING:
            raise ValueError("start envelopes must begin pending")
        if self.action is ExecutionAction.CANCEL and self.status in {
            ExecutionStatus.CANCELLED,
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
        }:
            raise ValueError("terminal executions cannot be cancelled")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "action": self.action.value,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "provider": self.provider,
            "authority_revision": self.authority_revision,
            "trace_id": self.trace_id,
            "status": self.status.value,
            "payload": sanitize_metadata(dict(self.payload)),
        }


class ProviderExecutionAdapter(Protocol):
    """Shape every execution provider exposes to the governed hub."""

    def start(self, envelope: ExecutionEnvelope) -> ExecutionEnvelope: ...

    def resume(self, envelope: ExecutionEnvelope) -> ExecutionEnvelope: ...

    def cancel(self, envelope: ExecutionEnvelope) -> ExecutionEnvelope: ...

    def status(self, envelope: ExecutionEnvelope) -> ExecutionEnvelope: ...

    def events(self, envelope: ExecutionEnvelope) -> Iterable[ExecutionEnvelope]: ...

    def outcome(self, envelope: ExecutionEnvelope) -> ExecutionEnvelope: ...


# Codex/Claude/Agy run through approved-dispatch; LangGraph through the
# governed pilot. These adapters only project that hub state into envelopes.
DISPATCH_PROVIDERS = frozenset({"codex", "claude", "agy"})
LANGGRAPH_PROVIDER = "langgraph"
WIRED_PROVIDERS = DISPATCH_PROVIDERS | {LANGGRAPH_PROVIDER}

_DISPATCH_STATE_STATUS = {
    "claimed": ExecutionStatus.RUNNING,
    "launched": ExecutionStatus.RUNNING,
    "running": ExecutionStatus.RUNNING,
    "blocked": ExecutionStatus.BLOCKED,
    "retryable": ExecutionStatus.BLOCKED,
    "finished": ExecutionStatus.SUCCEEDED,
    "failed": ExecutionStatus.FAILED,
    "denied": ExecutionStatus.FAILED,
    "cancelled": ExecutionStatus.CANCELLED,
    "expired": ExecutionStatus.FAILED,
    "refused": ExecutionStatus.FAILED,
}


def adapter_for(provider: str) -> ProviderExecutionAdapter:
    """Return the observation adapter for a wired provider.

    Unknown providers fail closed. The adapter never spawns a CLI, inserts a
    ``dispatch_runs`` row, or mutates a task — the hub remains the executor.
    """
    name = _identifier(provider, "provider")
    if name not in WIRED_PROVIDERS:
        raise ValueError(f"no execution adapter for provider: {name}")
    return ObservationAdapter(name)


def _label_matches_provider(agent_label: str, provider: str) -> bool:
    # The label's family decides (a renamed label, as in the public export,
    # still names its provider); free-form labels fall back to the text.
    family = config.agent_family(agent_label)
    if family is not None:
        return family.lower() == provider.lower()
    return provider.lower() in (agent_label or "").lower()


def _observe_dispatch(envelope: ExecutionEnvelope) -> tuple[ExecutionStatus, dict[str, Any]]:
    from .dispatch_reads import list_runs

    runs = list_runs(task_id=envelope.task_id, limit=50)
    matching = [
        run
        for run in runs
        if _label_matches_provider(str(run.get("agent_label") or ""), envelope.provider)
    ]
    if not matching:
        return ExecutionStatus.PENDING, {
            "source": "dispatch_runs",
            "matched": False,
            "spawned": False,
        }
    run = matching[0]
    status = _DISPATCH_STATE_STATUS.get(str(run.get("state") or ""), ExecutionStatus.PENDING)
    return status, {
        "source": "dispatch_runs",
        "matched": True,
        "spawned": False,
        "run_id": run.get("run_id"),
        "state": run.get("state"),
        "agent_label": run.get("agent_label"),
    }


def _observe_langgraph(envelope: ExecutionEnvelope) -> tuple[ExecutionStatus, dict[str, Any]]:
    from . import config

    path = config.data_home() / "langgraph" / "checkpoints.sqlite3"
    payload: dict[str, Any] = {
        "source": "langgraph_checkpoints",
        "spawned": False,
        "task_id": envelope.task_id,
        "checkpoint_db": str(path),
        "exists": path.is_file(),
    }
    if not path.is_file():
        return ExecutionStatus.PENDING, payload
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id LIKE 'ua-lg:%'"
        ).fetchone()
    except sqlite3.Error:
        payload["matched"] = False
        return ExecutionStatus.PENDING, payload
    finally:
        conn.close()
    count = int(row[0]) if row else 0
    payload["checkpoint_count"] = count
    payload["matched"] = count > 0
    return (ExecutionStatus.RUNNING if count else ExecutionStatus.PENDING), payload


class ObservationAdapter:
    """Project Codex/Claude/Agy/LangGraph hub state into ExecutionEnvelope.

    start/resume/cancel/status/events/outcome never launch work. They validate
    the envelope's provider and return the latest observed status.
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider

    def _require(self, envelope: ExecutionEnvelope, action: ExecutionAction) -> ExecutionEnvelope:
        if envelope.provider != self.provider:
            raise ValueError(
                f"envelope provider {envelope.provider!r} does not match adapter {self.provider!r}"
            )
        if envelope.action is not action:
            raise ValueError(f"{action.value} requires a {action.value} envelope")
        return envelope

    def _project(
        self,
        envelope: ExecutionEnvelope,
        *,
        action: ExecutionAction = ExecutionAction.STATUS,
        extra: Mapping[str, Any] | None = None,
    ) -> ExecutionEnvelope:
        if self.provider in DISPATCH_PROVIDERS:
            status, payload = _observe_dispatch(envelope)
        else:
            status, payload = _observe_langgraph(envelope)
        merged = dict(payload)
        if extra:
            merged.update(extra)
        return ExecutionEnvelope(
            action=action,
            execution_id=envelope.execution_id,
            task_id=envelope.task_id,
            provider=self.provider,
            authority_revision=envelope.authority_revision,
            trace_id=envelope.trace_id,
            status=status,
            payload=merged,
        )

    def start(self, envelope: ExecutionEnvelope) -> ExecutionEnvelope:
        self._require(envelope, ExecutionAction.START)
        return self._project(envelope)

    def resume(self, envelope: ExecutionEnvelope) -> ExecutionEnvelope:
        self._require(envelope, ExecutionAction.RESUME)
        return self._project(envelope)

    def cancel(self, envelope: ExecutionEnvelope) -> ExecutionEnvelope:
        self._require(envelope, ExecutionAction.CANCEL)
        return self._project(envelope, extra={"cancel_applied": False})

    def status(self, envelope: ExecutionEnvelope) -> ExecutionEnvelope:
        self._require(envelope, ExecutionAction.STATUS)
        return self._project(envelope)

    def events(self, envelope: ExecutionEnvelope) -> Iterable[ExecutionEnvelope]:
        self._require(envelope, ExecutionAction.EVENTS)
        yield self._project(envelope, action=ExecutionAction.EVENTS)

    def outcome(self, envelope: ExecutionEnvelope) -> ExecutionEnvelope:
        self._require(envelope, ExecutionAction.OUTCOME)
        return self._project(envelope, action=ExecutionAction.OUTCOME)


_A2A_STATUS_MAP = {
    "submitted": ExecutionStatus.PENDING,
    "pending": ExecutionStatus.PENDING,
    "unknown": ExecutionStatus.PENDING,
    "working": ExecutionStatus.RUNNING,
    "running": ExecutionStatus.RUNNING,
    "input-required": ExecutionStatus.BLOCKED,
    "input_required": ExecutionStatus.BLOCKED,
    "auth-required": ExecutionStatus.BLOCKED,
    "auth_required": ExecutionStatus.BLOCKED,
    "blocked": ExecutionStatus.BLOCKED,
    "canceled": ExecutionStatus.CANCELLED,
    "cancelled": ExecutionStatus.CANCELLED,
    "completed": ExecutionStatus.SUCCEEDED,
    "succeeded": ExecutionStatus.SUCCEEDED,
    "failed": ExecutionStatus.FAILED,
    "rejected": ExecutionStatus.FAILED,
}


def normalize_a2a_status(raw: str) -> str:
    """Map A2A TaskState values onto ExecutionStatus strings."""
    token = str(raw or "").strip().lower()
    if token.startswith("task_state_"):
        token = token[len("task_state_") :]
    mapped = _A2A_STATUS_MAP.get(token)
    if mapped is None:
        raise ValueError(f"unsupported A2A task status: {raw}")
    return mapped.value


def _require_http_url(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A2A URL must be http(s) with a host")
    return url


def _agent_card_url(base_url: str) -> str:
    from urllib.parse import urlparse, urlunparse

    url = _require_http_url(base_url.strip())
    if url.rstrip("/").endswith(("agent-card.json", "agent.json")):
        return url
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, "/.well-known/agent-card.json", "", "", ""))


def _execution_id_from_remote(remote_id: str) -> str:
    cleaned = str(remote_id or "").strip().lower()
    if _IDENTIFIER_RE.fullmatch(cleaned):
        return cleaned
    slug = "t-" + re.sub(r"[^a-z0-9_.-]", "-", cleaned)[:61]
    if _IDENTIFIER_RE.fullmatch(slug):
        return slug
    raise ValueError(f"A2A task id is not a usable identifier: {remote_id!r}")


def _live_get_json(url: str) -> Mapping[str, Any]:
    return _live_http_json("GET", url, None)


def _live_post_json(url: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
    return _live_http_json("POST", url, body)


def _live_http_json(method: str, url: str, body: Mapping[str, Any] | None) -> Mapping[str, Any]:
    import httpx

    from . import config
    from .safety import SSRFShieldTransport, check_fetch_host

    parsed_url = _require_http_url(url)
    from urllib.parse import urlparse

    hostname = urlparse(parsed_url).hostname or ""
    try:
        check_fetch_host(hostname)
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    try:
        with httpx.Client(
            transport=SSRFShieldTransport(),
            follow_redirects=True,
            max_redirects=5,
            timeout=config.FETCH_TIMEOUT_S,
        ) as client:
            resp = client.request(method, parsed_url, json=dict(body) if body is not None else None)
            resp.raise_for_status()
            if len(resp.content) > config.FETCH_MAX_BYTES:
                raise ValueError("A2A response exceeds fetch cap")
            data = resp.json()
    except httpx.HTTPError as exc:
        raise ValueError(f"A2A fetch failed: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("A2A response must be a JSON object")
    return data


@dataclass(frozen=True)
class AgentCard:
    """Discovered A2A agent card. It is not a Unified-Agent identity."""

    name: str
    url: str
    protocol_version: str = "1.0"
    skills: tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return sanitize_metadata(
            {
                "name": self.name,
                "url": self.url,
                "protocol_version": self.protocol_version,
                "skills": list(self.skills),
                "description": self.description,
            }
        )

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> AgentCard:
        if not isinstance(data, Mapping):
            raise ValueError("agent card must be an object")
        name = str(data.get("name") or "").strip()
        if not name or len(name) > 200:
            raise ValueError("agent card name must be 1-200 characters")
        url = str(data.get("url") or "").strip()
        version = str(data.get("protocolVersion") or data.get("version") or "1.0").strip() or "1.0"
        interfaces = data.get("supportedInterfaces") or []
        if not url and isinstance(interfaces, list):
            chosen: Mapping[str, Any] | None = None
            for item in interfaces:
                if not isinstance(item, Mapping) or not item.get("url"):
                    continue
                binding = str(item.get("protocolBinding") or "").upper().replace("+", "")
                if "JSONRPC" in binding or binding in {"JSON-RPC", "JSONRPC"}:
                    chosen = item
                    break
                if chosen is None:
                    chosen = item
            if chosen is not None:
                url = str(chosen.get("url") or "").strip()
                if chosen.get("protocolVersion"):
                    version = str(chosen["protocolVersion"]).strip()
        if not url:
            raise ValueError("agent card is missing an RPC url")
        _require_http_url(url)
        skills: list[str] = []
        raw_skills = data.get("skills") or []
        if isinstance(raw_skills, list):
            for skill in raw_skills[:32]:
                if isinstance(skill, Mapping):
                    sid = str(skill.get("id") or skill.get("name") or "").strip()
                else:
                    sid = str(skill).strip()
                if sid:
                    skills.append(sid[:64])
        return cls(
            name=name[:200],
            url=url,
            protocol_version=version[:32],
            skills=tuple(skills),
            description=str(data.get("description") or "")[:500],
        )


@dataclass(frozen=True)
class A2ATask:
    """Bounded A2A task exchange; it is not a Unified-Agent task row."""

    id: str
    context_id: str
    status: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": self.status,
            "metadata": sanitize_metadata(dict(self.metadata)),
        }


def _task_from_rpc(response: Mapping[str, Any]) -> A2ATask:
    if response.get("error"):
        raise ValueError(f"A2A RPC error: {response['error']}")
    result = response.get("result", response)
    task = result.get("task") if isinstance(result, Mapping) and "task" in result else result
    if not isinstance(task, Mapping):
        raise ValueError("A2A response is missing a task object")
    remote_id = str(task.get("id") or "").strip()
    context = str(task.get("contextId") or task.get("context_id") or "").strip()
    status = task.get("status")
    state = str(status.get("state") if isinstance(status, Mapping) else status or "").strip()
    if not remote_id or not context or not state:
        raise ValueError("A2A task is missing id, contextId, or status")
    return A2ATask(
        id=_execution_id_from_remote(remote_id),
        context_id=context,
        status=normalize_a2a_status(state),
        metadata={"remote_task_id": remote_id},
    )


class A2AAdapter:
    """Translate safe execution observations to and from A2A task payloads."""

    provider = "a2a"

    def export(self, envelope: ExecutionEnvelope) -> A2ATask:
        return A2ATask(
            id=envelope.execution_id,
            context_id=f"ua-task-{envelope.task_id}",
            status=envelope.status.value,
            metadata={
                "contract_version": envelope.contract_version,
                "trace_id": envelope.trace_id,
                "authority_revision": envelope.authority_revision,
            },
        )

    def import_status(
        self,
        task: A2ATask,
        *,
        task_id: int,
        authority_revision: str,
        trace_id: str,
    ) -> ExecutionEnvelope:
        expected_context = f"ua-task-{task_id}"
        if task.context_id != expected_context:
            raise ValueError(f"A2A context {task.context_id!r} does not belong to task {task_id}")
        try:
            status = ExecutionStatus(task.status)
        except ValueError as exc:
            raise ValueError(f"unsupported A2A task status: {task.status}") from exc
        return ExecutionEnvelope(
            action=ExecutionAction.STATUS,
            execution_id=task.id,
            task_id=task_id,
            provider=self.provider,
            authority_revision=authority_revision,
            trace_id=trace_id,
            status=status,
            payload={"a2a_context_id": task.context_id},
        )

    def discover(
        self,
        base_url: str,
        *,
        fetch: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> AgentCard:
        """GET the well-known Agent Card. Does not create or claim a UA task."""
        getter = fetch or _live_get_json
        payload = getter(_agent_card_url(base_url))
        if not isinstance(payload, Mapping):
            raise ValueError("agent card response must be a JSON object")
        return AgentCard.from_payload(payload)

    def exchange(
        self,
        card: AgentCard,
        envelope: ExecutionEnvelope,
        *,
        rpc: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    ) -> ExecutionEnvelope:
        """Send or poll a remote A2A task and import it as an observation.

        ``authority_revision`` always comes from the hub envelope. The remote
        cannot establish a claim or change task ownership.
        """
        exported = self.export(envelope)
        if envelope.action is ExecutionAction.STATUS:
            method = "GetTask"
            params: dict[str, Any] = {"id": exported.id}
        elif envelope.action is ExecutionAction.START:
            method = "SendMessage"
            text = str((envelope.payload or {}).get("text") or envelope.execution_id)
            params = {
                "message": {
                    "role": "user",
                    "messageId": exported.id,
                    "contextId": exported.context_id,
                    "parts": [{"text": text[:2000]}],
                }
            }
        else:
            raise ValueError("A2A exchange supports start and status envelopes only")
        request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        response = rpc(request) if rpc is not None else _live_post_json(card.url, request)
        if not isinstance(response, Mapping):
            raise ValueError("A2A RPC response must be a JSON object")
        remote = _task_from_rpc(response)
        observed = self.import_status(
            remote,
            task_id=envelope.task_id,
            authority_revision=envelope.authority_revision,
            trace_id=envelope.trace_id,
        )
        return replace(
            observed,
            payload={
                **observed.payload,
                "remote_task_id": (remote.metadata or {}).get("remote_task_id"),
                "rpc_method": method,
                "authority_from": "hub",
            },
        )


@dataclass(frozen=True)
class GovernedServer:
    """A reviewed MCP server or connector with explicit, revocable scopes."""

    server_id: str
    connector: str
    provenance: str
    scopes: frozenset[str]
    reviewed_by: str
    review_ref: str
    revoked_at: str | None = None
    created_at: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.server_id, "server_id")
        _identifier(self.connector, "connector")
        if (
            not self.provenance.strip()
            or not self.reviewed_by.strip()
            or not self.review_ref.strip()
        ):
            raise ValueError("provenance, reviewed_by, and review_ref are required")
        if not _PROVENANCE_RE.fullmatch(self.provenance.strip()):
            raise ValueError("provenance must be sha256:<hex>")
        if not self.scopes or any(not _IDENTIFIER_RE.fullmatch(scope) for scope in self.scopes):
            raise ValueError("scopes must contain valid identifiers")

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def review_ttl_days() -> int:
    import os

    raw = os.environ.get("UA_H2_REVIEW_TTL_DAYS", str(DEFAULT_REVIEW_TTL_DAYS)).strip()
    try:
        days = int(raw)
    except ValueError:
        days = DEFAULT_REVIEW_TTL_DAYS
    return max(1, min(days, 3650))


def review_is_current(created_at: str | None, *, now: datetime | None = None) -> bool:
    """True when the review window from created_at has not elapsed."""
    if not created_at:
        return True
    try:
        stamped = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current < stamped + timedelta(days=review_ttl_days())


def live_mcp_connections() -> frozenset[str] | None:
    """Live MCP server keys from UA_H2_MCP_CONNECTIONS. None means unbound."""
    import os

    if "UA_H2_MCP_CONNECTIONS" not in os.environ:
        return None
    raw = os.environ.get("UA_H2_MCP_CONNECTIONS", "")
    return frozenset(part.strip() for part in raw.split(":") if part.strip())


def bind_live_mcp_connection(connector: str, connection_ref: str | None = None) -> str:
    """Bind a registered connector to a live MCP server key. No second registry."""
    ref = (connection_ref or connector).strip()
    if ref != connector:
        raise PermissionError(
            f"connection_ref {ref!r} does not match registered connector {connector!r}"
        )
    inventory = live_mcp_connections()
    if inventory is not None and ref not in inventory:
        raise PermissionError(f"connector {ref!r} is not bound to a live MCP connection")
    return ref


class ServerGovernance:
    """Pure reviewed-server scope check; durable state remains the hub's concern."""

    def __init__(self, servers: Iterable[GovernedServer] = ()) -> None:
        self._servers: dict[str, GovernedServer] = {}
        for server in servers:
            self.register(server)

    def register(self, server: GovernedServer) -> None:
        if server.server_id in self._servers:
            raise ValueError(f"server already registered: {server.server_id}")
        self._servers[server.server_id] = server

    def revoke(self, server_id: str, *, at: datetime | None = None) -> GovernedServer:
        server = self.get(server_id)
        if not server.active:
            return server
        revoked_at = (at or datetime.now(UTC)).isoformat()
        updated = replace(server, revoked_at=revoked_at)
        self._servers[server_id] = updated
        return updated

    def get(self, server_id: str) -> GovernedServer:
        try:
            return self._servers[server_id]
        except KeyError as exc:
            raise PermissionError(f"unregistered server: {server_id}") from exc

    def authorize(
        self,
        server_id: str,
        scope: str,
        *,
        connection_ref: str | None = None,
        now: datetime | None = None,
    ) -> GovernedServer:
        """Check a persisted, approval-gated server record's active scope."""
        server = self.get(server_id)
        if not server.active:
            raise PermissionError(f"server is revoked: {server_id}")
        if not review_is_current(server.created_at, now=now):
            raise PermissionError(f"server review has expired: {server_id}")
        if scope not in server.scopes:
            raise PermissionError(f"scope {scope!r} is not approved for {server_id}")
        bind_live_mcp_connection(server.connector, connection_ref)
        return server


CONNECTOR_PACKAGES: dict[str, frozenset[str]] = {
    "slack": frozenset({"read", "search"}),
    "drive": frozenset({"read"}),
    "obsidian": frozenset({"read", "write"}),
    "readwise": frozenset({"read"}),
    "granola": frozenset({"read"}),
    "github-app": frozenset({"read", "checks", "comments"}),
}


def package_connector(
    package: str,
    *,
    provenance: str,
    review_ref: str,
    reviewed_by: str,
    server_id: str | None = None,
    scopes: Iterable[str] | None = None,
) -> GovernedServer:
    """Build a GovernedServer from a named package. Does not persist or dogfood."""
    try:
        allowed = CONNECTOR_PACKAGES[package]
    except KeyError as exc:
        raise ValueError(f"unknown connector package: {package}") from exc
    chosen = allowed if scopes is None else frozenset(scopes)
    extra = chosen - allowed
    if extra:
        raise ValueError(f"scopes {sorted(extra)} are not in package {package}")
    if not chosen:
        raise ValueError("package scopes must not be empty")
    return GovernedServer(
        server_id=server_id or f"{package}-default",
        connector=package,
        provenance=provenance,
        scopes=chosen,
        reviewed_by=reviewed_by,
        review_ref=review_ref,
    )


@dataclass(frozen=True)
class TrajectoryEvaluation:
    """Safe re-score of recorded, already-redacted telemetry spans."""

    trace_id: str
    spans: int
    successful: int
    failed: int
    score: float


def evaluate_trajectory(spans: Iterable[TelemetrySpan | Mapping[str, Any]]) -> TrajectoryEvaluation:
    """Re-score a recorded trace without rerunning providers or changing authority."""

    trace_id: str | None = None
    total = successful = 0
    for span in spans:
        if isinstance(span, TelemetrySpan):
            candidate_trace_id = span.trace_id
            outcome = span.outcome
        else:
            candidate_trace_id = str(span.get("trace_id") or "")
            outcome = str(span.get("outcome") or SpanOutcome.UNKNOWN.value)
        if not candidate_trace_id:
            raise ValueError("trajectory spans must share one non-empty trace_id")
        if trace_id is None:
            trace_id = candidate_trace_id
        elif trace_id != candidate_trace_id:
            raise ValueError("trajectory spans must share one non-empty trace_id")
        total += 1
        successful += str(outcome) == SpanOutcome.SUCCESS.value
    if trace_id is None:
        raise ValueError("trajectory requires at least one span")
    failed = total - successful
    return TrajectoryEvaluation(
        trace_id=trace_id,
        spans=total,
        successful=successful,
        failed=failed,
        score=round(successful / total, 3),
    )


class SpanExporter(Protocol):
    """Sink used by export_and_replay. Must not mutate hub authority."""

    def export(self, spans: Sequence[TelemetrySpan]) -> int: ...


def export_and_replay(
    spans: Iterable[TelemetrySpan],
    exporter: SpanExporter,
) -> TrajectoryEvaluation:
    """Export recorded spans, then re-score them. Does not rerun providers."""
    recorded = list(spans)
    exporter.export(recorded)
    return evaluate_trajectory(recorded)
