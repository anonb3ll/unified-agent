# Platform object model

**Task:** #1070 (slice 1 of #1069 — the platform control plane).
**Status:** the seam is in place; all fifteen declared object types are served.

## The problem this solves

Unified-Agent grew as an MCP tool server, so every surface was keyed on **tool
names**: the MCP manifest registers tools, `webui/api.py` dispatches by tool name
through `server.REGISTERED_TOOLS`, `profiles.py` is an allow/deny list of tool
names, `edition.py` gates feature identifiers, and the TypeScript SDK is a typed
tool caller.

The result was that the domain — tasks, runs, plans, approvals, delegations,
agents, sessions, memory notes, evidence events — existed **only as tool calls**.
Each consumer re-derived it, and the artifacts worth selling (the approval gate,
the tamper-evident evidence chain) could not be read by anyone who was not an
agent client.

`src/unified_agent/object_model.py` inverts that. Object types are declared once,
and everything else is derived.

```
ObjectType                     ->  MCP resource registration   (mcp_live_state)
(declared once, in              ->  profile visibility gate     (mcp_live_state)
 object_model.OBJECT_TYPES)     ->  subscription telemetry      (mcp_live_state)
                                ->  platform manifest           (object_model.manifest())
```

MCP is now one adapter over the object model, not the definition of it.

## What is served today

All fifteen declared object types are served. The first three are the resources
task #823 shipped — their URIs, names, titles, and descriptions are
byte-identical to before, now derived rather than hand-written (a test pins
that parity). Task #1072 wired the rest: approval and evidence_event first
(the auditor-facing pair), then plan, delegation, agent, session, and
memory_note. Later slices added agent tokens, work products, and checked-in
organization units.

| Kind | URI | Profile gate | Reader | Writers |
|---|---|---|---|---|
| `board` | `unified-agent://board` | `task_list` | `mcp_live_state.read_board` | — |
| `task` | `unified-agent://tasks/{task_id}` | `task_list` | `mcp_live_state.read_task` | `add`, `update`, `claim` |
| `run` | `unified-agent://runs/{run_id}` | `dispatch_report_self` | `mcp_live_state.read_run` | — |
| `approval` | `unified-agent://approvals/{approval_id}` | `approval_pending` | `platform_readers.read_approval` | `request`, `resolve` |
| `evidence_event` | `unified-agent://evidence/{event_ulid}` | `event_log_query` | `platform_readers.read_evidence_event` | `append` |
| `plan` | `unified-agent://plans/{plan_id}` | `plan_get` | `platform_readers.read_plan` | — |
| `delegation` | `unified-agent://delegations/{delegation_id}` | `delegate_list` | `platform_readers.read_delegation` | — |
| `agent` | `unified-agent://agents/{agent_label}` | `agent_list` | `platform_readers.read_agent` | — |
| `session` | `unified-agent://sessions/{session_id}` | `session_list` | `platform_readers.read_session` | — |
| `memory_note` | `unified-agent://memory/{memory_id}` | `memory_get` | `platform_readers.read_memory_note` | — |
| `agent_token` | `unified-agent://agent-tokens/{token_id}` | `agent_token_list` | `platform_readers.read_agent_token` | `issue`, `rotate` |
| `work_product` | `unified-agent://work-products/{work_product_id}` | `work_product_submit` | `platform_readers.read_work_product` | `submit` |
| `org_unit` | `unified-agent://org-units/{org_unit_id}` | `task_list` | `platform_readers.read_org_unit` | — |
| `goal` | `unified-agent://goals/{goal_id}` | `task_list` | `platform_readers.read_goal` | `add`, `link` |
| `comment` | `unified-agent://comments/{comment_id}` | `task_list` | `platform_readers.read_comment` | `append` |
| `org_proposal` | `unified-agent://org-proposals/{proposal_id}` | `task_list` | `platform_readers.read_org_proposal` | `propose`, `review`, `apply`, `reject` |

