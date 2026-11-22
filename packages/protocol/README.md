# `@unified-agent/protocol`

Cross-language contract for Unified Agent (ADR: TypeScript Platform Strategy).
**v0.2.1** keeps Phase 1 shapes, v0.2 Task provenance fields, and the #929
queue/review fields (`lineage`, compact markers, `untrusted`).

**Invariant:** this package defines shapes only. Policy, MCP, and execution stay in Python until a later conformance suite proves parity.

## Layout

| Path | Role |
|------|------|
| `src/schemas.ts` | Zod source of truth |
| `generated/json-schema/` | Exported Draft-07 JSON Schema |
| `generated/python/` | Pydantic v2 models from that JSON Schema |
| `scripts/` | `export-json-schema.ts`, `generate-pydantic.sh` |

## Commands

```bash
cd packages/protocol
npm install
npm test                 # node:test via tsx
npm run test:python      # validate the same fixtures with generated Pydantic v2 models
npm run export:json-schema
npm run generate:pydantic   # needs `uv` (uvx datamodel-code-generator)
npm run codegen             # both steps
npm run codegen:check       # regenerate and fail if committed artifacts drift
npm run build               # emit dist/ for TS consumers
```

## Schema catalog (ADR Phase 1 + v0.2 Task fields)

`Task`, `Tool`, `ToolInvocation`, `Capability`, `Approval`, `ApprovalToken`,
`Principal`, `Profile`, `PolicyDecision`, `Execution`, `Event`, `Artifact`,
`Agent`, `MemoryRecord`, `DispatchRequest`, `WorktreePolicy`, `PathResolutionResult`.

## Downstream

`@unified-agent/client` (`packages/client`) is this repository's typed hub client,
not the SDK for outside applications — that one lives in
[`anonb3ll/ua-rust`](https://github.com/anonb3ll/ua-rust) under `sdk/typescript`.
The client depends on this package for Zod shapes and calls the Python hub over MCP
streamable-HTTP — including mutations. It does not reimplement policy.

## Release notes — next (unreleased)

- `Approval` keeps an optional nullable `run_id` from `approval_pending`. A
  string is preserved, `null` stays `null`, and a row that omits the key still
  parses.

## Release notes — 0.2.1

- Generated Python models preserve their public `Enum` classes and enum-valued
  fields. `TaskUpdateRequest.priority` remains unset when omitted, while
  `TaskCreateRequest.priority` uses the schema-owned `Priority.med` default.
- `Task` models queue/review fields (`lineage`, `notes_chars`,
  `waiting_for_review`, `laptop_lane`, `notes_archived`, `untrusted`). Unknown
  hub keys stay strip-mode on purpose (Zod 4 `$loose` widens `status` to
  `any`); extend the schema when the hub adds a stable field (#929).
- `Approval` accepts optional pass TTL fields from `approval_pending`.
- Codegen pins: Zod `4.6.2`, `datamodel-code-generator==0.30.2`, `ruff==0.15.21`
  (`ruff format --isolated --line-length=100`, matching hub `[tool.ruff]`) so
  `npm run codegen:check` is reproducible in both the standalone SDK and the
  hub mirror, and mirrored `models.py` also passes UA `ruff format --check`.

## Release notes — 0.2.0

- `Task` accepts optional hub fields: `domain`, `lane`, `due_at`, `evidence_ref`,
  `source_host`, `archived_at` (all nullable/optional; existing fixtures remain valid).
