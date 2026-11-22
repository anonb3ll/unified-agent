---
title: Review — what still needs architectural rework for the MCP-to-platform move
date: 2026-10-03
author: agent-cloud-agent1
status: review (no task id; requested directly in a cloud session)
tags: [review, architecture, platform, unified-agent]
project: unified-agent
---

# What still needs architectural rework for the MCP-to-platform move

This is a review of the repository at `main` (`5c2ed66`, 2026-10-03) against
the stated direction: an agent-operations platform where MCP is one adapter
among HTTP/JSON, CLI, Web UI and SDK. It answers one question: **which parts
of the code are still shaped by "this is an MCP server", and which of those
need an architectural change rather than more incremental cleanup.**

Every number below was measured on this checkout with the commands listed in
the appendix. Paths are under `src/unified_agent/` unless stated.

## Verdict

The platform move is real at the **declaration** layer and not yet real at
the **enforcement** and **persistence** layers.

What is already platform-shaped, and should not be redone:

- The layer map (`docs/architecture/layers.md`, `layers.toml`,
  `scripts/check_layers.py`) with 9 remaining upward imports and a baseline
  that only shrinks.
- `object_model.py`: 16 object kinds declared once, MCP resources and
  `/api/platform/*` derived from them, with parity tests.
- `change_feed` (domain publishes, MCP subscribes), `table_schemas` and the
  post-init hooks (#1720), `tool_surface` as the registry seam.
- The TypeScript strategy ADR: Python keeps the only policy implementation;
  `packages/protocol` and `packages/conformance` exist to prove parity, not
  to move policy.

What is still MCP-shaped, in order of how much it blocks the platform:

| # | Finding | Evidence | Rework |
|---|---|---|---|
| 1 | Governance is composed inside the MCP adapter, so every non-MCP caller either goes through MCP or re-implements the gate | `server.py:529-745`; `webui/api.py:356-399`; `webui/routes.py:592-657`; `dispatch/`, `executor/` do not use `gateway` at all | A core `invoke` chokepoint that adapters and workers call |
| 2 | The caller is a bundle of context variables, not an object; the profile system is keyed on the MCP session id | `config.py:2428-2550`; `profiles.py:594-625, 857-861`; set from 8 places | A `Principal` built once per request by each adapter |
| 3 | The MCP `ToolError` is the domain exception | 139 modules import it; 1,375 raise sites; 14 domain exceptions subclass it; web layer catches it 44 times | A platform error hierarchy mapped per adapter |
| 4 | Authorization vocabulary is tool names | `profiles.py` frozensets; `policy.py` fnmatch on tool; `route_scopes.py` carries tool names; `object_model.profile_tool` maps kinds back to tools | Capabilities (`object.action`) with the tool list derived |
| 5 | No data-access layer; one 880-line migration; tenant filter copied per module | 64 modules with inline SQL; `tools/tasks.py` has 49 execute calls; `_TENANT_SQL` defined in 3 places; `config.py:2746` opens SQLite itself | Per-object stores in core; numbered migrations |

Items 1 to 4 are one programme and should be sequenced 2, 1, 3, 4. Item 5 is
separate, incremental, and is the real blocker for tenancy (invariant 6b in
the platform-invariants ADR) and for the uv-workspace split.

## 1. Governance lives in the MCP adapter

### What the code does

`server.py` is the composition root. The registration loop
(`server.py:827-1030`) wraps each of about 200 functions with
`tool_surface.register(fn)`, where the installed guard is
`monitor.instrumented(policy_guarded)`. `_policy_guarded_body`
(`server.py:529-730`) applies, in order: payload binding and read-only
classification, `agent_access.enforce`, identity liveness via
`config.agent_identity_var`, the profile ceiling
(`operational_profiles.tool_is_allowed`), trust presets, the edition feature
gate, per-tool special cases (secrets, `routine_fire`, mixed-action managers,
`git_merge_task`), then `approvals.approval_invocation`,
`safety.check_policy_gate` and `approvals.mark_governed_execution`. Outside it,
`gateway.invocation` gives the rate and bind scope and
`gateway.record_call_result` writes the decision event.

That is the "one choke point" `docs/architecture.md` describes. It is reached
only by calling the wrapped callable from `REGISTERED_TOOLS`, which only
exists once `server.py` has been imported. `tool_surface._load()` imports
`server` lazily for that reason (`tool_surface.py:94-97`), and `layers.md`
already names replacing that seam as the next step.

### Who bypasses it or rebuilds it

- **The Web API rebuilds it by hand twice.** `webui/api._registered_platform_write`
  (`api.py:356-399`) re-runs the profile check, `agent_access.enforce`,
  `gateway.invocation`, `safety.check_policy_gate`, `check_approval_gate` and
  `mark_governed_execution` in its own order, then calls
  `object_type.writer(action)` directly. `routes._governed_read`
  (`routes.py:592-657`) does the same for reads and reaches into
  `server._production_operation`. The rest of the web layer dispatches by
  tool name through `api.call_tool` (`api.py:278-294`, about 70 sites), with
  about 41 further direct references to `storage`, `registry` and tool
  modules, and `start_ui_dispatch` calling `dispatch.coordinator` directly.
- **The runtime layer does not use the gate.** `gateway` is imported by none
  of `dispatch/*.py` or `executor/*.py`. `dispatch/coordinator.py` and
  `dispatch/scheduler.py` write through `storage.connect()` directly and
  import the private `tools.approvals._pass_expires_at`
  (`coordinator.py:230`). `routine_cli.py` and `zulip_worker.py` call tool
  functions and storage directly.
- **The executor reaches governance only through MCP over HTTP.**
  `hub_client.HubClient.call_tool` posts `tools/call` to the hub, and
  refuses to fall back on 401/403 (`hub_client.py:271-300`). The in-process
  `_direct_tool_fallback` (`executor/daemon.py:67-99`) calls bare tool
  functions and is opt-in (`UA_EXECUTOR_ALLOW_DIRECT=1`). The default is
  safe, but it shows the shape of the problem: an internal worker must speak
  the MCP wire protocol to get a policy decision, because there is no
  in-process governed entry point.
- Only `ts_worker` and `tui` call the governed registry in-process
  (`ts_worker/worker.py:229-260`, `tui.py:51-63`).

### Why it matters for a platform

Each new adapter (HTTP objects, Slack, mobile, the executor, routines) has
to choose between going through MCP or copying the gate. The copies already
differ: `_registered_platform_write` skips the read-only classification,
trust presets and the edition gate that the MCP wrapper applies; the
dispatcher and routines skip rate limits and decision events entirely. The
"one policy" claim therefore holds for MCP and most of the web, not for the
runtime that carries real work.

The `server.py:1026-1028` comment ("this wrapper only engages for MCP calls")
is stale; the web, workflow, LangGraph and ts_worker paths call the wrapped
callables too.

### Rework

Move the composition out of `server.py` into a core module (working name
`governance` or `invoke`) that exposes one function:

```
invoke(principal, operation, payload, *, read_only=None) -> result
```

It owns the exact order of checks that `_policy_guarded_body` applies today
plus the `gateway` scope and decision recording. `server.py` becomes a thin
adapter: bind the MCP request to a principal, call `invoke`, map the result.
`webui/api.call_tool`, `_registered_platform_write` and `_governed_read`
collapse onto the same call. `dispatch`, `routine_cli` and the executor's
direct path call it in-process. The `tool_surface -> server` seam goes away
because the registry is filled by the core module, not by the MCP adapter.

Guard it with a test that enumerates every import of `tools.*` functions
from outside `tools/` and `invoke`, and fails on any caller not on an
explicit allowlist. That is the same pattern as `layers-baseline.txt`: the
allowlist only shrinks.

## 2. The caller is not an object

### What the code does

Identity is five context variables in `config.py`: `source_host_var` (2428),
`agent_identity_var` (2447), `auth_metadata_var` (2451),
`mcp_session_id_var` (2526) and `peer_device_var` (2535), plus workspace and
episode vars at 75-83. They are set in eight places: the hub ASGI middleware
(`server.py:1551-1564`), the WorkOS leg (`config.py:2493`), five sites in
`webui/api.py` and one in `webui/routes.py`, `slack_gateway.py:271`,
`fleet_actions.py:213` and `executor/daemon.py:69`. Tool code reads them
through `config.verified_agent_identity()` (156 uses, 61 in `tools/`).

The active profile is keyed on the MCP session id: `_active` is a dict of
`ActiveProfile(name, identity, session_id)` (`profiles.py:594-601`) looked up
by `_current_session_id()` (615-625), whose comment reads "mcp 2.x has no
ambient request_ctx". `exposure_is_active()` (857-861) returns false without
an MCP session id, which is why every HTTP path calls `tool_is_allowed_for`
explicitly and why `profile_activate` requires "an authenticated HTTP MCP
session" (604-612). A browser, a CLI or a worker cannot hold an activated
profile at all.

### Rework

Introduce a `Principal` (or `CallContext`) value object in core: identity
label, actor kind (agent, human, device, service), org id, session key,
device, source host, resolved profile, auth metadata. Each adapter builds it
once per request from its own credential (bearer, cookie, WorkOS session,
executor token, CLI label) and binds it in one place. `profiles` keys the
active profile on the transport-neutral session key. `verified_agent_identity()`
stays as a shim that reads the principal, so the 156 call sites do not move
in the same change.

This is the prerequisite for item 1: `invoke` needs a principal argument that
is the same object whichever adapter built it.

## 3. The MCP exception is the domain exception

### What the code does

139 of 306 modules import from `mcp`; 129 of them import only `ToolError` or
`ResourceError` from `mcp.server.mcpserver.exceptions`. `raise ToolError`
appears 1,375 times (701 in `tools/`, 579 in top-level modules, 36 in
`audit/`, 30 in `messaging/`). Fourteen domain exceptions subclass it,
including `policy.PolicyViolation`, `agent_access.AgentAccessDenied`,
`managed_secrets.SecretReadDenied`, `tools.approvals.ApprovalPendingError`,
`operating_policy.CommsPolicyDenied` and `self_organization.ScopeEscapeError`.
The web layer catches it 44 times to turn it into HTTP statuses, and
`webui/routes.py` imports it locally more than 15 times. `platform_http.py`
and `platform_readers.py`, the two modules written to be MCP-independent,
import `ResourceError`.

There is no project base exception. The other 72 exception classes derive
from `ValueError`, `RuntimeError` or `Exception` ad hoc.

Tool argument and return shapes are already neutral: functions take plain
keyword parameters, strictness is applied once by patching the SDK base
model (`server.py:145`), and returns are plain dicts (187 `-> dict`, 43
`-> dict[str, Any]`, 15 `-> str`). Nothing in `tools/` uses MCP content
types. That part needs no rework.

### Rework

Add `unified_agent/errors.py` with `PlatformError` and a small typed set:
`NotFound`, `Denied`, `Invalid`, `ApprovalPending`, `Conflict`, `Unavailable`.
`invoke` (item 1) is the single place that maps them to the adapter's wire
form: MCP `ToolError`, HTTP status plus problem body, CLI exit code.

Migration is mechanical. Step one makes `ToolError` an alias of
`PlatformError` at the import sites (a codemod over the 129 import lines),
so behaviour is unchanged. Step two retypes the 14 subclasses and the
`except ToolError` sites in `webui/`, `executor/` and `dispatch/`. Step three
replaces raw `raise ToolError(...)` with the typed variants where the type
carries information the adapter needs (not found versus denied). Progress
and elicitation stay MCP-only, which is correct, but `mcp_progress` should
be reached through an interface so `tools/pi_runner.py` and
`tools/langgraph.py` stop importing it directly.

## 4. Authorization is keyed on tool names

### What the code does

`profiles.py` is frozensets of tool names (`DEVELOPER_DENIED_TOOLS`,
`STEWARD_DENIED_TOOLS`, `RESEARCH_ALLOWED_TOOLS`, `OPERATOR_ALLOWED_TOOLS`,
`PUBLIC_ALLOWED_TOOLS`, `CLOUD_ASSISTANT_ALLOWED_TOOLS`, the `LEGACY_*` sets
and `DEFAULT_PROFILE_PRUNED_TOOLS`), with action-level sets for the
consolidated managers. `policy.PolicyRule.tool` is matched with `fnmatch`
(`policy.py:553`). `webui/route_scopes.Scope.tools` carries tool names
(`route_scopes.py:104-107`). `object_model.ObjectType.profile_tool` maps
every object kind back to the tool whose visibility it borrows (`task` to
`task_list`, `approval` to `approval_pending`). HTTP writes pass invented
tool-shaped strings such as `"evidence_event.append"` into the policy gate
(`api.py:320, 345`), and `agent_access.enforce` takes either a tool name or
`"platform_object:{kind}"`.

So the platform's permission model is "which MCP tools can this profile
see", and every non-MCP surface has to pretend to be a tool to be governed.
The 100-tool cap and the prune set (`DEFAULT_PROFILE_PRUNED_TOOLS`) are
managing MCP manifest size through the authorization layer.

### Rework

Define the capability vocabulary in `object_model`, which already declares
`writers` per kind: a capability is `<kind>.<action>` (`task.read`,
`task.claim`, `approval.resolve`, `evidence_event.append`, `workspace.run`).
Profiles, policy rules, route scopes and `agent_access` key on capabilities.
Each registered tool declares the capability it exercises (most are one to
one; the `_manage` tools map action to capability), and the MCP tool list a
profile sees is derived from the capabilities it holds, the same way MCP
resources are already derived from object kinds. The profile shrink then
becomes a manifest concern (which tools to list) separate from the
authorization concern (what the caller may do).

Keep the tool-name sets as generated aliases during migration, and pin
parity with a test that compares the derived allow set per profile against
the current frozensets, as `tests/test_object_model.py` does for resource
visibility.

## 5. Persistence has no seam of its own

### What the code does

`storage.py` (4,514 lines) holds 59 real tables as about 25 DDL string
constants interleaved with per-table helpers, a `connect()` that loads
`sqlite_vec` and registers a UDF, and `_migrate()` (about 880 lines,
`storage.py:2825-3700`), one idempotent function that inspects
`PRAGMA table_info` and issues `ALTER TABLE` (68 occurrences) or rebuilds
tables. `SCHEMA_VERSION = 42` is stamped, but version bumps are comments
inside `_migrate`, and there is no down path (`storage.py:3793`).

Sixty-four modules run inline SQL. There is no repository or data-access
layer: `tools/tasks.py` has 49 `execute` calls, and `task_add`
(`tools/tasks.py:1009-1155`) validates, resolves identity, opens a connection
and writes the INSERT in one function. The tenant filter `org_id IS ?` is
defined separately in `tools/tasks.py:149`, `tools/memory.py:85` and
`tools/export.py:538`; `platform_readers.read_memory_note` duplicates it in
raw SQL rather than calling `tools.memory`. `config.py:2746` opens
`memory.db` with `sqlite3.connect` itself, and `evolve`, `sleep_pass`,
`planning_loop` and `tools/dash` open it read-only outside `connect()`. The
docstring at `storage.py:3777` ("No read path filters on it") contradicts the
three modules that do.

### Why it matters

This is not what blocks the adapters, but it is what blocks three stated
goals: tenant partitioning (invariant 6b is deferred because reads cannot be
filtered centrally), a second storage backend or a hosted hub, and the
uv-workspace split (`layers.md`, "can the package be split yet: not yet"),
because `storage` cannot be packaged separately from the tool modules that
hold its queries.

### Rework

Per-object stores in core (`TaskStore`, `MemoryStore`, `ApprovalStore`,
`EventStore`, ...) that own the SQL for their tables, apply the tenant filter
once, and expose typed operations the tool functions and `platform_readers`
both call. Convert object by object, starting with tasks, approvals and
events (the ones the evidence product sells), and leave the long tail of
Zulip and Slack helpers for last. Split `_migrate` into numbered, ordered
steps keyed on `SCHEMA_VERSION` so a bump is code, not a comment. Stay on
SQLite; nothing here argues for changing the engine.

## What does not need architectural rework

- **`config.py` (4,379 lines, 257 functions, 173 `UA_*` names).** Free
  functions that read the environment at call time are slow to test but not
  platform-blocking. Once the identity vars move to `Principal` (item 2) and
  the token lookup at `config.py:3041` moves to a store (item 5), what is
  left is settings. A typed settings object is a later cleanup.
- **Two execution paths (executor B and routines D).** The substrate survey
  and #1435/#1436 already track convergence. Do not fold it into this
  programme; item 1 makes both paths governed without merging them.
- **Separate SQLite files** for LangGraph checkpoints, the pilot auth store
  and the mirror receiver are deliberate isolation. Leave them.
- **The TypeScript SDK being a typed tool caller.** The ADR says the Python
  core leads. After item 4, regenerate the client from the capability
  catalog rather than from tool names; until then it is correct as a mirror.
- **`webui/api.py` (5,982 lines) and `routes.py` (4,322 lines).** They will
  shrink as items 1 and 3 remove the hand-built gates and the 44 `ToolError`
  translations. A separate refactor would be rework twice.
- **The edition seam, the hash chain in `events.py`, `change_feed`,
  `table_schemas`.** Already placed correctly.

## Sequencing and cost

Order: **2, 1, 3, 4** as one programme, **5** alongside it.

- `Principal` first, because `invoke` must take the same caller object from
  every adapter.
- `invoke` second, because it is the one place errors are mapped and
  capabilities are enforced.
- Errors third; the alias step is safe to land early and the typed step
  follows `invoke`.
- Capabilities fourth; they need `invoke` to enforce and the object model to
  declare.
- Stores can start with tasks at any point and never block the others.

Relative size: items 2 and 3 are mechanical and wide (many files, little
logic). Item 1 is narrow and deep (`server.py`, `webui/api.py`, `dispatch/`),
and touches red-tier files (`server.py`, `safety.py`), so each PR escalates
to the operator under the reviewer's `RED_TIER_PATTERNS`. Item 4 is a data-model
change with a parity test. Item 5 is the longest and is best done per object
behind the existing tool signatures.

Test exposure to plan for: 63 test files import `server`, 26 drive an MCP
client end to end, and 299 set `UA_*` variables with `monkeypatch`. Keep
`server.REGISTERED_TOOLS`, `tool_surface.registry()` and
`config.verified_agent_identity()` as re-exports through the migration so
those tests keep running unchanged.

## Found, not fixed

- `server.py:1026-1028`: comment says the wrapper engages only for MCP calls.
  It also engages for the web, workflow, LangGraph and ts_worker paths.
- `storage.py:3777`: docstring says no read path filters on `org_id`.
  `tools/tasks.py`, `tools/memory.py` and `tools/export.py` do.
- `webui/api._registered_platform_write` (`api.py:356-399`) omits the
  read-only classification, trust-preset and edition checks that
  `server._policy_guarded_body` applies to the same operations over MCP.
  Repro: compare the check list at `server.py:546-612` with `api.py:356-399`.
- `dispatch/coordinator.py:230` imports the private
  `tools.approvals._pass_expires_at`.

## Appendix: measurements

Run from the repo root on `5c2ed66`.

```
find src/unified_agent -name '*.py' | wc -l                      # 306
grep -rl "^from mcp\|^import mcp\|^    from mcp" src/unified_agent --include=*.py | wc -l   # 137-139
grep -rn "raise ToolError" src/unified_agent --include=*.py | wc -l      # 1375
grep -rn "raise ToolError" src/unified_agent/tools --include=*.py | wc -l # 701
grep -rn "class .*(ToolError)" src/unified_agent --include=*.py | wc -l   # 14
grep -rlE '\.execute\(\s*f?"(SELECT|INSERT|UPDATE|DELETE)' src/unified_agent --include=*.py | wc -l  # 64
grep -l "gateway\." src/unified_agent/dispatch/*.py src/unified_agent/executor/*.py | wc -l  # 0
grep -rhoE 'UA_[A-Z0-9_]+' src/unified_agent/config.py | sort -u | wc -l # 173
grep -l "import server\|unified_agent.server" tests/*.py | wc -l         # 63
grep -l "call_tool\|ClientSession\|streamablehttp_client" tests/*.py | wc -l  # 26
grep -l 'setenv("UA_' tests/*.py | wc -l                                # 299
```

Sources read in full: `docs/architecture.md`, `docs/platform-object-model.md`,
`docs/architecture/layers.md`, `docs/architecture/platform-invariants-adr.md`,
`docs/architecture/typescript-platform-strategy-adr.md`,
`docs/execution-substrate-survey.md`, `docs/open-core-architecture.md`,
`docs/interfaces.md`, `docs/concepts.md`; and the cited regions of
`server.py`, `tool_surface.py`, `gateway.py`, `profiles.py`, `policy.py`,
`config.py`, `storage.py`, `events.py`, `object_model.py`,
`platform_readers.py`, `platform_http.py`, `webui/api.py`, `webui/routes.py`,
`executor/daemon.py`, `hub_client.py`, `dispatch/coordinator.py`.
