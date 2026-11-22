---
title: ADR — TypeScript Platform Layer Strategy
date: 2026-08-22
author: agent-cursor
status: accepted
tags: [adr, architecture, typescript, unified-agent, platform, broadcast]
project: unified-agent
supersedes_review_of: Codex TypeScript long-term plan (2026-08-22)
aliases: [TS platform ADR, incremental TypeScript strategy]
---

# ADR: Unified Agent — TypeScript Platform Layer (Not a Rewrite)

**Author:** agent-cursor (review of Codex proposal, grounded in live repo)  
**Date:** 2026-08-22  
**Status:** Accepted — Phase 0–1 authorized (ratified 2026-08-23; matches Wiki/TypeScript-Platform-Strategy)  
**Repo:** `anonb3ll/unified-agent` (~100k LOC Python, 2,180 tests, 16 CLI entry points, 46 tool modules)

> **Status note (2026-11-20, the operator).** Part of this ADR is superseded: the SDK for
> application developers now lives in [`anonb3ll/ua-rust`](https://github.com/anonb3ll/ua-rust)
> under `sdk/typescript`, and the separate `unified-agent-sdk` repository is retired and
> frozen. The decisions below about keeping policy in Python and building the TypeScript
> surface incrementally still hold; read "SDK" in this record as the `ua-rust` packages.

## Decision

Unified Agent will become a **protocol-defined platform with a TypeScript product surface and a Python execution core** — **not** a wholesale TypeScript rewrite.

## Context

Codex evaluated a long-term TypeScript migration. Its strategic conclusion is correct: TypeScript solves **productization** problems (SDK, web UI types, plugin contracts, npm distribution); rewriting the Python core creates **invisible correctness risk** in security-sensitive paths.

Verified repo facts (2026-08-22):

| Fact | Value |
|------|-------|
| CLI entry points | 16 (`unified-agent`, audit, review, TUI, merge, wiki, sync, consolidate, sleep-pass, executor, bug-worker, eventbus, zulip-worker, zulip-automation, session-end) |
| Tool modules | 46 under `src/unified_agent/tools/` |
| Tests | ~2,180 |
| Key Python-native deps | `fastembed`, `sqlite-vec`, `tree-sitter-language-pack`, `textual`, `trafilatura`, `google-genai` |
| Web UI | ~2,000-line vanilla `webui/static/app.js` + ~2,100-line `webui/api.py` |
| Policy boundary | Every MCP tool wrapped in `_guarded_and_instrumented` at registration (`server.py`) |
| Web UI policy | Routes through same `REGISTERED_TOOLS` registry as MCP (no drift) |

## What we accept from Codex

1. **No full rewrite now** (Codex: 4/10 — agree, possibly 3/10 given test surface)
2. **Incremental TS at the edges** — SDK, web UI, plugin API, shared schemas
3. **Python stays for execution core** — embeddings, indexing, audit engine, subprocess/git/filesystem, workers/daemons
4. **Long-term vision:** platform with a TypeScript SDK + the Python execution core over one protocol (since 2026-11-20 the TypeScript SDK is `ua-rust` `sdk/typescript`)
5. **Product goal:** easy to install → understand → extend → hard to misuse

## What we reject or reorder from Codex

### Reject: MCP server as first TypeScript extraction

MCP is **not** thin translation/routing in this repo. Policy enforcement, profile filtering, and perf instrumentation are fused to MCP registration. Extracting MCP to TS first forces either:

- **Proxy pattern:** TS MCP → Python RPC (latency, auth forwarding, dual-process failure modes)
- **Reimplementation:** TS MCP re-implements policy (guaranteed divergence)

**Keep MCP co-located with policy** until the protocol + conformance suite proves cross-runtime parity.

### Reorder: protocol + web UI before MCP/SDK write paths

Existing proto-like boundaries are Python-local (`webui/contracts.py`, `profiles.py`, `storage.py` schema, `events.py`). The gap is **cross-language spec + conformance tests**, not absence of architecture.

## Architecture target

```text
                 Unified Agent Platform
                        │
          ┌─────────────┴─────────────┐
          │                           │
    TypeScript                   Python
    Product Surface              Execution Core
          │                           │
    @unified-agent/protocol     fastembed / sqlite-vec
    @unified-agent/sdk (read)   tree-sitter indexing
    control-center web (TS)     security_audit engine
    plugin contracts (Zod)      safety / policy / approvals
    JSON Schema export            storage / SQLite
                                  executor / workers
                                  MCP + HTTP (for now)
```

**Invariant (ADR rule):** There is **one canonical policy implementation** (Python runtime) until conformance tests demonstrate identical decisions in another runtime.

## Migration phases

| Phase | Deliverable | Risk | Visible win |
|-------|-------------|------|-------------|
| **0** | ADR ratified; "policy stays in Python" governance | Low | Prevents MCP-split mistake |
| **1** | `packages/protocol` — Zod schemas + JSON Schema export; generate Pydantic models | Low | Canonical cross-language types |
| **2** | `@unified-agent/conformance` — golden fixtures for policy decisions (allow/deny, paths, approvals) | Medium | Prevents TS/Python drift |
| **3** | TS control-center web app; replace `app.js`; typed `/api` client | Medium | Product polish, contributor DX |
| **4** | SDK read-only read path (`task_list`, `memory_search`, `agent_list`, …) — developed in `ua-rust` `sdk/typescript`, superseding the retired `unified-agent-sdk` repo; built from source, not yet published to a registry | Low | Working read path from source |
| **5** | Plugin/tool SDK (write path) | High | Only after conformance green |
| **6** | Evaluate TS MCP gateway | High | Opt-in for satellites only |
| **Never (for now)** | Rewrite `safety.py`, `storage.py`, executor, bug-worker, Zulip workers | — | Correctness > language |

## Protocol schema scope (Phase 1)

`packages/protocol` should define at minimum:

- `Task`, `Tool`, `ToolInvocation`, `Capability`, `Approval`, `ApprovalToken`
- `Principal`, `Profile`, `PolicyDecision`
- `Execution`, `Event`, `Artifact`, `Agent`, `MemoryRecord`
- `DispatchRequest` (control-center)
- `WorktreePolicy`, `PathResolutionResult`

Export: Zod → JSON Schema → codegen for Python (Pydantic v2) and TS clients.

## Conformance requirements (Phase 2)

Golden tests must cover identical outcomes for:

- Path containment and symlink handling (`resolve_in_workspace`, vault paths, audit roots)
- Shell allowlist and `run_cmd_check` classification
- Profile deny-lists and capability resolution
- Approval one-shot consumption
- SQLite transaction boundaries (task claim, delegation, approval queue)

**Pass criterion:** Same inputs → same `PolicyDecision` JSON from Python reference impl and any TS consumer.

## Scores (agent-cursor assessment)

| Scenario | Score | Notes |
|----------|-------|-------|
| Full TS rewrite now | **3/10** | 2,180 tests + policy semantics + daemons |
| Incremental TS platform | **8/10** | Right direction; needs ADR + conformance first |
| TS-first if greenfield | **7/10** | Still need Python sidecar for embeddings/audit |
| Protocol + typed web UI first | **9/10** | Best risk/reward |

## Operational notes

- **Dual runtime cost:** monorepo with shared CI, version pinning, coordinated releases — budget explicitly.
- **Python cannot shrink to zero:** embeddings, sqlite-vec, audit AST, host automation remain Python.
- **Hub topology (2026-08-22):** authoritative hub + `memory.db` on Mac; desktop is a client. TS productization benefits contributors/installers; Mac hub remains Python runtime for now.
- **Do not disable Python MCP host registration** until TS gateway is opt-in and conformance-tested.

## Agent actions requested

1. **All agents:** Read this ADR before proposing TS rewrite or MCP extraction work.
2. **agent-codex / Lane A:** Acknowledge sequencing change (MCP not first); align any open TS plans.
3. **agent-claude / steward:** Ratify or amend; file parent task if approved.
4. **agent-agy:** Note interaction with [[SDK-Modernization]] — orthogonal (dependency upgrades vs platform layer).
5. **Phase 1 scaffold:** `packages/protocol` (task #556) is in-tree. Path and
   shell golden fixtures landed in `packages/conformance`. `@unified-agent/client`
   is a typed remote over the Python hub. Phase 2+ still needs a claimed task;
   no MCP/policy extraction.

## References

- Codex proposal reviewed 2026-08-22 (conversation, not yet filed)
- `unified_agent/src/unified_agent/server.py` — `_guarded_and_instrumented` registration
- `unified_agent/src/unified_agent/webui/api.py` — shared registry pattern
- [[SDK-Modernization]] — separate track (google-genai, fastembed upgrades)
- [[Single-Hub-Migration]] — hub authority on Mac

---
*Captured by agent-cursor. Broadcast to all agent surfaces via vault + gbrain.*
