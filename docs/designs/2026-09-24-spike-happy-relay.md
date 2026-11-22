# Spike: Happy's relay for I5 off-tailnet reach (adopt or build)

Status: spike verdict, 2026-09-24. agent-claude, hub task #1361 (Epic I, I5).
Parent design: [Fleet Console and connector-style auth](2026-09-24-fleet-console-and-connector-auth.md#build-vs-adopt).

## Verdict

**BUILD. Borrow Happy's pairing and encryption design, adopt none of its code or protocol.**

1. **Option A first (per-node tailnet membership).** Any runtime that can run
   `tailscaled` in userspace mode, or a tsnet shim, joins the tailnet as a
   tagged, ephemeral node. On GCP it authenticates with workload identity
   federation, so no auth key is stored. This needs no relay code, and J1's
   existing device binding (tailnet peer to node) keeps working, as long as
   the node dials through its tailnet proxy and the hub is reached on its
   tailnet address directly (see Option A1).
2. **Option B only for runtimes that can run ua-node but cannot join the
   tailnet.** Today that possibly means cloud Agent1 sandboxes and pilot-customer
   hosts. Grok Bot is **not** a B target, because it runs no node; it stays on
   the OAuth connector. Option B is our own stateless ciphertext relay. The node
   and the hub both dial out to it. The relay never receives a hub credential:
   the J1 token travels only inside an end-to-end channel whose static device
   key the hub checks against the token. The crypto follows Happy's pattern.

Happy's server is a full sync backend: 22 Prisma models plus social, voice,
GitHub and billing features. It is not a relay. Its auth is account-wide and
has no working revocation. Every device in an account shares one content key.
Because of that it cannot give per-device attribution or accept our J1 token
without a deep fork.

## Premise check (what "off-tailnet" means today)

The I5 task text says the GCP Agent5 worker "today need[s] IAP/SSH forwards". That
is **partly stale**. `docs/cloud-agent5.md` (this repo, `cd8d54fb`, lines 17-19 and
24-33) records the worker `ua-worker` (`<worker-tailnet-ipv4>`) as a tailnet node. It
reaches the hub over Tailscale and serves tailnet-only. IAP is used for operator
SSH (#741) and for the planned cloud-mirror receiver VM (`docs/cloud-mirror.md`,
"Private GCE receiver through an IAP SSH local forward"). Neither of those is on
the node-to-hub path.

The runtimes that are actually off-tailnet:

| Runtime | Why it is off-tailnet | Can it join? |
|---|---|---|
| Grok Bot (xAI cloud VM) | Third-party host; "no tailnet access and no bearer token" (`docs/cloud-agent5.md:46-51`) | Not ours to install on. It speaks only OAuth 2.1 MCP to the hub (`docs/grok-bot-connector.md:3-7`) and runs no ua-node, so **neither A nor B applies**. It stays on the public OAuth connector unless xAI's VM can host a ua-node, and that has not been verified. |
| Cloud Agent1 sessions (Anthropic-hosted sandboxes) | Ephemeral, no persistent state, egress policy we do not control | **Unverified.** Userspace `tailscaled` over DERP/443 may work; it is part of the live test below. |
| Future pilot-customer nodes (Epic H) | Not our tailnet | Relay (B), or the customer's own tailnet. |

This does not shrink the task. It changes the sequencing: A covers every host we
control, and B is the fallback for hosts we don't.

## 1. The Happy protocol (source-level reading)

**Identity verified from primary sources.** "Happy" (happy.engineering) is GitHub
org `slopus`. The canonical code is the monorepo
[`slopus/happy`](https://github.com/slopus/happy), MIT (`LICENSE`), at commit
`8517ab232528a6046271d6010aaed663e1187dfc` (2026-09-22). `slopus/happy-server` and
`slopus/happy-cli` are **archived** (GitHub API `archived=true`). Their READMEs
say "This repository has been merged to the main one". All `happy:` references
below are paths in that monorepo commit. Link base:
`https://github.com/slopus/happy/blob/8517ab232528a6046271d6010aaed663e1187dfc/`.

### Transport

- JSON over HTTP (`/v1`, `/v2`), plus **Socket.IO** at path `/v1/updates`
  (websocket and polling transports), with CORS `*`
  (`happy:docs/protocol.md:5-8`).
- There are three socket scopes: `user-scoped` (app), `session-scoped` (one
  agent process), and `machine-scoped` (daemon) (`happy:docs/protocol.md:27-49`).
  The server authenticates the handshake token in middleware
  (`happy:packages/happy-server/sources/app/api/socket.ts:55-94`).
- **Both ends dial out.** The CLI daemon and the phone app are both clients of
  the server. The server is the only listener. That matches our outbound-only
  requirement in shape.
- Point-to-point control uses **RPC over rooms**. A daemon joins room
  `rpc:<userId>:<method>`. A caller emits `rpc-call`, and the server forwards it
  as `rpc-request` with ack and a 30 s timeout
  (`happy:packages/happy-server/sources/app/api/socket/rpcHandler.ts:7-30,128-138,160-220`;
  `happy:docs/realtime-sync-and-rpc.md:50-60`).
- Multiple replicas need Redis Streams (`socket.ts:22-46`).

### What the server sees

**Ciphertext** for session messages, session metadata and agent state
(including pending permission requests), machine metadata and daemon state,
artifacts, KV values, and access keys (`happy:docs/encryption.md:157-245`). RPC
params and results are encrypted by the endpoints
(`happy:packages/happy-cli/src/api/rpc/RpcHandlerManager.ts:73-94`).

**Plaintext metadata** the server can see:

- The account id (CUID) on every socket, plus session ids, machine ids and
  every update's `seq`, timestamps and version numbers
  (`happy:docs/encryption.md:520-543`).
- **RPC method names**, as `<sessionOrMachineId>:<method>`
  (`RpcHandlerManager.ts:137-139`). They reveal the action: `bash`,
  `readFile`, `writeFile`, `permission`, `spawn-happy-session`, `stop-daemon`,
  `abort` (`happy:packages/happy-cli/src/modules/common/registerCommonHandlers.ts:176,262,282`;
  `happy:packages/happy-cli/src/claude/utils/permissionHandler.ts:377`;
  `happy:packages/happy-cli/src/api/apiMachine.ts:185-334`).
- **Usage reports**: `{ key, sessionId, tokens, cost }`
  (`happy:packages/happy-server/sources/app/api/socket/usageHandler.ts:12`).
- Presence and "thinking" ephemerals (`happy:docs/protocol.md:118-122`).
- Third-party tokens (GitHub, OpenAI, Anthropic, Gemini) are encrypted **with a
  server key**, not end-to-end (`happy:docs/encryption.md:486-518`).

It is honest E2E for content. The metadata leak is roughly what our own
cloud-mirror allowlist already accepts. The exception is that method names
disclose shell and file-write activity.

### Key exchange and pairing

- **Account root.** A client keypair signs a challenge to `POST /v1/auth`. The
  server upserts the Account by public key and issues a token
  (`happy:docs/user-identity.md:36-53`;
  `happy:packages/happy-server/sources/app/api/routes/authRoutes.ts:12-37`).
- **Terminal pairing (the useful part).**
  1. The CLI generates an ephemeral X25519 `box` keypair and posts the public
     key to `POST /v1/auth/request`
     (`happy:packages/happy-cli/src/ui/auth.ts:28-38`).
  2. It shows a QR code `happy://terminal?<base64url pubkey>`, or a web URL
     (`auth.ts:97-109`).
  3. The already-signed-in phone encrypts the account secret to that public key
     with `box` and an ephemeral sender key. The payload is the legacy 32-byte
     secret or, in v2, `0x00 ‖ contentPublicKey`. The phone posts it to
     `/v1/auth/response` (`authRoutes.ts:127-...`).
  4. The polling CLI receives a server token plus the sealed secret and opens
     it (`auth.ts:145-212,234-245`).
  5. The server only relays the sealed blob.
- **Content crypto.** There are two variants
  (`happy:docs/encryption.md:64-155`;
  `happy:packages/happy-cli/src/api/encryption.ts:62-112`):
  - **legacy**: `tweetnacl.secretbox` (XSalsa20-Poly1305, 24-byte nonce)
    under one shared 32-byte account secret.
  - **dataKey**: AES-256-GCM with a per-session or per-machine data key. The
    data key is wrapped with `box` to the account's content public key (layout
    `ver ‖ nonce12 ‖ ct ‖ tag16`).

  There is no forward secrecy and no per-message ratchet.

### Identity and auth model

- **One identity per account, not per device.** Tokens are `privacy-kit`
  persistent tokens signed from `HANDY_MASTER_SECRET`
  (`happy:packages/happy-server/sources/app/auth/auth.ts:36-45,67-85`). A
  terminal pairing mints `createToken(accountId, { session: authRequestId })`
  (`authRoutes.ts:78`). That is the only per-device trace, and it is a
  server-side id, not a device key.
- **No effective revocation.**
  - `invalidateToken` and `invalidateUserTokens` only evict the in-memory
    verify cache (`auth.ts:141-155`). A later cache miss re-verifies the
    signature and accepts the token again (`auth.ts:88-139`).
  - `grep` finds **no caller** of either function under
    `packages/happy-server/sources`.
- **Devices in an account are mutually trusted.**
  - `rpc-register` joins `rpc:<userId>:<method>` for any method string the
    socket names (`rpcHandler.ts:130-138`). Any token for the account can
    therefore register another machine's prefixed method.
  - With the legacy variant, every paired device holds the same content secret.
  - Machine and session writes are ownership-checked by `accountId` only
    (`sessionUpdateHandler.ts:27`, `machineUpdateHandler.ts:71`).

### Message schema

- `@slopus/happy-wire` is a publishable (MIT) zod schema package: session
  envelopes and `new-message`, `update-session` and `update-machine` bodies
  (`happy:docs/happy-wire.md:1-60`).
- Decrypted agent content is `{ role, content: { type: output|codex|acp|event, data }, meta }`,
  and agent state carries `requests` and `completedRequests` for permission
  prompts (`happy:docs/encryption.md:357-428`).
- The schema is **coupled to Happy's product model**. It is built around
  sessions, machines, artifacts, KV, feed and friends, not a generic
  envelope-routing protocol. The protocol's design rule is to encode new
  capabilities inside existing encrypted primitives rather than add routes
  (`happy:docs/protocol.md:10-22`).

**Reusable?** No, not as a relay protocol. It is a Happy-app sync protocol with
a relay-like RPC side channel. The reusable ideas are the pairing handshake and
the opaque-blob and RPC-room routing.

## 2. License, self-hosting, footprint

| | Finding | Source |
|---|---|---|
| License | MIT for the monorepo and for `happy-server` (`"license": "MIT"`). The archived split repos report no license. | `happy:LICENSE`; `happy:packages/happy-server/package.json` |
| Hosted default | `happy-api.slopus.com` | archived `slopus/happy-server` README |
| Production deps | Node (Fastify + Socket.IO), **Postgres** (Prisma), **Redis** (startup `ping`, required), **S3/MinIO** (falls back to local files if `S3_HOST` is unset). `HANDY_MASTER_SECRET` is required. | `happy:docs/deployment.md:5-34` |
| Self-host mode | `happy server` / `happy-server-self-host`: embedded **PGlite**, local file uploads, no Postgres, Redis or S3 | `happy:packages/happy-server-self-host/README.md`; `happy:packages/happy-server/sources/standalone.ts:24-25,116-118,210-213` |
| Size | ~13.9k lines of non-test TypeScript in `happy-server/sources`, 22 Prisma models, 40 migrations. Also bundles ElevenLabs voice, RevenueCat, Octokit/GitHub app, social feed and friends. | local `wc`/`grep` on the clone |
| Image | `Dockerfile.server` bundles FFmpeg and Python for media processing | `happy:docs/deployment.md:51-56` |

Self-hosting is feasible and permissively licensed. The footprint is a product
backend, not a relay.

## 3. Fit against our requirements

| Requirement | Happy as-is | Notes |
|---|---|---|
| Outbound-only from both ends | **Yes** | Both CLI and app are Socket.IO clients. The server still needs a public or tailnet listener, the same as any relay. |
| Auth with our J1 device token, not Happy accounts | **No** | Auth is `privacy-kit` tokens from `HANDY_MASTER_SECRET` plus public-key account upsert. Using J1 means replacing `auth.ts`, `authRoutes.ts` and the socket middleware: a fork, not a config change. |
| Relay sees only ciphertext or metadata | **Mostly** | Content is E2E. Method names (`bash`, `writeFile`, `permission`), usage and cost, and ids are plaintext. Vendor tokens are server-key encrypted, not E2E. |
| Approval prompts and evidence reach the hub unmodified | **No** | Permission requests live in encrypted `agentState` and are answered by the **phone** over the `permission` RPC. For the hub to see them it would have to hold the account content key and act as a Happy client. That puts our approval gate behind a third-party state machine. It is the governance-around-sessions value the parent design says we must own ("Build ua-node on ACP"). |
| Per-device attribution for the evidence ledger | **No** | One account identity. The per-device trace is a server-side auth-request id. No revocation. Devices share the legacy content secret and can register each other's RPC methods. |

## 4. Options compared

A premise that matters for every option is where J1's device binding comes from
today. The hub binds a device grant to the **direct peer's tailnet node**
(`src/unified_agent/oauth.py:121-139,907-953`, `_peer_ip` at `:1594-1598`
deliberately ignores forwarded headers; `src/unified_agent/tailnet.py`). The
node identity is resolved with `tailscale whois` and fails closed to `None`.
Anything that puts a relay between node and hub makes the direct peer the relay,
or nobody when the hub dials out. **A relay path therefore needs a new binding
source.**

### Option A: Tailscale (tsnet / Services)

The primary sources say:

- Tailscale Services went GA on 2026-02-19. A tsnet app "can join your tailnet as
  its own device", and every plan, including Personal, gets up to 10 services
  (<https://tailscale.com/blog/services-ga>).
- `tsnet.Server.ListenService` advertises a node as a Service host. The host must
  be a **tagged** node and run v1.86.0 or later
  (<https://tailscale.com/docs/features/tsnet/how-to/register-service>,
  <https://pkg.go.dev/tailscale.com/tsnet>).
- Workload identity federation also went GA on 2026-02-19 for GCP, AWS and
  GitHub, on all plans. It lets nodes and tsnet join with a short-lived
  cloud-provider OIDC token instead of a stored auth key
  (<https://tailscale.com/blog/workload-identity-ga>,
  <https://tailscale.com/docs/features/workload-identity-federation>).

There are two sub-shapes:

- **A1: the node joins the tailnet (recommended).**
  - ua-node runs next to `tailscaled --tun=userspace-networking`, which needs no
    root and no TUN, or next to a tiny Go tsnet sidecar. ua-node is Python and
    cannot embed tsnet directly.
  - The node is tagged `tag:ua-node` and ephemeral, and uses WIF on GCP.
  - J1 binding works unchanged, **with two conditions**, because the node is
    the direct tailnet peer:
    - In userspace mode the node has no tailnet interface. ua-node's hub
      client must send through `tailscaled`'s local proxy
      (`--outbound-http-proxy-listen` or `--socks5-server`), or through the
      tsnet sidecar's dialer. Traffic sent that way leaves over WireGuard from
      the node's own tailnet IP.
    - The hub must keep receiving on its tailnet address **directly**. If
      Tailscale Serve fronts it, the direct peer becomes loopback, and
      `_peer_ip` ignores forwarded headers by design (`oauth.py:1594-1598`).
      Then `device_node` would be `None`.
    T2 step 2 verifies both.
    There is no public ingress anywhere.
  - Effort: **~1-2 days.** That covers a unit or sidecar template, ACL grant and
    tag, the WIF trust setup and a runbook. The hub needs no change.
  - It does not help hosts we cannot install on.
- **A2: a relay as a tsnet Service node.**
  - The relay sits on the tailnet, but off-tailnet nodes still reach it over the
    public internet. It is Option B with a tailnet-side leg.
  - It inherits B's binding problem and adds a Go component. It only beats B if
    the hub must never dial out.

### Option B: our own stateless ciphertext relay

The in-repo precedent is the stateless public TLS forwarder in
`deploy/gcp/slack-gateway/forwarder.py` (#558), which has no store and forwards
to the hub over Tailscale. The task's "D5 forwarder #1205" was not found in this
repo at `cd8d54fb`.

`cloud_mirror_receiver.py` is **not** a good base. It is a one-way, metadata-only,
SQLite-backed ingest with no path back to the hub (`docs/cloud-mirror.md`,
"Acceptance mapping"). A relay needs bidirectional, stateless streaming.

- **Shape.**
  - A Starlette WebSocket rendezvous. Starlette is already a dependency.
  - It runs on Cloud Run, or on a separate small VM, **not** on `ua-worker`,
    whose boundary says no public ingress (`docs/cloud-agent5.md:38-40`).
  - The node dials `wss://relay/n/<node_key_id>` and the hub dials
    `wss://relay/h`. **Both are outbound.** Nothing about the hub is reachable
    from the internet.
  - **The relay never sees a hub credential.** The relay admits a node leg by
    a signature over a relay-issued nonce, made with the node's device key.
    The hub pushes the set of admitted device public keys (not tokens) to the
    relay over the hub's own outbound leg, and admits its own leg with a
    relay-only secret. The relay pairs legs by key id and forwards opaque
    frames. The J1 bearer is sent only **inside** the end-to-end channel,
    straight to the hub.
  - It holds no storage, so a restart drops live sessions and nodes reconnect.
- **E2E (borrowed from Happy's pairing).**
  - During `unified-agent login` the node generates a per-device X25519 keypair
    and sends the public key with the RFC 8628 request. This is Happy's
    `/v1/auth/request {publicKey}` step.
  - The approval page shows the key fingerprint next to the user code.
  - The issued token carries a confirmation claim (`cnf`, in the style of
    RFC 9449 DPoP). **That key becomes the device binding when there is no
    tailnet peer.**
  - **Proof of possession.** A `cnf` claim alone identifies a key; it does not
    prove the caller holds it. The end-to-end handshake is therefore mutually
    authenticated with static keys, in the style of Noise KK: the node's static
    X25519 key and the hub's pinned static key. The hub accepts a J1 token on a
    relay channel only when the handshake's authenticated node static key
    matches the token's `cnf`. A copied token without the device private key
    cannot finish the handshake, so it cannot act as that device or write
    evidence under its name.
  - The hub has a static X25519 key that is pinned at login.
  - Frames are X25519 ECDH → HKDF → ChaCha20-Poly1305 with a per-connection
    ephemeral, which gives forward secrecy that Happy's static keys do not.
    Everything comes from the existing `cryptography>=42` dependency, so no new
    crypto dependency is needed.
  - The relay sees only key ids, frame sizes and timing. Method names are not
    visible, unlike Happy.
- **Approvals and evidence.** ua-node's ACP `session/request_permission` and its
  evidence events are ordinary hub calls tunnelled end to end. The hub records
  them with `device_node` taken from the `cnf` key, so they reach the approval
  queue and ledger unmodified.
- **Effort: ~5-8 days.**

  | Piece | Estimate |
  |---|---|
  | Relay | ~300-500 lines + tests, 1.5 days |
  | Node and hub dial-out legs with reconnect/backoff | 1.5 days |
  | Key-bound J1: `cnf` claim, fingerprint on the approval page, binding fallback in `oauth.py` | 1.5-2 days, red tier (auth) |
  | Deploy template + runbook | 1 day |
  | Live acceptance | 0.5 day |

### Adopt Happy (for completeness)

- Fork `happy-server` and replace the auth layer with J1 introspection.
- Strip voice, billing, GitHub and social.
- Add per-device keys and real revocation.
- Write a Python client for Socket.IO and the Happy crypto in ua-node.
- Teach the hub to act as a Happy client so approvals land in our queue.

That is **~3-4 weeks**, plus a permanent fork of a fast-moving TypeScript
product repo. The last push was 2026-09-22, and the protocol doc promises
additive changes only. It still would not give forward secrecy or per-device
content keys.

| | Hub public ingress | J1 token | Relay sees | Per-device attribution | New code | Effort |
|---|---|---|---|---|---|---|
| **A1** node on tailnet | none | as-is | nothing (WireGuard) | as-is (tailnet peer) | config/templates | 1-2 d |
| **B** own relay | none (relay is public) | + `cnf` key, checked in handshake | key ids, signatures, sizes, timing | `cnf` key → device | relay + legs + binding | 5-8 d |
| A2 relay as tsnet Service | none | + `cnf` key | same as B | same as B | B + Go | 7-10 d |
| Adopt Happy | none (Happy server is public) | fork auth | ids, method names, usage | none without fork | fork + Python client | 3-4 wk |

Related but not chosen:

- `slopus/murmur` is an MLS (RFC 9420-style), forward-secret, per-device-inbox
  library with a ciphertext-only relay. Its packages declare MIT. It is the
  stronger crypto design and is worth re-reading if B ever needs group fan-out.
  For one node-to-hub pipe it is more than we need.
- `slopus/opentunnel` (last commit 2020) exposes a public endpoint per tunnel,
  which is the opposite of "no public ingress".

## Flip conditions

- **To ADOPT the Happy protocol:**
  - Happy ships per-device keys, server-side token revocation, and pluggable
    auth that could accept an external OAuth introspection endpoint; **and**
  - we decide to use the Happy mobile app as the phone UI instead of the
    Control Center (I3).
  Short of both, adopting moves the approval gate out of the hub.
- **To make B primary instead of A1:** the live test shows userspace
  `tailscaled` cannot reach DERP from cloud Agent1 sandboxes, **and** those
  sandboxes become a main I5 target; or Tailscale ephemeral-node or plan limits
  block per-worker nodes.
- **To choose A2 over B:** a hard rule appears that the hub must never make an
  outbound connection to a non-tailnet host.

## Live test plan (not run in this spike)

**Not executed here.** The constraints for this session were no Docker on the
host, no installing or running third-party code, and no account creation.
Everything above is source-level reading of `slopus/happy@8517ab2`, plus Tailscale
primary docs. Each test below records its commands and results in the task.

**T1: Happy confirmation (optional; only if someone challenges the source reading).**

- **Setup.** An isolated throwaway VM (not the hub, not `ua-worker`) running
  `happy server` (self-host, PGlite), with the CLI pointed at it through
  `settings.serverUrl`.
- **Session.** Pair one CLI through the web auth URL and run one session that
  triggers a permission prompt and one `bash` RPC.
- **Check:**
  1. Every `SessionMessage.content` row in PGlite is `{t:"encrypted"}`.
  2. The server log and socket frames show `<id>:bash` and `<id>:permission`
     method names in plaintext.
  3. `usage-report` tokens and cost are plaintext.
  4. After calling `invalidateToken` and then restarting, the old token still
     authenticates. This confirms there is no revocation.
  5. A second paired token for the same account can `rpc-register` the first
     machine's prefixed method, and it receives that machine's calls.

**T2: A1 acceptance (the one that matters first).**

1. On a fresh GCE VM with no Tailscale state, run userspace `tailscaled`
   joined through **workload identity federation**, tag `tag:ua-node`,
   ephemeral. Check that no auth key is stored on disk.
2. Run `unified-agent login` against the hub. Check that the device grant
   audit row `hub.oauth.device_authorization` carries this VM's `device_node`,
   meaning the tailnet binding resolved.
3. Run one outbound-only ACP session. Check the approval prompt in the `/ui`
   queue, and an evidence row whose device equals the VM node.
4. Scan the VM with `ss -tlnp`: there must be no public listener. Check that
   `tailscale funnel status` reports nothing.
5. Delete the VM and check that the ephemeral node disappears from the tailnet.
6. Repeat step 1 inside a cloud Agent1 session sandbox to answer the open
   question in the premise check.

**T3: B acceptance (build only if T2 step 6 fails, or a node-capable host outside the tailnet is in scope).**

1. Deploy the relay to Cloud Run. The node and the hub each dial out. There is
   no inbound rule on the hub or the node.
2. Capture the relay's received frames and logs. Assert that no prompt text,
   tool name, method name or token appears; plant a canary string in a prompt
   and grep for it.
3. Sign the device in with a `cnf` key. Check that the approval page shows the
   fingerprint and that the evidence row's device comes from the key. Then copy
   the token to a second host that has a different device key. Its relay
   admission or its end-to-end handshake must fail, and no call is accepted
   under the first device.
3a. In the relay-side capture from step 2, assert that the J1 access and
    refresh tokens never appear, including during admission.
4. Replay a captured frame. It must be rejected (AEAD nonce and sequence).
5. Kill the relay mid-session. The node reconnects, and the session resumes or
   fails closed; no approval may be auto-granted.
6. Revoke the device in the Devices pane. The hub removes the key from the
   relay's admitted set, the relay leg drops, and the hub rejects the token on
   any channel.

## Recommended follow-ups (for the orchestrator to file)

- **I5a (A1):** a `ua-node` userspace-Tailscale + WIF template and runbook, with
  the T2 acceptance above. Unblocked now; it does not depend on I1 code beyond
  the node itself.
- **I5b (B, conditional on T2 step 6 or on a node-capable off-tailnet host):**
  the stateless relay with key-bound J1 (`cnf`) and the static-key handshake.
  The auth change is red tier.
- Grok Bot stays on the OAuth connector. Revisit only if xAI's VM can run a
  ua-node.
- Correct the I5 task text: the GCP Agent5 worker is already a tailnet node.
