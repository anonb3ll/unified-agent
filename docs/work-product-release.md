# Work Product Release (task #1278, H2)

The non-code pilot wedge. Every connector before this one is developer-shaped
(GitHub PRs). Real pilots need a wedge for the case where an agent's output
*is* a document, not a diff: a report, a filing, a remediation package, a
patient note, an audit workpaper. This is that wedge, built vertical-neutral —
matter/client tags, reviewer roles, and release destinations all come from a
chosen **template pack**, never hardcoded here.

It traces the same accountability shape as
[evidence-report.md](evidence-report.md), one link further:

```
human -> device -> agent -> model -> document hash -> approval
```

## Flow

1. **Submit.** An agent calls `work_product_submit(source_path, title,
   template_pack, tags=..., model=...)`. The tool hashes the file
   (`content_sha256`, SHA-256), validates `tags` against the chosen pack's
   `required_tags`, and stores a `work_products` row with `status =
   'submitted'`. `source_path` is resolved through the workspace sandbox
   (`safety.resolve_in_workspace`): only files inside `UA_WORKSPACE` or the
   caller's active task worktree can be submitted. The row and its
   `work_product.submitted` event commit in one transaction, as does every
   later transition.
2. **Policy -> approval.** The submission always parks a `require_approval`
   request in the existing approval queue (`tools.approvals.
   check_approval_gate`, `force=True`) — the same mechanism
   `managed_secret_set` and `agent_token_issue` already use for other
   high-impact writes. **The submitter can never approve its own
   submission**: `approval_resolve` refuses an approve by the identity that
   requested it for every tool in `approvals.SEPARATION_OF_DUTIES_TOOLS`
   (`work_product_release`), under every profile — including
   `steward`/`chief-of-staff`, which can reach `approval_resolve`. It is also
   denied to the `developer` profile outright
   (`profiles.DEVELOPER_DENIED_TOOLS`). The submitter may still *deny* its own
   request; withdrawing is never an escalation.
   The approval payload shows the reviewer the title, content hash, pack,
   reviewer role and, for a new version, `previous_version_id`.
3. **Resolve.** A human resolves the pending row with the existing
   `approval_resolve` tool (or the `/ui` approval queue). Approving turns it
   into a one-shot pass; `approval_resolve` itself already stamps a signed
   authenticated-producer event (`audit.producer_sign`) binding the
   resolver's verified identity — and, when resolved over the tailnet, the
   calling device — to the approval id.
4. **Retry -> release.** Calling `work_product_submit` again with the
   identical file/title/pack/tags/model finds the same submission (looked up
   by content hash, pack, agent, title, model and canonical tags — changing
   any of them starts a fresh review) and its linked approval. The approval
   payload carries the tags, model and release subfolder, so the pass is
   bound to the metadata and destination the reviewer saw — a pack edited
   after approval asks for a fresh approval rather than redirecting the
   release. Approving requires a named, authenticated approver.
   The released copy is written with exclusive create: an existing
   different file at the destination is never overwritten.
   A replay after release returns the released row with
   `note: "already released"` — no second approval, copy, or event:
   - **Approved:** the pass is consumed, the row transitions `submitted ->
     approved -> released`, the exact bytes are copied to
     `UA_WORK_PRODUCT_RELEASE_DIR/<pack.release_subfolder>/`, the copy is
     re-hashed (never trusting the copy silently), and
     `work_product.approved` / `work_product.released` evidence events are
     appended linking the resolving human, the producing agent label, the
     producing model, the document hash, and the approval id. The release
     event records the *releasing* caller's device as `releasing_device`; the
     approving human's device is never inferred from it (see Evidence).
   - **Denied:** the row transitions to `rejected`, a `work_product.rejected`
     event is appended, and no bytes are ever copied.
   - **Release fails part-way** (source missing, release folder unwritable,
     commit error): the consumed one-shot pass is restored, so the next retry
     completes the *same* approval instead of queueing another one. The pass
     is spent only by a committed release or a tamper verdict.

No separate MCP "release" tool or "resume" flag exists — the retry-consumes-the-
pass idiom already in this codebase does the whole job.

