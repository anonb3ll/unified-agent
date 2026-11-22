# Tightening-only trust presets

Task #1421 adds named restrictions to the existing reviewed policy file.
Profiles still decide which tools a caller may see. Approval rules, budgets,
org ceilings, and autonomy modes still apply. A preset only refuses access;
it cannot expose a tool, issue a grant, or remove an approval requirement.

## Configuration

Add a `trust_presets` block to the policy selected by `UA_POLICY_FILE`:

```yaml
version: 1
trust_presets:
  default: inspection
  agents:
    agent-codex: active-worktree
```

The reviewed example is `examples/trust-presets-policy.yaml`. This is an
opt-in configuration; shipping the code does not change the live policy.
Only the three named presets below are accepted. Unknown names, fields,
invalid bindings, or malformed policy fail closed. There is no request-body
preset override. Tool enforcement uses the verified caller; explicit agent
arguments in the snapshot API are internal lifecycle-recording inputs.

| Preset | Mediated file scope | Writes |
|---|---|---|
| `inspection` | Configured workspace, intersected with each existing resolver boundary | Read-only, with existing bounded own-run reporting |
| `configured-workspace` | Configured workspace, intersected with existing boundaries | Resolved file writes only; no arbitrary mutable tool calls |
| `active-worktree` | One eligible live claim's worktree, intersected with existing boundaries | Resolved file writes only; no arbitrary mutable tool calls |

The active-worktree preset rejects a missing worktree, multiple eligible
claims, or lookup failure. It never chooses the newest of several claims or
falls back to the cockpit. Traversal, followed symlinks, broader project
reads, audit roots, and vault paths remain subject to the intersection.
Existing secret and hook protections remain in force. The active policy
file cannot be written through a preset, even when it is inside the chosen
file scope. Path checks do not eliminate concurrent symlink replacement
races; no operating-system sandbox guarantee is inferred from them.

Confined clients cannot provision task worktrees or change lifecycle state
through generic task calls. An orchestrator establishes context before
binding a preset. A caller still needs profile/policy permission for the
existing `dispatch_report_self` path; a preset never grants that tool.

## Native execution and child reports

Host shell, git subprocess tools, unsafe dispatch, and other unreviewed
mutations are refused. Native harness launch is denied when the executor caller, target, or
verified originator has a configured preset. The security audit tool is also
refused because its repository checks invoke Git subprocesses. Prompt instructions and CLI
sandbox flags are not treated as provider-neutral confinement evidence.
Supported native providers can be enabled only after #1355 supplies a
verified capability contract. Unbound executions retain their existing
behavior.

Child-to-parent reporting uses existing own-run evidence and delegation
completion callbacks. It introduces no messaging permission, new approval
store, or alternative scheduler. Internal cancellation, terminal recording, and historical evidence folding
remain available if policy becomes invalid. Governed remote tools still
require valid policy; a policy error stops native execution, and any refused
remote cleanup is reported rather than treated as successful.

## Evidence and diagnostics

Self-pickup claim events and folded run history carry an optional
`trust_preset` snapshot with `preset`, `policy_fingerprint`, `agent`, and
`enforcement` (`mediated_tools: true`, `native_runtime: false`). Dispatch
claim/resume events and observability also retain the originator snapshot
when applicable. Historical unbound records omit these fields. The
fingerprint identifies the parsed core policy, including preset bindings and
rules, used for that snapshot. Separate profile and budget authority remains
in its existing governance records. Later calls enforce current policy and
its existing governance. Remote
executors also consume hub-returned admission evidence before launch and
recheck authoritative current admission during execution; a local policy
copy cannot establish that a hub-bound run is allowed. Historical snapshots
remain unchanged when current policy is tightened or revoked.

Configured remote executors require an explicit positive claim attestation
and a current authenticated hub run check before preparing artifacts. They
also poll that hub during execution and refuse to fall back to a stale local
store. Explicit loopback standalone mode with `UA_EXECUTOR_ALLOW_DIRECT=1`
opts out of remote authority and keeps local behavior; it supplies no remote
admission guarantee. Node queues check hub policy before enqueue and claim,
and relay revocation as cancellation on heartbeat. Local detached supervisors
check their exact run while waiting, independently of scheduler ticks.
A requested stop remains distinct from confirmed cessation in existing evidence.

The `trust_presets` doctor row reports absent configuration as DORMANT,
valid mediated enforcement as LIVE with native launch refusal explicitly
stated, and invalid policy as MISCONFIGURED. It does not claim that native
isolation exists or activate a preset.
