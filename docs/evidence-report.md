# User-Attributable Agent Evidence Report

Task #781 (Phase 3.1) establishes the core wedge report surface for proving which human is accountable for what an AI agent did.

It traces the five-link accountability chain:
```
human -> device -> agent -> action -> proof
```

The report is read-only and may be emitted as JSON, Markdown, text, CSV, or PDF.
`human_provenance` preserves inferred candidates and their sources. A signed
authenticated-producer event present on the hash chain, whose HMAC verifies
against the hub's existing bearer for that identity (task #1155 / audit gap
12), settles who the *actor* was (`value`/`source`, and a `verified: true`
candidate) — but that alone is **not** what makes `human_provenance.verified`
true. As of task #1217, `verified` flips true only when the identity that
signed is itself classified `"human"`; since task #1673 that state is named
`human_authority: "credential-attributed"` (see "Actor identity vs. human
authority" below). A
uniquely-verifying *agent* signature (e.g. `agent-codex`) is preserved as
real actor evidence but leaves `verified` false. Without any verifying
producer event — or if the signature is missing, tampered, or the bearer
mapping is gone — verification also stays false (fail-closed).
`chain_integrity.verified` proves event hash-chain integrity alone.
`actor_authentication_verified` flips true once a signed producer contract is
satisfied for the report's in-scope records; `provenance_verified` requires
that *and* a verified human identity behind it.
Report-level gate: `attribution_coverage` requires ≥95% of done completions in
the trailing window to carry `claimed_by` (empty window → `no_data`, not pass);
failure is named `attribution_coverage_below_threshold` and clears `clean_pass`
alongside any failed evidence-chain integrity check. `clean_pass` is true only
when the chain verifies and coverage passes (task #1139 — does not block task
completion).

This coverage is **platform-wide for the selected time window**, independent of
the report's `task_id`, `project_id`, `project`, agent, or PR record filters.
The Markdown and text exports label that scope. A task-filtered report can
therefore show one fully verified row while the platform-wide coverage gate
still fails, or show `clean_pass=true` while that row's human or device link is
unverified. `clean_pass` summarizes ledger-chain integrity and platform
`claimed_by` coverage; it is not a per-row human/device attestation. For the
five-link chain, inspect each record's `human_provenance.verified` and
`device_link.verified` fields as well as the report-level gates.

### Actor identity vs. human authority (task #1217)

A verified HMAC signature proves which *credential* produced a claim — it does
not by itself prove a *human* produced it. Every fleet agent label (e.g.
`agent-codex`, `agent-hermes`) signs with exactly the same
`hmac-sha256-v1` scheme a human operator (`Desktop_Person`) does. Before task
#1217, a uniquely-verifying signed producer was unconditionally promoted to
`human_provenance.verified = true` regardless of whether the signing identity
was an agent or a human — an accountability misstatement (an agent's valid
signature is not evidence that a human authorized anything).

`human_provenance` now carries the distinction explicitly:

| Field | Meaning |
| --- | --- |
| `value` / `source` | The strongest available *actor* evidence — unchanged in shape, still the signed producer identity when one verifies, otherwise the best inferred candidate. Never discarded, even when it is an agent. |
| `actor_kind` | `"human"`, `"agent"`, `"unknown"`, or `"conflict"` — what kind of identity `value` names. Classification is source-aware (task #1217 review finding): a signed `evidence.producer_authenticated` candidate uses `config.actor_kind_for_identity()`, whose open-roster fallback treats any well-formed label as `"agent"` — safe there because the identity came through the agent bearer infrastructure. Every other candidate (an explicit `[human: ...]` note, `UA_OPERATOR_NAME`/`UA_HUMAN_IDENTITY`, an approval `resolved_by`, a delegation `from_agent`) uses the stricter `config.actor_kind_for_claim()`, which only returns `"agent"` for a *concrete governed* identity — a catalogued fleet label or a name in the operator's own `UA_AGENT_LABELS` roster (`config.is_governed_agent_identity()`) — never merely because a value happens to match the label syntax. This keeps an ordinary human name like `UA_OPERATOR_NAME=Alice` classified as human-or-unknown evidence instead of a false `"agent"`, while `[human: agent-codex]` (a real fleet label) still resolves to `"agent"` so the contradiction with the human claim surfaces as a `conflict`. |
| `human_authority` | What the evidence proves about a human (task #1673). `"credential-attributed"` when a credential bound to a **human** identity (an agent bearer HMAC or a hub-verified IdP session) signed the claim: it proves which credential was used, **not** that a human was present. `"presence-verified"` is reserved for a statement-bound passkey assertion (future H10) and is not emitted yet. `"unknown"` when the evidence does not establish a human (this includes a cleanly verified *agent* signature); `"conflict"` when the evidence disagrees with itself. Schema 1.1 to 1.6 wrote `"verified"` for credential attribution; readers map it to `"credential-attributed"`. |
| `credential_attributed` | Boolean, true exactly when `human_authority` is `credential-attributed` or `presence-verified` (task #1673). |
| `verified` | Kept for backward compatibility; same value as `credential_attributed`. It is the human-credential bit, not an actor-authentication bit, and it never meant a present human. New consumers should read `human_authority`. |
| `candidates` | Every inferred/signed candidate considered, each now also tagged with its own `actor_kind`. |

A verified *agent* signature is still real, valuable evidence — it is
preserved as the record's `human` value and as a `verified: true` candidate
with `actor_kind: "agent"` — but it is surfaced as unresolved human authority,
not a false "verified human" claim. The WorkOS/IdP integration landed in task
#1249 (Epic E, E2 — see "IdP-verified producer class" below): a producer
event stamped under a verified WorkOS session fills `human_authority:
"credential-attributed"`, marked `verified_via: "idp"`. An IdP session is a
stronger credential than a bearer, but it is still a credential: neither
proves a human was present for the action.

## Authenticated producer contract (task #1155)

When an approval is resolved or a delegation completes under a **verified** hub
identity that has a bearer in `UA_AGENT_TOKENS`, the write path appends an
`agent.evidence.producer_authenticated` event containing:

| Field | Meaning |
| --- | --- |
| `identity` | Verified agent/human label (never a secret) |
| `kind` | `approval` or `delegation` |
| `ref_id` | Approval or delegation id |
| `task_id` | Task id when known (links the event into the report) |
| `scheme` | `hmac-sha256-v1` |
| `signature` | HMAC-SHA256 hex digest over the canonical claim, keyed by that label's existing bearer |
| `device` | Present only when the request's peer resolved to a Tailscale node (task #1250): `{node, node_id?, user?, resolved_at}` — the hub-resolved node identity, never client-supplied data |

Verification recomputes the HMAC with `resolve_agent_bearer_token(identity)` and
requires the event's `entry_hash` to sit on a valid chain. Fail-closed cases
(no stamp, no flip):

- Caller not authenticated (`verified_agent_identity()` is unset)
- Identity has no bearer in `UA_AGENT_TOKENS` (e.g. OAuth/cookie-only label
  without a static token — cannot be re-verified at report time)
- Missing/unknown `scheme`, missing signature, or HMAC mismatch
- Hash chain not verified

Reports and logs never include bearer values — only the identity label and the
opaque signature hex.

### IdP-verified producer class (task #1249 / Epic E, E2)

When the resolver authenticated through the hub's WorkOS leg
(`Authorization: WorkOS <sealed-session>` → `workos_hub_auth.resolve` — JWKS,
exact issuer, the one configured organization, `sid`/`sub` agreement, and a
live membership/session re-check), the producer event uses scheme
`workos-idp-v1` instead. The human label needs **no** bearer — this closes the
v1 hole above — and the event carries the verified *claims*, never the
credential:

| Field | Meaning |
| --- | --- |
| `identity` | The UA label the WorkOS subject mapped to (the human identity) |
| `workos_subject` | IdP user id (`sub`) |
| `workos_organization_id` | IdP organization id (`org_id`) |
| `workos_session_id` | IdP session id (`sid`) |
| `workos_expires_at` | Session expiry (unix seconds) at stamp time |
| `signature` | HMAC-SHA256 over the canonical IdP claim, keyed by the leg's configured `WORKOS_COOKIE_PASSWORD` |

Verification recomputes the HMAC with the leg's configured secret **and**
re-checks the claims against current config — `workos_organization_id` must
equal `UA_HUB_WORKOS_ORGANIZATION_ID` and `workos_subject` must still map to
`identity` in `UA_HUB_WORKOS_IDENTITY_MAP` — so a wrong org, a missing or
rotated mapping, an unconfigured leg, or a tampered signature fails closed.
Each verified producer (and the provenance result it settles) carries
`verified_via`: `"idp"` for this class, `"agent_bearer"` for `hmac-sha256-v1`.
A human-attributed link renders as `credential-attributed-by-idp` (WorkOS
session) or `credential-attributed-by-agent_bearer` (bearer) in Markdown and
text output; anything else renders `unattributed`. Signed candidates are
marked `signed`. See [provenance-graph.md](provenance-graph.md) for the `provenance_graph` appendix schema.

## The 5-Link Chain

| Link | Definition | Source of Truth |
| --- | --- | --- |
| **`human`** | Accountable-person candidate, or `unknown` / `conflict`. Verified only via a signed producer event. | Approval `resolved_by`, delegation `from_agent`, task notes, configured operator environment, or `evidence.producer_authenticated` when the HMAC verifies; no implicit the operator fallback. |
| **`device`** | Host / node where execution took place. As of task #1250, an authenticated producer event stamped on a request whose peer resolves to a Tailscale node carries the node identity; when verified producer events agree on exactly one node the record's `device` renders as `<node> (verified)` and `device_link` carries `{verified: true, node, node_id?, user?, resolved_at}`. A bare hostname stays `unverified-device:<host>` (`device_link.verified: false`) — hostname data is never promoted to a verified device; disagreeing nodes render `unverified-device:multiple-nodes`. `unattributed` when neither is present. | `tasks.source_host` or event payload `host`/`source_host`; verified `device` claim on `evidence.producer_authenticated` events wins when present |
| **`agent`** | AI agent identity executing the work | `tasks.claimed_by`, notes `claimed-by: <label>`, event `originator` |
| **`action`** | Lifecycle transition, mutation, or task execution | Task title, status, priority, lane, and associated approvals |
| **`proof`** | Durable references and ledger integrity | `evidence_ref` (PR URL / commit SHA), matched `event_log` hashes, and an unverified `merge_claim` from notes. `tamper_evident` requires a valid chain AND at least one associated hashed event; hash-chain integrity does not authenticate an actor or prove a merge. A task whose PR the Approval Ledger settled as policy-authorized also carries `merge_authorization` (see below); every other record omits the key. |

The `agent.gateway.decision` events behind the **`action`** link record what
each tool call was asked to do, not only that it ran (task #1907): an
`args_sha256` of the supplied arguments and, for the main mutating tools, a
redacted `args_summary` (the `run_cmd` argv and cwd, or the path and content
sha256 of a write). A mutating call whose event cannot be written fails. See
[Security model](security.md#tool-call-evidence-task-1907). A read-only call's
ALLOW is recorded in an hourly `agent.gateway.decision.rollup` event with a
`count` instead (task #1922), so a call count sums `count` over rollups; see
[Read-only allows are coalesced](security.md#read-only-allows-are-coalesced-task-1922).

---

### Policy-authorized merges (task #1407)

When the Approval Ledger settles a merged pull request as policy-authorized
(see [connector-activation.md](connector-activation.md#policy-authorized-merges-task-1407)),
the task's `proof` gains a `merge_authorization` object:
`evidence_class: "policy-authorized"`, `human_approval: false`, the cited
`policy_ref`, `authorized_by` (the verified agent label that recorded it), the
`repo`, `number` and exact `head_sha`, the `authorization_id` and
`outcome_event` ledger ULIDs, `recorded_by`, and `tamper_evident`. It is never
folded into `human_provenance`: a policy citation is not human authority, and
`human_authority` stays whatever the human evidence says. Markdown renders it
as `merge authorization: policy-authorized (<policy_ref> by <label>; not a
human approval)`; text output adds `merge_authorization=policy-authorized (not
a human approval)`. CSV and PDF columns are unchanged.

## CLI Usage

Run directly via `unified-agent evidence` or `unified-agent audit evidence`:

```bash
# Report for a specific task as JSON
unified-agent evidence --task 777 --json

# Report for a task as a Markdown table
unified-agent evidence --task 777 --markdown

# Filter by executing agent
unified-agent evidence --agent agent-cursor --limit 10

# Filter by time window (ISO 8601)
unified-agent evidence --since 2026-09-01 --until 2026-09-06

# Save report directly to a file
unified-agent evidence --task 777 --markdown --out /path/to/evidence.md

# With signing configured, also writes /path/to/evidence.md.sig.json
export UA_EVIDENCE_SIGNING_KEY='operator-protected-value'
export UA_EVIDENCE_SIGNING_KEY_ID='release-2026-09'
unified-agent evidence --task 777 --json --out /path/to/evidence.json
unified-agent evidence verify /path/to/evidence.json

# Project chain demo artifact (task #844): spec, nested steps, merged PRs,
# last merge, and inline audit-verify. --project here is a task ID, not a name.
unified-agent evidence report --project 819
unified-agent evidence report --project 819 --json
unified-agent evidence report --project 819 --out docs/samples/project.md
```

`--project` on `unified-agent evidence` still filters by project *name*.
`unified-agent evidence report --project <id>` selects a `[project-spec]` root
and renders `project_chain`. Markdown is the default for `report`; JSON is
opt-in. The artifact never upgrades an unverified PR to merged. A
representative 2026-09-03 render lives at
`docs/samples/2026-09-03-project-evidence-report.md`. The PROOF-C2 attribution
sweep (UA #849) and a zero-unattributed render live at
`docs/samples/2026-09-09-proof-c2-attribution-sweep.md` and
`docs/samples/2026-09-09-proof-c2-project-evidence.md`.
The Control Center Board copies that same command onto each `[project-spec]`
card (task #847) so an operator can paste it without leaving `/ui`.

### Detached export signatures

Set `UA_EVIDENCE_SIGNING_KEY` only in the protected service environment. The
optional `UA_EVIDENCE_SIGNING_KEY_ID` labels the active key. For the managed
secret path, set `UA_EVIDENCE_SIGNING_SECRET_NAME` to a secret stored with
consumer `evidence-signing`; the secret value never enters the report, sidecar,
task notes, or response headers. The current portable fallback is
`hmac-sha256-v1`; the HMAC key is required when verifying.

When signing is configured, an output file gets a sibling `<file>.sig.json`
sidecar. The sidecar records the exact artifact SHA-256, signature, key id,
chain head, and report generation time. `unified-agent evidence verify <file>`
returns `0` for a valid untouched artifact, `1` for a digest/signature failure,
and `2` for missing files, malformed sidecars, or bad arguments. `--sig` selects
an alternate sidecar. `--pubkey` is reserved for the future Ed25519 mode.

Markdown, text, CSV, and PDF exports show the signature scheme and key id in
their header. A signed HTTP export carries the same detached signature in
`X-Evidence-Signature`, `X-Evidence-Signature-Key-Id`,
`X-Evidence-Signature-Scheme`, `X-Evidence-Signature-SHA256`,
`X-Evidence-Signature-Chain-Head`, and
`X-Evidence-Signature-Generated-At` response headers. Without a configured key,
the existing unsigned export behavior is unchanged.

### Chain-head anchoring (task #1265, Epic F/F2)

The signature above proves an export wasn't altered *after* it was written;
it says nothing about whether the operator could have quietly rewritten
`event_log` itself before generating the export. An **anchor** closes that
gap: it witnesses one ledger head (`anchor.anchored_head`) somewhere the operator does
not solely control, so a wholesale SQLite rewrite is detectable against a
record outside the hub.

Three backends implement `AnchorBackend.anchor(head_digest) -> ref` /
`.verify(ref, head_digest) -> result` (`src/unified_agent/audit/anchor.py`).
`UA_EVIDENCE_ANCHOR_BACKEND` selects one (default `vault`); `tsa` and `git`
are opt-in.

| Backend | What witnesses the head | Configuration |
| --- | --- | --- |
| `vault` (default) | A dated, locked note in the operator's own Obsidian vault (task #768). An external record, but not independent of the operator — kept as the zero-config default so nothing regresses. | `UA_VAULT_PATH` (existing) |
| `tsa` | An RFC-3161 Time-Stamp Authority. The token is signed by the TSA's key, which the operator does not hold. | `UA_EVIDENCE_TSA_URL` (default `https://freetsa.org/tsr` — overridable), `UA_EVIDENCE_TSA_CERT_PATH` (PEM containing the pinned signer certificate; CA-only PEM is insufficient), `UA_EVIDENCE_TSA_TIMEOUT_S` (default `10`) |
| `git` | A commit to a **separately** configured git repository — never the workspace repo. Signing uses the operator's existing Git setup; verification also requires an independently configured signer fingerprint. The module never generates or stores key material. | `UA_EVIDENCE_ANCHOR_GIT_REPO` (existing repo), `UA_EVIDENCE_ANCHOR_GIT_DIR` (default `evidence-anchors`), `UA_EVIDENCE_ANCHOR_GIT_SIGNER_FINGERPRINT` (pinned SSH or GPG signer) |

```bash
# Anchor today's chain head with the configured (or an explicit) backend.
unified-agent audit re-anchor                    # UA_EVIDENCE_ANCHOR_BACKEND, else vault
unified-agent audit re-anchor --backend tsa
unified-agent audit re-anchor --backend git
unified-agent audit re-anchor --backend git --json
```

`vault` is a thin pass-through to the original task #768 behavior: it never
mutates `event_log`. A dated witness that is not a regular file (a FIFO,
socket or device at `Logs/audit-ledger-heads/YYYY-MM-DD.md`) is refused and left
untouched (task #2212); move it aside and rerun the re-anchor for that day.
The lock verdict is taken from the bytes already read, so a second open of
the note is not required. `tsa`/`git` cache their reference in `event_log`
(channel `audit.chain.anchored`) purely so a report can look up "what did we
anchor for head H" without a network round trip — that cache is bookkeeping,
never the source of trust.

**What an anchor covers.** A witness is taken over one chain head H. Every
entry up to H is covered by it directly; entries recorded after H (including
the cache row that records the anchor itself) are linked to H only by the
hash chain. So a report's header carries the most recent anchor whose
`anchored_head` is at or before the report's `chain_integrity.head_hash`,
plus `entries_after_anchor`: how many later entries the witness does not
cover on its own. `evidence verify` checks the external proof against
`anchored_head`. When it differs from the report head, the JSON export alone
does not prove the later link: `evidence verify` checks the full local ledger
and the exact segment if that ledger is available, and reports an unverified
link otherwise. Re-anchor as often as you need that number to be small.

**What "verified" means per backend.** `tsa`: the token's signature verifies
under the pinned certificate in `UA_EVIDENCE_TSA_CERT_PATH`, the SignerInfo
names that certificate, it carries the `id-kp-timeStamping` extended key
usage, and it was valid at the token's `genTime`. `git`: the anchor file at
the commit matches the head, `git verify-commit` passes, and Git reports the
fingerprint pinned by `UA_EVIDENCE_ANCHOR_GIT_SIGNER_FINGERPRINT`. An unsigned
or differently signed commit is never `verified`. A malformed TSA reply is a clean error, not a
traceback. TSA HTTP responses are capped at 1 MiB before ASN.1 parsing.
`UA_EVIDENCE_ANCHOR_GIT_DIR` must be a relative path inside the
witness repo, and re-anchoring an unchanged head is a no-op.

`build_evidence_report` re-verifies the anchor live and offline on every call — a TSA
check is pure local cryptography, a git check is `git show`/`git
verify-commit` against a local repo path (5-second timeout per command), a vault check is a file read. No
network call happens at report-build time, only at `re-anchor` time.

**Third-party verification.** Given the JSON export (which carries
`anchor.ref`) and, for `tsa`, the pinned signer certificate, an outsider can
verify the historical `anchor.anchored_head`. A newer report head also needs
the local ledger to prove its link to that historical head:

```bash
# tsa: verify the token against anchor.anchored_head and the pinned TSA signer PEM
UA_EVIDENCE_TSA_CERT_PATH=tsa-signer.pem unified-agent evidence verify evidence.json

# git: confirm the commit's file content and its signature independently
git -C /path/to/witness-repo show <commit_sha>:<path>   # compare head_hash to anchor.anchored_head
git -C /path/to/witness-repo verify-commit <commit_sha>

# vault: open the locked note and confirm a matching `- head_hash: <head>` line
```

`unified-agent evidence verify <file>` checks the witness for a JSON export
and, when the anchored and report heads differ, checks their link against a
local ledger. An unsigned JSON export still reaches the anchor check, but the
overall command exits nonzero because the export itself is unsigned. A
`csv`/`markdown`/`text`/`pdf`
export only carries a one-line anchor summary (lossy formats can't hold a
TSA token or a full git ref), so `evidence verify` reports `anchor: not
checked (export is not JSON)` for those — verify the JSON export when the
anchor binding itself needs to be checked by a third party.

### Report-level attestation (task #1267, Epic F/F4)

The signature (F1) proves an export wasn't altered after it was written; the
anchor (F2) proves the ledger head wasn't quietly rewritten before it. Neither
says **who** is vouching for the report artifact itself. The `attestation`
header field answers that: it names the attesting caller, under which
identity class, with which signing key id, at what time, and over what
filter scope — so a standalone export names its own accountable attester
without a live hub lookup (Epic #1191).

This is a different question from `human_provenance` (task #1217, "Actor
identity vs. human authority" above), which is per-record and answers "who is
accountable for the recorded *task action*". `attestation` is report-level and
answers "who generated *this report*" — the authenticated HTTP/MCP requester
that ran `evidence`/`evidence report` at build time. A locally invoked CLI has
no hub-authenticated caller context, so it reports `attester: null` and
`attester_kind: "unknown"`; the CLI does not infer a human from the OS user or
the process that launched it.

| Field | Meaning |
| --- | --- |
| `attester` | The caller's verified identity label, or `null` when unauthenticated. |
| `attester_kind` | `"human"`, `"agent-bearer-only"`, or `"unknown"`. |
| `human_attested` | `true` only when `attester_kind == "human"`. |
| `verified_via` | `"idp"` for a live WorkOS session, `"agent_bearer"` for a plain bearer token, the raw `auth_method` for anything else, or `null` when unauthenticated. |
| `workos_subject` / `workos_organization_id` / `workos_session_id` | Present only for `verified_via == "idp"` — the verified session claims, never the credential. |
| `key_id` | The evidence-signing key id (`UA_EVIDENCE_SIGNING_KEY_ID`/managed secret name) active at build time, or `null` when signing is unconfigured. Never the key material itself. |
| `attested_at` | Same timestamp as the report's `generated_at`. |
| `scope` | The report's own `filter` block (task/project/time-window/agent/pr/limit), duplicated here so the attestation is self-contained. |

**Honesty rule (extends task #1217 to the report header itself):** the
caller's identity is promoted to `attester_kind: "human"` only when the
request authenticated through the hub's WorkOS leg — a live IdP session the
hub verified (JWKS, exact issuer, the one configured organization, `sid`/`sub`
agreement, live re-check; same verification `producer_sign.py` uses). A plain
bearer token or OAuth grant is never promoted to human attestation, even when
the resolved label is itself human-classified (e.g. `Desktop_Person`) — it
renders `agent-bearer-only`. An unauthenticated build (e.g. a local CLI run
with no `UA_AGENT_TOKENS` match) renders `attester: null`,
`attester_kind: "unknown"`.
Control Center/browser requests that arrive under an agent bearer follow the
same rule: the presence of a person at the browser is not IdP evidence, so
those reports remain `agent-bearer-only` unless the report request itself
carries the hub-verified WorkOS identity context.

JSON exports carry the full block. Markdown and text show a concise summary;
PDF shows a one-line summary. CSV carries attestation columns on record rows,
and emits a `record_type=report_metadata` row when there are no records, so
empty exports still carry the report-level identity, key id, timestamp, and
scope without masquerading as an evidence record.

When a report is filtered by project name, its `project_repo_binding` is
report-level metadata in JSON. Markdown, text, and PDF render that binding in
the report header. CSV repeats the serialized binding in a
`project_repo_binding` column on every record row, so a filtered or re-sorted
row retains its repository context; an empty CSV report carries it on the
`report_metadata` row.

`unified-agent evidence verify <file>` reports and validates the block for a JSON export:

```
evidence-verify: attestation: attester=Desktop_Person kind=human key_id=release-2026-09
```

Two checks run beyond the byte-level HMAC (which already fails closed on any
tamper to this block, since it ships inside the signed bytes like every other
field):

1. **Structural honesty.** For schema 1.5 and later, the attestation object
   is required, its kind and human flag must agree, and its timestamp/scope
   must match the report header. A `human` claim requires
   `verified_via == "idp"`, a nonempty subject, organization and session id,
   and `human_attested: true`. Older schema versions may omit the block.
   (`attestation inconsistent: human attestation claim without IdP evidence`,
   exit code `2`) — defense in depth for an export that arrives unsigned or
   whose sidecar was never checked.
2. **Key id consistency.** `attestation.key_id`, baked into the report body
   at build time, must equal the sidecar's actual signing `key_id` when both
   are present — they come from the same key lookup, so a mismatch means the
   report was assembled inconsistently (`attestation key_id (...) does not
   match signing key (...)`, exit code `2`).

A pre-`1.5` export with no `attestation` block reports `no attestation block
(pre-1.5 export)` and does not fail. A non-JSON export
(`csv`/`markdown`/`text`/`pdf`) carries only the one-line `Attestation: ...`
summary (see the JSON schema example below for the full structured block),
so `evidence verify` reports `attestation: not checked (export is not JSON)`
for those.

### Exit Codes
- `0`: Success, audit chain is valid or empty.
- `1`: Tamper-evident ledger integrity broken / tampered.
- `2`: Bad usage or database query error.

`unified-agent evidence verify` uses the same `0`/`1`/`2` convention for a
valid signature, a failed verification, and a usage/configuration error —
and folds in the anchor check the same way: `0` when the anchor is absent or
verifies and any later head is linked by the local ledger, `1` when a present
anchor fails verification, a later link cannot be established, or the export
is unsigned; `2` when
the anchor is present but cannot be checked (e.g. `UA_EVIDENCE_TSA_CERT_PATH`
not configured). The worse of the signature and anchor outcomes wins.

### Merge receipt (task #2307)

A merge receipt is one merged PR projected into one file, `receipt.json`
(`schema: merge_receipt/1`), plus a one-screen PR comment rendered from it.
It is built from records only: the evidence report for the bound task,
`review_reads` through the review-head gate, approval rows, self-pickup run
records, gateway decision rows, and the PR, its reviews and its
`unified-agent/review-head` check fetched from GitHub at render time. In
user-token mode that check is a commit status, and `review.check_run` carries
`source: commit_status` to say so (task #2320).

```bash
# On the hub (reads the store read-only, calls `gh api`):
unified-agent evidence receipt --pr 1361 --repo owner/name --out receipt.json
unified-agent evidence receipt --pr 1361 --repo owner/name --out receipt.json --markdown  # also print the comment
# Anywhere, with no hub, no database and no network:
unified-agent evidence verify receipt.json
```

`receipt` exits `0` when it wrote a receipt, `1` when it refused (the PR is
not merged, the binding is ambiguous, the ledger does not verify, or a rule
below would be broken), and `2` for a usage or environment error.

**Binding.** A receipt describes exactly one task: the one whose
`evidence_ref` carries the exact PR URL (same repo, same number; `/pull/136`
never matches `/pull/1361`). A URL in notes, or `#1361` text, never binds.
When no task or more than one task carries the URL, `receipt` refuses and
names the candidates. `--task <id>` only asserts the binding: a different id
is refused, and it cannot override a missing or ambiguous binding.

**Honesty rules**, enforced before the file is written and again by `verify`:

1. Every `null` field is listed in `omitted[]` with a reason (an entry covers
   its own path and everything under it), and no populated field is listed
   there (`false`, a negative claim such as `device.attested`, may be).
2. The comment shows a tick only where a third party can check the row
   without the hub: a GitHub review, the GitHub head containment, the
   `review-head` check run, and an `rfc3161` or `git` anchor. Hub records
   (review reads, hub approvals, agent identity, device attestation, ledger
   rows, vault anchors) never get a tick.
3. Authority comes only from a record that names this repo, PR and merged
   head: a GitHub review whose reviewer's latest decisive state is
   `APPROVED` at the merged head (a later `CHANGES_REQUESTED` or a dismissal
   withdraws it; a comment does not), shown as `credential-attributed`; or a
   hub approval row of `github_app.process_pr_event` for the same repo,
   number and `head_sha`, shown as `hub-attested`. Any other approval (a file
   write, a delegate) is not merge authority. Nothing qualifying renders
   `not-recorded`. Neither `authority.status` nor `human_authority` is ever
   `verified`.
4. `review.status: no-read-recorded` carries a `finding` ("no review read
   recorded at this head") and renders as one. A head the gate refuses as
   moved is the same finding, with the gate's reason.
5. The agent is always `hub-attested`. The device is attested only when a
   verified node has an accepted, operator-attested acceptance run
   (`node_acceptance`) for the same agent.
6. Denials always carry a coverage label and the caveat that coverage is
   partial. With no gateway rows bound to the task's run episodes,
   `policy.denials` is `null` and omitted. A zero count is never rendered:
   bound rows with no denial read "no denials recorded (coverage: …, partial)".
7. A GitHub read that fails (compare, reviews, check runs) becomes an
   `omitted` reason. Only a failure to read the PR or its merge commit aborts.

**Ledger rows are referenced, not copied.** Each row in `chain.events`
carries its `ulid`, `channel`, `created_at`, `prev_hash`, `entry_hash` and a
`payload_sha256`; the payload text is never written to `receipt.json`
(`chain.payloads: withheld`). A row hash covers the payload, so nobody can
recompute it from the receipt alone. A holder of the private row export can
match each payload against `payload_sha256` and then recompute `entry_hash`.
This command never produces that export.

**What `verify` checks offline.** It validates the structure first (required
blocks, field types, known status values, a well-formed digest); a malformed
receipt exits `2` with a reason, never a traceback. It then checks the
`digest` (SHA-256 over the receipt's sorted, compact UTF-8 JSON without the
`digest` key), the `omitted[]` rule both ways, the honesty rules, the
referenced rows (well-formed hashes, a matching count, consistent links
between referenced rows), and a third-party anchor when its witness is
available locally. It prints `consistency checks passed` and exits `0`, or
exits `1`. It always prints what it did **not** verify:

- authenticity: the receipt is unsigned, so anyone who edits it can
  recompute the digest;
- ledger-head membership: the link from the referenced rows to
  `chain.head_hash` is hub-attested;
- row hashes: payloads are withheld.

The `--markdown` comment carries the same unsigned caveat.

---

## Hub API Surface

Requires `UA_EDITION=enterprise` (the default). Core edition raises
`FeatureNotAvailableError` in the CLI and returns HTTP 403 from the hub.
The Control Center's Settings → Audit page (`/ui/settings/audit`) is the same
endpoint; it filters by task, pull request or commit, and each task cell links
to the task.

Mounted on the hub's Starlette app as a guarded read-only endpoint:

`GET /api/evidence`

### Query Parameters
- `task_id` / `task` (integer): Filter by task ID.
- `since` (string): Lower bound ISO timestamp.
- `until` (string): Upper bound ISO timestamp.
- `agent` (string): Filter by agent label.
- `project` (string): Filter by project name.
- `pr` (string): Substring search on PR URL or commit SHA.
- `limit` (integer, default 50, max 500): Record cap. Values below 1 fall back to 50. Project subtree queries (`project_id`) default to 500.
- `format` (string): `json` (default), `markdown`/`md`, `text`, `csv`, or `pdf`.

CSV and PDF are **hub-only** (`GET /api/evidence?format=`). The CLI
(`unified-agent evidence`) emits `text` by default, or JSON / Markdown with
`--json` / `--markdown`. `unified-agent evidence report` defaults to Markdown
(`--json` opt-in). There is no CLI `--pdf` / `--csv`. Passing `format=pdf`
to the hub:

- Returns `application/pdf` with `Content-Disposition: attachment;
  filename="evidence-report.pdf"` when the report has records and the event
  chain is not `tampered`.
- Returns **422** with `{error}` when there are no records, or when
  `chain_integrity.status` is `tampered`. The formatter refuses to emit a PDF
  that would self-certify a false chain (`src/unified_agent/audit/evidence_report.py`).
- Returns **503** when the optional `pdf` extra is missing (`fpdf2`). Install
  with `uv sync --extra pdf` or `uv sync --all-extras`.
- Degrades non-latin-1 characters (emoji, curly quotes) with `?` — Helvetica
  core fonts cannot encode them.

`format=csv` attaches `evidence-report.csv`. Empty or tampered reports are
allowed as CSV/Markdown/JSON; only PDF refuses them.

### JSON Schema

`schema_version` is bumped for any incompatible field-or-meaning change, not
just added fields. It moved `"1.0"` → `"1.1"` at task #1217 because
`human_provenance.verified` changed *meaning* — from "a signed producer
uniquely verified" (actor authentication) to "a verified **human** identity
signed the claim" (human authority) — with no change to the field's shape.
It moved `"1.1"` → `"1.2"` at task #1249, which added
`human_provenance.verified_via` (present on verified records: `"idp"` or
`"agent_bearer"`) so report consumers can distinguish an IdP-verified human
link from one verified only by an agent bearer.
A version-aware consumer must branch on `schema_version` rather than assume
`verified` still means actor authentication for reports at `"1.0"`.
It moved `"1.1"` → `"1.2"` at task #1250 because `device` changed *meaning* —
from a bare hostname string to a device link: `<node> (verified)` for a
verified tailnet node, `unverified-device:<host>` for hostname-only
provenance, `unattributed` when absent. Each record gains a structured
`device_link` field (`{verified, node?, node_id?, user?, resolved_at?}` for a
verified node, `{verified: false, ambiguous, nodes}` for disagreeing node
claims, `{verified: false, host}` for hostname-only, `null` when absent) so
consumers do not parse the string.
It moved `"1.2"` → `"1.3"` at task #1287, which added a `provenance_graph`
appendix — the same records reshaped as a node/edge graph with a canonical
hash. See [provenance-graph.md](provenance-graph.md) for the graph schema,
the node/edge vocabulary, and the query API.
It moved `"1.3"` → `"1.4"` at task #1265 (Epic F, F2), which added a named
`anchor` header field (see "Chain-head anchoring" below) — additive, but
versioned like the others per this doc's own rule ("bumped for any
incompatible field-or-meaning change, not just added fields") since a
version-aware consumer of the header shape should not have to guess whether
`anchor` is present.
It moved `"1.4"` → `"1.5"` at task #1267 (Epic F, F4), which added a named
`attestation` header field (see "Report-level attestation" below) — same
rationale as `anchor`: additive, but versioned so a consumer of the header
shape doesn't have to guess whether `attestation` is present.
Complete scheduled exports used schema `1.6` and include `"complete": true` to
state that the report was read without the interactive record cap.
It moved to `"1.7"` at task #1673 for every report, interactive or complete
(a complete export still carries `"complete": true`):

- `human_provenance.human_authority: "verified"` is renamed
  `"credential-attributed"`, because a bearer or IdP credential does not prove
  a present human. `"presence-verified"` is reserved for statement-bound
  passkey assertions. Records gain `human_provenance.credential_attributed`.
- The CSV column `human_verified` is renamed `human_credential_attributed`.
- New report-level `sod` and `sod_detail`. With one configured human (every
  deployment today: `config.human_identity_labels()` holds only the operator
  label) `sod` is `"not_applicable_single_operator"` rather than a pass or
  fail. With more than one, every granted approval in scope is checked and
  `sod` is `"pass"`, `"fail"` (a resolver who is the requester, aliases
  included, is listed in `sod_detail.violations`) or `"no_data"`.

It moved `"1.7"` → `"1.8"` at task #2309, which added a `denials` block to
every record (see "Gateway denials bound to runs" below). Additive, versioned
by the same rule as `anchor` and `attestation`.

Historical exports keep rendering: `evidence_report.normalize_human_authority`
and `read_historical_report` read `"verified"` as `"credential-attributed"`,
on records and on the `provenance_graph` appendix's human nodes (whose
`canonical_hash` is recomputed when a node changes). A 1.0 record has no
`human_authority`; its bare `verified: true` was actor authentication (agent
or human signer), so it reads as `"unknown"` and the flag is kept as
exported. `unified-agent evidence render <export.json> [--format markdown|text|csv|json]`
re-renders a saved JSON export from any schema version without touching the
hub database. Verify the original bytes with `unified-agent evidence verify`.

```json
{
  "report_type": "user_attributable_evidence",
  "schema_version": "1.7",
  "generated_at": "2026-09-06T10:25:15+00:00",
  "chain_integrity": {
    "status": "valid",
    "verified": true,
    "actor_authentication_verified": false,
    "provenance_verified": false,
    "head_hash": "a1b2c3d4...",
    "total_verified": 42
  },
  "anchor": {
    "status": "anchored",
    "backend": "tsa",
    "ref": {
      "backend": "tsa",
      "tsa_url": "https://freetsa.org/tsr",
      "token_b64": "MIIF...",
      "head_hash": "a1b2c3d4...",
      "anchored_at": "2026-09-06T10:26:00+00:00"
    },
    "anchored_at": "2026-09-06T10:26:00+00:00",
    "verification": {
      "verified": true,
      "reason": "signature verified against supplied TSA certificate",
      "detail": {
        "gen_time": "2026-09-06 10:26:01+00:00",
        "serial_number": "424242",
        "tsa_subject": "CN=Test TSA"
      }
    },
    "recipe": "Base64-decode `token_b64` ... openssl ts -verify ..."
  },
  "attestation": {
    "attester": "Desktop_Person",
    "attester_kind": "human",
    "human_attested": true,
    "verified_via": "idp",
    "workos_subject": "user_01ABC...",
    "workos_organization_id": "org_01XYZ...",
    "workos_session_id": "session_01DEF...",
    "key_id": "release-2026-09",
    "attested_at": "2026-09-06T10:25:15+00:00",
    "scope": {
      "task_id": 777,
      "project_id": null,
      "since": null,
      "until": null,
      "agent": null,
      "project": null,
      "pr": null,
      "limit": 50
    }
  },
  "filter": {
    "task_id": 777,
    "project_id": null,
    "since": null,
    "until": null,
    "agent": null,
    "project": null,
    "pr": null,
    "limit": 50
  },
  "total_records": 1,
  "signature": {
    "scheme": "hmac-sha256-v1",
    "key_id": "release-2026-09",
    "chain_head": "a1b2c3d4...",
    "generated_at": "2026-09-06T10:25:15+00:00"
  },
  "project_chain": null,
  "provenance_graph": {
    "schema_version": "1.0",
    "scope": {"kind": "task_scope", "task_id": 777},
    "node_count": 3,
    "edge_count": 2,
    "node_counts_by_kind": {"action": 1, "agent": 1, "task": 1},
    "nodes": ["... see provenance-graph.md ..."],
    "edges": ["... see provenance-graph.md ..."],
    "canonical_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b85"
  },
  "records": [
    {
      "task_id": 777,
      "project": "unified-agent",
      "human": "unknown",
      "human_provenance": {
        "value": "unknown",
        "source": "unknown",
        "candidates": [],
        "actor_kind": "unknown",
        "human_authority": "unknown",
        "verified": false,
        "conflict": false
      },
      "device": "unverified-device:<hub-host>.local",
      "device_link": {
        "verified": false,
        "host": "<hub-host>.local"
      },
      "agent": "agent-cursor",
      "action": {
        "type": "task",
        "title": "feat(langgraph): governed plan-build-verify-merge chain",
        "status": "done",
        "priority": "high",
        "lane": "hobby",
        "approvals": []
      },
      "proof": {
        "evidence_ref": "https://github.com/anonb3ll/unified-agent/pull/383",
        "pr_url": "https://github.com/anonb3ll/unified-agent/pull/383",
        "commit_sha": "baece57",
        "created_at": "2026-09-06T06:59:25+00:00",
        "updated_at": "2026-09-06T10:30:26+00:00",
        "completed_at": "2026-09-06T10:25:15+00:00",
        "event_hashes": [
          {
            "ulid": "01K4ABC...",
            "channel": "agent.task.completed",
            "created_at": "2026-09-06T10:25:15+00:00",
            "entry_hash": "e3b0c44...",
            "prev_hash": "d4c3b2a..."
          }
        ],
        "events_count": 1,
        "tamper_evident": true,
        "merge_verified": false,
        "merge_claim": {"status": "none", "source": "none", "verified": false, "conflict": false}
      }
    }
  ]
}
```

### Gateway denials bound to runs (task #2309)

A gateway deny (`agent.gateway.decision` with `decision: "deny"`) is written
with the episode of the request that caused it in `event_log.episode_id`. A
request made with a session credential is bound to `acp:<item>`: one node
dispatch item, i.e. one run. A deny of a registered call also carries
`args_sha256`, the redacted digest of its supplied arguments. A request made on
a bearer token or device sign-in (an interactive session, task #2313) is bound
to `run:<run_id>` when its caller holds exactly one live `doing` claim (a
lease that has not lapsed) and that task's open self-pickup run is the
caller's own. A caller with no live claim, two or more, or a claim with no
open run of its own gets no episode, and its denies keep `episode_id` NULL:
the hub does not guess which run a call belongs to. Read-only allows in a run
are still coalesced into rollups; each rollup carries the run's
episode. A per-episode policy cap (`max_calls_per_episode`,
`fatigue_band_cap`) counts a `run:` episode's calls from the gateway, rolled-up
and still-coalesced reads included, and treats the episode as existing when
the hub opened that self-pickup run. Rows written before the stamp are not backfilled.

Each record's `denials` block lists the denials bound to the record's runs:
for an `item` filter, that dispatch; for a task (and so for `pr` and project
reports, which select tasks), every dispatch item of the task, every
self-pickup run (`sp-...`, episode `run:<id>`), and any dispatch run no item
names.

```json
"denials": {
  "status": "recorded",
  "runs": [
    {"run": "node:<node>:nd-0123456789abcdef", "episode_id": "acp:nd-0123456789abcdef", "bound": true}
  ],
  "items": [
    {
      "ulid": "01K...",
      "created_at": "2026-11-03T10:00:00+00:00",
      "episode_id": "acp:nd-0123456789abcdef",
      "causal_parent_id": null,
      "tool": "fs_write",
      "rule": "no_writes",
      "reason_code": "policy_deny",
      "caller": "<agent label>",
      "profile": "developer",
      "args_sha256": "9f2c..."
    }
  ],
  "count": 1,
  "truncated": false,
  "unbound_in_window": 0,
  "window": {"since": "2026-11-03T09:58:00+00:00", "until": null, "callers": ["<agent label>"]}
}
```

- A run is `bound` when a session credential was minted for it or some event
  carries its episode.
- `unbound_in_window` counts denies with no episode written in the record's
  window (the dispatch's or the task's `created_at` to `completed_at`) by any
  agent in `window.callers`. They may belong to this work and can be neither
  attributed to it nor excluded.
- `window.callers` is every agent that may have acted on the work. An item
  record: the dispatch's agent and profile agent and its session-credential
  agents. A task record adds the record's agent, the claimant (current and
  `claimed-by:` history), every agent that ever opened a self-pickup run of
  the task (read from the immutable `selfpickup.claimed` events, so a former
  claimant stays in even after its notes and the claimant column change), and
  the agents of every dispatch item, dispatch run and session credential of
  the task. It is `null`, meaning every caller, when
  the record's agent is unattributed. An agent that touched the work without
  claiming it, being dispatched for it or holding a credential for it is not
  in the set.
- `status` is `recorded` only when every run is bound **and**
  `unbound_in_window` is 0. Only then does an empty `items` mean the work had
  no denials: one bound event does not prove every denial was bound.
- `partial`: at least one run is bound, but another run is unbound or an
  unbound deny falls in the window. `items` lists the bound denials; others
  may exist.
- `not-recorded`: the record has no run, or no bound run (its caller held
  several claims at once, or it ran before the stamp existed). It never means
  zero denials.
- `items` holds at most 200 rows; `count` is the full number and `truncated`
  says when the list was cut. Items never carry arguments or `args_summary`.

The JSON export carries the block; the CSV, Markdown, text and PDF renderings
do not show it yet.

### Project reports through MCP

The MCP `evidence_report(project_id=N)` call requires N to be a task carrying a
non-backticked `[project-spec]` marker. It returns that root and its multi-level
task subtree in root-first tree order. Display filters and the record limit apply
only to `records`; `project_chain` is always derived independently from the full
subtree. Missing or non-project roots are rejected, while projects without
verified merges return an explicit partial result.

Buyer-facing project artifacts omit records whose agent cannot be resolved
(`claimed_by`, notes `claimed-by:`, or event `originator`). Those historical
nullable rows are listed on `unattributed_excluded` and remain in
`project_chain.task_ids` / `project_chain.steps`. The renderer does not invent
an actor or backfill agent identity from `source_host`. Single-task and
project-name filters still surface historical `unattributed` rows.

When `project_id` is supplied,
`project_chain` contains:

```json
{
  "project_id": 819,
  "status": "partial",
  "task_ids": [819, 830, 831],
  "done_task_ids": [830, 831],
  "evidence": [
    {
      "task_id": 830,
      "evidence_ref": "https://github.com/anonb3ll/unified-agent/pull/423",
      "pr_url": "https://github.com/anonb3ll/unified-agent/pull/423",
      "commit_sha": null,
      "merge_verified": false,
      "merge_claim": {"status": "inferred", "source": "task.notes", "verified": false, "conflict": false},
      "completed_at": "2026-09-09T00:00:00+00:00"
    }
  ],
  "merged_prs": [],
  "last_merge": null
}
```
