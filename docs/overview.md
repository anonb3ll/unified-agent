# Overview

This page explains what `unified-agent` is, the problem it solves, and the
principles behind its design. Read it before the [Concepts](concepts.md) page.

## What it is

`unified-agent` is a **meta harness**. A *harness* is the program an AI agent
runs inside: Claude Code, Codex, Cursor, GitHub Copilot, Antigravity, Hermes,
Devin, OpenCode, a LangGraph graph, or your own agent loop. Each harness is
good at driving one model through one session. None of them is designed to
coordinate many agents, across many machines, on behalf of several people.

A meta harness sits one level above. It does not run the model. It gives every
harness the same five things:

1. **An identity.** Each agent, device, and person has a name the hub can
   verify, not just a label an agent types about itself.
2. **Shared work.** One task queue, one set of plans, one memory, and one
   record of who is doing what, visible to every agent.
3. **One policy.** The same sandbox, profiles, rules, and approval gate apply
   whichever harness makes the call.
4. **A human in charge.** Chosen actions pause until an authorized person
   approves them, and some actions can never be approved by the agent that
   asked.
5. **Proof.** Every decision and authorization lands on a tamper-evident
   ledger that can produce an auditor-ready report.

## The problem

Running one coding agent is easy. Running a team of them is not.

| Symptom | Root cause | What `unified-agent` provides |
|---|---|---|
| An agent repeats work another agent finished yesterday. | Memory and history live inside each harness. | Shared memory with semantic search, a shared task queue, and handoffs. |
| Two agents edit the same file and one overwrites the other. | Nothing arbitrates ownership. | Atomic task claims, per-task git worktrees, and leases that expire. |
| A permissive tool configuration on one machine becomes the real limit. | Each harness has its own sandbox and approval settings. | One hub-side policy: workspace sandbox, command allowlist, profiles, and rules. |
| Nobody can say who approved a risky change. | Approvals happen in chat, if at all. | A hub-side approval queue, signed approvals, and a five-link evidence chain. |
| Automation either does nothing useful or does too much. | There is no graded middle ground. | Autonomy modes, budgets, a kill switch, and validate-and-hold review. |
| Operators cannot see the fleet. | Every tool has its own logs. | A Control Center that shows the queue, sessions, approvals, devices, costs, and evidence. |

## Who it is for

- **Individual developers** who run several agents across a laptop, a desktop,
  and a server, and want them to act as one team.
- **Engineering teams** that want agents to take real work from a shared
  queue, with review and merge held by people.
- **Regulated organizations** such as legal, accounting, healthcare
  administration, and professional services. They must show which human
  authorized each piece of agent-produced work before it leaves the building.
  The [pilot kit](pilot/README.md) ships policy templates for these fields.
- **Platform builders** who want a governed backbone for their own agents
  instead of writing identity, approvals, and audit from scratch.

## What it is not

- **Not an agent framework.** It does not prompt models or plan tool calls.
  Your harness does that. Some executors do call a harness's headless mode on
  your behalf, but the harness still runs the model.
- **Not a replacement for your IDE or CLI.** Agents keep their native tools.
  The hub adds shared state and governance next to them.
- **Not a hosted SaaS.** It runs on hardware you control, with SQLite as the
  store. You can expose it to a tailnet or put a reverse proxy in front of
  it, but there is no multi-tenant cloud service.
- **Not an autopilot.** By design, merges, deploys, deletions, restarts,
  secret changes, and releases can be held for a human. The platform's own
  review pipeline validates and *holds*. It never merges on its own.

## How it works, in one paragraph

Agents connect to one hub over MCP, or over HTTP with the TypeScript SDK. Each
connection carries a bearer token, an OAuth grant, or a device-login token that
maps to one identity. On every call the hub checks that identity. It applies
the session's profile (which tools this session may use), evaluates policy
rules, and consults the approval gate. It then resolves any file path or
command against the sandbox, runs the operation, and appends the outcome to a
hash-chained event log. People watch and decide from the Control Center, which
calls the same code path. Executors and device workers pick up approved work
and run it in isolated git worktrees. Evidence reports read the ledger back
out as a chain from the human, through the device and agent, to the action
and its proof.

See [Architecture](architecture.md) for the full picture.

## Design principles

**Authority lives in one place.** Harnesses, SDKs, connectors, and the web UI
are adapters. None of them holds a second copy of policy. The TypeScript
client is a typed remote with no local fallback. The conformance package
checks that TypeScript consumers keep the Python decision shape exactly.

**Humans hold the keys.** Any tool can be put behind the global approval gate.
Merges, deploys, deletions, and restarts have their own scoped gate.
Separation of duties means a submitter can deny but never approve its own
release.

**Fail closed.** A missing token, a malformed policy file, an unknown
episode, or a signature that does not verify is a denial. It is never a
silent pass. An executor that cannot read its kill switch stops.

**Evidence over assertion.** A receipt proves that the hub recorded a request.
It does not prove that a side effect happened. Reports keep these apart. For
example, an agent's valid signature is recorded as *actor* evidence and never
promoted to *human* authority.

**Tightening only.** Presets, autonomy modes, and profiles can narrow what an
agent may do. None of them can widen a limit that a lower layer enforces.

**Local first, portable later.** Everything works on one laptop with no
network. The same hub serves a multi-device fleet over a private tailnet.

## Where to go next

- [Concepts](concepts.md) defines the vocabulary.
- [Getting started](getting-started.md) takes you from clone to a governed,
  evidenced action.
- [Human in the loop and evidence](human-in-the-loop.md) covers every control
  in one place.
