# Session-scoped hub MCP for node-launched ACP sessions (#1744, I14)

Status: approved (option A, 2026-10-06); phase 1 implemented behind flag
(`UA_ACP_SESSION_MCP=off` by default). Phase 2 (one-node evidence run) and phase 4
(environment scrub) are not done. Date: 2026-10-06. Code references are to `main` at
`f7c25543`; the phase-1 implementation notes are in §15.

Pattern source (inspiration only): t3code's per-provider-session MCP endpoint, where each
session gets its own authorization header and an explicit capability set, and the agent
never holds the device credential.

## 1. Problem and premise (verified)

When ua-node launches a harness over ACP, the hub gives that session no hub MCP server.
The session reaches the hub only if the harness happens to have its own local hub
configuration, and then it uses that harness's standing credential. Hub access exists,
but nothing ties it to the session. The defects are attribution, blast radius and
revocation.

| Claim | Evidence |
|---|---|
| Every session open sends an empty MCP list | `acp_client.py:354` (`session/new`), `:341` (`session/resume`), `:349` (`session/load`). The task text cites `:289`; the code has since moved. |
| The agent's MCP transport support is received but never read | `initialize` stores `agentCapabilities` at `acp_client.py:299`; nothing in `src/` reads `mcpCapabilities`. |
| The harness inherits the node's whole environment | `AcpBackend` builds `AcpClient` without `env` (`acp_backend.py:420-426`), so `Popen(env=None)` inherits (`acp_client.py:124-130`). The node itself falls back to `UA_AGENT_BEARER_TOKEN` (`node_agent.py:159-161`), and the Codex template reads the hub bearer from that variable (`templates/distro/codex_config.toml.tmpl:6`). |
| Hub-served config bundles cannot carry a credential | Bearer, token and header auth is refused at publish (`harness_config.py:7`, `:63`, `:130-139`). This is deliberate. |
| Standing credentials are broad and long-lived | Static bearers have no expiry (`config.py:3063`, matched at `server.py:1379`). OAuth access tokens default to 7 days (`config.py:866`), with 30-day refresh (`oauth.py:85`). Both resolve to the identity's default profile (`profiles.py:960-971`), which for a developer is about 100 tools, including `fs_write`, `run_cmd` and `task_update`. |
| A hub call cannot be traced back to a session | `agent.gateway.decision` records only `caller` and `profile` (`gateway.py:556-570`). `episode_id_var` (`config.py:79`) is never set on the MCP path. |
| The node channel is already trusted and bound | `/api/node/*` requires a device-code OAuth token (`webui/routes.py:4243-4256`). A node is bound to one agent label and one device (`node_registry.py:52-75`). A claimed item carries its agent, `task_id`, `run_id`, attempt and a lease of `timeout_s + 300 s` (`node_queue.py:870-872`, `:921-946`). |
| ACP events reach `event_log` without redaction | `record_node_event` stores `text`, `tool_call` and `data` as received (`node_queue.py:1781-1798`). |

The ACP spec defines `agentCapabilities.mcpCapabilities.http` in the `initialize` reply.
It also defines an `McpServer` variant `{"type":"http","name","url","headers":[{"name","value"}]}`
that the client passes in `session/new`, `session/resume` and `session/load`.

## 2. Goals and non-goals

Goals:

1. Each ACP session the hub launches gets its own hub credential. It is short-lived and
   revocable, bound to (agent label, node, item, attempt, task, ACP session), and limited
   to a narrow tool set.
2. ua-node passes the credential in `mcpServers` (HTTP transport) only when the agent
   advertises `mcpCapabilities.http`. Otherwise it behaves as today and records why in
   `session_open`.
3. Every MCP call made with the credential is recorded in `event_log` against that
   session.
4. The secret value never appears in argv, logs, events, receipts or transcripts.

Non-goals:

- Sandboxing the harness at the OS level. The harness runs as the node's OS user and can
  read the device token file. This design narrows what the hub hands out and stops
  pointing the harness at a standing token. It does not contain a hostile local process.
- Changing device login, WorkOS hub auth (`workos-platform-attachment.md`), or the
  static bearer leg.
- Hub-side CLI dispatch, which injects the standing bearer through
  `dispatch/child_env.py:99`. That is a follow-up (§12, Q7).
- A stdio MCP bridge for agents without HTTP support (Q5), and ACP v2.

## 3. Threat model

