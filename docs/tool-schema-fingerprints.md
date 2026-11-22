# Tool input-schema fingerprints

The existing task #1169 fingerprints remain version 1: 16 hex characters
covering tool names and top-level argument names/requiredness. They continue
to support the existing drift guard. Task #1956 adds a separate version 2
contract fingerprint over the advertised MCP `inputSchema`, so clients can
also detect type, enum, default and nested constraint changes.

## Health fields and comparison

`mcp_health_telemetry` keeps all existing fields and adds:

| Field | Meaning |
|---|---|
| `semantic_schema_fingerprint_version` | `2`, identifying the canonicalization below |
| `semantic_schema_fingerprint` | Every tool the current profile can call, including toolset-hidden names |
| `manifest_semantic_schema_fingerprint` | Only tools the current profile and toolset list |
| `registry_semantic_schema_fingerprint` | Every registered tool's advertised schema, regardless of profile/toolset |

The three new hashes are full SHA-256 hex digests. A version-tagged surface
containing sorted tool names and canonicalized `inputSchema` values is serialized
with sorted object keys and compact JSON separators, then hashed. The health
tool obtains schemas from the same MCP tool manager as `tools/list`; it does
not reconstruct schemas from Python signatures. No raw schema or defaults are
added to the health response.

Clients can reproduce a listed-manifest hash with
`semantic_fingerprint_from_tool_list` in `unified_agent.tool_schema_fingerprint`:

```python
from unified_agent.tool_schema_fingerprint import semantic_fingerprint_from_tool_list

payloads = [tool.model_dump(by_alias=True) for tool in listed_tools]
manifest_hash = semantic_fingerprint_from_tool_list(payloads)
assert manifest_hash == health["manifest_semantic_schema_fingerprint"]
```

Compare the same version and surface with the same profile/toolset context.
`tools/list` cannot reproduce the callable hash when the toolset hides callable
names. The full registry hash is not proof that the client has those permissions.
Changing a profile/toolset legitimately changes its affected hashes. A mismatch
requests a re-list or reconnect before writes; these are read-only diagnostics,
not an automatic denial, permission grant, or refresh. Version 1 comparisons
remain unchanged, and `SchemaDriftGuard` can compare either version's opaque
hash strings if the caller consistently captures the same version/surface.

## Version 2 canonicalization

The contract deliberately preserves more than validation assertions: defaults
affect callers, so `default` is retained even though JSON Schema defines it as
an annotation. See the official [validation vocabulary](https://json-schema.org/draft/2020-12/json-schema-validation#section-9.2)
and [core/applicator vocabulary](https://json-schema.org/draft/2020-12/json-schema-core#section-10).

- Only schema-node `title`, `description`, `$comment` and `examples` are omitted.
  Other annotations and unknown extension keywords are retained conservatively.
- `properties`, `patternProperties`, `$defs`, legacy `definitions` and
  `dependentSchemas` are maps of **names to schemas**. Names such as `title`
  or `description` remain present; only presentation keywords inside their
  schema values are omitted.
- Known individual subschemas are recursively canonicalized: `items`,
  `additionalItems`, `contains`, `additionalProperties`, `propertyNames`,
  `unevaluatedItems`, `unevaluatedProperties`, `not`, `if`, `then`, `else` and
  `contentSchema`. Boolean schemas remain distinct from object schemas.
- `required`, array-valued `type` and `enum` are sorted by canonical JSON;
  `allOf`, `anyOf` and `oneOf` sort canonicalized subschemas. Duplicates are
  retained. `dependentRequired` property-name arrays and legacy `dependencies`
  property-name arrays are likewise sorted; schema-valued legacy dependencies
  are recursively canonicalized.
- Positional `prefixItems` and legacy array-valued `items` preserve array order.
  Literal `default`, `const` and each `enum` member preserve every data key
  and nested array order. Object key order never affects the hash. An object
  default containing `description`, `required` or `$defs` is data, not a schema.

Reference exception: an annotation/data location can become an active schema
when targeted by a reference, for example `#/examples/0`. Sorting a branch array
can also change a target such as `#/anyOf/0`. Version 2 therefore retains the
**entire raw input schema** (all annotations and array order, with object-key
order still ignored) when any reference has an opaque target or scope.

Only fragment-only `$ref` pointers through known non-positional schema locations
are classified as safe to normalize: schema maps, individual schema keywords
and schema-valued legacy dependencies. Property/definition names are treated
as identifiers; `#/$defs/title` and `#/properties/description` still normalize
normally. Pointer percent encodings and `~0`/`~1` escapes are decoded for that
classification. Missing targets, annotation/literal/unknown locations,
branch-array pointers, anchors, URI references, dynamic/recursive references,
or any reference accompanied by a `$id` scope identifier retain the raw schema.
No reference is resolved or fetched.

The conservative scan also covers literal and unknown extension data. False
positives cause extra drift signals rather than hiding a potential target
change. In the raw-schema case, even an unrelated prose change affects the hash.

## Limits

This is a versioned canonical **advertised contract** fingerprint, not a
complete JSON Schema equivalence checker or a validator. It does not resolve,
fetch, inline or rename references. Reference strings, anchors, identifiers,
dialect declarations and bundled definitions are retained. Changing content
behind an unchanged external `$ref` cannot be detected from an unchanged
advertised payload. Equivalent schemas written using different references or
constraint formulations may hash differently. Unknown vocabularies are kept
as literal JSON, including array order and annotation-looking data keys.

Presentation is omitted according to the supported schema locations above;
unrecognized extensions are not assumed to contain subschemas. Output schemas,
tool descriptions and MCP tool annotations are outside this input-contract
hash. It does not detect handler implementation changes or guarantee that
runtime validation, authorization or policy stayed the same. Canonicalization
changes require a new version instead of silently changing version 2.
