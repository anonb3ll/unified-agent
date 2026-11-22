"""MCP 2.x client capability matrix and compatibility evaluator (task #863).

Defines the formal capability matrix for supported MCP 2.x clients
(Desktop Agent5, Claude Code / Desktop, Desktop_Agent2, Google Antigravity, Cursor)
and transports (Streamable HTTP, stdio, SSE), and provides deterministic
compatibility evaluation with explicit diagnostics for missing capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from . import config


class McpCapability(StrEnum):
    """Core MCP 2.x capabilities evaluated across clients."""

    TOOL_ANNOTATIONS = "tool_annotations"
    STRUCTURED_OUTPUTS = "structured_outputs"
    ELICITATION = "elicitation"
    PROGRESS = "progress"
    RESOURCE_SUBSCRIPTIONS = "resource_subscriptions"


class SupportStatus(StrEnum):
    """Support classification for a capability."""

    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    CONDITIONAL = "conditional"


class TransportType(StrEnum):
    """Supported MCP 2.x transport layers."""

    STREAMABLE_HTTP = "streamable_http"
    STDIO = "stdio"
    SSE = "sse"


@dataclass(frozen=True)
class CapabilityDetails:
    """Detailed support profile for an MCP capability."""

    status: SupportStatus
    summary: str
    fallback_behavior: str
    failure_indicator: str
    required_conditions: list[str]


@dataclass(frozen=True)
class ClientMatrixEntry:
    """Capability specification for an MCP 2.x client."""

    client_id: str
    client_name: str
    primary_role: str
    supported_transports: list[TransportType]
    capabilities: dict[McpCapability, CapabilityDetails]

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "client_name": self.client_name,
            "primary_role": self.primary_role,
            "supported_transports": [t.value for t in self.supported_transports],
            "capabilities": {
                cap.value: {
                    "status": det.status.value,
                    "summary": det.summary,
                    "fallback_behavior": det.fallback_behavior,
                    "failure_indicator": det.failure_indicator,
                    "required_conditions": det.required_conditions,
                }
                for cap, det in self.capabilities.items()
            },
        }


CLIENT_CAPABILITY_MATRIX: dict[str, ClientMatrixEntry] = {
    "hermes": ClientMatrixEntry(
        client_id="hermes",
        client_name="Desktop Agent5",
        primary_role="Autonomous coordination and queue stewardship",
        supported_transports=[TransportType.STREAMABLE_HTTP, TransportType.STDIO],
        capabilities={
            McpCapability.TOOL_ANNOTATIONS: CapabilityDetails(
                status=SupportStatus.SUPPORTED,
                summary="Reads readOnlyHint, destructiveHint, idempotentHint in tools/list",
                fallback_behavior="Defaults to unannotated tool policy",
                failure_indicator="Missing annotations in tool schema",
                required_conditions=[],
            ),
            McpCapability.STRUCTURED_OUTPUTS: CapabilityDetails(
                status=SupportStatus.SUPPORTED,
                summary="Parses typed JSON responses matching Pydantic output models",
                fallback_behavior="Rejects unparseable responses with JSON decode error",
                failure_indicator="Schema validation error on malformed fields",
                required_conditions=[],
            ),
            McpCapability.ELICITATION: CapabilityDetails(
                status=SupportStatus.UNSUPPORTED,
                summary="Hermes agent client does not declare elicitation capability",
                fallback_behavior="Approval-gated tools park in /ui queue and raise ApprovalPendingError",
                failure_indicator="ApprovalPendingError propagates with /ui queue resolution pointer",
                required_conditions=[],
            ),
            McpCapability.PROGRESS: CapabilityDetails(
                status=SupportStatus.UNSUPPORTED,
                summary="Agent loop does not bind progress handlers",
                fallback_behavior="Server mcp_progress.report_progress silently no-ops without error",
                failure_indicator="No progress events recorded in client logs",
                required_conditions=[],
            ),
            McpCapability.RESOURCE_SUBSCRIPTIONS: CapabilityDetails(
                status=SupportStatus.UNSUPPORTED,
                summary="Relies on active polling via task_list rather than live resource streams",
                fallback_behavior="Uses tools/call to task_list/run queries",
                failure_indicator="Subscription requests fail if attempted without listener",
                required_conditions=[],
            ),
        },
    ),
    "claude": ClientMatrixEntry(
        client_id="claude",
        client_name="Claude Code / Claude Desktop",
        primary_role="Interactive developer CLI and desktop assistant",
        supported_transports=[TransportType.STREAMABLE_HTTP, TransportType.STDIO],
        capabilities={
            McpCapability.TOOL_ANNOTATIONS: CapabilityDetails(
                status=SupportStatus.SUPPORTED,
                summary="Uses readOnlyHint and destructiveHint for user confirmation UX",
                fallback_behavior="Prompts user on every unannotated or destructive tool",
                failure_indicator="Unexpected confirmation dialogs on read-only tools",
                required_conditions=[],
            ),
            McpCapability.STRUCTURED_OUTPUTS: CapabilityDetails(
                status=SupportStatus.SUPPORTED,
                summary="Directly consumes structured JSON outputs with schema enforcement",
                fallback_behavior="Fails with tool call execution error if schema validation fails",
                failure_indicator="ValidationError on missing or invalid output fields",
                required_conditions=[],
            ),
            McpCapability.ELICITATION: CapabilityDetails(
                status=SupportStatus.CONDITIONAL,
                summary="Presents interactive approve/deny forms when enabled and permitted",
                fallback_behavior="Falls back to /ui queue if disabled, undeclared, or unprivileged",
                failure_indicator="ApprovalPendingError if UA_ELICITATION_APPROVALS=0 or profile restricted",
                required_conditions=[
                    "UA_ELICITATION_APPROVALS=1",
                    "Client declares elicitation capability",
                    "Session profile permits approval_resolve",
                ],
            ),
            McpCapability.PROGRESS: CapabilityDetails(
                status=SupportStatus.SUPPORTED,
                summary="Receives notifications/progress milestones via progressToken",
                fallback_behavior="Executes tool synchronously without progress display",
                failure_indicator="Progress bar omitted when progressToken is absent",
                required_conditions=["Client passes _meta.progressToken"],
            ),
            McpCapability.RESOURCE_SUBSCRIPTIONS: CapabilityDetails(
                status=SupportStatus.PARTIAL,
                summary="Reads resources/templates; desktop client handles resource updates",
                fallback_behavior="Falls back to explicit resource reads",
                failure_indicator="ResourceNotFoundError on unknown URIs; ResourceError on profile denial",
                required_conditions=["Valid resource URI", "Profile permission for resource tool"],
            ),
        },
    ),
    "codex": ClientMatrixEntry(
        client_id="codex",
        client_name="Desktop_Agent2",
        primary_role="Autonomous coding and test automation worker",
        supported_transports=[TransportType.STDIO, TransportType.STREAMABLE_HTTP],
        capabilities={
            McpCapability.TOOL_ANNOTATIONS: CapabilityDetails(
                status=SupportStatus.SUPPORTED,
                summary="Reads tool annotations for safe planning and retry decisions",
                fallback_behavior="Conservative fallback treating unannotated tools as non-idempotent",
                failure_indicator="Tools lack expected annotation fields",
                required_conditions=[],
            ),
            McpCapability.STRUCTURED_OUTPUTS: CapabilityDetails(
                status=SupportStatus.SUPPORTED,
                summary="Validates typed structured tool output payloads",
                fallback_behavior="Fails tool step if structured model validation fails",
                failure_indicator="ValidationError on output model mismatch",
                required_conditions=[],
            ),
            McpCapability.ELICITATION: CapabilityDetails(
                status=SupportStatus.UNSUPPORTED,
                summary="Non-interactive worker; does not declare elicitation capability",
                fallback_behavior="Approval-gated tools park in /ui queue and raise ApprovalPendingError",
                failure_indicator="ApprovalPendingError with approval ID logged",
                required_conditions=[],
            ),
            McpCapability.PROGRESS: CapabilityDetails(
                status=SupportStatus.UNSUPPORTED,
                summary="Non-interactive stdio runner; ignores progress tokens",
                fallback_behavior="report_progress no-ops in worker thread",
                failure_indicator="No progress callbacks attached",
                required_conditions=[],
            ),
            McpCapability.RESOURCE_SUBSCRIPTIONS: CapabilityDetails(
                status=SupportStatus.PARTIAL,
                summary="Supports resources/read; does not maintain long-lived subscription stream",
                fallback_behavior="Executes one-off resources/read calls",
                failure_indicator="ResourceNotFoundError on unknown URIs",
                required_conditions=[],
            ),
        },
    ),
    "agy": ClientMatrixEntry(
        client_id="agy",
        client_name="Google Antigravity (Agy)",
        primary_role="Agentic IDE and terminal orchestration client",
        supported_transports=[TransportType.STREAMABLE_HTTP, TransportType.STDIO],
        capabilities={
            McpCapability.TOOL_ANNOTATIONS: CapabilityDetails(
                status=SupportStatus.SUPPORTED,
                summary="Uses annotations for risk level rendering and confirmation gating",
                fallback_behavior="Standard tool prompt dialog",
                failure_indicator="Missing readOnlyHint/destructiveHint hints",
                required_conditions=[],
            ),
            McpCapability.STRUCTURED_OUTPUTS: CapabilityDetails(
                status=SupportStatus.SUPPORTED,
                summary="Natively parses structured outputs into typed context and widgets",
                fallback_behavior="Falls back to text serialization",
                failure_indicator="ValidationError when required output fields missing",
                required_conditions=[],
            ),
            McpCapability.ELICITATION: CapabilityDetails(
                status=SupportStatus.CONDITIONAL,
                summary="Interactive elicitation dialogs for approval resolution",
                fallback_behavior="Parks in /ui approval queue if not enabled or unprivileged",
                failure_indicator="ApprovalPendingError when elicitation unavailable",
                required_conditions=[
                    "UA_ELICITATION_APPROVALS=1",
                    "Client declares elicitation capability",
                    "Session profile permits approval_resolve",
                ],
            ),
            McpCapability.PROGRESS: CapabilityDetails(
                status=SupportStatus.SUPPORTED,
                summary="Renders live task progress bars from notifications/progress",
                fallback_behavior="Shows indeterminate spinner until tool completes",
                failure_indicator="Progress milestones omitted without progressToken",
                required_conditions=["Client passes _meta.progressToken"],
            ),
            McpCapability.RESOURCE_SUBSCRIPTIONS: CapabilityDetails(
                status=SupportStatus.SUPPORTED,
                summary="Live subscription to unified-agent://board, tasks, and runs",
                fallback_behavior="Explicit resource read or task_list polling",
                failure_indicator="ResourceNotFoundError / ResourceError on denied resources",
                required_conditions=["Valid resource URI", "Profile permission"],
            ),
        },
    ),
    "cursor": ClientMatrixEntry(
        client_id="cursor",
        client_name="Cursor",
        primary_role="IDE pair programmer client",
        supported_transports=[TransportType.STREAMABLE_HTTP, TransportType.STDIO],
        capabilities={
            McpCapability.TOOL_ANNOTATIONS: CapabilityDetails(
                status=SupportStatus.SUPPORTED,
                summary="Inspects annotations in tool list",
                fallback_behavior="Treats unannotated tools as potentially mutating",
                failure_indicator="Annotations missing in tools/list response",
                required_conditions=[],
            ),
            McpCapability.STRUCTURED_OUTPUTS: CapabilityDetails(
                status=SupportStatus.SUPPORTED,
                summary="Receives structured tool outputs",
                fallback_behavior="Parses JSON payload",
                failure_indicator="ValidationError on output model mismatch",
                required_conditions=[],
            ),
            McpCapability.ELICITATION: CapabilityDetails(
                status=SupportStatus.UNSUPPORTED,
                summary="Cursor client does not declare MCP elicitation capability",
                fallback_behavior="Gated tools park in /ui approval queue",
                failure_indicator="ApprovalPendingError pointing to /ui approval queue",
                required_conditions=[],
            ),
            McpCapability.PROGRESS: CapabilityDetails(
                status=SupportStatus.PARTIAL,
                summary="Supports progress events in background agent tasks",
                fallback_behavior="Indeterminate loading state",
                failure_indicator="No progress emitted when progressToken omitted",
                required_conditions=["Client passes _meta.progressToken"],
            ),
            McpCapability.RESOURCE_SUBSCRIPTIONS: CapabilityDetails(
                status=SupportStatus.PARTIAL,
                summary="Supports resources/read; subscriptions optional",
                fallback_behavior="Explicit resource read",
                failure_indicator="ResourceNotFoundError / ResourceError",
                required_conditions=[],
            ),
        },
    ),
}


def _canonical_client_id(client_name_or_id: str) -> str:
    """Normalize client name or label to standard client_id.

    A roster label resolves through ``config.agent_family`` first, so a
    neutralised label (the public export's ``Desktop_Agent1``) still finds
    its client; free-form names fall back to the substring match below.
    """
    # Client names are matched without case, as the substring fallback is.
    family = config.agent_family(client_name_or_id) or config.agent_family(
        (client_name_or_id or "").lower()
    )
    if family is not None and family.lower() in CLIENT_CAPABILITY_MATRIX:
        return family.lower()
    raw = (client_name_or_id or "").strip().lower()
    if "hermes" in raw:
        return "hermes"
    if "claude" in raw:
        return "claude"
    if "codex" in raw:
        return "codex"
    if "agy" in raw or "antigravity" in raw:
        return "agy"
    if "cursor" in raw:
        return "cursor"
    return raw


def get_client_matrix(
    client_name_or_id: str | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    """Return the client capability matrix as structured dictionaries.

    If client_name_or_id is provided, returns that single client's entry.
    Otherwise returns the complete list of client entries.
    """
    if client_name_or_id:
        cid = _canonical_client_id(client_name_or_id)
        entry = CLIENT_CAPABILITY_MATRIX.get(cid)
        if entry is None:
            raise KeyError(f"unknown client: {client_name_or_id!r}")
        return entry.to_dict()
    return [entry.to_dict() for entry in CLIENT_CAPABILITY_MATRIX.values()]


def check_client_compatibility(
    client_name_or_id: str,
    transport: str = "streamable_http",
    required_capabilities: list[str | McpCapability] | None = None,
) -> dict[str, Any]:
    """Deterministically check client & transport compatibility for requested capabilities.

    Returns a comprehensive diagnostic dictionary. If any requested capability is
    unsupported or missing preconditions, compatible is False and detailed
    diagnostics identify the missing capability instead of silently degrading.
    """
    cid = _canonical_client_id(client_name_or_id)
    entry = CLIENT_CAPABILITY_MATRIX.get(cid)
    if entry is None:
        return {
            "compatible": False,
            "client_id": cid,
            "client_name": client_name_or_id,
            "transport": transport,
            "error": f"Unknown client {client_name_or_id!r}. Supported clients: {sorted(CLIENT_CAPABILITY_MATRIX)}",
            "supported": [],
            "unsupported": [],
            "missing_capabilities": [str(c) for c in (required_capabilities or [])],
            "diagnostics": [
                f"Client {client_name_or_id!r} is not registered in the MCP 2.x capability matrix."
            ],
        }

    # Verify transport
    transport_enum = None
    for t in TransportType:
        if t.value == transport.lower().replace("-", "_"):
            transport_enum = t
            break

    transport_supported = transport_enum in entry.supported_transports if transport_enum else False

    requested_caps: list[McpCapability] = []
    if required_capabilities:
        for rc in required_capabilities:
            cap_val = rc.value if isinstance(rc, McpCapability) else str(rc)
            try:
                requested_caps.append(McpCapability(cap_val))
            except ValueError:
                return {
                    "compatible": False,
                    "client_id": entry.client_id,
                    "client_name": entry.client_name,
                    "transport": transport,
                    "error": f"Unknown MCP capability: {rc!r}",
                    "supported": [],
                    "unsupported": [str(rc)],
                    "missing_capabilities": [str(rc)],
                    "diagnostics": [
                        f"Capability {rc!r} is not a recognized MCP 2.x capability. Allowed: {[c.value for c in McpCapability]}"
                    ],
                }
    else:
        requested_caps = list(McpCapability)

    supported: list[str] = []
    unsupported: list[str] = []
    missing_capabilities: list[str] = []
    diagnostics: list[str] = []

    if not transport_supported:
        diagnostics.append(
            f"Transport {transport!r} is not supported by {entry.client_name}. Supported transports: {[t.value for t in entry.supported_transports]}"
        )

    for cap in requested_caps:
        details = entry.capabilities.get(cap)
        if details is None:
            unsupported.append(cap.value)
            missing_capabilities.append(cap.value)
            diagnostics.append(
                f"Capability {cap.value!r} is not configured for {entry.client_name}."
            )
        elif details.status in (SupportStatus.SUPPORTED, SupportStatus.PARTIAL):
            supported.append(cap.value)
        elif details.status == SupportStatus.CONDITIONAL:
            supported.append(cap.value)
            diagnostics.append(
                f"Capability {cap.value!r} is conditional for {entry.client_name}. Required conditions: {', '.join(details.required_conditions)}"
            )
        else:
            unsupported.append(cap.value)
            missing_capabilities.append(cap.value)
            diagnostics.append(
                f"Capability {cap.value!r} is unsupported by {entry.client_name}. Fallback: {details.fallback_behavior}. Failure indicator: {details.failure_indicator}"
            )

    compatible = transport_supported and len(missing_capabilities) == 0

    return {
        "compatible": compatible,
        "client_id": entry.client_id,
        "client_name": entry.client_name,
        "transport": transport,
        "transport_supported": transport_supported,
        "supported": supported,
        "unsupported": unsupported,
        "missing_capabilities": missing_capabilities,
        "diagnostics": diagnostics,
    }


def render_matrix_markdown() -> str:
    """Generate GitHub-flavored markdown representation of the capability matrix."""
    lines = [
        "# MCP 2.x Client Capability Matrix",
        "",
        "| Client | Transports | Tool Annotations | Structured Outputs | Elicitation | Progress | Resource Subscriptions |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]

    for entry in CLIENT_CAPABILITY_MATRIX.values():
        transports = ", ".join(t.value for t in entry.supported_transports)
        caps = entry.capabilities
        row = (
            f"| **{entry.client_name}** | `{transports}` "
            f"| {caps[McpCapability.TOOL_ANNOTATIONS].status.value} "
            f"| {caps[McpCapability.STRUCTURED_OUTPUTS].status.value} "
            f"| {caps[McpCapability.ELICITATION].status.value} "
            f"| {caps[McpCapability.PROGRESS].status.value} "
            f"| {caps[McpCapability.RESOURCE_SUBSCRIPTIONS].status.value} |"
        )
        lines.append(row)

    lines.append("")
    lines.append("## Capability Key & Failure Diagnostics")
    lines.append("")
    lines.append("- **Tool Annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`)**:")
    lines.append(
        "  - *Supported*: Clients consume tool risk hints to automate confirmation or safe execution."
    )
    lines.append(
        "  - *Failure mode*: Unannotated tools default to standard prompt dialogs without silent auto-execution."
    )
    lines.append("- **Structured Outputs (`output_model`)**:")
    lines.append(
        "  - *Supported*: Tools return validated Pydantic models; validation failures raise `ValidationError` identifying missing/invalid fields."
    )
    lines.append("- **Elicitation (`elicitation`, `InputRequiredResult`)**:")
    lines.append(
        "  - *Conditional/Supported*: Enabled via `UA_ELICITATION_APPROVALS=1` with operator profile authority."
    )
    lines.append(
        "  - *Unsupported/Missing*: Gated tool calls park in the `/ui` approval queue and raise `ApprovalPendingError` with approval ID."
    )
    lines.append("- **Progress Notifications (`report_progress`)**:")
    lines.append("  - *Supported*: Milestones emitted when `_meta.progressToken` is provided.")
    lines.append(
        "  - *Unsupported*: `mcp_progress.report_progress` safely no-ops without failing tool execution."
    )
    lines.append("- **Resource Subscriptions (`resources/subscribe`, live state)**:")
    lines.append(
        "  - *Supported*: Live updates emitted for `unified-agent://board`, `tasks/{id}`, `runs/{id}`."
    )
    lines.append(
        "  - *Failure mode*: Accessing unauthorized resources raises `ResourceError`; unknown URIs raise `ResourceNotFoundError`."
    )
    return "\n".join(lines)