| | Standing token (today) | Session credential (proposed) |
|---|---|---|
| Routes | All authenticated routes, including `/api/*` | `/mcp` only; any other path returns 403 |
| Tools | The identity's default profile (developer: about 100) | The phase-1 set in §6 (about 9), and never wider than the identity ceiling |
| Escalation | `profile_activate` up to the identity ceiling | `profile_activate` and `profile_reset` refused |
| Lifetime | Unlimited (static) or 7 d + 30 d refresh | Idle window 10 min, renewed by node heartbeat, capped at the item's lease deadline (at most 6 h + 5 min) |
| Valid while | The token exists | The item is `claimed` by this node at this attempt, the node is live, the agent is not revoked, and the feature flag is on |
| Revocation | Rotate the label's token, which breaks every session of that label | Per session, automatic at end, cancel, task close, lease expiry or device revoke; also by operator action |
| Attribution | Label only | Label, node, device, item, attempt, task, run, ACP session |
| Replay from another machine | Works from anywhere on the tailnet | Refused when the peer's tailnet device is not the node's device (§5) |

Who could steal a session credential: the harness process, which needs it; any process
running as the node's OS user, which could equally read the device token; a harness that
logs or saves its MCP config (Q4 checks this per harness); or the model, if a harness
exposes its config text to the model. Prompt injection cannot read HTTP headers that the
harness holds outside the model's context. In every case the stolen value can reach about
9 tools, only on `/mcp`, only from the node's device, and only until the session ends.

## 4. Credential format and storage

- **Value.** `uas_` plus 43 characters of base64url (32 random bytes from `secrets`). The
  prefix lets the middleware send the token to the right check without a database
  probe. It also gives redaction and the pre-push audit a pattern to match (§10).
- **At rest.** A new table, `session_credentials`, keyed by `sha256(value)`, the same
  scheme as `oauth_store.py:63-75`. The value is 256 bits of random data, so no salt or
  slow hash is needed. The plaintext is never stored. Columns: `credential_id` (16 random
  hex, the public handle), `token_sha256 UNIQUE`, `agent_label`, `device_agent`, `node`,
  `device_node`, `item_id`, `attempt`, `task_id`, `run_id`, `acp_session_id` (set at
  `session_open`), `agent_session_id`, `tools_json`, `issued_at`, `idle_expires_at`,
  `expires_at`, `last_used_at`, `revoked_at`, `revoke_reason`, `org_id`. There is at most
  one active row per (`item_id`, `attempt`).
- **Why not reuse the OAuth access-token store.** A token found there resolves through
  the OAuth leg (`server.py:1386-1404`) to the identity's default profile on every path.
  It also lives in a process cache (`oauth.py:842-900`), so revocation would depend on
  clearing that cache correctly. A separate check that fails closed and reads the
  database on every request keeps the narrowing structural. Session traffic is light,
  so one indexed SELECT per request is affordable.
- **Delivery.** Hub to node: the response to `POST /api/node/session_credential` (§8),
  over the device-token channel that already carries prompts. Node to harness: the
  `headers` of the HTTP `McpServer` entry in `session/new` (or `session/resume`/`load`),
  written to the harness's stdin pipe. The value is never in argv, never in the
  environment and never in a file the node writes. ACP sends nothing back that echoes
  `mcpServers`. A header is used rather than an environment variable because ACP has no
  per-server environment for the HTTP transport, and environment variables are inherited
  by every grandchild the harness starts.
- **Node handling.** The value exists only as a local variable in `AcpBackend.run`. It is
  passed to `new_session` and then dropped. It is never put into `emit`, the receipt, the
  receipt spool (`node_agent.py:646-680`), `session_dir` or a log line. `emit` also
  replaces any exact occurrence of the value with `[REDACTED_SECRET]` before an event
  leaves the node.

## 5. Binding and claims

Mint-time binding comes from rows the hub owns, never from node input. The node supplies
only `node`, `item_id` and `attempt`. The hub requires all of these:

- the caller passes `_node_request` and `_owned_node` (label and device match), **and**
  its auth method is device-code OAuth. The mint route checks this itself and does not
  honour `UA_NODE_ALLOW_BEARER=1`: that flag lets `_node_request` admit a static bearer
  (`webui/routes.py:4242-4257`), and a static bearer must never be able to mint;
- the item is `claimed` by that node, with `attempts` equal to the given attempt and an
  unexpired lease;
- the subject label is known (`config.is_known_agent_label`) and not in
  `config.revoked_agents()`.

