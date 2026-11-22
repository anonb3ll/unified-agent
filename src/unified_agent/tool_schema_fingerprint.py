"""Tool-schema surface fingerprint and client-side drift detection (task #1169).

Long-lived MCP clients cache the tool list from connection time. After a hub
deploy the server may advertise a different argument surface while the client
still strips unknown kwargs — the #1165 vault append-clobber class of bugs.

``compute_schema_fingerprint`` hashes the per-tool name + argument keys
(required vs optional). Clients capture it at session start (via
``mcp_health_telemetry``) and re-check before writes; a mismatch means
re-list (or reconnect) before further tool calls.

Task #1956 adds a separate versioned fingerprint of the advertised inputSchema
contract, preserving v1 behavior. This canonicalizes known schema locations;
it does not resolve references or claim complete JSON Schema equivalence.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import unquote

logger = logging.getLogger(__name__)

_FP_HEX_LEN = 16

SEMANTIC_SCHEMA_FINGERPRINT_VERSION = 2
_PRESENTATION_KEYWORDS = frozenset({"title", "description", "$comment", "examples"})
_SCHEMA_MAP_KEYWORDS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
_SINGLE_SCHEMA_KEYWORDS = frozenset(
    {
        "items",
        "additionalItems",
        "contains",
        "additionalProperties",
        "propertyNames",
        "unevaluatedItems",
        "unevaluatedProperties",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    }
)
_UNORDERED_SCHEMA_LISTS = frozenset({"allOf", "anyOf", "oneOf"})
_UNORDERED_VALUE_LISTS = frozenset({"required", "type", "enum"})
_POSITIONAL_SCHEMA_LISTS = frozenset({"prefixItems", "items"})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _literal_json(value: Any) -> Any:
    """Copy JSON data without interpreting keys as schema keywords."""
    if isinstance(value, Mapping):
        return {key: _literal_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_literal_json(item) for item in value]
    return value


def _contains_scope_identifier(value: Any) -> bool:
    if isinstance(value, Mapping):
        return "$id" in value or any(_contains_scope_identifier(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_scope_identifier(item) for item in value)
    return False


def _known_schema_pointer(root: Any, reference: str) -> bool:
    """Recognize only local pointers through known, non-positional schema locations.

    This classifies the advertised path without following references. Anything
    else (including annotation/data targets and branch-array indices) is opaque.
    Map identifiers are consumed as names, so properties/title is not annotation
    title. Percent and JSON-Pointer tilde escapes are decoded separately.
    """
    if not reference.startswith("#"):
        return False
    fragment = unquote(reference[1:])
    if not fragment:
        return True
    if not fragment.startswith("/"):
        return False
    parts = []
    for part in fragment[1:].split("/"):
        if "~" in part.replace("~0", "").replace("~1", ""):
            return False
        parts.append(part.replace("~1", "/").replace("~0", "~"))
    current = root
    index = 0
    while index < len(parts):
        if not isinstance(current, Mapping):
            return False
        keyword = parts[index]
        current = current.get(keyword)
        index += 1
        if keyword in _SCHEMA_MAP_KEYWORDS or keyword == "dependencies":
            if not isinstance(current, Mapping) or index >= len(parts):
                return False
            current = current.get(parts[index])
            index += 1
        elif keyword not in _SINGLE_SCHEMA_KEYWORDS:
            return False
    return isinstance(current, (Mapping, bool))


def _has_opaque_reference(value: Any, root: Any, has_scope_identifier: bool) -> bool:
    """Retain raw schemas when references make normalization unsafe or uncertain.

    Scan literal/unknown data too; conservative false positives preserve detail.
    Dynamic/recursive refs, anchors, URI refs and $id scope changes are opaque.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"} and isinstance(item, str):
                if key != "$ref" or has_scope_identifier or not _known_schema_pointer(root, item):
                    return True
            if _has_opaque_reference(item, root, has_scope_identifier):
                return True
    elif isinstance(value, list):
        return any(_has_opaque_reference(item, root, has_scope_identifier) for item in value)
    return False


