# Evidence guide

How to export the record of an approved release, check that it has not been
tampered with, and understand exactly what the record does and does not
prove. Written for a COO or risk owner, not a developer — every command
below is copy-pasteable, and every claim is checked against
[evidence-report.md](../evidence-report.md) and
[work-product-release.md](../work-product-release.md), the reference docs
this guide summarizes.

## What the evidence record is

Every release under the H2 work-product workflow is backed by a chain:

```
human -> device -> agent -> model -> document hash -> approval
```

That chain is read back from the same tamper-evident, hash-chained log every
other action in the system writes to — nothing about it is a separate,
lower-trust log. `unified-agent work-product evidence <id>` prints it for
one document; the platform-wide `unified-agent evidence` command (below)
prints it across a task, a time window, or an agent.

## Exporting a record

```bash
# One document's chain (human, device, agent, model, hash, approval)
unified-agent work-product evidence <work_product_id>

# The platform-wide evidence report — everything tied to a task
unified-agent evidence --task <task_id> --markdown
unified-agent evidence --task <task_id> --json --out /path/to/evidence.json

# With signing configured (ask your hub operator whether it is), the JSON/
# Markdown/text export also carries a detached signature proving the
# exported file itself was not altered after it was generated.
export UA_EVIDENCE_SIGNING_KEY='operator-protected-value'
unified-agent evidence --task <task_id> --json --out /path/to/evidence.json
# writes a sibling /path/to/evidence.json.sig.json alongside it
```

## Verifying an export

```bash
unified-agent evidence verify /path/to/evidence.json
```

This checks the detached signature over the exported file and, for an
anchored JSON report, verifies the external anchor. If the anchor covers
an older head, it also checks the link to the report head against the local
ledger when that ledger is available. It does not independently validate
the embedded `chain_integrity` result in an unanchored report. Check that
result separately before treating the underlying chain as sound (see
"Reading the report" below).

Exit codes: `0` valid, `1` a check failed, `2` a usage or configuration
problem (for example, the export is JSON but a required certificate for
anchor verification was not supplied). Only a JSON export carries the full
external-anchor proof; a Markdown or text export carries only a summary,
since those formats cannot hold a full cryptographic token. The CLI does
not offer CSV or PDF for this command.

## The external anchor (RFC-3161 / signed commit)

A signature on an export proves the file was not edited *after* it was
written. It says nothing about whether someone with database access could
have rewritten the underlying log *before* generating the export. The
**anchor** closes that gap: periodically, the operator has the current state
of the log witnessed by something they do not solely control.

Two backends are available (in addition to a lower-assurance default kept
for zero-config installs — ask your hub operator which one is configured):

- **`tsa`** — an RFC-3161 Time-Stamp Authority signs the log's current state
  with a key the operator does not hold. This is the standard timestamping
  protocol used across the software industry for exactly this purpose.
- **`git`** — the current state is committed to a separate git repository
  (never the workspace repo) and signed with the operator's own,
  independently configured signing key.

If your organization needs a third party (an auditor, a customer, a
regulator) to independently confirm the log has not been rewritten, ask your
hub operator which anchor backend is configured and for the piece each one
needs to verify externally: for `tsa`, the pinned Time-Stamp Authority
certificate; for `git`, the witness repository and the expected signer.
`unified-agent evidence verify` performs this check automatically when it
has what it needs; a third party can also verify a `tsa` anchor entirely on
their own with standard `openssl ts -verify` tooling, without needing access
to your hub at all.

## What `provenance_verified` means — and does not mean

Two evidence surfaces answer different questions:

- `unified-agent work-product verify <id>` re-hashes the document and its
  released copy. `unified-agent work-product evidence <id>` checks the
  work-product ledger chain, file consistency, and whether a valid producer
  signature names the same account as the approval resolver. Its
  `chain.human.verified` field is a **signature match**. This command does
  not classify that account as a human or return `provenance_verified`.
- The platform evidence report (`unified-agent evidence --task <task_id>`)
  classifies signed identities as `actor_kind: human`, `agent`, `unknown`,
  or `conflict`. Its `human_provenance.verified` and
  `human_authority: "credential-attributed"` mean that a valid signature
  came from a credential bound to an identity classified as **human**. That
  proves the credential, not that a human was present. A signed agent
  identity remains real actor evidence but does not become human
  authorization.

A human identity may be verified with a static account bearer
(`verified_via: "agent_bearer"`) or a WorkOS session (`verified_via:
"idp"`). Both can satisfy the platform report's human-authority bit when
the signature and identity classification pass. The IdP session provides
stronger independent assurance of who used the account; the bearer proves
control of its credential. Neither label by itself proves that the
reviewer read the document or that the organization met a legal standard.

Read `human_authority: "unknown"` or `"conflict"` literally: the report
does not establish a single verified human authority. Check the individual
record's `actor_kind`, signature, and `verified_via` before making a claim
about a reviewer. A document hash can still verify when attribution is
unknown, and attribution can verify while the released copy has changed.

## Live demo anchor gate

For a live five-link demo, use a synthetic work product that has completed the
real approval and release flow. Do not treat a task report's `clean_pass` as
proof of an individual human or device: it summarizes ledger-chain integrity
and platform-wide `claimed_by` coverage for its selected time window. Inspect
the work-product evidence for that specific id and require `consistent: true`,
`chain.human.verified: true`, a stated `chain.human.verified_via`, and
`chain.device.verified: true` with a device node. The signature must be for the
approval id on that work product and name the approval resolver. Keep the
`verified_via` distinction visible; a bearer signature proves control of that
credential, while an IdP session supplies stronger independent evidence of
human presence.

An operator must create the anchor by submitting a synthetic document,
resolving its pending request as the named human, and retrying release as the
original producer. Do not approve the request as an agent or substitute a
fixture. If no live anchor passes those checks, use the honest fallback: show
the chain-integrity status and identify any human or device link that remains
unverified.

## Reading the report

`unified-agent evidence --task <task_id> --markdown` renders a table you can
hand to someone without JSON-parsing tools. Key fields to look for per
record:

| Field | What it tells you |
| --- | --- |
| `human` / `human_provenance.verified` | Who the record credits, and whether that credit meets the human-verified bar above. |
| `device` / `device_link.verified` | Which machine the action ran from, if the network could confirm it. |
| `proof.tamper_evident` | Whether this record's log entries pass the hash-chain check. |
| `chain_integrity.status` | `valid` or `tampered` for the whole export, not just one record. |
| `anchor` | Present only when the operator has anchoring configured; shows the backend and whether it verified. |

See [evidence-report.md](../evidence-report.md) for the full field reference
and JSON schema if you need to build your own report against the exported
data.