Every reader is read-only, returns the object's stored row or checked-in charter
projection (or the established
not-found error for a missing/malformed id), and enforces the same profile gate
its declaration advertises — a denied profile can neither list nor read the
resource.

Mutable types also declare their write actions in
`ObjectType.writers`. Adapters resolve those actions through
`ObjectType.writer(action)` so task, approval, and evidence mutations share one
discoverable seam without changing existing tool signatures. The Web UI and
Slack/ts-worker evidence paths use these declared writers.

## Adding a served object

1. Add an `ObjectType` to `OBJECT_TYPES` in `src/unified_agent/object_model.py`.
   Transcribe `lifecycle` from the backing table's SQL `CHECK` constraint when
   one exists; a checked-in configuration object has no lifecycle.
2. Write the reader in the module that already knows how to read the object, and
   set `reader_path="module:function"` with `served=True`.
3. Run `pytest tests/test_object_model.py`. The registry invariants, the
   manifest, and the registration derivation all update themselves.

Update the agent-facing resources table in `docs/tools-reference.md` and the
served-kind test pin. For objects outside SQLite, add a revision snapshot so
subscriptions detect changes to their source. Registration and profile gating
still derive from the declaration.

## Design rules

1. **Declaration only.** `object_model` imports no MCP, no profiles, and no tool
   code. Readers resolve lazily by dotted path, so importing the registry can
   never import the tool layer.
2. **`served` means a reader exists.** Enforced by `validate_registry()`.
3. **Lifecycles are transcribed, not invented.** Every `lifecycle` tuple is
   copied from a SQL `CHECK` constraint. Where no state machine exists, the tuple
   is empty and `notes` says why — for example `agent` has no lifecycle because
   `registry.derive_status()` computes liveness from `last_seen` at read time, so
   a stored status column would read "online" forever exactly when it matters.
4. **No `exec`.** The derived MCP handler installs its parameter name through
   `__signature__` and `__annotations__` rather than generating a function with
   `exec`.

## The manifest

`object_model.manifest()` returns the whole model as plain JSON: kinds, URIs,
schemas, lifecycles, terminal states, event kinds, profile gates, backing tables,
and served flags, under a `schema_version`.

This is the artifact a read-only HTTP/JSON surface (task #1071), the SDK, or the
capability catalog should consume. It is why MCP can become one connection among
several instead of the only way to see the domain.

## The read-only HTTP/JSON transport (task #1071)

`src/unified_agent/platform_http.py` is the HTTP adapter over the registry,
mounted under `/api/platform/*` in the web control center:

| Route | Returns |
|---|---|
| `GET /api/platform/manifest` | `object_model.manifest()` — byte-identical, asserted by test |
| `GET /api/platform/objects/{kind}` | a collection read (`board`) |
| `GET /api/platform/objects/{kind}/{id}` | one object by registry kind + id |

The reads resolve the *same* reader the MCP resource path resolves for the same
id, so the payloads cannot drift. Unknown kind / unserved kind / unknown id are
4xx; a profile denied the object's declared `profile_tool` gets 403 — same gate
as MCP. Because the webui has no MCP session id (which would deactivate the
ambient profile machinery entirely), the adapter enforces the gate explicitly
via `profiles.tool_is_allowed_for(profile, tool)` on the identity-default
profile that `profiles.current()` resolves for an HTTP request; the reader's own
`deny()` remains the platform layer's standing guard for any route that reaches
it. This is what keeps the HTTP plane from being a permission bypass.

## Verifying it

```bash
PYTHONPATH=src pytest tests/test_object_model.py tests/test_mcp_live_state.py -q
```

`tests/test_object_model.py` holds the load-bearing checks:

- **Visibility parity** — the derived gate is compared against the exact
  hand-written rule it replaced, across permission states and real profiles, so
  the refactor cannot silently widen or narrow what a client sees.
- **Registration derivation** — the live MCP server's templates and resources are
  asserted to equal `served_types()`.
- **Validator bite** — deliberately broken declarations must produce findings. A
  validator that never fires is theatre.
- **Manifest cleanliness** — the manifest round-trips through JSON and contains
  no callables.
