# Invocation boundary: Stage 0 and first proof contract

Task **#1972**, child of **#1919**. Status: design for independent review;
the shared invocation service and transport cutover are not implemented here.

The operator approved the staged direction on 2026-10-09. This design
retains the product and its monorepo. It authorizes no live effects, credentials,
network changes, public publication, additional spending, production migrations,
new framework, or repository split. The initial effect adapter is synthetic.

## Source pin and ownership

The inspected product base is
`d4f1655be53efbb598495e0958939f0dcb77a83f`. Links below identify that base,
not a claim about a later deployment. A local architecture audit provides the decision
context. Its 394 selected tests and seven gap probes are scoped audit evidence,
not acceptance results for this design, full CI, or end-to-end certification.

The independent review of initial design head `c2394c1a` returned FIX-FIRST.
The four corrections below are design requirements, not implemented runtime
behavior. Only this new document is in the current write scope.

| Existing work | Boundary for this slice |
|---|---|
| #1919 | Parent architecture/consolidation work; requires a design before code. No YAML/LangGraph adoption decision is made here. |
| #1433 | Future structural organization partition and migration contract. This proof uses isolated fixtures, not a partial production migration or activation of multitenancy. |
| #1231 | Mechanical repack only, LAST AND ALONE when landing. No dependency on its physical moves and no behavior changes folded into it. |
| #1721 / #1722 | Identity and doctor probe extraction with compatibility re-exports. Leave their scope independent. |
| #1834 / PR #1084 | Tenant-fence work keeps its own owner. No duplicate repairs or writes to its source files. |
| #1969 / PR #1083 | Independent review/CI/steward path; this design does not freeze an otherwise eligible PR. |

At the 2026-10-09 reconciliation, #1433/#1231/#1721/#1722/#1919 were open
and unclaimed. #1083 had zero unresolved review threads but no covering CI;
#1084 had four unresolved findings. Both remained open. Approvals 882/883
were consumed at their respective exact heads; those approvals do not supply
correctness, missing CI, or permission for changed heads. Recheck these facts
before integrating; they are a dated snapshot, not standing shipping gates.

## Stage 0 inventory and compatibility obligations

This is an inventory of the selected slice and its reachable seams. Exhaustive
inventory of every integration, transport, run producer, and consumer remains
parent #1919/#1283 work; it must precede expansion beyond this slice.

| Surface or data owner | Observed seam | Contract to preserve |
|---|---|---|
| MCP tool calls | `server.py` registers wrapped callables and adds elicitation. | Authentication, profile/access ceilings, policy, trust presets, safety, feature gates and approval semantics precede effects. MCP elicitation remains an adapter concern. |
| HTTP task calls | `webui/api.py` routes task updates through `call_tool`; selected other object writes use `_registered_platform_write`. `webui/routes.py::action_task` handles actual `POST /api/action/task` requests. Its whitelist at line 2938 omits `expected_updated_at`. | Calling `api.update_task` directly bypasses route parsing/normalization and does not prove HTTP acceptance. The actual route currently rejects the selected revision field. A reviewed narrow route change is a rollout dependency, not something a synthetic harness can hide. |
| HTTP object reads | `platform_http.py` uses the object registry and profile gates shared with resource readers. | Keep this existing parity; it is not a new mutation dispatcher and should not acquire task SQL. |
| Tasks | `tools/tasks.py::task_update` owns a `BEGIN IMMEDIATE` mutation and a required idempotency write. | Preserve revision/run fencing, trust handling, lifecycle/worktree consequences, return shape and all unaffected update fields. The proof selects title only. |
| Receipts | `storage.py` owns legacy idempotency rows. | Retain historical identifiers/meaning; no unrestricted raw-key fallback, global string-prefix tenant convention, overwriting another tool's receipt, or silent receipt-write failure. |
| Evidence | `events.record_event_once_on_connection` appends without committing a caller's transaction; gateway result logging is a separate composition concern. | Required state/evidence/receipt commit has one owner. Post-commit socket fan-out can remain best effort. Post-effect ledger failure is not success or safe automatic retry. |
| Approvals | `tools/approvals.py` canonicalizes payloads, consumes by CAS, records a consumption nonce and validates/restores invocation-owned passes. | Keep identity, payload, expiry, generation, invocation lease and ABA defenses. A new replay receipt never grants authority. |
| Tenant ownership | `edition.tenant_org_id` currently derives optional edition context; five platform tables have nullable organization columns. | New proof principal is explicit and verified. Historical null ownership is not assigned to a tenant by guesswork. Work products and legacy receipts need separately proven ownership. |
| Runs and effects | `run_records`, executor/dispatch and `node_queue` have existing run/fence/receipt paths; LangGraph has pending-approval lookup helpers. | Do not create a competing task/run authority. No new task claim semantics, lifecycle state machine or framework consolidation in this slice. |