A concurrent retry of the *same* id is refused while the release lock is
held: `_work_product_release_lock` takes a non-blocking `flock` on
`.<db>.work-product-<id>.lock` beside `memory.db` (task #1297). The error
is `work product <id> release is already in progress; retry shortly`.
The flock dies with the process, so an abandoned consume window is
retryable without a lease timeout that could expire under a slow but
healthy release. Two racing denials never append a second rejection
event.

## Tamper detection

`work_product_verify(work_product_id)` (CLI: `unified-agent work-product
verify <id>`) re-hashes the source file and, if released, the released copy,
comparing both against the approved `content_sha256`. Before release the
source must still exist and match — a deleted draft is tampering, since it
was the only copy of the reviewed bytes; after release the released copy is the record, so
an edited *draft* is reported (`source_hash_matches: false`) but is not
tampering, while an altered or missing released copy is. A tamper marks the
row `status = 'tampered'` (idempotently) and appends a `work_product.tampered`
evidence event recording the expected and actual hashes and which stage
caught it (`pre_release`, `post_copy`, or `verify`). `verify` also self-heals
a denied-but-never-retried submission: if the linked approval was denied and
nobody retried `submit`, `verify` notices and records the
rejection immediately, so nothing reads as a stale `submitted` row.

## Evidence

`unified-agent work-product evidence <id>` (`work_product_evidence`) reads the
chain back from the hash-chained ledger rather than from the row:

| Link | Source | Verified when |
| --- | --- | --- |
| human | `approvals.resolved_by` | a signature-valid `agent.evidence.producer_authenticated` event for this approval id names the same identity. `verified_via` is `idp` (WorkOS session) or `agent_bearer` — an agent bearer is never presented as IdP verification (#1217). |
| device | that same signed event's tailnet node | the event carries a resolved node; otherwise `verified: false`, never a bare hostname. |
| agent / model | `producing_agent` / `producing_model` | recorded at submit, in `work_product.submitted`. |
| document hash | `content_sha256` (+ `released_sha256`) | re-checked by `verify`. |
| approval | `approval_id` | the approval row and its producer event. |

Agent, model and document hash are read from the `work_product.submitted` /
`work_product.released` event payloads, never from the mutable row. They are
cross-checked against the row and against the files on disk; any
disagreement or missing event sets `consistent: false` and is listed in
`mismatches`. If the ledger chain fails `verify_event_chain`, no producer
event is trusted. Either condition makes the command exit 1.

`reviewer_role` is descriptive: any identity other than the submitter that
can reach `approval_resolve` may approve. Binding roles to identities is the
role registry's job (#1289). Rows stay readable when
`UA_WORK_PRODUCT_ENABLED=0` on purpose — disabling the write path must not
hide existing evidence.

## Versions and the reviewer diff

Submitting the same `title` under the same pack with different bytes links
the new row's `previous_version_id` to the latest earlier one.
`unified-agent work-product diff <id>` prints a unified diff between the two
for text documents (by MIME type or suffix, up to 1 MiB). Both sides must
still hash to their recorded values — a tampered previous version is never
diffed — and non-text files report `diffable: false` with the reason.

## Template packs

A pack is one YAML file under `src/unified_agent/templates/work_product/`
(shipped inside the package, so the Docker image and installed wheels have
them; override the directory with `UA_WORK_PRODUCT_TEMPLATE_DIR` for a
pilot-specific location).
It declares, and nothing else in this feature ever hardcodes:

| Field | Meaning |
| --- | --- |
| `id` | Must match the filename (`legal.yaml` -> `id: legal`). |
| `title` / `description` | Human-facing labels. |
| `reviewer_role` | Descriptive label for who is expected to approve (authority itself comes entirely from the profile system, not from this field). |
| `required_tags` | Tag keys a submission must supply (e.g. `matter`, `client`). Missing any of them is rejected before anything is hashed or stored. |
| `release_subfolder` | Plain path segment under `UA_WORK_PRODUCT_RELEASE_DIR` the approved copy lands in. |

Two **synthetic** packs ship with this repo as fixtures — no real client,
matter, or system data belongs in them:

- `legal` — `matter`, `client` tags; `supervising_attorney` reviewer role.
- `accessibility-remediation` — `wcag_criterion`, `target_system` tags;
  `accessibility_lead` reviewer role.

## Platform surfaces

- **Object model:** `kind="work_product"`,
  `unified-agent://work-products/{work_product_id}`, registered in
  `object_model.OBJECT_TYPES` (`served_types()` includes it) with lifecycle
  `submitted -> approved -> released` (or `-> rejected` / `-> tampered`).
  Readable over the generic `unified-agent platform get work_product <id>`
  HTTP/CLI surface, gated by the same profile permission as
  `work_product_submit`.
- **Dedicated CLI** (`release` is already the name for versioned *software*
  release artifacts — see `unified_agent/release.py` — so this feature uses
  its own `work-product` namespace instead):
  ```
  unified-agent work-product submit <file> --pack <id> [--title T] [--tag k=v]... [--model M]
  unified-agent work-product submit <hub-path> --pack <id> --hub URL [--token-env NAME]
  unified-agent work-product release <id> --hub URL [--token-env NAME]
  unified-agent work-product verify <id>
  unified-agent work-product list [--status S] [--pack P]
  unified-agent work-product evidence <id>
  unified-agent work-product diff <id>
  unified-agent work-product packs
  ```
  The remote `--hub` mode invokes the mirrored TypeScript SDK consumer from
  a source checkout after building `packages/protocol` and `packages/client`.
  An installed Python wheel uses its HTTP adapter for the same routes. Both
  read the bearer from `UA_AGENT_BEARER_TOKEN` by default and keep it out of
  process arguments. The path on submit is on the hub's workspace, not an
  upload. Release takes only the stored work-product id and uses the same
  producer identity after human approval; a rejected or edited source cannot
  turn an ID-addressed retry into a new submission. A transport interruption
  leaves the outcome unconfirmed, so check hub state before retrying.
- **MCP tool:** exactly one, `work_product_submit` — release is a side effect
  of the retry, not a second tool. It is pruned from the default developer
  profile (Epic B's tool-surface shrink) and classified `WITHHELD` in
  `tool_classification.py`, alongside `agent_token_*` and
  `managed_secret_set`: the same "human/operator-gated write" class.
- **Authenticated HTTP writes:** `POST /api/work-products` accepts
  `source_path`, `title`, `template_pack`, optional string-to-string `tags`,
  and optional `model`. It returns `202` with `work_product_id` and
  `approval_id` when review is pending. After a named human resolves that
  approval, the original producing identity calls
  `POST /api/work-products/{id}/release` with an empty body. The hub reloads
  the submitted path and metadata; callers cannot substitute new inputs on
  release. This returns the released row (`200`), or `202` if approval is
  still pending. Both routes require a verified hub identity, apply the
  browser CSRF guard when a session cookie is used, and invoke the registered
  `work_product_submit` wrapper. The wrapper applies the same profile,
  policy, sandbox, and approval decisions as MCP. The developer profile
  remains denied; an HTTP route does not grant a new privilege.
- **Storage:** the `work_products` table (migration in `storage._migrate`,
  `SCHEMA_VERSION` 32), following the same additive, self-contained pattern
  the `agent_tokens` migration used.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `UA_WORK_PRODUCT_ENABLED` | `0` | Off by default. Set `1` to allow `work_product_submit` and the CLI past the disabled refusal. |
| `UA_WORK_PRODUCT_RELEASE_DIR` | `<UA_HOME>/work_products/released` | Local directory approved copies are written into, under a per-pack subfolder. Local path only for this slice — Drive/SharePoint destinations are an explicit out-of-scope follow-up. |
| `UA_WORK_PRODUCT_TEMPLATE_DIR` | the packs shipped in the package | Where template-pack YAML files are loaded from. |

## Out of scope (this slice)

- A document preview/diff UI in the Inbox's approval detail (`packages/web`) — the
  backend (`work_product_diff`), CLI, and MCP tool land here; the UI is D3 (#1263).
- The task-scoped evidence report (`audit/evidence_report.py`) does not yet
  include work products; the provenance graph (#1287) derives `work_product`
  nodes from the same ledger events.
- Drive/SharePoint (or any non-local) release destinations.