def _canonical_schema(schema: Any) -> Any:
    """Canonicalize known schema locations, preserving literal data and extensions.

    Maps such as properties/$defs hold *identifiers*, not schema keywords.
    default/const/enum members are JSON instances, not nested schemas. Keep
    unrecognized values literal rather than guessing at their vocabulary.
    """
    if not isinstance(schema, Mapping):
        return _literal_json(schema)
    canonical = {}
    for keyword, value in schema.items():
        if keyword in _PRESENTATION_KEYWORDS:
            continue
        if keyword in _SCHEMA_MAP_KEYWORDS and isinstance(value, Mapping):
            canonical[keyword] = {name: _canonical_schema(item) for name, item in value.items()}
        elif keyword in _SINGLE_SCHEMA_KEYWORDS and isinstance(value, (Mapping, bool)):
            canonical[keyword] = _canonical_schema(value)
        elif keyword in _UNORDERED_SCHEMA_LISTS and isinstance(value, list):
            canonical[keyword] = sorted(
                (_canonical_schema(item) for item in value), key=_canonical_json
            )
        elif keyword in _POSITIONAL_SCHEMA_LISTS and isinstance(value, list):
            canonical[keyword] = [_canonical_schema(item) for item in value]
        elif keyword in _UNORDERED_VALUE_LISTS and isinstance(value, list):
            canonical[keyword] = sorted(
                (_literal_json(item) for item in value), key=_canonical_json
            )
        elif keyword in {"dependentRequired", "dependencies"} and isinstance(value, Mapping):
            canonical[keyword] = {
                name: (
                    sorted((_literal_json(item) for item in entry), key=_canonical_json)
                    if isinstance(entry, list)
                    else _canonical_schema(entry)
                    if keyword == "dependencies"
                    else _literal_json(entry)
                )
                for name, entry in value.items()
            }
        else:
            canonical[keyword] = _literal_json(value)
    return canonical


