# Developer guide

Use this guide when you change the service. It describes the minimum work
needed to keep a new tool, safety change, or operator feature maintainable.

## Source map

| Need | Main source |
|---|---|
| Register MCP tools and run the service | [`server.py`](../src/unified_agent/server.py) |
| Read configuration | [`config.py`](../src/unified_agent/config.py) |
| Enforce filesystem and command limits | [`safety.py`](../src/unified_agent/safety.py) |
| Store durable state | [`storage.py`](../src/unified_agent/storage.py) |
| Implement public operations | [`tools/`](../src/unified_agent/tools/) |
| Approved-dispatch spawn, sandbox, report | [`dispatch/`](../src/unified_agent/dispatch/) |
| LangGraph pilot + governed MCP tools | [`langgraph_pilot.py`](../src/unified_agent/langgraph_pilot.py), [`langgraph_dispatch.py`](../src/unified_agent/langgraph_dispatch.py), [`langgraph_workflow.py`](../src/unified_agent/langgraph_workflow.py), [`tools/langgraph.py`](../src/unified_agent/tools/langgraph.py) |
| Horizon 2 connector boundary | [`horizon2.py`](../src/unified_agent/horizon2.py), [`tools/horizon2.py`](../src/unified_agent/tools/horizon2.py) |
| Open-core edition seam | [`edition.py`](../src/unified_agent/edition.py) |
| Prod-action / fleet restart | [`safety.py`](../src/unified_agent/safety.py) `check_prod_action_gate`, [`fleet_actions.py`](../src/unified_agent/fleet_actions.py) |
| Agents → Hosts page host snapshots | [`host_registry.py`](../src/unified_agent/host_registry.py) |
| Passive session capture | [`session_capture.py`](../src/unified_agent/session_capture.py), [`scripts/hook_session_capture.py`](../scripts/hook_session_capture.py) |
| Session friction nudge | [`session_friction.py`](../src/unified_agent/session_friction.py) |
| Evidence report | [`audit/evidence_report.py`](../src/unified_agent/audit/evidence_report.py) |
| Queue hygiene (review scan) | [`queue_hygiene.py`](../src/unified_agent/queue_hygiene.py) |
| PR-event watcher | [`pr_watch.py`](../src/unified_agent/pr_watch.py) |
| Lane B validation-gate evidence | [`lane_b_telemetry.py`](../src/unified_agent/lane_b_telemetry.py) |
| Serve the browser control center | [`webui/`](../src/unified_agent/webui/) (Starlette single-mount) and [`packages/web`](../packages/web/) (React 19 SPA) |
| Live crew watch / intervene | [`crew.py`](../src/unified_agent/crew.py) (`GET /api/sessions/{id}/crew`, `POST /api/action/crew-intervene`) |
| ua-node device worker | [`node_agent.py`](../src/unified_agent/node_agent.py), [`node_queue.py`](../src/unified_agent/node_queue.py), [`acp_backend.py`](../src/unified_agent/acp_backend.py) |
| Hub-served harness config | [`harness_config.py`](../src/unified_agent/harness_config.py) (`GET /api/harness-config`, `/api/node/config`) |
| Run background work | [`executor/`](../src/unified_agent/executor/) and [`deploy/`](../deploy/) |
| Typed hub client / protocol / conformance | [`packages/client`](../packages/client/), [`packages/protocol`](../packages/protocol/), [`packages/conformance`](../packages/conformance/) |
| Control Center SPA (React 19 + Vite) | [`packages/web`](../packages/web/) |
| Test behavior | [`tests/`](../tests/) |
| Normalized telemetry | [`telemetry/`](../src/unified_agent/telemetry/) |
| Session profiles | [`profiles.py`](../src/unified_agent/profiles.py) |