The subject is `profile_agent` when set (acp:hermes), otherwise the node's agent. This
matches the `agent_sessions.agent` choice at `node_queue.py:1632`. The device agent is
recorded alongside it.

Checks on every request, all of which must pass or the request gets 401:

1. The token's SHA-256 matches a row with no `revoked_at`.
2. `now < min(idle_expires_at, expires_at)`.
3. The bound item is still `claimed`, with the same `attempt` and node.
4. The node's `last_heartbeat` is within `NODE_LIVE_WINDOW_S` (90 s,
   `node_registry.py:21`).
5. The flag is on (§11).
6. The label is not revoked.
7. If the hub can resolve the peer's tailnet device (`tailnet.resolve_peer_device`,
   `server.py:1536-1547`), it must equal the row's `device_node`. A peer the hub cannot
   resolve is refused, unless the bound node is the hub host itself (loopback). The
   hub resolves the raw socket peer, so a hub behind a local reverse proxy sees every
   client as loopback and this check cannot tell nodes apart. For that deployment,
   `UA_ACP_SESSION_DEVICE_CHECK=best_effort` skips check 7 and records
   `device_check:"unavailable"` on every decision; the default, `strict`, refuses (Q9).
8. The path is `/mcp`.

A token with the `uas_` prefix never falls through to the bearer, OAuth or WorkOS checks.

## 6. Tool-profile narrowing

A new code-only profile, `acp-session`, is added to `VALID_PROFILES`. It cannot be
activated and sits below `PUBLIC` in `_PROFILE_PRIVILEGE_HIGH_TO_LOW`.

```
effective = _allowed_tools_for_profile(default_for_identity(label), REGISTERED_TOOLS)
            ∩ ACP_SESSION_ALLOWED_TOOLS
            ∩ item.session_tools          # optional per-item narrowing; can only shrink
```

The set is an intersection, never a subtraction. This follows the precedent of
`RESEARCH_ALLOWED_TOOLS` (`profiles.py:1072-1075`): a tool registered later is unreachable
until a reviewed change adds it. The proposed phase-1 `ACP_SESSION_ALLOWED_TOOLS`:

| Tool | Why | Guard |
|---|---|---|
| `profile_status` | The session can see its own grant | none |
| `memory_search`, `memory_get`, `memory_list` | Memory unification, the purpose of this task | read only |
| `memory_save` | The session's learnings join shared memory | provenance in §7 |
| `task_list` | Task context | read only |
| `task_comment` | Progress on its own task | only on the bound `task_id`; anything else is refused |
| `session_packet` | Packaged context for the bound task | read only |
| `evidence_report` | Reads evidence | read only |

Excluded, and only addable with operator approval: anything that writes files, runs
commands or uses git; `task_update`, `task_claim` and `task_add`; dispatch, approval,
delegation and messaging tools; vault writes; managed secrets and token tools; profile
switching. Tools that need a producer HMAC stamp (`producer_sign.py:388`, keyed on a
standing bearer) are excluded by construction.

Enforcement: the middleware binds a new context variable, `session_grant_var`, holding
the frozen tool set. `profiles.allowed_tools` (`profiles.py:1095`) intersects with it when
it is set. That one change covers `tools/list` (`server.py:1050`) and the call ceiling
(`server.py:575`). `profiles.activate` refuses while it is set. The bound-task guard runs
inside `_policy_guarded_body`, before the policy gate.

## 7. Attribution in event_log

For a session credential, the middleware binds:

- `auth_metadata` = `{"auth_method": "acp_session", "credential_id", "item_id", "attempt",
  "node", "task_id", "run_id", "acp_session_id", "device_agent"}` (all non-secret);
- `episode_id_var` = `acp:<item_id>`, which joins the call to the node's `acp_event`
  rows for the same item.

`gateway.record_call_result` and `record_denial` (`gateway.py:478-570`) add
`"session": {...}` from `auth_metadata` when `auth_method == "acp_session"`. Each allow,
deny, pending or error decision then carries the session, and `originator` stays the
label. `memory_save` records `source_session=credential_id` in the note's metadata. New
lifecycle channels: `hub.session_credential.minted`, `.revoked` (with `reason`) and
`.refused` (rate-limited, with the failed check from §5). None of them carry the value or
its full hash; `credential_id` is the only handle.

On `session_open` ingest (`node_queue.py:1804-1807`), the hub fills `acp_session_id` and
`agent_session_id` on the row. It also writes `credential_id` into `agent_sessions.meta`,
so the Sessions pane can link from a session to its hub calls. A resumed session keeps
its ACP session id but gets a new credential, and the existing session-opening paths
(`node_queue.py:1596-1624`) can return early for a row that already exists. The ingest
therefore updates `meta.credential_id` on the existing row as well (keeping prior ids in
`meta.credential_ids`), rather than writing it only when the row is created.