def semantic_schema_surface_from_tool_list(
    tools: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Sorted advertised tool input contracts, with presentation normalized.

    Accepts actual MCP inputSchema (or the Python input_schema alias), including
    boolean schemas. Missing or malformed schemas/names are refused instead of
    silently conflating them with an empty contract. This does not validate the
    schema vocabulary or dereference any $ref.
    """
    surface = []
    names: set[str] = set()
    for tool in tools:
        name = tool.get("name")
        schema = tool.get("inputSchema", tool.get("input_schema"))
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("tool schema fingerprint requires distinct nonempty tool names")
        if not isinstance(schema, (Mapping, bool)):
            raise ValueError("tool schema fingerprint requires an advertised inputSchema")
        names.add(name)
        # Annotation/data locations may become active schema through a $ref;
        # when path/scope is opaque, stripping any subtree could hide drift.
        preserve_raw = _has_opaque_reference(schema, schema, _contains_scope_identifier(schema))
        surface.append(
            {
                "name": name,
                "inputSchema": _literal_json(schema) if preserve_raw else _canonical_schema(schema),
            }
        )
    surface.sort(key=lambda row: row["name"])
    return surface


def semantic_fingerprint_from_tool_list(tools: Sequence[Mapping[str, Any]]) -> str:
    """Full SHA-256 of version 2 advertised input contracts, distinct from v1.

    Only known schema presentation and unordered keyword-list order are
    normalized. Defaults, literal data, positional arrays, references and unknown
    extension keywords remain contract inputs. See docs/tool-schema-fingerprints.md.
    """
    surface = {
        "version": SEMANTIC_SCHEMA_FINGERPRINT_VERSION,
        "tools": semantic_schema_surface_from_tool_list(tools),
    }
    return hashlib.sha256(_canonical_json(surface).encode("utf-8")).hexdigest()


def _unwrap(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Follow functools.wraps / instrumented wrappers to the published signature."""
    seen: set[int] = set()
    current: Any = fn
    while True:
        ident = id(current)
        if ident in seen:
            return current
        seen.add(ident)
        wrapped = getattr(current, "__wrapped__", None)
        if wrapped is None:
            return current
        current = wrapped


def _param_entries(fn: Callable[..., Any]) -> list[dict[str, Any]]:
    sig = inspect.signature(_unwrap(fn))
    entries: list[dict[str, Any]] = []
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            # Bare *args/**kwargs do not appear in the MCP JSON schema surface.
            continue
        if param.annotation is inspect.Parameter.empty and name == "elicit_ctx":
            # Injected by the elicitation boundary; never part of the published schema.
            continue
        entries.append(
            {
                "name": name,
                "required": param.default is inspect.Parameter.empty,
            }
        )
    entries.sort(key=lambda e: e["name"])
    return entries


def schema_surface_from_callables(tools: Mapping[str, Callable[..., Any]]) -> list[dict[str, Any]]:
    """Canonical schema surface: sorted tool names with sorted arg key metadata."""
    surface: list[dict[str, Any]] = []
    for name in sorted(tools):
        surface.append({"name": name, "params": _param_entries(tools[name])})
    return surface


def schema_surface_from_tool_list(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Canonical schema surface from an MCP ``tools/list`` payload."""
    surface: list[dict[str, Any]] = []
    for tool in tools:
        name = str(tool.get("name") or "")
        schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        if not isinstance(schema, Mapping):
            schema = {}
        properties = schema.get("properties") or {}
        if not isinstance(properties, Mapping):
            properties = {}
        required_raw = schema.get("required") or []
        required = (
            {str(r) for r in required_raw}
            if isinstance(required_raw, (list, tuple, set))
            else set()
        )
        params = [
            {"name": key, "required": key in required} for key in sorted(str(k) for k in properties)
        ]
        surface.append({"name": name, "params": params})
    surface.sort(key=lambda row: row["name"])
    return surface


def _hash_surface(surface: list[dict[str, Any]]) -> str:
    blob = json.dumps(surface, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:_FP_HEX_LEN]


def compute_schema_fingerprint(
    tools: Mapping[str, Callable[..., Any]] | None = None,
) -> str:
    """Return a short hex fingerprint of the tool-schema surface.

    When ``tools`` is omitted, uses the live ``server.REGISTERED_TOOLS`` map
    (lazy import to avoid import cycles at module load).
    """
    if tools is None:
        from .server import REGISTERED_TOOLS

        tools = REGISTERED_TOOLS
    return _hash_surface(schema_surface_from_callables(tools))


def fingerprint_from_tool_list(tools: Sequence[Mapping[str, Any]]) -> str:
    """Fingerprint an MCP ``tools/list`` result (name + inputSchema keys)."""
    return _hash_surface(schema_surface_from_tool_list(tools))


def check_schema_drift(session_fingerprint: str, current_fingerprint: str) -> dict[str, Any]:
    """Compare a session-start fingerprint to a freshly read one.

    On mismatch, logs a warning and returns ``action="relist_tools"`` so the
    caller can force ``tools/list`` (or reconnect) before further writes.
    """
    if session_fingerprint == current_fingerprint:
        return {
            "drifted": False,
            "warning": None,
            "action": None,
            "session_fingerprint": session_fingerprint,
            "current_fingerprint": current_fingerprint,
        }
    warning = (
        f"schema fingerprint drifted (session={session_fingerprint} "
        f"current={current_fingerprint}): re-list tools or reconnect before "
        "further write calls"
    )
    logger.warning("%s", warning)
    return {
        "drifted": True,
        "warning": warning,
        "action": "relist_tools",
        "session_fingerprint": session_fingerprint,
        "current_fingerprint": current_fingerprint,
    }


class SchemaDriftGuard:
    """Client-side guard: capture at session start, check before writes.

    Typical pattern (also documented in ``docs/automation.md``)::

        guard = SchemaDriftGuard()
        guard.capture(mcp_health_telemetry()["schema_fingerprint"])
        ...
        result = guard.check_before_write(mcp_health_telemetry()["schema_fingerprint"])
        if result["relist_required"]:
            transport.listTools()  # or reconnect the MCP host
            guard.note_relisted(result["current_fingerprint"])
    """

    def __init__(self) -> None:
        self._session_fingerprint: str | None = None

    @property
    def session_fingerprint(self) -> str | None:
        return self._session_fingerprint

    def capture(self, fingerprint: str) -> None:
        if not fingerprint:
            raise ValueError("schema fingerprint must be a non-empty string")
        self._session_fingerprint = fingerprint

    def check_before_write(self, current_fingerprint: str) -> dict[str, Any]:
        if self._session_fingerprint is None:
            raise RuntimeError("SchemaDriftGuard.capture() required at session start")
        result = check_schema_drift(self._session_fingerprint, current_fingerprint)
        return {
            **result,
            "relist_required": bool(result["drifted"]),
        }

    def note_relisted(self, fingerprint: str) -> None:
        """Advance the session fingerprint after a successful re-list/reconnect."""
        self.capture(fingerprint)
