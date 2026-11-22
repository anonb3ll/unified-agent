"""Client aliases over live discovery, not a second platform command registry."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

from ..hub_client import MAX_REQUEST_BYTES
from .result import CliError, strict_json

# These names only decorate authorized discovery. Platform command versions,
# writer declarations, schemas and gates remain the responsibility of #1434.
ALIASES = {
    "task.list": "task_list",
    "inbox.list": "message_list",
    "approval.list": "approval_pending",
    "evidence.show": "evidence_report",
    "doctor": "system_doctor",
}


# These tools can write vault files despite their read-only discovery hint.
# Require an explicit inline request, including for the manifest's safe default,
# so discovery defaults never silently select a write path for this read client.
_INLINE_EXPORTS = frozenset(
    {"export_tasks", "export_memory", "export_journal", "export_migration_manifest"}
)


def resolve(name: str, catalog: list[dict]) -> dict:
    tool_name = ALIASES.get(name, name)
    tool = next((item for item in catalog if item["name"] == tool_name), None)
    if tool is None:
        raise CliError("unavailable", "Command is not in your hub's authorized catalog.", 5)
    return tool


def readable(tool: dict, data: dict | None = None) -> bool:
    annotations = tool.get("annotations")
    if not isinstance(annotations, dict) or annotations.get("readOnlyHint") is not True:
        return False
    if tool["name"] in _INLINE_EXPORTS:
        return data is not None and data.get("write_to_vault") is False
    return True


def describe(tool: dict) -> dict:
    conditional_input = {"write_to_vault": False} if tool["name"] in _INLINE_EXPORTS else None
    result = {
        **tool,
        "client_alias": next(
            (alias for alias, name in ALIASES.items() if name == tool["name"]), None
        ),
        "read_only": readable(tool),
        "client_invokable": readable(tool, conditional_input),
        "platform_command_version": None,
    }
    if conditional_input is not None and result["client_invokable"]:
        result["read_only_input"] = conditional_input
    return result


def read_bytes(source: str) -> bytes:
    """Read ``-`` (stdin) or a file path, bounded to the request limit."""
    if source == "-":
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    else:
        with Path(source).open("rb") as stream:
            raw = stream.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise CliError("validation", "Input exceeds the 1 MiB client limit.", 2)
    return raw


def load_input(source: str | None) -> dict:
    if source is None:
        return {}
    try:
        if source == "-" or source.startswith("@"):
            raw = read_bytes(source if source == "-" else source[1:])
        else:
            raw = source.encode("utf-8")
        if len(raw) > MAX_REQUEST_BYTES:
            raise CliError("validation", "Input exceeds the 1 MiB client limit.", 2)

        data = strict_json(raw)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise CliError(
            "validation", "Input must be a readable, strict JSON object; use JSON, @file or -.", 2
        ) from None
    if not isinstance(data, dict):
        raise CliError("validation", "Command input must be a JSON object.", 2)
    return data


# Identity follows the hub-verified credential; caller input never names it.
_IDENTITY_KEYS = frozenset({"agent", "actor", "author", "from_agent", "requested_by"})


def write_input(data: dict, flags: dict) -> dict:
    """Merge explicit verb flags into caller input; either may not override the other."""
    if _IDENTITY_KEYS & data.keys():
        raise CliError("validation", "Identity is resolved by the hub; remove it from input.", 2)
    data = dict(data)
    for key, value in flags.items():
        if value is None:
            continue
        if key in data and data[key] != value:
            raise CliError("validation", f"Input {key} conflicts with a command flag.", 2)
        data[key] = value
    return data


def arguments(tool: dict, data: dict, context: dict[str, str | None]) -> dict:
    """Validate ``data``; ``context`` fills only keys the live schema declares."""
    schema = copy.deepcopy(tool["inputSchema"])
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise CliError("invalid_response", "Hub returned an invalid input schema.", 1)
    data = dict(data)
    for key, value in context.items():
        if value is not None and key in properties:
            if key in data and data[key] != value:
                raise CliError("validation", f"Input {key} conflicts with the selected context.", 2)
            data[key] = value
    # Match the hub's extra=forbid discipline even if older discovery omitted it.
    schema["additionalProperties"] = False
    try:
        Draft202012Validator.check_schema(schema)
        # Empty Registry has no remote retriever: schema references never fetch URLs.
        validator = Draft202012Validator(schema, registry=Registry())
        error = next(validator.iter_errors(data), None)
    except (SchemaError, Unresolvable, RecursionError, ValueError):
        raise CliError("invalid_response", "Hub schema cannot be validated locally.", 1) from None
    if error:
        # jsonschema messages echo supplied values; expose only the rule, not the input.
        raise CliError(
            "validation", f"Input violates {error.validator}; inspect capabilities describe.", 2
        )
    return data
