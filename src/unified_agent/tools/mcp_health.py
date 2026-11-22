"""MCP tool exposing runtime health, version-skew, and reconnect telemetry (UA #866)."""

from __future__ import annotations

from typing import Any

from .. import mcp_runtime_telemetry


def mcp_health_telemetry() -> dict[str, Any]:
    """Return runtime MCP 2.x health, protocol negotiation, capability set, and reconnect metrics.

    Read-only diagnostic distinguishing healthy, degraded, and incompatible states.
    Sanitized (no secrets/tokens), bounded, and directly actionable.

    Includes ``schema_fingerprint`` (task #1169): a short hash of every tool
    this session can call, including names ``UA_TOOLSETS`` hides from
    ``tools/list``. Clients capture it at session start and re-check before
    writes; a mismatch means a callable schema changed under a long-lived
    connection — re-list or reconnect. ``manifest_schema_fingerprint`` hashes
    only the listed tools, so a client comparing ``tools/list`` does not
    report drift from the filter itself. ``registry_schema_fingerprint``
    hashes the full registry and ignores both the profile and the toolset.

    Version 2 ``semantic_schema_fingerprint`` adds the advertised inputSchema
    contract (types, enums, defaults and nested constraints). The manifest and
    registry semantic hashes use the same surfaces as their v1 counterparts.
    Known presentation keywords and unordered keyword lists are normalized;
    opaque references preserve the raw advertised schema. References are never
    resolved. Existing v1 fields are unchanged.
    """
    from .. import tool_schema_fingerprint
    from ..server import (
        REGISTERED_TOOLS,
        _schema_payloads,
        callable_tool_names,
        client_schema_fingerprint,
        manifest_schema_fingerprint,
        manifest_tool_names,
        registry_schema_fingerprint,
    )

    snapshot = mcp_runtime_telemetry.get_telemetry_snapshot()
    snapshot["schema_fingerprint"] = client_schema_fingerprint()
    snapshot["manifest_schema_fingerprint"] = manifest_schema_fingerprint()
    snapshot["registry_schema_fingerprint"] = registry_schema_fingerprint()
    snapshot["semantic_schema_fingerprint_version"] = (
        tool_schema_fingerprint.SEMANTIC_SCHEMA_FINGERPRINT_VERSION
    )
    for field, names in (
        ("semantic_schema_fingerprint", callable_tool_names()),
        ("manifest_semantic_schema_fingerprint", manifest_tool_names()),
        ("registry_semantic_schema_fingerprint", frozenset(REGISTERED_TOOLS)),
    ):
        snapshot[field] = tool_schema_fingerprint.semantic_fingerprint_from_tool_list(
            _schema_payloads(names)
        )
    return snapshot
