# Browser → unified-agent projection contract, v1

Status: **implemented for one vertical slice** (focus anchor → task reference).

This document defines the only shape in which local browser cognition may cross
into the unified-agent control plane. It is deliberately narrow: everything not
named here is refused.

## 1. Why a contract at all

A browser producer may capture URLs, page bodies, tab context, dwell traces, and embeddings into local storage. The control plane needs almost none of that —
it needs to know *what the user is working on* so it can relate that to tasks,
memory, and (later, in S3) a peer hub. Without a contract, the natural failure
mode is that some future convenience change spreads an internal record onto the
wire and quietly exports a browsing history.

So the contract inverts the default: **projection is an allowlist, not a
redaction pass.** A field reaches the control plane only if it is named in
`KIND_FIELDS`, in the producer and receiver, deliberately.

## 2. Where the two halves live

| Half | Repo | File |
|---|---|---|
| Projector (transform + minimisation) | Explicitly configured browser producer | Producer-owned version 1 transform |
| Validator (untrusted-input gate) | Unified Agent | `src/unified_agent/projection/browser_v1.py` |

Minimisation happens **at the source**. The projector is the only code that sees
a full URL or a clean-text excerpt. The validator does not trust it: it
independently re-checks every rule and fails closed, because in S3 the sender may
be a different machine.

Unified-Agent has a narrow MCP wrapper,
`browser_derive_task_reference`, which accepts a focus-anchor record,
revalidates it, checks that the target task exists and is non-terminal, and
returns a derived task-reference record. It does not persist the record. The
`browser-projection` connector manifest is descriptive and default-off; it
has no Horizon 2 scopes or connector-local secret. No authenticated browser
client has been verified to call the wrapper, so the connector remains
DORMANT and explicitly cannot be registered or authorized through Horizon 2;
even a legacy registration row cannot make it LIVE or grant a scope without a
transport. The consumer remains unverified. The wrapper is withheld from the
developer, research, operator, and public profiles; it is available only to
steward and chief-of-staff profiles. There is still no transport or deployment
between a producer and this receiver.

### Interop pin

The receiver tests retain the literal version 1 `INTEROP_GOLDEN` record in
`tests/test_browser_projection.py`. Producer integrations must validate that
same record independently. The namespace rename leaves this record, its field
allowlist, and the payload/envelope version unchanged.

## 3. Envelope

Every projected record, of every kind:

```json
{
  "contract_version": "1.0",
  "kind": "focus_anchor",
  "record_id": "fa_me0mrk00_9251127f9a34ba6f",
  "provenance": {
    "origin_machine": "desktop",
    "origin_surface": "browser",
    "captured_at": 1754524800000,
    "projected_at": 1754524801000,
    "consent_basis": "standing_local_capture",
    "trust_tier": "local"
  },
  "payload": { "...kind-specific, allowlisted..." }
}
```

No field may be added at the envelope level without a version bump; unexpected
envelope keys are refused.

### Provenance

| Field | Values | Meaning |
|---|---|---|
| `origin_machine` | free string, ≤64 chars | which machine captured it (`desktop`, `laptop`) |
| `origin_surface` | `browser` \| `desktop` \| `vault` | which sensor produced it |
| `captured_at` | epoch ms, ≥0, finite | when the *user activity* happened |
| `projected_at` | epoch ms, ≥0, finite | when the projection was made |
| `consent_basis` | `explicit_user_action` \| `standing_local_capture` \| `derived_aggregate` | why this may cross the boundary |
| `trust_tier` | `local` \| `remote` \| `vault` | how much the receiver may believe it |

`trust_tier` is the field S3 will lean on: a record arriving over a peer link is
`remote` and must stay visibly `remote` through every downstream surface. Tests
in both repos assert local / remote / vault survive a round trip distinctly.

`origin_machine` is *not* a security claim in v1 — it is self-asserted by the
sender. S3 must bind it to an authenticated machine identity; until then, treat
it as a label, not an assertion.

## 4. Payload allowlists

Structural rule enforced by the validator: **payload values may only be strings
or finite numbers.** No arrays, no nested objects, no blobs. That single rule is
what makes `openTabsContext`, `vectorAnchor`, `userActions`, and
`browsing_history` structurally unrepresentable rather than merely discouraged.

| Kind | Fields | Retention (days) |
|---|---|---|
| `focus_anchor` | `focus_entity`, `intent_category`, `cognitive_load_level`, `state_digest`, `origin_ref` | 30 |
| `cognitive_frame` | + `friction_score`, `dwell_seconds` | 14 |
| `checkpoint` | `goal_title`, `focus_entity`, `state_digest`, `open_context_count`, `origin_ref` | 30 |
| `telemetry` | `cognitive_load_score`, `fatigue_index`, `context_switch_cost`, `intent_category`, `tab_switches_5m`, `session_minutes` | 90 |
| `task_reference` | `task_ref`, `relation`, `confidence`, `focus_entity` | 180 |
| `vault_proposal` | `note_title`, `summary_digest`, `suggested_folder`, `focus_entity` | 30 |

