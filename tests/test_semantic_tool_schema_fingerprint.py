"""Versioned advertised input-schema contract fingerprints (#1956)."""

from __future__ import annotations

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from mcp.client import Client

from unified_agent import config, profiles, server
from unified_agent import tool_schema_fingerprint as tsf
from unified_agent.tools.mcp_health import mcp_health_telemetry


def _payload(schema, name="probe"):
    return {"name": name, "inputSchema": schema}


def _fingerprint(schema):
    return tsf.semantic_fingerprint_from_tool_list([_payload(schema)])


@pytest.mark.parametrize(
    "before,after",
    [
        ({"type": "string"}, {"type": "integer"}),
        ({"enum": ["a", "b"]}, {"enum": ["a", "c"]}),
        ({"default": "append"}, {"default": "replace"}),
        ({"const": 1}, {"const": 2}),
        ({"type": "integer", "minimum": 0}, {"type": "integer", "minimum": 1}),
        ({"type": "string", "pattern": "a"}, {"type": "string", "pattern": "b"}),
        ({"format": "email"}, {"format": "hostname"}),
        ({"additionalProperties": True}, {"additionalProperties": False}),
        ({"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}),
        ({"x-contract": {"title": "first"}}, {"x-contract": {"title": "second"}}),
        (True, False),
    ],
)
def test_nested_contract_changes_affect_v2_without_changing_v1(before, after):
    left = {"type": "object", "properties": {"arg": before}}
    right = {"type": "object", "properties": {"arg": after}}
    assert tsf.fingerprint_from_tool_list([_payload(left)]) == tsf.fingerprint_from_tool_list(
        [_payload(right)]
    )
    assert _fingerprint(left) != _fingerprint(right)


@pytest.mark.parametrize("keyword", ["title", "description", "$comment", "examples"])
def test_schema_presentation_is_ignored_at_root_and_nested_nodes(keyword):
    original = {
        "type": "object",
        "properties": {"arg": {"type": "array", "items": {"type": "string"}}},
    }
    changed = deepcopy(original)
    changed[keyword] = ["example"] if keyword == "examples" else "new prose"
    changed["properties"]["arg"]["items"][keyword] = changed[keyword]
    assert _fingerprint(original) == _fingerprint(changed)


@pytest.mark.parametrize("keyword", ["properties", "$defs", "definitions", "patternProperties"])
@pytest.mark.parametrize("identifier", ["title", "description"])
def test_schema_map_identifiers_are_never_stripped(keyword, identifier):
    original = {keyword: {identifier: {"type": "string", "description": "old"}}}
    prose_change = deepcopy(original)
    prose_change[keyword][identifier]["description"] = "new"
    type_change = deepcopy(original)
    type_change[keyword][identifier]["type"] = "integer"
    assert _fingerprint(original) == _fingerprint(prose_change)
    assert _fingerprint(original) != _fingerprint(type_change)
    assert _fingerprint(original) != _fingerprint({keyword: {}})


@pytest.mark.parametrize("keyword", ["default", "const", "enum"])
def test_literal_data_preserves_annotation_named_keys_and_array_order(keyword):
    literal = {"title": "first", "description": "data", "required": ["b", "a"]}

    def schema(value):
        return {keyword: [value] if keyword == "enum" else value}

    changed = deepcopy(literal)
    changed["title"] = "second"
    assert _fingerprint(schema(literal)) != _fingerprint(schema(changed))
    changed = deepcopy(literal)
    changed["required"].reverse()
    assert _fingerprint(schema(literal)) != _fingerprint(schema(changed))
    assert _fingerprint(schema(literal)) == _fingerprint(schema(dict(reversed(literal.items()))))


@pytest.mark.parametrize(
    "keyword,values",
    [
        ("required", ["title", "description"]),
        ("type", ["string", "null"]),
        ("enum", [{"title": "data"}, [2, 1], "literal"]),
        ("allOf", [{"minimum": 0}, {"maximum": 10}]),
        ("anyOf", [{"type": "string"}, {"type": "integer"}]),
        ("oneOf", [{"const": 1}, {"const": 2}]),
    ],
)
def test_unordered_keyword_lists_are_normalized(keyword, values):
    assert _fingerprint({keyword: values}) == _fingerprint({keyword: list(reversed(values))})


@pytest.mark.parametrize("keyword", ["dependentRequired", "dependencies"])
def test_dependency_property_names_and_required_sets_are_normalized(keyword):
    assert _fingerprint({keyword: {"description": ["b", "a"]}}) == _fingerprint(
        {keyword: {"description": ["a", "b"]}}
    )
    assert _fingerprint({keyword: {"title": ["a"]}}) != _fingerprint({keyword: {}})


@pytest.mark.parametrize("keyword", ["prefixItems", "items"])
def test_positional_schema_lists_keep_order(keyword):
    items = [{"type": "string"}, {"type": "integer"}]
    assert _fingerprint({keyword: items}) != _fingerprint({keyword: list(reversed(items))})


@pytest.mark.parametrize("reference", ["#/anyOf/0", "#%2FanyOf%2F0", "external.json#/anyOf/0"])
@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_position_sensitive_references_preserve_branch_order(keyword, reference):
    schema = {keyword: reference, "anyOf": [{"const": 1}, {"const": 2}]}
    changed = deepcopy(schema)
    changed["anyOf"].reverse()
    assert _fingerprint(schema) != _fingerprint(changed)


def test_named_definition_references_still_normalize_unordered_branches():
    schema = {
        "$defs": {"title": {"type": "string"}, "description": {"type": "integer"}},
        "anyOf": [{"$ref": "#/$defs/title"}, {"$ref": "#/$defs/description"}],
    }
    changed = deepcopy(schema)
    changed["anyOf"].reverse()
    assert _fingerprint(schema) == _fingerprint(changed)


def test_referenced_examples_are_a_semantic_contract_not_discardable_prose():
    before = {"$ref": "#/examples/0", "examples": [{"type": "string"}]}
    after = {"$ref": "#/examples/0", "examples": [{"type": "integer"}]}
    Draft202012Validator.check_schema(before)
    Draft202012Validator.check_schema(after)
    assert Draft202012Validator(before).is_valid("hi")
    assert not Draft202012Validator(after).is_valid("hi")
    assert _fingerprint(before) != _fingerprint(after)


@pytest.mark.parametrize("keyword", ["title", "description", "$comment", "examples"])
def test_opaque_annotation_reference_keeps_entire_advertised_schema(keyword):
    target = {"type": "string"}
    value = [target] if keyword == "examples" else target
    pointer = f"#/{keyword}/0" if keyword == "examples" else f"#/{keyword}"
    before = {"$ref": pointer, keyword: value}
    after = deepcopy(before)
    if keyword == "examples":
        after[keyword][0]["type"] = "integer"
    else:
        after[keyword]["type"] = "integer"
    assert _fingerprint(before) != _fingerprint(after)


@pytest.mark.parametrize(
    "reference",
    [
        "#/$defs/outer~1scope~0name/examples/0",
        "#/$defs/outer%7E1scope%7E0name/examples/0",
    ],
)
def test_nested_encoded_annotation_target_drift_is_detected(reference):
    before = {
        "$ref": reference,
        "$defs": {"outer/scope~name": {"examples": [{"type": "string"}]}},
    }
    after = deepcopy(before)
    after["$defs"]["outer/scope~name"]["examples"][0]["type"] = "integer"
    assert Draft202012Validator(before).is_valid("hi")
    assert not Draft202012Validator(after).is_valid("hi")
    assert _fingerprint(before) != _fingerprint(after)


@pytest.mark.parametrize("keyword", ["default", "const", "enum", "x-custom-data"])
def test_literal_or_unknown_reference_target_keeps_raw_schema(keyword):
    target = {"type": "string"}
    value = [target] if keyword == "enum" else target
    pointer = f"#/{keyword}/0" if keyword == "enum" else f"#/{keyword}"
    before = {"$ref": pointer, keyword: value, "description": "first"}
    after = deepcopy(before)
    after["description"] = "second"
    assert _fingerprint(before) != _fingerprint(after)


@pytest.mark.parametrize("reference_keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
@pytest.mark.parametrize("reference", ["#opaque", "external.json#opaque", "#/unrecognized"])
def test_nonordinary_references_preserve_potential_annotation_targets(reference_keyword, reference):
    before = {
        reference_keyword: reference,
        "examples": [{"$anchor": "opaque", "type": "string"}],
    }
    after = deepcopy(before)
    after["examples"][0]["type"] = "integer"
    assert _fingerprint(before) != _fingerprint(after)


def test_scope_identifiers_with_references_preserve_raw_contract():
    before = {
        "$ref": "#/$defs/child",
        "$defs": {"child": {"$id": "child.json", "examples": [{"type": "string"}]}},
    }
    after = deepcopy(before)
    after["$defs"]["child"]["examples"][0]["type"] = "integer"
    assert _fingerprint(before) != _fingerprint(after)


def test_input_schema_alias_and_boolean_root_contracts():
    assert tsf.semantic_fingerprint_from_tool_list([{"name": "probe", "input_schema": False}]) == (
        _fingerprint(False)
    )
    assert _fingerprint(False) != _fingerprint({})
    assert _fingerprint(True) != _fingerprint(False)


@pytest.mark.parametrize(
    "payloads",
    [
        [{"name": "probe"}],
        [{"name": "probe", "inputSchema": []}],
        [{"name": "", "inputSchema": {}}],
        [_payload({}), _payload({})],
    ],
)
def test_invalid_advertised_surfaces_are_not_silently_hashed(payloads):
    with pytest.raises(ValueError):
        tsf.semantic_fingerprint_from_tool_list(payloads)


def test_tool_and_property_order_are_normalized_without_mutating_payload():
    tools = [
        _payload({"properties": {"b": {"type": "integer"}, "a": {"type": "string"}}}, "z"),
        _payload({"properties": {"title": {"type": "string"}}}, "a"),
    ]
    saved = deepcopy(tools)
    reordered = deepcopy(list(reversed(tools)))
    reordered[1]["inputSchema"]["properties"] = dict(
        reversed(reordered[1]["inputSchema"]["properties"].items())
    )
    assert tsf.semantic_fingerprint_from_tool_list(
        tools
    ) == tsf.semantic_fingerprint_from_tool_list(reordered)
    assert tools == saved


@pytest.mark.parametrize("selection", [None, "core", "core:ops"])
@pytest.mark.parametrize("authenticated", [False, True])
async def test_health_v2_matches_real_list_tools_and_preserves_surface_filters(
    monkeypatch, selection, authenticated
):
    if selection is None:
        monkeypatch.delenv("UA_TOOLSETS", raising=False)
    else:
        monkeypatch.setenv("UA_TOOLSETS", selection)
    monkeypatch.setenv("UA_ENFORCE_AUTH", "1" if authenticated else "0")
    identity = config.agent_identity_var.set("Desktop_Agent2")
    session_key = "semantic-schema-fingerprint-session"
    session = config.mcp_session_id_var.set(session_key)
    try:
        async with Client(server.mcp) as client:
            listed = (await client.session.list_tools()).tools
        payloads = [tool.model_dump(by_alias=True) for tool in listed]
        health = mcp_health_telemetry()
        assert health["semantic_schema_fingerprint_version"] == 2
        assert health[
            "manifest_semantic_schema_fingerprint"
        ] == tsf.semantic_fingerprint_from_tool_list(payloads)
        assert health["semantic_schema_fingerprint"] == tsf.semantic_fingerprint_from_tool_list(
            server._schema_payloads(server.callable_tool_names())
        )
        assert health[
            "registry_semantic_schema_fingerprint"
        ] == tsf.semantic_fingerprint_from_tool_list(
            server._schema_payloads(frozenset(server.REGISTERED_TOOLS))
        )
        assert health["schema_fingerprint"] == server.client_schema_fingerprint()
        assert health["manifest_schema_fingerprint"] == server.manifest_schema_fingerprint()
        assert health["registry_schema_fingerprint"] == server.registry_schema_fingerprint()
        assert len(health["semantic_schema_fingerprint"]) == 64
        assert {tool.name for tool in listed} == server.manifest_tool_names()
        if authenticated:
            assert "git_merge_task" not in server.callable_tool_names()
        if selection:
            assert "vault_write" not in server.manifest_tool_names()
            assert "vault_write" in server.callable_tool_names()
    finally:
        config.mcp_session_id_var.reset(session)
        config.agent_identity_var.reset(identity)
        with profiles._lock:
            profiles._active.pop(session_key, None)


def test_hidden_callable_type_drift_changes_callable_and_registry_but_not_manifest(monkeypatch):
    monkeypatch.setenv("UA_TOOLSETS", "core:ops")
    assert "vault_write" not in server.manifest_tool_names()
    assert "vault_write" in server.callable_tool_names()
    before = mcp_health_telemetry()
    info = next(
        tool for tool in server.mcp._tool_manager.list_tools() if tool.name == "vault_write"
    )
    updated = deepcopy(info.parameters)
    updated["properties"]["content"]["type"] = "integer"
    monkeypatch.setattr(info, "parameters", updated)
    after = mcp_health_telemetry()
    assert after["semantic_schema_fingerprint"] != before["semantic_schema_fingerprint"]
    assert (
        after["registry_semantic_schema_fingerprint"]
        != before["registry_semantic_schema_fingerprint"]
    )
    assert (
        after["manifest_semantic_schema_fingerprint"]
        == before["manifest_semantic_schema_fingerprint"]
    )
    assert after["schema_fingerprint"] == before["schema_fingerprint"]
    assert after["manifest_schema_fingerprint"] == before["manifest_schema_fingerprint"]
    assert after["registry_schema_fingerprint"] == before["registry_schema_fingerprint"]