Source references at the pin:
[MCP wiring and registered wrappers](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/server.py#L1021),
[HTTP object writes](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/webui/api.py#L395),
[HTTP task updates](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/webui/api.py#L5096),
[HTTP invocation composition](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/webui/routes.py#L643),
[object reads](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/platform_http.py#L1),
[task update](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/tools/tasks.py#L1333),
[required receipt write](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/tools/tasks.py#L2143),
[approval consumption](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/tools/approvals.py#L508),
[caller-owned evidence](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/events.py#L317),
[tenant columns](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/storage.py#L3893).

## Exact operations and invocation contract

1. **Task mutation:** `task_update(id, title, expected_updated_at,
   idempotency_key)`, with no status, claim, delegation, worktree or trust
   changes. Preserve the existing title validation and authorization checks.
   Unsupported fields must remain on the existing governed path; they cannot
   silently enter a title-only path or bypass any checks. No second task store.
2. **Effect proof:** synthetic notification send, with a fixture destination
   ID and body, routed to an injected durable synthetic provider. It simulates
   an outbound effect and provider reconciliation; it sends nothing to a real
   recipient and registers no production tool or HTTP endpoint.

Both adapters must ultimately call one invocation service. There are two
separate acceptance tiers:

- **Service proof:** isolated fixture repositories, typed principal, synthetic
  provider and direct adapter-function calls prove the transaction/replay
  contract. This is seam scaffolding, not registered application-path acceptance.
- **Application-path proof:** exercise the actual registered MCP call and the
  actual HTTP `POST /api/action/task` route, including authenticated request
  context, strict parsing/default normalization, profile/access/safety/policy
  gates, approval consumption, domain results and HTTP/MCP error mapping. Use
  only isolated state and injected synthetic effects. A direct `api.update_task`
  call, a new test-only route, replaced guards or a replacement task writer is
  insufficient. The selected revision precondition must reach the same service
  through both actual paths; first test the present HTTP rejection as a gap,
  then test its separately reviewed route integration.

The synthetic effect's registered application binding must likewise use the
actual guarded invocation composition, with a test-scoped injected provider;
no persistent tool registration or live provider configuration is authorized.
If no actual effect route can be exercised without a newly scoped adapter
change, report that gap rather than label service tests as application parity.
Production binding is a separate reviewed step after #1084 ownership/dependency
handling. Service proof passing alone cannot close the first slice.

The service receives a typed principal produced by existing authenticated
transport code: verified subject, organization, effective profile/capabilities,
and any session/task/run restrictions. Never trust a supplied `actor`, body
`org_id`, HTTP header, environment fallback or unverified label as this
principal. A missing or mismatched principal fails closed. Do not enable new
permissions to make the proof pass. Single-organization compatibility and
historical null ownership require an explicit reviewed mapping before cutover.

Use a structured unique replay identity of organization, verified subject,
operation name/version and client key. Do not encode tenants into a caller
controlled global key string. Bind that identity to a versioned digest of the
full validated semantic payload, including task ID, title, revision precondition
or synthetic destination/body. Normalize declared defaults consistently across
transports; reject unsupported values and non-finite JSON numbers. Key order
must not change the digest. Do not reuse the redacted gateway evidence digest
as a full payload identity, persist plaintext secrets, or log sensitive bodies.
The proof uses non-secret fixtures. Keep the semantic digest in the restricted
receipt store; public evidence uses existing redacted summaries. Extending this
to secret-bearing operations requires a reviewed protected equality strategy,
not a public unkeyed hash or a new credential created for this proof.

Authenticate and enforce current access/tenant/session restrictions before
returning a replay; a receipt cannot bypass revoked access. For a completed
matching receipt return the same domain result without another mutation,
effect or approval consumption. A changed payload under the same identity
returns a conflict. Cross-tenant or cross-principal lookups cannot disclose
another receipt or influence its state. Key conflicts are atomic DB constraints,
not an unlocked read-before-write cache or a process-local mutex.

## Transaction, approval and effect boundaries

### Replay before revision, with one task transaction owner

The order is current authentication and resource/tenant/session access, scoped
receipt lookup plus semantic digest comparison, then the revision precondition
**only for a new mutation**. A completed matching receipt returns its original
result even if the response was lost and a later authorized edit changed the
task revision/title. It must not write again, compare the old revision against
the new task, or consume another approval. Current access revocation can still
refuse disclosure. Main's task writer already looks up its receipt before the
revision check; the new service must preserve that ordering.

For a new task mutation, one service-owned SQLite transaction rechecks current
resource access, finds/conflict-checks any receipt committed in flight, validates
the new mutation's revision and any consumed grant binding, applies the title
update, writes mandatory domain and gateway result evidence on that connection,
and completes the receipt. Required receipt/evidence errors roll back all task
changes. Repository/evidence helpers cannot commit, open competing write
transactions, swallow errors, or fan out effects inside this transaction.

### Durable approval protocol: extend the existing gate, never replace it

Existing `approval_invocation` receipts and leases are ContextVars. They close
with their process/invocation; storing an approval ID or reopening a ContextVar
is not durable authority. Existing consumption commits on its own connection.
Do not call that writer while holding another SQLite write lock. The following
protocol is required before claiming approval-safe restart or application-path
acceptance; the needed integration helpers are not implemented by this design.

1. **Reserve:** after current authorization and completed-replay lookup, commit
   a unique reservation for principal/organization, operation/version, client
   key, semantic digest, stable operation ID and current attempt/fence. No task
   mutation or provider call occurs. Concurrent retries use this record; only
   its fenced owner may consume a pass. Others return pending or replay. This
   receipt fence does not introduce a competing task/run lease framework.
   **Takeover after a crash:** the reservation records an owner lease expiry.
   While the lease is live, other callers return pending. After it lapses, a
   caller with the same principal and semantic digest may take over by one
   compare-and-set that increments the fence and renews the lease; only one
   such takeover can win. Every later step (consume/bind, operation
   transaction, dispatch marker, completion) checks the fence it started with,
   so the old owner, if it resumes, fails its next fenced write and stops.
   Takeover never consumes a second pass: an existing bound consumption is
   recovered through step 5. Acceptance needs a test with a paused old owner,
   a lapsed lease, a recovering process and the old owner's refused resume.
2. **Consume and bind in the existing gate transaction:** the existing approval
   gate performs its canonical-payload/tenant/identity checks, expiry checks,
   approved-to-consumed CAS and consumption event. A narrowly reviewed binding
   hook in that *same connection/transaction, before commit* must CAS-link the
   reserved operation to the exact grant snapshot, tool/canonical payload,
   approval ID, consumed generation and consumption-event nonce. Include the
   verified principal/tenant and attempt fence in that durable ownership link.
   Binding failure rolls back consumption and its event. There must be no
   consume-then-bind post-commit window. Neither the hook nor reservation decides
   approval; tenant fences must reuse the reviewed #1084 work. Ungated operations
   record that existing policy disposition instead of fabricating a grant.
3. **Validate at the operation transaction:** expose a reviewed durable ownership
   validation seam that reads the bound reservation and reuses the existing
   grant/payload/expiry/generation/consumption-nonce checks. The current live
   seam is `approvals.revalidate_invocation_approvals(conn)`; its underlying
   validator already checks the row, grant snapshot, expiry and latest nonce.
   A fresh-process adapter must supply durable ownership proof to that same
   validation logic, not fake a live lease, replay `check_approval_gate`, trust
   an approval ID alone, or replace the gate. New task state/evidence/receipt,
   or effect intent/evidence, commits under this caller-owned transaction.
4. **Dispatch marker:** an effect intent remains non-dispatchable until a second
   transaction rechecks current authority, exact durable grant binding and
   attempt fence and commits its dispatch-started marker. Only the winning
   fence may call the provider. No provider call before the marker commits.
5. **Recover:** reopen the stores in a fresh process, verify current caller and
   resource access, capture/recheck the exact operation fence and inspect the
   durable state below. Never reconstruct authority solely from a process
   cache or use another identical-looking grant. Restoration is allowed only
   through the existing exact-consumption ownership/CAS rules after proving
   that execution never started. Expired/revoked/changed grants fail closed for
   any new mutation/send; no implicit replacement consumption or automatic
   restoration. Once dispatch may have started, the pass cannot be restored.

The binding hook, durable-validation/restoration seam, wrapper hook and task
transaction extraction need explicit file ownership coordination and delta
review. Until those exist, a proof using grant stand-ins cannot establish
approval restart safety. The first synthetic tests must use the existing gate
and validator against isolated real approval/event schemas. Broader durable
schema rollout remains #1433 work, not an unreviewed production migration here.

### Effect intent, uncertainty and reconciliation

After the authorized intent and evidence commit, call the injected synthetic
provider only after the dispatch marker above. The provider persists its own
operation-ID/digest deduplication and queryable results independently of the
hub transaction. SQLite cannot atomically commit an external effect; this
design does not promise general exactly-once delivery.

| Durable outcome / crash point | Retry and recovery behavior |
|---|---|
| Before reservation commit | No accepted operation, consumption or provider call. Retry may reserve. |
| Reservation committed, no consumption binding | No effect allowed. Its fenced owner may use the existing gate; pending/denied/expired disposition remains visible. |
| Consumption and binding committed, before mutation/intent commit | Resume only this operation using the durable exact-grant validator; no second consumption. If ownership/grant validation fails, stop. Proving no execution started is required before any permitted existing restoration. |
| Effect intent committed, before dispatch marker | No provider call has started by contract. Fresh-process recovery rechecks current authority, bound grant and fence before dispatch; expiry/revoke/restore-reconsume prevents dispatch. |
| Dispatch marker committed, including before the actual call | **Indeterminate** after a crash/timeout. Conservatively include the marker-before-call window; do not blindly send or restore the pass. |
| Provider confirms result for the exact operation/digest | A currently authorized recovery records the original factual result/evidence and completes the receipt under the current fence, without sending or consuming another pass. Grant expiry after execution does not erase the fact that execution occurred. |
| Provider supplies definitive no-effect evidence | Retry requires a separately reviewed fenced recovery disposition and the existing approval contract. Local receipt absence or a transient provider miss is insufficient. |
| Provider cannot determine outcome | Retain indeterminate state/evidence; no success/failure relabeling, blind send or pass restoration. |
| Completed task/effect receipt, including later task edits | Current authorized matching replay returns the original result, without new revision validation, mutation, effect or approval consumption. |

Restore/reconsume of a pass changes its consumption nonce/generation. A stale
reservation or process cannot dispatch or finish a successor's attempt using
that pass. If dispatch already occurred, preserve the original binding as
historical evidence and reconcile its outcome; never retarget it to the new
consumption. Recovery uses existing run/attempt fences where applicable and
operation-local CAS ownership, not a new autonomous resend sweep.

### Gateway evidence belongs to the operation transaction

At this pin, `server.py:880` calls `gateway.record_call_result` after the body;
`gateway.py:1040` appends on a separate connection and can raise LedgerWriteError
after a successful mutation. A new service that writes its own evidence but
leaves that mandatory post-body append unchanged does not solve this hazard.

For selected integrated operations, the shared invocation boundary must own
the canonical redacted gateway result event as well as domain evidence and
receipt/state. Extract a connection-taking path from the existing gateway
formatter and append with `events.record_event_once_on_connection`; do not
duplicate event formatting or reset the layer baseline. Intent and terminal
effect result events use distinct stage identities, never claim that intent
alone is a completed result, and are deduplicated within their transactions.

The registered wrapper needs a reviewed internal integration hook that accepts
only service-owned committed outcome/evidence references bound to this exact
invocation/principal/payload. After commit it marks the existing invocation as
already recorded and suppresses the duplicate mandatory post-body append.
Never trust a caller-supplied success/skip flag. A completed replay points to
its existing outcome/evidence and performs no second mandatory append that can
turn the committed result into a new write failure.

A rolled-back task operation keeps that explicit disposition even if optional
failure diagnostics cannot be written. A provider success followed by required
outcome-evidence failure remains indeterminate with its intent/dispatch marker;
the wrapper must preserve that disposition instead of masking it with another
post-body logging exception. Post-commit socket fan-out, telemetry and replay
diagnostics are separately best effort and cannot change committed outcomes.
This exception to post-body logging applies only to the reviewed service-owned
operations; legacy tools are not globally reclassified or silently weakened.

Source anchors for the reviewed seams:
[actual HTTP whitelist](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/webui/routes.py#L2938),
[task replay before revision](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/tools/tasks.py#L1659),
[existing approval validation](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/tools/approvals.py#L293),
[live invocation seam](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/tools/approvals.py#L311),
[post-body registered wrapper](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/server.py#L880),
[current gateway result writer](https://github.com/anonb3ll/unified-agent/blob/d4f1655be53efbb598495e0958939f0dcb77a83f/src/unified_agent/gateway.py#L1040).

## Acceptance matrix and next milestone

Run the service-proof matrix first, then its applicable cases through the
actual registered MCP and HTTP application paths described above. Authenticated
context, parsing, normalized defaults and existing guard/approval logic must be
real in that second tier. HTTP status/MCP error mapping may differ; authority,
domain outcome and receipt semantics may not. The following are planned
acceptance cases, not results claimed by this documentation PR.

| Test | Required assertion |
|---|---|
| Missing/unverified principal; body tenant/actor spoof | Refusal before any row, approval consumption or provider call. |
| Organization A/B, subject A/B, same client key | No cross-read, mutation, replay, pending approval lookup or receipt replacement. |
| Historical null ownership and prefix-like raw keys | No guessed ownership, raw-key aliasing or permissive legacy fallback. |
| Changed title, task ID, revision, destination or body | Same replay identity conflicts; no new mutation/effect. |
| Equivalent defaults/key order via actual MCP and HTTP route | Same semantic digest/result; no second receipt/effect. Direct API helpers cannot substitute for route acceptance. |
| Actual HTTP revision field and malformed/unknown arguments | Baseline test exposes current `expected_updated_at` rejection; reviewed route integration reaches the same revision validation as MCP. Both fail closed on unsupported arguments and preserve error mapping. |
| Lost mutation response, then another authorized title edit | Matching old-key retry returns its original result despite the new revision, with zero writes, effects or additional approval consumption. |
| Concurrent matching calls, separate DB connections | One title mutation/evidence/receipt and one synthetic provider effect. |
| Concurrent differing payloads | One winner and explicit conflict; no cache race. |
| Required evidence/receipt insert failure | Task state unchanged; effect intent failure prevents provider call. |
| Failure before/after each commit and provider boundary | Outcomes match the durable recovery table after reopening both fixture stores. |
| Provider success followed by outcome-ledger failure | Indeterminate; reconciliation returns the original provider result without resending. |
| Fresh-process crash after consumption/binding before intent | Exact durable owner resumes without repeat consumption; orphan consumption is impossible because binding/event/consumption commit together. |
| Fresh-process crash after intent before dispatch | Grant expiry/revoke/payload or generation change refuses dispatch; no provider call. |
| Fresh-process restore/reconsume ABA | Original durable binding cannot authorize a new marker or use a successor's nonce; no fabricated ContextVar lease. |
| Expired/denied/revoked approval; payload/generation/nonce mismatch | Existing gate/validator refuses new execution. No fake grant or repeat consumption. |
| Actual registered wrapper: required task gateway append fails | State/domain evidence/receipt roll back; no commit-then-ledger-error from a duplicate post-body append. |
| Actual wrapper replay and post-commit fan-out/diagnostic failure | Completed result returns unchanged with zero duplicate mandatory logging, writes or approval use. |
| Actual wrapper: provider success then mandatory outcome append fails | Indeterminate disposition survives wrapper handling; provider query/reconciliation recovers the original result without sending again. |
| Restore/reconsume ABA and stale attempt completion | Old invocation/attempt cannot use the new pass or publish a successor's result. |
| Access/session revocation between execution and replay | Current authority refuses replay disclosure; stored result remains intact. |
| Existing single-org title update and unaffected fields | Return shape, safety and policy behavior preserved; no accidental route diversion. |

**Next milestone:** independent delta review of all four FIX-FIRST corrections:
actual route acceptance versus scaffolding; replay before revision; the durable
existing-gate protocol and restart tests; and transactional gateway/wrapper
ownership. After the disposition is incorporated, record one bounded successor
implementation task under #1919, with one sole writer and an isolated worktree.
It must name new service/test files and any narrow layer-map entry before code.
No live provider binding, schema rollout or production transport cutover is
implied by a synthetic service proof passing. The eventual first-slice milestone
also requires registered application-path acceptance with synthetic effects;
it cannot be closed as a documentation-only or direct-helper-only result.

The successor can prove contracts in isolated fixtures without touching
#1084-owned files. Actual task transaction extraction and MCP/HTTP binding must
wait for source ownership coordination, integrate the reviewed tenant repairs
and use one authoritative task writer. Broader schema changes stay in #1433;
physical repacking stays in #1231. New modules must obey the existing layer
ratchet without resetting its baseline.

Use an explicit minimal environment, isolated UA_HOME/SQLite/provider fixtures,
no network/model downloads, and concise failure output that cannot dump
inherited environment values. Run only scoped acceptance/regression checks;
no full suite, paid executor launch, `ci:full` label or CI dispatch in this stage.

Authors push draft PRs with exact-head local evidence. The shipping steward handles
independent review, unresolved Devin findings, covering batch CI, authenticated
human Approval Ledger binding to the eventual exact merge head, pinned merge
and serialized deployment under existing policy. This design PR changes no
runtime, registration, OAuth, credential, approval configuration or deployment.