## Build and verify

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run unified-agent-audit . --fail-on high
git diff --check
```

Run the focused test first when you change one module. Run the full set before
review. `make smoke` also tests selected optional runtime dependencies in a
temporary state directory. It does not replace pytest.

## Add or change a tool

1. Put the function in the correct `tools/` module. Give it a complete docstring.

2. Route a path through `resolve_for_project_read` (reads that return
   content; it applies the secret-filename guard), `resolve_in_workspace`
   (writes, with `for_write=True`), `resolve_in_vault`, or `resolve_for_audit`.
   Route commands through `vet_command`.

3. Raise `ToolError` with an action the caller can take.

4. Add the function to the registration tuple in `server.py`. Prefer a
   `TypedDict` return annotation on tools that return structured objects
   (`EvidenceReport`, `CanvasReadResult`) so MCPServer publishes an output JSON
   Schema in the tool manifest (task #549).

5. Classify the tool in `server.py` (`_READ_ONLY_TOOLS` or
   `_DESTRUCTIVE_TOOLS`), add it to exactly one `TOOLSET_MEMBERS` entry in
   `toolsets.py` (the hub fails at import otherwise), and add it to one set in
   `tool_classification.py`, updating the pinned counts in
   `tests/test_tool_classification.py`. Add the name to `EXPECTED_TOOLS` in
   `tests/test_server.py`.

6. Add focused tests. Update `tools-reference.md`, the tool count in
   `docs/README.md`, and `docs/tool-catalog.md` in the same change.

7. Run `tests/test_docs_tool_count.py`. It rejects undocumented registered tools.
8. Run `uv run python scripts/tool_surface_baseline.py --write` and commit the
   diff to `docs/tool-surface-baseline.json`. An added, removed or re-profiled
   tool fails CI until the baseline records it (task #2416).

[Building on unified-agent](building-on-unified-agent.md#2-add-an-mcp-tool)
walks through all of these with a worked example.

The registration wrapper is not optional. A function that is not in
`REGISTERED_TOOLS` is not available through MCP or the web dispatcher.

Authenticated HTTP `developer` and `steward` sessions use deny lists, so a new
tool is reachable there until it is explicitly denied. The developer
(default) profile is also capped at ≤ 100 exposed tools
(`tests/test_profiles.py::test_default_profile_tool_count_is_capped_at_100`).
A new agent-facing tool that would push the default surface over 100 must
either replace a pruned name or be added to
`DEFAULT_PROFILE_PRUNED_TOOLS` until it has a verified consumer. Steward's
`task_update` exposure is gated inside the tool (claim-ownership check),
not by hiding it.
The `research` and `operator` profiles are allow lists. A new tool is
**unreachable** there until you add it on purpose. Do not add execution,
workspace mutation, or `profile_activate` to research. Do not add git, vault
writes, or task mutation to operator. See
[Architecture](architecture.md#operational-profiles).

## Change a safety boundary

Treat a safety change as a product change. Read the resolver, its callers,
and its tests before editing.

- A workspace resolver must resolve symlinks before it checks containment.
- A vault write must stay inside configured write directories.
- `fs_read` / `fs_list` / `fs_search` may resolve into a known project checkout;
  mutating tools must not.
- A command must run with `shell=False`.
- An HTTP change must preserve the public-path and authenticated-path split.
- An audit rule must have one clear owner to prevent duplicate findings.

Add regression tests for an allowed case and a rejected case. Read
[Security model](security.md) and the existing safety tests before review.

## Release and service refresh

MCP hosts load tools at session start. A merged tool change is not visible
to an old host session. Restart the hub only after approved deployment work:

```bash
systemctl --user restart unified-agent.service
```

Then start fresh host sessions and verify `/healthz`. Do not claim a deployment
from a passing local unit test. Check the running service, endpoint, and client.

## Documentation rule

Write documentation in short, direct sentences. Define a term before first
use. Identify the source file for a design claim. Mark a proposal as a
proposal. Do not turn a dated assessment into a current fact.