## 8. Flow

```
node: claim item ─> spawn harness ─> initialize
  └─ mcpCapabilities.http is true and the hub advertises the feature?
       yes ─> POST /api/node/session_credential {node, item_id, attempt}
              <─ {credential, credential_id, expires_at, tools}
              session/new { cwd, mcpServers: [{type:"http", name:"unified-agent",
                            url:"<hub origin>/mcp",
                            headers:[{name:"Authorization", value:"Bearer <cred>"}]}] }
              emit session_open {..., hub_mcp:"scoped", credential_id}
       no / mint failed ─> session/new { cwd, mcpServers: [] }
              emit session_open {..., hub_mcp:"none", hub_mcp_reason:<why>}
  ... turns: harness calls /mcp with the header; hub checks §5, narrows §6, attributes §7
receipt / cancel / lease expiry ─> hub revokes in the same transaction
```

The node passes a `credential_fn` into `AcpBackend.run`, in the same way it passes
`permission_fn` today (`node_agent.py:1146-1153`). A resumed session is a new item, so it
mints a new credential for `session/resume` or `session/load`. The URL is
`HubTransport.hub` + `/mcp`, which already passed `transport_is_protected`
(`node_agent.py:146-150`).

## 9. Lifetime, renewal and revocation

- **Expiry.** At mint, `expires_at` is the item's lease deadline at claim time:
  `claimed_at + timeout_s + LEASE_GRACE_S` (`node_queue.py:68-72`). That is 65 min by
  default and 6 h 5 min at most. `idle_expires_at` is `now + 10 min`. The value never
  rotates, because ACP has no way to change a session's `mcpServers` mid-session.
  Renewal is therefore hub-side only: a node heartbeat for a running item
  (`node_queue.py:358-375`) also slides `idle_expires_at` forward, in the same
  transaction. It never moves `expires_at`.
- **Revocation triggers.** Each one sets `revoked_at` and `revoke_reason` in the
  transaction that causes it.

| Trigger | Hook |
|---|---|
| Session end, receipt posted | `node_queue.post_receipt` (`:1132`) |
| Cancel | `node_queue.cancel` (`:969`) and `cancel_for_run_evidence` (`:987`). Revoked at the cancel request, not at the receipt |
| Task closed | `tools/tasks.task_update` (`:1177`) moving the bound task to `done` or `cancelled`; `task_delete` (`:2583`) |
| Node loss | `_fail_expired` (`:776`) when the lease expires; check 4 in §5 fails within 90 s |
| Device revoked | `oauth.revoke_sessions(device=…)` (`oauth.py:1424`) cascades to rows with that `device_node`; independently, every request requires the minting device sign-in to still exist (§15) |
| Agent revoked | `config.revoked_agents()` (check 6) |
| Operator action | New operator-only Control Center route that revokes by `credential_id`, item or node. Flag off disables every credential while it is off but revokes none (see §11) |

Enforcement on each request is the database read in §5, with no cache. The next request
after any trigger gets 401.

## 10. Keeping the secret out of every channel

- **argv:** unchanged. `resolve_argv` is fixed, and the `spawn` event records only
  `argv_tail` (`acp_backend.py:427`).
- **Environment:** in phase 4, `AcpBackend` passes an environment filtered like
  `dispatch/child_env.py`, without `UA_AGENT_BEARER_TOKEN`. The harness then cannot fall
  back to the standing token. This step is gated on Q4.
- **Events:** the node redacts the exact value (§4). The hub runs
  `redact_sensitive_content` over the `text`, `tool_call` and `data` strings in
  `record_node_event` (`node_queue.py:1781`). The `uas_` pattern is added to
  `CREDENTIAL_SHAPE_PATTERNS` (`telemetry/redaction.py:42`). That one change covers the
  hub's redaction and the `unified-agent-audit` secret rule (`audit/rules.py:69`), which
  the pre-push gate runs (`scripts/hook_pre_push.py`).
- **Logs:** neither the hub nor the node logs request headers. The mint route's response
  is excluded from any request or response debug logging.
- **Transcripts:** the harness's own session store is out of the hub's reach. Q4 checks
  each harness. If a harness persists the value, the saved copy is already revoked when
  the session ends.

