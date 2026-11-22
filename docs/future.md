# Future work and extension points

This page separates installed behavior from possible work. It does not approve
or schedule a change. An operator must review scope, authority, and risk first.

## Installed extension points

| Area | Current extension point | Code basis |
|---|---|---|
| MCP tools | Add a tested function and register it in the server tuple. | [`server.py`](../src/unified_agent/server.py) |
| Workflow steps | Add reviewed YAML in `src/unified_agent/workflows/`. | [`tools/workflow.py`](../src/unified_agent/tools/workflow.py) |
| Policy | Set a reviewed policy file for the policy engine (Phase 1 episode rules + Phase 3 tool/role/threshold + gateway rate_limit/bind). Example: [`examples/policy.phase3.yaml`](../examples/policy.phase3.yaml). | [`policy.py`](../src/unified_agent/policy.py), [`gateway.py`](../src/unified_agent/gateway.py), [`config.py`](../src/unified_agent/config.py) |
| Executors | Select Pi, Codex, or Agy executor support. | [`executor/backends.py`](../src/unified_agent/executor/backends.py) |
| Web UI | Add a route that dispatches through registered tools. | [`webui/`](../src/unified_agent/webui/) |
| Project streams | Extend the known project registry with tests. | [`config.py`](../src/unified_agent/config.py), [`webui/project_registry.py`](../src/unified_agent/webui/project_registry.py) |

## Current candidates

| Candidate | Reason | Status |
|---|---|---|
| Improve reviews, executor operations, and state-recovery evidence. | The service already has audit, backup, and review components. | Evaluate against current tests and operator needs. |
| Add a new model provider. | The LLM client has a subscription-first fallback chain (headless Claude CLI, then OpenRouter). | Requires a fresh billing and safety decision. |
| Add a project stream. | The web UI and executor use a fixed project list. | Requires source, tests, and operator approval. |

## Do not infer these capabilities

- A workflow does not authorize a merge, deployment, or destructive action.
- A local memory note does not move automatically to another machine.
- A model fallback does not create a spending limit.
- A security audit does not prove that a system is secure.
- A future document is not an implementation plan.

Use [Developer guide](developer-guide.md) to make an approved extension. Use
[Security model](security.md) to review new trust boundaries first.