Only `focus_anchor` and `task_reference` have a projector implementation in v1.
The other four kinds are **declared and validated but not yet produced** — their
allowlists are pinned now so that implementing them later is an additive change
reviewed against this table, not a fresh design.

### Size limits

| Limit | Value |
|---|---|
| Whole record, UTF-8 encoded | 8192 bytes |
| `focus_entity`, `goal_title`, `note_title`, `suggested_folder` | 200 chars |
| `state_digest`, `summary_digest` | 280 chars |
| `origin_ref` | exactly 16 hex chars |
| `task_ref` | 64 chars, must match `ua://task/<id>` |
| `origin_machine` | 64 chars |

**Sender clamps, receiver rejects.** The projector truncates free text to the
limits above (that is minimisation working). The validator never truncates — an
oversized record is refused outright, so a silently-shortened record can never be
mistaken for a complete one.

### `origin_ref`: correlation without disclosure

`origin_ref` is the first 16 hex chars of `SHA-256(url)`. It lets the control
plane tell "these two anchors are the same page" without learning which page.

Honest limitation: this is an unsalted hash of a low-entropy value. Anyone
holding a candidate URL list can confirm a guess. It defends against casual
disclosure and log inspection, **not** against a motivated party with a URL
dictionary. If that threat matters later, salt it per-installation — at the cost
of losing cross-machine correlation, which S3 may well want.

## 5. What never crosses

Asserted by tests in both repos, over an adversarial fixture that deliberately
carries all of it:

- raw page text (`textContent`, `cleanTextExcerpt`)
- page titles (`pageTitle`)
- full URLs, in any field, in any scheme — including `file://` and
  `chrome-extension://`
- tab context (`openTabsContext`) and browsing history
- embeddings / `vectorAnchor` of any dimension
- interaction traces (`userActions`)

Free-text fields are additionally swept for URL-shaped substrings: the projector
replaces them with `[redacted-link]`, and the validator *rejects* any string
still containing one. `task_ref` is exempted from the sweep because it carries
the internal `ua://` scheme, and is instead pinned by a far stricter regex.

### The one residual channel — read this before extending the contract

`state_digest` is free text authored by Browser from the user's
`workingStateSummary`. It is the single field on the wire whose contents are not
enumerable in advance. The contract bounds it (280 chars, link-redacted) and pins
its provenance (it is never back-filled from `cleanTextExcerpt` — there is a test
for exactly that), but it cannot guarantee a user's own summary contains nothing
sensitive.

This is a deliberate, documented trade: a projection with no free text is
substantially less useful. Any future kind that adds a *second* free-text field
should be treated as a contract-level decision, not an implementation detail.

## 6. Compatibility and rollback

Version is `MAJOR.MINOR` in `contract_version`.

- **Minor bump** — additive and backward compatible: a new optional payload field
  on an existing kind, or a new kind. The validator accepts any `1.x` where `x`
  is greater than its own, ignoring nothing and refusing unknown fields. So a
  newer sender talking to an older receiver fails *loudly on the new field*
  rather than silently dropping it. That is intentional: silent field-dropping
  across a privacy boundary is how contracts rot.
- **Major bump** — anything else: removing/renaming a field, tightening a limit,
  changing provenance semantics, changing `origin_ref` derivation. A receiver
  refuses a major it does not speak, with an explicit error naming both versions.

**Rollback.** The contract is a pure library in both repos with no storage or
transport. Unified-Agent's wrapper only derives a response and its manifest does
not enable a connector. Nothing persists, no migration is owed, and no running
transport changes behaviour. Re-document rollback if a future consumer adds a
transport or storage; do not infer either from the manifest or wrapper.

## 7. Handoff to S3 (#280)

S3 builds read-only peer transport. What it inherits:

1. `ProjectedRecord` is the only type that should ever cross a peer link. S3's
   wrapper requirement ("every remote result wrapped with source hub, host, peer
   identity, remote tool, retrieval timestamp") composes with `Provenance` —
   extend it, do not replace it, and keep `trust_tier` authoritative.
2. `parse_record()` is already written for untrusted input and fails closed. It
   is the right place to gate an inbound peer payload; it needs no changes to be
   used that way.
3. `trust_tier="remote"` already round-trips distinctly, so S4's labelling
   requirement has a field to render from on day one.
4. **S3 must fix what v1 does not do:** `origin_machine` is self-asserted. Bind
   it to the authenticated machine identity at the transport layer and reject
   records whose claimed origin disagrees with the authenticated peer.
5. The 8 KiB record budget is per-record. S3 owns batch/page limits; do not
   assume a peer response contains one record.

## 8. Test inventory

| Repo | File | Count |
|---|---|---|
| unified_agent | `tests/test_browser_projection.py` | 56 |
| browser | `tests/projection-contract-v1.test.ts` | 28 |

Negative coverage: malformed JSON and non-objects, missing/extra envelope fields,
missing provenance, out-of-vocabulary surface / tier / consent basis, unknown
kind, unknown payload field (parametrised over eight real internal Browser
field names), URL smuggled into free text (four schemes), non-hex `origin_ref`,
oversized record, oversized individual fields, non-finite and negative
timestamps, bad `task_id`, bad `confidence`, and deriving a task reference from a
non-anchor.
