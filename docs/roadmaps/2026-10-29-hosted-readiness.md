# Hosted readiness: accepted direction and bounded delivery

The operator approved the corrections to `unified-agent-direction-brief.md` and
`unified-agent-direction-research.md` on 2026-10-29, with an explicit boundary:
another session owns the GCP work. This page reconciles that direction with
existing implementation and tasks. It does not replace the live task queue,
release approvals, or the [CI coverage contract](../ci-batch-verify.md).
Inspected source baseline: `11eac6729df022d2aedf22147a65c0fdd55b9e91`.

## Delivery boundary

- The recorded host decision is GCP production and Railway staging/reference
  deployment. Tasks #2148 and #2252 own the host and Spot CI runner work.
  Do not alter their branches, scripts, infrastructure, or deployment paths.
- The Mac remains the authority until a separately accepted cutover. A
  healthy cloud process, merged deployment PR, or backup is not cutover proof.
- Preserve claims and one writer per file. This change owns only the new
  read-only coverage verifier, its tests, this page, and its changelog fragment.
- Continue demonstrated serious privacy fixes. There is no P0-only freeze,
  redaction line-count target, removal of tenant fences, new telemetry egress,
  credential rollout, or scheduler change in this delivery.
- Keep at most two implementation streams as the planning target. Measure
  the proposed ten-event CI/approval windows alongside approved work; those
  windows are not a prerequisite for all other implementation.

## Reuse the implementation and its acceptance tasks

| Concern | Existing implementation | Remaining work / task |
|---|---|---|
| CI coverage | Exact-head or verified-batch CI, with member ancestry and final-tree checks | #2254 adds a read-only machine-readable proof; #2229 retains runner-independent release enforcement and measurement follow-up. |
| Public evidence verification | Evidence CLI export/verify, HMAC sidecars, producer claims, anchors | #1674: vetted asymmetric signing, trusted public-key history, self-contained chain/anchor proofs and offline verification. |
| Telemetry | Typed spans, projection, redaction and OTLP mapping | #2255: allowlisted export boundary, secret references, attributable approval/device/policy/artifact metadata when known. |
| MCP approvals | `elicitation.py` already implements MRTR, capability checks, one-time challenges and resolver authority | #2256: negotiated URL-mode gap and real-client acceptance; preserve the existing gate and legacy fallback. |
| Ledger publication | Resolution fast path and active-tick polling decisions already exist | #2123 / #1867: actual scheduler acceptance, bounded signed webhook ingress if needed, poll reconciliation and measured latency. |
| Scoped credentials | `session_credentials.py` and the flag-off #1744 first phase already exist | #1744: approved rollout evidence and a reviewed hosted-origin/possession-binding contract; no second token store. |
| Recovery / execution | Backup/restore helpers, node reconciliation and acceptance machinery | #1279 restore and rollback, #2112 owner/operator device acceptance. The host session owns any GCP execution. |

The broad durable-run/effect, usage and budget foundation order remains in the
[October 7 roadmap](2026-10-07-meta-harness-priorities.md). This direction does
not make unknown effects successful, retry uncertain effects as new work, or
treat missing cost as zero.

## Read-only CI coverage proof (#2254)

`scripts/verify_ci_coverage.py` uses authenticated GitHub **GET** requests via
`gh`. It submits no build, changes no Git ref/status, and reads no secret
values. Supply full SHA pins from the release packet; a moving branch name
or abbreviated SHA is not an evidence pin.

Before a merge, with the actual PR, current base, and tested revisions:

```bash
python3 scripts/verify_ci_coverage.py --repo OWNER/REPO --pr NUMBER \
  --head FULL_PR_HEAD_SHA --base FULL_CURRENT_BASE_SHA --tested FULL_TESTED_SHA
```

After the complete batch merges, additionally supply the expected **current**
base-branch revision. This verifies tree equality even when squash/merge
commits differ from the tested commit:

```bash
python3 scripts/verify_ci_coverage.py --repo OWNER/REPO --pr NUMBER \
  --head FULL_PR_HEAD_SHA --base FULL_INTEGRATION_BASE_SHA \
  --tested FULL_TESTED_SHA --merged FULL_MERGED_BASE_BRANCH_SHA
```