## 11. Migration and rollout

Hub flag `UA_ACP_SESSION_MCP` = `off` (default) | `on`, plus an optional node allowlist,
`UA_ACP_SESSION_MCP_NODES`. The flag is read on every request, so `off` disables every
outstanding credential at once. Node side: a new feature string, `acp_session_mcp`, goes
in `NODE_FEATURES` (`node_agent.py:60`), and `UA_NODE_SESSION_MCP=0` opts a node out. A
hub without the route returns 404; the node then records `hub_mcp_reason:
"hub_unsupported"` and carries on.

1. **Phase 0.** The operator approves this note.
2. **Phase 1, flag off.** Schema (`ensure_schema`), mint, verify and revoke code, the
   middleware check, profile narrowing, the gateway session block, redaction and all
   tests in §12. Nothing changes in behaviour.
3. **Phase 2, one node.** Turn the flag on for one node and one harness that advertises
   `http`. Collect the live evidence (DoD).
4. **Phase 3, all nodes.** Default on. Watch `.refused` counts and `hub_mcp:"none"`
   reasons in the Sessions pane.
5. **Phase 4, environment scrub.** Remove `UA_AGENT_BEARER_TOKEN` from the harness
   environment for sessions marked `hub_mcp:"scoped"`, once Q4 is confirmed for each
   harness.

Rollback at any phase: set the flag to `off`. No data migration is needed; the table is
additive. Flag off is a kill switch, not a graceful rollback: ACP cannot replace a
running session's `mcpServers`, so a live session's scoped credential stops working at
once and that session has no hub MCP while the flag stays off. It does not regain a
standing token mid-session. New sessions fall back to today's behaviour. Flag off
disables credentials; it does not revoke them (no table-wide write on a toggle).
Turning the flag back on revives every credential that is still within its windows
and whose item is still running. For incident response, revoke instead: the
operator route revokes by `credential_id`, item or node, and a revoked credential
never comes back. For a graceful rollback,
remove nodes from `UA_ACP_SESSION_MCP_NODES` instead: the allowlist is read at mint
only, so running sessions keep their credential until they end and new sessions fall
back.

## 12. Test plan, mapped to the DoD

| DoD item | Tests (new file `tests/test_session_credentials.py` unless noted) |
|---|---|
| Mint | Issued only for a claimed item, the owning node, the matching attempt and a live lease. Refused for a foreign node, wrong device, stale attempt, finished item, unknown or revoked label, flag off, or a static-bearer caller (even with `UA_NODE_ALLOW_BEARER=1`). The database holds only the SHA-256. The value is returned once. A second mint for the same attempt revokes the first. |
| Revoke | Each trigger in §9 revokes, and the very next `/mcp` call returns 401 (no caching). Flag off refuses an otherwise valid token. |
| Expiry | With an injected clock: the idle window lapses without a heartbeat and slides with one; the absolute cap holds despite heartbeats; a stale node heartbeat is refused. |
| Narrowing | `tools/list` returns exactly the effective set. Calling an excluded tool raises `ToolError` and records a deny event. `profile_activate` is refused. `task_comment` on another task is refused. Non-`/mcp` paths return 403. A `uas_` token never reaches the OAuth check. Peer-device mismatch returns 401; with `best_effort` an unresolvable peer is allowed and the decision records `device_check:"unavailable"`. |
| Attribution | An allowed `memory_search` writes `agent.gateway.decision` with the `session` block, `originator` set to the label and `episode_id` set to `acp:<item_id>`. Deny events carry the block. `session_open` ingest fills `acp_session_id` and `agent_session_id`. |
| Fallback (`tests/test_acp_adapter.py`) | A fake agent without `mcpCapabilities.http` gets `mcpServers: []` and `hub_mcp:"none"`/`agent_lacks_mcp_http`. With `http` true it gets one HTTP entry; the trace records a hash of the header value, not the value. A failed mint falls back and the run still finishes. A resume mints a new credential and updates `meta.credential_id` on the existing session row. Flag off cuts off a live session's access without restoring a standing token; an allowlist removal leaves running sessions alone. |
| Secret never leaks | A test sweeps every place the value could appear and asserts it is absent from each: argv, every event the node posts, every `event_log` row (full table scan), `caplog` on the hub and the node, the receipt, the receipt spool, files under `session_dir`, and exception text. A `uas_` literal in a fixture fails `unified-agent-audit`. |
| Live evidence | Phase 2: a node-launched session on a real harness is asked to call `memory_search`. The `evidence_ref` is the `event_log_query` result: `.minted`, then the `memory_search` decision with `session.item_id`, then `.revoked` with `reason=session_end`. It is recorded on #1744. |

