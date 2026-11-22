# Fleet Console and connector-style auth

Status: design, accepted for build. Filed 2026-09-24 by agent-claude (steward pass).
Epics: **I — Fleet Console** (#1319), **J — Connector-style auth** (#1318).
First shipped slice: J1 device sign-in (#1320, this PR).

## The problem, as measured on 2026-09-24

The operator runs 8 agent harnesses (Claude Code, Codex, Cursor, Antigravity/agy,
Hermes, OpenCode, Copilot, Devin) across 4 hosts (desktop, Mac hub,
Mac laptop, GCP worker). Using them means:

- **Many apps.** A desktop `ps` shows the Claude desktop app (several `claude`
  children), the Hermes dashboard + gateway + a `hermes serve` SSH session, and a
  Codex `app-server`, all at once. Each harness is watched in its own window.
- **Many tunnels.** Tailscale for the hub, SSH for Mac-side work, IAP forwards for
  the GCP worker (#741 added IAP loopback), per-harness SSH for Hermes.
- **Many secrets.** Hub auth is 21 static bearers in the Mac LaunchAgent plist
  (`UA_AGENT_TOKENS`), each copied by hand into a harness config on a device; 9
  were disclosed (#1211). LLM provider keys sit in per-device env files. Each MCP
  server a harness uses (hub, gbrain, codebase-memory, browser) is registered
  separately with its own credential.
- **Weak device proof.** The evidence chain cannot say which human and device
  authorized a login, because a bearer is a shared string. That is why the 30-day
  attribution gate is at 83.5% and #1296's demo anchor shows `human: unknown`.

## What other tools do (research, 2026-09-24)

| Pattern | Examples | What we take |
|---|---|---|
| One remote control for many local agents: a daemon on each machine, an encrypted relay, phone/web UI for prompts and approvals | Happy (open source, E2E encrypted, Claude Code + Codex), Omnara (mobile-first, wraps Claude Code/Codex), Claude Code Remote Control | The shape of Epic I: device daemon dials out, UI anywhere, permission prompts answered from the phone. Happy is a credible **adopt** option for the relay if our node proves heavy. |
| Local multi-agent managers: worktree per agent, board or cockpit | Conductor (Mac), Vibe Kanban, Claude Squad, Nimbalyst, Superset | Confirms worktree-per-lane (we have it). None of them governs, attributes, or spans hosts, which is our wedge. |
| A standard protocol for driving any coding agent | Agent Client Protocol (ACP): Gemini CLI (reference), Codex via `codex-acp`, Copilot `--acp`, OpenCode/Goose/Kiro `acp`, Claude Code via the community `claude-agent-acp` adapter (Anthropic closed native ACP as not planned, 2026-02-09) | Epic I drives harnesses through ACP instead of scraping terminals. One adapter layer, many harnesses. |
| Agent auth brokers: token vault, per-user delegated OAuth, credentials never enter the agent context | Arcade, Composio, Nango (source-available ELv2, self-hostable), Scalekit | J5/J7: the hub is the vault; agents call through it. Nango was the **adopt** candidate for the provider-OAuth catalog; the #1359 spike chose BUILD (see Build vs adopt). |
| MCP gateways: one endpoint, aggregated servers, central OAuth, per-tool policy, audit | AWS AgentCore Gateway, Composio gateway, Lunar MCPX, MintMCP | J8: the hub becomes each harness's only MCP entry. |
| MCP auth spec | 2025-11-25: Client ID Metadata Documents (CIMD) preferred over DCR; enterprise-managed authorization. 2026-07-28: stateless transport, closer OAuth/OIDC alignment | J4 (CIMD), J3 (IdP-backed consent). The hub already targets 2026-07-28. |
| Device sign-in | OAuth 2.0 Device Authorization Grant, RFC 8628 (`gh auth login`, `gcloud auth login --no-browser`, TV apps) | **J1, shipped here.** |
| Mesh networking | Tailscale Services GA with tsnet: an app joins the tailnet as its own node | I5 option A for off-tailnet reach. |

Sources: happy.engineering; omnara.co; agentsroom.dev/blog/best-multi-agent-coding-tools;
nimbalyst.com/blog/best-agent-management-tools-2026; zed.dev/blog/codex-is-live-in-zed;
casys.ai/blog/acpx-multi-agent-orchestration; github.com/NousResearch/hermes-agent/issues/569;
arcade.dev/blog/ai-agent-integration-platforms; developersdigest.tech (Arcade vs Composio vs Nango vs Stytch);
composio.dev/content/best-mcp-gateway-for-developers; descope.com/blog/post/developer-guide-mcp-gateways;
aws.amazon.com/blogs/machine-learning/how-agentcore-gateway-supports-the-mcp-2026-07-28-spec;
workos.com/blog/mcp-client-registration-cimd-vs-dcr; aaronparecki.com/2025/11/25/1/mcp-authorization-spec-update;
tailscale.com/blog/services-ga; RFC 8628.

## Target architecture

```
 phone / browser ──► Control Center (/ui on the hub)
                        │  Sessions · Approvals · Devices · Connectors · Evidence
                        ▼
                 ┌──────────────── hub (Mac) ─────────────────┐
                 │ OAuth AS (J1–J4)  ·  MCP gateway (J8)       │
                 │ token vault / managed secrets (J5, J7)      │
                 │ dispatch + approvals + evidence ledger      │
                 └───▲───────────────▲───────────────▲─────────┘
       outbound only │               │               │  (tailnet, or relay I5)
               ua-node (desktop)  ua-node (MBP)   ua-node (GCP / MBA)
               ├ ACP: claude-agent-acp, codex-acp, gemini/copilot/opencode
               └ executor backends: agy, pi
```

1. **One sign-in per device (J1).** `unified-agent login` shows a code; the operator
   approves it on his phone; the device gets a refreshable token bound to its
   tailnet node. No bearer is copied.
2. **One daemon per device (I1).** `ua-node` dials out to the hub with that
   token. No inbound ports, no per-harness tunnels. It reports host facts
   (the Devices pane stops guessing) and takes dispatch.
3. **Any harness through one protocol (I2).** The node is an ACP client. It
   starts sessions, streams plans/diffs/tool calls, and forwards
   `session/request_permission` into the hub approval queue.
4. **One pane (I3).** Sessions pane: pick device × harness, prompt, watch, approve
   from the phone, cancel, resume. Every step lands in the evidence ledger with
   the device node.
5. **One MCP entry (J8) and one config source (I4).** Harnesses see only the hub.
   The hub fronts gbrain, codebase-memory, browser and connectors, and renders
   each harness's rules/skills/MCP config from one bundle (proposal mode, red tier).
   **I4 shipped (task #1330):** versioned `harness_config` bundles on the hub;
   `ua-node` syncs drift into Approvals + vault Inbox; apply requires a one-shot
   `harness_config_apply` pass; MCP entries must use OAuth (bearer refused at
   publish); files land under `UA_HARNESS_CONFIG_ROOT`.
6. **Credentials live only on the hub (J5, J7).** "Connect GitHub / Google /
   Slack" runs OAuth once in the Connectors pane; provider tokens and LLM keys are
   managed secrets consumed through hub tools and the hub LLM endpoint.
7. **The human is verified (J3).** Consent and device approval move from a shared
   passphrase to a WorkOS session (enterprise) or tailnet identity plus passkey
   (private). The approval that signs a device in is then itself `human.verified`
   evidence, which is the structural fix for the attribution gate.

## Why this is on the Product 1 path, not a detour

Approval Ledger's promise is *named human → device → agent → action → evidence*.
Today the weakest links are human and device, and they are weak because agents
authenticate with shared bearers. Device sign-in with a verified approver closes
both links for every action the fleet takes, not only staged demo rows. The same
machinery is what a pilot customer needs to run their own agents without us
handing out tokens (Epic H).

## Build vs adopt

- **Build** the OAuth pieces (J1–J4, J6): the hub already has an RFC 8414/7591/9728
  AS; the increments are small and they carry our evidence semantics.
- **Build** ua-node on ACP (I1–I3): our value is governance and evidence around
  sessions, which a third-party remote control cannot record.
- **Evaluate adopt** for two parts before building: Happy's relay protocol for I5
  (E2E encryption is already solved there) and Nango for J5's provider OAuth
  catalog (source-available ELv2, self-hostable, avoids maintaining per-provider flows).
  Each gets a one-day spike with a written verdict in its task.
- I5 relay spike verdict (#1361): BUILD, borrowing Happy's crypto design only — [2026-09-24-spike-happy-relay.md](2026-09-24-spike-happy-relay.md).
- **J5 Nango spike (#1359): BUILD.** The free self-hosted edition is ELv2
  (source-available), auth-and-proxy only, and aimed at evaluation. It needs
  Docker, Postgres and Redis, and its image is amd64 only. For GitHub and
  Google the hub-side PKCE and refresh broker is about 700 lines. Verdict,
  citations and the conditions that would flip it to adopt:
  [2026-09-24-spike-nango-connector-oauth.md](2026-09-24-spike-nango-connector-oauth.md).

## Sequencing (stated assumption: Codex takes the heavy code lanes per the operator's 09-23 routing)

| Order | Task | Why first |
|---|---|---|
| 1 | J1 device sign-in (#1320) | Shipped here. Every other piece authenticates with it. |
| 2 | J2 persist OAuth store (#1321) | A restart must not sign devices out before anyone relies on it. |
| 3 | I1 ua-node (#1327) + J8 gateway (#1336) | Removes tunnels and collapses MCP config. |
| 4 | I2 ACP adapters (#1328) → I3 Sessions pane (#1329) | The one-pane experience. |
| 5 | J3 verified-human consent (#1322) | Red tier; the operator reviews. Unlocks attribution. |
| 6 | J5/J7 connectors + key broker, J4 CIMD, I4 config, I5 relay, J6/I6 rollout | Breadth and retirement of the old paths. |

Epic DoDs are in #1318 and #1319.

## Coordination fixes found in the same pass

These are independent of the new epics and make the existing multi-agent loop
cheaper and quieter:

- **#1337 claim-lease renewal.** Queue hygiene reopens a `doing` task once its
  1-hour lease lapses, even while the claimant is online and working. The agent
  reclaims it, which is the main cause of `task_reassignment_thrash` (11 tasks on
  2026-09-24, mostly one agent each) and of briefings calling live work
  "UNCLAIMED". Renew the lease when the claimant is live and the task was touched
  recently; release only abandoned claims.
- **#1338 review lane trusts exact-head CI.** The review cron re-runs the full
  pytest suite (up to 1800 s) on the hub host every 15 minutes even when Depot CI
  already passed at the same SHA. Use the CI result as the pytest gate; keep the
  cheap local gates.
- **#1333 review pipeline crash and false reject.** `reconcile` aborts on a None
  lookup; delivered tasks without a worktree still fail review.
- **#1339 silent_agent noise.** Idle lanes that heartbeat but hold no work are
  flagged daily as silent.
- **#1335 memory churn.** PR watching writes a new memory note per PR event.
- **#1316 orphaned lane children.** Already filed: codex sandbox and pytest
  processes outlive dead lanes.

## I8 Hermes profile-control spike — 2026-09-30 (#1392)

**Verdict: reuse native ACP through ua-node.** The installed Hermes source is
`NousResearch/hermes-agent@4cb28de2cd14fa8ee24f0f3e8fbff461f108a9a2`
(the task's v0.21.5 premise predates this checkout). Read-only source inspection
found `hermes -p PROFILE acp`: `hermes_cli/main.py` consumes the profile before
imports; `hermes_cli/profiles.py:resolve_profile_env` resolves an existing live
profile; `acp_adapter/entry.py` starts the stdio server. In
`acp_adapter/server.py`, `initialize`, `new_session`, `prompt`, `cancel`,
`load_session` and `resume_session` exist, session updates stream tool/text/usage,
and permission requests go through ACP. Native resume may create a new session
when its id is missing; the Control Center does not claim strict resume support.
No peer profile or live service was started during the spike.

The gateway, headless serve and A2A ports are unnecessary for this slice. Keep
node_dispatch, agent_sessions, agent.node events and acp_permission approvals as
the only control/evidence stores. Dispatch selects an explicit discovered
profile, including default, with safe argv tokens; no sticky-profile fallback.
Name-only discovery follows native identity-marker and tombstone rules, with
profile-directory symlinks refused at the node control boundary. The hub's
existing surface map supplies profile provenance. Unmapped or unknown labels
fail closed. Device authentication/queue ownership remains distinct from the
profile's mapped agent, and hub native policy checks both ceilings.

This controls new ACP sessions per profile, not the profile's already-running
gateway, dashboard chat or cron process. Restart is a fresh session with the
stored prompt and profile; an unconfirmed lease expiry cannot restart.
The actual native capability and supported Control Center contract are recorded
in [the harness control matrix](../harness-control-matrix.md). Live end-to-end
acceptance on each host remains an operator exercise after its ua-node explicitly
advertises acp:hermes; tests use synthetic profiles and a stdio ACP fixture.