The proof checks:

1. The current PR still has the expected head and belongs to the target repo;
   it is open before merging or actually merged for the final-tree check.
2. The current target branch matches the supplied pre-merge or post-merge pin.
   The tested commit contains both the PR head and integration base.
3. The complete, bounded commit-status history contains successful `CI / gates`,
   four `CI / test (shard=N)` statuses, and `CI / full-verification`. The newest
   status id wins even when API results arrive out of order. Failed or pending
   optional JS/native statuses also refuse coverage.
4. Those statuses reference one common HTTPS CI run. Green shards from
   unrelated runs cannot be combined into a passing proof.
5. With `--merged`, the current merged tree equals the tested tree.
6. PR/branch pins and selected status evidence remain consistent across a
   second read. Missing, malformed, truncated or unreadable evidence fails.

Use `--require 'CI / js'` and/or `--require 'CI / desktop-core'` when those
legs must be observed. Absent optional statuses are reported as
`not_observed`; absence cannot prove the submitted classifier skipped them.
The tool deliberately requires all four Python shards. A valid classified
docs-only run with fewer shards is outside this first verifier's contract.
Check-run-only GitHub Actions/Depot CI is also outside this first contract;
it is refused rather than silently treating missing commit statuses as green.

Exit codes: **0** coverage observed, **1** readable evidence refuses coverage,
**2** evidence unavailable or malformed. Output is JSON with full pins,
successful status ids, optional unobserved contexts, and an observation time.
It is a snapshot of GitHub assertions, not independently verified build
provenance. It cannot certify review, human approval, the authenticity of a
human presence claim, or release authorization. Recheck the head/base at the
actual merge and retain the existing pinned steward release procedure.

Focused verification:

```bash
uv run pytest tests/test_verify_ci_coverage.py -q -n 0
```

The initial live read-only check used PR #1325 at
`ee5c05dc8f3681b22616278b4c21ee285c211aa2`, integration base
`ee83826f168aaf8e97b46bf45b07f5c6c5052674`, and tested/merged revision
`11eac6729df022d2aedf22147a65c0fdd55b9e91`. It observed the six required
successful statuses and tree `806f05f543c946425b253a33fbc9939b68a522d9`.
JS/native execution was unobserved. This dated check is not coverage for
the PR that introduces the verifier itself.

## Acceptance corrections for subsequent slices

- **Evidence:** verifiers need an independently trusted key/root, key validity
  and revocation history, and the exact segment connecting anchors to the
  exported head. Test substituted keys, removed/reordered events, changed
  manifests, rotated credentials, and unknown trust. A bundle can prove the
  integrity of recorded events without proving every real action was captured.
  Retain the distinction between credential attribution and human presence.
- **Privacy:** allowlisted field names do not make arbitrary string values
  safe. Keep free-form arguments/results out by default, bound any explicit
  opt-in, preserve existing redaction across affected sinks, and use synthetic
  secret fixtures. A serializer migration is not a waiver for existing leaks.
- **MCP:** MRTR standardizes client retries. URL `accept` is not the server-side
  approval decision. Bind resumptions to principal, request/payload/artifact,
  expiry and one-time consumption. Do not assume installed clients support it.
- **Credentials:** device proof at issuance does not prevent replay of a stolen
  bearer. Claim possession binding only with a verified per-request proof
  contract. Keep issuer/audience, task scope, revocation and legacy behavior
  explicit. Do not weaken the current public-host refusal as a shortcut.
- **Operations:** require restore and upgrade rollback evidence, bounded
  disconnect/reconnect behavior, cancellation/revocation, and prevention of
  duplicate effects. Fence the old authority during cutover. Preserve human
  device-acceptance attestation; matching code SHAs do not replace it.
- **Tenancy and product claims:** one hub per org is the initial deployment
  model; enforce it and retain authorization fences. Document bypass paths
  such as direct shell/provider access that the hub cannot mediate. Validate
  org-owned artifact/device evidence with buyers before claiming market
  uniqueness. PR counts are investigation signals, not productivity measures.

The public-key, telemetry, client and credential work remains open on the
linked tasks. No new execution slots, signing keys, hosted flags or cloud
resources are activated by this document.