No `UA_AGENT_TOKENS` value is read, copied or derived anywhere in this path.

## 13. Open questions (each with a recommended answer)

1. **Which label owns an acp:hermes session's calls?** Recommend `profile_agent`, with
   `device_agent` in the session block. This matches `agent_sessions`
   (`node_queue.py:1632`).
2. **Is the phase-1 tool set right?** Recommend the §6 table exactly. Grow it only
   through a reviewed change to `ACP_SESSION_ALLOWED_TOOLS`.
3. **Lifetimes.** Recommend a 10-minute idle window and a cap at the item's lease
   deadline. A cap shorter than the item's timeout would cut working sessions off,
   because ACP cannot re-key a live session.
4. **Server name and environment scrub.** Recommend naming the server `unified-agent`, so
   it shadows any local entry of that name, and scrubbing `UA_AGENT_BEARER_TOKEN` in
   phase 4 only. Before that, check each harness in the ACP matrix: whether it merges or
   replaces a server of the same name, and whether it writes `mcpServers` headers to its
   session store or logs.
5. **Agents without HTTP MCP.** Recommend `hub_mcp:"none"` for now. A stdio bridge
   (`unified-agent mcp-proxy`, credential passed in the stdio server's `env`) becomes a
   separate task, if one matters later.
6. **Should a session write shared memory?** Recommend yes, with `source_session`
   provenance. Unifying memory is the purpose, and every write is attributed and can be
   revoked.
7. **Should hub-side CLI dispatch adopt the same credential?** Recommend a follow-up
   task. `dispatch/child_env.py` injects the label's standing bearer today.
8. **Should closing a task cut off a live session?** Recommend yes. The session cannot
   close its own task (no `task_update`), so a close always comes from someone else and
   should take effect immediately.

9. **Device check behind a reverse proxy.** Recommend `strict` (the default) wherever the
   hub sees tailnet peers directly, and `best_effort` only for a proxied hub, which then
   relies on the other seven checks and records that the device was not verified.

## 14. Decision needed (plain language)

**What is going on.** When the hub tells a machine to start an agent, the agent either
cannot reach the hub's memory, or it reaches it with its own master key. A master key
opens about 100 doors for days or for good. When that agent does something, the hub
cannot tell which job did it, and the only way to take access back is to change the key
for every job that agent runs.

**The question.** Should each job get its own temporary pass?

| Option | What it means | Cost | Risk |
|---|---|---|---|
| **A. Per-job pass (recommended)** | The hub prints a pass for one job on one machine. It opens about 9 doors (memory, task reading, comments on its own task). It stops working when the job ends, is cancelled or goes quiet, or when you revoke it. Every use is logged against that job. | A new table, a new check at the hub's front door, changes to node and agent code; about one PR with tests. | Low. It is off until you turn it on, and turning it off kills every pass at once. |
| B. Reuse the existing sign-in tokens with fewer scopes | Same idea, built on the device sign-in store. | Smaller. | Those tokens are trusted on every route and cached in memory, so one missed check could hand out the master key's reach. |
| C. Label only | Keep master keys, and add a "job id" tag to calls. | Small. | Tells you which job, but takes nothing away, and the tag can be faked. |
| D. Do nothing | Today's behaviour. | None. | Memory for launched work stays hit or miss, and access cannot be taken back per job. |

**Recommendation: A.** Approve:

1. this design;
2. building it behind `UA_ACP_SESSION_MCP=off`;
3. the 9-tool list in §6;
4. the lifetimes in Q3;
5. turning it on for one machine to collect the evidence run, with the environment scrub
   (Q4) left for a later yes.

## 15. Phase 1 implementation notes (2026-10-06)

Where the code differs from the text above, the code is the closest safe
equivalent and fails closed:

- **Module and route.** `session_credentials.py` (services layer) owns mint,
  verify, revoke and renewal; the table DDL is `table_schemas.session_credentials`.
  `LEASE_GRACE_S` moved to `node_registry` (re-exported by `node_queue`) so the
  credential lifetime reads the same value. The operator route is
  `POST /api/action/session-credential-revoke` (operator only, CSRF-guarded).
- **Hub feature advertisement.** The hub lists `acp_session_mcp` in
  `hub_features` of the `/api/node/register` reply; a node that registered with
  a hub without it does not call the mint route. A 404 or 405 without a
  `reason` from the mint route is still read as `hub_unsupported`.
- **Mint checks beyond §5.** The node must have advertised `acp_session_mcp`
  in its facts, the item must be an `acp:*` item and not be cancelling, and the
  device agent as well as the subject must be known and unrevoked.
- **Per-request checks beyond §5.** Check 3 also refuses a cancelling item
  (direct or via its run); check 4 also requires the node row's agent and
  device binding to match the credential's. A request whose idle window or cap
  has lapsed revokes the row (`idle_expired` / `expired`). A request on a
  `UA_HTTP_PUBLIC_HOSTS` Host is refused.
- **Check 7.** A peer that resolves to a tailnet device must match under both
  modes, so `best_effort` relaxes only the unresolvable case. "The hub host
  itself" is taken as a loopback peer whose node has no device binding.
- **Item narrowing.** No per-item tool field exists yet; the row's
  `tools_json` (the effective set at mint) is the hook. Each request
  intersects it again with the current identity ceiling and
  `ACP_SESSION_ALLOWED_TOOLS`.
- **`memory_save` provenance.** The memory table has no metadata column and a
  column edit would need a schema bump, so `source_session` is recorded as a
  `hub.session_credential.memory_save` event (`note_id`, `source_session`,
  `item_id`, `task_id`).
- **Revocation outside one transaction.** `settle_expired_run` and
  `_fail_expired` revoke inside the caller's transaction; their `.revoked`
  events are written after it commits (`node_queue.flush_revoked_events`). The
  device-revoke cascade runs in `tools/oauth_devices.revoke_device_sessions`
  (both the provider and the durable-row branch) right after the OAuth rows are
  revoked, in its own transaction.
- **Node redaction.** Besides events and the receipt, the node scrubs the
  value from the harness stderr log it keeps under `session_dir`, and the ACP
  client never logs it in a dropped non-JSON line. The hub also redacts the
  `plan` field of node events.
- **Visibility.** Under a session grant, `profiles.tool_is_allowed` is the
  only visibility term in the tool wrapper: the HTTP read-helper authority and
  the chief-of-staff config rollback recovery never apply (both also refuse on
  their own when a grant is bound).
- **Bound task, read live.** Besides the revocation hooks in `task_update`
  and `task_delete`, every request reads the bound task: missing revokes as
  `task_deleted`, `done` or `cancelled` as `task_closed`. That covers task
  closes that bypass `task_update` (direct run completion, merge closeout,
  plain SQL). Mint refuses an item whose task is already closed or missing.
- **Mint subject.** A mint is refused when the subject is a human identity
  (the operator label or any label `actor_kind_for_identity` calls human). The
  tool set is the intersection of the subject's ceiling, the device agent's
  ceiling and the nine tools, at mint and on every request.
- **Device-revoke cascade.** Runs for a device revoke and for a single
  sign-in (`session_id`) revoke, on the device names and ids of the revoked
  sign-ins. A revoked sign-in with no device binding also revokes credentials
  of its label on nodes that have no device binding (this may over-revoke).
  The cascade is bookkeeping, not the guarantee. Node liveness (check 4) is
  no backstop: a node can keep heartbeating through another sign-in or a
  static bearer. The guarantee is a ninth per-request check, bound to one
  sign-in family. A device-code sign-in gets a random `signin_id` claim at
  issue, which every refresh rotation carries forward; the credential row
  records the `signin_id` of the sign-in that minted it, and every request
  requires the device agent to still hold an unexpired refresh row with that
  id. Other sign-ins of the same label (on another machine, in a browser, or
  on a node id reporting the same name) never keep it alive. A device or
  single-session revoke deletes the family's rows, so the next request is
  refused whether or not the cascade ran.
- **The sign-in check never writes.** A miss (or a store read error) refuses
  the request with 401 and leaves the row alone, like the other live-state
  checks; the cascade or an explicit revoke does the permanent write. A
  transient miss therefore never destroys a live session.
- **Atomic rotation.** `oauth_store.rotate_refresh` deletes the old refresh
  row and inserts its successor (same subject, device binding and claims,
  `signin_id` included) in one transaction, so a family is never without a
  row mid-rotation. A memory-only or store-down rotation keeps the old
  write-after path.
- **Mint.** Refused (`signin_missing`) when the access token carries no
  `signin_id` (a sign-in from before this change: run `unified-agent login`
  again) or no durable live row backs it. A credential row with no
  `signin_id` is refused on every request.
- **Failed cascade.** The revoke still commits and answers
  `status: "partial"`, `session_credentials: "failed"` and a warning that
  points at the node-wide revoke route (a retry finds no sign-in left to
  cascade from).
- **Not done: the in-memory deny list.** The live check reads the durable
  refresh rows, not the provider's in-memory `revoked_digests`; every revoke
  path deletes the rows, so the list only differs when a revoke's store write
  itself failed. The check lives below the OAuth adapter layer and cannot read
  it.
- **Permission prompts.** The node scrubs the value from a permission
  request before relaying it, and the hub redacts credential shapes from the
  stored `tool_call` before the approval gate. The scrub is deterministic, so
  a re-polled prompt still matches its pending approval row.
- **Split and truncated echoes.** The node redacts before any truncation, and
  keeps a tail of `len(value) - 1` characters per text stream so a value split
  across message chunks is redacted at the chunk boundary.
- **Stderr log timing.** The harness's stderr goes straight to
  `session_dir/acp-agent.stderr.log`; the node scrubs that file when the run
  ends. While the run is live, or if the node is killed mid-run, an agent that
  printed the value leaves it there in plaintext. The credential itself is
  revoked by the receipt or the lease expiry regardless.
- **What a session can read.** MCP resource reads ask
  `profiles.tool_is_allowed`, which honours the grant. `memory_*` reach shared
  memory as the label would. `task_list` (and the task, board, goal, org-unit
  and org-proposal resources gated on it), `session_packet` and
  `evidence_report` are not bound to the session's task: they read what the
  label's ceiling allows. Comments are bound: `task_comment` and comment
  resources answer only for the bound task.
- **Flag off.** Disables, does not revoke (see §11). Use the operator route to
  revoke.
- **Hardening (task #1893).** Before phase 2:
  - A stashed `.revoked` event is emitted only when the stored
    `revoke_reason` matches the stashed one. A different reason means the
    stashing transaction rolled back and another revoke committed and wrote
    its own event; the stale entry is dropped.
  - The node keeps a `_redact_stream` tail per tool call id and field
    (`toolCallId` plus the field path with list indexes folded), so a value
    split across streamed `tool_call_update`s is redacted at each boundary.
    The tail map holds 4096 streams and evicts another call's tail before the
    current call's. An update with more than 256 string fields is redacted as
    one flattened JSON stream, and nesting below depth 12 is flattened as in
    `_redact_tree`, so one wide or deep update cannot evict a pending tail.
  - The hub redacts node event text before cutting it to 4000 characters, and
    redacts every node-supplied string of a permission prompt (`session_id`,
    `optionId`, `name`, `kind`, the tool call) before bounding it. The scrub is
    deterministic, so a re-polled prompt still matches its pending row. The
    node selects only an option id that the prompt actually offered and
    answers `cancelled` for anything else, so an approved option id the hub
    redacted (or any id the harness did not offer) ends the prompt cancelled.
  - `memory_save` and `task_comment` append redact the credential from free
    text (content, title, body) on every save, with a `warning` in the result
    (kept in an idempotency receipt, so a replay repeats it). The match is
    exactly `uas_` plus 43 base64url characters not followed by another such
    character: a value glued after a word is caught, a longer run is not.
    Structured fields (tags, evidence, ids, mentions, idempotency keys, and
    the raw claimed `author`, or `agent` for `memory_promote`) that carry it
    are refused with a generic error before any identity check or write, so
    an unverified stdio save cannot store it as the author. Applied always, not only under a grant: the shape
    is never legitimate content.
  - Refresh rotation: once `rotate_refresh` has committed, the provider no
    longer re-checks expiry (the store already found the row live), and any
    raise after the commit deletes the successor row the client never
    received.
  - Store-down rotation can leave two rows of one family (the old row, whose
    atomic rotation failed, and the successor the memory fallback saved). We
    chose to revoke by family rather than try to prevent the duplicate in the
    live refresh path: a single-session revoke deletes every row with the same
    subject and `signin_id`, so the sign-in check (and the OAuth session)
    ends whichever row the operator picked.
  - The sign-in lookup reads `signin_id` only from rows whose claims are valid
    JSON (`json_valid` guard). A malformed row never matches, and no longer
    makes the query raise and refuse every credential of its label.
- **Not done here: proxy headers.** Check 7 resolves the raw socket peer. A hub
  that trusts `X-Forwarded-For` (uvicorn `proxy_headers`) would need the device
  check reworked; that is a hub-wide change tracked separately.

